"""
Tests for kbot.strategy.eth_15m — KXETH15M ticker grammar and the inherited
lognormal model/sizing path.

ETH15MStrategy is a SUBCLASS of BTCHourlyStrategy, so the bulk of the model
is already covered by tests/test_strategy_btc_hourly.py. What's tested here
is (a) the ETH-specific ticker grammar, (b) the series/feed identity, and
(c) that the shared fixes in _size_side (min_side_win_prob floor, raw_fair_p
plumbing) really do reach ETH through inheritance rather than needing a copy.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kbot.config import Settings
from kbot.models.types import OrderBook, PriceLevel, Side
from kbot.strategy.btc_hourly import BTCHourlyStrategy
from kbot.strategy.eth_15m import ETH15MStrategy


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
        near_atm_only=kwargs.get("near_atm_only", False),
        min_side_win_prob=kwargs.get("min_side_win_prob", 0.0),
        max_hours_to_expiry_for_entry=kwargs.get("max_hours_to_expiry_for_entry", 999_999.0),
        tradeable_band_low=kwargs.get("tradeable_band_low", 0.0001),
        tradeable_band_high=kwargs.get("tradeable_band_high", 0.9999),
        vol_window_seconds_hourly=kwargs.get("vol_window_seconds_hourly", 3600.0),
        vol_window_seconds_15m=kwargs.get("vol_window_seconds_15m", 900.0),
        vol_multiplier_15m=kwargs.get("vol_multiplier_15m", 1.0),
    )


def _book(yes_ask: float, no_ask: float, ticker: str) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes_ask, quantity_units=50000)],
        no=[PriceLevel(price=no_ask, quantity_units=50000)],
    )


def _ticker_15m_minutes_from(minutes_ahead: float) -> str:
    """Real KXETH15M ticker closing at/after ~minutes_ahead from now, rounded UP to the quarter-hour."""
    et = ZoneInfo("America/New_York")
    dt = (datetime.now(tz=timezone.utc) + timedelta(minutes=minutes_ahead)).astimezone(et)
    minutes_since_epoch = int(dt.timestamp() // 60)
    remainder = minutes_since_epoch % 15
    if remainder != 0:
        dt = dt + timedelta(minutes=15 - remainder)
    dt = dt.replace(second=0, microsecond=0)
    mm = f"{dt.minute:02d}"
    return f"KXETH15M-{dt.year-2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}{mm}-{mm}"


# ── Ticker parsing (real KXETH15M format, confirmed live 2026-07) ────────────

def test_parses_the_confirmed_live_ticker():
    """
    The exact ticker the format was confirmed against:
      KXETH15M-26JUL311800-00 | floor 1860.71 | close 2026-07-31T22:00:00Z
    18:00 US Eastern (EDT, UTC-4) == 22:00 UTC.
    """
    s = ETH15MStrategy(_cfg())
    result = s._parse("KXETH15M-26JUL311800-00")
    assert result is not None
    strike, expiry_ts, direction = result
    assert strike is None          # floor_strike is NEVER in the ticker
    assert direction == "above"
    expected = datetime(2026, 7, 31, 22, 0, tzinfo=timezone.utc).timestamp()
    assert expiry_ts == pytest.approx(expected)


def test_parse_is_case_insensitive():
    s = ETH15MStrategy(_cfg())
    assert s._parse("kxeth15m-26jul311800-00") is not None


def test_parse_rejects_malformed_tickers():
    s = ETH15MStrategy(_cfg())
    assert s._parse("KXETH15M-26XXX311800-00") is None   # invalid month
    assert s._parse("KXETH15M-26JUL31180-00") is None    # missing a digit
    assert s._parse("ETH15M-26JUL311800-00") is None     # wrong prefix
    assert s._parse("") is None


def test_parse_rejects_btc_tickers():
    """
    The ETH strategy must not silently price a BTC contract. Its regex is
    anchored to KXETH15M, so both BTC series fail to parse and evaluate()
    skips them.
    """
    s = ETH15MStrategy(_cfg())
    assert s._parse("KXBTC15M-26JUL060115-15") is None
    assert s._parse("KXBTC-26JUL0317-T48000") is None


def test_btc_strategy_rejects_eth_tickers():
    """The converse: the BTC strategy must not price an ETH contract."""
    s = BTCHourlyStrategy(_cfg())
    assert s._parse("KXETH15M-26JUL311800-00") is None


def test_all_quarter_hour_minutes_parse():
    s = ETH15MStrategy(_cfg())
    for minute in ("00", "15", "30", "45"):
        assert s._parse(f"KXETH15M-26JUL3112{minute}-{minute}") is not None


def test_expiry_ts_matches_parse():
    s = ETH15MStrategy(_cfg())
    ticker = "KXETH15M-26JUL311800-00"
    assert s.expiry_ts(ticker) == pytest.approx(s._parse(ticker)[1])


# ── Series / feed identity ──────────────────────────────────────────────────

def test_declares_eth_series_and_feed():
    s = ETH15MStrategy(_cfg())
    assert s.name == "kxeth15m"
    assert s.series_tickers == ["KXETH15M"]
    assert s.required_feeds == ["eth_spot"]
    assert s.spot_feed_name == "eth_spot"


def test_owns_only_eth_tickers():
    s = ETH15MStrategy(_cfg())
    assert s.owns_ticker("KXETH15M-26JUL311800-00") is True
    assert s.owns_ticker("KXBTC15M-26JUL060115-15") is False
    assert s.owns_ticker("KXBTC-26JUL0317-T48000") is False


def test_series_of_reports_the_eth_series():
    """Calibrators are keyed by series -- ETH must never look up BTC's."""
    s = ETH15MStrategy(_cfg())
    assert s._series_of("KXETH15M-26JUL311800-00") == "KXETH15M"


def test_is_single_binary_true_for_eth_15m():
    """One YES/NO market per window -> opposite-side re-entry is blocked."""
    s = ETH15MStrategy(_cfg())
    assert s.is_single_binary("KXETH15M-26JUL311800-00") is True


def test_uses_the_15m_vol_window_not_the_hourly_one():
    s = ETH15MStrategy(_cfg(vol_window_seconds_15m=900.0, vol_window_seconds_hourly=3600.0))
    assert s.vol_window_seconds("KXETH15M-26JUL311800-00") == 900.0


def test_uses_the_15m_vol_multiplier():
    s = ETH15MStrategy(_cfg(vol_multiplier_15m=1.25))
    assert s.vol_multiplier("KXETH15M-26JUL311800-00") == 1.25


def test_vol_model_functions_uses_the_eth_parser():
    """
    The runner fits ladder-implied vol with whatever parser the strategy
    hands it. If ETH handed back BTC's parser, ETH books would be invisible
    to the fit and BTC books would be silently included.
    """
    s = ETH15MStrategy(_cfg())
    parse_ticker, _lp, _lrp = s.vol_model_functions()
    assert parse_ticker("KXETH15M-26JUL311800-00") is not None
    assert parse_ticker("KXBTC15M-26JUL060115-15") is None


# ── floor_strike ingestion (not encoded in the ticker) ──────────────────────

def test_evaluate_skips_ticker_before_metadata_ingested():
    s = ETH15MStrategy(_cfg(min_net_edge=0.001))
    ticker = _ticker_15m_minutes_from(10)
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=1860.0, btc_vol=0.8, bankroll=1000.0)
    assert result == []  # floor_strike unknown -- must skip, never guess


def test_ingest_market_metadata_enables_pricing():
    s = ETH15MStrategy(_cfg(min_net_edge=0.001))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 1860.71})
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    result = s.evaluate(book, btc_price=1860.71, btc_vol=0.8, bankroll=1000.0)
    assert result != []


def test_ingest_ignores_missing_or_malformed_floor_strike():
    s = ETH15MStrategy(_cfg())
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {})
    s.ingest_market_metadata(ticker, {"floor_strike": "not-a-number"})
    assert ticker not in s._strike_cache


# ── Inherited _size_side fixes reach ETH (the whole point of subclassing) ───

def test_inherits_raw_fair_p_plumbing():
    """
    Fix (c): raw_fair_p is the PRE-calibration probability retrain fits on.
    With no calibrator installed (ETH's normal state) it equals fair_p, but
    it must be POPULATED -- a None here would make every ETH outcome fall
    back to model_prob and silently reintroduce the domain mismatch.
    """
    s = ETH15MStrategy(_cfg(min_net_edge=0.001))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 1860.71})
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    results = s.evaluate(book, btc_price=1860.71, btc_vol=0.8, bankroll=1000.0)
    assert results
    for r in results:
        assert r.raw_fair_p is not None
        assert r.raw_fair_p == pytest.approx(r.fair_p)  # no calibrator installed


def test_inherits_min_side_win_prob_floor():
    """
    Fix (d): a side whose calibrated win probability is below the floor is
    never bought, whatever its raw edge. Inherited from BTCHourlyStrategy --
    if ETH had been copy-pasted before this fix landed it would trade the
    miscalibrated 30-50% region.
    """
    s = ETH15MStrategy(_cfg(min_net_edge=0.001, min_side_win_prob=0.99))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 1860.71})
    book = _book(yes_ask=0.10, no_ask=0.10, ticker=ticker)
    results = s.evaluate(book, btc_price=1860.71, btc_vol=0.8, bankroll=1000.0)
    assert all(r.contracts == 0 for r in results)
    assert any(r.skip_reason == "below_min_side_win_prob" for r in results)


def test_no_calibrator_installed_by_default():
    """
    ETH starts with NO fitted calibrator -- raw lognormal probabilities and
    the backtest gate, not a borrowed BTC calibrator.
    """
    s = ETH15MStrategy(_cfg())
    assert s._calibrator_for("KXETH15M-26JUL311800-00") is None


def test_btc_calibrator_is_never_applied_to_eth():
    """
    Calibrators are series-scoped. Installing a BTC one on an ETH strategy
    instance must not affect ETH pricing -- the miscalibration profiles are
    unrelated, and cross-applying them is the exact failure the separate
    deployment exists to prevent.
    """
    class _Calibrator:
        def predict(self, xs):
            return [0.99 for _ in xs]

    s = ETH15MStrategy(_cfg())
    s.set_calibrator(_Calibrator(), series="KXBTC15M")
    assert s._calibrator_for("KXETH15M-26JUL311800-00") is None


def test_signals_carry_the_crypto_price_driver():
    """Shared exposure bucket with BTC, by decision (correlated underlyings)."""
    s = ETH15MStrategy(_cfg(min_net_edge=0.001))
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 1860.71})
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    results = s.evaluate(book, btc_price=1860.71, btc_vol=0.8, bankroll=1000.0)
    assert results
    assert all(r.event_driver == "crypto_price" for r in results)


def test_respects_no_trade_window_near_expiry():
    s = ETH15MStrategy(_cfg(no_trade_final_minutes=5))
    ticker = _ticker_15m_minutes_from(1)
    s.ingest_market_metadata(ticker, {"floor_strike": 1860.71})
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    results = s.evaluate(book, btc_price=1860.71, btc_vol=0.8, bankroll=1000.0)
    assert all(r.contracts == 0 for r in results)


def test_no_spot_data_returns_no_signals():
    s = ETH15MStrategy(_cfg())
    ticker = _ticker_15m_minutes_from(10)
    s.ingest_market_metadata(ticker, {"floor_strike": 1860.71})
    book = _book(yes_ask=0.46, no_ask=0.46, ticker=ticker)
    assert s.evaluate(book, btc_price=None, btc_vol=None, bankroll=1000.0) == []
