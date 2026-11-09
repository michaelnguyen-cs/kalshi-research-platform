"""
Tests for kbot.feeds.eth — ETH price feed and vol estimation.

Mirrors tests/test_feeds_btc.py case-for-case (the feed is a deliberate copy
of the BTC one -- see kbot.feeds.eth's docstring), with ETH-scale prices and
the ETH-USD product id. If you fix a bug in one feed, the paired test here
should fail too; if it doesn't, the two have drifted.
"""

import math
import time

import pytest
import httpx

from kbot.feeds.eth import ETHFeed


# ── Constructor validation ────────────────────────────────────────────────────

def test_rejects_sub_second_interval():
    with pytest.raises(ValueError, match=">="):
        ETHFeed(poll_interval=0.5)


def test_default_properties_before_fetch():
    feed = ETHFeed(poll_interval=60.0)
    assert feed.price is None
    assert feed.vol_annualised is None
    assert feed.history == []


# ── After injecting prices directly ──────────────────────────────────────────

def _feed_with_prices(prices: list[float], interval: float = 60.0) -> ETHFeed:
    """Build a feed and inject prices directly into the history deque."""
    feed = ETHFeed(poll_interval=interval)
    ts = time.time()
    for i, p in enumerate(prices):
        feed._history.append((ts + i * interval, p))
    return feed


def test_price_returns_last():
    feed = _feed_with_prices([1800, 1850, 1900])
    assert feed.price == 1900


def test_vol_none_with_single_price():
    feed = _feed_with_prices([1800])
    assert feed.vol_annualised is None


def test_vol_none_with_empty_history():
    feed = ETHFeed(poll_interval=60.0)
    assert feed.vol_annualised is None


def test_vol_positive_with_multiple_prices():
    feed = _feed_with_prices([1800, 1850, 1790, 1900, 1820])
    vol = feed.vol_annualised
    assert vol is not None
    assert vol > 0


def test_vol_zero_for_flat_series():
    feed = _feed_with_prices([1800] * 10)
    assert feed.vol_annualised == 0.0


def test_history_snapshot():
    feed = _feed_with_prices([1800, 1850])
    h = feed.history
    assert len(h) == 2
    assert h[0][1] == 1800
    assert h[1][1] == 1850


# ── fetch_once with mocked HTTP ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_once_success(mocker):
    feed = ETHFeed(poll_interval=1.0)
    fake_response = mocker.MagicMock(spec=httpx.Response)
    fake_response.json.return_value = {"data": {"amount": "1860.71"}}
    fake_response.raise_for_status = mocker.MagicMock()
    mocker.patch.object(feed._client, "get", return_value=fake_response)

    price = await feed.fetch_once()
    assert price == pytest.approx(1860.71)
    assert feed.price == pytest.approx(1860.71)


@pytest.mark.asyncio
async def test_fetch_once_error_returns_none(mocker):
    feed = ETHFeed(poll_interval=1.0)
    mocker.patch.object(feed._client, "get", side_effect=httpx.ConnectError("timeout"))
    price = await feed.fetch_once()
    assert price is None
    assert feed.price is None   # history unchanged


@pytest.mark.asyncio
async def test_fetch_once_hits_the_eth_spot_endpoint(mocker):
    """
    The one thing that must NOT be copied from the BTC feed: the product.
    A feed that silently polls BTC-USD would price every ETH contract off
    the wrong underlying and look completely healthy doing it.
    """
    feed = ETHFeed(poll_interval=1.0)
    fake_response = mocker.MagicMock(spec=httpx.Response)
    fake_response.json.return_value = {"data": {"amount": "1860.71"}}
    fake_response.raise_for_status = mocker.MagicMock()
    get = mocker.patch.object(feed._client, "get", return_value=fake_response)

    await feed.fetch_once()
    (url,), _kwargs = get.call_args
    assert url == "https://api.coinbase.com/v2/prices/ETH-USD/spot"
    assert "BTC-USD" not in url


def test_ws_subscribes_to_the_eth_product():
    from kbot.feeds.eth import _COINBASE_WS_URL, _WS_PRODUCT_ID
    assert _WS_PRODUCT_ID == "ETH-USD"
    assert _COINBASE_WS_URL == "wss://ws-feed.exchange.coinbase.com"


# ── spot_move_bps / has_spot_trigger (Source 6) ──────────────────────────────

def _feed_at_ts(samples: list[tuple[float, float]], interval: float = 1.0) -> ETHFeed:
    """Build a feed with explicit (timestamp, price) samples."""
    feed = ETHFeed(poll_interval=interval)
    for ts, price in samples:
        feed._history.append((ts, price))
    return feed


def test_spot_move_bps_none_with_empty_history():
    feed = ETHFeed(poll_interval=1.0)
    assert feed.spot_move_bps(3.0) is None


def test_spot_move_bps_zero_with_single_sample():
    feed = _feed_at_ts([(0.0, 1800.0)])
    assert feed.spot_move_bps(3.0, now=0.0) == pytest.approx(0.0)


def test_spot_move_bps_none_when_earliest_sample_outside_window():
    feed = _feed_at_ts([(0.0, 1800.0)])
    assert feed.spot_move_bps(3.0, now=10.0) is None


def test_spot_move_bps_positive_for_upward_move():
    feed = _feed_at_ts([(0.0, 1800.0), (1.0, 1801.8), (2.0, 1818.0)])
    # (1818 - 1800) / 1800 * 10000 = 100 bps
    assert feed.spot_move_bps(3.0, now=2.0) == pytest.approx(100.0, rel=1e-6)


def test_spot_move_bps_negative_for_downward_move():
    feed = _feed_at_ts([(0.0, 1800.0), (1.0, 1782.0)])
    assert feed.spot_move_bps(3.0, now=1.0) == pytest.approx(-100.0, rel=1e-6)


def test_spot_move_bps_ignores_samples_outside_window():
    feed = _feed_at_ts([(0.0, 3000.0), (9.0, 1800.0), (10.0, 1818.0)])
    assert feed.spot_move_bps(3.0, now=10.0) == pytest.approx(100.0, rel=1e-6)


def test_has_spot_trigger_true_above_threshold():
    feed = _feed_at_ts([(0.0, 1800.0), (1.0, 1818.0)])  # +100 bps
    assert feed.has_spot_trigger(threshold_bps=10.0, window_seconds=3.0, now=1.0) is True


def test_has_spot_trigger_false_below_threshold():
    feed = _feed_at_ts([(0.0, 1800.0), (1.0, 1800.18)])  # +1 bps
    assert feed.has_spot_trigger(threshold_bps=10.0, window_seconds=3.0, now=1.0) is False


def test_has_spot_trigger_true_for_large_downward_move():
    feed = _feed_at_ts([(0.0, 1800.0), (1.0, 1770.0)])  # ~-167 bps
    assert feed.has_spot_trigger(threshold_bps=10.0, window_seconds=3.0, now=1.0) is True


# ── history retention sized from vol_window_seconds ─────────────────────────

def test_history_len_derived_from_vol_window_and_poll_interval():
    feed = ETHFeed(poll_interval=2.0, vol_window_seconds=3600.0)
    assert feed._history.maxlen >= 1800


def test_history_len_derived_for_15m_window():
    feed = ETHFeed(poll_interval=2.0, vol_window_seconds=900.0)
    assert feed._history.maxlen >= 450


def test_explicit_history_len_overrides_derived_sizing():
    feed = ETHFeed(poll_interval=2.0, vol_window_seconds=3600.0, history_len=25)
    assert feed._history.maxlen == 25


def test_history_len_has_a_sane_minimum_floor():
    feed = ETHFeed(poll_interval=100.0, vol_window_seconds=100.0)
    assert feed._history.maxlen >= 60


# ── history_within (series-aware vol window) ────────────────────────────────

def test_history_within_empty_feed():
    feed = ETHFeed(poll_interval=60.0)
    assert feed.history_within(900.0) == []


def test_history_within_filters_to_trailing_window():
    feed = _feed_at_ts([(0.0, 1800.0), (500.0, 1810.0), (1000.0, 1850.0)])
    assert feed.history_within(600.0, now=1000.0) == [(500.0, 1810.0), (1000.0, 1850.0)]


def test_history_within_defaults_now_to_latest_sample():
    feed = _feed_at_ts([(0.0, 1800.0), (500.0, 1810.0), (1000.0, 1850.0)])
    assert feed.history_within(600.0) == [(500.0, 1810.0), (1000.0, 1850.0)]


def test_history_within_no_cross_contamination_between_windows():
    now = 3600.0
    feed = _feed_at_ts([(t, 1800.0 + t / 100.0) for t in range(0, 3601, 300)])
    slice_15m = feed.history_within(900.0, now=now)
    slice_hourly = feed.history_within(3600.0, now=now)
    assert all(ts >= now - 900.0 for ts, _ in slice_15m)
    assert set(slice_15m).issubset(set(slice_hourly))
    assert len(slice_15m) < len(slice_hourly)


# ── WS tick ingest ──────────────────────────────────────────────────────────

def _ticker_msg(price: str) -> str:
    import json
    return json.dumps({"type": "ticker", "product_id": "ETH-USD", "price": price})


@pytest.mark.asyncio
async def test_ws_ticker_message_appends_to_same_history():
    feed = ETHFeed(poll_interval=2.0)
    await feed._handle_ws_message(_ticker_msg("1860.71"))
    assert feed.price == pytest.approx(1860.71)
    assert len(feed.history) == 1
    assert feed.ws_tick_count == 1


@pytest.mark.asyncio
async def test_ws_ticks_interleave_with_poll_samples():
    """Both ingest paths share one deque -- consumers must see a merged series."""
    feed = ETHFeed(poll_interval=2.0)
    feed._history.append((time.time(), 1800.0))       # as if from the REST poll
    await feed._handle_ws_message(_ticker_msg("1818.0"))
    assert len(feed.history) == 2
    assert feed.spot_move_bps(3.0) == pytest.approx(100.0, rel=1e-6)


@pytest.mark.asyncio
async def test_ws_ignores_non_ticker_message_types():
    import json
    feed = ETHFeed(poll_interval=2.0)
    await feed._handle_ws_message(json.dumps({"type": "subscriptions", "channels": []}))
    await feed._handle_ws_message(json.dumps({"type": "error", "message": "nope"}))
    await feed._handle_ws_message(json.dumps({"type": "heartbeat", "price": "1.0"}))
    assert feed.history == []
    assert feed.ws_tick_count == 0


@pytest.mark.asyncio
async def test_ws_ignores_malformed_and_nonpositive_prices():
    import json
    feed = ETHFeed(poll_interval=2.0)
    await feed._handle_ws_message("not json at all")
    await feed._handle_ws_message(_ticker_msg("abc"))
    await feed._handle_ws_message(_ticker_msg("0"))
    await feed._handle_ws_message(_ticker_msg("-5"))
    await feed._handle_ws_message(json.dumps({"type": "ticker"}))  # no price key
    assert feed.history == []
    assert feed.price is None


@pytest.mark.asyncio
async def test_ws_callback_is_throttled_but_history_is_not():
    calls = []
    feed = ETHFeed(poll_interval=2.0, on_price=lambda p, t: calls.append(p),
                   ws_callback_min_interval=60.0)
    for i in range(5):
        await feed._handle_ws_message(_ticker_msg(str(1800 + i)))
    assert len(feed.history) == 5     # every tick retained
    assert len(calls) == 1            # only the first fired the callback


@pytest.mark.asyncio
async def test_ws_callback_fires_per_tick_when_throttle_disabled():
    calls = []
    feed = ETHFeed(poll_interval=2.0, on_price=lambda p, t: calls.append(p),
                   ws_callback_min_interval=0.0)
    for i in range(4):
        await feed._handle_ws_message(_ticker_msg(str(1800 + i)))
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_ws_callback_may_be_async():
    calls = []

    async def on_price(price, ts):
        calls.append(price)

    feed = ETHFeed(poll_interval=2.0, on_price=on_price, ws_callback_min_interval=0.0)
    await feed._handle_ws_message(_ticker_msg("1850.0"))
    assert calls == [1850.0]


def test_enable_tick_history_grows_deque_preserving_samples():
    feed = ETHFeed(poll_interval=2.0, vol_window_seconds=3600.0)
    assert feed._history.maxlen < 50_000
    feed._history.append((1.0, 1800.0))
    feed._enable_tick_history()
    assert feed._history.maxlen == 50_000
    assert feed.history == [(1.0, 1800.0)]   # existing samples survive


def test_enable_tick_history_is_idempotent():
    feed = ETHFeed(poll_interval=2.0)
    feed._enable_tick_history()
    feed._history.append((1.0, 1800.0))
    feed._enable_tick_history()
    assert feed._history.maxlen == 50_000
    assert len(feed.history) == 1


def test_enable_tick_history_never_shrinks_an_explicit_larger_deque():
    feed = ETHFeed(poll_interval=2.0, history_len=80_000)
    feed._enable_tick_history()
    assert feed._history.maxlen == 80_000


# ── vol_annualised spacing guard for mixed tick/poll data ────────────────────

def test_vol_annualisation_uses_observed_spacing_not_poll_interval():
    prices = [1800.0 * math.exp(0.0001 * ((-1) ** i)) for i in range(50)]
    tick_feed = ETHFeed(poll_interval=2.0)
    for i, p in enumerate(prices):
        tick_feed._history.append((i * 0.1, p))

    poll_feed = ETHFeed(poll_interval=2.0)
    for i, p in enumerate(prices):
        poll_feed._history.append((i * 2.0, p))

    assert tick_feed.vol_annualised == pytest.approx(
        poll_feed.vol_annualised * math.sqrt(20.0), rel=1e-6
    )


def test_vol_annualisation_falls_back_to_poll_interval_for_degenerate_timestamps():
    feed = ETHFeed(poll_interval=60.0)
    for p in [1800.0, 1810.0, 1790.0]:
        feed._history.append((5.0, p))
    vol = feed.vol_annualised
    assert vol is not None and vol > 0
