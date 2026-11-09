"""
STAGE 9. Root-cause probing for the residual: is B's lag a client-side backlog,
and does it line up with the temporal clustering of the disagreements?
"""

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
import sqlite3
from collections import Counter, defaultdict

DB = _DB
SP = _WORK


def main() -> int:
    with open(SP + "final.pkl", "rb") as fh:
        rows = pickle.load(fh)["rows"]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    fps = defaultdict(Counter)     # stream -> second -> frames
    for lab, ts in con.execute(
            "SELECT stream_label, recv_ts FROM ws_raw WHERE direction='in'"):
        fps[lab][int(ts)] += 1
    con.close()

    t0 = int(min(r["grid_ts"] for r in rows))
    resid = [r for r in rows if r["prod_emu_explained"] == 0]
    per_sec_all = Counter(int(r["grid_ts"]) for r in rows)
    per_sec_res = Counter(int(r["grid_ts"]) for r in resid)

    print("seconds with the most residual disagreements")
    print(f"{'t(s)':>5} {'resid':>6} {'all':>5} {'A frames':>9} {'B frames':>9} "
          f"{'B-A':>6}")
    for sec, c in per_sec_res.most_common(15):
        print(f"{sec-t0:5d} {c:6d} {per_sec_all[sec]:5d} "
              f"{fps['A'][sec]:9,} {fps['B'][sec]:9,} "
              f"{fps['B'][sec]-fps['A'][sec]:6d}")

    secs = sorted(set(fps["A"]) | set(fps["B"]))
    a_r = [fps["A"][s] for s in secs]
    b_r = [fps["B"][s] for s in secs]
    print(f"\nframes/s  A: mean {sum(a_r)/len(a_r):.0f}  max {max(a_r):,}")
    print(f"frames/s  B: mean {sum(b_r)/len(b_r):.0f}  max {max(b_r):,}")

    busiest = sorted(secs, key=lambda s: -(fps["A"][s] + fps["B"][s]))[:10]
    print("\nbusiest seconds of the run")
    print(f"{'t(s)':>5} {'A':>7} {'B':>7} {'total':>8} {'disagreements':>14} "
          f"{'residual':>9}")
    for s in busiest:
        print(f"{s-t0:5d} {fps['A'][s]:7,} {fps['B'][s]:7,} "
              f"{fps['A'][s]+fps['B'][s]:8,} {per_sec_all[s]:14d} "
              f"{per_sec_res[s]:9d}")

    # correlation of residual count with total frame rate
    xs = [fps["A"][s] + fps["B"][s] for s in secs]
    ys = [per_sec_res[s] for s in secs]
    n = len(xs)
    mx, my = sum(xs)/n, sum(ys)/n
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    den = (sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys)) ** 0.5
    print(f"\nPearson r(frames/s, residual disagreements/s) = {num/den:.3f} "
          f"over {n} seconds")

    # per-market top-of-book churn for the two heaviest residual tickers
    print("\nresidual concentration vs market churn")
    with open(SP + "timelines.pkl", "rb") as fh:
        tl = pickle.load(fh)["timelines"]
    top = Counter(r["ticker"] for r in resid).most_common(5)
    for tk, c in top:
        print(f"  {tk:46s} residual {c:3d}  "
              f"top changes A {len(tl['A'][tk]):5,} B {len(tl['B'][tk]):5,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
