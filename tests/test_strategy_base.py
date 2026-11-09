"""Tests for kbot.strategy.base — the Strategy ABC's defaults and shared hooks."""

from __future__ import annotations

from kbot.config import Settings
from kbot.models.types import OrderBook, SizingResult
from kbot.strategy.base import Strategy


class _DummyStrategy(Strategy):
    name = "dummy"

    def __init__(self, cfg=None):
        self._cfg = cfg

    def evaluate(self, book, *, btc_price=None, btc_vol=None, bankroll, current_time=None):
        return []


def test_default_is_near_atm_always_true():
    s = _DummyStrategy()
    assert s.is_near_atm("ANY-TICKER", spot_price=65000.0) is True
    assert s.is_near_atm("ANY-TICKER", spot_price=0.0, strikes=1) is True


def test_default_parse_strike_returns_none():
    s = _DummyStrategy()
    assert s.parse_strike("ANY-TICKER") is None


def test_default_series_tickers_and_required_feeds_are_empty():
    s = _DummyStrategy()
    assert s.series_tickers == []
    assert s.required_feeds == []


def test_default_owns_ticker_false_with_no_series():
    s = _DummyStrategy()
    assert s.owns_ticker("ANY-TICKER") is False


def test_default_ingest_market_metadata_is_noop():
    s = _DummyStrategy()
    assert s.ingest_market_metadata("ANY-TICKER", {"floor_strike": 100.0}) is None


def test_default_expiry_ts_returns_none():
    s = _DummyStrategy()
    assert s.expiry_ts("ANY-TICKER") is None


# ── cfg property/setter (2026-07: post-resolution scanner discount swap) ────

def test_cfg_property_returns_underlying_cfg():
    cfg = Settings(kalshi_key_id="test", dry_run=True, min_net_edge=0.05)
    s = _DummyStrategy(cfg)
    assert s.cfg is cfg


def test_cfg_setter_updates_underlying_cfg():
    cfg1 = Settings(kalshi_key_id="test", dry_run=True, min_net_edge=0.05)
    cfg2 = Settings(kalshi_key_id="test", dry_run=True, min_net_edge=0.02)
    s = _DummyStrategy(cfg1)
    s.cfg = cfg2
    assert s.cfg is cfg2
    assert s._cfg is cfg2


# ── vol_window_seconds / vol_multiplier defaults (2026-07 series-aware vol) ─

def test_default_vol_window_seconds_is_one_hour():
    s = _DummyStrategy()
    assert s.vol_window_seconds("ANY-TICKER") == 3600.0


def test_default_vol_multiplier_is_none():
    s = _DummyStrategy()
    assert s.vol_multiplier("ANY-TICKER") is None


# ── is_single_binary default (2026-07 opposite-side guard) ───────────────────

def test_default_is_single_binary_is_false():
    s = _DummyStrategy()
    assert s.is_single_binary("ANY-TICKER") is False
