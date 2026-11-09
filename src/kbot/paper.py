"""
Paper trading engine — live demo-feed data, simulated fills, SQLite logging.

PaperTrader is a thin single-strategy wrapper over kbot.runner.Runner
(Phase B2's multi-strategy engine): it builds a Runner with exactly one
strategy at allocation 1.0 and delegates everything to it. Kept for
backward compatibility and as the regression-guaranteed single-strategy
path -- `kbot paper --strategy X` behaves identically to before Phase B2.

For multiple strategies sharing one engine, construct kbot.runner.Runner
directly (see kbot.cli's --strategies path).

Runs until cancelled (Ctrl-C or kill signal).
"""

from __future__ import annotations

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.models.types import OrderBook
from kbot.runner import (  # noqa: F401 -- re-exported for existing callers
    BookInvalidated,
    Runner,
    _apply_delta,
    _parse_orderbook,
)
from kbot.strategy.base import Strategy

log = structlog.get_logger(__name__)


class PaperTrader:
    """
    Live demo-feed paper trader for a single strategy.

    Parameters
    ----------
    strategy:
        Instantiated Strategy object to evaluate each tick.
    tickers:
        Market tickers to subscribe to.
    cfg:
        Settings (defaults to module singleton).  dry_run MUST be True here
        (PaperTrader enforces this regardless of the config value).
    """

    def __init__(
        self,
        strategy: Strategy,
        tickers: list[str],
        cfg: Settings | None = None,
    ) -> None:
        self._cfg = cfg or _default_settings

        if not self._cfg.dry_run:
            raise RuntimeError(
                "PaperTrader requires DRY_RUN=true in settings.  "
                "Use kbot.execution.Executor directly for live trading."
            )

        self._strategy_name = strategy.name
        self._runner = Runner(
            {strategy.name: strategy},
            {strategy.name: 1.0},
            self._cfg,
            tickers=tickers,
            live_confirmed=False,
        )

    async def run(self) -> None:
        """Start all feeds and run the paper trading loop until cancelled."""
        log.info("paper.starting", strategy=self._strategy_name, tickers=self._runner._explicit_tickers)
        await self._runner.run()

    def expire_market(self, ticker: str) -> None:
        """
        Mark all open positions for `ticker` as closed (market resolved/expired).

        Call this whenever you receive a market-resolution notification.
        """
        self._runner.expire_market(ticker)

    def close(self) -> None:
        self._runner.close()

    # ── Backward-compat pass-through accessors ──────────────────────────────
    # kbot.runner.Runner owns all real state; these exist so existing
    # introspection/tests written against PaperTrader's old single-strategy
    # internals keep working unchanged.

    @property
    def _strategy(self) -> Strategy:
        return self._runner._strategies[self._strategy_name]

    @property
    def _books(self) -> dict[str, OrderBook]:
        return self._runner._books

    @property
    def _gates(self):
        return self._runner._gates

    @property
    def _executor(self):
        return self._runner._executor

    @property
    def _sheets(self):
        return self._runner._sheets

    @property
    def _btc_feed(self):
        return self._runner._feeds.get("btc_spot")

    @property
    def _session_spot_triggers(self) -> int:
        return self._runner._session_spot_triggers

    @property
    def _eval_cycles(self) -> int:
        return self._runner._eval_cycles[self._strategy_name]

    def _on_btc_price(self, price: float, ts: float) -> None:
        self._runner._on_btc_price(price, ts)

    def _on_ws_message(self, msg: dict) -> None:
        self._runner._on_ws_message(msg)

    def _run_consistency_scan(self) -> None:
        self._runner._run_consistency_scan()

    def _evaluate_ticker(
        self,
        ticker: str,
        book: OrderBook,
        btc_price: float | None,
        btc_vol: float | None,
    ) -> None:
        self._runner._evaluate_ticker(self._strategy_name, self._strategy, ticker, book, btc_price, btc_vol)
