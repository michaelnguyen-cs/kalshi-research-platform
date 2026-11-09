#!/usr/bin/env python
"""
READ-ONLY measurement of the two things the 2026-08 pricing fixes need from the
rec_*.sqlite recordings, and NOTHING else:

  Section 2  15-MINUTE REALIZED VOL.  The empirical distribution of the exact
             sigma the live 15-minute path produces -- kbot.vol.ewma_vol with
             window_seconds=900 -- so cfg.vol_floor_15m can be placed BELOW
             that distribution's low tail instead of inside its body.

  Section 3  COINBASE-vs-COMPOSITE BASIS.  The bot's S is a Coinbase print;
             the contract's K is a BRTI-derived floor_strike. This measures the
             gap, its stability, and -- the only question that matters -- its
             size RELATIVE TO sigma*sqrt(T_eff), which is what decides whether
             a basis correction is worth any complexity at all.

This script makes NO network calls, opens NO Kalshi endpoint, and writes only
the optional --out-vol-csv that scripts/replay_pricing_fixes.py consumes.
It does not tune anything and has no notion of P&L.

    python scripts/measure_basis_and_vol.py \\
        --rec data/rec_A_usmorning.sqlite \\
        --rec data/rec_B_overnight.sqlite \\
        --rec data/rec_C_highvol.sqlite \\
        --out-vol-csv data/measured_15m_vol.csv

WHAT THE COMPOSITE IS, AND IS NOT
---------------------------------
scripts/record_feeds.py records `composite` as the median QUOTE MID across
coinbase / bitstamp / kraken / gemini. That is a PROXY for CF Benchmarks BRTI,
not BRTI: the real index is depth-weighted and time-smoothed, this proxy is
top-of-book and instantaneous. So the number Section 3 reports is the
Coinbase-vs-proxy basis. Read it as an ESTIMATE of the Coinbase-vs-index basis
whose own error is unknown but bounded by how far the proxy sits from BRTI --
and note the proxy leads the real index on impulse moves, so the measured
dispersion is if anything an OVER-estimate of the persistent component.

WHICH COINBASE SERIES
---------------------
kbot.feeds.btc subscribes to Coinbase's `ticker` channel, i.e. TRADE PRINTS,
and REST-polls the spot endpoint -- it never sees a Coinbase quote mid. So
`coinbase_trade` is the series the bot actually prices off and is reported
first. `coinbase` (the quote mid) is reported alongside because the difference
between them is bid-ask bounce, which is noise the bot inherits for free and
which a mid-based feed would not have.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kbot.vol import ewma_vol  # noqa: E402

_SECONDS_PER_YEAR = 365.25 * 24 * 3600

# The live 15-minute vol window (cfg.vol_window_seconds_15m). Hardcoded rather
# than read from cfg so this measurement cannot silently change meaning if the
# config does -- if you change the live window, change it here deliberately.
_WINDOW_SECONDS = 900.0

# How often to evaluate the trailing-window estimator. The live bot evaluates
# far more often than this (every book update and every throttled spot tick);
# 60s is a decimation that keeps the windows heavily overlapping -- so these
# samples are NOT independent, and the percentiles below describe "sigma as the
# bot sees it at a random moment", which is exactly the quantity a floor guards.
_GRID_SECONDS = 60.0

# The hourly floor, for reference only -- never used as a target.
_HOURLY_FLOOR = 0.35

# First-principles ceiling on any 15-minute floor. See cfg.vol_floor_15m: at
# 15 minutes, 0.10 annualised is a terminal sd of 0.053% of spot (~$32 at BTC
# 60k). BTC does not sit inside a $32 one-sigma band for 15 minutes unless the
# feed has stalled -- which is precisely what a floor is for.
_FIRST_PRINCIPLES_CEILING = 0.10


def _pct(values: list[float], q: float) -> float:
    """Linear-interpolated percentile; `values` need not be sorted."""
    if not values:
        return float("nan")
    v = sorted(values)
    if len(v) == 1:
        return v[0]
    pos = (len(v) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return v[int(pos)]
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _describe(name: str, values: list[float], unit: str = "") -> None:
    if not values:
        print(f"  {name:<28} (no samples)")
        return
    print(
        f"  {name:<28} n={len(values):<7d} "
        f"mean={statistics.fmean(values):+.5f} sd={statistics.pstdev(values):.5f} "
        f"min={min(values):+.5f} p5={_pct(values, 5):+.5f} p50={_pct(values, 50):+.5f} "
        f"p95={_pct(values, 95):+.5f} max={max(values):+.5f} {unit}"
    )


@dataclass
class Series:
    """A (timestamp_ms, price) series with a bisect-able timestamp list."""
    ts: list[int]
    px: list[float]

    def at(self, when_ms: int, max_staleness_ms: int = 5_000) -> float | None:
        """Last observation at or before `when_ms`, or None if too stale/absent."""
        i = bisect.bisect_right(self.ts, when_ms) - 1
        if i < 0:
            return None
        if when_ms - self.ts[i] > max_staleness_ms:
            return None
        return self.px[i]

    def window(self, lo_ms: int, hi_ms: int) -> list[tuple[float, float]]:
        """(seconds, price) samples in (lo_ms, hi_ms], for kbot.vol.ewma_vol."""
        a = bisect.bisect_right(self.ts, lo_ms)
        b = bisect.bisect_right(self.ts, hi_ms)
        return [(self.ts[i] / 1000.0, self.px[i]) for i in range(a, b)]


def _load_feed(conn: sqlite3.Connection, feed: str) -> Series:
    rows = conn.execute(
        "SELECT local_ts_ms, price FROM underlying_tick WHERE feed=? ORDER BY local_ts_ms",
        (feed,),
    ).fetchall()
    return Series([int(r[0]) for r in rows], [float(r[1]) for r in rows])


def _load_composite(conn: sqlite3.Connection, min_sources: int) -> Series:
    rows = conn.execute(
        "SELECT local_ts_ms, median_price FROM composite "
        "WHERE median_price IS NOT NULL AND n_sources >= ? ORDER BY local_ts_ms",
        (min_sources,),
    ).fetchall()
    return Series([int(r[0]) for r in rows], [float(r[1]) for r in rows])


def _load_excluded(conn: sqlite3.Connection) -> list[tuple[int, int, str]]:
    try:
        return [
            (int(a), int(b), str(c))
            for a, b, c in conn.execute(
                "SELECT start_ts_ms, end_ts_ms, reason FROM excluded_span"
            )
        ]
    except sqlite3.Error:
        return []


def _excluded(spans: list[tuple[int, int, str]], lo_ms: int, hi_ms: int) -> bool:
    """True if [lo_ms, hi_ms] overlaps any span analysis was told to drop."""
    return any(not (hi_ms < s or lo_ms > e) for s, e, _ in spans)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ── Section 1: inventory ─────────────────────────────────────────────────────

def inventory(path: Path, conn: sqlite3.Connection) -> None:
    print(f"\n  {path.name}")
    feeds = conn.execute(
        "SELECT feed, COUNT(*), MIN(local_ts_ms), MAX(local_ts_ms) "
        "FROM underlying_tick GROUP BY feed ORDER BY feed"
    ).fetchall()
    for feed, n, lo, hi in feeds:
        span_h = (hi - lo) / 3_600_000.0
        print(f"    {feed:<20} n={n:<9d} {_iso(lo)} -> {_iso(hi)}  ({span_h:.2f}h, {n / max(span_h * 3600, 1):.2f}/s)")
    comp = conn.execute(
        "SELECT COUNT(*), MIN(n_sources), MAX(n_sources), "
        "SUM(CASE WHEN n_sources >= 3 THEN 1 ELSE 0 END) FROM composite"
    ).fetchone()
    if comp and comp[0]:
        print(f"    composite            n={comp[0]:<9d} n_sources {comp[1]}..{comp[2]}, "
              f"{100.0 * comp[3] / comp[0]:.1f}% with >=3 sources")
    spans = _load_excluded(conn)
    if spans:
        print(f"    excluded_span        {len(spans)} span(s) -- dropped from every statistic below")
        for s, e, why in spans[:5]:
            print(f"        {_iso(s)} -> {_iso(e)}  {why}")
        if len(spans) > 5:
            print(f"        ... and {len(spans) - 5} more")


# ── Section 2: 15-minute realized vol ────────────────────────────────────────

def measure_vol(
    label: str,
    series: Series,
    spans: list[tuple[int, int, str]],
) -> list[tuple[int, float]]:
    """
    (timestamp_ms, sigma) for the live 15-minute estimator evaluated on a
    _GRID_SECONDS grid. Uses kbot.vol.ewma_vol -- the SAME function, the SAME
    900s window -- so what comes out is the distribution of the number the live
    fallback path actually produces, not a re-derivation of it.
    """
    if not series.ts:
        return []
    out: list[tuple[int, float]] = []
    step = int(_GRID_SECONDS * 1000)
    win = int(_WINDOW_SECONDS * 1000)
    start = series.ts[0] + win
    for t in range(start, series.ts[-1] + 1, step):
        if _excluded(spans, t - win, t):
            continue
        samples = series.window(t - win, t)
        if len(samples) < 30:          # a window this thin is a feed gap, not a quiet market
            continue
        sigma = ewma_vol(samples, window_seconds=_WINDOW_SECONDS)
        if sigma is None or not math.isfinite(sigma) or sigma <= 0:
            continue
        out.append((t, sigma))
    return out


def block_realized_vol(
    series: Series,
    spans: list[tuple[int, int, str]],
) -> list[float]:
    """
    Independent cross-check that does not go through EWMA at all: annualised
    close-to-close vol of DISJOINT 15-minute blocks sampled at 15s. If this and
    the EWMA distribution disagree wildly, the EWMA's warm-up/decay is doing
    something and the floor conclusion should not be trusted.
    """
    if not series.ts:
        return []
    out: list[float] = []
    win = int(_WINDOW_SECONDS * 1000)
    for t in range(series.ts[0] + win, series.ts[-1] + 1, win):
        if _excluded(spans, t - win, t):
            continue
        px = [series.at(t - win + k * 15_000, max_staleness_ms=15_000) for k in range(61)]
        px = [p for p in px if p is not None and p > 0]
        if len(px) < 40:
            continue
        rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
        if len(rets) < 2:
            continue
        sd = statistics.pstdev(rets)
        if sd <= 0:
            continue
        out.append(sd * math.sqrt(_SECONDS_PER_YEAR / 15.0))
    return out


# ── Section 3: basis ─────────────────────────────────────────────────────────

def measure_basis(
    coinbase: Series,
    composite: Series,
    spans: list[tuple[int, int, str]],
    max_staleness_ms: int,
) -> list[tuple[int, float, float]]:
    """
    (timestamp_ms, basis_dollars, basis_logs) sampled on the composite's own
    update times -- the composite is the slower object, so aligning to it never
    invents a Coinbase observation. Both feeds carry LOCAL receive timestamps
    (record_feeds.py's common clock); exchange timestamps are deliberately not
    used, because four exchange clocks with unknown skew cannot be aligned and
    the question is about what WE could see anyway.
    """
    out: list[tuple[int, float, float]] = []
    for t_ms, comp_px in zip(composite.ts, composite.px):
        if _excluded(spans, t_ms, t_ms):
            continue
        cb = coinbase.at(t_ms, max_staleness_ms=max_staleness_ms)
        if cb is None or cb <= 0 or comp_px <= 0:
            continue
        out.append((t_ms, cb - comp_px, math.log(cb / comp_px)))
    return out


def report_basis_drift(samples: list[tuple[int, float, float]]) -> None:
    """Is the basis a constant worth subtracting, or a wandering thing that is not?"""
    if len(samples) < 60:
        print("    (too few samples to characterise drift)")
        return

    # Hourly means: a stable basis has hourly means clustered near the overall
    # mean; a drifting one walks.
    buckets: dict[int, list[float]] = {}
    for t_ms, d, _ in samples:
        buckets.setdefault(t_ms // 3_600_000, []).append(d)
    hourly = [(h, statistics.fmean(v), len(v)) for h, v in sorted(buckets.items()) if len(v) >= 30]
    if hourly:
        means = [m for _, m, _ in hourly]
        print(f"    hourly means: n_hours={len(hourly)} "
              f"min={min(means):+.2f} max={max(means):+.2f} "
              f"sd_across_hours={statistics.pstdev(means):.3f}")
        print("      (sd ACROSS hourly means vs sd WITHIN: if across >> within/sqrt(n), "
              "the basis drifts and a single constant will not capture it)")
        for h, m, n in hourly[:12]:
            print(f"        {_iso(h * 3_600_000)}  mean={m:+8.2f}  n={n}")
        if len(hourly) > 12:
            print(f"        ... and {len(hourly) - 12} more hours")

    # OLS slope of basis on elapsed hours.
    t0 = samples[0][0]
    xs = [(t_ms - t0) / 3_600_000.0 for t_ms, _, _ in samples]
    ys = [d for _, d, _ in samples]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx > 0:
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        print(f"    OLS drift: {slope:+.4f} $/hour over {max(xs):.1f}h "
              f"(total implied move {slope * max(xs):+.2f})")

    # Lag-1 autocorrelation on a 60s decimation: a basis that is genuinely a
    # persistent offset is highly autocorrelated minute to minute; one that is
    # bid-ask bounce is not.
    dec = [d for i, (_, d, _) in enumerate(samples) if i % max(1, len(samples) // 2000) == 0]
    if len(dec) > 30:
        m = statistics.fmean(dec)
        num = sum((dec[i] - m) * (dec[i - 1] - m) for i in range(1, len(dec)))
        den = sum((x - m) ** 2 for x in dec)
        if den > 0:
            print(f"    lag-1 autocorrelation (decimated, n={len(dec)}): {num / den:+.3f}")
            print("      (near 1.0 = persistent offset, correctable; near 0.0 = "
                  "independent noise, which inflates variance rather than shifting the mean)")


def report_basis_significance(dollars: list[float], sigma_p50: float, spot: float) -> None:
    """
    The only question that decides whether to implement a basis correction:
    how big is it compared with sigma*sqrt(T_eff), the scale ln(S/K) is
    measured against?
    """
    if not dollars or not math.isfinite(sigma_p50) or sigma_p50 <= 0 or spot <= 0:
        return
    t_eff = (900.0 - (2.0 / 3.0) * 60.0) / _SECONDS_PER_YEAR      # 15m contract, 60s average
    scale_logs = sigma_p50 * math.sqrt(t_eff)
    scale_dollars = scale_logs * spot

    mean_d = statistics.fmean(dollars)
    sd_d = statistics.pstdev(dollars)
    # Near the money d ~ 0, so dp/d(ln S) = phi(0) / (sigma*sqrt(T_eff)).
    phi0 = 1.0 / math.sqrt(2.0 * math.pi)
    dp_per_dollar = phi0 / scale_logs / spot

    print()
    print(f"    reference scale at sigma=p50={sigma_p50:.4f}, T=15min, tau=60s:")
    print(f"      sigma*sqrt(T_eff) = {scale_logs:.6f} in logs = ${scale_dollars:.2f} at spot ${spot:,.0f}")
    print(f"    basis MEAN  ${mean_d:+.2f} = {abs(mean_d) / scale_dollars:.3f} sigma "
          f"-> shifts near-ATM fair_p by {dp_per_dollar * mean_d:+.4f}")
    print(f"    basis SD    ${sd_d:.2f} = {sd_d / scale_dollars:.3f} sigma "
          f"-> inflates effective variance by {(sd_d / scale_dollars) ** 2:.4f}x sigma^2")
    print()
    print("    HOW TO READ THIS. The MEAN is a shift: it moves fair_p one way on every")
    print("    contract and is correctable by subtracting a constant. The SD is not a")
    print("    shift -- independent noise s in S makes E[Phi((x+eps)/k)] = Phi(x/sqrt(k^2+s^2)),")
    print("    i.e. it COMPRESSES fair_p toward 0.5, the same direction as the vol floor.")
    print("    Correcting it means ADDING s^2 to the variance, which widens sigma; do that")
    print("    only if s is a material fraction of sigma*sqrt(T_eff).")
    print()
    print("    DECISION RULE (fixed before looking at the numbers, not after):")
    print("      implement the mean shift only if |mean| > 0.10 sigma (~1 cent of fair_p);")
    print("      implement the variance inflation only if sd > 0.30 sigma (>4.5% on sigma).")
    print("    Otherwise report it as negligible and add no code. Complexity in the pricing")
    print("    path has to earn its place against a measured number.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rec", action="append", type=Path, required=True,
                    help="a rec_*.sqlite recording; repeat for each")
    ap.add_argument("--coinbase-feed", default="coinbase_trade",
                    help="feed name for the Coinbase series the BOT prices off (default: coinbase_trade)")
    ap.add_argument("--min-composite-sources", type=int, default=3,
                    help="drop composite rows built from fewer than this many exchanges (default: 3)")
    ap.add_argument("--max-staleness-ms", type=int, default=5000,
                    help="max age of a carried-forward Coinbase print when aligning (default: 5000)")
    ap.add_argument("--out-vol-csv", type=Path, default=None,
                    help="write the measured 15-minute sigmas here for replay_pricing_fixes.py")
    args = ap.parse_args()

    missing = [p for p in args.rec if not p.exists()]
    if missing:
        print("ERROR: recording(s) not found: " + ", ".join(str(p) for p in missing))
        print("These live on the VM, not on a dev checkout.")
        return 1

    print("=" * 78)
    print("SECTION 1 -- RECORDING INVENTORY")
    print("=" * 78)

    all_vol_ewma: list[tuple[str, int, float]] = []
    all_vol_block: list[float] = []
    all_vol_composite: list[float] = []
    all_basis_trade: list[tuple[int, float, float]] = []
    all_basis_mid: list[tuple[int, float, float]] = []
    spot_level: list[float] = []

    for path in args.rec:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            inventory(path, conn)
            spans = _load_excluded(conn)
            cb_trade = _load_feed(conn, args.coinbase_feed)
            cb_mid = _load_feed(conn, "coinbase")
            comp = _load_composite(conn, args.min_composite_sources)

            if comp.px:
                spot_level.append(statistics.median(comp.px))

            for t, s in measure_vol(path.name, cb_trade, spans):
                all_vol_ewma.append((path.name, t, s))
            all_vol_block.extend(block_realized_vol(cb_trade, spans))
            all_vol_composite.extend(s for _, s in measure_vol(path.name, comp, spans))

            all_basis_trade.extend(measure_basis(cb_trade, comp, spans, args.max_staleness_ms))
            all_basis_mid.extend(measure_basis(cb_mid, comp, spans, args.max_staleness_ms))
        finally:
            conn.close()

    # ── Section 2 ───────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 2 -- MEASURED 15-MINUTE REALIZED VOL (annualised)")
    print("=" * 78)
    print(f"  estimator: kbot.vol.ewma_vol(window_seconds={_WINDOW_SECONDS:.0f}) -- the live")
    print(f"  15-minute fallback path, evaluated every {_GRID_SECONDS:.0f}s. Windows overlap")
    print("  heavily, so these samples are NOT independent; they describe sigma as the bot")
    print("  sees it at a random moment, which is the quantity a floor has to sit under.")
    print()

    sig = [s for _, _, s in all_vol_ewma]
    if not sig:
        print("  ERROR: no usable vol samples. Check --coinbase-feed matches a recorded feed.")
        return 1

    _describe(f"EWMA900 on {args.coinbase_feed}", sig)
    _describe("EWMA900 on composite", all_vol_composite)
    _describe("block close-to-close", all_vol_block)
    print()
    print("  percentiles of the live estimator:")
    for q in (1, 5, 10, 25, 50, 75, 90, 99):
        print(f"    p{q:<3d} {_pct(sig, q):.4f}")
    print()

    below = sum(1 for s in sig if s < _HOURLY_FLOOR)
    print(f"  fraction below the CURRENT hourly floor {_HOURLY_FLOOR}: "
          f"{below}/{len(sig)} = {100.0 * below / len(sig):.1f}%")
    print("    -> this is the fraction of the time max(EWMA, 0.35) would return the FLOOR")
    print("       rather than the estimate, i.e. the fraction of the time sigma is a")
    print("       constant and fair_p is therefore pinned near 0.5.")
    print()

    p5 = _pct(sig, 5)
    measured_bound = math.floor(p5 * 100.0) / 100.0
    recommended = min(_FIRST_PRINCIPLES_CEILING, measured_bound)
    print("  FLOOR DERIVATION (no outcome, no P&L, no tuning):")
    print(f"    (a) first-principles degeneracy ceiling      = {_FIRST_PRINCIPLES_CEILING:.2f}")
    print("        0.10 annualised is a 15-minute terminal sd of 0.053% of spot (~$32 at")
    print("        BTC 60k). BTC does not hold a $32 one-sigma band for 15 minutes unless")
    print("        the feed has stalled -- the only case a floor should ever catch. Same")
    print("        character as kbot.vol._SIGMA_LO = 0.05.")
    print(f"    (b) measured p5, rounded down to 0.01        = {measured_bound:.2f}   (raw p5 {p5:.4f})")
    print(f"    floor := min(a, b)                           = {recommended:.2f}")
    print()
    print(f"    cfg.vol_floor_15m is currently {_FIRST_PRINCIPLES_CEILING:.2f}. If the line above")
    print(f"    reads lower than that, set cfg.vol_floor_15m = {recommended:.2f} -- a floor that sits")
    print("    inside the body of the distribution it guards is not a guard, it IS the model.")
    print("    Do NOT raise it because a lower one trades more or scores better.")

    if args.out_vol_csv:
        args.out_vol_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out_vol_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["recording", "local_ts_ms", "sigma_ewma_900s"])
            for rec, t, s in all_vol_ewma:
                w.writerow([rec, t, f"{s:.6f}"])
        print()
        print(f"  wrote {len(all_vol_ewma)} sigma samples -> {args.out_vol_csv}")
        print("  (feed this to scripts/replay_pricing_fixes.py --vol-csv)")

    # ── Section 3 ───────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 3 -- COINBASE vs COMPOSITE BASIS")
    print("=" * 78)
    print("  composite = median quote-mid across 4 exchanges = a PROXY for BRTI, not BRTI.")
    print("  See this file's header for what that proxy does and does not license.")
    print()

    for label, samples in (
        (f"{args.coinbase_feed} - composite  (the series the bot prices off)", all_basis_trade),
        ("coinbase mid  - composite  (reference: removes bid-ask bounce)", all_basis_mid),
    ):
        print(f"  {label}")
        if not samples:
            print("    (no aligned samples)")
            print()
            continue
        dollars = [d for _, d, _ in samples]
        logs = [g for _, _, g in samples]
        _describe("basis, dollars", dollars, "USD")
        _describe("basis, log points", logs, "(x1e4 for bp)")
        print(f"    p1={_pct(dollars, 1):+.2f} p25={_pct(dollars, 25):+.2f} "
              f"p75={_pct(dollars, 75):+.2f} p99={_pct(dollars, 99):+.2f}")
        report_basis_drift(samples)
        print()

    if all_basis_trade and sig:
        print(f"  SIGNIFICANCE OF THE {args.coinbase_feed} BASIS")
        report_basis_significance(
            [d for _, d, _ in all_basis_trade],
            _pct(sig, 50),
            statistics.fmean(spot_level) if spot_level else 0.0,
        )

    print()
    print("=" * 78)
    print("Nothing here was fitted to an outcome. If a number above disagrees with the")
    print("value currently in config.py, the number wins.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
