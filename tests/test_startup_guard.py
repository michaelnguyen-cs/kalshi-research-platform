"""
Tests for kbot.startup_guard -- the fail-closed pre-flight check that runs
before any order-capable component is constructed.

Three properties are under test:

  1. An unresolvable effective series is fatal.
  2. A DRY_RUN that is not an explicit recognized true/false value is fatal.
  3. A KXETH* series with DRY_RUN not true is fatal.

plus the wiring property that makes them worth anything: the check runs
BEFORE kbot.runner.Runner builds a KalshiClient or an Executor.
"""

from __future__ import annotations

import pytest

from kbot.config import Settings
from kbot.runner import Runner
from kbot.startup_guard import (
    StartupGuardError,
    assert_startup_safe,
    raw_dry_run_value,
    resolve_dry_run,
    resolve_series,
)
from kbot.strategy.base import Strategy


class _Stub(Strategy):
    """Minimal Strategy carrying only the attributes the guard reads."""

    def __init__(self, name, series_tickers=None, series_15m=None, series_hourly=None):
        self._name = name
        self.series_tickers = series_tickers if series_tickers is not None else []
        if series_15m is not None:
            self.series_15m = series_15m
        if series_hourly is not None:
            self.series_hourly = series_hourly

    @property
    def name(self) -> str:
        return self._name

    def evaluate(self, book, *, btc_price=None, btc_vol=None, bankroll, current_time=None):
        return []


def _cfg(**kwargs) -> Settings:
    return Settings(kalshi_key_id="", dry_run=kwargs.get("dry_run", True))


@pytest.fixture(autouse=True)
def no_ambient_dry_run(monkeypatch, tmp_path):
    """
    Every test states its own DRY_RUN. chdir into tmp_path so the repo's real
    .env is never the answer, and clear any inherited process value.
    """
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.chdir(tmp_path)


# ── 1. Series resolution fails closed ─────────────────────────────────────────

def test_no_strategies_loaded_is_fatal():
    with pytest.raises(StartupGuardError, match="no strategies were loaded"):
        resolve_series({})


def test_strategy_declaring_no_series_is_fatal():
    with pytest.raises(StartupGuardError, match="Cannot resolve the effective series"):
        resolve_series({"a": _Stub("a")})


def test_one_seriesless_strategy_among_several_is_fatal():
    strategies = {"a": _Stub("a", ["KXBTC15M"]), "b": _Stub("b")}
    with pytest.raises(StartupGuardError, match="'b'"):
        resolve_series(strategies)


def test_series_tickers_as_a_bare_string_is_fatal():
    # "KXETH15M" iterates as characters; accepting it would resolve the series
    # to {"K","X","E",...} and let the KXETH check pass.
    with pytest.raises(StartupGuardError, match="must be a list"):
        resolve_series({"a": _Stub("a", "KXETH15M")})


def test_series_unions_declared_tickers_and_calibrator_domains():
    # BTCHourlyStrategy is constructed with a NARROWED series_tickers while
    # series_15m/series_hourly keep the full domain -- both are series sources.
    stub = _Stub("a", ["KXBTC15M"], series_15m="KXBTC15M", series_hourly="KXBTC")
    assert resolve_series({"a": stub}) == frozenset({"KXBTC15M", "KXBTC"})


def test_series_are_upper_cased_and_stripped():
    assert resolve_series({"a": _Stub("a", ["  kxbtc15m "])}) == frozenset({"KXBTC15M"})


def test_explicit_tickers_contribute_their_series_prefix():
    series = resolve_series(
        {"a": _Stub("a", ["KXBTC15M"])}, ["KXETH15M-26JUL311800-00"]
    )
    assert series == frozenset({"KXBTC15M", "KXETH15M"})


@pytest.mark.parametrize("ticker", ["", "   ", None, 42, "-FOO"])
def test_unresolvable_explicit_ticker_is_fatal(ticker):
    with pytest.raises(StartupGuardError, match="Cannot resolve the effective series"):
        resolve_series({"a": _Stub("a", ["KXBTC15M"])}, [ticker])


# ── 2. DRY_RUN must be an explicit recognized true/false value ────────────────

@pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "false", "FALSE", " false "])
def test_recognized_tokens_are_accepted(raw):
    expected = raw.strip().lower() == "true"
    assert resolve_dry_run(_cfg(dry_run=expected), raw=raw) is expected


@pytest.mark.parametrize("raw", ["1", "0", "yes", "no", "on", "off", "t", "f", "y", "n", "", "maybe", "2"])
def test_values_pydantic_would_accept_but_this_project_does_not_are_fatal(raw):
    # pydantic's bool parser takes most of these; DRY_RUN=0 arming live mode
    # is precisely the quiet failure this guard exists to stop.
    with pytest.raises(StartupGuardError, match="not an explicit recognized true/false value"):
        resolve_dry_run(_cfg(dry_run=True), raw=raw)


def test_non_string_raw_is_fatal():
    with pytest.raises(StartupGuardError, match="is not a string"):
        resolve_dry_run(_cfg(dry_run=True), raw=0)


def test_raw_disagreeing_with_the_resolved_config_is_fatal():
    with pytest.raises(StartupGuardError, match="disagrees with the resolved config"):
        resolve_dry_run(_cfg(dry_run=True), raw="false")


def test_unset_dry_run_resolves_to_the_safe_default():
    # Absent is not fatal: absence can only ever produce the Settings default,
    # which is True -- the closed direction.
    assert resolve_dry_run(_cfg(dry_run=True), raw=None) is True


def test_unset_dry_run_with_a_live_config_is_fatal():
    # Live mode always requires someone to have typed the word "false".
    with pytest.raises(StartupGuardError, match="not set in the environment"):
        resolve_dry_run(_cfg(dry_run=False), raw=None)


def test_non_bool_resolved_dry_run_is_fatal():
    class _DuckCfg:
        dry_run = "false"  # truthy: reads as dry-run at every call site

    with pytest.raises(StartupGuardError, match="not a bool"):
        resolve_dry_run(_DuckCfg(), raw="false")


# ── raw value resolution mirrors pydantic-settings ────────────────────────────

def test_raw_value_read_from_the_process_environment(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    assert raw_dry_run_value(Settings) == "false"


def test_raw_value_lookup_is_case_insensitive(monkeypatch):
    # Settings sets case_sensitive=False, so Dry_Run=false really does arm
    # live mode; a case-sensitive check would walk straight past it.
    monkeypatch.setenv("Dry_Run", "false")
    assert raw_dry_run_value(Settings) == "false"


def test_raw_value_falls_back_to_the_env_file(tmp_path):
    (tmp_path / ".env").write_text("KALSHI_ENV=demo\nDRY_RUN=false\n", encoding="utf-8")
    assert raw_dry_run_value(Settings) == "false"


def test_process_environment_wins_over_the_env_file(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("DRY_RUN=false\n", encoding="utf-8")
    monkeypatch.setenv("DRY_RUN", "true")
    assert raw_dry_run_value(Settings) == "true"


def test_raw_value_is_none_when_set_nowhere(tmp_path):
    (tmp_path / ".env").write_text("KALSHI_ENV=demo\n", encoding="utf-8")
    assert raw_dry_run_value(Settings) is None


# ── 3. KXETH* may only run in dry-run ─────────────────────────────────────────

def test_eth_series_live_is_fatal():
    with pytest.raises(StartupGuardError, match=r"KXETH15M.*only be traded with DRY_RUN=true"):
        assert_startup_safe(
            {"kxeth15m": _Stub("kxeth15m", ["KXETH15M"], series_15m="KXETH15M")},
            _cfg(dry_run=False),
            raw_dry_run="false",
        )


def test_eth_series_dry_run_is_allowed():
    result = assert_startup_safe(
        {"kxeth15m": _Stub("kxeth15m", ["KXETH15M"], series_15m="KXETH15M")},
        _cfg(dry_run=True),
        raw_dry_run="true",
    )
    assert result.series == frozenset({"KXETH15M"})
    assert result.dry_run is True


def test_eth_prefix_match_covers_series_other_than_kxeth15m():
    # A future KXETHD/KXETH hourly series inherits the refusal for free.
    with pytest.raises(StartupGuardError, match="KXETHD"):
        assert_startup_safe(
            {"x": _Stub("x", ["KXETHD"])}, _cfg(dry_run=False), raw_dry_run="false"
        )


def test_eth_check_is_case_insensitive():
    with pytest.raises(StartupGuardError, match="KXETH15M"):
        assert_startup_safe(
            {"x": _Stub("x", ["kxeth15m"])}, _cfg(dry_run=False), raw_dry_run="false"
        )


def test_eth_reached_only_via_an_explicit_ticker_is_still_fatal():
    # Explicit TICKERS bypass series discovery entirely, so they are checked
    # as an independent series source.
    with pytest.raises(StartupGuardError, match="KXETH15M"):
        assert_startup_safe(
            {"a": _Stub("a", ["KXBTC15M"])},
            _cfg(dry_run=False),
            tickers=["KXETH15M-26JUL311800-00"],
            raw_dry_run="false",
        )


def test_non_eth_series_live_is_allowed():
    result = assert_startup_safe(
        {"kxbtc15m": _Stub("kxbtc15m", ["KXBTC15M"])},
        _cfg(dry_run=False),
        raw_dry_run="false",
    )
    assert result.dry_run is False
    assert result.series == frozenset({"KXBTC15M"})


def test_eth_live_is_fatal_even_when_co_loaded_with_an_approved_series():
    with pytest.raises(StartupGuardError, match="KXETH15M"):
        assert_startup_safe(
            {"a": _Stub("a", ["KXBTC15M"]), "b": _Stub("b", ["KXETH15M"])},
            _cfg(dry_run=False),
            raw_dry_run="false",
        )


# ── 4. The guard runs before any order-capable component is constructed ───────

def _explode(*args, **kwargs):
    raise AssertionError("an order-capable component was constructed before the guard ran")


@pytest.mark.parametrize(
    "strategies, cfg_kwargs, env, tickers",
    [
        # unresolvable series
        ({"a": _Stub("a")}, {"dry_run": True}, {"DRY_RUN": "true"}, None),
        # unrecognized DRY_RUN
        ({"a": _Stub("a", ["KXBTC15M"])}, {"dry_run": True}, {"DRY_RUN": "1"}, None),
        # KXETH live
        ({"a": _Stub("a", ["KXETH15M"])}, {"dry_run": False}, {"DRY_RUN": "false"}, None),
        # KXETH reached through an explicit ticker
        (
            {"a": _Stub("a", ["KXBTC15M"])},
            {"dry_run": False},
            {"DRY_RUN": "false"},
            ["KXETH15M-26JUL311800-00"],
        ),
    ],
)
def test_runner_refuses_before_building_a_client_or_executor(
    monkeypatch, strategies, cfg_kwargs, env, tickers
):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("kbot.runner.KalshiClient", _explode)
    monkeypatch.setattr("kbot.runner.Executor", _explode)

    with pytest.raises(StartupGuardError):
        Runner(
            strategies,
            {name: 1.0 / len(strategies) for name in strategies},
            _cfg(**cfg_kwargs),
            tickers=tickers,
            live_confirmed=True,
        )


def test_runner_guard_precedes_the_allocations_cross_check(monkeypatch):
    # Ordering proof: a Runner that is wrong in BOTH ways reports the guard's
    # refusal, not the allocations ValueError further down __init__.
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr("kbot.runner.KalshiClient", _explode)
    with pytest.raises(StartupGuardError):
        Runner({"a": _Stub("a")}, {"nonexistent": 1.0}, _cfg(dry_run=True))


# ── 5. The real strategy, through the production loader path ──────────────────
#
# Everything above builds a _Stub whose series attributes are whatever the test
# typed. That proves the guard's logic but not that the guard and the shipped
# KXETH15M strategy agree about what series it trades: a rename of
# series_tickers, a narrowing at construction, or an ETH15MStrategy that
# inherited BTCHourlyStrategy's series_hourly="KXBTC" would all leave the
# stub tests green while the live deployment walks straight past the ETH check.
#
# So these load the real strategy the way `kbot run --strategies kxeth15m`
# does -- STRATEGY_ALLOCATIONS -> _resolve_strategies_and_allocations ->
# _load_strategy -> ETH15MStrategy -- and hand the result to a real Runner.


def _load_real_eth(monkeypatch):
    """
    {"kxeth15m": ETH15MStrategy}, {"kxeth15m": 1.0} via the production loader.

    Patches cli.settings rather than the environment because that is the
    object _load_kxeth15m() closes over; the autouse fixture has already
    chdir'd to tmp_path, so _install_champion_calibrator opens a throwaway
    registry and finds no champion (which is the real deployment's state too
    -- KXETH15M has no fitted calibrator).
    """
    from kbot import cli

    monkeypatch.setattr(cli, "settings", Settings(
        kalshi_key_id="", dry_run=True, strategy_allocations="kxeth15m:1.0",
    ))
    return cli._resolve_strategies_and_allocations("all")


def test_real_eth_strategy_resolves_to_exactly_the_eth_series(monkeypatch):
    # Pins what the guard actually sees. series_tickers is the narrowed list
    # ETH15MStrategy declares; series_15m repeats it and series_hourly is None
    # (no KXETH hourly counterpart), so the union is a single series. If a
    # future edit widens any of the three, this fails here rather than in
    # production.
    strategies, allocations = _load_real_eth(monkeypatch)
    strategy = strategies["kxeth15m"]

    assert allocations == {"kxeth15m": 1.0}
    assert strategy.series_tickers == ["KXETH15M"]
    assert strategy.series_15m == "KXETH15M"
    assert strategy.series_hourly is None
    assert resolve_series(strategies) == frozenset({"KXETH15M"})


def test_real_eth_strategy_live_is_refused_before_a_client_exists(monkeypatch):
    strategies, allocations = _load_real_eth(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr("kbot.runner.KalshiClient", _explode)
    monkeypatch.setattr("kbot.runner.Executor", _explode)

    # live_confirmed=True on purpose: the CLI's --live gauntlet has already
    # been satisfied at this point, so nothing but the guard is left to say no.
    with pytest.raises(StartupGuardError, match="KXETH15M"):
        Runner(strategies, allocations, _cfg(dry_run=False), live_confirmed=True)


def test_real_eth_strategy_constructs_in_dry_run(monkeypatch):
    # The other half of the property: the guard refuses live ETH without
    # refusing ETH. A paper deployment of the real strategy still starts.
    strategies, allocations = _load_real_eth(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")

    runner = Runner(strategies, allocations, _cfg(dry_run=True))
    try:
        assert runner._cfg.dry_run is True
    finally:
        runner.close()
