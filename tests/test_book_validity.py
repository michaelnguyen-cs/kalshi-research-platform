"""
Fail-closed book validity, WS sequence continuity, and the subscribe params.

Integrity patch items 2-4 (KILL_CRITERIA.md section 7.6). The invariant under
test: a book is usable ONLY while it was built from a snapshot on the current
WS session and has been kept current by an unbroken run of deltas.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kbot.quantity import UNITS_PER_CONTRACT
from kbot.ws_client import SequenceGap, WSClient


# ── Book validity (runner side) ───────────────────────────────────────────────

class _FakeRunnerBooks:
    """The runner's validity surface, isolated from the rest of Runner."""

    def __init__(self):
        from kbot.runner import Runner
        self._books = {}
        self._book_invalidations = 0
        self._last_invalidation = None
        self._invalidate_all_books = Runner._invalidate_all_books.__get__(self)
        self._invalidate_book = Runner._invalidate_book.__get__(self)


def test_invalidate_all_books_empties_the_store():
    r = _FakeRunnerBooks()
    r._books["A"] = object()
    r._books["B"] = object()

    r._invalidate_all_books("disconnected")

    assert r._books == {}
    assert r._last_invalidation == "disconnected"
    assert r._book_invalidations == 1


def test_invalidate_mutates_in_place_so_shared_references_see_it():
    """
    book_provider and PostResolutionScanner hold a reference to this exact
    dict. Rebinding it would leave them reading a stale book forever -- which
    is precisely the failure this patch exists to prevent.
    """
    r = _FakeRunnerBooks()
    shared = r._books
    r._books["A"] = object()

    r._invalidate_all_books("reconnect")

    assert shared is r._books
    assert shared == {}


def test_invalidate_single_book_leaves_the_others_valid():
    r = _FakeRunnerBooks()
    r._books["A"] = object()
    r._books["B"] = object()

    r._invalidate_book("A", "delta_inconsistent")

    assert "A" not in r._books
    assert "B" in r._books


def test_invalidating_an_absent_book_is_a_noop():
    r = _FakeRunnerBooks()
    r._invalidate_book("GONE", "whatever")
    assert r._book_invalidations == 0


# ── Sequence continuity, keyed by SID ─────────────────────────────────────────

def _client(**kw) -> WSClient:
    return WSClient.__new__(WSClient)   # no credentials / no socket needed


def _seq_checker(**kw):
    c = _client()
    c._seq_by_sid = {}
    return c


def test_consecutive_sequence_is_accepted():
    c = _seq_checker()
    for n in (1, 2, 3, 4):
        c._check_sequence({"sid": 7, "seq": n})
    assert c._seq_by_sid[7] == 4


def test_sequence_gap_raises():
    c = _seq_checker()
    c._check_sequence({"sid": 7, "seq": 1})
    with pytest.raises(SequenceGap):
        c._check_sequence({"sid": 7, "seq": 3})


def test_sequence_is_tracked_per_sid_not_per_ticker():
    """
    Two subscriptions interleave on one socket, each with its own counter.
    A per-ticker or global counter would see this as a storm of gaps.
    """
    c = _seq_checker()
    c._check_sequence({"sid": 1, "seq": 1, "msg": {"market_ticker": "A"}})
    c._check_sequence({"sid": 2, "seq": 1, "msg": {"market_ticker": "B"}})
    c._check_sequence({"sid": 1, "seq": 2, "msg": {"market_ticker": "B"}})
    c._check_sequence({"sid": 2, "seq": 2, "msg": {"market_ticker": "A"}})
    assert c._seq_by_sid == {1: 2, 2: 2}


def test_one_sids_gap_does_not_depend_on_another_sids_progress():
    c = _seq_checker()
    c._check_sequence({"sid": 1, "seq": 1})
    c._check_sequence({"sid": 2, "seq": 1})
    c._check_sequence({"sid": 2, "seq": 2})
    with pytest.raises(SequenceGap):
        c._check_sequence({"sid": 1, "seq": 5})


def test_messages_without_seq_are_ignored():
    """Command acks and subscription confirmations carry no seq."""
    c = _seq_checker()
    c._check_sequence({"type": "subscribed", "id": 1})
    c._check_sequence({"sid": 3})
    assert c._seq_by_sid == {}


def test_replayed_or_older_seq_is_not_a_gap():
    """Nothing is MISSING on a repeat, so it must not force a reconnect."""
    c = _seq_checker()
    c._check_sequence({"sid": 1, "seq": 5})
    c._check_sequence({"sid": 1, "seq": 5})
    c._check_sequence({"sid": 1, "seq": 4})
    assert c._seq_by_sid[1] == 5


def test_first_seq_for_a_sid_sets_the_baseline_whatever_its_value():
    c = _seq_checker()
    c._check_sequence({"sid": 9, "seq": 4321})
    assert c._seq_by_sid[9] == 4321


# ── A gap invalidates and propagates ──────────────────────────────────────────

def test_gap_invalidates_books_and_propagates_to_kill_the_socket():
    reasons = []
    c = _client()
    c._seq_by_sid = {}
    c._on_invalidate = reasons.append
    c._on_message = lambda msg: None

    asyncio.run(c._dispatch(json.dumps({"type": "orderbook_delta", "sid": 1, "seq": 1})))
    with pytest.raises(SequenceGap):
        asyncio.run(c._dispatch(json.dumps({"type": "orderbook_delta", "sid": 1, "seq": 9})))

    assert reasons == ["sequence_gap"]


def test_handler_never_sees_a_message_from_after_a_gap():
    """
    The check runs BEFORE the handler: a delta delivered out of a gap has
    already corrupted the book by the time the handler returns.
    """
    seen = []
    c = _client()
    c._seq_by_sid = {}
    c._on_invalidate = lambda reason: None
    c._on_message = lambda msg: seen.append(msg["seq"])

    asyncio.run(c._dispatch(json.dumps({"type": "orderbook_delta", "sid": 1, "seq": 1})))
    with pytest.raises(SequenceGap):
        asyncio.run(c._dispatch(json.dumps({"type": "orderbook_delta", "sid": 1, "seq": 4})))

    assert seen == [1]


def test_server_error_invalidates_and_does_not_reach_the_handler():
    """Covers a rejected subscription: nothing is updating that book any more."""
    reasons, seen = [], []
    c = _client()
    c._seq_by_sid = {}
    c._on_invalidate = reasons.append
    c._on_message = seen.append

    asyncio.run(c._dispatch(json.dumps({"type": "error", "msg": {"code": 6}})))

    assert reasons == ["server_error"]
    assert seen == []


def test_a_failing_invalidate_callback_cannot_break_the_reconnect_loop():
    def boom(reason):
        raise RuntimeError("callback exploded")

    c = _client()
    c._on_invalidate = boom
    c._invalidate("disconnected")   # must not raise


# ── Subscribe params ──────────────────────────────────────────────────────────

def test_subscribe_sends_use_yes_price_false_explicitly():
    """
    _parse_orderbook decodes yes/no BIDS. If the server default ever flipped,
    every book would silently invert -- so the value is stated, not assumed.
    """
    sent = []

    class _FakeWS:
        async def send(self, payload):
            sent.append(json.loads(payload))

    c = _client()
    c._cmd_id = 0
    c._channels = ["orderbook_delta", "ticker"]

    asyncio.run(c._send_subscribe(_FakeWS(), ["KXBTC-1"]))

    assert sent[0]["params"]["use_yes_price"] is False
    assert sent[0]["params"]["market_tickers"] == ["KXBTC-1"]


def test_subscribe_with_no_tickers_sends_nothing():
    class _FakeWS:
        async def send(self, payload):
            raise AssertionError("must not send an empty subscribe")

    c = _client()
    c._cmd_id = 0
    c._channels = ["orderbook_delta"]
    asyncio.run(c._send_subscribe(_FakeWS(), []))
