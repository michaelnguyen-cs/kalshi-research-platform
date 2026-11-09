"""
Section 8's equivalence machinery: the B -> YES transform, the skew rule and the
verdict.

The network part cannot be unit tested, but everything that decides PASS / FAIL
/ INSUFFICIENT can be, and it is the part where being wrong is expensive: a skew
rule that is too generous would let a real decoder bug through with a "PASS".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.book import CanonicalBook  # noqa: E402
from depthrec.equivalence import (  # noqa: E402
    RESOLUTION_SKEW,
    RESOLUTION_UNEXPLAINED,
    EquivalenceTest,
    StreamHistory,
    _key,
)


def book(yes=None, no=None, *, use_yes_price=True):
    b = CanonicalBook(ticker="T")
    msg = {"market_ticker": "T"}
    if yes:
        msg["yes_dollars_fp"] = yes
    if no:
        msg["no_dollars_fp"] = no
    b.apply_snapshot(msg, seq=1, sid=1, recv_ts=0.0, use_yes_price=use_yes_price)
    return b


# -- The transform ---------------------------------------------------------------

def test_the_two_conventions_reconstruct_the_same_canonical_book():
    """
    A resting NO bid quoted at 0.29 (no-leg) and at 0.71 (yes-leg) is the same
    offer. This is the whole of 'transform B into YES coordinates'.
    """
    a = book(yes=[["0.6900", "12.00"]], no=[["0.7100", "40.00"]], use_yes_price=True)
    b = book(yes=[["0.6900", "12.00"]], no=[["0.2900", "40.00"]], use_yes_price=False)
    assert _key(a.top_of_book()) == _key(b.top_of_book())


def test_deci_cent_levels_above_ninety_cents_survive_the_transform():
    """Section 8 widened the universe specifically to exercise this regime."""
    a = book(no=[["0.9950", "3.00"]], use_yes_price=True)
    b = book(no=[["0.0050", "3.00"]], use_yes_price=False)
    assert _key(a.top_of_book()) == _key(b.top_of_book())
    assert a.top_of_book().ask_price == 9950


def test_a_genuine_convention_bug_would_show_as_a_disagreement():
    """If B were decoded WITHOUT the transform, the tops must differ."""
    a = book(no=[["0.7100", "40.00"]], use_yes_price=True)
    wrong = book(no=[["0.2900", "40.00"]], use_yes_price=True)   # transform skipped
    assert _key(a.top_of_book()) != _key(wrong.top_of_book())


# -- The skew rule -----------------------------------------------------------------

def test_the_lagging_stream_catching_up_in_time_is_explainable():
    hist = StreamHistory()
    hist.observe(100.0, 100.0, ("a",))
    hist.observe(100.2, 100.2, ("b",))
    assert hist.reaches(("b",), after=100.0, within_ms=500) == pytest.approx(200.0)


def test_catching_up_too_late_is_not_explainable():
    hist = StreamHistory()
    hist.observe(100.0, 100.0, ("a",))
    hist.observe(100.9, 100.9, ("b",))
    assert hist.reaches(("b",), after=100.0, within_ms=500) is None


def test_an_intervening_contradictory_state_breaks_the_explanation():
    """
    The frozen rule requires no intervening contradictory top-of-book state. A
    stream that passes through a third state on its way is not simply behind.
    """
    hist = StreamHistory()
    hist.observe(100.0, 100.0, ("a",))
    hist.observe(100.1, 100.1, ("c",))      # contradictory
    hist.observe(100.2, 100.2, ("b",))
    assert hist.reaches(("b",), after=100.0, within_ms=500) is None


def test_a_state_never_reached_is_not_explainable():
    hist = StreamHistory()
    hist.observe(100.0, 100.0, ("a",))
    assert hist.reaches(("b",), after=100.0, within_ms=500) is None


def test_history_only_records_changes():
    hist = StreamHistory()
    for i in range(50):
        hist.observe(100.0 + i * 0.01, 100.0 + i * 0.01, ("a",))
    assert len(hist.entries) == 1


def test_either_stream_may_be_the_one_behind(tmp_path, monkeypatch):
    test = _bare_test(tmp_path, monkeypatch)
    test.tickers = ["T"]
    a_hist, b_hist = StreamHistory(), StreamHistory()
    test._history[("A", "T")] = a_hist
    test._history[("B", "T")] = b_hist

    # B is behind and catches up to A.
    a_hist.observe(100.0, 100.0, ("new",))
    b_hist.observe(100.0, 100.0, ("old",))
    b_hist.observe(100.3, 100.3, ("new",))
    resolution, skew = test._resolve({"ticker": "T", "a": ("new",), "b": ("old",),
                                      "at": 100.0, "at_mono": 100.0}, 100.0)
    assert resolution == RESOLUTION_SKEW and skew == pytest.approx(300.0)

    # A is behind and catches up to B.
    a2, b2 = StreamHistory(), StreamHistory()
    test._history[("A", "T")] = a2
    test._history[("B", "T")] = b2
    a2.observe(200.0, 200.0, ("old",))
    a2.observe(200.4, 200.4, ("new",))
    b2.observe(200.0, 200.0, ("new",))
    resolution, skew = test._resolve({"ticker": "T", "a": ("old",), "b": ("new",),
                                      "at": 200.0, "at_mono": 200.0}, 200.0)
    assert resolution == RESOLUTION_SKEW and skew == pytest.approx(400.0)


def test_a_persistent_disagreement_is_unexplained(tmp_path, monkeypatch):
    test = _bare_test(tmp_path, monkeypatch)
    test._history[("A", "T")] = StreamHistory()
    test._history[("B", "T")] = StreamHistory()
    test._history[("A", "T")].observe(100.0, 100.0, ("a",))
    test._history[("B", "T")].observe(100.0, 100.0, ("b",))
    test._history[("A", "T")].observe(101.0, 101.0, ("a2",))
    test._history[("B", "T")].observe(101.0, 101.0, ("b2",))
    resolution, skew = test._resolve({"ticker": "T", "a": ("a",), "b": ("b",),
                                      "at": 100.0, "at_mono": 100.0}, 100.0)
    assert resolution == RESOLUTION_UNEXPLAINED and skew is None


# -- The verdict ---------------------------------------------------------------------

def verdict(comparisons, agreements, unexplained):
    """The section 8 decision, exactly as _compare_loop applies it."""
    rate = agreements / comparisons if comparisons else 0.0
    if comparisons < frozen.EQUIV_MIN_COMPARISONS:
        return frozen.EQUIV_RESULT_INSUFFICIENT
    if unexplained == 0 and rate >= frozen.EQUIV_PASS_AGREEMENT:
        return frozen.EQUIV_RESULT_PASS
    return frozen.EQUIV_RESULT_FAIL


def test_pass_requires_both_conditions():
    assert verdict(10_000, 10_000, 0) == frozen.EQUIV_RESULT_PASS
    assert verdict(10_000, 9_995, 0) == frozen.EQUIV_RESULT_PASS      # 99.95%, all skew


def test_a_single_unexplained_disagreement_fails_even_at_a_high_rate():
    """
    Condition 2 fails on ANY unexplained disagreement. The 99.9% bar is not
    permission to tolerate parser errors.
    """
    assert verdict(10_000, 9_999, 1) == frozen.EQUIV_RESULT_FAIL
    assert verdict(1_000_000, 999_999, 1) == frozen.EQUIV_RESULT_FAIL


def test_below_the_agreement_bar_fails_even_with_nothing_unexplained():
    assert verdict(10_000, 9_000, 0) == frozen.EQUIV_RESULT_FAIL


def test_too_few_comparisons_is_insufficient_not_a_pass():
    assert verdict(9_999, 9_999, 0) == frozen.EQUIV_RESULT_INSUFFICIENT


def test_the_universe_is_sorted_series_then_ticker(tmp_path, monkeypatch):
    """
    Section 8's tie-break for the case where N binds, applied unconditionally so
    the attempted order is deterministic and no selection can depend on observed
    decoder agreement.
    """
    test = _bare_test(tmp_path, monkeypatch)

    class Entry:
        def __init__(self, series, ticker):
            self.series, self.ticker = series, ticker

    test.census.entries = {
        "b": Entry("KXETH15M", "B-2"),
        "a": Entry("KXETH15M", "B-1"),
        "c": Entry("KXBTC15M", "A-9"),
    }
    monkeypatch.setattr(test.census, "run_live_pass", lambda **_k: [])
    assert test.build_universe() == ["A-9", "B-1", "B-2"]


# -- Section 13 amendment, 2026-08-22: what "continuous" means ----------------

def test_the_duration_clock_is_stamped_once_and_never_reset(tmp_path, monkeypatch):
    """
    "Continuous minutes" is continuous execution of the process, not continuous
    socket uptime. A disconnect must not restart the 60-minute clock, so
    `started` is read exactly once in _compare_loop.
    """
    import inspect

    from depthrec import equivalence as eq

    src = inspect.getsource(eq.EquivalenceTest._compare_loop)
    assert src.count("started = time.time()") == 1
    # Nothing in the loop body may re-stamp it.
    body = src.split("while True:", 1)[1]
    assert "started =" not in body


def test_an_unmatched_cell_enters_neither_numerator_nor_denominator(tmp_path, monkeypatch):
    """
    While either stream is not VALID the cell is unmatched. It is counted as a
    diagnostic and excluded from the agreement rate entirely -- so a flaky
    connection cannot manufacture agreement, only fail to accumulate.
    """
    test = _bare_test(tmp_path, monkeypatch)
    a, b = _fake_streams(test, {
        "T-BOTH": (True, True),
        "T-A-ONLY": (True, False),
        "T-NEITHER": (False, False),
    })
    matched, agreed, unmatched, pending = test._compare_at(100.0)
    assert matched == 1 and agreed == 1 and unmatched == 2 and pending == []


def test_diagnostics_accumulate_ticks_cells_and_unmatched(tmp_path, monkeypatch):
    from depthrec.equivalence import AvailabilityDiagnostics

    diag = AvailabilityDiagnostics()
    diag.ticks = 10
    diag.grid_cells = 1000
    diag.unmatched_cells = 250
    assert diag.unmatched_fraction == 0.25
    assert diag.as_dict()["unmatched_fraction"] == 0.25
    assert "NOT part of the verdict" in diag.summary()


def test_disconnects_and_reconnect_durations_are_recorded(tmp_path, monkeypatch):
    """
    The stopwatch runs from `disconnect` to `subscribed` -- not to the bare TCP
    connect -- because a stream is not comparing again until the server has
    accepted its subscription.
    """
    test = _bare_test(tmp_path, monkeypatch)
    sink = test._health("A")
    sink("disconnect", 100.0, "UNAVAILABLE_DISCONNECT", "")
    sink("connect", 103.0, "", "")           # TCP up, not yet subscribed
    sink("subscribed", 105.5, "", "sid=1")
    sink("disconnect", 200.0, "UNAVAILABLE_DISCONNECT", "")
    sink("subscribed", 201.0, "", "sid=1")

    diag = test._diag.as_dict()
    assert diag["disconnects"] == {"A": 2}
    assert diag["reconnects"]["A"]["count"] == 2
    assert diag["reconnects"]["A"]["max_s"] == pytest.approx(5.5)
    assert diag["reconnects"]["A"]["total_s"] == pytest.approx(6.5)


def test_a_flaky_connection_yields_insufficient_never_a_pass():
    """
    The amendment's load-bearing consequence. Few matched comparisons is
    INSUFFICIENT even at perfect agreement -- disconnects can cost the run its
    sample, they can never buy it a PASS.
    """
    assert verdict(500, 500, 0) == frozen.EQUIV_RESULT_INSUFFICIENT
    assert verdict(9_999, 9_999, 0) == frozen.EQUIV_RESULT_INSUFFICIENT
    # And the bar itself is untouched by any of this.
    assert frozen.EQUIV_MIN_COMPARISONS == 10_000
    assert frozen.EQUIV_MAX_MINUTES == 240
    assert frozen.EQUIV_PASS_AGREEMENT == 0.999
    assert frozen.EQUIV_SKEW_TOLERANCE_MS == 500
    assert frozen.EQUIV_GRID_HZ == 1.0


def test_a_stream_resumes_only_after_a_fresh_snapshot(monkeypatch):
    """
    "After a disconnect or sequence invalidation, that stream may resume
    comparisons only after obtaining a fresh snapshot and returning to VALID."
    read_book() enforces this for free: nothing but a snapshot clears an
    invalidation, so an invalidated market simply stays unmatched.
    """
    import json as _json

    from depthrec.session import ResearchSession

    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    s = ResearchSession(tickers=["T1"], use_yes_price=True)
    s.connected = True
    s.session_epoch = 1
    s.last_activity_ts = 1000.0

    def feed(msg):
        import asyncio as _a
        _a.run(s._dispatch(_json.dumps(msg)))

    feed({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
          "msg": {"market_ticker": "T1", "yes_dollars_fp": [["0.6000", "5.00"]]}})
    assert s.read_book("T1", at_ts=1000.0).is_valid

    feed({"type": "orderbook_delta", "sid": 1, "seq": 9,
          "msg": {"market_ticker": "T1", "side": "yes",
                  "price_dollars": "0.5900", "delta_fp": "1.00"}})
    assert not s.read_book("T1", at_ts=1000.0).is_valid       # unmatched from here

    feed({"type": "orderbook_delta", "sid": 1, "seq": 10,
          "msg": {"market_ticker": "T1", "side": "yes",
                  "price_dollars": "0.5900", "delta_fp": "1.00"}})
    assert not s.read_book("T1", at_ts=1000.0).is_valid       # a delta is not enough

    feed({"type": "orderbook_snapshot", "sid": 1, "seq": 11,
          "msg": {"market_ticker": "T1", "yes_dollars_fp": [["0.5000", "2.00"]]}})
    assert s.read_book("T1", at_ts=1000.0).is_valid            # snapshot resumes it


def _fake_streams(test, spec: dict[str, tuple[bool, bool]]):
    """
    Point the test's two sessions at stub read_book()s. `spec` maps ticker to
    (a_valid, b_valid); valid markets return an identical top on both streams.
    """
    from depthrec.book import TopOfBook
    from depthrec.session import ReadResult

    test.tickers = list(spec)

    def make(index: int):
        def read_book(ticker, *, at_ts=None, include_levels=True):
            ok = spec[ticker][index]
            return ReadResult(
                ticker=ticker,
                validity=frozen.VALIDITY_VALID if ok else frozen.BUCKET_UNAVAILABLE,
                reason=None if ok else frozen.UNAVAILABLE_DISCONNECT,
                detail="",
                top=TopOfBook(6800, 500, 7100, 4000) if ok else None,
                snapshot_recv_ts=None, last_update_recv_ts=None,
                last_update_exchange_ts_ms=None, snapshot_seq=None, last_seq=None,
                sid=None, session_epoch=1, updates_applied=0,
                session_activity_ts=None, session_activity_age=None, last_ping_ts=None,
            )
        return read_book

    class Stub:
        pass

    test.session_a = Stub()
    test.session_b = Stub()
    test.session_a.read_book = make(0)
    test.session_b.read_book = make(1)
    return test.session_a, test.session_b


def test_comparisons_are_written_in_one_batch_per_tick(tmp_path):
    """
    619 markets on a 1 Hz grid for up to four hours: one autocommit insert per
    market per tick would be ~619 transactions a second on the event loop, and
    the comparison loop cannot afford to fall behind the grid it samples.
    """
    from depthrec.store import RunContext, Store

    store = Store(tmp_path / "c.sqlite", RunContext("run-1", "cafe"))
    try:
        rows = [
            {"grid_ts": 100.0 + i, "ticker": f"T{i}", "agree": 1,
             "a_bid": 6800, "a_bid_qty": 500, "a_ask": 7100, "a_ask_qty": 4000,
             "b_bid": 6800, "b_bid_qty": 500, "b_ask": 7100, "b_ask_qty": 4000,
             "resolution": "agree", "skew_ms": None}
            for i in range(50)
        ]
        store.record_comparisons(rows)
        store.record_comparisons([])            # empty batch is a no-op
        got = list(store.conn.execute(
            "SELECT * FROM equivalence_comparison ORDER BY id"
        ))
        assert len(got) == 50
        assert got[0]["run_id"] == "run-1"
        assert got[0]["git_commit"] == "cafe"
        assert got[0]["a_ask"] == 7100 and got[0]["b_ask_qty"] == 4000
        assert got[0]["skew_ms"] is None
    finally:
        store.close()


def _bare_test(tmp_path, monkeypatch) -> EquivalenceTest:
    class FakeBucket:
        def acquire(self): pass

    class FakeClient:
        def __init__(self, *_a, **_k):
            self._read_bucket = FakeBucket()
            self._markets_api = None
        def close(self): pass

    monkeypatch.setattr("depthrec.equivalence.KalshiClient", FakeClient)
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    return EquivalenceTest(db_path=tmp_path / "equiv.sqlite")
