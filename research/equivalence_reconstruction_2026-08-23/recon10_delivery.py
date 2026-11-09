"""
STAGE 10. Direct measurement of per-stream delivery lag.

Both streams subscribe to the same 3,472 markets on one subscription each and
the server numbers each subscription's messages from 1. If message `seq = N`
carries the same market and the same book event on both streams, the two feeds
are the same logical sequence and

    recv_ts_B(N) - recv_ts_A(N)

is the delivery-plus-dispatch lag of B behind A for that exact event -- measured
without any model, from persisted local receive timestamps only.

The pairing is verified before it is used: every paired seq must carry the same
market_ticker and the same message type on both streams.
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
import pickle
import sqlite3
import zlib
from collections import Counter

DB = _DB
SP = _WORK


def q(s, p):
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zd = {r["id"]: bytes(r["zdict"]) for r in con.execute(
        "SELECT id, zdict FROM raw_dictionary")}

    by = {"A": {}, "B": {}}
    has_ts_ms = Counter()
    for row in con.execute(
        "SELECT stream_label, direction, recv_ts, raw, codec, zdict_id, payload, "
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
        seq = m.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        body = m.get("msg") or {}
        by[row["stream_label"]][seq] = (
            row["recv_ts"], m.get("type"), body.get("market_ticker"),
            body.get("ts_ms"))
        if body.get("ts_ms") is not None:
            has_ts_ms[row["stream_label"]] += 1
    con.close()

    common = sorted(set(by["A"]) & set(by["B"]))
    print(f"seq numbers: A {len(by['A']):,}  B {len(by['B']):,}  "
          f"common {len(common):,}")
    print(f"deltas carrying an exchange ts_ms: {dict(has_ts_ms)}")

    mismatch = sum(1 for s in common
                   if by["A"][s][1] != by["B"][s][1]
                   or by["A"][s][2] != by["B"][s][2])
    print(f"paired seqs whose type+market differ between streams: {mismatch:,}")
    if mismatch:
        print("  pairing is NOT safe; lag below is not meaningful")
        return 1
    print("  pairing verified: the two streams carry the same logical sequence")

    lags = sorted((by["B"][s][0] - by["A"][s][0]) * 1000.0 for s in common)
    print(f"\nB minus A delivery lag over {len(lags):,} paired messages (ms)")
    for lbl, p in (("min", 0.0), ("p01", .01), ("p25", .25), ("median", .5),
                   ("p75", .75), ("p95", .95), ("p99", .99), ("p999", .999),
                   ("max", 1.0)):
        print(f"  {lbl:7s} {q(lags, p):9.1f}")
    over = [x for x in lags if x > 500]
    print(f"  messages delivered to B more than 500 ms after A: {len(over):,} "
          f"({100*len(over)/len(lags):.3f}%)")
    under = [x for x in lags if x < -500]
    print(f"  messages delivered to A more than 500 ms after B: {len(under):,} "
          f"({100*len(under)/len(lags):.3f}%)")

    with open(SP + "lags.pkl", "wb") as fh:
        pickle.dump({"lags": lags}, fh, protocol=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
