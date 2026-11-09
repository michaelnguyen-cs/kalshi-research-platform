"""
Kelly sizing, position caps, and daily-loss kill-switch.

The kill-switch is a module-level flag that is set (and never cleared) when
the daily loss limit is breached.  Once tripped it prevents any new order
from reaching the execution layer for the rest of the process lifetime.
A process restart is required to re-enable trading — intentional.
"""

from __future__ import annotations

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.models.types import SizingResult

log = structlog.get_logger(__name__)

# ── Kill-switch ───────────────────────────────────────────────────────────────

_KILL_SWITCH_TRIPPED = False


def kill_switch_tripped() -> bool:
    return _KILL_SWITCH_TRIPPED


def check_daily_loss(
    current_balance: float,
    start_of_day_balance: float,
    cfg: Settings | None = None,
) -> None:
    """
    Trip the kill-switch if today's loss exceeds the limit.

    Irreversible for the lifetime of the process.
    """
    global _KILL_SWITCH_TRIPPED
    cfg = cfg or _default_settings
    if start_of_day_balance <= 0:
        return
    loss_pct = (start_of_day_balance - current_balance) / start_of_day_balance
    if loss_pct >= cfg.daily_loss_limit_pct:
        _KILL_SWITCH_TRIPPED = True
        log.critical(
            "kill_switch.tripped",
            loss_pct=round(loss_pct, 4),
            limit_pct=cfg.daily_loss_limit_pct,
            current_balance=current_balance,
            start_balance=start_of_day_balance,
        )


# ── Exposure accounting ───────────────────────────────────────────────────────

class ExposureTracker:
    """
    Tracks dollar exposure across all open positions.

    Dollar exposure = contracts * price_per_contract.
    """

    def __init__(self) -> None:
        self._exposures: dict[str, float] = {}   # ticker → dollar exposure

    def update(self, ticker: str, contracts: int, price_dollars: float) -> None:
        self._exposures[ticker] = contracts * price_dollars

    def close(self, ticker: str) -> None:
        self._exposures.pop(ticker, None)

    def total_exposure(self) -> float:
        return sum(self._exposures.values())

    def position_exposure(self, ticker: str) -> float:
        return self._exposures.get(ticker, 0.0)


# ── Cap enforcement ───────────────────────────────────────────────────────────

def apply_caps(
    result: SizingResult,
    bankroll: float,
    tracker: ExposureTracker,
    cfg: Settings | None = None,
) -> SizingResult:
    """
    Reduce result.contracts to respect position and portfolio exposure caps.

    Returns a new SizingResult (originals are immutable pydantic models).
    """
    cfg = cfg or _default_settings

    if result.contracts <= 0:
        return result

    price = result.price_dollars
    if price <= 0:
        return result

    # How many contracts can we add before breaching the per-position cap?
    max_pos_dollars = cfg.max_position_pct * bankroll
    existing_pos = tracker.position_exposure(result.ticker)
    pos_budget = max(0.0, max_pos_dollars - existing_pos)
    pos_cap_contracts = int(pos_budget / price)

    # How many before breaching total-exposure cap?
    max_total_dollars = cfg.max_total_exposure_pct * bankroll
    existing_total = tracker.total_exposure()
    total_budget = max(0.0, max_total_dollars - existing_total)
    total_cap_contracts = int(total_budget / price)

    # Hard per-order ceiling.
    order_cap = cfg.max_contracts_per_order

    allowed = min(
        result.contracts,
        pos_cap_contracts,
        total_cap_contracts,
        order_cap,
    )

    capped = allowed < result.contracts
    if capped:
        log.info(
            "risk.capped",
            ticker=result.ticker,
            requested=result.contracts,
            allowed=allowed,
            pos_cap=pos_cap_contracts,
            total_cap=total_cap_contracts,
            order_cap=order_cap,
        )

    return result.model_copy(update={"contracts": allowed, "capped": capped})
