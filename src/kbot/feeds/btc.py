"""
External BTC spot price and returns feed.

Two ingest paths write into the SAME (timestamp, price) history deque:

  run()     REST poll of the Coinbase public spot endpoint. Fixed interval,
            always-on heartbeat/fallback -- it is what keeps the feed alive
            when the socket is down or geo-blocked.
  run_ws()  WebSocket subscription to the Coinbase Exchange `ticker`
            channel. Pushes a sample on every trade print, so the feed sees
            a spot move in tens of milliseconds instead of waiting out a
            poll interval.

The WS path exists because this bot's entire edge is Kalshi's 3-7 s
repricing lag behind spot: at a 2 s poll the feed alone burned up to 2.5 s
(poll interval + request latency) of that window before the strategy even
saw the move. Ticks cut that to the socket's own latency.

All public — no auth, no rate-limit concerns for reasonable polling intervals.
Polling interval should be >= 1 s to avoid being soft-blocked by Coinbase.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections import deque

import httpx
import structlog
import websockets.asyncio.client as ws_asyncio

log = structlog.get_logger(__name__)

# Coinbase public spot price endpoint (no API key needed).
_COINBASE_PRICE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# Coinbase Exchange public market-data socket (no API key needed for `ticker`).
# NOT Binance: binance.com and binance.us market-data endpoints are geo-blocked
# from the US IPs this bot runs on, so a Binance feed silently never connects.
_COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
_WS_PRODUCT_ID = "BTC-USD"

# The strategy needs recent history for vol estimation; keep the last N prices.
_DEFAULT_HISTORY = 60   # minutes of data at 1-min polling

# Deque size once ticks are flowing. Sized for tick density, not poll density:
# BTC-USD prints tens of ticks/second in a fast move, and the retention that
# was ample for a 2 s poll (~1810 samples for a 3600 s window) would hold only
# a couple of minutes of ticks. 50k samples is a few MB and covers the largest
# vol window with room to spare. Consumers filter by TIME (history_within), so
# an oversized deque costs memory, never correctness.
_WS_HISTORY_LEN = 50_000

# Floor on the spacing between `on_price` callbacks fired from the WS path.
# Ticks ALWAYS land in the history deque -- this throttles only the callback.
# The callback (runner._on_btc_price) runs a full off-cycle evaluation of every
# near-ATM ticker synchronously; at tick rate a single 10 bps move would re-run
# that on every print for the whole 3 s trigger window, stalling the socket
# read loop behind it. 0.25 s keeps ~10x the responsiveness of the 2 s poll
# while bounding the evaluation rate.
_WS_CALLBACK_MIN_INTERVAL = 0.25


class BTCFeed:
    """
    Async polling feed for BTC/USD spot price.

    Maintains a rolling window of (timestamp, price) pairs and exposes
    the current price and annualised log-return volatility estimate.

    Parameters
    ----------
    poll_interval:
        Seconds between price fetches (minimum 1 s recommended).
    history_len:
        Number of price samples to keep for vol estimation. If None
        (the default), sized from `vol_window_seconds` and
        `poll_interval` instead of a fixed constant -- see
        `vol_window_seconds`.
    vol_window_seconds:
        Largest trailing window (seconds) any consumer of this feed's
        history is expected to request (e.g. kbot.vol.ewma_vol's own
        window_seconds param). Only used to size `history_len` when that's
        not given explicitly. 2026-07: this feed is a SHARED singleton
        across every strategy/ticker needing "btc_spot" (see
        kbot.runner.Runner), and different tickers can need different
        windows (KXBTC15M's 900s vs KXBTC's 3600s -- see
        Strategy.vol_window_seconds) -- retaining enough raw history for
        the LARGEST window lets each caller filter down to its own
        shorter window via `history_within()` without the feed needing to
        know per-ticker specifics. The previous fixed default (60 samples)
        badly undersized this at typical 2s polling (120s of retained
        history against a 3600s window requirement), silently starving
        ewma_vol of the data a window filter would need.
    on_price:
        Optional async or sync callback invoked after each successful fetch,
        and after each WS tick (subject to `ws_callback_min_interval`).
        Receives (price: float, timestamp: float).
    ws_callback_min_interval:
        Minimum seconds between `on_price` invocations originating from the
        WS path. Ticks always reach the history deque regardless; this only
        bounds how often the callback's downstream work runs.
    max_ws_reconnect_wait:
        Back-off ceiling in seconds between WS reconnect attempts.
    """

    def __init__(
        self,
        *,
        poll_interval: float = 60.0,
        history_len: int | None = None,
        vol_window_seconds: float = 3600.0,
        on_price=None,
        ws_callback_min_interval: float = _WS_CALLBACK_MIN_INTERVAL,
        max_ws_reconnect_wait: float = 60.0,
    ) -> None:
        if poll_interval < 1.0:
            raise ValueError("poll_interval must be >= 1 second")
        self._interval = poll_interval
        self._vol_window_seconds = vol_window_seconds
        if history_len is None:
            history_len = max(_DEFAULT_HISTORY, int(vol_window_seconds / poll_interval) + 10)
        self._history: deque[tuple[float, float]] = deque(maxlen=history_len)
        self._on_price = on_price
        self._client = httpx.AsyncClient(timeout=10.0)
        self._running = False

        self._ws_callback_min_interval = ws_callback_min_interval
        self._max_ws_reconnect_wait = max_ws_reconnect_wait
        self._ws_connected = False
        self._ws_ticks = 0
        self._last_ws_tick_ts: float | None = None
        self._last_ws_callback_ts = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def price(self) -> float | None:
        """Most recent BTC/USD price, or None if no data yet."""
        if not self._history:
            return None
        return self._history[-1][1]

    @property
    def vol_annualised(self) -> float | None:
        """
        Annualised log-return volatility from the price history.

        Returns None if fewer than 2 samples are available.

        APPROXIMATE for mixed tick/poll data. The estimator treats every
        log-return as covering the same amount of time, which is exactly
        true for the REST poll alone but not once run_ws() is interleaving
        irregularly-spaced ticks with it. The guard below derives the
        annualisation factor from the MEAN OBSERVED spacing of the retained
        samples rather than assuming `poll_interval` -- that keeps the
        scaling roughly right when ticks dominate. Tick-spaced returns are
        small because each covers ~0.1 s rather than 2 s, so annualising
        them with the nominal poll factor would UNDERSTATE vol several-fold.
        It does NOT correct for the variance
        of the spacing itself; a time-weighted estimator would. Prefer
        kbot.vol.ewma_vol over this property for anything a trade depends
        on -- the runner already does.
        """
        if len(self._history) < 2:
            return None

        prices = [p for _, p in self._history]
        log_returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
        ]
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / max(n - 1, 1)
        std_per_sample = math.sqrt(variance)

        # Mean observed spacing, falling back to the nominal poll interval
        # when timestamps are degenerate (all-equal, or non-monotonic).
        span = self._history[-1][0] - self._history[0][0]
        spacing = span / n if span > 0 else self._interval

        samples_per_year = (365.25 * 24 * 3600) / spacing
        return std_per_sample * math.sqrt(samples_per_year)

    @property
    def history(self) -> list[tuple[float, float]]:
        """Snapshot of the (timestamp, price) history, oldest first."""
        return list(self._history)

    def history_within(self, window_seconds: float, *, now: float | None = None) -> list[tuple[float, float]]:
        """
        (timestamp, price) samples within the trailing `window_seconds` of
        `now` (defaults to the latest sample's own timestamp), oldest
        first. Lets a caller needing a SHORTER window than this feed's own
        retention (e.g. KXBTC15M's 900s vs. the feed's 3600s-sized
        retention -- see vol_window_seconds) get just that slice, without
        the feed itself needing to know per-ticker specifics.
        """
        if not self._history:
            return []
        now = now if now is not None else self._history[-1][0]
        cutoff = now - window_seconds
        return [(t, p) for t, p in self._history if t >= cutoff]

    def spot_move_bps(self, window_seconds: float = 3.0, *, now: float | None = None) -> float | None:
        """
        Signed basis-point move of the current price relative to the oldest
        sample still within `window_seconds` of `now`.

        Used by the event-driven spot trigger (Source 6): a large move in a
        short window means Kalshi's 3-7s repricing lag hasn't caught up yet.
        Returns None if there's no earlier sample inside the window (e.g.
        right at startup, or a window shorter than the poll interval).
        """
        if not self._history:
            return None
        now = now if now is not None else self._history[-1][0]
        current_price = self._history[-1][1]

        earliest_in_window: float | None = None
        for ts, price in self._history:
            if now - ts <= window_seconds:
                earliest_in_window = price
                break
        if earliest_in_window is None or earliest_in_window <= 0:
            return None

        return (current_price - earliest_in_window) / earliest_in_window * 10_000.0

    def has_spot_trigger(
        self,
        threshold_bps: float,
        window_seconds: float = 3.0,
        *,
        now: float | None = None,
    ) -> bool:
        """True if the absolute spot move over `window_seconds` is >= `threshold_bps`."""
        move = self.spot_move_bps(window_seconds, now=now)
        return move is not None and abs(move) >= threshold_bps

    async def run(self) -> None:
        """
        Poll for prices indefinitely.

        Retries silently on fetch errors (logs a warning).
        Cancel the owning asyncio.Task to stop.
        """
        self._running = True
        log.info("btc_feed.started", interval_s=self._interval)
        try:
            while True:
                await self._fetch_once()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            log.info("btc_feed.stopped")
            raise
        finally:
            await self._client.aclose()
            self._running = False

    async def fetch_once(self) -> float | None:
        """Fetch a single price update.  Returns the price or None on error."""
        return await self._fetch_once()

    @property
    def ws_connected(self) -> bool:
        """True while the WS tick stream is connected."""
        return self._ws_connected

    @property
    def ws_tick_count(self) -> int:
        """Ticks ingested from the WS stream this process."""
        return self._ws_ticks

    async def run_ws(self) -> None:
        """
        Stream ticks from the Coinbase Exchange `ticker` channel indefinitely,
        appending into the same history deque the REST poll writes to.

        Runs ALONGSIDE run(), not instead of it: the poll stays on as a
        heartbeat and as the fallback that keeps `price` fresh whenever the
        socket is down. Duplicate/interleaved samples from both paths are
        harmless -- every consumer (history_within, spot_move_bps, ewma_vol)
        reads (timestamp, price) pairs and filters by time.

        Reconnects with exponential back-off on any non-cancellation error.
        Cancel the owning asyncio.Task to stop.
        """
        self._enable_tick_history()
        attempt = 0
        log.info("btc_feed.ws_started", url=_COINBASE_WS_URL, product=_WS_PRODUCT_ID)
        while True:
            try:
                await self._ws_connect_and_consume()
                # A clean server-side close resets the back-off.
                attempt = 0
            except asyncio.CancelledError:
                log.info("btc_feed.ws_stopped", ticks=self._ws_ticks)
                raise
            except Exception as exc:  # noqa: BLE001
                wait = min(
                    self._max_ws_reconnect_wait,
                    (2 ** attempt) * (0.5 + random.random() * 0.5),
                )
                log.warning(
                    "btc_feed.ws_reconnecting",
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                    attempt=attempt + 1,
                    wait_s=round(wait, 2),
                )
                await asyncio.sleep(wait)
                attempt += 1
            finally:
                self._ws_connected = False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _enable_tick_history(self) -> None:
        """
        Grow the history deque to tick density, preserving existing samples.

        Called once at run_ws() start rather than in __init__ so a REST-only
        deployment keeps its poll-sized retention. Rebinding the deque is safe
        under asyncio: there is no await between construction and assignment,
        and every accessor reads self._history fresh.
        """
        if (self._history.maxlen or 0) >= _WS_HISTORY_LEN:
            return
        old_maxlen = self._history.maxlen
        self._history = deque(self._history, maxlen=_WS_HISTORY_LEN)
        log.info("btc_feed.ws_history_resized", was=old_maxlen, now=_WS_HISTORY_LEN)

    async def _ws_connect_and_consume(self) -> None:
        log.info("btc_feed.ws_connecting", url=_COINBASE_WS_URL)
        async with ws_asyncio.connect(_COINBASE_WS_URL) as ws:
            await ws.send(json.dumps({
                "type": "subscribe",
                "product_ids": [_WS_PRODUCT_ID],
                "channels": ["ticker"],
            }))
            self._ws_connected = True
            log.info("btc_feed.ws_connected", product=_WS_PRODUCT_ID)
            try:
                async for raw in ws:
                    await self._handle_ws_message(raw)
            finally:
                self._ws_connected = False

    async def _handle_ws_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("btc_feed.ws_parse_error", error=str(exc), raw=str(raw)[:200])
            return

        msg_type = msg.get("type")

        if msg_type == "error":
            # Subscribe rejected / bad product -- surface it, then let the
            # socket's own close drive the reconnect.
            log.error("btc_feed.ws_server_error", msg=str(msg)[:300])
            return

        if msg_type == "subscriptions":
            log.debug("btc_feed.ws_subscriptions", msg=str(msg)[:300])
            return

        if msg_type != "ticker":
            return

        raw_price = msg.get("price")
        if raw_price is None:
            return
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            log.warning("btc_feed.ws_bad_price", raw=str(raw_price)[:80])
            return
        if price <= 0:
            return

        # Local receipt time, not the exchange's `time` field: every other
        # sample in this deque is stamped locally, and the whole point is
        # measuring OUR latency to the move. Mixing clocks would skew
        # spot_move_bps' short windows.
        ts = time.time()
        self._history.append((ts, price))
        self._ws_ticks += 1
        self._last_ws_tick_ts = ts
        log.debug("btc_feed.ws_tick", price=price)

        if self._on_price is None:
            return
        if ts - self._last_ws_callback_ts < self._ws_callback_min_interval:
            return
        self._last_ws_callback_ts = ts
        result = self._on_price(price, ts)
        if asyncio.iscoroutine(result):
            await result

    async def _fetch_once(self) -> float | None:
        try:
            resp = await self._client.get(_COINBASE_PRICE_URL)
            resp.raise_for_status()
            data = resp.json()
            price = float(data["data"]["amount"])
            ts = time.time()
            self._history.append((ts, price))
            log.debug("btc_feed.price", price=price)

            if self._on_price is not None:
                result = self._on_price(price, ts)
                if asyncio.iscoroutine(result):
                    await result

            return price

        except Exception as exc:  # noqa: BLE001
            log.warning(
                "btc_feed.fetch_error",
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None
