"""
Segment lifecycle: COMPLETE is earned, finalized archives are immutable, and a
segment left open by a dead process is labelled INCOMPLETE_CRASH rather than
quietly passed off as a finished day.

Also pinned here: a manifest carries every field the frozen spec requires, and
carries nothing that describes data. A manifest that grew a price, a spread, a
depth statistic or a per-market activity count would turn an operational record
into a research artifact, which is exactly what this recorder must not produce.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from depthrec.rawwriter import RawWriter  # noqa: E402
from depthrec.store import read_raw_text  # noqa: E402
from threadc import frozen, segment as sg  # noqa: E402
from threadc.universe import AppendOnlyUniverse  # noqa: E402
from threadc_fakes import frame  # noqa: E402


@pytest.fixture
def identity(tmp_path):
    return AppendOnlyUniverse(tmp_path).identity()


def open_seg(tmp_path, identity, *, now=None, run_id="run-1"):
    return sg.open_segment(
        tmp_path, run_id=run_id, git_commit="cafebabe",
        mode=frozen.MODE_PRODUCTION, now=now, universe_identity=identity,
    )


def write_frames(seg, n: int) -> RawWriter:
    w = RawWriter(seg.store, max_pending=10_000_000)
    w.start()
    for i in range(n):
        w.submit(stream_label="S00", session_epoch=1, direction="in",
                 recv_ts=1000.0 + i, raw=frame("KXMLBGAME-M01", i))
    w.stop()
    return w


# -- Clean COMPLETE finalization ---------------------------------------------


def test_clean_finalization_is_complete_and_verifiable(tmp_path, identity):
    seg = open_seg(tmp_path, identity)
    write_frames(seg, 40)

    m = sg.finalize(seg, state=frozen.SEGMENT_COMPLETE,
                    close_reason=frozen.CLOSE_ROTATION_UTC_MIDNIGHT,
                    universe_identity=identity)

    assert m["segment_state"] == frozen.SEGMENT_COMPLETE
    assert m["integrity_check"] == "ok"
    assert m["raw_frame_count"] == 40
    assert m["byte_size"] == seg.db_path.stat().st_size
    assert m["sha256"] == frozen.sha256_file(seg.db_path)
    # Every finalization step ran and none failed.
    for step in ("metadata", "wal_checkpoint_truncate", "database_close",
                 "integrity_check", "sha256"):
        assert not str(m["finalization_steps"][step]).startswith("failed"), step
    # The WAL was checkpointed away, so the .sqlite file is self-contained.
    assert not Path(str(seg.db_path) + "-wal").exists()


def test_frames_survive_finalization_byte_for_byte(tmp_path, identity):
    seg = open_seg(tmp_path, identity)
    originals = [frame("KXMLBGAME-M01", i) for i in range(600)]
    w = RawWriter(seg.store, max_pending=10_000_000)
    w.start()
    for i, f in enumerate(originals):
        w.submit(stream_label="S00", session_epoch=1, direction="in",
                 recv_ts=1000.0 + i, raw=f)
    w.stop()
    sg.finalize(seg, state=frozen.SEGMENT_COMPLETE, close_reason="test",
                universe_identity=identity)

    conn = sqlite3.connect(str(seg.db_path))
    conn.row_factory = sqlite3.Row
    zdicts = {r["id"]: bytes(r["zdict"])
              for r in conn.execute("SELECT id, zdict FROM raw_dictionary")}
    rows = list(conn.execute("SELECT * FROM ws_raw ORDER BY id"))
    conn.close()
    assert len(rows) == len(originals)
    assert [read_raw_text(r, zdicts) for r in rows] == originals


def test_manifest_carries_every_required_field(tmp_path, identity):
    seg = open_seg(tmp_path, identity)
    write_frames(seg, 5)
    m = sg.finalize(seg, state=frozen.SEGMENT_COMPLETE,
                    close_reason=frozen.CLOSE_COLLECTION_END,
                    universe_identity=identity)

    required = [
        "filename", "segment_state", "close_reason", "utc_start", "utc_end",
        "byte_size", "raw_frame_count", "sha256", "integrity_check",
        "recorder_git_commit", "collection_universe_id",
        "collection_universe_v1_sha256", "universe_version",
        "addition_log_position", "ws_operational", "any_census_incomplete",
    ]
    for key in required:
        assert key in m, key
    assert m["close_reason"] == frozen.CLOSE_COLLECTION_END
    assert m["collection_universe_id"] == frozen.UNIVERSE_V1_ID
    assert m["universe_version"] == 1 and m["addition_log_position"] == 0


def test_manifest_contains_no_research_data(tmp_path, identity):
    seg = open_seg(tmp_path, identity)
    write_frames(seg, 5)
    sg.finalize(seg, state=frozen.SEGMENT_COMPLETE, close_reason="test",
                universe_identity=identity)
    m = json.loads(seg.manifest_path.read_text(encoding="utf-8"))
    # The predicate prose and the Q1 note are documentation of what is collected,
    # not data about markets; everything else in the manifest is checked.
    for prose_key in ("inclusion_predicate", "q1"):
        m.pop(prose_key, None)
    text = json.dumps(m).lower()
    for banned in ("price", "spread", "depth", "bid", "ask", "volume",
                   "settle", "outcome", "pnl", "midpoint", "returns"):
        assert not re.search(rf"\b{banned}", text), f"manifest mentions {banned!r}"


# -- Immutability -------------------------------------------------------------


def test_finalized_segment_is_never_rewritten(tmp_path, identity):
    seg = open_seg(tmp_path, identity)
    write_frames(seg, 3)
    sg.finalize(seg, state=frozen.SEGMENT_COMPLETE, close_reason="first",
                universe_identity=identity)
    before = seg.manifest_path.read_text(encoding="utf-8")

    with pytest.raises(sg.SegmentImmutable):
        sg.finalize(seg, state=frozen.SEGMENT_COMPLETE, close_reason="second",
                    universe_identity=identity)
    assert seg.manifest_path.read_text(encoding="utf-8") == before


def test_a_new_process_on_the_same_day_gets_a_new_segment(tmp_path, identity):
    now = time.time()
    first = open_seg(tmp_path, identity, now=now)
    sha_before = None
    write_frames(first, 3)
    sg.finalize(first, state=frozen.SEGMENT_COMPLETE, close_reason="restart",
                universe_identity=identity)
    sha_before = frozen.sha256_file(first.db_path)

    second = open_seg(tmp_path, identity, now=now, run_id="run-2")
    assert second.name != first.name
    assert second.index == first.index + 1
    assert second.utc_date == first.utc_date
    # The already-finalized file was not touched.
    assert frozen.sha256_file(first.db_path) == sha_before
    assert json.loads(first.manifest_path.read_text(encoding="utf-8"))[
        "segment_state"] == frozen.SEGMENT_COMPLETE


# -- Crash / orphan handling ---------------------------------------------------


def test_orphan_is_classified_incomplete_crash_not_complete(tmp_path, identity):
    seg = open_seg(tmp_path, identity)
    write_frames(seg, 12)
    seg.store.checkpoint_truncate()
    seg.store.close()          # the process dies here: manifest still says OPEN
    assert json.loads(seg.manifest_path.read_text(encoding="utf-8"))[
        "segment_state"] == frozen.SEGMENT_OPEN

    recovered = sg.recover_orphans(tmp_path, universe_identity=identity)
    assert len(recovered) == 1
    m = recovered[0]
    assert m["segment_state"] == frozen.SEGMENT_INCOMPLETE_CRASH
    assert m["close_reason"] == frozen.CLOSE_ORPHAN_FOUND
    assert m["raw_frame_count"] == 12          # preserved, not discarded
    assert m["sha256"] and m["integrity_check"] == "ok"


def test_orphan_recovery_does_not_touch_finalized_segments(tmp_path, identity):
    done = open_seg(tmp_path, identity)
    write_frames(done, 2)
    sg.finalize(done, state=frozen.SEGMENT_COMPLETE, close_reason="clean",
                universe_identity=identity)
    before = done.manifest_path.read_text(encoding="utf-8")

    orphan = open_seg(tmp_path, identity, run_id="run-2")
    write_frames(orphan, 2)
    orphan.store.close()

    recovered = sg.recover_orphans(tmp_path, universe_identity=identity)
    assert [m["segment_name"] for m in recovered] == [orphan.name]
    assert done.manifest_path.read_text(encoding="utf-8") == before


def test_current_segment_is_excluded_from_orphan_recovery(tmp_path, identity):
    live = open_seg(tmp_path, identity)
    assert sg.recover_orphans(tmp_path, universe_identity=identity,
                              exclude={live.name}) == []
    assert json.loads(live.manifest_path.read_text(encoding="utf-8"))[
        "segment_state"] == frozen.SEGMENT_OPEN


def test_complete_is_downgraded_when_finalization_cannot_verify(tmp_path, identity):
    """
    COMPLETE is earned. If the integrity check or the hash cannot be produced,
    the segment is not a normal finished day and must not be labelled as one.
    """
    seg = open_seg(tmp_path, identity)
    write_frames(seg, 4)
    seg.store.close()
    seg.db_path.unlink()       # the file is gone; nothing can be verified

    m = sg.finalize(seg, state=frozen.SEGMENT_COMPLETE, close_reason="rotation",
                    universe_identity=identity)
    assert m["segment_state"] == frozen.SEGMENT_INCOMPLETE_CRASH
    assert "finalization_failed" in m["close_reason"]


# -- Rotation boundary arithmetic ---------------------------------------------


def test_next_utc_midnight_is_the_next_boundary_strictly_after(tmp_path):
    from datetime import datetime, timezone
    noon = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    boundary = sg.next_utc_midnight(noon)
    assert datetime.fromtimestamp(boundary, tz=timezone.utc) == datetime(
        2026, 9, 5, 0, 0, 0, tzinfo=timezone.utc)

    midnight = datetime(2026, 9, 5, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    assert sg.next_utc_midnight(midnight) == midnight + 86400.0
    assert sg.utc_date_of(midnight) == "2026-09-05"
