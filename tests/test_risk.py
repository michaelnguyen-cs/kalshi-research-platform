"""Tests for kbot.risk — exposure tracker, caps, and kill-switch."""

import pytest

import kbot.risk as risk_module
from kbot.config import Settings
from kbot.models.types import Side, SizingResult
from kbot.risk import ExposureTracker, apply_caps, check_daily_loss


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env="demo",
        dry_run=True,
        max_position_pct=kwargs.get("max_position_pct", 0.05),
        max_total_exposure_pct=kwargs.get("max_total_exposure_pct", 0.40),
        max_contracts_per_order=kwargs.get("max_contracts_per_order", 100),
        daily_loss_limit_pct=kwargs.get("daily_loss_limit_pct", 0.10),
    )


def _result(contracts=10, price=0.60, ticker="TICK") -> SizingResult:
    return SizingResult(
        ticker=ticker,
        side=Side.YES,
        contracts=contracts,
        price_dollars=price,
        net_edge_dollars=0.05,
        kelly_fraction_used=0.1,
    )


@pytest.fixture(autouse=True)
def reset_kill_switch():
    """Reset the module-level kill-switch before each test."""
    risk_module._KILL_SWITCH_TRIPPED = False
    yield
    risk_module._KILL_SWITCH_TRIPPED = False


# ── ExposureTracker ───────────────────────────────────────────────────────────

def test_tracker_starts_empty():
    t = ExposureTracker()
    assert t.total_exposure() == 0.0


def test_tracker_update_and_query():
    t = ExposureTracker()
    t.update("TICK-A", 10, 0.60)
    assert abs(t.position_exposure("TICK-A") - 6.0) < 1e-9
    assert abs(t.total_exposure() - 6.0) < 1e-9


def test_tracker_multiple_tickers():
    t = ExposureTracker()
    t.update("A", 10, 0.60)
    t.update("B", 20, 0.40)
    assert abs(t.total_exposure() - (6.0 + 8.0)) < 1e-9


def test_tracker_overwrite():
    t = ExposureTracker()
    t.update("A", 10, 0.60)
    t.update("A", 5, 0.60)   # reduces position
    assert abs(t.total_exposure() - 3.0) < 1e-9


def test_tracker_close():
    t = ExposureTracker()
    t.update("A", 10, 0.60)
    t.close("A")
    assert t.total_exposure() == 0.0
    assert t.position_exposure("A") == 0.0


# ── apply_caps ────────────────────────────────────────────────────────────────

def test_caps_not_applied_when_within_limits():
    cfg = _cfg(max_position_pct=0.50, max_total_exposure_pct=0.90, max_contracts_per_order=100)
    result = _result(contracts=5, price=0.60)
    tracker = ExposureTracker()
    out = apply_caps(result, bankroll=1000.0, tracker=tracker, cfg=cfg)
    assert out.contracts == 5
    assert not out.capped


def test_per_position_cap_applied():
    # max_position_pct=0.05, bankroll=1000 → max $50 exposure at $0.60 → 83 contracts
    # but we request 200
    cfg = _cfg(max_position_pct=0.05, max_total_exposure_pct=0.90, max_contracts_per_order=1000)
    result = _result(contracts=200, price=0.60)
    tracker = ExposureTracker()
    out = apply_caps(result, bankroll=1000.0, tracker=tracker, cfg=cfg)
    assert out.contracts == 83   # floor(50/0.60)
    assert out.capped


def test_order_cap_applied():
    cfg = _cfg(max_position_pct=0.90, max_total_exposure_pct=0.99, max_contracts_per_order=10)
    result = _result(contracts=50, price=0.60)
    tracker = ExposureTracker()
    out = apply_caps(result, bankroll=10000.0, tracker=tracker, cfg=cfg)
    assert out.contracts == 10
    assert out.capped


def test_total_exposure_cap_applied():
    # Already have $380 of $400 max total budget used; only $20 left → 33 contracts at $0.60
    cfg = _cfg(max_position_pct=0.50, max_total_exposure_pct=0.40, max_contracts_per_order=1000)
    tracker = ExposureTracker()
    tracker.update("OTHER", 633, 0.60)  # 633 * 0.60 = 379.8
    result = _result(contracts=200, price=0.60, ticker="NEW")
    out = apply_caps(result, bankroll=1000.0, tracker=tracker, cfg=cfg)
    assert out.contracts == 33   # floor((400 - 379.8) / 0.60) = floor(33.67) = 33
    assert out.capped


def test_zero_contracts_passthrough():
    result = _result(contracts=0)
    tracker = ExposureTracker()
    out = apply_caps(result, bankroll=1000.0, tracker=tracker, cfg=_cfg())
    assert out.contracts == 0


# ── kill-switch ───────────────────────────────────────────────────────────────

def test_kill_switch_not_tripped_within_limit():
    check_daily_loss(950.0, 1000.0, _cfg(daily_loss_limit_pct=0.10))
    assert not risk_module._KILL_SWITCH_TRIPPED


def test_kill_switch_trips_at_limit():
    check_daily_loss(900.0, 1000.0, _cfg(daily_loss_limit_pct=0.10))
    assert risk_module._KILL_SWITCH_TRIPPED


def test_kill_switch_trips_beyond_limit():
    check_daily_loss(800.0, 1000.0, _cfg(daily_loss_limit_pct=0.10))
    assert risk_module._KILL_SWITCH_TRIPPED


def test_kill_switch_ignores_zero_start_balance():
    check_daily_loss(0.0, 0.0, _cfg())
    assert not risk_module._KILL_SWITCH_TRIPPED


def test_kill_switch_query():
    from kbot.risk import kill_switch_tripped
    assert not kill_switch_tripped()
    risk_module._KILL_SWITCH_TRIPPED = True
    assert kill_switch_tripped()
