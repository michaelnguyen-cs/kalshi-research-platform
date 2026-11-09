"""
Mechanism of the three markets that did not pair event-for-event.

Aligns each market's A and B delta sequences with difflib on the content key
(exchange ts_ms, side, canonical price, delta) and prints every insertion or
deletion together with the surrounding raw frames from both streams, so the
mechanism is read off the wire rather than guessed.
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

import difflib
import hashlib
import json
import sqlite3
import zlib
from collections import Counter, defaultdict
from decimal import Decimal

DB = _DB
TARGETS = ["KXBTC15M-26AUG222030-30",
           "KXETH15M-26AUG222030-30",
           "KXMLBHRR-26AUG222040MINSD-MINKCLEMENS2-1"]


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zd = {r["id"]: bytes(r["zdict"]) for r in con.execute(
        "SELECT id, zdict FROM raw_dictionary")}

    ev = {"A": defaultdict(list), "B": defaultdict(list)}
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
        if m.get("type") != "orderbook_delta":
            continue
        b = m["msg"]
        if b["market_ticker"] not in TARGETS:
            continue
        lab = row["stream_label"]
        p = int(Decimal(str(b["price_dollars"])) * 10000)
        if b["side"] == "no" and lab == "B":
            p = 10000 - p
        ev[lab][b["market_ticker"]].append({
            "id": row["id"], "recv": row["recv_ts"], "seq": m["seq"],
            "ts_ms": b.get("ts_ms"), "side": b["side"], "cp": p,
            "wire_price": b["price_dollars"],
            "d": int(Decimal(str(b["delta_fp"])) * 100),
        })
    con.close()

    for tk in TARGETS:
        a, b = ev["A"][tk], ev["B"][tk]
        print("=" * 96)
        print(f"{tk}\n  A deltas {len(a):,}   B deltas {len(b):,}   "
              f"difference {len(b)-len(a):+d}")

        ka = [(e["ts_ms"], e["side"], e["cp"], e["d"]) for e in a]
        kb = [(e["ts_ms"], e["side"], e["cp"], e["d"]) for e in b]
        sm = difflib.SequenceMatcher(a=ka, b=kb, autojunk=False)
        ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
        print(f"  aligned blocks differing: {len(ops)}")

        # duplicate-key census: does the key identify an event uniquely?
        ca, cb = Counter(ka), Counter(kb)
        dupa = sum(v - 1 for v in ca.values() if v > 1)
        dupb = sum(v - 1 for v in cb.values() if v > 1)
        print(f"  non-unique content keys: A {dupa:,}  B {dupb:,}")
        coll_a = Counter(e["ts_ms"] for e in a)
        print(f"  max events sharing one ts_ms: A "
              f"{max(coll_a.values()) if coll_a else 0}  B "
              f"{max(Counter(e['ts_ms'] for e in b).values()) if b else 0}")

        for tag, i1, i2, j1, j2 in ops[:6]:
            print(f"\n  --- {tag}: A[{i1}:{i2}] vs B[{j1}:{j2}] ---")
            for e in a[i1:i2][:6]:
                print(f"    A only  id={e['id']:<7d} seq={e['seq']:<7d} "
                      f"recv={e['recv']:.4f} ts_ms={e['ts_ms']} "
                      f"side={e['side']:3s} wire={e['wire_price']:>7s} "
                      f"canon={e['cp']:5d} delta={e['d']/100:+.2f}")
            for e in b[j1:j2][:6]:
                print(f"    B only  id={e['id']:<7d} seq={e['seq']:<7d} "
                      f"recv={e['recv']:.4f} ts_ms={e['ts_ms']} "
                      f"side={e['side']:3s} wire={e['wire_price']:>7s} "
                      f"canon={e['cp']:5d} delta={e['d']/100:+.2f}")
            ctx_a = a[max(0, i1-2):i1]
            ctx_b = b[max(0, j1-2):j1]
            if ctx_a:
                print("    context, last matched A events:")
                for e in ctx_a:
                    print(f"      id={e['id']:<7d} ts_ms={e['ts_ms']} "
                          f"side={e['side']:3s} canon={e['cp']:5d} "
                          f"delta={e['d']/100:+.2f}")
            if ctx_b:
                print("    context, last matched B events:")
                for e in ctx_b:
                    print(f"      id={e['id']:<7d} ts_ms={e['ts_ms']} "
                          f"side={e['side']:3s} canon={e['cp']:5d} "
                          f"delta={e['d']/100:+.2f}")
        if len(ops) > 6:
            print(f"\n  ... {len(ops)-6} further differing blocks")

        # net effect on the ladder
        na = Counter()
        nb = Counter()
        for e in a:
            na[(e["side"], e["cp"])] += e["d"]
        for e in b:
            nb[(e["side"], e["cp"])] += e["d"]
        diff = {k: nb.get(k, 0) - na.get(k, 0)
                for k in set(na) | set(nb) if nb.get(k, 0) != na.get(k, 0)}
        print(f"\n  net cumulative delta per (side, canonical price) differing "
              f"between streams: {len(diff)} levels")
        for k, v in sorted(diff.items())[:8]:
            print(f"    side={k[0]:3s} canon={k[1]:5d}  B-A = {v/100:+.2f} contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
