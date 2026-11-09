"""
STAGE 6 (supersedes stage 4). Final classification.

Stage 4 had a defect: its convergence search recorded an intervening
contradiction but did not abort on one, so it applied "reached within 500 ms"
instead of the frozen rule's "reached within 500 ms with no intervening
contradictory top". Fixed here; every number below uses the frozen rule.

Two resolvers are run over the SAME reconstructed timeline:

  FROZEN   the frozen rule applied to the complete retained timeline
  PRODEMU  the same rule applied through production's bounded in-memory ring
           (2.0 s horizon, 2048 entries, rebuilt as it stood 550 ms after the
           tick). This is an emulation of the probe's own resolver, used only
           to reconcile against the probe's reported 1,071 / 314 split -- no
           stored resolution label is read anywhere.

CATEGORY 4 / CATEGORY 5 BOUNDARY, DECLARED IN ADVANCE: >= 5.000 s of retained
evidence on both streams after the comparison instant counts as adequate
observation. Below that the case is right-censored.
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
import csv
import json
import pickle
from collections import Counter, defaultdict, deque

SP = _WORK
TOL = 500.0
FOLLOWUP_MIN_S = 5.0
HISTORY_SECONDS = 2.0
HISTORY_MAX = 2048
RESOLVE_DELAY = 0.55


def reaches(entries, target, after, within_ms):
    """Frozen rule. Returns delay_ms, or None."""
    limit = after + within_ms / 1000.0
    started_in, seen = None, False
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


def first_arrival(entries, target, after):
    """
    (delay_ms, contradiction) ignoring the contradiction rule, over the whole
    retained archive. Used only to separate categories 2/3 from 4/5.
    """
    i = bisect.bisect_right([e[0] for e in entries], after) - 1
    started_in = entries[i][1] if i >= 0 else None
    contra = None
    for ts, st in entries[i + 1 if i >= 0 else 0:]:
        if st == target:
            return (ts - after) * 1000.0, contra
        if started_in is not None and st != started_in and contra is None:
            contra = (ts, st)
    return None, contra


def ring_at(pts, resolve_ts):
    dq = deque()
    for ts, st in pts:
        if ts > resolve_ts:
            break
        if dq and dq[-1][1] == st:
            continue
        dq.append((ts, st))
        cut = ts - HISTORY_SECONDS
        while len(dq) > 1 and dq[0][0] < cut:
            dq.popleft()
        while len(dq) > HISTORY_MAX:
            dq.popleft()
    return list(dq)


def gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def main() -> int:
    with open(SP + "timelines.pkl", "rb") as fh:
        tld = pickle.load(fh)
    with open(SP + "cells.pkl", "rb") as fh:
        cd = pickle.load(fh)
    tl = tld["timelines"]
    cells, grid = cd["cells"], cd["grid"]
    archive_end = min(tld["stats"][l]["last_activity"] for l in ("A", "B"))

    # tick regime per market
    prices = defaultdict(set)
    for lab in ("A", "B"):
        for tk, pts in tl[lab].items():
            for _ts, st in pts:
                if isinstance(st, tuple):
                    for v in (st[0], st[2]):
                        if v:
                            prices[tk].add(v)
    regime = {}
    for tk, ps in prices.items():
        g = 0
        for p in ps:
            g = p if g == 0 else gcd(g, p)
        regime[tk] = "1c" if g and g % 100 == 0 else ("0.1c" if g and g % 10 == 0 else "finer")

    rate = Counter()
    for lab in ("A", "B"):
        for tk, pts in tl[lab].items():
            for ts, _st in pts:
                rate[int(ts)] += 1
    rr = sorted(rate.values())
    burst_hi = rr[int(0.95 * len(rr))]

    rows = []
    tally = Counter()
    for g, cut, tk, sa, sb in cells:
        pa, pb = tl["A"][tk], tl["B"][tk]

        f_b = reaches(pb, sa, cut, TOL)          # B catches up to A's state
        f_a = reaches(pa, sb, cut, TOL)          # A catches up to B's state
        frozen_ok = f_b is not None or f_a is not None
        frozen_ms = min([x for x in (f_b, f_a) if x is not None], default=None)
        frozen_who = ("B" if f_b is not None and (f_a is None or f_b <= f_a)
                      else ("A" if f_a is not None else ""))

        rb = ring_at(pb, cut + RESOLVE_DELAY)
        ra = ring_at(pa, cut + RESOLVE_DELAY)
        prod_ok = (reaches(rb, sa, cut, TOL) is not None
                   or reaches(ra, sb, cut, TOL) is not None)

        ab_ms, ab_contra = first_arrival(pb, sa, cut)
        aa_ms, aa_contra = first_arrival(pa, sb, cut)
        cands = [(m, w, c) for m, w, c in ((ab_ms, "B", ab_contra),
                                           (aa_ms, "A", aa_contra)) if m is not None]
        any_ms, any_who, any_contra = min(cands) if cands else (None, "", None)
        if any_contra is None:
            any_contra = ab_contra or aa_contra

        followup = archive_end - cut

        if frozen_ok and prod_ok:
            cat = "0_explained_both"
        elif frozen_ok and not prod_ok:
            cat = "1_prod_missed_valid_convergence"
        elif prod_ok and not frozen_ok:
            cat = "X_prod_lenient_beyond_frozen_rule"
        elif any_ms is not None and any_ms <= TOL:
            cat = "3_disqualified_by_contradiction"
        elif any_ms is not None:
            cat = "2_late_convergence"
        elif followup >= FOLLOWUP_MIN_S:
            cat = "4_persistent"
        else:
            cat = "5_right_censored"
        tally[cat] += 1

        same_px = sa[0] == sb[0] and sa[2] == sb[2]
        ta = [e[0] for e in pa]
        tb = [e[0] for e in pb]
        ia = bisect.bisect_right(ta, cut) - 1
        ib = bisect.bisect_right(tb, cut) - 1
        if any_who:
            behind = any_who
        elif ia >= 0 and ib >= 0 and ta[ia] != tb[ib]:
            behind = "B" if ta[ia] > tb[ib] else "A"
        else:
            behind = "indeterminate"

        rows.append({
            "grid_ts": round(g, 4), "eval_ts": round(cut, 4), "ticker": tk,
            "series": tk.split("-")[0], "both_valid": 1,
            "a_bid": sa[0], "a_bid_qty": sa[1], "a_ask": sa[2], "a_ask_qty": sa[3],
            "b_bid": sb[0], "b_bid_qty": sb[1], "b_ask": sb[2], "b_ask_qty": sb[3],
            "disagreement_type": "same_price_diff_size" if same_px else "different_price",
            "stream_behind": behind,
            "frozen_rule_explained": int(frozen_ok),
            "frozen_ms": "" if frozen_ms is None else round(frozen_ms, 1),
            "frozen_stream": frozen_who,
            "prod_emu_explained": int(prod_ok),
            "any_convergence_ms": "" if any_ms is None else round(any_ms, 1),
            "any_convergence_stream": any_who,
            "contradiction_ts": "" if any_contra is None else round(any_contra[0], 4),
            "contradiction_state": "" if any_contra is None else json.dumps(any_contra[1]),
            "tick_regime": regime.get(tk, "unknown"),
            "price_level_bp": sa[0] if sa[0] is not None else (sa[2] or ""),
            "followup_s": round(followup, 3),
            "near_reconnect": 0, "near_subscription_update": 0, "near_seq_gap": 0,
            "events_this_second": rate[int(cut)],
            "in_rate_burst_p95": int(rate[int(cut)] >= burst_hi),
            "category": cat,
        })

    n = len(rows)
    print(f"reconstructed disagreements: {n:,}\n")
    order = ["0_explained_both", "1_prod_missed_valid_convergence",
             "X_prod_lenient_beyond_frozen_rule", "2_late_convergence",
             "3_disqualified_by_contradiction", "4_persistent", "5_right_censored"]
    for k in order:
        print(f"  {k:38s} {tally[k]:6,}")
    print(f"  {'TOTAL':38s} {sum(tally.values()):6,}")

    fe = tally["0_explained_both"] + tally["1_prod_missed_valid_convergence"]
    pe = tally["0_explained_both"] + tally["X_prod_lenient_beyond_frozen_rule"]
    print(f"\nfrozen-rule explained : {fe:,}   residual {n-fe:,}")
    print(f"prod-emu explained    : {pe:,}   residual {n-pe:,}   "
          f"(probe reported 1,071 explained / 314 residual)")

    with open(SP + "final.csv", "w", newline="", encoding="utf-8") as fh:
        # An explicit LF terminator, not the platform default: final.csv is
        # hashed in the README and must be byte-identical on any OS.
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()),
                           lineterminator=chr(10))
        w.writeheader()
        w.writerows(rows)
    with open(SP + "final.pkl", "wb") as fh:
        pickle.dump({"rows": rows, "archive_end": archive_end,
                     "burst_hi": burst_hi, "tally": dict(tally)}, fh, protocol=4)
    print(f"\nwrote {SP}final.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
