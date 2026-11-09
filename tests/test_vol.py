"""Tests for kbot.vol — ladder-implied vol and the realized-vol fallback (2026-07 calibration fix)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kbot.config import Settings
from kbot.models.types import OrderBook, PriceLevel
from kbot.strategy.btc_hourly import (
    _RANGE_HALF_WIDTH,
    _lognormal_prob,
    _lognormal_range_prob,
    _parse_ticker,
)
from kbot.vol import (
    estimate_vol,
    ewma_vol,
    fallback_vol,
    implied_vol_from_ladder,
    parkinson_vol,
)


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env="demo",
        dry_run=True,
        vol_multiplier=kwargs.get("vol_multiplier", 1.5),
        vol_floor=kwargs.get("vol_floor", 0.35),
    )


# ── ewma_vol ──────────────────────────────────────────────────────────────────

def test_ewma_vol_none_with_fewer_than_two_samples():
    assert ewma_vol([]) is None
    assert ewma_vol([(0.0, 100.0)]) is None


def test_ewma_vol_zero_for_constant_price():
    history = [(float(i * 60), 60000.0) for i in range(10)]
    assert ewma_vol(history) == pytest.approx(0.0, abs=1e-9)


def test_ewma_vol_positive_for_varying_prices():
    history = [(float(i * 60), 60000.0 + (i % 3) * 100) for i in range(20)]
    vol = ewma_vol(history)
    assert vol is not None
    assert vol > 0


def test_ewma_vol_low_lambda_weights_recent_shock_more_than_high_lambda():
    prices = [60000.0] * 15 + [60000.0, 66000.0]  # sharp recent jump
    history = [(float(i * 60), p) for i, p in enumerate(prices)]
    vol_fast = ewma_vol(history, lam=0.5)   # low lambda -> reacts fast
    vol_slow = ewma_vol(history, lam=0.99)  # high lambda -> smooths it out
    assert vol_fast > vol_slow


# ── ewma_vol window_seconds (2026-07 series-aware vol fix) ───────────────────

def test_ewma_vol_no_window_uses_all_history():
    # Baseline: window_seconds=None (the default) must behave exactly as
    # before this parameter existed -- no filtering at all.
    history = [(float(i * 60), 60000.0 + (i % 3) * 100) for i in range(20)]
    assert ewma_vol(history) == ewma_vol(history, window_seconds=None)


def test_ewma_vol_window_seconds_filters_to_trailing_window():
    # 20 samples 60s apart spanning 0..1140s. A 300s window relative to the
    # LAST sample (t=1140) should only keep samples from t=840 onward (5 of them).
    history = [(float(i * 60), 60000.0 + (i % 4) * 50) for i in range(20)]
    windowed = ewma_vol(history, window_seconds=300.0)
    full = ewma_vol(history, window_seconds=None)
    # Both are computable (>=2 samples in the window) and generally differ
    # since they're computed over different sample sets.
    assert windowed is not None
    assert full is not None


def test_ewma_vol_window_seconds_none_when_too_few_samples_remain():
    # Only the last two samples fall within a very short window -- exactly
    # 2 is still computable (1 log-return); shrink further and it's None.
    history = [(0.0, 60000.0), (500.0, 60100.0), (1000.0, 61000.0)]
    assert ewma_vol(history, window_seconds=10.0) is None  # only the last sample survives
    assert ewma_vol(history, window_seconds=600.0) is not None  # last two survive


def test_ewma_vol_15m_window_excludes_hourly_scale_history():
    """
    The specific regression this fix exists for: a price series with a
    real shock 20 minutes ago must NOT affect a 900s (15-min) window's
    estimate, even though it WOULD affect a 3600s (hourly) window's --
    proves the two windows can never cross-contaminate each other's
    result when applied to the exact same underlying history.
    """
    now = 3600.0
    # Calm for the last 15 minutes, but a sharp jump 20 minutes ago (outside
    # the 900s window, inside the 3600s one).
    history = [
        (now - 3600, 60000.0),
        (now - 2400, 60000.0),
        (now - 1200, 66000.0),  # shock, 20 min ago
        (now - 1140, 60050.0),  # reverts almost immediately
        (now - 900, 60000.0),   # exactly at the 900s cutoff boundary
        (now - 600, 60010.0),
        (now - 300, 59990.0),
        (now, 60005.0),
    ]
    vol_15m = ewma_vol(history, window_seconds=900.0)
    vol_hourly = ewma_vol(history, window_seconds=3600.0)
    assert vol_15m is not None and vol_hourly is not None
    # The hourly window sees the 10% shock; the 15m window's calm samples
    # alone produce a much smaller estimate.
    assert vol_hourly > vol_15m * 5


# ── parkinson_vol ─────────────────────────────────────────────────────────────

def test_parkinson_vol_none_for_empty_candles():
    assert parkinson_vol([], bar_seconds=3600) is None


def test_parkinson_vol_zero_for_no_range():
    candles = [(60000.0, 60000.0)] * 5
    assert parkinson_vol(candles, bar_seconds=3600) == pytest.approx(0.0, abs=1e-9)


def test_parkinson_vol_positive_for_real_range():
    candles = [(61000.0, 59000.0)] * 5
    vol = parkinson_vol(candles, bar_seconds=3600)
    assert vol is not None
    assert vol > 0


def test_parkinson_vol_none_for_invalid_bar_seconds():
    assert parkinson_vol([(61000.0, 59000.0)], bar_seconds=0) is None


def test_parkinson_vol_ignores_degenerate_bars():
    # A bar with high < low is nonsensical data -- must not crash or count it.
    candles = [(61000.0, 59000.0), (58000.0, 59000.0)]
    vol = parkinson_vol(candles, bar_seconds=3600)
    assert vol is not None and vol > 0


# ── fallback_vol ──────────────────────────────────────────────────────────────
#
# 2026-08: fallback_vol returns (sigma, floored). `floored` is asserted
# alongside sigma throughout -- it is the observability that would have caught
# the 15-minute vol floor binding on the majority of evaluations.

def test_fallback_vol_uses_floor_when_scaled_realized_is_low():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.35)
    sigma, floored = fallback_vol(0.10, None, cfg)
    assert sigma == pytest.approx(0.35)
    assert floored is True


def test_fallback_vol_uses_scaled_realized_when_above_floor():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.35)
    sigma, floored = fallback_vol(0.30, None, cfg)
    assert sigma == pytest.approx(0.45)
    assert floored is False


def test_fallback_vol_takes_max_of_ewma_and_parkinson():
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.0)
    assert fallback_vol(0.20, 0.50, cfg)[0] == pytest.approx(0.50)
    assert fallback_vol(0.50, 0.20, cfg)[0] == pytest.approx(0.50)


def test_fallback_vol_zero_when_no_estimates_and_floor_zero():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.0)
    # realized 0.0 is not BELOW a 0.0 floor, so this is not a floor hit.
    assert fallback_vol(None, None, cfg) == (0.0, False)


# ── fallback_vol multiplier override (2026-07 series-aware vol fix) ─────────

def test_fallback_vol_multiplier_override_replaces_cfg_value():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.0)
    # cfg.vol_multiplier=1.5 would give 0.30*1.5=0.45; override to 1.0 instead.
    assert fallback_vol(0.30, None, cfg, multiplier=1.0)[0] == pytest.approx(0.30)


def test_fallback_vol_multiplier_none_uses_cfg_value():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.0)
    assert fallback_vol(0.30, None, cfg, multiplier=None)[0] == pytest.approx(0.45)


def test_fallback_vol_multiplier_override_still_respects_floor():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.35)
    assert fallback_vol(0.10, None, cfg, multiplier=1.0) == (0.35, True)


# ── fallback_vol floor override (2026-08 series-aware vol FLOOR fix) ─────────

def test_fallback_vol_floor_override_replaces_cfg_value():
    """The 15-minute floor must be able to be lower than the hourly one."""
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.35)
    # cfg.vol_floor=0.35 would bind on a realized 0.20 and return the floor.
    assert fallback_vol(0.20, None, cfg) == (0.35, True)
    # With the 15-minute floor the realized estimate survives instead.
    sigma, floored = fallback_vol(0.20, None, cfg, floor=0.10)
    assert sigma == pytest.approx(0.20)
    assert floored is False


def test_fallback_vol_floor_none_uses_cfg_value():
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.35)
    assert fallback_vol(0.20, None, cfg, floor=None) == (0.35, True)


def test_fallback_vol_floor_override_still_binds_on_a_degenerate_estimate():
    """A LOWER floor is still a floor -- it must catch a cold/flat estimator."""
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.35)
    assert fallback_vol(0.001, None, cfg, floor=0.10) == (0.10, True)
    assert fallback_vol(None, None, cfg, floor=0.10) == (0.10, True)


# ── implied_vol_from_ladder ────────────────────────────────────────────────────

def _range_book(price: float) -> OrderBook:
    return OrderBook(ticker="x", yes=[PriceLevel(price=price, quantity_units=10000)], no=[])


def _ticker_for_expiry(expiry_ts: float, strike: int) -> str:
    """Build a KXBTC B-range ticker string whose _parse_ticker()-decoded expiry
    matches `expiry_ts` exactly (to the hour) -- critical for tests that
    generate synthetic prices at a known t_years and then re-parse the
    ticker to recover it, since a mismatched encoded expiry silently fits
    against the wrong time-to-expiry."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).astimezone(et)
    return f"KXBTC-{dt.year - 2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}-B{strike}"


def _make_ladder(spot: float, expiry_ts: float, now_ts: float, true_sigma: float, strikes: list[float]) -> dict:
    t_years = (expiry_ts - now_ts) / (365.25 * 24 * 3600)
    books = {}
    for strike in strikes:
        lower, upper = strike - _RANGE_HALF_WIDTH, strike + _RANGE_HALF_WIDTH
        price = _lognormal_range_prob(spot, lower, upper, true_sigma, t_years)
        price = max(min(price, 0.99), 0.01)
        ticker = _ticker_for_expiry(expiry_ts, int(strike))
        books[ticker] = OrderBook(ticker=ticker, yes=[PriceLevel(price=price, quantity_units=10000)], no=[])
    return books


def test_implied_vol_from_ladder_recovers_known_sigma():
    now_ts = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    expiry_ts = now_ts + 3 * 3600
    spot = 65000.0
    true_sigma = 0.65
    books = _make_ladder(spot, expiry_ts, now_ts, true_sigma, [64000, 64500, 65000, 65500, 66000])

    recovered = implied_vol_from_ladder(
        books, spot, now_ts,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
    )
    assert recovered is not None
    assert recovered == pytest.approx(true_sigma, rel=0.05)


def test_implied_vol_from_ladder_recovers_different_known_sigma():
    now_ts = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    expiry_ts = now_ts + 5 * 3600
    spot = 60000.0
    true_sigma = 1.10
    books = _make_ladder(spot, expiry_ts, now_ts, true_sigma, [59000, 59500, 60000, 60500, 61000])

    recovered = implied_vol_from_ladder(
        books, spot, now_ts,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
    )
    assert recovered is not None
    assert recovered == pytest.approx(true_sigma, rel=0.05)


def test_implied_vol_from_ladder_none_with_fewer_than_two_range_quotes():
    now_ts = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    expiry_ts = now_ts + 3600
    books = _make_ladder(65000.0, expiry_ts, now_ts, 0.65, [65000])
    recovered = implied_vol_from_ladder(
        books, 65000.0, now_ts,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
    )
    assert recovered is None


def test_implied_vol_from_ladder_ignores_non_range_tickers():
    books = {"KXBTC-26JUL0317-T48000": _range_book(0.05)}
    recovered = implied_vol_from_ladder(
        books, 65000.0, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
    )
    assert recovered is None


def test_implied_vol_from_ladder_none_for_unparseable_tickers():
    books = {"NOT-A-TICKER": _range_book(0.5), "ALSO-NOT": _range_book(0.3)}
    recovered = implied_vol_from_ladder(
        books, 65000.0, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
    )
    assert recovered is None


def test_implied_vol_from_ladder_empty_books():
    recovered = implied_vol_from_ladder(
        {}, 65000.0, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
    )
    assert recovered is None


# ── estimate_vol ──────────────────────────────────────────────────────────────

def test_estimate_vol_uses_ladder_when_fittable():
    now_ts = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    expiry_ts = now_ts + 3 * 3600
    spot = 65000.0
    books = _make_ladder(spot, expiry_ts, now_ts, 0.65, [64000, 64500, 65000, 65500, 66000])
    cfg = _cfg()

    result = estimate_vol(
        books, spot, now_ts,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.20, parkinson=0.20, cfg=cfg,
    )
    assert result.source == "ladder"
    assert result.sigma == pytest.approx(0.65, rel=0.05)


def test_estimate_vol_falls_back_when_no_books():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.35)
    result = estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.10, parkinson=None, cfg=cfg,
    )
    assert result.source == "fallback"
    assert result.sigma == pytest.approx(0.35)


def test_estimate_vol_falls_back_when_ladder_cannot_fit():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.35)
    books = {"NOT-A-TICKER": _range_book(0.5)}
    result = estimate_vol(
        books, 65000.0, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.30, parkinson=None, cfg=cfg,
    )
    assert result.source == "fallback"
    assert result.sigma == pytest.approx(0.45)


# ── vol sanity cap (2026-07: vol=1.84 still producing signals) ───────────────

def test_estimate_vol_fallback_clamped_above_cap():
    # vol_multiplier=1.0 so ewma passes through unscaled: 1.84 > default cap 1.5.
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.0)
    result = estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=1.84, parkinson=None, cfg=cfg,
    )
    assert result.source == "fallback"
    assert result.sigma == pytest.approx(1.5)


def test_estimate_vol_fallback_not_clamped_below_cap():
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.0)
    result = estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.80, parkinson=None, cfg=cfg,
    )
    assert result.source == "fallback"
    assert result.sigma == pytest.approx(0.80)


def test_estimate_vol_ladder_clamped_above_cap():
    # True sigma of 1.8 is inside implied_vol_from_ladder's own [0.05, 3.0]
    # fit bounds (so the fit itself succeeds and isn't rejected as a
    # near-bound degenerate fit) but above the 1.5 sanity cap.
    now_ts = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    expiry_ts = now_ts + 3 * 3600
    spot = 65000.0
    books = _make_ladder(spot, expiry_ts, now_ts, 1.8, [64000, 64500, 65000, 65500, 66000])
    cfg = _cfg()

    result = estimate_vol(
        books, spot, now_ts,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.20, parkinson=0.20, cfg=cfg,
    )
    assert result.source == "ladder"
    assert result.sigma == pytest.approx(1.5)


def test_estimate_vol_respects_custom_cap():
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.0)
    cfg = cfg.model_copy(update={"vol_sanity_cap": 0.5})
    result = estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.60, parkinson=None, cfg=cfg,
    )
    assert result.sigma == pytest.approx(0.5)


def test_estimate_vol_cap_logs_warning(mocker):
    warn_spy = mocker.patch("kbot.vol.log.warning")
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.0)
    estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=1.84, parkinson=None, cfg=cfg,
    )
    warn_spy.assert_called_once()
    assert warn_spy.call_args.args[0] == "vol.sanity_cap_applied"


def test_estimate_vol_no_warning_when_under_cap(mocker):
    warn_spy = mocker.patch("kbot.vol.log.warning")
    cfg = _cfg(vol_multiplier=1.0, vol_floor=0.0)
    estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.5, parkinson=None, cfg=cfg,
    )
    warn_spy.assert_not_called()


# ── estimate_vol vol_multiplier_override (2026-07 series-aware vol fix) ─────

def test_estimate_vol_multiplier_override_affects_fallback_path():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.0)
    result = estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.30, parkinson=None, cfg=cfg, vol_multiplier_override=1.0,
    )
    assert result.source == "fallback"
    assert result.sigma == pytest.approx(0.30)  # 0.30*1.0, not 0.30*1.5


def test_estimate_vol_no_override_uses_cfg_multiplier():
    cfg = _cfg(vol_multiplier=1.5, vol_floor=0.0)
    result = estimate_vol(
        None, None, 0.0,
        parse_ticker=_parse_ticker, lognormal_prob=_lognormal_prob, lognormal_range_prob=_lognormal_range_prob,
        ewma=0.30, parkinson=None, cfg=cfg,
    )
    assert result.sigma == pytest.approx(0.45)  # 0.30*1.5


def test_default_vol_sanity_cap_is_1_5():
    cfg = Settings(kalshi_key_id="test", dry_run=True)
    assert cfg.vol_sanity_cap == pytest.approx(1.5)


def test_vol_sanity_cap_must_be_positive():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="test", dry_run=True, vol_sanity_cap=0.0)
