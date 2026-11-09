"""Tests for kbot.execution — the 4-layer safety gauntlet."""

import pytest

import kbot.risk as risk_module
from kbot.config import Settings
from kbot.execution import Executor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.models.types import Side, SizingResult
from kbot.position_tracker import PositionTracker


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env=kwargs.get("kalshi_env", "demo"),
        dry_run=kwargs.get("dry_run", True),
        min_net_edge=kwargs.get("min_net_edge", 0.04),
        taker_price_floor=kwargs.get("taker_price_floor", 0.15),
    )


def _result(
    contracts=5,
    net_edge_dollars=0.05,
    skip_reason=None,
    price=0.60,
    side=Side.YES,
    ticker="TICK",
) -> SizingResult:
    return SizingResult(
        ticker=ticker,
        side=side,
        contracts=contracts,
        price_dollars=price,
        net_edge_dollars=net_edge_dollars,
        kelly_fraction_used=0.1,
        skip_reason=skip_reason,
    )


@pytest.fixture(autouse=True)
def reset_kill_switch():
    risk_module._KILL_SWITCH_TRIPPED = False
    yield
    risk_module._KILL_SWITCH_TRIPPED = False


def _filled_ack(*, dry_run=True, order_id="oid-456", client_order_id="cid-123"):
    """
    place_order side_effect simulating a COMPLETE fill at the requested price.

    Orders are immediate_or_cancel, so an ack with fill_count 0 means nothing
    traded -- a fixture that omitted the fill fields would be asserting on the
    no-fill path, not the happy one. Echoing `count` keeps it correct for
    tests that size differently.
    """
    def _place(*, ticker, side, count, price_dollars, **kwargs):
        return OrderAck(
            client_order_id=client_order_id,
            order_id=order_id,
            dry_run=dry_run,
            fill_count_units=count * 100,
            remaining_count_units=0,
            average_fill_price=price_dollars,
        )
    return _place


@pytest.fixture()
def mock_client(mocker):
    client = mocker.MagicMock(spec=KalshiClient)
    client.place_order.side_effect = _filled_ack()
    return client


# ── Kill-switch blocks all orders ─────────────────────────────────────────────

def test_kill_switch_blocks(mock_client):
    risk_module._KILL_SWITCH_TRIPPED = True
    executor = Executor(mock_client, _cfg())
    ack = executor.execute(_result())
    assert ack is None
    mock_client.place_order.assert_not_called()


# ── Zero-contract results are skipped ─────────────────────────────────────────

def test_zero_contracts_skipped(mock_client):
    executor = Executor(mock_client, _cfg())
    ack = executor.execute(_result(contracts=0))
    assert ack is None
    mock_client.place_order.assert_not_called()


def test_skip_reason_logged(mock_client):
    executor = Executor(mock_client, _cfg())
    ack = executor.execute(_result(contracts=0, skip_reason="edge_below_min"))
    assert ack is None


# ── Edge gate ─────────────────────────────────────────────────────────────────

def test_edge_gate_blocks_low_edge(mock_client):
    cfg = _cfg(min_net_edge=0.04)
    executor = Executor(mock_client, cfg)
    ack = executor.execute(_result(net_edge_dollars=0.03))
    assert ack is None
    mock_client.place_order.assert_not_called()


def test_edge_gate_passes_sufficient_edge(mock_client):
    cfg = _cfg(min_net_edge=0.04, dry_run=True)
    executor = Executor(mock_client, cfg)
    ack = executor.execute(_result(net_edge_dollars=0.05))
    assert ack is not None
    mock_client.place_order.assert_called_once()


# ── min_net_edge_override (2026-07: post-resolution scanner discount) ───────

def test_min_net_edge_override_lets_below_normal_edge_through(mock_client):
    cfg = _cfg(min_net_edge=0.10)
    executor = Executor(mock_client, cfg)
    # 0.08 fails the normal 0.10 bar but clears a 0.07 discounted bar.
    ack = executor.execute(_result(net_edge_dollars=0.08), min_net_edge_override=0.07)
    assert ack is not None
    mock_client.place_order.assert_called_once()


def test_min_net_edge_override_still_blocks_below_the_override(mock_client):
    cfg = _cfg(min_net_edge=0.10)
    executor = Executor(mock_client, cfg)
    ack = executor.execute(_result(net_edge_dollars=0.05), min_net_edge_override=0.07)
    assert ack is None
    mock_client.place_order.assert_not_called()


def test_min_net_edge_override_none_uses_normal_threshold(mock_client):
    cfg = _cfg(min_net_edge=0.10)
    executor = Executor(mock_client, cfg)
    ack = executor.execute(_result(net_edge_dollars=0.08), min_net_edge_override=None)
    assert ack is None  # 0.08 < 0.10 normal bar, no override given
    mock_client.place_order.assert_not_called()


def test_min_net_edge_override_via_check_gates_directly(mock_client):
    cfg = _cfg(min_net_edge=0.10)
    executor = Executor(mock_client, cfg)
    reason, contracts = executor.check_gates(_result(net_edge_dollars=0.08), min_net_edge_override=0.07)
    assert reason is None
    assert contracts == 5


def test_adverse_selection_flag_overrides_the_discount(mock_client, mocker):
    cfg = _cfg(min_net_edge=0.10)
    executor = Executor(mock_client, cfg)
    mocker.patch("kbot.analytics.adverse_selection.is_flagged", return_value=True)
    mocker.patch("kbot.analytics.adverse_selection.effective_min_net_edge", return_value=0.20)

    # Would clear a 0.07 discount, but the strategy is adverse-selection-flagged
    # (raised bar 0.20) -- safety must win over the discount.
    ack = executor.execute(_result(net_edge_dollars=0.08), strategy="btc_hourly", min_net_edge_override=0.07)
    assert ack is None
    mock_client.place_order.assert_not_called()


def test_no_adverse_selection_flag_lets_discount_apply(mock_client, mocker):
    cfg = _cfg(min_net_edge=0.10)
    executor = Executor(mock_client, cfg)
    mocker.patch("kbot.analytics.adverse_selection.is_flagged", return_value=False)

    ack = executor.execute(_result(net_edge_dollars=0.08), strategy="btc_hourly", min_net_edge_override=0.07)
    assert ack is not None
    mock_client.place_order.assert_called_once()


# ── Live-mode gauntlet ────────────────────────────────────────────────────────

def test_live_order_blocked_on_demo_env(mock_client):
    cfg = _cfg(kalshi_env="demo", dry_run=False)
    executor = Executor(mock_client, cfg, live_confirmed=True)
    ack = executor.execute(_result())
    assert ack is None
    mock_client.place_order.assert_not_called()


def test_live_order_blocked_without_confirmation(mock_client):
    cfg = _cfg(kalshi_env="prod", dry_run=False)
    executor = Executor(mock_client, cfg, live_confirmed=False)
    ack = executor.execute(_result())
    assert ack is None
    mock_client.place_order.assert_not_called()


def test_live_order_passes_gauntlet(mock_client):
    mock_client.place_order.side_effect = _filled_ack(
        dry_run=False, order_id="oid-1", client_order_id="cid-1"
    )
    cfg = _cfg(kalshi_env="prod", dry_run=False, min_net_edge=0.04)
    executor = Executor(mock_client, cfg, live_confirmed=True)
    ack = executor.execute(_result(net_edge_dollars=0.05))
    assert ack is not None
    mock_client.place_order.assert_called_once()


# ── V2 fill accounting (immediate_or_cancel can fill partially or not at all) ─

def test_partial_fill_records_only_what_filled(mock_client, tmp_path):
    """
    Requested 5, filled 3. The position, the P&L cost basis and the exposure
    must all follow the 3 that actually traded -- recording 5 would settle two
    contracts that never existed.
    """
    tracker = PositionTracker(tmp_path / "pos.db")
    mock_client.place_order.side_effect = lambda *, ticker, side, count, price_dollars, **kw: OrderAck(
        client_order_id="cid-1", order_id="oid-1", dry_run=True,
        fill_count_units=300, remaining_count_units=200, average_fill_price=0.58,
    )
    executor = Executor(mock_client, _cfg(), tracker=tracker)

    ack = executor.execute(_result(contracts=5, price=0.60))

    assert ack is not None
    assert ack.fill_count_units == 300
    row = tracker.open_positions_with_exposure()[0]
    assert row["contracts_units"] == 300
    assert row["price"] == pytest.approx(0.58)   # achieved, not requested
    tracker.close()


def test_zero_fill_opens_no_position(mock_client, tmp_path):
    """
    An immediate_or_cancel order that crossed nothing is final -- no position
    was opened, so recording one would both invent a trade for P&L to settle
    and block the ticker behind the position guard.
    """
    tracker = PositionTracker(tmp_path / "pos.db")
    mock_client.place_order.side_effect = lambda *, ticker, side, count, price_dollars, **kw: OrderAck(
        client_order_id="cid-1", order_id="oid-1", dry_run=True,
        fill_count_units=0, remaining_count_units=count * 100, average_fill_price=None,
    )
    executor = Executor(mock_client, _cfg(), tracker=tracker)

    ack = executor.execute(_result())

    assert ack is None
    assert tracker.open_positions_with_exposure() == []
    assert not tracker.is_held("TICK", Side.YES)
    tracker.close()


def test_full_fill_uses_the_achieved_average_price(mock_client, tmp_path):
    tracker = PositionTracker(tmp_path / "pos.db")
    mock_client.place_order.side_effect = lambda *, ticker, side, count, price_dollars, **kw: OrderAck(
        client_order_id="cid-1", order_id="oid-1", dry_run=True,
        fill_count_units=count * 100, remaining_count_units=0, average_fill_price=0.57,
    )
    executor = Executor(mock_client, _cfg(), tracker=tracker)

    executor.execute(_result(contracts=5, price=0.60))

    row = tracker.open_positions_with_exposure()[0]
    assert row["contracts_units"] == 500
    assert row["price"] == pytest.approx(0.57)
    tracker.close()


def test_executor_passes_the_side_and_price_through_unconverted(mock_client):
    """
    The yes/no -> bid/ask conversion belongs to KalshiClient alone. The
    executor must keep handing it the bot's own model, or the translation
    would be applied twice.
    """
    executor = Executor(mock_client, _cfg())
    executor.execute(_result(side=Side.NO, price=0.30, contracts=5))

    kwargs = mock_client.place_order.call_args.kwargs
    assert kwargs["side"] == "no"
    assert kwargs["price_dollars"] == pytest.approx(0.30)
    assert kwargs["count"] == 5


# ── Dry-run passthrough ───────────────────────────────────────────────────────

def test_dry_run_returns_ack(mock_client):
    cfg = _cfg(dry_run=True)
    executor = Executor(mock_client, cfg)
    ack = executor.execute(_result(net_edge_dollars=0.05))
    assert ack is not None
    assert ack.client_order_id == "cid-123"


# ── RULE R1 taker price floor ───────────────────────────────────────────────────

def test_taker_price_floor_blocks_sub_floor_order(mock_client):
    cfg = _cfg(taker_price_floor=0.15, min_net_edge=0.01)
    executor = Executor(mock_client, cfg)
    # 0.10 clears the edge gate (net_edge 0.05 >= 0.01) but is below the floor.
    ack = executor.execute(_result(price=0.10, net_edge_dollars=0.05))
    assert ack is None
    mock_client.place_order.assert_not_called()


def test_taker_price_floor_allows_at_floor(mock_client):
    cfg = _cfg(taker_price_floor=0.15, min_net_edge=0.01)
    executor = Executor(mock_client, cfg)
    ack = executor.execute(_result(price=0.15, net_edge_dollars=0.05))
    assert ack is not None
    mock_client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=5, price_dollars=0.15
    )


# ── Opposite-side guard for single-binary markets (2026-07) ──────────────────

def test_single_binary_blocks_opposite_side_when_other_side_held(mock_client):
    executor = Executor(mock_client, _cfg(min_net_edge=0.01))
    # Already holding the NO side of this single-binary ticker.
    executor._tracker.open_position("TICK", Side.NO)

    ack = executor.execute(
        _result(side=Side.YES, net_edge_dollars=0.05), is_single_binary=True,
    )
    assert ack is None  # opposite side blocked
    mock_client.place_order.assert_not_called()


def test_single_binary_opposite_side_returns_specific_reason(mock_client):
    executor = Executor(mock_client, _cfg(min_net_edge=0.01))
    executor._tracker.open_position("TICK", Side.YES)

    reason, contracts = executor.check_gates(
        _result(side=Side.NO), is_single_binary=True,
    )
    assert reason == "opposite_side_held_single_binary"
    assert contracts == 0


def test_non_single_binary_allows_opposite_side(mock_client):
    # The hourly B/T ladder: holding NO on this ticker must NOT block a YES
    # signal (it's a different contract in a spread), so is_single_binary=False.
    executor = Executor(mock_client, _cfg(min_net_edge=0.01))
    executor._tracker.open_position("TICK", Side.NO)

    ack = executor.execute(
        _result(side=Side.YES, net_edge_dollars=0.05), is_single_binary=False,
    )
    assert ack is not None  # opposite side allowed on a ladder market
    mock_client.place_order.assert_called_once()


def test_single_binary_same_side_still_blocked_by_position_guard(mock_client):
    # The existing same-side guard fires regardless of is_single_binary.
    executor = Executor(mock_client, _cfg(min_net_edge=0.01))
    executor._tracker.open_position("TICK", Side.YES)

    reason, _ = executor.check_gates(_result(side=Side.YES), is_single_binary=True)
    assert reason == "position_already_held"  # same-side guard, not the opposite-side one


def test_single_binary_first_entry_not_blocked(mock_client):
    # Nothing held yet -> a single-binary market's first side goes through.
    executor = Executor(mock_client, _cfg(min_net_edge=0.01))
    ack = executor.execute(
        _result(side=Side.YES, net_edge_dollars=0.05), is_single_binary=True,
    )
    assert ack is not None
    mock_client.place_order.assert_called_once()


def test_single_binary_flag_defaults_off(mock_client):
    # Backward-compat: without the flag, opposite-side entry is allowed
    # (pre-2026-07 behavior -- the guard is opt-in per market type).
    executor = Executor(mock_client, _cfg(min_net_edge=0.01))
    executor._tracker.open_position("TICK", Side.NO)
    ack = executor.execute(_result(side=Side.YES, net_edge_dollars=0.05))
    assert ack is not None
    mock_client.place_order.assert_called_once()


def test_fractional_partial_fill_is_recorded_exactly(mock_client, tmp_path):
    """
    Kalshi reports fill_count as a fixed-point string. The old _parse_count
    did int(round(value)), so a 3.5-contract fill was booked as 4 -- half a
    contract of cost basis, exposure and settlement value invented outright.
    """
    tracker = PositionTracker(tmp_path / "pos.db")
    mock_client.place_order.side_effect = lambda *, ticker, side, count, price_dollars, **kw: OrderAck(
        client_order_id="cid-1", order_id="oid-1", dry_run=True,
        fill_count_units=350, remaining_count_units=150, average_fill_price=0.58,
    )
    executor = Executor(mock_client, _cfg(), tracker=tracker)

    ack = executor.execute(_result(contracts=5, price=0.60))

    assert ack is not None
    assert ack.fill_count_units == 350
    assert ack.fill_contracts == 3.5
    row = tracker.open_positions_with_exposure()[0]
    assert row["contracts_units"] == 350
    tracker.close()
