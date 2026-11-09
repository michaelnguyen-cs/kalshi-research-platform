"""Tests for kbot.config.parse_strategy_allocations (Phase B2 multi-strategy runner)."""

from __future__ import annotations

import pytest

from kbot.config import Settings, parse_strategy_allocations


def test_parse_single_strategy():
    assert parse_strategy_allocations("btc_hourly:1.0") == {"btc_hourly": 1.0}


def test_parse_multiple_strategies():
    result = parse_strategy_allocations("btc_hourly:0.7,base_rate:0.3")
    assert result == {"btc_hourly": 0.7, "base_rate": 0.3}


def test_parse_tolerates_whitespace():
    result = parse_strategy_allocations(" btc_hourly : 0.7 , base_rate : 0.3 ")
    assert result == {"btc_hourly": 0.7, "base_rate": 0.3}


def test_parse_fractions_summing_to_exactly_one_ok():
    result = parse_strategy_allocations("a:0.5,b:0.5")
    assert result == {"a": 0.5, "b": 0.5}


def test_parse_fractions_under_one_ok():
    result = parse_strategy_allocations("a:0.3")
    assert result == {"a": 0.3}


def test_parse_rejects_sum_over_one():
    with pytest.raises(ValueError, match="must be <= 1.0"):
        parse_strategy_allocations("a:0.7,b:0.5")


def test_parse_rejects_malformed_entry():
    with pytest.raises(ValueError, match="name:fraction"):
        parse_strategy_allocations("btc_hourly")


def test_parse_rejects_non_numeric_fraction():
    with pytest.raises(ValueError, match="non-numeric"):
        parse_strategy_allocations("btc_hourly:abc")


def test_parse_rejects_zero_fraction():
    with pytest.raises(ValueError, match="fraction > 0"):
        parse_strategy_allocations("btc_hourly:0")


def test_parse_rejects_negative_fraction():
    with pytest.raises(ValueError, match="fraction > 0"):
        parse_strategy_allocations("btc_hourly:-0.1")


def test_parse_rejects_duplicate_name():
    with pytest.raises(ValueError, match="more than once"):
        parse_strategy_allocations("a:0.5,a:0.2")


def test_parse_rejects_empty_string():
    with pytest.raises(ValueError, match="at least one"):
        parse_strategy_allocations("")


def test_parse_ignores_trailing_comma():
    result = parse_strategy_allocations("a:0.5,")
    assert result == {"a": 0.5}


def test_settings_validates_strategy_allocations_field():
    with pytest.raises(Exception):  # pydantic ValidationError wraps the ValueError
        Settings(kalshi_key_id="", dry_run=True, strategy_allocations="bad")


def test_settings_default_strategy_allocations():
    # 2026-07: default switched to kxbtc15m (btc_hourly retired -- no edge on
    # 60-minute contracts; 15m has a backtest-validated calibrated edge).
    s = Settings(kalshi_key_id="", dry_run=True)
    assert s.strategy_allocations == "kxbtc15m:1.0"


# ── post-resolution scanner config (2026-07) ─────────────────────────────────

def test_default_post_resolution_window_minutes():
    s = Settings(kalshi_key_id="", dry_run=True)
    assert s.post_resolution_window_minutes == pytest.approx(30.0)


def test_default_post_resolution_edge_discount():
    s = Settings(kalshi_key_id="", dry_run=True)
    assert s.post_resolution_edge_discount == pytest.approx(0.30)


def test_post_resolution_window_minutes_must_be_positive():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, post_resolution_window_minutes=0.0)
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, post_resolution_window_minutes=-5.0)


def test_post_resolution_edge_discount_must_be_in_valid_range():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, post_resolution_edge_discount=-0.1)
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, post_resolution_edge_discount=1.0)


def test_post_resolution_edge_discount_zero_is_allowed():
    # 0 discount = no-op (post-resolution window still opens/tracks, just
    # doesn't lower the bar) -- a valid, if degenerate, configuration.
    s = Settings(kalshi_key_id="", dry_run=True, post_resolution_edge_discount=0.0)
    assert s.post_resolution_edge_discount == 0.0


# ── series-aware vol window/multiplier (2026-07) ─────────────────────────────

def test_default_vol_multiplier_15m():
    s = Settings(kalshi_key_id="", dry_run=True)
    assert s.vol_multiplier_15m == pytest.approx(1.0)


def test_default_vol_window_seconds_hourly_and_15m():
    s = Settings(kalshi_key_id="", dry_run=True)
    assert s.vol_window_seconds_hourly == pytest.approx(3600.0)
    assert s.vol_window_seconds_15m == pytest.approx(900.0)


def test_vol_multiplier_15m_must_be_non_negative():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, vol_multiplier_15m=-0.1)


def test_vol_window_seconds_must_be_positive():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, vol_window_seconds_hourly=0.0)
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, vol_window_seconds_15m=-1.0)


# ── ticker re-discovery interval (2026-07) ───────────────────────────────────

def test_default_ticker_rediscovery_interval():
    s = Settings(kalshi_key_id="", dry_run=True)
    assert s.ticker_rediscovery_interval_seconds == pytest.approx(600.0)


def test_ticker_rediscovery_interval_zero_allowed_disables():
    s = Settings(kalshi_key_id="", dry_run=True, ticker_rediscovery_interval_seconds=0.0)
    assert s.ticker_rediscovery_interval_seconds == 0.0


def test_ticker_rediscovery_interval_negative_rejected():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, ticker_rediscovery_interval_seconds=-1.0)


# ── paper bankroll override (2026-07) ────────────────────────────────────────

def test_paper_bankroll_override_default_is_none():
    # _env_file=None isolates from the operator's real .env (which may set
    # PAPER_BANKROLL_OVERRIDE to enable paper accumulation) so this asserts
    # the genuine CODE default, not the ambient environment.
    s = Settings(kalshi_key_id="", dry_run=True, _env_file=None)
    assert s.paper_bankroll_override is None


def test_paper_bankroll_override_accepts_positive():
    s = Settings(kalshi_key_id="", dry_run=True, paper_bankroll_override=1000.0)
    assert s.paper_bankroll_override == pytest.approx(1000.0)


def test_paper_bankroll_override_rejects_zero_and_negative():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, paper_bankroll_override=0.0)
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, paper_bankroll_override=-100.0)


# ── min_side_win_prob low-bucket floor (2026-07 WS-feed miscalibration fix) ───

def test_min_side_win_prob_default_is_half():
    # _env_file=None isolates from the operator's real .env so this asserts
    # the genuine CODE default, not the ambient environment.
    s = Settings(kalshi_key_id="", dry_run=True, _env_file=None)
    assert s.min_side_win_prob == pytest.approx(0.50)


def test_min_side_win_prob_accepts_bounds():
    assert Settings(kalshi_key_id="", dry_run=True, min_side_win_prob=0.0).min_side_win_prob == 0.0
    assert Settings(kalshi_key_id="", dry_run=True, min_side_win_prob=1.0).min_side_win_prob == 1.0


def test_min_side_win_prob_rejects_out_of_range():
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, min_side_win_prob=-0.1)
    with pytest.raises(Exception):
        Settings(kalshi_key_id="", dry_run=True, min_side_win_prob=1.5)


# ── Per-deployment paths (2026-07: co-tenant services on one host) ───────────

def test_path_defaults_are_unchanged():
    """
    These were hardcoded relative literals before becoming settings. A
    deployment with no env override must resolve exactly what it did before,
    or an existing bot silently starts writing to new files on upgrade.
    _env_file=None so this asserts the CODE default, not the ambient .env.
    """
    s = Settings(kalshi_key_id="", dry_run=True, _env_file=None)
    assert s.heartbeat_path == "kbot_heartbeat.json"
    assert s.log_path == "logs/paper.log"
    assert s.events_db_path == "kbot_events.db"
    assert s.gates_db_path == "kbot_gates.db"
    assert s.registry_db_path == "kbot_registry.db"
    assert s.positions_db_path == "kbot_positions.db"


def test_path_settings_are_overridable():
    s = Settings(
        kalshi_key_id="", dry_run=True, _env_file=None,
        heartbeat_path="/opt/kbot-eth/kbot_eth_heartbeat.json",
        log_path="/opt/kbot-eth/logs/paper.log",
        events_db_path="/opt/kbot-eth/kbot_eth_events.db",
    )
    assert s.heartbeat_path == "/opt/kbot-eth/kbot_eth_heartbeat.json"
    assert s.log_path == "/opt/kbot-eth/logs/paper.log"
    assert s.events_db_path == "/opt/kbot-eth/kbot_eth_events.db"


def test_path_settings_read_from_env(monkeypatch):
    """The deployment mechanism itself: env vars, as systemd EnvironmentFile sets them."""
    monkeypatch.setenv("HEARTBEAT_PATH", "/opt/kbot-eth/hb.json")
    monkeypatch.setenv("LOG_PATH", "/opt/kbot-eth/logs/paper.log")
    s = Settings(kalshi_key_id="", dry_run=True, _env_file=None)
    assert s.heartbeat_path == "/opt/kbot-eth/hb.json"
    assert s.log_path == "/opt/kbot-eth/logs/paper.log"


def test_two_settings_with_different_heartbeat_paths_do_not_collide():
    btc = Settings(kalshi_key_id="", dry_run=True, _env_file=None)
    eth = Settings(kalshi_key_id="", dry_run=True, _env_file=None,
                   heartbeat_path="/opt/kbot-eth/kbot_eth_heartbeat.json")
    assert btc.heartbeat_path != eth.heartbeat_path
    assert btc.heartbeat_path == "kbot_heartbeat.json"  # BTC keeps the old default
