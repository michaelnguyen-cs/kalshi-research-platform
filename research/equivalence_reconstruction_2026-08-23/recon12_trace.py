"""
STAGE 12. Fully expanded raw-frame traces for representative cases of every
root-cause class present, plus a robustness check on the category-1 count.

Each trace gives: the comparison instant; both streams' canonical bid/ask price
and size; the ws_raw frame ids, local receive timestamps and messages that
produced those states; the subsequent state changes on both streams; and the
sequence/session validity on both sides at every step.
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
import hashlib
import json
import pickle
import sqlite3
import zlib
from collections import deque
from decimal import Decimal

DB = _DB
SP = _WORK
TOL = 500.0
HS, HMAX, RD = 2.0, 2048, 0.55


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


def fmt(st):
    if not isinstance(st, tuple):
        return str(st)
    bp, bq, ap, aq = st
    b = "  --  " if bp is None else f"{bp/100:6.2f}c"
    bs = "   -- " if bq is None else f"{bq/100:7.2f}"
    a = "  --  " if ap is None else f"{ap/100:6.2f}c"
    aslot = "   -- " if aq is None else f"{aq/100:7.2f}"
    return f"bid {b} x {bs}   ask {a} x {aslot}"


class Frames:
    def __init__(self, con):
        self.con = con
        self.zd = {r["id"]: bytes(r["zdict"]) for r in con.execute(
            "SELECT id, zdict FROM raw_dictionary")}

    def around(self, ticker, lab, t0, t1):
        out = []
        for row in self.con.execute(
            "SELECT id, stream_label, recv_ts, raw, codec, zdict_id, payload, "
            "raw_sha256 FROM ws_raw WHERE direction='in' AND stream_label=? "
            "AND recv_ts BETWEEN ? AND ? ORDER BY id", (lab, t0, t1)
        ):
            blob = row["payload"]
            if blob is None:
                text = row["raw"]
            else:
                if row["codec"] == 0:
                    data = bytes(blob)
                else:
                    o = zlib.decompressobj(zdict=self.zd[row["zdict_id"]])
                    data = o.decompress(bytes(blob)) + o.flush()
                if hashlib.sha256(data).digest() != bytes(row["raw_sha256"]):
                    raise SystemExit("checksum mismatch")
                text = data.decode()
            m = json.loads(text)
            if (m.get("msg") or {}).get("market_ticker") != ticker:
                continue
            out.append((row["id"], row["recv_ts"], m))
        return out


def trace(fr, row, tl, why):
    tk, cut = row["ticker"], row["eval_ts"]
    sa = (row["a_bid"], row["a_bid_qty"], row["a_ask"], row["a_ask_qty"])
    sb = (row["b_bid"], row["b_bid_qty"], row["b_ask"], row["b_ask_qty"])
    print("=" * 100)
    print(f"{why}")
    print(f"  ticker            {tk}")
    print(f"  series            {row['series']}")
    print(f"  grid timestamp    {row['grid_ts']:.4f}  "
          f"(evaluation instant {cut:.4f})")
    print(f"  both VALID        yes (session epoch 1 on both; no sequence gap, "
          f"no reconnect, no subscription update anywhere in the run)")
    print(f"  A canonical top   {fmt(sa)}")
    print(f"  B canonical top   {fmt(sb)}")
    print(f"  type              {row['disagreement_type']}")
    print(f"  tick regime       {row['tick_regime']}")
    fz = "EXPLAINED" if row["frozen_rule_explained"] else "NOT explained"
    if row["frozen_ms"] != "":
        fz += f" at {row['frozen_ms']} ms"
    print(f"  frozen rule       {fz}")
    print(f"  production emu    {'EXPLAINED' if row['prod_emu_explained'] else 'NOT explained'}")
    print(f"  first arrival of the other state: "
          f"{row['any_convergence_ms']} ms on stream {row['any_convergence_stream']}")
    if row["contradiction_ts"] != "":
        print(f"  intervening contradiction at {row['contradiction_ts']:.4f} "
              f"({(row['contradiction_ts']-cut)*1000:.1f} ms after): "
              f"{fmt(tuple(json.loads(row['contradiction_state'])))}")
    print(f"  retained follow-up {row['followup_s']:.3f} s")

    for lab in ("A", "B"):
        print(f"\n  --- stream {lab} raw frames, {tk}, "
              f"[{cut-0.60:.3f}, {cut+1.20:.3f}] ---")
        for fid, ts, m in fr.around(tk, lab, cut - 0.60, cut + 1.20):
            b = m.get("msg") or {}
            mark = "<== comparison instant is here" if ts <= cut else ""
            if m["type"] == "orderbook_delta":
                d = (f"delta  side={b['side']:3s} price={b['price_dollars']:>7s} "
                     f"delta_fp={b['delta_fp']:>10s} ts_ms={b.get('ts_ms')}")
            else:
                d = f"{m['type']} (full book replacement)"
            print(f"    id={fid:<7d} recv={ts:.4f} (t{(ts-cut)*1000:+8.1f} ms) "
                  f"sid={m.get('sid')} seq={m.get('seq'):<7} {d}")
        pts = tl[lab][tk]
        i = bisect.bisect_right([p[0] for p in pts], cut) - 1
        print(f"  --- stream {lab} canonical top-of-book states ---")
        for ts, st in pts[max(0, i - 1):i + 6]:
            tag = " <= state used in the comparison" if ts <= cut and (
                pts[i][0] == ts) else ""
            print(f"    {ts:.4f} (t{(ts-cut)*1000:+9.1f} ms)  {fmt(st)}{tag}")


def main() -> int:
    with open(SP + "final.pkl", "rb") as fh:
        rows = pickle.load(fh)["rows"]
    with open(SP + "timelines.pkl", "rb") as fh:
        tl = pickle.load(fh)["timelines"]

    # -- category 1 robustness ------------------------------------------------
    print("category-1 robustness (frozen rule explains, production does not)")
    for rd in (0.50, 0.55, 0.60, 0.70, 1.00):
        c1 = 0
        for r in rows:
            tk, cut = r["ticker"], r["eval_ts"]
            sa = (r["a_bid"], r["a_bid_qty"], r["a_ask"], r["a_ask_qty"])
            sb = (r["b_bid"], r["b_bid_qty"], r["b_ask"], r["b_ask_qty"])
            pa, pb = tl["A"][tk], tl["B"][tk]
            fz = (reaches(pb, sa, cut, TOL) is not None
                  or reaches(pa, sb, cut, TOL) is not None)
            if not fz:
                continue
            pr = (reaches(ring_at(pb, cut + rd), sa, cut, TOL) is not None
                  or reaches(ring_at(pa, cut + rd), sb, cut, TOL) is not None)
            if not pr:
                c1 += 1
        print(f"  resolve delay {rd:.2f}s -> category 1 = {c1}")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fr = Frames(con)

    picks = [
        ("2_late_convergence", "CLASS: CATEGORY 2 -- late convergence (>500 ms)"),
        ("3_disqualified_by_contradiction",
         "CLASS: CATEGORY 3 -- other state reached <=500 ms but an intervening "
         "contradictory top disqualifies it"),
        ("X_prod_lenient_beyond_frozen_rule",
         "CLASS: PRODUCTION LENIENCY -- production's bounded ring explained a "
         "case the frozen rule does not permit"),
    ]
    for cat, title in picks:
        cand = [r for r in rows if r["category"] == cat]
        if not cand:
            print(f"\n(no cases in {cat})")
            continue
        cand.sort(key=lambda r: -float(r["any_convergence_ms"] or 0))
        for r in cand[:1]:
            print()
            trace(fr, r, tl, title)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
