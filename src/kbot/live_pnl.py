"""
Real-money P&L accounting -> the "<X> Live P&L" sheet tab.

This module answers one question and refuses to guess at it: which settled
trades were placed with REAL MONEY, and what did they actually make?

The answer comes exclusively from the `is_live` flag stamped on each position
at ORDER-PLACEMENT time (kbot.execution.Executor.execute ->
PositionTracker.open_position -> kbot.outcomes.build_trade_outcome). It is
never re-derived from:

  * the process's current DRY_RUN -- that's config read at whatever time the
    code happens to run. A trade_outcome recorded by a CLI backfill would take
    the shell's DRY_RUN, and flipping DRY_RUN after a demote would relabel
    real positions still waiting to settle.
  * the strategy's current gate level -- that's mutable state in a separate DB.
    One `kbot gates demote` would retroactively turn every real trade in the
    history into a paper one.

A missing flag reads as PAPER, always. Under-reporting a real trade is a
visible gap in a tab the operator reads; over-reporting a paper trade silently
inflates a real-money running total, which is the error that actually costs
something.

Idempotency: the row set is rebuilt from the event log on every push and
upserted by (ticker, side) -- see SheetsClient.upsert_live_pnl. Re-running
`kbot learn --evaluate` any number of times converges to the same tab.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Real-money history must never be silently truncated the way a rolling
# analytics window can be, so this is set far above any plausible trade count
# rather than reusing the 10_000 default the learning path uses.
_OUTCOME_QUERY_LIMIT = 1_000_000


def is_live_outcome(payload: dict) -> bool:
    """
    Whether a trade_outcome payload represents a REAL-MONEY trade.

    Strictly the persisted placement-time flag. Anything else -- missing,
    None, False -- is paper.
    """
    return bool(payload.get("is_live", False))


def balance_dollars(raw: Any) -> float | None:
    """
    Account balance in dollars from a KalshiClient.get_balance() response.

    Follows the convention already used for position sizing in kbot.runner
    (`float(bal["balance"])` taken as dollars). If `kbot balance` reports cents
    for your account -- i.e. it prints ~10000 for a $100 balance -- this is the
    single place to divide by 100, and the same correction is then needed in
    kbot.runner._resolve_bankroll, where the same value feeds live Kelly
    sizing. Deliberately left unconverted rather than guessed at: a wrong
    constant here misstates the drift column, and the same wrong constant there
    misstates real position sizes.

    Returns None when no usable number is present, so callers leave the
    balance cell empty instead of writing a fabricated 0.
    """
    if raw is None:
        return None
    value = raw.get("balance") if isinstance(raw, dict) else raw
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def live_outcomes(
    telemetry: Any,
    strategy: str | None = None,
    limit: int = _OUTCOME_QUERY_LIMIT,
) -> list[dict]:
    """
    Real-money trade_outcome events, oldest first, one per (ticker, side).

    Each returned dict is {"ts": float, "payload": dict}.

    `strategy`, when given, keeps only outcomes recorded for it. Outcomes with
    NO strategy recorded are kept and warned about: each deployment owns its
    own events DB (see Settings.events_db_path), so an unattributed real trade
    in this DB belongs to this deployment, and dropping a real settled trade
    from money accounting is worse than including one whose label went missing.

    Duplicate (ticker, side) outcomes collapse to the earliest -- the same
    identity kbot.outcomes.backfill_outcomes dedupes on. A second outcome for
    one trade would otherwise double-count it in the running total.
    """
    events = telemetry.recent(event_type="trade_outcome", limit=limit)
    events = sorted(events, key=lambda e: e.get("ts", 0.0))

    seen: dict[tuple[str, str], dict] = {}
    duplicates = 0
    unattributed = 0

    for ev in events:
        payload = ev.get("payload") or {}
        if not is_live_outcome(payload):
            continue

        recorded_strategy = payload.get("strategy")
        if strategy is not None and recorded_strategy != strategy:
            if recorded_strategy is not None:
                continue
            unattributed += 1

        key = (str(ev.get("ticker") or payload.get("ticker", "")), str(payload.get("side", "")))
        if key in seen:
            duplicates += 1
            continue
        seen[key] = {"ts": ev.get("ts", 0.0), "payload": payload}

    if duplicates:
        log.warning("live_pnl.duplicate_outcomes_ignored", count=duplicates, strategy=strategy)
    if unattributed:
        log.warning("live_pnl.unattributed_outcomes_included", count=unattributed, strategy=strategy)

    return list(seen.values())


def build_live_pnl_rows(
    outcomes: list[dict],
    balance: float | None = None,
) -> list[dict]:
    """
    Live P&L sheet rows from `live_outcomes` output, with a running total.

    cumulative_realized_pnl is recomputed from the full ordered history on
    every call, so it is a pure function of the event log -- it cannot drift
    from repeated pushes, and a trade recorded late lands in the right place
    with the totals after it corrected.

    `balance` (the account balance observed now) is stamped on every row here;
    SheetsClient.upsert_live_pnl then keeps it only for rows it appends,
    preserving what was recorded for rows already in the sheet.
    """
    rows: list[dict] = []
    cumulative = 0.0

    for entry in outcomes:
        payload = entry["payload"]
        pnl = float(payload.get("pnl_dollars", 0.0) or 0.0)
        cumulative += pnl
        cumulative_rounded = round(cumulative, 4)

        rows.append({
            "resolved_at": _iso(entry.get("ts")),
            "ticker": payload.get("ticker", ""),
            "side": payload.get("side", ""),
            "contracts": payload.get("contracts", ""),
            "fill_price": payload.get("fill_price", ""),
            "pnl_dollars": round(pnl, 4),
            "cumulative_realized_pnl": cumulative_rounded,
            "kalshi_balance": "" if balance is None else round(balance, 4),
            "balance_minus_cumulative": (
                "" if balance is None else round(balance - cumulative_rounded, 4)
            ),
        })

    return rows


def push_live_pnl(
    telemetry: Any,
    sheets: Any,
    *,
    strategy: str | None = None,
    balance: float | None = None,
) -> dict[str, int]:
    """
    Build and upsert this strategy's real-money P&L rows. Best-effort.

    `strategy` defaults to the sheets client's own scope, so the tab written
    and the trades selected always come from the same strategy -- they can't
    drift apart into ETH trades landing in a BTC tab.

    Returns the upsert counts ({"appended", "updated", "unchanged"}); all-zero
    when there are no real-money trades yet, in which case no tab is created.
    """
    empty = {"appended": 0, "updated": 0, "unchanged": 0}
    try:
        if strategy is None:
            strategy = getattr(sheets, "strategy", None)
        outcomes = live_outcomes(telemetry, strategy=strategy)
        if not outcomes:
            log.info("live_pnl.no_live_trades", strategy=strategy)
            return empty
        rows = build_live_pnl_rows(outcomes, balance=balance)
        return sheets.upsert_live_pnl(rows)
    except Exception as exc:  # noqa: BLE001 — accounting must never crash a caller
        log.error("live_pnl.push_failed", strategy=strategy, error=str(exc)[:300])
        return empty


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
