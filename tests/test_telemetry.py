"""Tests for kbot.telemetry — SQLite event log."""

import time

import pytest

from kbot.config import Settings
from kbot.telemetry import Telemetry


@pytest.fixture()
def tel(tmp_path):
    cfg = Settings(kalshi_key_id="test", kalshi_env="demo", dry_run=True)
    db = tmp_path / "test_events.db"
    with Telemetry(cfg, db_path=db) as t:
        yield t


# ── Schema / basic writes ─────────────────────────────────────────────────────

def test_empty_db_returns_no_events(tel):
    assert tel.recent() == []


def test_log_order_is_retrievable(tel):
    tel.log_order("TICK", {"contracts": 5, "price": 0.60})
    events = tel.recent(event_type="order")
    assert len(events) == 1
    assert events[0]["event_type"] == "order"
    assert events[0]["ticker"] == "TICK"
    assert events[0]["payload"]["contracts"] == 5


def test_log_fill(tel):
    tel.log_fill("TICK", {"order_id": "oid-1", "count": 3})
    events = tel.recent(event_type="fill")
    assert len(events) == 1
    assert events[0]["payload"]["order_id"] == "oid-1"


def test_log_pnl(tel):
    tel.log_pnl({"daily_pnl": -12.5})
    events = tel.recent(event_type="pnl")
    assert len(events) == 1
    assert events[0]["ticker"] is None


def test_log_kill_switch(tel):
    tel.log_kill_switch({"loss_pct": 0.12})
    events = tel.recent(event_type="kill_switch")
    assert len(events) == 1


def test_log_generic_event(tel):
    tel.log_event("custom", {"key": "value"}, ticker="MY-TICK")
    events = tel.recent(event_type="custom")
    assert events[0]["ticker"] == "MY-TICK"
    assert events[0]["payload"]["key"] == "value"


# ── Filtering and ordering ────────────────────────────────────────────────────

def test_recent_filters_by_type(tel):
    tel.log_order("A", {"x": 1})
    tel.log_fill("B", {"y": 2})
    orders = tel.recent(event_type="order")
    fills = tel.recent(event_type="fill")
    assert len(orders) == 1
    assert len(fills) == 1


def test_recent_returns_newest_first(tel):
    tel.log_order("A", {"seq": 1})
    tel.log_order("B", {"seq": 2})
    events = tel.recent(event_type="order", limit=10)
    # Most recent first
    assert events[0]["payload"]["seq"] == 2
    assert events[1]["payload"]["seq"] == 1


def test_recent_respects_limit(tel):
    for i in range(10):
        tel.log_order("T", {"i": i})
    events = tel.recent(event_type="order", limit=3)
    assert len(events) == 3


# ── Dry-run flag ──────────────────────────────────────────────────────────────

def test_dry_run_flag_recorded(tel):
    tel.log_order("X", {})
    events = tel.recent()
    assert events[0]["dry_run"] is True


# ── Timestamp ─────────────────────────────────────────────────────────────────

def test_timestamp_is_recent(tel):
    tel.log_order("X", {})
    events = tel.recent()
    assert abs(events[0]["ts"] - time.time()) < 2.0


# ── resolved_paper_trades (2026-07 paper-gate promotion check) ────────────────

def test_resolved_paper_trades_returns_dry_run_outcomes_oldest_first(tel):
    tel.log_trade_outcome("T1", {"strategy": "s", "pnl_dollars": 1.0})
    tel.log_trade_outcome("T2", {"strategy": "s", "pnl_dollars": 2.0})
    trades = tel.resolved_paper_trades("s")
    assert [t["pnl_dollars"] for t in trades] == [1.0, 2.0]  # oldest first


def test_resolved_paper_trades_filters_by_strategy(tel):
    tel.log_trade_outcome("T1", {"strategy": "s", "pnl_dollars": 1.0})
    tel.log_trade_outcome("T2", {"strategy": "other", "pnl_dollars": 9.0})
    trades = tel.resolved_paper_trades("s")
    assert len(trades) == 1
    assert trades[0]["pnl_dollars"] == 1.0


def test_resolved_paper_trades_excludes_live_dry_run_zero(tmp_path):
    # A live (dry_run=0) trade_outcome must NOT count as a resolved PAPER trade.
    live = Settings(kalshi_key_id="test", dry_run=False)
    db = tmp_path / "mixed.db"
    with Telemetry(live, db_path=db) as t_live:
        t_live.log_trade_outcome("LIVE", {"strategy": "s", "pnl_dollars": 5.0})
    paper = Settings(kalshi_key_id="test", dry_run=True)
    with Telemetry(paper, db_path=db) as t_paper:
        t_paper.log_trade_outcome("PAPER", {"strategy": "s", "pnl_dollars": 1.0})
        trades = t_paper.resolved_paper_trades("s")
    assert [t["pnl_dollars"] for t in trades] == [1.0]  # only the dry_run=1 one
