"""
WebSocket feed client for Kalshi market data (WS API v2).

Sends RSA-PSS auth headers on the HTTP upgrade handshake, then subscribes
to the requested channels.  Runs a reconnect loop indefinitely; callers
cancel the asyncio Task to stop.

Supported channels:
  orderbook_delta  — incremental yes/no-side price-level updates
  ticker           — last price, volume, open-interest summary
  trade            — individual fills on the public tape
  fill             — your own fills (requires credentials)

Quick usage:
    async def handle(msg: dict) -> None:
        print(msg)

    client = WSClient(tickers=["KXBTC-25JUN2600"], on_message=handle)
    asyncio.run(client.run())
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
import websockets
import websockets.asyncio.client as ws_asyncio

from kbot.auth import kalshi_headers, load_private_key
from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

# Path signed during the HTTP upgrade (query string is excluded per auth spec).
_WS_SIGN_PATH = "/trade-api/ws/v2"


class SequenceGap(Exception):
    """
    Raised when a subscription's `seq` skips. Propagates out of the consume
    loop so the socket is torn down and the reconnect path re-subscribes,
    which is the only way to get a fresh snapshot.

    Deliberately NOT recoverable in place: a partial-resync state machine has
    to decide which levels survived the gap, and getting that wrong leaves a
    silently wrong book -- exactly the failure this class exists to prevent.
    Drop everything and resnapshot.
    """


class WSClient:
    """
    Async WebSocket client with automatic reconnect and exponential backoff.

    Parameters
    ----------
    cfg:
        Settings instance; defaults to the module singleton.
    tickers:
        Market tickers to subscribe to (e.g. ["KXBTC-25JUN2600"]).
    channels:
        WS channels; defaults to ["orderbook_delta", "ticker"].
    on_message:
        Called for every JSON message received.  May be sync or async.
    on_invalidate:
        Called (synchronously) the instant this client can no longer vouch for
        the books it has fed the caller: disconnect, server error, subscription
        error, or a sequence gap. The caller MUST treat every book as unusable
        until a fresh snapshot arrives. Never called speculatively -- if it
        fires, the data really is suspect.
    max_reconnect_wait:
        Maximum back-off ceiling in seconds before each reconnect attempt.
    """

    def __init__(
        self,
        cfg: Settings | None = None,
        *,
        tickers: list[str],
        channels: list[str] | None = None,
        on_message: MessageHandler | None = None,
        on_invalidate: Callable[[str], None] | None = None,
        max_reconnect_wait: float = 60.0,
    ) -> None:
        self._cfg = cfg or _default_settings
        # Authoritative subscription set. Ordered for stable logging; a set
        # mirror guards against duplicate subscribes. This is what gets
        # re-sent on every reconnect, so add/prune here (subscribe_additional
        # / prune_tickers) is what makes a change survive a reconnect.
        self._tickers = list(dict.fromkeys(tickers))
        self._ticker_set: set[str] = set(self._tickers)
        self._channels = channels if channels is not None else ["orderbook_delta", "ticker"]
        self._on_message = on_message
        self._on_invalidate = on_invalidate
        self._max_wait = max_reconnect_wait
        self._cmd_id = 0
        # Sequence continuity is tracked per SUBSCRIPTION ID, not per ticker.
        # One subscription multiplexes every ticker it covers onto a single
        # seq counter, so a per-ticker counter would see each ticker's own
        # messages as a stream full of holes and false-positive constantly.
        # Cleared on every (re)connect: seq restarts per subscription.
        self._seq_by_sid: dict[Any, int] = {}
        # The live connection while connected, else None. Set on connect,
        # cleared on disconnect -- subscribe_additional sends on it only when
        # non-None (otherwise the new tickers just ride the next reconnect's
        # subscribe). Lets mid-session subscribes happen without reconnecting.
        self._ws: Any = None

        self._cfg.require_credentials()
        self._private_key = load_private_key(self._cfg.private_key_path)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def active_tickers(self) -> list[str]:
        """Current subscription set (what a reconnect would re-subscribe to)."""
        return list(self._tickers)

    async def subscribe_additional(self, tickers: list[str]) -> list[str]:
        """
        Subscribe to `tickers` not already subscribed, on the EXISTING
        connection (no reconnect). Returns the tickers actually newly added.

        New tickers are always added to the authoritative subscription set
        (so a later reconnect re-subscribes to them); a subscribe command is
        additionally sent immediately if currently connected. Sending a
        second `subscribe` for just the new tickers is valid WS-v2 -- each
        subscribe creates its own subscription; the existing ones are
        untouched, so no in-flight book state is lost.
        """
        new = [t for t in dict.fromkeys(tickers) if t not in self._ticker_set]
        if not new:
            return []
        self._tickers.extend(new)
        self._ticker_set.update(new)
        if self._ws is not None:
            await self._send_subscribe(self._ws, new)
            log.info("ws.subscribe_additional", added=len(new), tickers=new)
        else:
            log.info("ws.subscribe_additional_deferred", added=len(new), tickers=new)
        return new

    def prune_tickers(self, tickers: list[str]) -> list[str]:
        """
        Drop `tickers` from the authoritative subscription set so a future
        reconnect no longer re-subscribes to them. Returns the tickers
        actually removed.

        No server-side `unsubscribe` is sent: a settled Kalshi market stops
        emitting updates on its own, and WS-v2 unsubscribe is keyed by
        server-assigned subscription id (not ticker), which this client does
        not track. The caller (runner) also drops the pruned tickers' books
        so they stop being evaluated -- that, plus not re-subscribing on
        reconnect, is what "pruned" means here.
        """
        removed = [t for t in dict.fromkeys(tickers) if t in self._ticker_set]
        if not removed:
            return []
        self._ticker_set.difference_update(removed)
        self._tickers = [t for t in self._tickers if t in self._ticker_set]
        log.info("ws.prune_tickers", removed=len(removed), tickers=removed)
        return removed

    async def run(self) -> None:
        """
        Connect to the Kalshi WS feed and process messages forever.

        Reconnects automatically on any non-cancellation error.
        Cancel the owning asyncio.Task to stop cleanly.
        """
        attempt = 0
        while True:
            try:
                await self._connect_and_consume()
                # A clean server-side close resets the backoff.
                attempt = 0
                self._invalidate("disconnected")
            except asyncio.CancelledError:
                log.info("ws.stopped")
                self._invalidate("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                self._invalidate(f"connection_lost:{type(exc).__name__}")
                wait = min(
                    self._max_wait,
                    (2 ** attempt) * (0.5 + random.random() * 0.5),
                )
                log.warning(
                    "ws.reconnecting",
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                    attempt=attempt + 1,
                    wait_s=round(wait, 2),
                )
                await asyncio.sleep(wait)
                attempt += 1

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _connect_and_consume(self) -> None:
        auth_headers = kalshi_headers(
            "GET",
            _WS_SIGN_PATH,
            self._cfg.kalshi_key_id,
            self._private_key,
        )

        url = self._cfg.ws_url
        log.info(
            "ws.connecting",
            url=url,
            tickers=self._tickers,
            channels=self._channels,
        )

        # Nothing received before this point belongs to the session about to
        # start. Reset seq state and tell the caller its books are stale BEFORE
        # a single message of the new session can arrive.
        self._seq_by_sid.clear()
        self._invalidate("reconnect")

        async with ws_asyncio.connect(url, additional_headers=auth_headers) as ws:
            log.info("ws.connected")
            self._ws = ws
            try:
                await self._send_subscribe(ws, self._tickers)

                async for raw in ws:
                    await self._dispatch(raw)
            finally:
                # Never leave a stale handle: a subscribe_additional between
                # this disconnect and the next reconnect must defer (ride the
                # reconnect's subscribe), not send on a dead socket.
                self._ws = None

    async def _send_subscribe(self, ws, tickers: list[str]) -> None:
        """Send one `subscribe` command for `tickers` on `ws`. No-op if empty."""
        if not tickers:
            return
        self._cmd_id += 1
        payload = {
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": self._channels,
                "market_tickers": list(tickers),
                # Sent EXPLICITLY rather than relying on the server default.
                # False means the book is quoted as yes/no BIDS, which is what
                # _parse_orderbook and _apply_delta decode (a resting NO bid at
                # p is the offer that fills a YES buy at 1-p). If the server
                # default ever flipped, every book would silently invert.
                "use_yes_price": False,
            },
        }
        await ws.send(json.dumps(payload))
        log.debug("ws.subscribed", cmd_id=self._cmd_id, payload=payload)

    def _invalidate(self, reason: str) -> None:
        """
        Tell the caller every book is unusable. Never raises: a failing
        invalidation callback must not stop the reconnect loop, and must not
        be mistaken for a successful one.
        """
        if self._on_invalidate is None:
            return
        try:
            self._on_invalidate(reason)
        except Exception as exc:  # noqa: BLE001
            log.error("ws.invalidate_callback_failed", reason=reason, error=str(exc))

    def _check_sequence(self, msg: dict[str, Any]) -> None:
        """
        Enforce per-SID sequence continuity. Raises SequenceGap on a skip.

        Keyed by `sid` (subscription id), not by ticker: one subscription
        carries every ticker it covers on a single counter, so a per-ticker
        view of `seq` is full of legitimate holes.

        Messages without both `sid` and `seq` (command acks, subscription
        confirmations) are not part of any data stream and are skipped. A
        REPEATED or older seq is not a gap -- nothing is missing -- so it is
        logged and dropped rather than triggering a reconnect.
        """
        sid = msg.get("sid")
        seq = msg.get("seq")
        if sid is None or not isinstance(seq, int) or isinstance(seq, bool):
            return

        previous = self._seq_by_sid.get(sid)
        if previous is not None and seq != previous + 1:
            if seq <= previous:
                log.warning("ws.seq_replay", sid=sid, seq=seq, previous=previous)
                return
            raise SequenceGap(
                f"sid={sid} seq jumped {previous} -> {seq} "
                f"({seq - previous - 1} message(s) missing)"
            )
        self._seq_by_sid[sid] = seq

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("ws.parse_error", error=str(exc), raw=str(raw)[:200])
            return

        msg_type = msg.get("type", "unknown")

        if msg_type == "error":
            # Covers a subscription error too: if a subscribe was rejected we
            # are not receiving that ticker's updates, so anything already
            # delivered is now unmaintained. Fail closed rather than keep
            # serving a book nothing is updating.
            log.error("ws.server_error", msg=msg)
            self._invalidate("server_error")
            return

        # Before the handler sees it. A message delivered out of a gap has
        # already corrupted the book by the time the handler returns.
        try:
            self._check_sequence(msg)
        except SequenceGap as gap:
            log.error("ws.sequence_gap", detail=str(gap))
            self._invalidate("sequence_gap")
            raise

        log.debug("ws.message", type=msg_type, seq=msg.get("seq"))

        if self._on_message is not None:
            result = self._on_message(msg)
            if asyncio.iscoroutine(result):
                await result
