"""
KXBTC15M is the one unpaired market whose full ladders did not match at the four
spot checks. This walks its book on both streams and reports, level by level and
over time, exactly which levels differ -- separating a transient lag difference
(a level that differs at one probe and agrees at the next) from a persistent
divergence (the same level differing throughout).
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

import collections
import hashlib
import json
import sqlite3
import zlib
from decimal import Decimal

DB = _DB
TK = "KXBTC15M-26AUG222030-30"
PROBE = [1787444260.0 + i * 10 for i in range(24)]


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zd = {r["id"]: bytes(r["zdict"]) for r in con.execute(
        "SELECT id, zdict FROM raw_dictionary")}

    bk = {l: {"bids": {}, "asks": {}} for l in "AB"}
    last_ev = {l: 0.0 for l in "AB"}
    snaps = {}
    out = []
    probes = list(PROBE)

    for row in con.execute(
        "SELECT id, stream_label, recv_ts, raw, codec, zdict_id, payload, "
        "raw_sha256 FROM ws_raw WHERE direction='in' ORDER BY id"
    ):
        blob = row["payload"]
        if blob is None:
            text = row["raw"]
        else:
            if row["codec"] == 0:
                data = bytes(blob)
            else:
                o = zlib.decompressobj(zdict=zd[row["zdict_id"]])
                data = o.decompress(bytes(blob)) + o.flush()
            assert hashlib.sha256(data).digest() == bytes(row["raw_sha256"])
            text = data.decode()
        m = json.loads(text)
        mt = m.get("type")
        if mt in ("orderbook_snapshot", "orderbook_delta"):
            b = m["msg"]
            if b.get("market_ticker") == TK:
                lab = row["stream_label"]
                uyp = lab == "A"
                last_ev[lab] = row["recv_ts"]
                if mt == "orderbook_snapshot":
                    bk[lab] = {"bids": {}, "asks": {}}
                    for side, key in (("yes", "yes_dollars_fp"),
                                      ("no", "no_dollars_fp")):
                        lad = bk[lab]["bids" if side == "yes" else "asks"]
                        for lv in b.get(key) or ():
                            p = int(Decimal(str(lv[0])) * 10000)
                            if side == "no" and not uyp:
                                p = 10000 - p
                            q = int(Decimal(str(lv[1])) * 100)
                            if q:
                                lad[p] = q
                    snaps[lab] = (row["id"], row["recv_ts"], len(bk[lab]["bids"]),
                                  len(bk[lab]["asks"]))
                else:
                    p = int(Decimal(str(b["price_dollars"])) * 10000)
                    if b["side"] == "no" and not uyp:
                        p = 10000 - p
                    d = int(Decimal(str(b["delta_fp"])) * 100)
                    lad = bk[lab]["bids" if b["side"] == "yes" else "asks"]
                    n = lad.get(p, 0) + d
                    if n <= 0:
                        lad.pop(p, None)
                    else:
                        lad[p] = n
        while probes and row["recv_ts"] > probes[0]:
            t = probes.pop(0)
            out.append((t, {l: {"bids": dict(bk[l]["bids"]),
                                "asks": dict(bk[l]["asks"])} for l in "AB"},
                        dict(last_ev)))
    con.close()

    print(f"snapshots: A id={snaps['A'][0]} recv={snaps['A'][1]:.4f} "
          f"{snaps['A'][2]} bid / {snaps['A'][3]} ask levels")
    print(f"           B id={snaps['B'][0]} recv={snaps['B'][1]:.4f} "
          f"{snaps['B'][2]} bid / {snaps['B'][3]} ask levels")

    persist = collections.Counter()
    print("\nper-probe level differences (A vs B), 10 s apart")
    for t, st, lev in out:
        diffs = []
        for name in ("bids", "asks"):
            x, y = st["A"][name], st["B"][name]
            for p in sorted(set(x) | set(y)):
                if x.get(p) != y.get(p):
                    diffs.append((name[:3], p, x.get(p), y.get(p)))
                    persist[(name[:3], p)] += 1
        gap = f"A last ev {(t-lev['A'])*1000:6.0f} ms ago, B {(t-lev['B'])*1000:6.0f} ms ago"
        if not diffs:
            print(f"  t=+{t-1787444257:5.0f}s  identical                          {gap}")
        else:
            d = "; ".join(f"{n} {p} A={xa} B={xb}" for n, p, xa, xb in diffs)
            print(f"  t=+{t-1787444257:5.0f}s  {d}    {gap}")

    print("\nlevels that ever differed, and at how many of the "
          f"{len(out)} probes:")
    for k, v in persist.most_common():
        print(f"  {k[0]} canon={k[1]:5d} ({k[1]/100:.2f}c)  {v}/{len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
