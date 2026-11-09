#!/usr/bin/env python
"""
Download recent KXBTC / KXBTC15M market history from Kalshi's candlestick
endpoint and write it to CSV in the format kbot.backtest.load_csv() expects:

    timestamp, btc_price, btc_vol, ticker, yes_ask, no_ask, resolved_yes, floor_strike

Real Kalshi contract prices (via KalshiClient.get_candlesticks) close the
biggest honesty gap in our backtest pipeline (MARKET_RESEARCH.md Source 8 /
V1) — previously we could only backtest against synthetic/hand-built order
books, never actual traded prices.

btc_price / btc_vol are backfilled from Coinbase's public historical candles
endpoint (no auth needed) over the same timestamp range, using the same
log-return volatility estimator as kbot.feeds.btc.BTCFeed.vol_annualised.

resolved_yes prefers Kalshi's own settlement data over any inferred value:
result (if populated) -> expiration_value compared against the market's own
strike -> Coinbase spot near close_time as a last resort only (see
resolved_yes_from_market's docstring for why that order matters).

floor_strike is populated (from the market record) for KXBTC15M rows only --
that series' strike is a dynamic per-window value not encoded in the ticker
at all (see kbot.strategy.btc_hourly's module docstring); it's blank for
hourly rows, whose strike comes from the ticker string as before.

Usage:
    python scripts/pull_history.py --series KXBTC --days 7 --out data/kxbtc_history.csv
    python scripts/pull_history.py --series KXBTC15M --days 14 --out data/kxbtc15m_history.csv

FIXED 2026-07 after a live 400 ("Invalid format for parameter period_interval:
parsing \"1h\": invalid syntax") exposed two bugs, both confirmed by probing
the live API directly with raw httpx + our RSA-PSS signer:

  1. period_interval must be an INT in {1, 60, 1440} (minute/hour/day), not
     a string like "1h". Any other value 400s with a "oneof" validation
     error. start_ts/end_ts are also REQUIRED (400 "Query argument ... is
     required" if omitted) despite the SDK marking them optional.
  2. The SDK's Candlestick model doesn't match the real response shape at
     all — every field silently deserialized to None with no error. The
     real shape is nested and dollar-denominated, not flat cents:
         end_period_ts, price{...}, yes_ask{open/high/low/close_dollars},
         yes_bid{...}, volume_fp, open_interest_fp
     KalshiClient.get_candlesticks() now bypasses the SDK (raw httpx) and
     returns these raw dicts untouched — see its docstring for the full
     shape and units.

CAVEATS remaining — verify before trusting a backtest built from this data:
  - yes_bid all-zero ("0.0000") means no resting bid was observed that
    period, not that the bid was literally zero. Rows with no valid
    two-sided quote are DROPPED, not faked — see candlestick_to_row().
    Far-OTM / thin KXBTC markets can end up with very few or zero usable
    rows; that's a real liquidity finding, not a bug.
  - The API caps each response at 5000 candlesticks — chunk_time_range()
    below splits wide requests to stay under that.
  - Coinbase's public candles host has moved before (api.pro.coinbase.com
    -> api.exchange.coinbase.com); if BTC backfill starts 404ing, that's
    the first thing to check.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kbot.config import settings  # noqa: E402
from kbot.kalshi_client import KalshiClient  # noqa: E402
from kbot.strategy.btc_hourly import BTCHourlyStrategy  # noqa: E402
from kbot.vol import ewma_vol, fallback_vol, parkinson_vol  # noqa: E402

_COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
_COINBASE_MAX_CANDLES_PER_CALL = 300
_KALSHI_MAX_CANDLES_PER_CALL = 5000
_VALID_PERIOD_INTERVALS = (1, 60, 1440)  # minute / hour / day

# 429 retry policy for get_candlesticks() (2026-07 -- live pulls were
# crashing outright on rate limits; KalshiClient.get_candlesticks() is
# deliberately unretried, so backoff lives here instead).
_CANDLES_MAX_ATTEMPTS = 5
_CANDLES_INITIAL_BACKOFF_SECONDS = 2.0
_CANDLES_MAX_BACKOFF_SECONDS = 30.0

# Inter-market pacing: 0.5s between markets keeps us well under the Basic
# tier's 20 reads/sec even with retries and chunked wide-range fetches.
_INTER_MARKET_DELAY_SECONDS = 0.5

# Progress ping cadence for long runs (~1,500 markets at 0.5s/market is ~12min).
_PROGRESS_PRINT_EVERY = 50

# Liquidity pre-filter (2026-07 -- the series index carries 36k+ markets;
# a --days 14 window alone still leaves ~32k of them, and far-OTM strike
# buckets see little to no trading activity: a 200-market --max-markets test
# run in ticker-sort order (all far-OTM B-buckets) yielded exactly 1 usable
# row). Default width is deliberately wider than btc_hourly's own
# near_atm_only trading filter (fair_p in [0.25, 0.75]) -- this is a
# fetch-budget optimization, not a trading decision, so it should err
# toward keeping markets, not matching the strategy's live trading band.
_LIQUIDITY_NEAR_ATM_STRIKES_DEFAULT = 8


# ── Pure conversion / math helpers (unit tested) ─────────────────────────────

def chunk_time_range(
    start_ts: int,
    end_ts: int,
    period_interval: int,
    max_candles: int = 4500,  # margin under the API's 5000 cap
) -> list[tuple[int, int]]:
    """Split [start_ts, end_ts) into chunks each covering <= max_candles candlesticks."""
    if start_ts >= end_ts:
        return []
    chunk_seconds = max_candles * period_interval * 60
    chunks = []
    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(end_ts, cursor + chunk_seconds)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


def candlestick_to_row(
    candle: dict,
    ticker: str,
    btc_price: float,
    btc_vol: float,
    resolved_yes: bool,
    floor_strike: float | None = None,
) -> dict | None:
    """
    Convert one real Kalshi candlestick (see KalshiClient.get_candlesticks's
    docstring for the exact shape) + backfilled BTC spot data into one
    backtest CSV row.

    `floor_strike`, if given, is written through unchanged -- populated by
    the caller for series whose strike isn't ticker-encoded (KXBTC15M; see
    kbot.strategy.btc_hourly's module docstring), left None/blank for
    hourly rows (whose strike comes from the ticker string, no CSV column
    needed).

    Returns None if the candle has no valid two-sided quote (missing or
    all-zero yes_ask/yes_bid) -- an all-zero yes_bid means no resting bid
    was observed that period, not that NO's implied price was $1.
    Fabricating a price here would defeat the entire point of using real
    data, so these rows are honestly dropped instead.
    """
    yes_ask_str = (candle.get("yes_ask") or {}).get("close_dollars")
    yes_bid_str = (candle.get("yes_bid") or {}).get("close_dollars")
    ts = candle.get("end_period_ts")
    if yes_ask_str is None or yes_bid_str is None or ts is None:
        return None

    yes_ask = float(yes_ask_str)
    yes_bid = float(yes_bid_str)
    if yes_ask <= 0.0 or yes_bid <= 0.0:
        return None  # no real two-sided quote this period

    no_ask = round(1.0 - yes_bid, 4)
    return {
        "timestamp": ts,
        "btc_price": btc_price,
        "btc_vol": btc_vol,
        "ticker": ticker,
        "yes_ask": round(yes_ask, 4),
        "no_ask": no_ask,
        "resolved_yes": int(bool(resolved_yes)),
        "floor_strike": floor_strike if floor_strike is not None else "",
    }


def nearest_btc_sample(btc_history: list[tuple[float, float]], ts: float) -> float | None:
    """
    Price from the most recent (timestamp, price) sample in btc_history
    STRICTLY BEFORE `ts` -- never a sample at or after `ts`.

    2026-07 LOOK-AHEAD FIX: this used to pick whichever of the two
    neighboring samples was closest in EITHER direction, which could (and,
    checked directly against 500 real KXBTC15M rows, did in 41% of cases,
    by up to 120s) pick a sample from AFTER the row's own timestamp --
    leaking future BTC spot into that row's fair-value input. For a
    15-minute market, 90-120s of look-ahead is a large fraction of the
    remaining time-to-expiry, and the effect is amplified by the model's
    sigma*sqrt(t) denominator shrinking as expiry approaches -- the
    likely root cause of KXBTC15M's severe calibration miss (see
    MARKET_RESEARCH.md history / the 2026-07 diagnosis). Now strictly
    causal: only `btc_history[idx-1]`, whose timestamp is GUARANTEED
    less than `ts` by bisect_left's own definition, is ever returned.

    Requires btc_history sorted by timestamp ascending (fetch_coinbase_candles
    already sorts it) -- uses binary search rather than a linear scan, since
    this is called once per candidate market during liquidity filtering.
    2026-07: a 30-day pull has ~128k candidates x ~8.6k BTC samples; a linear
    scan here made that one filtering step take many CPU-minutes before any
    network fetch even began.

    Returns None if no sample exists strictly before `ts` (e.g. `ts` is at
    or before the very first sample) -- callers must not fabricate a spot
    price when there's honestly none available yet.
    """
    if not btc_history:
        return None
    idx = bisect.bisect_left(btc_history, ts, key=lambda sample: sample[0])
    if idx == 0:
        return None  # no sample strictly before ts
    return btc_history[idx - 1][1]


def realized_vol_at(
    close_samples: list[tuple[float, float]],
    ohlc_bars: list[tuple[float, float, float]],
    ts: float,
    cfg,
    window_seconds: float = 3600.0,
    bar_seconds: float = 300.0,
    multiplier: float | None = None,
    floor: float | None = None,
) -> float:
    """
    Realized-vol estimate as of `ts`: max(EWMA, Parkinson) over the trailing
    `window_seconds`, scaled by `multiplier` (cfg.vol_multiplier if not
    given) and floored at cfg.vol_floor (see kbot.vol.fallback_vol).

    2026-07 calibration fix: replaces the old flat-window close-to-close
    estimator (previously named rolling_vol_at), which badly understated
    true BTC vol -- confirmed by back-solving market-implied vol for 312
    honest backtest trades (100% had implied_vol > our_vol, median ratio
    2.2x). EWMA reacts faster to a genuine regime change than a flat
    window average; Parkinson captures intra-bar range/jump vol that any
    close-only estimator misses entirely. Always returns a usable value
    (never None) -- see kbot.vol.fallback_vol.

    2026-07 series-aware vol window: `window_seconds` MUST match the
    contract's own horizon -- passing the same 3600s window used for
    KXBTC (60-minute contracts) to KXBTC15M (15-minute contracts)
    overestimated the latter's vol by ~2x (median implied/estimated ratio
    0.488), confirmed to be the root cause of KXBTC15M's severe
    calibration miss. Callers must pass a per-series window (see
    Strategy.vol_window_seconds) instead of relying on the default.

    2026-08 series-aware vol FLOOR: `floor` likewise overrides cfg.vol_floor
    per series (see Strategy.vol_floor / cfg.vol_floor_15m). A CSV built with
    the hourly floor on 15-minute rows reproduces the exact defect this fix
    removes -- the floor binds on most rows, sigma becomes a constant, and
    every fair_p in the resulting backtest lands at ~0.5. Pass the same floor
    the live strategy would use for that series.
    """
    window_closes = [(t, p) for t, p in close_samples if ts - window_seconds < t <= ts]
    window_hl = [(h, l) for t, h, l in ohlc_bars if ts - window_seconds < t <= ts]

    ewma = ewma_vol(window_closes)
    parkinson = parkinson_vol([(h, l) for h, l in window_hl], bar_seconds=bar_seconds)
    # fallback_vol returns (sigma, floored) as of 2026-08; this backtest-CSV
    # builder only wants the number. The floor's binding RATE on live data is
    # measured from kbot_events.db by scripts/replay_pricing_fixes.py.
    sigma, _floored = fallback_vol(ewma, parkinson, cfg, multiplier=multiplier, floor=floor)
    return sigma


def _resolve_against_strike(
    value: float,
    strike_type: str | None,
    floor_strike: float | None,
    cap_strike: float | None,
) -> bool | None:
    """
    True/False if `value` (a settlement price) clears `strike_type`'s
    threshold(s), or None if the strike fields needed for that type are
    missing. Verified live 2026-07 against real KXBTC/KXBTC15M market
    records -- strike_type conventions:
      "less"             : YES if value <  cap_strike   (KXBTC T-low)
      "less_or_equal"     : YES if value <= cap_strike
      "greater"           : YES if value >  floor_strike (KXBTC T-high)
      "greater_or_equal"  : YES if value >= floor_strike (KXBTC15M)
      "between"           : YES if floor_strike <= value <= cap_strike (KXBTC B-range)
    """
    if strike_type == "less":
        return None if cap_strike is None else value < float(cap_strike)
    if strike_type == "less_or_equal":
        return None if cap_strike is None else value <= float(cap_strike)
    if strike_type == "greater":
        return None if floor_strike is None else value > float(floor_strike)
    if strike_type == "greater_or_equal":
        return None if floor_strike is None else value >= float(floor_strike)
    if strike_type == "between":
        if floor_strike is None or cap_strike is None:
            return None
        return float(floor_strike) <= value <= float(cap_strike)
    return None


def resolved_yes_from_market(
    market: dict,
    spot_near_close: float | None = None,
) -> bool | None:
    """
    Determine resolved_yes for one market dict, preferring Kalshi's own
    settlement data over any inferred value -- a three-level hierarchy:

      1. result ("yes"/"no") -- Kalshi's authoritative settlement call.
         Cheapest and most trustworthy; used whenever present.
      2. expiration_value (the measured settlement price, e.g. the CF
         Benchmarks BRTI average Kalshi actually settles against) compared
         to the market's own strike_type/floor_strike/cap_strike -- this
         recovers markets where `result` wasn't populated (yet, or ever)
         but the settlement price is, using data straight from Kalshi's
         own market record.
      3. spot_near_close (e.g. Coinbase BTC spot nearest the market's
         close_time), same strike comparison -- LAST RESORT only. Coinbase
         spot is a DIFFERENT reference index than whatever Kalshi's own
         settlement source is, and the two can disagree right at a strike
         boundary (the "BRRNY residual" -- Kalshi's settlement provider has
         changed over time; whichever it is now, it isn't Coinbase spot).
         Only used when neither Kalshi field above resolves the market.

    Returns None if none of the three resolve it (market not yet settled,
    or missing the strike fields needed to compare against).
    """
    result = str(market.get("result") or "").lower()
    if result in ("yes", "no"):
        return result == "yes"

    strike_type = market.get("strike_type")
    floor_strike = market.get("floor_strike")
    cap_strike = market.get("cap_strike")

    exp_value_str = market.get("expiration_value")
    if exp_value_str:
        try:
            exp_value = float(exp_value_str)
        except (TypeError, ValueError):
            exp_value = None
        if exp_value is not None:
            outcome = _resolve_against_strike(exp_value, strike_type, floor_strike, cap_strike)
            if outcome is not None:
                return outcome

    if spot_near_close is not None:
        return _resolve_against_strike(spot_near_close, strike_type, floor_strike, cap_strike)

    return None


def resolved_yes_from_events(events: list[dict], market_ticker: str) -> bool | None:
    """
    True/False if `market_ticker` appears settled in `events` (as returned
    by KalshiClient.get_events(..., with_nested_markets=True)), else None.
    """
    for ev in events:
        for m in ev.get("markets") or []:
            if m.get("ticker") == market_ticker:
                outcome = resolved_yes_from_market(m)
                if outcome is not None:
                    return outcome
    return None


def build_resolved_index(events: list[dict]) -> dict[str, bool]:
    """
    ticker -> resolved_yes for every resolvable market across all of
    `events`, built in one O(events x markets) pass, using
    resolved_yes_from_market()'s result/expiration_value hierarchy (no
    spot -- that fallback only applies per-market in main(), where a
    Coinbase sample near that specific market's close_time is available).

    Calling resolved_yes_from_events() per-candidate against the whole
    series index (tens of thousands of tickers) is O(N^2); this index makes
    each lookup O(1) and is also what filter_markets_by_resolution() below
    uses to pick out the handful of markets worth fetching at all.
    """
    index: dict[str, bool] = {}
    for ev in events:
        for m in ev.get("markets") or []:
            ticker = m.get("ticker")
            if not ticker:
                continue
            outcome = resolved_yes_from_market(m)
            if outcome is not None:
                index[ticker] = outcome
    return index


def is_btc_series(series: str) -> bool:
    """
    True if `series` is KXBTC or KXBTC15M (case-insensitive). The Coinbase
    BTC spot backfill and the near-ATM liquidity filter both need a BTC
    spot price to measure distance from -- meaningless overhead for any
    other --series (e.g. base_rate's KXFEDDECISION/KXCPI/KXPAYROLLS), so
    main() skips them and leaves btc_price/btc_vol as 0.0 placeholders in
    the output CSV.
    """
    return series.upper() in ("KXBTC", "KXBTC15M")


def filter_markets_by_resolution(
    markets_by_ticker: dict[str, dict],
    resolved_index: dict[str, bool],
) -> dict[str, dict]:
    """
    Keep only markets that resolved YES.

    DO NOT use this to select fetch candidates for backtest data. It was
    briefly wired into main() on this reasoning: a live probe of one KXBTC
    hourly event showed exactly one market -- whichever bucket actually
    contained the settlement price -- with a real resting bid; every
    sibling bucket showed yes_bid == "0.0000" all hour, even at 1-minute
    granularity. That's true, but filtering fetch candidates on the
    resolved outcome means every row in the resulting CSV has
    resolved_yes=True by construction -- any YES-side backtest trade then
    trivially "wins", which is look-ahead bias, not a real edge (caught
    2026-07 when this produced a 99% backtest win rate with near-100% win
    rate in every price bucket). Use filter_markets_by_liquidity's
    distance-based check instead -- it only uses BTC spot at fetch time,
    which is honestly available before the outcome is known, so losing
    trades can still appear in the data.

    Kept only as a tested utility for callers that are not building
    backtest datasets (e.g. auditing settlement rates).
    """
    return {t: m for t, m in markets_by_ticker.items() if resolved_index.get(t) is True}


def _parse_iso_ts(value: str | None) -> int | None:
    """Unix seconds from a Kalshi ISO-8601 timestamp string, or None if unparseable/missing."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def market_time_window(market: dict, floor_ts: int, ceil_ts: int) -> tuple[int, int] | None:
    """
    (start_ts, end_ts) for `market`'s own open_time/close_time, clamped to
    [floor_ts, ceil_ts]. Returns None if the market has no usable window in
    that range (e.g. it closed before floor_ts, or fields are missing).
    """
    m_start = _parse_iso_ts(market.get("open_time"))
    m_end = _parse_iso_ts(market.get("close_time"))
    if m_start is None or m_end is None:
        return None
    start = max(m_start, floor_ts)
    end = min(m_end, ceil_ts)
    if start >= end:
        return None
    return start, end


def filter_markets_by_close_window(
    markets_by_ticker: dict[str, dict],
    start_ts: int,
    end_ts: int,
) -> dict[str, dict]:
    """
    Keep only markets whose close_time falls within [start_ts, end_ts].

    The series index can carry tens of thousands of markets going back to
    launch; most are long expired and irrelevant to a --days N pull. This
    runs BEFORE resolved_yes_from_events() (an O(events x markets) lookup)
    or any candlestick fetch, so a --days 14 run doesn't burn time (or rate
    limit budget) on markets that closed months ago.
    """
    kept: dict[str, dict] = {}
    for ticker, market in markets_by_ticker.items():
        close_ts = _parse_iso_ts(market.get("close_time"))
        if close_ts is not None and start_ts <= close_ts <= end_ts:
            kept[ticker] = market
    return kept


def filter_markets_by_liquidity(
    markets_by_ticker: dict[str, dict],
    strategy: BTCHourlyStrategy,
    btc_history: list[tuple[float, float]],
    strikes: int = _LIQUIDITY_NEAR_ATM_STRIKES_DEFAULT,
) -> dict[str, dict]:
    """
    Keep only markets within `strikes` bracket-widths ($500 each) of the BTC
    spot price observed nearest each market's own [open_time, close_time]
    midpoint -- far-OTM KXBTC range/threshold buckets see little to no
    trading activity, so fetching them mostly burns rate-limit budget on
    candlesticks candlestick_to_row() will drop for lack of a two-sided quote.

    This is a fetch-budget optimization, not a correctness filter: markets
    we can't reference a spot price for (unparseable ticker, no BTC sample
    near their window) are always kept rather than guessed away. Pass
    strikes=0 (or a falsy value) to disable filtering entirely.
    """
    if not strikes:
        return dict(markets_by_ticker)

    kept: dict[str, dict] = {}
    for ticker, market in markets_by_ticker.items():
        m_start = _parse_iso_ts(market.get("open_time"))
        m_end = _parse_iso_ts(market.get("close_time"))
        if m_start is None or m_end is None:
            kept[ticker] = market
            continue
        mid_ts = (m_start + m_end) / 2.0
        spot = nearest_btc_sample(btc_history, mid_ts)
        if spot is None or strategy.is_near_atm(ticker, spot, strikes=strikes):
            kept[ticker] = market
    return kept


# ── Network I/O (not unit tested — see the caveats in this file's docstring) ───

def fetch_coinbase_candles(
    start_ts: int,
    end_ts: int,
    granularity_seconds: int = 300,
) -> tuple[list[tuple[float, float]], list[tuple[float, float, float]]]:
    """
    Historical BTC-USD data from Coinbase's public candles endpoint.

    Returns (close_samples, ohlc_bars):
      close_samples: (timestamp, close) pairs, oldest first -- spot lookups
        (nearest_btc_sample) and EWMA vol both only need the close.
      ohlc_bars: (timestamp, high, low) triples -- feeds the Parkinson range
        vol estimator (2026-07 calibration fix, see kbot.vol.parkinson_vol),
        which close-only history can't compute since it discards intra-bar
        range entirely.
    """
    close_samples: list[tuple[float, float]] = []
    ohlc_bars: list[tuple[float, float, float]] = []
    window = granularity_seconds * _COINBASE_MAX_CANDLES_PER_CALL
    with httpx.Client(timeout=10.0) as client:
        cursor = end_ts
        while cursor > start_ts:
            window_start = max(start_ts, cursor - window)
            resp = client.get(_COINBASE_CANDLES_URL, params={
                "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(window_start)),
                "end": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cursor)),
                "granularity": granularity_seconds,
            })
            resp.raise_for_status()
            for row in resp.json():
                # Coinbase candle row: [time, low, high, open, close, volume]
                ts, low, high, _open, close, _volume = row
                close_samples.append((float(ts), float(close)))
                ohlc_bars.append((float(ts), float(high), float(low)))
            cursor = window_start
            time.sleep(0.34)  # ~3 req/s -- well under Coinbase's public rate limit
    close_samples.sort(key=lambda s: s[0])
    ohlc_bars.sort(key=lambda s: s[0])
    return close_samples, ohlc_bars


def _fetch_chunk_with_retry(
    client: KalshiClient,
    series: str,
    ticker: str,
    chunk_start: int,
    chunk_end: int,
    period_interval: int,
    max_attempts: int = _CANDLES_MAX_ATTEMPTS,
) -> list[dict] | None:
    """
    Fetch one candlestick chunk, retrying on 429 with exponential backoff
    (2s, 4s, 8s, ... capped at 30s). Any other HTTP error is raised
    immediately (not retried -- 429 is the only transient case here).
    Returns None if still rate-limited after `max_attempts`, so the caller
    can skip this chunk rather than aborting the whole pull.
    """
    backoff = _CANDLES_INITIAL_BACKOFF_SECONDS
    for attempt in range(1, max_attempts + 1):
        try:
            return client.get_candlesticks(
                series, ticker, start_ts=chunk_start, end_ts=chunk_end, period_interval=period_interval,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                raise
            if attempt == max_attempts:
                print(
                    f"  WARNING: {ticker} still rate-limited (429) after "
                    f"{max_attempts} attempts -- skipping"
                )
                return None
            print(f"  429 for {ticker}, retrying in {backoff:.0f}s (attempt {attempt}/{max_attempts})...")
            time.sleep(backoff)
            backoff = min(backoff * 2, _CANDLES_MAX_BACKOFF_SECONDS)
    return None  # unreachable, satisfies type checkers


def fetch_market_candles(
    client: KalshiClient,
    series: str,
    ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int,
) -> list[dict]:
    """
    Fetch candlesticks for one market, chunking to respect the 5000-candle
    cap. Each chunk retries on 429 (see _fetch_chunk_with_retry); a chunk
    that's still rate-limited after all retries is skipped (its candles are
    simply missing from the result) rather than crashing the whole pull.
    """
    candles: list[dict] = []
    for chunk_start, chunk_end in chunk_time_range(start_ts, end_ts, period_interval):
        chunk_candles = _fetch_chunk_with_retry(client, series, ticker, chunk_start, chunk_end, period_interval)
        if chunk_candles is not None:
            candles.extend(chunk_candles)
    return candles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", default="KXBTC", help="Series ticker (default: KXBTC)")
    parser.add_argument("--days", type=int, default=7, help="Days of history to pull")
    parser.add_argument(
        "--period-interval", type=int, default=60, choices=_VALID_PERIOD_INTERVALS,
        help="Candlestick period in minutes: 1 (minute), 60 (hour), or 1440 (day)",
    )
    parser.add_argument("--out", type=Path, default=Path("data/kxbtc_history.csv"))
    parser.add_argument(
        "--max-markets", type=int, default=500,
        help="Stop after fetching candlesticks for this many markets (0 = no limit). "
             "Default 500 keeps test runs fast; a full pull needs --max-markets 0.",
    )
    parser.add_argument(
        "--near-atm-strikes", type=int, default=_LIQUIDITY_NEAR_ATM_STRIKES_DEFAULT,
        help="Liquidity pre-filter: skip markets more than this many $500 "
             "bracket-widths from BTC spot at their own active window (0 = disable). "
             f"Default {_LIQUIDITY_NEAR_ATM_STRIKES_DEFAULT}. Distance-based only -- "
             "never filters by outcome, to avoid look-ahead bias in backtest data "
             "(see filter_markets_by_resolution's docstring).",
    )
    parser.add_argument(
        "--end-days-ago", type=int, default=0,
        help="Shift the whole --days window back by this many days from now "
             "(default 0 = ending now). E.g. --days 30 --end-days-ago 30 pulls "
             "the PRIOR 30-day window (days 31-60 back) -- for out-of-sample "
             "validation against a --days 30 --end-days-ago 0 backtest.",
    )
    args = parser.parse_args()

    end_ts = int(time.time()) - args.end_days_ago * 86400
    start_ts = end_ts - args.days * 86400

    client = KalshiClient(settings)
    try:
        # KXBTC runs ~1 event/hour, KXBTC15M ~4/hour (96/day) -- page back
        # far enough to plausibly cover the FULL span back to the start of
        # the window -- that's (days + end_days_ago), not just --days,
        # since --end-days-ago shifts the whole window further into the
        # past and events are returned newest-first (so reaching an older
        # window means paging through everything more recent first).
        # Getting this wrong is silent: a 2026-07 OOS pull (--days 30
        # --end-days-ago 30, needing ~60 days of coverage) used a
        # --days-only estimate of 6 pages, which a direct check showed
        # only reached ~49.6 days back -- quietly truncating the older
        # third of the intended window. A separate 2026-07 KXBTC15M pull
        # (--days 14) hit the SAME failure mode from the opposite
        # direction: the estimate assumed hourly cadence (24 events/day)
        # for a series that actually creates 96/day, so it hit the page
        # cap after covering only ~8.3 of the requested 14 days -- fixed
        # by making the daily-events assumption series-aware. Healthy
        # safety margin on top (+2 pages) as before.
        events_per_page = 200
        events_per_day = 96 if args.series.upper() == "KXBTC15M" else 24
        total_days_back = args.days + args.end_days_ago
        est_max_pages = max(1, math.ceil(total_days_back * events_per_day / events_per_page) + 2)
        print(f"Fetching events for series {args.series} (up to {est_max_pages} pages)...")
        events = client.get_events(series_ticker=args.series, limit=events_per_page, max_pages=est_max_pages)
        all_markets_by_ticker = {
            m["ticker"]: m for ev in events for m in (ev.get("markets") or []) if m.get("ticker")
        }
        print(f"Fetched {len(events)} events, {len(all_markets_by_ticker)} markets in the series index.")

        resolved_index = build_resolved_index(events)

        markets_by_ticker = filter_markets_by_close_window(all_markets_by_ticker, start_ts, end_ts)
        print(
            f"{len(markets_by_ticker)} markets close within the --days {args.days} window "
            f"({len(all_markets_by_ticker) - len(markets_by_ticker)} expired/out-of-window skipped)."
        )

        # BTC spot backfill + the near-ATM liquidity filter are both specific
        # to KXBTC (they need a BTC spot price to measure distance from);
        # for any other --series (e.g. base_rate's KXFEDDECISION/KXCPI/
        # KXPAYROLLS) they're meaningless overhead, so skip them and leave
        # btc_price/btc_vol as 0.0 placeholders in the output CSV -- those
        # columns are simply unused by non-BTC strategies.
        pulling_btc_series = is_btc_series(args.series)
        if pulling_btc_series:
            print("Backfilling BTC/USD spot history from Coinbase...")
            btc_history, ohlc_bars = fetch_coinbase_candles(start_ts, end_ts)
            print(f"Got {len(btc_history)} BTC samples ({len(ohlc_bars)} OHLC bars for Parkinson vol).")

            # NOTE: markets are NOT pre-filtered by resolution outcome here.
            # filter_markets_by_resolution() exists but must never be used to
            # pick fetch candidates for backtest data: keeping only resolved_yes
            # == True markets makes every row in the resulting CSV a YES
            # outcome, so any YES-side backtest trade trivially "wins" -- pure
            # look-ahead bias, not a real edge (caught 2026-07 when a pull built
            # this way produced a 99% backtest win rate). The distance-based
            # filter below only uses BTC spot at fetch time, which is honestly
            # available before the outcome is known.
            strategy = BTCHourlyStrategy(settings)
            before_liquidity_filter = len(markets_by_ticker)
            markets_by_ticker = filter_markets_by_liquidity(
                markets_by_ticker, strategy, btc_history, strikes=args.near_atm_strikes
            )
            print(
                f"{len(markets_by_ticker)} markets pass the near-ATM liquidity pre-filter "
                f"(strikes={args.near_atm_strikes}; {before_liquidity_filter - len(markets_by_ticker)} far-OTM skipped)."
            )
        else:
            btc_history, ohlc_bars = [], []
            print(f"Series {args.series!r} is not KXBTC -- skipping BTC spot backfill and the near-ATM liquidity filter.")

        candidates = sorted(markets_by_ticker.items())
        total_candidates = len(candidates)

        rows: list[dict] = []
        skipped_no_result = 0
        skipped_no_window = 0
        skipped_max_markets = 0
        fetched_count = 0
        for ticker, market in candidates:
            if args.max_markets and fetched_count >= args.max_markets:
                skipped_max_markets = total_candidates - fetched_count
                print(f"Reached --max-markets {args.max_markets} -- stopping ({skipped_max_markets} markets not fetched).")
                break

            resolved = resolved_index.get(ticker)
            if resolved is None and pulling_btc_series:
                # LAST-RESORT fallback: neither `result` nor `expiration_value`
                # resolved this market from build_resolved_index() alone --
                # try Coinbase spot nearest its own close_time. Documented
                # BRRNY-residual risk (see resolved_yes_from_market's
                # docstring); only reached when Kalshi's own settlement
                # fields didn't already answer it.
                close_ts = _parse_iso_ts(market.get("close_time"))
                if close_ts is not None:
                    spot = nearest_btc_sample(btc_history, close_ts)
                    if spot is not None:
                        resolved = resolved_yes_from_market(market, spot_near_close=spot)
            if resolved is None:
                skipped_no_result += 1
                continue  # still open -- no outcome to backtest against yet

            window = market_time_window(market, start_ts, end_ts)
            if window is None:
                skipped_no_window += 1
                continue
            m_start, m_end = window

            fetched_count += 1
            if fetched_count == 1 or fetched_count % _PROGRESS_PRINT_EVERY == 0:
                print(f"Fetching market {fetched_count} of {total_candidates}: {ticker}")

            # floor_strike is only meaningful (and only ever needed downstream)
            # for series whose strike isn't ticker-encoded -- KXBTC15M. Hourly's
            # floor_strike API field is null for T-low markets and doesn't match
            # what evaluate() actually prices against (the ticker-embedded
            # strike), so leave it blank there rather than write something
            # partial/misleading.
            floor_strike = market.get("floor_strike") if args.series.upper() == "KXBTC15M" else None

            candles = fetch_market_candles(client, args.series, ticker, m_start, m_end, args.period_interval)
            for candle in candles:
                row = candlestick_to_row(
                    candle, ticker, btc_price=0.0, btc_vol=0.0, resolved_yes=resolved, floor_strike=floor_strike,
                )
                if row is None:
                    continue
                if pulling_btc_series:
                    btc_price = nearest_btc_sample(btc_history, row["timestamp"])
                    if btc_price is None:
                        continue
                    row["btc_price"] = btc_price
                    # Series-aware window/multiplier/floor (2026-07, 2026-08):
                    # KXBTC15M's 15-minute contracts need a much shorter
                    # trailing vol window (and independent multiplier and
                    # floor) than KXBTC's 60-minute ones -- a mismatched 3600s
                    # window for both was confirmed to overestimate 15m vol by
                    # ~2x, and the shared 0.35 floor then bound on most 15m
                    # rows, pinning sigma and collapsing fair_p to ~0.5.
                    row["btc_vol"] = realized_vol_at(
                        btc_history, ohlc_bars, row["timestamp"], settings,
                        window_seconds=strategy.vol_window_seconds(ticker),
                        multiplier=strategy.vol_multiplier(ticker),
                        floor=strategy.vol_floor(ticker),
                    )
                rows.append(row)

            time.sleep(_INTER_MARKET_DELAY_SECONDS)

        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "timestamp", "btc_price", "btc_vol", "ticker", "yes_ask", "no_ask", "resolved_yes", "floor_strike",
            ])
            writer.writeheader()
            writer.writerows(rows)

        print(
            f"Wrote {len(rows)} rows to {args.out}  "
            f"(fetched {fetched_count} markets; skipped {skipped_no_result} unresolved, "
            f"{skipped_no_window} outside window, {skipped_max_markets} over --max-markets cap)"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
