"""
Portfolio-level exposure caps by event_driver — the correlation guard.

Individual per-position and per-order caps (risk.py) don't stop the bot from
stacking correlated risk: five different BTC-hourly markets all move on the
same underlying spot price, so five "independent" positions can be one bad
tick away from a single correlated loss. Every signal is tagged with an
event_driver (crypto_price, us_macro, weather_{city}, sports_game:{game_id},
other); PortfolioRisk caps total open exposure per driver bucket, not just
per ticker.

This is a soft cap, not a kill-switch: a driver bucket that's full clamps
new contracts (or skips the signal with skip_reason="driver_cap") — it does
not touch positions already open and does not affect other drivers.

DRIVER_IMPACT_MAP ties research advisory flags to the driver buckets they
put at correlated risk (e.g. an imminent high-impact macro event moves both
us_macro and crypto_price markets together). While a mapped flag is set,
those drivers' caps are halved. This mirrors execution.py's advisory-flag
gate but is scoped to specific drivers rather than a global trading halt.
"""

from __future__ import annotations

import threading

import structlog

from kbot.quantity import UNITS_PER_CONTRACT

from kbot.config import Settings, settings as _default_settings
from kbot.models.types import SizingResult

log = structlog.get_logger(__name__)

# Flag name -> event_driver buckets whose cap is halved while that flag is set.
# Extend this as research/flags.py grows more granular event-type flags
# (e.g. a future "weather_event_nyc" flag would map to ["weather_nyc"]).
DRIVER_IMPACT_MAP: dict[str, list[str]] = {
    "high_impact_event_imminent": ["us_macro", "crypto_price"],
}

_HALVING_FACTOR = 0.5


def _flagged_drivers() -> set[str]:
    """Union of driver buckets currently halved by an active research flag."""
    try:
        from kbot.research.flags import get_flag
    except ImportError:
        return set()

    affected: set[str] = set()
    for flag_name, drivers in DRIVER_IMPACT_MAP.items():
        if get_flag(flag_name):
            affected.update(drivers)
    return affected


class PortfolioRisk:
    """
    Tracks open dollar exposure per event_driver and enforces a per-driver
    cap as a fraction of bankroll (cfg.driver_cap_pct, default 0.25).

    Thread-safe: shared between the asyncio paper-trading loop and any
    background threads (e.g. MakerExecutor's reprice/cancel path).
    """

    def __init__(self, cfg: Settings | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._lock = threading.Lock()
        self._exposure_by_driver: dict[str, float] = {}
        # 2026-07: per-ticker committed dollars, so a settled/closed ticker can
        # release EXACTLY what it added to its driver bucket (see
        # release_exposure) regardless of later price moves. Without this the
        # driver bucket only ever grew (opens added, nothing released on
        # expiry) and eventually blocked all trades. _ticker_driver remembers
        # which bucket each ticker's dollars belong to.
        self._exposure_by_ticker: dict[str, float] = {}
        self._ticker_driver: dict[str, str] = {}

    # ── Cap enforcement ───────────────────────────────────────────────────────

    def apply_driver_cap(
        self,
        signal: SizingResult,
        current_exposure_by_driver: dict[str, float],
        bankroll: float,
    ) -> int:
        """
        Return the max contracts allowed for `signal` under its event_driver's
        exposure cap.

        `current_exposure_by_driver` is taken as an explicit argument (rather
        than always reading self's own state) so callers can check a signal
        against a specific snapshot — in production that snapshot is
        self.exposure_summary(). Halves the cap for drivers currently listed
        in DRIVER_IMPACT_MAP for an active research advisory flag.

        Returns 0 (and logs skip_reason="driver_cap") if the bucket is
        already full. Otherwise clamps signal.contracts to what fits in the
        remaining bucket capacity.
        """
        if signal.contracts <= 0 or signal.price_dollars <= 0 or bankroll <= 0:
            return max(signal.contracts, 0)

        driver = signal.event_driver
        cap_pct = self._cfg.driver_cap_pct
        halved = driver in _flagged_drivers()
        if halved:
            cap_pct *= _HALVING_FACTOR

        max_driver_dollars = cap_pct * bankroll
        existing = current_exposure_by_driver.get(driver, 0.0)
        budget = max(0.0, max_driver_dollars - existing)
        cap_contracts = int(budget / signal.price_dollars)

        allowed = max(0, min(signal.contracts, cap_contracts))

        if allowed == 0:
            log.info(
                "risk.driver_cap",
                ticker=signal.ticker,
                driver=driver,
                requested=signal.contracts,
                existing_exposure=round(existing, 2),
                max_driver_dollars=round(max_driver_dollars, 2),
                halved=halved,
                skip_reason="driver_cap",
            )
        elif allowed < signal.contracts:
            log.info(
                "risk.driver_cap_clamped",
                ticker=signal.ticker,
                driver=driver,
                requested=signal.contracts,
                allowed=allowed,
                existing_exposure=round(existing, 2),
                max_driver_dollars=round(max_driver_dollars, 2),
                halved=halved,
            )

        return allowed

    # ── Exposure tracking ─────────────────────────────────────────────────────

    def update_exposure(
        self,
        ticker: str,
        driver: str,
        contracts_units: int,
        price: float,
        action: str,
    ) -> None:
        """
        Adjust the driver bucket after a fill or a close.

        `contracts_units` is in integer hundredths of a contract (kbot.quantity);
        the dollar figure is units/100 * price, so a fractional fill contributes
        its true exposure instead of a rounded one.

        action="open"  adds that dollar amount to the driver bucket
                       and records it against `ticker`.
        action="close" releases `ticker`'s RECORDED exposure via
                       release_exposure() -- the passed contracts/price/driver
                       are ignored for a close, so the bucket returns to
                       exactly its pre-open value even if price has since
                       moved. Use release_exposure(ticker) directly for
                       expiry; this close branch exists for callers that
                       already speak the open/close API.
        """
        if action not in ("open", "close"):
            raise ValueError(f"action must be 'open' or 'close', got {action!r}")

        if action == "close":
            self.release_exposure(ticker)  # uses the recorded amount, not a recompute
            return

        dollars = (contracts_units / UNITS_PER_CONTRACT) * price
        with self._lock:
            self._exposure_by_driver[driver] = self._exposure_by_driver.get(driver, 0.0) + dollars
            self._exposure_by_ticker[ticker] = self._exposure_by_ticker.get(ticker, 0.0) + dollars
            self._ticker_driver[ticker] = driver

        log.debug(
            "risk.driver_exposure_updated",
            ticker=ticker,
            driver=driver,
            action="open",
            dollars=round(dollars, 2),
        )

    def release_exposure(self, ticker: str) -> float:
        """
        Release ALL of `ticker`'s recorded exposure from its driver bucket and
        forget the ticker (called on settlement/expiry or an explicit close).

        Uses the amount RECORDED at open time (self._exposure_by_ticker), not
        a recomputation from current price -- so the bucket returns to exactly
        what it was before this ticker opened. Floors the bucket at 0 (never
        negative). A no-op returning 0.0 for a ticker with no recorded
        exposure (already released, or never opened). Returns the dollars
        released.
        """
        with self._lock:
            amount = self._exposure_by_ticker.pop(ticker, 0.0)
            driver = self._ticker_driver.pop(ticker, None)
            if driver is not None:
                current = self._exposure_by_driver.get(driver, 0.0)
                self._exposure_by_driver[driver] = max(0.0, current - amount)
        if amount:
            log.debug(
                "risk.driver_exposure_released",
                ticker=ticker,
                driver=driver,
                dollars=round(amount, 2),
            )
        return amount

    def exposure_summary(self) -> dict[str, float]:
        """Current open dollar exposure per event_driver."""
        with self._lock:
            return dict(self._exposure_by_driver)

    def ticker_exposure_summary(self) -> dict[str, float]:
        """Current open dollar exposure per ticker (what release_exposure would free)."""
        with self._lock:
            return dict(self._exposure_by_ticker)

    def is_driver_halved(self, driver: str) -> bool:
        """True if `driver`'s cap is currently halved by an active research flag."""
        return driver in _flagged_drivers()
