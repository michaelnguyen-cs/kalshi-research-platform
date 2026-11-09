"""
Section 8's skew rule, evaluated at the resolution it actually needs.

The defect this pins: StreamHistory was fed only from _compare_at, once per
1 Hz grid tick. The frozen rule asks whether a lagging stream reached the
other's canonical top-of-book within 500 ms -- and observations one second
apart cannot answer a 500 ms question. Every genuine sub-second skew came back
UNEXPLAINED, and section 8 fails on ANY unexplained disagreement, so the test
would have failed spuriously on fast books. A 240-second probe over 3,702
markets logged roughly 20-30 such disagreements, nearly all same-price
different-size on active markets.

The fix is resolution, not tolerance. The 1 Hz comparison grid is untouched and
remains the only source of matched comparison cells; the 500 ms tolerance is
untouched. History is now recorded per book-changing message.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.book import TopOfBook  # noqa: E402
from depthrec.equivalence import (  # noqa: E402
    HISTORY_MAX_ENTRIES,
    HISTORY_SECONDS,
    RESOLUTION_SKEW,
    RESOLUTION_UNEXPLAINED,
    EquivalenceTest,
    StreamHistory,
    _key,
)

# Canonical top-of-book states, as the ring stores them.
OLD = (6800, 500, 7100, 4000)
NEW = (6800, 500, 7100, 3500)
OTHER = (6700, 100, 7200, 900)


def _bare(tmp_path, monkeypatch) -> EquivalenceTest:
    class FakeBucket:
        def acquire(self): pass

    class FakeClient:
        def __init__(self, *a, **k):
            self._read_bucket = FakeBucket()
            self._markets_api = None
        def close(self): pass

    monkeypatch.setattr("depthrec.equivalence.KalshiClient", FakeClient)
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    return EquivalenceTest(db_path=tmp_path / "skew.sqlite")


def _resolve(test, *, a_state, b_state, at_mono, ticker="T1"):
    return test._resolve(
        {"ticker": ticker, "a": a_state, "b": b_state, "at": 1_000_000.0,
         "at_mono": at_mono},
        grid_ts=1_000_000.0,
    )


# -- The four required cases ---------------------------------------------------

def test_a_200ms_catch_up_resolves_as_explained_skew(tmp_path, monkeypatch):
    """
    A is ahead at the grid tick; B reaches A's state 200 ms later. Inside the
    frozen 500 ms tolerance, so this is benign asynchronous observation and must
    NOT be counted against the decoder.
    """
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()
    a.observe(100.000, 1.0, NEW)          # A already moved
    b.observe(99.900, 0.9, OLD)           # B still on the old state at the tick
    b.observe(100.200, 1.2, NEW)          # ... catches up 200 ms later

    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OLD, at_mono=100.0)
    assert resolution == RESOLUTION_SKEW
    assert skew_ms == pytest.approx(200.0)


def test_a_501ms_catch_up_does_not_resolve(tmp_path, monkeypatch):
    """One millisecond past the frozen tolerance is outside it."""
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()
    a.observe(100.000, 1.0, NEW)
    b.observe(99.900, 0.9, OLD)
    b.observe(100.501, 1.5, NEW)

    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OLD, at_mono=100.0)
    assert resolution == RESOLUTION_UNEXPLAINED
    assert skew_ms is None


def test_a_catch_up_within_200ms_through_a_contradictory_top_does_not_resolve(
    tmp_path, monkeypatch
):
    """
    The frozen rule requires no intervening contradictory top-of-book state. A
    stream that passes through a third state on its way is not merely behind --
    the two streams genuinely saw different books.
    """
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()
    a.observe(100.000, 1.0, NEW)
    b.observe(99.900, 0.9, OLD)
    b.observe(100.050, 1.05, OTHER)       # contradictory
    b.observe(100.200, 1.2, NEW)

    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OLD, at_mono=100.0)
    assert resolution == RESOLUTION_UNEXPLAINED
    assert skew_ms is None


def test_a_true_canonical_disagreement_is_unexplained(tmp_path, monkeypatch):
    """Neither stream ever reaches the other's state. That is a real decoder
    disagreement and must fail section 8."""
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()
    a.observe(99.900, 0.9, NEW)
    b.observe(99.900, 0.9, OTHER)
    a.observe(100.100, 1.1, NEW)          # A stays put
    b.observe(100.100, 1.1, OTHER)        # B stays put

    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OTHER, at_mono=100.0)
    assert resolution == RESOLUTION_UNEXPLAINED
    assert skew_ms is None


# -- Either stream may lag ------------------------------------------------------

def test_the_rule_is_symmetric(tmp_path, monkeypatch):
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()
    b.observe(100.000, 1.0, NEW)
    a.observe(99.900, 0.9, OLD)
    a.observe(100.300, 1.3, NEW)          # A is the laggard this time

    resolution, skew_ms = _resolve(test, a_state=OLD, b_state=NEW, at_mono=100.0)
    assert resolution == RESOLUTION_SKEW
    assert skew_ms == pytest.approx(300.0)


# -- The ring itself -------------------------------------------------------------

def test_history_is_event_driven_not_grid_sampled():
    """
    Two changes 200 ms apart must produce two entries. Under the old 1 Hz
    sampling they would have collapsed into one, which is exactly why a 500 ms
    rule could not be evaluated.
    """
    h = StreamHistory()
    h.observe(100.0, 1.0, OLD)
    h.observe(100.2, 1.2, NEW)
    assert len(h.entries) == 2
    assert h.reaches(NEW, after=100.0, within_ms=500) == pytest.approx(200.0)


def test_identical_consecutive_states_are_collapsed():
    """A delta that churns an inner level without moving the top is not an
    observation; keeping it would fill the ring with noise."""
    h = StreamHistory()
    for i in range(500):
        h.observe(100.0 + i * 0.001, 1.0, OLD)
    assert len(h.entries) == 1


def test_the_ring_expires_by_time_and_keeps_the_window():
    h = StreamHistory()
    h.observe(100.0, 1.0, OLD)
    h.observe(100.4, 1.4, NEW)
    h.observe(100.0 + HISTORY_SECONDS + 0.5, 3.0, OTHER)
    # Everything older than HISTORY_SECONDS is gone, newest is kept.
    assert [e[2] for e in h.entries] == [OTHER]
    assert HISTORY_SECONDS >= 4 * (frozen.EQUIV_SKEW_TOLERANCE_MS / 1000.0)


def test_the_hard_cap_bounds_the_ring_and_is_counted():
    """
    The cap is a burst guard, not a sampling policy -- if it bites, evidence is
    being discarded and that must be visible rather than silent.
    """
    h = StreamHistory()
    for i in range(HISTORY_MAX_ENTRIES + 250):
        # Distinct states, all within the time window, so only the cap can trim.
        h.observe(100.0 + i * 1e-6, 1.0, (6800, 500, 7100, i))
    assert len(h.entries) == HISTORY_MAX_ENTRIES
    assert h.dropped_to_cap == 250


def test_the_cap_cannot_truncate_the_500ms_window_at_supported_load():
    """
    Busiest single ticker measured today: KXBTC15M at 206 frames/s in its final
    quarter hour. Only top-of-book CHANGES become entries, so 500 ms needs at
    most ~103 -- an order of magnitude under the cap.
    """
    worst_frames_per_second = 206
    needed = worst_frames_per_second * (frozen.EQUIV_SKEW_TOLERANCE_MS / 1000.0)
    assert HISTORY_MAX_ENTRIES > 10 * needed


def test_elapsed_time_uses_the_monotonic_clock(tmp_path, monkeypatch):
    """
    A wall clock can step under NTP; the 500 ms rule is an elapsed-time test and
    must not be affected. The ring stores both, and resolution reads the
    monotonic one -- so a wall-clock jump of an hour changes nothing.
    """
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()
    a.observe(100.000, 1_000.0, NEW)
    b.observe(99.900, 999.9, OLD)
    b.observe(100.200, 999.9 + 3600.0, NEW)   # wall clock jumped an hour forward

    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OLD, at_mono=100.0)
    assert resolution == RESOLUTION_SKEW
    assert skew_ms == pytest.approx(200.0)


def test_wall_clock_is_retained_for_provenance():
    h = StreamHistory()
    h.observe(100.0, 1_787_400_000.5, OLD)
    mono, wall, state = h.entries[0]
    assert mono == 100.0 and wall == 1_787_400_000.5 and state == OLD


# -- The session feeds it, and only from book messages --------------------------

def test_the_session_reports_state_only_for_book_changing_messages(monkeypatch):
    """
    Ping/Pong, acks and dropped deltas must not create observations -- they are
    not book state, and a phantom entry could resolve a real disagreement as
    skew.
    """
    import asyncio
    import json as _json

    from depthrec.session import ResearchSession

    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    seen: list[tuple] = []
    s = ResearchSession(
        tickers=["T1"], use_yes_price=True,
        on_book_state=lambda t, top, w, m: seen.append((t, _key(top), w, m)),
    )
    s.connected = True
    s.session_epoch = 1
    s.last_activity_ts = 1000.0

    def feed(msg):
        asyncio.run(s._dispatch(_json.dumps(msg)))

    feed({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
          "msg": {"market_ticker": "T1", "yes_dollars_fp": [["0.6800", "5.00"]],
                  "no_dollars_fp": [["0.7100", "40.00"]]}})
    assert len(seen) == 1
    assert seen[0][1] == (6800, 500, 7100, 4000)

    feed({"type": "orderbook_delta", "sid": 1, "seq": 2,
          "msg": {"market_ticker": "T1", "side": "no",
                  "price_dollars": "0.7100", "delta_fp": "-5.00"}})
    assert len(seen) == 2
    assert seen[1][1] == (6800, 500, 7100, 3500)

    # Non-book traffic: an ack carrying sid+seq. No new observation.
    feed({"type": "ok", "id": 3, "sid": 1, "seq": 3, "msg": {"market_tickers": ["T1"]}})
    assert len(seen) == 2

    # A delta after a sequence gap is dropped, so it must not be observed.
    feed({"type": "orderbook_delta", "sid": 1, "seq": 9,
          "msg": {"market_ticker": "T1", "side": "no",
                  "price_dollars": "0.7100", "delta_fp": "-1.00"}})
    feed({"type": "orderbook_delta", "sid": 1, "seq": 10,
          "msg": {"market_ticker": "T1", "side": "no",
                  "price_dollars": "0.7100", "delta_fp": "-1.00"}})
    assert len(seen) == 2, "a dropped delta must not produce a state observation"

    # Monotonic timestamps are strictly non-decreasing and distinct from wall.
    assert seen[1][3] >= seen[0][3]


def test_the_state_handed_over_is_canonical_yes_coordinates(monkeypatch):
    """
    Both conventions must produce the SAME ring entries for the same book --
    otherwise the skew rule would be comparing two coordinate systems.
    """
    import asyncio
    import json as _json

    from depthrec.session import ResearchSession

    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    out = {}
    for label, use_yes, no_price in (("A", True, "0.7100"), ("B", False, "0.2900")):
        seen: list = []
        s = ResearchSession(tickers=["T1"], use_yes_price=use_yes, label=label,
                            on_book_state=lambda t, top, w, m, s=seen: s.append(_key(top)))
        s.connected = True
        s.session_epoch = 1
        s.last_activity_ts = 1000.0
        asyncio.run(s._dispatch(_json.dumps(
            {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
             "msg": {"market_ticker": "T1", "yes_dollars_fp": [["0.6800", "5.00"]],
                     "no_dollars_fp": [[no_price, "40.00"]]}})))
        out[label] = seen
    assert out["A"] == out["B"] == [(6800, 500, 7100, 4000)]


# -- The evicted-comparison-state defect -----------------------------------------

def test_an_evicted_comparison_state_refuses_to_resolve(tmp_path, monkeypatch):
    """
    A quiet market whose last top-of-book change predates the ring horizon.

    Timing and state sequence taken from a real traced case in the 240-second
    engineering probe of 2026-08-23 (see the evidence README for the raw frame
    ids): B's comparison-instant state was set 3,763.6 ms before the tick, so
    the first append after the tick expired it and no entry at or before the
    comparison instant survives. A contradictory top then appears at +371 ms and
    A's state is reached at +492.3 ms -- inside the 500 ms tolerance.

    With no witness to the comparison instant the contradiction cannot be ruled
    out, so the resolver must refuse to explain. Before the fix `seen_start`
    stayed False, the contradiction check never ran, and this returned an
    explained skew at 492.3 ms.
    """
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()

    at = 100.0
    a.observe(at - 0.100, 1.0, NEW)           # A is at NEW when the tick fires

    # B last changed well before the ring horizon, then sat still.
    b.observe(at - 3.7636, 0.1, OLD)
    assert [e[2] for e in b.entries] == [OLD]
    # The next append expires it: OLD is older than HISTORY_SECONDS.
    b.observe(at + 0.371, 1.4, OTHER)         # contradictory top
    b.observe(at + 0.4923, 1.5, NEW)          # A's state, inside 500 ms

    assert all(e[0] > at for e in b.entries), (
        "the fixture must leave no ring entry at or before the comparison "
        "instant, which is the condition under test"
    )

    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OLD, at_mono=at)
    assert resolution == RESOLUTION_UNEXPLAINED
    assert skew_ms is None


def test_an_evicted_comparison_state_refuses_even_with_no_contradiction(
    tmp_path, monkeypatch
):
    """
    Fail-closed, not contradiction-detection-by-other-means.

    Same eviction, but the only post-tick state IS the target. The ring still
    cannot witness the comparison instant -- anything could have happened in the
    interval it no longer holds -- so this must not resolve either. Seeding
    `started_in` from the earliest survivor would wrongly explain this one.
    """
    test = _bare(tmp_path, monkeypatch)
    test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()

    at = 100.0
    test._history[("A", "T1")].observe(at - 0.100, 1.0, NEW)
    b.observe(at - 3.7636, 0.1, OLD)
    b.observe(at + 0.200, 1.2, NEW)

    assert all(e[0] > at for e in b.entries)
    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OLD, at_mono=at)
    assert resolution == RESOLUTION_UNEXPLAINED
    assert skew_ms is None


def test_a_surviving_comparison_state_still_resolves(tmp_path, monkeypatch):
    """
    The fix must not swallow legitimate catch-ups: the same 492.3 ms arrival,
    with the comparison-instant state still in the ring and no contradiction,
    resolves exactly as before.
    """
    test = _bare(tmp_path, monkeypatch)
    a = test._history[("A", "T1")] = StreamHistory()
    b = test._history[("B", "T1")] = StreamHistory()

    at = 100.0
    a.observe(at - 0.100, 1.0, NEW)
    b.observe(at - 0.300, 0.7, OLD)           # inside the horizon, survives
    b.observe(at + 0.4923, 1.5, NEW)

    assert any(e[0] <= at for e in b.entries)
    resolution, skew_ms = _resolve(test, a_state=NEW, b_state=OLD, at_mono=at)
    assert resolution == RESOLUTION_SKEW
    assert skew_ms == pytest.approx(492.3, abs=0.5)


def test_an_empty_ring_refuses_to_resolve():
    """The degenerate case of the same rule: no evidence, no explanation."""
    assert StreamHistory().reaches(NEW, after=100.0, within_ms=500.0) is None


# -- Nothing frozen moved --------------------------------------------------------

def test_no_frozen_protocol_parameter_changed():
    assert frozen.EQUIV_GRID_HZ == 1.0
    assert frozen.EQUIV_SKEW_TOLERANCE_MS == 500
    assert frozen.EQUIV_PASS_AGREEMENT == 0.999
    assert frozen.EQUIV_MIN_COMPARISONS == 10_000
    assert frozen.EQUIV_MIN_MINUTES == 60
    assert frozen.EQUIV_MAX_MINUTES == 240
