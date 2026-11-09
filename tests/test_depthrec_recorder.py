"""
Recorder bucketing and section 5 reporting.

The load-bearing claims here:

  * the executable ask is the one ON THE SIDE THE FROZEN RULE BUYS. Buying YES
    lifts the ask ladder; buying NO lifts the BID ladder at 1 - p. The frozen
    rule buys NO whenever the YES price is below 0.50, which on the pilot's own
    data is most of the time -- 31 of the 80 fixture markets. Reading the YES
    ask's depth for a NO-side signal would measure the wrong queue entirely.
  * ELIGIBLE requires trustworthy state AND an executable ask AND displayed size
    >= X = 10. Anything short of that is NON-TRADEABLE, and a data-quality
    failure is UNAVAILABLE -- the first two are never merged.
  * a firing signal the recorder holds NO state for is UNAVAILABLE_NO_SNAPSHOT,
    never a drop. The denominator is what REST says existed.
  * the three buckets sum to every otherwise-firing signal.
  * the recorder never subscribes its way into the denominator: a market that
    was never subscribed still gets a row.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.book import CanonicalBook  # noqa: E402
from depthrec.recorder import DepthRecorder, bucket_for  # noqa: E402
from depthrec.session import BookState  # noqa: E402


class FakeBucket:
    def acquire(self) -> None:
        pass


class FakeClient:
    def __init__(self, *_a, **_k) -> None:
        self._read_bucket = FakeBucket()
        self._markets_api = None

    def close(self) -> None:
        pass


@pytest.fixture
def recorder(tmp_path, monkeypatch) -> DepthRecorder:
    monkeypatch.setattr("depthrec.recorder.KalshiClient", FakeClient)
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    rec = DepthRecorder(db_path=tmp_path / "rec.sqlite", subscribe_universe=False)
    rec.session.connected = True
    rec.session.session_epoch = 1
    rec.session.last_activity_ts = 1_000_000.0
    yield rec
    rec.store.close()


def seed_book(recorder, ticker, *, yes=None, no=None):
    book = CanonicalBook(ticker=ticker)
    msg = {"market_ticker": ticker}
    if yes:
        msg["yes_dollars_fp"] = yes
    if no:
        msg["no_dollars_fp"] = no
    book.apply_snapshot(msg, seq=1, sid=5, recv_ts=1_000_000.0, use_yes_price=True)
    recorder.session._books[ticker] = BookState(book=book, session_epoch=1)
    recorder.session._tickers.append(ticker)


def observed(recorder, ticker):
    row = recorder.store.conn.execute(
        "SELECT * FROM signal_observation WHERE run_id=? AND ticker=?",
        (recorder.run_id, ticker),
    ).fetchone()
    return dict(row) if row else None


def observe(recorder, ticker, close_ts=1_787_393_700):
    recorder._observe(ticker, "KXBTC15M", close_ts, close_ts - 420, 1_000_000.0)


# -- Bucketing ----------------------------------------------------------------

def bucket_of(recorder, ticker, side):
    """The section 4 verdict for `ticker`, given the side the rule buys."""
    row = observed(recorder, ticker)
    if row is None:
        return bucket_for(validity=None, unavailable_reason=None, side=side,
                          exec_price=None, exec_size_fp=None,
                          x_contracts=recorder.x_contracts)
    price = row["no_ask_price"] if side == "NO" else row["yes_ask_price"]
    size = row["no_ask_size_fp"] if side == "NO" else row["yes_ask_size_fp"]
    return bucket_for(
        validity=row["validity"], unavailable_reason=row["unavailable_reason"],
        side=side, exec_price=price, exec_size_fp=size,
        x_contracts=recorder.x_contracts,
    )


def test_the_observation_records_both_sides_executable_ask(recorder):
    """
    Section 2's executable ask, for each side, from ONE canonical book. Buying
    YES lifts the ask ladder at 0.71 with 40 behind it; buying NO lifts the bid
    ladder, so its ask is 1 - 0.68 = 0.32 with 5 behind it.
    """
    seed_book(recorder, "T-OK", yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]])
    observe(recorder, "T-OK")
    row = observed(recorder, "T-OK")
    assert row["validity"] == frozen.VALIDITY_VALID
    assert row["best_bid"] == 0.68 and row["bid_size"] == 5.0
    assert row["best_ask"] == 0.71 and row["ask_size"] == 40.0
    assert row["yes_ask_price"] == 0.71 and row["yes_ask_size_fp"] == 4000
    assert row["no_ask_price"] == pytest.approx(0.32) and row["no_ask_size_fp"] == 500
    assert row["x_contracts"] == 10


def test_the_same_book_is_eligible_for_one_side_and_not_the_other(recorder):
    """
    The whole point of deriving the bucket from the side. 40 contracts behind
    the YES ask clears X = 10; 5 behind the NO ask does not. Reading the YES
    ask's depth for a NO-side signal would call this ELIGIBLE and be wrong.
    """
    seed_book(recorder, "T-ASYM", yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]])
    observe(recorder, "T-ASYM")
    assert bucket_of(recorder, "T-ASYM", "YES")[0] == frozen.BUCKET_ELIGIBLE
    bucket, _, detail = bucket_of(recorder, "T-ASYM", "NO")
    assert bucket == frozen.BUCKET_NON_TRADEABLE
    assert "on the NO side" in detail


def test_size_at_ask_at_least_x_is_eligible(recorder):
    seed_book(recorder, "T-OK", yes=[["0.6800", "50.00"]], no=[["0.7100", "10.00"]])
    observe(recorder, "T-OK")
    assert bucket_of(recorder, "T-OK", "YES")[0] == frozen.BUCKET_ELIGIBLE
    assert bucket_of(recorder, "T-OK", "NO")[0] == frozen.BUCKET_ELIGIBLE


def test_one_contract_short_of_x_is_non_tradeable(recorder):
    seed_book(recorder, "T-THIN", yes=[["0.6800", "50.00"]], no=[["0.7100", "9.00"]])
    observe(recorder, "T-THIN")
    bucket, reason, detail = bucket_of(recorder, "T-THIN", "YES")
    assert bucket == frozen.BUCKET_NON_TRADEABLE
    assert reason == ""                                   # not a data-quality failure
    assert observed(recorder, "T-THIN")["validity"] == frozen.VALIDITY_VALID
    assert "size_at_ask 9.0 on the YES side < X = 10" in detail


def test_a_fractional_shortfall_is_non_tradeable(recorder):
    """9.99 contracts is not 10. The API quotes hundredths; so does the test."""
    seed_book(recorder, "T-FRAC", no=[["0.7100", "9.99"]])
    observe(recorder, "T-FRAC")
    assert bucket_of(recorder, "T-FRAC", "YES")[0] == frozen.BUCKET_NON_TRADEABLE


def test_no_executable_ask_on_the_bought_side_is_non_tradeable(recorder):
    """An empty ladder on the side the rule buys is illiquidity, not a data
    failure -- the state was trustworthy and said there was nothing to lift."""
    seed_book(recorder, "T-NOASK", yes=[["0.6800", "50.00"]])
    observe(recorder, "T-NOASK")
    bucket, reason, detail = bucket_of(recorder, "T-NOASK", "YES")
    assert bucket == frozen.BUCKET_NON_TRADEABLE
    assert reason == ""
    assert "no executable ask on the YES side" in detail
    # The NO side of the same book IS liftable.
    assert bucket_of(recorder, "T-NOASK", "NO")[0] == frozen.BUCKET_ELIGIBLE


def test_no_recorder_state_is_unavailable_no_snapshot(recorder):
    observe(recorder, "T-UNKNOWN")
    row = observed(recorder, "T-UNKNOWN")
    assert row["validity"] == frozen.BUCKET_UNAVAILABLE
    assert row["unavailable_reason"] == frozen.UNAVAILABLE_NO_SNAPSHOT
    assert row["subscribed"] == 0
    assert row["best_ask"] is None and row["no_ask_price"] is None
    assert bucket_of(recorder, "T-UNKNOWN", "YES")[0] == frozen.BUCKET_UNAVAILABLE


def test_data_quality_failure_and_illiquidity_are_never_merged(recorder):
    seed_book(recorder, "T-THIN", yes=[["0.6800", "50.00"]], no=[["0.7100", "1.00"]])
    observe(recorder, "T-THIN")
    observe(recorder, "T-GONE")
    assert bucket_of(recorder, "T-THIN", "YES")[0] == frozen.BUCKET_NON_TRADEABLE
    assert bucket_of(recorder, "T-GONE", "YES")[0] == frozen.BUCKET_UNAVAILABLE
    assert recorder.store.validity_counts() == {frozen.VALIDITY_VALID: 1,
                                                frozen.BUCKET_UNAVAILABLE: 1}


def test_the_observation_carries_no_bucket_column(recorder):
    """
    Deliberate: the bucket needs the side, and the side is not known at T-7.
    Storing a side-blind bucket would be storing a number that is wrong for
    every NO-side signal.
    """
    seed_book(recorder, "T-OK", no=[["0.7100", "20.00"]])
    observe(recorder, "T-OK")
    assert "bucket" not in observed(recorder, "T-OK")


# -- Provenance ------------------------------------------------------------------

def test_every_row_carries_the_recorder_git_commit(recorder):
    seed_book(recorder, "T-OK", no=[["0.7100", "20.00"]])
    observe(recorder, "T-OK")
    assert observed(recorder, "T-OK")["git_commit"] == recorder.git_commit
    assert recorder.git_commit and recorder.git_commit != ""


def test_the_row_carries_sequence_session_and_timestamp_state(recorder):
    seed_book(recorder, "T-OK", no=[["0.7100", "20.00"]])
    observe(recorder, "T-OK")
    row = observed(recorder, "T-OK")
    assert row["snapshot_seq"] == 1
    assert row["last_seq"] == 1
    assert row["sid"] == "5"
    assert row["session_epoch"] == 1
    assert row["snapshot_recv_ts"] == 1_000_000.0
    assert row["session_activity_ts"] == 1_000_000.0
    assert row["target_ts"] == row["close_ts"] - frozen.TARGET_OFFSET_SECONDS
    assert row["book_json"]


def test_the_full_book_is_persisted_not_just_the_top(recorder):
    seed_book(recorder, "T-OK",
              yes=[["0.6800", "5.00"], ["0.6700", "9.00"]],
              no=[["0.7100", "20.00"], ["0.7200", "30.00"]])
    observe(recorder, "T-OK")
    import json
    book = json.loads(observed(recorder, "T-OK")["book_json"])
    assert book["bids"] == [[6800, 500], [6700, 900]]
    assert book["asks"] == [[7100, 2000], [7200, 3000]]


# -- Section 5 reporting -----------------------------------------------------------

def classify_row(recorder, ticker, *, firing: bool, close_ts=1_787_393_700):
    recorder.store.record_classification({
        "ticker": ticker, "series": "KXBTC15M", "close_ts": close_ts,
        "target_ts": close_ts - 420, "classified_ts": 1_000_100.0,
        "is_firing": int(firing), "not_firing_reason": "" if firing else "below threshold",
        "yes_price": 0.72 if firing else 0.55, "price_source": "mid",
        "side": "YES" if firing else "", "entry_price": 0.72 if firing else None,
        "candle_end_ts": close_ts - 420, "candles_json": "[]",
        "candles_sha256": "x", "classifier_pin": "{}",
    })


def test_report_counts_only_firing_signals_and_the_buckets_sum(recorder):
    seed_book(recorder, "T-ELIG", no=[["0.7100", "20.00"]])
    seed_book(recorder, "T-THIN", no=[["0.7100", "2.00"]])
    for ticker in ("T-ELIG", "T-THIN", "T-GONE", "T-NOTFIRING"):
        observe(recorder, ticker)
    for ticker in ("T-ELIG", "T-THIN", "T-GONE"):
        classify_row(recorder, ticker, firing=True)
    classify_row(recorder, "T-NOTFIRING", firing=False)

    report = recorder.report()
    assert report["all_otherwise_firing"] == 3
    assert sum(report["buckets"].values()) == 3
    assert report["buckets"][frozen.BUCKET_ELIGIBLE] == 1
    assert report["buckets"][frozen.BUCKET_NON_TRADEABLE] == 1
    assert report["buckets"][frozen.BUCKET_UNAVAILABLE] == 1
    assert report["unavailable_by_reason"][frozen.UNAVAILABLE_NO_SNAPSHOT] == 1
    assert report["raw_availability"] == pytest.approx(2 / 3)
    assert report["depth_clearance_D"] == pytest.approx(1 / 2)
    assert report["effective_executable_yield_Y"] == pytest.approx(1 / 3)


def test_a_firing_signal_with_no_observation_at_all_is_no_snapshot_not_a_drop(recorder):
    """
    A market REST proved existed, that the recorder never even observed. It must
    still appear in ALL. Dropping it would let recorder downtime raise the
    measured clearance rate.
    """
    classify_row(recorder, "T-NEVER-OBSERVED", firing=True)
    report = recorder.report()
    assert report["all_otherwise_firing"] == 1
    assert report["buckets"][frozen.BUCKET_UNAVAILABLE] == 1
    assert report["unavailable_by_reason"][frozen.UNAVAILABLE_NO_SNAPSHOT] == 1
    assert report["depth_clearance_D"] is None
    assert report["effective_executable_yield_Y"] == 0.0


def test_technical_availability_excludes_scheduled_maintenance(recorder):
    seed_book(recorder, "T-ELIG", no=[["0.7100", "20.00"]])
    observe(recorder, "T-ELIG")
    classify_row(recorder, "T-ELIG", firing=True)

    # A signal whose T-7 fell inside Kalshi's Thursday window.
    recorder.store.record_observation({
        "ticker": "T-MAINT", "series": "KXBTC15M", "close_ts": 1, "target_ts": 1,
        "observed_ts": 1.0,
        "validity": frozen.BUCKET_UNAVAILABLE,
        "unavailable_reason": frozen.UNAVAILABLE_SCHEDULED_MAINTENANCE,
        "detail": "", "subscribed": 1, "x_contracts": 10,
    })
    classify_row(recorder, "T-MAINT", firing=True)

    report = recorder.report()
    assert report["all_otherwise_firing"] == 2
    assert report["raw_availability"] == pytest.approx(1 / 2)
    # One of the two was maintenance, so the technical denominator is 1.
    assert report["technical_availability"] == pytest.approx(1 / 1)
    assert report["unavailable_by_reason"][frozen.UNAVAILABLE_SCHEDULED_MAINTENANCE] == 1


# -- The sample window ---------------------------------------------------------------

def test_a_target_before_the_window_start_is_out_of_sample_not_unavailable(recorder):
    """
    The reconciliation pass looks six hours back, so it routinely finds markets
    whose T-7 moment predates the recorder. Those are outside the sample, not
    UNAVAILABLE observations -- counting them would fill the denominator with
    startup artifacts and depress every availability figure.
    """
    from depthrec.census import CensusEntry

    past_close = int(recorder.window_start_ts) - 3600
    recorder.denominator_census.entries["T-PAST"] = CensusEntry(
        ticker="T-PAST", series="KXBTC15M", close_ts=past_close, event_ticker="",
        status="finalized", first_seen_ts=0.0, last_seen_ts=0.0, discovered_by="reconcile",
    )
    recorder._observe_due(recorder.window_start_ts + 1)

    assert observed(recorder, "T-PAST") is None
    assert "T-PAST" not in recorder._pending_classify
    changes = [dict(r) for r in recorder.store.conn.execute(
        "SELECT * FROM census_change WHERE change='out_of_window'"
    )]
    assert [c["ticker"] for c in changes] == ["T-PAST"]


def test_an_out_of_window_market_is_recorded_once_not_every_tick(recorder):
    from depthrec.census import CensusEntry

    recorder.denominator_census.entries["T-PAST"] = CensusEntry(
        ticker="T-PAST", series="KXBTC15M", close_ts=int(recorder.window_start_ts) - 3600,
        event_ticker="", status="finalized", first_seen_ts=0.0, last_seen_ts=0.0,
        discovered_by="reconcile",
    )
    for _ in range(5):
        recorder._observe_due(recorder.window_start_ts + 1)
    n = recorder.store.conn.execute(
        "SELECT COUNT(*) FROM census_change WHERE change='out_of_window'"
    ).fetchone()[0]
    assert n == 1


def test_a_target_inside_the_window_is_observed(recorder):
    from depthrec.census import CensusEntry

    close_ts = int(recorder.window_start_ts) + 500
    recorder.denominator_census.entries["T-SOON"] = CensusEntry(
        ticker="T-SOON", series="KXBTC15M", close_ts=close_ts, event_ticker="",
        status="active", first_seen_ts=0.0, last_seen_ts=0.0, discovered_by="live",
    )
    target = close_ts - frozen.TARGET_OFFSET_SECONDS
    assert recorder.in_window(target)
    recorder._observe_due(target - 1)
    assert observed(recorder, "T-SOON") is None      # not yet at T-7
    recorder._observe_due(target + 0.05)
    assert observed(recorder, "T-SOON") is not None


def test_an_explicit_window_end_excludes_later_targets(tmp_path, monkeypatch):
    monkeypatch.setattr("depthrec.recorder.KalshiClient", FakeClient)
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    rec = DepthRecorder(
        db_path=tmp_path / "w.sqlite", subscribe_universe=False,
        window_start_ts=1_000_000, window_end_ts=1_000_100,
    )
    try:
        assert not rec.in_window(999_999)
        assert rec.in_window(1_000_000)
        assert rec.in_window(1_000_099)
        assert not rec.in_window(1_000_100)          # half-open, as UTC days are
    finally:
        rec.store.close()


# -- Shutdown ------------------------------------------------------------------

class StubSession:
    """
    A session whose run() never returns on its own -- exactly like the real one,
    whose reconnect loop exits only once stop() has been called.
    """

    label = "A"
    use_yes_price = True
    session_epoch = 1
    connected_since = 0.0
    subscribe_params: dict = {}

    def __init__(self) -> None:
        self.stopped = False
        self.tickers: list[str] = []
        self._books: dict = {}
        self.stop_calls = 0

    async def run(self) -> None:
        while not self.stopped:
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stopped = True

    async def update_markets(self, add, remove) -> None:
        for t in add:
            self.tickers.append(t)
        for t in remove:
            self.tickers.remove(t)

    def read_book(self, ticker, *, at_ts=None, include_levels=True):
        raise AssertionError("not used in this test")


@pytest.mark.asyncio
async def test_a_duration_bounded_run_actually_returns(tmp_path, monkeypatch):
    """
    The deadlock regression.

    asyncio.wait(..., FIRST_EXCEPTION) returns early only when a task RAISES;
    when tasks merely finish normally it waits for ALL of them. The deadline
    task set _stop and returned, the three loops exited -- and session.run()
    kept reconnecting forever, because the stop() that ends it lives in the
    teardown that was never reached. A 26-minute smoke run hit its deadline,
    reconnected 81 seconds later, and had to be killed.

    This test fails by HANGING if that regresses, so it carries its own timeout.
    """
    monkeypatch.setattr("depthrec.recorder.KalshiClient", FakeClient)
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )

    rec = DepthRecorder(db_path=tmp_path / "shut.sqlite", subscribe_universe=False)
    stub = StubSession()
    rec.session = stub
    monkeypatch.setattr(rec.denominator_census, "run_live_pass", lambda **_k: [])
    monkeypatch.setattr(rec.denominator_census, "run_reconcile_pass", lambda **_k: [])

    status = await asyncio.wait_for(rec.run(duration_seconds=0.3), timeout=20)

    assert status is not None
    assert stub.stopped, "the session must be stopped during teardown"
    assert stub.stop_calls >= 1


@pytest.mark.asyncio
async def test_the_run_summary_is_written_even_when_the_deadline_fires(tmp_path, monkeypatch):
    """A bounded run must close its own books: ended_ts set, report computed."""
    monkeypatch.setattr("depthrec.recorder.KalshiClient", FakeClient)
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    rec = DepthRecorder(db_path=tmp_path / "shut2.sqlite", subscribe_universe=False)
    rec.session = StubSession()
    monkeypatch.setattr(rec.denominator_census, "run_live_pass", lambda **_k: [])
    monkeypatch.setattr(rec.denominator_census, "run_reconcile_pass", lambda **_k: [])

    await asyncio.wait_for(rec.run(duration_seconds=0.3), timeout=20)

    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "shut2.sqlite"))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM recorder_run").fetchone()
        assert row["ended_ts"] is not None
        assert row["started_ts"] <= row["ended_ts"]
    finally:
        conn.close()


def test_the_report_contains_no_settlement_or_ev_field(recorder):
    """
    Section 9: settlements, EV, winners, losers, markouts and series-specific
    profitability may not be inspected during the pilot. The reporter has no way
    to produce them.
    """
    banned = ("ev", "pnl", "profit", "settle", "win", "loss", "markout", "return")
    flat = repr(recorder.report()).lower()
    for word in banned:
        assert f'"{word}' not in flat and f"'{word}" not in flat
