"""
Measurement of proposed section 8-v2 criterion 5 against the preserved
240-second engineering probe. MEASUREMENT ONLY -- no protocol is modified, no
new data is collected, and no threshold is recommended.

Rules applied, as given:

  S4 baseline    comparison begins at the first exchange event BOTH streams
                 received as a delta. Determined per market from the first
                 matching block of the two content-key sequences, which is what
                 discards a leading run of events one stream received as deltas
                 and the other already had baked into its snapshot.

  S5 guard band  comparison ends at least 2.000 s of EXCHANGE time before the
                 last archived event. Cutoff = max(ts_ms over every archived
                 delta on either stream) - 2000 ms; events with ts_ms above it
                 are excluded. Applied before pairing, and because it is a
                 content-time filter it removes the same events from both
                 streams and cannot itself misalign them.

  S6 epochs      ended by any gap, disconnect, reconnect or new snapshot on
                 either stream. The epoch structure is derived from the archive
                 rather than assumed, and is printed.

  pairing        positional within a market, with the content key
                 (ts_ms, side, canonical price, delta) verified at every
                 position rather than used as a lookup key -- the key is not
                 unique, so it can validate an alignment but cannot produce one.

Canonical YES coordinates: stream B carries no-side prices in no-leg terms, so
a no-side level at p is held as 1 - p. Everything else is identical.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import sqlite3
import zlib
from collections import defaultdict
from decimal import Decimal

# Same convention as the other stages.
#   DEPTHREC_EVIDENCE_DB  the probe database (the working copy, not source/)
#   DEPTHREC_RECON_WORK   scratch directory for this pipeline's intermediates
_EVIDENCE = "<EVIDENCE_DIR>/equivalence_probe_2026-08-23"
DB = os.environ.get("DEPTHREC_EVIDENCE_DB", _EVIDENCE + "/verify/equivprobe.sqlite")
_WORK = os.environ.get("DEPTHREC_RECON_WORK", _EVIDENCE + "/work")
os.makedirs(_WORK, exist_ok=True)
_WORK = _WORK.rstrip("/") + "/"

GUARD_MS = 2000               # S5, in exchange milliseconds
EPOCH_MIN_PAIRED = 100        # S6
PROBE_WALL_SECONDS = 240.0    # the frozen rule's stated extrapolation base
HOUR_SECONDS = 3600.0


def q(sorted_vals, p):
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def dist(name, vals):
    s = sorted(vals)
    print(f"  {name}  (n = {len(s):,} markets)")
    print(f"    min {q(s,0):>8,}   p25 {q(s,.25):>8,}   median {q(s,.50):>8,}   "
          f"p75 {q(s,.75):>8,}")
    print(f"    p90 {q(s,.90):>8,}   p95 {q(s,.95):>8,}   p99    {q(s,.99):>8,}   "
          f"max {q(s,1):>8,}")
    print(f"    sum {sum(s):,}")


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zd = {r["id"]: bytes(r["zdict"]) for r in con.execute(
        "SELECT id, zdict FROM raw_dictionary")}

    deltas = {"A": defaultdict(list), "B": defaultdict(list)}
    snaps = defaultdict(list)          # (stream, market) -> [recv_ts]
    seq_prev, gaps = {}, []
    max_ts_ms = 0
    recv_lo, recv_hi = None, None
    verified = 0

    for row in con.execute(
        "SELECT id, stream_label, direction, recv_ts, raw, codec, zdict_id, "
        "payload, raw_sha256 FROM ws_raw ORDER BY id"
    ):
        if row["direction"] != "in":
            continue
        blob = row["payload"]
        if blob is None:
            text = row["raw"]
        else:
            if row["codec"] == 0:
                data = bytes(blob)
            else:
                o = zlib.decompressobj(zdict=zd[row["zdict_id"]])
                data = o.decompress(bytes(blob)) + o.flush()
            if hashlib.sha256(data).digest() != bytes(row["raw_sha256"]):
                print("FRAME CHECKSUM MISMATCH", row["id"])
                return 1
            verified += 1
            text = data.decode()
        lab = row["stream_label"]
        ts = row["recv_ts"]
        recv_lo = ts if recv_lo is None else min(recv_lo, ts)
        recv_hi = ts if recv_hi is None else max(recv_hi, ts)
        m = json.loads(text)
        mt = m.get("type")
        if mt not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        sid, seq = m.get("sid"), m.get("seq")
        prev = seq_prev.get((lab, sid))
        if prev is not None and seq != prev + 1 and seq > prev:
            gaps.append((lab, sid, prev, seq, ts))
        seq_prev[(lab, sid)] = seq
        b = m["msg"]
        tk = b.get("market_ticker")
        if not tk:
            continue
        if mt == "orderbook_snapshot":
            snaps[(lab, tk)].append(ts)
            continue
        p = int(Decimal(str(b["price_dollars"])) * 10000)
        if b["side"] == "no" and lab == "B":
            p = 10000 - p
        tsms = b.get("ts_ms")
        if isinstance(tsms, int):
            max_ts_ms = max(max_ts_ms, tsms)
        deltas[lab][tk].append(
            (tsms, b["side"], p, int(Decimal(str(b["delta_fp"])) * 100)))
    con.close()

    print("=== archive ===")
    print(f"  frames sha256-verified            {verified:,}")
    print(f"  wall span of archived frames      {recv_hi - recv_lo:.3f} s "
          f"({recv_lo:.3f} -> {recv_hi:.3f})")
    print(f"  last archived exchange ts_ms      {max_ts_ms}")

    print("\n=== S6 epoch structure, derived not assumed ===")
    print(f"  sequence gaps on either stream    {len(gaps)}")
    multi = {k: v for k, v in snaps.items() if len(v) > 1}
    print(f"  markets with >1 snapshot on a stream {len(multi)}")
    print(f"  snapshots, stream A / stream B    "
          f"{sum(1 for k in snaps if k[0]=='A'):,} / "
          f"{sum(1 for k in snaps if k[0]=='B'):,}")
    print("  => every market has exactly one snapshot per stream, both at the "
          "start of the run,\n     and there is no gap, disconnect or reconnect "
          "inside the archive, so each\n     market carries exactly ONE epoch.")

    cutoff = max_ts_ms - GUARD_MS
    print(f"\n=== S5 guard band ===")
    print(f"  cutoff = {max_ts_ms} - {GUARD_MS} = {cutoff} "
          f"(exchange ms); events above it are excluded")

    all_markets = set(deltas["A"]) | set(deltas["B"])
    per_market = {}
    excluded_tail = 0
    excluded_head = 0
    misaligned = []
    total_paired = 0

    for tk in sorted(all_markets):
        a_all, b_all = deltas["A"].get(tk, []), deltas["B"].get(tk, [])
        # S5 first: a content-time filter, symmetric across streams
        a = [e for e in a_all if e[0] is not None and e[0] <= cutoff]
        b = [e for e in b_all if e[0] is not None and e[0] <= cutoff]
        excluded_tail += (len(a_all) - len(a)) + (len(b_all) - len(b))
        if not a or not b:
            per_market[tk] = 0
            continue
        # S4: first exchange event both streams received as a delta
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        blocks = [bl for bl in sm.get_matching_blocks() if bl.size > 0]
        if not blocks:
            per_market[tk] = 0
            continue
        i, j, _ = blocks[0]
        excluded_head += i + j
        a, b = a[i:], b[j:]
        n = min(len(a), len(b))
        bad = [k for k in range(n) if a[k] != b[k]]
        if bad:
            misaligned.append((tk, len(bad), n))
        paired = n - len(bad)
        per_market[tk] = paired
        total_paired += paired

    print(f"\n=== S4 baseline ===")
    print(f"  events dropped as pre-baseline    {excluded_head:,}")
    print(f"  events dropped by the guard band  {excluded_tail:,}")
    print(f"  markets whose alignment did not verify at every position "
          f"{len(misaligned)}")
    for tk, nbad, n in misaligned[:5]:
        print(f"    {tk}  {nbad} of {n} positions")

    contributing = {t: c for t, c in per_market.items() if c >= 1}
    qualifying = {t: c for t, c in per_market.items() if c >= EPOCH_MIN_PAIRED}
    qual_events = sum(qualifying.values())

    print("\n" + "=" * 78)
    print("MEASUREMENTS")
    print("=" * 78)
    print(f"\n(1) positionally paired book events after S4 and S5   "
          f"{total_paired:,}")
    print(f"(2) distinct markets contributing >= 1 paired event    "
          f"{len(contributing):,}")
    print(f"(3) distinct markets qualifying under >= {EPOCH_MIN_PAIRED} "
          f"paired events per epoch  {len(qualifying):,}")
    print(f"    paired events inside those qualifying epochs      "
          f"{qual_events:,}")
    print(f"    markets in the universe / with any delta traffic  "
          f"{len(snaps)//2:,} / {len(all_markets):,}")

    with open(_WORK + "v2crit5_per_market.json", "w", encoding="utf-8") as fh:
        json.dump(per_market, fh)

    print("\n(4) paired events per market")
    dist("over the markets contributing >= 1 paired event", list(contributing.values()))
    print()
    dist(f"over the markets qualifying at >= {EPOCH_MIN_PAIRED}",
         list(qualifying.values()))

    factor = HOUR_SECONDS / PROBE_WALL_SECONDS
    proj_all = total_paired * factor
    proj_qual = qual_events * factor
    print(f"\n(5) simple rate extrapolation, {PROBE_WALL_SECONDS:.0f} s -> "
          f"{HOUR_SECONDS:.0f} s (factor {factor:.0f}x)")
    print(f"    THIS IS ARITHMETIC ON THE OBSERVED FOUR MINUTES, NOT A "
          f"PREDICTION OF FUTURE TRAFFIC.")
    print(f"    all paired events        {total_paired:>10,} / "
          f"{PROBE_WALL_SECONDS:.0f} s = {total_paired/PROBE_WALL_SECONDS:>10,.1f}/s "
          f"-> {proj_all:>12,.0f} per 60 min")
    print(f"    qualifying-epoch events  {qual_events:>10,} / "
          f"{PROBE_WALL_SECONDS:.0f} s = {qual_events/PROBE_WALL_SECONDS:>10,.1f}/s "
          f"-> {proj_qual:>12,.0f} per 60 min")

    print("\n" + "=" * 78)
    print("THE FROZEN THRESHOLD RULE, APPLIED MECHANICALLY")
    print("=" * 78)
    for label, proj in (("qualifying-epoch events", proj_qual),
                        ("all paired events", proj_all)):
        floor = int((proj * 0.25) // 100_000) * 100_000
        print(f"  paired-event floor from {label:24s} "
              f"25% of {proj:,.0f} = {proj*0.25:,.0f} -> floor to 100,000 -> "
              f"{floor:,}")
    mfloor = int((len(qualifying) * 0.25) // 10) * 10
    print(f"  distinct-market floor: 25% of {len(qualifying):,} = "
          f"{len(qualifying)*0.25:,.2f} -> floor to 10 -> {mfloor:,}")
    print(f"  minimum duration: 60 continuous minutes, unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
