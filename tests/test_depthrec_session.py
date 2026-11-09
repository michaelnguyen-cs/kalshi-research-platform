"""
Session state machine: snapshot -> sequenced deltas, and read_book()'s section 4
verdict.

Driven by feeding frames straight into ResearchSession._dispatch, so no network
and no credentials are needed. The connection itself is stubbed; everything
under test -- sequence continuity, invalidation, the maintenance window, session
staleness, the reason precedence -- is pure state.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.session import ResearchSession, SubscriptionRejected  # noqa: E402


@pytest.fixture
def session(monkeypatch):
    """A ResearchSession with credential loading stubbed out."""
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    s = ResearchSession(tickers=["T1", "T2"], use_yes_price=True, label="A")
    s.connected = True
    s.session_epoch = 1
    s.last_activity_ts = 1_000_000.0
    return s


def feed(session, msg):
    asyncio.run(session._dispatch(json.dumps(msg)))


def snapshot(seq, ticker="T1", yes=None, no=None, sid=5):
    body = {"market_ticker": ticker}
    if yes:
        body["yes_dollars_fp"] = yes
    if no:
        body["no_dollars_fp"] = no
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": body}


def delta(seq, ticker="T1", side="no", price="0.7000", qty="5.00", sid=5):
    return {
        "type": "orderbook_delta", "sid": sid, "seq": seq,
        "msg": {"market_ticker": ticker, "side": side,
                "price_dollars": price, "delta_fp": qty},
    }


def seed(session, ticker="T1", sid=5, seq=1):
    feed(session, snapshot(seq, ticker, yes=[["0.6800", "50.00"]],
                           no=[["0.7100", "40.00"]], sid=sid))


# -- The happy path -------------------------------------------------------------

def test_snapshot_then_deltas_produce_a_valid_book(session):
    seed(session)
    feed(session, delta(2, price="0.7000", qty="12.00"))
    read = session.read_book("T1", at_ts=session.last_activity_ts)
    assert read.is_valid
    assert read.top.ask_price == 7000
    assert read.top.ask_qty == 1200
    assert read.snapshot_seq == 1 and read.last_seq == 2
    assert read.reason is None


def test_an_unsubscribed_market_is_no_snapshot_never_a_drop(session):
    read = session.read_book("NEVER-SEEN", at_ts=session.last_activity_ts)
    assert read.validity == frozen.BUCKET_UNAVAILABLE
    assert read.reason == frozen.UNAVAILABLE_NO_SNAPSHOT


def test_quiet_market_stays_valid_while_the_session_is_alive(session):
    """
    Section 3: an unchanged book remains valid. Quiet markets are
    disproportionately the illiquid ones, and classifying them as unrecorded
    would remove them from the denominator and inflate clearance.
    """
    seed(session)
    later = session.last_activity_ts + 3600
    session.last_activity_ts = later          # session heartbeat, no market data
    read = session.read_book("T1", at_ts=later + 5)
    assert read.is_valid
    assert read.top.ask_price == 7100


# -- Sequence and disconnect invalidation ----------------------------------------

def test_a_sequence_gap_invalidates_until_a_fresh_snapshot(session):
    seed(session)
    feed(session, delta(2, qty="1.00"))
    feed(session, delta(7, qty="1.00"))       # 3..6 missing
    read = session.read_book("T1", at_ts=session.last_activity_ts)
    assert read.reason == frozen.UNAVAILABLE_SEQUENCE_GAP

    # A later delta does NOT repair it -- only a snapshot does.
    feed(session, delta(8, qty="1.00"))
    assert session.read_book("T1").reason == frozen.UNAVAILABLE_SEQUENCE_GAP

    feed(session, snapshot(9, yes=[["0.5000", "1.00"]]))
    read = session.read_book("T1", at_ts=session.last_activity_ts)
    assert read.is_valid
    assert read.top.bid_price == 5000


def test_a_gap_invalidates_every_market_on_that_subscription(session):
    seed(session, "T1", seq=1)
    feed(session, snapshot(2, "T2", yes=[["0.4000", "9.00"]]))
    feed(session, delta(9, "T1"))
    assert session.read_book("T1").reason == frozen.UNAVAILABLE_SEQUENCE_GAP
    assert session.read_book("T2").reason == frozen.UNAVAILABLE_SEQUENCE_GAP


def test_a_repeated_seq_is_not_a_gap(session):
    seed(session)
    feed(session, delta(2, qty="1.00"))
    feed(session, delta(2, qty="1.00"))
    assert session.read_book("T1", at_ts=session.last_activity_ts).is_valid


def test_a_pre_gap_book_is_never_carried_across_a_reconnect(session):
    seed(session)
    session._invalidate_all(frozen.UNAVAILABLE_DISCONNECT, "reconnecting")
    session.session_epoch += 1
    session._seq_by_sid.clear()
    read = session.read_book("T1", at_ts=session.last_activity_ts)
    assert read.reason == frozen.UNAVAILABLE_DISCONNECT
    assert read.top is None


def test_a_delta_before_any_snapshot_is_no_snapshot(session):
    feed(session, delta(1, "T2"))
    assert session.read_book("T2").reason == frozen.UNAVAILABLE_NO_SNAPSHOT


def test_disconnected_session_reports_disconnect_for_everything(session):
    seed(session)
    session.connected = False
    assert session.read_book("T1").reason == frozen.UNAVAILABLE_DISCONNECT
    assert session.read_book("NEVER-SEEN").reason == frozen.UNAVAILABLE_DISCONNECT


# -- Session health ---------------------------------------------------------------

def test_a_silent_connection_goes_stale_after_the_frozen_window(session):
    seed(session)
    at = session.last_activity_ts + frozen.SESSION_ACTIVITY_MAX_AGE_SECONDS + 0.001
    assert session.read_book("T1", at_ts=at).reason == frozen.UNAVAILABLE_SESSION_STALE
    at_ok = session.last_activity_ts + frozen.SESSION_ACTIVITY_MAX_AGE_SECONDS - 0.001
    assert session.read_book("T1", at_ts=at_ok).is_valid


def test_a_received_ping_counts_as_session_activity(session):
    """Kalshi pings every 10 s, so the 30 s window tests the CONNECTION."""
    seed(session)
    quiet = session.last_activity_ts + 25
    session.last_activity_ts = quiet          # as the frame hook would set it
    session.last_ping_ts = quiet
    read = session.read_book("T1", at_ts=quiet + 5)
    assert read.is_valid
    assert read.last_ping_ts == quiet


# -- Maintenance -------------------------------------------------------------------

def _thursday_et(hour: int) -> float:
    """A Thursday at `hour` ET, expressed as a unix second."""
    local = datetime(2026, 8, 20, hour, 0, tzinfo=frozen.MAINTENANCE_TZ)
    return local.astimezone(timezone.utc).timestamp()


def test_maintenance_window_outranks_every_other_reason(session):
    seed(session)
    session.connected = False                 # would otherwise be DISCONNECT
    read = session.read_book("T1", at_ts=_thursday_et(4))
    assert read.reason == frozen.UNAVAILABLE_SCHEDULED_MAINTENANCE


def test_maintenance_window_is_half_open_and_tz_aware(session):
    assert frozen.in_scheduled_maintenance(_thursday_et(3))
    assert frozen.in_scheduled_maintenance(_thursday_et(4))
    assert not frozen.in_scheduled_maintenance(_thursday_et(5))
    assert not frozen.in_scheduled_maintenance(_thursday_et(2))
    # Wednesday at the same hour is not the window.
    wed = datetime(2026, 8, 19, 4, 0, tzinfo=frozen.MAINTENANCE_TZ)
    assert not frozen.in_scheduled_maintenance(wed.astimezone(timezone.utc).timestamp())


# -- Errors and raw retention -------------------------------------------------------

def test_a_server_error_is_raised_with_its_exact_payload(session):
    payload = {"type": "error", "id": 1,
               "msg": {"code": 26, "msg": "Subscription market limit exceeded"}}
    with pytest.raises(SubscriptionRejected) as excinfo:
        feed(session, payload)
    assert excinfo.value.code == 26
    assert excinfo.value.is_market_limit
    assert excinfo.value.payload == payload


def test_every_frame_reaches_the_raw_sink_before_parsing(monkeypatch):
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    seen: list[tuple[str, str]] = []
    s = ResearchSession(
        tickers=["T1"], use_yes_price=True,
        raw_sink=lambda direction, ts, raw: seen.append((direction, raw)),
    )
    s.connected = True
    s.session_epoch = 1
    s.last_activity_ts = 1.0

    text = json.dumps(snapshot(1, yes=[["0.6000", "1.00"]]))
    asyncio.run(s._dispatch(text))
    asyncio.run(s._dispatch("{not json"))     # unparseable frames are kept too
    assert seen == [("in", text), ("in", "{not json")]


def test_a_book_decode_failure_is_unavailable_other_not_a_silent_skip(session):
    seed(session)
    feed(session, delta(2, price="0.7100", qty="-999.00"))   # drives level negative
    assert session.read_book("T1").reason == frozen.UNAVAILABLE_OTHER


def test_subscribe_payload_states_use_yes_price_explicitly(monkeypatch):
    monkeypatch.setattr("depthrec.session.load_private_key", lambda _p: object())
    monkeypatch.setattr(
        "kbot.config.Settings.require_credentials", lambda self: None, raising=False
    )
    sent: list[str] = []

    class FakeWS:
        async def send(self, text):
            sent.append(text)

    for use_yes in (True, False):
        s = ResearchSession(tickers=["T1", "T2"], use_yes_price=use_yes)
        asyncio.run(s._send_subscribe(FakeWS()))
        params = json.loads(sent[-1])["params"]
        assert "use_yes_price" in params, "must never rely on the server default"
        assert params["use_yes_price"] is use_yes
        assert params["channels"] == [frozen.ORDERBOOK_CHANNEL]
        assert params["market_tickers"] == ["T1", "T2"]


def test_production_convention_is_frozen_true():
    assert frozen.USE_YES_PRICE is True
