"""Tests for kbot.execution_maker — maker-order execution (Part 3, Phase A1)."""

import pytest

import kbot.risk as risk_module
from kbot.config import Settings
from kbot.edge import MAKER_FEE_SPECIAL_SERIES, net_edge_maker, net_edge_taker
from kbot.execution import Executor
from kbot.execution_maker import MakerExecutor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.models.types import OrderBook, PriceLevel, Side, SizingResult


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env=kwargs.get("kalshi_env", "demo"),
        dry_run=kwargs.get("dry_run", True),
        min_net_edge=kwargs.get("min_net_edge", 0.04),
        fee_coefficient=kwargs.get("fee_coefficient", 0.07),
        fee_coefficient_maker=kwargs.get("fee_coefficient_maker", 0.0),
        maker_fee_default=kwargs.get("maker_fee_default", 0.0),
        assumed_slippage=kwargs.get("assumed_slippage", 0.005),
        execution_mode=kwargs.get("execution_mode", "auto"),
        maker_timeout_seconds=kwargs.get("maker_timeout_seconds", 120),
        max_reprices=kwargs.get("max_reprices", 3),
        taker_price_floor=kwargs.get("taker_price_floor", 0.15),
    )


def _result(
    ticker="TICK",
    side=Side.YES,
    contracts=5,
    fair_p=0.72,
    price=0.65,
) -> SizingResult:
    return SizingResult(
        ticker=ticker,
        side=side,
        contracts=contracts,
        price_dollars=price,
        fair_p=fair_p,
        net_edge_dollars=0.0,  # MakerExecutor recomputes taker/maker edge itself
        kelly_fraction_used=0.1,
    )


def _book(yes_ask: float, no_ask: float, ticker="TICK") -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes_ask, quantity_units=10000)],
        no=[PriceLevel(price=no_ask, quantity_units=10000)],
    )


@pytest.fixture(autouse=True)
def reset_kill_switch():
    risk_module._KILL_SWITCH_TRIPPED = False
    yield
    risk_module._KILL_SWITCH_TRIPPED = False


@pytest.fixture()
def mock_client(mocker):
    client = mocker.MagicMock(spec=KalshiClient)

    def _place(*, ticker, side, count, price_dollars, **kwargs):
        # Complete fill at the requested price. fill_count must be set:
        # immediate_or_cancel acks report 0 when nothing traded, and the
        # executor treats that as "no position opened".
        return OrderAck(
            client_order_id="cid-123",
            order_id="oid-456",
            dry_run=True,
            fill_count_units=count * 100,
            remaining_count_units=0,
            average_fill_price=price_dollars,
        )

    client.place_order.side_effect = _place
    client.cancel_order.return_value = {"simulated": True}
    return client


@pytest.fixture()
def executor(mock_client):
    return Executor(mock_client, _cfg())


# ── 1. Tight spread falls back to taker ────────────────────────────────────────

def test_tight_spread_falls_back_to_taker(mock_client, executor):
    maker = MakerExecutor(executor, _cfg())
    # yes_ask=0.61, no_ask=0.40 -> bid=0.60, spread=0.01 < 0.02
    book = _book(yes_ask=0.61, no_ask=0.40)
    result = _result(fair_p=0.75)

    ack = maker.execute(result, book)

    assert ack is not None
    mock_client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=5, price_dollars=0.61
    )
    assert maker.resting_orders == {}


# ── 2. Wide spread places a resting maker order at bid + 0.01 ─────────────────

def test_wide_spread_places_maker_order(mock_client, executor):
    maker = MakerExecutor(executor, _cfg())
    # yes_ask=0.65, no_ask=0.40 -> bid=0.60, spread=0.05 >= 0.02
    book = _book(yes_ask=0.65, no_ask=0.40)
    result = _result(fair_p=0.72)

    ack = maker.execute(result, book, now=0.0)

    assert ack is not None
    mock_client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=5, price_dollars=0.61
    )
    resting = maker.resting_orders[("TICK", "yes")]
    assert resting.price == 0.61
    assert resting.reprice_count == 0


# ── 3. Repricing capped at max_reprices, then abandoned ────────────────────────

def test_reprice_capped_then_abandoned(mock_client, executor):
    cfg = _cfg(max_reprices=3)
    maker = MakerExecutor(executor, cfg)
    result = _result(fair_p=0.72)

    # Initial placement: bid=0.60 -> resting @ 0.61
    maker.execute(result, _book(yes_ask=0.65, no_ask=0.40), now=0.0)
    assert maker.resting_orders[("TICK", "yes")].price == 0.61

    # Market creeps up each tick; ideal price keeps moving -> reprice each time.
    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.65, no_ask=0.39), now=10.0
    )
    resting = maker.resting_orders[("TICK", "yes")]
    assert (resting.price, resting.reprice_count) == (0.62, 1)

    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.65, no_ask=0.38), now=20.0
    )
    resting = maker.resting_orders[("TICK", "yes")]
    assert (resting.price, resting.reprice_count) == (0.63, 2)

    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.65, no_ask=0.37), now=30.0
    )
    resting = maker.resting_orders[("TICK", "yes")]
    assert (resting.price, resting.reprice_count) == (0.64, 3)

    # 4th market move: reprice_count already == max_reprices -> give up.
    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.65, no_ask=0.36), now=40.0
    )
    assert ("TICK", "yes") not in maker.resting_orders

    assert mock_client.place_order.call_count == 4  # initial + 3 reprices
    assert mock_client.cancel_order.call_count == 4  # 3 reprices + final abandon


# ── 4. Timeout cancels a resting order older than maker_timeout_seconds ────────

def test_timeout_cancels_resting_order(mock_client, executor):
    cfg = _cfg(maker_timeout_seconds=120)
    maker = MakerExecutor(executor, cfg)
    result = _result(fair_p=0.72)

    maker.execute(result, _book(yes_ask=0.65, no_ask=0.40), now=0.0)
    assert ("TICK", "yes") in maker.resting_orders

    # Same book (no reprice trigger), but well past the timeout.
    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.65, no_ask=0.40), now=125.0
    )

    assert ("TICK", "yes") not in maker.resting_orders
    mock_client.cancel_order.assert_called_once_with("oid-456")
    assert mock_client.place_order.call_count == 1  # no reprice attempted


# ── 5. DRY_RUN paper fill only on trade-through, never on time passage ─────────

def test_dry_run_fill_only_on_trade_through(mock_client, executor):
    cfg = _cfg(dry_run=True)
    maker = MakerExecutor(executor, cfg)
    result = _result(fair_p=0.72)

    maker.execute(result, _book(yes_ask=0.65, no_ask=0.40), now=0.0)
    assert ("TICK", "yes") in maker.resting_orders

    # Time passes, market unchanged -> still just resting, no fill.
    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.65, no_ask=0.40), now=50.0
    )
    assert ("TICK", "yes") in maker.resting_orders
    assert not executor.tracker.is_held("TICK", Side.YES)

    # Market trades through our resting price (ask drops to/through 0.61).
    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.60, no_ask=0.40), now=60.0
    )
    assert ("TICK", "yes") not in maker.resting_orders
    assert executor.tracker.is_held("TICK", Side.YES)
    mock_client.cancel_order.assert_not_called()


# ── 6. net_edge_maker and net_edge_taker are computed independently ────────────

def test_net_edge_maker_and_taker_independent():
    cfg_a = _cfg(fee_coefficient=0.07, fee_coefficient_maker=0.0)
    taker_edge = net_edge_taker(0.72, 0.65, cfg_a)
    maker_edge = net_edge_maker(0.72, 0.61, cfg_a)
    assert taker_edge != maker_edge

    # Changing the taker fee coefficient must not move the maker edge, and
    # vice versa -- they are independent computations.
    cfg_b = _cfg(fee_coefficient=0.20, fee_coefficient_maker=0.0)
    assert net_edge_maker(0.72, 0.61, cfg_b) == maker_edge
    assert net_edge_taker(0.72, 0.65, cfg_b) != taker_edge

    cfg_c = _cfg(fee_coefficient=0.07, fee_coefficient_maker=0.10)
    assert net_edge_taker(0.72, 0.65, cfg_c) == taker_edge
    assert net_edge_maker(0.72, 0.61, cfg_c) != maker_edge


def test_mode_choice_uses_independent_edges(mock_client, executor):
    # taker leg fails the gate (crossing at 0.65 barely clears fees, not the
    # 0.04 minimum); maker leg clears easily resting at 0.61 with zero fee.
    cfg = _cfg(min_net_edge=0.04)
    maker = MakerExecutor(executor, cfg)
    result = _result(fair_p=0.68)

    taker_edge = net_edge_taker(0.68, 0.65, cfg)
    maker_edge = net_edge_maker(0.68, 0.61, cfg)
    assert taker_edge < cfg.min_net_edge
    assert maker_edge >= cfg.min_net_edge

    ack = maker.execute(result, _book(yes_ask=0.65, no_ask=0.40), now=0.0)

    assert ack is not None
    mock_client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=5, price_dollars=0.61
    )


# ── 7. Ambiguous cancel is logged, never retried ────────────────────────────────

def test_ambiguous_cancel_logged_not_retried(mock_client, executor, mocker):
    mock_client.cancel_order.side_effect = RuntimeError("connection reset")
    log_spy = mocker.patch("kbot.execution_maker.log")

    cfg = _cfg(maker_timeout_seconds=120)
    maker = MakerExecutor(executor, cfg)
    result = _result(fair_p=0.72)

    maker.execute(result, _book(yes_ask=0.65, no_ask=0.40), now=0.0)
    log_spy.reset_mock()

    maker.check_and_reprice(
        "TICK", Side.YES, _book(yes_ask=0.65, no_ask=0.40), now=125.0
    )

    mock_client.cancel_order.assert_called_once_with("oid-456")
    assert ("TICK", "yes") not in maker.resting_orders  # dropped for manual review

    error_calls = [c for c in log_spy.error.call_args_list if c.args[0] == "maker.cancel_ambiguous"]
    assert len(error_calls) == 1
    _, kwargs = error_calls[0]
    assert kwargs["order_id"] == "oid-456"


# ── RULE R1 taker price floor (maker path) ──────────────────────────────────────

@pytest.fixture()
def special_event_series():
    MAKER_FEE_SPECIAL_SERIES.add("KXSPECIAL")
    yield "KXSPECIAL"
    MAKER_FEE_SPECIAL_SERIES.discard("KXSPECIAL")


def test_maker_price_floor_exempt_on_standard_market(mock_client):
    cfg = _cfg(min_net_edge=0.04, taker_price_floor=0.15)
    executor = Executor(mock_client, cfg)
    maker = MakerExecutor(executor, cfg)
    # yes_ask=0.10, no_ask=0.95 -> bid=0.05, spread=0.05 -> maker_price=0.06 (< floor)
    book = _book(yes_ask=0.10, no_ask=0.95)
    result = _result(fair_p=0.20)

    ack = maker.execute(result, book, now=0.0)

    assert ack is not None
    mock_client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=5, price_dollars=0.06
    )


def test_maker_price_floor_blocked_on_special_event_market(mock_client, special_event_series):
    cfg = _cfg(min_net_edge=0.04, taker_price_floor=0.15)
    executor = Executor(mock_client, cfg)
    maker = MakerExecutor(executor, cfg)
    ticker = f"{special_event_series}-26JUL03-YES"
    book = _book(yes_ask=0.10, no_ask=0.95, ticker=ticker)
    result = _result(ticker=ticker, fair_p=0.20)

    ack = maker.execute(result, book, now=0.0)

    assert ack is None
    mock_client.place_order.assert_not_called()
