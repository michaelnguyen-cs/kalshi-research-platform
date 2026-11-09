"""
Fee formula and net-edge gate.

Kalshi fee formula (taker, most markets):
  fee_per_contract = ceil(coeff * C * P * (1 - P))

where C = number of contracts, P = price in dollars.

The gate rejects any order whose expected net edge per contract is below
settings.min_net_edge.  This is the last check before execution.
"""

from __future__ import annotations

import math

from kbot.config import Settings, settings as _default_settings
from kbot.models.types import SizingResult


def fee_per_contract(price_dollars: float, cfg: Settings | None = None) -> float:
    """
    Estimated taker fee per contract in dollars.

    Uses settings.fee_coefficient (default 0.07).  Always verify against
    the current Kalshi fee schedule — maker/special-market rates differ.
    """
    cfg = cfg or _default_settings
    raw = cfg.fee_coefficient * price_dollars * (1.0 - price_dollars)
    # Kalshi rounds up to the nearest cent; we use the raw float for modelling.
    return raw


def net_edge(
    fair_prob: float,
    ask_dollars: float,
    cfg: Settings | None = None,
) -> float:
    """
    Net edge per contract in dollars:
      (fair_p - ask) - fee_per_contract(ask) - assumed_slippage
    """
    cfg = cfg or _default_settings
    gross = fair_prob - ask_dollars
    fee = fee_per_contract(ask_dollars, cfg)
    return gross - fee - cfg.assumed_slippage


def passes_edge_gate(
    result: SizingResult,
    cfg: Settings | None = None,
    half_spread: float = 0.0,
) -> bool:
    """
    Return True if the sizing result's net_edge_dollars clears
    min_net_edge + half_spread and the contract count is positive.

    `half_spread` defaults to 0.0 (unchanged behaviour) for callers that
    don't have book data on hand. Pass it (see half_spread_dollars) when
    you do -- a claimed edge that's smaller than half the bid-ask spread
    is spread noise, not a real mispricing (2026-07 calibration audit).
    """
    cfg = cfg or _default_settings
    return result.contracts > 0 and result.net_edge_dollars >= cfg.min_net_edge + half_spread


def half_spread_dollars(yes_ask: float, no_ask: float) -> float:
    """
    Half the implied bid-ask spread, from Kalshi's complementary pricing
    (yes_bid == 1 - no_ask): spread = yes_ask - yes_bid = yes_ask + no_ask - 1.

    Clamped to >= 0 so a crossed/tight book never loosens the gate.
    """
    return max(0.0, (yes_ask + no_ask - 1.0) / 2.0)


# ── Maker / taker edge (Part 3, Phase A1) ──────────────────────────────────────
# Crossing the spread (taker) and resting inside it (maker) have different
# prices and different fee rates, so they must be evaluated independently —
# a signal can clear the gate on one path while failing on the other.

def fee_per_contract_maker(price_dollars: float, cfg: Settings | None = None) -> float:
    """
    Estimated maker (resting-order) fee per contract in dollars.

    Uses settings.fee_coefficient_maker (default 0.0). ALWAYS verify against
    the current Kalshi fee schedule before trusting.
    """
    cfg = cfg or _default_settings
    return cfg.fee_coefficient_maker * price_dollars * (1.0 - price_dollars)


def net_edge_taker(
    fair_prob: float,
    ask_dollars: float,
    cfg: Settings | None = None,
) -> float:
    """Net edge per contract when crossing the spread at `ask_dollars`."""
    return net_edge(fair_prob, ask_dollars, cfg)


def net_edge_maker(
    fair_prob: float,
    maker_price_dollars: float,
    cfg: Settings | None = None,
) -> float:
    """Net edge per contract when resting a limit order at `maker_price_dollars`."""
    cfg = cfg or _default_settings
    gross = fair_prob - maker_price_dollars
    fee = fee_per_contract_maker(maker_price_dollars, cfg)
    return gross - fee - cfg.assumed_slippage


# ── Phase A4 — research-driven rules (see MARKET_RESEARCH.md) ──────────────────

# RULE R1 (Source 1/4/7): sub-15c contracts are a structural fee-death zone
# for TAKER entries — a near-flat fee is a huge percentage tax on a tiny
# stake, and the favorite-longshot bias means longshot buyers lose money on
# average. Maker orders are exempt on standard (0% maker fee) markets, since
# resting doesn't pay that tax — but NOT exempt on special-event markets
# charging the flat 0.25% maker fee (see MAKER_FEE_SPECIAL_SERIES below),
# where the same fee-death math applies to makers too.
def passes_price_floor(
    price_dollars: float,
    ticker: str,
    cfg: Settings | None = None,
    *,
    is_maker: bool = False,
) -> bool:
    """RULE R1 taker price floor. Returns True if the order is allowed to proceed."""
    cfg = cfg or _default_settings
    if not is_maker:
        return price_dollars >= cfg.taker_price_floor
    if is_special_event_market(ticker):
        return price_dollars >= cfg.taker_price_floor
    return True


# Per-series maker fee override registry (Source 7). Standard markets charge
# 0% maker fee; special events (championships, elections, etc.) charge a
# flat 0.25% of contract price instead of the usual P*(1-P) formula.
#
# Verified 2026-07 against the installed kalshi_python SDK: Market, Series,
# and Event models expose NO fee-schedule field over the API today (Market:
# ticker, series_ticker, event_ticker, title, subtitle, open_time, close_time,
# expiration_time, status, yes_bid, yes_ask, no_bid, no_ask, last_price,
# volume, volume_24h, result, can_close_early, cap_count — no fee fields).
# So this code-level registry is the authoritative source until Kalshi
# exposes fee metadata over the API. Populate as special-fee series are
# confirmed against the published fee schedule.
MAKER_FEE_SPECIAL_SERIES: set[str] = set()

_SPECIAL_EVENT_MAKER_FEE_PCT = 0.0025  # 0.25% of contract price, flat per contract


def series_ticker_prefix(ticker: str) -> str:
    """
    Best-effort series ticker from a market ticker.

    Kalshi market tickers are "{SERIES}-{suffix}", e.g.
    "KXBTC-26JUL0317-T48000" -> "KXBTC". Falls back to the full ticker if
    there is no separator.
    """
    return ticker.split("-", 1)[0] if ticker else ticker


def is_special_event_market(ticker: str) -> bool:
    """True if `ticker`'s series charges the special-event flat maker fee."""
    return series_ticker_prefix(ticker) in MAKER_FEE_SPECIAL_SERIES


def maker_fee_coefficient_for_market(ticker: str, cfg: Settings | None = None) -> float:
    """
    The P*(1-P) maker fee coefficient for `ticker`'s series, or None if this
    series uses the special-event flat-percentage model instead (see
    maker_fee_dollars_for_market, which handles both cases).
    """
    cfg = cfg or _default_settings
    return cfg.maker_fee_default


def maker_fee_dollars_for_market(
    ticker: str,
    price_dollars: float,
    cfg: Settings | None = None,
) -> float:
    """
    Per-contract maker fee in dollars for `ticker`, resolved per-market
    rather than from a single global coefficient (Source 7 / V2).

    Standard markets: cfg.maker_fee_default * P * (1-P) (0.0 by default).
    Special-event markets (MAKER_FEE_SPECIAL_SERIES): a flat 0.25% of the
    contract price, per Kalshi's published special-event maker fee.
    """
    cfg = cfg or _default_settings
    if is_special_event_market(ticker):
        return _SPECIAL_EVENT_MAKER_FEE_PCT * price_dollars
    return maker_fee_coefficient_for_market(ticker, cfg) * price_dollars * (1.0 - price_dollars)


def net_edge_maker_for_market(
    fair_prob: float,
    maker_price_dollars: float,
    ticker: str,
    cfg: Settings | None = None,
) -> float:
    """Net edge per contract when resting at `maker_price_dollars` on `ticker`, per-market fee."""
    cfg = cfg or _default_settings
    gross = fair_prob - maker_price_dollars
    fee = maker_fee_dollars_for_market(ticker, maker_price_dollars, cfg)
    return gross - fee - cfg.assumed_slippage
