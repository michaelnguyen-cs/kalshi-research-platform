#!/usr/bin/env python
"""
READ-ONLY DIAGNOSTIC for the three VM findings. Measures; changes nothing.

Makes no network call, opens no Kalshi endpoint, writes no file, imports no
execution path. Every number it prints is a measurement of code that already
ran -- nothing here is tuned, fitted, or judged against P&L.

    python scripts/diagnose_vol_and_replay.py \
        --rec data/rec_A_usmorning.sqlite \
        --rec data/rec_B_overnight.sqlite \
        --rec data/rec_C_highvol.sqlite \
        --events kbot_events.db --registry kbot_registry.db

PART A  why measure_basis_and_vol.py section 2 produced zero samples: the
        filter chain, counted stage by stage, plus excluded_span coverage
        BY FEED (that script's _excluded() ignores the feed column).
PART B  the RAW ewma_vol distribution before any clamp, plus the tick-spacing
        diagnostics that decide whether the dispersion is the market (a) or
        the estimator (b).
PART C  the SECTION 3 reconstruction failure: error split by strike source,
        a monotonicity test that separates a wrong strike from a wrong
        calibrator without needing either, and an exact strike inversion.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import pickle
import sqlite3
import statistics
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scipy.stats import norm                                    # noqa: E402
from kbot.config import settings as _cfg                        # noqa: E402
from kbot.vol import ewma_vol                                   # noqa: E402
from kbot.learning.calibration import apply_calibrator          # noqa: E402
from kbot.strategy.btc_hourly import _parse_ticker, effective_t_years  # noqa: E402

YEAR = 365.25 * 24 * 3600
WINDOW = 900.0          # cfg.vol_window_seconds_15m, the live 15m window
GRID = 60.0             # same decimation measure_basis_and_vol.py uses
LAM = 0.94              # kbot.vol.ewma_vol default
CTRL_STEP = 5.0         # uniform-grid control estimator step, seconds


def pct(v, q):
    if not v:
        return float("nan")
    s = sorted(v)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[int(pos)] if lo == hi else s[lo] + (s[hi] - s[lo]) * (pos - lo)


def desc(name, v, w=30):
    if not v:
        print(f"  {name:<{w}} (none)")
        return
    print(f"  {name:<{w}} n={len(v):<7d} p1={pct(v,1):.4f} p5={pct(v,5):.4f} "
          f"p25={pct(v,25):.4f} p50={pct(v,50):.4f} p75={pct(v,75):.4f} "
          f"p95={pct(v,95):.4f} p99={pct(v,99):.4f} min={min(v):.4f} max={max(v):.4f}")


def spearman(x, y):
    """Rank correlation, so a monotone reshaping cannot hide a real disagreement."""
    n = len(x)
    if n < 3:
        return float("nan")

    def ranks(a):
        order = sorted(range(n), key=lambda i: a[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else float("nan")


class Series:
    def __init__(self, ts, px):
        self.ts, self.px = ts, px

    def window(self, lo_ms, hi_ms):
        a = bisect.bisect_right(self.ts, lo_ms)
        b = bisect.bisect_right(self.ts, hi_ms)
        return [(self.ts[i] / 1000.0, self.px[i]) for i in range(a, b)]

    def at(self, when_ms, max_stale_ms):
        i = bisect.bisect_right(self.ts, when_ms) - 1
        if i < 0 or when_ms - self.ts[i] > max_stale_ms:
            return None
        return self.px[i]


def load_feed(conn, feed):
    rows = conn.execute(
        "SELECT local_ts_ms, price FROM underlying_tick WHERE feed=? ORDER BY local_ts_ms",
        (feed,),
    ).fetchall()
    return Series([int(r[0]) for r in rows], [float(r[1]) for r in rows])


def ewma_parts(samples):
    """
    kbot.vol.ewma_vol's internals, re-derived so the pieces are visible.

    Returns (sigma, std_per_sample, dt_avg, dt_recent, n_returns, zero_frac).
    `sigma` is checked against the real ewma_vol() output by the caller, so this
    copy can never silently drift from the function being diagnosed.
    """
    if len(samples) < 2:
        return None
    ts = [t for t, _ in samples]
    px = [p for _, p in samples]
    rets, rts = [], []
    for i in range(1, len(px)):
        if px[i] > 0 and px[i - 1] > 0:
            rets.append(math.log(px[i] / px[i - 1]))
            rts.append(ts[i] - ts[i - 1])
    if not rets:
        return None
    var = rets[0] ** 2
    for r in rets[1:]:
        var = LAM * var + (1.0 - LAM) * r * r
    sps = math.sqrt(var)
    dt_avg = (ts[-1] - ts[0]) / max(len(rets), 1)
    if dt_avg <= 0:
        return None
    sigma = sps * math.sqrt(YEAR / dt_avg)
    # Effective memory of a lambda=0.94 EWMA: the weights sum to ~1-1/e after
    # about 1/(1-lambda) samples. Those are the returns the variance is actually
    # made of; dt_avg above is the mean spacing over the WHOLE window.
    n_eff = int(round(1.0 / (1.0 - LAM)))
    recent = rts[-n_eff:] if len(rts) >= n_eff else rts
    dt_recent = statistics.fmean(recent) if recent else dt_avg
    zero_frac = sum(1 for r in rets if r == 0.0) / len(rets)
    return sigma, sps, dt_avg, dt_recent, len(rets), zero_frac


def control_sigma(series, t_ms, win_ms):
    """
    Spacing-free control: close-to-close on a UNIFORM CTRL_STEP grid over the
    same window. Same data, same window, no dependence on tick arrival times.
    """
    step_ms = int(CTRL_STEP * 1000)
    n = int(win_ms / step_ms)
    px = [series.at(t_ms - win_ms + k * step_ms, step_ms * 3) for k in range(n + 1)]
    px = [p for p in px if p is not None and p > 0]
    if len(px) < int(0.8 * n):
        return None
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    if len(rets) < 10:
        return None
    sd = statistics.pstdev(rets)
    if sd <= 0:
        return None
    return sd * math.sqrt(YEAR / CTRL_STEP)


# -- PART A -------------------------------------------------------------------

def part_a(paths):
    print("=" * 78)
    print("PART A -- WHY SECTION 2 OF measure_basis_and_vol.py FOUND ZERO SAMPLES")
    print("=" * 78)
    print("  measure_vol() drops a grid point at one of four stages. Counted here.")
    print("  NOTE: that script's _excluded() ignores excluded_span.feed, so a gap on")
    print("  ANY feed (kraken, gemini, the kalshi book) drops the window for EVERY")
    print("  feed -- and it tests a 900s window, so one 1s gap kills 15 grid points.")
    print()
    for p in paths:
        if not p.exists():
            print(f"  {p}: NOT FOUND")
            continue
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            print(f"  --- {p.name} ---")
            feeds = conn.execute(
                "SELECT feed, COUNT(*), MIN(local_ts_ms), MAX(local_ts_ms) "
                "FROM underlying_tick GROUP BY feed ORDER BY feed").fetchall()
            for f, n, lo, hi in feeds:
                span_h = (hi - lo) / 3_600_000.0
                print(f"    feed {f:<18} n={n:<9d} {span_h:6.2f}h  {n / max(span_h * 3600, 1):7.2f}/s")
            try:
                spans = [(int(a), int(b), str(c), str(d)) for a, b, c, d in conn.execute(
                    "SELECT start_ts_ms, end_ts_ms, reason, feed FROM excluded_span")]
            except sqlite3.Error as exc:
                print(f"    excluded_span unreadable: {exc}")
                spans = []
            t0 = min((lo for _, _, lo, _ in feeds), default=0)
            t1 = max((hi for _, _, _, hi in feeds), default=0)
            total_ms = max(t1 - t0, 1)
            by_feed = {}
            for s, e, _r, fd in spans:
                by_feed.setdefault(fd, []).append((s, e))
            print(f"    excluded_span rows: {len(spans)}")
            for fd, sp in sorted(by_feed.items()):
                cov = sum(e - s for s, e in sp)
                print(f"      {fd:<20} {len(sp):>5d} span(s)  raw coverage {100.0 * cov / total_ms:6.2f}%")
            merged = []
            for s, e in sorted((s, e) for s, e, _r, _f in spans):
                if merged and s <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            union = sum(e - s for s, e in merged)
            dil = []
            for s, e in sorted((s - int(WINDOW * 1000), e) for s, e, _r, _f in spans):
                if dil and s <= dil[-1][1]:
                    dil[-1][1] = max(dil[-1][1], e)
                else:
                    dil.append([s, e])
            dilated = sum(e - s for s, e in dil)
            print(f"    UNION of all spans, any feed:            {100.0 * union / total_ms:6.2f}% of the recording")
            print(f"    UNION dilated by the 900s window test:   {100.0 * dilated / total_ms:6.2f}% "
                  f"<-- fraction of grid points measure_vol() skips")

            cb = load_feed(conn, "coinbase_trade")
            if not cb.ts:
                print("    coinbase_trade: NO ROWS -- the feed name really is wrong here")
                continue
            n_total = n_excl = n_thin = n_bad = n_ok = 0
            step, win = int(GRID * 1000), int(WINDOW * 1000)
            for t in range(cb.ts[0] + win, cb.ts[-1] + 1, step):
                n_total += 1
                if any(not (t < s or (t - win) > e) for s, e, _r, _f in spans):
                    n_excl += 1
                    continue
                w = cb.window(t - win, t)
                if len(w) < 30:
                    n_thin += 1
                    continue
                s = ewma_vol(w, window_seconds=WINDOW)
                if s is None or not math.isfinite(s) or s <= 0:
                    n_bad += 1
                    continue
                n_ok += 1
            print(f"    grid points on coinbase_trade: {n_total}")
            print(f"      dropped by _excluded()  : {n_excl:>6d} ({100.0 * n_excl / max(n_total, 1):5.1f}%)")
            print(f"      dropped by <30 samples  : {n_thin:>6d} ({100.0 * n_thin / max(n_total, 1):5.1f}%)")
            print(f"      dropped by bad sigma    : {n_bad:>6d} ({100.0 * n_bad / max(n_total, 1):5.1f}%)")
            print(f"      SURVIVED                : {n_ok:>6d} ({100.0 * n_ok / max(n_total, 1):5.1f}%)")
        finally:
            conn.close()
        print()


# -- PART B -------------------------------------------------------------------

def part_b(paths):
    print("=" * 78)
    print("PART B -- RAW ewma_vol BEFORE ANY CLAMP, AND WHERE ITS SPREAD COMES FROM")
    print("=" * 78)
    print("  Every grid point is measured with excluded_span IGNORED (part A shows what")
    print("  that filter was costing). The clamps are never applied here: this is the")
    print("  input to max(EWMA, floor) and min(., cap), not the output.")
    print()
    print("  CAVEAT, stated once: the recordings hold the WS trade stream only. The LIVE")
    print("  deque in kbot.feeds.btc also receives a REST poll of a DIFFERENT endpoint")
    print("  (api.coinbase.com/v2/prices/BTC-USD/spot) every 2s. The synthetic MIXED row")
    print("  below reproduces that mixture; the pure row cannot.")
    print()

    agg = {k: [] for k in ("sigma", "sigma_local", "ctrl", "ratio", "dt_avg",
                           "dt_recent", "zero", "nret", "eff_span", "sigma_mixed")}
    pairs, pairs_local, pairs_mixed = [], [], []

    for p in paths:
        if not p.exists():
            continue
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            cb = load_feed(conn, "coinbase_trade")
            mid = load_feed(conn, "coinbase")
            if not cb.ts:
                continue
            gaps = [(cb.ts[i] - cb.ts[i - 1]) / 1000.0 for i in range(1, len(cb.ts))]
            print(f"  {p.name}: coinbase_trade inter-arrival seconds")
            desc("    dt between prints", gaps, 26)
            dup = sum(1 for i in range(1, len(cb.px)) if cb.px[i] == cb.px[i - 1])
            print(f"    consecutive identical prints: {dup}/{len(cb.px) - 1} = "
                  f"{100.0 * dup / max(len(cb.px) - 1, 1):.1f}%  (each is a log-return of exactly 0)")

            step, win = int(GRID * 1000), int(WINDOW * 1000)
            for t in range(cb.ts[0] + win, cb.ts[-1] + 1, step):
                w = cb.window(t - win, t)
                if len(w) < 30:
                    continue
                parts = ewma_parts(w)
                if parts is None:
                    continue
                sigma, sps, dt_avg, dt_recent, nret, zero = parts
                live = ewma_vol(w, window_seconds=WINDOW)
                if live is None or abs(live - sigma) > 1e-9 * max(1.0, live):
                    print(f"    !! ewma_parts disagrees with kbot.vol.ewma_vol at t={t}")
                    continue
                if not math.isfinite(sigma) or sigma <= 0:
                    continue
                sigma_local = sps * math.sqrt(YEAR / dt_recent)
                ctrl = control_sigma(cb, t, win)

                agg["sigma"].append(sigma)
                agg["sigma_local"].append(sigma_local)
                agg["ratio"].append(dt_recent / dt_avg)
                agg["dt_avg"].append(dt_avg)
                agg["dt_recent"].append(dt_recent)
                agg["zero"].append(zero)
                agg["nret"].append(float(nret))
                agg["eff_span"].append(dt_recent / (1.0 - LAM))
                if ctrl is not None:
                    agg["ctrl"].append(ctrl)
                    pairs.append((sigma, ctrl))
                    pairs_local.append((sigma_local, ctrl))

                if mid.ts:
                    inj = []
                    for k in range(0, int(WINDOW / 2.0)):
                        tm = t - win + k * 2000
                        v = mid.at(tm, 4000)
                        if v is not None:
                            inj.append((tm / 1000.0, v))
                    if inj:
                        blended = sorted(w + inj, key=lambda x: x[0])
                        sm = ewma_vol(blended, window_seconds=WINDOW)
                        if sm is not None and math.isfinite(sm) and sm > 0:
                            agg["sigma_mixed"].append(sm)
                            if ctrl is not None:
                                pairs_mixed.append((sm, ctrl))
        finally:
            conn.close()
        print()

    sig = agg["sigma"]
    if not sig:
        print("  no grid points survived -- see part A")
        return

    print("  (3) RAW ewma_vol DISTRIBUTION, ANNUALISED, BEFORE CLAMPING")
    desc("live estimator (pure trades)", sig)
    desc("live estimator (MIXED, synth)", agg["sigma_mixed"])
    desc("same var, local annualisation", agg["sigma_local"])
    desc("uniform-grid control", agg["ctrl"])
    print()
    for label, v in (("pure", sig), ("mixed", agg["sigma_mixed"]), ("control", agg["ctrl"])):
        if not v:
            continue
        b010 = sum(1 for s in v if s < 0.10)
        b035 = sum(1 for s in v if s < 0.35)
        a150 = sum(1 for s in v if s > 1.50)
        n = len(v)
        lg = [math.log(s) for s in v if s > 0]
        print(f"    {label:<8} below 0.10 (vol_floor_15m): {100.0 * b010 / n:5.1f}%   "
              f"below 0.35 (vol_floor): {100.0 * b035 / n:5.1f}%   "
              f"above 1.50 (sanity cap): {100.0 * a150 / n:5.1f}%   "
              f"sd(log sigma)={statistics.pstdev(lg):.3f}")
    print()
    print("    sd(log sigma) is the dispersion number that decides (a) vs (b). Realized")
    print("    15-minute BTC vol is a roughly lognormal object with sd(log) of order")
    print("    0.35-0.45. A materially larger value on the live estimator than on the")
    print("    uniform-grid control over THE SAME WINDOWS is estimator noise, not market.")
    print()

    print("  (3b) TICK SPACING, AND THE HORIZON THE ESTIMATOR ACTUALLY USES")
    desc("dt_avg over the 900s window", agg["dt_avg"])
    desc("dt of the last ~16 returns", agg["dt_recent"])
    desc("ratio dt_recent / dt_avg", agg["ratio"])
    desc("EWMA effective span (s)", agg["eff_span"])
    desc("returns per 900s window", agg["nret"])
    desc("fraction of zero returns", agg["zero"])
    print()
    print("    sigma_hat = sigma_true * sqrt(dt_recent/dt_avg): the variance is built from")
    print("    the last ~1/(1-0.94) = 17 returns but annualised with the mean spacing of")
    print("    ALL of them. The 'EWMA effective span' row is how many SECONDS of data the")
    print("    lambda=0.94 decay actually retains -- compare it with the nominal 900.")
    med_dt = max(pct(agg["dt_avg"], 50), 1e-9)
    print(f"    annualisation factor at the median spacing: "
          f"sqrt(YEAR/{med_dt:.3f}) = {math.sqrt(YEAR / med_dt):,.0f}x")
    print()

    print("  (3c) DOES THE ESTIMATOR TRACK REALIZED VOL AT ALL?")
    print("    Same windows, same data, paired. A spacing artifact shows up as a low or")
    print("    NEGATIVE rank correlation against the spacing-free control.")
    for name, pr in (("live (pure trades)", pairs),
                     ("live (MIXED, synth)", pairs_mixed),
                     ("local annualisation", pairs_local)):
        if len(pr) < 30:
            print(f"    {name:<22} (too few pairs)")
            continue
        x = [a for a, _ in pr]
        y = [b for _, b in pr]
        rl = spearman([math.log(a) for a in x], [math.log(b) for b in y])
        print(f"    {name:<22} n={len(pr):<6d} spearman(sigma, control) = {rl:+.3f}   "
              f"median ratio sigma/control = {pct([a / b for a, b in pr if b > 0], 50):.3f}")
    print()


# -- PART C -------------------------------------------------------------------

def _yes_of(r):
    """The YES-side raw fair_p, from a row whose raw_fair_p is the TRADED side's."""
    return r["raw"] if r["side"] == "yes" else 1.0 - r["raw"]


def _mono_violations(pairs):
    """
    Sort by the replayed raw probability; count how often the LOGGED calibrated
    value then goes DOWN.

    This is the decisive strike-vs-calibrator test and it needs no calibrator at
    all. apply_calibrator is monotone non-decreasing, so if the replayed raw p is
    the same number the live bot computed, the logged calibrated values must come
    out already sorted -- zero violations -- no matter WHICH calibrator produced
    them. Violations mean the reconstructed inputs (strike / spot / sigma / T) are
    wrong. Zero violations alongside a non-zero error means the inputs are right
    and only the MAP differs.
    """
    if len(pairs) < 3:
        return None, 0
    # Tolerance: some logged fair_p values come from the `signal` event, which
    # rounds to 4dp. A drop smaller than that granularity is rounding, not a
    # real inversion, so only larger ones are counted.
    tol = 1e-4
    s = sorted(pairs)
    bad = sum(1 for i in range(1, len(s)) if s[i][1] < s[i - 1][1] - tol)
    return bad, len(s)


def _strike_sources(events, rec_paths, cache_path, tickers):
    """The three sources replay_pricing_fixes.py uses, kept separate."""
    out = {"chain": {}, "recordings": {}, "cache/api": {}}
    conn = sqlite3.connect(f"file:{events}?mode=ro", uri=True)
    try:
        settled = {}
        for tk, pl in conn.execute(
                "SELECT ticker, payload FROM events WHERE event_type='trade_outcome'"):
            parsed = _parse_ticker(tk or "")
            if parsed is None:
                continue
            try:
                settled[parsed[1]] = float(json.loads(pl)["expiration_value"])
            except (KeyError, TypeError, ValueError):
                continue
    finally:
        conn.close()
    for tk in tickers:
        parsed = _parse_ticker(tk)
        if parsed is None:
            continue
        prev = settled.get(parsed[1] - 900.0)
        if prev is not None and prev > 0:
            out["chain"][tk] = prev
    for p in rec_paths:
        if not p.exists():
            continue
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            for tk, fs in c.execute("SELECT ticker, floor_strike FROM kalshi_market "
                                    "WHERE floor_strike IS NOT NULL"):
                if tk in tickers and tk not in out["recordings"]:
                    out["recordings"][tk] = float(fs)
        except sqlite3.Error:
            pass
        finally:
            c.close()
    if cache_path.exists():
        try:
            for tk, fs in json.loads(cache_path.read_text()).items():
                if tk in tickers and fs is not None:
                    out["cache/api"][tk] = float(fs)
        except (ValueError, OSError):
            pass
    return out


def part_c(events, registry, rec_paths, cache_path):
    print("=" * 78)
    print("PART C -- WHY THE SECTION 3 RECONSTRUCTION MISSES")
    print("=" * 78)
    if not events.exists():
        print(f"  {events} not found")
        return
    conn = sqlite3.connect(f"file:{events}?mode=ro", uri=True)
    try:
        opens = conn.execute(
            "SELECT ts, ticker, payload FROM events WHERE event_type='position_open' "
            "AND ticker LIKE 'KXBTC15M%' ORDER BY ts").fetchall()
        rows, no_raw, no_fp = [], 0, 0
        for ts, tk, pl in opens:
            po = json.loads(pl)
            sg = conn.execute(
                "SELECT ts, payload FROM events WHERE event_type='signal' AND ticker=? "
                "AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                (tk, ts + 0.001, ts - 5.0)).fetchone()
            if sg is None:
                continue
            s = json.loads(sg[1])
            if s.get("side") != po.get("side") or \
                    abs(float(s.get("ask", -1)) - float(po["price"])) > 1e-6:
                continue
            raw = po.get("raw_fair_p")
            if raw is None:
                no_raw += 1
            if po.get("fair_p") is None:
                no_fp += 1
            rows.append({"ticker": tk, "open_ts": ts, "sig_ts": float(sg[0]),
                         "side": po["side"],
                         "raw": None if raw is None else float(raw),
                         "cal": float(po["fair_p"]) if po.get("fair_p") is not None
                                else float(s["fair_p"]),
                         "spot": float(s["btc_price"]), "vol": float(s["vol"]),
                         "ask": float(s["ask"])})
    finally:
        conn.close()
    print(f"  matched position_open+signal pairs: {len(rows)}   "
          f"({no_raw} without raw_fair_p, {no_fp} falling back to the signal's fair_p)")
    if not rows:
        return
    have_raw = [r for r in rows if r["raw"] is not None]
    desc("signal -> fill lag (s)", [r["open_ts"] - r["sig_ts"] for r in rows])
    print("    (this is the 'timestamp misalignment' candidate. The replay dates T from")
    print("     the FILL event while the model priced at the SIGNAL event; the gap above")
    print("     is the whole size of that error source.)")
    print()

    print("  (c0) PLATEAU STRUCTURE")
    print(f"    distinct logged CALIBRATED fair_p (4dp): "
          f"{len({round(r['cal'], 4) for r in rows})} of {len(rows)}")
    if have_raw:
        print(f"    distinct logged RAW fair_p (4dp)       : "
              f"{len({round(r['raw'], 4) for r in have_raw})} of {len(have_raw)}")
        ys = [_yes_of(r) for r in have_raw]
        outside = sum(1 for y in ys if not (_cfg.near_atm_fair_p_min - 1e-9 <= y
                                            <= _cfg.near_atm_fair_p_max + 1e-9))
        print(f"    raw YES-side fair_p range: {min(ys):.4f} .. {max(ys):.4f}   "
              f"outside the near_atm band: {outside}")
        print("      (evaluate() applies near_atm_only to the YES-side RAW fair_p BEFORE")
        print("       either side is priced, so the band edges ARE the observed min/max.")
        print("       That is where 0.2500/0.7500 comes from -- a gate, not a model range.)")
    print("    An isotonic map is a step function, so its flat segments ABSORB small input")
    print("    errors. A reconstruction error whose MEDIAN is non-zero therefore cannot be")
    print("    a small error in S/K/sigma/T -- those would show as zero on most fills and")
    print("    a whole plateau step on a few.")
    print()

    print("  (c1) CALIBRATOR PROVENANCE")
    cal = None
    if registry.exists():
        rc = sqlite3.connect(f"file:{registry}?mode=ro", uri=True)
        try:
            vs = rc.execute("SELECT tag, created_at, is_champion FROM model_versions "
                            "ORDER BY created_at").fetchall()
            lo_f = min(r["sig_ts"] for r in rows)
            hi_f = max(r["sig_ts"] for r in rows)
            print(f"    model_versions rows: {len(vs)}    fills span {lo_f:.0f} .. {hi_f:.0f}")
            during = sum(1 for _t, c, _ch in vs if c and lo_f < float(c) < hi_f)
            after = sum(1 for _t, c, _ch in vs if c and float(c) >= hi_f)
            print(f"    versions created DURING the fill window: {during}   after it: {after}")
            print("      Any non-zero count here means load_champion_calibrator() returns a")
            print("      map that was NOT live for some of these fills, and section 3's")
            print("      metric charges that difference to the pricing model.")
            for tag, c, ch in vs[-10:]:
                print(f"      {'CHAMPION ' if ch else '         '}{tag:<30} created_at={c:.0f}")
            row = rc.execute("SELECT calibrator FROM model_versions WHERE is_champion=1 "
                             "ORDER BY created_at DESC LIMIT 1").fetchone()
            if row and row[0] is not None:
                cal = pickle.loads(row[0])
        finally:
            rc.close()
    if cal is not None and have_raw:
        outs = [apply_calibrator(cal, r["raw"]) for r in have_raw]
        print(f"    distinct outputs of TODAY's champion on the logged raw inputs (4dp): "
              f"{len({round(o, 4) for o in outs})}")
        desc("  |champion(logged raw) - logged cal|",
             [abs(o - r["cal"]) for o, r in zip(outs, have_raw)], 38)
        print("      CALIBRATOR-ONLY ERROR. It recomputes nothing: it pushes the LOGGED raw")
        print("      value through TODAY's map and compares with the LOGGED calibrated one.")
        print("      Whatever this row shows is inside section 3's metric verbatim, with no")
        print("      contribution from strikes, spot, sigma or time.")
    print()

    print("  (c2) RECONSTRUCTION ERROR, SPLIT BY STRIKE SOURCE AND BY CONVENTION")
    print("    Section 3 replays with avg_seconds=0 (its BEFORE variant). If the live build")
    print("    that produced these fills already had the settlement fix, that is a")
    print("    convention mismatch and not a data problem -- so both are swept.")
    srcs = _strike_sources(events, rec_paths, cache_path, {r["ticker"] for r in rows})
    for nm, d in srcs.items():
        print(f"    source {nm:<12} resolves {len(d)} ticker(s)")
    # replay_pricing_fixes.py precedence: chain, then recordings, then cache.
    chosen = {}
    for nm in ("chain", "recordings", "cache/api"):
        for tk, k in srcs[nm].items():
            chosen.setdefault(tk, (nm, k))
    print()
    for avg_s in (0.0, _cfg.settlement_avg_seconds):
        print(f"    --- avg_seconds = {avg_s} ---")
        buckets = {}
        for r in rows:
            hit = chosen.get(r["ticker"])
            if hit is None:
                continue
            nm, K = hit
            parsed = _parse_ticker(r["ticker"])
            if parsed is None:
                continue
            _k, expiry, direction = parsed
            t_eff = effective_t_years((expiry - r["sig_ts"]) / YEAR, avg_s)
            if t_eff <= 0 or r["vol"] <= 0 or K <= 0:
                continue
            d = math.log(r["spot"] / K) / (r["vol"] * math.sqrt(t_eff))
            p_yes = float(norm.cdf(d))
            if direction != "above":
                p_yes = 1.0 - p_yes
            ps = p_yes if r["side"] == "yes" else 1.0 - p_yes
            err = abs(apply_calibrator(cal, ps) - r["cal"])
            buckets.setdefault(nm, []).append((ps, r["cal"], err))
        allb = [v for b in buckets.values() for v in b]
        names = list(buckets) + (["ALL"] if len(buckets) > 1 else [])
        for nm in names:
            b = allb if nm == "ALL" else buckets[nm]
            if not b:
                continue
            e = [x[2] for x in b]
            bad, n = _mono_violations([(x[0], x[1]) for x in b])
            print(f"      {nm:<12} n={len(b):<5d} |err| med={pct(e, 50):.4f} "
                  f"p95={pct(e, 95):.4f} max={max(e):.4f}   "
                  f"monotonicity violations={bad}/{n}")
        print()
    print("    HOW TO READ c2. Zero monotonicity violations in a source means the replayed")
    print("    raw p is exactly the number the live bot computed for those fills, so that")
    print("    source's strikes are RIGHT and its |err| is entirely the calibrator map.")
    print("    Violations mean the strikes (or spot/sigma/T) are wrong for that source.")
    print()

    if not have_raw:
        print("  (c3) SKIPPED: no fill carries raw_fair_p, so the strike cannot be inverted")
        print("       exactly. An isotonic map is not invertible on its flat segments.")
        return
    print("  (c3) THE STRIKE THE LIVE BOT ACTUALLY USED, RECOVERED EXACTLY")
    print("    raw_fair_p is the PRE-calibration lognormal output, which IS invertible:")
    print("      p_yes = raw if side=yes else 1-raw")
    print("      K_live = S * exp(-Phi^-1(p_yes) * sigma * sqrt(T_eff))")
    for avg_s in (0.0, _cfg.settlement_avg_seconds):
        k_live = {}
        for r in have_raw:
            parsed = _parse_ticker(r["ticker"])
            if parsed is None:
                continue
            _k, expiry, direction = parsed
            t_eff = effective_t_years((expiry - r["sig_ts"]) / YEAR, avg_s)
            if t_eff <= 0 or r["vol"] <= 0:
                continue
            p_yes = _yes_of(r)
            if direction != "above":
                p_yes = 1.0 - p_yes
            p_yes = min(max(p_yes, 1e-9), 1 - 1e-9)
            k_live[r["ticker"]] = r["spot"] * math.exp(
                -float(norm.ppf(p_yes)) * r["vol"] * math.sqrt(t_eff))
        print(f"    avg_seconds={avg_s:<5.1f} recovered K for {len(k_live)} ticker(s)")
        for nm, src in srcs.items():
            common = [tk for tk in src if tk in k_live]
            if not common:
                print(f"      {nm:<12} 0 overlapping tickers")
                continue
            dd = [src[tk] - k_live[tk] for tk in common]
            ad = [abs(x) for x in dd]
            print(f"      {nm:<12} n={len(common):<5d} mean={statistics.fmean(dd):+9.2f} "
                  f"sd={statistics.pstdev(dd):8.2f} |d| med={pct(ad, 50):7.2f} "
                  f"p95={pct(ad, 95):8.2f} max={max(ad):8.2f}  USD")
    print("      A source agreeing to a few cents IS the strike the bot used. The chain")
    print("      assumption under test is floor_strike(t) == expiration_value(t-900).")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", action="append", type=Path, default=[])
    ap.add_argument("--events", type=Path, default=Path("kbot_events.db"))
    ap.add_argument("--registry", type=Path, default=Path("kbot_registry.db"))
    ap.add_argument("--strike-cache", type=Path, default=Path("data/floor_strikes.json"))
    a = ap.parse_args()
    print(f"cfg: vol_floor={_cfg.vol_floor} vol_floor_15m={_cfg.vol_floor_15m} "
          f"vol_sanity_cap={_cfg.vol_sanity_cap} vol_window_15m={_cfg.vol_window_seconds_15m} "
          f"vol_mult_15m={_cfg.vol_multiplier_15m} settlement_avg={_cfg.settlement_avg_seconds}")
    print()
    if a.rec:
        part_a(a.rec)
        part_b(a.rec)
    part_c(a.events, a.registry, a.rec, a.strike_cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
