"""
STAGE 2. Reproduce the 1 Hz comparison grid from the reconstructed timelines and
validate the reconstruction against the cells the probe actually persisted.

The validation uses only the persisted top-of-book VALUES of agreeing cells --
data, not labels. No resolution label is read, and the production resolver is
never called.

The production tick evaluates every market at one wall instant T_tick captured
inside a synchronous function, so T_tick is at or just after the nominal grid
timestamp. That offset is estimated here by which cut best reproduces the
persisted cells; it is a property of the reconstruction, not of the protocol.
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

import bisect
import pickle
import sqlite3
from collections import defaultdict

DB = _DB
PKL = _WORK + "timelines.pkl"
OUT = _WORK + "gridcells.pkl"


def load():
    with open(PKL, "rb") as fh:
        return pickle.load(fh)


def state_at(times, states, t):
    """Observable state at wall time t: the last change point with ts <= t."""
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return "NO_SNAPSHOT"
    return states[i]


def main() -> int:
    d = load()
    grid, tickers = d["grid"], d["tickers"]
    tl = d["timelines"]

    idx = {}
    for lab in ("A", "B"):
        idx[lab] = {}
        for tk, pts in tl[lab].items():
            idx[lab][tk] = ([p[0] for p in pts], [p[1] for p in pts])

    # ---- persisted cells (values only) --------------------------------------
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    persisted = {}
    for (g, tk, ab, aq, ak, akq, bb, bq, bk, bkq) in con.execute(
        "SELECT grid_ts, ticker, a_bid, a_bid_qty, a_ask, a_ask_qty, "
        "b_bid, b_bid_qty, b_ask, b_ask_qty FROM equivalence_comparison"
    ):
        persisted[(g, tk)] = ((ab, aq, ak, akq), (bb, bq, bk, bkq))
    con.close()
    print(f"persisted cells: {len(persisted):,}")

    # ---- offset scan --------------------------------------------------------
    print("\noffset scan (mismatches against persisted agreeing cells):")
    best = None
    for off in (0.0, 0.002, 0.005, 0.01, 0.02, 0.05, 0.10, 0.20):
        bad = 0
        for (g, tk), (pa, pb) in persisted.items():
            t = g + off
            sa = state_at(*idx["A"][tk], t)
            sb = state_at(*idx["B"][tk], t)
            if sa != pa or sb != pb:
                bad += 1
        print(f"  +{off*1000:6.1f} ms : {bad:,} mismatched of {len(persisted):,}")
        if best is None or bad < best[1]:
            best = (off, bad)
    off, bad = best
    print(f"\nbest offset {off*1000:.1f} ms, {bad:,} mismatches "
          f"({100.0*(len(persisted)-bad)/len(persisted):.6f}% reproduced)")

    # ---- full grid at the chosen cut ---------------------------------------
    cells = []            # (grid_ts, ticker, sa, sb)
    missing = []          # cells the probe never persisted
    kinds = defaultdict(int)
    for g in grid:
        t = g + off
        for tk in tickers:
            sa = state_at(*idx["A"][tk], t)
            sb = state_at(*idx["B"][tk], t)
            key = (g, tk)
            if key in persisted:
                continue
            missing.append((g, tk, sa, sb))
            a_ok = not isinstance(sa, str)
            b_ok = not isinstance(sb, str)
            if not (a_ok and b_ok):
                kinds["unmatched:" + ("A=" + sa if not a_ok else "") +
                      ("/B=" + sb if not b_ok else "")] += 1
            elif sa != sb:
                kinds["disagreement"] += 1
            else:
                kinds["reconstructed_agree(!)"] += 1

    print(f"\ncells the probe never persisted: {len(missing):,}")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k:55s} {v:,}")

    with open(OUT, "wb") as fh:
        pickle.dump({"offset": off, "missing": missing, "grid": grid,
                     "reproduced": len(persisted) - bad,
                     "persisted": len(persisted), "mismatch": bad}, fh, protocol=4)
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
