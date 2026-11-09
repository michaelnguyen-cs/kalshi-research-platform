"""
Real-money settlement reconciliation: what KALSHI says a live trade made,
not what our model computes it should have made.

Paper trades resolve locally (kbot.outcomes.build_trade_outcome): fetch the
settled market, read `result`, and compute P&L from the backtest formula. That
is the right behaviour for paper — it must stay directly comparable to the
backtest distribution the paper gate and kill-fast check against, and there is
no exchange record of a trade that never happened.

For REAL money it is the wrong source. The locally computed number is an
ESTIMATE in two places:

  * `fee` is modelled as fee_coefficient * contracts * price * (1 - price).
    Kalshi charges a rounded-up cent amount off its own schedule; the modelled
    figure is close but is not what was debited.
  * the payout is derived from our recorded fill price and contract count,
    which is our record of the fill, not the exchange's.

Both are small per trade and both compound across a history that is supposed
to reconcile against an actual account balance. So a live position's outcome
is taken from the exchange's own accounting for that market — `market_result`,
`realized_pnl`, `fees_paid`, `total_cost` off GET /portfolio/positions — and
the locally computed values are kept alongside it for comparison.

The local path remains the FALLBACK for a live trade whose position record
can't be fetched or hasn't settled, so a reconciliation outage degrades to an
estimate rather than to nothing. Every outcome records which source it used in
`pnl_source`, so an estimate can never be mistaken for a fact after the event.

Units: Kalshi returns realized_pnl / fees_paid / total_cost as integer CENTS.
Everything this module returns is DOLLARS.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Cents-to-dollars, matching the same correction applied to get_balance in
# kbot.runner._resolve_bankroll / kbot.live_pnl.balance_dollars.
_CENTS = 100.0

# Tolerance when deciding whether Kalshi's realized_pnl is quoted gross or net
# of fees (see net_pnl_dollars). One cent of slack absorbs the exchange's
# cent-rounding without being loose enough to match the wrong interpretation:
# fees on any position large enough to matter exceed this by an order of
# magnitude.
_PNL_BASIS_TOLERANCE = 0.011


def _cents_to_dollars(raw: Any) -> float | None:
    """Integer-cent field -> dollars. None when absent or unparseable."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw) / _CENTS
    except (TypeError, ValueError):
        return None


def find_position(positions: list[dict], ticker: str) -> dict | None:
    """
    The position record for `ticker`, or None.

    Matches on the `ticker` field only. A settled position keeps its ticker,
    so this works after the market closes — which is the whole point.
    """
    for pos in positions or []:
        if str(pos.get("ticker") or "") == ticker:
            return pos
    return None


def settled_result(position: dict) -> bool | None:
    """
    True/False if Kalshi has settled this market YES/NO, else None.

    `market_result` is the exchange's own settlement call on the position
    record. It is empty until the market settles, and an empty value must read
    as "not settled yet" rather than as NO — resolving a live trade against a
    guess is exactly the failure this module exists to prevent.
    """
    result = str(position.get("market_result") or "").strip().lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    return None


def net_pnl_dollars(
    position: dict,
    *,
    won: bool,
    contracts: int,
    fill_price: float,
) -> tuple[float | None, str]:
    """
    Kalshi's realized P&L for this position in dollars, NET of fees, plus a
    label describing how it was derived.

    Kalshi reports `realized_pnl` and `fees_paid` as separate cent fields, and
    does not document whether the former is quoted gross or net of the latter.
    Rather than hard-code an assumption that would silently misstate every
    live trade if wrong, this checks the reported figure against the payout
    arithmetic and reports which reading it matched:

        "kalshi_gross_minus_fees"  realized_pnl matched payout - cost, so fees
                                   are still outstanding and get subtracted.
        "kalshi_net"               realized_pnl already had fees taken out.
        "kalshi_ambiguous"         neither matched (a partial close, a
                                   multi-fill position, an unexpected cost
                                   basis). Fees are subtracted anyway, which
                                   UNDER-states profit — the same direction
                                   kbot.live_pnl deliberately errs in, since a
                                   visible shortfall beats a silently inflated
                                   real-money total.

    Returns (None, "unavailable") when realized_pnl is absent, so the caller
    falls back to the local estimate instead of recording a fabricated zero.
    """
    realized = _cents_to_dollars(position.get("realized_pnl"))
    if realized is None:
        return None, "unavailable"

    fees = _cents_to_dollars(position.get("fees_paid")) or 0.0

    # Kalshi's own cost basis when it reports one; ours otherwise. A settled
    # position can report total_cost 0 once it is closed out, which is not a
    # real $0 basis — fall back rather than treat it as one.
    cost = _cents_to_dollars(position.get("total_cost"))
    if not cost:
        cost = contracts * fill_price

    payout = float(contracts) if won else 0.0
    expected_gross = payout - cost

    if abs(realized - expected_gross) <= _PNL_BASIS_TOLERANCE:
        return round(realized - fees, 6), "kalshi_gross_minus_fees"
    if abs(realized - (expected_gross - fees)) <= _PNL_BASIS_TOLERANCE:
        return round(realized, 6), "kalshi_net"

    log.warning(
        "live_settlement.pnl_basis_ambiguous",
        ticker=position.get("ticker"),
        realized_pnl=realized,
        fees_paid=fees,
        expected_gross=round(expected_gross, 6),
        contracts=contracts,
        fill_price=fill_price,
    )
    return round(realized - fees, 6), "kalshi_ambiguous"


def reconcile_position(
    position: dict,
    *,
    side: str,
    contracts: int,
    fill_price: float,
) -> dict | None:
    """
    Kalshi's settled accounting for one held position, or None if the market
    has not settled yet (caller should retry later, not record a guess).

    `side`/`contracts`/`fill_price` are OUR record of the fill and are used
    only to interpret the exchange's figures — they never override them.

    The returned dict is merged into the trade_outcome payload by
    kbot.outcomes.build_live_trade_outcome.
    """
    resolved = settled_result(position)
    if resolved is None:
        return None

    side_val = (side.value if hasattr(side, "value") else str(side)).lower()
    won = (side_val == "yes" and resolved) or (side_val == "no" and not resolved)

    pnl, basis = net_pnl_dollars(
        position, won=won, contracts=contracts, fill_price=fill_price,
    )
    if pnl is None:
        return None

    fees = _cents_to_dollars(position.get("fees_paid")) or 0.0
    return {
        "resolved_yes": resolved,
        "won": won,
        "pnl_dollars": round(pnl, 6),
        "net_pnl": round(pnl, 6),
        "fee": round(fees, 6),
        "gross_pnl": float(contracts) if won else 0.0,
        "result": "yes" if resolved else "no",
        "pnl_source": basis,
        # The exchange's raw figures, kept verbatim so a later audit can
        # re-derive the interpretation above without re-querying Kalshi.
        "kalshi_realized_pnl": _cents_to_dollars(position.get("realized_pnl")),
        "kalshi_fees_paid": fees,
        "kalshi_total_cost": _cents_to_dollars(position.get("total_cost")),
        "kalshi_market_result": str(position.get("market_result") or "").lower(),
    }


def fetch_settled_position(
    client: Any,
    ticker: str,
    *,
    side: str,
    contracts: int,
    fill_price: float,
) -> dict | None:
    """
    Fetch `ticker` from Kalshi and reconcile it. None on any failure — an
    unreachable exchange must leave the caller free to fall back to the local
    estimate, never raise into a settlement path or a trading loop.
    """
    try:
        positions = client.get_positions(ticker=ticker)
    except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
        log.warning(
            "live_settlement.get_positions_failed", ticker=ticker, error=str(exc)[:200]
        )
        return None

    position = find_position(positions, ticker)
    if position is None:
        log.warning("live_settlement.position_not_found", ticker=ticker)
        return None

    return reconcile_position(
        position, side=side, contracts=contracts, fill_price=fill_price,
    )
