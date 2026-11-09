"""
The corrected capacity measurement: an exactly bracketed hour that excludes its
own setup, and a report that says only what a capacity run is allowed to say.

The first capacity run measured from process start, so setup, the initial market
census and the establishment of ~10,000 subscriptions all landed inside the
interval whose bytes were divided by an hour. Those costs are real but one-off.
These tests pin the correction: the window opens only after subscriptions are
established, it is bracketed by two filesystem readings, and every number in the
report describes the window rather than the process.

The permitted quantities are exactly: the window and its two disk readings,
aggregate message counts by protocol type, peak simultaneous subscriptions,
error-26 count, reconnects, errors, CPU and RSS. Anything market-level -- a
per-market count, a price, a spread, a depth, an outcome -- would turn a capacity
measurement into a research observation.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from threadc import config as tc, frozen, segment as sg  # noqa: E402
from threadc.recorder import CapacityModeViolation, ThreadCRecorder  # noqa: E402
from threadc_fakes import (  # noqa: E402
    FakeAPI, FakeSession, ample_disk, event, frame, market, trade_frame,
)

NO_CONFIG_FILE = Path(__file__).with_name("no-such-threadc-config.json")
"""A path that does not exist, so threadc.config.load() builds the configuration
from the overrides alone.

Passing None here would resolve to config/threadc.json and quietly layer these
overrides on top of the committed production file -- including its ticker-only
channel selection, which is a production decision and not this test's subject."""


ALLOWED_REPORT_KEYS = {
    "mode", "run_id",
    "measurement_complete", "abort_reason",
    "measurement_start_ts", "measurement_start_utc",
    "measurement_end_ts", "measurement_end_utc",
    "measurement_window_seconds", "setup_seconds_excluded",
    "measurement_disk_growth_bytes",
    "disk_free_at_measurement_start_bytes", "disk_free_at_measurement_end_bytes",
    "archive_dir_growth_bytes",
    "ticker_message_count", "trade_message_count", "total_ws_message_count",
    "ws_message_counts_by_type", "raw_frames_written_in_window",
    "peak_simultaneous_subscribed_markets", "subscription_limit_error_26_count",
    "reconnects", "errors",
    "cpu_user_seconds", "cpu_system_seconds",
    "max_rss_bytes_in_window", "max_rss_bytes_process_lifetime",
    "channels", "rule_1_discovery_performed", "segments", "notes",
}


def capacity_recorder(tmp_path, markets=None, channels=None):
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": markets or [market("KXMLBGAME", 1)]},
    )
    over = {
        "archive_dir": str(tmp_path / "capacity"),
        "collection_end_utc": "2099-01-01T00:00:00Z",
        "disk_reserve_bytes": 10 * 1024**3,
        "discovery_daily_target_utc": "00:00:00",
    }
    if channels is not None:
        over["channels"] = list(channels)
    cfg = tc.load(NO_CONFIG_FILE, mode=frozen.MODE_CAPACITY, overrides=over)
    return ThreadCRecorder(
        cfg, api=api, git_commit="deadbeef",
        session_factory=lambda *, tickers, label: FakeSession(tickers=tickers, label=label),
        disk_check=ample_disk(),
    )


# -- The run itself -----------------------------------------------------------

async def test_capacity_run_stops_cleanly_when_the_window_closes(tmp_path):
    rec = capacity_recorder(tmp_path)
    status = await rec.run(capacity_window_seconds=0.2)

    assert status.exit_code == frozen.EXIT_OK          # not 75: nothing terminal
    assert rec.final_manifest["segment_state"] == frozen.SEGMENT_COMPLETE
    assert rec.final_manifest["close_reason"] == frozen.CLOSE_CAPACITY_RUN_END


async def test_capacity_segments_cannot_be_mistaken_for_production(tmp_path):
    rec = capacity_recorder(tmp_path)
    await rec.run(capacity_window_seconds=0.2)
    m = rec.final_manifest

    assert m["mode"] == frozen.MODE_CAPACITY
    assert m["close_reason"] != frozen.CLOSE_COLLECTION_END
    assert rec.cfg.archive_dir == tmp_path / "capacity"
    assert not list(sg.segments_dir(tmp_path).glob("*.sqlite"))


async def test_capacity_run_does_not_stop_at_the_collection_end(tmp_path):
    """
    Capacity mode ignores the collection end -- it is bounded by its measurement
    window instead, so a capacity run cannot be ended early by, or confused
    with, the frozen production window.
    """
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [market("KXMLBGAME", 1)]},
    )
    cfg = tc.load(NO_CONFIG_FILE, mode=frozen.MODE_CAPACITY, overrides={
        "archive_dir": str(tmp_path / "capacity"),
        "collection_end_utc": "2020-01-01T00:00:00Z",   # long past
        "disk_reserve_bytes": 10 * 1024**3,
        "discovery_daily_target_utc": "00:00:00",
    })
    rec = ThreadCRecorder(
        cfg, api=api, git_commit="deadbeef",
        session_factory=lambda *, tickers, label: FakeSession(tickers=tickers, label=label),
        disk_check=ample_disk(),
    )
    status = await rec.run(capacity_window_seconds=0.2)
    assert status.exit_code == frozen.EXIT_OK
    assert rec.final_manifest["close_reason"] == frozen.CLOSE_CAPACITY_RUN_END


async def test_a_measurement_window_is_refused_in_production_mode(tmp_path):
    """
    A production process is bounded by the collection end, never by a
    measurement window. Refused at the entry point rather than left to
    configuration discipline.
    """
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [market("KXMLBGAME", 1)]},
    )
    cfg = tc.load(NO_CONFIG_FILE, mode=frozen.MODE_PRODUCTION, overrides={
        "archive_dir": str(tmp_path / "prod"),
        "collection_end_utc": "2099-01-01T00:00:00Z",
        "disk_reserve_bytes": 10 * 1024**3,
    })
    rec = ThreadCRecorder(
        cfg, api=api, git_commit="deadbeef",
        session_factory=lambda *, tickers, label: FakeSession(tickers=tickers, label=label),
        disk_check=ample_disk(),
    )
    with pytest.raises(CapacityModeViolation):
        await rec.run(capacity_window_seconds=0.2)


# -- The bracket --------------------------------------------------------------

async def test_setup_falls_outside_the_measured_window(tmp_path):
    """
    The whole point of the correction. Startup, the census and subscription
    establishment happen before measurement_start, and the reported window is
    the window -- not the process lifetime.
    """
    rec = capacity_recorder(tmp_path)
    await rec.run(capacity_window_seconds=0.3)
    m = rec.capacity

    assert m is not None and m.complete
    assert m.ready_ts is not None and m.started_ts is not None
    assert m.started_ts > rec.status.started_ts
    assert m.elapsed == pytest.approx(0.3, abs=0.25)

    report = rec.capacity_report()
    assert report["setup_seconds_excluded"] > 0
    assert report["measurement_window_seconds"] == pytest.approx(0.3, abs=0.25)


async def test_the_window_opens_only_once_subscriptions_are_established(tmp_path):
    rec = capacity_recorder(tmp_path)
    await rec.run(capacity_window_seconds=0.2)
    m = rec.capacity

    assert rec._subscriptions_established.is_set()
    # Establishment banks the explicit initial state, and that is setup too:
    # it must be finished before the clock starts.
    assert rec.status.initial_states_banked > 0
    assert m.started_ts >= m.ready_ts


async def test_disk_is_read_at_both_marks_and_growth_is_their_difference(tmp_path):
    rec = capacity_recorder(tmp_path)
    await rec.run(capacity_window_seconds=0.2)
    m = rec.capacity

    assert m.disk_start is not None and m.disk_end is not None
    report = rec.capacity_report()
    assert report["measurement_disk_growth_bytes"] == (
        m.disk_start.free_bytes - m.disk_end.free_bytes
    )
    assert report["disk_free_at_measurement_start_bytes"] == m.disk_start.free_bytes
    assert report["disk_free_at_measurement_end_bytes"] == m.disk_end.free_bytes


async def test_an_unestablished_subscription_aborts_rather_than_measures(tmp_path):
    """
    A window started on a half-subscribed universe would understate the rate,
    and a understated B_1h is the dangerous direction: it sizes the reserve too
    small. So the measurement refuses to start instead.
    """
    rec = capacity_recorder(tmp_path)
    # A session that never acknowledges its subscription.
    rec._session_factory = lambda *, tickers, label: _SilentSession(
        tickers=tickers, label=label
    )
    original = frozen.CAPACITY_ESTABLISH_TIMEOUT_SECONDS
    frozen.CAPACITY_ESTABLISH_TIMEOUT_SECONDS = 0.2
    try:
        await rec.run(capacity_window_seconds=5.0)
    finally:
        frozen.CAPACITY_ESTABLISH_TIMEOUT_SECONDS = original

    report = rec.capacity_report()
    assert report["measurement_complete"] is False
    assert "not established" in report["abort_reason"]
    assert "measurement_disk_growth_bytes" not in report


class _SilentSession(FakeSession):
    """Connects but never acknowledges a subscription."""

    def connect(self) -> None:
        self.session_epoch += 1
        self.connected = True
        if self._health_sink:
            self._health_sink("connect", 0.0, "", f"epoch={self.session_epoch}")


# -- What the report may contain ----------------------------------------------

async def test_capacity_report_carries_only_permitted_quantities(tmp_path):
    rec = capacity_recorder(tmp_path)

    async def feed_once_up():
        # run() seeds two census passes on worker threads before the sessions
        # exist, so wait for the recorder to actually be up rather than guessing
        # a sleep.
        while not rec.sessions or rec._writer is None:
            await asyncio.sleep(0.01)
        for s in rec.sessions.values():
            for i in range(50):
                s.receive(frame("KXMLBGAME-M01", i))
            for i in range(7):
                s.receive(trade_frame("KXMLBGAME-M01", i))

    task = asyncio.ensure_future(feed_once_up())
    await rec.run(capacity_window_seconds=1.0)
    await task

    report = rec.capacity_report()
    assert set(report) == ALLOWED_REPORT_KEYS
    assert report["mode"] == frozen.MODE_CAPACITY
    assert report["channels"] == list(frozen.CHANNELS)
    assert report["measurement_disk_growth_bytes"] is not None


async def test_capacity_report_names_no_market_and_no_market_data(tmp_path):
    rec = capacity_recorder(tmp_path)

    async def feed_once_up():
        while not rec.sessions or rec._writer is None:
            await asyncio.sleep(0.01)
        for s in rec.sessions.values():
            s.receive(frame("KXMLBGAME-M01", 1))

    task = asyncio.ensure_future(feed_once_up())
    await rec.run(capacity_window_seconds=0.5)
    await task

    text = json.dumps(rec.capacity_report()).lower()
    # No market identity of any kind.
    assert "kxmlbgame" not in text
    # No market DATA. `ticker` and `trade` survive as protocol channel names --
    # they are aggregate message-type labels, not observations of a market --
    # so the ban is on the quantities that would describe one.
    for banned in ("price", "spread", "depth", "bid", "ask", "volume",
                   "settle", "outcome", "pnl", "midpoint", "open_interest",
                   "size_fp", "liquidity"):
        assert not re.search(rf"\b{banned}", text), f"capacity report mentions {banned!r}"


async def test_message_counts_are_by_type_and_windowed(tmp_path):
    """
    Aggregate counts of ticker, trade and total messages INSIDE the window. A
    message received during setup belongs to setup.
    """
    rec = capacity_recorder(tmp_path)

    async def feed_during_window():
        while rec.capacity is None or rec.capacity.started_ts is None:
            await asyncio.sleep(0.01)
        for s in rec.sessions.values():
            for i in range(11):
                s.receive(frame("KXMLBGAME-M01", i))
            for i in range(4):
                s.receive(trade_frame("KXMLBGAME-M01", i))

    task = asyncio.ensure_future(feed_during_window())
    await rec.run(capacity_window_seconds=0.6)
    await task

    report = rec.capacity_report()
    assert report["ticker_message_count"] == 11
    assert report["trade_message_count"] == 4
    assert report["total_ws_message_count"] == 15
    assert report["ws_message_counts_by_type"]["ticker"] == 11
    assert report["raw_frames_written_in_window"] >= 15


async def test_messages_received_before_the_window_are_not_counted_in_it(tmp_path):
    rec = capacity_recorder(tmp_path)

    async def feed_before_window():
        while not rec.sessions or rec._writer is None:
            await asyncio.sleep(0.01)
        for s in rec.sessions.values():
            for i in range(9):
                s.receive(frame("KXMLBGAME-M01", i))

    task = asyncio.ensure_future(feed_before_window())
    await rec.run(capacity_window_seconds=0.4)
    await task

    counted = rec.capacity_report()["ticker_message_count"]
    total_received = sum(
        s.message_counts["ticker"] for s in rec.sessions.values()
    )
    assert total_received == 9
    assert counted < total_received


# -- Config and discipline ----------------------------------------------------

def test_capacity_config_uses_the_reserve_floor_not_a_guess():
    """
    The shipped capacity config uses exactly the 10 GiB floor of
    R = max(10 GiB, 48 x B_1h). It must not carry a guessed 48 x B_1h.
    """
    cfg = json.loads(
        (frozen.REPO_ROOT / "config" / "threadc.capacity.json").read_text(encoding="utf-8")
    )
    from threadc.config import MIN_DISK_RESERVE_BYTES
    assert cfg["disk_reserve_bytes"] == MIN_DISK_RESERVE_BYTES
    assert cfg["archive_dir"] != json.loads(
        (frozen.REPO_ROOT / "config" / "threadc.json").read_text(encoding="utf-8")
    )["archive_dir"]


def test_the_measured_window_is_frozen_at_one_hour():
    assert frozen.CAPACITY_MEASUREMENT_SECONDS == 3600.0


async def test_the_recorder_never_selects_a_storage_fallback(tmp_path):
    """
    The recorder reports the measured rate. Choosing between ticker+trade,
    ticker-only, paid storage, a shorter window or a narrower universe is a human
    decision, and nothing in the report may pre-empt it.
    """
    rec = capacity_recorder(tmp_path)
    await rec.run(capacity_window_seconds=0.2)
    text = json.dumps(rec.capacity_report()).lower()
    for banned in ("ticker-only", "ticker only", "paid storage", "branch",
                   "regular season", "narrow", "recommend", "instead", "should"):
        assert banned not in text, f"the report pre-empts the decision: {banned!r}"


async def test_capacity_mode_cannot_launch_a_full_universe_sweep(tmp_path):
    """
    Structural, not incidental. The entry point refuses, so a capacity run
    cannot spend a 25-minute sweep inside the hour it is measuring even if
    something calls it directly.
    """
    rec = capacity_recorder(tmp_path)
    assert rec.cfg.discovery_enabled is False
    with pytest.raises(CapacityModeViolation):
        await rec.run_discovery_once()
    with pytest.raises(CapacityModeViolation):
        await rec.run_discovery_once(force=True)

    await rec.run(capacity_window_seconds=0.3)
    assert rec.status.discovery_censuses == 0
    assert rec.capacity_report()["rule_1_discovery_performed"] is False
    # Nothing was persisted to the discovery schedule, so a capacity run cannot
    # even consume the production restart-storm budget.
    assert not (rec.cfg.archive_dir / "discovery_schedule.state.json").exists()
    # No /events probe was issued at all -- the sweep's signature call.
    assert not [c for c in rec.api.calls if c[0] == "/events"]


async def test_capacity_run_starts_from_the_current_append_only_universe(tmp_path):
    """It records what is already admitted, and does not go looking for more."""
    rec = capacity_recorder(tmp_path)
    assert len(rec.universe.members) == frozen.EXPECTED_V1_QUALIFYING_COUNT
    await rec.run(capacity_window_seconds=0.3)
    assert len(rec.universe.members) == frozen.EXPECTED_V1_QUALIFYING_COUNT
    assert rec.status.universe_additions == []


async def test_capacity_report_counts_peak_subscriptions_and_error_26(tmp_path):
    rec = capacity_recorder(tmp_path)
    await rec.run(capacity_window_seconds=0.3)
    report = rec.capacity_report()

    assert report["peak_simultaneous_subscribed_markets"] == 1
    assert report["subscription_limit_error_26_count"] == 0


async def test_subscription_limit_error_26_is_counted(tmp_path):
    """
    The count comes from the server_error health event the session records
    immediately before it raises, so each occurrence is seen exactly once.
    """
    rec = capacity_recorder(tmp_path)

    async def emit_then_wait():
        while rec.capacity is None or rec.capacity.started_ts is None:
            await asyncio.sleep(0.01)
        session = next(iter(rec.sessions.values()))
        payload = json.dumps({
            "type": "error",
            "msg": {"code": 26, "msg": "Subscription market limit exceeded"},
        })
        session._health_sink("server_error", 0.0, "UNAVAILABLE_OTHER", payload)

    task = asyncio.ensure_future(emit_then_wait())
    await rec.run(capacity_window_seconds=0.5)
    await task

    assert rec.capacity_report()["subscription_limit_error_26_count"] == 1


# -- Branch 2: the same measurement, one channel ------------------------------
#
# Branch 1 measured B_1h,disk = 373,444,608 bytes/hour on ticker+trade: 12.2x
# the full-window storage budget, 4.4x regular-season-only. The precommitted
# hierarchy's next branch is ticker-only, and it is measured rather than
# inferred -- trade was 5.1% of Branch 1's MESSAGE COUNT, and a message count is
# not a disk contribution. So Branch 2 is this same bracketed hour with a
# narrower subscription and nothing else changed.


async def test_the_default_capacity_run_measures_the_frozen_pair(tmp_path):
    rec = capacity_recorder(tmp_path)
    await rec.run(capacity_window_seconds=0.3)

    assert rec.cfg.channels == frozen.CHANNELS
    assert rec.capacity_report()["channels"] == ["ticker", "trade"]
    assert rec.final_manifest["channels"] == ["ticker", "trade"]


async def test_a_ticker_only_capacity_run_records_its_selection(tmp_path):
    """
    The selection is written where a reader of the archive will find it: the
    capacity report AND the segment manifest. A ticker-only archive whose
    manifest claimed both channels would read, years later, as an hour in which
    nothing traded.
    """
    rec = capacity_recorder(tmp_path, channels=["ticker"])
    await rec.run(capacity_window_seconds=0.3)

    assert rec.cfg.channels == ("ticker",)
    report = rec.capacity_report()
    assert set(report) == ALLOWED_REPORT_KEYS      # no new key; the same report
    assert report["channels"] == ["ticker"]
    assert report["mode"] == frozen.MODE_CAPACITY

    m = rec.final_manifest
    assert m["channels"] == ["ticker"]
    assert m["mode"] == frozen.MODE_CAPACITY
    assert m["close_reason"] == frozen.CLOSE_CAPACITY_RUN_END
    assert m["segment_state"] == frozen.SEGMENT_COMPLETE
    # The startup record says it too, and says that it is not the frozen pair.
    startup = rec.startup_record()
    assert startup["channels"] == ["ticker"]
    assert startup["channels_are_frozen_pair"] is False


async def test_branch_2_changes_nothing_else_about_the_measurement(tmp_path):
    """
    Same universe, same initial-state banking, same storage schema, same
    manifest fields, same measured window. Only the subscription is narrower --
    otherwise the two branches would not be comparable and neither number would
    mean anything.
    """
    both = capacity_recorder(tmp_path / "pair")
    await both.run(capacity_window_seconds=0.3)
    one = capacity_recorder(tmp_path / "ticker", channels=["ticker"])
    await one.run(capacity_window_seconds=0.3)

    assert one.universe.identity() == both.universe.identity()
    assert len(one.universe.members) == frozen.EXPECTED_V1_QUALIFYING_COUNT
    assert one.status.universe_additions == []
    assert one.status.discovery_censuses == 0
    assert one.capacity_report()["rule_1_discovery_performed"] is False
    # The explicit REST initial state is still banked -- neither channel
    # snapshots on subscribe, and dropping `trade` does not change that.
    assert one.status.initial_states_banked == both.status.initial_states_banked > 0

    ignored = {"channels", "run_id", "recorder_git_commit", "sha256", "utc_start",
               "utc_start_ts", "utc_end", "utc_end_ts", "byte_size", "segment_name",
               "utc_date", "filename", "raw_frame_count", "raw_uncompressed_bytes",
               "ws_operational", "finalization_steps", "initial_state"}
    assert set(one.final_manifest) == set(both.final_manifest)
    for key in set(one.final_manifest) - ignored:
        assert one.final_manifest[key] == both.final_manifest[key], key

    # The measured window itself is untouched: still exactly one hour by default.
    assert frozen.CAPACITY_MEASUREMENT_SECONDS == 3600.0


async def test_the_selection_reaches_the_real_session_not_just_the_report(tmp_path):
    """
    A report that said "ticker" while the socket subscribed to both would be the
    worst possible outcome: a Branch-1 number filed as Branch 2. The recorder's
    own session factory is what puts the selection on the wire.
    """
    rec = capacity_recorder(tmp_path, channels=["ticker"])
    session = rec._default_session_factory(tickers=["KXMLBGAME-M01"], label="S00")
    assert session.channels == ("ticker",)

    pair = capacity_recorder(tmp_path / "pair")
    assert pair._default_session_factory(
        tickers=["KXMLBGAME-M01"], label="S00").channels == frozen.CHANNELS
