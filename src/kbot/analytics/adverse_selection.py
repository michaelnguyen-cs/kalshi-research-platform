"""
Adverse-selection monitor — did the market move against us right after a fill?

markout_Ns = (mid_price_at_fill_plus_N - reference) * side_sign
  side_sign = +1 for YES buys, -1 for NO buys
  reference = mid_at_fill (the book mid snapshotted at fill time) when
              available, else fill_price (legacy rows / no snapshot).

Positive markout means the price moved our way after we bought (we got a
good price); negative means we got picked off (someone with better
information traded through us right before the market moved further).

The reference is the MID at fill, not the fill price itself (2026-07 fix):
a taker fill at the ask sits ~half a spread above mid by construction, so a
fill-price reference gave every markout a -half_spread baseline even when
the book never moved. In low-vol regimes there is no favorable drift to
offset that bias, and the flag tripped on spread cost alone -- which is
already charged separately in the edge model (half_spread gate + assumed
slippage), never twice here.

Per-strategy state lives in this module only (a leaf, like research/flags.py)
and is intentionally NOT wired into execution.py's kill-switch-style gate:
tripping adverse_selection_detected does not stop trading. It raises that
strategy's effective min_net_edge via effective_min_net_edge() so weaker
signals are filtered out while the strategy keeps running.

All recording functions are best-effort: any failure (bad book snapshot,
SQLite error, etc.) is logged and swallowed so a bug here can never take
down the trading loop. This mirrors telemetry.py's own never-raise contract
for writes.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.models.types import OrderBook, Side
from kbot.telemetry import Telemetry

log = structlog.get_logger(__name__)

# Hysteresis band: trip below _TRIP_THRESHOLD, clear above _CLEAR_THRESHOLD.
# The gap prevents flapping when the rolling mean sits right at the edge.
_TRIP_THRESHOLD = -0.01
_CLEAR_THRESHOLD = -0.005

# Rows fetched per `window` markouts wanted. Fills with no +60s book snapshot
# persist a NULL markout_60s, so the newest `window` ROWS can hold fewer than
# `window` usable values -- over-fetch, then take the first `window` non-NULL.
_OVERFETCH_MULTIPLIER = 3

_LOCK = threading.Lock()
_STRATEGY_FLAGS: dict[str, bool] = {}


# ── Markout math ────────────────────────────────────────────────────────────────

def markout(fill_price: float, mid_price: float, side: Side) -> float:
    """(mid - fill) * side_sign. Positive = price moved our way after the fill."""
    side_sign = 1.0 if side == Side.YES else -1.0
    return (mid_price - fill_price) * side_sign


def mid_from_book(book: OrderBook | None) -> float | None:
    """Convenience wrapper: mid price from a book snapshot, or None if unavailable."""
    if book is None:
        return None
    return book.mid_price()


# ── Recording (called at fill+60s and fill+300s) ─────────────────────────────────

def record_markout_60s(
    telemetry: Telemetry,
    *,
    fill_event_id: str,
    ticker: str,
    strategy: str,
    side: Side,
    fill_price: float,
    mid_60s: float | None,
    mid_at_fill: float | None = None,
    cfg: Settings | None = None,
) -> None:
    """
    Persist the +60s leg of a markout row and re-evaluate the strategy's
    adverse-selection flag. Never raises.

    `mid_at_fill`, when available, is the markout reference (post-fill mid
    drift); fill_price is the fallback reference (legacy behavior, biased by
    ~-half_spread on taker fills -- see module docstring).
    """
    cfg = cfg or _default_settings
    try:
        reference = mid_at_fill if mid_at_fill is not None else fill_price
        markout_60s = markout(reference, mid_60s, side) if mid_60s is not None else None
        telemetry.log_markout({
            "fill_event_id": fill_event_id,
            "ticker": ticker,
            "strategy": strategy,
            "side": side.value,
            "fill_price": fill_price,
            "mid_at_fill": mid_at_fill,
            "mid_60s": mid_60s,
            "markout_60s": markout_60s,
        })
        if markout_60s is not None:
            evaluate_flag(telemetry, strategy, cfg)
    except Exception as exc:  # noqa: BLE001 — must never propagate into the trading loop
        log.error(
            "adverse_selection.record_60s_failed",
            error=str(exc),
            fill_event_id=fill_event_id,
            ticker=ticker,
        )


def record_markout_300s(
    telemetry: Telemetry,
    *,
    fill_event_id: str,
    side: Side,
    fill_price: float,
    mid_300s: float | None,
    mid_at_fill: float | None = None,
) -> None:
    """
    Fill in the +300s leg of an existing markout row. Never raises.
    Same reference rule as record_markout_60s: mid_at_fill when available,
    fill_price as the legacy fallback.
    """
    try:
        reference = mid_at_fill if mid_at_fill is not None else fill_price
        markout_300s = markout(reference, mid_300s, side) if mid_300s is not None else None
        telemetry.update_markout_300s(fill_event_id, mid_300s, markout_300s)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "adverse_selection.record_300s_failed",
            error=str(exc),
            fill_event_id=fill_event_id,
        )


# ── Scheduling (fire-and-forget background thread) ───────────────────────────────

def schedule_markout(
    *,
    telemetry: Telemetry,
    fill_event_id: str,
    ticker: str,
    strategy: str,
    side: Side,
    fill_price: float,
    book_provider: Callable[[str], OrderBook | None],
    mid_at_fill: float | None = None,
    cfg: Settings | None = None,
    delays: tuple[float, float] = (60.0, 300.0),
) -> None:
    """
    Spawn a daemon thread that records markouts at fill+60s and fill+300s
    using `book_provider(ticker)` for the live orderbook snapshot.

    `mid_at_fill` is the book mid snapshotted by the caller at fill time --
    the markout reference when given (post-fill mid drift); when None the
    markouts fall back to fill_price as the reference (legacy behavior).

    Fire-and-forget: never raises into the caller. `book_provider` itself may
    raise or return None (e.g. the market has no book yet) — both are treated
    as "no data" and logged, not propagated.
    """
    cfg = cfg or _default_settings
    try:
        thread = threading.Thread(
            target=_markout_worker,
            args=(telemetry, fill_event_id, ticker, strategy, side, fill_price, mid_at_fill,
                  book_provider, cfg, delays),
            daemon=True,
            name=f"markout-{fill_event_id}",
        )
        thread.start()
    except Exception as exc:  # noqa: BLE001
        log.error("adverse_selection.schedule_failed", error=str(exc), fill_event_id=fill_event_id)


def _safe_mid(book_provider: Callable[[str], OrderBook | None], ticker: str) -> float | None:
    try:
        return mid_from_book(book_provider(ticker))
    except Exception as exc:  # noqa: BLE001
        log.error("adverse_selection.book_provider_failed", error=str(exc), ticker=ticker)
        return None


def _markout_worker(
    telemetry: Telemetry,
    fill_event_id: str,
    ticker: str,
    strategy: str,
    side: Side,
    fill_price: float,
    mid_at_fill: float | None,
    book_provider: Callable[[str], OrderBook | None],
    cfg: Settings,
    delays: tuple[float, float],
) -> None:
    time.sleep(delays[0])
    mid_60s = _safe_mid(book_provider, ticker)
    record_markout_60s(
        telemetry,
        fill_event_id=fill_event_id,
        ticker=ticker,
        strategy=strategy,
        side=side,
        fill_price=fill_price,
        mid_60s=mid_60s,
        mid_at_fill=mid_at_fill,
        cfg=cfg,
    )

    time.sleep(max(0.0, delays[1] - delays[0]))
    mid_300s = _safe_mid(book_provider, ticker)
    record_markout_300s(
        telemetry,
        fill_event_id=fill_event_id,
        side=side,
        fill_price=fill_price,
        mid_300s=mid_300s,
        mid_at_fill=mid_at_fill,
    )


# ── Aggregation ──────────────────────────────────────────────────────────────────

def _recent_markouts_60s(telemetry: Telemetry, strategy: str, window: int) -> list[float]:
    """
    The newest `window` non-NULL markout_60s values for `strategy`, newest first.

    Over-fetches `window * _OVERFETCH_MULTIPLIER` rows and then takes the first
    `window` non-NULL ones. A fill whose +60s book snapshot was unavailable is
    still INSERTed with markout_60s NULL (see record_markout_60s), so fetching
    exactly `window` rows and dropping NULLs returned FEWER than `window` values
    and silently jammed the flag under evaluate_flag's sample-size floor -- 100
    fills at -0.0537 behind 3 book-less ones reported n=27 and never tripped.

    The over-fetch is deliberately bounded rather than "scan until we find
    `window`": if more than two-thirds of the recent rows have no markout, the
    sample is too gappy to trust and not evaluating is the right outcome. It
    also bounds how far back in time the mean can reach.
    """
    rows = telemetry.recent_markouts(strategy, limit=window * _OVERFETCH_MULTIPLIER)
    values = [r["markout_60s"] for r in rows if r["markout_60s"] is not None]
    return values[:window]


def rolling_mean_markout(telemetry: Telemetry, strategy: str, window: int = 30) -> float | None:
    """Mean markout_60s over the last `window` fills for `strategy`. None if no data."""
    values = _recent_markouts_60s(telemetry, strategy, window)
    if not values:
        return None
    return sum(values) / len(values)


def pct_negative(telemetry: Telemetry, strategy: str, window: int = 30) -> float | None:
    """Fraction (0-1) of the last `window` fills with markout_60s < 0. None if no data."""
    values = _recent_markouts_60s(telemetry, strategy, window)
    if not values:
        return None
    return sum(1 for v in values if v < 0) / len(values)


# ── Advisory flag ────────────────────────────────────────────────────────────────

def is_flagged(strategy: str) -> bool:
    with _LOCK:
        return _STRATEGY_FLAGS.get(strategy, False)


def _set_flag(strategy: str, value: bool) -> None:
    with _LOCK:
        changed = _STRATEGY_FLAGS.get(strategy, False) != value
        _STRATEGY_FLAGS[strategy] = value
    if changed:
        log.warning(
            "adverse_selection.flag_changed",
            strategy=strategy,
            flag="adverse_selection_detected",
            value=value,
        )


def evaluate_flag(telemetry: Telemetry, strategy: str, cfg: Settings | None = None) -> bool:
    """
    Recompute the rolling mean and update the flag with hysteresis:
      mean < -0.01  -> trip
      mean > -0.005 -> clear
      otherwise     -> unchanged (dead band)

    Requires at least `cfg.adverse_selection_window` fills with a recorded
    markout_60s before the flag can change at all — a couple of noisy fills
    must never trip it. Returns the (possibly unchanged) flag state.
    """
    cfg = cfg or _default_settings
    window = cfg.adverse_selection_window
    values = _recent_markouts_60s(telemetry, strategy, window)

    if len(values) < window:
        return is_flagged(strategy)  # not enough data yet — leave state as-is

    mean = sum(values) / len(values)
    if mean < _TRIP_THRESHOLD:
        _set_flag(strategy, True)
    elif mean > _CLEAR_THRESHOLD:
        _set_flag(strategy, False)
    return is_flagged(strategy)


def restore_flags_from_history(
    telemetry: Telemetry,
    strategies: list[str],
    cfg: Settings | None = None,
) -> dict[str, bool]:
    """
    Seed _STRATEGY_FLAGS from the persisted markouts for each of `strategies`.

    Call once at startup. The flag cache is process-local and is otherwise only
    written when a NEW fill's +60s markout lands, so a restarted process trades
    with adverse_selection_detected=False -- and therefore an un-multiplied
    min_net_edge (see effective_min_net_edge) -- until its first fresh fill,
    however bad the persisted history is. Restarts are frequent enough (crashes,
    watchdog restarts) that this silently un-protected the strategy exactly when
    the history said it was being picked off.

    Never raises: an analytics failure must not stop the engine from starting.
    Returns the resolved flag state per strategy (empty on failure).
    """
    cfg = cfg or _default_settings
    restored: dict[str, bool] = {}
    for strategy in strategies:
        try:
            restored[strategy] = evaluate_flag(telemetry, strategy, cfg)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "adverse_selection.restore_failed",
                strategy=strategy,
                error=str(exc),
            )
    flagged = sorted(s for s, v in restored.items() if v)
    log.info(
        "adverse_selection.flags_restored",
        strategies=sorted(restored),
        flagged=flagged,
    )
    return restored


def effective_min_net_edge(strategy: str | None, cfg: Settings | None = None) -> float:
    """
    cfg.min_net_edge, multiplied by cfg.adverse_selection_edge_multiplier if
    `strategy` currently has the adverse_selection_detected flag set.
    """
    cfg = cfg or _default_settings
    base = cfg.min_net_edge
    if strategy and is_flagged(strategy):
        return base * cfg.adverse_selection_edge_multiplier
    return base


# ── Status / sheet summaries ──────────────────────────────────────────────────────

def strategy_summary(telemetry: Telemetry, strategy: str, cfg: Settings | None = None) -> dict:
    """
    One summary dict per strategy for `kbot status` and the Markout sheet tab.

    flag_state comes from evaluate_flag (i.e. recomputed from the persisted
    markouts), NOT from the in-memory is_flagged(). `kbot status` runs in a
    fresh CLI process where _STRATEGY_FLAGS is always empty, so reading the
    cache reported "ok" next to an avg_markout of -0.0537 over n=30 -- the
    stats came from SQLite while the flag came from memory. check_paper_promotion
    already recomputes for exactly this reason (see gates.py).
    """
    cfg = cfg or _default_settings
    window = cfg.adverse_selection_window
    values = _recent_markouts_60s(telemetry, strategy, window)
    mean = sum(values) / len(values) if values else None
    neg_pct = (sum(1 for v in values if v < 0) / len(values)) if values else None
    return {
        "strategy": strategy,
        "avg_markout_60s": mean,
        "pct_negative_60s": neg_pct,
        "flag_state": evaluate_flag(telemetry, strategy, cfg),
        "sample_size": len(values),
    }


# ── Test / reset helper ────────────────────────────────────────────────────────────

def clear_all_flags() -> None:
    """Reset all per-strategy flags to False. Used in tests and on clean startup."""
    with _LOCK:
        _STRATEGY_FLAGS.clear()
