#!/usr/bin/env python
"""
Read-only calibration study of Kalshi's own 15-minute crypto prices.

QUESTION: are KXBTC15M / KXETH15M prices miscalibrated at scale? I.e. does a
contract quoted at 30c actually settle YES ~30% of the time, or systematically
more/less? If the market itself is biased in a stable, size-able way, that is a
tradeable edge that needs NO fair-value model at all -- it is measured directly
off the exchange's own prices and its own settlements.

WHAT IT DOES
  1. Enumerates every market in each series whose close_time falls in the last
     --days days (default 30) and that Kalshi has SETTLED.
  2. For each such market, pulls 1-minute candlesticks over the last ~12 minutes
     of its life and samples the YES price at T-10min, T-5min and T-2min before
     close (configurable via --slices).
  3. Buckets those samples into 5-cent price buckets and, per (series, slice,
     bucket), reports n / implied probability (bucket midpoint) / realized YES
     settle rate / after-fee EV per contract of buying the cheaper side.
  4. Flags buckets where |realized - implied| >= --flag-threshold with n >= --min-n.

READ-ONLY. This script calls exactly three KalshiClient methods, all reads:
get_events(), get_market(), get_candlesticks(). It NEVER calls place_order() /
cancel_order() and must never be edited to. It writes nothing but its own local
cache and (optionally) a CSV.

CACHING
Every raw API response is cached in a local sqlite file (--cache, default
data/calibration_study.sqlite) keyed by ticker (+ time window for candles), so a
rerun costs no network calls at all and you can iterate on the ANALYSIS without
re-paying for the FETCH. Empty candle responses are cached too -- "this market
never had candles" is a real, stable answer worth remembering. Use --refresh to
force a refetch of everything.

RATE LIMITS
KalshiClient already token-buckets reads at cfg.rate_limit_reads. On top of that
this script sleeps --sleep seconds (default 0.25) between markets and retries
429s with exponential backoff, matching scripts/pull_history.py's policy. A full
uncached --days 30 run over both series is ~5,800 markets, so budget roughly
30-60 minutes; a cached rerun is seconds.

SAMPLED PRICE (what "the YES price" means here)
Preference order per candle, first available wins:
  1. mid of (yes_bid.close_dollars, yes_ask.close_dollars) -- the honest implied
     probability, and the only one with no side bias.
  2. price.close_dollars -- last TRADE that minute; used only when the book
     wasn't two-sided that period.
An all-zero yes_bid ("0.0000") means no resting bid was observed that period,
NOT that the bid was zero (see KalshiClient.get_candlesticks' docstring), so
those are treated as missing rather than as a 0c bid. Samples are strictly
causal: for target time T the candle used is the latest one whose
end_period_ts <= T, never one that ends after it.

FEES
Kalshi taker fee, rounded UP to the next cent ONCE PER ORDER, with the contract
count INSIDE the ceiling:
    order_fee = ceil(100 * coeff * multiplier * C * P * (1 - P)) / 100
The per-contract cost is that divided by C, so it depends on the order size: at
P=0.70 it is 2.00c at C=1, 1.50c at C=10, 1.47c at C=200. C is therefore an
explicit input (--contracts-per-order, default 10, pre-committed in
KILL_CRITERIA.md 2) and is never silently 1. Note P*(1-P) is symmetric, so YES
at P and NO at 1-P pay the same fee.

`multiplier` is Kalshi's per-series `fee_multiplier`, which is NOT always 1
(observed 0, 0.5, 1). Read it with KalshiClient.get_series(). KXBTC15M and
KXETH15M both read 1, so the 0.07 base rate is correct for the studied series.

Usage:
    python scripts/market_calibration_study.py --days 30
    python scripts/market_calibration_study.py --days 30 --series KXBTC15M
    python scripts/market_calibration_study.py --days 7 --slices 10,5,2,1 --csv out.csv
    python scripts/market_calibration_study.py --days 30 --refresh    # ignore cache

CAVEATS -- read before trusting a number out of this
  - Settlement comes from Kalshi's own record (`result`, else `expiration_value`
    vs the market's strike -- see pull_history.resolved_yes_from_market). No
    Coinbase-spot fallback is used here: this study is ABOUT the exchange's own
    pricing vs its own settlement, so inferring outcomes from a different index
    would contaminate exactly the quantity being measured.
  - A settle rate measured off mid prices is not the same as a fill. The EV
    column assumes you buy at the sampled price; crossing the real spread is
    worse. Treat EV as an upper bound, and check the spread separately before
    concluding anything is tradeable.
  - Survivorship: markets with no two-sided quote and no trade in a given minute
    contribute no sample for that slice. Thin markets are therefore
    under-represented -- coverage counts are printed per slice so you can see
    how much was dropped.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kbot.config import settings  # noqa: E402
from kbot.kalshi_client import KalshiClient  # noqa: E402

# Reuse the settlement hierarchy rather than re-deriving it: getting
# expiration_value's semantics wrong (it is a PRICE, not a $1/$0 payout) is the
# single easiest way to silently invert this entire study.
from pull_history import resolved_yes_from_market  # noqa: E402

_DEFAULT_SERIES = ["KXBTC15M", "KXETH15M"]
_DEFAULT_SLICES = [10, 5, 2]          # minutes before close
_BUCKET_WIDTH = 0.05                  # 5-cent price buckets
_N_BUCKETS = int(round(1.0 / _BUCKET_WIDTH))

_EVENTS_PER_PAGE = 200
_EVENTS_PER_DAY = 96                  # 15-minute series create 4 events/hour
_EVENT_PAGE_MARGIN = 2

_CANDLE_PERIOD_INTERVAL = 1           # 1-minute candles (API allows 1/60/1440)
_CANDLE_LEAD_MINUTES = 2              # fetch this much before the earliest slice

_CANDLES_MAX_ATTEMPTS = 5
_CANDLES_INITIAL_BACKOFF_SECONDS = 2.0
_CANDLES_MAX_BACKOFF_SECONDS = 30.0

_PROGRESS_PRINT_EVERY = 100


# ── Pure helpers ─────────────────────────────────────────────────────────────

def parse_iso_ts(value: str | None) -> int | None:
    """Unix seconds from a Kalshi ISO-8601 timestamp string, or None if unparseable/missing."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def _dollars(raw: object) -> float | None:
    """Float from a Kalshi dollar-string field ("0.0100"), or None if absent/unparseable."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def yes_price_from_candle(candle: dict) -> tuple[float | None, str]:
    """
    (yes_price_dollars, source) for one candlestick, or (None, reason).

    Source is "mid" when a genuine two-sided quote existed that period, else
    "trade" when only a trade print is available. An all-zero bid or ask means
    "not observed", not a real 0c quote -- see this module's docstring.
    """
    yes_bid = _dollars((candle.get("yes_bid") or {}).get("close_dollars"))
    yes_ask = _dollars((candle.get("yes_ask") or {}).get("close_dollars"))
    if yes_bid is not None and yes_ask is not None and yes_bid > 0.0 and yes_ask > 0.0:
        mid = (yes_bid + yes_ask) / 2.0
        if 0.0 < mid < 1.0:
            return mid, "mid"

    last = _dollars((candle.get("price") or {}).get("close_dollars"))
    if last is not None and 0.0 < last < 1.0:
        return last, "trade"

    return None, "no_quote"


def candle_at_or_before(candles: list[dict], target_ts: int) -> dict | None:
    """
    The latest candle whose end_period_ts <= target_ts, or None if none exists.

    Strictly causal by construction: a candle ending after `target_ts` contains
    price information from after the sample time, and using it would leak the
    future into a T-minus-N measurement. `candles` must be sorted ascending by
    end_period_ts (sort_candles does that).
    """
    if not candles:
        return None
    idx = bisect.bisect_right(candles, target_ts, key=lambda c: c.get("end_period_ts") or 0)
    if idx == 0:
        return None
    return candles[idx - 1]


def sort_candles(candles: list[dict]) -> list[dict]:
    """Candles sorted ascending by end_period_ts, dropping any without one."""
    return sorted(
        (c for c in candles if c.get("end_period_ts") is not None),
        key=lambda c: c["end_period_ts"],
    )


def order_fee(
    price_dollars: float,
    contracts: int,
    coefficient: float = 0.07,
    multiplier: float = 1.0,
) -> float:
    """
    Kalshi taker fee for ONE ORDER of `contracts` contracts, in dollars:
        ceil(100 * coeff * multiplier * C * P * (1 - P)) / 100

    The ceiling applies ONCE, at the order level, with C inside it -- not once
    per contract. The earlier form here fixed C at 1, which overstated the fee
    by up to 0.9c/contract in the sub-15c zone and by 0.53c at P=0.70 (see
    KILL_CRITERIA.md 9, 2026-08-21).

    Deliberately NOT kbot.edge.fee_per_contract, which returns a raw unrounded
    float with no ceiling at all. This study reports realized cash EV, so it
    charges the fee the exchange actually charges. The 1e-9 nudge absorbs binary
    float error so an exact-cent fee doesn't round up a whole extra cent.

    `multiplier` is the per-series `fee_multiplier` (0, 0.5 and 1 all occur in
    the wild); 1.0 is correct for KXBTC15M and KXETH15M but must be read from
    the API for any other series.
    """
    if contracts < 1:
        raise ValueError(f"contracts must be >= 1, got {contracts}")
    raw_cents = (
        coefficient * multiplier * contracts
        * price_dollars * (1.0 - price_dollars) * 100.0
    )
    return math.ceil(raw_cents - 1e-9) / 100.0


def fee_per_contract(
    price_dollars: float,
    contracts: int,
    coefficient: float = 0.07,
    multiplier: float = 1.0,
) -> float:
    """
    Per-contract share of the order-level fee: order_fee(...) / contracts.

    `contracts` is REQUIRED. An order-level ceiling is undefined without an
    order size, and defaulting it to 1 is exactly the bug this replaced.
    """
    return order_fee(price_dollars, contracts, coefficient, multiplier) / contracts


def cheaper_side(yes_price: float) -> tuple[str, float]:
    """
    (side, entry_price) for buying whichever side of this market is cheaper.

    YES at exactly 0.50 is a tie; YES is taken, arbitrarily but consistently --
    the fee and payoff are symmetric there, so the choice only matters for
    labelling, not for EV.
    """
    if yes_price <= 0.5:
        return "YES", yes_price
    return "NO", 1.0 - yes_price


def observation_ev(
    yes_price: float,
    settled_yes: bool,
    coefficient: float,
    contracts: int,
) -> float:
    """
    Realized after-fee P&L per contract from buying the cheaper side at
    `yes_price` in an order of `contracts` contracts, held to settlement:
        payout (1 if that side won, else 0) - entry price - fee/contract

    Both `coefficient` and `contracts` are required: the fee depends on the
    order size, so there is no defensible default EV.
    """
    side, entry = cheaper_side(yes_price)
    won = settled_yes if side == "YES" else not settled_yes
    return (1.0 if won else 0.0) - entry - fee_per_contract(entry, contracts, coefficient)


def bucket_index(price: float) -> int:
    """5-cent bucket index 0..19 for a price in (0, 1). 1.00 clamps into the top bucket."""
    return min(int(price / _BUCKET_WIDTH), _N_BUCKETS - 1)


def bucket_bounds(index: int) -> tuple[float, float]:
    """(low, high) price bounds of bucket `index`."""
    return index * _BUCKET_WIDTH, (index + 1) * _BUCKET_WIDTH


def bucket_midpoint(index: int) -> float:
    """The implied YES probability this study attributes to a bucket."""
    low, high = bucket_bounds(index)
    return (low + high) / 2.0


def bucket_label(index: int) -> str:
    low, high = bucket_bounds(index)
    return f"{low:.2f}-{high:.2f}"


def estimated_event_pages(days: int) -> int:
    """
    Pages of /events to follow to plausibly cover `days` of a 15-minute series.

    Events come back newest-first, so reaching a `days`-old event means paging
    through everything more recent first. Under-estimating this is SILENT -- the
    older part of the window just quietly goes missing (this exact bug bit
    pull_history twice), hence the deliberate margin.
    """
    return max(1, math.ceil(days * _EVENTS_PER_DAY / _EVENTS_PER_PAGE) + _EVENT_PAGE_MARGIN)


# ── Cache ────────────────────────────────────────────────────────────────────

class StudyCache:
    """
    Local sqlite cache of raw API responses, so reruns cost zero network calls.

    Stores responses VERBATIM (as JSON text) rather than parsed/derived values:
    the whole point is that changing the analysis -- different slices, a
    different price definition -- must never require refetching.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS markets (
                ticker     TEXT PRIMARY KEY,
                series     TEXT NOT NULL,
                close_ts   INTEGER,
                raw_json   TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candles (
                ticker          TEXT NOT NULL,
                start_ts        INTEGER NOT NULL,
                end_ts          INTEGER NOT NULL,
                period_interval INTEGER NOT NULL,
                raw_json        TEXT NOT NULL,
                fetched_at      INTEGER NOT NULL,
                PRIMARY KEY (ticker, start_ts, end_ts, period_interval)
            );
            """
        )
        self._db.commit()

    # -- markets ------------------------------------------------------------
    def get_market(self, ticker: str) -> dict | None:
        row = self._db.execute("SELECT raw_json FROM markets WHERE ticker = ?", (ticker,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_market(self, ticker: str, series: str, close_ts: int | None, market: dict) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO markets (ticker, series, close_ts, raw_json, fetched_at) VALUES (?,?,?,?,?)",
            (ticker, series, close_ts, json.dumps(market), int(time.time())),
        )
        self._db.commit()

    def cached_markets(self, series: str, start_ts: int, end_ts: int) -> dict[str, dict]:
        """Cached market records for `series` closing in [start_ts, end_ts]."""
        rows = self._db.execute(
            "SELECT ticker, raw_json FROM markets WHERE series = ? AND close_ts BETWEEN ? AND ?",
            (series, start_ts, end_ts),
        ).fetchall()
        return {ticker: json.loads(raw) for ticker, raw in rows}

    # -- candles ------------------------------------------------------------
    def get_candles(self, ticker: str, start_ts: int, end_ts: int, period: int) -> list[dict] | None:
        row = self._db.execute(
            "SELECT raw_json FROM candles WHERE ticker=? AND start_ts=? AND end_ts=? AND period_interval=?",
            (ticker, start_ts, end_ts, period),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put_candles(self, ticker: str, start_ts: int, end_ts: int, period: int, candles: list[dict]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO candles (ticker, start_ts, end_ts, period_interval, raw_json, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (ticker, start_ts, end_ts, period, json.dumps(candles), int(time.time())),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


# ── Network I/O (all read-only endpoints) ────────────────────────────────────

def enumerate_settled_markets(
    client: KalshiClient,
    cache: StudyCache,
    series: str,
    start_ts: int,
    end_ts: int,
    *,
    refresh: bool,
) -> dict[str, dict]:
    """
    ticker -> market record for every `series` market closing in
    [start_ts, end_ts], from GET /events (with nested markets).

    Served entirely from cache when the cache already holds markets for this
    window and --refresh wasn't passed. The nested market records carry
    `result` / `expiration_value`, so no per-ticker call is needed for the ones
    Kalshi has already populated.
    """
    if not refresh:
        cached = cache.cached_markets(series, start_ts, end_ts)
        if cached:
            print(f"  [{series}] {len(cached)} markets served from cache (use --refresh to refetch).")
            return cached

    pages = estimated_event_pages(max(1, math.ceil((end_ts - start_ts) / 86400)))
    print(f"  [{series}] fetching events (up to {pages} pages of {_EVENTS_PER_PAGE})...")
    events = client.get_events(series_ticker=series, limit=_EVENTS_PER_PAGE, max_pages=pages)

    markets: dict[str, dict] = {}
    for ev in events:
        for market in ev.get("markets") or []:
            ticker = market.get("ticker")
            if not ticker:
                continue
            close_ts = parse_iso_ts(market.get("close_time"))
            if close_ts is None or not (start_ts <= close_ts <= end_ts):
                continue
            markets[ticker] = market
            cache.put_market(ticker, series, close_ts, market)

    print(f"  [{series}] {len(events)} events -> {len(markets)} markets closing in window.")
    return markets


def resolve_settlement(
    client: KalshiClient,
    cache: StudyCache,
    series: str,
    ticker: str,
    market: dict,
    *,
    allow_refetch: bool,
) -> bool | None:
    """
    True/False if Kalshi settled `ticker` YES/NO, else None (not settled, or
    settlement fields missing).

    The nested event record answers this for almost every settled market. When
    it doesn't, GET /markets/{ticker} is retried once per ticker (its record is
    refreshed independently and often carries `result` when the cached nested
    copy predates settlement) and the fresher record replaces the cached one.
    No spot-price fallback -- see this module's CAVEATS.
    """
    outcome = resolved_yes_from_market(market)
    if outcome is not None or not allow_refetch:
        return outcome

    try:
        fresh = client.get_market(ticker)
    except Exception as exc:  # noqa: BLE001 -- one bad ticker must not kill the run
        print(f"  WARNING: get_market({ticker}) failed ({type(exc).__name__}) -- treating as unsettled")
        return None
    if not fresh:
        return None

    cache.put_market(ticker, series, parse_iso_ts(fresh.get("close_time")), fresh)
    return resolved_yes_from_market(fresh)


def fetch_candles_cached(
    client: KalshiClient,
    cache: StudyCache,
    series: str,
    ticker: str,
    start_ts: int,
    end_ts: int,
    *,
    refresh: bool,
    sleep_seconds: float,
) -> list[dict]:
    """
    1-minute candlesticks for `ticker` over [start_ts, end_ts], cached.

    An EMPTY result is cached too: "this market produced no candles" is a
    stable fact about a settled market, and re-asking every rerun would burn
    the majority of the rate-limit budget on exactly the markets with no data.
    Retries 429 with exponential backoff (KalshiClient.get_candlesticks is
    deliberately unretried); a market still limited after all attempts yields
    an empty list and is NOT cached, so a later rerun picks it up.
    """
    if not refresh:
        cached = cache.get_candles(ticker, start_ts, end_ts, _CANDLE_PERIOD_INTERVAL)
        if cached is not None:
            return cached

    backoff = _CANDLES_INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _CANDLES_MAX_ATTEMPTS + 1):
        try:
            candles = client.get_candlesticks(
                series, ticker,
                start_ts=start_ts, end_ts=end_ts,
                period_interval=_CANDLE_PERIOD_INTERVAL,
            )
            cache.put_candles(ticker, start_ts, end_ts, _CANDLE_PERIOD_INTERVAL, candles)
            time.sleep(sleep_seconds)
            return candles
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                print(f"  WARNING: {ticker} candles HTTP {exc.response.status_code} -- skipping")
                return []
            if attempt == _CANDLES_MAX_ATTEMPTS:
                print(f"  WARNING: {ticker} still rate-limited after {_CANDLES_MAX_ATTEMPTS} attempts -- skipping")
                return []
            print(f"  429 for {ticker}, retrying in {backoff:.0f}s (attempt {attempt}/{_CANDLES_MAX_ATTEMPTS})...")
            time.sleep(backoff)
            backoff = min(backoff * 2, _CANDLES_MAX_BACKOFF_SECONDS)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: {ticker} candles failed ({type(exc).__name__}: {str(exc)[:120]}) -- skipping")
            return []
    return []


# ── Study ────────────────────────────────────────────────────────────────────

class Observation:
    """One (market, time-slice) price sample paired with its settled outcome."""

    __slots__ = ("series", "ticker", "close_ts", "slice_minutes", "yes_price", "source", "settled_yes")

    def __init__(
        self,
        series: str,
        ticker: str,
        close_ts: int,
        slice_minutes: int,
        yes_price: float,
        source: str,
        settled_yes: bool,
    ) -> None:
        self.series = series
        self.ticker = ticker
        self.close_ts = close_ts
        self.slice_minutes = slice_minutes
        self.yes_price = yes_price
        self.source = source
        self.settled_yes = settled_yes


def sample_market(
    series: str,
    ticker: str,
    close_ts: int,
    candles: list[dict],
    settled_yes: bool,
    slices: list[int],
) -> list[Observation]:
    """One Observation per slice that has a usable causal price sample."""
    ordered = sort_candles(candles)
    observations: list[Observation] = []
    for minutes in slices:
        target = close_ts - minutes * 60
        candle = candle_at_or_before(ordered, target)
        if candle is None:
            continue
        price, source = yes_price_from_candle(candle)
        if price is None:
            continue
        observations.append(Observation(series, ticker, close_ts, minutes, price, source, settled_yes))
    return observations


def aggregate(observations: list[Observation], coefficient: float, contracts: int) -> dict:
    """
    (series, slice, bucket) -> {n, implied, realized, ev, mean_price}.

    `implied` is the bucket MIDPOINT (the study's stated convention) while
    `mean_price` is the actual average price in the bucket -- if the two drift
    apart the bucket is lopsided and its midpoint-based EV is optimistic, so
    both are reported.
    """
    grouped: dict[tuple[str, int, int], list[Observation]] = defaultdict(list)
    for obs in observations:
        grouped[(obs.series, obs.slice_minutes, bucket_index(obs.yes_price))].append(obs)

    stats: dict[tuple[str, int, int], dict] = {}
    for key, group in grouped.items():
        n = len(group)
        realized = sum(1 for o in group if o.settled_yes) / n
        ev = sum(observation_ev(o.yes_price, o.settled_yes, coefficient, contracts) for o in group) / n
        stats[key] = {
            "n": n,
            "implied": bucket_midpoint(key[2]),
            "realized": realized,
            "ev": ev,
            "mean_price": sum(o.yes_price for o in group) / n,
        }
    return stats


def print_tables(
    observations: list[Observation],
    stats: dict,
    series_list: list[str],
    slices: list[int],
    min_n: int,
    flag_threshold: float,
    coefficient: float,
    contracts: int,
) -> list[tuple]:
    """Print one calibration table per (series, slice). Returns the flagged rows."""
    flagged: list[tuple] = []

    for series in series_list:
        series_obs = [o for o in observations if o.series == series]
        if not series_obs:
            print(f"\n=== {series}: no usable observations ===")
            continue

        markets = len({o.ticker for o in series_obs})
        print(f"\n{'=' * 92}")
        print(f"=== {series} -- {markets} settled markets, {len(series_obs)} price samples")
        print(f"{'=' * 92}")

        for minutes in slices:
            slice_obs = [o for o in series_obs if o.slice_minutes == minutes]
            if not slice_obs:
                print(f"\n  T-{minutes}min: no samples")
                continue

            mids = sum(1 for o in slice_obs if o.source == "mid")
            print(
                f"\n  T-{minutes}min  (n={len(slice_obs)}, "
                f"{mids / len(slice_obs) * 100:.0f}% from two-sided mid, "
                f"{(len(slice_obs) - mids) / len(slice_obs) * 100:.0f}% from last trade)"
            )
            print(f"  {'bucket':<12}{'n':>7}{'implied':>10}{'realized':>10}{'diff':>9}"
                  f"{'mean px':>10}{'EV/contract':>13}   flag")
            print(f"  {'-' * 78}")

            for index in range(_N_BUCKETS):
                row = stats.get((series, minutes, index))
                if row is None:
                    continue
                diff = row["realized"] - row["implied"]
                is_flagged = row["n"] >= min_n and abs(diff) >= flag_threshold
                marker = "  <<< MISCALIBRATED" if is_flagged else ""
                if is_flagged:
                    flagged.append((series, minutes, bucket_label(index), row["n"],
                                    row["implied"], row["realized"], diff, row["ev"]))
                print(
                    f"  {bucket_label(index):<12}{row['n']:>7}{row['implied']:>10.3f}"
                    f"{row['realized']:>10.3f}{diff:>+9.3f}{row['mean_price']:>10.3f}"
                    f"{row['ev']:>+13.4f}{marker}"
                )

            n = len(slice_obs)
            tot_realized = sum(1 for o in slice_obs if o.settled_yes) / n
            tot_price = sum(o.yes_price for o in slice_obs) / n
            tot_ev = sum(observation_ev(o.yes_price, o.settled_yes, coefficient, contracts) for o in slice_obs) / n
            print(f"  {'-' * 78}")
            print(
                f"  {'TOTAL':<12}{n:>7}{tot_price:>10.3f}{tot_realized:>10.3f}"
                f"{tot_realized - tot_price:>+9.3f}{tot_price:>10.3f}{tot_ev:>+13.4f}"
            )
            print(
                f"  {'':12}mean price is the implied prob here (not a bucket midpoint); "
                f"total EV over {n} contracts = ${tot_ev * n:+,.2f}"
            )

    return flagged


def print_flagged(flagged: list[tuple], min_n: int, flag_threshold: float) -> None:
    print(f"\n{'=' * 92}")
    print(f"=== FLAGGED BUCKETS  (|realized - implied| >= {flag_threshold:.2f} with n >= {min_n})")
    print(f"{'=' * 92}")
    if not flagged:
        print("  None. No bucket met the sample-size and deviation thresholds.")
        print("  That is the null result: at this resolution the market prices look calibrated.")
        return

    print(f"  {'series':<12}{'slice':>7}{'bucket':>13}{'n':>7}{'implied':>10}"
          f"{'realized':>10}{'diff':>9}{'EV/contract':>13}")
    print(f"  {'-' * 81}")
    for series, minutes, label, n, implied, realized, diff, ev in sorted(
        flagged, key=lambda r: -abs(r[6])
    ):
        print(
            f"  {series:<12}{'T-' + str(minutes):>7}{label:>13}{n:>7}"
            f"{implied:>10.3f}{realized:>10.3f}{diff:>+9.3f}{ev:>+13.4f}"
        )
    print(
        "\n  A positive diff means the market UNDERPRICES YES in that bucket "
        "(YES settles more often than its price implies); negative means it OVERPRICES YES."
    )
    print(
        "  EV/contract is the after-fee result of buying the CHEAPER side at the sampled price. "
        "It is an upper bound -- you would cross a real spread to get filled."
    )


def write_csv(path: Path, observations: list[Observation], coefficient: float, contracts: int) -> None:
    """Dump every raw observation, so the aggregation can be redone elsewhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "series", "ticker", "close_ts", "close_iso", "slice_minutes",
            "yes_price", "price_source", "bucket", "settled_yes", "ev_cheaper_side",
        ])
        for o in observations:
            writer.writerow([
                o.series, o.ticker, o.close_ts,
                datetime.fromtimestamp(o.close_ts, tz=timezone.utc).isoformat(),
                o.slice_minutes, f"{o.yes_price:.4f}", o.source,
                bucket_label(bucket_index(o.yes_price)),
                int(o.settled_yes),
                f"{observation_ev(o.yes_price, o.settled_yes, coefficient, contracts):.4f}",
            ])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--days", type=int, default=30, help="Days of settled history to study (default 30)")
    parser.add_argument(
        "--series", action="append", default=None,
        help=f"Series to study; repeatable. Default: {' '.join(_DEFAULT_SERIES)}",
    )
    parser.add_argument(
        "--slices", default=",".join(str(s) for s in _DEFAULT_SLICES),
        help="Comma-separated minutes-before-close to sample (default 10,5,2)",
    )
    parser.add_argument("--cache", type=Path, default=Path("data/calibration_study.sqlite"),
                        help="Local sqlite cache of raw API responses (default data/calibration_study.sqlite)")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the cache and refetch everything from the API")
    parser.add_argument("--sleep", type=float, default=0.25,
                        help="Seconds to sleep between market fetches (default 0.25)")
    parser.add_argument("--min-n", type=int, default=30,
                        help="Minimum bucket sample size before a deviation is flagged (default 30)")
    parser.add_argument("--flag-threshold", type=float, default=0.05,
                        help="|realized - implied| at or above which a bucket is flagged (default 0.05)")
    parser.add_argument("--fee-coefficient", type=float, default=0.07,
                        help="Kalshi taker fee coefficient (default 0.07)")
    parser.add_argument("--contracts-per-order", type=int, default=10,
                        help="Contracts per order, inside the order-level fee ceiling "
                             "(default 10, pre-committed in KILL_CRITERIA.md section 2)")
    parser.add_argument("--no-resolve-refetch", action="store_true",
                        help="Don't call get_market() for markets the cached event record left unresolved "
                             "(faster; drops markets whose nested record predates settlement)")
    parser.add_argument("--max-markets", type=int, default=0,
                        help="Cap markets fetched per series (0 = no limit). Use a small value for a smoke test.")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Optional path to dump every raw observation as CSV")
    args = parser.parse_args()

    series_list = args.series or list(_DEFAULT_SERIES)
    try:
        slices = sorted({int(s) for s in args.slices.split(",") if s.strip()}, reverse=True)
    except ValueError:
        parser.error(f"--slices must be comma-separated integers, got {args.slices!r}")
    if not slices:
        parser.error("--slices must name at least one time slice")

    end_ts = int(time.time())
    start_ts = end_ts - args.days * 86400
    # Fetch a couple of extra minutes before the earliest slice so a market with
    # sparse candles still has one ending at or before that slice's target.
    candle_span_minutes = max(slices) + _CANDLE_LEAD_MINUTES

    print(f"Kalshi 15-minute crypto calibration study -- READ-ONLY")
    print(f"  window : {datetime.fromtimestamp(start_ts, tz=timezone.utc):%Y-%m-%d %H:%M} -> "
          f"{datetime.fromtimestamp(end_ts, tz=timezone.utc):%Y-%m-%d %H:%M} UTC ({args.days} days)")
    print(f"  series : {', '.join(series_list)}")
    print(f"  slices : {', '.join('T-' + str(s) + 'min' for s in slices)}")
    print(f"  cache  : {args.cache}{' (REFRESHING)' if args.refresh else ''}")

    cache = StudyCache(args.cache)
    client = KalshiClient(settings)
    observations: list[Observation] = []
    skipped_unsettled = 0
    skipped_no_candles = 0
    skipped_no_close = 0

    try:
        for series in series_list:
            print(f"\n--- {series} ---")
            markets = enumerate_settled_markets(
                client, cache, series, start_ts, end_ts, refresh=args.refresh
            )

            candidates = sorted(markets.items())
            if args.max_markets:
                candidates = candidates[-args.max_markets:]
                print(f"  [{series}] --max-markets {args.max_markets}: studying the most recent "
                      f"{len(candidates)} of {len(markets)}.")

            processed = 0
            for ticker, market in candidates:
                close_ts = parse_iso_ts(market.get("close_time"))
                if close_ts is None:
                    skipped_no_close += 1
                    continue

                settled_yes = resolve_settlement(
                    client, cache, series, ticker, market,
                    allow_refetch=not args.no_resolve_refetch,
                )
                if settled_yes is None:
                    skipped_unsettled += 1
                    continue

                candles = fetch_candles_cached(
                    client, cache, series, ticker,
                    close_ts - candle_span_minutes * 60, close_ts,
                    refresh=args.refresh, sleep_seconds=args.sleep,
                )
                if not candles:
                    skipped_no_candles += 1
                    continue

                observations.extend(sample_market(series, ticker, close_ts, candles, settled_yes, slices))

                processed += 1
                if processed == 1 or processed % _PROGRESS_PRINT_EVERY == 0:
                    print(f"  [{series}] {processed}/{len(candidates)} markets sampled "
                          f"({len(observations)} observations so far)")

            print(f"  [{series}] done: {processed} markets sampled.")
    finally:
        client.close()

    print(f"\nCollected {len(observations)} observations "
          f"({len({o.ticker for o in observations})} distinct settled markets).")
    print(f"Skipped: {skipped_unsettled} unsettled, {skipped_no_candles} with no candles, "
          f"{skipped_no_close} with no close_time.")

    if not observations:
        print("\nNothing to analyse. If this is a first run, check credentials "
              "(get_events/get_candlesticks need auth) and that --days covers settled markets.")
        cache.close()
        return

    stats = aggregate(observations, args.fee_coefficient, args.contracts_per_order)
    flagged = print_tables(
        observations, stats, series_list, slices,
        args.min_n, args.flag_threshold, args.fee_coefficient,
        args.contracts_per_order,
    )
    print_flagged(flagged, args.min_n, args.flag_threshold)

    if args.csv:
        write_csv(args.csv, observations, args.fee_coefficient, args.contracts_per_order)
        print(f"\nWrote {len(observations)} raw observations to {args.csv}")

    cache.close()


if __name__ == "__main__":
    main()
