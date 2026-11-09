"""
STAGE 5. Why does the reconstruction explain 1,183 of the 1,385 when the probe
explained 1,071?

The probe's per-case labels were never written to the database, so the two sets
cannot be intersected. What CAN be done is to re-run the frozen rule over the
reconstructed timeline under several candidate resolver implementations and see
which one reproduces the probe's 1,071 / 314 split. Whichever does is the
mechanism behind the difference.

Variants:
  V1  full retained timeline, both directions, contradiction enforced
  V2  production's bounded ring (2.0 s horizon, 2048 entries) rebuilt as it
      would have stood 550 ms after the tick, including reaches()' behaviour
      when no pre-comparison entry survives eviction
  V3  as V1 but only the B-catches-up direction is tried
  V4  history sampled on the 1 Hz grid instead of on book events -- the
      pre-fix implementation
  V5  as V2 but the ring is seeded only from the tick at which the market was
      first observed (no warm-up before the run's first grid tick)
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
from collections import Counter, deque

SP = _WORK
TOL = 500.0
HISTORY_SECONDS = 2.0
HISTORY_MAX = 2048
RESOLVE_DELAY = 0.55


def reaches_seq(entries, target, after, within_ms):
    """The frozen rule over an ordered [(ts, state)] sequence."""
    limit = after + within_ms / 1000.0
    started_in = None
    seen = False
    for ts, st in entries:
        if ts <= after:
            started_in, seen = st, True
            continue
        if ts > limit:
            return None
        if st == target:
            return (ts - after) * 1000.0
        if seen and st != started_in:
            return None
    return None


def ring_at(pts, resolve_ts):
    """Production's bounded ring as it stood at `resolve_ts`."""
    dq = deque()
    for ts, st in pts:
        if ts > resolve_ts:
            break
        if dq and dq[-1][1] == st:
            continue
        dq.append((ts, st))
        cutoff = ts - HISTORY_SECONDS
        while len(dq) > 1 and dq[0][0] < cutoff:
            dq.popleft()
        while len(dq) > HISTORY_MAX:
            dq.popleft()
    return list(dq)


def main() -> int:
    with open(SP + "timelines.pkl", "rb") as fh:
        tld = pickle.load(fh)
    with open(SP + "cells.pkl", "rb") as fh:
        cd = pickle.load(fh)
    tl = tld["timelines"]
    grid = cd["grid"]
    cells = cd["cells"]

    idx = {lab: {tk: ([p[0] for p in pts], [p[1] for p in pts])
                 for tk, pts in tl[lab].items()} for lab in ("A", "B")}

    def state_at(lab, tk, t):
        times, states = idx[lab][tk]
        i = bisect.bisect_right(times, t) - 1
        return "NO_SNAPSHOT" if i < 0 else states[i]

    # 1 Hz-sampled history, the pre-fix shape: one observation per grid tick
    grid_hist = {}
    for lab in ("A", "B"):
        for tk in tl[lab]:
            seq = []
            for g in grid:
                st = state_at(lab, tk, g)
                if seq and seq[-1][1] == st:
                    continue
                seq.append((g, st))
            grid_hist[(lab, tk)] = seq

    counts = Counter()
    for g, cut, tk, sa, sb in cells:
        pa, pb = tl["A"][tk], tl["B"][tk]

        # V1
        v1 = (reaches_seq(pa, sb, cut, TOL) is not None
              or reaches_seq(pb, sa, cut, TOL) is not None)
        # V2
        ra = ring_at(pa, cut + RESOLVE_DELAY)
        rb = ring_at(pb, cut + RESOLVE_DELAY)
        v2 = (reaches_seq(rb, sa, cut, TOL) is not None
              or reaches_seq(ra, sb, cut, TOL) is not None)
        # V3
        v3 = reaches_seq(pb, sa, cut, TOL) is not None
        # V4
        v4 = (reaches_seq(grid_hist[("B", tk)], sa, cut, TOL) is not None
              or reaches_seq(grid_hist[("A", tk)], sb, cut, TOL) is not None)
        # V5
        ra5 = [e for e in ra if e[0] >= grid[0]]
        rb5 = [e for e in rb if e[0] >= grid[0]]
        v5 = (reaches_seq(rb5, sa, cut, TOL) is not None
              or reaches_seq(ra5, sb, cut, TOL) is not None)

        for name, ok in (("V1", v1), ("V2", v2), ("V3", v3), ("V4", v4), ("V5", v5)):
            if ok:
                counts[name] += 1

    n = len(cells)
    print(f"disagreements: {n:,}\n")
    print(f"{'variant':6s} {'explained':>10s} {'residual':>10s}   probe reported 1,071 / 314")
    for v in ("V1", "V2", "V3", "V4", "V5"):
        e = counts[v]
        flag = "  <== matches the probe" if (e, n - e) == (1071, 314) else ""
        print(f"{v:6s} {e:10,} {n-e:10,}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
