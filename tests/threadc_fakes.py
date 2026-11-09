"""
Fakes for the Thread C recorder tests.

Deliberately shaped like the real thing rather than mocked at the call site: the
fake API returns the same JSON shapes Kalshi returns (including the "finalized"
market status the SDK's typed models choke on) and honours the same split
between the `status` QUERY FILTER and the `status` a market body CARRIES
(QUERY_STATUS_TO_RETURNED below), and the fake WebSocket session exposes the
same surface threadc.session.TickerBankSession does -- an infinite run(), a
session_epoch that only increments on a real reconnect, an on_epoch_established
callback fired when a subscription actually comes up, per-type message counters,
and sinks assigned after construction. That is what makes "rotation did not
restart the WebSocket" and "the bank has an explicit state to start from"
checkable claims instead of assertions about a mock.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threadc.frozen import CHANNELS  # noqa: E402


def event(series: str, competition, index: int = 1) -> dict:
    """One /events record, with product_metadata shaped as Kalshi ships it."""
    return {
        "event_ticker": f"{series}-EV{index:02d}",
        "series_ticker": series,
        "product_metadata": ({} if competition is _MISSING else {
            "competition": competition, "competition_scope": None,
        }),
    }


class _Missing:
    pass


_MISSING = _Missing()


QUERY_STATUS_TO_RETURNED = {
    "open": ("active",),
    "unopened": ("initialized",),
    "closed": ("closed", "determined"),
    "settled": ("settled", "finalized"),
}
"""Kalshi's two market-status vocabularies, as the live API actually behaves.

The keys are the values GET /markets accepts in its `status` QUERY FILTER; the
values are the statuses a market BODY comes back carrying. They are disjoint:
verified live 2026-09-05, `?status=open&limit=200` returned 200 markets every
one of which was `"status": "active"`, and `?status=active` is rejected 400
`{"details": "invalid status filter"}`.

Modelled here rather than filtered by string equality because a fake that
accepts `status=open` and hands back markets whose status is literally "open"
cannot fail the way production failed."""


def market(series: str, index: int = 1, *, status: str = "active",
           close_ts: int | None = None) -> dict:
    """
    One /markets record.

    Carries the displayed touch fields Kalshi actually ships in a market body --
    `yes_bid_size_fp` and `yes_ask_size_fp` among them, verified live 2026-09-06.
    They are the reason the recorder can bank an explicit initial state at every
    subscription establishment without a single extra request: they are the same
    displayed fields the `ticker` channel carries.
    """
    close_ts = close_ts if close_ts is not None else int(time.time()) + 3600
    return {
        "ticker": f"{series}-M{index:02d}",
        "event_ticker": f"{series}-EV01",
        "series_ticker": series,
        "status": status,
        "close_time": close_ts,
        "updated_time": "2026-09-06T12:00:00Z",
        "yes_bid_dollars": "0.5000",
        "yes_ask_dollars": "0.5200",
        "yes_bid_size_fp": "10.00",
        "yes_ask_size_fp": "8.00",
        "no_bid_dollars": "0.4800",
        "no_ask_dollars": "0.5000",
        "last_price_dollars": "0.5100",
        "previous_price_dollars": "0.5000",
        "volume_fp": "100.00",
        "volume_24h_fp": "40.00",
        "open_interest_fp": "50.00",
        "liquidity_dollars": "1234.00",
    }


class FakeAPI:
    """
    A GET-only Kalshi stand-in with the same method surface as
    threadc.readonly.ReadOnlyAPI.

    `unreadable` makes named series raise on their /events probe, which is how a
    Gate-3 INCOMPLETE census is produced. Mutating `catalog`, `events` or
    `unreadable` between calls is how "a later census recovers" is tested.
    """

    base_url = "https://fake.kalshi.test/trade-api/v2"
    api_env = "test"

    def __init__(self, catalog=None, events=None, markets=None, unreadable=None):
        self.catalog = list(catalog or [])
        self.events = dict(events or {})
        self.markets = dict(markets or {})
        self.unreadable = set(unreadable or ())
        self.request_count = 0
        self.error_count = 0
        self.closed = False
        self.calls: list[tuple[str, dict]] = []

    # -- ReadOnlyAPI surface --------------------------------------------------

    def get_raw(self, path: str, params: dict | None = None):
        self.request_count += 1
        self.calls.append((path, dict(params or {})))
        if path == "/series":
            body = json.dumps(
                {"series": [{"series_ticker": t} for t in self.catalog]}
            ).encode()
            return body, 200
        if path == "/markets":
            series = (params or {}).get("series_ticker", "")
            rows = list(self.markets.get(series, []))
            status = (params or {}).get("status")
            if status:
                if status not in QUERY_STATUS_TO_RETURNED:
                    # What the live API does with `status=active`: HTTP 400
                    # "invalid status filter". A filter word is not a market
                    # status and the fake must not pretend otherwise.
                    raise RuntimeError(
                        f"invalid status filter {status!r} (accepted: "
                        f"{sorted(QUERY_STATUS_TO_RETURNED)})"
                    )
                allowed = QUERY_STATUS_TO_RETURNED[status]
                rows = [m for m in rows if m.get("status") in allowed]
            return json.dumps({"markets": rows, "cursor": ""}).encode(), 200
        if path == "/events":
            series = (params or {}).get("series_ticker", "")
            if series in self.unreadable:
                raise RuntimeError(f"simulated transient failure reading {series}")
            return json.dumps(
                {"events": list(self.events.get(series, [])), "cursor": ""}
            ).encode(), 200
        raise AssertionError(f"unexpected path {path!r}")

    def get(self, path: str, params: dict | None = None) -> dict:
        body, _ = self.get_raw(path, params)
        return json.loads(body)

    def get_retrying(self, path: str, params: dict | None = None) -> dict:
        return self.get(path, params)

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """
    Stands in for threadc.session.TickerBankSession.

    run() never returns on its own -- exactly like the real reconnect loop -- so
    a test that sees run_calls or session_epoch change knows the socket really
    was restarted.
    """

    def __init__(self, *, tickers, label, channels=CHANNELS):
        self._tickers = list(tickers)
        # What this session subscribes to. The real TickerBankSession is handed
        # the run's selection by the recorder's default factory; an injected
        # factory keeps its two-argument signature, so a test that cares passes
        # the selection in the same way.
        self.channels = tuple(channels)
        self.message_counts = Counter()
        self.initial_tickers = list(tickers)
        self.created_ts = time.monotonic()
        self.label = label
        self.session_epoch = 0
        self.connected = False
        self.stopped = False
        self.subscribe_params: dict = {}
        self.run_calls = 0
        self.stop_calls = 0
        self.updates: list[tuple[list, list]] = []
        self._raw_sink = None
        self._health_sink = None
        self._on_epoch_established = None
        self._running = asyncio.Event()

    @property
    def tickers(self):
        return list(self._tickers)

    async def run(self) -> None:
        self.run_calls += 1
        self.connect()
        await asyncio.Event().wait()  # never completes, like the real session

    def connect(self) -> None:
        """
        Simulate one connection epoch coming up, subscription and all.

        Fires `on_epoch_established` exactly like the real session does once
        every channel is acknowledged, because that callback is what makes the
        recorder bank an explicit initial state -- and "the bank has a state to
        start from" must be testable without a socket.
        """
        self.session_epoch += 1
        self.connected = True
        self.subscribe_params = {
            "channels": list(self.channels),
            "market_tickers": list(self._tickers),
            "use_yes_price": True,
        }
        if self._health_sink:
            self._health_sink("connect", time.time(), "", f"epoch={self.session_epoch}")
        if self._on_epoch_established:
            self._on_epoch_established(self.label, self.session_epoch, time.time())

    def counts_snapshot(self) -> dict:
        return dict(self.message_counts)

    def receive(self, text: str) -> None:
        """
        Deliver one inbound frame the way the real dispatch does: raw sink
        first, then the type counter.
        """
        self.feed(text)
        try:
            mtype = str(json.loads(text).get("type") or "_untyped")
        except json.JSONDecodeError:
            mtype = "_undecodable"
        self.message_counts[mtype] += 1

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stopped = True
        self.connected = False

    async def update_markets(self, add, remove) -> None:
        self.updates.append((list(add), list(remove)))
        for t in add:
            if t not in self._tickers:
                self._tickers.append(t)
        for t in remove:
            if t in self._tickers:
                self._tickers.remove(t)

    def feed(self, text: str, *, direction: str = "in") -> None:
        """Deliver one raw frame exactly as the real session's dispatch would."""
        assert self._raw_sink is not None, "no raw sink attached"
        self._raw_sink(direction, time.time(), text)


def frame(ticker: str, seq: int) -> str:
    """A frame shaped like a real `ticker` message -- same keys, same formats."""
    return json.dumps({
        "type": "ticker", "sid": 1, "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_bid_dollars": f"0.{(seq % 98) + 1:02d}00",
            "yes_ask_dollars": f"0.{(seq % 98) + 2:02d}00",
            "yes_bid_size_fp": f"{(seq % 40) + 1}.00",
            "yes_ask_size_fp": f"{(seq % 30) + 1}.00",
            "price_dollars": f"0.{(seq % 98) + 1:02d}00",
            "volume_fp": f"{seq}.00",
            "open_interest_fp": f"{seq * 2}.00",
            "last_trade_size_fp": "1.00",
            "ts_ms": 1793145600000 + seq,
        },
    })


def trade_frame(ticker: str, seq: int) -> str:
    """A frame shaped like a real `trade` message."""
    return json.dumps({
        "type": "trade", "sid": 2, "seq": seq,
        "msg": {
            "trade_id": f"trade-{seq:08d}",
            "market_ticker": ticker,
            "yes_price_dollars": f"0.{(seq % 98) + 1:02d}00",
            "no_price_dollars": f"0.{98 - (seq % 98):02d}00",
            "count_fp": "1.00",
            "taker_side": "yes" if seq % 2 else "no",
            "taker_outcome_side": "yes" if seq % 2 else "no",
            "taker_book_side": "no" if seq % 2 else "yes",
            "is_block_trade": False,
            "ts_ms": 1793145600000 + seq,
        },
    })


def ample_disk(free_multiplier: float = 4.0):
    """
    A disk check that always reports plenty of headroom.

    Injected by default in the recorder tests so they assert the recorder's
    behaviour rather than the developer machine's free space. The real
    threadc.disk.check is exercised directly in the low-disk tests, which
    construct the DiskStatus they need.
    """
    from threadc import disk

    def check(path, reserve):
        free = int(reserve * free_multiplier)
        return disk.DiskStatus(
            path=str(path), total_bytes=free * 2, used_bytes=free,
            free_bytes=free, reserve_bytes=int(reserve),
        )

    return check


def starved_disk(free_fraction: float = 0.5):
    """A disk check that always reports free space at or below the reserve."""
    from threadc import disk

    def check(path, reserve):
        free = int(reserve * free_fraction)
        return disk.DiskStatus(
            path=str(path), total_bytes=int(reserve) * 2,
            used_bytes=int(reserve) * 2 - free, free_bytes=free,
            reserve_bytes=int(reserve),
        )

    return check


__all__ = ["FakeAPI", "FakeSession", "QUERY_STATUS_TO_RETURNED", "event",
           "market", "frame", "trade_frame", "ample_disk", "starved_disk"]
