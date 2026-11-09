"""Tests for kbot.strategy.btc_hourly — ticker parsing and lognormal model."""

import math
from datetime import datetime, timezone

import pytest

from kbot.config import Settings
from kbot.models.types import OrderBook, PriceLevel, Side
from kbot.strategy.btc_hourly import (
    BTCHourlyStrategy,
    _SIGMA_CUTOFF,
    _is_far_otm,
    _lognormal_prob,
    effective_t_years,
    _lognormal_range_prob,
    _parse_ticker,
)


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env="demo",
        dry_run=True,
        min_net_edge=kwargs.get("min_net_edge", 0.04),
        kelly_fraction=kwargs.get("kelly_fraction", 0.25),
        max_position_pct=kwargs.get("max_position_pct", 0.05),
        max_contracts_per_order=kwargs.get("max_contracts_per_order", 100),
        no_trade_final_minutes=kwargs.get("no_trade_final_minutes", 5),
        fee_coefficient=0.07,
        assumed_slippage=0.005,
        # Most tests below use deliberately far-OTM fixtures to exercise the
        # edge-computation and far-OTM-guard code paths in isolation -- opt
        # out of the 2026-07 near-ATM restriction so they keep testing that,
        # not the new filter (see the dedicated near_atm_only tests).
        near_atm_only=kwargs.get("near_atm_only", False),
        near_atm_fair_p_min=kwargs.get("near_atm_fair_p_min", 0.25),
        near_atm_fair_p_max=kwargs.get("near_atm_fair_p_max", 0.75),
        # 2026-07 low-bucket win-prob floor. Disabled (0.0) by default here so
        # the many pre-existing fixtures that legitimately trade a sub-50%
        # side (e.g. cheap B-range YES trades) keep testing their own code
        # paths, not this filter -- see the dedicated min_side_win_prob tests.
        min_side_win_prob=kwargs.get("min_side_win_prob", 0.0),
        # Most tests below use multi-day tickers (days_ahead=7/14) to test
        # unrelated code paths (edge computation, far-OTM guard) -- opt out
        # of the 2026-07 expiry-cutoff default (6h) so they keep testing
        # that, not the new filter (see the dedicated expiry-cutoff tests).
        max_hours_to_expiry_for_entry=kwargs.get("max_hours_to_expiry_for_entry", 999_999.0),
        tradeable_band_low=kwargs.get("tradeable_band_low", 0.0001),
        tradeable_band_high=kwargs.get("tradeable_band_high", 0.9999),
        vol_window_seconds_hourly=kwargs.get("vol_window_seconds_hourly", 3600.0),
        vol_window_seconds_15m=kwargs.get("vol_window_seconds_15m", 900.0),
        vol_multiplier_15m=kwargs.get("vol_multiplier_15m", 1.0),
    )


# ── _parse_ticker (real KXBTC format) ────────────────────────────────────────

def test_parse_bottom_t_ticker():
    # T48000 = YES if BTC < $48,000 (low catch-all)
    # KXBTC-26JUL0317: Jul 3, 2026 at 17:00 EDT = 21:00 UTC
    result = _parse_ticker("KXBTC-26JUL0317-T48000")
    assert result is not None
    strike, expiry_ts, direction = result
    assert direction == "below"
    assert strike == 48000.0
    dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
    assert dt.year == 2026 and dt.month == 7 and dt.day == 3 and dt.hour == 21


def test_parse_top_t_ticker():
    # T71999.99 = YES if BTC >= $72,000 (high catch-all)
    result = _parse_ticker("KXBTC-26JUL0317-T71999.99")
    assert result is not None
    strike, _, direction = result
    assert direction == "above"
    assert strike == 72000.0   # ceil(71999.99)


def test_parse_b_range_ticker():
    # B59750 = YES if BTC in [$59,500, $60,000)  — midpoint 59750, ±250
    result = _parse_ticker("KXBTC-26JUL0317-B59750")
    assert result is not None
    strike, _, direction = result
    assert direction == "range"
    assert strike == 59750.0


def test_parse_winter_event():
    # KXBTC-26JAN1501 = Jan 15, 2026 at 01:00 EST (UTC-5) = 06:00 UTC
    result = _parse_ticker("KXBTC-26JAN1501-T45000")
    assert result is not None
    _, expiry_ts, _ = result
    dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
    assert dt.year == 2026 and dt.month == 1 and dt.day == 15 and dt.hour == 6


def test_parse_case_insensitive():
    assert _parse_ticker("kxbtc-26jul0317-t48000") is not None


def test_parse_invalid_returns_none():
    assert _parse_ticker("BTC-26JUL0317-T48000") is None     # wrong prefix
    assert _parse_ticker("KXBTC-26XXX0317-T48000") is None   # invalid month
    assert _parse_ticker("KXBTC-26JUL0317-X48000") is None   # invalid type char
    assert _parse_ticker("") is None


# ── _parse_ticker (real KXBTC15M format, verified live 2026-07) ──────────────

def test_parse_15m_ticker_strike_is_none():
    # floor_strike is a dynamic per-window value, never encoded in the
    # ticker -- see this module's docstring. Callers must resolve it via
    # BTCHourlyStrategy._strike_cache / ingest_market_metadata.
    result = _parse_ticker("KXBTC15M-26JUL060115-15")
    assert result is not None
    strike, _expiry_ts, direction = result
    assert strike is None
    assert direction == "above"


def test_parse_15m_ticker_expiry_matches_close_time_not_expiration_time():
    # Verified live: KXBTC15M-26JUL060115-15 has close_time
    # 2026-07-06T05:15:00Z (01:15 EDT) -- NOT expiration_time
    # (2026-07-13T05:15:00Z, a decoy ~7-day-out SLA deadline field).
    result = _parse_ticker("KXBTC15M-26JUL060115-15")
    assert result is not None
    _, expiry_ts, _ = result
    dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 6, 5, 15)


def test_parse_15m_ticker_crosses_day_boundary_correctly():
    # Verified live: KXBTC15M-26JUL070000-00 has close_time
    # 2026-07-07T04:00:00Z (00:00 EDT July 7 -- midnight rollover from the
    # ticker's own "07" day component landing exactly on the UTC boundary).
    result = _parse_ticker("KXBTC15M-26JUL070000-00")
    assert result is not None
    _, expiry_ts, _ = result
    dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 7, 4, 0)


def test_parse_15m_ticker_all_quarter_hour_suffixes():
    for minute in ("00", "15", "30", "45"):
        result = _parse_ticker(f"KXBTC15M-26JUL0612{minute}-{minute}")
        assert result is not None, minute
        strike, _, direction = result
        assert strike is None
        assert direction == "above"


def test_parse_15m_case_insensitive():
    assert _parse_ticker("kxbtc15m-26jul060115-15") is not None


def test_parse_15m_invalid_returns_none():
    assert _parse_ticker("KXBTC15M-26XXX060115-15") is None   # invalid month
    assert _parse_ticker("KXBTC15M-26JUL06011-15") is None    # missing a digit
    assert _parse_ticker("NOTKXBTC15M-26JUL060115-15") is None


def test_parse_15m_does_not_match_hourly_regex_and_vice_versa():
    # The two formats must never cross-match -- KXBTC15M's extra "series"
    # suffix ("15M") means the hourly regex's ^KXBTC- anchor should reject it.
    assert _parse_ticker("KXBTC15M-26JUL060115-15")[0] is None   # matched by the 15m regex
    hourly = _parse_ticker("KXBTC-26JUL0317-T48000")
    assert hourly[0] == 48000.0   # matched by the hourly regex, strike present


# ── effective_t_years (2026-08 settlement-object fix) ─────────────────────────

_SPY = 365.25 * 24 * 3600


def test_effective_t_years_is_t_minus_two_thirds_tau():
    """Var of a uniform average over the final tau = sigma^2 * (T - 2*tau/3)."""
    T = 900.0 / _SPY
    assert effective_t_years(T, 60.0) * _SPY == pytest.approx(860.0)
    assert effective_t_years(3600.0 / _SPY, 60.0) * _SPY == pytest.approx(3560.0)


def test_effective_t_years_disabled_by_zero_avg_seconds():
    """avg_seconds=0 is point settlement -- the pre-2026-08 behaviour."""
    T = 900.0 / _SPY
    assert effective_t_years(T, 0.0) == T


def test_effective_t_years_is_continuous_at_t_equals_tau():
    """Both branches must agree at T = tau, where each gives tau/3."""
    tau = 60.0
    T = tau / _SPY
    assert effective_t_years(T, tau) * _SPY == pytest.approx(tau / 3.0)


def test_effective_t_years_stays_positive_inside_the_averaging_window():
    """
    The regression this exists for: the naive T - (2/3)*tau goes NON-POSITIVE
    at T = 40s, which would make sigma*sqrt(T_eff) zero and hand the caller
    fair_p of exactly 0 or 1 -- unbounded fake edge at the moment the model is
    least trustworthy. The T < tau branch must stay strictly positive.
    """
    for secs in (59.0, 40.0, 20.0, 1.0):
        assert effective_t_years(secs / _SPY, 60.0) > 0.0


def test_effective_t_years_zero_at_expiry():
    assert effective_t_years(0.0, 60.0) == 0.0
    assert effective_t_years(-1.0, 60.0) == 0.0


def test_settlement_average_moves_fair_p_away_from_half():
    """
    An average is less variable than a point value, so the corrected model is
    MORE confident, not less. Direction matters: this is the same direction as
    the vol-floor fix, which is why the two compound rather than cancel.
    """
    spot, strike, sigma = 60_000.0, 59_900.0, 0.20
    t_years = 900.0 / _SPY
    point = _lognormal_prob(spot, strike, sigma, t_years, "above")
    averaged = _lognormal_prob(spot, strike, sigma, t_years, "above", avg_seconds=60.0)
    assert point > 0.5
    assert averaged > point


def test_settlement_average_is_symmetric_for_below():
    spot, strike, sigma = 60_000.0, 60_100.0, 0.20
    t_years = 900.0 / _SPY
    p_above = _lognormal_prob(spot, strike, sigma, t_years, "above", avg_seconds=60.0)
    p_below = _lognormal_prob(spot, strike, sigma, t_years, "below", avg_seconds=60.0)
    assert p_above + p_below == pytest.approx(1.0)


def test_settlement_average_correction_grows_as_expiry_approaches():
    """
    The correction is 2.2% of sigma at 15 min but 6.9% at 5 min -- it is not
    flat in T, and a fixed "shave 2% off sigma" would be wrong at both ends.

    Stated on sqrt(T_eff/T), not on fair_p: the effect on fair_p ALSO depends
    on where the strike sits, because Phi saturates. At a fixed dollar strike
    offset the near-expiry contract is many sigma out and its fair_p barely
    moves however much sigma shrinks; the probability impact is largest at
    moderate |d|, which -- given cfg.near_atm_only -- is exactly the region
    this bot trades.
    """
    def sigma_factor(secs: float) -> float:
        t = secs / _SPY
        return (effective_t_years(t, 60.0) / t) ** 0.5

    assert sigma_factor(3600.0) == pytest.approx(0.9944, abs=1e-4)
    assert sigma_factor(900.0) == pytest.approx(0.9775, abs=1e-4)
    assert sigma_factor(300.0) == pytest.approx(0.9309, abs=1e-4)
    assert sigma_factor(300.0) < sigma_factor(900.0) < sigma_factor(3600.0) < 1.0


def test_range_prob_settlement_average_narrows_the_bracket_probability():
    """
    Less terminal variance means MORE mass inside a bracket straddling spot --
    the range market's mirror of the T-market result above.
    """
    t_years = 900.0 / _SPY
    point = _lognormal_range_prob(60_000.0, 59_750.0, 60_250.0, 0.20, t_years)
    averaged = _lognormal_range_prob(
        60_000.0, 59_750.0, 60_250.0, 0.20, t_years, avg_seconds=60.0,
    )
    assert averaged > point


def test_pricing_defaults_to_point_settlement():
    """
    The default MUST stay 0.0 so every existing caller (and every stored
    backtest CSV) keeps its pre-2026-08 meaning; only evaluate() opts in, via
    cfg.settlement_avg_seconds.
    """
    t_years = 900.0 / _SPY
    assert _lognormal_prob(60_000.0, 59_900.0, 0.20, t_years, "above") == pytest.approx(
        _lognormal_prob(60_000.0, 59_900.0, 0.20, t_years, "above", avg_seconds=0.0)
    )


# ── vol_floor series scoping (2026-08) ────────────────────────────────────────

def test_vol_floor_is_series_scoped():
    """
    The 15-minute series must get its own floor. A shared 0.35 bound on the
    large majority of 15-minute evaluations, which made sigma a constant and
    fair_p a near-constant ~0.5.
    """
    strat = BTCHourlyStrategy()
    assert strat.vol_floor("KXBTC15M-26JUL060115-15") == strat._cfg.vol_floor_15m
    assert strat.vol_floor("KXBTC-26JUL0317-T48000") is None


def test_fifteen_minute_vol_floor_is_below_the_hourly_one():
    """
    A floor guards an estimator only while it sits BELOW that estimator's low
    tail, and the 900s estimator's tail is lower than the 3600s one's. If this
    ever inverts, the 15-minute floor is back to being the estimate.
    """
    strat = BTCHourlyStrategy()
    assert strat._cfg.vol_floor_15m < strat._cfg.vol_floor


# ── _lognormal_prob ───────────────────────────────────────────────────────────

def test_prob_50_when_atm():
    # S == K → d=0 → N(0) = 0.5
    p = _lognormal_prob(50000, 50000, 0.80, 1.0 / 52, "above")
    assert abs(p - 0.5) < 1e-6


def test_prob_above_50_when_spot_above_strike():
    p = _lognormal_prob(70000, 65000, 0.80, 1.0 / 52, "above")
    assert p > 0.5


def test_prob_below_50_when_spot_below_strike():
    p = _lognormal_prob(60000, 65000, 0.80, 1.0 / 52, "above")
    assert p < 0.5


def test_prob_below_ticker_is_complement_of_above():
    p_above = _lognormal_prob(70000, 65000, 0.80, 1.0 / 52, "above")
    p_below = _lognormal_prob(70000, 65000, 0.80, 1.0 / 52, "below")
    assert abs(p_above + p_below - 1.0) < 1e-9


def test_prob_degenerate_returns_half():
    p = _lognormal_prob(65000, 65000, 0.80, 0.0, "above")
    assert p == 0.5


def test_prob_vol_zero_degenerate():
    p = _lognormal_prob(65000, 65000, 0.0, 1.0, "above")
    assert p == 0.5


# ── _lognormal_range_prob ─────────────────────────────────────────────────────

def test_range_prob_positive_and_bounded():
    # ATM bracket: spot near midpoint → should give some positive probability
    p = _lognormal_range_prob(59750, 59500, 60000, 0.8, 14 / 365.25)
    assert 0 < p < 1


def test_range_prob_near_bucket_greater_than_far_bucket():
    spot = 60000.0
    vol, t = 0.8, 14 / 365.25
    p_near = _lognormal_range_prob(spot, spot - 250, spot + 250, vol, t)
    p_far  = _lognormal_range_prob(spot, spot - 5000, spot - 4500, vol, t)
    assert p_near > p_far


def test_range_prob_degenerate():
    assert _lognormal_range_prob(60000, 60000, 59000, 0.8, 1.0) == 0.0   # upper < lower
    assert _lognormal_range_prob(60000, 59500, 60000, 0.8, 0.0) == 0.0   # t=0


# ── BTCHourlyStrategy.evaluate ────────────────────────────────────────────────

def _book(yes_ask: float, no_ask: float, ticker: str) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes_ask, quantity_units=50000)],
        no=[PriceLevel(price=no_ask, quantity_units=50000)],
    )


def _future_ticker(days_ahead: int = 7, strike: int = 65000, mkt_type: str = "T") -> str:
    """Return a ticker in real KXBTC format expiring ~days_ahead days from now."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt = (datetime.now(tz=timezone.utc) + timedelta(days=days_ahead)).astimezone(et)
    return f"KXBTC-{dt.year-2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}-{mkt_type}{strike}"


def _ticker_hours_from(hours_ahead: float, strike: int = 65000, mkt_type: str = "T") -> str:
    """Return a ticker in real KXBTC format expiring ~hours_ahead hours from now."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt = (datetime.now(tz=timezone.utc) + timedelta(hours=hours_ahead)).astimezone(et)
    return f"KXBTC-{dt.year-2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}-{mkt_type}{strike}"


def test_evaluate_returns_empty_without_btc_data():
    s = BTCHourlyStrategy(_cfg())
    book = _book(0.60, 0.40, _future_ticker())
    result = s.evaluate(book, btc_price=None, btc_vol=0.8, bankroll=1000.0)
    assert result == []


def test_evaluate_returns_empty_without_btc_vol():
    s = BTCHourlyStrategy(_cfg())
    book = _book(0.60, 0.40, _future_ticker())
    result = s.evaluate(book, btc_price=67000, btc_vol=None, bankroll=1000.0)
    assert result == []


def test_evaluate_skips_unparseable_ticker():
    s = BTCHourlyStrategy(_cfg())
    book = _book(0.60, 0.40, "NONSENSE-TICKER")
    result = s.evaluate(book, btc_price=67000, btc_vol=0.8, bankroll=1000.0)
    assert result == []


def test_evaluate_skips_near_expiry():
    # Expired ticker: yesterday's date/hour in ET → t_minutes < 0 < no_trade_final_minutes.
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt_et = (datetime.now(tz=timezone.utc) - timedelta(days=1)).astimezone(et)
    ticker = (
        f"KXBTC-{dt_et.year-2000:02d}{dt_et.strftime('%b').upper()}"
        f"{dt_et.day:02d}{dt_et.hour:02d}-T65000"
    )
    s = BTCHourlyStrategy(_cfg(no_trade_final_minutes=5))
    book = _book(0.60, 0.40, ticker)
    result = s.evaluate(book, btc_price=67000, btc_vol=0.8, bankroll=1000.0)
    assert len(result) == 1
    assert result[0].contracts == 0
    assert "expiry" in (result[0].skip_reason or "")


def test_evaluate_produces_signal_with_positive_edge():
    # T65000 = YES if BTC < $65k.  spot=50000 → P(below 65k) ≈ 0.95 >> yes_ask=0.60.
    # spot=50000 vs strike=65000 is |ln(50000/65000)| = 0.26, which is 1.68σ on a
    # 14-day 0.8-vol lognormal — well within the 3σ far-OTM filter.
    ticker = _future_ticker(days_ahead=14, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01, kelly_fraction=0.25))
    book = _book(yes_ask=0.60, no_ask=0.40, ticker=ticker)
    result = s.evaluate(book, btc_price=50000, btc_vol=0.8, bankroll=1000.0)
    assert len(result) == 1
    assert result[0].contracts > 0
    assert result[0].side == Side.YES
    assert result[0].net_edge_dollars > 0


def test_evaluate_skips_when_edge_below_min():
    # T65000 ATM (spot=65000): P(below)=0.5; both asks just above fair → both skip.
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.04))
    book = _book(yes_ask=0.52, no_ask=0.52, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    assert len(result) == 1
    assert result[0].contracts == 0
    assert result[0].skip_reason == "edge_below_min"


def test_evaluate_produces_no_signal_when_spot_above_t_strike():
    # T65000 = YES if BTC < $65k.  spot=80000 >> strike → P(below) ≈ 0.07.
    # YES is overpriced at 0.40; NO at 0.50 is massively underpriced → buy NO.
    ticker = _future_ticker(days_ahead=14, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01, kelly_fraction=0.25))
    book = _book(yes_ask=0.40, no_ask=0.50, ticker=ticker)
    result = s.evaluate(book, btc_price=80000, btc_vol=0.8, bankroll=1000.0)
    no_trades = [r for r in result if r.side == Side.NO and r.contracts > 0]
    assert len(no_trades) >= 1


def test_evaluate_b_range_market_produces_signal():
    # B65000 = YES if BTC in [$64,750, $65,250).  spot=65000 (ATM).
    # With σ=0.8 and 14 days, the ±$250 bucket has fair_p ≈ 2%.
    # yes_ask=0.01 leaves a small but positive edge; min_net_edge=0.001 lets it through.
    ticker = _future_ticker(days_ahead=14, strike=65000, mkt_type="B")
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, kelly_fraction=0.25))
    book = _book(yes_ask=0.01, no_ask=0.98, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert len(yes_trades) >= 1


# ── _is_far_otm unit tests ────────────────────────────────────────────────────

def test_far_otm_returns_true_when_sigma_zero():
    assert _is_far_otm(60000, 48000, sigma=0.0, t_years=1 / 365.25) is True


def test_far_otm_returns_true_when_t_zero():
    assert _is_far_otm(60000, 48000, sigma=0.8, t_years=0.0) is True


def test_far_otm_atm_returns_false():
    assert _is_far_otm(60000, 60000, sigma=0.8, t_years=1 / 52) is False


def test_far_otm_close_strike_returns_false():
    # 1-day contract, 80% vol: 3σ band is ±3*0.80*sqrt(1/365.25) ≈ ±0.126 log-pts
    # Strike 1% from spot → 0.01 log-pts << 0.126 → not far-OTM
    assert _is_far_otm(60000, 59400, sigma=0.80, t_years=1 / 365.25) is False


def test_far_otm_extreme_strike_returns_true():
    # 1-hour contract (t=1/8766), vol=0.80:
    # 3σ band = 3 * 0.80 * sqrt(1/8766) ≈ 0.0256 log-pts
    # T48000 vs spot=60000: ln(60000/48000) = 0.223 >> 0.0256
    t_hour = 1 / (365.25 * 24)
    assert _is_far_otm(60000, 48000, sigma=0.80, t_years=t_hour) is True


def test_far_otm_t71999_returns_true():
    # T71999.99 → strike=72000; spot=60000; 1-hour contract
    t_hour = 1 / (365.25 * 24)
    assert _is_far_otm(60000, 72000, sigma=0.80, t_years=t_hour) is True


def test_far_otm_sigma_distance_scales_with_time():
    # Longer time → wider 3σ band → same strike distance may stop being far-OTM
    spot, strike, sigma = 60000, 55000, 0.80
    t_short = 1 / (365.25 * 24)          # 1 hour
    t_long  = 30 / 365.25                 # 30 days
    # At 1 hour the strike is very far; at 30 days it's within 3σ
    assert _is_far_otm(spot, strike, sigma, t_short) is True
    assert _is_far_otm(spot, strike, sigma, t_long)  is False


# ── evaluate() far-OTM filter integration tests ───────────────────────────────

def test_evaluate_filters_t48000_with_live_spot():
    # T48000 (low catch-all) is ~26σ away from spot=60000 on a 1-hour contract.
    # Must return [] regardless of vol, not a degenerate signal.
    ticker = _future_ticker(days_ahead=1, strike=48000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    book = _book(yes_ask=0.01, no_ask=0.99, ticker=ticker)
    result = s.evaluate(book, btc_price=60000, btc_vol=0.80, bankroll=1000.0)
    assert result == [], f"Expected [], got {result}"


def test_evaluate_filters_t71999_high_catchall():
    # T71999.99 (high catch-all → strike=72000) is ~34σ away from spot=60000.
    ticker = _future_ticker(days_ahead=1, strike=71999.99)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    book = _book(yes_ask=0.01, no_ask=0.99, ticker=ticker)
    result = s.evaluate(book, btc_price=60000, btc_vol=0.80, bankroll=1000.0)
    assert result == [], f"Expected [], got {result}"


def test_evaluate_no_degenerate_signal_with_zero_vol():
    # Before the fix: sigma=0 caused _lognormal_prob → 0.5, producing a
    # massive fake edge on far-OTM markets (e.g. T48000 ask=0.01 → edge≈0.48).
    # After the fix: _is_far_otm(sigma=0) → True → evaluate returns [].
    ticker = _future_ticker(days_ahead=1, strike=48000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01))
    book = _book(yes_ask=0.01, no_ask=0.99, ticker=ticker)
    result = s.evaluate(book, btc_price=60000, btc_vol=0.0, bankroll=1000.0)
    assert result == [], "Degenerate signal detected — far-OTM filter must block sigma=0 case"


def test_evaluate_atm_market_not_filtered():
    # B65000 (midpoint=65000) vs spot=65000: |ln(1)| = 0 → never far-OTM.
    ticker = _future_ticker(days_ahead=1, strike=65000, mkt_type="B")
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001))
    book = _book(yes_ask=0.10, no_ask=0.90, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.80, bankroll=1000.0)
    # Should return something (a skip SizingResult or a signal), not []
    assert result != [], "ATM market should not be filtered by the far-OTM guard"


# ── is_near_atm (Phase A4 item 5, event-driven spot trigger) ────────────────────

def test_is_near_atm_true_when_spot_at_strike():
    s = BTCHourlyStrategy(_cfg())
    ticker = _future_ticker(strike=65000, mkt_type="B")
    assert s.is_near_atm(ticker, spot_price=65000.0) is True


def test_is_near_atm_true_within_two_strikes():
    s = BTCHourlyStrategy(_cfg())
    ticker = _future_ticker(strike=65000, mkt_type="B")
    # 2 strikes = 2 * 500 = 1000 -- right at the boundary.
    assert s.is_near_atm(ticker, spot_price=66000.0, strikes=2) is True


def test_is_near_atm_false_beyond_two_strikes():
    s = BTCHourlyStrategy(_cfg())
    ticker = _future_ticker(strike=65000, mkt_type="B")
    assert s.is_near_atm(ticker, spot_price=67000.0, strikes=2) is False


def test_is_near_atm_respects_custom_strikes_count():
    s = BTCHourlyStrategy(_cfg())
    ticker = _future_ticker(strike=65000, mkt_type="B")
    assert s.is_near_atm(ticker, spot_price=67000.0, strikes=5) is True


def test_is_near_atm_unparseable_ticker_defaults_true():
    s = BTCHourlyStrategy(_cfg())
    assert s.is_near_atm("NOT-A-REAL-TICKER", spot_price=65000.0) is True


# ── parse_strike (Phase A4 item 7, consistency scanner) ─────────────────────────

def test_parse_strike_above_direction():
    s = BTCHourlyStrategy(_cfg())
    ticker = _future_ticker(strike=71999.99, mkt_type="T")  # decimal -> "above"
    result = s.parse_strike(ticker)
    assert result is not None
    strike, direction, group_key = result
    assert strike == 72000.0
    assert direction == "above"


def test_parse_strike_below_direction():
    s = BTCHourlyStrategy(_cfg())
    ticker = _future_ticker(strike=48000, mkt_type="T")  # integer -> "below"
    result = s.parse_strike(ticker)
    assert result is not None
    strike, direction, group_key = result
    assert strike == 48000.0
    assert direction == "below"


def test_parse_strike_range_direction_excluded():
    s = BTCHourlyStrategy(_cfg())
    ticker = _future_ticker(strike=65000, mkt_type="B")
    assert s.parse_strike(ticker) is None


def test_parse_strike_unparseable_ticker_returns_none():
    s = BTCHourlyStrategy(_cfg())
    assert s.parse_strike("NOT-A-REAL-TICKER") is None


def test_parse_strike_same_expiry_shares_group_key():
    s = BTCHourlyStrategy(_cfg())
    t1 = _future_ticker(days_ahead=3, strike=60000, mkt_type="T")
    t2 = _future_ticker(days_ahead=3, strike=61000, mkt_type="T")
    r1 = s.parse_strike(t1)
    r2 = s.parse_strike(t2)
    assert r1[2] == r2[2]  # same expiry -> same group_key


# ── near_atm_only (2026-07 calibration fix) ──────────────────────────────────
# Real-data backtest showed 0-15c contracts (deep OTM, low fair_p) won 1.9%
# against an 8% breakeven requirement, while near-ATM 30-50c contracts won
# 60%. near_atm_only restricts trading to fair_p in [near_atm_fair_p_min,
# near_atm_fair_p_max] (default [0.25, 0.75]).

def test_near_atm_only_filters_deep_otm_market():
    # spot=50000 vs strike=65000, 14 days, 0.8 vol -> fair_p ≈ 0.95 (deep ITM
    # for "below" direction) -- well outside the default [0.25, 0.75] band.
    ticker = _future_ticker(days_ahead=14, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01, near_atm_only=True))
    book = _book(yes_ask=0.60, no_ask=0.40, ticker=ticker)
    result = s.evaluate(book, btc_price=50000, btc_vol=0.8, bankroll=1000.0)
    assert result == []


def test_near_atm_only_allows_genuinely_atm_market():
    # spot == strike -> fair_p == 0.5, always inside the band.
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01, near_atm_only=True))
    book = _book(yes_ask=0.40, no_ask=0.40, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    assert result != []


def test_near_atm_only_disabled_allows_deep_otm_market():
    # Same deep-OTM setup as above, but with the filter off -- the far-OTM
    # sigma guard doesn't trip at this distance (1.68σ, see the docstring on
    # test_evaluate_produces_signal_with_positive_edge), so a signal comes through.
    ticker = _future_ticker(days_ahead=14, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01, near_atm_only=False))
    book = _book(yes_ask=0.60, no_ask=0.40, ticker=ticker)
    result = s.evaluate(book, btc_price=50000, btc_vol=0.8, bankroll=1000.0)
    assert result != []


def test_near_atm_only_respects_custom_bounds():
    # fair_p ≈ 0.95 is inside a widened [0.0, 1.0] band.
    ticker = _future_ticker(days_ahead=14, strike=65000)
    s = BTCHourlyStrategy(_cfg(
        min_net_edge=0.01, near_atm_only=True,
        near_atm_fair_p_min=0.0, near_atm_fair_p_max=1.0,
    ))
    book = _book(yes_ask=0.60, no_ask=0.40, ticker=ticker)
    result = s.evaluate(book, btc_price=50000, btc_vol=0.8, bankroll=1000.0)
    assert result != []


# ── Spread-aware edge gate (2026-07 calibration fix) ─────────────────────────
# median observed spread on real KXBTC candlestick data is ~3c -- a claimed
# edge smaller than half that spread is noise, not a real mispricing.

def test_evaluate_blocked_by_wide_spread_despite_clearing_min_net_edge():
    # ATM: fair_p=0.5, ask=0.46 -> net_edge ≈ 0.018, clears min_net_edge=0.01
    # on its own. But no_ask=0.60 makes yes_ask+no_ask=1.06 -> half_spread=0.03,
    # raising the bar to 0.04 -- net_edge no longer clears it.
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01, near_atm_only=False))
    book = _book(yes_ask=0.46, no_ask=0.60, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert yes_trades == []


def test_evaluate_passes_wide_spread_when_edge_clearly_exceeds_half_spread():
    # ATM: fair_p=0.5, ask=0.30 -> net_edge ≈ 0.18, far exceeds min_net_edge
    # (0.01) + half_spread (0.025 from yes_ask=0.30 + no_ask=0.75 -> 1.05).
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.01, near_atm_only=False))
    book = _book(yes_ask=0.30, no_ask=0.75, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert len(yes_trades) == 1


# ── Tradeable band (2026-07 vol-calibration fix, two-sided RULE R1) ──────────

def test_evaluate_blocked_when_ask_below_tradeable_band_low():
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, tradeable_band_low=0.15, tradeable_band_high=0.85))
    book = _book(yes_ask=0.10, no_ask=0.91, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert yes_trades == []


def test_evaluate_blocked_when_ask_above_tradeable_band_high():
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, tradeable_band_low=0.15, tradeable_band_high=0.85))
    book = _book(yes_ask=0.90, no_ask=0.10, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert yes_trades == []


def test_evaluate_allowed_within_tradeable_band():
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, tradeable_band_low=0.15, tradeable_band_high=0.85))
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert len(yes_trades) == 1


def test_tradeable_band_skip_reason():
    ticker = _future_ticker(days_ahead=7, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, tradeable_band_low=0.15, tradeable_band_high=0.85))
    book = _book(yes_ask=0.10, no_ask=0.91, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_skips = [r for r in result if r.side == Side.YES]
    assert len(yes_skips) == 1
    assert yes_skips[0].skip_reason == "outside_tradeable_band"


# ── Expiry cutoff (2026-07 vol-calibration fix) ──────────────────────────────
# Evidence-based direction: bucketing 312 honest-backtest trades by
# hours-to-expiry showed win rate INCREASING as expiry approaches
# (>12h: 15.5%, 6-12h: 13.6%, 2-6h: 24.2%, 0-2h: 36.0%) -- the opposite of
# the "settlement noise near expiry" assumption. This cutoff blocks entries
# FAR from expiry, not near it.

def test_evaluate_blocked_beyond_max_hours_to_expiry():
    ticker = _ticker_hours_from(20, strike=65000)  # far beyond a 6h cutoff
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=6.0))
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    assert len(result) == 1
    assert result[0].contracts == 0
    assert "to_expiry" in (result[0].skip_reason or "")


def test_evaluate_allowed_within_max_hours_to_expiry():
    ticker = _ticker_hours_from(2, strike=65000)  # well within a 6h cutoff
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=6.0))
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert len(yes_trades) == 1


def test_max_hours_to_expiry_disabled_with_large_value():
    ticker = _ticker_hours_from(20, strike=65000)
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=999_999.0))
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert len(yes_trades) == 1


# ── KXBTC15M strike cache / ingest_market_metadata ────────────────────────────

def _ticker_15m_minutes_from(minutes_ahead: float) -> str:
    """Real KXBTC15M ticker whose close time is at or after ~minutes_ahead from now, rounded UP to the quarter-hour."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt = (datetime.now(tz=timezone.utc) + timedelta(minutes=minutes_ahead)).astimezone(et)
    # Round UP (not down) -- rounding down can land the close time BEFORE
    # "now", making the ticker already-expired depending on the current
    # wall-clock minute and flaking tests that assume a future expiry.
    minutes_since_epoch = int(dt.timestamp() // 60)
    remainder = minutes_since_epoch % 15
    if remainder != 0:
        dt = dt + timedelta(minutes=15 - remainder)
    dt = dt.replace(second=0, microsecond=0)
    mm = f"{dt.minute:02d}"
    return f"KXBTC15M-{dt.year-2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}{mm}-{mm}"


def test_evaluate_skips_15m_ticker_before_metadata_ingested():
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=999_999.0))
    ticker = _ticker_15m_minutes_from(10)
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    assert result == []  # floor_strike unknown -- must skip, never guess


def test_ingest_market_metadata_enables_15m_pricing():
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=999_999.0))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)
    assert result != []


def test_ingest_market_metadata_ignores_missing_floor_strike():
    s = BTCHourlyStrategy(_cfg())
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": None})
    assert ticker not in s._strike_cache


def test_ingest_market_metadata_ignores_non_numeric_floor_strike():
    s = BTCHourlyStrategy(_cfg())
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": "not-a-number"})
    assert ticker not in s._strike_cache


def test_ingest_market_metadata_is_per_ticker():
    s = BTCHourlyStrategy(_cfg())
    ticker_a = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker_a, {"floor_strike": 65000.0})
    assert s._strike_cache.get(ticker_a) == 65000.0
    assert "OTHER-TICKER" not in s._strike_cache


def test_hourly_ticker_never_needs_strike_cache():
    # Hourly's strike comes straight from the ticker string -- ingest_market_metadata
    # is a no-op concern for it (evaluate() must work identically with or without
    # ever calling it, since live/backtest for KXBTC never calls it).
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=999_999.0))
    ticker = _ticker_hours_from(2, strike=65000)
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert len(yes_trades) == 1


def test_is_near_atm_true_for_15m_ticker_before_metadata_ingested():
    s = BTCHourlyStrategy(_cfg())
    ticker = _ticker_15m_minutes_from(10)
    assert s.is_near_atm(ticker, 65000.0) is True  # unresolved strike -- don't filter out


def test_is_near_atm_uses_cached_strike_once_ingested():
    s = BTCHourlyStrategy(_cfg())
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    assert s.is_near_atm(ticker, 65000.0, strikes=1) is True
    assert s.is_near_atm(ticker, 200000.0, strikes=1) is False


def test_parse_strike_excludes_15m_tickers():
    # Single-market events have no ladder to check monotonicity against.
    s = BTCHourlyStrategy(_cfg())
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    assert s.parse_strike(ticker) is None


def test_series_tickers_includes_both_series():
    s = BTCHourlyStrategy(_cfg())
    assert set(s.series_tickers) == {"KXBTC", "KXBTC15M"}


def test_series_tickers_constructor_override():
    s = BTCHourlyStrategy(_cfg(), series_tickers=["KXBTC15M"])
    assert s.series_tickers == ["KXBTC15M"]
    assert s.owns_ticker("KXBTC-26JUL0317-T48000") is False
    assert s.owns_ticker("KXBTC15M-26JUL060115-15") is True


# ── expiry_ts (2026-07: post-resolution scanner settlement detection) ───────

def test_expiry_ts_matches_hourly_parse():
    s = BTCHourlyStrategy(_cfg())
    result = s.expiry_ts("KXBTC-26JUL0317-T48000")
    _, expected_expiry, _ = _parse_ticker("KXBTC-26JUL0317-T48000")
    assert result == expected_expiry


def test_expiry_ts_matches_15m_parse():
    s = BTCHourlyStrategy(_cfg())
    result = s.expiry_ts("KXBTC15M-26JUL060115-15")
    _, expected_expiry, _ = _parse_ticker("KXBTC15M-26JUL060115-15")
    assert result == expected_expiry


def test_expiry_ts_returns_none_for_unparseable_ticker():
    s = BTCHourlyStrategy(_cfg())
    assert s.expiry_ts("NOT-A-REAL-TICKER") is None


# ── vol_window_seconds / vol_multiplier per-ticker (2026-07 series-aware vol) ─

def test_vol_window_seconds_hourly_ticker_uses_hourly_cfg_field():
    s = BTCHourlyStrategy(_cfg(vol_window_seconds_hourly=3600.0, vol_window_seconds_15m=900.0))
    assert s.vol_window_seconds("KXBTC-26JUL0317-T48000") == 3600.0


def test_vol_window_seconds_15m_ticker_uses_15m_cfg_field():
    s = BTCHourlyStrategy(_cfg(vol_window_seconds_hourly=3600.0, vol_window_seconds_15m=900.0))
    assert s.vol_window_seconds("KXBTC15M-26JUL060115-15") == 900.0


def test_vol_window_seconds_no_cross_contamination_same_instance():
    """
    The specific regression this exists for: ONE strategy instance trades
    BOTH series simultaneously -- calling vol_window_seconds for a KXBTC15M
    ticker must never leak the hourly value (or vice versa), even when
    both are queried back-to-back on the same instance.
    """
    s = BTCHourlyStrategy(_cfg(vol_window_seconds_hourly=3600.0, vol_window_seconds_15m=900.0))
    hourly_ticker = "KXBTC-26JUL0317-T48000"
    m15_ticker = "KXBTC15M-26JUL060115-15"
    for _ in range(5):  # repeated interleaved calls -- no state leakage between them
        assert s.vol_window_seconds(hourly_ticker) == 3600.0
        assert s.vol_window_seconds(m15_ticker) == 900.0


def test_vol_multiplier_hourly_ticker_returns_none():
    s = BTCHourlyStrategy(_cfg(vol_multiplier_15m=1.0))
    assert s.vol_multiplier("KXBTC-26JUL0317-T48000") is None


def test_vol_multiplier_15m_ticker_returns_cfg_vol_multiplier_15m():
    s = BTCHourlyStrategy(_cfg(vol_multiplier_15m=1.25))
    assert s.vol_multiplier("KXBTC15M-26JUL060115-15") == pytest.approx(1.25)


# ── is_single_binary per-ticker (2026-07 opposite-side guard) ────────────────

def test_is_single_binary_true_for_kxbtc15m():
    s = BTCHourlyStrategy(_cfg())
    assert s.is_single_binary("KXBTC15M-26JUL060115-15") is True


def test_is_single_binary_false_for_hourly_ladder():
    s = BTCHourlyStrategy(_cfg())
    # Hourly B/T strike ladder -- opposite sides on different strikes are a
    # legitimate spread, so single-binary must be False.
    assert s.is_single_binary("KXBTC-26JUL0317-B61750") is False
    assert s.is_single_binary("KXBTC-26JUL0317-T48000") is False


def test_is_single_binary_no_cross_contamination_same_instance():
    s = BTCHourlyStrategy(_cfg())
    for _ in range(3):  # interleaved calls on one instance must not leak
        assert s.is_single_binary("KXBTC15M-26JUL060115-15") is True
        assert s.is_single_binary("KXBTC-26JUL0317-B61750") is False


# ── per-series isotonic calibrator (2026-07 bootstrap) ───────────────────────

class _FakeCalibrator:
    """Deterministic stand-in for a fitted IsotonicRegression: maps every
    input probability to a fixed constant, so a test can prove the strategy
    actually routed fair_p through .predict()."""

    def __init__(self, constant: float) -> None:
        self._constant = constant
        self.seen: list[float] = []

    def predict(self, xs):
        self.seen.extend(float(x) for x in xs)
        return [self._constant for _ in xs]


def test_series_of_distinguishes_15m_from_hourly():
    # KXBTC15M- also starts with KXBTC-, so ordering matters.
    # (Instance method, not static, since 2026-07: the series names are
    # per-instance class attrs so a sibling underlying can override them --
    # see BTCHourlyStrategy.series_15m / kbot.strategy.eth_15m.)
    s = BTCHourlyStrategy(_cfg())
    assert s._series_of("KXBTC15M-26JUL060115-15") == "KXBTC15M"
    assert s._series_of("KXBTC-26JUL0317-T48000") == "KXBTC"


def test_no_calibrator_installed_uses_raw_fair_p():
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=999_999.0))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)
    # Baseline: with no calibrator the signal is emitted from the raw model.
    assert result != []


def test_calibrator_is_applied_to_15m_fair_p():
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=999_999.0))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    cal = _FakeCalibrator(constant=0.99)  # force fair_p -> 0.99 for whichever side is priced
    s.set_calibrator(cal, series="KXBTC15M")

    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)

    assert cal.seen, "calibrator.predict was never called -- fair_p not routed through it"
    # The emitted signal's fair_p must be the calibrated constant, not the raw model prob.
    assert any(abs(r.fair_p - 0.99) < 1e-9 for r in result if r.contracts > 0)


def test_calibrator_is_series_scoped_hourly_not_touched_by_15m_calibrator():
    """
    A calibrator installed for KXBTC15M must NEVER be applied to a KXBTC
    (hourly) ticker -- the two series have distinct miscalibration profiles,
    so cross-application would corrupt hourly pricing.
    """
    s = BTCHourlyStrategy(_cfg(min_net_edge=0.001, max_hours_to_expiry_for_entry=999_999.0, near_atm_only=False))
    cal = _FakeCalibrator(constant=0.99)
    s.set_calibrator(cal, series="KXBTC15M")

    hourly_ticker = _ticker_hours_from(1.0, strike=65000, mkt_type="T")  # ATM, 1h out -> reaches _size_side
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=hourly_ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)

    # Non-empty result proves _size_side actually ran for the hourly ticker,
    # so an empty cal.seen is a meaningful "not applied", not a vacuous pass.
    assert result != []
    assert cal.seen == [], "15m calibrator must not be invoked for an hourly ticker"


def test_calibrator_for_returns_installed_calibrator_by_series():
    s = BTCHourlyStrategy(_cfg())
    cal = _FakeCalibrator(constant=0.5)
    s.set_calibrator(cal, series="KXBTC15M")
    assert s._calibrator_for("KXBTC15M-26JUL060115-15") is cal
    assert s._calibrator_for("KXBTC-26JUL0317-T48000") is None


# ── min_side_win_prob low-bucket floor (2026-07 WS-feed miscalibration fix) ──
# After the WebSocket BTC feed (Fix 4) pulled far more thin-edge, noise-
# triggered entries into the 30-50% predicted region, those buckets realized
# only 14-31%. The floor rejects any side whose CALIBRATED win prob is below
# cfg.min_side_win_prob, leaving the well-calibrated 50%+ buckets untouched.

def test_min_side_win_prob_blocks_calibrated_low_confidence_side():
    # Calibrator forces both sides' fair_p to 0.40 (< the 0.50 floor). Both
    # sides have a large raw edge at ask=0.20/0.79, so absent the floor they
    # would trade -- proving the floor, not the edge gate, is what blocks them.
    s = BTCHourlyStrategy(_cfg(
        min_net_edge=0.01, min_side_win_prob=0.50,
        max_hours_to_expiry_for_entry=999_999.0,
    ))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    s.set_calibrator(_FakeCalibrator(constant=0.40), series="KXBTC15M")
    book = _book(yes_ask=0.20, no_ask=0.79, ticker=ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)
    assert all(r.contracts == 0 for r in result)
    assert any(r.skip_reason == "below_min_side_win_prob" for r in result)


def test_min_side_win_prob_zero_disables_floor():
    # Identical setup, floor disabled -> the calibrated-0.40 YES side trades.
    s = BTCHourlyStrategy(_cfg(
        min_net_edge=0.01, min_side_win_prob=0.0,
        max_hours_to_expiry_for_entry=999_999.0,
    ))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    s.set_calibrator(_FakeCalibrator(constant=0.40), series="KXBTC15M")
    book = _book(yes_ask=0.20, no_ask=0.79, ticker=ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)
    assert any(r.side == Side.YES and r.contracts > 0 for r in result)


def test_min_side_win_prob_allows_side_at_or_above_floor():
    # Calibrated fair_p = 0.60 (>= 0.50 floor) -> the profitable region is
    # untouched: the YES side still trades and carries the calibrated prob.
    s = BTCHourlyStrategy(_cfg(
        min_net_edge=0.01, min_side_win_prob=0.50,
        max_hours_to_expiry_for_entry=999_999.0,
    ))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    s.set_calibrator(_FakeCalibrator(constant=0.60), series="KXBTC15M")
    # no_ask=0.62 leaves the NO side (also calibrated to 0.60) without edge,
    # isolating the YES trade.
    book = _book(yes_ask=0.45, no_ask=0.62, ticker=ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert len(yes_trades) == 1
    assert yes_trades[0].fair_p == pytest.approx(0.60)


def test_min_side_win_prob_uses_calibrated_not_raw_prob():
    # Raw model fair_p here is ~0.50 (ATM), which would clear a 0.50 floor;
    # the calibrator pushes it to 0.30, which must NOT. Proves the floor is
    # applied in calibrated space, after apply_calibrator.
    s = BTCHourlyStrategy(_cfg(
        min_net_edge=0.01, min_side_win_prob=0.50,
        max_hours_to_expiry_for_entry=999_999.0,
    ))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 65000.0})
    s.set_calibrator(_FakeCalibrator(constant=0.30), series="KXBTC15M")
    book = _book(yes_ask=0.20, no_ask=0.79, ticker=ticker)
    result = s.evaluate(book, btc_price=65000.0, btc_vol=0.8, bankroll=1000.0)
    assert all(r.contracts == 0 for r in result)
    assert any(r.skip_reason == "below_min_side_win_prob" for r in result)


# ── instance name override (2026-07: kxbtc15m runs as its own gated strategy) ─

def test_default_name_is_btc_hourly():
    assert BTCHourlyStrategy(_cfg()).name == "btc_hourly"


def test_name_override_sets_instance_name():
    s = BTCHourlyStrategy(_cfg(), series_tickers=["KXBTC15M"], name="kxbtc15m")
    assert s.name == "kxbtc15m"


def test_name_override_does_not_leak_to_class_or_other_instances():
    s1 = BTCHourlyStrategy(_cfg(), name="kxbtc15m")
    s2 = BTCHourlyStrategy(_cfg())
    assert s1.name == "kxbtc15m"
    assert s2.name == "btc_hourly"          # unaffected instance
    assert BTCHourlyStrategy.name == "btc_hourly"  # class attr untouched


def test_kxbtc15m_scoped_instance_only_owns_15m_tickers():
    s = BTCHourlyStrategy(_cfg(), series_tickers=["KXBTC15M"], name="kxbtc15m")
    assert s.owns_ticker("KXBTC15M-26JUL060115-15") is True
    assert s.owns_ticker("KXBTC-26JUL0317-T48000") is False  # hourly not routed to it
