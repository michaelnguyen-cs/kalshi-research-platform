"""Tests for kbot.feeds.btc — price feed and vol estimation."""

import math
import time

import pytest
import httpx

from kbot.feeds.btc import BTCFeed


# ── Constructor validation ────────────────────────────────────────────────────

def test_rejects_sub_second_interval():
    with pytest.raises(ValueError, match=">="):
        BTCFeed(poll_interval=0.5)


def test_default_properties_before_fetch():
    feed = BTCFeed(poll_interval=60.0)
    assert feed.price is None
    assert feed.vol_annualised is None
    assert feed.history == []


# ── After injecting prices directly ──────────────────────────────────────────

def _feed_with_prices(prices: list[float], interval: float = 60.0) -> BTCFeed:
    """Build a feed and inject prices directly into the history deque."""
    feed = BTCFeed(poll_interval=interval)
    ts = time.time()
    for i, p in enumerate(prices):
        feed._history.append((ts + i * interval, p))
    return feed


def test_price_returns_last():
    feed = _feed_with_prices([60000, 61000, 62000])
    assert feed.price == 62000


def test_vol_none_with_single_price():
    feed = _feed_with_prices([60000])
    assert feed.vol_annualised is None


def test_vol_none_with_empty_history():
    feed = BTCFeed(poll_interval=60.0)
    assert feed.vol_annualised is None


def test_vol_positive_with_multiple_prices():
    # Any real price series should produce positive vol
    feed = _feed_with_prices([60000, 61000, 59500, 62000, 60500])
    vol = feed.vol_annualised
    assert vol is not None
    assert vol > 0


def test_vol_zero_for_flat_series():
    # Identical prices → log-returns = 0 → std = 0 → vol = 0
    feed = _feed_with_prices([60000] * 10)
    vol = feed.vol_annualised
    assert vol == 0.0


def test_vol_annualisation():
    # For a known per-sample std, check annualisation factor.
    # Two prices with a specific return:
    import math
    p0, p1 = 60000.0, 60000.0 * math.exp(0.01)   # log-return = 0.01
    feed = _feed_with_prices([p0, p1], interval=60.0)
    # With 1 return of 0.01 and mean ≈ 0.01, variance ≈ 0 (only 1 data point)
    # but let's at least check it produces a float
    assert isinstance(feed.vol_annualised, float)


def test_history_snapshot():
    feed = _feed_with_prices([60000, 61000])
    h = feed.history
    assert len(h) == 2
    assert h[0][1] == 60000
    assert h[1][1] == 61000


# ── fetch_once with mocked HTTP ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_once_success(mocker):
    import httpx

    feed = BTCFeed(poll_interval=1.0)
    fake_response = mocker.MagicMock(spec=httpx.Response)
    fake_response.json.return_value = {"data": {"amount": "65432.10"}}
    fake_response.raise_for_status = mocker.MagicMock()
    mocker.patch.object(feed._client, "get", return_value=fake_response)

    price = await feed.fetch_once()
    assert price == pytest.approx(65432.10)
    assert feed.price == pytest.approx(65432.10)


@pytest.mark.asyncio
async def test_fetch_once_error_returns_none(mocker):
    feed = BTCFeed(poll_interval=1.0)
    # Patch the async client's get to raise
    mocker.patch.object(feed._client, "get", side_effect=httpx.ConnectError("timeout"))
    price = await feed.fetch_once()
    assert price is None
    assert feed.price is None   # history unchanged


# ── spot_move_bps / has_spot_trigger (Phase A4 item 5, Source 6) ────────────────

def _feed_at_ts(samples: list[tuple[float, float]], interval: float = 1.0) -> BTCFeed:
    """Build a feed with explicit (timestamp, price) samples."""
    feed = BTCFeed(poll_interval=interval)
    for ts, price in samples:
        feed._history.append((ts, price))
    return feed


def test_spot_move_bps_none_with_empty_history():
    feed = BTCFeed(poll_interval=1.0)
    assert feed.spot_move_bps(3.0) is None


def test_spot_move_bps_zero_with_single_sample():
    # Only one observation exists -- it's compared against itself -> 0 bps.
    feed = _feed_at_ts([(0.0, 60000.0)])
    assert feed.spot_move_bps(3.0, now=0.0) == pytest.approx(0.0)


def test_spot_move_bps_none_when_earliest_sample_outside_window():
    # The only sample is older than the window -> nothing to compare against.
    feed = _feed_at_ts([(0.0, 60000.0)])
    assert feed.spot_move_bps(3.0, now=10.0) is None


def test_spot_move_bps_positive_for_upward_move():
    feed = _feed_at_ts([(0.0, 60000.0), (1.0, 60060.0), (2.0, 60600.0)])
    move = feed.spot_move_bps(3.0, now=2.0)
    # (60600 - 60000) / 60000 * 10000 = 100 bps
    assert move == pytest.approx(100.0, rel=1e-6)


def test_spot_move_bps_negative_for_downward_move():
    feed = _feed_at_ts([(0.0, 60000.0), (1.0, 59400.0)])
    move = feed.spot_move_bps(3.0, now=1.0)
    assert move == pytest.approx(-100.0, rel=1e-6)


def test_spot_move_bps_ignores_samples_outside_window():
    # Sample at ts=0 is far outside a 3s window measured from now=10.
    feed = _feed_at_ts([(0.0, 100000.0), (9.0, 60000.0), (10.0, 60600.0)])
    move = feed.spot_move_bps(3.0, now=10.0)
    # Earliest sample INSIDE (now-3, now] = (7, 10] is (9.0, 60000.0).
    assert move == pytest.approx(100.0, rel=1e-6)


def test_has_spot_trigger_true_above_threshold():
    feed = _feed_at_ts([(0.0, 60000.0), (1.0, 60600.0)])  # +100 bps
    assert feed.has_spot_trigger(threshold_bps=10.0, window_seconds=3.0, now=1.0) is True


def test_has_spot_trigger_false_below_threshold():
    feed = _feed_at_ts([(0.0, 60000.0), (1.0, 60006.0)])  # +1 bps
    assert feed.has_spot_trigger(threshold_bps=10.0, window_seconds=3.0, now=1.0) is False


def test_has_spot_trigger_true_for_large_downward_move():
    feed = _feed_at_ts([(0.0, 60000.0), (1.0, 59000.0)])  # ~-167 bps
    assert feed.has_spot_trigger(threshold_bps=10.0, window_seconds=3.0, now=1.0) is True


# ── history retention sized from vol_window_seconds (2026-07) ───────────────

def test_history_len_derived_from_vol_window_and_poll_interval():
    # 3600s window / 2s polling -> at least 1800 samples retained, not the
    # old fixed default of 60 (which at 2s polling only covered 120s --
    # nowhere near enough for a 3600s window filter to have any data left).
    feed = BTCFeed(poll_interval=2.0, vol_window_seconds=3600.0)
    assert feed._history.maxlen >= 1800


def test_history_len_derived_for_15m_window():
    feed = BTCFeed(poll_interval=2.0, vol_window_seconds=900.0)
    assert feed._history.maxlen >= 450


def test_explicit_history_len_overrides_derived_sizing():
    feed = BTCFeed(poll_interval=2.0, vol_window_seconds=3600.0, history_len=25)
    assert feed._history.maxlen == 25


def test_history_len_has_a_sane_minimum_floor():
    # A huge poll_interval combined with a small window shouldn't derive
    # to something degenerately tiny.
    feed = BTCFeed(poll_interval=100.0, vol_window_seconds=100.0)
    assert feed._history.maxlen >= 60


# ── history_within (2026-07 series-aware vol window) ─────────────────────────

def test_history_within_empty_feed():
    feed = BTCFeed(poll_interval=60.0)
    assert feed.history_within(900.0) == []


def test_history_within_filters_to_trailing_window():
    feed = _feed_at_ts([(0.0, 60000.0), (500.0, 60100.0), (1000.0, 61000.0)])
    result = feed.history_within(600.0, now=1000.0)
    assert result == [(500.0, 60100.0), (1000.0, 61000.0)]


def test_history_within_defaults_now_to_latest_sample():
    feed = _feed_at_ts([(0.0, 60000.0), (500.0, 60100.0), (1000.0, 61000.0)])
    result = feed.history_within(600.0)  # now defaults to 1000.0, the last sample
    assert result == [(500.0, 60100.0), (1000.0, 61000.0)]


def test_history_within_no_cross_contamination_between_windows():
    """
    The specific regression this exists for: a 900s (15m) slice and a
    3600s (hourly) slice of the SAME underlying history must never be
    confused with each other -- the 15m slice must never include a
    sample the hourly-only range would exclude, and must be a strict
    subset whenever the underlying data actually differs between windows.
    """
    now = 3600.0
    feed = _feed_at_ts([(t, 60000.0 + t) for t in range(0, 3601, 300)])
    slice_15m = feed.history_within(900.0, now=now)
    slice_hourly = feed.history_within(3600.0, now=now)
    assert all(ts >= now - 900.0 for ts, _ in slice_15m)
    assert all(ts >= now - 3600.0 for ts, _ in slice_hourly)
    assert set(slice_15m).issubset(set(slice_hourly))
    assert len(slice_15m) < len(slice_hourly)


# ── WS tick ingest (2026-07 Fix 4: tick-level latency) ───────────────────────

def _ticker_msg(price: str) -> str:
    import json
    return json.dumps({"type": "ticker", "product_id": "BTC-USD", "price": price})


@pytest.mark.asyncio
async def test_ws_ticker_message_appends_to_same_history():
    feed = BTCFeed(poll_interval=2.0)
    await feed._handle_ws_message(_ticker_msg("65432.10"))
    assert feed.price == pytest.approx(65432.10)
    assert len(feed.history) == 1
    assert feed.ws_tick_count == 1


@pytest.mark.asyncio
async def test_ws_ticks_interleave_with_poll_samples():
    """Both ingest paths share one deque -- consumers must see a merged series."""
    feed = BTCFeed(poll_interval=2.0)
    feed._history.append((time.time(), 60000.0))       # as if from the REST poll
    await feed._handle_ws_message(_ticker_msg("60600.0"))
    assert len(feed.history) == 2
    assert feed.spot_move_bps(3.0) == pytest.approx(100.0, rel=1e-6)


@pytest.mark.asyncio
async def test_ws_ignores_non_ticker_message_types():
    feed = BTCFeed(poll_interval=2.0)
    import json
    await feed._handle_ws_message(json.dumps({"type": "subscriptions", "channels": []}))
    await feed._handle_ws_message(json.dumps({"type": "error", "message": "nope"}))
    await feed._handle_ws_message(json.dumps({"type": "heartbeat", "price": "1.0"}))
    assert feed.history == []
    assert feed.ws_tick_count == 0


@pytest.mark.asyncio
async def test_ws_ignores_malformed_and_nonpositive_prices():
    feed = BTCFeed(poll_interval=2.0)
    await feed._handle_ws_message("not json at all")
    await feed._handle_ws_message(_ticker_msg("abc"))
    await feed._handle_ws_message(_ticker_msg("0"))
    await feed._handle_ws_message(_ticker_msg("-5"))
    import json
    await feed._handle_ws_message(json.dumps({"type": "ticker"}))  # no price key
    assert feed.history == []
    assert feed.price is None


@pytest.mark.asyncio
async def test_ws_callback_is_throttled_but_history_is_not():
    """
    The trigger callback runs a full off-cycle evaluation; at tick rate that
    must not fire on every print. Ticks still all land in history.
    """
    calls = []
    feed = BTCFeed(poll_interval=2.0, on_price=lambda p, t: calls.append(p),
                   ws_callback_min_interval=60.0)
    for i in range(5):
        await feed._handle_ws_message(_ticker_msg(str(60000 + i)))
    assert len(feed.history) == 5     # every tick retained
    assert len(calls) == 1            # only the first fired the callback


@pytest.mark.asyncio
async def test_ws_callback_fires_per_tick_when_throttle_disabled():
    calls = []
    feed = BTCFeed(poll_interval=2.0, on_price=lambda p, t: calls.append(p),
                   ws_callback_min_interval=0.0)
    for i in range(4):
        await feed._handle_ws_message(_ticker_msg(str(60000 + i)))
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_ws_callback_may_be_async():
    calls = []

    async def on_price(price, ts):
        calls.append(price)

    feed = BTCFeed(poll_interval=2.0, on_price=on_price, ws_callback_min_interval=0.0)
    await feed._handle_ws_message(_ticker_msg("61000.0"))
    assert calls == [61000.0]


def test_enable_tick_history_grows_deque_preserving_samples():
    feed = BTCFeed(poll_interval=2.0, vol_window_seconds=3600.0)
    assert feed._history.maxlen < 50_000
    feed._history.append((1.0, 60000.0))
    feed._enable_tick_history()
    assert feed._history.maxlen == 50_000
    assert feed.history == [(1.0, 60000.0)]   # existing samples survive


def test_enable_tick_history_is_idempotent():
    feed = BTCFeed(poll_interval=2.0)
    feed._enable_tick_history()
    feed._history.append((1.0, 60000.0))
    feed._enable_tick_history()
    assert feed._history.maxlen == 50_000
    assert len(feed.history) == 1


def test_enable_tick_history_never_shrinks_an_explicit_larger_deque():
    feed = BTCFeed(poll_interval=2.0, history_len=80_000)
    feed._enable_tick_history()
    assert feed._history.maxlen == 80_000


# ── vol_annualised spacing guard for mixed tick/poll data ────────────────────

def test_vol_annualisation_uses_observed_spacing_not_poll_interval():
    """
    Nominal poll_interval is 2s but the samples are 100ms apart (tick rate).
    Tick returns are small because they cover 100ms, so annualising them with
    the nominal 2s factor would UNDERSTATE vol by sqrt(20) ~ 4.5x.
    """
    prices = [60000.0 * math.exp(0.0001 * ((-1) ** i)) for i in range(50)]
    tick_feed = BTCFeed(poll_interval=2.0)
    for i, p in enumerate(prices):
        tick_feed._history.append((i * 0.1, p))

    poll_feed = BTCFeed(poll_interval=2.0)
    for i, p in enumerate(prices):
        poll_feed._history.append((i * 2.0, p))

    # Same per-sample returns, 20x denser spacing -> 20x the samples per year
    # -> annualised vol scales by sqrt(20).
    assert tick_feed.vol_annualised == pytest.approx(
        poll_feed.vol_annualised * math.sqrt(20.0), rel=1e-6
    )


def test_vol_annualisation_falls_back_to_poll_interval_for_degenerate_timestamps():
    # All samples share a timestamp -> no observable spacing -> nominal interval.
    feed = BTCFeed(poll_interval=60.0)
    for p in [60000.0, 60100.0, 59900.0]:
        feed._history.append((5.0, p))
    vol = feed.vol_annualised
    assert vol is not None and vol > 0
