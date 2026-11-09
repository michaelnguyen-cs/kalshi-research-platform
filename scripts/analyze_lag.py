#!/usr/bin/env python
"""
READ-ONLY analysis of recordings made by scripts/record_feeds.py: does Kalshi
lag the underlying, and is any lag TRADEABLE at our latency?

This script never touches the exchange except for two optional, explicitly
flagged READ calls (--measure-latency probes GET /markets/{ticker};
--fetch-settlement reads `result` for windows the recorder could not settle).
It cannot place an order; kbot.execution is not imported.

    python scripts/analyze_lag.py --recordings data/a.sqlite data/b.sqlite

The verdict is pre-registered and printed BEFORE any result.  Read the
"PRE-REGISTRATION" block that this script prints first -- the thresholds, the
signal definition, and the minimum sample size were all fixed in this source
file before any recording was analysed, and the git history of this file is
the audit trail for that claim.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from scipy.stats import norm

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kbot.config import settings  # noqa: E402
from kbot.edge import fee_per_contract  # noqa: E402

# ═════════════════════════════════════════════════════════════════════════════
# PRE-REGISTRATION.  Everything in this block was chosen from first principles
# (cost arithmetic and measured latency), NOT by looking at what performed
# best.  Changing a value here after seeing results invalidates the study; if
# you must, say so out loud in the report and treat the result as exploratory.
# ═════════════════════════════════════════════════════════════════════════════

# --- The ONE primary signal ---------------------------------------------------
# "The composite-implied probability differs from the price we would actually
#  PAY by at least X cents, that gap has been continuously present for at least
#  Y ms, and at least Z contracts are resting at that price."
SIGNAL_X_CENTS = 3.0      # minimum gap vs the tradeable price (never mid)
SIGNAL_Y_MS = 300         # minimum sustain before we would act
SIGNAL_Z_SIZE = 20        # minimum contracts resting at that price

# Why X = 3c: the taker fee at p=0.50 is ceil(0.07*0.25*100) = 2c per contract.
#   A typical 15-minute book quotes 1-2c wide, so crossing costs ~1c more.
#   2c fee + 1c spread + 1c margin = 4c of cost at the money; 3c is already
#   BELOW breakeven at p=0.5 and only clears at wide-of-the-money prices where
#   the fee is smaller.  It is deliberately the *loosest* threshold that could
#   conceivably work -- picking a larger X would flatter the win rate while
#   shrinking n.  Criterion 2 re-tests cost coverage per trade at the actual
#   price anyway, so this threshold cannot smuggle in a losing trade.
# Why Y = 300ms: our order cannot arrive faster than the measured p95 read
#   round trip (a lower bound on order latency, typically 100-300ms from a
#   cloud VM).  A gap that does not persist at least that long is not a gap we
#   could ever have traded, and a single-tick gap is usually one stale feed.
# Why Z = 20: criterion 3 of the pass/fail requires >= 20 contracts resting.
#   Same number in both places on purpose -- a signal that fires on size we
#   are not allowed to count is a signal that fires on nothing.

# --- Eligibility (guards, not tuning) ----------------------------------------
TTE_MIN_S = 60.0          # inside the last minute the lognormal is unusable
TTE_MAX_S = 780.0         # 13 min: the window has only just opened before this
FRESHNESS_MS = 500        # every constituent must be fresher than this
MIN_SOURCES = 3           # composite needs >= 3 fresh constituents
VOL_WINDOW_S = 300.0      # EWMA vol window, matched to the 15-minute horizon
VOL_LAMBDA = 0.94         # RiskMetrics, same as kbot.vol.ewma_vol
GRID_MS = 100             # analysis grid resolution

# --- Replay mechanics (pessimistic by construction) --------------------------
REPLAY_ORDER_SIZE = 20    # contracts we would send
REPLAY_COOLDOWN_S = 5.0   # per ticker, after any fired signal
MAX_CONCURRENT_PER_TICKER = 1

# --- Cross-correlation -------------------------------------------------------
CCF_MAX_LAG_MS = 5000
N_SURROGATES = 200        # null-control replicates
SURROGATE_BLOCK_MS = 2000 # block size for the block-shuffle null

# --- Response profile --------------------------------------------------------
MOVE_BPS = 5.0            # N: a "move" is >= 5 bps of the underlying in 1s
MOVE_SIGMA = 3.0          # K: ...or >= 3 sigma of 1s returns, whichever is met
MOVE_WINDOW_S = 1.0
RESPONSE_OFFSETS_MS = (250, 500, 1000, 2000, 3000, 5000, 10000)
FULL_REPRICE_HORIZON_MS = 30000

# --- Maker variant -----------------------------------------------------------
MAKER_ADVERSE_HORIZON_S = 10.0
MAKER_ADVERSE_CENTS = 1.0

# --- Verdict -----------------------------------------------------------------
MIN_SIGNAL_EVENTS = 200   # absolute floor for reporting any replay verdict
CCF_MIN_LAG_MS = 500      # criterion 1
CCF_NULL_Z = 1.0          # peak must exceed the null band's upper edge
GAP_MARGIN_CENTS = 1.0    # criterion 2 margin over fee + full spread
BOOTSTRAP_N = 10000

PREREG_TEXT = f"""
================================================================================
PRE-REGISTRATION -- thresholds and signal fixed BEFORE any result was seen
================================================================================
PRIMARY SIGNAL (one, chosen up front, not swept):
  Fire when the COMPOSITE-implied probability differs from the price we would
  actually PAY (best ask to buy YES / best ask to buy NO) by >= {SIGNAL_X_CENTS:.0f} cents,
  that gap has been continuously present for >= {SIGNAL_Y_MS} ms, and >= {SIGNAL_Z_SIZE} contracts
  rest at that price.  Eligible only when {TTE_MIN_S:.0f}s <= time-to-expiry <= {TTE_MAX_S:.0f}s,
  all constituents fresher than {FRESHNESS_MS} ms, and >= {MIN_SOURCES} fresh constituents.

PASS requires ALL FOUR:
  1. Peak cross-correlation lag of Kalshi mid behind the COMPOSITE-implied
     probability is >= {CCF_MIN_LAG_MS} ms, with a clear peak above the null-control band.
  2. Median tradeable gap during response windows exceeds
     (taker fee at that price) + (full spread crossed) + {GAP_MARGIN_CENTS:.0f}c margin.
  3. >= {SIGNAL_Z_SIZE} contracts rest at that price at the moment we would act.
  4. Ex-ante replay is profitable after fees AND beats both controls
     (random-entry, always-buy-cheaper-side), with the bootstrapped 95% CI
     excluding zero.
Anything less is a FAIL.  There is no partial credit and no post-hoc slice.

MINIMUM SAMPLE: {MIN_SIGNAL_EVENTS} filled signal events, pooled.  Held to settlement, per-trade
  P&L is near-Bernoulli with sd ~ 0.35-0.50 dollars/contract.  Detecting a 2c
  per-contract edge at 95% confidence therefore needs n ~ (1.96*0.45/0.02)^2
  ~ 1950 trades.  {MIN_SIGNAL_EVENTS} is the floor for printing a verdict AT ALL, not the
  number at which the CI becomes informative -- expect a wide CI below ~2000
  and read a "CI includes zero" there as "not measured", which is a FAIL.

OUT-OF-SAMPLE: with >= 2 recordings, the FIRST (earliest) session is in-sample
  and every later session is out-of-sample.  Both are reported side by side.
  The verdict is computed on POOLED data AND must also hold out-of-sample.
  Nothing is fitted on the in-sample data (the parameters above are fixed), so
  the split is a stability check, not a fit/validate split.

INDEX-BASIS CAVEAT: these contracts settle on CF Benchmarks BRTI, a book-based
  multi-exchange index.  Unless the recording carries a real BRTI feed, the
  composite here is a PROXY (median of constituent trade prices).  Kalshi
  lagging Coinbase while tracking the composite is index basis, NOT edge.
================================================================================
"""

DOLLAR = 1.0
CENT = 0.01


# ═════════════════════════════════════════════════════════════════════════════
# Loading
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class BookSeries:
    ts: np.ndarray
    bid: np.ndarray
    bid_size: np.ndarray
    ask: np.ndarray
    ask_size: np.ndarray
    mid: np.ndarray
    yes_levels: list
    no_levels: list


@dataclass
class Session:
    path: Path
    label: str
    meta: dict
    started_ms: int
    ticks: dict[str, tuple[np.ndarray, np.ndarray]]
    composite_ts: np.ndarray
    composite_px: np.ndarray
    composite_n: np.ndarray
    books: dict[str, BookSeries]
    markets: dict[str, dict]
    excluded: list[tuple[str, int, int, str]]
    latency: dict[str, np.ndarray]
    ntp: list[tuple]
    feed_events: list[tuple]
    kalshi_stats: dict = field(default_factory=dict)
    composite_source: str = "proxy"


def load_session(path: Path, label: str) -> Session:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    meta = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT * FROM meta")}

    ticks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for (feed,) in con.execute("SELECT DISTINCT feed FROM underlying_tick"):
        rows = con.execute(
            "SELECT local_ts_ms, price FROM underlying_tick WHERE feed=? ORDER BY local_ts_ms",
            (feed,),
        ).fetchall()
        arr = np.array([(r[0], r[1]) for r in rows], dtype=float)
        if len(arr):
            ticks[feed] = (arr[:, 0], arr[:, 1])

    # A real BRTI feed, if the recorder captured one, beats the proxy.
    # The proxy's own basis changed on 2026-08-11 (trade prints -> quote mids),
    # so take the label from the recording rather than hardcoding it: an old
    # sqlite and a new one must not both claim to be the same thing.
    composite_source = str(meta.get("brti_source") or "proxy_median_of_trades")
    if "brti_real" in ticks and len(ticks["brti_real"]) > 100:
        cts, cpx = ticks["brti_real"]
        cn = np.full(len(cts), MIN_SOURCES, dtype=float)
        composite_source = "brti_real"
    else:
        rows = con.execute(
            "SELECT local_ts_ms, median_price, n_sources FROM composite ORDER BY local_ts_ms"
        ).fetchall()
        arr = np.array([(r[0], r[1] if r[1] is not None else np.nan, r[2]) for r in rows], dtype=float)
        cts = arr[:, 0] if len(arr) else np.zeros(0)
        cpx = arr[:, 1] if len(arr) else np.zeros(0)
        cn = arr[:, 2] if len(arr) else np.zeros(0)

    books: dict[str, BookSeries] = {}
    for (ticker,) in con.execute("SELECT DISTINCT ticker FROM kalshi_book"):
        rows = con.execute(
            "SELECT local_ts_ms,best_bid,bid_size,best_ask,ask_size,mid,yes_levels,no_levels "
            "FROM kalshi_book WHERE ticker=? ORDER BY local_ts_ms",
            (ticker,),
        ).fetchall()
        if not rows:
            continue
        n = len(rows)
        f = lambda v: np.nan if v is None else float(v)  # noqa: E731
        books[ticker] = BookSeries(
            ts=np.array([r[0] for r in rows], dtype=float),
            bid=np.array([f(r[1]) for r in rows]),
            bid_size=np.array([f(r[2]) for r in rows]),
            ask=np.array([f(r[3]) for r in rows]),
            ask_size=np.array([f(r[4]) for r in rows]),
            mid=np.array([f(r[5]) for r in rows]),
            yes_levels=[r[6] for r in rows],
            no_levels=[r[7] for r in rows],
        )

    markets = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM kalshi_market")}
    excluded = [(r[0], r[1], r[2], r[3]) for r in con.execute("SELECT * FROM excluded_span")]
    feed_events = [tuple(r) for r in con.execute("SELECT * FROM feed_event")]
    ntp = [tuple(r) for r in con.execute("SELECT * FROM ntp_offset")]

    latency: dict[str, np.ndarray] = {}
    for (kind,) in con.execute("SELECT DISTINCT kind FROM latency_sample"):
        vals = [r[0] for r in con.execute(
            "SELECT rtt_ms FROM latency_sample WHERE kind=?", (kind,))]
        latency[kind] = np.array(vals, dtype=float)

    stats = {}
    for key, sql in (
        ("rows", "SELECT COUNT(*) FROM kalshi_book"),
        ("deltas", "SELECT COUNT(*) FROM kalshi_book WHERE msg_type='orderbook_delta'"),
        ("rejected", "SELECT COUNT(*) FROM kalshi_book WHERE applied=0"),
        ("nochange", "SELECT COUNT(*) FROM kalshi_book WHERE changed=0"),
    ):
        stats[key] = con.execute(sql).fetchone()[0]

    con.close()
    return Session(
        path=path, label=label, meta=meta,
        started_ms=int(meta.get("started_ms", 0)),
        ticks=ticks, composite_ts=cts, composite_px=cpx, composite_n=cn,
        books=books, markets=markets, excluded=excluded, latency=latency,
        ntp=ntp, feed_events=feed_events, kalshi_stats=stats,
        composite_source=composite_source,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Grid construction
# ═════════════════════════════════════════════════════════════════════════════

def ffill(src_ts: np.ndarray, src_val: np.ndarray, grid: np.ndarray,
          max_age_ms: float | None = None) -> np.ndarray:
    """
    Last value at or before each grid point (NaN before the first sample, and
    NaN if the last sample is older than max_age_ms).

    Forward-fill with an explicit staleness ceiling, never interpolation:
    interpolating between two ticks invents a price we never saw, which is the
    kind of convenience that turns into a fake signal.
    """
    if len(src_ts) == 0:
        return np.full(len(grid), np.nan)
    idx = np.searchsorted(src_ts, grid, side="right") - 1
    out = np.where(idx >= 0, src_val[np.clip(idx, 0, None)], np.nan)
    if max_age_ms is not None:
        age = np.where(idx >= 0, grid - src_ts[np.clip(idx, 0, None)], np.inf)
        out = np.where(age <= max_age_ms, out, np.nan)
    return out


def ewma_sigma_series(ts: np.ndarray, px: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """
    Annualised EWMA vol of the underlying, evaluated on the grid using ONLY
    data at or before each grid point (this is used inside an ex-ante replay,
    so a two-sided or full-sample estimator would be lookahead).

    Computed on 1-second bars rather than raw ticks: at tick spacing the
    estimator measures bid-ask bounce, not volatility.
    """
    if len(ts) < 10:
        return np.full(len(grid), np.nan)
    t0, t1 = ts[0], ts[-1]
    bar_ts = np.arange(t0, t1 + 1000, 1000.0)
    bar_px = ffill(ts, px, bar_ts, max_age_ms=5000)
    ok = np.isfinite(bar_px)
    bar_ts, bar_px = bar_ts[ok], bar_px[ok]
    if len(bar_px) < 10:
        return np.full(len(grid), np.nan)

    rets = np.diff(np.log(bar_px))
    var = np.empty(len(rets))
    # Seeded from the first 30 bars, then updated one bar at a time -- the value
    # at index i uses returns up to i and nothing after it.  lam=0.94 on 1-second
    # bars has an effective memory far inside VOL_WINDOW_S, so the running value
    # IS the trailing window; no separate truncation is needed.
    running = float(np.var(rets[: min(30, len(rets))])) or 1e-10
    for i, r in enumerate(rets):
        running = VOL_LAMBDA * running + (1 - VOL_LAMBDA) * r * r
        var[i] = running
    sigma_per_bar = np.sqrt(np.maximum(var, 1e-16))
    ann = sigma_per_bar * math.sqrt(365.25 * 24 * 3600)  # 1-second bars
    return ffill(bar_ts[1:], ann, grid, max_age_ms=30_000)


def implied_prob(spot: np.ndarray, strike: float, sigma: np.ndarray,
                 tte_s: np.ndarray) -> np.ndarray:
    """
    P(S_T >= strike) under the same driftless lognormal the live strategy uses
    (kbot.strategy.btc_hourly._lognormal_prob).  Deliberately the same model:
    if the model is wrong, the trading system is wrong in the same way, and
    this study should measure the system we would actually run.
    """
    t_years = tte_s / (365.25 * 24 * 3600)
    with np.errstate(divide="ignore", invalid="ignore"):
        d = np.log(spot / strike) / (sigma * np.sqrt(t_years))
    p = norm.cdf(d)
    bad = ~np.isfinite(d) | (sigma <= 0) | (t_years <= 0) | (spot <= 0)
    return np.where(bad, np.nan, p)


@dataclass
class Window:
    """One 15-minute contract, resampled onto the common local-time grid."""
    session: str
    ticker: str
    strike: float
    close_ms: int
    result: str | None
    grid: np.ndarray
    tte: np.ndarray
    bid: np.ndarray
    bid_size: np.ndarray
    ask: np.ndarray
    ask_size: np.ndarray
    mid: np.ndarray
    comp_px: np.ndarray
    comp_n: np.ndarray
    cb_px: np.ndarray
    sigma: np.ndarray
    p_comp: np.ndarray
    p_cb: np.ndarray
    valid: np.ndarray
    yes_levels: list
    no_levels: list
    excluded_frac: float


def build_windows(sess: Session) -> list[Window]:
    out: list[Window] = []
    for ticker, bs in sess.books.items():
        mkt = sess.markets.get(ticker, {})
        strike = mkt.get("floor_strike")
        close_ms = mkt.get("close_ts_ms")
        if strike is None or close_ms is None:
            print(f"  [skip] {ticker}: missing "
                  f"{'floor_strike' if strike is None else 'close_time'} in the recording")
            continue
        strike = float(strike)
        close_ms = int(close_ms)

        start = max(bs.ts[0], sess.composite_ts[0] if len(sess.composite_ts) else bs.ts[0])
        end = min(bs.ts[-1], float(close_ms))
        if end - start < 60_000:
            print(f"  [skip] {ticker}: only {(end-start)/1000:.0f}s of usable overlap")
            continue
        grid = np.arange(start, end, GRID_MS, dtype=float)

        bid = ffill(bs.ts, bs.bid, grid)
        ask = ffill(bs.ts, bs.ask, grid)
        bid_size = ffill(bs.ts, bs.bid_size, grid)
        ask_size = ffill(bs.ts, bs.ask_size, grid)
        mid = ffill(bs.ts, bs.mid, grid)
        lvl_idx = np.clip(np.searchsorted(bs.ts, grid, side="right") - 1, 0, None)

        comp_px = ffill(sess.composite_ts, sess.composite_px, grid, max_age_ms=FRESHNESS_MS)
        comp_n = ffill(sess.composite_ts, sess.composite_n, grid, max_age_ms=FRESHNESS_MS)
        cb_ts, cb_px_raw = sess.ticks.get("coinbase", (np.zeros(0), np.zeros(0)))
        cb_px = ffill(cb_ts, cb_px_raw, grid, max_age_ms=FRESHNESS_MS)

        sigma = ewma_sigma_series(sess.composite_ts, sess.composite_px, grid)
        tte = (close_ms - grid) / 1000.0
        p_comp = implied_prob(comp_px, strike, sigma, tte)
        p_cb = implied_prob(cb_px, strike, sigma, tte)

        valid = (
            np.isfinite(bid) & np.isfinite(ask) & np.isfinite(comp_px)
            & np.isfinite(sigma) & (comp_n >= MIN_SOURCES) & (tte > 0)
        )
        for feed, s, e, _reason in sess.excluded:
            valid &= ~((grid >= s) & (grid <= e))
        excluded_frac = 1.0 - float(valid.mean()) if len(valid) else 1.0

        out.append(Window(
            session=sess.label, ticker=ticker, strike=strike, close_ms=close_ms,
            result=(mkt.get("result") or None),
            grid=grid, tte=tte, bid=bid, bid_size=bid_size, ask=ask,
            ask_size=ask_size, mid=mid, comp_px=comp_px, comp_n=comp_n,
            cb_px=cb_px, sigma=sigma, p_comp=p_comp, p_cb=p_cb, valid=valid,
            yes_levels=[bs.yes_levels[i] for i in lvl_idx],
            no_levels=[bs.no_levels[i] for i in lvl_idx],
            excluded_frac=excluded_frac,
        ))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# 0. Data quality
# ═════════════════════════════════════════════════════════════════════════════

def report_quality(sessions: list[Session]) -> bool:
    print("\n" + "=" * 78)
    print("0. DATA QUALITY -- read this before any number below")
    print("=" * 78)
    ok = True
    for s in sessions:
        dur_s = 0.0
        if len(s.composite_ts) > 1:
            dur_s = (s.composite_ts[-1] - s.composite_ts[0]) / 1000
        print(f"\n[{s.label}] {s.path.name}  series={s.meta.get('series')}  "
              f"span={dur_s/60:.1f} min  composite_source={s.composite_source}")
        if s.composite_source != "brti_real":
            print("  NOTE: composite is a PROXY for BRTI (median of constituent "
                  "trade prices), not the settlement index itself.")

        print(f"  {'feed':<12} {'ticks':>9} {'per_s':>8} {'p50_gap_ms':>11} {'max_gap_s':>10}")
        for feed, (ts, _px) in sorted(s.ticks.items()):
            gaps = np.diff(ts) if len(ts) > 1 else np.array([np.nan])
            rate = len(ts) / max(dur_s, 1)
            flag = "  <-- STARVED" if rate < 0.05 else ""
            print(f"  {feed:<12} {len(ts):>9} {rate:>8.2f} {np.nanmedian(gaps):>11.0f} "
                  f"{np.nanmax(gaps)/1000:>10.1f}{flag}")
            if rate < 0.05:
                ok = False

        st = s.kalshi_stats
        print(f"  kalshi book rows={st.get('rows')} deltas={st.get('deltas')} "
              f"rejected={st.get('rejected')} unchanged={st.get('nochange')}")
        if st.get("deltas") and st["rejected"] / max(st["deltas"], 1) > 0.01:
            print("  !! >1% of deltas REJECTED -- wire shape may have changed. "
                  "Results from this session are NOT trustworthy.")
            ok = False
        if st.get("deltas") and st["nochange"] / max(st["deltas"], 1) > 0.5:
            print("  !! >50% of book messages changed nothing -- frozen-book "
                  "signature.  Results from this session are NOT trustworthy.")
            ok = False

        excl_by_feed: dict[str, int] = {}
        for feed, a, b, _r in s.excluded:
            excl_by_feed[feed] = excl_by_feed.get(feed, 0) + (b - a)
        total_excl = sum(excl_by_feed.values())
        print(f"  excluded: {total_excl/1000:.1f}s total "
              f"({100*total_excl/1000/max(dur_s,1):.1f}% of span) "
              f"{ {k: round(v/1000,1) for k, v in excl_by_feed.items()} }")

        kinds = {}
        for _ts, _feed, kind, _d in s.feed_events:
            kinds[kind] = kinds.get(kind, 0) + 1
        print(f"  feed events: {kinds or 'none'}")

        for _ts, phase, server, off, err in s.ntp:
            print(f"  ntp[{phase}] {server}: "
                  + (f"{off:+.1f} ms" if off is not None else f"FAILED ({err})"))
        offs = [o for _t, _p, _s, o, _e in s.ntp if o is not None]
        if len(offs) >= 2 and abs(offs[-1] - offs[0]) > 50:
            print(f"  !! clock DRIFTED {offs[-1]-offs[0]:+.1f} ms during the run -- "
                  f"lags of that order are suspect.")
        settled = sum(1 for m in s.markets.values() if m.get("result"))
        print(f"  markets={len(s.markets)} settled={settled} "
              f"unsettled={len(s.markets)-settled}")
    return ok


# ═════════════════════════════════════════════════════════════════════════════
# 1. Cross-correlation with null controls
# ═════════════════════════════════════════════════════════════════════════════

def _diff_series(win: Window, which: str) -> tuple[np.ndarray, np.ndarray]:
    p = win.p_comp if which == "composite" else win.p_cb
    k = win.mid
    good = win.valid & np.isfinite(p) & np.isfinite(k)
    dp = np.diff(p)
    dk = np.diff(k)
    g = good[1:] & good[:-1]
    return np.where(g, dp, np.nan), np.where(g, dk, np.nan)


def ccf(dp: np.ndarray, dk: np.ndarray, max_lag: int) -> tuple[np.ndarray, np.ndarray]:
    """
    corr(dk[t], dp[t - lag]) for lag in [-max_lag, +max_lag] grid steps.
    Positive lag = Kalshi moves AFTER the underlying (Kalshi lags).

    First differences, not levels: two series that both trend produce a huge,
    meaningless level correlation at every lag.  Changes are the question.
    """
    lags = np.arange(-max_lag, max_lag + 1)
    out = np.full(len(lags), np.nan)
    for i, lag in enumerate(lags):
        if lag >= 0:
            a, b = dk[lag:], dp[: len(dp) - lag] if lag else dp
        else:
            a, b = dk[: len(dk) + lag], dp[-lag:]
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() < 100:
            continue
        av, bv = a[m], b[m]
        if av.std() == 0 or bv.std() == 0:
            continue
        out[i] = float(np.corrcoef(av, bv)[0, 1])
    return lags, out


def _phase_randomize(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    y = np.nan_to_num(x)
    f = np.fft.rfft(y)
    phases = rng.uniform(0, 2 * np.pi, len(f))
    phases[0] = 0
    out = np.fft.irfft(np.abs(f) * np.exp(1j * phases), n=len(y))
    return np.where(np.isfinite(x), out, np.nan)


def _block_shuffle(x: np.ndarray, rng: np.random.Generator, block: int) -> np.ndarray:
    n = len(x)
    nb = max(1, n // block)
    idx = rng.permutation(nb)
    parts = [x[i * block:(i + 1) * block] for i in idx]
    out = np.concatenate(parts) if parts else x.copy()
    if len(out) < n:
        out = np.concatenate([out, x[len(out):]])
    return out[:n]


def analyse_ccf(windows: list[Window], rng: np.random.Generator) -> dict:
    print("\n" + "=" * 78)
    print("1. CROSS-CORRELATION (Kalshi mid vs implied prob), with null controls")
    print("=" * 78)
    max_lag = int(CCF_MAX_LAG_MS / GRID_MS)
    results = {}
    for which in ("coinbase", "composite"):
        dps, dks = [], []
        for w in windows:
            dp, dk = _diff_series(w, which)
            dps.append(dp)
            dks.append(dk)
            dps.append(np.full(max_lag + 1, np.nan))  # separator: no cross-window pairing
            dks.append(np.full(max_lag + 1, np.nan))
        dp = np.concatenate(dps) if dps else np.zeros(0)
        dk = np.concatenate(dks) if dks else np.zeros(0)
        n_pairs = int((np.isfinite(dp) & np.isfinite(dk)).sum())
        if n_pairs < 1000:
            print(f"\n vs {which}: only {n_pairs} usable paired samples -- "
                  f"INSUFFICIENT for a cross-correlation.")
            results[which] = {"n": n_pairs, "peak_lag_ms": None}
            continue

        lags, c = ccf(dp, dk, max_lag)
        if not np.isfinite(c).any():
            print(f"\n vs {which}: correlation undefined at every lag.")
            results[which] = {"n": n_pairs, "peak_lag_ms": None}
            continue
        peak_i = int(np.nanargmax(c))
        peak_lag_ms = int(lags[peak_i] * GRID_MS)
        peak_c = float(c[peak_i])

        null_peaks = []
        for _ in range(N_SURROGATES // 2):
            for maker in (_phase_randomize, lambda x, r: _block_shuffle(x, r, int(SURROGATE_BLOCK_MS / GRID_MS))):
                s = maker(dp, rng)
                _, cs = ccf(s, dk, max_lag)
                if np.isfinite(cs).any():
                    null_peaks.append(float(np.nanmax(cs)))
        null = np.array(null_peaks)
        band_hi = float(np.percentile(null, 97.5)) if len(null) else np.nan
        band_lo = float(np.percentile(null, 2.5)) if len(null) else np.nan

        zero_c = float(c[max_lag])
        prominence = peak_c - float(np.nanmedian(c))
        clear_peak = bool(np.isfinite(band_hi) and peak_c > band_hi and prominence > 0.02)

        print(f"\n vs {which}-implied prob   (n_pairs={n_pairs:,})")
        print(f"   peak lag = {peak_lag_ms:+d} ms   corr@peak = {peak_c:.4f}   "
              f"corr@0 = {zero_c:.4f}")
        print(f"   null band (2.5-97.5 pct of {len(null)} surrogates, "
              f"phase-randomised + block-shuffled): [{band_lo:.4f}, {band_hi:.4f}]")
        print(f"   peak prominence over median-lag corr: {prominence:.4f}   "
              f"-> {'CLEAR PEAK' if clear_peak else 'FLAT / not above null'}")
        step = max(1, max_lag // 12)
        print("   profile: " + "  ".join(
            f"{int(l*GRID_MS):+d}ms:{v:.3f}" for l, v in zip(lags[::step], c[::step])
            if np.isfinite(v)))
        results[which] = {
            "n": n_pairs, "peak_lag_ms": peak_lag_ms, "peak_corr": peak_c,
            "band_hi": band_hi, "clear_peak": clear_peak, "corr_at_0": zero_c,
        }

    cb, cp = results.get("coinbase", {}), results.get("composite", {})
    print("\n INTERPRETATION")
    if cb.get("peak_lag_ms") is None or cp.get("peak_lag_ms") is None:
        print("   Not enough data to distinguish index basis from a real lag.")
    else:
        lag_cb, lag_cp = cb["peak_lag_ms"], cp["peak_lag_ms"]
        if lag_cb >= CCF_MIN_LAG_MS and lag_cp < CCF_MIN_LAG_MS:
            print(f"   Kalshi lags COINBASE by {lag_cb} ms but tracks the COMPOSITE "
                  f"({lag_cp} ms).\n   => This is INDEX BASIS, not a tradeable lag. "
                  f"Coinbase is one constituent of a\n      multi-exchange index; "
                  f"trading its lead is trading the basis, and the basis\n      "
                  f"mean-reverts without the contract ever being mispriced.")
        elif lag_cp >= CCF_MIN_LAG_MS and cp.get("clear_peak"):
            print(f"   Kalshi lags the COMPOSITE by {lag_cp} ms with a peak above the "
                  f"null band.\n   => Candidate real lag.  Note the composite is a "
                  f"trade-price proxy for a\n      book-based, time-smoothed index, "
                  f"so the proxy LEADS the true BRTI on\n      impulse moves; part of "
                  f"this lag is proxy artefact.  Criteria 2-4 decide.")
        else:
            print(f"   No lag above threshold against either reference "
                  f"(coinbase {lag_cb} ms, composite {lag_cp} ms).")
    return results


# ═════════════════════════════════════════════════════════════════════════════
# 2. Response profile
# ═════════════════════════════════════════════════════════════════════════════

def find_moves(w: Window) -> list[int]:
    """
    Indices where the underlying moved >= MOVE_BPS bps OR >= MOVE_SIGMA sigma
    over MOVE_WINDOW_S.  Both criteria are pre-registered (N=5 bps, K=3 sigma);
    sigma here is the same ex-ante EWMA the replay uses, scaled to the window.
    """
    k = int(MOVE_WINDOW_S * 1000 / GRID_MS)
    if len(w.grid) <= k:
        return []
    past = np.concatenate([np.full(k, np.nan), w.comp_px[:-k]])
    with np.errstate(invalid="ignore", divide="ignore"):
        bps = (w.comp_px - past) / past * 1e4
        sig_bps = w.sigma / math.sqrt(365.25 * 24 * 3600) * math.sqrt(MOVE_WINDOW_S) * 1e4
    big = (np.abs(bps) >= MOVE_BPS) | (np.abs(bps) >= MOVE_SIGMA * sig_bps)
    elig = w.valid & big & np.isfinite(bps) & (w.tte >= TTE_MIN_S) & (w.tte <= TTE_MAX_S)
    idx = np.flatnonzero(elig)
    # De-duplicate: one event per move, not one per grid point inside it.
    out, last = [], -10**9
    for i in idx:
        if i - last >= k:
            out.append(int(i))
            last = i
    return out


def tte_bucket(tte: float) -> str:
    if tte > 600:
        return ">10m"
    if tte > 300:
        return "10-5m"
    if tte > 120:
        return "5-2m"
    return "<2m"


def analyse_response(windows: list[Window]) -> dict:
    print("\n" + "=" * 78)
    print(f"2. RESPONSE PROFILE  (move = |1s composite move| >= {MOVE_BPS:.0f} bps "
          f"OR >= {MOVE_SIGMA:.0f} sigma)")
    print("=" * 78)
    rows: list[dict] = []
    for w in windows:
        for i in find_moves(w):
            mv_bps = float((w.comp_px[i] - w.comp_px[max(i - int(1000/GRID_MS), 0)])
                           / w.comp_px[max(i - int(1000/GRID_MS), 0)] * 1e4)
            sign = math.copysign(1.0, mv_bps)
            base_mid = w.mid[i]
            full_j = i + int(FULL_REPRICE_HORIZON_MS / GRID_MS)
            if full_j >= len(w.grid) or not np.isfinite(base_mid):
                continue
            full_move = (w.mid[full_j] - base_mid) * sign
            row = {"tte": tte_bucket(float(w.tte[i])), "bps": abs(mv_bps),
                   "full": full_move, "session": w.session}
            for off in RESPONSE_OFFSETS_MS:
                j = i + int(off / GRID_MS)
                row[off] = (w.mid[j] - base_mid) * sign if j < len(w.grid) else np.nan
            rows.append(row)

    if not rows:
        print("  No qualifying moves found.")
        return {"n": 0}

    print(f"  {len(rows)} qualifying moves.")
    big_cut = float(np.percentile([r["bps"] for r in rows], 75))
    print(f"  'largest moves' = top quartile by size (>= {big_cut:.1f} bps)\n")
    header = f"  {'tte':<7} {'n':>5} " + " ".join(f"{o/1000:>7.2f}s" for o in RESPONSE_OFFSETS_MS) + f" {'+30s':>8}"
    print(header)
    for bucket in (">10m", "10-5m", "5-2m", "<2m"):
        sel = [r for r in rows if r["tte"] == bucket and r["bps"] >= big_cut]
        if not sel:
            print(f"  {bucket:<7} {'0':>5}   (no large moves in this bucket)")
            continue
        cells = []
        for off in RESPONSE_OFFSETS_MS:
            v = np.nanmedian([r[off] for r in sel])
            cells.append(f"{v*100:>7.2f}" if np.isfinite(v) else f"{'--':>7}")
        full = np.nanmedian([r["full"] for r in sel])
        print(f"  {bucket:<7} {len(sel):>5} " + " ".join(cells) + f" {full*100:>8.2f}")
    print("  (cells are median Kalshi mid change in CENTS, signed with the move)")

    for bucket in (">10m", "10-5m", "5-2m", "<2m"):
        sel = [r for r in rows if r["tte"] == bucket and r["bps"] >= big_cut
               and abs(r["full"]) > 0.005]
        if not sel:
            continue
        fracs = []
        for off in RESPONSE_OFFSETS_MS:
            f = np.nanmedian([r[off] / r["full"] for r in sel if r["full"] != 0])
            fracs.append(f"{off/1000:.2f}s:{100*f:.0f}%")
        print(f"  fraction of eventual +30s reprice completed [{bucket}, n={len(sel)}]: "
              + " ".join(fracs))
    return {"n": len(rows), "rows": rows, "big_cut": big_cut}


# ═════════════════════════════════════════════════════════════════════════════
# 3. Tradeable gap
# ═════════════════════════════════════════════════════════════════════════════

def fee_cents(price_dollars: float) -> float:
    """Kalshi taker fee per contract, in cents, ROUNDED UP (as Kalshi does)."""
    return math.ceil(fee_per_contract(price_dollars, settings) * 100.0)


def analyse_gap(windows: list[Window]) -> dict:
    print("\n" + "=" * 78)
    print("3. TRADEABLE GAP during response windows (never mid)")
    print("=" * 78)
    recs = []
    for w in windows:
        for i in find_moves(w):
            for j_off in range(0, int(3000 / GRID_MS)):   # first 3s after the move
                j = i + j_off
                if j >= len(w.grid) or not w.valid[j]:
                    continue
                fair = w.p_comp[j]
                if not np.isfinite(fair):
                    continue
                ask, bid = w.ask[j], w.bid[j]
                if not (np.isfinite(ask) and np.isfinite(bid)):
                    continue
                spread = max(0.0, ask - bid)
                # Buy YES at ask, or buy NO at (1-bid).  Both expressed as
                # "edge on the side we would actually pay for".
                buy_edge = fair - ask
                sell_edge = bid - fair
                if buy_edge >= sell_edge:
                    recs.append((buy_edge, ask, w.ask_size[j], spread, "buy_yes", w.session))
                else:
                    recs.append((sell_edge, 1 - bid, w.bid_size[j], spread, "buy_no", w.session))
    if not recs:
        print("  No usable response-window observations.")
        return {"n": 0, "criterion2": False}

    edges = np.array([r[0] for r in recs]) * 100
    prices = np.array([r[1] for r in recs])
    sizes = np.array([r[2] for r in recs])
    spreads = np.array([r[3] for r in recs]) * 100
    fees = np.array([fee_cents(p) for p in prices])
    hurdle = fees + spreads + GAP_MARGIN_CENTS

    def q(a, p):
        return float(np.nanpercentile(a, p))

    print(f"  n={len(recs):,} observations in the 3s after a qualifying move")
    print(f"  {'quantity':<28} {'p5':>8} {'p25':>8} {'median':>8} {'p75':>8} {'p95':>8}")
    for name, arr in (("gap vs traded price (c)", edges), ("price paid (c)", prices * 100),
                      ("size at that price", sizes), ("full spread (c)", spreads),
                      ("fee at that price (c)", fees), ("cost hurdle (c)", hurdle)):
        print(f"  {name:<28} {q(arr,5):>8.2f} {q(arr,25):>8.2f} {q(arr,50):>8.2f} "
              f"{q(arr,75):>8.2f} {q(arr,95):>8.2f}")

    med_gap = float(np.nanmedian(edges))
    med_hurdle = float(np.nanmedian(hurdle))
    passes = med_gap > med_hurdle
    print(f"\n  CRITERION 2: median gap {med_gap:.2f}c vs median hurdle "
          f"{med_hurdle:.2f}c (fee + full spread + {GAP_MARGIN_CENTS:.0f}c) "
          f"-> {'PASS' if passes else 'FAIL'}")

    big = sizes[edges >= SIGNAL_X_CENTS]
    frac = float((big >= SIGNAL_Z_SIZE).mean()) if len(big) else 0.0
    print(f"  CRITERION 3: of {len(big):,} observations with a gap >= "
          f"{SIGNAL_X_CENTS:.0f}c, {100*frac:.1f}% had >= {SIGNAL_Z_SIZE} contracts "
          f"resting (median size {np.median(big) if len(big) else float('nan'):.0f}) "
          f"-> {'PASS' if frac > 0.5 else 'FAIL'}")
    return {"n": len(recs), "median_gap_c": med_gap, "median_hurdle_c": med_hurdle,
            "criterion2": passes, "criterion3": frac > 0.5, "size_frac": frac}


# ═════════════════════════════════════════════════════════════════════════════
# 4. Latency
# ═════════════════════════════════════════════════════════════════════════════

def analyse_latency(sessions: list[Session], measure: bool, ticker: str | None) -> float:
    print("\n" + "=" * 78)
    print("4. OUR LATENCY -- LOWER BOUND on order latency")
    print("=" * 78)
    pooled: dict[str, list[float]] = {}
    for s in sessions:
        for kind, arr in s.latency.items():
            pooled.setdefault(kind, []).extend(arr.tolist())

    if measure:
        from kbot.kalshi_client import KalshiClient
        tk = ticker or next(
            (t for s in sessions for t in s.markets), None
        )
        if tk:
            print(f"  sampling 50 live read round trips against {tk} ...")
            with KalshiClient(settings) as c:
                for _ in range(50):
                    t0 = time.perf_counter()
                    try:
                        c.get_market(tk)
                        pooled.setdefault("read_rtt_live", []).append(
                            (time.perf_counter() - t0) * 1000)
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.2)

    p95 = None
    for kind, vals in sorted(pooled.items()):
        a = np.array(vals)
        if not len(a):
            continue
        print(f"  {kind:<16} n={len(a):>4}  median={np.median(a):>7.1f} ms  "
              f"p95={np.percentile(a,95):>7.1f} ms  max={a.max():>7.1f} ms")
        if kind.startswith("read_rtt"):
            p95 = float(np.percentile(a, 95))
    if p95 is None:
        p95 = 300.0
        print("  !! no read-RTT samples in the recording; assuming 300 ms p95 for "
              "the replay.  Re-run with --measure-latency for a real number.")
    print(f"\n  Replay will assume orders arrive {p95:.0f} ms after the signal.")
    print("  THIS IS A LOWER BOUND.  It measures a READ round trip; an order "
          "traverses\n  the write path (auth, risk checks, matching) and this "
          "study never sent one\n  to measure it.  Real order latency is higher, "
          "so every replay result below\n  is OPTIMISTIC by an unmeasured amount.")
    return p95


# ═════════════════════════════════════════════════════════════════════════════
# 5. Ex-ante replay + controls
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    session: str
    ticker: str
    ts_ms: float
    side: str          # "yes" or "no"
    limit_price: float # what we would pay per contract
    filled: int
    fee_c: float
    pnl: float         # dollars, after fees, held to settlement
    settled: str


def _fill(w: Window, arrive_idx: int, side: str, limit: float, want: int) -> tuple[int, float]:
    """
    Pessimistic fill: we are a taker with a LIMIT at the price the signal saw.

    - We fill only against size resting AT THAT PRICE AT ARRIVAL.  If the book
      moved away in the latency window, we get nothing (no chasing).
    - Partial fill when the resting size is smaller than our order.
    - Everything is evaluated at the arrival index, never the signal index.
    """
    if arrive_idx >= len(w.grid) or not w.valid[arrive_idx]:
        return 0, 0.0
    if side == "yes":
        price, size = w.ask[arrive_idx], w.ask_size[arrive_idx]
    else:
        price, size = (1.0 - w.bid[arrive_idx]), w.bid_size[arrive_idx]
    if not (np.isfinite(price) and np.isfinite(size)):
        return 0, 0.0
    if price > limit + 1e-9:       # price moved away -> our limit does not fill
        return 0, 0.0
    return int(min(want, max(0, int(size)))), float(price)


def _settle_pnl(w: Window, side: str, price: float, qty: int) -> tuple[float, float] | None:
    if not w.result:
        return None
    won = (w.result == "yes") if side == "yes" else (w.result == "no")
    fee = fee_cents(price) / 100.0
    per = (1.0 - price if won else -price) - fee
    return per * qty, fee * qty


def replay(windows: list[Window], latency_ms: float,
           entry_fn: Callable[[Window], Iterable[tuple[int, str, float]]],
           label: str) -> tuple[list[Trade], int, int]:
    """
    Walk forward.  entry_fn yields (grid_index, side, limit_price) using only
    data at or before that index; this function adds latency, fills, and
    settles.  Returns (trades, n_signals, n_unsettled_dropped).
    """
    lat = int(round(latency_ms / GRID_MS))
    trades: list[Trade] = []
    n_signals = 0
    n_unsettled = 0
    for w in windows:
        cooldown_until = -10**9
        for idx, side, limit in entry_fn(w):
            if idx < cooldown_until:
                continue
            n_signals += 1
            cooldown_until = idx + int(REPLAY_COOLDOWN_S * 1000 / GRID_MS)
            qty, price = _fill(w, idx + lat, side, limit, REPLAY_ORDER_SIZE)
            if qty <= 0:
                continue
            got = _settle_pnl(w, side, price, qty)
            if got is None:
                n_unsettled += 1
                continue
            pnl, fee = got
            trades.append(Trade(w.session, w.ticker, float(w.grid[idx + lat]), side,
                                price, qty, fee, pnl, w.result or ""))
    return trades, n_signals, n_unsettled


def signal_entries(w: Window) -> list[tuple[int, str, float]]:
    """
    THE pre-registered signal.  Ex-ante by construction: every array it reads
    (p_comp, ask, bid, sizes, sigma) is forward-filled from data at or before
    each index, and the sustain test looks only backwards.
    """
    n = len(w.grid)
    sustain = int(SIGNAL_Y_MS / GRID_MS)
    elig = w.valid & (w.tte >= TTE_MIN_S) & (w.tte <= TTE_MAX_S) & (w.comp_n >= MIN_SOURCES)
    buy_edge = (w.p_comp - w.ask) * 100
    sell_edge = (w.bid - w.p_comp) * 100
    buy_ok = elig & (buy_edge >= SIGNAL_X_CENTS) & (w.ask_size >= SIGNAL_Z_SIZE)
    sell_ok = elig & (sell_edge >= SIGNAL_X_CENTS) & (w.bid_size >= SIGNAL_Z_SIZE)

    out: list[tuple[int, str, float]] = []
    for i in range(sustain, n):
        if buy_ok[i] and buy_ok[i - sustain:i + 1].all():
            out.append((i, "yes", float(w.ask[i])))
        elif sell_ok[i] and sell_ok[i - sustain:i + 1].all():
            out.append((i, "no", float(1.0 - w.bid[i])))
    return out


def _ticker_seed(ticker: str) -> int:
    """
    Stable per-ticker seed.  NOT builtin hash(): Python salts string hashing per
    process, so a control built on hash() would give a different answer on every
    run of the same recording -- unacceptable in a pre-registered study.
    """
    return zlib.crc32(ticker.encode()) % 100_000


def control_random(trades_per_window: dict[str, int], seed: int):
    """Random entry at matched times/sizes, same fill machinery."""
    def entries(w: Window) -> list[tuple[int, str, float]]:
        rng = np.random.default_rng(seed + _ticker_seed(w.ticker))
        k = trades_per_window.get(w.ticker, 0)
        if k == 0:
            return []
        elig = np.flatnonzero(
            w.valid & (w.tte >= TTE_MIN_S) & (w.tte <= TTE_MAX_S)
            & np.isfinite(w.ask) & np.isfinite(w.bid))
        if len(elig) == 0:
            return []
        picks = rng.choice(elig, size=min(k, len(elig)), replace=False)
        out = []
        for i in sorted(int(x) for x in picks):
            if rng.random() < 0.5:
                out.append((i, "yes", float(w.ask[i])))
            else:
                out.append((i, "no", float(1.0 - w.bid[i])))
        return out
    return entries


def control_cheaper(trades_per_window: dict[str, int], seed: int):
    """Always buy whichever side is cheaper, at matched times."""
    def entries(w: Window) -> list[tuple[int, str, float]]:
        rng = np.random.default_rng(seed + 7 + _ticker_seed(w.ticker))
        k = trades_per_window.get(w.ticker, 0)
        if k == 0:
            return []
        elig = np.flatnonzero(
            w.valid & (w.tte >= TTE_MIN_S) & (w.tte <= TTE_MAX_S)
            & np.isfinite(w.ask) & np.isfinite(w.bid))
        if len(elig) == 0:
            return []
        picks = rng.choice(elig, size=min(k, len(elig)), replace=False)
        out = []
        for i in sorted(int(x) for x in picks):
            yes_price, no_price = float(w.ask[i]), float(1.0 - w.bid[i])
            if yes_price <= no_price:
                out.append((i, "yes", yes_price))
            else:
                out.append((i, "no", no_price))
        return out
    return entries


def bootstrap_ci(x: np.ndarray, rng: np.random.Generator, n: int = BOOTSTRAP_N):
    if len(x) == 0:
        return (np.nan, np.nan)
    idx = rng.integers(0, len(x), size=(n, len(x)))
    means = x[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def summarise(trades: list[Trade], label: str, rng: np.random.Generator) -> dict:
    if not trades:
        print(f"  {label:<26} no fills")
        return {"n": 0, "mean": np.nan, "lo": np.nan, "hi": np.nan, "total": 0.0}
    per = np.array([t.pnl / t.filled for t in trades])
    total = float(sum(t.pnl for t in trades))
    lo, hi = bootstrap_ci(per, rng)
    wins = float(np.mean([t.pnl > 0 for t in trades]))
    contracts = sum(t.filled for t in trades)
    print(f"  {label:<26} n={len(trades):>5}  contracts={contracts:>6}  "
          f"win={100*wins:>5.1f}%  mean={100*per.mean():>+7.2f}c/contract  "
          f"95% CI [{100*lo:+.2f}, {100*hi:+.2f}]c  total=${total:+.2f}")
    return {"n": len(trades), "mean": float(per.mean()), "lo": lo, "hi": hi,
            "total": total, "win": wins, "per": per}


def analyse_replay(windows: list[Window], latency_ms: float, rng: np.random.Generator,
                   tag: str) -> dict:
    print(f"\n  --- {tag} ---")
    trades, n_sig, n_unsettled = replay(windows, latency_ms, signal_entries, "signal")
    if n_unsettled:
        print(f"  ({n_unsettled} filled signals dropped: window never settled in the "
              f"recording -- run with --fetch-settlement)")
    print(f"  signals fired: {n_sig}   filled: {len(trades)}   "
          f"unfilled (book moved away / no size): {n_sig - len(trades) - n_unsettled}")
    per_window: dict[str, int] = {}
    for t in trades:
        per_window[t.ticker] = per_window.get(t.ticker, 0) + 1

    res = {"n_signals": n_sig}
    res["signal"] = summarise(trades, "SIGNAL", rng)
    ctrl_r, _, _ = replay(windows, latency_ms, control_random(per_window, 12345), "random")
    res["random"] = summarise(ctrl_r, "control: random entry", rng)
    ctrl_c, _, _ = replay(windows, latency_ms, control_cheaper(per_window, 999), "cheaper")
    res["cheaper"] = summarise(ctrl_c, "control: buy cheaper side", rng)
    res["trades"] = trades
    return res


# ═════════════════════════════════════════════════════════════════════════════
# 6. Maker variant
# ═════════════════════════════════════════════════════════════════════════════

def _level_size(levels_json: str, price: float, is_yes_side: bool) -> float:
    try:
        levels = json.loads(levels_json) or []
    except Exception:  # noqa: BLE001
        return np.nan
    target = price if is_yes_side else round(1.0 - price, 4)
    for p, q in levels:
        if abs(p - target) < 1e-6:
            return float(q)
    return 0.0


def analyse_maker(windows: list[Window], latency_ms: float) -> dict:
    print("\n" + "=" * 78)
    print("6. MAKER VARIANT (we join the best price, BEHIND all existing size)")
    print("=" * 78)
    fills = adverse = attempts = stranded = 0
    time_to_fill = []
    for w in windows:
        lat = int(round(latency_ms / GRID_MS))
        for idx, side, _limit in signal_entries(w):
            attempts += 1
            i = idx + lat
            if i >= len(w.grid) or not w.valid[i]:
                continue
            # Join the side we want to buy, at the current best price on that side.
            if side == "yes":
                my_price = float(w.bid[i])          # rest a YES bid at the best bid
                size_series = w.bid_size
                px_series = w.bid
                through = lambda p, j: w.bid[j] < p - 1e-9   # noqa: E731
            else:
                my_price = float(1.0 - w.ask[i])
                size_series = w.ask_size
                px_series = w.ask
                through = lambda p, j: w.ask[j] > (1.0 - p) + 1e-9  # noqa: E731
            if not np.isfinite(my_price) or not np.isfinite(size_series[i]):
                continue
            queue_ahead = float(size_series[i])
            filled_at = None
            for j in range(i + 1, min(len(w.grid), i + int(120_000 / GRID_MS))):
                if abs(px_series[j] - px_series[i]) > 1e-9:
                    # Best moved: either through us (we get filled, adversely)
                    # or away (we are stranded off-touch and give up).
                    if through(my_price, j):
                        filled_at = j
                    else:
                        stranded += 1
                    break
                depleted = queue_ahead - float(size_series[j])
                if depleted >= queue_ahead:
                    filled_at = j
                    break
            if filled_at is None:
                continue
            fills += 1
            time_to_fill.append((w.grid[filled_at] - w.grid[i]) / 1000)
            h = filled_at + int(MAKER_ADVERSE_HORIZON_S * 1000 / GRID_MS)
            if h < len(w.grid):
                moved = (w.mid[h] - w.mid[filled_at]) * (1 if side == "no" else -1)
                if moved * 100 >= MAKER_ADVERSE_CENTS:
                    adverse += 1

    fill_rate = fills / attempts if attempts else float("nan")
    adverse_rate = adverse / fills if fills else float("nan")
    print(f"  attempts={attempts}  fills={fills}  fill rate={100*fill_rate:.1f}%  "
          f"stranded off-touch={stranded}")
    print(f"  median time to fill: "
          f"{np.median(time_to_fill) if time_to_fill else float('nan'):.2f}s")
    print(f"  ADVERSE-FILL RATE (mid moved >= {MAKER_ADVERSE_CENTS:.0f}c through us "
          f"within {MAKER_ADVERSE_HORIZON_S:.0f}s): {100*adverse_rate:.1f}%")
    print(f"  cancel round trip (LOWER BOUND, 2x measured read RTT p95): "
          f"{2*latency_ms:.0f} ms")
    quick = float(np.mean([t <= 2 * latency_ms / 1000 for t in time_to_fill])) if time_to_fill else float("nan")
    print(f"  fraction of fills that happened INSIDE one cancel round trip "
          f"(i.e. uncancellable): {100*quick:.1f}%")
    print("  MODEL CAVEAT: Kalshi's public feed gives book deltas, not a trade "
          "tape, so\n  every size DECREASE at our level is counted as a fill "
          "ahead of us.  Cancels\n  look identical to trades, so this OVERSTATES "
          "the fill rate; treat it as an\n  upper bound.")
    return {"attempts": attempts, "fills": fills, "fill_rate": fill_rate,
            "adverse_rate": adverse_rate}


# ═════════════════════════════════════════════════════════════════════════════
# 7. Sanity
# ═════════════════════════════════════════════════════════════════════════════

def analyse_sanity(windows: list[Window]) -> None:
    print("\n" + "=" * 78)
    print("7. SANITY -- distribution of EVERY intermediate value")
    print("=" * 78)
    cat: dict[str, list[np.ndarray]] = {}

    def add(name: str, arr: np.ndarray) -> None:
        cat.setdefault(name, []).append(np.asarray(arr, dtype=float))

    for w in windows:
        v = w.valid
        add("composite price", w.comp_px[v])
        add("coinbase price", w.cb_px[v])
        add("sigma (annualised)", w.sigma[v])
        add("time-to-expiry (s)", w.tte[v])
        add("implied prob (composite)", w.p_comp[v])
        add("implied prob (coinbase)", w.p_cb[v])
        add("kalshi mid", w.mid[v])
        add("best bid", w.bid[v])
        add("best ask", w.ask[v])
        add("spread (c)", (w.ask[v] - w.bid[v]) * 100)
        add("bid size", w.bid_size[v])
        add("ask size", w.ask_size[v])
        add("gap buy-yes (c)", (w.p_comp[v] - w.ask[v]) * 100)
        add("gap buy-no (c)", (w.bid[v] - w.p_comp[v]) * 100)

    print(f"  {'value':<26} {'n':>9} {'uniq':>7} {'min':>10} {'median':>10} "
          f"{'max':>10} {'flags'}")
    for name, parts in cat.items():
        a = np.concatenate(parts)
        a = a[np.isfinite(a)]
        if not len(a):
            print(f"  {name:<26} {'0':>9}   ALL NaN  <-- DEGENERATE")
            continue
        uniq = len(np.unique(np.round(a, 6)))
        flags = []
        if uniq <= 3:
            flags.append("PINNED/DEGENERATE")
        if np.isclose(a.min(), a.max()):
            flags.append("CONSTANT")
        if "prob" in name or name == "kalshi mid":
            edge = float(np.mean((a <= 0.01) | (a >= 0.99)))
            if edge > 0.5:
                flags.append(f"{100*edge:.0f}% at bounds")
        if "sigma" in name and (a.max() > 5 or a.min() <= 0):
            flags.append("implausible vol")
        print(f"  {name:<26} {len(a):>9,} {uniq:>7,} {a.min():>10.4f} "
              f"{np.median(a):>10.4f} {a.max():>10.4f} {' '.join(flags)}")
    print("\n  A 'uniq' of 1-3 on any row, or a probability pinned near a bound, "
          "means the\n  pipeline is broken upstream -- do not read the verdict, "
          "fix that first.\n  (This is the check that would have caught fair_p "
          "sitting at 0.51.)")


# ═════════════════════════════════════════════════════════════════════════════
# Parameter sweep (opt-in, never feeds the verdict)
# ═════════════════════════════════════════════════════════════════════════════

def analyse_sweep(windows: list[Window], latency_ms: float, rng: np.random.Generator) -> None:
    print("\n" + "=" * 78)
    print("APPENDIX: FULL PARAMETER SWEEP -- exploratory, NOT the verdict")
    print("=" * 78)
    global SIGNAL_X_CENTS, SIGNAL_Y_MS, SIGNAL_Z_SIZE
    keep = (SIGNAL_X_CENTS, SIGNAL_Y_MS, SIGNAL_Z_SIZE)
    combos = []
    print(f"  {'X(c)':>5} {'Y(ms)':>6} {'Z':>4} {'n':>6} {'mean c/ct':>10} {'CI lo':>8} {'CI hi':>8}")
    for x in (1, 2, 3, 4, 5):
        for y in (100, 300, 1000):
            for z in (5, 20, 50):
                SIGNAL_X_CENTS, SIGNAL_Y_MS, SIGNAL_Z_SIZE = float(x), y, z
                tr, _, _ = replay(windows, latency_ms, signal_entries, "sweep")
                if tr:
                    per = np.array([t.pnl / t.filled for t in tr])
                    lo, hi = bootstrap_ci(per, rng, n=2000)
                    mean = float(per.mean())
                else:
                    per, lo, hi, mean = np.array([]), np.nan, np.nan, np.nan
                combos.append((x, y, z, len(tr), mean))
                print(f"  {x:>5} {y:>6} {z:>4} {len(tr):>6} {100*mean:>10.2f} "
                      f"{100*lo:>8.2f} {100*hi:>8.2f}")
    SIGNAL_X_CENTS, SIGNAL_Y_MS, SIGNAL_Z_SIZE = keep
    n = len(combos)
    best = max((c for c in combos if c[3] > 0), key=lambda c: c[4], default=None)
    print(f"\n  ALL {n} combinations are printed above -- none omitted.")
    if best:
        print(f"  Best cell: X={best[0]} Y={best[1]} Z={best[2]} -> "
              f"{100*best[4]:.2f}c/contract on n={best[3]}.")
    print(f"  THE BEST-OF-{n} NUMBER IS INFLATED.  With {n} independent-ish tries, the\n"
          f"  expected maximum of pure noise sits roughly {norm.ppf(1 - 1/(2*n)):.1f} standard errors "
          f"above\n  zero -- for a typical per-trade sd of 0.45 and n=300 trades that is about\n"
          f"  {100*norm.ppf(1 - 1/(2*n))*0.45/math.sqrt(300):.1f}c/contract of pure selection bias.  "
          f"The pre-registered cell\n  (X={keep[0]:.0f}, Y={keep[1]}, Z={keep[2]}) is the only one that counts.")


# ═════════════════════════════════════════════════════════════════════════════
# Settlement backfill (read-only)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_settlements(paths: list[Path]) -> None:
    from kbot.kalshi_client import KalshiClient
    print("\nfetching missing settlements (READ-ONLY get_market)...")
    with KalshiClient(settings) as client:
        for p in paths:
            con = sqlite3.connect(str(p))
            todo = [r[0] for r in con.execute(
                "SELECT ticker FROM kalshi_market WHERE result IS NULL OR result=''")]
            for ticker in todo:
                try:
                    m = client.get_market(ticker)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {ticker}: {type(exc).__name__} {str(exc)[:80]}")
                    continue
                res = str(m.get("result") or "").strip().lower()
                if res in ("yes", "no"):
                    con.execute(
                        "UPDATE kalshi_market SET result=?, status=?, expiration_value=? "
                        "WHERE ticker=?",
                        (res, str(m.get("status") or ""),
                         str(m.get("expiration_value") or ""), ticker))
                    print(f"  {ticker}: {res}")
            con.commit()
            con.close()


# ═════════════════════════════════════════════════════════════════════════════
# Verdict
# ═════════════════════════════════════════════════════════════════════════════

def verdict(ccf_res: dict, gap_res: dict, pooled: dict, oos: dict | None,
            n_sessions: int, per_session: dict[str, dict]) -> None:
    print("\n" + "=" * 78)
    print("PRE-REGISTERED VERDICT")
    print("=" * 78)

    cp = ccf_res.get("composite", {})
    c1 = bool(cp.get("peak_lag_ms") is not None
              and cp["peak_lag_ms"] >= CCF_MIN_LAG_MS and cp.get("clear_peak"))
    print(f"  1. composite lag >= {CCF_MIN_LAG_MS}ms with clear peak: "
          f"{'PASS' if c1 else 'FAIL'}  "
          f"(peak {cp.get('peak_lag_ms')} ms, corr {cp.get('peak_corr', float('nan')):.4f}, "
          f"null hi {cp.get('band_hi', float('nan')):.4f})")

    c2 = bool(gap_res.get("criterion2"))
    print(f"  2. median gap > fee + spread + {GAP_MARGIN_CENTS:.0f}c: "
          f"{'PASS' if c2 else 'FAIL'}  "
          f"({gap_res.get('median_gap_c', float('nan')):.2f}c vs "
          f"{gap_res.get('median_hurdle_c', float('nan')):.2f}c)")

    c3 = bool(gap_res.get("criterion3"))
    print(f"  3. >= {SIGNAL_Z_SIZE} contracts resting when we act: "
          f"{'PASS' if c3 else 'FAIL'}  "
          f"({100*gap_res.get('size_frac', 0):.1f}% of gap observations)")

    sig = pooled.get("signal", {})
    n = sig.get("n", 0)
    insufficient = n < MIN_SIGNAL_EVENTS
    beats = (
        n > 0
        and np.isfinite(sig.get("mean", np.nan))
        and sig["mean"] > pooled["random"].get("mean", np.inf)
        and sig["mean"] > pooled["cheaper"].get("mean", np.inf)
    )
    ci_excludes_zero = n > 0 and np.isfinite(sig.get("lo", np.nan)) and sig["lo"] > 0
    c4 = bool(not insufficient and beats and ci_excludes_zero and sig.get("mean", 0) > 0)
    if insufficient:
        print(f"  4. replay profitable, beats controls, CI excludes zero: "
              f"INSUFFICIENT DATA -- record more\n"
              f"     (n={n} filled events, minimum {MIN_SIGNAL_EVENTS})")
    else:
        print(f"  4. replay profitable, beats controls, CI excludes zero: "
              f"{'PASS' if c4 else 'FAIL'}\n"
              f"     (mean {100*sig['mean']:+.2f}c/contract, CI "
              f"[{100*sig['lo']:+.2f}, {100*sig['hi']:+.2f}], vs random "
              f"{100*pooled['random'].get('mean', float('nan')):+.2f}c, cheaper "
              f"{100*pooled['cheaper'].get('mean', float('nan')):+.2f}c)")

    if oos is not None:
        osig = oos.get("signal", {})
        if osig.get("n", 0) == 0:
            print("     out-of-sample: no fills -- OOS requirement NOT met")
            c4 = False
        else:
            oos_ok = (np.isfinite(osig.get("lo", np.nan)) and osig["lo"] > 0
                      and osig["mean"] > oos["random"].get("mean", np.inf)
                      and osig["mean"] > oos["cheaper"].get("mean", np.inf))
            print(f"     out-of-sample (n={osig['n']}): mean "
                  f"{100*osig['mean']:+.2f}c CI [{100*osig['lo']:+.2f}, "
                  f"{100*osig['hi']:+.2f}] -> {'holds' if oos_ok else 'does NOT hold'}")
            c4 = c4 and bool(oos_ok)

    if len(per_session) > 1:
        positives = [k for k, v in per_session.items()
                     if v.get("signal", {}).get("n", 0) > 0
                     and v["signal"].get("mean", 0) > 0]
        print(f"\n  per-session profitability: {len(positives)}/{len(per_session)} "
              f"sessions positive {positives}")
        if len(positives) == 1 and len(per_session) > 1:
            print("  !! RED FLAG: the result holds in exactly ONE session. That is the "
                  "signature\n     of noise, not a discovery.")

    print()
    if insufficient:
        print("  VERDICT: INSUFFICIENT DATA -- record more.")
        print("  (Criteria 1-3 above still stand on their own; criterion 4 is "
              "unresolved,\n   and an unresolved criterion 4 is not a PASS.)")
    elif c1 and c2 and c3 and c4:
        print("  VERDICT: PASS -- all four pre-registered criteria met.")
        print("  Caveats that survive a PASS: order latency was measured as a LOWER "
              "BOUND\n  (read path only), the composite is a proxy for BRTI unless a "
              "real feed was\n  recorded, and the maker fill rate is an upper bound.")
    else:
        failed = [str(i) for i, ok in enumerate([c1, c2, c3, c4], 1) if not ok]
        print(f"  VERDICT: FAIL -- criteria {', '.join(failed)} not met.")


# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recordings", nargs="+", required=True, type=Path)
    p.add_argument("--measure-latency", action="store_true",
                   help="take 50 live READ round-trip samples (no orders)")
    p.add_argument("--fetch-settlement", action="store_true",
                   help="backfill missing settlements via READ-ONLY get_market")
    p.add_argument("--sweep", action="store_true",
                   help="print the FULL parameter sweep (exploratory appendix)")
    p.add_argument("--seed", type=int, default=20260810)
    args = p.parse_args()

    print(PREREG_TEXT)

    if args.fetch_settlement:
        fetch_settlements(args.recordings)

    rng = np.random.default_rng(args.seed)
    sessions = []
    for path in args.recordings:
        if not path.exists():
            print(f"missing recording: {path}")
            return 2
        sessions.append(load_session(path, path.stem))
    sessions.sort(key=lambda s: s.started_ms)
    for i, s in enumerate(sessions):
        s.label = f"S{i+1}:{s.path.stem[-15:]}"
    print(f"Loaded {len(sessions)} recording(s): {[s.label for s in sessions]}")

    quality_ok = report_quality(sessions)
    if not quality_ok:
        print("\n!! DATA QUALITY PROBLEMS ABOVE.  The analysis continues so you can "
              "see how\n!! bad it is, but a PASS on damaged data is not a PASS.")

    print("\nbuilding analysis grids...")
    windows_by_session: dict[str, list[Window]] = {}
    for s in sessions:
        ws = build_windows(s)
        windows_by_session[s.label] = ws
        print(f"  {s.label}: {len(ws)} usable window(s); mean excluded "
              f"{100*np.mean([w.excluded_frac for w in ws]) if ws else 0:.1f}% of grid")
    all_windows = [w for ws in windows_by_session.values() for w in ws]
    if not all_windows:
        print("\nNo usable windows -- nothing to analyse.")
        return 1

    settled = [w for w in all_windows if w.result]
    print(f"  pooled: {len(all_windows)} windows, {len(settled)} with a recorded "
          f"settlement (replay uses only these)")

    ccf_res = analyse_ccf(all_windows, rng)
    analyse_response(all_windows)
    gap_res = analyse_gap(all_windows)
    latency_p95 = analyse_latency(sessions, args.measure_latency, None)

    print("\n" + "=" * 78)
    print("5. EX-ANTE REPLAY (walk-forward, pessimistic fills, held to settlement)")
    print("=" * 78)
    print(f"  order size {REPLAY_ORDER_SIZE} contracts, arrives {latency_p95:.0f} ms "
          f"after the signal, fills only\n  against size resting at that price at "
          f"arrival, fee rounded up per contract.")

    per_session: dict[str, dict] = {}
    for label, ws in windows_by_session.items():
        if ws:
            per_session[label] = analyse_replay(ws, latency_p95, rng, f"session {label}")

    pooled = analyse_replay(all_windows, latency_p95, rng, "POOLED (verdict basis)")

    oos = None
    if len(sessions) >= 2:
        is_label = sessions[0].label
        oos_windows = [w for lbl, ws in windows_by_session.items() if lbl != is_label
                       for w in ws]
        print("\n  --- IN-SAMPLE vs OUT-OF-SAMPLE ---")
        print(f"  in-sample = {is_label} (first recording); out-of-sample = the rest.")
        print("  Nothing is fitted on the in-sample data -- the parameters were fixed "
              "in\n  source before any recording -- so this is a stability check.")
        analyse_replay(windows_by_session[is_label], latency_p95, rng, "IN-SAMPLE")
        oos = analyse_replay(oos_windows, latency_p95, rng, "OUT-OF-SAMPLE")
    else:
        print("\n  !! ONLY ONE RECORDING.  No out-of-sample split is possible, so the "
              "OOS\n  !! requirement cannot be satisfied.  Record more sessions.")

    analyse_maker(all_windows, latency_p95)
    analyse_sanity(all_windows)
    if args.sweep:
        analyse_sweep(all_windows, latency_p95, rng)

    verdict(ccf_res, gap_res, pooled, oos, len(sessions), per_session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
