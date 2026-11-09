"""
The Thread C bank's WebSocket session: `ticker` + `trade`, raw, and nothing else.

Deliberately NOT depthrec.session.ResearchSession. That class is Thread A's, it
is frozen on the `orderbook_delta` channel by section 6 of the mechanics
document, and it exists to RECONSTRUCT A BOOK -- snapshot, sequenced deltas,
canonical ladders, read_book()'s validity verdicts. Thread C's bank keeps none of
that: it preserves messages, it does not interpret them. Widening the frozen
research channel tuple to make one class serve both would couple this recorder's
fidelity to Thread A's frozen parameters, which is exactly the coupling the
project forbids.

What this session does:

  * one authenticated connection carrying one subscribe over `ticker` and
    `trade` for its shard's markets, with `use_yes_price` sent EXPLICITLY;
  * hands EVERY frame, in and out, to the raw sink before any parsing, so the
    archive holds the exact bytes Kalshi sent;
  * counts messages by type, because the corrected capacity measurement must
    report aggregate ticker, trade and total message counts and there is no
    honest way to get those from compressed archive rows during the run;
  * records connect, disconnect, server error and sequence gap as health events,
    so a gap in the archive is attributable to a connection rather than inferred
    from silence;
  * follows rotation through update_markets(), the same add/delete protocol the
    proven recorder uses.

What it deliberately does NOT do: build a book, hold a price, judge a market,
decide anything from message content, or place an order. It has no order path
because it has no client that could contain one.

THE CHANNEL DOES NOT SNAPSHOT. `orderbook_delta` answers a subscribe with an
`orderbook_snapshot` per market; `ticker` and `trade` answer with nothing at all
and stay silent until something changes. So the absence of a message after a
subscribe is NOT a statement that the previous state still holds, and this
session does not pretend otherwise: it fires `on_epoch_established` once per
connection epoch and leaves it to the recorder to bank an explicit initial state
from REST. Every epoch is numbered and recorded, so the boundary between "we
know this because we were told" and "we know this because we asked" is always
visible in the archive.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from typing import Any, Callable

import structlog
import websockets.asyncio.client as ws_asyncio

from depthrec.session import (  # reused: identical wire protocol, identical intent
    SUBSCRIPTION_BUFFER_OVERFLOW_CODE,
    SUBSCRIPTION_MARKET_LIMIT_CODE,
    SubscriptionRejected,
    _ObservingConnection,
)
from kbot.auth import kalshi_headers, load_private_key
from kbot.config import Settings, settings as _default_settings
from threadc import frozen

log = structlog.get_logger(__name__)

_WS_SIGN_PATH = "/trade-api/ws/v2"

RawSink = Callable[[str, float, str], None]
"""(direction, recv_ts, raw_text), called for every frame before any parsing."""

HealthSink = Callable[[str, float, str, str], None]
"""(kind, ts, reason, detail)."""

EpochSink = Callable[[str, int, float], None]
"""(shard_label, session_epoch, ts), fired once per established subscription."""


class TickerBankSession:
    """One connection banking `ticker` + `trade` for one shard of the universe.

    A capacity measurement may hand it a narrower subset of the frozen pair; a
    production run never does, and the default is the frozen pair itself.
    """

    def __init__(
        self,
        *,
        tickers: list[str],
        label: str = "A",
        cfg: Settings | None = None,
        raw_sink: RawSink | None = None,
        health_sink: HealthSink | None = None,
        on_epoch_established: EpochSink | None = None,
        channels: tuple[str, ...] | list[str] = frozen.CHANNELS,
        max_reconnect_wait: float = 60.0,
    ) -> None:
        self._cfg = cfg or _default_settings
        self._tickers = list(dict.fromkeys(tickers))
        self.label = label
        # The frozen pair unless a capacity measurement selected a narrower
        # subset of it (threadc.config._parse_channels). Held per session rather
        # than read from threadc.frozen at each use, because the establishment
        # rule below counts acknowledgements against it: a session that expects
        # two sids while the server was only asked for one would never fire
        # on_epoch_established, so no initial state would be banked and a
        # capacity window would never open.
        self.channels: tuple[str, ...] = tuple(channels)
        self._raw_sink = raw_sink
        self._health_sink = health_sink
        self._on_epoch_established = on_epoch_established
        self._max_wait = max_reconnect_wait

        self._sids: set[Any] = set()
        self._seq_by_sid: dict[Any, int] = {}
        self._cmd_id = 0
        self._ws: Any = None

        self.connected = False
        self.session_epoch = 0
        self.last_activity_ts: float | None = None
        self.last_ping_ts: float | None = None
        self.last_pong_ts: float | None = None
        self.connected_since: float | None = None
        self.subscribe_params: dict[str, Any] | None = None
        self.stopped = False

        # Aggregate protocol counters. Message TYPES only -- never a per-market
        # count, which would be a research statistic about activity.
        self.message_counts: Counter[str] = Counter()

        # Credentials are loaded lazily, at connect. Constructing a session does
        # no I/O, which is what lets the dispatch and subscription logic be
        # tested without a key on disk. Startup still fails closed on missing
        # credentials: threadc.readonly.ReadOnlyAPI requires them in its own
        # constructor, and the recorder builds one before any session exists.
        self._private_key: Any = None

    # -- Public surface --------------------------------------------------------

    @property
    def tickers(self) -> list[str]:
        return list(self._tickers)

    def counts_snapshot(self) -> dict[str, int]:
        return dict(self.message_counts)

    async def update_markets(self, add: list[str], remove: list[str]) -> None:
        """
        Follow rotation: add newly opened markets, drop ones past close.

        The authoritative list is `self._tickers`, so a change made here survives
        the next reconnect even when there is no live connection to send it on.
        A newly added market gets no snapshot from either channel, so the
        recorder is told about it through `on_epoch_established`'s sibling path
        and banks an explicit REST state for it.
        """
        add = [t for t in dict.fromkeys(add) if t not in self._tickers]
        remove = [t for t in dict.fromkeys(remove) if t in self._tickers]
        if not add and not remove:
            return
        for t in add:
            self._tickers.append(t)
        for t in remove:
            self._tickers.remove(t)

        ws = self._ws
        sid = next(iter(self._sids), None)
        if ws is None or sid is None:
            log.info(
                "threadc.ws.update_deferred",
                label=self.label, added=len(add), removed=len(remove),
            )
            return
        # One update per subscription id: `ticker` and `trade` are separate
        # subscriptions on this connection and each carries its own sid.
        for action, tickers in (("add_markets", add), ("delete_markets", remove)):
            if not tickers:
                continue
            for target_sid in sorted(self._sids, key=repr):
                self._cmd_id += 1
                payload = {
                    "id": self._cmd_id,
                    "cmd": "update_subscription",
                    "params": {
                        "sid": target_sid, "action": action, "market_tickers": tickers,
                    },
                }
                text = json.dumps(payload)
                if self._raw_sink:
                    self._raw_sink("out", time.time(), text)
                await ws.send(text)
            log.info(
                "threadc.ws.update_subscription",
                label=self.label, action=action, n=len(tickers), sids=len(self._sids),
            )

    async def run(self) -> None:
        """Connect, subscribe, consume; reconnect with backoff until stopped."""
        attempt = 0
        while not self.stopped:
            try:
                await self._connect_and_consume()
                attempt = 0
            except asyncio.CancelledError:
                self._on_disconnect("cancelled", "session task cancelled")
                raise
            except SubscriptionRejected:
                self._on_disconnect("subscription_rejected", "server refused the subscribe")
                raise
            except Exception as exc:  # noqa: BLE001
                self._on_disconnect(type(exc).__name__, str(exc))
                if self.stopped:
                    return
                wait = min(self._max_wait, 2 ** min(attempt, 6))
                log.warning(
                    "threadc.ws.reconnect",
                    label=self.label, error=str(exc), wait_s=round(wait, 2),
                )
                await asyncio.sleep(wait)
                attempt += 1

    async def stop(self) -> None:
        self.stopped = True
        ws = self._ws
        if ws is not None:
            await ws.close()

    # -- Internals -------------------------------------------------------------

    def _credentials(self) -> Any:
        if self._private_key is None:
            self._cfg.require_credentials()
            self._private_key = load_private_key(self._cfg.private_key_path)
        return self._private_key

    async def _connect_and_consume(self) -> None:
        headers = kalshi_headers(
            "GET", _WS_SIGN_PATH, self._cfg.kalshi_key_id, self._credentials()
        )
        url = self._cfg.ws_url

        self._seq_by_sid.clear()
        self._sids.clear()
        self.session_epoch += 1

        def hook(kind: str, ts: float) -> None:
            self.last_activity_ts = ts
            if kind == "ping":
                self.last_ping_ts = ts
            elif kind == "pong":
                self.last_pong_ts = ts

        connection_class = type(
            "_ThreadCConnection", (_ObservingConnection,), {"depthrec_hook": staticmethod(hook)}
        )

        log.info(
            "threadc.ws.connecting",
            label=self.label, url=url, markets=len(self._tickers),
            channels=list(self.channels),
        )
        async with ws_asyncio.connect(
            url, additional_headers=headers, create_connection=connection_class, max_size=None
        ) as ws:
            self._ws = ws
            self.connected = True
            self.connected_since = time.time()
            self.last_activity_ts = self.connected_since
            self._health("connect", "", f"epoch={self.session_epoch}")
            log.info("threadc.ws.connected", label=self.label, epoch=self.session_epoch)
            try:
                await self._send_subscribe(ws)
                async for raw in ws:
                    self._dispatch(raw)
            finally:
                self._ws = None
                self.connected = False

    async def _send_subscribe(self, ws) -> None:
        """
        One subscribe covering every ticker on every selected channel, with
        `use_yes_price` sent EXPLICITLY.

        No API default is relied upon: Kalshi documents that the default will
        flip and that the flag will later be removed, so an unchanged codebase
        could otherwise silently change price semantics part-way through a
        two-month collection.
        """
        if not self._tickers:
            return
        self._cmd_id += 1
        params = {
            "channels": list(self.channels),
            "market_tickers": list(self._tickers),
            "use_yes_price": frozen.USE_YES_PRICE,
        }
        payload = {"id": self._cmd_id, "cmd": "subscribe", "params": params}
        self.subscribe_params = params
        text = json.dumps(payload)
        if self._raw_sink:
            self._raw_sink("out", time.time(), text)
        await ws.send(text)
        log.info(
            "threadc.ws.subscribe_sent",
            label=self.label, cmd_id=self._cmd_id, markets=len(self._tickers),
            channels=params["channels"], use_yes_price=frozen.USE_YES_PRICE,
        )

    def _dispatch(self, raw: str | bytes) -> None:
        recv_ts = time.time()
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if self._raw_sink:
            self._raw_sink("in", recv_ts, text)
        self.last_activity_ts = recv_ts

        try:
            msg: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            self.message_counts["_undecodable"] += 1
            self._health("parse_error", "", str(exc))
            return

        mtype = str(msg.get("type") or "_untyped")
        self.message_counts[mtype] += 1

        if mtype == "error":
            self._health("server_error", "", text[:1000])
            raise SubscriptionRejected(msg)

        if mtype == "subscribed":
            body = msg.get("msg") or {}
            sid = body.get("sid")
            self._sids.add(sid)
            self._health("subscribed", "", f"sid={sid} channel={body.get('channel')}")
            log.info(
                "threadc.ws.subscribed",
                label=self.label, sid=sid, channel=body.get("channel"),
            )
            if len(self._sids) == len(self.channels) and self._on_epoch_established:
                # Every subscribed channel is live. This is the moment the
                # recorder must establish an explicit initial state, because
                # neither channel will ever volunteer one.
                self._on_epoch_established(self.label, self.session_epoch, recv_ts)
            return

        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is not None and isinstance(seq, int) and not isinstance(seq, bool):
            self._check_sequence(sid, seq)

    def _check_sequence(self, sid: Any, seq: int) -> None:
        """
        Per-SID sequence continuity, recorded and never repaired.

        Nothing here reconstructs state, so a gap cannot invalidate a book --
        there is no book. What a gap does mean is that messages the archive was
        supposed to preserve are missing, which is exactly the kind of hole a
        reader must be able to see rather than infer, so it is written down as a
        health event with the count of missing messages.

        A repeated or older seq is not a gap: nothing is missing, so it is noted
        and otherwise ignored.
        """
        previous = self._seq_by_sid.get(sid)
        if previous is not None and seq != previous + 1:
            if seq <= previous:
                self._health("seq_replay", "", f"sid={sid} seq={seq} previous={previous}")
            else:
                detail = (
                    f"sid={sid} seq jumped {previous} -> {seq} "
                    f"({seq - previous - 1} message(s) missing)"
                )
                self._health("sequence_gap", "", detail)
                log.error("threadc.ws.sequence_gap", label=self.label, detail=detail)
        self._seq_by_sid[sid] = seq

    def _on_disconnect(self, reason: str, detail: str) -> None:
        self.connected = False
        self._health("disconnect", reason, detail)

    def _health(self, kind: str, reason: str, detail: str) -> None:
        if self._health_sink:
            self._health_sink(kind, time.time(), reason, detail)


__all__ = [
    "TickerBankSession", "SubscriptionRejected",
    "SUBSCRIPTION_MARKET_LIMIT_CODE", "SUBSCRIPTION_BUFFER_OVERFLOW_CODE",
]
