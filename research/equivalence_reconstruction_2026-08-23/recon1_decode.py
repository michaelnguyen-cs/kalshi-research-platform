"""
STAGE 1 of the independent reconstruction of the 240 s engineering probe.

Rebuilds each stream's canonical YES top-of-book timeline from `ws_raw` alone,
using each row's persisted local receive timestamp.

WHAT IS SHARED WITH PRODUCTION CODE: nothing. Not one symbol is imported from
`depthrec`. Frame storage is undone with stdlib `zlib` against the dictionary
blob stored in the database's own `raw_dictionary` table, and every frame is
checked against its stored SHA-256 of the ORIGINAL bytes -- so "did I get the
frame the recorder received?" is answered cryptographically rather than by
trusting a shared reader.

WHAT IS INDEPENDENTLY IMPLEMENTED HERE: JSON framing, subscription/sid tracking,
per-sid sequence validity, the yes/no -> canonical-YES coordinate transform,
snapshot replacement, delta application, ladder bookkeeping, book invariants,
top-of-book extraction, and the validity state machine. All of it written
against the Kalshi wire format, not copied from depthrec.book/session.

Output: a pickle of per-(stream, ticker) observable-state timelines.
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
import sys
import zlib
from collections import defaultdict
from decimal import Decimal

DB = _DB
OUT = _WORK + "timelines.pkl"

PRICE_SCALE = 10_000
QTY_SCALE = 100
MIN_PRICE, MAX_PRICE = 1, PRICE_SCALE - 1
STALE_MAX_AGE = 30.0            # session-activity staleness limit (frozen)

# Non-VALID observable states. Kept as distinct sentinels so a grid cell can say
# WHY it was not comparable, which is what separates "unmatched" from
# "disagreement" downstream.
NO_SNAPSHOT = "NO_SNAPSHOT"
SEQ_GAP = "SEQ_GAP"
BOOK_ERROR = "BOOK_ERROR"


class Bad(ValueError):
    pass


def px(raw) -> int:
    """Dollar string -> tenths of a cent. Decimal, never float."""
    d = Decimal(str(raw)) * PRICE_SCALE
    if d != d.to_integral_value():
        raise Bad(f"price {raw!r} finer than a tenth of a cent")
    return int(d)


def qt(raw) -> int:
    d = Decimal(str(raw)) * QTY_SCALE
    if d != d.to_integral_value():
        raise Bad(f"qty {raw!r} finer than 0.01 contracts")
    return int(d)


class Stream:
    """
    One WebSocket stream, decoded independently.

    `use_yes_price` is the only difference between A and B: when it is false the
    server prices the NO ladder in no-leg terms, so a no-side level at p is the
    same resting interest as a yes-leg level at 1 - p. Both streams are then
    held in identical canonical YES coordinates:

        yes-side levels  -> the YES BID ladder  (best = highest price)
        no-side  levels  -> the YES ASK ladder  (best = lowest price)
    """

    def __init__(self, label: str, use_yes_price: bool) -> None:
        self.label = label
        self.uyp = use_yes_price
        self.bids: dict[str, dict[int, int]] = {}
        self.asks: dict[str, dict[int, int]] = {}
        self.has_snap: set[str] = set()
        self.invalid: dict[str, str] = {}      # ticker -> sentinel
        self.sid_of: dict[str, object] = {}    # ticker -> sid its snapshot came on
        self.seq_by_sid: dict[object, int] = {}
        self.last_activity: float | None = None
        self.timeline: dict[str, list] = defaultdict(list)
        self.gaps: list[tuple[float, object, int, int]] = []
        self.replays = 0
        self.book_errors: list[tuple[float, str, str]] = []
        self.max_frame_gap = 0.0
        self.snapshots = 0
        self.deltas = 0
        self.frames = 0

    # -- observable state -------------------------------------------------

    def top(self, ticker: str):
        b, a = self.bids.get(ticker), self.asks.get(ticker)
        bp = max(b) if b else None
        ap = min(a) if a else None
        return (bp, b[bp] if bp is not None else None,
                ap, a[ap] if ap is not None else None)

    def state(self, ticker: str):
        """None-free observable state: a sentinel string, or the top tuple."""
        if ticker in self.invalid:
            return self.invalid[ticker]
        if ticker not in self.has_snap:
            return NO_SNAPSHOT
        return self.top(ticker)

    def emit(self, ticker: str, ts: float) -> None:
        """Append a change point, collapsing identical consecutive states."""
        st = self.state(ticker)
        tl = self.timeline[ticker]
        if tl and tl[-1][1] == st:
            return
        tl.append((ts, st))

    # -- invariants -------------------------------------------------------

    def check(self, ticker: str) -> None:
        b, a = self.bids.get(ticker, {}), self.asks.get(ticker, {})
        for name, lad in (("bid", b), ("ask", a)):
            for p, q in lad.items():
                if not (MIN_PRICE <= p <= MAX_PRICE):
                    raise Bad(f"{name} price {p} out of range")
                if q <= 0:
                    raise Bad(f"{name} level {p} qty {q}")
        ov = set(b) & set(a)
        if ov:
            raise Bad(f"levels {sorted(ov)} on both sides")
        if b and a and max(b) > min(a):
            raise Bad(f"crossed: bid {max(b)} > ask {min(a)}")

    # -- message handling -------------------------------------------------

    def invalidate_sid(self, sid, reason: str, ts: float) -> None:
        for tk, s in self.sid_of.items():
            if s == sid and tk not in self.invalid:
                self.invalid[tk] = reason
                self.emit(tk, ts)

    def feed(self, ts: float, text: str) -> None:
        self.frames += 1
        if self.last_activity is not None:
            self.max_frame_gap = max(self.max_frame_gap, ts - self.last_activity)
        self.last_activity = ts

        try:
            m = json.loads(text)
        except json.JSONDecodeError:
            return
        mt = m.get("type")
        if mt == "subscribed" or mt == "error":
            return
        sid, seq = m.get("sid"), m.get("seq")
        if sid is None or not isinstance(seq, int) or isinstance(seq, bool):
            return

        # Per-SID sequence continuity, independently implemented. A gap
        # invalidates every book on that subscription until a fresh snapshot
        # repairs it; a repeated or older seq is a replay, not a gap.
        prev = self.seq_by_sid.get(sid)
        if prev is not None and seq != prev + 1:
            if seq <= prev:
                self.replays += 1
                return
            self.gaps.append((ts, sid, prev, seq))
            self.invalidate_sid(sid, SEQ_GAP, ts)
            self.seq_by_sid[sid] = seq
            return
        self.seq_by_sid[sid] = seq

        body = m.get("msg") or {}
        tk = body.get("market_ticker")
        if not tk:
            return

        try:
            if mt == "orderbook_snapshot":
                self.apply_snapshot(tk, body, sid)
                self.snapshots += 1
            elif mt == "orderbook_delta":
                if tk in self.invalid or tk not in self.has_snap:
                    return          # no book to apply to; waits for a snapshot
                self.apply_delta(tk, body)
                self.deltas += 1
            else:
                return
        except Bad as exc:
            self.invalid[tk] = BOOK_ERROR
            self.book_errors.append((ts, tk, str(exc)))
            self.emit(tk, ts)
            return

        self.emit(tk, ts)

    def apply_snapshot(self, tk: str, body: dict, sid) -> None:
        bids: dict[int, int] = {}
        asks: dict[int, int] = {}
        for wire_side, key in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
            lad = bids if wire_side == "yes" else asks
            for level in body.get(key) or ():
                if not isinstance(level, (list, tuple)) or len(level) != 2:
                    raise Bad(f"malformed {key} level {level!r}")
                p = px(level[0])
                if wire_side == "no" and not self.uyp:
                    p = PRICE_SCALE - p
                q = qt(level[1])
                if q < 0:
                    raise Bad("negative snapshot qty")
                if q == 0:
                    continue
                if p in lad:
                    raise Bad(f"duplicate {wire_side} level {p}")
                lad[p] = q
        self.bids[tk], self.asks[tk] = bids, asks
        self.has_snap.add(tk)
        self.invalid.pop(tk, None)
        self.sid_of[tk] = sid
        self.check(tk)

    def apply_delta(self, tk: str, body: dict) -> None:
        side = body.get("side")
        if side not in ("yes", "no"):
            raise Bad(f"delta side {side!r}")
        p = px(body.get("price_dollars"))
        if side == "no" and not self.uyp:
            p = PRICE_SCALE - p
        d = qt(body.get("delta_fp"))
        lad = self.bids[tk] if side == "yes" else self.asks[tk]
        new = lad.get(p, 0) + d
        if new < 0:
            raise Bad(f"{side} level {p} driven to {new}")
        if new == 0:
            lad.pop(p, None)
        else:
            lad[p] = new
        self.check(tk)


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zdicts = {r["id"]: bytes(r["zdict"])
              for r in con.execute("SELECT id, zdict FROM raw_dictionary")}
    print(f"dictionaries: {len(zdicts)}")

    streams = {"A": Stream("A", True), "B": Stream("B", False)}
    verified = 0
    n = 0
    # `id` is the writer's submission order, which is per-stream arrival order;
    # recv_ts monotonicity within each stream is asserted below.
    last_ts = {"A": 0.0, "B": 0.0}
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
                o = zlib.decompressobj(zdict=zdicts[row["zdict_id"]])
                data = o.decompress(bytes(blob)) + o.flush()
            want = bytes(row["raw_sha256"] or b"")
            if want and hashlib.sha256(data).digest() != want:
                print(f"FRAME CHECKSUM MISMATCH at id={row['id']}", file=sys.stderr)
                return 1
            verified += 1
            text = data.decode("utf-8")
        lab = row["stream_label"]
        ts = row["recv_ts"]
        if ts < last_ts[lab]:
            print(f"NON-MONOTONIC recv_ts on {lab} at id={row['id']}", file=sys.stderr)
            return 1
        last_ts[lab] = ts
        streams[lab].feed(ts, text)
        n += 1
        if n % 100_000 == 0:
            print(f"  {n:,} frames")

    for lab, s in streams.items():
        print(f"stream {lab}: frames={s.frames:,} snapshots={s.snapshots:,} "
              f"deltas={s.deltas:,} tickers={len(s.timeline):,} "
              f"seq_gaps={len(s.gaps)} replays={s.replays} "
              f"book_errors={len(s.book_errors)} "
              f"max_frame_gap={s.max_frame_gap:.3f}s")
        if s.book_errors[:3]:
            for e in s.book_errors[:3]:
                print("   book error:", e)
        if s.gaps[:3]:
            for g in s.gaps[:3]:
                print("   seq gap:", g)

    grid = [r[0] for r in con.execute(
        "SELECT DISTINCT grid_ts FROM equivalence_comparison ORDER BY grid_ts")]
    tickers = sorted({r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM equivalence_comparison")})
    con.close()

    print(f"frames sha256-verified: {verified:,}")
    print(f"grid ticks: {len(grid)}  tickers: {len(tickers)}")

    with open(OUT, "wb") as fh:
        pickle.dump({
            "grid": grid,
            "tickers": tickers,
            "timelines": {lab: dict(s.timeline) for lab, s in streams.items()},
            "stats": {lab: {"frames": s.frames, "snapshots": s.snapshots,
                            "deltas": s.deltas, "gaps": s.gaps,
                            "replays": s.replays, "book_errors": s.book_errors,
                            "max_frame_gap": s.max_frame_gap,
                            "last_activity": s.last_activity}
                      for lab, s in streams.items()},
        }, fh, protocol=4)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
