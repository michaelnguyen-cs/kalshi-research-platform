#!/usr/bin/env python
"""
Does the longshot rule survive the SPREAD? -- read-only spread check.

QUESTION: scripts/test_longshot_rule.py measures the longshot rule's edge at
the SAMPLED MID price. market_calibration_study.py is explicit that this is an
upper bound -- "A settle rate measured off mid prices is not the same as a
fill. The EV column assumes you buy at the sampled price; crossing the real
spread is worse." This script closes exactly that gap: it re-prices the same
rule at the price you could actually have HIT, and reports what is left.

WHAT IT DOES
  1. Reads data/calib_full.csv, keeps rows with slice_minutes == --slice
     (default 7) where the rule fires: yes_price >= --hi (buy YES) or
     yes_price <= --lo (buy NO).
  2. Samples ~--sample tickers spread EVENLY over the full close_ts range:
     the span is cut into --sample equal-duration strata and the ticker
     nearest each stratum's centre is taken. That keeps a busy week from
     dominating the sample the way uniform-random draws would.
  3. For each sampled ticker, takes the 1-minute candle at the T-minus-slice
     timestamp -- the same strictly-causal candle the study sampled -- and
     reads yes_bid / yes_ask off it.
  4. Prices the trade at the EXECUTABLE side of that book:
         YES: settled_yes       - yes_ask       - fee(yes_ask)
         NO : (1 - settled_yes) - (1 - yes_bid) - fee(1 - yes_bid)
     and reports mean EV per contract, a bootstrap 95% CI, and the median
     half-spread. Mid-price EV is printed alongside as the optimistic
     baseline, so the cost of crossing is visible as a single subtraction.

READ-ONLY. Reads the CSV, reads the study's candle cache, and -- unless
--no-network -- calls exactly one KalshiClient method, get_candlesticks(), a
read. It NEVER calls place_order() / cancel_order() and must never be edited
to. Its only write is the study's own candle cache.

It IMPORTS market_calibration_study rather than re-deriving anything, so the
fee model, the "an all-zero quote means missing, not zero" rule and the causal
candle-selection rule cannot drift from the study that produced the CSV.
market_calibration_study.py and test_longshot_rule.py are PINNED for a frozen
test; this script does not modify them.

CACHING
Candles come from the study's sqlite cache (--cache, default
data/calibration_study.sqlite) when present -- any cached window covering the
ticker is reused, not just the exact window key the study happened to fetch.
Anything missing is fetched once and cached, so a rerun is free. --no-network
turns the fetch off entirely and reports coverage off the cache alone.

Usage:
    python scripts/check_longshot_spread.py
    python scripts/check_longshot_spread.py --sample 300 --bootstrap 20000
    python scripts/check_longshot_spread.py --no-network        # cache only

CAVEATS -- read before trusting a number out of this
  - yes_ask / yes_bid are the CLOSE of the T-minus-slice minute candle, not a
    live book snapshot at the instant you would have sent the order. It is a
    far better fill estimate than the mid, but it is still an estimate.
  - An all-zero bid or ask means "no resting quote observed that minute", not
    a 0c quote. Those rows are DROPPED, not read as free contracts. Because a
    missing quote is likeliest in the thinnest markets, the surviving sample is
    biased towards the more liquid ones -- i.e. if anything this OVERSTATES the
    executable edge. Drop counts are printed; read them.
  - Taker-only. It assumes you cross. Resting a passive order might do better,
    but it might also never fill, and this study cannot measure that.
  - Fees are charged on the executable price, not on the mid: a fee quoted off
    a price you never paid is the wrong fee.
  - Top-of-book only. Depth is not modelled, so any size beyond the touch would
    fill worse than this.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import random
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse, never re-derive: fee_per_contract (the order-level ceiling divided by
# the order size, as the exchange charges it -- C is an explicit argument, see
# --contracts-per-order), _dollars (Kalshi's dollar-strings), candle_at_or_before
# (strictly causal) and the cache itself. Re-implementing any of these is how
# this check would silently stop measuring the same thing as the study.
from market_calibration_study import (  # noqa: E402
    _CANDLE_LEAD_MINUTES,
    _CANDLE_PERIOD_INTERVAL,
    StudyCache,
    _dollars,
    candle_at_or_before,
    fee_per_contract,
    fetch_candles_cached,
    sort_candles,
)

_DEFAULT_CSV = Path("data/calib_full.csv")
_DEFAULT_CACHE = Path("data/calibration_study.sqlite")
_DEFAULT_SLICE = 7
_DEFAULT_SAMPLE = 300
_DEFAULT_BOOTSTRAP = 20_000
_DEFAULT_SEED = 20260821
_HI = 0.70          # yes_price at or above this -> rule buys YES
_LO = 0.30          # yes_price at or below this -> rule buys NO
_MID_TOLERANCE = 5e-4   # CSV mid vs recomputed mid, in dollars (half a hundredth of a cent)
_PROGRESS_EVERY = 50


# ── Rows ─────────────────────────────────────────────────────────────────────

class Row:
    """One slice-`slice_minutes` observation from calib_full.csv."""

    __slots__ = ("series", "ticker", "close_ts", "slice_minutes",
                 "yes_price", "price_source", "settled_yes")

    def __init__(
        self,
        series: str,
        ticker: str,
        close_ts: int,
        slice_minutes: int,
        yes_price: float,
        price_source: str,
        settled_yes: bool,
    ) -> None:
        self.series = series
        self.ticker = ticker
        self.close_ts = close_ts
        self.slice_minutes = slice_minutes
        self.yes_price = yes_price
        self.price_source = price_source
        self.settled_yes = settled_yes


def rule_side(yes_price: float, hi: float, lo: float) -> str | None:
    """"YES" / "NO" if the longshot rule fires at this price, else None."""
    if yes_price >= hi:
        return "YES"
    if yes_price <= lo:
        return "NO"
    return None


def load_rows(path: Path, slice_minutes: int, hi: float, lo: float) -> list[Row]:
    """Rows at `slice_minutes` on which the rule fires, one per ticker, by close_ts."""
    if not path.exists():
        sys.exit(
            f"ERROR: {path} not found.\n"
            "This script re-prices that frozen CSV; it does not create it.\n"
            "Regenerate it (that DOES hit the network, ~30-60 min uncached) with:\n"
            "  python scripts/market_calibration_study.py --days 30 \\\n"
            "      --series KXBTC15M --series KXETH15M \\\n"
            "      --slices 14,12,10,7,5,2 --csv data/calib_full.csv"
        )

    by_ticker: dict[str, Row] = {}
    total = wrong_slice = unusable = no_fire = duplicate = 0
    with open(path, newline="") as f:
        for raw in csv.DictReader(f):
            total += 1
            try:
                if int(raw["slice_minutes"]) != slice_minutes:
                    wrong_slice += 1
                    continue
                row = Row(
                    series=raw["series"],
                    ticker=raw["ticker"],
                    close_ts=int(raw["close_ts"]),
                    slice_minutes=slice_minutes,
                    yes_price=float(raw["yes_price"]),
                    price_source=raw.get("price_source", ""),
                    settled_yes=bool(int(raw["settled_yes"])),
                )
            except (KeyError, TypeError, ValueError):
                unusable += 1
                continue
            # 0.00 / 1.00 is a degenerate quote, not a tradeable one -- the same
            # exclusion test_longshot_rule.py applies.
            if not 0.0 < row.yes_price < 1.0:
                unusable += 1
                continue
            if rule_side(row.yes_price, hi, lo) is None:
                no_fire += 1
                continue
            if row.ticker in by_ticker:
                duplicate += 1
                continue
            by_ticker[row.ticker] = row

    print(f"  read {total} rows: {wrong_slice} at other slices, {unusable} unusable, "
          f"{no_fire} where the rule does not fire, {duplicate} duplicate tickers")
    if not by_ticker:
        sys.exit(
            f"ERROR: no rows in {path} at slice {slice_minutes} where the rule fires "
            f"(yes_price >= {hi:.2f} or <= {lo:.2f})."
        )
    return sorted(by_ticker.values(), key=lambda r: r.close_ts)


def sample_evenly(rows: list[Row], want: int) -> list[Row]:
    """
    ~`want` rows spread evenly over the full close_ts span.

    The span is cut into `want` equal-DURATION strata and the row nearest each
    stratum's centre is taken. Even in calendar time, not in row count: a week
    that happened to produce more qualifying markets does not get more of the
    sample than a quiet one. Deterministic -- no RNG, so the sample is
    reproducible without a seed. Strata whose nearest row was already taken by
    a neighbour contribute nothing, so the result can come back slightly short
    of `want`; the achieved count is printed.
    """
    if want <= 0 or len(rows) <= want:
        return list(rows)

    first, last = rows[0].close_ts, rows[-1].close_ts
    span = last - first
    if span <= 0:
        return rows[:want]

    stamps = [r.close_ts for r in rows]
    picked: dict[int, Row] = {}
    for i in range(want):
        centre = first + span * (2 * i + 1) / (2 * want)
        # Nearest row to the stratum centre: bisect, then check both neighbours.
        j = bisect.bisect_left(stamps, centre)
        best_idx: int | None = None
        best_gap: float | None = None
        for k in (j - 1, j):
            if 0 <= k < len(rows):
                gap = abs(stamps[k] - centre)
                if best_gap is None or gap < best_gap:
                    best_idx, best_gap = k, gap
        if best_idx is not None:
            picked.setdefault(best_idx, rows[best_idx])
    return sorted(picked.values(), key=lambda r: r.close_ts)


# ── Candles ──────────────────────────────────────────────────────────────────

def cached_candles_any_window(db: sqlite3.Connection, ticker: str) -> list[dict]:
    """
    Every cached 1-minute candle for `ticker`, merged across cache windows.

    The study keys its cache by (ticker, start_ts, end_ts, period), and the
    exact start_ts depends on the --slices it was run with -- so an exact-key
    lookup would miss a perfectly good cached window fetched under a different
    slice set. Merging every window for the ticker and de-duplicating on
    end_period_ts reuses whatever is already there.
    """
    rows = db.execute(
        "SELECT raw_json FROM candles WHERE ticker = ? AND period_interval = ?",
        (ticker, _CANDLE_PERIOD_INTERVAL),
    ).fetchall()
    merged: dict[int, dict] = {}
    for (raw,) in rows:
        try:
            candles = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for candle in candles or []:
            end_ts = candle.get("end_period_ts")
            if end_ts is not None:
                merged[end_ts] = candle
    return sort_candles(list(merged.values()))


def quote_from_candle(candle: dict) -> tuple[float, float] | None:
    """
    (yes_bid, yes_ask) in dollars, or None if the book was not two-sided.

    An all-zero string is "no resting quote observed this period", not a 0c
    quote -- so it is missing data, and a missing side makes the trade
    unpriceable rather than free. A crossed or locked book (ask <= bid) is
    unusable here too: it means the two sides came from different moments
    inside the minute, so neither is a price you could have paid.
    """
    yes_bid = _dollars((candle.get("yes_bid") or {}).get("close_dollars"))
    yes_ask = _dollars((candle.get("yes_ask") or {}).get("close_dollars"))
    if yes_bid is None or yes_ask is None:
        return None
    if not 0.0 < yes_bid < 1.0 or not 0.0 < yes_ask < 1.0:
        return None
    if yes_ask <= yes_bid:
        return None
    return yes_bid, yes_ask


# ── EV ───────────────────────────────────────────────────────────────────────

class Fill:
    """One rule firing, priced at the executable side of the book."""

    __slots__ = ("row", "side", "yes_bid", "yes_ask", "entry", "fee",
                 "ev", "ev_mid", "half_spread")

    def __init__(self, row: Row, side: str, yes_bid: float, yes_ask: float,
                 coefficient: float, contracts: int) -> None:
        self.row = row
        self.side = side
        self.yes_bid = yes_bid
        self.yes_ask = yes_ask
        self.half_spread = (yes_ask - yes_bid) / 2.0

        settled = 1.0 if row.settled_yes else 0.0
        if side == "YES":
            # Buy YES by lifting the YES ask.
            self.entry = yes_ask
            payout = settled
            mid_entry = row.yes_price
            mid_payout = settled
        else:
            # Buy NO by lifting the NO ask, which is 1 - yes_bid.
            self.entry = 1.0 - yes_bid
            payout = 1.0 - settled
            mid_entry = 1.0 - row.yes_price
            mid_payout = 1.0 - settled

        # Fee is charged on the price actually paid, not on the mid, and the
        # ceiling applies once per order of `contracts` -- not once per contract.
        self.fee = fee_per_contract(self.entry, contracts, coefficient)
        self.ev = payout - self.entry - self.fee
        self.ev_mid = mid_payout - mid_entry - fee_per_contract(mid_entry, contracts, coefficient)


def bootstrap_ci(values: list[float], iterations: int, seed: int,
                 alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean, resampling with replacement."""
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iterations):
        draw = rng.choices(values, k=n)
        means.append(sum(draw) / n)
    means.sort()
    lo_idx = max(0, int(round(alpha / 2 * iterations)) - 1)
    hi_idx = min(iterations - 1, int(round((1 - alpha / 2) * iterations)) - 1)
    return means[lo_idx], means[hi_idx]


# ── Report ───────────────────────────────────────────────────────────────────

def describe(label: str, fills: list[Fill], iterations: int, seed: int) -> None:
    """One line: n, mean EV, bootstrap CI, and how often the trade actually won."""
    if not fills:
        print(f"  {label:<9} n=   0")
        return
    evs = [f.ev for f in fills]
    mean = sum(evs) / len(evs)
    lo, hi = bootstrap_ci(evs, iterations, seed)
    wins = sum(1 for f in fills if f.ev > 0)
    print(f"  {label:<9} n={len(evs):>4}   mean EV {mean * 100:+6.2f}c   "
          f"95% CI [{lo * 100:+6.2f}c, {hi * 100:+6.2f}c]   won {wins / len(evs):5.1%}")


def write_dump(path: Path, fills: list[Fill]) -> None:
    """Dump every priced fill, so the aggregation can be redone elsewhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "series", "ticker", "close_ts", "close_iso", "slice_minutes",
            "yes_price", "yes_bid", "yes_ask", "half_spread", "side", "entry",
            "fee", "settled_yes", "ev_executable", "ev_mid",
        ])
        for fill in fills:
            row = fill.row
            writer.writerow([
                row.series, row.ticker, row.close_ts,
                datetime.fromtimestamp(row.close_ts, tz=timezone.utc).isoformat(),
                row.slice_minutes, f"{row.yes_price:.4f}", f"{fill.yes_bid:.4f}",
                f"{fill.yes_ask:.4f}", f"{fill.half_spread:.4f}", fill.side,
                f"{fill.entry:.4f}", f"{fill.fee:.4f}",
                int(row.settled_yes), f"{fill.ev:.4f}", f"{fill.ev_mid:.4f}",
            ])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV,
                        help=f"Frozen calibration CSV to re-price (default {_DEFAULT_CSV})")
    parser.add_argument("--cache", type=Path, default=_DEFAULT_CACHE,
                        help=f"The study's sqlite candle cache (default {_DEFAULT_CACHE})")
    parser.add_argument("--slice", type=int, default=_DEFAULT_SLICE,
                        help=f"Minutes before close to price at (default {_DEFAULT_SLICE})")
    parser.add_argument("--sample", type=int, default=_DEFAULT_SAMPLE,
                        help=f"Approx tickers to sample, evenly over the date range "
                             f"(default {_DEFAULT_SAMPLE}; 0 = all)")
    parser.add_argument("--hi", type=float, default=_HI,
                        help=f"yes_price at or above which the rule buys YES (default {_HI})")
    parser.add_argument("--lo", type=float, default=_LO,
                        help=f"yes_price at or below which the rule buys NO (default {_LO})")
    parser.add_argument("--bootstrap", type=int, default=_DEFAULT_BOOTSTRAP,
                        help=f"Bootstrap resamples for the CI (default {_DEFAULT_BOOTSTRAP})")
    parser.add_argument("--seed", type=int, default=_DEFAULT_SEED,
                        help=f"Bootstrap RNG seed (default {_DEFAULT_SEED})")
    parser.add_argument("--fee-coefficient", type=float, default=0.07,
                        help="Kalshi taker fee coefficient (default 0.07)")
    parser.add_argument("--contracts-per-order", type=int, default=10,
                        help="Contracts per order, inside the order-level fee ceiling "
                             "(default 10, pre-committed in KILL_CRITERIA.md section 2)")
    parser.add_argument("--no-network", action="store_true",
                        help="Use only candles already cached; never call the API")
    parser.add_argument("--sleep", type=float, default=0.25,
                        help="Seconds between candle fetches (default 0.25)")
    parser.add_argument("--dump", type=Path, default=None,
                        help="Optional CSV of every priced fill, for inspection")
    args = parser.parse_args()

    print("Longshot rule vs the spread -- READ-ONLY")
    print(f"  csv    : {args.csv}")
    print(f"  slice  : T-{args.slice}min")
    print(f"  rule   : buy YES at yes_price >= {args.hi:.2f}, "
          f"buy NO at yes_price <= {args.lo:.2f}")
    print(f"  cache  : {args.cache}{'  (--no-network)' if args.no_network else ''}")
    print(f"  fee    : order-level ceiling, C={args.contracts_per_order} contracts/order")

    rows = load_rows(args.csv, args.slice, args.hi, args.lo)
    print(f"  {len(rows)} distinct tickers fire the rule at T-{args.slice}min")

    sampled = sample_evenly(rows, args.sample)
    first = datetime.fromtimestamp(sampled[0].close_ts, tz=timezone.utc)
    last = datetime.fromtimestamp(sampled[-1].close_ts, tz=timezone.utc)
    print(f"  sampled {len(sampled)} evenly over "
          f"{first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M} UTC")

    if args.no_network and not args.cache.exists():
        sys.exit(
            f"ERROR: --no-network was given but the candle cache {args.cache} does not exist.\n"
            "Nothing can be priced without candles. Drop --no-network to fetch them "
            "(read-only get_candlesticks calls, cached for reruns)."
        )

    cache = StudyCache(args.cache)
    reader = sqlite3.connect(str(args.cache))
    client = None
    if not args.no_network:
        # Imported lazily so --no-network needs no credentials at all.
        from kbot.config import settings
        from kbot.kalshi_client import KalshiClient
        client = KalshiClient(settings)

    fills: list[Fill] = []
    no_candle = no_quote = mid_mismatch = 0
    try:
        for i, row in enumerate(sampled, start=1):
            target = row.close_ts - args.slice * 60
            candle = candle_at_or_before(cached_candles_any_window(reader, row.ticker), target)
            if candle is None and client is not None:
                fetched = fetch_candles_cached(
                    client, cache, row.series, row.ticker,
                    row.close_ts - (args.slice + _CANDLE_LEAD_MINUTES) * 60, row.close_ts,
                    refresh=False, sleep_seconds=args.sleep,
                )
                candle = candle_at_or_before(sort_candles(fetched), target)
            if candle is None:
                no_candle += 1
                continue

            quote = quote_from_candle(candle)
            if quote is None:
                no_quote += 1
                continue
            yes_bid, yes_ask = quote

            # Integrity check: where the CSV's price came from a mid, the mid of
            # the candle just read must reproduce it. A mismatch means this is
            # not the candle the study looked at, and every number below is off.
            if row.price_source == "mid" and abs((yes_bid + yes_ask) / 2.0 - row.yes_price) > _MID_TOLERANCE:
                mid_mismatch += 1

            side = rule_side(row.yes_price, args.hi, args.lo)
            fills.append(Fill(row, side, yes_bid, yes_ask, args.fee_coefficient,
                              args.contracts_per_order))

            if i == 1 or i % _PROGRESS_EVERY == 0:
                print(f"  priced {i}/{len(sampled)} ({len(fills)} usable)")
    finally:
        reader.close()
        cache.close()
        if client is not None:
            client.close()

    print(f"\nDropped: {no_candle} with no causal candle at T-{args.slice}min, "
          f"{no_quote} whose candle had no two-sided quote.")
    if mid_mismatch:
        print(f"WARNING: {mid_mismatch} rows whose CSV price_source is 'mid' do NOT match the "
              "mid of the candle read here -- the CSV and the cache are out of sync. "
              "Regenerate one or the other before believing these numbers.")
    if not fills:
        sys.exit("ERROR: nothing was priceable. Nothing to report.")

    half_spreads = [f.half_spread for f in fills]
    mean_mid = sum(f.ev_mid for f in fills) / len(fills)
    mean_exec = sum(f.ev for f in fills) / len(fills)

    print(f"\n=== Longshot rule at the EXECUTABLE price, T-{args.slice}min "
          f"({len(fills)} contracts) ===")
    describe("ALL", fills, args.bootstrap, args.seed)
    describe("buy YES", [f for f in fills if f.side == "YES"], args.bootstrap, args.seed)
    describe("buy NO", [f for f in fills if f.side == "NO"], args.bootstrap, args.seed)

    print(f"\n  median half-spread   {statistics.median(half_spreads) * 100:5.2f}c "
          f"(mean {sum(half_spreads) / len(half_spreads) * 100:.2f}c, "
          f"max {max(half_spreads) * 100:.2f}c)")
    print(f"  mean EV at the mid  {mean_mid * 100:+6.2f}c/contract  <- the study's upper bound")
    print(f"  mean EV executable  {mean_exec * 100:+6.2f}c/contract")
    print(f"  cost of crossing    {(mean_mid - mean_exec) * 100:6.2f}c/contract")

    print(f"\n  => at the executable price the rule is "
          f"{'POSITIVE' if mean_exec > 0 else 'NEGATIVE'} on this sample.")
    print("     A positive mean whose 95% CI straddles zero is not an edge. Read the CI.")

    if args.dump:
        write_dump(args.dump, fills)
        print(f"\nWrote {len(fills)} priced fills to {args.dump}")


if __name__ == "__main__":
    main()
