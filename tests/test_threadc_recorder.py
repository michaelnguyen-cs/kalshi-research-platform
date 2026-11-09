"""
The recorder's safety-critical operational behaviour.

Everything here is a property the frozen spec states in the imperative: rotate
internally without restarting the WebSocket; never drop a frame across the swap;
halt on low disk with exit 75; stop at the frozen collection end with exit 75;
keep recording through an incomplete Gate-3 census; shard deterministically; and
never, anywhere, have an order path.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from depthrec.store import read_raw_text  # noqa: E402
from threadc import config as tc, disk, frozen, health, segment as sg, shard  # noqa: E402
from threadc import recorder as rec_mod  # noqa: E402
from threadc.recorder import ThreadCRecorder  # noqa: E402
from threadc.schedule import DiscoverySchedule  # noqa: E402
from threadc_fakes import (  # noqa: E402
    FakeAPI, FakeSession, ample_disk, event, frame, market, starved_disk,
)

NO_CONFIG_FILE = Path(__file__).with_name("no-such-threadc-config.json")
"""A path that does not exist, so threadc.config.load() builds the configuration
from the overrides alone.

Passing None here would resolve to config/threadc.json and quietly layer these
overrides on top of the committed production file -- including its ticker-only
channel selection, which is a production decision and not this test's subject."""


RESERVE = 10 * 1024**3
FUTURE = "2099-01-01T00:00:00Z"


def make_config(tmp_path, **kw):
    return tc.load(NO_CONFIG_FILE, overrides={
        "archive_dir": str(tmp_path),
        "collection_end_utc": FUTURE,
        "disk_reserve_bytes": RESERVE,
        "discovery_daily_target_utc": "00:00:00",
        **kw,
    })


def make_api(series=("KXMLBGAME",), *, unreadable=()):
    return FakeAPI(
        catalog=list(series),
        events={s: [event(s, "Pro Baseball")] for s in series},
        markets={s: [market(s, 1), market(s, 2)] for s in series},
        unreadable=unreadable,
    )


CONFIG_KEYS = ("ws_shards", "max_markets_per_shard", "collection_end_utc",
               "discovery_daily_target_utc", "channels")


def suppress_discovery(tmp_path, *, now=None):
    """
    Mark today's UTC discovery already accepted, through the real schedule.

    Most recorder tests are about rotation, disk, sharding or teardown, and a
    full-universe sweep firing in the middle of them would make what they assert
    depend on the wall clock. Suppressing it through DiscoverySchedule rather
    than a flag means the tests exercise the same persistence the recorder does.
    """
    now = time.time() if now is None else now
    s = DiscoverySchedule(tmp_path)
    s.record_attempt_start(now, "test-preseed", sg.utc_date_of(now))
    s.record_result(now, "test-preseed", frozen.CENSUS_COMPLETE, sg.utc_date_of(now))
    return s


def make_recorder(tmp_path, api=None, *, discovery=False, **kw):
    api = api or make_api()
    sessions: list[FakeSession] = []
    cfg = make_config(tmp_path, **{k: v for k, v in kw.items() if k in CONFIG_KEYS})

    def factory(*, tickers, label):
        # The recorder's own default factory hands the run's channel selection
        # to the session it builds; this stands in for that, so a ticker-only
        # run's subscribe params say ticker only here too.
        s = FakeSession(tickers=tickers, label=label, channels=cfg.channels)
        sessions.append(s)
        return s

    if not discovery:
        suppress_discovery(tmp_path)

    rec = ThreadCRecorder(
        cfg,
        api=api, session_factory=factory, git_commit="deadbeef",
        # Injected so the tests assert the recorder, not the host's free space.
        disk_check=kw.get("disk_check") or ample_disk(),
    )
    rec._test_sessions = sessions
    return rec


async def bring_up(rec):
    """The part of run() that opens a segment and builds the shards."""
    rec._log_process_start(rec.startup_record())
    rec._open_segment()
    rec._start_writer()
    await asyncio.to_thread(rec.census.run_live_pass)
    rec._rebuild_shards()
    for s in rec.sessions.values():
        s.connect()
    return rec


async def finish(rec, state=frozen.SEGMENT_COMPLETE, reason="test"):
    """Finalize the open segment the way teardown does -- on a worker thread."""
    return await asyncio.to_thread(rec._finalize_current, state, reason)


def frames_in(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    zdicts = {r["id"]: bytes(r["zdict"])
              for r in conn.execute("SELECT id, zdict FROM raw_dictionary")}
    rows = list(conn.execute("SELECT * FROM ws_raw ORDER BY id"))
    conn.close()
    return [read_raw_text(r, zdicts) for r in rows]


# -- Internal daily UTC rotation ---------------------------------------------


async def test_rotation_does_not_restart_the_websocket(tmp_path):
    rec = await bring_up(make_recorder(tmp_path))
    session = rec.sessions["S00"]
    runs_before, stops_before = session.run_calls, session.stop_calls
    epoch_before = session.session_epoch

    first = rec.segment
    manifest = await rec.rotate(now=time.time())

    assert manifest["segment_state"] == frozen.SEGMENT_COMPLETE
    assert manifest["close_reason"] == frozen.CLOSE_ROTATION_UTC_MIDNIGHT
    # The socket was never stopped, never reconnected, and kept its epoch.
    assert session.stop_calls == stops_before == 0
    assert session.run_calls == runs_before
    assert session.session_epoch == epoch_before
    assert session.connected is True
    # A new segment is open and it is a different file.
    assert rec.segment is not None and rec.segment.name != first.name
    assert rec.segment.db_path != first.db_path

    await finish(rec)


async def test_rotation_drops_no_frames_and_splits_them_correctly(tmp_path):
    rec = await bring_up(make_recorder(tmp_path))
    session = rec.sessions["S00"]

    before = [frame("KXMLBGAME-M01", i) for i in range(200)]
    for f in before:
        session.feed(f)
    first_seg = rec.segment

    await rec.rotate(now=time.time())

    after = [frame("KXMLBGAME-M02", i) for i in range(200, 350)]
    for f in after:
        session.feed(f)
    second_seg = rec.segment
    await finish(rec)

    got_first = frames_in(first_seg.db_path)
    got_second = frames_in(second_seg.db_path)
    assert got_first == before
    assert got_second == after
    assert len(got_first) + len(got_second) == 350   # nothing dropped, nothing doubled


async def test_rotation_carries_connection_metadata_into_the_new_segment(tmp_path):
    rec = await bring_up(make_recorder(tmp_path))
    await rec.rotate(now=time.time())
    new_seg = rec.segment
    manifest = await finish(rec)

    ws = manifest["ws_operational"]
    assert ws["connection_epochs"] >= 1
    assert ws["shards"] == ["S00"]
    kinds = new_seg_health(new_seg.db_path)
    assert "rotation_in" in kinds


def new_seg_health(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    out = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM health_event")}
    conn.close()
    return out


async def test_rotation_boundary_is_utc_midnight(tmp_path):
    rec = make_recorder(tmp_path)
    await bring_up(rec)
    started = rec.segment.started_ts
    boundary = sg.next_utc_midnight(started)
    as_dt = datetime.fromtimestamp(boundary, tz=timezone.utc)
    assert (as_dt.hour, as_dt.minute, as_dt.second, as_dt.microsecond) == (0, 0, 0, 0)
    assert sg.utc_date_of(boundary) != sg.utc_date_of(started)
    await finish(rec)


# -- Terminal stops -----------------------------------------------------------


async def test_frozen_collection_end_stops_cleanly_with_exit_75(tmp_path):
    past = "2020-01-01T00:00:00Z"
    rec = make_recorder(tmp_path, collection_end_utc=past)
    status = await rec.run()

    assert status.exit_code == frozen.EXIT_TERMINAL_NO_RESTART
    assert frozen.CLOSE_COLLECTION_END in status.stop_reason
    m = rec.final_manifest
    assert m["segment_state"] == frozen.SEGMENT_COMPLETE
    assert m["close_reason"] == frozen.CLOSE_COLLECTION_END
    assert m["integrity_check"] == "ok"
    # Not a restart loop: the socket was closed once, deliberately.
    assert all(s.stop_calls == 1 for s in rec.sessions.values())


async def test_low_disk_halts_finalizes_and_exits_75(tmp_path):
    calls = {"n": 0}
    plenty, starved = ample_disk(), starved_disk()

    def flaky_disk(path, reserve):
        calls["n"] += 1
        # The startup check passes; the reserve is breached only afterwards, so
        # the halt has an open segment to finalize as HALTED_LOW_DISK.
        return plenty(path, reserve) if calls["n"] == 1 else starved(path, reserve)

    rec = make_recorder(tmp_path, disk_check=flaky_disk)
    status = await rec.run()

    assert status.exit_code == frozen.EXIT_TERMINAL_NO_RESTART
    assert frozen.CLOSE_LOW_DISK in status.stop_reason
    m = rec.final_manifest
    assert m["segment_state"] == frozen.SEGMENT_HALTED_LOW_DISK
    assert m["close_reason"] == frozen.CLOSE_LOW_DISK
    assert m["sha256"] and m["integrity_check"] == "ok"


async def test_low_disk_at_startup_opens_nothing_and_exits_75(tmp_path):
    from threadc.recorder import TerminalStop

    rec = make_recorder(tmp_path, disk_check=starved_disk(free_fraction=0.0))
    with pytest.raises(TerminalStop) as excinfo:
        await rec.run()
    assert excinfo.value.exit_code == frozen.EXIT_TERMINAL_NO_RESTART
    assert not list(sg.segments_dir(tmp_path).glob("*.sqlite"))


# -- Gate 3 during production -------------------------------------------------


async def test_incomplete_census_changes_nothing_and_recording_continues(tmp_path):
    api = make_api(("KXMLBGAME", "KXFLAKY"), unreadable={"KXFLAKY"})
    rec = await bring_up(make_recorder(tmp_path, api=api, discovery=True))
    session = rec.sessions["S00"]
    subscribed_before = set(session.tickers)
    universe_before = len(rec.universe.members)

    result = await rec.run_discovery_once()

    assert result.status == frozen.CENSUS_INCOMPLETE
    assert len(rec.universe.members) == universe_before   # zero additions
    assert rec.status.discovery_incomplete == 1
    # The live recorder is untouched: same subscription, same socket, still writing.
    assert set(session.tickers) == subscribed_before
    assert session.stop_calls == 0 and session.connected
    session.feed(frame("KXMLBGAME-M01", 1))

    # The failed census is itself an operational record.
    seg = rec.segment
    rows = seg.store.query("SELECT status, additions FROM discovery_census")
    assert [(r["status"], r["additions"]) for r in rows] == [
        (frozen.CENSUS_INCOMPLETE, 0)
    ]

    manifest = await finish(rec)
    assert manifest["any_census_incomplete"] is True
    assert manifest["raw_frame_count"] == 1


async def test_complete_census_admits_and_subscribes_without_a_restart(tmp_path):
    api = make_api(("KXMLBGAME",))
    rec = await bring_up(make_recorder(tmp_path, api=api, discovery=True))
    session = rec.sessions["S00"]
    runs_before = session.run_calls

    # Kalshi creates the World Series series mid-collection.
    api.catalog.append("KXMLBWS")
    api.events["KXMLBWS"] = [event("KXMLBWS", "Pro Baseball")]
    api.markets["KXMLBWS"] = [market("KXMLBWS", 1)]

    result = await rec.run_discovery_once()
    assert result.complete
    assert "KXMLBWS" in rec.universe.members
    assert "KXMLBWS" in rec.census.series
    assert "KXMLBWS-M01" in session.tickers      # on the wire, no restart needed
    assert session.run_calls == runs_before and session.stop_calls == 0

    manifest = await finish(rec)
    assert manifest["universe_version"] == 2
    assert manifest["addition_log_position"] == 1
    assert manifest["any_census_incomplete"] is False


# -- Sharding -----------------------------------------------------------------


def test_shard_assignment_is_deterministic_and_exclusive():
    tickers = [f"KXMLB{i}-M{j:02d}" for i in range(20) for j in range(5)]
    a = shard.partition(tickers, 4)
    b = shard.partition(list(reversed(tickers)), 4)
    assert a == b                                     # order-independent
    seen = [t for v in a.values() for t in v]
    assert sorted(seen) == sorted(tickers)            # every market placed once
    assert len(seen) == len(set(seen))                # exactly one shard each
    assert set(a) == {"S00", "S01", "S02", "S03"}     # empty shards still present


def test_shard_assignment_is_stable_under_growth():
    base = [f"KXMLB-M{i:03d}" for i in range(100)]
    before = shard.partition(base, 3)
    after = shard.partition(base + ["KXMLBWS-M01", "KXWSAL-M01"], 3)
    for label, tickers in before.items():
        assert set(tickers) <= set(after[label]), (
            f"{label} lost markets when the universe grew"
        )


async def test_markets_are_never_double_subscribed_across_shards(tmp_path):
    api = make_api(tuple(f"KXMLB{i}" for i in range(6)))
    rec = await bring_up(make_recorder(tmp_path, api=api, ws_shards=3))
    assert set(rec.sessions) == {"S00", "S01", "S02"}
    all_tickers = [t for s in rec.sessions.values() for t in s.tickers]
    assert len(all_tickers) == len(set(all_tickers))
    for label, session in rec.sessions.items():
        for t in session.tickers:
            assert shard.shard_label(shard.shard_index(t, 3)) == label
    await finish(rec)


async def test_shard_assignment_is_persisted(tmp_path):
    rec = await bring_up(make_recorder(tmp_path, ws_shards=2))
    seg = rec.segment
    rows = seg.store.query("SELECT ticker, shard_label, action FROM shard_assignment")
    assert rows, "no shard assignment recorded"
    for r in rows:
        assert shard.shard_label(shard.shard_index(r["ticker"], 2)) == r["shard_label"]
    await finish(rec)


# -- Subscription follows the census, with no research horizon ----------------


async def test_a_market_returned_with_the_live_status_is_subscribed(tmp_path):
    """
    The regression. `status=open` is Kalshi's QUERY FILTER; the market body it
    returns carries `"status": "active"`, and `?status=active` is rejected 400
    "invalid status filter" -- the two vocabularies are disjoint. The recorder
    compared a censused entry's RETURNED status against the filter word, so it
    matched nothing: a live 5-minute smoke censused 6,947 markets, subscribed to
    none of them, banked zero frames, and finalized COMPLETE.

    The market here carries exactly what the exchange returns.
    """
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [market("KXMLBGAME", 1, status="active")]},
    )
    rec = await bring_up(make_recorder(tmp_path, api=api))

    assert rec.census.entries["KXMLBGAME-M01"].status == "active"
    assert rec.wanted_markets() == {"KXMLBGAME-M01"}
    assert rec._subscribed_market_count() == 1
    assert rec.status.peak_subscribed_markets == 1

    # ...and "open" appears only where it belongs: as a request parameter.
    filters = [p.get("status") for path, p in api.calls if path == "/markets"]
    assert "open" in filters
    assert "active" not in filters
    await finish(rec)


async def test_the_first_subscription_happens_at_startup(tmp_path):
    """
    A delayed first subscribe looks exactly like the status mismatch in a short
    run -- zero frames, zero shard assignments, a COMPLETE segment -- so it is
    pinned separately.

    run() ends after 50 ms here. The market census loop's first tick is 60 s
    away and cannot have fired, so every market on the wire got there from the
    startup census: the sessions were CONSTRUCTED with the full set rather than
    having it trickle in through update_markets a minute later.
    """
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [market("KXMLBGAME", 1), market("KXMLBGAME", 2)]},
    )
    rec = make_recorder(tmp_path, api=api)
    await asyncio.wait_for(rec.run(stop_after_seconds=0.05), timeout=20)

    sessions = rec._test_sessions
    assert sessions, "no WebSocket session was ever constructed"
    initial = sorted(t for s in sessions for t in s.initial_tickers)
    assert initial == ["KXMLBGAME-M01", "KXMLBGAME-M02"]
    assert all(s.updates == [] for s in sessions)
    assert rec.status.peak_subscribed_markets == 2

    # The shard assignments are in the segment, written at startup.
    manifest = rec.final_manifest
    assert manifest["segment_state"] == frozen.SEGMENT_COMPLETE
    conn = sqlite3.connect(str(rec.segment.db_path))
    assigned = {r[0] for r in conn.execute(
        "SELECT ticker FROM shard_assignment WHERE action = 'assign'")}
    conn.close()
    assert assigned == {"KXMLBGAME-M01", "KXMLBGAME-M02"}


async def test_a_census_that_subscribes_to_nothing_is_written_down(tmp_path):
    """
    The silent-failure signature itself: markets are being censused and none of
    them reaches the wire. Recorded as an operational health event, with counts
    only -- how many, never which.
    """
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [
            market("KXMLBGAME", 1, status="finalized",
                   close_ts=int(time.time()) - 86400),
        ]},
    )
    rec = await bring_up(make_recorder(tmp_path, api=api))
    await asyncio.to_thread(rec.census.run_reconcile_pass)
    await rec._sync_subscription()

    assert rec.census.entries and rec.wanted_markets() == set()
    rows = rec.segment.store.query(
        "SELECT detail FROM health_event WHERE kind = 'subscription_set_empty'"
    )
    assert len(rows) == 1, "the empty subscription set was not recorded once"
    assert "1 market(s)" in rows[0]["detail"]
    assert "KXMLBGAME" not in rows[0]["detail"], "aggregate counts only"
    await finish(rec)


async def test_subscription_covers_every_open_market_with_no_lead_window(tmp_path):
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [
            market("KXMLBGAME", 1, close_ts=int(time.time()) + 60),
            market("KXMLBGAME", 2, close_ts=int(time.time()) + 86400 * 30),
        ]},
    )
    rec = await bring_up(make_recorder(tmp_path, api=api))
    want = rec.wanted_markets()
    # A market closing in 30 days is subscribed exactly like one closing in a
    # minute: Thread C chooses no horizon.
    assert want == {"KXMLBGAME-M01", "KXMLBGAME-M02"}
    await finish(rec)


async def test_long_closed_markets_are_dropped_from_the_wire(tmp_path):
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [
            market("KXMLBGAME", 1, status="finalized",
                   close_ts=int(time.time()) - 86400),
        ]},
    )
    rec = await bring_up(make_recorder(tmp_path, api=api))
    await asyncio.to_thread(rec.census.run_reconcile_pass)
    assert rec.wanted_markets() == set()
    # It is still in the census: the recorder records that it existed.
    assert "KXMLBGAME-M01" in rec.census.entries
    await finish(rec)


# -- Process restarts are visible ---------------------------------------------


async def test_each_process_start_is_logged_and_orphans_are_recovered(tmp_path):
    first = make_recorder(tmp_path, collection_end_utc="2020-01-01T00:00:00Z")
    await first.run()

    # A process that dies without finalizing leaves an OPEN segment behind.
    crashed = await bring_up(make_recorder(tmp_path))
    crashed.segment.store.checkpoint_truncate()
    crashed.segment.store.close()
    crashed._writer.stop()

    third = make_recorder(tmp_path, collection_end_utc="2020-01-01T00:00:00Z")
    await third.run()

    states = {m["segment_name"]: m["segment_state"]
              for m in sg.list_manifests(tmp_path)}
    assert states[crashed.segment.name] == frozen.SEGMENT_INCOMPLETE_CRASH
    assert list(states.values()).count(frozen.SEGMENT_COMPLETE) == 2

    starts = (tmp_path / "process_starts.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(starts) == 3
    assert len({json.loads(s)["run_id"] for s in starts}) == 3


async def test_an_unexpected_failure_is_not_labelled_complete(tmp_path):
    """
    A crash must not be filed under the one state the health check reads as
    healthy. It is finalized as INCOMPLETE_CRASH and re-raised for systemd.
    """
    plenty = ample_disk()

    def broken_disk(path, reserve):
        if broken_disk.calls:
            raise RuntimeError("simulated recorder fault")
        broken_disk.calls.append(1)
        return plenty(path, reserve)

    broken_disk.calls = []

    rec = make_recorder(tmp_path, disk_check=broken_disk)
    with pytest.raises(RuntimeError, match="simulated recorder fault"):
        await rec.run()

    m = rec.final_manifest
    assert m["segment_state"] == frozen.SEGMENT_INCOMPLETE_CRASH
    assert m["close_reason"] == "unexpected_failure"
    assert rec.status.exit_code == frozen.EXIT_FAILURE


# -- Startup ordering: banking never waits for discovery ----------------------


class SlowDiscoveryAPI(FakeAPI):
    """A catalog probe that takes its time, like a real ~13.8k-series sweep."""

    def __init__(self, *a, events_delay=0.0, **kw):
        super().__init__(*a, **kw)
        self.events_delay = events_delay
        self.events_started = None
        self.events_finished = None

    def get_raw(self, path, params=None):
        if path == "/events":
            if self.events_started is None:
                self.events_started = time.monotonic()
            time.sleep(self.events_delay)
            self.events_finished = time.monotonic()
        return super().get_raw(path, params)


async def test_a_slow_full_universe_discovery_does_not_delay_ws_recording(tmp_path):
    """
    The load-bearing startup property. A ~25-minute sweep must never sit in
    front of the first banked frame, on a cold start or a restart.
    """
    api = SlowDiscoveryAPI(
        catalog=["KXMLBGAME", "KXSLOWA", "KXSLOWB"],
        events={t: [event(t, "Pro Baseball")] for t in
                ("KXMLBGAME", "KXSLOWA", "KXSLOWB")},
        markets={"KXMLBGAME": [market("KXMLBGAME", 1)]},
        events_delay=1.0,
    )
    rec = make_recorder(tmp_path, api=api, discovery=True,
                        discovery_daily_target_utc="00:00:00")

    started = time.monotonic()
    ws_up: dict[str, float] = {}

    async def watch_and_feed():
        await rec._ws_recording_started.wait()
        ws_up["at"] = time.monotonic()
        ws_up["discovery_done"] = api.events_finished is not None
        for s in rec.sessions.values():
            for i in range(25):
                s.feed(frame("KXMLBGAME-M01", i))

    task = asyncio.ensure_future(watch_and_feed())
    await rec.run(stop_after_seconds=6.0)
    await task

    # Banking came up long before the sweep could have finished (3 series x 1 s
    # serialised across the shared read budget).
    assert ws_up["at"] - started < 1.0, "WS recording waited on discovery"
    assert ws_up["discovery_done"] is False
    # And the frames written while the sweep was still running are in the archive.
    assert rec.final_manifest["raw_frame_count"] == 25
    # The sweep did run, afterwards, and completed.
    assert rec.status.discovery_censuses == 1
    assert api.events_finished is not None
    assert api.events_finished > ws_up["at"]


async def test_discovery_waits_for_banking_even_when_already_overdue(tmp_path):
    """A restart at 14:00 with the 09:00 target long past still banks first."""
    api = SlowDiscoveryAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [market("KXMLBGAME", 1)]},
        events_delay=0.5,
    )
    rec = make_recorder(tmp_path, api=api, discovery=True,
                        discovery_daily_target_utc="00:00:00")
    order: list[str] = []

    async def watch():
        await rec._ws_recording_started.wait()
        order.append("ws")
        while api.events_finished is None:
            await asyncio.sleep(0.01)
        order.append("discovery")

    task = asyncio.ensure_future(watch())
    await rec.run(stop_after_seconds=4.0)
    await task
    assert order == ["ws", "discovery"]


# -- The restart-storm guard, through the recorder ----------------------------


async def test_the_recorder_refuses_a_second_sweep_within_the_guard(tmp_path):
    rec = await bring_up(make_recorder(tmp_path, discovery=True))
    first = await rec.run_discovery_once()
    assert first is not None and first.complete

    second = await rec.run_discovery_once()
    assert second is None, "a second sweep started inside the frozen 3600 s guard"
    assert rec.status.discovery_censuses == 1
    await finish(rec)


async def test_the_guard_survives_a_process_restart(tmp_path):
    """
    A crash loop must not begin a fresh sweep on every restart. The attempt-start
    timestamp lives on disk, so a brand-new recorder object over the same archive
    is still guarded.
    """
    first = await bring_up(make_recorder(tmp_path, discovery=True))
    await first.run_discovery_once()
    await finish(first)

    for _ in range(3):                       # Restart=always, RestartSec=5
        restarted = await bring_up(make_recorder(tmp_path, discovery=True))
        assert await restarted.run_discovery_once() is None
        assert restarted.schedule.blocked_by_guard(time.time()) > 0
        await finish(restarted)

    state = json.loads(
        (tmp_path / "discovery_schedule.state.json").read_text(encoding="utf-8")
    )
    assert state["last_attempt_started_ts"] is not None
    assert len(state["accepted_days"]) == 1


async def test_an_incomplete_sweep_leaves_the_day_unaccepted(tmp_path):
    api = make_api(("KXMLBGAME", "KXFLAKY"), unreadable={"KXFLAKY"})
    rec = await bring_up(make_recorder(tmp_path, api=api, discovery=True))
    result = await rec.run_discovery_once()

    assert result is not None and not result.complete
    day = sg.utc_date_of(time.time())
    assert rec.schedule.day_is_accepted(day) is False
    # Still guarded, so the retry is an hour away, not immediate.
    assert rec.schedule.blocked_by_guard(time.time()) > 0
    await finish(rec)


# -- One shared REST read budget ----------------------------------------------


def test_discovery_and_market_census_share_one_read_bucket(tmp_path):
    """
    Both paths funnel through the SAME ReadOnlyAPI token bucket, so the two
    cannot each independently consume the nominal 20 reads/sec.
    """
    from threadc.readonly import CensusClientAdapter, ReadOnlyAPI, _NullBucket

    api = ReadOnlyAPI.__new__(ReadOnlyAPI)       # no credentials needed
    api.read_bucket = object()
    adapter = CensusClientAdapter(api)

    # The census reaches the exchange only through this adapter, and the adapter
    # reaches it only through the same ReadOnlyAPI instance.
    assert adapter._markets_api._api is api
    # RestCensus acquires client._read_bucket before each call; that one is a
    # no-op precisely so the single real limiter inside ReadOnlyAPI.get_raw is
    # the only thing rate-limiting either path.
    assert isinstance(adapter._read_bucket, _NullBucket)


async def test_one_recorder_holds_exactly_one_api_and_one_limiter(tmp_path):
    rec = make_recorder(tmp_path)
    assert rec.census._client.api is rec.api
    assert rec.census._client._markets_api._api is rec.api
    # The discovery sweep is handed the same object the census writes through.
    import inspect
    src = inspect.getsource(rec.run_discovery_once)
    assert "self.api" in src


def test_the_shared_bucket_actually_bounds_the_aggregate_rate():
    """
    Concurrent probe threads and the census thread together cannot exceed the
    configured read rate, because they contend for one bucket.
    """
    import threading
    from threadc.readonly import TokenBucket

    bucket = TokenBucket(20)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(6):
            bucket.acquire()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(stamps) == 30
    elapsed = time.monotonic() - start
    # 30 acquisitions with a 20-token burst and 20/s refill cannot finish
    # sooner than the 10 tokens beyond the burst take to accrue.
    assert elapsed >= 0.4, f"aggregate rate was not bounded ({elapsed:.3f}s)"


# -- Which channels a production run banks, and where it says so --------------
#
# Production selects ticker-only, on the Branch-2 capacity measurement. Every
# place the recorder writes the selection down has to say which selection it
# was, because a ticker-only segment read years later as a two-channel segment
# would look like a market in which nothing ever traded.


async def test_a_production_run_with_no_channels_key_banks_the_frozen_pair(tmp_path):
    """
    Absence is still the frozen pair -- and is still not neutral, which is why
    config/threadc.json states its selection rather than relying on this.
    """
    rec = await bring_up(make_recorder(tmp_path))
    assert rec.cfg.mode == frozen.MODE_PRODUCTION
    assert rec.cfg.channels == frozen.CHANNELS

    startup = rec.startup_record()
    assert startup["channels"] == ["ticker", "trade"]
    assert startup["channels_are_frozen_pair"] is True
    assert startup["config"]["channels"] == ["ticker", "trade"]

    row = rec.segment.store.query("SELECT channels FROM ws_session")[0]
    assert json.loads(row["channels"]) == ["ticker", "trade"]
    assert json.loads(
        rec.segment.manifest_path.read_text(encoding="utf-8")
    )["channels"] == ["ticker", "trade"]


async def test_a_production_ticker_only_run_identifies_itself_everywhere(tmp_path):
    """
    The startup record, the OPEN manifest, the segment's own metadata, the
    ws_session row and its subscribe params, and the final manifest.
    """
    rec = make_recorder(tmp_path, channels=["ticker"])
    assert rec.cfg.mode == frozen.MODE_PRODUCTION
    assert rec.cfg.channels == ("ticker",)
    assert rec.cfg.channels_are_frozen_pair is False

    startup = rec.startup_record()
    assert startup["channels"] == ["ticker"]
    assert startup["channels_are_frozen_pair"] is False
    assert startup["config"]["channels"] == ["ticker"]

    await bring_up(rec)

    open_manifest = json.loads(rec.segment.manifest_path.read_text(encoding="utf-8"))
    assert open_manifest["segment_state"] == frozen.SEGMENT_OPEN
    assert open_manifest["channels"] == ["ticker"]

    meta = rec.segment.store.query(
        "SELECT value FROM meta WHERE key = 'channels'")[0]["value"]
    assert json.loads(meta) == ["ticker"]

    row = rec.segment.store.query("SELECT channels, subscribe_params FROM ws_session")[0]
    assert json.loads(row["channels"]) == ["ticker"]
    assert json.loads(row["subscribe_params"])["channels"] == ["ticker"]

    for s in rec._test_sessions:
        s.feed(frame("KXMLBGAME-M01", 1))
    final = await finish(rec)
    assert final["channels"] == ["ticker"]
    assert final["raw_frame_count"] == len(rec._test_sessions)

    # The startup record is persisted, so the archive itself carries the
    # selection this process came up on and not only the log.
    logged = [
        json.loads(line)
        for line in (tmp_path / rec_mod.PROCESS_LOG_FILE).read_text(
            encoding="utf-8").splitlines()
    ]
    assert logged[-1]["channels"] == ["ticker"]


async def test_the_readiness_line_names_the_selection_that_came_up(tmp_path,
                                                                   monkeypatch):
    """
    "recording started" has to say WHAT is being banked. An operator watching a
    production start should see ticker-only there, without opening a manifest.
    """
    emitted: list[tuple[str, dict]] = []

    class RecordingLog:
        def __getattr__(self, level):
            def emit(event, **kw):
                emitted.append((event, kw))
            return emit

    monkeypatch.setattr(rec_mod, "log", RecordingLog())

    rec = make_recorder(tmp_path, channels=["ticker"])

    async def feed_once():
        await rec._ws_recording_started.wait()
        for s in rec.sessions.values():
            s.feed(frame("KXMLBGAME-M01", 1))

    task = asyncio.ensure_future(feed_once())
    await rec.run(stop_after_seconds=2.0)
    await task

    ready = [kw for name, kw in emitted if name == "threadc.ws_recording_started"]
    assert ready, "no readiness line was emitted"
    assert ready[0]["channels"] == ["ticker"]
    assert ready[0]["channels_are_frozen_pair"] is False

    assert rec.final_manifest["channels"] == ["ticker"]
    assert rec.final_manifest["raw_frame_count"] >= 1

    # The daily health check deliberately does NOT repeat the selection: its
    # output is pinned to four facts and no token that reads as market data,
    # and a channel literally named "ticker" is indistinguishable from a market
    # ticker in that output. The evidence lives in the manifests it reads, and
    # tests/test_threadc_health.py holds that line.
    rep = health.report(tmp_path, 1024, service="nonexistent.service",
                        day=rec.final_manifest["utc_date"])
    assert len(rep.day_segments) == 1
    assert "channels" not in rep.day_segments[0]


def test_a_channel_outside_the_frozen_pair_never_reaches_a_production_run(tmp_path):
    """
    Fails closed while building the configuration, so no segment is opened and
    nothing subscribes. Widening what the bank preserves stays a code change to
    threadc.frozen.CHANNELS.
    """
    with pytest.raises(tc.ConfigError, match="not one of the frozen channels"):
        make_recorder(tmp_path, channels=["ticker", "orderbook_delta"])
    assert not (tmp_path / "segments").exists()
