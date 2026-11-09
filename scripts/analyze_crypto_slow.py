#!/usr/bin/env python
"""
READ-ONLY analysis of recordings made by scripts/record_crypto_slow.py.

Two hypotheses, both pre-registered, both evaluated verbatim:

  H1 NEWS-RESPONSE LAG  After a crypto shock, do slow Kalshi crypto contracts
                        reprice over minutes-to-hours rather than instantly,
                        leaving a window we could trade into?
  H2 CROSS-VENUE SPREAD For the same real-world question listed on BOTH
                        Kalshi and Polymarket, how big is the price gap and
                        how long does it last?

This script touches no exchange at all: it reads sqlite and prints.  It
cannot place an order; nothing that can is imported.

    python scripts/analyze_crypto_slow.py --db data/crypto_slow.sqlite
    python scripts/analyze_crypto_slow.py --db a.sqlite --db b.sqlite
    python scripts/analyze_crypto_slow.py --db a.sqlite --emit-pair-review out.json

The PASS/FAIL criteria below were fixed in this source file BEFORE any
recording was analysed, and the git history of this file is the audit trail
for that claim.  They are printed in full before any number is computed.
Changing one after seeing a result invalidates the study.

WHAT THIS STUDY CAN AND CANNOT ESTABLISH
----------------------------------------
It measures REPRICING.  It cannot measure PROFITABILITY, because these
series settle roughly once a year and a 24-hour recording contains ~zero
settlements.  Section 6 states the sample size that would be required and
why it is not reachable on these series.  Anyone reading a PASS here as
"there is money in this" has skipped section 6.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

# Polymarket resolution texts are full of typographic quotes, and this report's
# whole point is to print them verbatim.  A Windows console defaults to cp1252
# and would raise UnicodeEncodeError halfway through the pair table, killing the
# run after the numbers but before the verdict.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ═════════════════════════════════════════════════════════════════════════════
# PRE-REGISTRATION.  Chosen from cost arithmetic and reaction-time realism,
# NOT from looking at what performed best.
# ═════════════════════════════════════════════════════════════════════════════

# --- H1 ----------------------------------------------------------------------
H1_REPRICE_CENTS = 2.0     # criterion 1: it must eventually move at all
H1_HORIZON_S = 3600        # "eventually" = 60 minutes
H1_REACTION_S = 60         # criterion 2: our reaction time, deliberately slow
H1_EDGE_CENTS = 3.0        # criterion 2: gap still available at that moment
H1_MIN_SIZE = 20           # criterion 3: contracts resting at that price
H1_MIN_SHOCKS = 30         # criterion 4: independent shock events

# Why 2c on criterion 1: below 2c the "repricing" is inside the tick structure
#   of a book that quotes in whole cents at most prices; there would be nothing
#   to be early to even if we were infinitely fast.
# Why 60s reaction: this is a REST-polled study on books that update a few
#   times an hour.  Claiming a 300ms reaction on a hypothesis whose whole
#   premise is "these books are slow and human-paced" would be flattering
#   ourselves; 60s is what a human-in-the-loop or a slow poller really costs.
# Why 3c edge: Kalshi's taker fee at p=0.50 is ceil(0.07*0.25*100) = 2c per
#   contract, and slow books quote wider than the 15-minute ones.  3c is the
#   loosest threshold that could conceivably clear cost, chosen deliberately
#   loose so a FAIL here is decisive rather than an artefact of a strict bar.
# Why 20 contracts: an edge on 3 contracts is not a strategy, and the lag
#   study used the same number -- keeping it identical prevents the quiet
#   ratchet where each study lowers the size bar a little.
# Why 30 shocks: the response is measured across many correlated markets, so
#   the effective sample size is the number of SHOCKS, not the number of
#   (shock, market) observations.  Every confidence interval below is
#   clustered on the shock for that reason.

H1_HORIZONS_S = (60, 300, 900, 1800, 3600, 21600)   # +1m +5m +15m +30m +60m +6h
H1_FULL_MOVE_HORIZON_S = 21600                       # the "eventual" move: +6h

# --- H2 ----------------------------------------------------------------------
H2_GAP_CENTS = 4.0         # criterion 1: median |Kalshi - Polymarket| price gap
H2_PERSIST_S = 60          # criterion 2: how long that gap must survive
# criterion 3: resolution criteria verified equivalent -- see EQUIVALENCE below.

# Why 4c: the gap has to cover BOTH venues' costs, and it is a two-legged
#   trade.  Kalshi taker fee ~2c at the money, Polymarket's taker fee is
#   currently zero but its spread is not, and each leg crosses a spread.
#   4c is the floor at which a two-leg round trip is not obviously negative.
# Why 60s: at a 10s poll interval, 60s is six independent observations -- the
#   shortest window at which "persisted" means something more than one sample.

# --- Alignment / staleness (guards, not tuning) ------------------------------
STALE_TOL_S = 180          # a quote older than this does not represent "now"
CROSS_VENUE_TOL_S = 30     # max age difference when comparing the two venues
WINDOW_TOL_H = 36          # observation-window start mismatch that blocks a pair
BOOTSTRAP_N = 10_000
RNG_SEED = 20260820        # fixed: the CIs must not move between runs

# --- Equivalence check (H2 criterion 3) --------------------------------------
# A pair is VERIFIED EQUIVALENT only if none of these blocking conditions
# holds.  Each is counted and reported separately so a FAIL says WHY.
#   B1 strike mismatch beyond the recorder's tolerance
#   B2 direction or touch/terminal style not known-and-equal on both sides
#   B3 resolution dates differ by more than the recorder's tolerance
#   B4 observation windows START at different times (a touch market that has
#      been watching since last November is not the same contract as one that
#      started watching in January, however identical the titles look)
#   B5 different named settlement source, or a different quote currency
#      (BRTI/USD vs Binance BTC/USDT is a different number, and for a ONE-TOUCH
#      contract a wick that prints on one and not the other flips the payout)
#   B6 one side can resolve/close early and the other cannot
#   B7 the Polymarket resolution text could not be parsed well enough to check
#      B4/B5 at all -- unverifiable is not the same as verified
BLOCKERS = {
    "B1": "strike mismatch",
    "B2": "direction/style not known-and-equal",
    "B3": "resolution date mismatch",
    "B4": "observation window starts differ",
    "B5": "different settlement source or quote currency",
    "B6": "early-resolution asymmetry",
    "B7": "resolution text unparseable (cannot verify)",
}

# Named price sources, normalised.  A pair whose two sides name different
# sources is not the same contract even when every other field agrees.
SOURCE_PATTERNS = (
    ("binance", re.compile(r"\bbinance\b", re.I)),
    ("coinbase", re.compile(r"\bcoinbase\b", re.I)),
    ("cf_benchmarks", re.compile(r"\bcf benchmarks\b|\bbrti\b|\bbrr\b", re.I)),
    ("chainlink", re.compile(r"\bchainlink\b", re.I)),
    ("kraken", re.compile(r"\bkraken\b", re.I)),
    ("pyth", re.compile(r"\bpyth\b", re.I)),
)
QUOTE_CCY_RX = re.compile(r"\b(?:BTC|ETH|SOL|XRP|DOGE|BNB|ZEC|NEAR|HYPE)[/_-](USDT|USDC|USD)\b", re.I)

# "between November 24, 2025, 15:45 and December 31, 2026, 23:59"
# "from 00:00 AM ET on the first day"
POLY_WINDOW_RX = re.compile(
    r"between\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})(?:,\s*(\d{1,2}:\d{2}))?\s+and", re.I)
POLY_MONTH_WINDOW_RX = re.compile(
    r"during the month specified in the title", re.I)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def PREREG_TEXT(args: argparse.Namespace) -> str:
    return f"""
================================================================================
PRE-REGISTRATION -- fixed in source BEFORE any result was seen
================================================================================
H1  NEWS-RESPONSE LAG on slow Kalshi crypto contracts.
    A SHOCK is a move of >= {args.shock_bps_note} in the recorder's window (whatever
    --shock-bps/--shock-window the recording used; the recorder stamped it).
    PASSES only if ALL FOUR hold:
      1. Median slow-market Kalshi MID moves by >= {H1_REPRICE_CENTS:.0f}c within
         {H1_HORIZON_S//60} minutes of a shock.  (If it never reprices, there is
         nothing to be early to and the hypothesis is empty.)
      2. At shock + {H1_REACTION_S}s -- our assumed reaction time -- the price we would
         actually PAY (best ask to buy, never the mid) is still >= {H1_EDGE_CENTS:.0f}c away
         from where the mid sits {H1_HORIZON_S//60} minutes after the shock.
      3. Median size resting at that price at that moment >= {H1_MIN_SIZE} contracts.
      4. >= {H1_MIN_SHOCKS} independent shock events observed.  Fewer = INSUFFICIENT DATA,
         and no other criterion can rescue it.
    Criterion 2 is measured IN THE DIRECTION THE MARKET ACTUALLY WENT, i.e.
    with perfect hindsight about the sign.  That makes it an UPPER BOUND on
    any real edge.  A FAIL is therefore decisive; a PASS is necessary but
    nowhere near sufficient.

H2  CROSS-VENUE SPREAD, Kalshi vs Polymarket, on CONFIDENTLY matched pairs
    (asset, strike, direction, touch-vs-terminal style and resolution date all
    agreed at record time; title similarity was never used).
    PASSES only if ALL THREE hold:
      1. Median |Kalshi price - Polymarket price| >= {H2_GAP_CENTS:.0f}c.
      2. That gap persists >= {H2_PERSIST_S}s (median duration of a >= {H2_GAP_CENTS:.0f}c episode).
      3. Resolution criteria VERIFIED EQUIVALENT, blocking on any of:
         {'; '.join(f'{k} {v}' for k, v in list(BLOCKERS.items())[:4])};
         {'; '.join(f'{k} {v}' for k, v in list(BLOCKERS.items())[4:])}.
         Survivors and every drop reason are counted and printed.

ANYTHING LESS IS A FAIL, STATED PLAINLY.  No partial credit, no post-hoc
slices, no moving the numbers.  A criterion that cannot be evaluated for lack
of data is reported as INSUFFICIENT DATA, never as a pass.
================================================================================
"""


# ═════════════════════════════════════════════════════════════════════════════
# Loading
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TickerSeries:
    ticker: str
    ts: np.ndarray          # ms, ascending
    mid: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bid_size: np.ndarray
    ask_size: np.ndarray


@dataclass
class PairRow:
    kalshi_ticker: str
    condition_id: str
    confidence: str
    asset: str | None
    kalshi_strike: float | None
    poly_strike: float | None
    strike_rel_diff: float | None
    direction: str | None
    style: str | None
    kalshi_close_ts: int | None
    poly_end_ts: int | None
    date_delta_h: float | None
    kalshi_text: str
    poly_text: str
    # filled from the market tables
    kalshi_open_ts: int | None = None
    kalshi_sources: list[str] = field(default_factory=list)
    kalshi_can_close_early: bool = False
    poly_question: str = ""
    poly_description: str = ""
    poly_slug: str = ""
    # filled by the equivalence check
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    paths: list[Path]
    kalshi: dict[str, TickerSeries]
    poly: dict[str, TickerSeries]
    shocks: list[dict]
    pairs: list[PairRow]
    markets: dict[str, dict]
    cycles: np.ndarray            # (n, 2) started_ms, ended_ms
    events: list[tuple]
    runs: list[tuple]
    shock_params: dict[str, float]


def _rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"{exc}\nThis database was not written by record_crypto_slow.py, or "
            f"was written by a different schema version.") from exc


def load(paths: Sequence[Path]) -> Dataset:
    kq: dict[str, list[tuple]] = defaultdict(list)
    pq: dict[str, list[tuple]] = defaultdict(list)
    shocks: list[dict] = []
    pairs_raw: dict[tuple[str, str], PairRow] = {}
    markets: dict[str, dict] = {}
    poly_markets: dict[str, dict] = {}
    cycles: list[tuple] = []
    events: list[tuple] = []
    runs: list[tuple] = []

    for path in paths:
        if not path.exists():
            raise SystemExit(f"no such recording: {path}")
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for t, s, m, b, a, bs, asz in _rows(db,
                "SELECT ticker, ts_ms, mid, yes_bid, yes_ask, yes_bid_size, "
                "yes_ask_size FROM kalshi_quote"):
            kq[t].append((s, m, b, a, bs, asz))
        for c, s, m, b, a, bs, asz in _rows(db,
                "SELECT condition_id, ts_ms, mid, yes_bid, yes_ask, yes_bid_size,"
                " yes_ask_size FROM poly_quote"):
            pq[c].append((s, m, b, a, bs, asz))
        for row in _rows(db, "SELECT asset, ts_ms, ref_ts_ms, ref_price, price,"
                             " move_bps, window_s, direction FROM shock"):
            shocks.append(dict(zip(
                ("asset", "ts_ms", "ref_ts_ms", "ref_price", "price", "move_bps",
                 "window_s", "direction"), row)))
        for row in _rows(db, "SELECT ticker, series, title, subtitle, rules_primary,"
                             " rules_secondary, open_ts, close_ts, asset, style,"
                             " direction, settlement_sources_json, raw_json,"
                             " frequency FROM kalshi_market"):
            markets[row[0]] = dict(zip(
                ("ticker", "series", "title", "subtitle", "rules_primary",
                 "rules_secondary", "open_ts", "close_ts", "asset", "style",
                 "direction", "sources_json", "raw_json", "frequency"), row))
        for row in _rows(db, "SELECT condition_id, question, description, slug,"
                             " end_ts FROM poly_market"):
            poly_markets[row[0]] = dict(zip(
                ("condition_id", "question", "description", "slug", "end_ts"), row))
        for row in _rows(db,
                "SELECT kalshi_ticker, condition_id, confidence, asset,"
                " kalshi_strike, poly_strike, strike_rel_diff, direction, style,"
                " kalshi_close_ts, poly_end_ts, date_delta_h, kalshi_text,"
                " poly_text FROM pair"):
            pr = PairRow(*row)
            pairs_raw[(pr.kalshi_ticker, pr.condition_id)] = pr
        cycles.extend(_rows(db, "SELECT started_ms, ended_ms FROM poll_cycle"))
        events.extend(_rows(db, "SELECT ts_ms, kind, detail FROM event_log"))
        runs.extend(_rows(db, "SELECT started_ms, ended_ms, n_cycles, note FROM run"))
        db.close()

    def to_series(store: dict[str, list[tuple]]) -> dict[str, TickerSeries]:
        out: dict[str, TickerSeries] = {}
        for key, rows in store.items():
            rows.sort(key=lambda r: r[0])
            arr = np.array(
                [[r[0], np.nan if r[1] is None else r[1],
                  np.nan if r[2] is None else r[2], np.nan if r[3] is None else r[3],
                  np.nan if r[4] is None else r[4], np.nan if r[5] is None else r[5]]
                 for r in rows], dtype=float)
            if not len(arr):
                continue
            out[key] = TickerSeries(key, arr[:, 0], arr[:, 1], arr[:, 2],
                                    arr[:, 3], arr[:, 4], arr[:, 5])
        return out

    # De-duplicate shocks across overlapping recordings (same asset, same second).
    seen: set[tuple[str, int]] = set()
    uniq_shocks = []
    for s in sorted(shocks, key=lambda s: s["ts_ms"]):
        key = (s["asset"], int(s["ts_ms"] // 1000))
        if key in seen:
            continue
        seen.add(key)
        uniq_shocks.append(s)

    pairs = list(pairs_raw.values())
    for pr in pairs:
        km = markets.get(pr.kalshi_ticker, {})
        pm = poly_markets.get(pr.condition_id, {})
        pr.kalshi_open_ts = km.get("open_ts")
        try:
            srcs = json.loads(km.get("sources_json") or "[]")
            pr.kalshi_sources = [str(s.get("name", "")) for s in srcs]
        except (TypeError, ValueError):
            pr.kalshi_sources = []
        try:
            raw = json.loads(km.get("raw_json") or "{}")
            pr.kalshi_can_close_early = bool(raw.get("can_close_early"))
        except (TypeError, ValueError):
            pr.kalshi_can_close_early = False
        pr.poly_question = pm.get("question") or ""
        pr.poly_description = pm.get("description") or ""
        pr.poly_slug = pm.get("slug") or ""

    shock_params: dict[str, float] = {}
    if uniq_shocks:
        shock_params["window_s"] = float(uniq_shocks[0]["window_s"])
        shock_params["min_bps"] = float(min(abs(s["move_bps"]) for s in uniq_shocks))

    return Dataset(
        paths=list(paths), kalshi=to_series(kq), poly=to_series(pq),
        shocks=uniq_shocks, pairs=pairs, markets=markets,
        cycles=np.array(sorted(cycles), dtype=float) if cycles else np.zeros((0, 2)),
        events=events, runs=runs, shock_params=shock_params)


# ═════════════════════════════════════════════════════════════════════════════
# Small numeric helpers
# ═════════════════════════════════════════════════════════════════════════════

def as_of(ts: np.ndarray, t_ms: float, tol_ms: float) -> int | None:
    """Index of the last observation at or before t_ms, if it is fresh enough."""
    i = int(np.searchsorted(ts, t_ms, side="right")) - 1
    if i < 0:
        return None
    if t_ms - ts[i] > tol_ms:
        return None
    return i


def median_ci(x: np.ndarray, clusters: np.ndarray | None, rng: np.random.Generator,
              n: int = BOOTSTRAP_N) -> tuple[float, float, float]:
    """
    Median with a bootstrap CI, CLUSTERED on `clusters` when given.

    Clustering matters more than usual here: 469 slow markets all respond to
    the same shock, so resampling observations would treat 469 correlated
    numbers as 469 independent ones and report a CI several times too tight.
    Resampling SHOCKS keeps the effective sample size honest.
    """
    x = np.asarray(x, dtype=float)
    if clusters is not None:
        clusters = np.asarray(clusters)
        if len(clusters) != len(x):
            raise ValueError("clusters must be parallel to x")
        keep = np.isfinite(x)
        x, clusters = x[keep], clusters[keep]
    else:
        x = x[np.isfinite(x)]
    if not len(x):
        return (np.nan, np.nan, np.nan)
    med = float(np.median(x))
    if clusters is None:
        idx = rng.integers(0, len(x), size=(n, len(x)))
        draws = np.median(x[idx], axis=1)
    else:
        groups: dict[Any, np.ndarray] = {}
        for c in np.unique(clusters):
            groups[c] = x[clusters == c]
        keys = list(groups)
        if len(keys) < 2:
            return (med, np.nan, np.nan)
        draws = np.empty(n)
        for i in range(n):
            pick = rng.integers(0, len(keys), size=len(keys))
            draws[i] = np.median(np.concatenate([groups[keys[j]] for j in pick]))
    return med, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def describe(name: str, a: np.ndarray) -> str:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if not len(a):
        return f"  {name:<34} {'0':>8}   ALL NaN/EMPTY  <-- DEGENERATE"
    uniq = len(np.unique(np.round(a, 8)))
    flags = []
    if uniq <= 3:
        flags.append("PINNED/DEGENERATE")
    if np.isclose(a.min(), a.max()):
        flags.append("CONSTANT")
    return (f"  {name:<34} {len(a):>8,} {uniq:>7,} {a.min():>11.4f} "
            f"{np.percentile(a, 25):>10.4f} {np.median(a):>10.4f} "
            f"{np.percentile(a, 75):>10.4f} {a.max():>11.4f}  {' '.join(flags)}")


def iso(ms: float | None) -> str:
    if ms is None or not np.isfinite(ms):
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def hdr(n: int, title: str) -> None:
    print("\n" + "=" * 78)
    print(f"{n}. {title}")
    print("=" * 78)


# ═════════════════════════════════════════════════════════════════════════════
# 0. Data quality -- printed before anything is concluded from the data
# ═════════════════════════════════════════════════════════════════════════════

def report_quality(ds: Dataset) -> bool:
    hdr(0, "DATA QUALITY -- read this before any result below")
    if not len(ds.cycles):
        print("  NO POLL CYCLES RECORDED. There is nothing to analyse.")
        return False

    starts, ends = ds.cycles[:, 0], ds.cycles[:, 1]
    span_h = (ends.max() - starts.min()) / 3_600_000.0
    covered_h = float(np.sum(ends - starts)) / 3_600_000.0
    gaps = (starts[1:] - ends[:-1]) / 1000.0
    gaps = gaps[gaps > 0]

    print(f"  recordings           {', '.join(p.name for p in ds.paths)}")
    print(f"  wall span            {iso(starts.min())} .. {iso(ends.max())} "
          f"({span_h:.2f}h)")
    print(f"  poll cycles          {len(ds.cycles):,}  "
          f"({covered_h:.2f}h inside a cycle)")
    if len(gaps):
        print(f"  inter-cycle gap (s)  p50={np.median(gaps):.1f} "
              f"p95={np.percentile(gaps, 95):.1f} max={gaps.max():.1f}")
        big = gaps[gaps > 60]
        if len(big):
            print(f"  !! {len(big)} gaps over 60s (max {big.max()/60:.1f} min). "
                  f"These are HOLES; nothing below interpolates across them, and "
                  f"any horizon that lands inside one is dropped, not filled.")
    print(f"  kalshi markets       {len(ds.kalshi):,} with quotes "
          f"({len(ds.markets):,} defined)")
    print(f"  polymarket markets   {len(ds.poly):,} with quotes")
    print(f"  shocks               {len(ds.shocks):,}")
    print(f"  pairs proposed       " + "  ".join(
        f"{c}={sum(1 for p in ds.pairs if p.confidence == c)}"
        for c in ("HIGH", "MEDIUM", "MANUAL")))

    kinds = defaultdict(int)
    for _, kind, _ in ds.events:
        kinds[kind] += 1
    if kinds:
        print(f"  recorder events      {dict(sorted(kinds.items()))}")
    for ts, kind, detail in ds.events:
        if kind in ("fatal", "coverage_gap", "resume_gap", "coverage_degraded"):
            print(f"    !! {iso(ts)} {kind}: {detail}")
    for started, ended, n_cycles, note in ds.runs:
        if note and note not in ("completed", ""):
            print(f"    !! run {iso(started)} ended: {note}")

    # A recording shorter than the longest horizon cannot answer H1 at all.
    if span_h * 3600 < H1_FULL_MOVE_HORIZON_S:
        print(f"\n  !! the recording spans {span_h:.2f}h but the +6h horizon needs "
              f"{H1_FULL_MOVE_HORIZON_S/3600:.0f}h.\n     The +6h column will be "
              f"empty or near-empty; that is a coverage fact, not a market fact.")
    return True


# ═════════════════════════════════════════════════════════════════════════════
# 1. Shock census
# ═════════════════════════════════════════════════════════════════════════════

def analyse_shocks(ds: Dataset) -> None:
    hdr(1, "SHOCK CENSUS")
    if not ds.shocks:
        print("  ZERO shocks recorded.  H1 cannot be evaluated; see the verdict.")
        if ds.shock_params:
            print(f"  (recorder threshold: {ds.shock_params}) ")
        return
    bps = np.array([s["move_bps"] for s in ds.shocks], dtype=float)
    ts = np.array([s["ts_ms"] for s in ds.shocks], dtype=float)
    print(f"  n = {len(ds.shocks)}   window = "
          f"{ds.shocks[0]['window_s']:.0f}s (as recorded)")
    print(f"  |move| bps           min={np.abs(bps).min():.1f} "
          f"p50={np.median(np.abs(bps)):.1f} p95={np.percentile(np.abs(bps), 95):.1f} "
          f"max={np.abs(bps).max():.1f}")
    print(f"  direction            up={int(np.sum(bps > 0))}  down={int(np.sum(bps < 0))}")
    by_asset = defaultdict(int)
    for s in ds.shocks:
        by_asset[s["asset"]] += 1
    print(f"  by asset             {dict(by_asset)}")
    if len(ts) > 1:
        spacing = np.diff(np.sort(ts)) / 60000.0
        print(f"  spacing (min)        p50={np.median(spacing):.1f} "
              f"min={spacing.min():.1f} max={spacing.max():.1f}")
        # Independence is enforced PER ASSET by the recorder's refractory, so a
        # BTC shock and an ETH shock in the same second do not violate it --
        # though they are also not two independent events, which is why the
        # cross-asset overlap is counted separately and reported.
        for asset in by_asset:
            at = np.sort([sh["ts_ms"] for sh in ds.shocks if sh["asset"] == asset])
            if len(at) > 1 and np.min(np.diff(at)) / 60000.0 < 1.0:
                print(f"  !! two {asset} shocks less than a minute apart survived "
                      f"the recorder's\n     refractory filter -- check "
                      f"--shock-refractory before treating them as independent.")
        if len(by_asset) > 1:
            near = sum(1 for s1 in ds.shocks for s2 in ds.shocks
                       if s1["asset"] < s2["asset"]
                       and abs(s1["ts_ms"] - s2["ts_ms"]) < 60_000)
            if near:
                print(f"  !! {near} cross-asset shock pairs land within 60s of each "
                      f"other.\n     BTC and ETH move together, so those are NOT "
                      f"independent events and the\n     criterion-4 count of "
                      f"{len(ds.shocks)} is optimistic by up to that many.")
    by_hour = defaultdict(int)
    for t in ts:
        by_hour[datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour] += 1
    print(f"  by UTC hour          "
          + " ".join(f"{h:02d}:{by_hour.get(h, 0)}" for h in range(24) if by_hour.get(h)))


# ═════════════════════════════════════════════════════════════════════════════
# 2 + 3. Response curves and the TRADEABLE gap
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Obs:
    """One (shock, market) observation."""
    shock_id: int
    ticker: str
    series: str
    pre_mid: float
    moves: dict[int, float]        # horizon_s -> mid(h) - pre_mid, in cents
    edge_c: float | None           # criterion-2 tradeable edge, cents
    size: float | None             # size resting at that price
    react_price: float | None      # what we would have paid
    target_mid: float | None       # the +60m mid we are trading toward


def build_observations(ds: Dataset) -> list[Obs]:
    tol = STALE_TOL_S * 1000
    obs: list[Obs] = []
    for si, shock in enumerate(ds.shocks):
        t0 = float(shock["ts_ms"])
        for ticker, s in ds.kalshi.items():
            i0 = as_of(s.ts, t0, tol)
            if i0 is None or not np.isfinite(s.mid[i0]):
                continue
            pre = float(s.mid[i0])

            moves: dict[int, float] = {}
            for h in H1_HORIZONS_S:
                ih = as_of(s.ts, t0 + h * 1000, tol)
                if ih is None or not np.isfinite(s.mid[ih]):
                    continue
                moves[h] = (float(s.mid[ih]) - pre) * 100.0

            edge_c = size = react_px = target = None
            ih = as_of(s.ts, t0 + H1_HORIZON_S * 1000, tol)
            ir = as_of(s.ts, t0 + H1_REACTION_S * 1000, tol)
            if (ih is not None and ir is not None
                    and np.isfinite(s.mid[ih])):
                target = float(s.mid[ih])
                direction = np.sign(target - pre)
                if direction > 0 and np.isfinite(s.ask[ir]):
                    # The market ends up higher: we would buy YES at the ask.
                    react_px = float(s.ask[ir])
                    edge_c = (target - react_px) * 100.0
                    size = float(s.ask_size[ir]) if np.isfinite(s.ask_size[ir]) else 0.0
                elif direction < 0 and np.isfinite(s.bid[ir]):
                    # The market ends up lower: we would buy NO at 1 - bid,
                    # whose value at the target is 1 - target.
                    react_px = 1.0 - float(s.bid[ir])
                    edge_c = ((1.0 - target) - react_px) * 100.0
                    size = float(s.bid_size[ir]) if np.isfinite(s.bid_size[ir]) else 0.0
                else:
                    # It did not move at all: there was nothing to trade.
                    react_px, edge_c, size = None, 0.0, 0.0

            obs.append(Obs(
                shock_id=si, ticker=ticker,
                series=(ds.markets.get(ticker, {}) or {}).get("series", "?"),
                pre_mid=pre, moves=moves, edge_c=edge_c, size=size,
                react_price=react_px, target_mid=target))
    return obs


def analyse_response(ds: Dataset, obs: list[Obs], rng: np.random.Generator) -> dict:
    hdr(2, "PER-SHOCK RESPONSE CURVES (Kalshi MID relative to pre-shock)")
    if not obs:
        print("  no (shock, market) observations -- nothing to profile.")
        return {}

    print("  Mid is used HERE ONLY, to answer 'does it reprice at all'.  Every")
    print("  tradeability number is in section 3 and uses the price we would pay.\n")
    print(f"  {'horizon':>8} {'n_obs':>8} {'n_shocks':>9} {'median |move| c':>16} "
          f"{'p90 |move| c':>13} {'frac of +6h move':>18}")

    full = np.array([abs(o.moves.get(H1_FULL_MOVE_HORIZON_S, np.nan)) for o in obs])
    full_med = float(np.nanmedian(full)) if np.any(np.isfinite(full)) else np.nan

    per_h: dict[int, dict] = {}
    for h in H1_HORIZONS_S:
        vals = np.array([abs(o.moves[h]) for o in obs if h in o.moves])
        cl = np.array([o.shock_id for o in obs if h in o.moves])
        if not len(vals):
            print(f"  {h//60:>6}m  {'0':>8} {'-':>9} {'no data':>16} {'-':>13} {'-':>18}")
            per_h[h] = {"n": 0}
            continue
        med, lo, hi = median_ci(vals, cl, rng)
        frac = (med / full_med) if (np.isfinite(full_med) and full_med > 0) else np.nan
        print(f"  {h//60:>6}m  {len(vals):>8,} {len(np.unique(cl)):>9} "
              f"{med:>10.2f} [{lo:.2f},{hi:.2f}] {np.percentile(vals, 90):>13.2f} "
              f"{'-' if not np.isfinite(frac) else f'{100*frac:>17.1f}%'}")
        per_h[h] = {"n": len(vals), "n_shocks": int(len(np.unique(cl))),
                    "median": med, "lo": lo, "hi": hi}

    # Per-series, at the criterion-1 horizon only (the table is already wide).
    print(f"\n  BY SERIES at +{H1_HORIZON_S//60}m "
          f"(median |mid move|, cents; series with >= 5 observations):")
    by_series: dict[str, list[float]] = defaultdict(list)
    for o in obs:
        if H1_HORIZON_S in o.moves:
            by_series[o.series].append(abs(o.moves[H1_HORIZON_S]))
    rows = [(s, np.median(v), len(v)) for s, v in by_series.items() if len(v) >= 5]
    rows.sort(key=lambda r: -r[1])
    if not rows:
        print("    (no series has 5 observations at this horizon)")
    for s, med, n in rows[:25]:
        bar = "#" * min(40, int(med * 4))
        print(f"    {s:<22} n={n:>5}  {med:>6.2f}c  {bar}")
    if len(rows) > 25:
        print(f"    ... {len(rows)-25} more series")
    return per_h


def analyse_tradeable(ds: Dataset, obs: list[Obs], rng: np.random.Generator) -> dict:
    hdr(3, f"TRADEABLE GAP WITH SIZE at shock + {H1_REACTION_S}s "
           f"(the price we would PAY, never the mid)")
    usable = [o for o in obs if o.edge_c is not None]
    if not usable:
        print("  no observation had both a reaction-time quote and a "
              f"+{H1_HORIZON_S//60}m quote.\n  H1 criteria 2 and 3 are "
              "unevaluable on this recording.")
        return {"n": 0}

    edges = np.array([o.edge_c for o in usable], dtype=float)
    sizes = np.array([0.0 if o.size is None else o.size for o in usable], dtype=float)
    cl = np.array([o.shock_id for o in usable])

    e_med, e_lo, e_hi = median_ci(edges, cl, rng)
    s_med, s_lo, s_hi = median_ci(sizes, cl, rng)
    print(f"  observations         {len(usable):,} over "
          f"{len(np.unique(cl))} shocks, {len({o.ticker for o in usable})} markets")
    print(f"  edge (cents)         median {e_med:+.2f}  95% CI "
          f"[{e_lo:+.2f}, {e_hi:+.2f}]  (clustered on shock)")
    print(f"                       p25={np.percentile(edges, 25):+.2f} "
          f"p75={np.percentile(edges, 75):+.2f} "
          f"p95={np.percentile(edges, 95):+.2f} max={edges.max():+.2f}")
    print(f"  size at that price   median {s_med:.1f}  95% CI [{s_lo:.1f}, {s_hi:.1f}]")
    print(f"  frac edge >= {H1_EDGE_CENTS:.0f}c        "
          f"{100*np.mean(edges >= H1_EDGE_CENTS):.1f}%")
    print(f"  frac size >= {H1_MIN_SIZE}       {100*np.mean(sizes >= H1_MIN_SIZE):.1f}%")
    both = (edges >= H1_EDGE_CENTS) & (sizes >= H1_MIN_SIZE)
    print(f"  frac BOTH            {100*np.mean(both):.1f}%  ({int(both.sum())} of "
          f"{len(both)} observations, over "
          f"{len(np.unique(cl[both])) if both.any() else 0} distinct shocks)")
    if both.any():
        sub = edges[both]
        m2, l2, h2 = median_ci(sub, cl[both], rng)
        print(f"  edge | both (cents)  median {m2:+.2f} 95% CI [{l2:+.2f}, {h2:+.2f}] "
              f"-- SUPPLEMENTARY, selected on the outcome; not a criterion.")

    print(f"\n  gap by horizon (same construction, different reaction time):")
    print(f"  {'react':>7} {'n':>8} {'median edge c':>15} {'median size':>13} "
          f"{'frac edge>={:.0f}c'.format(H1_EDGE_CENTS):>16}")
    for react in (0, 60, 300, 900):
        vals, szs = [], []
        tol = STALE_TOL_S * 1000
        for o in obs:
            if o.target_mid is None:
                continue
            s = ds.kalshi[o.ticker]
            t0 = float(ds.shocks[o.shock_id]["ts_ms"])
            ir = as_of(s.ts, t0 + react * 1000, tol)
            if ir is None:
                continue
            d = np.sign(o.target_mid - o.pre_mid)
            if d > 0 and np.isfinite(s.ask[ir]):
                vals.append((o.target_mid - float(s.ask[ir])) * 100.0)
                szs.append(float(s.ask_size[ir]) if np.isfinite(s.ask_size[ir]) else 0.0)
            elif d < 0 and np.isfinite(s.bid[ir]):
                vals.append(((1.0 - o.target_mid) - (1.0 - float(s.bid[ir]))) * 100.0)
                szs.append(float(s.bid_size[ir]) if np.isfinite(s.bid_size[ir]) else 0.0)
            else:
                vals.append(0.0)
                szs.append(0.0)
        if not vals:
            print(f"  {react:>6}s {'0':>8} {'no data':>15}")
            continue
        v = np.array(vals)
        print(f"  {react:>6}s {len(v):>8,} {np.median(v):>15.2f} "
              f"{np.median(szs):>13.1f} {100*np.mean(v >= H1_EDGE_CENTS):>15.1f}%")

    print(f"\n  REMINDER: the sign of the trade is taken from what the market "
          f"actually did\n  {H1_HORIZON_S//60} minutes later. This is hindsight. "
          f"Treat every number here as an\n  UPPER BOUND on a real strategy's "
          f"edge, not an estimate of one.")
    return {"n": len(usable), "edges": edges, "sizes": sizes, "clusters": cl,
            "edge_median": e_med, "edge_lo": e_lo, "edge_hi": e_hi,
            "size_median": s_med}


# ═════════════════════════════════════════════════════════════════════════════
# 4. Cross-venue
# ═════════════════════════════════════════════════════════════════════════════

def parse_poly_window_start(desc: str, end_ts: int | None) -> int | None:
    """
    The datetime a Polymarket touch market started watching, from its text.

    This is load-bearing, not cosmetic.  Two "will BTC reach $150k by Dec 31
    2026" contracts are DIFFERENT contracts if one has been watching since
    November 2025 and the other since January 2026: a touch in the overlap
    resolves one YES while the other is still live.  Returns None when the
    text does not say, and None is treated as unverifiable (blocker B7).
    """
    m = POLY_WINDOW_RX.search(desc or "")
    if m:
        date_part, time_part = m.group(1), m.group(2) or "00:00"
        mm = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", date_part)
        if mm:
            mon = _MONTHS.get(mm.group(1).lower())
            if mon:
                hh, mi = (int(x) for x in time_part.split(":"))
                try:
                    dt = datetime(int(mm.group(3)), mon, int(mm.group(2)), hh, mi,
                                  tzinfo=timezone.utc)
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    return None
    if POLY_MONTH_WINDOW_RX.search(desc or "") and end_ts:
        # "during the month specified in the title": the window is that month.
        # end_ts is the EXCLUSIVE end (an August market ends 2026-09-01T00:00),
        # so step back a second before reading the month off it.
        dt = datetime.fromtimestamp((end_ts - 1000) / 1000, tz=timezone.utc)
        start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
        return int(start.timestamp() * 1000)
    return None


def named_sources(text: str) -> set[str]:
    return {name for name, rx in SOURCE_PATTERNS if rx.search(text or "")}


def quote_currency(text: str) -> set[str]:
    return {m.upper() for m in QUOTE_CCY_RX.findall(text or "")}


def check_equivalence(pr: PairRow, strike_tol: float, date_tol_h: float,
                      review: dict[str, dict]) -> None:
    """Fill pr.blockers / pr.notes.  Pure function of the recorded text."""
    key = f"{pr.kalshi_ticker}|{pr.condition_id}"
    override = review.get(key) or {}
    verdict = str(override.get("verdict", "")).strip().lower()
    # An UNREVIEWED entry is not a rejection -- it falls through to the
    # automatic check.  Only an explicit human verdict overrides, and it
    # overrides in BOTH directions so the file cannot quietly only add pairs.
    if verdict in ("equivalent", "different", "not-equivalent"):
        pr.notes.append(f"MANUAL REVIEW: {verdict} -- {override.get('note', '')}")
        pr.blockers = [] if verdict == "equivalent" else ["MANUAL:not-equivalent"]
        return

    if pr.strike_rel_diff is None or pr.strike_rel_diff > strike_tol:
        pr.blockers.append("B1")
    if not pr.direction or not pr.style:
        pr.blockers.append("B2")
    if pr.date_delta_h is None or abs(pr.date_delta_h) > date_tol_h:
        pr.blockers.append("B3")

    poly_start = parse_poly_window_start(pr.poly_description, pr.poly_end_ts)
    k_start = pr.kalshi_open_ts
    if poly_start is None or k_start is None:
        pr.blockers.append("B7")
        pr.notes.append("could not read the Polymarket observation window")
    else:
        delta_h = abs(poly_start - k_start) / 3_600_000.0
        pr.notes.append(f"window start kalshi={iso(k_start)} poly={iso(poly_start)} "
                        f"(delta {delta_h:.0f}h)")
        if delta_h > WINDOW_TOL_H:
            pr.blockers.append("B4")

    ksrc = named_sources(" ".join(pr.kalshi_sources) + " " + pr.kalshi_text)
    psrc = named_sources(pr.poly_description)
    kccy = quote_currency(pr.kalshi_text) or {"USD"}
    pccy = quote_currency(pr.poly_description)
    pr.notes.append(f"source kalshi={sorted(ksrc) or ['?']}/{sorted(kccy)} "
                    f"poly={sorted(psrc) or ['?']}/{sorted(pccy) or ['?']}")
    if not ksrc or not psrc:
        if "B7" not in pr.blockers:
            pr.blockers.append("B7")
    elif ksrc != psrc or (pccy and kccy != pccy):
        pr.blockers.append("B5")

    early_poly = bool(re.search(r"immediately resolve|resolve early|as soon as",
                                pr.poly_description or "", re.I))
    if bool(pr.kalshi_can_close_early) != early_poly:
        pr.blockers.append("B6")


def _gap_series(ds: Dataset, prs: Sequence[PairRow]
                ) -> tuple[dict[str, dict], list[float], list[str],
                           list[tuple[float, bool]]]:
    """
    Per-pair |Kalshi mid - Polymarket mid| in cents, aligned on OUR clock.

    Alignment is Polymarket-driven: for each Polymarket book we take the most
    recent Kalshi quote no older than CROSS_VENUE_TOL_S.  Never the other way
    round and never interpolated -- a gap measured against a Kalshi quote from
    four minutes ago is a measurement of staleness, not of a price difference.
    """
    tol = CROSS_VENUE_TOL_S * 1000
    per_pair: dict[str, dict] = {}
    all_gaps: list[float] = []
    all_clusters: list[str] = []
    episodes: list[tuple[float, bool]] = []      # (duration_s, right_censored)

    for pr in prs:
        ks = ds.kalshi.get(pr.kalshi_ticker)
        ps = ds.poly.get(pr.condition_id)
        if ks is None or ps is None:
            continue
        gaps, times = [], []
        for j in range(len(ps.ts)):
            if not np.isfinite(ps.mid[j]):
                continue
            i = as_of(ks.ts, ps.ts[j], tol)
            if i is None or not np.isfinite(ks.mid[i]):
                continue
            gaps.append(abs(float(ks.mid[i]) - float(ps.mid[j])) * 100.0)
            times.append(float(ps.ts[j]))
        if not gaps:
            continue
        g = np.array(gaps)
        t = np.array(times)
        key = f"{pr.kalshi_ticker}|{pr.condition_id}"
        per_pair[key] = {"pair": pr, "gaps": g, "times": t,
                         "median": float(np.median(g))}
        all_gaps.extend(g.tolist())
        all_clusters.extend([key] * len(g))

        # Contiguous runs at or above the threshold.  A run of ONE observation
        # is recorded as 0s rather than as one poll interval: we only know the
        # gap existed at that instant, and crediting it with an interval it was
        # never observed to survive would inflate the persistence criterion.
        above = g >= H2_GAP_CENTS
        i = 0
        while i < len(above):
            if not above[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(above) and above[j + 1]:
                j += 1
            episodes.append(((t[j] - t[i]) / 1000.0, j == len(above) - 1))
            i = j + 1
    return per_pair, all_gaps, all_clusters, episodes


def analyse_cross_venue(ds: Dataset, rng: np.random.Generator, args) -> dict:
    hdr(4, "CROSS-VENUE: Kalshi vs Polymarket")
    tiers = ["HIGH"] + (["MEDIUM"] if args.include_medium else [])
    pairs = [p for p in ds.pairs if p.confidence in tiers]
    print(f"  pairs at confidence {tiers}: {len(pairs)}")
    if args.include_medium:
        print("  !! --include-medium is SUPPLEMENTARY. The pre-registered H2 "
              "verdict uses HIGH only.")
    if not pairs:
        print("  No confidently-matched pair exists in this recording.\n"
              "  That is itself the answer to 'is the same question listed on "
              "both venues':\n  under the pre-registered matching gates, no.")
        return {"n_pairs": 0, "verified": [], "drops": {}}

    review: dict[str, dict] = {}
    if args.pair_review:
        review = json.loads(Path(args.pair_review).read_text(encoding="utf-8"))
        print(f"  manual review file: {args.pair_review} ({len(review)} verdicts)")

    for pr in pairs:
        check_equivalence(pr, args.strike_tol, args.date_tol_hours, review)

    verified = [p for p in pairs if not p.blockers]
    drops: dict[str, int] = defaultdict(int)
    for p in pairs:
        for b in p.blockers:
            drops[b] += 1

    print(f"\n  EQUIVALENCE CHECK (H2 criterion 3)")
    print(f"    matched pairs examined      {len(pairs)}")
    print(f"    VERIFIED EQUIVALENT         {len(verified)}")
    print(f"    dropped                     {len(pairs) - len(verified)}")
    for code in sorted(BLOCKERS):
        if drops.get(code):
            print(f"      {code} {BLOCKERS[code]:<44} {drops[code]}")
    for code, n in sorted(drops.items()):
        if code not in BLOCKERS:
            print(f"      {code:<48} {n}")
    only_b5 = [p for p in pairs if p.blockers == ["B5"]]
    if only_b5:
        print(f"\n    SUPPLEMENTARY: {len(only_b5)} pairs were dropped ONLY for a "
              f"settlement-source\n    difference. If you are willing to carry "
              f"index basis (e.g. CF Benchmarks\n    BRTI/USD vs Binance "
              f"BTC/USDT 1-minute candles) as an unhedged risk,\n    those become "
              f"tradeable pairs -- but that is a different, riskier trade\n    than "
              f"the one H2 pre-registered, and it is NOT counted in the verdict.")

    # -- gap series -----------------------------------------------------------
    per_pair, all_gaps, all_clusters, episodes = _gap_series(ds, verified or pairs)

    if not per_pair:
        print("\n  No pair had overlapping quotes on both venues. H2 is "
              "unevaluable on this recording.")
        return _verdict_stats(ds, pairs, verified, drops, rng, args)

    ga = np.array(all_gaps)
    ca = np.array(all_clusters)
    g_med, g_lo, g_hi = median_ci(ga, ca, rng)
    scope = "VERIFIED-EQUIVALENT" if verified else "ALL MATCHED (none verified)"
    print(f"\n  GAP DISTRIBUTION on {scope} pairs")
    print(f"    pairs with overlapping quotes  {len(per_pair)}")
    print(f"    observations                   {len(ga):,}")
    print(f"    median |gap|                   {g_med:.2f}c  95% CI "
          f"[{g_lo:.2f}, {g_hi:.2f}]  (clustered on pair)")
    print(f"    p25 / p75 / p95 / max          {np.percentile(ga,25):.2f} / "
          f"{np.percentile(ga,75):.2f} / {np.percentile(ga,95):.2f} / {ga.max():.2f}c")
    print(f"    frac >= {H2_GAP_CENTS:.0f}c                     "
          f"{100*np.mean(ga >= H2_GAP_CENTS):.1f}%")

    if episodes:
        durs = np.array([d for d, _ in episodes])
        cens = int(sum(1 for _, c in episodes if c))
        print(f"\n  PERSISTENCE of a >= {H2_GAP_CENTS:.0f}c gap")
        print(f"    episodes                       {len(durs)}  "
              f"({cens} still open when the recording ended -- right-censored, "
              f"so this UNDERSTATES duration)")
        print(f"    median duration                {np.median(durs):.0f}s")
        print(f"    p75 / max                      {np.percentile(durs,75):.0f}s / "
              f"{durs.max():.0f}s")
        print(f"    frac >= {H2_PERSIST_S}s                    "
              f"{100*np.mean(durs >= H2_PERSIST_S):.1f}%")
        persist_med = float(np.median(durs))
    else:
        print(f"\n  PERSISTENCE: no episode ever reached {H2_GAP_CENTS:.0f}c.")
        persist_med = 0.0

    # -- the tradeable version, reported alongside (never a criterion) --------
    print(f"\n  SUPPLEMENTARY -- the actual two-leg cost (buy YES on the cheap "
          f"venue,\n  buy NO on the dear one). A pair is a real arb only when "
          f"this is < $1.00:")
    print(f"    {'pair':<52} {'min cost':>9} {'n':>7}")
    tol = CROSS_VENUE_TOL_S * 1000
    shown = 0
    for key, d in sorted(per_pair.items(), key=lambda kv: -kv[1]["median"]):
        pr = d["pair"]
        ks, ps = ds.kalshi[pr.kalshi_ticker], ds.poly[pr.condition_id]
        costs = []
        for j in range(len(ps.ts)):
            i = as_of(ks.ts, ps.ts[j], tol)
            if i is None:
                continue
            if np.isfinite(ks.ask[i]) and np.isfinite(ps.ask[j]):
                # YES on Kalshi + NO on Polymarket (NO ask = 1 - YES bid).
                if np.isfinite(ps.bid[j]):
                    costs.append(float(ks.ask[i]) + (1.0 - float(ps.bid[j])))
                if np.isfinite(ks.bid[i]):
                    costs.append(float(ps.ask[j]) + (1.0 - float(ks.bid[i])))
        if costs:
            print(f"    {key[:52]:<52} {min(costs):>9.4f} {len(costs):>7}")
            shown += 1
        if shown >= 15:
            break

    # -- the side-by-side table the user asked for ---------------------------
    print("\n" + "-" * 78)
    print("  EVERY MATCHED PAIR, BOTH RESOLUTION TEXTS, SIDE BY SIDE")
    print("  (read these. the numbers above are only as good as these two texts")
    print("   describing the same event.)")
    print("-" * 78)
    for pr in pairs:
        key = f"{pr.kalshi_ticker}|{pr.condition_id}"
        d = per_pair.get(key)
        status = "VERIFIED EQUIVALENT" if not pr.blockers else \
            "DROPPED: " + ", ".join(f"{b}({BLOCKERS.get(b, b)})" for b in pr.blockers)
        print(f"\n  [{pr.confidence}] {status}")
        print(f"  asset={pr.asset} strike k={pr.kalshi_strike} p={pr.poly_strike} "
              f"dir={pr.direction} style={pr.style} "
              f"date_delta={None if pr.date_delta_h is None else round(pr.date_delta_h,1)}h")
        if d is not None:
            print(f"  observed gap: median {d['median']:.2f}c over {len(d['gaps'])} obs")
        else:
            print("  observed gap: NO OVERLAPPING QUOTES")
        for note in pr.notes:
            print(f"  note: {note}")
        maxl = 10_000 if args.full_text else 10
        print(f"  KALSHI  {pr.kalshi_ticker}")
        for line in _wrap(pr.kalshi_text, 70, maxl):
            print(f"          {line}")
        print(f"  POLY    {pr.poly_slug}")
        for line in _wrap(f"{pr.poly_question} | {pr.poly_description}", 70, maxl):
            print(f"          {line}")

    return _verdict_stats(ds, pairs, verified, drops, rng, args)


def _verdict_stats(ds: Dataset, shown: Sequence[PairRow],
                   shown_verified: Sequence[PairRow], drops: dict[str, int],
                   rng: np.random.Generator, args) -> dict:
    """
    The numbers the H2 verdict is allowed to see: HIGH-confidence pairs ONLY.

    The printed tables above may be widened with --include-medium, and that is
    fine for reading.  The verdict is recomputed here from the HIGH subset so
    that a display flag can never move a pre-registered result -- the exact
    mechanism by which "just to have a look" becomes a published number.
    """
    high = [p for p in shown if p.confidence == "HIGH"]
    verified = [p for p in shown_verified if p.confidence == "HIGH"]
    high_drops = {k: v for k, v in drops.items()} if not args.include_medium else {}
    if args.include_medium:
        for p in high:
            for b in p.blockers:
                high_drops[b] = high_drops.get(b, 0) + 1
    per_pair, gaps, clusters, episodes = _gap_series(ds, verified)
    out: dict[str, Any] = {"n_pairs": len(high), "verified": verified,
                           "drops": high_drops, "n_obs": len(gaps),
                           "n_episodes": len(episodes)}
    if gaps:
        med, lo, hi = median_ci(np.array(gaps), np.array(clusters), rng)
        out.update(gap_median=med, gap_lo=lo, gap_hi=hi)
    if episodes:
        out["persist_median"] = float(np.median([d for d, _ in episodes]))
    else:
        out["persist_median"] = 0.0
    return out


def _wrap(text: str, width: int, max_lines: int = 10) -> list[str]:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                lines.append("...")
                return lines
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or ["(empty)"]


# ═════════════════════════════════════════════════════════════════════════════
# 5. Sanity
# ═════════════════════════════════════════════════════════════════════════════

def analyse_sanity(ds: Dataset, obs: list[Obs], cross: dict) -> None:
    hdr(5, "SANITY -- distribution of EVERY intermediate value")
    cat: dict[str, list[np.ndarray]] = {}

    def add(name: str, arr) -> None:
        cat.setdefault(name, []).append(np.asarray(arr, dtype=float))

    for s in ds.kalshi.values():
        add("kalshi mid", s.mid)
        add("kalshi best bid", s.bid)
        add("kalshi best ask", s.ask)
        add("kalshi spread (c)", (s.ask - s.bid) * 100)
        add("kalshi bid size", s.bid_size)
        add("kalshi ask size", s.ask_size)
    for s in ds.poly.values():
        add("poly mid", s.mid)
        add("poly best bid", s.bid)
        add("poly best ask", s.ask)
        add("poly spread (c)", (s.ask - s.bid) * 100)
        add("poly bid size", s.bid_size)
        add("poly ask size", s.ask_size)
    if ds.shocks:
        add("shock |move| bps", [abs(s["move_bps"]) for s in ds.shocks])
    if obs:
        add("pre-shock mid", [o.pre_mid for o in obs])
        for h in H1_HORIZONS_S:
            v = [o.moves[h] for o in obs if h in o.moves]
            if v:
                add(f"mid move +{h//60}m (c)", v)
        e = [o.edge_c for o in obs if o.edge_c is not None]
        if e:
            add("tradeable edge (c)", e)
        sz = [o.size for o in obs if o.size is not None]
        if sz:
            add("size at trade price", sz)
    if len(ds.cycles) > 1:
        add("poll interval (s)", (ds.cycles[1:, 0] - ds.cycles[:-1, 1]) / 1000.0)
        add("cycle duration (s)", (ds.cycles[:, 1] - ds.cycles[:, 0]) / 1000.0)

    print(f"  {'value':<34} {'n':>8} {'uniq':>7} {'min':>11} {'p25':>10} "
          f"{'median':>10} {'p75':>10} {'max':>11}")
    for name, parts in cat.items():
        print(describe(name, np.concatenate([p.ravel() for p in parts])))

    print("\n  DEGENERACY CHECKS")
    problems: list[str] = []
    n_mid = sum(int(np.sum(np.isfinite(s.mid))) for s in ds.kalshi.values())
    n_all = sum(len(s.mid) for s in ds.kalshi.values())
    if n_all:
        frac = n_mid / n_all
        print(f"    kalshi quotes with a two-sided mid   {100*frac:.1f}% "
              f"({n_mid:,}/{n_all:,})")
        if frac < 0.3:
            problems.append(
                f"only {100*frac:.0f}% of Kalshi quotes are two-sided; the mid-based "
                f"criteria are computed on a minority of the book")
    n_pmid = sum(int(np.sum(np.isfinite(s.mid))) for s in ds.poly.values())
    n_pall = sum(len(s.mid) for s in ds.poly.values())
    if n_pall:
        print(f"    poly quotes with a two-sided mid     "
              f"{100*n_pmid/n_pall:.1f}% ({n_pmid:,}/{n_pall:,})")
    frozen = sum(1 for s in ds.kalshi.values()
                 if len(np.unique(s.mid[np.isfinite(s.mid)])) <= 1)
    print(f"    kalshi markets whose mid NEVER moved  {frozen}/{len(ds.kalshi)}")
    if ds.kalshi and frozen / max(1, len(ds.kalshi)) > 0.9:
        problems.append(
            f"{frozen} of {len(ds.kalshi)} markets never moved at all -- either "
            f"the recording is too short to contain a repricing, or the poll is "
            f"reading a cached field")
    if len(ds.cycles) > 1:
        iv = (ds.cycles[1:, 0] - ds.cycles[:-1, 1]) / 1000.0
        if np.median(iv) <= 0:
            problems.append("poll intervals are non-positive: the cycle clock is wrong")

    if problems:
        print("\n  !! PIPELINE PROBLEMS -- fix these before reading the verdict:")
        for p in problems:
            print(f"     - {p}")
    else:
        print("    no degenerate or pinned intermediate detected.")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Limitations
# ═════════════════════════════════════════════════════════════════════════════

def analyse_limitations(ds: Dataset, trade: dict) -> None:
    hdr(6, "LIMITATIONS -- what this study CANNOT establish")

    print("""  1. THIS MEASURES REPRICING, NOT PROFITABILITY.
     Every slow crypto series here settles on a monthly-to-annual cadence.
     A 24-hour recording contains approximately zero settled outcomes, so
     nothing above is validated against a payout. A PASS on H1 says "the
     price moved late", not "we would have made money".""")

    # How many settled outcomes would be needed, from the observed dispersion.
    edges = trade.get("edges")
    if edges is not None and len(edges):
        edge = max(0.5, abs(float(np.median(edges))))
        # A binary contract's per-contract PnL has sd ~ 100*sqrt(p(1-p)) cents;
        # at the worst case p=0.5 that is 50c. Use the observed entry prices'
        # implied worst case rather than a flattering one.
        sd = 50.0
        n_needed = int(math.ceil(((1.96 + 0.84) ** 2) * (sd ** 2) / (edge ** 2)))
        print(f"""
  2. HOW MANY SETTLED OUTCOMES WOULD BE NEEDED.
     To distinguish the observed {edge:.1f}c edge from zero at 5% alpha and 80%
     power, with a binary payout's per-contract sd of ~{sd:.0f}c:
         n = (1.96 + 0.84)^2 * {sd:.0f}^2 / {edge:.1f}^2  =  {n_needed:,} settled trades.
     These series settle roughly annually and there are {len(ds.kalshi)} of them
     live right now, so one settlement round yields at most ~{len(ds.kalshi)} outcomes.
     Reaching {n_needed:,} would take on the order of {max(1, n_needed // max(1, len(ds.kalshi)))} years of settlements.
     That is the honest answer: PROFITABILITY IS NOT MEASURABLE ON THESE
     SERIES in any reasonable horizon. If a repricing lag is real, it has to
     be validated on faster-settling contracts or on paper fills, not here.""")
    else:
        print("""
  2. HOW MANY SETTLED OUTCOMES WOULD BE NEEDED.
     Not computable: no tradeable-edge observation survived. As a reference
     point, detecting a 3c edge against a binary payout's ~50c per-contract
     sd at 5%/80% needs ~2,200 settled trades.""")

    print("""
  3. SHOCKS ARE DEFINED FROM SPOT, NOT FROM NEWS.
     A shock here is a spot move. A pure-narrative event that moves an
     event contract (an ETF headline, a regulatory filing) without moving
     BTC/ETH spot is INVISIBLE to this study, and those are exactly the
     events the event-market series (KXBTCRESERVE, KXTREASBUYBTC) trade on.
     H1 as measured therefore covers price-driven repricing only.

  4. THE HINDSIGHT SIGN.
     H1 criterion 2 buys the side that turned out to be right. No strategy
     has that. The measured edge is a ceiling, and a real one would have to
     survive being wrong about direction roughly half the time.

  5. ONE RECORDING WINDOW.
     Everything above is conditioned on the volatility regime, the news
     flow and the market inventory of the recorded window. Repeat on
     separate windows before believing any of it; the lag study needed
     three sessions before its result was stable.

  6. POLYMARKET LIQUIDITY IS NOT MODELLED BEYOND TOP OF BOOK.
     Sizes are recorded at best bid/ask only. A gap that exists on 5 shares
     is not a gap. Section 4's size columns are the check to apply.

  7. CROSS-VENUE EXECUTION IS NOT MODELLED AT ALL.
     H2 measures a price difference. Capturing it needs USDC on Polygon, a
     funded Kalshi account, two fills, and capital locked on both legs until
     resolution -- which for these series is months. Carry cost alone can
     exceed the gap.""")


# ═════════════════════════════════════════════════════════════════════════════
# Verdict
# ═════════════════════════════════════════════════════════════════════════════

def verdict(ds: Dataset, obs: list[Obs], resp: dict, trade: dict, cross: dict,
            rng: np.random.Generator) -> int:
    hdr(7, "PRE-REGISTERED VERDICT")

    # ---- H1 -----------------------------------------------------------------
    print("H1  NEWS-RESPONSE LAG")
    n_shocks = len(ds.shocks)
    c4 = n_shocks >= H1_MIN_SHOCKS
    print(f"  4. >= {H1_MIN_SHOCKS} independent shocks           "
          f"observed {n_shocks:<8} {'PASS' if c4 else 'FAIL'}")

    h = H1_HORIZON_S
    moves = np.array([abs(o.moves[h]) for o in obs if h in o.moves])
    cl = np.array([o.shock_id for o in obs if h in o.moves])
    if len(moves):
        m1, l1, h1_ = median_ci(moves, cl, rng)
        c1 = m1 >= H1_REPRICE_CENTS
        print(f"  1. median |mid move| in {h//60}m >= {H1_REPRICE_CENTS:.0f}c   "
              f"{m1:>6.2f}c [{l1:.2f},{h1_:.2f}]  n={len(moves):,}  "
              f"{'PASS' if c1 else 'FAIL'}")
    else:
        c1 = False
        print(f"  1. median |mid move| in {h//60}m >= {H1_REPRICE_CENTS:.0f}c   "
              f"{'no data':>12}  INSUFFICIENT DATA")

    if trade.get("n"):
        c2 = trade["edge_median"] >= H1_EDGE_CENTS
        c3 = trade["size_median"] >= H1_MIN_SIZE
        print(f"  2. median edge at +{H1_REACTION_S}s >= {H1_EDGE_CENTS:.0f}c        "
              f"{trade['edge_median']:>6.2f}c [{trade['edge_lo']:.2f},"
              f"{trade['edge_hi']:.2f}]  n={trade['n']:,}  {'PASS' if c2 else 'FAIL'}")
        print(f"  3. median size >= {H1_MIN_SIZE} contracts       "
              f"{trade['size_median']:>6.1f}                     "
              f"{'PASS' if c3 else 'FAIL'}")
    else:
        c2 = c3 = False
        print(f"  2. median edge at +{H1_REACTION_S}s >= {H1_EDGE_CENTS:.0f}c        "
              f"{'no data':>12}  INSUFFICIENT DATA")
        print(f"  3. median size >= {H1_MIN_SIZE} contracts       "
              f"{'no data':>12}  INSUFFICIENT DATA")

    if not c4:
        h1_verdict = "INSUFFICIENT DATA"
        h1_why = (f"only {n_shocks} shocks were observed; the pre-registered floor "
                  f"is {H1_MIN_SHOCKS}. No other criterion can substitute for sample size.")
    elif c1 and c2 and c3:
        h1_verdict = "PASS"
        h1_why = ("all four criteria met. Read section 6 before acting: the sign "
                  "of the trade was taken with hindsight and nothing here is "
                  "validated against a settled outcome.")
    else:
        failed = [n for n, ok in (("1", c1), ("2", c2), ("3", c3)) if not ok]
        h1_verdict = "FAIL"
        h1_why = f"criteri{'on' if len(failed)==1 else 'a'} {', '.join(failed)} not met."
    print(f"\n  ==> H1: {h1_verdict}")
    print(f"      {h1_why}")

    # ---- H2 -----------------------------------------------------------------
    print("\nH2  CROSS-VENUE SPREAD")
    verified = cross.get("verified") or []
    n_pairs = cross.get("n_pairs", 0)
    d1 = cross.get("gap_median")
    d2 = cross.get("persist_median")

    c3_label = "n/a" if n_pairs == 0 else ("PASS" if verified else "FAIL")
    print(f"  3. resolution criteria verified   {len(verified)} of {n_pairs} pairs "
          f"survived  {c3_label}")
    if cross.get("drops"):
        print(f"     drop reasons: " + ", ".join(
            f"{k}={v}" for k, v in sorted(cross["drops"].items())))
    if d1 is not None and cross.get("n_obs"):
        e1 = d1 >= H2_GAP_CENTS
        print(f"  1. median |gap| >= {H2_GAP_CENTS:.0f}c            "
              f"{d1:>6.2f}c [{cross['gap_lo']:.2f},{cross['gap_hi']:.2f}]  "
              f"n={cross['n_obs']:,}  {'PASS' if e1 else 'FAIL'}")
        e2 = (d2 or 0) >= H2_PERSIST_S
        print(f"  2. gap persists >= {H2_PERSIST_S}s           "
              f"{d2:>6.0f}s over {cross.get('n_episodes', 0)} episodes  "
              f"{'PASS' if e2 else 'FAIL'}")
    else:
        e1 = e2 = False
        print(f"  1. median |gap| >= {H2_GAP_CENTS:.0f}c            "
              f"{'no data':>12}  INSUFFICIENT DATA")
        print(f"  2. gap persists >= {H2_PERSIST_S}s           "
              f"{'no data':>12}  INSUFFICIENT DATA")

    if n_pairs == 0:
        h2_verdict = "INSUFFICIENT DATA"
        h2_why = ("no pair passed the recorder's matching gates, so there is no "
                  "same-question pair to price. Note this is a real finding about "
                  "the two venues' listings, not a missing-data accident.")
    elif not verified:
        h2_verdict = "FAIL"
        h2_why = (f"none of the {n_pairs} matched pairs survived the resolution-"
                  f"equivalence check, so criterion 3 fails outright. Whatever "
                  f"price gap exists between them is not an arbitrage; it is the "
                  f"price of two different contracts.")
    elif not cross.get("n_obs"):
        h2_verdict = "INSUFFICIENT DATA"
        h2_why = "verified pairs exist but never had simultaneous quotes on both venues."
    elif e1 and e2:
        h2_verdict = "PASS"
        h2_why = ("all three criteria met on verified-equivalent pairs. Section 6 "
                  "item 7 still applies: capturing this needs capital locked on "
                  "both legs until resolution.")
    else:
        failed = [n for n, ok in (("1", e1), ("2", e2)) if not ok]
        h2_verdict = "FAIL"
        h2_why = f"criteri{'on' if len(failed)==1 else 'a'} {', '.join(failed)} not met."
    print(f"\n  ==> H2: {h2_verdict}")
    print(f"      {h2_why}")

    print("\n" + "=" * 78)
    print(f"  H1 NEWS-RESPONSE LAG : {h1_verdict}")
    print(f"  H2 CROSS-VENUE SPREAD: {h2_verdict}")
    print("=" * 78)
    return 0


# ═════════════════════════════════════════════════════════════════════════════
# Pair-review template
# ═════════════════════════════════════════════════════════════════════════════

def emit_pair_review(ds: Dataset, out: Path) -> None:
    doc: dict[str, dict] = {}
    for pr in ds.pairs:
        doc[f"{pr.kalshi_ticker}|{pr.condition_id}"] = {
            "verdict": "UNREVIEWED",
            "note": "set verdict to 'equivalent' or 'different' after reading both texts",
            "kalshi_text": pr.kalshi_text,
            "poly_text": pr.poly_text,
        }
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"wrote {len(doc)} pairs to {out}")
    print("Fill in each 'verdict' and re-run with --pair-review to override the")
    print("automatic equivalence check. A manual verdict beats the automatic one")
    print("in BOTH directions, and is recorded in the output for auditability.")


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Analyse a record_crypto_slow.py recording. Read-only.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--db", action="append", required=True,
                    help="recording sqlite (repeatable; pooled)")
    ap.add_argument("--full-text", action="store_true",
                    help="print resolution texts untruncated in the pair table")
    ap.add_argument("--include-medium", action="store_true",
                    help="also show MEDIUM-confidence pairs (SUPPLEMENTARY; the "
                         "pre-registered H2 verdict always uses HIGH only)")
    ap.add_argument("--pair-review", default=None,
                    help="JSON of manual equivalence verdicts (see --emit-pair-review)")
    ap.add_argument("--emit-pair-review", default=None,
                    help="write a review template for every proposed pair and exit")
    ap.add_argument("--strike-tol", type=float, default=0.01,
                    help="relative strike tolerance used by the equivalence check")
    ap.add_argument("--date-tol-hours", type=float, default=36.0,
                    help="resolution-date tolerance used by the equivalence check")
    args = ap.parse_args()

    paths = [Path(p) for p in args.db]
    ds = load(paths)

    if args.emit_pair_review:
        emit_pair_review(ds, Path(args.emit_pair_review))
        return 0

    args.shock_bps_note = (
        f"{ds.shock_params['min_bps']:.0f}bps over {ds.shock_params['window_s']:.0f}s"
        if ds.shock_params else "the recorder's configured threshold")
    print(PREREG_TEXT(args))

    rng = np.random.default_rng(RNG_SEED)
    if not report_quality(ds):
        return 1

    analyse_shocks(ds)
    obs = build_observations(ds)
    resp = analyse_response(ds, obs, rng)
    trade = analyse_tradeable(ds, obs, rng)
    cross = analyse_cross_venue(ds, rng, args)
    analyse_sanity(ds, obs, cross)
    analyse_limitations(ds, trade)
    return verdict(ds, obs, resp, trade, cross, rng)


if __name__ == "__main__":
    raise SystemExit(main())
