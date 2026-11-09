"""
Diagnostic only (no code/gate changes): vol-underestimation audit for the
KXBTC15M calibration miss.

1. For the actual backtest trade set, compares our_vol (EWMA+Parkinson
   fallback, what the model used) against implied_vol (back-solved from
   the market's own traded ask price) via a numerical fit -- generic
   across "above"/"below"/"range" market types.
2. sigma*sqrt(tau) distribution for the same trades.
3. Re-runs the backtest with vol_multiplier overridden to 2.0/2.5/3.0
   (via a scratch Settings object -- never touches the real config file
   or gate registry).
4. Same implied/our vol comparison on the KXBTC hourly trade set.

Run: python scripts/analyze_vol_calibration.py
"""
from __future__ import annotations

import csv
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

from scipy.optimize import minimize_scalar

from kbot.backtest import _FAIR_P_BUCKETS, load_csv, run_backtest
from kbot.config import settings as _default_settings
from kbot.edge import passes_edge_gate, passes_price_floor
from kbot.models.types import OrderBook, PriceLevel, Side
from kbot.strategy.btc_hourly import (
    BTCHourlyStrategy, _lognormal_prob, _lognormal_range_prob, _parse_ticker, _RANGE_HALF_WIDTH,
)


@dataclass
class TradeVolSample:
    ticker: str
    side: str
    direction: str
    ask: float
    spot: float
    strike: float  # midpoint for range markets
    t_years: float
    our_vol: float
    implied_vol: float | None


def _implied_vol(spot: float, direction: str, strike: float, t_years: float, ask: float, side: Side) -> float | None:
    """Back-solve sigma that reproduces `ask` for this trade's own side/direction."""
    def objective(sigma: float) -> float:
        if direction == "range":
            lower, upper = strike - _RANGE_HALF_WIDTH, strike + _RANGE_HALF_WIDTH
            model_p_yes = _lognormal_range_prob(spot, lower, upper, sigma, t_years)
        else:
            model_p_yes = _lognormal_prob(spot, strike, sigma, t_years, direction)
        model_ask = model_p_yes if side == Side.YES else 1.0 - model_p_yes
        return (model_ask - ask) ** 2

    lo, hi = 0.01, 5.0
    result = minimize_scalar(objective, bounds=(lo, hi), method="bounded")
    if not result.success:
        return None
    if result.x <= lo * 1.01 or result.x >= hi * 0.99:
        return None  # degenerate fit pinned at a bound -- not a real signal
    return float(result.x)


def collect_trades(csv_path: Path, strategy_factory) -> list[TradeVolSample]:
    """
    Replicate run_backtest's exact trade-selection logic (edge gate, price
    floor, one-entry-per-ticker+side position guard) so this analysis is
    over the SAME trade set the reported backtest numbers came from --
    not a resampled or differently-filtered set.
    """
    rows = load_csv(csv_path)
    strategy = strategy_factory()
    cfg = _default_settings
    held: set[tuple[str, str]] = set()
    samples: list[TradeVolSample] = []

    for row in rows:
        if row.floor_strike is not None:
            strategy.ingest_market_metadata(row.ticker, {"floor_strike": row.floor_strike})

        book = OrderBook(
            ticker=row.ticker,
            yes=[PriceLevel(price=row.yes_ask, quantity_units=1000 * 100)],
            no=[PriceLevel(price=row.no_ask, quantity_units=1000 * 100)],
        )
        sizing = strategy.evaluate(book, btc_price=row.btc_price, btc_vol=row.btc_vol, bankroll=1000.0, current_time=row.timestamp)

        for s in sizing:
            if not passes_edge_gate(s, cfg):
                continue
            key = (row.ticker, s.side.value)
            if key in held:
                continue
            fill_ask = row.yes_ask if s.side == Side.YES else row.no_ask
            if not passes_price_floor(fill_ask, row.ticker, cfg, is_maker=False):
                continue
            if s.contracts <= 0:
                continue
            held.add(key)

            parsed = _parse_ticker(row.ticker)
            raw_strike, expiry_ts, direction = parsed
            strike = raw_strike if raw_strike is not None else row.floor_strike
            t_years = (expiry_ts - row.timestamp) / (365.25 * 24 * 3600)

            implied = _implied_vol(row.btc_price, direction, strike, t_years, fill_ask, s.side)
            samples.append(TradeVolSample(
                ticker=row.ticker, side=s.side.value, direction=direction, ask=fill_ask,
                spot=row.btc_price, strike=strike, t_years=t_years, our_vol=row.btc_vol, implied_vol=implied,
            ))
    return samples


def _pctile(values: list[float], p: float) -> float:
    values = sorted(values)
    if not values:
        return float("nan")
    k = (p / 100.0) * (len(values) - 1)
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    return values[f] + (values[c] - values[f]) * (k - f)


def _describe(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label}: no data")
        return
    print(
        f"  {label}: n={len(values)} mean={statistics.mean(values):.4f} "
        f"median={statistics.median(values):.4f} p5={_pctile(values, 5):.4f} p95={_pctile(values, 95):.4f}"
    )


def part1_and_2_vol_audit(samples: list[TradeVolSample], label: str) -> None:
    print(f"\n=== {label}: VOL DISTRIBUTION AUDIT (part 1) ===")
    our_vols = [s.our_vol for s in samples]
    implied = [s.implied_vol for s in samples if s.implied_vol is not None]
    ratios = [s.implied_vol / s.our_vol for s in samples if s.implied_vol is not None and s.our_vol > 0]
    print(f"  total trades: {len(samples)}, implied_vol solvable for: {len(implied)}")
    _describe("our_vol (EWMA+Parkinson fallback)", our_vols)
    _describe("implied_vol (back-solved from ask)", implied)
    _describe("ratio implied_vol/our_vol", ratios)
    if ratios:
        median_ratio = statistics.median(ratios)
        print(f"  MEDIAN RATIO: {median_ratio:.3f} -> {'>1.3: vol underestimation IS a plausible root cause' if median_ratio > 1.3 else '<=1.3: vol underestimation alone does not explain the gap'}")

    print(f"\n=== {label}: SIGMA*SQRT(TAU) DISTRIBUTION (part 2) ===")
    sigma_sqrt_t = [s.our_vol * (s.t_years ** 0.5) for s in samples]
    _describe("sigma*sqrt(tau) [our_vol]", sigma_sqrt_t)
    below_001 = sum(1 for v in sigma_sqrt_t if v < 0.01)
    print(f"  fraction with sigma*sqrt(tau) < 0.01: {below_001}/{len(sigma_sqrt_t)} = {below_001/len(sigma_sqrt_t)*100:.1f}%")


def part3_vol_multiplier_sensitivity(csv_path: Path) -> None:
    """
    Recomputes btc_vol FROM SCRATCH (re-fetches Coinbase history, re-derives
    EWMA+Parkinson) for each candidate vol_multiplier -- avoids any
    approximation error from trying to algebraically "undo" the multiplier
    already baked into the CSV (which isn't exactly invertible wherever
    cfg.vol_floor was the binding constraint).
    """
    from kbot.config import Settings
    from kbot.vol import fallback_vol
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pull_history import fetch_coinbase_candles, realized_vol_at

    print("\n=== KXBTC15M: VOL MULTIPLIER SENSITIVITY (part 3) ===")
    rows = load_csv(csv_path)
    timestamps = [r.timestamp for r in rows]
    start_ts, end_ts = int(min(timestamps)) - 3600, int(max(timestamps)) + 3600
    print(f"  Re-fetching Coinbase BTC/USD history for [{start_ts}, {end_ts}]...")
    btc_history, ohlc_bars = fetch_coinbase_candles(start_ts, end_ts)
    print(f"  Got {len(btc_history)} BTC samples.")

    for multiplier in (1.5, 2.0, 2.5, 3.0):
        scratch_cfg = Settings(
            kalshi_key_id="test", dry_run=True,
            min_net_edge=_default_settings.min_net_edge,
            vol_multiplier=multiplier,
            vol_floor=_default_settings.vol_floor,
        )
        adjusted_rows = [
            row.__class__(
                timestamp=row.timestamp, btc_price=row.btc_price,
                btc_vol=realized_vol_at(btc_history, ohlc_bars, row.timestamp, scratch_cfg),
                ticker=row.ticker, yes_ask=row.yes_ask, no_ask=row.no_ask,
                resolved_yes=row.resolved_yes, floor_strike=row.floor_strike,
            )
            for row in rows
        ]

        strategy = BTCHourlyStrategy(scratch_cfg)
        result = run_backtest(strategy, adjusted_rows, starting_bankroll=1000.0, cfg=scratch_cfg)
        print(f"\n--- vol_multiplier={multiplier} (baseline is 1.5, the current gate config) ---")
        print(f"  trades={result.total_trades} win_rate={result.win_rate:.3f} pnl={sum(result.pnl_series):.2f} sharpe={result.sharpe:.3f}")
        report = result.calibration_report()
        bounds_by_label = {label: (lo, hi) for label, lo, hi in _FAIR_P_BUCKETS}
        max_miss_pp = 0.0
        for bucket in report:
            if bucket["trades"] > 0:
                lo, hi = bounds_by_label[bucket["label"]]
                bucket_mid = (lo + hi) / 2.0 * 100
                miss = abs(bucket["win_rate"] * 100 - bucket_mid)
                max_miss_pp = max(max_miss_pp, miss)
                print(f"    {bucket['label']:>8}: trades={bucket['trades']:>4} actual_win_rate={bucket['win_rate']*100:5.1f}%  (miss vs bucket mid: {miss:4.1f}pp)")
        print(f"  MAX MISS ACROSS BUCKETS: {max_miss_pp:.1f}pp -> {'WITHIN 10pp -- candidate fix' if max_miss_pp <= 10 else 'still > 10pp'}")


def main() -> None:
    samples_15m = collect_trades(Path("data/kxbtc15m_history.csv"), lambda: BTCHourlyStrategy(_default_settings))
    part1_and_2_vol_audit(samples_15m, "KXBTC15M")

    part3_vol_multiplier_sensitivity(Path("data/kxbtc15m_history.csv"))

    samples_hourly = collect_trades(Path("data/kxbtc_history.csv"), lambda: BTCHourlyStrategy(_default_settings))
    part1_and_2_vol_audit(samples_hourly, "KXBTC (hourly)")


if __name__ == "__main__":
    main()
