"""Tests for kbot.backtest — replay engine metrics."""

import math
import time

import pytest

from kbot.backtest import BacktestResult, BacktestRow, PriceBucketStats, run_backtest
from kbot.config import Settings
from kbot.strategy.btc_hourly import BTCHourlyStrategy


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env="demo",
        dry_run=True,
        min_net_edge=kwargs.get("min_net_edge", 0.01),
        kelly_fraction=0.25,
        fee_coefficient=0.07,
        assumed_slippage=0.005,
        max_position_pct=0.50,
        max_contracts_per_order=1000,
        no_trade_final_minutes=0,
        # These pre-date the 2026-07 calibration fix and use deliberately
        # far-OTM synthetic fixtures to exercise win/loss/bucket accounting
        # in isolation -- opt out of the near-ATM restriction so they keep
        # testing that, not the new filter (see test_run_backtest_near_atm_only_*).
        near_atm_only=kwargs.get("near_atm_only", False),
        # These pre-date the 2026-07 expiry-cutoff/tradeable-band fixes and
        # use synthetic fixtures (multi-day tickers, ask as low as 0.01) to
        # exercise win/loss/bucket accounting in isolation -- opt out so
        # they keep testing that, not the new filters.
        max_hours_to_expiry_for_entry=kwargs.get("max_hours_to_expiry_for_entry", 999_999.0),
        tradeable_band_low=kwargs.get("tradeable_band_low", 0.0001),
        tradeable_band_high=kwargs.get("tradeable_band_high", 0.9999),
    )


def _ticker_7d_from(ts: float, strike: int = 65000, mkt_type: str = "T") -> str:
    """Return a real-format KXBTC ticker expiring 7 days after ts."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt = (datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(days=7)).astimezone(et)
    return f"KXBTC-{dt.year-2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}-{mkt_type}{strike}"


# ── BacktestResult metrics ────────────────────────────────────────────────────

def test_win_rate_zero_trades():
    r = BacktestResult()
    assert r.win_rate == 0.0


def test_win_rate_calculation():
    r = BacktestResult(total_trades=4, winning_trades=3)
    assert r.win_rate == 0.75


def test_total_return():
    r = BacktestResult(pnl_series=[10.0, -5.0, 8.0])
    assert abs(r.total_return_pct - 13.0) < 1e-9


def test_max_drawdown_zero():
    r = BacktestResult(pnl_series=[5.0, 3.0, 2.0])
    assert r.max_drawdown_pct == 0.0


def test_max_drawdown_calculated():
    # Peak cumulative P&L = 10, trough = 6, starting_bankroll = 10 → dd = 4/10 = 0.4
    r = BacktestResult(pnl_series=[10.0, -4.0], starting_bankroll=10.0)
    assert abs(r.max_drawdown_pct - 0.4) < 1e-9


def test_max_drawdown_all_losses():
    # All losses: peak stays 0, cum reaches -50 → dd = 50/1000 = 0.05
    r = BacktestResult(pnl_series=[-25.0, -25.0], starting_bankroll=1000.0)
    assert abs(r.max_drawdown_pct - 0.05) < 1e-9


def test_sharpe_single_return():
    r = BacktestResult(daily_returns=[0.01])
    assert r.sharpe == 0.0  # need at least 2 observations


def test_sharpe_constant_returns():
    # Constant return → std=0 → Sharpe=0 (no division by zero)
    r = BacktestResult(daily_returns=[0.01] * 30)
    assert r.sharpe == 0.0


def test_sharpe_positive_for_good_returns():
    # Strongly positive daily returns → positive Sharpe
    r = BacktestResult(daily_returns=[0.01] * 20 + [-0.001] * 5)
    assert r.sharpe > 0


# ── run_backtest ──────────────────────────────────────────────────────────────

def _make_rows(n: int, btc_price: float, yes_ask: float, resolved_yes: bool) -> list[BacktestRow]:
    base_ts = time.time()
    rows = []
    for i in range(n):
        ts = base_ts + i * 60      # 1 minute apart
        rows.append(BacktestRow(
            timestamp=ts,
            btc_price=btc_price,
            btc_vol=0.80,
            ticker=_ticker_7d_from(ts),       # T65000 format
            yes_ask=yes_ask,
            no_ask=round(1.0 - yes_ask - 0.02, 4),   # small spread
            resolved_yes=resolved_yes,
        ))
    return rows


def test_backtest_all_wins():
    # T65000 = YES if BTC < $65k.  btc=50000 → P(below 65k) ≈ 0.99 >> ask=0.50.
    # spot=50000 is |ln(50k/65k)| = 0.26 = 2.4σ on a 7-day 0.80-vol lognormal,
    # which is within the 3σ far-OTM filter so the market is evaluated normally.
    rows = _make_rows(5, btc_price=50000, yes_ask=0.50, resolved_yes=True)
    strategy = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    result = run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg())
    assert result.total_trades > 0
    assert result.win_rate == 1.0


def test_backtest_all_losses():
    # Same setup but resolved_yes=False (BTC ended above $65k despite starting at $50k).
    # Strategy buys YES; YES does not resolve → all losses.
    rows = _make_rows(5, btc_price=50000, yes_ask=0.50, resolved_yes=False)
    strategy = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    result = run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg())
    assert result.total_trades > 0
    assert result.win_rate == 0.0


def test_backtest_no_signal_skips():
    # ATM (P=0.5): yes_ask=0.52, no_ask=0.46 (via _make_rows formula).
    # YES edge: 0.5-0.52 - fee(0.52) - slip ≈ -0.043 < 0.04 → skip
    # NO  edge: 0.5-0.46 - fee(0.46) - slip ≈ +0.018 < 0.04 → skip
    # → neither side has enough edge; no trades.
    rows = _make_rows(5, btc_price=65000, yes_ask=0.52, resolved_yes=True)
    strategy = BTCHourlyStrategy(_cfg(min_net_edge=0.04))
    result = run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg(min_net_edge=0.04))
    assert result.total_trades == 0


def test_backtest_empty_data():
    strategy = BTCHourlyStrategy(_cfg())
    result = run_backtest(strategy, [], starting_bankroll=1000.0, cfg=_cfg())
    assert result.total_trades == 0
    assert result.pnl_series == []


# ── RULE R4 fee-tier simulator ───────────────────────────────────────────────────

def test_price_bucket_stats_win_rate():
    bucket = PriceBucketStats(label="30-50c", low=0.30, high=0.50, trades=4, wins=3, net_pnl=10.0)
    assert bucket.win_rate == 0.75


def test_price_bucket_stats_win_rate_zero_trades():
    bucket = PriceBucketStats(label="30-50c", low=0.30, high=0.50)
    assert bucket.win_rate == 0.0


def test_breakeven_win_rate_equals_price_plus_fee():
    cfg = _cfg()  # fee_coefficient=0.07, assumed_slippage baked into edge.py, not here
    bucket = PriceBucketStats(label="50-70c", low=0.50, high=0.70)
    be_taker, be_maker = bucket.breakeven_win_rate(cfg)

    mid = 0.60
    from kbot.edge import fee_per_contract, maker_fee_dollars_for_market
    expected_taker = mid + fee_per_contract(mid, cfg)
    expected_maker = mid + maker_fee_dollars_for_market("", mid, cfg)
    assert be_taker == pytest.approx(expected_taker)
    assert be_maker == pytest.approx(expected_maker)


def test_breakeven_win_rate_taker_above_maker_when_maker_fee_lower():
    # Standard markets: maker_fee_default=0.0 < fee_coefficient=0.07 -> maker
    # breakeven is lower (cheaper to get to breakeven as a maker).
    cfg = _cfg()
    bucket = PriceBucketStats(label="50-70c", low=0.50, high=0.70)
    be_taker, be_maker = bucket.breakeven_win_rate(cfg)
    assert be_maker < be_taker


def test_fee_tier_report_has_six_buckets_in_order():
    result = BacktestResult()
    report = result.fee_tier_report(_cfg())
    labels = [r["label"] for r in report]
    assert labels == ["0-15c", "15-30c", "30-50c", "50-70c", "70-85c", "85-99c"]


def test_fee_tier_report_empty_backtest_has_zero_trades_everywhere():
    result = BacktestResult()
    report = result.fee_tier_report(_cfg())
    assert all(row["trades"] == 0 for row in report)
    assert all(row["net_pnl"] == 0.0 for row in report)


def test_run_backtest_populates_price_bucket_for_traded_price():
    # yes_ask=0.50 falls in the "50-70c" bucket ([0.50, 0.70)).
    rows = _make_rows(5, btc_price=50000, yes_ask=0.50, resolved_yes=True)
    strategy = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    result = run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg())

    report = {row["label"]: row for row in result.fee_tier_report(_cfg())}
    assert report["50-70c"]["trades"] == result.total_trades
    assert report["50-70c"]["win_rate"] == 1.0
    assert report["50-70c"]["net_pnl"] > 0
    # No trades leaked into unrelated buckets.
    assert report["0-15c"]["trades"] == 0
    assert report["85-99c"]["trades"] == 0


def test_run_backtest_bucket_net_pnl_sums_to_total():
    rows = _make_rows(5, btc_price=50000, yes_ask=0.50, resolved_yes=True)
    strategy = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    result = run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg())

    bucket_pnl_total = sum(b.net_pnl for b in result.price_buckets)
    assert bucket_pnl_total == pytest.approx(sum(result.pnl_series))


# ── Calibration report (2026-07 calibration audit) ────────────────────────────

def test_calibration_report_has_ten_buckets_in_order():
    result = BacktestResult()
    report = result.calibration_report()
    labels = [r["label"] for r in report]
    assert labels == [
        "0-10%", "10-20%", "20-30%", "30-40%", "40-50%",
        "50-60%", "60-70%", "70-80%", "80-90%", "90-100%",
    ]


def test_calibration_report_empty_backtest_has_zero_trades_everywhere():
    result = BacktestResult()
    report = result.calibration_report()
    assert all(row["trades"] == 0 for row in report)
    assert all(row["win_rate"] == 0.0 for row in report)


def test_run_backtest_populates_calibration_bucket_for_traded_fair_p():
    # T65000 "below", spot=50000, 7d, 0.8 vol -> fair_p ≈ 0.99 -> "90-100%" bucket.
    rows = _make_rows(5, btc_price=50000, yes_ask=0.50, resolved_yes=True)
    strategy = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    result = run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg())

    report = {row["label"]: row for row in result.calibration_report()}
    assert report["90-100%"]["trades"] == result.total_trades
    assert report["90-100%"]["win_rate"] == 1.0
    assert report["90-100%"]["mean_ask"] == pytest.approx(0.50)
    assert report["90-100%"]["mean_net_edge"] > 0
    # No trades leaked into unrelated buckets.
    assert report["0-10%"]["trades"] == 0
    assert report["40-50%"]["trades"] == 0


# ── Position-tracking guard (2026-07: no repeated entries per ticker+side) ──
# Without this, a market sampled at fine candle granularity relative to its
# own lifetime (e.g. minute candles on a 15-minute KXBTC15M window) let the
# same already-resolved bet get "entered" repeatedly across correlated
# rows -- confirmed live: an early KXBTC15M backtest showed Sharpe 6.1 and
# 5190 "trades" against only 1326 real markets; after this guard, 1783
# trades and Sharpe 13.25 (still high, but no longer inflated by re-entry).

class _AlwaysSignalsStrategy:
    """Emits the same fixed SizingResult for every ticker+side it's asked about, forever."""
    name = "always_signals"
    series_tickers: list = []
    required_feeds: list = []

    def owns_ticker(self, ticker: str) -> bool:
        return True

    def ingest_market_metadata(self, ticker: str, metadata: dict) -> None:
        pass

    def evaluate(self, book, *, btc_price=None, btc_vol=None, bankroll, current_time=None):
        from kbot.models.types import Side, SizingResult
        return [SizingResult(
            ticker=book.ticker, side=Side.YES, contracts=10, price_dollars=0.30,
            fair_p=0.60, net_edge_dollars=0.20, kelly_fraction_used=0.1,
        )]


def _rows_same_ticker(n: int, ticker: str = "TICK-A", resolved_yes: bool = True) -> list[BacktestRow]:
    base_ts = time.time()
    return [
        BacktestRow(
            timestamp=base_ts + i * 60, btc_price=65000.0, btc_vol=0.8, ticker=ticker,
            yes_ask=0.30, no_ask=0.68, resolved_yes=resolved_yes,
        )
        for i in range(n)
    ]


def test_run_backtest_enters_same_ticker_side_only_once():
    rows = _rows_same_ticker(10)
    result = run_backtest(_AlwaysSignalsStrategy(), rows, starting_bankroll=1000.0, cfg=_cfg())
    assert result.total_trades == 1
    assert result.skipped_ticks == 9


def test_run_backtest_different_tickers_each_get_own_entry():
    rows = _rows_same_ticker(5, ticker="TICK-A") + _rows_same_ticker(5, ticker="TICK-B")
    result = run_backtest(_AlwaysSignalsStrategy(), rows, starting_bankroll=1000.0, cfg=_cfg())
    assert result.total_trades == 2


def test_run_backtest_position_guard_does_not_affect_pnl_sign():
    # Repeated entries on a correctly-called market shouldn't multiply the
    # P&L either -- only one contract count's worth of P&L should be booked.
    rows = _rows_same_ticker(10, resolved_yes=True)
    result = run_backtest(_AlwaysSignalsStrategy(), rows, starting_bankroll=1000.0, cfg=_cfg())
    assert result.total_trades == 1
    assert result.pnl_series[0] > 0  # YES resolved True, single win booked once


# ── floor_strike wiring (KXBTC15M: strike not encoded in the ticker) ────────

class _RecordingStrategy:
    """Records every ingest_market_metadata() call; never signals."""
    name = "recording"
    series_tickers: list = []
    required_feeds: list = []

    def __init__(self):
        self.ingested: list[tuple[str, dict]] = []

    def owns_ticker(self, ticker: str) -> bool:
        return True

    def ingest_market_metadata(self, ticker: str, metadata: dict) -> None:
        self.ingested.append((ticker, dict(metadata)))

    def evaluate(self, book, *, btc_price=None, btc_vol=None, bankroll, current_time=None):
        return []


def test_run_backtest_ingests_floor_strike_when_present():
    strategy = _RecordingStrategy()
    rows = [BacktestRow(
        timestamp=time.time(), btc_price=65000.0, btc_vol=0.8, ticker="KXBTC15M-26JUL060115-15",
        yes_ask=0.30, no_ask=0.68, resolved_yes=True, floor_strike=63202.05,
    )]
    run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg())
    assert strategy.ingested == [("KXBTC15M-26JUL060115-15", {"floor_strike": 63202.05})]


def test_run_backtest_does_not_ingest_when_floor_strike_is_none():
    strategy = _RecordingStrategy()
    rows = [BacktestRow(
        timestamp=time.time(), btc_price=65000.0, btc_vol=0.8, ticker="KXBTC-26JUL0317-T48000",
        yes_ask=0.30, no_ask=0.68, resolved_yes=True, floor_strike=None,
    )]
    run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg())
    assert strategy.ingested == []


def test_run_backtest_prices_15m_market_after_floor_strike_ingested():
    strategy = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now_ts = time.time()
    dt = (datetime.fromtimestamp(now_ts, tz=timezone.utc) + timedelta(minutes=10)).astimezone(et)
    # Round UP (not down) to the next quarter-hour -- rounding down can land
    # the computed close time BEFORE now_ts depending on the current wall-clock
    # minute, making the ticker already-expired and flaking the test.
    minutes_since_epoch = int(dt.timestamp() // 60)
    remainder = minutes_since_epoch % 15
    if remainder != 0:
        dt = dt + timedelta(minutes=15 - remainder)
    dt = dt.replace(second=0, microsecond=0)
    mm = f"{dt.minute:02d}"
    ticker = f"KXBTC15M-{dt.year-2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}{mm}-{mm}"

    rows = [BacktestRow(
        # spot == floor_strike (near-ATM, well inside the far-OTM cutoff even
        # at 10 minutes to expiry) -> fair_p ~= 0.5 -> the model must have
        # actually priced this row (evaluate() didn't just no-op for lack of
        # a strike), producing a genuine edge_below_min skip, not silence.
        timestamp=now_ts, btc_price=65000.0, btc_vol=0.8, ticker=ticker,
        yes_ask=0.30, no_ask=0.68, resolved_yes=True, floor_strike=65000.0,
    )]
    result = run_backtest(strategy, rows, starting_bankroll=1000.0, cfg=_cfg(min_net_edge=0.001))
    assert result.total_trades > 0


# ── load_csv (floor_strike column) ───────────────────────────────────────────

def test_load_csv_reads_floor_strike_column(tmp_path):
    from kbot.backtest import load_csv
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "timestamp,btc_price,btc_vol,ticker,yes_ask,no_ask,resolved_yes,floor_strike\n"
        "1000,65000,0.8,KXBTC15M-26JUL060115-15,0.30,0.68,1,63202.05\n"
    )
    rows = load_csv(csv_path)
    assert len(rows) == 1
    assert rows[0].floor_strike == pytest.approx(63202.05)


def test_load_csv_blank_floor_strike_is_none(tmp_path):
    from kbot.backtest import load_csv
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "timestamp,btc_price,btc_vol,ticker,yes_ask,no_ask,resolved_yes,floor_strike\n"
        "1000,65000,0.8,KXBTC-26JUL0317-T48000,0.30,0.68,1,\n"
    )
    rows = load_csv(csv_path)
    assert rows[0].floor_strike is None


def test_load_csv_missing_floor_strike_column_is_none(tmp_path):
    # Backward compat: older CSVs without the column at all must still load.
    from kbot.backtest import load_csv
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "timestamp,btc_price,btc_vol,ticker,yes_ask,no_ask,resolved_yes\n"
        "1000,65000,0.8,KXBTC-26JUL0317-T48000,0.30,0.68,1\n"
    )
    rows = load_csv(csv_path)
    assert rows[0].floor_strike is None
