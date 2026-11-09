#!/usr/bin/env python
"""
Reconstruct section 5's quantities from a recorder database, offline.

    python scripts/depthrec_report.py --db /mnt/depthrec/depthrec_pilot.sqlite
    python scripts/depthrec_report.py --db /mnt/depthrec/depthrec_pilot.sqlite --verify-replay

Nothing here talks to Kalshi and nothing here needs the recorder process. ALL,
VALID, NON-TRADEABLE and ELIGIBLE are rebuilt by joining `classification`
against `signal_observation`, which is the point of persisting both: the census
says what existed, the classifier says what fired, and the book says what could
have executed -- three separately checkable records rather than one derived
number.

Only the four inspectable quantities are computed: raw availability, technical
availability, depth clearance D and effective executable yield Y, plus health
and the itemised UNAVAILABLE reasons. Settlements, EV, winners, losers, markouts
and series-specific profitability are section 9's may-not-inspect list and this
script has no code that could produce them.

--verify-replay rebuilds every observation's top of book from `ws_raw` alone --
the untouched frames, replayed in receive order through the snapshot/delta
reconstruction -- and compares it against the `book_json` the recorder stored.
Agreement proves the raw retention is SUFFICIENT: the frozen data really can be
replayed. Swap the decoder below for an independently written one and the same
harness becomes the independent check section 7 asks for.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.book import BookError, CanonicalBook, contracts_to_qty  # noqa: E402
from depthrec.recorder import bucket_for  # noqa: E402
from depthrec.store import read_raw_text  # noqa: E402


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM recorder_run ORDER BY started_ts"))


def report(conn: sqlite3.Connection, run_id: str) -> dict:
    rows = list(conn.execute(
        "SELECT c.ticker, c.series, c.side, o.validity, o.unavailable_reason,"
        "       o.subscribed, o.yes_ask_price, o.yes_ask_size_fp,"
        "       o.no_ask_price, o.no_ask_size_fp"
        "  FROM classification c"
        "  LEFT JOIN signal_observation o"
        "    ON o.run_id = c.run_id AND o.ticker = c.ticker"
        " WHERE c.run_id = ? AND c.is_firing = 1",
        (run_id,),
    ))

    buckets = {b: 0 for b in frozen.BUCKETS}
    reasons = {r: 0 for r in frozen.UNAVAILABLE_REASONS}
    by_side = defaultdict(int)
    unsubscribed_but_counted = 0

    for row in rows:
        side = row["side"] or ""
        by_side[side or "(none)"] += 1
        # The side the rule buys decides WHICH ladder's depth is the executable
        # one. bucket_for() is imported from the recorder so an offline
        # reconstruction cannot disagree with the run that produced the data.
        price = row["no_ask_price"] if side == "NO" else row["yes_ask_price"]
        size = row["no_ask_size_fp"] if side == "NO" else row["yes_ask_size_fp"]
        bucket, reason, _ = bucket_for(
            validity=row["validity"], unavailable_reason=row["unavailable_reason"],
            side=side, exec_price=price, exec_size_fp=size,
            x_contracts=frozen.X_CONTRACTS,
        )
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if bucket == frozen.BUCKET_UNAVAILABLE:
            reasons[reason] = reasons.get(reason, 0) + 1
            if not row["subscribed"]:
                unsubscribed_but_counted += 1

    total = len(rows)
    valid = buckets[frozen.BUCKET_NON_TRADEABLE] + buckets[frozen.BUCKET_ELIGIBLE]
    eligible = buckets[frozen.BUCKET_ELIGIBLE]
    maintenance = reasons.get(frozen.UNAVAILABLE_SCHEDULED_MAINTENANCE, 0)

    classified = conn.execute(
        "SELECT COUNT(*) FROM classification WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    census_n = conn.execute(
        "SELECT COUNT(*) FROM census_market WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    census_fail = conn.execute(
        "SELECT COUNT(*) FROM census_request WHERE run_id=? AND ok=0", (run_id,)
    ).fetchone()[0]
    census_ok = conn.execute(
        "SELECT COUNT(*) FROM census_request WHERE run_id=? AND ok=1", (run_id,)
    ).fetchone()[0]
    raw_n = conn.execute(
        "SELECT COUNT(*) FROM ws_raw WHERE run_id=?", (run_id,)
    ).fetchone()[0]

    health = defaultdict(int)
    for row in conn.execute(
        "SELECT kind, COUNT(*) n FROM health_event WHERE run_id=? GROUP BY kind",
        (run_id,),
    ):
        health[row["kind"]] = row["n"]

    return {
        "run_id": run_id,
        "all_otherwise_firing": total,
        "buckets": buckets,
        "buckets_sum_to_all": sum(buckets.values()) == total,
        "unavailable_by_reason": reasons,
        "raw_availability": (valid / total) if total else None,
        "technical_availability": (
            valid / (total - maintenance) if total - maintenance > 0 else None
        ),
        "depth_clearance_D": (eligible / valid) if valid else None,
        "effective_executable_yield_Y": (eligible / total) if total else None,
        "firing_by_side": dict(by_side),
        "counted_though_never_subscribed": unsubscribed_but_counted,
        "census": {
            "markets": census_n,
            "requests_ok": census_ok,
            "requests_failed": census_fail,
        },
        "classified_markets": classified,
        "raw_frames_retained": raw_n,
        "health_events": dict(health),
    }


def verify_replay(conn: sqlite3.Connection, run_id: str) -> dict:
    """
    Rebuild every observation's top of book from ws_raw alone.

    Replays each stream's frames in receive order, honouring the sid/seq rules,
    and snapshots the reconstructed top of book at each observation's raw_last_id
    watermark. Compares against the stored book_json.
    """
    watermarks: dict[int, list[sqlite3.Row]] = defaultdict(list)
    checked = agree = skipped = 0
    mismatches = []
    for row in conn.execute(
        "SELECT ticker, book_json, raw_last_id, validity FROM signal_observation"
        " WHERE run_id=? AND raw_last_id IS NOT NULL AND book_json != ''",
        (run_id,),
    ):
        watermarks[row["raw_last_id"]].append(row)
    if not watermarks:
        return {"checked": 0, "note": "no observations carry a raw watermark"}

    use_yes_price = {}
    for row in conn.execute(
        "SELECT stream_label, use_yes_price FROM ws_session WHERE run_id=?", (run_id,)
    ):
        use_yes_price[row["stream_label"]] = bool(row["use_yes_price"])

    # The archive is self-describing: its compression dictionaries live in its
    # own raw_dictionary table, so replay needs nothing but the file.
    have_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    zdicts: dict[int, bytes] = {}
    if "raw_dictionary" in have_tables:
        zdicts = {r["id"]: bytes(r["zdict"])
                  for r in conn.execute("SELECT id, zdict FROM raw_dictionary")}

    books: dict[tuple[str, int, str], CanonicalBook] = {}
    seq_by: dict[tuple[str, int, object], int] = {}
    broken: set[tuple[str, int, str]] = set()

    # Schema 2 databases have no codec/payload columns. Select only what the
    # file actually has, so an archive recorded before compression landed
    # replays through exactly this command with no workflow change.
    have = {r[1] for r in conn.execute("PRAGMA table_info(ws_raw)")}
    cols = [c for c in ("id", "stream_label", "session_epoch", "direction", "raw",
                        "codec", "zdict_id", "payload", "raw_sha256", "raw_len")
            if c in have]

    verified = 0
    for row in conn.execute(
        f"SELECT {', '.join(cols)} FROM ws_raw WHERE run_id=? ORDER BY id", (run_id,),
    ):
        if row["direction"] == "in":
            # verify=True on every frame: each read re-proves that the stored
            # bytes decompress to exactly the bytes that arrived. That proof is
            # the whole point of compressing the archive at all.
            text = read_raw_text(row, zdicts, verify=True)
            verified += 1
            _replay_frame(row, text, books, seq_by, broken,
                          use_yes_price.get(row["stream_label"], True))
        for obs in watermarks.get(row["id"], ()):
            checked += 1
            stored = json.loads(obs["book_json"])
            key = (row["stream_label"], row["session_epoch"], obs["ticker"])
            book = books.get(key)
            if book is None or key in broken:
                skipped += 1
                continue
            rebuilt = {
                "bids": [list(x) for x in sorted(book.bids.items(), reverse=True)],
                "asks": [list(x) for x in sorted(book.asks.items())],
            }
            if rebuilt == stored:
                agree += 1
            else:
                mismatches.append({"ticker": obs["ticker"],
                                   "stored": stored, "replayed": rebuilt})

    return {
        "checked": checked,
        "agree": agree,
        "skipped_no_replayed_book": skipped,
        "mismatches": len(mismatches),
        "frames_decoded_and_checksum_verified": verified,
        "dictionaries": len(zdicts),
        "examples": mismatches[:5],
    }


def _replay_frame(row, text, books, seq_by, broken, use_yes_price: bool) -> None:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return

    label, epoch = row["stream_label"], row["session_epoch"]
    sid, seq = msg.get("sid"), msg.get("seq")
    mtype = msg.get("type")

    # Sequence bookkeeping FIRST, and for every message that carries sid+seq --
    # not just the two orderbook types. `ok` responses to update_subscription
    # carry a seq on the same per-subscription counter, and skipping them here
    # made two mid-session add/delete_markets acks look like two forward jumps,
    # falsely breaking every book in that session. The live path counts them, so
    # this one must too: an independent decoder that counts differently is not
    # an independent check, it is a second bug.
    if sid is not None and isinstance(seq, int) and not isinstance(seq, bool):
        skey = (label, epoch, sid)
        previous = seq_by.get(skey)
        if previous is not None and seq != previous + 1:
            if seq <= previous:
                # A repeat or an older seq. Nothing is missing, and the counter
                # must NOT rewind -- rewinding turns the next normal message
                # into a fabricated gap. Mirrors ResearchSession._check_sequence.
                return
            for key in list(books):
                if key[0] == label and key[1] == epoch:
                    broken.add(key)
        seq_by[skey] = seq

    if mtype not in ("orderbook_snapshot", "orderbook_delta"):
        return
    body = msg.get("msg") or {}
    ticker = body.get("market_ticker")
    if ticker is None or not isinstance(seq, int):
        return

    key = (label, epoch, ticker)
    try:
        if mtype == "orderbook_snapshot":
            book = CanonicalBook(ticker=ticker)
            book.apply_snapshot(body, seq=seq, sid=sid, recv_ts=0.0,
                                use_yes_price=use_yes_price)
            books[key] = book
            broken.discard(key)
        else:
            book = books.get(key)
            if book is None or key in broken:
                return
            book.apply_delta(body, seq=seq, recv_ts=0.0, use_yes_price=use_yes_price)
    except BookError:
        broken.add(key)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--run-id", default=None, help="default: the most recent run")
    ap.add_argument("--verify-replay", action="store_true",
                    help="rebuild every observation's book from ws_raw and compare")
    args = ap.parse_args()

    conn = connect(args.db)
    all_runs = runs(conn)
    if not all_runs:
        print("no recorder_run rows in this database", file=sys.stderr)
        return 1
    run = next((r for r in all_runs if r["run_id"] == args.run_id), all_runs[-1])

    print(f"run_id        : {run['run_id']}")
    print(f"git_commit    : {run['git_commit']}")
    print(f"api_env       : {run['api_env']}  {run['base_url']}")
    print(f"doc           : {run['doc_reference']}")
    print(f"frozen config : {run['frozen_config']}")
    print(f"integrity     : {run['integrity']}")
    print(f"X             : {frozen.X_CONTRACTS} contracts "
          f"({contracts_to_qty(frozen.X_CONTRACTS)} in fixed point)")
    print()
    print(json.dumps(report(conn, run["run_id"]), indent=2, default=str))

    if args.verify_replay:
        print("\nraw replay verification:")
        print(json.dumps(verify_replay(conn, run["run_id"]), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
