"""
Historical replay engine (net-of-fee metrics).

Accepts a CSV/JSON dataset of (timestamp, btc_price, btc_vol, orderbook)
rows and replays them through the strategy + edge gate, accumulating a
simulated P&L curve.

Output metrics:
  - Total return (%)
  - Sharpe ratio (annualised, daily returns)
  - Max drawdown (%)
  - Win rate (% of trades that resolved profitably)
  - Total trades placed / skipped

Usage:
    kbot backtest --strategy btc_hourly --data path/to/data.csv
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.quantity import UNITS_PER_CONTRACT
from kbot.edge import fee_per_contract, maker_fee_dollars_for_market, passes_edge_gate, passes_price_floor
from kbot.models.types import OrderBook, PriceLevel, Side, SizingResult
from kbot.strategy.base import Strategy

log = structlog.get_logger(__name__)

# ── Fee-tier price buckets (RULE R4, MARKET_RESEARCH.md) ────────────────────────
# Fee drag as a fraction of a contract's price is worst at the extremes — these
# buckets make "fee death zones" visible per strategy in every backtest report.
_PRICE_BUCKETS: list[tuple[str, float, float]] = [
    ("0-15c",  0.00, 0.15),
    ("15-30c", 0.15, 0.30),
    ("30-50c", 0.30, 0.50),
    ("50-70c", 0.50, 0.70),
    ("70-85c", 0.70, 0.85),
    ("85-99c", 0.85, 0.99),
]

# ── Calibration price buckets (2026-07 calibration audit) ───────────────────────
# Bucketed by the model's *claimed* fair_p rather than the traded ask price —
# this is what tells you whether the model itself is well-calibrated (actual
# win rate tracking claimed probability) as opposed to where fees bite.
_FAIR_P_BUCKETS: list[tuple[str, float, float]] = [
    ("0-10%",   0.00, 0.10),
    ("10-20%",  0.10, 0.20),
    ("20-30%",  0.20, 0.30),
    ("30-40%",  0.30, 0.40),
    ("40-50%",  0.40, 0.50),
    ("50-60%",  0.50, 0.60),
    ("60-70%",  0.60, 0.70),
    ("70-80%",  0.70, 0.80),
    ("80-90%",  0.80, 0.90),
    ("90-100%", 0.90, 1.00),
]


# ── Row-level price validity ─────────────────────────────────────────────────
# The Kalshi candlestick endpoint returns settled-contract rows (yes_ask/no_ask
# == $1.00 or $0.00 in the contract's final hour) and occasional sparse-book
# rows (only one side quoted). Neither carries price-discovery value and both
# violate PriceLevel's 0 < price < 1 constraint, so they're filtered before
# any OrderBook is built.
_MAX_BOOK_SUM = 1.10
_MIN_BOOK_SUM = 0.50


def _row_price_is_valid(yes_ask: float, no_ask: float) -> bool:
    """True if yes_ask/no_ask are both in (0, 1) and not a crossed/sparse book."""
    if not (0.0 < yes_ask < 1.0):
        return False
    if not (0.0 < no_ask < 1.0):
        return False
    total = yes_ask + no_ask
    return _MIN_BOOK_SUM <= total <= _MAX_BOOK_SUM


# ── Data row ──────────────────────────────────────────────────────────────────

@dataclass
class BacktestRow:
    """One tick of historical data."""
    timestamp: float        # Unix seconds
    btc_price: float
    btc_vol: float          # annualised
    ticker: str
    yes_ask: float          # best YES ask price at this tick
    no_ask: float           # best NO ask price at this tick
    resolved_yes: bool      # how the contract actually resolved
    floor_strike: float | None = None  # KXBTC15M etc.: strike not encoded in the ticker (see btc_hourly.py)


# ── Results ───────────────────────────────────────────────────────────────────

@dataclass
class PriceBucketStats:
    """Per-price-bucket trade stats (RULE R4 fee-tier simulator)."""
    label: str
    low: float
    high: float
    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    def breakeven_win_rate(self, cfg: Settings) -> tuple[float, float]:
        """
        (breakeven_wr_taker, breakeven_wr_maker) at this bucket's midpoint.

        For a $1-payout contract bought at price P with per-contract fee F,
        expected value is zero when w*(1-P-F) - (1-w)*(P+F) = 0, i.e. the
        breakeven win rate is simply P + F.
        """
        mid = (self.low + self.high) / 2.0
        taker_fee = fee_per_contract(mid, cfg)
        maker_fee = maker_fee_dollars_for_market("", mid, cfg)  # "" -> no series override
        return mid + taker_fee, mid + maker_fee


@dataclass
class TradeRecord:
    """One executed trade, retained for post-hoc calibration/threshold analysis."""
    ticker: str
    side: Side
    fair_p: float           # model's claimed win probability for the side traded
    ask: float               # price paid
    net_edge: float          # net_edge_dollars claimed by the strategy at entry
    won: bool
    pnl: float


@dataclass
class CalibrationBucketStats:
    """Per-fair_p-bucket calibration stats: does actual win rate track claimed probability?"""
    label: str
    low: float
    high: float
    trades: int = 0
    wins: int = 0
    sum_ask: float = 0.0
    sum_net_edge: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def mean_ask(self) -> float:
        return self.sum_ask / self.trades if self.trades else 0.0

    @property
    def mean_net_edge(self) -> float:
        return self.sum_net_edge / self.trades if self.trades else 0.0


def _find_calibration_bucket(
    buckets: list[CalibrationBucketStats], fair_p: float
) -> CalibrationBucketStats | None:
    """Highest bucket whose range contains `fair_p` (buckets are half-open [low, high))."""
    for bucket in buckets:
        if bucket.low <= fair_p < bucket.high:
            return bucket
    if buckets and fair_p >= buckets[-1].high:
        return buckets[-1]
    return None


@dataclass
class BacktestResult:
    total_trades: int = 0
    winning_trades: int = 0
    skipped_ticks: int = 0
    skipped_invalid_price_rows: int = 0
    pnl_series: list[float] = field(default_factory=list)   # cumulative P&L per trade
    daily_returns: list[float] = field(default_factory=list)
    starting_bankroll: float = 1000.0
    price_buckets: list[PriceBucketStats] = field(
        default_factory=lambda: [PriceBucketStats(label, lo, hi) for label, lo, hi in _PRICE_BUCKETS]
    )
    trades: list[TradeRecord] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades else 0.0

    def fee_tier_report(self, cfg: Settings | None = None) -> list[dict]:
        """
        RULE R4: per-price-bucket table — trades, win rate, break-even win
        rate given fees at that bucket (taker and maker), and net P&L.
        Makes fee "death zones" visible per strategy.
        """
        cfg = cfg or _default_settings
        rows = []
        for bucket in self.price_buckets:
            be_taker, be_maker = bucket.breakeven_win_rate(cfg)
            rows.append({
                "label": bucket.label,
                "trades": bucket.trades,
                "win_rate": bucket.win_rate,
                "breakeven_wr_taker": be_taker,
                "breakeven_wr_maker": be_maker,
                "net_pnl": bucket.net_pnl,
            })
        return rows

    def calibration_report(self) -> list[dict]:
        """
        2026-07 calibration audit: bucket executed trades by the model's
        claimed fair_p and compare against actual win rate. A well-calibrated
        model has actual win rate ≈ bucket midpoint in every bucket; a large
        gap (actual << claimed) means the model is systematically overconfident
        in that region of probability space.
        """
        buckets = [CalibrationBucketStats(label, lo, hi) for label, lo, hi in _FAIR_P_BUCKETS]
        for t in self.trades:
            bucket = _find_calibration_bucket(buckets, t.fair_p)
            if bucket is not None:
                bucket.trades += 1
                if t.won:
                    bucket.wins += 1
                bucket.sum_ask += t.ask
                bucket.sum_net_edge += t.net_edge
        return [
            {
                "label": b.label,
                "trades": b.trades,
                "win_rate": b.win_rate,
                "mean_ask": b.mean_ask,
                "mean_net_edge": b.mean_net_edge,
            }
            for b in buckets
        ]

    @property
    def total_return_pct(self) -> float:
        return sum(self.pnl_series)  # in dollar terms — caller divides by starting bankroll

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe from daily_returns (assumes 252 trading days)."""
        if len(self.daily_returns) < 2:
            return 0.0
        n = len(self.daily_returns)
        mean = sum(self.daily_returns) / n
        variance = sum((r - mean) ** 2 for r in self.daily_returns) / max(n - 1, 1)
        std = math.sqrt(variance)
        return (mean / std * math.sqrt(252)) if std > 1e-12 else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        """Maximum peak-to-trough drawdown as a fraction of starting bankroll."""
        if not self.pnl_series:
            return 0.0
        peak = 0.0   # cumulative P&L high-water mark (0 = starting value)
        cum = 0.0
        max_dd = 0.0
        denom = self.starting_bankroll if self.starting_bankroll > 0 else 1.0
        for pnl in self.pnl_series:
            cum += pnl
            peak = max(peak, cum)
            # Drawdown from HWM relative to starting bankroll
            dd = (peak - cum) / denom
            max_dd = max(max_dd, dd)
        return max_dd


# ── Loader ────────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list[BacktestRow]:
    """
    Load a CSV file with columns:
      timestamp, btc_price, btc_vol, ticker, yes_ask, no_ask, resolved_yes
      [, floor_strike]

    resolved_yes should be 1/0 or true/false. floor_strike is optional
    (older CSVs without the column, or hourly rows that don't need it,
    just leave it blank -- see BTCHourlyStrategy for which series do).

    Rows with no price-discovery value (settled contracts at $1.00/$0.00,
    crossed books, or sparse-book artifacts) are dropped here so the
    invalid-row count is visible before the backtest runs.
    """
    rows: list[BacktestRow] = []
    skipped = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            yes_ask = float(row["yes_ask"])
            no_ask = float(row["no_ask"])
            if not _row_price_is_valid(yes_ask, no_ask):
                skipped += 1
                continue
            floor_strike_str = row.get("floor_strike")
            rows.append(BacktestRow(
                timestamp=float(row["timestamp"]),
                btc_price=float(row["btc_price"]),
                btc_vol=float(row["btc_vol"]),
                ticker=row["ticker"],
                yes_ask=yes_ask,
                no_ask=no_ask,
                resolved_yes=str(row["resolved_yes"]).strip().lower() in ("1", "true", "yes"),
                floor_strike=float(floor_strike_str) if floor_strike_str else None,
            ))
    log.info(
        "backtest.load_csv.done",
        rows_loaded=len(rows),
        rows_skipped_invalid_price=skipped,
    )
    return rows


def _find_bucket(buckets: list[PriceBucketStats], price: float) -> PriceBucketStats | None:
    """Highest bucket whose range contains `price` (buckets are half-open [low, high))."""
    for bucket in buckets:
        if bucket.low <= price < bucket.high:
            return bucket
    # 0.99 (the top of the last bucket) and any price >= 0.99 fall in the
    # top bucket -- contracts don't trade above ~0.99 in practice.
    if buckets and price >= buckets[-1].high:
        return buckets[-1]
    return None


# ── Engine ────────────────────────────────────────────────────────────────────

def run_backtest(
    strategy: Strategy,
    data: list[BacktestRow],
    starting_bankroll: float = 1000.0,
    cfg: Settings | None = None,
) -> BacktestResult:
    """
    Replay `data` through `strategy` and return aggregate metrics.

    Contracts sized by the strategy; fills assumed at yes_ask (no slippage
    modelling beyond the assumed_slippage baked into the edge gate).

    Enters at most once per (ticker, side) -- mirrors the live Executor's
    PositionTracker guard (kbot.position_tracker), which this replay has no
    other equivalent of. Without it, a market sampled at fine candle
    granularity relative to its own lifetime (e.g. 1-minute candles on a
    15-minute KXBTC15M window -- ~14 rows for ONE real outcome) lets the
    same already-resolved bet get "entered" repeatedly across correlated
    rows, since consecutive candles of an already-trending market keep
    clearing the edge gate together. That inflates trade count and Sharpe
    on whatever's correlated with re-clearing the gate, not on real edge
    (2026-07 finding: an early KXBTC15M backtest showed Sharpe 6.1 and a
    severely bimodal calibration table -- 8% actual win rate in a 20-40%
    claimed-fair_p bucket alongside 85-92% actual in a 60-80% bucket --
    before this guard was added; both artifacts disappeared once repeated
    same-market entries stopped being double-counted).
    """
    cfg = cfg or _default_settings
    result = BacktestResult(starting_bankroll=starting_bankroll)
    bankroll = starting_bankroll
    last_date: str | None = None
    day_start_bankroll = starting_bankroll
    held: set[tuple[str, str]] = set()

    from datetime import datetime, timezone

    for row in data:
        date_str = datetime.fromtimestamp(row.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
        if date_str != last_date:
            if last_date is not None:
                daily_ret = (bankroll - day_start_bankroll) / day_start_bankroll
                result.daily_returns.append(daily_ret)
            day_start_bankroll = bankroll
            last_date = date_str

        if not _row_price_is_valid(row.yes_ask, row.no_ask):
            result.skipped_invalid_price_rows += 1
            continue

        book = OrderBook(
            ticker=row.ticker,
            # Synthetic depth: 1000 contracts a side, in integer hundredths.
            # The backtest never consults depth, so the figure only has to be
            # large enough not to look like an empty level.
            yes=[PriceLevel(price=row.yes_ask, quantity_units=1000 * UNITS_PER_CONTRACT)],
            no=[PriceLevel(price=row.no_ask, quantity_units=1000 * UNITS_PER_CONTRACT)],
        )

        if row.floor_strike is not None:
            # Series whose strike isn't ticker-encoded (KXBTC15M) need this
            # fed in before evaluate() can price the row at all -- cheap
            # and idempotent to call every row (see ingest_market_metadata).
            strategy.ingest_market_metadata(row.ticker, {"floor_strike": row.floor_strike})

        sizing = strategy.evaluate(
            book,
            btc_price=row.btc_price,
            btc_vol=row.btc_vol,
            bankroll=bankroll,
            current_time=row.timestamp,
        )

        for s in sizing:
            if not passes_edge_gate(s, cfg):
                result.skipped_ticks += 1
                continue

            position_key = (row.ticker, s.side.value)
            if position_key in held:
                result.skipped_ticks += 1
                continue

            fill_ask = row.yes_ask if s.side == Side.YES else row.no_ask

            # RULE R1 taker price floor (edge.py) -- already enforced on the
            # live execution path (execution.py) but was missing here, so the
            # backtest was crediting sub-15c taker fills that live trading
            # would never place (2026-07 calibration audit).
            if not passes_price_floor(fill_ask, row.ticker, cfg, is_maker=False):
                result.skipped_ticks += 1
                continue

            contracts = s.contracts
            cost = contracts * fill_ask
            if cost > bankroll:
                contracts = int(bankroll / fill_ask)
                cost = contracts * fill_ask
            if contracts <= 0:
                result.skipped_ticks += 1
                continue

            held.add(position_key)
            fee = cfg.fee_coefficient * contracts * fill_ask * (1.0 - fill_ask)

            # YES wins when resolved_yes; NO wins when not resolved_yes.
            won = row.resolved_yes if s.side == Side.YES else not row.resolved_yes
            if won:
                pnl = contracts * 1.0 - cost - fee
            else:
                pnl = -cost - fee

            bankroll += pnl
            result.total_trades += 1
            if pnl > 0:
                result.winning_trades += 1
            result.pnl_series.append(pnl)

            bucket = _find_bucket(result.price_buckets, fill_ask)
            if bucket is not None:
                bucket.trades += 1
                if pnl > 0:
                    bucket.wins += 1
                bucket.net_pnl += pnl

            result.trades.append(TradeRecord(
                ticker=row.ticker,
                side=s.side,
                fair_p=s.fair_p,
                ask=fill_ask,
                net_edge=s.net_edge_dollars,
                won=won,
                pnl=pnl,
            ))

            log.debug(
                "backtest.trade",
                ticker=row.ticker,
                side=s.side.value,
                contracts=contracts,
                ask=fill_ask,
                resolved_yes=row.resolved_yes,
                pnl=round(pnl, 4),
                bankroll=round(bankroll, 2),
            )

    # Final day return.
    if last_date is not None:
        result.daily_returns.append((bankroll - day_start_bankroll) / day_start_bankroll)

    log.info(
        "backtest.done",
        trades=result.total_trades,
        win_rate=round(result.win_rate, 3),
        total_pnl=round(result.total_return_pct, 2),
        sharpe=round(result.sharpe, 3),
        max_dd_pct=round(result.max_drawdown_pct, 4),
        skipped_invalid_price_rows=result.skipped_invalid_price_rows,
    )
    return result
