"""
Order placement — the single point where intents become real orders.

Safety gauntlet enforced here (not bypassed by any caller):
  1. Kill-switch check (daily-loss limit).
  2. Advisory flag check (research agent stand-down).
  3. Degenerate result (zero contracts).
  3.5. Taker price floor (RULE R1) — sub-15c contracts are a fee-death zone.
       Applies to maker orders too on special-event fee markets.
  4. Edge gate (net_edge >= min_net_edge).
  4.5. Portfolio driver cap — clamps (or zeroes) contracts against the
       signal's event_driver exposure bucket. Unlike the other gates, this
       one can shrink the order instead of only pass/block it.
  5. Position guard — refuse to re-enter a ticker+side already held.
  6. LIVE mode requires KALSHI_ENV=prod + explicit live_confirmed=True flag
     passed by the CLI after the interactive confirmation prompt.

Order writes are NEVER retried — a duplicate submission can double a position.
"""

from __future__ import annotations

from typing import Callable

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.edge import passes_price_floor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.models.types import OrderBook, Side, SizingResult
from kbot.quantity import UNITS_PER_CONTRACT
from kbot.position_tracker import PositionTracker
from kbot.risk import kill_switch_tripped
from kbot.risk_portfolio import PortfolioRisk
from kbot.telemetry import Telemetry

log = structlog.get_logger(__name__)


class Executor:
    """
    Converts SizingResults into placed orders (or dry-run logs).

    Parameters
    ----------
    client:
        KalshiClient instance to use for order placement.
    cfg:
        Settings (defaults to module singleton).
    live_confirmed:
        Must be True for live orders.  Set by CLI after the typed
        "type CONFIRM LIVE" prompt — never set programmatically.
    telemetry, book_provider:
        Optional. When both are supplied, a successful fill schedules an
        adverse-selection markout check at +60s/+300s (see
        kbot.analytics.adverse_selection). Either left None (the default)
        disables markout scheduling entirely — existing callers are
        unaffected.
    portfolio_risk, bankroll_provider:
        Optional. When both are supplied, the driver-cap gate clamps
        contracts against the signal's event_driver exposure bucket (see
        kbot.risk_portfolio). portfolio_risk alone (no bankroll_provider)
        still tracks exposure via update_exposure() but the cap itself is
        skipped, since it needs bankroll to compute a dollar budget. Either
        left None (the default) disables driver-cap enforcement entirely —
        existing callers are unaffected.
    """

    def __init__(
        self,
        client: KalshiClient,
        cfg: Settings | None = None,
        *,
        live_confirmed: bool = False,
        tracker: PositionTracker | None = None,
        telemetry: Telemetry | None = None,
        book_provider: Callable[[str], OrderBook | None] | None = None,
        portfolio_risk: PortfolioRisk | None = None,
        bankroll_provider: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._cfg = cfg or _default_settings
        self._live_confirmed = live_confirmed
        self._tracker = tracker if tracker is not None else PositionTracker()
        self._telemetry = telemetry
        self._book_provider = book_provider
        self._portfolio_risk = portfolio_risk
        self._bankroll_provider = bankroll_provider

    # ── Accessors ────────────────────────────────────────────────────────────
    # Read-only access for callers that need to reuse the safety gauntlet
    # without going through execute() (e.g. MakerExecutor).

    @property
    def client(self) -> KalshiClient:
        return self._client

    @property
    def cfg(self) -> Settings:
        return self._cfg

    @property
    def tracker(self) -> PositionTracker:
        return self._tracker

    @property
    def telemetry(self) -> Telemetry | None:
        return self._telemetry

    @property
    def book_provider(self) -> Callable[[str], OrderBook | None] | None:
        return self._book_provider

    @property
    def portfolio_risk(self) -> PortfolioRisk | None:
        return self._portfolio_risk

    @property
    def bankroll_provider(self) -> Callable[[], float] | None:
        return self._bankroll_provider

    def check_gates(
        self,
        result: SizingResult,
        *,
        net_edge_override: float | None = None,
        min_net_edge_override: float | None = None,
        strategy: str | None = None,
        price: float | None = None,
        is_maker: bool = False,
        is_single_binary: bool = False,
    ) -> tuple[str | None, int]:
        """
        Run every safety gate except order placement and position tracking.

        `net_edge_override` lets a caller (e.g. MakerExecutor, which prices
        maker and taker legs differently) check the gate against an edge
        value other than result.net_edge_dollars.

        `min_net_edge_override` lets a caller LOWER (or otherwise change)
        the required threshold itself, instead of the value being compared
        against it -- used by kbot.scanners.post_resolution to apply its
        discounted edge gate during a post-resolution window. Safety wins
        over the discount: if `strategy`'s adverse_selection_detected flag
        is currently set, the (raised) adverse-selection threshold is used
        instead of the override, never the other way around.

        `strategy`, if given, raises the effective min_net_edge threshold
        when that strategy's adverse_selection_detected flag is set (see
        kbot.analytics.adverse_selection.effective_min_net_edge). Trading
        is NOT halted by that flag — only the edge bar is raised.

        `price`/`is_maker` feed the RULE R1 taker price floor. `price`
        defaults to result.price_dollars (correct for the taker path, where
        callers already set price_dollars to the actual execution price);
        MakerExecutor passes the real maker price explicitly, since it can
        differ from result.price_dollars.

        Returns (reason, effective_contracts). reason is None if every gate
        passes, otherwise the skip/block reason (effective_contracts is 0 in
        that case). effective_contracts may be smaller than result.contracts
        even when reason is None — the driver-cap gate can shrink an order
        instead of only pass/block it.
        """
        # ── 1. Kill-switch ────────────────────────────────────────────────────
        if kill_switch_tripped():
            log.warning("execute.blocked", reason="kill_switch", ticker=result.ticker)
            return "kill_switch", 0

        # ── 2. Advisory flags (research agent stand-down) ─────────────────────
        # Deferred import so loading execution.py never drags in the research
        # package; also gracefully skipped if research is not installed.
        try:
            from kbot.research.flags import active_flags, any_advisory_flag_set
            if any_advisory_flag_set():
                log.warning(
                    "execute.blocked",
                    reason="advisory_flag",
                    flags=active_flags(),
                    ticker=result.ticker,
                )
                return "advisory_flag", 0
        except ImportError:
            pass

        # ── 3. Degenerate result ──────────────────────────────────────────────
        if result.contracts <= 0:
            reason = result.skip_reason or "zero_contracts"
            log.debug("execute.skip", ticker=result.ticker, reason=reason)
            return reason, 0

        # ── 3.5 Taker price floor (RULE R1) ────────────────────────────────────
        exec_price = result.price_dollars if price is None else price
        if not passes_price_floor(exec_price, result.ticker, self._cfg, is_maker=is_maker):
            log.info(
                "execute.skip",
                ticker=result.ticker,
                price=exec_price,
                floor=self._cfg.taker_price_floor,
                is_maker=is_maker,
                reason="taker_price_floor",
            )
            return "taker_price_floor", 0

        # ── 4. Edge gate ──────────────────────────────────────────────────────
        edge = result.net_edge_dollars if net_edge_override is None else net_edge_override
        min_edge = self._cfg.min_net_edge if min_net_edge_override is None else min_net_edge_override
        if strategy is not None:
            try:
                from kbot.analytics.adverse_selection import is_flagged
                if is_flagged(strategy):
                    # An active adverse-selection flag must never be
                    # bypassed by a discount (post-resolution or otherwise)
                    # -- safety wins, use the raised threshold instead.
                    from kbot.analytics.adverse_selection import effective_min_net_edge
                    min_edge = effective_min_net_edge(strategy, self._cfg)
            except ImportError:
                pass
        if not (result.contracts > 0 and edge >= min_edge):
            log.info(
                "execute.skip",
                ticker=result.ticker,
                net_edge=edge,
                min_net_edge=min_edge,
                reason="edge_gate",
            )
            return "edge_gate", 0

        # ── 4.5 Portfolio driver cap ────────────────────────────────────────────
        # Only enforced when both portfolio_risk and bankroll_provider are
        # configured; otherwise this is a no-op (existing callers unaffected).
        effective_contracts = result.contracts
        if self._portfolio_risk is not None and self._bankroll_provider is not None:
            bankroll = self._bankroll_provider()
            effective_contracts = self._portfolio_risk.apply_driver_cap(
                result, self._portfolio_risk.exposure_summary(), bankroll
            )
            if effective_contracts <= 0:
                return "driver_cap", 0

        # ── 5. Position guard ─────────────────────────────────────────────────
        # DO NOT remove this check before going live — without it the bot will
        # stack unlimited contracts on the same position every 5 seconds.
        if self._tracker.is_held(result.ticker, result.side):
            log.warning(
                "execute.blocked",
                reason="position_already_held",
                ticker=result.ticker,
                side=result.side.value,
            )
            return "position_already_held", 0

        # ── 5.5 Opposite-side guard for single-binary markets ─────────────────
        # A single-binary market (e.g. KXBTC15M: one "up in next 15 min?"
        # YES/NO per event) has no reason to be held on both sides -- doing so
        # pays spread + fees on both legs for a pointless self-hedge. Block
        # the opposite side if we already hold this ticker's other side. NOT
        # applied to the hourly B/T strike ladder (is_single_binary False
        # there), where YES on one strike and NO on another are legitimately
        # different contracts -- a real spread trade.
        if is_single_binary:
            opposite = Side.NO if result.side == Side.YES else Side.YES
            if self._tracker.is_held(result.ticker, opposite):
                log.warning(
                    "execute.blocked",
                    reason="opposite_side_held_single_binary",
                    ticker=result.ticker,
                    side=result.side.value,
                    held_side=opposite.value,
                )
                return "opposite_side_held_single_binary", 0

        # ── 6. Live-mode gauntlet ─────────────────────────────────────────────
        if not self._cfg.dry_run:
            if self._cfg.kalshi_env != "prod":
                log.error(
                    "execute.blocked",
                    reason="live_order_on_non_prod_env",
                    env=self._cfg.kalshi_env,
                )
                return "live_order_on_non_prod_env", 0
            if not self._live_confirmed:
                log.error(
                    "execute.blocked",
                    reason="live_confirmation_not_given",
                )
                return "live_confirmation_not_given", 0

        return None, effective_contracts

    def execute(
        self,
        result: SizingResult,
        *,
        price_override: float | None = None,
        strategy: str | None = None,
        min_net_edge_override: float | None = None,
        is_single_binary: bool = False,
    ) -> OrderAck | None:
        """
        Attempt to place the order described by `result`.

        `price_override` places at a different price than result.price_dollars
        while still gating on result.net_edge_dollars (used by taker fallback
        in MakerExecutor, where the sizing price and the live ask can differ).

        `strategy`, if given, is used both for the adverse-selection edge
        threshold (see check_gates) and to tag the markout scheduled after a
        successful fill.

        `min_net_edge_override`, if given, lowers (or otherwise changes) the
        edge-gate threshold itself -- see check_gates for the precedence
        rule against an active adverse-selection flag.

        Returns the OrderAck on success (or dry-run simulation), None on any
        skip. An order that comes back with zero fills counts as a skip: it is
        sent immediate_or_cancel, so a no-fill is final and no position was
        opened. A PARTIAL fill is a success — the position is recorded at the
        filled quantity and the achieved average price.
        """
        price = result.price_dollars if price_override is None else price_override

        reason, contracts = self.check_gates(
            result, strategy=strategy, price=price, min_net_edge_override=min_net_edge_override,
            is_single_binary=is_single_binary,
        )
        if reason is not None:
            return None
        if contracts != result.contracts:
            result = result.model_copy(update={"contracts": contracts, "capped": True})

        # ── Place order (KalshiClient handles DRY_RUN internally) ────────────
        log.info(
            "execute.placing",
            ticker=result.ticker,
            side=result.side,
            contracts=result.contracts,
            price=price,
            dry_run=self._cfg.dry_run,
        )

        ack = self._client.place_order(
            ticker=result.ticker,
            side=result.side.value,
            count=result.contracts,
            price_dollars=price,
        )

        # ── Record what actually FILLED, not what was requested ──────────────
        # Orders go out immediate_or_cancel, so the ack is final: whatever did
        # not fill was cancelled and will never fill later. An unfilled order
        # opened no position, so recording one would block the ticker behind
        # the position guard and invent a trade for P&L to settle.
        if ack.fill_count_units <= 0:
            log.info(
                "execute.unfilled",
                ticker=result.ticker,
                side=result.side.value,
                requested_contracts=result.contracts,
                price=price,
                client_order_id=ack.client_order_id,
                order_id=ack.order_id,
                reason="no_fill",
            )
            return None

        filled_units = ack.fill_count_units
        # An IOC never fills worse than its limit; place_order falls back to
        # the limit price when the exchange reports no average.
        fill_price = price if ack.average_fill_price is None else ack.average_fill_price

        if filled_units != result.contracts * UNITS_PER_CONTRACT:
            log.warning(
                "execute.partial_fill",
                ticker=result.ticker,
                side=result.side.value,
                requested_contracts=result.contracts,
                filled_units=filled_units,
                remaining_count_units=ack.remaining_count_units,
            )
            # SizingResult.contracts is a whole-contract count (it is what an
            # order's `count` was). Carry the exact filled size alongside it in
            # units rather than rounding it into that field.
            result = result.model_copy(update={
                "contracts": int(filled_units // UNITS_PER_CONTRACT),
                "filled_units": filled_units,
            })
        else:
            result = result.model_copy(update={"filled_units": filled_units})

        # Record the position so subsequent signals for the same ticker+side
        # are blocked by the guard above until the market expires. The
        # exposure fields are persisted too, so PortfolioRisk can be rebuilt
        # from the positions DB on restart (see Runner startup reconstruction).
        #
        # is_live comes from the ACK -- what place_order actually did -- not
        # from self._cfg.dry_run. This is the one moment the real-vs-paper
        # question has a definite answer; everything downstream (settlement,
        # evaluate, the Live P&L tab) reads this persisted stamp rather than
        # re-deriving it from config or gate level, both of which can change
        # after the fact and would retroactively relabel real trades.
        #
        # `price` here is the ACHIEVED average fill price (in this side's own
        # denomination -- KalshiClient converts NO fills back from the YES
        # quote), which is what outcomes.compute_outcome charges as the cost
        # basis and what the Live P&L tab displays as fill_price.
        self._tracker.open_position(
            result.ticker, result.side,
            contracts_units=filled_units, price=fill_price,
            driver=result.event_driver,
            is_live=not ack.dry_run,
        )

        if self._portfolio_risk is not None:
            self._portfolio_risk.update_exposure(
                result.ticker, result.event_driver, filled_units, fill_price,
                action="open",
            )

        log.info(
            "execute.done",
            client_order_id=ack.client_order_id,
            order_id=ack.order_id,
            dry_run=ack.dry_run,
            contracts=filled_units / UNITS_PER_CONTRACT,
            filled_units=filled_units,
            fill_price=fill_price,
        )

        self._schedule_markout(ack, result, fill_price, strategy)

        return ack

    def close_position(
        self,
        ticker: str,
        side: Side,
        *,
        driver: str = "other",
        contracts: int = 0,
        price: float = 0.0,
    ) -> None:
        """
        Record that a position was closed (market resolved/expired, or
        manually closed) — clears the position guard and, if portfolio_risk
        is configured, releases the ticker's RECORDED driver exposure via
        release_exposure (the recorded amount, not a recompute from the
        passed contracts/price, so the bucket returns exactly to pre-open).
        The driver/contracts/price params are kept for backward compatibility
        but are no longer needed for the exposure release.
        """
        self._tracker.close_position(ticker, side)
        if self._portfolio_risk is not None:
            self._portfolio_risk.release_exposure(ticker)

    # ── Adverse-selection wiring ─────────────────────────────────────────────

    def _schedule_markout(
        self,
        ack: OrderAck,
        result: SizingResult,
        fill_price: float,
        strategy: str | None,
    ) -> None:
        """
        After a successful fill, schedule a markout check at +60s/+300s.
        No-op unless both telemetry and book_provider were supplied at
        construction. Never raises — a scheduling failure must never take
        down the trading loop.
        """
        if self._telemetry is None or self._book_provider is None:
            return
        try:
            from kbot.analytics.adverse_selection import mid_from_book, schedule_markout
            # Snapshot the mid NOW so the markout measures post-fill mid
            # drift, not drift-plus-half-spread: a taker fill at the ask
            # starts ~half a spread above mid by construction, which biased
            # every markout negative and tripped the adverse-selection flag
            # in low-vol regimes where no favorable drift masked it.
            try:
                mid_at_fill = mid_from_book(self._book_provider(result.ticker))
            except Exception:  # noqa: BLE001 — snapshot failure must not block scheduling
                mid_at_fill = None
            schedule_markout(
                telemetry=self._telemetry,
                fill_event_id=ack.client_order_id,
                ticker=result.ticker,
                strategy=strategy or "unknown",
                side=result.side,
                fill_price=fill_price,
                mid_at_fill=mid_at_fill,
                book_provider=self._book_provider,
                cfg=self._cfg,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("adverse_selection.schedule_failed", error=str(exc), ticker=result.ticker)
