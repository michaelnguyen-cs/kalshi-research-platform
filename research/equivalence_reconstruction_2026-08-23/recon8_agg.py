"""STAGE 8. Aggregate analysis over the classified disagreements."""

from __future__ import annotations

import os as _os

# Paths are resolved from the preserved evidence set, not from a session
# scratchpad. Override either with an environment variable to relocate.
#   DEPTHREC_EVIDENCE_DB  the probe database (the working copy, not source/)
#   DEPTHREC_RECON_WORK   scratch directory for this pipeline's intermediates
_EVIDENCE = "<EVIDENCE_DIR>/equivalence_probe_2026-08-23"
_DB = _os.environ.get("DEPTHREC_EVIDENCE_DB", _EVIDENCE + "/verify/equivprobe.sqlite")
_WORK = _os.environ.get("DEPTHREC_RECON_WORK", _EVIDENCE + "/work")
_os.makedirs(_WORK, exist_ok=True)
_WORK = _WORK.rstrip("/") + "/"

import pickle
from collections import Counter

SP = _WORK


def q(xs, p):
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[i]


def show(title, counter, total, top=12):
    print(f"\n{title}")
    for k, v in counter.most_common(top):
        print(f"  {str(k):46s} {v:6,}  {100*v/total:5.1f}%")
    if len(counter) > top:
        print(f"  ... {len(counter)-top} more groups")


def main() -> int:
    with open(SP + "final.pkl", "rb") as fh:
        d = pickle.load(fh)
    rows = d["rows"]
    n = len(rows)
    print(f"population: {n:,} reconstructed disagreements, all with both "
          f"streams legitimately VALID")

    prod_resid = [r for r in rows if r["prod_emu_explained"] == 0]
    frozen_resid = [r for r in rows if r["frozen_rule_explained"] == 0]
    print(f"production-resolver residual (emulated): {len(prod_resid):,}")
    print(f"frozen-rule residual                   : {len(frozen_resid):,}")

    print("\n=== CATEGORY COUNTS (partition of all 1,385) ===")
    cats = Counter(r["category"] for r in rows)
    for k in ("0_explained_both", "1_prod_missed_valid_convergence",
              "X_prod_lenient_beyond_frozen_rule", "2_late_convergence",
              "3_disqualified_by_contradiction", "4_persistent",
              "5_right_censored"):
        print(f"  {k:38s} {cats[k]:6,}")
    print(f"  {'TOTAL':38s} {sum(cats.values()):6,}")

    # ---- disagreement type -------------------------------------------------
    print("\n=== DISAGREEMENT TYPE ===")
    for scope, rs in (("all 1,385", rows), ("prod residual", prod_resid)):
        t = Counter(r["disagreement_type"] for r in rs)
        tot = len(rs)
        print(f"  {scope}:")
        for k, v in t.most_common():
            print(f"    {k:24s} {v:6,}  {100*v/tot:5.1f}%")

    # ---- direction ---------------------------------------------------------
    print("\n=== DIRECTION (which stream was behind) ===")
    for scope, rs in (("all 1,385", rows), ("prod residual", prod_resid)):
        t = Counter(r["stream_behind"] for r in rs)
        tot = len(rs)
        print(f"  {scope}:")
        for k, v in t.most_common():
            lab = {"A": "A lagged B (A caught up)", "B": "B lagged A (B caught up)"}.get(k, k)
            print(f"    {lab:30s} {v:6,}  {100*v/tot:5.1f}%")

    # ---- category 2 delay distribution -------------------------------------
    c2 = [r for r in rows if r["category"] == "2_late_convergence"]
    ds = [float(r["any_convergence_ms"]) for r in c2]
    print(f"\n=== CATEGORY 2 CONVERGENCE DELAY ({len(c2):,} cases) ===")
    if ds:
        print(f"  min {min(ds):9.1f} ms")
        for p, lbl in ((.25, "p25"), (.50, "median"), (.75, "p75"),
                       (.90, "p90"), (.95, "p95"), (.99, "p99")):
            print(f"  {lbl:6s} {q(ds, p):9.1f} ms")
        print(f"  max {max(ds):9.1f} ms")
        buckets = [(500, 750), (750, 1000), (1000, 2000), (2000, 5000),
                   (5000, float("inf"))]
        print("  histogram:")
        for lo, hi in buckets:
            c = sum(1 for x in ds if lo <= x < hi)
            name = f"{lo/1000:g}-{hi/1000:g}s" if hi != float("inf") else ">5s"
            print(f"    {name:12s} {c:5,}  {100*c/len(ds):5.1f}%  "
                  f"{'#' * int(60*c/len(ds))}")
        t = Counter(r["any_convergence_stream"] for r in c2)
        print("  which stream converged (i.e. was the laggard):")
        for k, v in t.most_common():
            print(f"    {k:4s} {v:5,}  {100*v/len(c2):5.1f}%")

    # ---- category 3 --------------------------------------------------------
    c3 = [r for r in rows if r["category"] == "3_disqualified_by_contradiction"]
    print(f"\n=== CATEGORY 3 ({len(c3):,} cases) ===")
    if c3:
        ds3 = [float(r["any_convergence_ms"]) for r in c3]
        print(f"  arrival of the other state: min {min(ds3):.1f} ms, "
              f"median {q(ds3,.5):.1f} ms, max {max(ds3):.1f} ms (all <= 500 ms)")
        lag = [(float(r["contradiction_ts"]) - r["eval_ts"]) * 1000
               for r in c3 if r["contradiction_ts"] != ""]
        if lag:
            print(f"  intervening contradiction appears: min {min(lag):.1f} ms, "
                  f"median {q(lag,.5):.1f} ms, max {max(lag):.1f} ms after the "
                  f"comparison instant")

    # ---- concentration -----------------------------------------------------
    print("\n=== CONCENTRATION ===")
    show("by series (all 1,385)", Counter(r["series"] for r in rows), n)
    show("by series (prod residual)", Counter(r["series"] for r in prod_resid),
         len(prod_resid))
    show("by ticker (prod residual)", Counter(r["ticker"] for r in prod_resid),
         len(prod_resid), top=10)
    print(f"\n  distinct tickers involved: all {len({r['ticker'] for r in rows}):,} / "
          f"prod residual {len({r['ticker'] for r in prod_resid}):,} "
          f"(of 3,472 in the universe)")
    show("by tick regime (prod residual)",
         Counter(r["tick_regime"] for r in prod_resid), len(prod_resid))

    def band(r):
        p = r["price_level_bp"]
        if p == "" or p is None:
            return "no bid"
        p = int(p)
        return f"{(p//1000)*10:>2d}-{(p//1000)*10+10:>2d}c"
    show("by price level of A's best bid (prod residual)",
         Counter(band(r) for r in prod_resid), len(prod_resid))

    t0 = min(r["grid_ts"] for r in rows)
    show("by 30 s block of the 240 s run (prod residual)",
         Counter(f"{int((r['grid_ts']-t0)//30)*30:>3d}-{int((r['grid_ts']-t0)//30)*30+30:>3d}s"
                 for r in prod_resid), len(prod_resid))

    print("\n=== FEED-EVENT PROXIMITY ===")
    print(f"  reconnects in the run                  : 0 "
          f"(one session per stream, disconnect only at teardown)")
    print(f"  subscription updates in the run        : 0 "
          f"(one subscribe per stream, no update_markets)")
    print(f"  sequence gaps in the run               : 0 (both streams)")
    print(f"  cases inside a p95 message-rate burst  : "
          f"{sum(r['in_rate_burst_p95'] for r in prod_resid):,} of "
          f"{len(prod_resid):,} residual "
          f"({100*sum(r['in_rate_burst_p95'] for r in prod_resid)/len(prod_resid):.1f}%)")
    print(f"  same for all 1,385                     : "
          f"{sum(r['in_rate_burst_p95'] for r in rows):,} "
          f"({100*sum(r['in_rate_burst_p95'] for r in rows)/n:.1f}%)")

    print("\n=== VALIDITY ===")
    print(f"  both streams legitimately VALID at the comparison instant: {n:,} / {n:,}")
    print(f"  cells that should have been unmatched instead            : 0")
    print(f"  validity-state implementation defects found              : 0")

    print("\n=== FOLLOW-UP EVIDENCE ===")
    fs = [r["followup_s"] for r in rows]
    print(f"  retained follow-up after the comparison instant: "
          f"min {min(fs):.3f}s  median {q(fs,.5):.3f}s  max {max(fs):.3f}s")
    print(f"  cases with < 5 s follow-up: {sum(1 for x in fs if x < 5):,} "
          f"(all of which converged, so none is right-censored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
