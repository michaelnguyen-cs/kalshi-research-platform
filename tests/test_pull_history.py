"""
Tests for scripts/pull_history.py's pure conversion/math helpers
(Phase A4 item 6, hotfixed 2026-07 after a live 400 exposed two bugs --
see the module docstring in scripts/pull_history.py for the full story).
Network I/O (fetch_coinbase_candles, main) is not unit tested here.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pull_history.py"
_spec = importlib.util.spec_from_file_location("pull_history", _SCRIPT_PATH)
pull_history = importlib.util.module_from_spec(_spec)
sys.modules["pull_history"] = pull_history
_spec.loader.exec_module(pull_history)


# ── chunk_time_range ─────────────────────────────────────────────────────────

def test_chunk_time_range_single_chunk_when_small():
    chunks = pull_history.chunk_time_range(0, 3600, period_interval=60, max_candles=4500)
    assert chunks == [(0, 3600)]


def test_chunk_time_range_splits_wide_range():
    # period_interval=1 (minute), max_candles=100 -> each chunk covers 6000s.
    chunks = pull_history.chunk_time_range(0, 15000, period_interval=1, max_candles=100)
    assert chunks == [(0, 6000), (6000, 12000), (12000, 15000)]


def test_chunk_time_range_empty_for_invalid_range():
    assert pull_history.chunk_time_range(100, 100, period_interval=60) == []
    assert pull_history.chunk_time_range(200, 100, period_interval=60) == []


def test_chunk_time_range_covers_full_span_without_gaps():
    chunks = pull_history.chunk_time_range(0, 100_000, period_interval=1, max_candles=37)
    # Every chunk's end must equal the next chunk's start; first starts at 0, last ends at end_ts.
    assert chunks[0][0] == 0
    assert chunks[-1][1] == 100_000
    for (a_start, a_end), (b_start, b_end) in zip(chunks, chunks[1:]):
        assert a_end == b_start


# ── candlestick_to_row ───────────────────────────────────────────────────────

def _candle(yes_ask="0.6200", yes_bid="0.5900", end_period_ts=4600) -> dict:
    c = {"end_period_ts": end_period_ts}
    if yes_ask is not None:
        c["yes_ask"] = {"close_dollars": yes_ask}
    if yes_bid is not None:
        c["yes_bid"] = {"close_dollars": yes_bid}
    return c


def test_candlestick_to_row_basic():
    row = pull_history.candlestick_to_row(_candle(), "TICK", 65000.0, 0.80, True)
    assert row == {
        "timestamp": 4600,
        "btc_price": 65000.0,
        "btc_vol": 0.80,
        "ticker": "TICK",
        "yes_ask": 0.62,
        "no_ask": 0.41,
        "resolved_yes": 1,
        "floor_strike": "",
    }


def test_candlestick_to_row_with_floor_strike():
    row = pull_history.candlestick_to_row(_candle(), "TICK", 65000.0, 0.80, True, floor_strike=63202.05)
    assert row["floor_strike"] == 63202.05


def test_candlestick_to_row_resolved_no():
    row = pull_history.candlestick_to_row(_candle(), "TICK", 65000.0, 0.80, False)
    assert row["resolved_yes"] == 0


def test_candlestick_to_row_dollar_strings_not_cents():
    # Regression test for the original bug: prices are ALREADY dollar
    # strings ("0.6200" == $0.62), not cents needing a /100 conversion.
    row = pull_history.candlestick_to_row(_candle(yes_ask="0.0100"), "TICK", 65000.0, 0.80, True)
    assert row["yes_ask"] == 0.01  # NOT 0.0001


def test_candlestick_to_row_none_when_yes_ask_missing():
    assert pull_history.candlestick_to_row(_candle(yes_ask=None), "TICK", 65000.0, 0.80, True) is None


def test_candlestick_to_row_none_when_yes_bid_missing():
    assert pull_history.candlestick_to_row(_candle(yes_bid=None), "TICK", 65000.0, 0.80, True) is None


def test_candlestick_to_row_none_when_no_resting_bid_observed():
    # yes_bid all-zero -- must be dropped, not treated as a real $0 bid.
    row = pull_history.candlestick_to_row(_candle(yes_bid="0.0000"), "TICK", 65000.0, 0.80, True)
    assert row is None


def test_candlestick_to_row_none_when_yes_ask_zero():
    row = pull_history.candlestick_to_row(_candle(yes_ask="0.0000"), "TICK", 65000.0, 0.80, True)
    assert row is None


def test_candlestick_to_row_none_when_timestamp_missing():
    candle = {"yes_ask": {"close_dollars": "0.5"}, "yes_bid": {"close_dollars": "0.4"}}
    assert pull_history.candlestick_to_row(candle, "TICK", 65000.0, 0.80, True) is None


def test_candlestick_to_row_no_ask_derived_from_yes_bid():
    row = pull_history.candlestick_to_row(_candle(yes_bid="0.3500"), "TICK", 65000.0, 0.80, True)
    assert row["no_ask"] == pytest.approx(0.65)


# ── nearest_btc_sample (2026-07: strictly causal, look-ahead fix) ───────────
# Confirmed live 2026-07: the OLD "nearest in either direction" version
# picked a sample from AFTER the query timestamp in 41% of 500 real
# KXBTC15M row checks, by up to 120s -- leaking future spot into that
# row's fair-value input. Fixed to only ever return a sample strictly
# before `ts`; every test below now asserts that contract, not "closest".

def test_nearest_btc_sample_returns_most_recent_before_ts():
    history = [(100.0, 60000.0), (200.0, 61000.0), (300.0, 62000.0)]
    # 190 is closest to 200 (61000), but 200 is AFTER 190 -- must return
    # the sample at 100 (the most recent one strictly before 190), not 61000.
    assert pull_history.nearest_btc_sample(history, 190.0) == 60000.0
    assert pull_history.nearest_btc_sample(history, 305.0) == 62000.0


def test_nearest_btc_sample_empty_history():
    assert pull_history.nearest_btc_sample([], 100.0) is None


def test_nearest_btc_sample_ts_before_all_samples_returns_none():
    # No sample exists strictly before ts=0.0 -- must not fabricate one by
    # reaching into the future (the old behavior returned the first sample,
    # which is AFTER ts here).
    history = [(100.0, 60000.0), (200.0, 61000.0)]
    assert pull_history.nearest_btc_sample(history, 0.0) is None


def test_nearest_btc_sample_ts_after_all_samples():
    history = [(100.0, 60000.0), (200.0, 61000.0)]
    assert pull_history.nearest_btc_sample(history, 1000.0) == 61000.0


def test_nearest_btc_sample_exact_match_excludes_the_exact_sample():
    # ts=200.0 exactly matches the second sample's own timestamp -- STRICTLY
    # less than ts required, so that sample must be excluded, not returned.
    history = [(100.0, 60000.0), (200.0, 61000.0), (300.0, 62000.0)]
    assert pull_history.nearest_btc_sample(history, 200.0) == 60000.0


def test_nearest_btc_sample_never_returns_a_future_sample():
    """
    The specific regression this fix exists for: for every (ts, expected
    causal answer) pair, the returned sample's timestamp must be strictly
    less than ts -- proven both by direct assertion and by cross-checking
    against a brute-force causal-only reference implementation, across
    ts values landing before, between, on, and after every sample.
    """
    history = [(100.0, 60000.0), (200.0, 61000.0), (300.0, 62000.0), (400.0, 63000.0)]

    def _causal_brute_force(hist, ts):
        before = [s for s in hist if s[0] < ts]
        return max(before, key=lambda s: s[0])[1] if before else None

    probe_timestamps = [0.0, 50.0, 100.0, 150.0, 199.999, 200.0, 200.001, 250.0, 300.0, 305.0, 400.0, 500.0]
    for ts in probe_timestamps:
        result = pull_history.nearest_btc_sample(history, ts)
        expected = _causal_brute_force(history, ts)
        assert result == expected, f"ts={ts}: got {result}, expected {expected}"
        if result is not None:
            # Recover the timestamp of whichever sample produced `result`
            # and assert it directly -- the actual "no future sample" proof.
            matching = [s for s in history if s[1] == result]
            assert all(s[0] < ts for s in matching), f"ts={ts}: sample at {matching} is not strictly before ts"


def test_nearest_btc_sample_matches_causal_brute_force_on_random_data():
    import random

    rng = random.Random(42)
    # Unique prices (sample without replacement) so a price->timestamp
    # reverse-lookup below is unambiguous -- randint could otherwise
    # produce duplicate prices at different timestamps.
    prices = rng.sample(range(50_000, 70_000), 500)
    timestamps = sorted(rng.sample(range(0, 100_000), 500))
    history = list(zip((float(t) for t in timestamps), (float(p) for p in prices)))
    price_to_ts = {p: t for t, p in history}

    def _causal_brute_force(hist, ts):
        before = [s for s in hist if s[0] < ts]
        return max(before, key=lambda s: s[0])[1] if before else None

    for _ in range(200):
        ts = rng.uniform(-10_000, 110_000)
        result = pull_history.nearest_btc_sample(history, ts)
        assert result == _causal_brute_force(history, ts)
        if result is not None:
            assert price_to_ts[result] < ts


# ── realized_vol_at (2026-07 calibration fix, replaces rolling_vol_at) ────────

def _vol_cfg(**kwargs):
    from kbot.config import Settings
    return Settings(
        kalshi_key_id="test", kalshi_env="demo", dry_run=True,
        vol_multiplier=kwargs.get("vol_multiplier", 1.5),
        vol_floor=kwargs.get("vol_floor", 0.35),
    )


def test_realized_vol_at_always_returns_a_value_even_with_no_samples():
    # Unlike the old rolling_vol_at (returned None below 2 samples),
    # realized_vol_at always returns something usable -- the vol_floor.
    cfg = _vol_cfg(vol_floor=0.35)
    vol = pull_history.realized_vol_at([], [], 100.0, cfg)
    assert vol == pytest.approx(0.35)


def test_realized_vol_at_constant_price_hits_floor():
    cfg = _vol_cfg(vol_multiplier=1.5, vol_floor=0.35)
    history = [(t, 60000.0) for t in range(0, 3600, 300)]
    vol = pull_history.realized_vol_at(history, [], 3300.0, cfg, window_seconds=3600.0)
    assert vol == pytest.approx(0.35)  # zero realized vol -> floor wins


def test_realized_vol_at_excludes_samples_outside_window():
    cfg = _vol_cfg(vol_multiplier=1.0, vol_floor=0.0)
    history = [(0.0, 100000.0), (3000.0, 60000.0), (3300.0, 61000.0)]
    vol = pull_history.realized_vol_at(history, [], 3300.0, cfg, window_seconds=600.0)
    manual = pull_history.realized_vol_at(
        [(3000.0, 60000.0), (3300.0, 61000.0)], [], 3300.0, cfg, window_seconds=600.0
    )
    assert vol == pytest.approx(manual)
    assert not math.isnan(vol)


def test_realized_vol_at_positive_for_volatile_history():
    cfg = _vol_cfg(vol_multiplier=1.0, vol_floor=0.0)
    history = [(0.0, 60000.0), (300.0, 61000.0), (600.0, 59000.0), (900.0, 62000.0)]
    vol = pull_history.realized_vol_at(history, [], 900.0, cfg, window_seconds=3600.0)
    assert vol > 0


def test_realized_vol_at_uses_parkinson_when_it_exceeds_ewma():
    cfg = _vol_cfg(vol_multiplier=1.0, vol_floor=0.0)
    # Flat closes (EWMA ~ 0) but a wide intra-bar range each period -> Parkinson
    # should dominate and produce a positive vol that a close-only estimator
    # would have missed entirely.
    closes = [(t, 60000.0) for t in range(0, 3600, 300)]
    ohlc = [(t, 61000.0, 59000.0) for t in range(0, 3600, 300)]
    vol_close_only = pull_history.realized_vol_at(closes, [], 3300.0, cfg, window_seconds=3600.0)
    vol_with_range = pull_history.realized_vol_at(closes, ohlc, 3300.0, cfg, window_seconds=3600.0)
    assert vol_close_only == pytest.approx(0.0, abs=1e-9)
    assert vol_with_range > vol_close_only


# ── realized_vol_at multiplier + series-aware window (2026-07) ──────────────

def test_realized_vol_at_multiplier_override_replaces_cfg_value():
    cfg = _vol_cfg(vol_multiplier=1.5, vol_floor=0.0)
    history = [(0.0, 60000.0), (300.0, 61000.0), (600.0, 59000.0), (900.0, 62000.0)]
    default_multiplier = pull_history.realized_vol_at(history, [], 900.0, cfg, window_seconds=3600.0)
    overridden = pull_history.realized_vol_at(history, [], 900.0, cfg, window_seconds=3600.0, multiplier=1.0)
    assert overridden == pytest.approx(default_multiplier / 1.5)


def test_realized_vol_at_different_windows_give_different_results_on_same_history():
    """
    The specific regression this exists for: computing realized_vol_at for
    the SAME (close_samples, ohlc_bars, ts) with a 900s window vs a 3600s
    window must be able to differ -- proving the window argument actually
    reaches the underlying ewma_vol/parkinson_vol filtering, not just a
    cosmetic parameter that gets ignored.
    """
    cfg = _vol_cfg(vol_multiplier=1.0, vol_floor=0.0)
    # A shock early in the hour, calm for the last 15 minutes.
    history = [
        (0.0, 60000.0), (600.0, 66000.0), (1200.0, 60100.0),  # shock + revert, first 20 min
        (2700.0, 60000.0), (3000.0, 60010.0), (3300.0, 59990.0), (3600.0, 60005.0),  # calm last 15 min
    ]
    vol_15m = pull_history.realized_vol_at(history, [], 3600.0, cfg, window_seconds=900.0)
    vol_hourly = pull_history.realized_vol_at(history, [], 3600.0, cfg, window_seconds=3600.0)
    assert vol_hourly > vol_15m * 2


# ── resolved_yes_from_events ─────────────────────────────────────────────────

def test_resolved_yes_from_events_finds_yes():
    events = [{"markets": [{"ticker": "TICK-A", "result": "yes"}]}]
    assert pull_history.resolved_yes_from_events(events, "TICK-A") is True


def test_resolved_yes_from_events_finds_no():
    events = [{"markets": [{"ticker": "TICK-A", "result": "no"}]}]
    assert pull_history.resolved_yes_from_events(events, "TICK-A") is False


def test_resolved_yes_from_events_unresolved_market_returns_none():
    events = [{"markets": [{"ticker": "TICK-A", "result": ""}]}]
    assert pull_history.resolved_yes_from_events(events, "TICK-A") is None


def test_resolved_yes_from_events_ticker_not_found_returns_none():
    events = [{"markets": [{"ticker": "OTHER", "result": "yes"}]}]
    assert pull_history.resolved_yes_from_events(events, "TICK-A") is None


def test_resolved_yes_from_events_empty_events():
    assert pull_history.resolved_yes_from_events([], "TICK-A") is None


# ── build_resolved_index ──────────────────────────────────────────────────────

def test_build_resolved_index_maps_yes_and_no():
    events = [{"markets": [
        {"ticker": "A", "result": "yes"},
        {"ticker": "B", "result": "no"},
    ]}]
    index = pull_history.build_resolved_index(events)
    assert index == {"A": True, "B": False}


def test_build_resolved_index_excludes_unresolved():
    events = [{"markets": [{"ticker": "A", "result": ""}, {"ticker": "B"}]}]
    index = pull_history.build_resolved_index(events)
    assert index == {}


def test_build_resolved_index_spans_multiple_events():
    events = [
        {"markets": [{"ticker": "A", "result": "yes"}]},
        {"markets": [{"ticker": "B", "result": "no"}]},
    ]
    index = pull_history.build_resolved_index(events)
    assert index == {"A": True, "B": False}


def test_build_resolved_index_empty_events():
    assert pull_history.build_resolved_index([]) == {}


def test_build_resolved_index_matches_resolved_yes_from_events():
    events = [{"markets": [
        {"ticker": "A", "result": "yes"},
        {"ticker": "B", "result": "no"},
        {"ticker": "C", "result": ""},
    ]}]
    index = pull_history.build_resolved_index(events)
    for ticker in ("A", "B", "C"):
        assert index.get(ticker) == pull_history.resolved_yes_from_events(events, ticker)


# ── _resolve_against_strike (2026-07: strike_type conventions, verified live) ─

def test_resolve_against_strike_less():
    assert pull_history._resolve_against_strike(50000, "less", None, 53200) is True
    assert pull_history._resolve_against_strike(60000, "less", None, 53200) is False


def test_resolve_against_strike_less_or_equal():
    assert pull_history._resolve_against_strike(53200, "less_or_equal", None, 53200) is True
    assert pull_history._resolve_against_strike(53201, "less_or_equal", None, 53200) is False


def test_resolve_against_strike_greater():
    assert pull_history._resolve_against_strike(72500, "greater", 71799.99, None) is True
    assert pull_history._resolve_against_strike(71000, "greater", 71799.99, None) is False


def test_resolve_against_strike_greater_or_equal():
    # KXBTC15M's convention -- exact equality counts as YES.
    assert pull_history._resolve_against_strike(63202.05, "greater_or_equal", 63202.05, None) is True
    assert pull_history._resolve_against_strike(63202.04, "greater_or_equal", 63202.05, None) is False


def test_resolve_against_strike_between():
    assert pull_history._resolve_against_strike(53250, "between", 53200, 53299.99) is True
    assert pull_history._resolve_against_strike(53300, "between", 53200, 53299.99) is False


def test_resolve_against_strike_missing_fields_returns_none():
    assert pull_history._resolve_against_strike(50000, "less", None, None) is None
    assert pull_history._resolve_against_strike(50000, "greater", None, None) is None
    assert pull_history._resolve_against_strike(50000, "between", 100, None) is None


def test_resolve_against_strike_unknown_type_returns_none():
    assert pull_history._resolve_against_strike(50000, "some_new_type", 100, 200) is None


# ── resolved_yes_from_market (2026-07: result > expiration_value > spot) ─────

def test_resolved_yes_from_market_prefers_result():
    market = {"result": "yes", "expiration_value": "40000", "strike_type": "greater", "floor_strike": 50000}
    # expiration_value would say NO (40000 < 50000) -- result must win.
    assert pull_history.resolved_yes_from_market(market) is True


def test_resolved_yes_from_market_falls_back_to_expiration_value():
    market = {"result": "", "expiration_value": "63202.05", "strike_type": "greater_or_equal", "floor_strike": 63202.05}
    assert pull_history.resolved_yes_from_market(market) is True


def test_resolved_yes_from_market_expiration_value_no_result():
    market = {"result": "", "expiration_value": "63202.04", "strike_type": "greater_or_equal", "floor_strike": 63202.05}
    assert pull_history.resolved_yes_from_market(market) is False


def test_resolved_yes_from_market_falls_back_to_spot_as_last_resort():
    market = {"result": "", "expiration_value": "", "strike_type": "greater_or_equal", "floor_strike": 63202.05}
    assert pull_history.resolved_yes_from_market(market, spot_near_close=64000.0) is True
    assert pull_history.resolved_yes_from_market(market, spot_near_close=60000.0) is False


def test_resolved_yes_from_market_prefers_expiration_value_over_spot():
    market = {"result": "", "expiration_value": "63202.05", "strike_type": "greater_or_equal", "floor_strike": 63202.05}
    # spot disagrees (would say NO) -- expiration_value must still win.
    assert pull_history.resolved_yes_from_market(market, spot_near_close=1000.0) is True


def test_resolved_yes_from_market_none_when_nothing_resolves():
    market = {"result": "", "expiration_value": "", "strike_type": "greater_or_equal", "floor_strike": 63202.05}
    assert pull_history.resolved_yes_from_market(market) is None


def test_resolved_yes_from_market_malformed_expiration_value_falls_through():
    market = {"result": "", "expiration_value": "not-a-number", "strike_type": "greater_or_equal", "floor_strike": 63202.05}
    assert pull_history.resolved_yes_from_market(market) is None
    assert pull_history.resolved_yes_from_market(market, spot_near_close=64000.0) is True


# ── filter_markets_by_resolution ──────────────────────────────────────────────

def test_filter_by_resolution_keeps_only_resolved_yes():
    markets = {"WON": {}, "LOST": {}, "OPEN": {}}
    resolved_index = {"WON": True, "LOST": False}  # OPEN absent -- still unresolved
    kept = pull_history.filter_markets_by_resolution(markets, resolved_index)
    assert kept == {"WON": markets["WON"]}


def test_filter_by_resolution_empty_index_keeps_nothing():
    markets = {"A": {}, "B": {}}
    assert pull_history.filter_markets_by_resolution(markets, {}) == {}


def test_filter_by_resolution_empty_markets():
    assert pull_history.filter_markets_by_resolution({}, {"A": True}) == {}


# ── is_btc_series (2026-07 base_rate / --series pull path) ───────────────────

def test_is_btc_series_true_for_kxbtc():
    assert pull_history.is_btc_series("KXBTC") is True


def test_is_btc_series_case_insensitive():
    assert pull_history.is_btc_series("kxbtc") is True
    assert pull_history.is_btc_series("KxBtc") is True


def test_is_btc_series_false_for_macro_series():
    assert pull_history.is_btc_series("KXFEDDECISION") is False
    assert pull_history.is_btc_series("KXCPI") is False
    assert pull_history.is_btc_series("KXPAYROLLS") is False


def test_is_btc_series_false_for_similar_looking_series():
    # Must be an exact match, not a substring/prefix check.
    assert pull_history.is_btc_series("KXBTCUSD") is False
    assert pull_history.is_btc_series("KX") is False
    assert pull_history.is_btc_series("") is False


# ── market_time_window ────────────────────────────────────────────────────────

def test_market_time_window_basic():
    market = {"open_time": "2026-07-01T20:00:00Z", "close_time": "2026-07-02T21:00:00Z"}
    window = pull_history.market_time_window(market, floor_ts=0, ceil_ts=2_000_000_000)
    assert window is not None
    start, end = window
    assert start < end


def test_market_time_window_clamped_to_floor_and_ceil():
    market = {"open_time": "2026-07-01T20:00:00Z", "close_time": "2026-07-02T21:00:00Z"}
    open_ts = int(pull_history.datetime.fromisoformat("2026-07-01T20:00:00+00:00").timestamp())
    close_ts = int(pull_history.datetime.fromisoformat("2026-07-02T21:00:00+00:00").timestamp())
    mid_ts = (open_ts + close_ts) // 2

    window = pull_history.market_time_window(market, floor_ts=mid_ts, ceil_ts=close_ts + 1000)
    assert window == (mid_ts, close_ts)


def test_market_time_window_none_when_missing_fields():
    assert pull_history.market_time_window({}, 0, 100) is None
    assert pull_history.market_time_window({"open_time": "2026-07-01T20:00:00Z"}, 0, 100) is None


def test_market_time_window_none_when_outside_range():
    market = {"open_time": "2026-07-01T20:00:00Z", "close_time": "2026-07-02T21:00:00Z"}
    window = pull_history.market_time_window(market, floor_ts=0, ceil_ts=1)  # long before the market existed
    assert window is None


# ── filter_markets_by_close_window ────────────────────────────────────────────

def _market(close_time: str | None) -> dict:
    m = {"open_time": "2026-06-01T00:00:00Z"}
    if close_time is not None:
        m["close_time"] = close_time
    return m


def test_filter_markets_keeps_close_time_within_window():
    start_ts = int(pull_history.datetime.fromisoformat("2026-07-01T00:00:00+00:00").timestamp())
    end_ts = int(pull_history.datetime.fromisoformat("2026-07-15T00:00:00+00:00").timestamp())
    markets = {"IN-WINDOW": _market("2026-07-10T00:00:00Z")}
    kept = pull_history.filter_markets_by_close_window(markets, start_ts, end_ts)
    assert "IN-WINDOW" in kept


def test_filter_markets_drops_expired_market_before_window():
    start_ts = int(pull_history.datetime.fromisoformat("2026-07-01T00:00:00+00:00").timestamp())
    end_ts = int(pull_history.datetime.fromisoformat("2026-07-15T00:00:00+00:00").timestamp())
    markets = {"EXPIRED": _market("2026-01-01T00:00:00Z")}
    kept = pull_history.filter_markets_by_close_window(markets, start_ts, end_ts)
    assert kept == {}


def test_filter_markets_drops_market_closing_after_window():
    start_ts = int(pull_history.datetime.fromisoformat("2026-07-01T00:00:00+00:00").timestamp())
    end_ts = int(pull_history.datetime.fromisoformat("2026-07-15T00:00:00+00:00").timestamp())
    markets = {"FUTURE": _market("2026-12-01T00:00:00Z")}
    kept = pull_history.filter_markets_by_close_window(markets, start_ts, end_ts)
    assert kept == {}


def test_filter_markets_drops_missing_close_time():
    markets = {"NO-CLOSE": _market(None)}
    kept = pull_history.filter_markets_by_close_window(markets, 0, 2_000_000_000)
    assert kept == {}


def test_filter_markets_mixed_keeps_only_in_window():
    start_ts = int(pull_history.datetime.fromisoformat("2026-07-01T00:00:00+00:00").timestamp())
    end_ts = int(pull_history.datetime.fromisoformat("2026-07-15T00:00:00+00:00").timestamp())
    markets = {
        "IN": _market("2026-07-10T00:00:00Z"),
        "EXPIRED": _market("2026-01-01T00:00:00Z"),
        "NO-CLOSE": _market(None),
    }
    kept = pull_history.filter_markets_by_close_window(markets, start_ts, end_ts)
    assert kept == {"IN": markets["IN"]}


# ── filter_markets_by_liquidity ───────────────────────────────────────────────

@pytest.fixture()
def strategy():
    return pull_history.BTCHourlyStrategy(pull_history.settings)


def _btc_market(ticker: str, open_time: str = "2026-07-01T00:00:00Z", close_time: str = "2026-07-01T01:00:00Z") -> dict:
    return {"open_time": open_time, "close_time": close_time}


def _mid_ts(market: dict) -> float:
    start = pull_history._parse_iso_ts(market["open_time"])
    end = pull_history._parse_iso_ts(market["close_time"])
    return (start + end) / 2.0


def test_liquidity_filter_keeps_near_atm_market(strategy):
    market = _btc_market("KXBTC-26JUL0100-B59150")
    mid_ts = _mid_ts(market)
    markets = {"KXBTC-26JUL0100-B59150": market}
    # Sample strictly BEFORE mid_ts -- nearest_btc_sample is causal-only
    # (2026-07 look-ahead fix); a sample AT mid_ts would resolve to None
    # and this test would then only be exercising the "no spot -> keep"
    # fallback, not the near-ATM check it claims to test.
    btc_history = [(mid_ts - 1.0, 59200.0)]  # $50 from the $59,150 strike -- well within default 8*$500=$4000

    kept = pull_history.filter_markets_by_liquidity(markets, strategy, btc_history)
    assert "KXBTC-26JUL0100-B59150" in kept


def test_liquidity_filter_drops_far_otm_market(strategy):
    market = _btc_market("KXBTC-26JUL0100-B59150")
    mid_ts = _mid_ts(market)
    markets = {"KXBTC-26JUL0100-B59150": market}
    # Sample strictly BEFORE mid_ts -- nearest_btc_sample is causal-only
    # (2026-07 look-ahead fix) and would return None for a sample AT mid_ts.
    btc_history = [(mid_ts - 1.0, 40000.0)]  # $19,150 from strike -- way beyond default $4000 band

    kept = pull_history.filter_markets_by_liquidity(markets, strategy, btc_history)
    assert kept == {}


def test_liquidity_filter_disabled_with_strikes_zero(strategy):
    market = _btc_market("KXBTC-26JUL0100-B59150")
    mid_ts = _mid_ts(market)
    markets = {"KXBTC-26JUL0100-B59150": market}
    btc_history = [(mid_ts, 40000.0)]  # would be dropped at default strikes, but filter is off

    kept = pull_history.filter_markets_by_liquidity(markets, strategy, btc_history, strikes=0)
    assert kept == markets


def test_liquidity_filter_keeps_market_when_no_btc_sample(strategy):
    market = _btc_market("KXBTC-26JUL0100-B59150")
    markets = {"KXBTC-26JUL0100-B59150": market}

    kept = pull_history.filter_markets_by_liquidity(markets, strategy, btc_history=[])
    assert "KXBTC-26JUL0100-B59150" in kept


def test_liquidity_filter_keeps_unparseable_ticker(strategy):
    market = _btc_market("NOT-A-REAL-TICKER")
    mid_ts = _mid_ts(market)
    markets = {"NOT-A-REAL-TICKER": market}
    btc_history = [(mid_ts, 40000.0)]

    kept = pull_history.filter_markets_by_liquidity(markets, strategy, btc_history)
    assert "NOT-A-REAL-TICKER" in kept


def test_liquidity_filter_keeps_market_with_missing_time_fields(strategy):
    market = {"open_time": "2026-07-01T00:00:00Z"}  # no close_time
    markets = {"KXBTC-26JUL0100-B59150": market}

    kept = pull_history.filter_markets_by_liquidity(markets, strategy, btc_history=[(0.0, 40000.0)])
    assert "KXBTC-26JUL0100-B59150" in kept


def test_liquidity_filter_custom_strikes_width(strategy):
    market = _btc_market("KXBTC-26JUL0100-B59150")
    mid_ts = _mid_ts(market)
    markets = {"KXBTC-26JUL0100-B59150": market}
    # Sample strictly BEFORE mid_ts -- nearest_btc_sample is causal-only
    # (2026-07 look-ahead fix) and would return None for a sample AT mid_ts.
    btc_history = [(mid_ts - 1.0, 61150.0)]  # $2,000 from strike

    # 2 strikes = $1000 band -- $2000 away is dropped.
    assert pull_history.filter_markets_by_liquidity(markets, strategy, btc_history, strikes=2) == {}
    # 4 strikes = $2000 band -- $2000 away is right at the edge, kept.
    kept = pull_history.filter_markets_by_liquidity(markets, strategy, btc_history, strikes=4)
    assert "KXBTC-26JUL0100-B59150" in kept


# ── _fetch_chunk_with_retry (429 backoff) ─────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHTTPStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        self.response = _FakeResponse(status_code)


class _FlakyClient:
    """Fake KalshiClient.get_candlesticks that fails N times with 429, then succeeds."""

    def __init__(self, fail_times: int, final_status: int | None = None) -> None:
        self.fail_times = fail_times
        self.final_status = final_status  # if set, always fails with this status
        self.calls = 0

    def get_candlesticks(self, series, ticker, *, start_ts, end_ts, period_interval):
        self.calls += 1
        if self.final_status is not None:
            raise pull_history.httpx.HTTPStatusError(
                "boom", request=None, response=_FakeResponse(self.final_status)
            )
        if self.calls <= self.fail_times:
            raise pull_history.httpx.HTTPStatusError(
                "rate limited", request=None, response=_FakeResponse(429)
            )
        return [{"end_period_ts": 123}]


def test_fetch_chunk_with_retry_succeeds_after_transient_429s(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(pull_history.time, "sleep", lambda s: sleeps.append(s))

    client = _FlakyClient(fail_times=2)
    result = pull_history._fetch_chunk_with_retry(client, "KXBTC", "TICK", 0, 100, 60)

    assert result == [{"end_period_ts": 123}]
    assert client.calls == 3  # 2 failures + 1 success
    assert sleeps == [2.0, 4.0]  # exponential backoff: 2s, 4s


def test_fetch_chunk_with_retry_gives_up_after_max_attempts(monkeypatch, capsys):
    sleeps: list[float] = []
    monkeypatch.setattr(pull_history.time, "sleep", lambda s: sleeps.append(s))

    client = _FlakyClient(fail_times=999)  # always 429
    result = pull_history._fetch_chunk_with_retry(client, "KXBTC", "TICK", 0, 100, 60, max_attempts=5)

    assert result is None
    assert client.calls == 5
    assert sleeps == [2.0, 4.0, 8.0, 16.0]  # 4 waits between 5 attempts, none after the last
    assert "still rate-limited" in capsys.readouterr().out


def test_fetch_chunk_with_retry_backoff_capped_at_30s(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(pull_history.time, "sleep", lambda s: sleeps.append(s))

    client = _FlakyClient(fail_times=999)
    pull_history._fetch_chunk_with_retry(client, "KXBTC", "TICK", 0, 100, 60, max_attempts=8)

    assert max(sleeps) <= 30.0
    assert sleeps[-1] == 30.0  # 2,4,8,16,30(capped),30,30 for a 7-wait sequence


def test_fetch_chunk_with_retry_non_429_raises_immediately(monkeypatch):
    monkeypatch.setattr(pull_history.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))

    client = _FlakyClient(fail_times=0, final_status=500)
    try:
        pull_history._fetch_chunk_with_retry(client, "KXBTC", "TICK", 0, 100, 60)
        assert False, "expected HTTPStatusError to propagate"
    except pull_history.httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500
    assert client.calls == 1  # no retry attempted for a non-429 error
