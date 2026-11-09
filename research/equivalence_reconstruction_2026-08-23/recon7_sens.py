"""
STAGE 7. Sensitivity of the production-resolver emulation to the two quantities
the probe never recorded: the per-tick evaluation instant and the resolve
instant. If the probe's reported 314 residual falls inside the range this
produces, the reconciliation is complete and the 12-case gap is timing
uncertainty rather than an unexplained difference.

The FROZEN-rule numbers are re-run at each setting too, to show they are stable.
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
from collections import deque

SP = _WORK
TOL = 500.0
HS, HMAX = 2.0, 2048


def reaches(entries, target, after, within_ms):
    limit = after + within_ms / 1000.0
    started, seen = None, False
    for ts, st in entries:
        if ts <= after:
            started, seen = st, True
            continue
        if ts > limit:
            return None
        if st == target:
            return (ts - after) * 1000.0
        if seen and st != started:
            return None
    return None


def ring_at(pts, resolve_ts):
    dq = deque()
    for ts, st in pts:
        if ts > resolve_ts:
            break
        if dq and dq[-1][1] == st:
            continue
        dq.append((ts, st))
        c = ts - HS
        while len(dq) > 1 and dq[0][0] < c:
            dq.popleft()
        while len(dq) > HMAX:
            dq.popleft()
    return list(dq)


def state_at(pts, t):
    i = bisect.bisect_right([p[0] for p in pts], t) - 1
    return "NO_SNAPSHOT" if i < 0 else pts[i][1]


def main() -> int:
    with open(SP + "timelines.pkl", "rb") as fh:
        tld = pickle.load(fh)
    with open(SP + "cells.pkl", "rb") as fh:
        cd = pickle.load(fh)
    tl = tld["timelines"]
    cells = cd["cells"]

    print(f"{'cut shift':>10s} {'resolve':>8s} {'frozen ok':>10s} {'frozen res':>11s} "
          f"{'prod ok':>9s} {'prod res':>9s}")
    for shift in (-0.010, -0.005, 0.0, 0.005, 0.010):
        for rd in (0.50, 0.55, 0.60, 0.70):
            fo = po = n = 0
            for g, cut, tk, sa0, sb0 in cells:
                pa, pb = tl["A"][tk], tl["B"][tk]
                c = cut + shift
                sa, sb = state_at(pa, c), state_at(pb, c)
                if isinstance(sa, str) or isinstance(sb, str) or sa == sb:
                    continue          # not a disagreement at this cut
                n += 1
                if (reaches(pb, sa, c, TOL) is not None
                        or reaches(pa, sb, c, TOL) is not None):
                    fo += 1
                if (reaches(ring_at(pb, c + rd), sa, c, TOL) is not None
                        or reaches(ring_at(pa, c + rd), sb, c, TOL) is not None):
                    po += 1
            print(f"{shift*1000:9.0f}ms {rd:7.2f}s {fo:10,} {n-fo:11,} "
                  f"{po:9,} {n-po:9,}   (n={n:,})")
    print("\nprobe reported: 1,071 explained / 314 residual of 1,385")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
