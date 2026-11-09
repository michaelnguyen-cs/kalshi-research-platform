"""
The frozen daily health check: exactly four facts, and no research data.

The point of these tests is that the check is honest in both directions -- a day
with an INCOMPLETE_CRASH segment beside two clean ones must read as unhealthy, a
day with no segments at all must not read as healthy by default, and a day whose
segments all finalized COMPLETE while banking nothing must not read as healthy
either. That last one is fact 4: an empty recorder rotates and finalizes exactly
like a working one, so COMPLETE alone cannot be the evidence that data exists.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from threadc import frozen, health, segment as sg  # noqa: E402
from threadc.universe import AppendOnlyUniverse  # noqa: E402

RESERVE = 1024
"""Deliberately tiny. Fact 3 is "free space is above the configured
reserve"; asserting that against a real 10 GiB reserve would be asserting
how much room the developer's laptop happens to have. The inclusive
at-or-below semantics are pinned separately against a constructed
DiskStatus in test_free_space_at_the_reserve_is_a_failure."""


def day_ts(days_ago: int) -> float:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()


def make_segment(tmp_path, identity, *, when: float, state: str, run_id="r",
                 frames: int = 0):
    """
    One finalized segment, optionally having banked `frames` raw frames.

    The frames go in through the segment store's own writer surface rather than
    being faked into the manifest, so the count fact 4 reads is the count the
    archive actually holds.
    """
    seg = sg.open_segment(tmp_path, run_id=run_id, git_commit="cafe",
                          mode=frozen.MODE_PRODUCTION, now=when,
                          universe_identity=identity)
    if frames:
        payload = b'{"type":"orderbook_delta"}'
        seg.store.insert_raw_batch([
            (None, run_id, "S00", 1, "in", when + i, 0, None,
             payload, b"\x00" * 32, len(payload))
            for i in range(frames)
        ])
    return sg.finalize(seg, state=state, close_reason="test",
                       universe_identity=identity, now=when + 60)


def test_healthy_day_reports_all_complete(tmp_path):
    identity = AppendOnlyUniverse(tmp_path).identity()
    yesterday = day_ts(1)
    make_segment(tmp_path, identity, when=yesterday, state=frozen.SEGMENT_COMPLETE,
                 frames=7)
    make_segment(tmp_path, identity, when=yesterday + 3600,
                 state=frozen.SEGMENT_COMPLETE, run_id="r2", frames=5)

    rep = health.report(tmp_path, RESERVE, service="nonexistent.service")
    assert rep.day == health.yesterday_utc()
    assert rep.day_segments_complete is True
    assert len(rep.day_segments) == 2
    assert rep.disk_ok is True
    # Fact 4: one aggregate over the day, not a per-segment or per-market count.
    assert rep.day_frames_ok is True
    assert rep.day_raw_frame_count == 12
    # Fact 1 fails only because there is no such service on this host.
    assert rep.service_active is False
    assert rep.ok is False


def test_a_day_of_empty_complete_segments_is_not_healthy(tmp_path):
    """
    The 63-day failure. A recorder that subscribes to nothing still rotates at
    00:00 UTC, still finalizes COMPLETE and still leaves free space alone, so
    facts 1-3 all pass while the archive is empty.
    """
    identity = AppendOnlyUniverse(tmp_path).identity()
    yesterday = day_ts(1)
    make_segment(tmp_path, identity, when=yesterday, state=frozen.SEGMENT_COMPLETE)
    make_segment(tmp_path, identity, when=yesterday + 3600,
                 state=frozen.SEGMENT_COMPLETE, run_id="r2")

    rep = health.report(tmp_path, RESERVE, service="nonexistent.service")
    # Fact 2 still passes -- the segments really did finalize cleanly.
    assert rep.day_segments_complete is True
    assert rep.day_frames_ok is False
    assert rep.day_raw_frame_count == 0
    assert "0 raw frames" in rep.day_frames_detail
    assert rep.ok is False
    assert "ATTENTION REQUIRED" in rep.render()


def test_one_segment_that_banked_frames_carries_the_day(tmp_path):
    """
    Fact 4 is the day's TOTAL, not a per-segment requirement: a rotation that
    produced an empty tail segment is not by itself a dead recorder.
    """
    identity = AppendOnlyUniverse(tmp_path).identity()
    yesterday = day_ts(1)
    make_segment(tmp_path, identity, when=yesterday,
                 state=frozen.SEGMENT_COMPLETE, frames=3)
    make_segment(tmp_path, identity, when=yesterday + 3600,
                 state=frozen.SEGMENT_COMPLETE, run_id="r2")

    ok, detail, total = health.check_day_frames(
        health.day_segments(tmp_path, health.yesterday_utc()),
        health.yesterday_utc(),
    )
    assert (ok, total) == (True, 3)
    assert "3 raw frames" in detail


def test_a_day_with_no_segments_has_no_frames(tmp_path):
    AppendOnlyUniverse(tmp_path)
    ok, detail, total = health.check_day_frames([], health.yesterday_utc())
    assert ok is False and total == 0 and "no segments" in detail


def test_a_crashed_segment_makes_the_day_unhealthy(tmp_path):
    identity = AppendOnlyUniverse(tmp_path).identity()
    yesterday = day_ts(1)
    make_segment(tmp_path, identity, when=yesterday, state=frozen.SEGMENT_COMPLETE)
    make_segment(tmp_path, identity, when=yesterday + 3600,
                 state=frozen.SEGMENT_INCOMPLETE_CRASH, run_id="r2")

    ok, detail, segments = health.check_day(tmp_path, health.yesterday_utc())
    assert ok is False
    assert "INCOMPLETE_CRASH" in detail
    assert len(segments) == 2


def test_a_halted_segment_makes_the_day_unhealthy(tmp_path):
    identity = AppendOnlyUniverse(tmp_path).identity()
    make_segment(tmp_path, identity, when=day_ts(1),
                 state=frozen.SEGMENT_HALTED_LOW_DISK)
    ok, detail, _ = health.check_day(tmp_path, health.yesterday_utc())
    assert ok is False and "HALTED_LOW_DISK" in detail


def test_a_day_with_no_segments_is_not_healthy(tmp_path):
    AppendOnlyUniverse(tmp_path)
    ok, detail, segments = health.check_day(tmp_path, health.yesterday_utc())
    assert ok is False
    assert "no segments" in detail and segments == []


def test_free_space_at_the_reserve_is_a_failure(tmp_path):
    """The threshold is inclusive: 'at or below R' halts."""
    from threadc import disk
    exactly = disk.DiskStatus(path=str(tmp_path), total_bytes=100, used_bytes=90,
                              free_bytes=10, reserve_bytes=10)
    assert exactly.below_reserve is True
    above = disk.DiskStatus(path=str(tmp_path), total_bytes=100, used_bytes=89,
                            free_bytes=11, reserve_bytes=10)
    assert above.below_reserve is False


def test_report_shows_no_research_data(tmp_path):
    identity = AppendOnlyUniverse(tmp_path).identity()
    make_segment(tmp_path, identity, when=day_ts(1),
                 state=frozen.SEGMENT_COMPLETE, frames=4)
    rep = health.report(tmp_path, RESERVE, service="nonexistent.service")

    text = (json.dumps(rep.as_dict()) + rep.render()).lower()
    for banned in ("price", "spread", "depth", "bid", "ask", "volume",
                   "settle", "outcome", "pnl", "midpoint", "ticker"):
        assert not re.search(rf"\b{banned}", text), f"health output mentions {banned!r}"


def test_report_is_read_only(tmp_path):
    """The check opens no database and modifies nothing."""
    identity = AppendOnlyUniverse(tmp_path).identity()
    make_segment(tmp_path, identity, when=day_ts(1), state=frozen.SEGMENT_COMPLETE)
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size)
              for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    health.report(tmp_path, RESERVE, service="nonexistent.service")
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size)
             for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert before == after


def test_the_four_facts_are_all_present(tmp_path):
    AppendOnlyUniverse(tmp_path)
    d = health.report(tmp_path, RESERVE, service="nonexistent.service").as_dict()
    assert set(d) == {
        "ok",
        "fact_1_service_active",
        "fact_2_segments_complete",
        "fact_3_free_space_above_reserve",
        "fact_4_raw_frames_banked",
    }
    # Fact 4 stays aggregate: one count for the day, and the day it covers.
    assert set(d["fact_4_raw_frames_banked"]) == {
        "utc_day", "any_frames", "raw_frame_count", "detail"
    }
