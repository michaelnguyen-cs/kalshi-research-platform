#!/usr/bin/env python
"""
STEP 0: the live Kalshi protocol-semantics probe for `ticker` size-only emission.

    python scripts/threadc_ticker_probe.py --minutes 20 --out probe_report.json

One authenticated WebSocket connection, both `orderbook_delta` and `ticker`
subscribed simultaneously over the SAME mechanically selected markets, so a
reconstructed top-of-book change can be matched in time against the ticker
stream. Nothing is banked: no raw archive is written, no segment is opened, and
the process holds only the bounded in-memory state its matching windows need.

Read-only against Kalshi. There is no order path: the REST side is
threadc.readonly.ReadOnlyAPI (four GET endpoints, allowlisted), and the
WebSocket side sends exactly one `subscribe` and nothing else.

This is NOT a capacity experiment. It runs only as long as it needs to
accumulate size-only opportunities, and it reports protocol counts -- never a
market identity, a price, an outcome or a research statistic.

Exit statuses:

    0   the probe ran and produced a verdict (any verdict, including a refuting
        or insufficient one -- the run itself succeeded)
    1   interrupted
    3   the probe failed to run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import structlog  # noqa: E402
import websockets.asyncio.client as ws_asyncio  # noqa: E402

from depthrec.book import BookError, CanonicalBook  # noqa: E402
from depthrec.census import RestCensus  # noqa: E402
from kbot.auth import kalshi_headers, load_private_key  # noqa: E402
from kbot.config import settings  # noqa: E402
from kbot.log_config import configure_logging  # noqa: E402
from threadc import frozen, ticker_probe as tp  # noqa: E402
from threadc.readonly import CensusClientAdapter, ReadOnlyAPI  # noqa: E402
from threadc.universe import AppendOnlyUniverse  # noqa: E402

log = structlog.get_logger("threadc.ticker_probe")

_WS_SIGN_PATH = "/trade-api/ws/v2"

INITIAL_STATE_WINDOW_SECONDS = 10.0
"""How long after the subscribe is sent a message counts as "initial state".

Answers section 3's question -- does subscribing establish an explicit initial
ticker/top-of-book state, or does the channel stay silent until something
changes? -- by counting which markets were heard from at all in that window,
against the number subscribed. Aggregate counts only."""

PROGRESS_INTERVAL_SECONDS = 60.0


class _NullCensusStore:
    """
    A store that discards everything RestCensus writes.

    RestCensus persists every request, response body and discovery into a
    segment archive. The probe opens no archive: it needs the census only for
    the mechanical market list, and banking thousands of response bodies for a
    run that is explicitly not a collection run would be storage this probe has
    no business writing.
    """

    def record_census_request(self, **_: Any) -> int:
        return 0

    def upsert_census_market(self, **_: Any) -> str:
        return "seen"

    def record_census_change(self, **_: Any) -> None:
        return None


class TickerProbe:
    """One probe run: subscribe, observe, analyse."""

    def __init__(self, *, market_limit: int, duration_seconds: float, archive_dir: Path) -> None:
        self.market_limit = market_limit
        self.duration = duration_seconds
        self.archive_dir = archive_dir

        self.api = ReadOnlyAPI()
        self.universe = AppendOnlyUniverse(archive_dir)
        self.census = RestCensus(
            client=CensusClientAdapter(self.api),
            store=_NullCensusStore(),
            series=self.universe.series,
        )

        self.matcher = tp.StreamingMatcher()
        self.analysis = self.matcher.analysis
        self.books: dict[str, CanonicalBook] = {}
        self._seq_by_sid: dict[Any, int] = {}
        self.markets: list[str] = []
        self.subscribe_ts: float | None = None
        self.started_ts: float | None = None
        self.initial_snapshot_markets: set[str] = set()
        self.initial_ticker_markets: set[str] = set()
        self.channels_acknowledged: set[str] = set()
        self._stop = asyncio.Event()
        self._cfg = settings
        self._cfg.require_credentials()
        self._private_key = load_private_key(self._cfg.private_key_path)

    # -- Market selection ------------------------------------------------------

    def select_markets(self) -> list[str]:
        self.census.run_live_pass()
        active = [
            e.ticker
            for e in self.census.entries.values()
            if e.status == frozen.LIVE_MARKET_STATUS
        ]
        self.markets = tp.select_probe_markets(active, self.market_limit)
        log.info(
            "probe.markets_selected",
            active_in_universe=len(active),
            selected=len(self.markets),
            limit=self.market_limit,
            series_in_universe=len(self.universe.series),
            rule="lexicographic ticker order, first N (0 = all)",
        )
        return self.markets

    # -- The socket ------------------------------------------------------------

    async def run(self) -> tp.ProbeAnalysis:
        if not self.markets:
            self.select_markets()
        if not self.markets:
            self.analysis.notes.append(
                "no market in the frozen universe reported status "
                f"{frozen.LIVE_MARKET_STATUS!r}; nothing was subscribed"
            )
            return self.finish()

        self.started_ts = time.time()
        deadline = time.monotonic() + self.duration
        try:
            await self._connect_and_consume(deadline)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.analysis.errors += 1
            self.analysis.notes.append(f"probe ended on {type(exc).__name__}: {exc}")
            log.error("probe.failed", error=str(exc))
        return self.finish()

    async def _connect_and_consume(self, deadline: float) -> None:
        headers = kalshi_headers(
            "GET", _WS_SIGN_PATH, self._cfg.kalshi_key_id, self._private_key
        )
        log.info(
            "probe.connecting", url=self._cfg.ws_url, markets=len(self.markets),
            channels=list(tp.PROBE_CHANNELS),
        )
        async with ws_asyncio.connect(
            self._cfg.ws_url, additional_headers=headers, max_size=None
        ) as ws:
            payload = {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": list(tp.PROBE_CHANNELS),
                    "market_tickers": list(self.markets),
                    "use_yes_price": frozen.USE_YES_PRICE,
                },
            }
            self.subscribe_ts = time.time()
            await ws.send(json.dumps(payload))
            log.info("probe.subscribe_sent", markets=len(self.markets))

            next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    continue
                self._dispatch(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                if time.monotonic() >= next_progress:
                    next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
                    self._log_progress(deadline)

    def _log_progress(self, deadline: float) -> None:
        log.info(
            "probe.progress",
            seconds_left=round(deadline - time.monotonic()),
            orderbook_messages=self.analysis.orderbook_messages,
            ticker_messages=self.analysis.ticker_messages,
            top_of_book_transitions=self.analysis.top_of_book_transitions,
            opportunities=self.analysis.qualifying_opportunities,
            unambiguous=self.analysis.unambiguous_opportunities,
            matched=self.analysis.matched,
            lag_spread_s=self.analysis.lag.spread(),
            backlog_excluded=self.analysis.outcomes[tp.AMBIGUOUS_CONSUMER_BACKLOG],
        )

    # -- Message handling ------------------------------------------------------

    def _dispatch(self, text: str) -> None:
        recv_ts = time.time()
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            self.analysis.errors += 1
            return

        mtype = msg.get("type")
        if mtype == "error":
            self.analysis.errors += 1
            code = (msg.get("msg") or {}).get("code")
            if code in (25, 26):
                self.analysis.subscription_limit_errors += 1
            self.analysis.notes.append(f"server error frame, code={code!r}")
            log.error("probe.server_error", payload=text[:500])
            self._stop.set()
            return
        if mtype == "subscribed":
            body = msg.get("msg") or {}
            channel = str(body.get("channel") or "")
            if channel:
                self.channels_acknowledged.add(channel)
            log.info("probe.subscribed", sid=body.get("sid"), channel=channel)
            return
        if mtype in ("ok", "unsubscribed", "pong"):
            return

        sid, seq = msg.get("sid"), msg.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and sid is not None:
            previous = self._seq_by_sid.get(sid)
            if previous is not None and seq > previous + 1:
                # Messages are missing on that subscription. A book carried
                # across the hole would produce transitions the exchange never
                # sent, so every reconstructed book is dropped and rebuilt from
                # the next snapshot.
                self.analysis.sequence_gaps += 1
                self.analysis.errors += 1
                self.books.clear()
                self.matcher.reset_books()
                log.error(
                    "probe.sequence_gap", sid=sid, missing=seq - previous - 1,
                )
            self._seq_by_sid[sid] = seq

        body = msg.get("msg") or {}
        market = body.get("market_ticker")
        if not market:
            return
        market = str(market)

        # How far behind the exchange this consumer is running. Matching two
        # channels by receive time is only sound while this stays small, so it
        # is measured on every message that carries an exchange timestamp.
        exchange_ts = tp.exchange_ts_seconds(body)
        if exchange_ts is not None:
            self.matcher.note_lag(recv_ts, recv_ts - exchange_ts)

        if mtype in ("orderbook_snapshot", "orderbook_delta"):
            self.analysis.orderbook_messages += 1
            self._apply_book(mtype, market, body, seq=seq, sid=sid, recv_ts=recv_ts)
            if mtype == "orderbook_snapshot" and self._within_initial_window(recv_ts):
                self.initial_snapshot_markets.add(market)
        elif mtype == tp.TICKER_CHANNEL:
            self.analysis.ticker_messages += 1
            self.matcher.on_ticker(market, body, recv_ts)
            if self._within_initial_window(recv_ts):
                self.initial_ticker_markets.add(market)
        elif mtype == "trade":
            self.analysis.trade_messages += 1

    def _within_initial_window(self, recv_ts: float) -> bool:
        return (
            self.subscribe_ts is not None
            and recv_ts - self.subscribe_ts <= INITIAL_STATE_WINDOW_SECONDS
        )

    def _apply_book(
        self, mtype: str, market: str, body: dict[str, Any], *,
        seq: Any, sid: Any, recv_ts: float,
    ) -> None:
        try:
            if mtype == "orderbook_snapshot":
                book = CanonicalBook(ticker=market)
                book.apply_snapshot(
                    body, seq=int(seq or 0), sid=sid, recv_ts=recv_ts,
                    use_yes_price=frozen.USE_YES_PRICE,
                )
                self.books[market] = book
            else:
                book = self.books.get(market)
                if book is None or book.snapshot_seq is None:
                    # A delta with no snapshot behind it is not applied to
                    # anything. Inventing a base book would manufacture
                    # top-of-book transitions that never happened.
                    return
                book.apply_delta(
                    body, seq=int(seq or 0), recv_ts=recv_ts,
                    use_yes_price=frozen.USE_YES_PRICE,
                )
        except BookError:
            self.analysis.errors += 1
            self.books.pop(market, None)
            return

        wire = book.top_of_book()
        self.matcher.on_top(
            market,
            tp.Top(
                bid_price=wire.bid_price, bid_qty=wire.bid_qty,
                ask_price=wire.ask_price, ask_qty=wire.ask_qty,
            ),
            recv_ts,
        )

    # -- Result ----------------------------------------------------------------

    def finish(self) -> tp.ProbeAnalysis:
        now = time.time()
        self.analysis.subscribed_markets = len(self.markets)
        self.analysis.initial_state_window_seconds = INITIAL_STATE_WINDOW_SECONDS
        self.analysis.markets_with_initial_orderbook_snapshot = len(
            self.initial_snapshot_markets
        )
        self.analysis.markets_with_initial_ticker = len(self.initial_ticker_markets)
        self.analysis.observed_seconds = (
            0.0 if self.started_ts is None else now - self.started_ts
        )
        if self.channels_acknowledged:
            self.analysis.notes.append(
                "channels acknowledged by the server: "
                + ", ".join(sorted(self.channels_acknowledged))
            )
        self.matcher.flush(now)
        try:
            self.api.close()
        except Exception:  # noqa: BLE001
            pass
        return self.analysis

    def request_stop(self) -> None:
        self._stop.set()


async def _main(args: argparse.Namespace) -> int:
    probe = TickerProbe(
        market_limit=args.markets,
        duration_seconds=args.minutes * 60.0,
        archive_dir=args.archive_dir,
    )
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, probe.request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    analysis = await probe.run()
    text = tp.format_report(analysis.report())
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--minutes", type=float, default=20.0,
                    help="how long to observe (default 20)")
    ap.add_argument("--markets", type=int, default=0,
                    help="mechanical cap on subscribed markets; 0 (default) means "
                         "every active market in the frozen universe")
    ap.add_argument("--archive-dir", type=Path,
                    default=Path("./threadc-probe-universe"),
                    help="where the append-only universe state is read/seeded from. "
                         "The probe writes no archive; this holds universe state only.")
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    configure_logging(args.verbose)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"PROBE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
