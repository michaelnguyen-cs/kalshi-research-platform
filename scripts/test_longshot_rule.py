"""
Out-of-sample test of the favorite-longshot bias on KXBTC15M / KXETH15M.

WHY THIS EXISTS
---------------
The 30-day calibration study (scripts/market_calibration_study.py) found that
aggregate calibration is near-perfect -- every TOTAL row's realized-minus-
implied diff sits at ~0 -- while individual price buckets show a coherent
favorite-longshot pattern: cheap buckets settle LESS often than their price
implies, expensive buckets settle MORE often. That is the direction the
published Kalshi literature reports, so it is a plausible real effect.

It is also exactly the shape a post-hoc bucket hunt produces by chance. Those
flagged cells were picked out of ~240 (series x slice x bucket), so in-sample
they prove nothing. This script re-tests the pattern the only way that counts:
fix ONE rule, then run that frozen rule on the later half and see whether it
still makes money after real fees.

WHERE THE RULE COMES FROM -- THE POINT OF THE TWO MODES
-------------------------------------------------------
A frozen rule is only frozen if it is written down somewhere the script cannot
edit. This script therefore has two modes, and only one of them is a test:

  * TEST MODE (--rule-threshold T --rule-slice S). The rule is an INPUT. The
    in-sample sweep is skipped entirely -- not run, not printed, not consulted.
    The script evaluates exactly the rule it was handed on the out-of-sample
    half. This is the mode that produces evidence, and it is the mode
    KILL_CRITERIA.md section 2's pinned rule must be run in.

  * DISCOVERY MODE (neither argument). The script sweeps thresholds x slices
    on the in-sample half and takes the argmax. That NAMES a candidate rule;
    it does not test one. Its output is labelled as discovery throughout.

This script used to have only the second mode and called its result a test:
every invocation re-derived the rule from the sweep, so the "frozen" rule
silently followed whatever the argmax happened to be. When the fee model was
corrected the argmax moved from >= 0.70 to >= 0.55 and the script duly reported
an out-of-sample PASS on a rule section 2 does not pre-register. See
KILL_CRITERIA.md section 9 (2026-08-21). The rule is now an argument so that
the document governs the code.

READS data/calib_full.csv AND NOTHING ELSE. No network, no API, no cache. The
CSV is the frozen record; rerunning this script can never change the data it is
judged against.

DIRECTION -- THE EASY WAY TO GET THIS BACKWARDS
-----------------------------------------------
calib_full.csv carries an `ev_cheaper_side` column: the EV of buying whichever
side is CHEAPER. Under a favorite-longshot bias the cheaper side is the LOSING
side, so that column measures the wrong trade. This script computes its own EV
from `yes_price` and `settled_yes`, buying the FAVORITE (the side priced above
0.50). `ev_cheaper_side` is used only as control (c) and as a load-time
cross-check that this script's fee/EV arithmetic reproduces the study's.

FEES
----
The real Kalshi taker fee, rounded UP to the next cent ONCE PER ORDER, with the
contract count inside the ceiling:

    order_fee = ceil(100 * 0.07 * C * P * (1 - P)) / 100

imported from market_calibration_study at C = 10 contracts per order, the size
pre-committed in KILL_CRITERIA.md section 2. This script previously carried its
own copy of the formula with C fixed at 1, which overstated the fee by roughly
0.4-0.9c/contract; see KILL_CRITERIA.md section 9 (2026-08-21).

Charged on the entry price of the side actually bought. Note the fee is
symmetric -- fee(P) == fee(1-P) -- which gives a useful identity used as a
self-check below: buying a random side of a market costs exactly the fee in
expectation, no more and no less.

WHAT THIS DOES NOT MODEL
------------------------
  * Fills. Prices are the study's causal T-minus-N samples (two-sided mid, or
    last trade). Buying the favorite at the mid is optimistic; you would cross
    a real spread. Every EV here is an UPPER BOUND.
  * Size. Every trade is one contract. Thin markets count as much as deep ones.
  * Slippage, queue position, and the fact that a favorite at 0.90 may simply
    not have 100 contracts offered.
So a marginally-positive result is not a tradeable edge. A NEGATIVE result,
though, is conclusive -- costs only go up from here.

CLUSTERED INFERENCE (--cluster) -- A DIAGNOSTIC, NOT A NEW LOOK
---------------------------------------------------------------
The bootstrap below resamples TRADES i.i.d. Every quarter-hour can contribute
both a KXBTC15M and a KXETH15M market, and those two share crypto direction,
volatility regime, time-of-day and liquidity conditions; consecutive
quarter-hours may share the same regimes again. Trades are therefore not
plausibly independent, and analyze_crypto_slow.py already clustered on shock
while this script treated its trades as independent. That inconsistency needs
to be characterised before a fresh confirmatory test is designed.

--cluster {none,close_ts,day} adds a read-only DIAGNOSTIC section that
re-analyses the already-consumed pilot sample under alternative resampling
units. It exists to MEASURE DEPENDENCE and to size a future new-data-only
confirmation. It is emphatically NOT a new PASS/FAIL look:

  * The frozen rule, threshold, slice, fee model, split date, trade set,
    estimand and PASS bar are untouched.
  * The reported RULE result, the controls, the per-series split and the
    VERDICT block are always computed i.i.d., exactly as before. --cluster
    never feeds them.
  * With --cluster none (the default) nothing extra is printed at all, so a
    pinned re-run is byte-identical to the pre-change script.

Read the three units as:
    none      legacy comparison -- the frozen i.i.d. unit, reproduced exactly.
    close_ts  within-quarter-hour dependence diagnostic (BTC and ETH sharing a
              close_ts are always drawn together, never split).
    day       the PROPOSED primary clustering unit for the future
              new-data-only confirmatory test.

Positive within-cluster dependence generally makes i.i.d. inference
ANTICONSERVATIVE, so a clustered CI is expected to be wider. That is a
tendency, not a mechanical guarantee: a clustered percentile bootstrap can
return a narrower realized interval on any particular sample. Do NOT choose
among the three units by which produces the most favourable CI. `day` was
named as the intended unit before these numbers were computed.

Each clustered replicate draws G clusters with replacement (G = the observed
number of clusters), concatenates ALL trades in the drawn clusters, and takes
the same TRADE-LEVEL mean EV the i.i.d. path takes. It is deliberately NOT an
unweighted mean of cluster means -- that would silently change the estimand.

USAGE
    # TEST MODE -- evaluate the pre-registered rule, no sweep:
    python scripts/test_longshot_rule.py --rule-threshold 0.70 --rule-slice 7

    # DISCOVERY MODE -- sweep in-sample to name a candidate rule (NOT a test):
    python scripts/test_longshot_rule.py

    python scripts/test_longshot_rule.py --csv data/calib_full.csv --bootstrap 20000

    # DEPENDENCE DIAGNOSTIC -- adds the clustering table, verdict unchanged:
    python scripts/test_longshot_rule.py --rule-threshold 0.70 --rule-slice 7 \
        --cluster day

(Named test_* for readability, not for pytest; pyproject's testpaths is
["tests"], so a bare `pytest` run does not collect this file.)
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse, never re-derive. This script used to keep a private copy of the fee
# formula "mirroring" the study; the two silently diverged the moment the study
# was corrected to the order-level ceiling. Importing the one implementation is
# what makes the cross-check in verify_ev_column meaningful.
from market_calibration_study import fee_per_contract  # noqa: E402

_DEFAULT_CSV = Path("data/calib_full.csv")
_FEE_COEFFICIENT = 0.07

# Contracts per order, inside the order-level fee ceiling. Pre-committed in
# KILL_CRITERIA.md section 2; not a free parameter and not exposed as a flag.
_CONTRACTS_PER_ORDER = 10
_THRESHOLDS = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
_BUCKET_WIDTH = 0.05
_N_BUCKETS = int(round(1.0 / _BUCKET_WIDTH))

# A sweep cell needs at least this many in-sample trades to be selectable. The
# max of a noisy statistic over many cells is drawn to whichever cell has the
# fewest samples; without a floor the "best" rule is reliably an n=3 fluke.
_MIN_INSAMPLE_TRADES = 100

_BOOTSTRAP_RESAMPLES = 10_000
_SEED = 20260820

# Share of rows allowed to disagree with the CSV's own ev_cheaper_side column
# before the run aborts. See verify_ev_column.
_EV_MISMATCH_TOLERANCE = 0.005

# -- Clustered-inference diagnostic ------------------------------------------
_CLUSTER_UNITS = ("none", "close_ts", "day")

# Below this many clusters a percentile bootstrap over clusters is unreliable:
# the resampling distribution is built from a handful of exchangeable blocks, so
# its tails are coarse and its coverage is not trustworthy. 30 is the
# conventional rule-of-thumb floor for cluster-robust inference; it is a warning
# threshold, not a hard error, because the diagnostic is still informative about
# the DIRECTION of dependence when it trips.
_MIN_CLUSTERS_RELIABLE = 30

# The published frozen result, for the mandatory "does --cluster none still
# reproduce the pre-change numbers?" self-check. Only applied when the pinned
# configuration is actually the one being run (see verify_legacy_reproduction).
# Source: data/oos_20260821_corrected.txt, KILL_CRITERIA.md section 9.
_FROZEN_RULE_THRESHOLD = 0.70
_FROZEN_RULE_SLICE = 7
_FROZEN_REFERENCE = {"n": 1873, "mean_ev": 0.0164, "ci_lo": 0.0018, "ci_hi": 0.0304}
# The frozen record prints 4 decimal places, so agreement is checked at that
# resolution rather than to full float precision.
_FROZEN_TOLERANCE = 5e-5


# -- Fees, sides, EV ---------------------------------------------------------

def favorite_side(yes_price: float) -> tuple[str, float]:
    """
    (side, entry_price) for buying the FAVORITE -- the side priced above 0.50.

    At exactly 0.50 there is no favorite; YES is returned so the function is
    total, but no rule in this script trades there (thresholds start at 0.55).
    """
    if yes_price >= 0.5:
        return "YES", yes_price
    return "NO", 1.0 - yes_price


def longshot_side(yes_price: float) -> tuple[str, float]:
    """(side, entry_price) for buying the LONGSHOT -- the side priced below 0.50."""
    side, entry = favorite_side(yes_price)
    return ("NO", 1.0 - entry) if side == "YES" else ("YES", 1.0 - entry)


def cheaper_side(yes_price: float) -> tuple[str, float]:
    """
    (side, entry_price) for buying whichever side is cheaper.

    Identical to longshot_side by construction -- kept separate because it is
    the study's framing (control (c)), and because the equivalence is a fact
    worth stating out loud rather than hiding behind a shared helper.
    """
    if yes_price <= 0.5:
        return "YES", yes_price
    return "NO", 1.0 - yes_price


def trade_ev(entry: float, won: bool, coefficient: float = _FEE_COEFFICIENT) -> float:
    """Realized after-fee P&L per contract: payout - entry - fee/contract."""
    return (
        (1.0 if won else 0.0) - entry
        - fee_per_contract(entry, _CONTRACTS_PER_ORDER, coefficient)
    )


def side_won(side: str, settled_yes: bool) -> bool:
    return settled_yes if side == "YES" else not settled_yes


# -- Data --------------------------------------------------------------------

class Observation:
    """One (market, time-slice) price sample paired with its settled outcome."""

    __slots__ = ("series", "ticker", "close_ts", "slice_minutes", "yes_price",
                 "price_source", "settled_yes", "ev_cheaper_side")

    def __init__(self, series, ticker, close_ts, slice_minutes, yes_price,
                 price_source, settled_yes, ev_cheaper_side):
        self.series = series
        self.ticker = ticker
        self.close_ts = close_ts
        self.slice_minutes = slice_minutes
        self.yes_price = yes_price
        self.price_source = price_source
        self.settled_yes = settled_yes
        self.ev_cheaper_side = ev_cheaper_side


def load_observations(path: Path) -> list[Observation]:
    """Parse calib_full.csv. Rows with unusable prices are dropped and counted."""
    if not path.exists():
        sys.exit(
            f"ERROR: {path} not found.\n"
            "This script reads that frozen CSV and nothing else -- it makes no API calls.\n"
            "Regenerate it (that DOES hit the network, ~30-60 min uncached) with:\n"
            "  python scripts/market_calibration_study.py --days 30 \\\n"
            "      --series KXBTC15M --series KXETH15M \\\n"
            "      --slices 14,12,10,7,5,2 --csv data/calib_full.csv"
        )

    observations: list[Observation] = []
    skipped = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                obs = Observation(
                    series=row["series"],
                    ticker=row["ticker"],
                    close_ts=int(row["close_ts"]),
                    slice_minutes=int(row["slice_minutes"]),
                    yes_price=float(row["yes_price"]),
                    price_source=row.get("price_source", ""),
                    settled_yes=bool(int(row["settled_yes"])),
                    ev_cheaper_side=float(row["ev_cheaper_side"]),
                )
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            # A 0.00 or 1.00 price is a degenerate quote, not a tradeable one.
            if not 0.0 < obs.yes_price < 1.0:
                skipped += 1
                continue
            observations.append(obs)

    if skipped:
        print(f"  dropped {skipped} unusable rows (unparseable, or price at 0.00/1.00)")
    if not observations:
        sys.exit(f"ERROR: no usable observations in {path}.")
    return observations


def verify_ev_column(observations: list[Observation], coefficient: float) -> None:
    """
    Cross-check: recomputing ev_cheaper_side from price + outcome must reproduce
    the CSV. A mismatch means this script's fee model has drifted from the study
    that wrote the file, and every number below would be quietly wrong.

    Judged on the FRACTION of disagreeing rows, not the worst one. A price
    written as exactly 0.5000 is a knife edge -- the study picked its side from
    the unrounded float, so a row a hair either way of the tie can legitimately
    flip sides and differ by a full dollar. Real fee drift shows up in nearly
    every row; a handful of ties is rounding, and must not abort the run.
    """
    mismatched = 0
    worst = 0.0
    for o in observations:
        side, entry = cheaper_side(o.yes_price)
        recomputed = trade_ev(entry, side_won(side, o.settled_yes), coefficient)
        diff = abs(recomputed - o.ev_cheaper_side)
        worst = max(worst, diff)
        if diff > 0.005:
            mismatched += 1
    fraction = mismatched / len(observations)
    ok = fraction <= _EV_MISMATCH_TOLERANCE
    verdict = "OK" if ok else "MISMATCH -- fee model has drifted from the study"
    print(f"  fee/EV cross-check vs ev_cheaper_side column: "
          f"{mismatched:,}/{len(observations):,} rows disagree ({fraction * 100:.3f}%), "
          f"max abs diff {worst:.4f}  [{verdict}]")
    if mismatched and ok:
        print("    (a few disagreements at prices of exactly 0.50 are tie-rounding, not drift)")
    if not ok:
        sys.exit("ERROR: refusing to report EV numbers that disagree with the source CSV.")


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def split_by_close_time(
    observations: list[Observation],
) -> tuple[list[Observation], list[Observation], int]:
    """
    Split into an earlier (in-sample) and later (out-of-sample) half by close time.

    The split is on the MARKET's close timestamp, so every observation of a
    given market -- all of its slices -- lands on the same side. Splitting on
    rows instead would leak the same market's outcome across the boundary.
    Markets sharing a close_ts (BTC and ETH both close on the quarter hour) stay
    together for the same reason, which is why the halves are not exactly equal.
    """
    close_by_ticker = {o.ticker: o.close_ts for o in observations}
    ordered = sorted(close_by_ticker.values())
    split_ts = ordered[len(ordered) // 2]
    earlier = [o for o in observations if o.close_ts < split_ts]
    later = [o for o in observations if o.close_ts >= split_ts]
    return earlier, later, split_ts


def describe_half(name: str, obs: list[Observation]) -> None:
    by_series: dict[str, set[str]] = defaultdict(set)
    for o in obs:
        by_series[o.series].add(o.ticker)
    tickers = {o.ticker for o in obs}
    lo = min(o.close_ts for o in obs)
    hi = max(o.close_ts for o in obs)
    print(f"  {name:<26} {len(obs):>7,} obs   {len(tickers):>6,} markets   "
          f"{iso(lo)} -> {iso(hi)}")
    for series in sorted(by_series):
        n_obs = sum(1 for o in obs if o.series == series)
        print(f"      {series:<22} {n_obs:>7,} obs   {len(by_series[series]):>6,} markets")


# -- Trade construction ------------------------------------------------------

def favorite_trades(
    observations: list[Observation],
    threshold: float,
    slice_minutes: int,
    coefficient: float,
) -> tuple[list[float], list[bool]]:
    """
    (per-contract after-fee EVs, win flags) for "buy the favorite at >= threshold,
    at slice T-`slice_minutes`min".

    At a single slice there is at most one observation per market, so these
    trades are one-per-market -- no market is counted twice, which is what makes
    the bootstrap below legitimate.
    """
    evs: list[float] = []
    wins: list[bool] = []
    for o in observations:
        if o.slice_minutes != slice_minutes:
            continue
        side, entry = favorite_side(o.yes_price)
        if entry < threshold:
            continue
        won = side_won(side, o.settled_yes)
        evs.append(trade_ev(entry, won, coefficient))
        wins.append(won)
    return evs, wins


def qualifying(
    observations: list[Observation], threshold: float, slice_minutes: int
) -> list[Observation]:
    """The observations the rule fires on -- shared by the rule and its controls."""
    return [
        o for o in observations
        if o.slice_minutes == slice_minutes and favorite_side(o.yes_price)[1] >= threshold
    ]


# -- Statistics --------------------------------------------------------------

def bootstrap_ci(
    values: list[float],
    resamples: int,
    seed: int,
    alpha: float = 0.05,
    cluster_keys: list | None = None,
) -> tuple[float, float]:
    """
    Percentile bootstrap CI for the mean.

    cluster_keys is None (the default) -> resamples trades i.i.d., which is the
    frozen behaviour. This branch is byte-identical to the pre-clustering
    implementation and consumes the RNG in exactly the same order, so a pinned
    re-run reproduces the frozen CI exactly.

    cluster_keys given -> resamples whole CLUSTERS with replacement, drawing G
    clusters where G is the observed number of clusters. All trades in a drawn
    cluster come along together (so BTC and ETH sharing a close_ts are never
    split), and the replicate statistic is the TRADE-LEVEL mean over the
    concatenated trades: sum of all drawn trades / count of all drawn trades.
    It is NOT an unweighted mean of cluster means -- that is a different
    estimand, and the estimand is frozen.

    Because clusters differ in size, the replicate denominator varies. That is
    correct and intended: it is the same trade-level mean the point estimate
    uses, evaluated on a resampled set of clusters.
    """
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    if cluster_keys is None:
        draws = rng.integers(0, len(arr), size=(resamples, len(arr)))
        means = arr[draws].mean(axis=1)
        return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))

    if len(cluster_keys) != len(values):
        raise ValueError(
            f"cluster_keys has {len(cluster_keys)} entries but there are "
            f"{len(values)} trades; they must be aligned one-to-one."
        )
    members: dict = defaultdict(list)
    for idx, key in enumerate(cluster_keys):
        members[key].append(idx)
    # sorted() so cluster order -- and therefore the RNG->cluster mapping -- is
    # deterministic for a given seed rather than dict-insertion dependent.
    ordered = [np.asarray(members[k], dtype=np.int64) for k in sorted(members)]
    sums = np.array([float(arr[m].sum()) for m in ordered], dtype=float)
    counts = np.array([len(m) for m in ordered], dtype=np.int64)
    n_clusters = len(ordered)
    if n_clusters < 2:
        return (float("nan"), float("nan"))
    draws = rng.integers(0, n_clusters, size=(resamples, n_clusters))
    means = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def cluster_keys_for(observations: list[Observation], unit: str) -> list | None:
    """
    Cluster key per observation, aligned with favorite_trades' output order.

    none      -> None, the i.i.d. path.
    close_ts  -> the market's close timestamp. BTC and ETH close on the same
                 quarter-hour, so their two trades land in one cluster.
    day       -> the UTC calendar day of close_ts. close_ts // 86400 is exactly
                 the UTC day index because the epoch is itself UTC midnight, so
                 this needs no timezone conversion and no DST handling.
    """
    if unit == "none":
        return None
    if unit == "close_ts":
        return [o.close_ts for o in observations]
    if unit == "day":
        return [o.close_ts // 86400 for o in observations]
    raise ValueError(f"unknown cluster unit {unit!r}; expected one of {_CLUSTER_UNITS}")


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial rate -- honest at small n and near 0/1."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def summarize(evs: list[float], wins: list[bool]) -> dict:
    n = len(evs)
    return {
        "n": n,
        "win_rate": (sum(wins) / n) if n else float("nan"),
        "mean_ev": (sum(evs) / n) if n else float("nan"),
        "total_ev": sum(evs),
    }


# -- Step 2: in-sample sweep -------------------------------------------------

def run_sweep(
    in_sample: list[Observation],
    slices: list[int],
    coefficient: float,
    min_trades: int,
) -> tuple[list[dict], dict | None]:
    """Full (threshold x slice) grid on the in-sample half. Returns (all cells, best)."""
    print(f"\n  Sweeping {len(_THRESHOLDS)} thresholds x {len(slices)} slices "
          f"= {len(_THRESHOLDS) * len(slices)} cells, IN-SAMPLE ONLY.")
    print(f"  A cell needs n >= {min_trades} in-sample trades to be selectable.\n")
    header = (f"  {'slice':>8}{'T':>7}{'n':>8}{'win rate':>11}"
              f"{'mean EV/ct':>13}{'total EV':>12}   note")
    print(header)
    print("  " + "-" * (len(header) - 2))

    cells: list[dict] = []
    for slice_minutes in slices:
        for threshold in _THRESHOLDS:
            evs, wins = favorite_trades(in_sample, threshold, slice_minutes, coefficient)
            cell = summarize(evs, wins)
            cell.update(threshold=threshold, slice_minutes=slice_minutes)
            cell["eligible"] = cell["n"] >= min_trades
            cells.append(cell)
            note = "" if cell["eligible"] else f"(n < {min_trades}: not selectable)"
            label = f"T-{slice_minutes}"
            if cell["n"] == 0:
                print(f"  {label:>8}{threshold:>7.2f}{0:>8}"
                      f"{'--':>11}{'--':>13}{'--':>12}   {note}")
            else:
                print(f"  {label:>8}{threshold:>7.2f}{cell['n']:>8,}"
                      f"{cell['win_rate'] * 100:>10.1f}%{cell['mean_ev']:>+13.4f}"
                      f"{cell['total_ev']:>+12.2f}   {note}")
        print()

    eligible = [c for c in cells if c["eligible"]]
    if not eligible:
        return cells, None
    best = max(eligible, key=lambda c: (c["mean_ev"], c["n"]))
    return cells, best


# -- Step 6: calibration curve -----------------------------------------------

def calibration_curve(observations: list[Observation], title: str, note: str = "") -> None:
    """Realized YES settle rate vs implied price, in 5-cent buckets."""
    print(f"\n  {title}   (n={len(observations):,})")
    if note:
        print(f"  {note}")
    if not observations:
        print("      no observations")
        return
    print(f"  {'bucket':>12}{'n':>8}{'mean price':>12}{'realized':>11}"
          f"{'diff':>9}   95% CI on realized")
    print("  " + "-" * 76)

    buckets: dict[int, list[Observation]] = defaultdict(list)
    for o in observations:
        buckets[min(int(o.yes_price / _BUCKET_WIDTH), _N_BUCKETS - 1)].append(o)

    for index in range(_N_BUCKETS):
        rows = buckets.get(index)
        if not rows:
            continue
        n = len(rows)
        implied = sum(o.yes_price for o in rows) / n
        yes_count = sum(1 for o in rows if o.settled_yes)
        realized = yes_count / n
        lo, hi = wilson_interval(yes_count, n)
        label = f"{index * _BUCKET_WIDTH:.2f}-{(index + 1) * _BUCKET_WIDTH:.2f}"
        # Flag only where the CI on realized excludes the implied price. A diff
        # whose CI straddles the price is noise, however large it looks.
        flag = "  <-- off" if (implied < lo or implied > hi) else ""
        print(f"  {label:>12}{n:>8,}{implied:>12.3f}{realized:>11.3f}"
              f"{realized - implied:>+9.3f}   [{lo:.3f}, {hi:.3f}]{flag}")


# -- Reporting ---------------------------------------------------------------

def report_cluster_diagnostic(
    evs: list[float],
    fired: list[Observation],
    resamples: int,
    seed: int,
    selected: str,
    rule_stats: dict,
    threshold: float,
    slice_minutes: int,
) -> dict:
    """
    Read-only dependence diagnostic on the rule's out-of-sample trades.

    Re-analyses the ALREADY-CONSUMED pilot sample under three resampling units.
    Changes nothing: the caller's rule result, controls, per-series split and
    VERDICT are computed i.i.d. and are not touched here. This is a measurement
    of dependence for sizing a future new-data-only confirmatory test.

    Returns the row for `selected`, so the VERDICT block can echo that CI
    without running a second bootstrap. Recomputing it there would open a
    separate RNG path that could print a slightly different interval from the
    one in the table above it, for no gain.
    """
    print("\n" + "-" * 84)
    print("3b. DEPENDENCE DIAGNOSTIC -- clustered resampling (NOT a new PASS/FAIL look)")
    print("-" * 84)
    print("  Re-analysis of the same already-consumed trades under alternative")
    print("  resampling units. The rule, threshold, slice, fee model, split, trade")
    print("  set, estimand and PASS bar are unchanged, and the VERDICT below is")
    print("  computed i.i.d. exactly as before.\n")

    rows = []
    for unit in _CLUSTER_UNITS:
        keys = cluster_keys_for(fired, unit)
        lo, hi = bootstrap_ci(evs, resamples, seed, cluster_keys=keys)
        rows.append({
            "unit": unit,
            "n": len(evs),
            "clusters": len(evs) if keys is None else len(set(keys)),
            "mean": sum(evs) / len(evs),
            "lo": lo,
            "hi": hi,
            "width": hi - lo,
        })
    base = rows[0]

    # -- Mandatory gate: 'none' must still reproduce the legacy numbers --------
    if not verify_legacy_reproduction(base, rule_stats, threshold, slice_minutes):
        sys.exit(
            "ERROR: --cluster none did not reproduce the unclustered result, so the "
            "clustered numbers cannot be trusted as a like-for-like comparison. "
            "Refusing to report them. See the mismatch printed above."
        )

    print(f"  {'unit':<10}{'trades':>8}{'clusters':>10}{'mean EV':>11}"
          f"{'95% CI':>22}{'width':>10}{'infl':>8}{'deff':>8}")
    for r in rows:
        infl = r["width"] / base["width"] if base["width"] > 0 else float("nan")
        note = "  <-- SELECTED" if r["unit"] == selected else ""
        print(f"  {r['unit']:<10}{r['n']:>8,}{r['clusters']:>10,}{r['mean']:>+11.4f}"
              f"   [{r['lo']:+.4f}, {r['hi']:+.4f}]{r['width']:>10.4f}"
              f"{infl:>8.3f}{infl ** 2:>8.3f}{note}")
    print("\n  infl = clustered CI width / unclustered CI width")
    print("  deff = infl^2, the variance design effect")
    print(f"  All three use the same seed ({seed}) and the same "
          f"{resamples:,} replicates.")

    day_row = next(r for r in rows if r["unit"] == "day")
    if day_row["clusters"] < _MIN_CLUSTERS_RELIABLE:
        print(f"\n  !! WARNING: only {day_row['clusters']} day clusters "
              f"(< {_MIN_CLUSTERS_RELIABLE}). A percentile bootstrap over this few")
        print("     clusters has poor finite-sample reliability: the resampling")
        print("     distribution is built from a handful of exchangeable blocks, its")
        print("     tails are coarse, and its coverage should not be trusted as a")
        print("     calibrated interval. Read the day row as a DIRECTIONAL indication")
        print("     of dependence, not as a usable CI, and size the confirmatory test")
        print("     to deliver enough day clusters for this unit to be sound.")

    print("\n  Positive within-cluster dependence generally makes i.i.d. inference")
    print("  anticonservative, so a wider clustered CI is the expected direction --")
    print("  but that is a tendency, not a mechanical guarantee, and a clustered")
    print("  bootstrap can return a narrower realized interval on any one sample.")
    print("  'day' was named as the intended unit for the future confirmatory test")
    print("  BEFORE these numbers existed. Do not re-pick the unit from this table.")

    return next(r for r in rows if r["unit"] == selected)


def verify_legacy_reproduction(
    base: dict, rule_stats: dict, threshold: float, slice_minutes: int
) -> bool:
    """
    True if --cluster none still reproduces the unclustered result.

    Two checks. The first always applies: the diagnostic's 'none' row must match
    the RULE result the main pipeline already computed, which is the same code
    path at the same seed. The second applies only when the pinned frozen
    configuration is the one being run, and compares against the PUBLISHED
    frozen numbers so a change that silently moved the frozen result is caught
    here rather than in a later document.
    """
    ok = True
    ref_ci = rule_stats.get("ci")
    if (base["n"] != rule_stats["n"]
            or abs(base["mean"] - rule_stats["mean_ev"]) > 1e-12
            or ref_ci is None
            or abs(base["lo"] - ref_ci[0]) > 1e-12
            or abs(base["hi"] - ref_ci[1]) > 1e-12):
        print("  !! --cluster none disagrees with the unclustered RULE result:")
        print(f"       diagnostic : n={base['n']:,} mean={base['mean']:+.6f} "
              f"CI=[{base['lo']:+.6f}, {base['hi']:+.6f}]")
        print(f"       rule result: n={rule_stats['n']:,} "
              f"mean={rule_stats['mean_ev']:+.6f} CI={ref_ci}")
        ok = False

    is_frozen_config = (
        abs(threshold - _FROZEN_RULE_THRESHOLD) < 1e-12
        and slice_minutes == _FROZEN_RULE_SLICE
        and base["n"] == _FROZEN_REFERENCE["n"]
    )
    if is_frozen_config:
        ref = _FROZEN_REFERENCE
        drift = {
            "mean_ev": abs(base["mean"] - ref["mean_ev"]),
            "ci_lo": abs(base["lo"] - ref["ci_lo"]),
            "ci_hi": abs(base["hi"] - ref["ci_hi"]),
        }
        if any(d > _FROZEN_TOLERANCE for d in drift.values()):
            print("  !! --cluster none does not reproduce the PUBLISHED frozen result:")
            print(f"       frozen record : n={ref['n']:,} mean={ref['mean_ev']:+.4f} "
                  f"CI=[{ref['ci_lo']:+.4f}, {ref['ci_hi']:+.4f}]")
            print(f"       this run      : n={base['n']:,} mean={base['mean']:+.4f} "
                  f"CI=[{base['lo']:+.4f}, {base['hi']:+.4f}]")
            print(f"       drift         : {drift}")
            ok = False
        else:
            print(f"  legacy check: --cluster none reproduces the frozen record "
                  f"(n={ref['n']:,}, mean {ref['mean_ev']:+.4f}, "
                  f"CI [{ref['ci_lo']:+.4f}, {ref['ci_hi']:+.4f}])  [OK]\n")
    else:
        print("  legacy check: --cluster none matches this run's unclustered RULE "
              "result.\n               (Not the pinned frozen configuration, so the "
              "published-numbers check was skipped.)\n")
    return ok


def report_result(label: str, evs: list[float], wins: list[bool],
                  resamples: int, seed: int, indent: str = "  ") -> dict:
    stats = summarize(evs, wins)
    if stats["n"] == 0:
        print(f"{indent}{label:<34} n=0  -- no trades")
        return stats
    lo, hi = bootstrap_ci(evs, resamples, seed)
    stats["ci"] = (lo, hi)
    print(f"{indent}{label:<34} n={stats['n']:>6,}  win {stats['win_rate'] * 100:>5.1f}%  "
          f"mean EV {stats['mean_ev']:>+8.4f}  total {stats['total_ev']:>+9.2f}  "
          f"95% CI [{lo:+.4f}, {hi:+.4f}]")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV,
                        help=f"Frozen calibration CSV (default {_DEFAULT_CSV})")
    parser.add_argument("--rule-threshold", type=float, default=None, metavar="T",
                        help="Price threshold of the rule to test out-of-sample, e.g. "
                             "0.70. Must be given together with --rule-slice; when both "
                             "are given the in-sample sweep is SKIPPED ENTIRELY and only "
                             "this rule is evaluated. Omit both to run the sweep in "
                             "discovery mode instead.")
    parser.add_argument("--rule-slice", type=int, default=None, metavar="S",
                        help="Slice of the rule to test out-of-sample, in minutes before "
                             "close, e.g. 7. Must be given together with "
                             "--rule-threshold.")
    parser.add_argument("--bootstrap", type=int, default=_BOOTSTRAP_RESAMPLES,
                        help=f"Bootstrap resamples (default {_BOOTSTRAP_RESAMPLES})")
    parser.add_argument("--seed", type=int, default=_SEED,
                        help=f"Seed for the bootstrap and the random-side control "
                             f"(default {_SEED})")
    parser.add_argument("--min-trades", type=int, default=_MIN_INSAMPLE_TRADES,
                        help=f"Min in-sample trades for a sweep cell to be selectable "
                             f"(default {_MIN_INSAMPLE_TRADES})")
    parser.add_argument("--fee-coefficient", type=float, default=_FEE_COEFFICIENT,
                        help=f"Kalshi taker fee coefficient (default {_FEE_COEFFICIENT})")
    parser.add_argument("--cluster", choices=_CLUSTER_UNITS, default="none",
                        help="Resampling unit for the DEPENDENCE DIAGNOSTIC section. "
                             "'none' (default) prints nothing extra and leaves the run "
                             "byte-identical to the frozen script. 'close_ts' or 'day' "
                             "adds a read-only table comparing all three units on the "
                             "rule's out-of-sample trades. This NEVER feeds the reported "
                             "RULE result, the controls, the per-series split or the "
                             "VERDICT -- those stay i.i.d. and the PASS bar is unchanged.")
    args = parser.parse_args()
    coefficient = args.fee_coefficient

    # A rule is a (threshold, slice) PAIR. Accepting one without the other would
    # mean silently filling the other half from the sweep -- exactly the
    # half-pinned, half-discovered rule this mode split exists to prevent.
    if (args.rule_threshold is None) != (args.rule_slice is None):
        parser.error(
            "--rule-threshold and --rule-slice must be given together; a rule is a "
            "(threshold, slice) pair. Give BOTH to test a pre-registered rule with no "
            "sweep, or NEITHER to run the in-sample sweep in discovery mode."
        )
    rule_is_pinned = args.rule_threshold is not None

    print("=" * 84)
    if rule_is_pinned:
        print("OUT-OF-SAMPLE TEST: favorite-longshot bias, KXBTC15M / KXETH15M")
    else:
        print("RULE DISCOVERY (NOT A TEST): favorite-longshot bias, KXBTC15M / KXETH15M")
    print("=" * 84)
    print(f"  source     : {args.csv}  (no network calls, no API)")
    print(f"  fee        : ceil(100 * {coefficient} * C * P * (1-P)) / 100 per ORDER "
          f"at C = {_CONTRACTS_PER_ORDER} contracts,")
    print(f"               i.e. {_CONTRACTS_PER_ORDER} contracts inside one ceiling, "
          f"charged on the side bought")
    print("  direction  : buying the FAVORITE (side priced above 0.50), NOT the cheaper side")
    if rule_is_pinned:
        print(f"  rule       : SUPPLIED AS ARGUMENT -- favorite at >= "
              f"{args.rule_threshold:.2f}, at T-{args.rule_slice}min")
        print("               the in-sample sweep will be SKIPPED")
    else:
        print("  rule       : NOT SUPPLIED -- one will be DISCOVERED by in-sample sweep")
        print("               pass --rule-threshold/--rule-slice to test a pinned rule")

    # -- 1. Load and split ---------------------------------------------------
    print("\n" + "-" * 84)
    print("1. DATA")
    print("-" * 84)
    observations = load_observations(args.csv)
    verify_ev_column(observations, coefficient)

    slices = sorted({o.slice_minutes for o in observations}, reverse=True)
    all_tickers = {o.ticker for o in observations}
    print(f"  loaded {len(observations):,} observations over {len(all_tickers):,} settled markets")
    print(f"  series : {', '.join(sorted({o.series for o in observations}))}")
    print(f"  slices : {', '.join('T-' + str(s) + 'min' for s in slices)}")
    for slice_minutes in slices:
        n = sum(1 for o in observations if o.slice_minutes == slice_minutes)
        print(f"      T-{slice_minutes:<3}min  {n:>7,} obs")

    in_sample, out_sample, split_ts = split_by_close_time(observations)
    if not in_sample or not out_sample:
        sys.exit("ERROR: the close-time split produced an empty half. "
                 "The CSV probably covers a single close timestamp.")
    print(f"\n  split at market close {iso(split_ts)} "
          f"(markets closing before it are in-sample; at/after, out-of-sample)")
    describe_half("IN-SAMPLE (earlier)", in_sample)
    describe_half("OUT-OF-SAMPLE (later)", out_sample)

    # -- 2. Where the rule comes from ----------------------------------------
    if rule_is_pinned:
        threshold = args.rule_threshold
        slice_minutes = args.rule_slice
        rule_source = "ARGUMENT"
        rule_source_detail = (
            "--rule-threshold / --rule-slice, supplied on the command line. "
            "No sweep ran."
        )
        print("\n" + "-" * 84)
        print("2. RULE UNDER TEST -- SUPPLIED AS AN ARGUMENT; NO SWEEP WAS RUN")
        print("-" * 84)
        print("  Both --rule-threshold and --rule-slice were given, so the in-sample sweep")
        print("  is skipped entirely: not run, not printed, not consulted. Nothing in this")
        print("  run inspects the in-sample half to choose a rule. The rule comes from")
        print("  outside the script, which is what makes the out-of-sample half below a")
        print("  test rather than a discovery.")
        if slice_minutes not in slices:
            sys.exit(f"ERROR: --rule-slice {slice_minutes} is not a slice present in "
                     f"{args.csv}. Available: "
                     f"{', '.join('T-' + str(s) + 'min' for s in slices)}.")
    else:
        rule_source = "SWEEP"
        rule_source_detail = (
            "in-sample argmax over the section-2 sweep, DISCOVERED during this run. "
            "Not pre-registered."
        )
        print("\n" + "-" * 84)
        print("2. RULE DISCOVERY -- NOT A TEST: in-sample sweep over threshold x slice")
        print("-" * 84)
        print("  No rule was supplied, so one is being DISCOVERED here by argmax over the")
        print("  in-sample half. Discovery is not evidence. Everything printed in this")
        print("  section is in-sample and inflated by selection; its only job is to NAME a")
        print("  candidate rule for a human to pre-register in KILL_CRITERIA.md.")
        print("  A rule that KILL_CRITERIA.md already pins must instead be passed in with")
        print("  --rule-threshold/--rule-slice, so the document governs the code.")
        cells, best = run_sweep(in_sample, slices, coefficient, args.min_trades)
        if best is None:
            sys.exit(f"ERROR: no sweep cell reached {args.min_trades} in-sample trades. "
                     "Nothing to evaluate out-of-sample.")

        n_cells = len(cells)
        n_eligible = sum(1 for c in cells if c["eligible"])
        print(f"  BEST IN-SAMPLE CELL: buy the favorite at >= {best['threshold']:.2f}, "
              f"at T-{best['slice_minutes']}min")
        print(f"    in-sample n={best['n']:,}  win {best['win_rate'] * 100:.1f}%  "
              f"mean EV {best['mean_ev']:+.4f}/contract  total {best['total_ev']:+.2f}")
        print()
        print("    THIS IN-SAMPLE NUMBER IS INFLATED AND MUST NOT BE QUOTED AS AN EDGE.")
        print(f"    It is the maximum over {n_cells} cells ({n_eligible} selectable), chosen after")
        print(f"    seeing the outcomes. The max of {n_eligible} noisy estimates is biased upward")
        print("    even when every underlying edge is exactly zero. Its only job is to name one")
        print("    rule, and the argmax moves whenever the arithmetic under it moves.")

        threshold = best["threshold"]
        slice_minutes = best["slice_minutes"]

    # The one line every reader of this output has to see: WHICH rule, and on
    # whose authority. A number here that nobody pre-registered is a discovery
    # wearing a test's clothes.
    print()
    print("*" * 84)
    print(f"*  RULE UNDER TEST : buy the FAVORITE at price >= {threshold:.2f}, "
          f"at the T-{slice_minutes}min slice")
    print(f"*  RULE SOURCE     : {rule_source}")
    print(f"*                    {rule_source_detail}")
    if rule_source == "SWEEP":
        print("*  WARNING         : this rule was chosen by looking at the data. An")
        print("*                    out-of-sample result on it is NOT a result on any")
        print("*                    pre-registered rule, whatever the verdict below says.")
    print("*" * 84)

    # -- 3. Out-of-sample evaluation -----------------------------------------
    print("\n" + "-" * 84)
    print(f"3. OUT-OF-SAMPLE: the rule under test -- buy the favorite at >= {threshold:.2f}, "
          f"at T-{slice_minutes}min")
    print("-" * 84)
    oos_slice_obs = [o for o in out_sample if o.slice_minutes == slice_minutes]
    fired = qualifying(out_sample, threshold, slice_minutes)
    print(f"  out-of-sample observations at T-{slice_minutes}min : {len(oos_slice_obs):,}")
    pct = (len(fired) / len(oos_slice_obs) * 100) if oos_slice_obs else 0.0
    print(f"  of those, the rule fires on                : {len(fired):,} ({pct:.1f}%)")
    print("  (one trade per market -- a single slice samples each market once)\n")

    rule_evs, rule_wins = favorite_trades(out_sample, threshold, slice_minutes, coefficient)
    rule = report_result("RULE: buy favorite", rule_evs, rule_wins, args.bootstrap, args.seed)
    if rule["n"] == 0:
        sys.exit(f"ERROR: the rule under test (favorite at >= {threshold:.2f}, "
                 f"T-{slice_minutes}min) never fires out-of-sample. Nothing to test.")
    print(f"    bootstrap: {args.bootstrap:,} resamples of {rule['n']:,} trades, "
          f"percentile method, seed {args.seed}")

    # -- 3b. Dependence diagnostic (opt-in, changes nothing) ------------------
    # `fired` is qualifying() over the same list with the same filters that
    # favorite_trades() applies, in the same order, so its close_ts values are
    # aligned one-to-one with rule_evs. bootstrap_ci re-checks that alignment.
    cluster_row: dict | None = None
    if args.cluster != "none":
        cluster_row = report_cluster_diagnostic(
            rule_evs, fired, args.bootstrap, args.seed, args.cluster,
            rule, threshold, slice_minutes,
        )

    # -- 4. Controls ---------------------------------------------------------
    print("\n" + "-" * 84)
    print("4. CONTROLS -- same out-of-sample half, same fee treatment")
    print("-" * 84)

    # (a) The longshot: the mirror trade on the IDENTICAL market set, since
    #     "favorite >= T" is the same condition as "longshot <= 1 - T".
    long_evs: list[float] = []
    long_wins: list[bool] = []
    for o in fired:
        side, entry = longshot_side(o.yes_price)
        won = side_won(side, o.settled_yes)
        long_evs.append(trade_ev(entry, won, coefficient))
        long_wins.append(won)
    print(f"  (a) buy the LONGSHOT at <= {1 - threshold:.2f} -- the mirror trade on the "
          f"same {len(fired):,} markets")
    control_long = report_result("CONTROL (a): buy longshot", long_evs, long_wins,
                                 args.bootstrap, args.seed + 1, indent="      ")

    # (b) Random side at matched prices, on the same market set.
    rng = random.Random(args.seed)
    rand_evs: list[float] = []
    rand_wins: list[bool] = []
    for o in fired:
        pick = favorite_side if rng.random() < 0.5 else longshot_side
        side, entry = pick(o.yes_price)
        won = side_won(side, o.settled_yes)
        rand_evs.append(trade_ev(entry, won, coefficient))
        rand_wins.append(won)
    print(f"\n  (b) buy a RANDOM side at matched prices -- same {len(fired):,} markets, "
          f"seeded coin flip")
    control_rand = report_result("CONTROL (b): random side", rand_evs, rand_wins,
                                 args.bootstrap, args.seed + 2, indent="      ")
    # The fee is symmetric, fee(P) == fee(1-P), and exactly one side pays out,
    # so favorite EV + longshot EV == -2 * fee identically. A random side
    # therefore earns exactly minus the fee in expectation -- a closed-form
    # check that the two directional numbers above are arithmetically consistent.
    mean_fee = sum(
        fee_per_contract(favorite_side(o.yes_price)[1], _CONTRACTS_PER_ORDER, coefficient)
        for o in fired
    ) / len(fired)
    analytic = (rule["mean_ev"] + control_long["mean_ev"]) / 2
    ok = abs(analytic + mean_fee) < 1e-9
    print(f"      identity check: (favorite + longshot)/2 = {analytic:+.4f} "
          f"must equal -mean fee = {-mean_fee:+.4f}  [{'OK' if ok else 'MISMATCH'}]")
    print("      (so a coin flip is a pure fee drag -- the seeded draw above is one "
          "realization of it)")

    # (c) Cheaper side. On the fired set this IS control (a), so it is reported
    #     on the full slice as well, which is the study's original framing.
    cheap_evs: list[float] = []
    cheap_wins: list[bool] = []
    for o in oos_slice_obs:
        side, entry = cheaper_side(o.yes_price)
        won = side_won(side, o.settled_yes)
        cheap_evs.append(trade_ev(entry, won, coefficient))
        cheap_wins.append(won)
    print(f"\n  (c) buy the CHEAPER side -- ALL {len(oos_slice_obs):,} out-of-sample "
          f"observations at T-{slice_minutes}min")
    print(f"      (on the rule's own {len(fired):,}-market subset the cheaper side IS the "
          f"longshot, i.e. control (a) verbatim)")
    control_cheap = report_result("CONTROL (c): cheaper side", cheap_evs, cheap_wins,
                                  args.bootstrap, args.seed + 3, indent="      ")

    # -- 5. Per-series split -------------------------------------------------
    print("\n" + "-" * 84)
    print("5. THE SAME RULE, SPLIT BY SERIES (out-of-sample)")
    print("-" * 84)
    per_series: dict[str, dict] = {}
    for series in sorted({o.series for o in out_sample}):
        series_obs = [o for o in out_sample if o.series == series]
        evs, wins = favorite_trades(series_obs, threshold, slice_minutes, coefficient)
        per_series[series] = report_result(series, evs, wins, args.bootstrap, args.seed + 4)

    positive = [s for s, r in per_series.items() if r["n"] > 0 and r["mean_ev"] > 0]
    negative = [s for s, r in per_series.items() if r["n"] > 0 and r["mean_ev"] <= 0]
    if positive and negative:
        print()
        print("  " + "!" * 80)
        print("  !! THE RULE DOES NOT HOLD ACROSS SERIES.")
        print(f"  !! positive: {', '.join(positive)}   non-positive: {', '.join(negative)}")
        print("  !! A pooled edge carried by one series is a one-series effect, not a")
        print("  !! favorite-longshot law. Do not deploy this pooled.")
        print("  " + "!" * 80)
    elif len(per_series) > 1 and not negative:
        print("\n  Both series point the same way (all mean EVs > 0).")

    # -- 6. Calibration curve ------------------------------------------------
    print("\n" + "-" * 84)
    print("6. OUT-OF-SAMPLE CALIBRATION CURVE -- does the pattern itself replicate?")
    print("-" * 84)
    print("  Independent of any trading rule. Under favorite-longshot bias, LOW buckets")
    print("  should sit BELOW their implied price (realized < implied, negative diff) and")
    print("  HIGH buckets ABOVE it.")
    calibration_curve(
        oos_slice_obs,
        f"At T-{slice_minutes}min (the rule's slice) -- one observation per market",
    )
    calibration_curve(
        out_sample,
        "Pooled across all slices",
        note="CAVEAT: each market appears once per slice, so these rows are NOT "
             "independent;\n  n is inflated and the CIs are too narrow. Read the shape, "
             "not the intervals.",
    )
    for series in sorted({o.series for o in out_sample}):
        calibration_curve(
            [o for o in oos_slice_obs if o.series == series],
            f"{series} at T-{slice_minutes}min",
        )

    # -- 7. Verdict ----------------------------------------------------------
    print("\n" + "=" * 84)
    print("7. VERDICT")
    print("=" * 84)
    print(f"  rule   : buy the FAVORITE at >= {threshold:.2f}, at T-{slice_minutes}min")
    print(f"  source : {rule_source} -- {rule_source_detail}")
    print()
    ci_lo, ci_hi = rule.get("ci", (float("nan"), float("nan")))
    checks = [
        ("out-of-sample mean EV per contract > 0",
         rule["mean_ev"] > 0, f"{rule['mean_ev']:+.4f}"),
        ("95% CI excludes zero (lower bound > 0)",
         ci_lo > 0, f"[{ci_lo:+.4f}, {ci_hi:+.4f}]"),
        ("beats control (a) buy the longshot",
         control_long["n"] > 0 and rule["mean_ev"] > control_long["mean_ev"],
         f"{rule['mean_ev']:+.4f} vs {control_long['mean_ev']:+.4f}"),
        ("beats control (b) random side",
         control_rand["n"] > 0 and rule["mean_ev"] > control_rand["mean_ev"],
         f"{rule['mean_ev']:+.4f} vs {control_rand['mean_ev']:+.4f}"),
        ("beats control (c) cheaper side",
         control_cheap["n"] > 0 and rule["mean_ev"] > control_cheap["mean_ev"],
         f"{rule['mean_ev']:+.4f} vs {control_cheap['mean_ev']:+.4f}"),
    ]
    for label, passed_check, detail in checks:
        print(f"  [{'PASS' if passed_check else 'FAIL'}]  {label:<44} {detail}")

    # Two informational lines, printed only when the dependence diagnostic ran.
    # Neither is a criterion and neither changes `checks` or `passed` above: the
    # CI is echoed verbatim from the section 3b row rather than re-bootstrapped,
    # so it cannot disagree with the table the reader just saw.
    if cluster_row is not None:
        cluster_label = (f"clustered 95% CI ({cluster_row['unit']}, "
                         f"{cluster_row['clusters']:,} clusters)")
        print(f"  [ -- ]  {cluster_label:<44} "
              f"[{cluster_row['lo']:+.4f}, {cluster_row['hi']:+.4f}]  not a criterion")
        print("          The criteria above are computed i.i.d. BY DESIGN and --cluster never")
        print("          feeds them; KILL_CRITERIA.md section 2 additionally requires the")
        print("          measured crossing cost, which this script does not subtract -- so a")
        print("          PASS printed below is NOT a section 2 PASS.")

    passed = all(c[1] for c in checks)
    print()
    if passed:
        print("  ***  PASS  ***")
        print(f"  The rule 'buy the favorite at >= {threshold:.2f}, at T-{slice_minutes}min' "
              f"survived out-of-sample")
        print(f"  on {rule['n']:,} trades, after real fees, and beat all three controls.")
        print("  Remaining caveats before calling this an edge: prices are mids, so fills are")
        print("  optimistic; size is one contract; and one out-of-sample half is one test, not")
        print("  a track record.")
        if rule_source == "SWEEP":
            print()
            print("  " + "!" * 80)
            print("  !! THIS PASS IS NOT A PASS OF ANY PRE-REGISTERED RULE.")
            print("  !! The rule was chosen by the sweep above, from this same dataset, during")
            print("  !! this run. Re-run with --rule-threshold/--rule-slice set to the rule")
            print("  !! KILL_CRITERIA.md pins if you want a result that counts. Note also that")
            print("  !! the out-of-sample half has now been looked at for this rule, and a look")
            print("  !! cannot be taken back.")
            print("  " + "!" * 80)
    else:
        print("  ***  FAIL  ***")
        print("  The out-of-sample result does not clear the bar. The in-sample buckets are")
        print("  best explained as post-hoc selection over ~240 cells, not a tradeable")
        print("  favorite-longshot bias. Do not build a strategy on this.")
    print("=" * 84)


if __name__ == "__main__":
    main()
