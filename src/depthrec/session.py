"""
Research WebSocket session: snapshot -> sequenced deltas, session health, and
read_book() with section 7's invariants.

Deliberately NOT kbot.ws_client. That client is the trading bot's, it is frozen
on `use_yes_price: False` (section 6 says so explicitly), and it exposes neither
Ping/Pong observation nor raw-message retention. Migrating it would couple the
research recording to the bot's release cycle -- the coupling section 6 forbids.
Nothing here imports kbot.execution.

Session health (section 3) is measured from actual received WebSocket frames.
Kalshi sends a Ping (opcode 0x9, body `heartbeat`) every 10 seconds, so a
30-second activity window is a test of the CONNECTION, not of market activity.
An unchanged book in a quiet market stays VALID -- quiet markets are
disproportionately the illiquid ones, and calling them unrecorded would remove
them from the denominator and inflate the measured clearance rate in the
favourable direction.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
import websockets
import websockets.asyncio.client as ws_asyncio
from websockets.asyncio.client import ClientConnection
from websockets.frames import Frame, Opcode

from depthrec import frozen
from depthrec.book import BookError, CanonicalBook, TopOfBook
from kbot.auth import kalshi_headers, load_private_key
from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)

_WS_SIGN_PATH = "/trade-api/ws/v2"

SUBSCRIPTION_MARKET_LIMIT_CODE = 26
"""AsyncAPI error code 26, "Subscription market limit exceeded". The code
exists; Kalshi publishes no number for the limit itself. If the server ever
returns this, the exact payload is what gets reported -- the recorder does not
go looking for a workable N on its own."""

SUBSCRIPTION_BUFFER_OVERFLOW_CODE = 25

RawSink = Callable[[str, float, str], None]
"""(direction, recv_ts, raw_text). Called for every frame in and out, BEFORE
any parsing, so a questioned decoder can be replayed against untouched bytes."""

HealthSink = Callable[[str, float, str, str], None]
"""(kind, ts, reason, detail)."""

BookStateSink = Callable[[str, "TopOfBook", float, float], None]
"""(ticker, canonical top of book, wall recv_ts, monotonic recv_ts).

Fired after every orderbook_snapshot or orderbook_delta that actually CHANGED
the book, and never for anything else -- not Ping/Pong, not acks, not a delta
that was dropped because the book was invalid. The state handed over is already
in canonical YES coordinates, because it comes from the reconstructed book
rather than from the wire message.

Both clocks are passed. Elapsed-time decisions must use the monotonic one: a
wall clock can jump under NTP, and the section 8 skew rule is an elapsed-time
test. The wall clock is carried alongside purely for provenance."""


class SubscriptionRejected(RuntimeError):
    """
    The server refused a subscribe. Carries the exact payload.

    Raised rather than retried with a smaller ticker list: choosing an N by
    bisecting against the server is exactly the experimental search the recorder
    is forbidden from doing.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else {}
        self.code = msg.get("code")
        super().__init__(json.dumps(payload, sort_keys=True))

    @property
    def is_market_limit(self) -> bool:
        return self.code in (SUBSCRIPTION_MARKET_LIMIT_CODE, SUBSCRIPTION_BUFFER_OVERFLOW_CODE)


class _ObservingConnection(ClientConnection):
    """
    A ClientConnection that timestamps every frame it receives, control frames
    included.

    websockets answers a Ping automatically inside the sans-I/O layer and offers
    no callback for it, so the only honest way to observe Kalshi's 10-second
    heartbeat is to watch frames arrive. process_event sees every frame the
    protocol accepted, so PING and PONG land here alongside data.
    """

    depthrec_hook: Callable[[str, float], None] | None = None

    def process_event(self, event) -> None:  # noqa: ANN001 - matches base signature
        hook = getattr(self, "depthrec_hook", None)
        if hook is not None and isinstance(event, Frame):
            if event.opcode is Opcode.PING:
                hook("ping", time.time())
            elif event.opcode is Opcode.PONG:
                hook("pong", time.time())
            elif event.opcode in (Opcode.TEXT, Opcode.BINARY, Opcode.CONT):
                hook("data", time.time())
        super().process_event(event)


@dataclass
class BookState:
    """
    One market's book plus everything read_book() needs to judge it.

    `invalid_reason` is a section 4 UNAVAILABLE_* string, or None when the book
    is trustworthy. It is set the moment trust is lost and cleared only by a
    fresh snapshot on the current session -- never by time passing.
    """

    book: CanonicalBook
    session_epoch: int
    invalid_reason: str | None = None
    invalid_detail: str = ""
    last_raw_ref: int | None = None

    @property
    def has_snapshot(self) -> bool:
        return self.book.snapshot_seq is not None


@dataclass
class ReadResult:
    """
    What read_book() returns: a section 4 validity verdict and, when VALID, the
    canonical top of book behind it.
    """

    ticker: str
    validity: str
    reason: str | None
    detail: str
    top: TopOfBook | None
    snapshot_recv_ts: float | None
    last_update_recv_ts: float | None
    last_update_exchange_ts_ms: int | None
    snapshot_seq: int | None
    last_seq: int | None
    sid: Any
    session_epoch: int
    updates_applied: int
    session_activity_ts: float | None
    session_activity_age: float | None
    last_ping_ts: float | None
    bids: list[tuple[int, int]] = field(default_factory=list)
    asks: list[tuple[int, int]] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.validity == frozen.VALIDITY_VALID


class ResearchSession:
    """
    One authenticated WebSocket connection carrying one orderbook_delta
    subscription over `tickers`.

    Section 8 runs two of these on separate connections, one per price
    convention, which is why `use_yes_price` is a constructor argument here
    while production is frozen on frozen.USE_YES_PRICE.
    """

    def __init__(
        self,
        *,
        tickers: list[str],
        use_yes_price: bool,
        label: str = "A",
        cfg: Settings | None = None,
        raw_sink: RawSink | None = None,
        health_sink: HealthSink | None = None,
        on_book_update: Callable[[str, float], Awaitable[None] | None] | None = None,
        on_book_state: BookStateSink | None = None,
        max_reconnect_wait: float = 60.0,
    ) -> None:
        self._cfg = cfg or _default_settings
        self._tickers = list(dict.fromkeys(tickers))
        self._use_yes_price = use_yes_price
        self.label = label
        self._raw_sink = raw_sink
        self._health_sink = health_sink
        self._on_book_update = on_book_update
        self._on_book_state = on_book_state
        self._max_wait = max_reconnect_wait

        self._books: dict[str, BookState] = {}
        self._seq_by_sid: dict[Any, int] = {}
        self._sids: set[Any] = set()
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

        self._cfg.require_credentials()
        self._private_key = load_private_key(self._cfg.private_key_path)

    # -- Public surface -------------------------------------------------------

    @property
    def tickers(self) -> list[str]:
        return list(self._tickers)

    @property
    def use_yes_price(self) -> bool:
        return self._use_yes_price

    def read_book(
        self, ticker: str, *, at_ts: float | None = None, include_levels: bool = True
    ) -> ReadResult:
        """
        Section 7's read_book(): the book's validity at `at_ts`, and its
        canonical top of book when VALID.

        The precedence of UNAVAILABLE reasons is fixed and deliberate:

          1. scheduled maintenance -- Kalshi pauses trading Thursday 03:00-05:00
             ET and disconnections there are expected, so labelling those
             moments DISCONNECT would bury a known-good explanation under a
             data-quality one. Section 4 keeps them in the raw census precisely
             so downtime cannot be hidden by deleting known-bad hours.
          2. not connected -- no trustworthy state can exist without a session.
          3. no snapshot on this session -- startup, or a market not yet
             subscribed. Never a drop: a signal with no recorder state is
             UNAVAILABLE_NO_SNAPSHOT, and it still counts in ALL.
          4. the book's own invalidation (sequence gap, decode failure).
          5. session staleness -- checked LAST among the failure modes because
             it is the weakest signal: it means the connection itself went
             quiet, not that this market did.

        Absence of a market-specific update is never a reason. An unchanged book
        stays VALID while session and sequence state remain valid.

        `include_levels=False` omits the full sorted ladders and returns only the
        top of book. The section 8 comparison loop reads 619 markets on two
        streams every second and only ever compares tops; sorting both ladders
        1,238 times a second for four hours would be pure waste. The recorder's
        own T-7 observation leaves it True -- it persists the whole book.
        """
        now = time.time() if at_ts is None else at_ts
        state = self._books.get(ticker)
        activity = self.last_activity_ts
        age = None if activity is None else now - activity

        def result(validity: str, reason: str | None, detail: str = "") -> ReadResult:
            book = state.book if state else None
            return ReadResult(
                ticker=ticker,
                validity=validity,
                reason=reason,
                detail=detail,
                top=book.top_of_book() if (book and validity == frozen.VALIDITY_VALID) else None,
                snapshot_recv_ts=book.snapshot_recv_ts if book else None,
                last_update_recv_ts=book.last_update_recv_ts if book else None,
                last_update_exchange_ts_ms=book.last_update_exchange_ts_ms if book else None,
                snapshot_seq=book.snapshot_seq if book else None,
                last_seq=book.last_seq if book else None,
                sid=book.sid if book else None,
                session_epoch=state.session_epoch if state else self.session_epoch,
                updates_applied=book.updates_applied if book else 0,
                session_activity_ts=activity,
                session_activity_age=age,
                last_ping_ts=self.last_ping_ts,
                bids=sorted(book.bids.items(), reverse=True) if (book and include_levels) else [],
                asks=sorted(book.asks.items()) if (book and include_levels) else [],
            )

        if frozen.in_scheduled_maintenance(now):
            return result(
                frozen.BUCKET_UNAVAILABLE,
                frozen.UNAVAILABLE_SCHEDULED_MAINTENANCE,
                "target time falls in Kalshi's Thursday 03:00-05:00 ET window",
            )
        if not self.connected:
            return result(
                frozen.BUCKET_UNAVAILABLE,
                frozen.UNAVAILABLE_DISCONNECT,
                "no live WebSocket session at target time",
            )
        if state is None or not state.has_snapshot:
            return result(
                frozen.BUCKET_UNAVAILABLE,
                frozen.UNAVAILABLE_NO_SNAPSHOT,
                "no orderbook_snapshot for this market on the current session",
            )
        if state.session_epoch != self.session_epoch:
            return result(
                frozen.BUCKET_UNAVAILABLE,
                frozen.UNAVAILABLE_DISCONNECT,
                f"book is from session epoch {state.session_epoch}, current is "
                f"{self.session_epoch}; a pre-gap book is never carried forward",
            )
        if state.invalid_reason is not None:
            return result(frozen.BUCKET_UNAVAILABLE, state.invalid_reason, state.invalid_detail)
        if age is None or age > frozen.SESSION_ACTIVITY_MAX_AGE_SECONDS:
            return result(
                frozen.BUCKET_UNAVAILABLE,
                frozen.UNAVAILABLE_SESSION_STALE,
                f"last session frame {age!r}s ago, limit is "
                f"{frozen.SESSION_ACTIVITY_MAX_AGE_SECONDS}s",
            )

        try:
            state.book.assert_invariants()
        except BookError as exc:
            self._invalidate_book(ticker, frozen.UNAVAILABLE_OTHER, str(exc))
            return result(frozen.BUCKET_UNAVAILABLE, frozen.UNAVAILABLE_OTHER, str(exc))

        return result(frozen.VALIDITY_VALID, None, "")

    def snapshot_all(self) -> dict[str, ReadResult]:
        """read_book() for every subscribed ticker, at one instant."""
        now = time.time()
        return {t: self.read_book(t, at_ts=now) for t in self._tickers}

    async def update_markets(self, add: list[str], remove: list[str]) -> None:
        """
        Follow rotation: add newly opened markets, drop ones long past close.

        The authoritative ticker list is `self._tickers`, so a change made here
        survives the next reconnect even if the live `update_subscription`
        cannot be sent (no connection right now). New markets get their own
        `orderbook_snapshot`; until it arrives they read UNAVAILABLE_NO_SNAPSHOT,
        never a book borrowed from anywhere else.
        """
        add = [t for t in dict.fromkeys(add) if t not in self._tickers]
        remove = [t for t in dict.fromkeys(remove) if t in self._tickers]
        if not add and not remove:
            return
        for t in add:
            self._tickers.append(t)
        for t in remove:
            self._tickers.remove(t)
            self._books.pop(t, None)

        ws = self._ws
        sid = next(iter(self._sids), None)
        if ws is None or sid is None:
            log.info(
                "depthrec.ws.update_deferred",
                label=self.label, added=len(add), removed=len(remove),
            )
            return
        for action, tickers in (("add_markets", add), ("delete_markets", remove)):
            if not tickers:
                continue
            self._cmd_id += 1
            payload = {
                "id": self._cmd_id,
                "cmd": "update_subscription",
                "params": {"sid": sid, "action": action, "market_tickers": tickers},
            }
            text = json.dumps(payload)
            if self._raw_sink:
                self._raw_sink("out", time.time(), text)
            await ws.send(text)
            log.info(
                "depthrec.ws.update_subscription",
                label=self.label, action=action, n=len(tickers), sid=sid,
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
                    "depthrec.ws.reconnect",
                    label=self.label, error=str(exc), wait_s=round(wait, 2),
                )
                await asyncio.sleep(wait)
                attempt += 1

    async def stop(self) -> None:
        self.stopped = True
        ws = self._ws
        if ws is not None:
            await ws.close()

    # -- Internals ------------------------------------------------------------

    async def _connect_and_consume(self) -> None:
        headers = kalshi_headers(
            "GET", _WS_SIGN_PATH, self._cfg.kalshi_key_id, self._private_key
        )
        url = self._cfg.ws_url

        # Every book from the previous session is unusable BEFORE a single
        # message of the new one can arrive.
        self._invalidate_all(frozen.UNAVAILABLE_DISCONNECT, "reconnecting")
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
            "_DepthrecConnection", (_ObservingConnection,), {"depthrec_hook": staticmethod(hook)}
        )

        log.info(
            "depthrec.ws.connecting",
            label=self.label, url=url, markets=len(self._tickers),
            use_yes_price=self._use_yes_price,
        )
        async with ws_asyncio.connect(
            url,
            additional_headers=headers,
            create_connection=connection_class,
            max_size=None,
        ) as ws:
            self._ws = ws
            self.connected = True
            self.connected_since = time.time()
            self.last_activity_ts = self.connected_since
            self._health("connect", "", f"epoch={self.session_epoch}")
            log.info("depthrec.ws.connected", label=self.label, epoch=self.session_epoch)
            try:
                await self._send_subscribe(ws)
                async for raw in ws:
                    await self._dispatch(raw)
            finally:
                self._ws = None
                self.connected = False

    async def _send_subscribe(self, ws) -> None:
        """
        One subscribe covering every ticker, with `use_yes_price` sent
        EXPLICITLY (section 6). No API default is relied upon in either
        direction: the B stream of the equivalence test sends `false` just as
        explicitly as production sends `true`.
        """
        if not self._tickers:
            return
        self._cmd_id += 1
        params = {
            "channels": list(frozen.RESEARCH_CHANNELS),
            "market_tickers": list(self._tickers),
            "use_yes_price": self._use_yes_price,
        }
        payload = {"id": self._cmd_id, "cmd": "subscribe", "params": params}
        self.subscribe_params = params
        text = json.dumps(payload)
        if self._raw_sink:
            self._raw_sink("out", time.time(), text)
        await ws.send(text)
        log.info(
            "depthrec.ws.subscribe_sent",
            label=self.label, cmd_id=self._cmd_id, markets=len(self._tickers),
            channels=params["channels"], use_yes_price=self._use_yes_price,
        )

    async def _dispatch(self, raw: str | bytes) -> None:
        recv_ts = time.time()
        recv_mono = time.monotonic()
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if self._raw_sink:
            self._raw_sink("in", recv_ts, text)
        self.last_activity_ts = recv_ts

        try:
            msg: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            self._health("parse_error", frozen.UNAVAILABLE_OTHER, str(exc))
            return

        mtype = msg.get("type")

        if mtype == "error":
            self._health("server_error", frozen.UNAVAILABLE_OTHER, text[:1000])
            self._invalidate_all(frozen.UNAVAILABLE_OTHER, f"server error: {text[:200]}")
            raise SubscriptionRejected(msg)

        if mtype == "subscribed":
            sid = (msg.get("msg") or {}).get("sid")
            self._sids.add(sid)
            self._health("subscribed", "", f"sid={sid}")
            log.info("depthrec.ws.subscribed", label=self.label, sid=sid)
            return

        sid = msg.get("sid")
        seq = msg.get("seq")
        if sid is None or not isinstance(seq, int) or isinstance(seq, bool):
            return

        if not self._check_sequence(sid, seq):
            return

        market = (msg.get("msg") or {}).get("market_ticker")
        if not market:
            return

        try:
            if mtype == "orderbook_snapshot":
                changed = self._apply_snapshot(
                    market, msg["msg"], seq=seq, sid=sid, recv_ts=recv_ts
                )
            elif mtype == "orderbook_delta":
                changed = self._apply_delta(market, msg["msg"], seq=seq, recv_ts=recv_ts)
            else:
                return
        except BookError as exc:
            self._invalidate_book(market, frozen.UNAVAILABLE_OTHER, str(exc))
            self._health("book_error", frozen.UNAVAILABLE_OTHER, f"{market}: {exc}")
            return

        # Event-level canonical state, for the section 8 skew rule only. The
        # 1 Hz grid remains the sole source of matched comparison cells; this
        # exists so that "did the lagging stream catch up within 500 ms?" is
        # answerable at all, which at 1 Hz sampling it is not.
        if changed and self._on_book_state is not None:
            state = self._books.get(market)
            if state is not None:
                self._on_book_state(market, state.book.top_of_book(), recv_ts, recv_mono)

        if self._on_book_update is not None:
            out = self._on_book_update(market, recv_ts)
            if asyncio.iscoroutine(out):
                await out

    def _check_sequence(self, sid: Any, seq: int) -> bool:
        """
        Per-SID sequence continuity. A gap invalidates every book on that
        subscription until a fresh snapshot repairs each one.

        Keyed by subscription id, not by ticker: one subscription multiplexes
        every ticker it covers onto a single counter, so a per-ticker view of
        `seq` is full of legitimate holes and would false-positive constantly.

        A repeated or older seq is not a gap -- nothing is missing -- so it is
        recorded and dropped rather than treated as corruption.
        """
        previous = self._seq_by_sid.get(sid)
        if previous is not None and seq != previous + 1:
            if seq <= previous:
                self._health("seq_replay", "", f"sid={sid} seq={seq} previous={previous}")
                return False
            detail = (
                f"sid={sid} seq jumped {previous} -> {seq} "
                f"({seq - previous - 1} message(s) missing)"
            )
            self._health("sequence_gap", frozen.UNAVAILABLE_SEQUENCE_GAP, detail)
            log.error("depthrec.ws.sequence_gap", label=self.label, detail=detail)
            self._invalidate_sid(sid, frozen.UNAVAILABLE_SEQUENCE_GAP, detail)
            self._seq_by_sid[sid] = seq
            return False
        self._seq_by_sid[sid] = seq
        return True

    def _apply_snapshot(
        self, ticker: str, body: dict[str, Any], *, seq: int, sid: Any, recv_ts: float
    ) -> bool:
        state = self._books.get(ticker)
        if state is None or state.session_epoch != self.session_epoch:
            state = BookState(book=CanonicalBook(ticker=ticker), session_epoch=self.session_epoch)
            self._books[ticker] = state
        state.book.apply_snapshot(
            body, seq=seq, sid=sid, recv_ts=recv_ts, use_yes_price=self._use_yes_price
        )
        # A fresh snapshot on the current session is the ONLY thing that repairs
        # a book: section 7 says a pre-gap book is never carried forward.
        state.invalid_reason = None
        state.invalid_detail = ""
        state.session_epoch = self.session_epoch
        return True

    def _apply_delta(
        self, ticker: str, body: dict[str, Any], *, seq: int, recv_ts: float
    ) -> bool:
        state = self._books.get(ticker)
        if state is None or not state.has_snapshot or state.session_epoch != self.session_epoch:
            # A delta with no snapshot behind it cannot be applied to anything.
            # Recording it as NO_SNAPSHOT rather than inventing a base book is
            # the difference between a missing measurement and a wrong one.
            self._books.setdefault(
                ticker, BookState(book=CanonicalBook(ticker=ticker), session_epoch=self.session_epoch)
            )
            self._invalidate_book(
                ticker, frozen.UNAVAILABLE_NO_SNAPSHOT, "delta before any snapshot"
            )
            return False
        if state.invalid_reason is not None:
            return False
        state.book.apply_delta(
            body, seq=seq, recv_ts=recv_ts, use_yes_price=self._use_yes_price
        )
        return True

    def _invalidate_book(self, ticker: str, reason: str, detail: str) -> None:
        state = self._books.get(ticker)
        if state is None:
            state = BookState(book=CanonicalBook(ticker=ticker), session_epoch=self.session_epoch)
            self._books[ticker] = state
        state.invalid_reason = reason
        state.invalid_detail = detail

    def _invalidate_sid(self, sid: Any, reason: str, detail: str) -> None:
        for ticker, state in self._books.items():
            if state.book.sid == sid and state.invalid_reason is None:
                state.invalid_reason = reason
                state.invalid_detail = detail

    def _invalidate_all(self, reason: str, detail: str) -> None:
        for state in self._books.values():
            if state.invalid_reason is None:
                state.invalid_reason = reason
                state.invalid_detail = detail

    def _on_disconnect(self, reason: str, detail: str) -> None:
        self.connected = False
        self._invalidate_all(frozen.UNAVAILABLE_DISCONNECT, f"{reason}: {detail}")
        self._health("disconnect", frozen.UNAVAILABLE_DISCONNECT, f"{reason}: {detail}")

    def _health(self, kind: str, reason: str, detail: str) -> None:
        if self._health_sink:
            self._health_sink(kind, time.time(), reason, detail)


__all__ = [
    "ResearchSession",
    "ReadResult",
    "BookState",
    "SubscriptionRejected",
    "SUBSCRIPTION_MARKET_LIMIT_CODE",
    "SUBSCRIPTION_BUFFER_OVERFLOW_CODE",
]
