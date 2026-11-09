"""
The ticker/trade bank session: what it subscribes to, what it counts, and what
it refuses to infer from silence.

Constructed without credentials on purpose -- the session does no I/O until it
connects -- so these exercise the real dispatch and subscription logic rather
than a mock of it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from threadc import frozen  # noqa: E402
from threadc.session import SubscriptionRejected, TickerBankSession  # noqa: E402
from threadc_fakes import frame, trade_frame  # noqa: E402


class _Recorder:
    """Captures everything a session hands to its sinks."""

    def __init__(self) -> None:
        self.raw: list[tuple[str, str]] = []
        self.health: list[tuple[str, str, str]] = []
        self.epochs: list[tuple[str, int]] = []

    def raw_sink(self, direction, recv_ts, text):
        self.raw.append((direction, text))

    def health_sink(self, kind, ts, reason, detail):
        self.health.append((kind, reason, detail))

    def epoch_sink(self, label, epoch, ts):
        self.epochs.append((label, epoch))


def session(tickers=("KXMLBGAME-M01",), sink=None):
    sink = sink or _Recorder()
    s = TickerBankSession(
        tickers=list(tickers), label="S00",
        raw_sink=sink.raw_sink, health_sink=sink.health_sink,
        on_epoch_established=sink.epoch_sink,
    )
    return s, sink


def subscribed(sid, channel):
    return json.dumps({"type": "subscribed", "msg": {"sid": sid, "channel": channel}})


def establish(s):
    """Acknowledge both channels, exactly as the server does."""
    for i, channel in enumerate(frozen.CHANNELS, start=1):
        s._dispatch(subscribed(i, channel))


# -- What it subscribes to ----------------------------------------------------

class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text):
        self.sent.append(json.loads(text))


async def test_the_subscribe_names_both_channels_and_sends_use_yes_price():
    s, sink = session(["A", "B"])
    ws = _FakeWS()
    await s._send_subscribe(ws)

    assert len(ws.sent) == 1
    params = ws.sent[0]["params"]
    assert ws.sent[0]["cmd"] == "subscribe"
    assert params["channels"] == list(frozen.CHANNELS) == ["ticker", "trade"]
    assert params["market_tickers"] == ["A", "B"]
    # Explicit, never the API default: Kalshi documents that the default will
    # flip and that the flag will later be removed.
    assert params["use_yes_price"] is True
    # The outbound frame is banked too.
    assert sink.raw[0][0] == "out"


async def test_an_empty_shard_sends_no_subscribe_at_all():
    s, _ = session([])
    ws = _FakeWS()
    await s._send_subscribe(ws)
    assert ws.sent == []


async def test_rotation_updates_every_subscription_on_the_connection():
    """
    `ticker` and `trade` are separate subscriptions with separate sids. A market
    added to only one of them would be half-banked -- quotes without prints, or
    the reverse.
    """
    s, _ = session(["A"])
    ws = _FakeWS()
    s._ws = ws
    s._sids = {1, 2}
    await s.update_markets(["B"], [])

    assert len(ws.sent) == 2
    assert {m["params"]["sid"] for m in ws.sent} == {1, 2}
    assert all(m["params"]["action"] == "add_markets" for m in ws.sent)
    assert all(m["params"]["market_tickers"] == ["B"] for m in ws.sent)
    assert s.tickers == ["A", "B"]


async def test_an_update_with_no_connection_still_changes_the_authoritative_list():
    s, _ = session(["A"])
    await s.update_markets(["B"], ["A"])
    assert s.tickers == ["B"]        # survives to the next reconnect


# -- Establishment ------------------------------------------------------------

def test_the_epoch_is_established_only_once_every_channel_is_acknowledged():
    """
    Neither channel snapshots on subscribe, so the recorder must bank an
    explicit initial state -- but only once the subscription it describes is
    actually complete.
    """
    s, sink = session()
    s.session_epoch = 1
    s._dispatch(subscribed(1, "ticker"))
    assert sink.epochs == []
    s._dispatch(subscribed(2, "trade"))
    assert sink.epochs == [("S00", 1)]


def test_the_channels_are_recorded_as_sending_no_snapshot():
    """
    Established live, and written down where the recorder reads it, so nobody
    later assumes silence means unchanged.
    """
    assert frozen.CHANNELS_SEND_NO_SNAPSHOT_ON_SUBSCRIBE is True


# -- Counting -----------------------------------------------------------------

def test_messages_are_counted_by_type():
    s, _ = session()
    establish(s)
    for i in range(5):
        s._dispatch(frame("KXMLBGAME-M01", i))
    for i in range(3):
        s._dispatch(trade_frame("KXMLBGAME-M01", i))

    counts = s.counts_snapshot()
    assert counts["ticker"] == 5
    assert counts["trade"] == 3
    assert counts["subscribed"] == 2


def test_counting_carries_no_market_identity():
    """Types, never markets. A per-market count is a research statistic."""
    s, _ = session()
    s._dispatch(frame("KXMLBGAME-M01", 1))
    s._dispatch(frame("KXMLBGAME-M02", 2))
    assert s.counts_snapshot() == {"ticker": 2}


def test_an_undecodable_frame_is_counted_and_recorded_not_raised():
    s, sink = session()
    s._dispatch("{not json")
    assert s.counts_snapshot() == {"_undecodable": 1}
    assert [h[0] for h in sink.health] == ["parse_error"]


# -- Raw preservation ---------------------------------------------------------

def test_every_frame_reaches_the_raw_sink_before_parsing():
    s, sink = session()
    text = frame("KXMLBGAME-M01", 1)
    s._dispatch(text)
    assert sink.raw == [("in", text)]


def test_an_undecodable_frame_is_still_banked_verbatim():
    """The archive keeps what Kalshi sent, including what this decoder cannot read."""
    s, sink = session()
    s._dispatch("{not json")
    assert sink.raw == [("in", "{not json")]


# -- Gaps and errors ----------------------------------------------------------

def test_a_sequence_gap_is_recorded_with_the_number_of_missing_messages():
    s, sink = session()
    s._dispatch(json.dumps({"type": "ticker", "sid": 1, "seq": 1, "msg": {}}))
    s._dispatch(json.dumps({"type": "ticker", "sid": 1, "seq": 5, "msg": {}}))

    gaps = [h for h in sink.health if h[0] == "sequence_gap"]
    assert len(gaps) == 1
    assert "3 message(s) missing" in gaps[0][2]


def test_a_replayed_sequence_is_not_a_gap():
    s, sink = session()
    s._dispatch(json.dumps({"type": "ticker", "sid": 1, "seq": 5, "msg": {}}))
    s._dispatch(json.dumps({"type": "ticker", "sid": 1, "seq": 5, "msg": {}}))
    assert not [h for h in sink.health if h[0] == "sequence_gap"]
    assert [h[0] for h in sink.health] == ["seq_replay"]


def test_sequences_are_tracked_per_subscription_not_per_market():
    """
    One subscription multiplexes every market onto one counter, so a per-market
    view of `seq` is full of legitimate holes.
    """
    s, sink = session()
    for sid in (1, 2):
        for seq in (1, 2, 3):
            s._dispatch(json.dumps({"type": "ticker", "sid": sid, "seq": seq, "msg": {}}))
    assert not [h for h in sink.health if h[0] == "sequence_gap"]


def test_a_server_error_raises_rather_than_being_retried_smaller():
    """
    Bisecting against the server to find a workable subscription size is exactly
    the experimental search this recorder is forbidden from doing.
    """
    s, sink = session()
    payload = json.dumps({
        "type": "error", "msg": {"code": 26, "msg": "Subscription market limit exceeded"},
    })
    with pytest.raises(SubscriptionRejected) as exc:
        s._dispatch(payload)
    assert exc.value.code == 26
    assert exc.value.is_market_limit
    assert [h[0] for h in sink.health] == ["server_error"]


# -- A capacity measurement's narrower selection ------------------------------


async def test_a_selected_subset_is_what_actually_goes_on_the_wire():
    """
    The Branch-2 capacity measurement is ticker-only, so the subscribe must name
    one channel. Anything else would measure Branch 1 again under a Branch-2
    label.
    """
    s = TickerBankSession(tickers=["A"], label="S00", channels=("ticker",))
    ws = _FakeWS()
    await s._send_subscribe(ws)

    params = ws.sent[0]["params"]
    assert params["channels"] == ["ticker"]
    assert params["use_yes_price"] is True
    # The frozen tuple is not what was edited to get here.
    assert frozen.CHANNELS == ("ticker", "trade")


def test_a_session_defaults_to_the_frozen_pair():
    assert TickerBankSession(tickers=["A"]).channels == frozen.CHANNELS


def test_establishment_counts_acknowledgements_against_the_selection():
    """
    The recorder banks its explicit initial state on this callback, and the
    capacity window opens only once every shard has fired it. A ticker-only
    session that waited for a second sid would wait forever: no initial state,
    and a measurement that never starts.
    """
    sink = _Recorder()
    s = TickerBankSession(
        tickers=["A"], label="S00", channels=("ticker",),
        on_epoch_established=sink.epoch_sink,
    )
    s.session_epoch = 1
    s._dispatch(subscribed(1, "ticker"))
    assert sink.epochs == [("S00", 1)]
