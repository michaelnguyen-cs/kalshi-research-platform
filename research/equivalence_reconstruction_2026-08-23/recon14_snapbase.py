"""
Decisive test for the three unpaired markets.

Hypothesis: the two subscriptions were served orderbook_snapshots taken at
DIFFERENT exchange instants. Events that fall between the earlier snapshot and
the later one are delivered to the earlier-snapshotted stream as deltas, and to
the later-snapshotted stream not at all -- because they are already baked into
its snapshot. The delta streams then differ by exactly those events while the
reconstructed books remain identical.

If true: (a) the head-of-stream extra deltas carry an exchange ts_ms at or
before the other stream's snapshot instant, and (b) the FULL ladders -- not just
the tops -- agree once both snapshots have been applied.

The tail-of-archive extras are tested separately: they should be the last frames
before recording teardown, with no counterpart on the lagging stream simply
because the run ended first.
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

import hashlib
import json
import sqlite3
import zlib
from decimal import Decimal

DB = _DB
TARGETS = ["KXBTC15M-26AUG222030-30",
           "KXETH15M-26AUG222030-30",
           "KXMLBHRR-26AUG222040MINSD-MINKCLEMENS2-1"]
PROBE = [1787444260.0, 1787444300.0, 1787444400.0, 1787444490.0]


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zd = {r["id"]: bytes(r["zdict"]) for r in con.execute(
        "SELECT id, zdict FROM raw_dictionary")}

    books = {(l, t): {"bids": {}, "asks": {}, "snap": None, "snap_id": None}
             for l in "AB" for t in TARGETS}
    snap_seen = {}
    ladders = {k: [] for k in books}

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
        if mt not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        b = m["msg"]
        tk = b.get("market_ticker")
        if tk not in TARGETS:
            continue
        lab = row["stream_label"]
        uyp = lab == "A"
        bk = books[(lab, tk)]
        if mt == "orderbook_snapshot":
            bk["bids"], bk["asks"] = {}, {}
            for side, key in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
                lad = bk["bids"] if side == "yes" else bk["asks"]
                for lv in b.get(key) or ():
                    p = int(Decimal(str(lv[0])) * 10000)
                    if side == "no" and not uyp:
                        p = 10000 - p
                    q = int(Decimal(str(lv[1])) * 100)
                    if q:
                        lad[p] = q
            bk["snap"] = row["recv_ts"]
            bk["snap_id"] = row["id"]
            snap_seen[(lab, tk)] = (row["id"], row["recv_ts"], m["seq"])
        else:
            p = int(Decimal(str(b["price_dollars"])) * 10000)
            if b["side"] == "no" and not uyp:
                p = 10000 - p
            d = int(Decimal(str(b["delta_fp"])) * 100)
            lad = bk["bids"] if b["side"] == "yes" else bk["asks"]
            n = lad.get(p, 0) + d
            if n <= 0:
                lad.pop(p, None)
            else:
                lad[p] = n
        while probes and row["recv_ts"] > probes[0]:
            t = probes.pop(0)
            for k, v in books.items():
                ladders[k].append((t, dict(v["bids"]), dict(v["asks"])))
    con.close()

    print("=== snapshot delivery instants ===")
    for tk in TARGETS:
        a, b = snap_seen[("A", tk)], snap_seen[("B", tk)]
        print(f"  {tk}")
        print(f"    A snapshot ws_raw id={a[0]:<6d} seq={a[2]:<5d} recv={a[1]:.4f}")
        print(f"    B snapshot ws_raw id={b[0]:<6d} seq={b[2]:<5d} recv={b[1]:.4f}"
              f"   (B is {(b[1]-a[1])*1000:+.1f} ms vs A)")

    print("\n=== FULL LADDER comparison, A vs B, all levels ===")
    for tk in TARGETS:
        print(f"  {tk}")
        for i, t in enumerate(PROBE):
            if i >= len(ladders[("A", tk)]):
                continue
            _, ab, aa = ladders[("A", tk)][i]
            _, bb, ba = ladders[("B", tk)][i]
            same = ab == bb and aa == ba
            nb = len(set(ab.items()) ^ set(bb.items()))
            na = len(set(aa.items()) ^ set(ba.items()))
            print(f"    t={t:.0f}  bids A {len(ab):4d} B {len(bb):4d}  "
                  f"asks A {len(aa):4d} B {len(ba):4d}  "
                  f"{'IDENTICAL' if same else f'DIFFER (bid levels {nb}, ask levels {na})'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
