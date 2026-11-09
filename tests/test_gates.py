"""Tests for kbot.gates — RULE R3 kill-fast strategy gate registry."""

from __future__ import annotations

import pytest

import json
import time

from kbot.config import Settings
from kbot.gates import (
    LEVELS,
    GateRegistry,
    check_kill_fast,
    check_paper_promotion,
    pnl_distribution_summary,
)
from kbot.telemetry import Telemetry

_DAY = 86400.0


def _cfg(**kwargs) -> Settings:
    return Settings(kalshi_key_id="test", dry_run=True)


@pytest.fixture(autouse=True)
def _clear_adverse_flags():
    # evaluate_flag (used by check_paper_promotion) mutates a process-global
    # flag dict; clear it around every test so state never leaks between them.
    from kbot.analytics.adverse_selection import clear_all_flags
    clear_all_flags()
    yield
    clear_all_flags()


@pytest.fixture()
def registry(tmp_path) -> GateRegistry:
    r = GateRegistry(db_path=tmp_path / "gates.db")
    yield r
    r.close()


@pytest.fixture()
def telemetry(tmp_path) -> Telemetry:
    t = Telemetry(db_path=tmp_path / "events.db")
    yield t
    t.close()


def _log_trade(telemetry: Telemetry, strategy: str, pnl: float, ticker: str = "TICK") -> None:
    telemetry.log_trade_outcome(ticker, {"strategy": strategy, "pnl_dollars": pnl})


def _seed_paper_ready(
    registry: GateRegistry,
    telemetry: Telemetry,
    strategy: str = "stratP",
    *,
    n_trades: int = 50,
    pnl_each: float = 1.0,
    days_ago: float = 15.0,
) -> str:
    """Seed a strategy that PASSES every paper-gate criterion, then callers
    break exactly one to test that criterion in isolation."""
    registry.register_backtest(strategy, [1.0, 2.0, 3.0], notes="bt")
    # Backdate the backtest registration so the 14-day clock is satisfied by
    # real wall-clock now (keeps kill-fast event timestamps consistent).
    registry._conn.execute(
        "UPDATE strategy_gates SET backtest_registered_at = ? WHERE strategy = ?",
        (time.time() - days_ago * _DAY, strategy),
    )
    registry._conn.commit()
    for i in range(n_trades):
        _log_trade(telemetry, strategy, pnl_each, ticker=f"T{i}")
    return strategy


# ── Registry basics: level ordering, demote, promote ────────────────────────────

def test_new_strategy_defaults_to_first_level(registry):
    assert registry.get_gate("stratA") is None  # nothing registered yet
    level = registry.demote("stratA", "test")
    assert level == LEVELS[0]  # already at the floor -- demote can't go lower


def test_demote_moves_down_one_level(registry):
    registry.promote("stratA")  # backtest -> paper
    registry.promote("stratA")  # paper -> live_small
    assert registry.get_gate("stratA").level == "live_small"

    new_level = registry.demote("stratA", "zero_for_ten")
    assert new_level == "paper"
    assert registry.get_gate("stratA").paused is True


def test_demote_floors_at_lowest_level(registry):
    registry.demote("stratA", "zero_for_ten")
    assert registry.get_gate("stratA").level == LEVELS[0]
    registry.demote("stratA", "zero_for_ten_again")
    assert registry.get_gate("stratA").level == LEVELS[0]  # stays at floor


def test_promote_moves_up_one_level_and_clears_pause(registry):
    registry.demote("stratA", "zero_for_ten")
    assert registry.is_paused("stratA") is True

    new_level = registry.promote("stratA")
    assert new_level == "paper"  # backtest -> paper
    assert registry.is_paused("stratA") is False


def test_promote_ceilings_at_highest_level(registry):
    for _ in range(10):
        registry.promote("stratA")
    assert registry.get_gate("stratA").level == LEVELS[-1]


def test_is_paused_false_for_unknown_strategy(registry):
    assert registry.is_paused("never_seen") is False


# ── register_backtest / pnl_distribution_summary ────────────────────────────────

def test_pnl_distribution_summary_basic():
    summary = pnl_distribution_summary([-10.0, -5.0, 0.0, 5.0, 10.0])
    assert summary["n"] == 5
    assert summary["min"] == -10.0
    assert summary["max"] == 10.0
    assert summary["p50"] == pytest.approx(0.0)


def test_pnl_distribution_summary_empty():
    assert pnl_distribution_summary([]) == {"n": 0}


def test_register_backtest_persists_p5(registry):
    registry.register_backtest("stratA", [-1.0] * 5 + [1.0] * 95, notes="documented crowding")
    gate = registry.get_gate("stratA")
    assert gate.backtest_pnl_p5 is not None
    assert gate.notes == "documented crowding"


def test_register_backtest_preserves_existing_level(registry):
    registry.promote("stratA")  # -> paper
    registry.register_backtest("stratA", [1.0, 2.0, 3.0])
    assert registry.get_gate("stratA").level == "paper"


# ── check_kill_fast: 0-for-10 ────────────────────────────────────────────────────

def test_kill_fast_triggers_on_zero_for_ten(registry, telemetry):
    for _ in range(10):
        _log_trade(telemetry, "stratA", -1.0)

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason == "zero_for_ten"
    assert registry.is_paused("stratA") is True


def test_kill_fast_does_not_trigger_with_one_win_in_last_ten(registry, telemetry):
    for _ in range(9):
        _log_trade(telemetry, "stratA", -1.0)
    _log_trade(telemetry, "stratA", 1.0)  # one win breaks the streak

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason is None
    assert registry.is_paused("stratA") is False


def test_kill_fast_does_not_trigger_with_fewer_than_ten_trades(registry, telemetry):
    for _ in range(9):
        _log_trade(telemetry, "stratA", -1.0)

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason is None
    assert registry.is_paused("stratA") is False


def test_kill_fast_checks_most_recent_ten_not_all_time(registry, telemetry):
    # 5 wins, then 10 straight losses -- the losing streak is what matters.
    for _ in range(5):
        _log_trade(telemetry, "stratA", 1.0)
    for _ in range(10):
        _log_trade(telemetry, "stratA", -1.0)

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason == "zero_for_ten"


def test_kill_fast_zero_pnl_counts_as_non_win(registry, telemetry):
    # pnl == 0 is "not a win" (<=0), so 10 zero-pnl trades also trips it.
    for _ in range(10):
        _log_trade(telemetry, "stratA", 0.0)

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason == "zero_for_ten"


# ── check_kill_fast: below backtest p5 ──────────────────────────────────────────

def test_kill_fast_triggers_below_backtest_p5(registry, telemetry):
    # Backtest distribution has a p5 around -1.9 (tight, mostly-winning distribution).
    registry.register_backtest("stratA", [1.0] * 95 + [-2.0] * 5)
    p5 = registry.get_gate("stratA").backtest_pnl_p5

    # Live: 20 resolved trades, alternating small wins/losses so no 0-for-10
    # streak fires, but the average is far below the backtest p5.
    for i in range(20):
        pnl = 0.1 if i % 2 == 0 else -5.0
        _log_trade(telemetry, "stratA", pnl)

    avg = sum([0.1 if i % 2 == 0 else -5.0 for i in range(20)]) / 20
    assert avg < p5  # sanity check on the test's own arithmetic

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason == "below_backtest_p5"
    assert registry.is_paused("stratA") is True


def test_kill_fast_no_trigger_without_backtest_registered(registry, telemetry):
    # No register_backtest() call -> no p5 to compare against -> that leg is a no-op.
    for i in range(20):
        pnl = 0.1 if i % 2 == 0 else -5.0
        _log_trade(telemetry, "stratA", pnl)

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason is None


def test_kill_fast_no_trigger_above_backtest_p5(registry, telemetry):
    registry.register_backtest("stratA", [-5.0] * 10 + [1.0] * 90)
    p5 = registry.get_gate("stratA").backtest_pnl_p5
    assert p5 < 0.5  # sanity check on the test's own arithmetic

    for _ in range(20):
        _log_trade(telemetry, "stratA", 0.5)  # solidly above the backtest p5

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason is None
    assert registry.is_paused("stratA") is False


# ── check_kill_fast: already paused / never raises ──────────────────────────────

def test_kill_fast_no_op_when_already_paused(registry, telemetry):
    registry.demote("stratA", "manual_test_setup")
    for _ in range(10):
        _log_trade(telemetry, "stratA", -1.0)

    reason = check_kill_fast("stratA", telemetry, registry, _cfg())

    assert reason is None  # already paused -- nothing "new" triggers


def test_kill_fast_survives_broken_telemetry(registry, mocker):
    broken_telemetry = mocker.MagicMock()
    broken_telemetry.resolved_trades.side_effect = RuntimeError("db locked")

    reason = check_kill_fast("stratA", broken_telemetry, registry, _cfg())  # must not raise

    assert reason is None


def test_kill_fast_isolates_strategies(registry, telemetry):
    for _ in range(10):
        _log_trade(telemetry, "stratA", -1.0)
    _log_trade(telemetry, "stratB", 1.0)

    check_kill_fast("stratA", telemetry, registry, _cfg())
    check_kill_fast("stratB", telemetry, registry, _cfg())

    assert registry.is_paused("stratA") is True
    assert registry.is_paused("stratB") is False


# ── kill-fast trip is persisted (feeds check_paper_promotion) ────────────────

def test_kill_fast_trip_is_logged_to_telemetry(registry, telemetry):
    for _ in range(10):
        _log_trade(telemetry, "stratK", -1.0)
    check_kill_fast("stratK", telemetry, registry, _cfg())

    events = telemetry.recent_types(["kill_fast"])
    assert len(events) == 1
    assert events[0]["payload"]["strategy"] == "stratK"
    assert events[0]["payload"]["reason"] == "zero_for_ten"


# ── backtest_registered_at anchoring ─────────────────────────────────────────

def test_register_backtest_sets_registered_at(registry):
    registry.register_backtest("s", [1.0, 2.0], notes="bt")
    gate = registry.get_gate("s")
    assert gate.backtest_registered_at is not None
    assert abs(gate.backtest_registered_at - time.time()) < 2.0


def test_registered_at_not_reset_by_later_writes(registry):
    registry.register_backtest("s", [1.0, 2.0], notes="bt")
    first = registry.get_gate("s").backtest_registered_at
    time.sleep(0.01)
    registry.register_backtest("s", [3.0, 4.0], notes="refresh")  # re-register
    registry.demote("s", "some_reason")                            # and a demote
    assert registry.get_gate("s").backtest_registered_at == first  # unchanged


# ── check_paper_promotion: all criteria pass ─────────────────────────────────

def test_paper_promotion_passes_when_all_criteria_met(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry)
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is True
    assert result.failures == []


# ── each criterion independently blocks ──────────────────────────────────────

def test_blocks_when_under_14_days(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry, days_ago=10.0)  # too recent
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is False
    assert len(result.failures) == 1
    assert "days since backtest" in result.failures[0]


def test_blocks_when_under_50_resolved_trades(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry, n_trades=49)
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is False
    assert len(result.failures) == 1
    assert "resolved paper trades" in result.failures[0]


def test_blocks_when_pnl_not_positive(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry, pnl_each=-1.0)  # 50 losers
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is False
    assert len(result.failures) == 1
    assert "fee-adjusted paper P&L" in result.failures[0]


def test_blocks_on_kill_fast_trip_in_last_7_days(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry)
    telemetry.log_event("kill_fast", {"strategy": s, "reason": "zero_for_ten"})  # now
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is False
    assert len(result.failures) == 1
    assert "kill-fast trip" in result.failures[0]


def test_kill_fast_trip_older_than_7_days_does_not_block(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry)
    # Insert an 8-day-old kill_fast event directly (log_event uses now()).
    telemetry._conn.execute(
        "INSERT INTO events (ts, event_type, ticker, dry_run, payload) VALUES (?, 'kill_fast', NULL, 1, ?)",
        (time.time() - 8 * _DAY, json.dumps({"strategy": s, "reason": "zero_for_ten"})),
    )
    telemetry._conn.commit()
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is True  # outside the 7-day window -> not a blocker


def test_blocks_on_adverse_selection_flag(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry)
    # 30 fills (= adverse_selection_window) with a strongly negative markout
    # trips evaluate_flag (mean < -0.01).
    for i in range(30):
        telemetry.log_markout({
            "fill_event_id": f"f{i}", "ticker": "T", "strategy": s,
            "side": "yes", "fill_price": 0.5, "markout_60s": -0.02,
        })
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is False
    assert len(result.failures) == 1
    assert "adverse_selection" in result.failures[0]


def test_no_registration_blocks_with_clear_reason(registry, telemetry):
    result = check_paper_promotion("never_registered", registry, telemetry, _cfg())
    assert result.passed is False
    assert any("no backtest registration" in f for f in result.failures)


def test_partial_pass_reports_exactly_the_failing_criteria(registry, telemetry):
    # Break TWO criteria: too recent (5 days) AND too few trades (10).
    # P&L (10 positive), kill-fast (none), adverse (none) all pass.
    s = _seed_paper_ready(registry, telemetry, days_ago=5.0, n_trades=10, pnl_each=1.0)
    result = check_paper_promotion(s, registry, telemetry, _cfg())
    assert result.passed is False
    assert len(result.failures) == 2
    joined = " | ".join(result.failures)
    assert "days since backtest" in joined
    assert "resolved paper trades" in joined
    assert "fee-adjusted" not in joined  # this one passed


def test_check_paper_promotion_is_read_only(registry, telemetry):
    s = _seed_paper_ready(registry, telemetry, days_ago=1.0)  # will fail
    before = registry.get_gate(s)
    check_paper_promotion(s, registry, telemetry, _cfg())
    after = registry.get_gate(s)
    assert after.level == before.level        # gate untouched
    assert after.paused == before.paused
