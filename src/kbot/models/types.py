"""
Pydantic data models shared across the bot.

Prices are dollar floats internally (the API sends/receives dollar strings,
conversion happens at the client boundary in kalshi_client.py).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from kbot.quantity import UNITS_PER_CONTRACT as _UNITS_PER_CONTRACT


# ── Enumerations ──────────────────────────────────────────────────────────────

class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    RESTING = "resting"
    FILLED = "filled"
    CANCELLED = "cancelled"
    PENDING = "pending"


# ── Market snapshot ───────────────────────────────────────────────────────────

class PriceLevel(BaseModel):
    """
    A single price level in the order book (yes or no side).

    `quantity_units` is in integer HUNDREDTHS of a contract -- 1.00 contract is
    100 units. See kbot.quantity: Kalshi quotes sizes as fixed-point strings
    with two decimals, and int(float(...)) silently truncated the fraction in
    both directions. The `_units` suffix is load-bearing; do not drop it.
    """
    price: Annotated[float, Field(gt=0.0, lt=1.0)]   # 0 < p < 1 in dollar terms
    quantity_units: int = Field(ge=0)

    @property
    def quantity_contracts(self) -> float:
        """Size in contracts, for dollar math and display only."""
        return self.quantity_units / _UNITS_PER_CONTRACT


class OrderBook(BaseModel):
    """Snapshot of a market's order book."""
    ticker: str
    yes: list[PriceLevel] = Field(default_factory=list)
    no: list[PriceLevel] = Field(default_factory=list)

    def best_yes(self) -> PriceLevel | None:
        """Cheapest YES ask (lowest price to buy YES)."""
        return min(self.yes, key=lambda l: l.price, default=None)

    def best_no(self) -> PriceLevel | None:
        """Cheapest NO ask (lowest price to buy NO)."""
        return min(self.no, key=lambda l: l.price, default=None)

    def mid_price(self) -> float | None:
        """Midpoint between best YES ask and implied YES bid from best NO ask."""
        best_y = self.best_yes()
        best_n = self.best_no()
        if best_y is None or best_n is None:
            return None
        # In Kalshi: YES price + NO price = $1.00 at fair value.
        # Mid = average of best_yes_ask and (1 - best_no_ask).
        return (best_y.price + (1.0 - best_n.price)) / 2.0


class MarketTicker(BaseModel):
    """Lightweight market summary from the ticker channel."""
    ticker: str
    yes_ask: float | None = None
    yes_bid: float | None = None
    last_price: float | None = None
    volume: int = 0
    open_interest: int = 0


# ── Orders ────────────────────────────────────────────────────────────────────

class OrderIntent(BaseModel):
    """
    Validated order parameters ready to hand to KalshiClient.place_order().

    Using a model (rather than raw kwargs) forces validation before the order
    reaches the network — a miscalculated price gets caught at construction.
    """
    ticker: str
    side: Side
    action: Action = Action.BUY
    order_type: OrderType = OrderType.LIMIT
    count: int = Field(ge=1)
    price_dollars: float = Field(gt=0.0, lt=1.0)

    @field_validator("price_dollars")
    @classmethod
    def round_to_cent(cls, v: float) -> float:
        # Kalshi prices have 4-decimal precision but are multiples of $0.01 in practice.
        return round(v, 4)


class FillEvent(BaseModel):
    """A fill received from the 'fill' WebSocket channel."""
    order_id: str
    client_order_id: str
    ticker: str
    side: Side
    action: Action
    count: int
    price_dollars: float
    timestamp: datetime


# ── Sizing output ─────────────────────────────────────────────────────────────

class SizingResult(BaseModel):
    """Output from the risk/sizing layer for a single prospective trade."""
    ticker: str
    side: Side
    contracts: int              # proposed contract count (may be 0 = skip)
    price_dollars: float
    fair_p: float = 0.0         # model fair probability for this side (post-calibration)
    # Pre-calibration model probability. Calibrators must be FIT on this,
    # never on fair_p: fitting on the calibrated output and applying to the
    # raw input compounds a domain mismatch every retrain generation (see
    # kbot.learning.retrain). None for strategies that don't calibrate.
    raw_fair_p: float | None = None
    net_edge_dollars: float     # edge per contract after fees + slippage
    kelly_fraction_used: float
    # Size ACTUALLY filled, in integer hundredths of a contract (kbot.quantity).
    # `contracts` above is the whole-contract count that was ORDERED; a partial
    # fill can be fractional, and this is the only field that keeps that exact.
    # None until an order has been placed and acked.
    filled_units: int | None = None
    capped: bool = False        # True if any cap was applied
    skip_reason: str | None = None  # why contracts == 0, if applicable
    # False when this result is a STAND-DOWN that never priced the book -- the
    # strategy declined before reading a quote or running the model. On such a
    # result price_dollars / fair_p / net_edge_dollars are placeholder zeros
    # carrying no information, and must never be logged or analysed as if they
    # were measurements. Telemetry emits None for them instead (see
    # kbot.runner._evaluate_ticker).
    priced: bool = True
    # Correlation bucket for portfolio-level exposure caps (see risk_portfolio.py).
    # e.g. crypto_price, us_macro, weather_nyc, sports_game:{game_id}.
    event_driver: str = "other"


# ── P&L accounting ────────────────────────────────────────────────────────────

class PositionSnapshot(BaseModel):
    """Portfolio position as reported by the balance/positions API."""
    ticker: str
    position: int       # signed: positive = long YES, negative = short YES
    market_exposure: float   # dollar exposure at current price
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
