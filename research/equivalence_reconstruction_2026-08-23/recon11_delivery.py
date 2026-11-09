"""
STAGE 10 (v2). Direct measurement of per-stream delivery lag.

Per-SID `seq` cannot pair the two streams: B received 36 messages A did not, so
the counters diverge. Two pairing-free / pairing-safe measurements are used
instead.

(1) EXCHANGE-STAMPED LATENCY. Every orderbook_delta carries the exchange's own
    `ts_ms`. `recv_ts - ts_ms/1000` is that stream's end-to-end latency up to a
    constant local-clock offset, which is common to both streams because both
    ran in one process on one machine. Differences between the two streams'
    latency distributions are therefore real.

(2) PER-MARKET EVENT PAIRING. Within one market both streams see the same
    events in the same order, so the k-th delta on A pairs with the k-th delta
    on B. The pairing is verified event by event -- same exchange ts_ms, same
    side, same canonical price, same delta -- and any market whose sequences do
    not line up is excluded and counted rather than fudged.
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
from collections import defaultdict
from decimal import Decimal

DB = _DB
SP = _WORK


def q(s, p):
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def stats(name, xs, unit="ms"):
    s = sorted(xs)
    print(f"  {name}  n={len(s):,}")
    print(f"    min {q(s,0):8.1f}  p01 {q(s,.01):8.1f}  p25 {q(s,.25):8.1f}  "
          f"median {q(s,.5):8.1f}")
    print(f"    p75 {q(s,.75):8.1f}  p95 {q(s,.95):8.1f}  p99 {q(s,.99):8.1f}  "
          f"p999 {q(s,.999):8.1f}  max {q(s,1):8.1f}  {unit}")


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zd = {r["id"]: bytes(r["zdict"]) for r in con.execute(
        "SELECT id, zdict FROM raw_dictionary")}

    ev = {"A": defaultdict(list), "B": defaultdict(list)}
    lat = {"A": [], "B": []}
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
        lab = row["stream_label"]
        ts_ms = b.get("ts_ms")
        p = int(Decimal(str(b["price_dollars"])) * 10000)
        if b["side"] == "no" and lab == "B":
            p = 10000 - p
        ev[lab][b["market_ticker"]].append(
            (row["recv_ts"], ts_ms, b["side"], p,
             int(Decimal(str(b["delta_fp"])) * 100), row["id"]))
        if isinstance(ts_ms, int):
            lat[lab].append((row["recv_ts"] - ts_ms / 1000.0) * 1000.0)
    con.close()

    print("(1) exchange-stamped end-to-end latency, recv_ts - ts_ms "
          "(common clock offset included)")
    for lab in ("A", "B"):
        stats(f"stream {lab}", lat[lab])
    print(f"    median difference B - A: "
          f"{q(sorted(lat['B']),.5) - q(sorted(lat['A']),.5):+.1f} ms")

    print("\n(2) per-market event pairing")
    paired, skipped, lags = 0, 0, []
    per_market_bad = []
    for tk in ev["A"]:
        a, b = ev["A"][tk], ev["B"].get(tk, [])
        if len(a) != len(b):
            skipped += 1
            per_market_bad.append((tk, len(a), len(b)))
            continue
        ok = all(x[1] == y[1] and x[2] == y[2] and x[3] == y[3] and x[4] == y[4]
                 for x, y in zip(a, b))
        if not ok:
            skipped += 1
            per_market_bad.append((tk, len(a), len(b)))
            continue
        paired += len(a)
        lags.extend((y[0] - x[0]) * 1000.0 for x, y in zip(a, b))
    print(f"  markets paired event-for-event: {len(ev['A'])-skipped:,} of "
          f"{len(ev['A']):,}  (skipped {skipped:,})")
    if per_market_bad[:5]:
        print("  skipped examples (ticker, A deltas, B deltas):")
        for x in per_market_bad[:5]:
            print("   ", x)
    if lags:
        stats("B minus A per event", lags)
        for th in (100, 250, 500, 750, 1000):
            n = sum(1 for x in lags if x > th)
            print(f"    events reaching B more than {th:4d} ms after A: "
                  f"{n:7,}  ({100*n/len(lags):.4f}%)")
        n = sum(1 for x in lags if x < -500)
        print(f"    events reaching A more than  500 ms after B: {n:7,}  "
              f"({100*n/len(lags):.4f}%)")

    with open(SP + "delivery.pkl", "wb") as fh:
        pickle.dump({"lags": lags, "lat": lat}, fh, protocol=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
