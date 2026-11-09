"""Tests for kbot.strategy.base_rate — ticker parsing and entry-window gating (C5)."""

from __future__ import annotations

import pytest

from kbot.config import Settings
from kbot.feeds.econ_history import load_release_calendar, release_for_period
from kbot.models.types import OrderBook, PriceLevel, Side
from kbot.strategy.base_rate import BaseRateStrategy, parse_ticker


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env="demo",
        dry_run=True,
        min_net_edge=kwargs.get("min_net_edge", 0.01),
        kelly_fraction=kwargs.get("kelly_fraction", 0.25),
        max_position_pct=kwargs.get("max_position_pct", 0.50),
        max_contracts_per_order=kwargs.get("max_contracts_per_order", 1000),
        fee_coefficient=0.07,
        assumed_slippage=0.005,
        tradeable_band_low=kwargs.get("tradeable_band_low", 0.15),
        tradeable_band_high=kwargs.get("tradeable_band_high", 0.85),
        base_rate_entry_days=kwargs.get("base_rate_entry_days", 7),
        base_rate_stand_down_minutes=kwargs.get("base_rate_stand_down_minutes", 30),
    )


def _book(yes_ask: float, no_ask: float, ticker: str) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes_ask, quantity_units=50000)],
        no=[PriceLevel(price=no_ask, quantity_units=50000)],
    )


# ── parse_ticker ──────────────────────────────────────────────────────────────

def test_parse_ticker_fomc_hold():
    result = parse_ticker("KXFEDDECISION-28JAN-H0")
    assert result == ("fomc", "2028-01", "H0")


def test_parse_ticker_fomc_all_outcomes():
    for outcome in ("C26", "C25", "H0", "H25", "H26"):
        result = parse_ticker(f"KXFEDDECISION-26JUL-{outcome}")
        assert result == ("fomc", "2026-07", outcome)


def test_parse_ticker_cpi_positive_strike():
    assert parse_ticker("KXCPI-26NOV-T0.2") == ("cpi", "2026-11", 0.2)


def test_parse_ticker_cpi_negative_strike():
    assert parse_ticker("KXCPI-26NOV-T-0.1") == ("cpi", "2026-11", -0.1)


def test_parse_ticker_cpi_zero_strike():
    assert parse_ticker("KXCPI-26NOV-T0.0") == ("cpi", "2026-11", 0.0)


def test_parse_ticker_payrolls_positive_strike():
    assert parse_ticker("KXPAYROLLS-26NOV-T10000") == ("jobs", "2026-11", 10000.0)


def test_parse_ticker_payrolls_negative_strike():
    assert parse_ticker("KXPAYROLLS-26NOV-T-25000") == ("jobs", "2026-11", -25000.0)


def test_parse_ticker_case_insensitive_month():
    assert parse_ticker("KXFEDDECISION-26jul-H0") == ("fomc", "2026-07", "H0")


def test_parse_ticker_invalid_returns_none():
    assert parse_ticker("NOT-A-TICKER") is None
    assert parse_ticker("KXFED-27APR-T0.00") is None  # KXFED (rate level ladder), not KXFEDDECISION
    assert parse_ticker("KXFEDDECISION-26JUL-X99") is None  # not a real outcome code
    assert parse_ticker("") is None


def test_parse_ticker_invalid_month_returns_none():
    assert parse_ticker("KXFEDDECISION-26XXX-H0") is None


# ── Entry window / stand-down gating ──────────────────────────────────────────

@pytest.fixture()
def fomc_release():
    calendar = load_release_calendar()
    event = release_for_period(calendar, "fomc", "2026-07")
    assert event is not None
    return event


def test_evaluate_blocked_beyond_entry_window(fomc_release):
    s = BaseRateStrategy(_cfg(base_rate_entry_days=7))
    book = _book(0.5, 0.5, "KXFEDDECISION-26JUL-H0")
    ts = fomc_release.release_ts - 10 * 86400  # 10 days out -- beyond the 7-day window
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    assert len(result) == 1
    assert result[0].contracts == 0
    assert "entry_window" in (result[0].skip_reason or "")


def test_evaluate_blocked_within_stand_down_minutes(fomc_release):
    s = BaseRateStrategy(_cfg(base_rate_entry_days=7, base_rate_stand_down_minutes=30))
    book = _book(0.5, 0.5, "KXFEDDECISION-26JUL-H0")
    ts = fomc_release.release_ts - 10 * 60  # 10 minutes out -- within the 30-min stand-down
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    assert len(result) == 1
    assert result[0].contracts == 0
    assert "stand_down" in (result[0].skip_reason or "") or "min_of_release" in (result[0].skip_reason or "")


def test_evaluate_blocked_after_release_has_passed(fomc_release):
    s = BaseRateStrategy(_cfg())
    book = _book(0.5, 0.5, "KXFEDDECISION-26JUL-H0")
    ts = fomc_release.release_ts + 3600  # 1 hour after release
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    assert result == []


def test_evaluate_allowed_within_entry_window_produces_signal(fomc_release):
    s = BaseRateStrategy(_cfg(min_net_edge=0.01))
    book = _book(0.5, 0.5, "KXFEDDECISION-26JUL-H0")
    ts = fomc_release.release_ts - 3 * 86400  # 3 days out -- within the 7-day window
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    assert result != []
    # Either a real trade or an edge-below-min skip -- but NOT an entry-window skip.
    for r in result:
        assert r.skip_reason not in ("beyond_7d_entry_window",)


def test_evaluate_unparseable_ticker_returns_empty():
    s = BaseRateStrategy(_cfg())
    book = _book(0.5, 0.5, "NOT-A-REAL-TICKER")
    result = s.evaluate(book, bankroll=1000.0, current_time=0.0)
    assert result == []


def test_evaluate_unknown_release_period_returns_empty():
    # Year 1999 is nowhere in the shipped calendar.
    s = BaseRateStrategy(_cfg())
    book = _book(0.5, 0.5, "KXFEDDECISION-99JAN-H0")
    result = s.evaluate(book, bankroll=1000.0, current_time=0.0)
    assert result == []


def test_evaluate_tradeable_band_blocks_extreme_price(fomc_release):
    s = BaseRateStrategy(_cfg(min_net_edge=0.001, tradeable_band_low=0.15, tradeable_band_high=0.85))
    book = _book(0.97, 0.05, "KXFEDDECISION-26JUL-H0")
    ts = fomc_release.release_ts - 3 * 86400
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    yes_trades = [r for r in result if r.side == Side.YES and r.contracts > 0]
    assert yes_trades == []


def test_evaluate_event_driver_is_us_macro(fomc_release):
    s = BaseRateStrategy(_cfg(min_net_edge=0.001))
    book = _book(0.3, 0.72, "KXFEDDECISION-26JUL-H0")
    ts = fomc_release.release_ts - 3 * 86400
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    assert all(r.event_driver == "us_macro" for r in result)


def test_evaluate_cpi_ticker_within_entry_window():
    calendar = load_release_calendar()
    event = release_for_period(calendar, "cpi", "2026-06")
    assert event is not None
    s = BaseRateStrategy(_cfg(min_net_edge=0.001))
    book = _book(0.4, 0.62, "KXCPI-26JUN-T0.2")
    ts = event.release_ts - 3 * 86400
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    assert result != []


def test_evaluate_payrolls_ticker_within_entry_window():
    calendar = load_release_calendar()
    event = release_for_period(calendar, "jobs", "2026-06")
    assert event is not None
    s = BaseRateStrategy(_cfg(min_net_edge=0.001))
    book = _book(0.4, 0.62, "KXPAYROLLS-26JUN-T10000")
    ts = event.release_ts - 3 * 86400
    result = s.evaluate(book, bankroll=1000.0, current_time=ts)
    assert result != []


def test_evaluate_name_is_base_rate():
    s = BaseRateStrategy(_cfg())
    assert s.name == "base_rate"
