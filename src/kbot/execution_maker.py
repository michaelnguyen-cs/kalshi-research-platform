"""
Maker-order execution — rest inside the spread instead of always crossing it.

Wraps an Executor so the full safety gauntlet (kill-switch, advisory flags,
edge gate, position guard, live-mode gauntlet) still applies, but a resting
(maker) order is placed instead of a taker order whenever it clears the edge
gate and the spread is wide enough to make resting worthwhile.

Fill detection here is deliberately conservative: DRY_RUN simulates a fill
only when the live orderbook shows the market trading through our resting
price. LIVE fills are not detected in this module — that requires the
'fill' WebSocket channel and is out of scope for Part 3 Phase A1. A resting
order placed live will NOT open a tracked position until that integration
exists; do not run this module against LIVE order flow yet.

Cancels are writes and are NEVER retried. If cancel_order raises, the
outcome is ambiguous (we don't know if the exchange executed it) — this is
logged at error level and the order is dropped from automated management so
a human can check the exchange-side state directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.edge import net_edge_maker_for_market, net_edge_taker
from kbot.execution import Executor
from kbot.kalshi_client import OrderAck
from kbot.quantity import UNITS_PER_CONTRACT
from kbot.models.types import OrderBook, Side, SizingResult

log = structlog.get_logger(__name__)

# Minimum spread (in dollars) required before resting is worth the fill risk.
_MIN_MAKER_SPREAD = 0.02
_CENT = 0.01


@dataclass
class RestingOrderBook:
    """Lifecycle record for a single resting (maker) limit order."""
    order_id: str
    client_order_id: str
    ticker: str
    side: Side
    price: float
    placed_at: float
    contracts: int
    reprice_count: int = 0
    strategy: str | None = None
    driver: str = "other"


def _ask_bid(book: OrderBook, side: Side) -> tuple[float | None, float | None]:
    """Return (ask, bid) in dollars for `side`, or (None, None) if the book is incomplete."""
    best_yes = book.best_yes()
    best_no = book.best_no()
    if best_yes is None or best_no is None:
        return None, None
    if side == Side.YES:
        return best_yes.price, round(1.0 - best_no.price, 4)
    return best_no.price, round(1.0 - best_yes.price, 4)


class MakerExecutor:
    """
    Chooses between crossing the spread (taker) and resting inside it (maker),
    then manages the resting order's lifecycle (reprice / timeout / cancel).

    Parameters
    ----------
    executor:
        The underlying Executor — reused for the full safety gauntlet and for
        the taker path unchanged.
    cfg:
        Settings (defaults to executor.cfg).
    """

    def __init__(self, executor: Executor, cfg: Settings | None = None) -> None:
        self._executor = executor
        self._cfg = cfg or executor.cfg or _default_settings
        self._resting: dict[tuple[str, str], RestingOrderBook] = {}

    @property
    def resting_orders(self) -> dict[tuple[str, str], RestingOrderBook]:
        return self._resting

    # ── Entry point ──────────────────────────────────────────────────────────

    def execute(
        self,
        result: SizingResult,
        book: OrderBook,
        *,
        now: float | None = None,
        strategy: str | None = None,
    ) -> OrderAck | None:
        """
        Decide taker vs. maker for this signal and place the order.

        `strategy`, if given, raises the effective min_net_edge threshold
        when that strategy's adverse_selection_detected flag is set, and
        tags any markout scheduled after a fill.

        Returns the OrderAck on success (taker fill or maker order accepted
        to rest), None on any skip.
        """
        now = time.time() if now is None else now
        key = (result.ticker, result.side.value)

        if key in self._resting:
            log.info(
                "execute.skip",
                ticker=result.ticker,
                side=result.side.value,
                reason="resting_order_already_active",
            )
            return None

        ask, bid = _ask_bid(book, result.side)
        if ask is None or bid is None:
            log.warning("maker.book_incomplete", ticker=result.ticker, side=result.side.value)
            return self._executor.execute(result, strategy=strategy)

        spread = round(ask - bid, 4)
        mode, taker_edge, maker_edge, maker_price = self._choose_mode(
            result, ask, bid, spread, strategy
        )

        log.info(
            "execution.mode_chosen",
            ticker=result.ticker,
            side=result.side.value,
            mode=mode or "none",
            spread=spread,
            net_edge_taker=round(taker_edge, 4),
            net_edge_maker=round(maker_edge, 4) if maker_edge is not None else None,
            execution_mode_cfg=self._cfg.execution_mode,
        )

        if mode is None:
            log.info(
                "execute.skip",
                ticker=result.ticker,
                side=result.side.value,
                reason="edge_gate",
                net_edge_taker=round(taker_edge, 4),
                net_edge_maker=round(maker_edge, 4) if maker_edge is not None else None,
            )
            return None

        if mode == "taker":
            taker_result = result.model_copy(
                update={"price_dollars": ask, "net_edge_dollars": taker_edge}
            )
            return self._executor.execute(taker_result, strategy=strategy)

        return self._place_maker_order(result, maker_price, maker_edge, now, strategy)

    # ── Mode selection ───────────────────────────────────────────────────────

    def _choose_mode(
        self,
        result: SizingResult,
        ask: float,
        bid: float,
        spread: float,
        strategy: str | None = None,
    ) -> tuple[str | None, float, float | None, float | None]:
        """
        Returns (chosen_mode, net_edge_taker, net_edge_maker, maker_price).

        chosen_mode is "taker", "maker", or None (neither clears the gate).
        net_edge_maker / maker_price are None when the spread is too tight
        for resting to be considered at all.
        """
        taker_edge = net_edge_taker(result.fair_p, ask, self._cfg)
        min_edge = self._cfg.min_net_edge
        if strategy is not None:
            try:
                from kbot.analytics.adverse_selection import effective_min_net_edge
                min_edge = effective_min_net_edge(strategy, self._cfg)
            except ImportError:
                pass

        maker_price: float | None = None
        maker_edge: float | None = None
        if spread >= _MIN_MAKER_SPREAD:
            maker_price = round(bid + _CENT, 2)
            maker_edge = net_edge_maker_for_market(result.fair_p, maker_price, result.ticker, self._cfg)

        mode_cfg = self._cfg.execution_mode

        if mode_cfg == "taker" or spread < _MIN_MAKER_SPREAD:
            chosen = "taker" if taker_edge >= min_edge else None
        elif mode_cfg == "maker":
            chosen = "maker" if maker_edge >= min_edge else None
        else:  # auto, spread qualifies for maker
            if maker_edge >= min_edge:
                chosen = "maker"
            elif taker_edge >= min_edge:
                chosen = "taker"
            else:
                chosen = None

        return chosen, taker_edge, maker_edge, maker_price

    # ── Maker order placement ────────────────────────────────────────────────

    def _place_maker_order(
        self,
        result: SizingResult,
        maker_price: float,
        maker_edge: float,
        now: float,
        strategy: str | None = None,
    ) -> OrderAck | None:
        # Same gauntlet as a taker order, gated on the maker-priced edge — but
        # position tracking is deferred until the order actually fills.
        # price=maker_price/is_maker=True lets check_gates apply RULE R1 (the
        # taker price floor) correctly for maker orders: exempt on standard
        # markets, still enforced on special-event flat-maker-fee markets.
        reason, contracts = self._executor.check_gates(
            result, net_edge_override=maker_edge, strategy=strategy,
            price=maker_price, is_maker=True,
        )
        if reason is not None:
            return None
        if contracts != result.contracts:
            result = result.model_copy(update={"contracts": contracts, "capped": True})

        ack = self._executor.client.place_order(
            ticker=result.ticker,
            side=result.side.value,
            count=result.contracts,
            price_dollars=maker_price,
        )

        order_key = ack.order_id or ack.client_order_id
        resting = RestingOrderBook(
            order_id=order_key,
            client_order_id=ack.client_order_id,
            ticker=result.ticker,
            side=result.side,
            price=maker_price,
            placed_at=now,
            contracts=result.contracts,
            strategy=strategy,
            driver=result.event_driver,
        )
        self._resting[(result.ticker, result.side.value)] = resting

        log.info(
            "maker.resting_order_placed",
            ticker=result.ticker,
            side=result.side.value,
            price=maker_price,
            order_id=resting.order_id,
            dry_run=ack.dry_run,
        )
        return ack

    # ── Resting-order lifecycle ──────────────────────────────────────────────

    def check_and_reprice(
        self,
        ticker: str,
        side: Side,
        book: OrderBook,
        *,
        now: float | None = None,
    ) -> None:
        """
        Manage one resting order against the latest orderbook snapshot: fill,
        timeout, or reprice. No-op if there is no resting order for ticker+side.

        Call this on every strategy-loop tick for tickers with a resting order.
        """
        now = time.time() if now is None else now
        key = (ticker, side.value)
        resting = self._resting.get(key)
        if resting is None:
            return

        ask, bid = _ask_bid(book, side)

        # ── DRY_RUN paper fill: only on trade-through, never on time alone ───
        if self._cfg.dry_run and ask is not None and ask <= resting.price:
            self._resting.pop(key, None)
            # A simulated maker fill is always the whole resting order, so the
            # exact conversion is contracts * UNITS_PER_CONTRACT. Real partial
            # fills arrive through Executor.execute, which carries the
            # exchange-reported units.
            resting_units = resting.contracts * UNITS_PER_CONTRACT
            self._executor.tracker.open_position(
                ticker, side,
                contracts_units=resting_units, price=resting.price,
                driver=resting.driver,
            )
            if self._executor.portfolio_risk is not None:
                self._executor.portfolio_risk.update_exposure(
                    ticker, resting.driver, resting_units, resting.price, action="open"
                )
            log.info(
                "maker.filled",
                ticker=ticker,
                side=side.value,
                price=resting.price,
                order_id=resting.order_id,
                simulated=True,
            )
            self._schedule_markout(resting)
            return

        # ── Timeout ───────────────────────────────────────────────────────────
        if now - resting.placed_at >= self._cfg.maker_timeout_seconds:
            self._cancel_resting(key, resting, reason="maker_timeout")
            return

        # ── Reprice ───────────────────────────────────────────────────────────
        if ask is None or bid is None:
            return

        ideal_price = round(bid + _CENT, 2)
        if ideal_price == resting.price:
            return  # still resting at the right level, nothing to do

        if resting.reprice_count >= self._cfg.max_reprices:
            cancelled = self._cancel_resting(key, resting, reason="maker_chase_abandoned")
            if cancelled:
                log.info(
                    "execute.skip",
                    ticker=ticker,
                    side=side.value,
                    reason="maker_chase_abandoned",
                    skip_reason="maker_chase_abandoned",
                    reprice_count=resting.reprice_count,
                )
            return

        cancelled = self._cancel_resting(key, resting, reason="reprice")
        if not cancelled:
            return  # ambiguous cancel — do not place a new order on top of an unknown one

        new_spread = round(ask - bid, 4)
        if new_spread < _MIN_MAKER_SPREAD:
            log.info(
                "maker.reprice_abandoned",
                ticker=ticker,
                side=side.value,
                reason="spread_tightened",
            )
            return

        new_ack = self._executor.client.place_order(
            ticker=ticker,
            side=side.value,
            count=resting.contracts,
            price_dollars=ideal_price,
        )
        new_key_id = new_ack.order_id or new_ack.client_order_id
        new_reprice_count = resting.reprice_count + 1
        self._resting[key] = RestingOrderBook(
            order_id=new_key_id,
            client_order_id=new_ack.client_order_id,
            ticker=ticker,
            side=side,
            price=ideal_price,
            placed_at=now,
            contracts=resting.contracts,
            reprice_count=new_reprice_count,
            strategy=resting.strategy,
            driver=resting.driver,
        )
        log.info(
            "maker.repriced",
            ticker=ticker,
            side=side.value,
            old_price=resting.price,
            new_price=ideal_price,
            reprice_count=new_reprice_count,
        )

    def _schedule_markout(self, resting: RestingOrderBook) -> None:
        """
        After a simulated maker fill, schedule a markout check at +60s/+300s.
        No-op unless the wrapped Executor has both telemetry and a
        book_provider configured. Never raises.
        """
        telemetry = self._executor.telemetry
        book_provider = self._executor.book_provider
        if telemetry is None or book_provider is None:
            return
        try:
            from kbot.analytics.adverse_selection import schedule_markout
            schedule_markout(
                telemetry=telemetry,
                fill_event_id=resting.client_order_id,
                ticker=resting.ticker,
                strategy=resting.strategy or "unknown",
                side=resting.side,
                fill_price=resting.price,
                book_provider=book_provider,
                cfg=self._cfg,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("adverse_selection.schedule_failed", error=str(exc), ticker=resting.ticker)

    def _cancel_resting(
        self,
        key: tuple[str, str],
        resting: RestingOrderBook,
        *,
        reason: str,
    ) -> bool:
        """
        Cancel a resting order. Never retried. Returns True if the cancel call
        completed, False if it raised (ambiguous outcome — logged for manual
        review, order dropped from automated management either way).
        """
        try:
            self._executor.client.cancel_order(resting.order_id)
        except Exception as exc:  # noqa: BLE001 — surfaced via structured log, not swallowed
            log.error(
                "maker.cancel_ambiguous",
                order_id=resting.order_id,
                ticker=resting.ticker,
                side=resting.side.value,
                reason=reason,
                error=str(exc),
                action_required="manual_check",
            )
            self._resting.pop(key, None)
            return False

        self._resting.pop(key, None)
        log.info(
            "maker.cancelled",
            order_id=resting.order_id,
            ticker=resting.ticker,
            side=resting.side.value,
            reason=reason,
        )
        return True
