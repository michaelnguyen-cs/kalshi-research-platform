"""
Tests for kbot.kalshi_client — candlestick endpoint (Phase A4 item 6, hotfix)
and the V2 order-placement migration (2026-08).
"""

from __future__ import annotations

import json

import pytest

from kbot.config import Settings
from kbot.kalshi_client import (
    KalshiClient,
    from_v2_fill_price,
    to_v2_side_and_price,
)


def _cfg() -> Settings:
    return Settings(kalshi_key_id="", dry_run=True)  # no credentials -> unauthenticated


def test_get_candlesticks_requires_auth():
    client = KalshiClient(_cfg())
    with pytest.raises(RuntimeError):
        client.get_candlesticks("KXBTC", "KXBTC-26JUL0317-T48000", start_ts=1000, end_ts=4600)


def test_get_candlesticks_rejects_invalid_period_interval():
    client = KalshiClient(_cfg())
    client._authed = True  # bypass the auth guard -- want to hit the period_interval check
    with pytest.raises(ValueError, match="1, 60, or 1440"):
        client.get_candlesticks(
            "KXBTC", "KXBTC-26JUL0317-T48000", start_ts=1000, end_ts=4600, period_interval=5
        )


def test_get_candlesticks_calls_correct_route_and_params(mocker):
    client = KalshiClient(_cfg())
    client._authed = True  # bypass the auth guard without real credentials
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={"KALSHI-ACCESS-KEY": "fake"})

    # Real response shape (confirmed live 2026-07): nested dollar-string
    # fields, no flat open/high/low/close -- see get_candlesticks()'s docstring.
    raw_candles = [
        {
            "end_period_ts": 4600,
            "price": {"close_dollars": "0.6200"},
            "yes_ask": {"open_dollars": "0.6000", "high_dollars": "0.6300", "low_dollars": "0.6000", "close_dollars": "0.6200"},
            "yes_bid": {"open_dollars": "0.5800", "high_dollars": "0.6000", "low_dollars": "0.5800", "close_dollars": "0.5900"},
            "volume_fp": "12.00",
            "open_interest_fp": "340.00",
        }
    ]
    fake_response = mocker.MagicMock()
    fake_response.json.return_value = {"candlesticks": raw_candles, "ticker": "KXBTC-26JUL0317-T48000"}
    fake_response.raise_for_status = mocker.MagicMock()
    mock_get = mocker.patch.object(client._http, "get", return_value=fake_response)

    rows = client.get_candlesticks(
        "KXBTC", "KXBTC-26JUL0317-T48000", start_ts=1000, end_ts=4600, period_interval=60
    )

    assert rows == raw_candles  # raw dicts passed through untouched
    args, kwargs = mock_get.call_args
    assert args[0] == client._cfg.base_url + "/series/KXBTC/markets/KXBTC-26JUL0317-T48000/candlesticks"
    assert kwargs["params"] == {"start_ts": 1000, "end_ts": 4600, "period_interval": 60}


def test_get_candlesticks_empty_response(mocker):
    client = KalshiClient(_cfg())
    client._authed = True
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})

    fake_response = mocker.MagicMock()
    fake_response.json.return_value = {"candlesticks": [], "ticker": "TICK"}
    fake_response.raise_for_status = mocker.MagicMock()
    mocker.patch.object(client._http, "get", return_value=fake_response)

    rows = client.get_candlesticks("KXBTC", "KXBTC-26JUL0317-T48000", start_ts=1000, end_ts=4600)

    assert rows == []


def test_get_candlesticks_raises_on_http_error(mocker):
    import httpx

    client = KalshiClient(_cfg())
    client._authed = True
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})

    fake_response = mocker.MagicMock()
    fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400 Bad Request", request=mocker.MagicMock(), response=mocker.MagicMock(status_code=400)
    )
    mocker.patch.object(client._http, "get", return_value=fake_response)

    with pytest.raises(httpx.HTTPStatusError):
        client.get_candlesticks("KXBTC", "KXBTC-26JUL0317-T48000", start_ts=1000, end_ts=4600)


# ── get_events pagination (2026-07 fix — cursor was previously discarded) ─────

def _events_response(mocker, events: list[dict], cursor: str | None):
    resp = mocker.MagicMock()
    resp.json.return_value = {"events": events, "cursor": cursor}
    resp.raise_for_status = mocker.MagicMock()
    return resp


def test_get_events_default_max_pages_is_single_page(mocker):
    """Default max_pages=1 preserves old single-page behavior for existing callers."""
    client = KalshiClient(_cfg())
    client._authed = True
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})

    page1 = _events_response(mocker, [{"markets": [{"ticker": "A"}]}], cursor="NEXT")
    mock_get = mocker.patch.object(client._http, "get", return_value=page1)

    events = client.get_events(series_ticker="KXBTC", limit=200)

    assert len(events) == 1
    assert mock_get.call_count == 1  # never followed the cursor


def test_get_events_follows_cursor_up_to_max_pages(mocker):
    client = KalshiClient(_cfg())
    client._authed = True
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})

    page1 = _events_response(mocker, [{"markets": [{"ticker": "A"}]}], cursor="PAGE2")
    page2 = _events_response(mocker, [{"markets": [{"ticker": "B"}]}], cursor="PAGE3")
    page3 = _events_response(mocker, [{"markets": [{"ticker": "C"}]}], cursor="PAGE4")
    mock_get = mocker.patch.object(client._http, "get", side_effect=[page1, page2, page3])

    events = client.get_events(series_ticker="KXBTC", limit=200, max_pages=3)

    assert len(events) == 3
    assert mock_get.call_count == 3
    # Second and third calls must carry the cursor from the previous response.
    assert mock_get.call_args_list[1].kwargs["params"]["cursor"] == "PAGE2"
    assert mock_get.call_args_list[2].kwargs["params"]["cursor"] == "PAGE3"


def test_get_events_stops_when_cursor_exhausted(mocker):
    client = KalshiClient(_cfg())
    client._authed = True
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})

    page1 = _events_response(mocker, [{"markets": [{"ticker": "A"}]}], cursor="PAGE2")
    page2 = _events_response(mocker, [{"markets": [{"ticker": "B"}]}], cursor=None)  # no more pages
    mock_get = mocker.patch.object(client._http, "get", side_effect=[page1, page2])

    events = client.get_events(series_ticker="KXBTC", limit=200, max_pages=0)  # 0 = unlimited

    assert len(events) == 2
    assert mock_get.call_count == 2  # stopped once the cursor ran out, not because of a page cap


def test_get_events_stops_when_page_is_empty(mocker):
    client = KalshiClient(_cfg())
    client._authed = True
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})

    page1 = _events_response(mocker, [{"markets": [{"ticker": "A"}]}], cursor="PAGE2")
    page2 = _events_response(mocker, [], cursor="PAGE3")  # cursor present but no events
    mock_get = mocker.patch.object(client._http, "get", side_effect=[page1, page2])

    events = client.get_events(series_ticker="KXBTC", limit=200, max_pages=0)

    assert len(events) == 1
    assert mock_get.call_count == 2


# ── V2 order placement (2026-08 migration off the 410'd /portfolio/orders) ────
#
# The YES/NO -> bid/ask conversion is the highest-risk transform in the bot:
# getting it backwards opens the OPPOSITE position with real money. It is
# asserted here in both directions, at the pure-function level and through the
# built request payload.

V2_ORDERS_PATH = "/portfolio/events/orders"


def _live_cfg() -> Settings:
    return Settings(kalshi_key_id="", dry_run=False)


def _live_client(mocker) -> KalshiClient:
    client = KalshiClient(_live_cfg())
    client._authed = True   # bypass the auth guard without real credentials
    mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})
    return client


def _order_response(mocker, body: dict, status: int = 201):
    resp = mocker.MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.raise_for_status = mocker.MagicMock()
    return resp


# ── The conversion itself ─────────────────────────────────────────────────────

def test_yes_buy_maps_to_bid_at_the_same_price():
    assert to_v2_side_and_price("yes", 0.65) == ("bid", 0.65)


def test_no_buy_maps_to_ask_at_one_minus_price():
    """
    Buying NO @ 0.30 == selling YES @ 0.70: collect 0.70, post 1.00 collateral,
    net outlay 0.30/contract. 'bid' here would BUY YES instead -- the opposite
    position, at a price the strategy judged too rich.
    """
    assert to_v2_side_and_price("no", 0.30) == ("ask", 0.70)


@pytest.mark.parametrize("price", [0.01, 0.30, 0.37, 0.5, 0.65, 0.99])
def test_no_conversion_round_trips_through_the_fill_price_inverse(price):
    _, yes_price = to_v2_side_and_price("no", price)
    assert from_v2_fill_price("no", yes_price) == pytest.approx(price)


def test_yes_fill_price_is_unchanged_but_no_fill_price_is_inverted():
    assert from_v2_fill_price("yes", 0.70) == 0.70
    assert from_v2_fill_price("no", 0.70) == 0.30


@pytest.mark.parametrize("bad_price", [0.0, 1.0, -0.1, 1.5])
def test_out_of_range_price_is_rejected_before_the_network(bad_price):
    with pytest.raises(ValueError, match="between 0 and 1"):
        to_v2_side_and_price("yes", bad_price)


def test_unknown_side_is_rejected():
    with pytest.raises(ValueError, match="'yes' or 'no'"):
        to_v2_side_and_price("bid", 0.5)


# ── Request payload ───────────────────────────────────────────────────────────

def test_yes_order_builds_the_v2_payload(mocker):
    client = _live_client(mocker)
    mock_post = mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {
            "order_id": "ord-1", "client_order_id": "cid-1",
            "fill_count": "10.00", "remaining_count": "0.00",
            "average_fill_price": "0.5600",
        }),
    )

    client.place_order(
        ticker="KXBTC15M-T1", side="yes", count=10,
        price_dollars=0.56, client_order_id="cid-1",
    )

    args, kwargs = mock_post.call_args
    assert args[0] == client._cfg.base_url + V2_ORDERS_PATH
    assert kwargs["json"] == {
        "ticker": "KXBTC15M-T1",
        "client_order_id": "cid-1",
        "side": "bid",
        "count": "10.00",
        "price": "0.5600",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
    }


def test_no_order_builds_an_ask_at_one_minus_price(mocker):
    """A NO buy at model price 0.30 must leave as ask @ "0.7000"."""
    client = _live_client(mocker)
    mock_post = mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {
            "order_id": "ord-2", "client_order_id": "cid-2",
            "fill_count": "4.00", "remaining_count": "0.00",
            "average_fill_price": "0.7000",
        }),
    )

    client.place_order(
        ticker="KXBTC15M-T1", side="no", count=4,
        price_dollars=0.30, client_order_id="cid-2",
    )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["side"] == "ask"          # sell YES == buy NO
    assert payload["price"] == "0.7000"      # 1 - 0.30
    assert payload["count"] == "4.00"


def test_order_posts_to_the_v2_route_not_the_deprecated_one(mocker):
    client = _live_client(mocker)
    mock_post = mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {"order_id": "o", "fill_count": "1.00"}),
    )

    client.place_order(ticker="TICK", side="yes", count=1, price_dollars=0.5)

    url = mock_post.call_args.args[0]
    assert url.endswith("/portfolio/events/orders")
    assert not url.endswith("/trade-api/v2/portfolio/orders")


def test_order_is_signed_over_the_v2_path(mocker):
    """The RSA-PSS signature must cover the new path or the request 401s."""
    client = KalshiClient(_live_cfg())
    client._authed = True
    mock_headers = mocker.patch("kbot.kalshi_client.kalshi_headers", return_value={})
    mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {"order_id": "o", "fill_count": "1.00"}),
    )

    client.place_order(ticker="TICK", side="yes", count=1, price_dollars=0.5)

    method, path, _key, _pk = mock_headers.call_args.args
    assert method == "POST"
    assert path == "/trade-api/v2/portfolio/events/orders"


def test_zero_or_negative_count_is_rejected(mocker):
    client = _live_client(mocker)
    with pytest.raises(ValueError, match="count must be positive"):
        client.place_order(ticker="TICK", side="yes", count=0, price_dollars=0.5)


# ── Response parsing ──────────────────────────────────────────────────────────

def test_response_parsing_extracts_flat_v2_fields(mocker):
    client = _live_client(mocker)
    mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {
            "order_id": "ord-abc",
            "client_order_id": "cid-abc",
            "fill_count": "10.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.5600",
            "ts_ms": 1754200000000,
        }),
    )

    ack = client.place_order(
        ticker="TICK", side="yes", count=10,
        price_dollars=0.56, client_order_id="cid-abc",
    )

    assert ack.order_id == "ord-abc"          # flat, not nested under "order"
    assert ack.client_order_id == "cid-abc"
    assert ack.fill_count_units == 1000
    assert ack.remaining_count_units == 0
    assert ack.average_fill_price == pytest.approx(0.56)
    assert ack.dry_run is False


def test_no_side_average_fill_price_is_converted_back_to_no_terms(mocker):
    """
    The exchange reports the fill on the YES book. A NO trade filled at YES
    0.70 cost 0.30/contract -- reporting 0.70 would more than double the cost
    basis compute_outcome charges and corrupt Live P&L.
    """
    client = _live_client(mocker)
    mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {
            "order_id": "ord-no", "fill_count": "3.00",
            "remaining_count": "0.00", "average_fill_price": "0.7000",
        }),
    )

    ack = client.place_order(ticker="TICK", side="no", count=3, price_dollars=0.30)

    assert ack.average_fill_price == pytest.approx(0.30)


def test_partial_fill_is_reported_not_rounded_up(mocker):
    client = _live_client(mocker)
    mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {
            "order_id": "ord-p", "fill_count": "3.00",
            "remaining_count": "7.00", "average_fill_price": "0.5600",
        }),
    )

    ack = client.place_order(ticker="TICK", side="yes", count=10, price_dollars=0.56)

    assert ack.fill_count_units == 300
    assert ack.remaining_count_units == 700


def test_zero_fill_reports_no_average_price(mocker):
    """IOC that crossed nothing: no fill, and no fabricated cost basis."""
    client = _live_client(mocker)
    mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {
            "order_id": "ord-z", "fill_count": "0.00", "remaining_count": "10.00",
        }),
    )

    ack = client.place_order(ticker="TICK", side="yes", count=10, price_dollars=0.56)

    assert ack.fill_count_units == 0
    assert ack.average_fill_price is None


def test_missing_average_falls_back_to_the_limit_price(mocker):
    """An IOC never fills worse than its limit, so the limit is a safe floor."""
    client = _live_client(mocker)
    mocker.patch.object(
        client._http, "post",
        return_value=_order_response(mocker, {"order_id": "o", "fill_count": "5.00"}),
    )

    ack = client.place_order(ticker="TICK", side="yes", count=5, price_dollars=0.56)

    assert ack.fill_count_units == 500
    assert ack.average_fill_price == pytest.approx(0.56)
    assert ack.remaining_count_units == 0  # derived: requested - filled


def test_rejected_order_raises_and_is_never_retried(mocker):
    import httpx

    client = _live_client(mocker)
    resp = _order_response(mocker, {}, status=400)
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "400", request=mocker.MagicMock(), response=mocker.MagicMock(status_code=400)
    )
    resp.text = "bad request"
    mock_post = mocker.patch.object(client._http, "post", return_value=resp)

    with pytest.raises(httpx.HTTPStatusError):
        client.place_order(ticker="TICK", side="yes", count=5, price_dollars=0.56)

    assert mock_post.call_count == 1  # a double-submit would double the position


# ── DRY_RUN ───────────────────────────────────────────────────────────────────

def test_dry_run_places_nothing_and_simulates_a_complete_fill(mocker):
    client = KalshiClient(_cfg())   # dry_run=True
    mock_post = mocker.patch.object(client._http, "post")

    ack = client.place_order(ticker="TICK", side="no", count=7, price_dollars=0.30)

    mock_post.assert_not_called()
    assert ack.dry_run is True
    assert ack.order_id is None
    assert ack.fill_count_units == 700               # paper positions still get recorded
    assert ack.average_fill_price == pytest.approx(0.30)   # in NO terms
    assert ack.raw["side"] == "ask"          # conversion still exercised on paper
    assert ack.raw["price"] == "0.7000"


# ── Cancel ────────────────────────────────────────────────────────────────────

def test_cancel_uses_the_v2_route(mocker):
    client = _live_client(mocker)
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.json.return_value = {}
    mock_delete = mocker.patch.object(client._http, "delete", return_value=resp)

    client.cancel_order("ord-1")

    assert mock_delete.call_args.args[0].endswith("/portfolio/events/orders/ord-1")


# ── get_positions (2026-08) ───────────────────────────────────────────────────
#
# The SDK path this replaced read `resp.market_positions`, but the response
# model's field is `positions` -- every call raised AttributeError, so the
# method could never return a position. Live-trade settlement reconciliation
# depends on it, so these pin the shape.

def _positions_response(mocker, body: dict, status: int = 200):
    resp = mocker.MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.raise_for_status = mocker.MagicMock()
    return resp


_SETTLED = {
    "ticker": "KXBTC15M-T1", "market_result": "yes",
    "realized_pnl": 385, "fees_paid": 12, "total_cost": 315, "position": 0,
}


def test_get_positions_reads_the_positions_field(mocker):
    client = _live_client(mocker)
    mocker.patch.object(
        client._http, "get",
        return_value=_positions_response(mocker, {"positions": [_SETTLED]}),
    )

    assert client.get_positions(ticker="KXBTC15M-T1") == [_SETTLED]


def test_get_positions_accepts_the_legacy_market_positions_key(mocker):
    """The name the old SDK path used; a mismatch here was the original bug."""
    client = _live_client(mocker)
    mocker.patch.object(
        client._http, "get",
        return_value=_positions_response(mocker, {"market_positions": [_SETTLED]}),
    )

    assert client.get_positions() == [_SETTLED]


def test_get_positions_requests_settled_positions(mocker):
    """Settled positions are the only ones carrying a realized outcome."""
    client = _live_client(mocker)
    mock_get = mocker.patch.object(
        client._http, "get",
        return_value=_positions_response(mocker, {"positions": []}),
    )

    client.get_positions(ticker="KXBTC15M-T1")

    args, kwargs = mock_get.call_args
    assert args[0] == client._cfg.base_url + "/portfolio/positions"
    assert kwargs["params"]["settlement_status"] == "all"
    assert kwargs["params"]["ticker"] == "KXBTC15M-T1"


def test_get_positions_retries_without_settlement_status_on_400(mocker):
    """An API that rejects the filter degrades to its default, not to nothing."""
    client = _live_client(mocker)
    rejected = _positions_response(mocker, {}, status=400)
    rejected.text = "unknown query argument settlement_status"
    ok = _positions_response(mocker, {"positions": [_SETTLED]})
    mock_get = mocker.patch.object(client._http, "get", side_effect=[rejected, ok])

    assert client.get_positions() == [_SETTLED]
    assert "settlement_status" not in mock_get.call_args[1]["params"]


def test_get_positions_follows_the_cursor(mocker):
    """A single ticker's settled position must not be missed on page two."""
    client = _live_client(mocker)
    page1 = _positions_response(mocker, {"positions": [{"ticker": "A"}], "cursor": "c1"})
    page2 = _positions_response(mocker, {"positions": [_SETTLED], "cursor": ""})
    mocker.patch.object(client._http, "get", side_effect=[page1, page2])

    assert client.get_positions() == [{"ticker": "A"}, _SETTLED]


# ── get_series: the per-series fee fields ────────────────────────────────────
# The SDK's typed Series model declares only ticker/title/category/status, so
# it DROPS fee_type and fee_multiplier. get_series must use the raw-JSON path.

class _FakeRaw:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class _FakeSeriesApi:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[str] = []

    def get_series_by_ticker_without_preload_content(self, ticker: str) -> _FakeRaw:
        self.calls.append(ticker)
        return _FakeRaw(self._payload)


def test_get_series_returns_the_fee_fields():
    client = KalshiClient(_cfg())
    client._series_api = _FakeSeriesApi({
        "series": {
            "ticker": "KXBTC15M",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
            "last_updated_ts": "2026-08-04T22:47:28.068782Z",
        }
    })
    series = client.get_series("KXBTC15M")
    assert series["fee_type"] == "quadratic"
    assert series["fee_multiplier"] == 1
    assert client._series_api.calls == ["KXBTC15M"]


def test_get_series_preserves_a_non_unit_multiplier():
    """fee_multiplier is NOT always 1 -- 0 and 0.5 both occur on live series."""
    client = KalshiClient(_cfg())
    client._series_api = _FakeSeriesApi({
        "series": {"ticker": "KXMLBF5SPREAD", "fee_type": "quadratic", "fee_multiplier": 0.5}
    })
    assert client.get_series("KXMLBF5SPREAD")["fee_multiplier"] == 0.5


def test_get_series_returns_empty_dict_when_absent():
    client = KalshiClient(_cfg())
    client._series_api = _FakeSeriesApi({})
    assert client.get_series("NOPE") == {}


def test_get_series_needs_no_credentials():
    """Unauthenticated clients must still be able to read the fee schedule."""
    client = KalshiClient(_cfg())
    assert client._authed is False
    assert client._series_api is not None
