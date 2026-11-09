"""
Archive-then-purge-local.

The property under test is that raw evidence cannot leave the host by accident.
Section 8 keeps validation data out of the statistical sample; section 7 keeps
raw frames as the provenance behind the verdict. Those are different claims, and
the second one survives the first -- so every gate has to hold before a row is
dropped, and each gate is tested by making it, and only it, fail.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec.archive import ArchiveRefused, archive_and_purge, sha256_file  # noqa: E402
from depthrec.rawwriter import RawWriter  # noqa: E402
from depthrec.store import RunContext, Store, read_raw_text  # noqa: E402

RUN = "equiv-1"


def frame(i: int) -> str:
    return json.dumps({
        "type": "orderbook_delta", "sid": 1, "seq": i,
        "msg": {"market_ticker": "KXBTC15M-26AUG220730-30", "side": "yes",
                "price_dollars": f"0.{(i % 90) + 5:02d}00", "delta_fp": "3.00"},
    })


def build(tmp_path: Path, *, with_verdict: bool = True, frames: int = 2000) -> Path:
    db = tmp_path / "src" / "equiv.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db, RunContext(RUN, "cafe1234"))
    store.record_run(started_ts=0.0, api_env="prod", base_url="b", ws_url="w",
                     frozen_config={"x": 10}, integrity={"ok": True}, doc_reference="d")
    store.record_session(session_id="s", stream_label="A", session_epoch=1,
                         use_yes_price=True, channels=["orderbook_delta"],
                         subscribe_params={"market_tickers": ["T1"]},
                         ws_url="w", opened_ts=0.0)
    w = RawWriter(store, compress=True, max_pending=10_000_000)
    w.start()
    for i in range(frames):
        w.submit(stream_label="A", session_epoch=1, direction="in",
                 recv_ts=1000.0 + i, raw=frame(i))
    w.stop(timeout=120)
    store.record_comparisons([
        {"grid_ts": 100.0 + i, "ticker": "T1", "agree": 1,
         "a_bid": 6800, "a_bid_qty": 500, "a_ask": 7100, "a_ask_qty": 4000,
         "b_bid": 6800, "b_bid_qty": 500, "b_ask": 7100, "b_ask_qty": 4000,
         "resolution": "agree", "skew_ms": None} for i in range(50)
    ])
    if with_verdict:
        store.record_equivalence_result({
            "started_ts": 0.0, "ended_ts": 3600.0, "minutes": 60.0,
            "comparisons": 10_000, "agreements": 10_000, "disagreements": 0,
            "skew_explained": 0, "unexplained": 0, "agreement_rate": 1.0,
            "verdict": "PASS", "diagnostics": json.dumps({"ticks": 3600}),
            "params": json.dumps({"grid_hz": 1.0}),
        })
    store.checkpoint_truncate()
    store.close()
    return db


def rows(db: Path, table: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# -- The gates ------------------------------------------------------------------

def test_all_gates_pass_on_a_complete_run(tmp_path):
    db = build(tmp_path)
    report = archive_and_purge(db, tmp_path / "archive", allow_same_volume=True)
    assert report.ok, [g.name for g in report.gates if not g.passed]
    assert [g.name for g in report.gates] == [
        "verdict_row_exists", "replay_verified", "archive_location_separate",
        "archive_copied", "archive_hash_recorded", "copy_reread_and_matched",
    ]
    assert report.verdict == "PASS"
    assert report.purged is False, "no purge unless asked"
    assert rows(db, "ws_raw") == 2000


def test_no_verdict_row_blocks_everything(tmp_path):
    """
    Raw is provenance FOR a verdict. With no verdict there is nothing the
    evidence is supporting yet, and no reason to be moving it off the host.
    """
    db = build(tmp_path, with_verdict=False)
    report = archive_and_purge(db, tmp_path / "archive", allow_same_volume=True)
    assert not report.ok
    failed = {g.name for g in report.gates if not g.passed}
    assert "verdict_row_exists" in failed
    assert not (tmp_path / "archive").exists()
    assert rows(db, "ws_raw") == 2000


def test_no_verdict_row_makes_purge_raise(tmp_path):
    db = build(tmp_path, with_verdict=False)
    with pytest.raises(ArchiveRefused, match="verdict_row_exists"):
        archive_and_purge(db, tmp_path / "archive", purge=True, allow_same_volume=True)
    assert rows(db, "ws_raw") == 2000


def test_a_corrupted_frame_fails_replay_verification_and_blocks_purge(tmp_path):
    db = build(tmp_path)
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT id, payload FROM ws_raw WHERE codec!=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    blob = bytearray(row[1])
    blob[len(blob) // 2] ^= 0xFF
    conn.execute("UPDATE ws_raw SET payload=? WHERE id=?", (bytes(blob), row[0]))
    conn.commit()
    conn.close()

    with pytest.raises(ArchiveRefused, match="replay_verified"):
        archive_and_purge(db, tmp_path / "archive", purge=True, allow_same_volume=True)
    assert rows(db, "ws_raw") == 2000


def test_the_archive_may_not_sit_in_the_databases_own_directory(tmp_path):
    """A copy beside the original survives neither a disk failure nor the purge
    that is about to free the space."""
    db = build(tmp_path)
    report = archive_and_purge(db, db.parent)
    assert not report.ok
    assert "archive_location_separate" in {g.name for g in report.gates if not g.passed}


def test_an_empty_archive_is_refused(tmp_path):
    """Nothing to archive is not the same as a successful archive."""
    db = build(tmp_path, frames=0)
    report = archive_and_purge(db, tmp_path / "archive", allow_same_volume=True)
    assert not report.ok
    assert any(g.name == "replay_verified" and "no raw frames" in g.detail
               for g in report.gates)


# -- The copy is proved, not assumed ---------------------------------------------

def test_the_copy_is_reread_from_disk_and_matched(tmp_path):
    db = build(tmp_path)
    report = archive_and_purge(db, tmp_path / "archive", allow_same_volume=True)
    archive = Path(report.archive_path)
    assert archive.exists()
    # Recomputed outside the code under test.
    assert sha256_file(archive) == report.archive_sha256 == report.source_sha256
    assert report.reread_sha256 == report.source_sha256
    # The SOURCE has moved on since the copy -- the raw_archive bookkeeping row
    # was written into it. That is expected, and it is why the proof is a
    # re-read of the destination rather than a later re-hash of the source.
    assert sha256_file(db) != report.archive_sha256
    assert rows(db, "raw_archive") == 1


def test_the_archive_is_a_complete_replayable_copy(tmp_path):
    """
    Not an extract. The archive must decode on its own, with its own
    dictionaries, byte for byte.
    """
    db = build(tmp_path)
    report = archive_and_purge(db, tmp_path / "archive", purge=True, allow_same_volume=True)
    assert report.purged

    conn = sqlite3.connect(f"file:{report.archive_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        zdicts = {r["id"]: bytes(r["zdict"])
                  for r in conn.execute("SELECT id, zdict FROM raw_dictionary")}
        got = [read_raw_text(r, zdicts, verify=True)
               for r in conn.execute("SELECT * FROM ws_raw ORDER BY id")]
    finally:
        conn.close()
    assert got == [frame(i) for i in range(2000)]


# -- What the purge removes, and what it must not ---------------------------------

def test_purge_removes_raw_and_keeps_everything_that_says_what_happened(tmp_path):
    db = build(tmp_path)
    before = db.stat().st_size
    report = archive_and_purge(db, tmp_path / "archive", purge=True, allow_same_volume=True)
    assert report.purged

    assert rows(db, "ws_raw") == 0
    assert rows(db, "raw_dictionary") == 0
    # Everything that states the conclusion, or points at the evidence, stays.
    assert rows(db, "equivalence_result") == 1
    assert rows(db, "equivalence_comparison") == 50
    assert rows(db, "recorder_run") == 1
    assert rows(db, "ws_session") == 1
    assert rows(db, "raw_archive") == 1
    assert db.stat().st_size < before


def test_the_retained_row_carries_location_hash_commit_and_verdict(tmp_path):
    db = build(tmp_path)
    report = archive_and_purge(db, tmp_path / "archive", purge=True,
                               allow_same_volume=True, git_commit="abc123def")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM raw_archive").fetchone()
        diag = conn.execute("SELECT diagnostics, verdict FROM equivalence_result").fetchone()
        purged = conn.execute(
            "SELECT value FROM meta WHERE key='raw_purged_locally'").fetchone()
    finally:
        conn.close()
    assert row["archive_path"] == report.archive_path
    assert row["archive_sha256"] == report.archive_sha256
    assert row["source_sha256"] == row["reread_sha256"] == row["archive_sha256"]
    assert row["git_commit"] == "abc123def"
    assert row["verdict"] == "PASS"
    assert row["raw_rows"] == 2000 and row["raw_bytes"] > 0
    assert row["purged_ts"] is not None
    assert json.loads(diag["diagnostics"])["ticks"] == 3600
    assert diag["verdict"] == "PASS"
    assert purged["value"] == "1"


def test_nothing_is_purged_when_purge_is_not_requested(tmp_path):
    db = build(tmp_path)
    report = archive_and_purge(db, tmp_path / "archive", allow_same_volume=True)
    assert report.ok and not report.purged
    assert rows(db, "ws_raw") == 2000
    assert rows(db, "raw_dictionary") > 0
