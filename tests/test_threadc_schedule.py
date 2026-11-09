"""
Discovery cadence and the restart-storm guard.

Two properties carry the whole design:

  * the schedule is anchored to the wall clock, so a reboot cannot shift it --
    an interval measured from process start would drift a little on every
    restart and permanently after a few;
  * the attempt-start timestamp is persisted BEFORE the sweep begins, so a crash
    four minutes into a 25-minute sweep comes back to a guard that already knows
    a sweep was started. With Restart=always and RestartSec=5, that ordering is
    the difference between one sweep an hour and one every five seconds.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threadc import frozen  # noqa: E402
from threadc.schedule import (  # noqa: E402
    DISCOVERY_STATE_FILE, DiscoverySchedule, ScheduleError, parse_hms,
)

TARGET = "09:00:00"


def ts(y=2026, mo=9, d=10, h=0, mi=0, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


def sched(tmp_path, **kw) -> DiscoverySchedule:
    return DiscoverySchedule(tmp_path, target_utc=kw.pop("target_utc", TARGET), **kw)


# -- Frozen values ------------------------------------------------------------


def test_the_guard_interval_is_frozen_at_3600():
    assert frozen.DISCOVERY_ATTEMPT_MIN_INTERVAL_SECONDS == 3600
    assert DiscoverySchedule(Path(".")).min_attempt_interval == 3600


def test_production_config_targets_0900_utc():
    cfg = json.loads(
        (frozen.REPO_ROOT / "config" / "threadc.json").read_text(encoding="utf-8")
    )
    assert cfg["discovery_daily_target_utc"] == "09:00:00"
    assert "universe_census_interval_seconds" not in cfg


def test_target_parsing_refuses_what_it_cannot_read():
    assert parse_hms("09:00:00") == (9, 0, 0)
    assert parse_hms("09:00") == (9, 0, 0)
    for bad in ("9", "25:00:00", "09:60:00", "nine", "", "09:00:00:00"):
        with pytest.raises(ScheduleError):
            parse_hms(bad)


# -- Daily cadence ------------------------------------------------------------


def test_not_due_before_the_daily_target(tmp_path):
    s = sched(tmp_path)
    d = s.decide(ts(h=8, mi=59))
    assert d.due is False
    assert "before today's target" in d.reason
    assert d.next_check_seconds == pytest.approx(60, abs=1)


def test_due_at_the_daily_target(tmp_path):
    s = sched(tmp_path)
    d = s.decide(ts(h=9))
    assert d.due is True and d.utc_day == "2026-09-10"
    assert "daily target reached" in d.reason


def test_not_due_again_once_the_day_is_accepted(tmp_path):
    s = sched(tmp_path)
    now = ts(h=9)
    s.record_attempt_start(now, "c1", "2026-09-10")
    s.record_result(now + 1500, "c1", frozen.CENSUS_COMPLETE, "2026-09-10")

    later = s.decide(ts(h=23, mi=59))
    assert later.due is False
    assert "already accepted" in later.reason
    # Points at tomorrow's target, not at an interval from now.
    assert later.next_check_seconds > 0


def test_only_one_accepted_sweep_per_utc_day_but_the_next_day_runs(tmp_path):
    s = sched(tmp_path)
    now = ts(d=10, h=9)
    s.record_attempt_start(now, "c1", "2026-09-10")
    s.record_result(now + 1500, "c1", frozen.CENSUS_COMPLETE, "2026-09-10")

    assert s.decide(ts(d=10, h=20)).due is False
    assert s.decide(ts(d=11, h=9)).due is True     # a new UTC day
    assert s.decide(ts(d=11, h=8)).due is False    # but not before its target


def test_a_missed_target_is_caught_up_once_after_restart(tmp_path):
    """Down at 09:00, back up at 14:00: the day's sweep runs once, immediately."""
    s = sched(tmp_path)
    d = s.decide(ts(h=14))
    assert d.due is True
    assert "catch-up" in d.reason

    # And once it is accepted, the catch-up does not repeat.
    s.record_attempt_start(ts(h=14), "c1", "2026-09-10")
    s.record_result(ts(h=14, mi=25), "c1", frozen.CENSUS_COMPLETE, "2026-09-10")
    assert s.decide(ts(h=15)).due is False


def test_reboots_do_not_shift_the_cadence(tmp_path):
    """
    The schedule is a function of the clock and the persisted state, never of
    when this process happened to start.
    """
    now = ts(d=10, h=9)
    first = sched(tmp_path)
    first.record_attempt_start(now, "c1", "2026-09-10")
    first.record_result(now + 1200, "c1", frozen.CENSUS_COMPLETE, "2026-09-10")

    # Three restarts through the day change nothing about the next due time.
    for hour in (10, 13, 22):
        restarted = sched(tmp_path)
        assert restarted.decide(ts(d=10, h=hour)).due is False
    tomorrow = sched(tmp_path)
    assert tomorrow.decide(ts(d=11, h=9)).due is True


# -- The restart-storm guard --------------------------------------------------


def test_attempt_start_is_persisted_before_the_sweep(tmp_path):
    s = sched(tmp_path)
    s.record_attempt_start(ts(h=9), "c1", "2026-09-10")

    state = json.loads((tmp_path / DISCOVERY_STATE_FILE).read_text(encoding="utf-8"))
    assert state["last_attempt_started_ts"] == ts(h=9)
    assert state["last_attempt_status"] == "IN_PROGRESS"
    # The day is NOT accepted merely because a sweep started.
    assert state["accepted_days"] == {}


def test_guard_blocks_a_second_sweep_within_the_hour(tmp_path):
    s = sched(tmp_path)
    s.record_attempt_start(ts(h=9), "c1", "2026-09-10")
    s.record_result(ts(h=9, mi=5), "c1", frozen.CENSUS_INCOMPLETE, "2026-09-10")

    blocked = s.decide(ts(h=9, mi=30))
    assert blocked.due is False
    assert "restart-storm guard" in blocked.reason
    assert blocked.next_check_seconds == pytest.approx(1800, abs=2)

    # After the frozen hour, the unaccepted day is retried.
    assert s.decide(ts(h=10, mi=1)).due is True


def test_guard_applies_however_the_previous_attempt_ended(tmp_path):
    for status in (frozen.CENSUS_COMPLETE, frozen.CENSUS_INCOMPLETE, None):
        d = tmp_path / f"case-{status}"
        s = sched(d)
        s.record_attempt_start(ts(d=10, h=9), "c1", "2026-09-10")
        if status is not None:
            s.record_result(ts(d=10, h=9, mi=2), "c1", status, "2026-09-10")
        # `None` is the killed-mid-sweep case: no result was ever recorded.
        assert s.blocked_by_guard(ts(d=10, h=9, mi=30)) == pytest.approx(1800, abs=2)


def test_the_guard_survives_a_crash_and_restart(tmp_path):
    """
    The whole point. A process killed mid-sweep leaves no result behind, but the
    attempt start is on disk, so the restarted process is still guarded.
    """
    crashed = sched(tmp_path)
    crashed.record_attempt_start(ts(h=9), "c1", "2026-09-10")
    del crashed                                   # the process dies mid-sweep

    for restart_minute in (1, 2, 3, 30, 59):      # Restart=always, RestartSec=5
        restarted = sched(tmp_path)
        d = restarted.decide(ts(h=9, mi=restart_minute))
        assert d.due is False, f"a restart at 09:{restart_minute:02d} started a sweep"
        assert "restart-storm guard" in d.reason

    assert sched(tmp_path).decide(ts(h=10, mi=1)).due is True


def test_incomplete_census_leaves_the_day_unaccepted_for_retry(tmp_path):
    s = sched(tmp_path)
    s.record_attempt_start(ts(h=9), "c1", "2026-09-10")
    s.record_result(ts(h=9, mi=20), "c1", frozen.CENSUS_INCOMPLETE, "2026-09-10")
    assert s.day_is_accepted("2026-09-10") is False

    s2 = sched(tmp_path)
    s2.record_attempt_start(ts(h=10, mi=5), "c2", "2026-09-10")
    s2.record_result(ts(h=10, mi=25), "c2", frozen.CENSUS_COMPLETE, "2026-09-10")
    assert s2.day_is_accepted("2026-09-10") is True
    assert s2.decide(ts(h=12)).due is False


# -- Failing closed -----------------------------------------------------------


def test_a_corrupt_state_file_fails_closed(tmp_path):
    (tmp_path / DISCOVERY_STATE_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ScheduleError, match="restart-storm guard"):
        sched(tmp_path)


def test_summary_is_operational_only(tmp_path):
    s = sched(tmp_path)
    s.record_attempt_start(ts(h=9), "c1", "2026-09-10")
    text = json.dumps(s.summary()).lower()
    for banned in ("price", "spread", "depth", "bid", "ask", "volume", "outcome"):
        assert banned not in text
