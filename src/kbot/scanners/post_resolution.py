"""
Post-resolution repricing scanner (MARKET_RESEARCH.md item 3 / Source 5).

After a major market resolves, adjacent contracts in the same series often
lag in repricing for 15-30 minutes -- a structural lag edge (faster eyes on
the SAME model, no forecasting required). This scanner:

  1. Detects a market transitioning from open to settled by watching
     wall-clock time cross each tracked ticker's own known expiry (via
     Strategy.expiry_ts) -- not an assumed/unverified WS lifecycle-message
     format. Every strategy already computes an exact expiry while parsing
     its own ticker, so exposing that is enough; no new WS channel needed.
  2. Opens a per-SERIES post-resolution window (config
     POST_RESOLUTION_WINDOW_MINUTES, default 30) the first time a ticker in
     that series settles. A second settlement in the same series while a
     window is already open restarts it against the newest settlement.
  3. While a window is open, evaluates that series' OTHER tracked tickers
     at a faster cadence (every 1s, driven by this scanner's own loop --
     the runner's normal 5s cycle is untouched) with a discounted edge-gate
     threshold (config POST_RESOLUTION_EDGE_DISCOUNT, default 0.30 = 30%
     lower bar): the market is actively repricing, edges appear and
     disappear faster than the normal cycle can catch.
  4. Logs a window-close summary: which market settled, how many signals
     fired, how many cleared the normal gate vs the discounted gate.

Runs as its own background task (see kbot.runner.Runner), sharing the
runner's books dict (read-only -- never mutated here), Executor, and
Telemetry: it doesn't own any state the main eval loop doesn't already
have, and fired signals go through the EXACT SAME safety gauntlet
(kill-switch, advisory flags, price floor, driver cap, position guard,
live-mode gate) as the main loop, via Executor.execute()'s
min_net_edge_override parameter -- not a parallel execution pipeline.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.execution import Executor
from kbot.models.types import OrderBook, SizingResult
from kbot.strategy.base import Strategy
from kbot.telemetry import Telemetry

log = structlog.get_logger(__name__)

# Post-resolution windows re-evaluate every second (vs the runner's normal
# 5s cycle) -- the whole point is to catch a fast-repricing market the
# normal cadence would mostly miss.
_MONITOR_INTERVAL_SECONDS: float = 1.0


@dataclass
class PostResolutionWindow:
    """One active (or just-closed) post-resolution window for a series."""
    series: str
    strategy_name: str
    settled_ticker: str
    opened_at: float
    signals_fired: int = 0
    cleared_normal: int = 0
    cleared_discounted: int = 0


class PostResolutionScanner:
    """
    Parameters
    ----------
    books:
        Shared, read-only reference to the runner's ticker -> OrderBook
        dict. Never mutated here -- the scanner only reads it.
    strategies:
        name -> Strategy, the same dict the runner drives its main
        evaluation loop with.
    executor:
        Shared Executor. Signals fired here go through the SAME safety
        gauntlet as the main loop, just with a discounted edge-gate
        threshold passed via min_net_edge_override.
    telemetry:
        Shared Telemetry, for window-open/close event logging.
    bankroll_provider:
        Callable(strategy_name) -> allocation-scaled bankroll, mirroring
        kbot.runner.Runner._evaluate_ticker's own alloc_bankroll.
    cfg:
        Settings (defaults to the module singleton).
    """

    def __init__(
        self,
        books: dict[str, OrderBook],
        strategies: dict[str, Strategy],
        executor: Executor,
        telemetry: Telemetry,
        bankroll_provider,
        cfg: Settings | None = None,
    ) -> None:
        self._books = books
        self._strategies = strategies
        self._executor = executor
        self._telemetry = telemetry
        self._bankroll_provider = bankroll_provider
        self._cfg = cfg or _default_settings

        # Tickers already flagged as settled -- a settlement is a one-time
        # transition, this stops it from re-triggering every tick for as
        # long as the ticker's book entry lingers.
        self._settled_seen: set[str] = set()
        self._active_windows: dict[str, PostResolutionWindow] = {}  # series -> window

    # ── Public API ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Monitor forever (until the owning task is cancelled)."""
        while True:
            await asyncio.sleep(_MONITOR_INTERVAL_SECONDS)
            self.tick()

    def tick(self) -> None:
        """
        One monitoring pass: detect new settlements, close windows whose
        duration has elapsed, then evaluate whatever's still active.
        Closing BEFORE evaluating keeps the boundary crisp -- a window
        that just turned stale gets zero further evaluations in the same
        tick that closes it, rather than one last "farewell" evaluation.
        Synchronous and side-effect-ordered so tests can drive it directly
        without an asyncio event loop.
        """
        now = time.time()
        self._detect_settlements(now)
        self._close_expired_windows(now)
        self._evaluate_active_windows(now)

    @property
    def active_windows(self) -> dict[str, PostResolutionWindow]:
        """Read-only snapshot of currently open windows, keyed by series."""
        return dict(self._active_windows)

    # ── Settlement detection ─────────────────────────────────────────────

    @staticmethod
    def _series_for_ticker(strategy: Strategy, ticker: str) -> str | None:
        """Which of strategy.series_tickers matches `ticker`, or None."""
        for series in strategy.series_tickers:
            if ticker.startswith(f"{series}-"):
                return series
        return None

    def _detect_settlements(self, now: float) -> None:
        for ticker in list(self._books):
            if ticker in self._settled_seen:
                continue
            for name, strategy in self._strategies.items():
                if not strategy.owns_ticker(ticker):
                    continue
                expiry = strategy.expiry_ts(ticker)
                if expiry is None or now <= expiry:
                    continue
                self._settled_seen.add(ticker)
                series = self._series_for_ticker(strategy, ticker)
                if series is not None:
                    self._open_window(series, name, ticker, now)
                break  # exactly one strategy should own a given ticker

    def _open_window(self, series: str, strategy_name: str, settled_ticker: str, now: float) -> None:
        window = PostResolutionWindow(
            series=series, strategy_name=strategy_name, settled_ticker=settled_ticker, opened_at=now,
        )
        self._active_windows[series] = window  # a fresh settlement restarts the window
        log.info(
            "post_resolution.window_opened",
            series=series, strategy=strategy_name, settled_ticker=settled_ticker,
        )
        self._telemetry.log_event(
            "post_resolution_window_opened",
            payload={"series": series, "strategy": strategy_name, "settled_ticker": settled_ticker},
        )

    # ── Active-window evaluation (discounted edge gate) ─────────────────

    def _evaluate_active_windows(self, now: float) -> None:
        for series, window in list(self._active_windows.items()):
            strategy = self._strategies.get(window.strategy_name)
            if strategy is None:
                continue
            normal_min_edge = self._cfg.min_net_edge
            discounted_min_edge = normal_min_edge * (1.0 - self._cfg.post_resolution_edge_discount)

            for ticker, book in list(self._books.items()):
                if ticker == window.settled_ticker:
                    continue  # nothing left to trade on an already-settled market
                if self._series_for_ticker(strategy, ticker) != series:
                    continue

                bankroll = self._bankroll_provider(window.strategy_name)
                results = self._evaluate_with_discount(strategy, book, bankroll, now, discounted_min_edge)

                for r in results:
                    self._handle_result(window, ticker, r, normal_min_edge, discounted_min_edge)

    def _evaluate_with_discount(
        self,
        strategy: Strategy,
        book: OrderBook,
        bankroll: float,
        now: float,
        discounted_min_edge: float,
    ) -> list[SizingResult]:
        """
        Evaluate `book` with `strategy`'s own min_net_edge temporarily
        swapped to the discounted threshold, so its internal Kelly sizing
        produces real (non-zero) contracts for edges between the
        discounted and normal bars -- not just an already-rejected skip
        the strategy's OWN unmodified cfg would have produced.

        Safe under asyncio's cooperative scheduling: this swap-call-restore
        sequence contains no `await`, so no other coroutine (including the
        runner's own main eval loop, which might read the same strategy's
        cfg) can run between the swap and its restoration -- holds as long
        as evaluate() itself stays fully synchronous, true of every
        strategy in this codebase today.
        """
        original_cfg = strategy.cfg
        discounted_cfg = original_cfg.model_copy(update={"min_net_edge": discounted_min_edge})
        strategy.cfg = discounted_cfg
        try:
            return strategy.evaluate(book, bankroll=bankroll, current_time=now)
        finally:
            strategy.cfg = original_cfg

    def _handle_result(
        self,
        window: PostResolutionWindow,
        ticker: str,
        r: SizingResult,
        normal_min_edge: float,
        discounted_min_edge: float,
    ) -> None:
        if r.contracts <= 0:
            return  # didn't even clear the discounted bar

        window.signals_fired += 1
        window.cleared_discounted += 1  # true by construction: contracts>0 under the discounted cfg
        if r.net_edge_dollars >= normal_min_edge:
            window.cleared_normal += 1

        strategy = self._strategies.get(window.strategy_name)
        is_single_binary = strategy.is_single_binary(ticker) if strategy is not None else False
        ack = self._executor.execute(
            r, strategy=window.strategy_name, min_net_edge_override=discounted_min_edge,
            is_single_binary=is_single_binary,
        )
        if ack is not None:
            self._telemetry.log_order(ticker, {
                "strategy": window.strategy_name,
                "client_order_id": ack.client_order_id,
                "dry_run": ack.dry_run,
                "contracts": r.contracts,
                "price": r.price_dollars,
                "net_edge": r.net_edge_dollars,
                "post_resolution": True,
            })

    # ── Window close / logging ──────────────────────────────────────────

    def _close_expired_windows(self, now: float) -> None:
        window_seconds = self._cfg.post_resolution_window_minutes * 60.0
        for series, window in list(self._active_windows.items()):
            if now - window.opened_at < window_seconds:
                continue
            log.info(
                "post_resolution.window_closed",
                series=series,
                strategy=window.strategy_name,
                settled_ticker=window.settled_ticker,
                signals_fired=window.signals_fired,
                cleared_normal=window.cleared_normal,
                cleared_discounted=window.cleared_discounted,
            )
            self._telemetry.log_event(
                "post_resolution_window_closed",
                payload={
                    "series": series,
                    "strategy": window.strategy_name,
                    "settled_ticker": window.settled_ticker,
                    "signals_fired": window.signals_fired,
                    "cleared_normal": window.cleared_normal,
                    "cleared_discounted": window.cleared_discounted,
                },
            )
            del self._active_windows[series]
