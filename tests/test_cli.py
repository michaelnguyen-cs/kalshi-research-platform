"""Tests for kbot.cli helpers -- dead-man's switch heartbeat staleness check
and the OS-dispatched watchdog/supervisor health check.

The Windows schtasks parsing path stays untested (it needs Task Scheduler
output), but the platform dispatch and the Linux systemd path are covered by
mocking platform.system() and subprocess.run().
"""

import json
import time

import pytest
import typer

from kbot.cli import heartbeat_is_stale


def test_heartbeat_missing_file_is_stale(tmp_path):
    hb_path = tmp_path / "kbot_heartbeat.json"
    assert heartbeat_is_stale(hb_path, now=time.time()) is True


def test_heartbeat_fresh_is_not_stale(tmp_path):
    hb_path = tmp_path / "kbot_heartbeat.json"
    now = time.time()
    hb_path.write_text(json.dumps({"ts": now - 60}))  # 1 minute old
    assert heartbeat_is_stale(hb_path, now=now) is False


def test_heartbeat_older_than_30min_is_stale(tmp_path):
    hb_path = tmp_path / "kbot_heartbeat.json"
    now = time.time()
    hb_path.write_text(json.dumps({"ts": now - 31 * 60}))  # 31 minutes old
    assert heartbeat_is_stale(hb_path, now=now) is True


def test_heartbeat_exactly_at_threshold_is_not_stale():
    # Boundary: age == max_age_seconds is not > max_age_seconds -- not stale.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        hb_path = Path(d) / "kbot_heartbeat.json"
        now = 1_000_000.0
        hb_path.write_text(json.dumps({"ts": now - 1800}))
        assert heartbeat_is_stale(hb_path, now=now, max_age_seconds=1800.0) is False


def test_heartbeat_just_over_custom_threshold_is_stale(tmp_path):
    hb_path = tmp_path / "kbot_heartbeat.json"
    now = time.time()
    hb_path.write_text(json.dumps({"ts": now - 601}))
    assert heartbeat_is_stale(hb_path, now=now, max_age_seconds=600.0) is True


def test_heartbeat_default_threshold_is_30_minutes():
    from kbot.cli import _HEARTBEAT_DEADMAN_SECONDS
    assert _HEARTBEAT_DEADMAN_SECONDS == 30 * 60


def test_heartbeat_unreadable_json_is_stale(tmp_path):
    hb_path = tmp_path / "kbot_heartbeat.json"
    hb_path.write_text("not valid json{{{")
    assert heartbeat_is_stale(hb_path, now=time.time()) is True


def test_heartbeat_missing_ts_field_treated_as_epoch_zero_is_stale(tmp_path):
    hb_path = tmp_path / "kbot_heartbeat.json"
    hb_path.write_text(json.dumps({"btc_price": 65000.0}))  # no "ts" key
    assert heartbeat_is_stale(hb_path, now=time.time()) is True


# ── watchdog health allow-list (2026-07) ─────────────────────────────────────

def test_ignorenew_refusal_code_is_treated_as_healthy():
    # 0x800710E0: the 5-min re-check trigger fired while the watchdog was
    # already running and IgnoreNew refused a duplicate -- healthy, not an
    # error. Must be in the OK set so `kbot status` doesn't false-alarm.
    from kbot.cli import _WATCHDOG_OK_RESULTS
    assert "-2147020576" in _WATCHDOG_OK_RESULTS
    assert "0" in _WATCHDOG_OK_RESULTS and "267009" in _WATCHDOG_OK_RESULTS


# ── watchdog check OS dispatch + Linux systemd path ──────────────────────────

class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_watchdog_check_dispatches_to_systemd_on_linux(mocker):
    from kbot import cli
    mocker.patch("kbot.cli.platform.system", return_value="Linux")
    win = mocker.patch("kbot.cli._check_watchdog_task_windows")
    linux = mocker.patch("kbot.cli._check_systemd_service_linux")

    cli._check_watchdog_task()

    linux.assert_called_once()
    win.assert_not_called()


def test_watchdog_check_dispatches_to_schtasks_on_windows(mocker):
    from kbot import cli
    mocker.patch("kbot.cli.platform.system", return_value="Windows")
    win = mocker.patch("kbot.cli._check_watchdog_task_windows")
    linux = mocker.patch("kbot.cli._check_systemd_service_linux")

    cli._check_watchdog_task()

    win.assert_called_once()
    linux.assert_not_called()


def test_watchdog_check_unsupported_os_warns_and_does_not_raise(mocker):
    from kbot import cli
    mocker.patch("kbot.cli.platform.system", return_value="Darwin")
    win = mocker.patch("kbot.cli._check_watchdog_task_windows")
    linux = mocker.patch("kbot.cli._check_systemd_service_linux")
    printed = mocker.patch("kbot.cli.console.print")

    cli._check_watchdog_task()  # must not raise

    win.assert_not_called()
    linux.assert_not_called()
    assert "Darwin" in " ".join(str(c.args[0]) for c in printed.call_args_list)


def test_systemd_check_active_service_is_silent(mocker):
    from kbot import cli
    mocker.patch("kbot.cli.subprocess.run", return_value=_FakeProc(stdout="active\n", returncode=0))
    printed = mocker.patch("kbot.cli.console.print")

    cli._check_systemd_service_linux("kbot")

    printed.assert_not_called()


@pytest.mark.parametrize("state", ["inactive", "failed", "activating", "unknown"])
def test_systemd_check_non_active_state_warns(mocker, state):
    from kbot import cli
    rc = 0 if state == "active" else 3
    mocker.patch("kbot.cli.subprocess.run", return_value=_FakeProc(stdout=state + "\n", returncode=rc))
    printed = mocker.patch("kbot.cli.console.print")

    cli._check_systemd_service_linux("kbot")

    printed.assert_called_once()
    msg = str(printed.call_args.args[0])
    assert "NOT ACTIVE" in msg
    assert state in msg


def test_systemd_check_uses_is_active_with_default_service_name(mocker):
    """The default (no arg) must query the deployed unit name via is-active."""
    from kbot import cli
    run = mocker.patch("kbot.cli.subprocess.run", return_value=_FakeProc(stdout="active", returncode=0))

    cli._check_systemd_service_linux()  # rely on the default

    argv = run.call_args.args[0]
    assert argv == ["systemctl", "is-active", "kbot"]


def test_systemd_check_query_failure_is_swallowed(mocker):
    from kbot import cli
    mocker.patch("kbot.cli.subprocess.run", side_effect=FileNotFoundError("systemctl not found"))
    printed = mocker.patch("kbot.cli.console.print")

    cli._check_systemd_service_linux("kbot")  # must not raise

    printed.assert_called_once()
    assert "Could not query" in str(printed.call_args.args[0])


def test_default_linux_service_name_is_deployed_unit():
    from kbot.cli import _LINUX_SERVICE_NAME
    assert _LINUX_SERVICE_NAME == "kbot"


# ── _resolve_strategies_and_allocations (Phase B2 --strategies flag) ─────────

def test_resolve_all_loads_every_configured_strategy(monkeypatch):
    from kbot import cli
    from kbot.config import Settings

    monkeypatch.setattr(cli, "settings", Settings(
        kalshi_key_id="", dry_run=True, strategy_allocations="btc_hourly:0.7,base_rate:0.3",
    ))
    strategies, allocations = cli._resolve_strategies_and_allocations("all")

    assert set(strategies) == {"btc_hourly", "base_rate"}
    assert allocations == {"btc_hourly": 0.7, "base_rate": 0.3}


def test_resolve_subset_filters_to_requested_names(monkeypatch):
    from kbot import cli
    from kbot.config import Settings

    monkeypatch.setattr(cli, "settings", Settings(
        kalshi_key_id="", dry_run=True, strategy_allocations="btc_hourly:0.7,base_rate:0.3",
    ))
    strategies, allocations = cli._resolve_strategies_and_allocations("btc_hourly")

    assert set(strategies) == {"btc_hourly"}
    assert allocations == {"btc_hourly": 0.7}


def test_resolve_unknown_name_exits_with_error(monkeypatch):
    from kbot import cli
    from kbot.config import Settings

    monkeypatch.setattr(cli, "settings", Settings(
        kalshi_key_id="", dry_run=True, strategy_allocations="btc_hourly:1.0",
    ))
    with pytest.raises(typer.Exit):
        cli._resolve_strategies_and_allocations("nonexistent_strategy")


def test_resolve_all_is_case_and_whitespace_tolerant(monkeypatch):
    from kbot import cli
    from kbot.config import Settings

    monkeypatch.setattr(cli, "settings", Settings(
        kalshi_key_id="", dry_run=True, strategy_allocations="btc_hourly:1.0",
    ))
    strategies, _ = cli._resolve_strategies_and_allocations("  ALL  ")
    assert set(strategies) == {"btc_hourly"}


# ── champion calibrator loader (2026-07 bootstrap) ───────────────────────────

def test_install_champion_calibrator_noop_when_registry_empty(tmp_path, monkeypatch):
    from kbot import cli
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    from kbot.config import Settings

    monkeypatch.chdir(tmp_path)  # ModelRegistry() defaults to cwd/kbot_registry.db
    s = BTCHourlyStrategy(Settings(kalshi_key_id="", dry_run=True))
    cli._install_champion_calibrator(s, series="KXBTC15M")  # empty registry -> no-op, no raise
    assert s._calibrator_for("KXBTC15M-26JUL060115-15") is None


def test_install_champion_calibrator_loads_matching_series(tmp_path, monkeypatch):
    from kbot import cli
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    from kbot.config import Settings
    from kbot.learning.calibration import fit_calibrator
    from kbot.learning.registry import ModelRegistry
    import numpy as np

    monkeypatch.chdir(tmp_path)
    rng = np.random.default_rng(0)
    probs = rng.uniform(0.2, 0.8, 200).tolist()
    outcomes = rng.binomial(1, probs).tolist()
    cal = fit_calibrator(probs, outcomes)

    reg = ModelRegistry()
    reg.save_version(tag="kxbtc15m_bootstrap", params={"series": "KXBTC15M"},
                     calibrator=cal, metrics={}, is_champion=True)
    reg.close()

    s = BTCHourlyStrategy(Settings(kalshi_key_id="", dry_run=True))
    cli._install_champion_calibrator(s, series="KXBTC15M")
    assert s._calibrator_for("KXBTC15M-26JUL060115-15") is not None


def test_load_kxbtc15m_is_scoped_and_named(tmp_path, monkeypatch):
    """The live kxbtc15m loader: scoped to KXBTC15M only, gated as 'kxbtc15m'."""
    from kbot import cli
    monkeypatch.chdir(tmp_path)  # empty registry -> calibrator install is a no-op
    s = cli._load_kxbtc15m()
    assert s.name == "kxbtc15m"
    assert s.series_tickers == ["KXBTC15M"]
    assert s.owns_ticker("KXBTC15M-26JUL060115-15") is True
    assert s.owns_ticker("KXBTC-26JUL0317-T48000") is False


def test_load_strategy_resolves_kxbtc15m(tmp_path, monkeypatch):
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    s = cli._load_strategy("kxbtc15m")
    assert s.name == "kxbtc15m"


def test_load_kxeth15m_is_scoped_and_named(tmp_path, monkeypatch):
    """The kxeth15m loader: scoped to KXETH15M only, gated as 'kxeth15m'."""
    from kbot import cli
    monkeypatch.chdir(tmp_path)  # empty registry -> calibrator install is a no-op
    s = cli._load_kxeth15m()
    assert s.name == "kxeth15m"
    assert s.series_tickers == ["KXETH15M"]
    assert s.required_feeds == ["eth_spot"]
    assert s.owns_ticker("KXETH15M-26JUL311800-00") is True
    assert s.owns_ticker("KXBTC15M-26JUL060115-15") is False


def test_load_strategy_resolves_kxeth15m(tmp_path, monkeypatch):
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    s = cli._load_strategy("kxeth15m")
    assert s.name == "kxeth15m"


def test_kxeth15m_starts_with_no_calibrator(tmp_path, monkeypatch):
    """
    ETH has no backtest history: it must start on RAW lognormal probabilities
    and earn its way up from the backtest gate, never inheriting a calibrator.
    """
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    s = cli._load_kxeth15m()
    assert s._calibrator_for("KXETH15M-26JUL311800-00") is None


def test_btc_champion_calibrator_is_not_installed_on_eth(tmp_path, monkeypatch):
    """
    Even if a BTC champion is sitting in this deployment's registry, the
    series guard must refuse to hand it to ETH -- the two have unrelated
    miscalibration profiles.
    """
    from kbot import cli
    from kbot.learning.calibration import fit_calibrator
    from kbot.learning.registry import ModelRegistry
    import numpy as np

    monkeypatch.chdir(tmp_path)
    rng = np.random.default_rng(2)
    probs = rng.uniform(0.2, 0.8, 200).tolist()
    outcomes = rng.binomial(1, probs).tolist()
    cal = fit_calibrator(probs, outcomes)

    reg = ModelRegistry()
    reg.save_version(tag="kxbtc15m_bootstrap", params={"series": "KXBTC15M"},
                     calibrator=cal, metrics={}, is_champion=True)
    reg.close()

    s = cli._load_kxeth15m()
    assert s._calibrator_for("KXETH15M-26JUL311800-00") is None


def test_install_champion_calibrator_ignores_mismatched_series(tmp_path, monkeypatch):
    """A champion whose params name a DIFFERENT series must not be applied."""
    from kbot import cli
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    from kbot.config import Settings
    from kbot.learning.calibration import fit_calibrator
    from kbot.learning.registry import ModelRegistry
    import numpy as np

    monkeypatch.chdir(tmp_path)
    rng = np.random.default_rng(1)
    probs = rng.uniform(0.2, 0.8, 200).tolist()
    outcomes = rng.binomial(1, probs).tolist()
    cal = fit_calibrator(probs, outcomes)

    reg = ModelRegistry()
    reg.save_version(tag="some_other", params={"series": "KXBTC"},
                     calibrator=cal, metrics={}, is_champion=True)
    reg.close()

    s = BTCHourlyStrategy(Settings(kalshi_key_id="", dry_run=True))
    cli._install_champion_calibrator(s, series="KXBTC15M")
    assert s._calibrator_for("KXBTC15M-26JUL060115-15") is None


# ── gates promote --to paper / --force (2026-07 paper-gate enforcement) ───────

def _clear_flags():
    from kbot.analytics.adverse_selection import clear_all_flags
    clear_all_flags()


def _seed_gate_only(strategy="kxbtc15m"):
    """Register a strategy at backtest with NO paper trades -> fails the gate."""
    from kbot.gates import GateRegistry
    with GateRegistry(db_path="kbot_gates.db") as g:
        g.register_backtest(strategy, [1.0, 2.0], notes="bt")


def _seed_paper_ready(strategy="kxbtc15m", n_trades=50, pnl_each=1.0, days_ago=15.0):
    import time
    from kbot.gates import GateRegistry
    from kbot.telemetry import Telemetry
    from kbot.config import Settings
    with GateRegistry(db_path="kbot_gates.db") as g:
        g.register_backtest(strategy, [1.0, 2.0, 3.0], notes="bt")
        g._conn.execute(
            "UPDATE strategy_gates SET backtest_registered_at = ? WHERE strategy = ?",
            (time.time() - days_ago * 86400.0, strategy),
        )
        g._conn.commit()
    with Telemetry(Settings(kalshi_key_id="", dry_run=True), db_path="kbot_events.db") as t:
        for i in range(n_trades):
            t.log_trade_outcome(f"T{i}", {"strategy": strategy, "pnl_dollars": pnl_each})


def _gate_level(strategy="kxbtc15m"):
    from kbot.gates import GateRegistry
    with GateRegistry(db_path="kbot_gates.db") as g:
        gate = g.get_gate(strategy)
        return gate.level if gate else None


def test_promote_to_paper_refused_when_criteria_fail(tmp_path, monkeypatch):
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    _clear_flags()
    _seed_gate_only()

    with pytest.raises(typer.Exit):
        cli.gates_promote("kxbtc15m", to="paper", force=False)
    assert _gate_level() == "backtest"  # NOT promoted


def test_promote_to_paper_allowed_when_all_criteria_pass(tmp_path, monkeypatch):
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    _clear_flags()
    _seed_paper_ready()

    cli.gates_promote("kxbtc15m", to="paper", force=False)  # must not raise
    assert _gate_level() == "paper"


def test_force_bypasses_check_with_correct_phrase(tmp_path, monkeypatch, mocker):
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    _clear_flags()
    _seed_gate_only()  # would fail the real check
    mocker.patch("kbot.cli.typer.prompt", return_value="I UNDERSTAND THE RISKS")

    cli.gates_promote("kxbtc15m", to="paper", force=True)
    assert _gate_level() == "paper"  # bypassed and promoted


def test_force_aborts_on_wrong_confirmation_phrase(tmp_path, monkeypatch, mocker):
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    _clear_flags()
    _seed_gate_only()
    mocker.patch("kbot.cli.typer.prompt", return_value="whatever")

    with pytest.raises(typer.Exit):
        cli.gates_promote("kxbtc15m", to="paper", force=True)
    assert _gate_level() == "backtest"  # not promoted


def test_to_mismatch_is_rejected(tmp_path, monkeypatch):
    from kbot import cli
    monkeypatch.chdir(tmp_path)
    _seed_gate_only()
    # Next level up from backtest is 'paper', not 'live_small'.
    with pytest.raises(typer.Exit):
        cli.gates_promote("kxbtc15m", to="live_small", force=False)
    assert _gate_level() == "backtest"


# ── `kbot status` honours the configured paths (2026-07) ─────────────────────
#
# Both were hardcoded relative literals, so `status` reported on whichever
# service owned ./kbot_heartbeat.json and ./kbot_events.db regardless of the
# environment it was invoked under -- i.e. it would have shown BTC's health
# when asked about ETH.

def test_status_reads_the_configured_heartbeat_path(tmp_path, monkeypatch):
    from pathlib import Path
    from kbot import cli

    monkeypatch.chdir(tmp_path)
    # A decoy at the OLD hardcoded default; the real one lives elsewhere.
    Path("kbot_heartbeat.json").write_text(json.dumps({"ts": time.time(), "pid": 1111}))
    (tmp_path / "eth").mkdir()
    real = tmp_path / "eth" / "hb.json"
    real.write_text(json.dumps({"ts": time.time(), "pid": 2222, "strategies": {}}))

    monkeypatch.setattr(cli.settings, "heartbeat_path", str(real))
    seen = {}
    monkeypatch.setattr(cli, "heartbeat_is_stale",
                        lambda p, now, **kw: seen.setdefault("path", p) and False)
    monkeypatch.setattr(cli, "_check_watchdog_task", lambda: None)

    cli.status(since=None)
    assert seen["path"] == real


def test_status_reads_the_configured_events_db(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path
    from kbot import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_check_watchdog_task", lambda: None)

    # Decoy at the old default -- if status reads this, the override is ignored.
    sqlite3.connect("kbot_events.db").close()
    configured = tmp_path / "eth_events.db"
    monkeypatch.setattr(cli.settings, "events_db_path", str(configured))
    monkeypatch.setattr(cli.settings, "heartbeat_path", str(tmp_path / "absent.json"))

    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(str(a)))

    # The configured DB does not exist -> status must say so and return, rather
    # than silently falling back to the default file that DOES exist.
    cli.status(since=None)
    assert any("No telemetry DB found" in p for p in printed)
    assert not configured.exists()
