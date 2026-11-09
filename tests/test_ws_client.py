"""
Tests for kbot.ws_client — mid-session subscription management
(subscribe_additional / prune_tickers) added 2026-07 for periodic ticker
re-discovery. Auth (require_credentials / load_private_key) is mocked so the
client can be built without real Kalshi credentials.
"""

from __future__ import annotations

import json

import pytest

from kbot.config import Settings
from kbot.ws_client import WSClient


def _make_client(mocker, tickers, channels=None):
    mocker.patch.object(Settings, "require_credentials", lambda self: None)
    mocker.patch("kbot.ws_client.load_private_key", return_value=object())
    cfg = Settings(kalshi_key_id="test", dry_run=True)
    return WSClient(cfg, tickers=tickers, channels=channels or ["orderbook_delta", "ticker"])


class _FakeWS:
    """Records every payload sent, so a test can assert the WS message format."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


# ── active_tickers / construction ────────────────────────────────────────────

def test_active_tickers_reflects_initial_set(mocker):
    c = _make_client(mocker, ["A", "B"])
    assert c.active_tickers == ["A", "B"]


def test_constructor_dedups_initial_tickers(mocker):
    c = _make_client(mocker, ["A", "B", "A"])
    assert c.active_tickers == ["A", "B"]


# ── subscribe_additional ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_additional_sends_only_new_tickers_on_live_connection(mocker):
    c = _make_client(mocker, ["A", "B"])
    fake = _FakeWS()
    c._ws = fake  # simulate a live connection

    added = await c.subscribe_additional(["B", "C", "D"])  # B already subscribed

    assert added == ["C", "D"]
    assert c.active_tickers == ["A", "B", "C", "D"]
    # Exactly one subscribe command, carrying ONLY the new tickers.
    assert len(fake.sent) == 1
    payload = fake.sent[0]
    assert payload["cmd"] == "subscribe"
    assert payload["params"]["market_tickers"] == ["C", "D"]
    assert payload["params"]["channels"] == ["orderbook_delta", "ticker"]


@pytest.mark.asyncio
async def test_subscribe_additional_does_not_reconnect(mocker):
    """The same live ws object is used -- no new connection is opened."""
    c = _make_client(mocker, ["A"])
    fake = _FakeWS()
    c._ws = fake
    connect_spy = mocker.patch("kbot.ws_client.ws_asyncio.connect")

    await c.subscribe_additional(["B"])

    connect_spy.assert_not_called()  # mid-session subscribe, not a reconnect


@pytest.mark.asyncio
async def test_subscribe_additional_deferred_when_not_connected(mocker):
    c = _make_client(mocker, ["A"])
    assert c._ws is None  # not connected

    added = await c.subscribe_additional(["B", "C"])

    # Added to the authoritative set (rides the next reconnect's subscribe)...
    assert added == ["B", "C"]
    assert c.active_tickers == ["A", "B", "C"]
    # ...but nothing was sent, since there's no live socket.
    # (No _FakeWS attached; a send would have raised AttributeError.)


@pytest.mark.asyncio
async def test_subscribe_additional_all_duplicates_is_noop(mocker):
    c = _make_client(mocker, ["A", "B"])
    fake = _FakeWS()
    c._ws = fake

    added = await c.subscribe_additional(["A", "B"])

    assert added == []
    assert fake.sent == []  # nothing sent when there's nothing new


@pytest.mark.asyncio
async def test_subscribe_additional_new_tickers_resubscribed_on_reconnect(mocker):
    """A ticker added mid-session must be in the set the next connect subscribes."""
    c = _make_client(mocker, ["A"])
    fake = _FakeWS()
    c._ws = fake
    await c.subscribe_additional(["B"])

    # Simulate the reconnect subscribe using the same private helper run() uses.
    reconnect_ws = _FakeWS()
    await c._send_subscribe(reconnect_ws, c._tickers)

    assert reconnect_ws.sent[0]["params"]["market_tickers"] == ["A", "B"]


# ── prune_tickers ────────────────────────────────────────────────────────────

def test_prune_tickers_removes_from_active_set(mocker):
    c = _make_client(mocker, ["A", "B", "C"])
    removed = c.prune_tickers(["B", "X"])  # X isn't subscribed
    assert removed == ["B"]
    assert c.active_tickers == ["A", "C"]


def test_prune_tickers_not_resubscribed_on_reconnect(mocker):
    c = _make_client(mocker, ["A", "B", "C"])
    c.prune_tickers(["B"])
    assert "B" not in c._tickers  # gone from the authoritative set


def test_prune_tickers_empty_is_noop(mocker):
    c = _make_client(mocker, ["A", "B"])
    assert c.prune_tickers(["Z"]) == []
    assert c.active_tickers == ["A", "B"]


@pytest.mark.asyncio
async def test_send_subscribe_empty_tickers_is_noop(mocker):
    c = _make_client(mocker, ["A"])
    fake = _FakeWS()
    await c._send_subscribe(fake, [])
    assert fake.sent == []
