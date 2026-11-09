"""
STAGE 3. Recover each tick's evaluation instant, then settle the grid.

The probe read every market at one wall instant per tick inside a synchronous
function, and never recorded that instant. A global +50 ms cut already
reproduces all 828,423 persisted cells, but 87 of the 1,385 unpersisted cells
come back as agreeing at that cut -- which cannot be what the probe saw, because
an agreeing cell would have been persisted.

So the instant is recovered per tick instead of assumed. For tick g the feasible
cut is any time at which

  (1) every persisted cell of that tick reproduces its stored A and B tops, AND
  (2) no unpersisted cell of that tick reconstructs as a two-sided agreement.

Between consecutive frame receive times the whole grid state is constant, so the
feasible set is a union of intervals bounded by event timestamps and can be
found exactly by walking the events in the window.

This calibrates one unrecorded nuisance quantity against persisted DATA. It
reads no resolution label and calls no production code.
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
SP = _WORK
WINDOW = 0.40          # search span after the nominal tick


def state_at(times, states, t):
    i = bisect.bisect_right(times, t) - 1
    return "NO_SNAPSHOT" if i < 0 else states[i]


def main() -> int:
    with open(SP + "timelines.pkl", "rb") as fh:
        d = pickle.load(fh)
    grid, tickers = d["grid"], d["tickers"]
    tl = d["timelines"]

    idx = {lab: {tk: ([p[0] for p in pts], [p[1] for p in pts])
                 for tk, pts in tl[lab].items()} for lab in ("A", "B")}

    # merged event list per stream: (ts, ticker, new_state)
    events = []
    for lab in ("A", "B"):
        for tk, pts in tl[lab].items():
            for ts, st in pts:
                events.append((ts, lab, tk, st))
    events.sort(key=lambda e: e[0])
    ev_times = [e[0] for e in events]
    print(f"state-change events: {len(events):,}")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    per_tick_persisted = defaultdict(dict)
    for (g, tk, ab, aq, ak, akq, bb, bq, bk, bkq) in con.execute(
        "SELECT grid_ts, ticker, a_bid, a_bid_qty, a_ask, a_ask_qty, "
        "b_bid, b_bid_qty, b_ask, b_ask_qty FROM equivalence_comparison"
    ):
        per_tick_persisted[g][tk] = ((ab, aq, ak, akq), (bb, bq, bk, bkq))
    con.close()

    tset = set(tickers)
    results = []          # (grid_ts, cut, ticker, sa, sb) for unpersisted cells
    cuts = []
    unsolved = []

    for g in grid:
        pers = per_tick_persisted[g]
        miss = [tk for tk in tickers if tk not in pers]

        cur = {}
        for tk in tickers:
            cur[tk] = [state_at(*idx["A"][tk], g), state_at(*idx["B"][tk], g)]

        def viol(tk):
            sa, sb = cur[tk]
            p = pers.get(tk)
            if p is not None:
                return 0 if (sa == p[0] and sb == p[1]) else 1
            # unpersisted: must NOT be a two-sided agreement
            if isinstance(sa, str) or isinstance(sb, str):
                return 0
            return 1 if sa == sb else 0

        total = sum(viol(tk) for tk in tickers)
        feasible = []
        lo = g
        if total == 0:
            open_from = g
        else:
            open_from = None

        i = bisect.bisect_right(ev_times, g)
        while i < len(events) and events[i][0] <= g + WINDOW:
            ts, lab, tk, st = events[i]
            i += 1
            if tk not in tset:
                continue
            before = viol(tk)
            cur[tk][0 if lab == "A" else 1] = st
            after = viol(tk)
            if before != after:
                if total == 0 and after > before and open_from is not None:
                    feasible.append((open_from, ts))
                    open_from = None
                total += after - before
                if total == 0 and open_from is None:
                    open_from = ts
        if total == 0 and open_from is not None:
            feasible.append((open_from, g + WINDOW))

        if not feasible:
            unsolved.append(g)
            cut = g + 0.05
        else:
            # widest feasible interval, evaluated at its midpoint
            a, b = max(feasible, key=lambda iv: iv[1] - iv[0])
            cut = (a + b) / 2.0
        cuts.append(cut - g)

        for tk in miss:
            sa = state_at(*idx["A"][tk], cut)
            sb = state_at(*idx["B"][tk], cut)
            results.append((g, cut, tk, sa, sb))

    cs = sorted(cuts)
    print(f"\nrecovered tick offsets (s): min {cs[0]:.4f}  p25 {cs[len(cs)//4]:.4f}  "
          f"median {cs[len(cs)//2]:.4f}  p75 {cs[3*len(cs)//4]:.4f}  max {cs[-1]:.4f}")
    print(f"ticks with no feasible cut: {len(unsolved)}")

    kinds = defaultdict(int)
    for g, cut, tk, sa, sb in results:
        if isinstance(sa, str) or isinstance(sb, str):
            kinds["unmatched"] += 1
        elif sa == sb:
            kinds["still_agree(unexplained)"] += 1
        else:
            kinds["disagreement"] += 1
    print(f"\nunpersisted cells: {len(results):,}")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k:30s} {v:,}")

    with open(SP + "cells.pkl", "wb") as fh:
        pickle.dump({"cells": results, "grid": grid, "offsets": cuts,
                     "unsolved": unsolved}, fh, protocol=4)
    print(f"\nwrote {SP}cells.pkl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
