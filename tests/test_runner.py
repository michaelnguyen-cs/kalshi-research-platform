"""
Tests for kbot.runner.Runner (Phase B2 multi-strategy engine): WS
multiplex routing, shared feed singletons, per-strategy bankroll
allocation, exception/kill-fast isolation, per-strategy gate enforcement,
heartbeat schema, and single-strategy regression (via PaperTrader).

Constructing a Runner is side-effect-free besides opening local SQLite
files (kbot_events.db / kbot_gates.db), so tests chdir into tmp_path.
.run() (which touches the network/WS) is never called here -- _run_cycle_once()
drives one fixed-interval evaluation pass synchronously instead.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kbot.config import Settings
from kbot.gates import GateRegistry, StrategyGate
from kbot.models.types import OrderBook, PriceLevel, Side, SizingResult
from kbot.paper import PaperTrader
from kbot.runner import Runner
from kbot.strategy.base import Strategy
from kbot.telemetry import Telemetry


def _cfg(**kwargs) -> Settings:
    # Path settings are forwarded ONLY when the caller asks for them, so every
    # existing test keeps exercising the real Settings defaults.
    extra = {k: kwargs[k] for k in ("heartbeat_path", "log_path") if k in kwargs}
    return Settings(
        kalshi_key_id="",
        dry_run=kwargs.get("dry_run", True),
        paused_allows_dry_run=kwargs.get("paused_allows_dry_run", True),
        min_net_edge=kwargs.get("min_net_edge", 0.01),
        vol_multiplier_15m=kwargs.get("vol_multiplier_15m", 1.0),
        vol_window_seconds_hourly=kwargs.get("vol_window_seconds_hourly", 3600.0),
        vol_window_seconds_15m=kwargs.get("vol_window_seconds_15m", 900.0),
        paper_bankroll_override=kwargs.get("paper_bankroll_override", None),
        **extra,
    )


def _book(ticker: str, yes_ask: float = 0.50, no_ask: float = 0.48) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes_ask, quantity_units=1000)],
        no=[PriceLevel(price=no_ask, quantity_units=1000)],
    )


def _signal(ticker: str, contracts: int = 10, event_driver: str = "other") -> SizingResult:
    return SizingResult(
        ticker=ticker, side=Side.YES, contracts=contracts, price_dollars=0.5,
        fair_p=0.6, net_edge_dollars=0.1, kelly_fraction_used=0.1,
        capped=False, skip_reason=None, event_driver=event_driver,
    )


class _FakeStrategy(Strategy):
    """Controllable Strategy stub for exercising Runner behavior in isolation."""

    def __init__(self, name, series_tickers=None, required_feeds=None, evaluate_fn=None,
                 expiries=None, single_binary=False, spot_feed_name=None,
                 vol_model=None):
        self._name = name
        # Default to a single series named after the strategy ("a" ->
        # "SERIESA"), matching the SERIESA-1 / SERIESB-1 ticker convention the
        # routing tests below already use. It is NOT the base class's empty
        # list: kbot.startup_guard.assert_startup_safe (called first thing in
        # Runner.__init__) fails closed on a strategy that declares no series,
        # so a seriesless stub can no longer construct a Runner at all. Tests
        # that care about routing still pass series_tickers explicitly; for the
        # rest a series no book matches is inert.
        self.series_tickers = (
            list(series_tickers) if series_tickers is not None else [f"SERIES{name.upper()}"]
        )
        self.required_feeds = list(required_feeds or [])
        # Which feed (if any) supplies this strategy's spot price. None keeps
        # the base-class default ("no spot underlying") for the many tests
        # that don't care.
        self.spot_feed_name = spot_feed_name
        self._vol_model = vol_model
        self._evaluate_fn = evaluate_fn
        self._expiries = dict(expiries or {})  # ticker -> expiry_ts (for re-discovery pruning tests)
        self._single_binary = single_binary
        self.evaluate_calls: list[dict] = []
        self.ingested_metadata: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    def expiry_ts(self, ticker):
        return self._expiries.get(ticker)

    def is_single_binary(self, ticker):
        return self._single_binary

    def ingest_market_metadata(self, ticker, metadata):
        self.ingested_metadata.append((ticker, dict(metadata)))

    def vol_model_functions(self):
        return self._vol_model

    def evaluate(self, book, *, btc_price=None, btc_vol=None, bankroll, current_time=None):
        self.evaluate_calls.append({
            "ticker": book.ticker, "btc_price": btc_price, "btc_vol": btc_vol,
            "bankroll": bankroll, "current_time": current_time,
        })
        if self._evaluate_fn is not None:
            return self._evaluate_fn(book, btc_price, btc_vol, bankroll, current_time)
        return []


def _fake_gate(level="backtest", paused=False) -> StrategyGate:
    return StrategyGate(strategy="x", level=level, paused=paused, backtest_pnl_summary=None, notes="", updated_at=0.0)


@pytest.fixture()
def chdir_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def live_env(monkeypatch):
    """
    A process environment that explicitly declares live mode.

    kbot.startup_guard rejects a resolved dry_run=False that has no raw
    DRY_RUN behind it, so a live-mode Runner test has to say so in the
    environment as well as in its Settings -- which is exactly what a live
    deployment does.
    """
    monkeypatch.setenv("DRY_RUN", "false")


# ── Ticker discovery / metadata ingestion (2026-07: KXBTC15M floor_strike) ──

def test_discover_tickers_feeds_market_metadata_to_owning_strategy(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["KXBTC15M"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    market = {"ticker": "KXBTC15M-26JUL060115-15", "floor_strike": 63202.05}
    mocker.patch.object(runner._client, "get_events", return_value=[
        {"close_time": "2026-07-06T05:15:00Z", "markets": [market]}
    ])

    tickers = runner._discover_tickers()

    assert tickers == ["KXBTC15M-26JUL060115-15"]
    assert a.ingested_metadata == [("KXBTC15M-26JUL060115-15", market)]
    runner.close()


def test_discover_tickers_does_not_feed_metadata_to_non_owning_strategy(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    b = _FakeStrategy("b", series_tickers=["SERIESB"])
    runner = Runner({"a": a, "b": b}, {"a": 0.5, "b": 0.5}, _cfg())

    def fake_get_events(series_ticker=None, **kw):
        return [{"close_time": "2026-07-06T05:15:00Z", "markets": [
            {"ticker": f"{series_ticker}-1", "floor_strike": 100.0},
        ]}]

    mocker.patch.object(runner._client, "get_events", side_effect=fake_get_events)
    runner._discover_tickers()

    assert [t for t, _ in a.ingested_metadata] == ["SERIESA-1"]
    assert [t for t, _ in b.ingested_metadata] == ["SERIESB-1"]
    runner.close()


def test_discover_tickers_handles_events_lookup_failure(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    mocker.patch.object(runner._client, "get_events", side_effect=RuntimeError("boom"))

    tickers = runner._discover_tickers()  # must not raise

    assert tickers == []
    assert a.ingested_metadata == []
    runner.close()


def test_discover_tickers_returns_all_open_events_not_just_nearest(chdir_tmp, mocker):
    # 2026-07: continuous coverage of a fast-rolling series needs EVERY open
    # window (current + just-opened next), not only the nearest-close event.
    a = _FakeStrategy("a", series_tickers=["KXBTC15M"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    mocker.patch.object(runner._client, "get_events", return_value=[
        {"close_time": "2026-07-06T05:15:00Z", "markets": [{"ticker": "KXBTC15M-A"}]},
        {"close_time": "2026-07-06T05:30:00Z", "markets": [{"ticker": "KXBTC15M-B"}]},
    ])
    assert runner._discover_tickers() == ["KXBTC15M-A", "KXBTC15M-B"]
    runner.close()


# ── Driver-exposure release + startup reconstruction (2026-07) ─────────────────

def test_expire_market_releases_driver_exposure(chdir_tmp, mocker):
    from kbot.models.types import Side
    a = _FakeStrategy("a", series_tickers=["KX15"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    mocker.patch.object(runner._client, "get_market", return_value={})  # no network / no outcome
    # Simulate a fill's exposure + guard entry.
    runner._portfolio_risk.update_exposure("KX15-1", "crypto_price", 96, 0.36, action="open")
    runner._tracker.open_position("KX15-1", Side.YES, contracts_units=9600, price=0.36, driver="crypto_price")
    assert runner._portfolio_risk.exposure_summary()["crypto_price"] > 0

    runner.expire_market("KX15-1")

    assert runner._portfolio_risk.exposure_summary().get("crypto_price", 0.0) == pytest.approx(0.0)
    assert not runner._tracker.is_held("KX15-1", Side.YES)
    runner.close()


def test_expire_market_records_trade_outcome(chdir_tmp, mocker):
    from kbot.models.types import Side
    a = _FakeStrategy("a", series_tickers=["KXBTC15M"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    # A held YES position on a market that settled YES -> a winning trade_outcome.
    runner._tracker.open_position("KXBTC15M-X", Side.YES, contracts_units=9600, price=0.36, driver="crypto_price")
    mocker.patch.object(runner._client, "get_market", return_value={"result": "yes", "expiration_value": "63962.01"})

    runner.expire_market("KXBTC15M-X")

    trades = runner._telemetry.resolved_paper_trades("a")  # strategy name is the dict key "a"
    assert len(trades) == 1
    assert trades[0]["won"] is True
    assert trades[0]["ticker"] == "KXBTC15M-X"
    assert trades[0]["pnl_dollars"] == trades[0]["net_pnl"]
    runner.close()


def test_expire_market_carries_fair_p_from_position_open_into_outcome(chdir_tmp, mocker):
    # End-to-end: a fill logs a position_open WITH fair_p; on settlement, the
    # recorded trade_outcome carries that fair_p forward (so learn --evaluate
    # can bucket it). This is the full path that a restarted live bot exercises.
    from kbot.models.types import Side
    a = _FakeStrategy("a", series_tickers=["KXBTC15M"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._tracker.open_position("KXBTC15M-X", Side.YES, contracts_units=9600, price=0.36, driver="crypto_price")
    runner._telemetry.log_position_open(
        "KXBTC15M-X", "yes", {"strategy": "a", "contracts": 96, "price": 0.36, "fair_p": 0.58},
    )
    mocker.patch.object(runner._client, "get_market", return_value={"result": "yes"})

    runner.expire_market("KXBTC15M-X")

    outcome = runner._telemetry.resolved_paper_trades("a")[0]
    assert outcome["fair_p"] == pytest.approx(0.58)
    assert outcome["model_prob"] == pytest.approx(0.58)  # what learn --evaluate buckets on
    runner.close()


def test_expire_market_survives_get_market_failure(chdir_tmp, mocker):
    # A settlement-fetch error must never crash the expiry/trading path.
    from kbot.models.types import Side
    a = _FakeStrategy("a", series_tickers=["KX15"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._tracker.open_position("KX15-1", Side.YES, contracts_units=1000, price=0.5, driver="crypto_price")
    mocker.patch.object(runner._client, "get_market", side_effect=RuntimeError("api down"))

    runner.expire_market("KX15-1")  # must not raise

    assert not runner._tracker.is_held("KX15-1", Side.YES)  # still cleared
    runner.close()


def test_runner_reconstructs_exposure_from_positions_on_startup(chdir_tmp):
    import time
    from kbot.models.types import Side
    from kbot.position_tracker import PositionTracker

    # Seed the positions DB (in cwd) with an open position carrying exposure.
    seed = PositionTracker(db_path="kbot_positions.db")
    seed.open_position("KX15-LIVE", Side.YES, contracts_units=10000, price=0.50, driver="crypto_price")
    seed.close()

    # A FUTURE expiry marks it as genuinely live -> reconstructed.
    a = _FakeStrategy("a", series_tickers=["KX15"], expiries={"KX15-LIVE": time.time() + 600.0})
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())

    # $50 (100 * 0.50) must be reconstructed into PortfolioRisk on startup.
    assert runner._portfolio_risk.exposure_summary()["crypto_price"] == pytest.approx(50.0)
    runner.close()


def test_runner_does_not_reconstruct_expired_position_exposure(chdir_tmp):
    # Phantom-exposure guard: a row that reaches reconstruction (no expiry_provider
    # sweep here) but whose window has PAST expiry must NOT be reconstructed.
    import time
    from kbot.models.types import Side
    from kbot.position_tracker import PositionTracker

    seed = PositionTracker(db_path="kbot_positions.db")
    seed.open_position("KX15-OLD", Side.YES, contracts_units=10000, price=0.50, driver="crypto_price")
    seed.close()

    a = _FakeStrategy("a", series_tickers=["KX15"], expiries={"KX15-OLD": time.time() - 3600.0})
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())

    # Expired -> phantom exposure NOT rebuilt.
    assert runner._portfolio_risk.exposure_summary().get("crypto_price", 0.0) == pytest.approx(0.0)
    runner.close()


def test_runner_does_not_reconstruct_unknown_expiry_orphan_exposure(chdir_tmp):
    # An orphan ticker no loaded strategy can resolve (_ticker_expiry -> None,
    # e.g. leftover from a retired strategy) must NOT get phantom exposure --
    # nothing would ever release it. It survives the sweep (don't guess) but
    # its exposure is not reconstructed.
    from kbot.models.types import Side
    from kbot.position_tracker import PositionTracker

    seed = PositionTracker(db_path="kbot_positions.db")
    seed.open_position("KXOTHER-ORPHAN", Side.YES, contracts_units=10000, price=0.50, driver="crypto_price")
    seed.close()

    a = _FakeStrategy("a", series_tickers=["KX15"])  # does not own KXOTHER -> expiry None
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())

    assert runner._portfolio_risk.exposure_summary().get("crypto_price", 0.0) == pytest.approx(0.0)
    # The guard entry is still held (not expired on a guess), just no exposure.
    assert runner._tracker.is_held("KXOTHER-ORPHAN", Side.YES)
    runner.close()


def test_runner_reconstruction_skips_swept_stale_positions(chdir_tmp):
    import time
    from kbot.models.types import Side
    from kbot.position_tracker import PositionTracker

    settled = "KXBTC15M-26JUL060115-15"
    seed = PositionTracker(db_path="kbot_positions.db")
    seed.open_position(settled, Side.YES, contracts_units=10000, price=0.50, driver="crypto_price")
    seed.close()

    # Strategy reports the ticker already expired -> stale sweep drops it BEFORE
    # reconstruction, so no exposure is rebuilt for it.
    a = _FakeStrategy("a", series_tickers=["KXBTC15M"], expiries={settled: time.time() - 3600.0})
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())

    assert runner._portfolio_risk.exposure_summary().get("crypto_price", 0.0) == pytest.approx(0.0)
    runner.close()


# ── Startup stale-position cleanup (2026-07) ───────────────────────────────────

def test_runner_expires_stale_positions_on_construction(chdir_tmp):
    import time
    from kbot.position_tracker import PositionTracker
    from kbot.models.types import Side

    # Pre-seed the positions DB (in chdir_tmp cwd) with a settled + a live pos.
    settled = "KXBTC15M-26JUL060115-15"  # a real parseable 15m ticker
    seed = PositionTracker(db_path="kbot_positions.db")
    seed.open_position(settled, Side.YES)
    seed.open_position("MYSTERY-TICKER", Side.NO)  # unparseable -> unknown expiry
    seed.close()

    a = _FakeStrategy("a", series_tickers=["KXBTC15M"], expiries={settled: time.time() - 3600.0})
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())  # constructs the tracker with expiry_provider

    assert not runner._tracker.is_held(settled, Side.YES)       # settled -> swept
    assert runner._tracker.is_held("MYSTERY-TICKER", Side.NO)   # unknown expiry -> kept
    assert (settled, "yes") in runner._tracker.expired_on_load
    runner.close()


# ── Periodic ticker re-discovery (2026-07) ─────────────────────────────────────

class _FakeWSClient:
    """Records subscribe/prune calls; subscribe_additional 'adds' everything asked."""

    def __init__(self):
        self.subscribed: list[list[str]] = []
        self.pruned: list[list[str]] = []

    async def subscribe_additional(self, tickers):
        self.subscribed.append(list(tickers))
        return list(tickers)

    def prune_tickers(self, tickers):
        self.pruned.append(list(tickers))
        return list(tickers)


@pytest.mark.asyncio
async def test_rediscover_once_subscribes_new_tickers_without_reconnect(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["KX15"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    fake_ws = _FakeWSClient()
    runner._ws_client = fake_ws
    runner._active_tickers = {"KX15-1"}
    mocker.patch.object(runner, "_discover_tickers", return_value=["KX15-1", "KX15-2", "KX15-3"])

    await runner._rediscover_once()

    assert fake_ws.subscribed == [["KX15-2", "KX15-3"]]  # only the genuinely-new ones
    assert fake_ws.pruned == []
    assert runner._active_tickers == {"KX15-1", "KX15-2", "KX15-3"}
    runner.close()


@pytest.mark.asyncio
async def test_rediscover_once_prunes_expired_tickers(chdir_tmp, mocker):
    now = time.time()
    a = _FakeStrategy(
        "a", series_tickers=["KX15"],
        expiries={"KX15-old": now - 60, "KX15-live": now + 600},
    )
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    fake_ws = _FakeWSClient()
    runner._ws_client = fake_ws
    runner._active_tickers = {"KX15-old", "KX15-live"}
    runner._books["KX15-old"] = _book("KX15-old")
    runner._books["KX15-live"] = _book("KX15-live")
    # Discovery still returns the live one only (expired window is gone).
    mocker.patch.object(runner, "_discover_tickers", return_value=["KX15-live"])

    await runner._rediscover_once()

    assert fake_ws.pruned == [["KX15-old"]]
    assert "KX15-old" not in runner._active_tickers
    assert "KX15-old" not in runner._books          # dropped so it stops being evaluated
    assert "KX15-live" in runner._active_tickers      # not-yet-expired kept
    assert "KX15-live" in runner._books
    runner.close()


@pytest.mark.asyncio
async def test_rediscover_once_keeps_ticker_with_unknown_expiry(chdir_tmp, mocker):
    # A None expiry (unparseable/unowned) must NOT be pruned on a guess.
    a = _FakeStrategy("a", series_tickers=["KX15"], expiries={})  # expiry_ts -> None
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._ws_client = _FakeWSClient()
    runner._active_tickers = {"KX15-1"}
    mocker.patch.object(runner, "_discover_tickers", return_value=["KX15-1"])

    await runner._rediscover_once()

    assert runner._active_tickers == {"KX15-1"}
    assert runner._ws_client.pruned == []
    runner.close()


@pytest.mark.asyncio
async def test_rediscover_once_noop_when_no_ws_client(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["KX15"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._ws_client = None  # not connected yet
    spy = mocker.patch.object(runner, "_discover_tickers")
    await runner._rediscover_once()  # must not raise, must not discover
    spy.assert_not_called()
    runner.close()


@pytest.mark.asyncio
async def test_rediscovery_loop_calls_rediscover_once_each_interval(chdir_tmp, mocker):
    import asyncio
    a = _FakeStrategy("a", series_tickers=["KX15"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    mocker.patch("kbot.runner.asyncio.sleep", return_value=None)  # don't actually wait

    calls = []
    async def fake_once():
        calls.append(1)
        if len(calls) >= 3:
            raise asyncio.CancelledError
    mocker.patch.object(runner, "_rediscover_once", side_effect=fake_once)

    with pytest.raises(asyncio.CancelledError):
        await runner._rediscovery_loop()
    assert len(calls) == 3  # ran on schedule, repeatedly
    runner.close()


@pytest.mark.asyncio
async def test_rediscovery_loop_survives_a_failing_pass(chdir_tmp, mocker):
    import asyncio
    a = _FakeStrategy("a", series_tickers=["KX15"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    mocker.patch("kbot.runner.asyncio.sleep", return_value=None)

    calls = []
    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("discovery hiccup")  # must be caught, loop continues
        if len(calls) >= 2:
            raise asyncio.CancelledError
    mocker.patch.object(runner, "_rediscover_once", side_effect=flaky)

    with pytest.raises(asyncio.CancelledError):
        await runner._rediscovery_loop()
    assert len(calls) == 2  # the RuntimeError on pass 1 did not kill the loop
    runner.close()


# ── Multiplex routing ─────────────────────────────────────────────────────────

def test_run_cycle_routes_ticker_only_to_owning_strategy(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    b = _FakeStrategy("b", series_tickers=["SERIESB"])
    runner = Runner({"a": a, "b": b}, {"a": 0.5, "b": 0.5}, _cfg())
    runner._bankroll = 1000.0
    runner._books["SERIESA-1"] = _book("SERIESA-1")
    runner._books["SERIESB-1"] = _book("SERIESB-1")

    runner._run_cycle_once()

    a_tickers = {c["ticker"] for c in a.evaluate_calls}
    b_tickers = {c["ticker"] for c in b.evaluate_calls}
    assert a_tickers == {"SERIESA-1"}
    assert b_tickers == {"SERIESB-1"}
    runner.close()


def test_run_cycle_ticker_matching_no_strategy_is_not_evaluated(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._bankroll = 1000.0
    runner._books["UNRELATED-1"] = _book("UNRELATED-1")

    runner._run_cycle_once()

    assert a.evaluate_calls == []
    runner.close()


def test_owns_ticker_prefix_match_is_exact_not_substring(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["KXCPI"])
    assert a.owns_ticker("KXCPI-26JUN-T0.2") is True
    assert a.owns_ticker("KXCPICORE-26JUN-T0.2") is False  # must not match as a bare substring


# ── Shared feed singleton ─────────────────────────────────────────────────────

def test_two_strategies_sharing_btc_spot_get_one_feed_instance(chdir_tmp, mocker):
    build_spy = mocker.patch("kbot.runner._build_feed", wraps=__import__("kbot.runner", fromlist=["_build_feed"])._build_feed)
    a = _FakeStrategy("a", series_tickers=["KXBTC"], required_feeds=["btc_spot"])
    b = _FakeStrategy("b", series_tickers=["KXBTC"], required_feeds=["btc_spot"])
    runner = Runner({"a": a, "b": b}, {"a": 0.5, "b": 0.5}, _cfg())

    assert build_spy.call_count == 1  # only built once despite two strategies declaring it
    assert "btc_spot" in runner._feeds
    feed_via_a, _ = runner._feed_inputs_for(a, "KXBTC-1")
    # Same underlying BTCFeed object serves both -- identity check via the feeds dict.
    assert runner._feeds["btc_spot"] is runner._feeds["btc_spot"]
    runner.close()


def test_strategy_with_no_required_feeds_gets_no_feed(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["KXCPI"], required_feeds=[])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    assert runner._feeds == {}
    price, vol = runner._feed_inputs_for(a, "KXCPI-1")
    assert price is None and vol is None
    runner.close()


def test_unknown_required_feed_raises_at_construction(chdir_tmp):
    a = _FakeStrategy("a", required_feeds=["nonexistent_feed"])
    with pytest.raises(ValueError, match="Unknown required feed"):
        Runner({"a": a}, {"a": 1.0}, _cfg())


# ── Per-strategy allocation sizing ────────────────────────────────────────────

# ── _resolve_bankroll: PAPER_BANKROLL_OVERRIDE (2026-07) ───────────────────────

def test_resolve_bankroll_uses_real_balance_when_no_override(chdir_tmp, mocker):
    # get_balance reports CENTS -- 1200c is $12.00. This test was left asserting
    # the pre-conversion value when the /100 fix landed (589013e) and had been
    # failing since; the behaviour it now pins is the shipped one.
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())  # dry_run, no override
    mocker.patch.object(runner._client, "get_balance", return_value={"balance": 1200.0})
    assert runner._resolve_bankroll() == pytest.approx(12.0)
    runner.close()


def test_resolve_bankroll_override_replaces_real_balance_in_paper_mode(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(paper_bankroll_override=5000.0))
    bal_spy = mocker.patch.object(runner._client, "get_balance", return_value={"balance": 12.0})

    assert runner._resolve_bankroll() == pytest.approx(5000.0)  # override wins
    bal_spy.assert_not_called()  # real balance isn't even queried
    runner.close()


def test_resolve_bankroll_override_ignored_in_live_mode(chdir_tmp, mocker):
    # The safety-critical guarantee: override NEVER applies to real capital.
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(paper_bankroll_override=5000.0))
    mocker.patch.object(runner._client, "get_balance", return_value={"balance": 1200.0})
    # Swap in a live cfg (dry_run False) for the resolve call only -- constructing
    # a live Runner needs live_confirmed + creds; the branch reads self._cfg.
    runner._cfg = _cfg(dry_run=False, paper_bankroll_override=5000.0)

    assert runner._resolve_bankroll() == pytest.approx(12.0)  # real balance (cents), override ignored
    runner.close()


def test_resolve_bankroll_falls_back_when_balance_unavailable(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())  # no override
    mocker.patch.object(runner._client, "get_balance", side_effect=RuntimeError("no api"))
    assert runner._resolve_bankroll() == pytest.approx(1000.0)  # testing fallback
    runner.close()


def test_evaluate_ticker_scales_bankroll_by_allocation(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 0.25}, _cfg())
    runner._bankroll = 2000.0

    runner._evaluate_ticker("a", a, "SERIESA-1", _book("SERIESA-1"), None, None)

    assert len(a.evaluate_calls) == 1
    assert a.evaluate_calls[0]["bankroll"] == pytest.approx(500.0)  # 0.25 * 2000
    runner.close()


def test_two_strategies_get_independently_scaled_bankrolls(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    b = _FakeStrategy("b", series_tickers=["SERIESB"])
    runner = Runner({"a": a, "b": b}, {"a": 0.7, "b": 0.3}, _cfg())
    runner._bankroll = 1000.0
    runner._books["SERIESA-1"] = _book("SERIESA-1")
    runner._books["SERIESB-1"] = _book("SERIESB-1")

    runner._run_cycle_once()

    assert a.evaluate_calls[0]["bankroll"] == pytest.approx(700.0)
    assert b.evaluate_calls[0]["bankroll"] == pytest.approx(300.0)
    runner.close()


def test_allocations_must_match_strategies_keys(chdir_tmp):
    a = _FakeStrategy("a")
    with pytest.raises(ValueError, match="allocations"):
        Runner({"a": a}, {"a": 0.5, "b": 0.5}, _cfg())
    with pytest.raises(ValueError, match="allocations"):
        Runner({"a": a}, {}, _cfg())


def test_driver_cap_uses_full_bankroll_not_allocation_scaled(chdir_tmp):
    # PortfolioRisk.apply_driver_cap is fed via Executor's bankroll_provider,
    # which the Runner wires to the FULL (unscaled) bankroll regardless of
    # any strategy's allocation -- driver caps are portfolio-level by design.
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 0.1}, _cfg())
    runner._bankroll = 5000.0

    assert runner._executor.bankroll_provider() == pytest.approx(5000.0)
    runner.close()


# ── Exception isolation ───────────────────────────────────────────────────────

def test_evaluate_exception_is_caught_and_does_not_propagate(chdir_tmp):
    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=_boom)
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._bankroll = 1000.0

    result = runner._safe_evaluate("a", a, _book("SERIESA-1"), None, None, 1000.0)
    assert result == []
    runner.close()


def test_other_strategy_keeps_running_when_one_raises(chdir_tmp):
    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    broken = _FakeStrategy("broken", series_tickers=["SERIESA"], evaluate_fn=_boom)
    healthy = _FakeStrategy("healthy", series_tickers=["SERIESB"])
    runner = Runner({"broken": broken, "healthy": healthy}, {"broken": 0.5, "healthy": 0.5}, _cfg())
    runner._bankroll = 1000.0
    runner._books["SERIESA-1"] = _book("SERIESA-1")
    runner._books["SERIESB-1"] = _book("SERIESB-1")

    runner._run_cycle_once()  # must not raise

    assert len(healthy.evaluate_calls) == 1
    runner.close()


def test_three_failures_in_window_triggers_advisory_pause(chdir_tmp, mocker):
    mocker.patch("kbot.runner.Runner._send_alert")  # avoid real alert dispatch
    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=_boom)
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())

    for _ in range(3):
        runner._safe_evaluate("a", a, _book("SERIESA-1"), None, None, 1000.0)

    assert "a" in runner._advisory_paused
    runner.close()


def test_advisory_paused_strategy_stops_calling_evaluate(chdir_tmp, mocker):
    mocker.patch("kbot.runner.Runner._send_alert")
    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=_boom)
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    for _ in range(3):
        runner._safe_evaluate("a", a, _book("SERIESA-1"), None, None, 1000.0)
    assert len(a.evaluate_calls) == 3  # each failing call still reached evaluate()

    # A 4th call, now advisory-paused, must skip calling evaluate() entirely.
    runner._safe_evaluate("a", a, _book("SERIESA-1"), None, None, 1000.0)
    assert len(a.evaluate_calls) == 3
    runner.close()


def test_two_failures_does_not_trigger_pause(chdir_tmp):
    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=_boom)
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    for _ in range(2):
        runner._safe_evaluate("a", a, _book("SERIESA-1"), None, None, 1000.0)

    assert "a" not in runner._advisory_paused
    runner.close()


def test_failures_outside_window_do_not_accumulate(chdir_tmp, mocker):
    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=_boom)
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())

    now = time.time()
    times = iter([now - 1000, now - 900, now])  # first two are outside the 300s window by the 3rd call
    mocker.patch("kbot.runner.time.time", side_effect=lambda: next(times))
    for _ in range(3):
        runner._safe_evaluate("a", a, _book("SERIESA-1"), None, None, 1000.0)

    assert "a" not in runner._advisory_paused
    runner.close()


# ── Kill-fast isolation ───────────────────────────────────────────────────────

def test_kill_fast_checked_independently_per_strategy(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    b = _FakeStrategy("b", series_tickers=["SERIESB"])
    runner = Runner({"a": a, "b": b}, {"a": 0.5, "b": 0.5}, _cfg())
    runner._bankroll = 1000.0

    kf_spy = mocker.patch("kbot.runner.check_kill_fast")
    runner._run_cycle_once()

    called_names = {call.args[0] for call in kf_spy.call_args_list}
    assert called_names == {"a", "b"}
    runner.close()


def test_one_strategy_paused_via_gate_does_not_block_the_others_evaluate(chdir_tmp):
    # Gate pause affects whether a SIGNAL executes (see gate-enforcement
    # tests below), not whether evaluate() is called at all -- a paused
    # strategy keeps being evaluated (that's how forward evidence for
    # re-promotion accumulates) and other strategies are wholly unaffected.
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    b = _FakeStrategy("b", series_tickers=["SERIESB"])
    runner = Runner({"a": a, "b": b}, {"a": 0.5, "b": 0.5}, _cfg())
    runner._bankroll = 1000.0
    with GateRegistry() as reg:
        reg.demote("a", "test_pause")

    runner._books["SERIESA-1"] = _book("SERIESA-1")
    runner._books["SERIESB-1"] = _book("SERIESB-1")
    runner._run_cycle_once()

    assert len(a.evaluate_calls) == 1
    assert len(b.evaluate_calls) == 1
    runner.close()


# ── Gate enforcement per strategy ─────────────────────────────────────────────

def test_gate_check_paper_mode_paused_blocked_by_default(chdir_tmp, mocker):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=True, paused_allows_dry_run=False))
    mocker.patch.object(runner._gates, "get_gate", return_value=_fake_gate(paused=True))
    assert runner._gate_check("a") == "gate_paused"
    runner.close()


def test_gate_check_paper_mode_paused_allowed_when_configured(chdir_tmp, mocker):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=True, paused_allows_dry_run=True))
    mocker.patch.object(runner._gates, "get_gate", return_value=_fake_gate(paused=True))
    assert runner._gate_check("a") is None
    runner.close()


def test_gate_check_paper_mode_not_paused_always_allowed(chdir_tmp, mocker):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=True))
    mocker.patch.object(runner._gates, "get_gate", return_value=_fake_gate(paused=False))
    assert runner._gate_check("a") is None
    runner.close()


def test_gate_check_paper_mode_unregistered_strategy_allowed(chdir_tmp):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=True))
    assert runner._gate_check("a") is None  # get_gate returns None -> not paused
    runner.close()


def test_gate_check_live_mode_blocks_unapproved_level(chdir_tmp, live_env, mocker):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=False), live_confirmed=True)
    mocker.patch.object(runner._gates, "get_gate", return_value=_fake_gate(level="paper", paused=False))
    assert runner._gate_check("a") == "not_live_approved"
    runner.close()


def test_gate_check_live_mode_allows_live_small(chdir_tmp, live_env, mocker):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=False), live_confirmed=True)
    mocker.patch.object(runner._gates, "get_gate", return_value=_fake_gate(level="live_small", paused=False))
    assert runner._gate_check("a") is None
    runner.close()


def test_gate_check_live_mode_blocks_even_if_approved_but_paused(chdir_tmp, live_env, mocker):
    # paused_allows_dry_run must NEVER let a paused strategy trade live.
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=False, paused_allows_dry_run=True), live_confirmed=True)
    mocker.patch.object(runner._gates, "get_gate", return_value=_fake_gate(level="live_full", paused=True))
    assert runner._gate_check("a") == "gate_paused"
    runner.close()


def test_gate_check_live_mode_unregistered_strategy_blocked(chdir_tmp, live_env):
    # An unregistered strategy defaults to LEVELS[0]="backtest" -- never live-approved.
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=False), live_confirmed=True)
    assert runner._gate_check("a") == "not_live_approved"
    runner.close()


def test_runner_requires_dry_run_or_live_confirmed(chdir_tmp, live_env):
    a = _FakeStrategy("a")
    # Matched on "live_confirmed", not "DRY_RUN": the startup guard's own
    # refusals also say DRY_RUN, and this test is about Runner's gauntlet.
    with pytest.raises(RuntimeError, match="live_confirmed"):
        Runner({"a": a}, {"a": 1.0}, _cfg(dry_run=False))  # live_confirmed defaults False


def test_evaluate_ticker_execute_blocked_when_gate_paused_and_disallowed(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=lambda *a, **kw: [_signal("SERIESA-1")])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg(paused_allows_dry_run=False))
    runner._bankroll = 1000.0
    mocker.patch.object(runner._gates, "get_gate", return_value=_fake_gate(paused=True))
    execute_spy = mocker.patch.object(runner._executor, "execute", return_value=None)

    runner._evaluate_ticker("a", a, "SERIESA-1", _book("SERIESA-1"), None, None)

    execute_spy.assert_not_called()
    runner.close()


def test_evaluate_ticker_executes_when_not_paused(chdir_tmp, mocker):
    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=lambda *a, **kw: [_signal("SERIESA-1")])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._bankroll = 1000.0
    execute_spy = mocker.patch.object(runner._executor, "execute", return_value=None)

    runner._evaluate_ticker("a", a, "SERIESA-1", _book("SERIESA-1"), None, None)

    execute_spy.assert_called_once()
    _, kwargs = execute_spy.call_args
    assert kwargs["strategy"] == "a"
    runner.close()


def test_position_open_event_includes_fair_p(chdir_tmp, mocker):
    from kbot.kalshi_client import OrderAck
    sig = _signal("SERIESA-1")  # _signal sets fair_p=0.6
    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=lambda *a, **kw: [sig])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._bankroll = 1000.0
    mocker.patch.object(
        runner._executor, "execute",
        return_value=OrderAck(client_order_id="c", order_id="o", dry_run=True,
                              fill_count_units=100, remaining_count_units=0, average_fill_price=0.5),
    )

    runner._evaluate_ticker("a", a, "SERIESA-1", _book("SERIESA-1"), None, None)

    fp = runner._telemetry.position_open_fair_p("SERIESA-1", "yes")
    assert fp == pytest.approx(0.6)  # carried from the SizingResult
    runner.close()


def test_evaluate_ticker_passes_single_binary_flag_to_execute(chdir_tmp, mocker):
    a = _FakeStrategy(
        "a", series_tickers=["SERIESA"], single_binary=True,
        evaluate_fn=lambda *a, **kw: [_signal("SERIESA-1")],
    )
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._bankroll = 1000.0
    execute_spy = mocker.patch.object(runner._executor, "execute", return_value=None)

    runner._evaluate_ticker("a", a, "SERIESA-1", _book("SERIESA-1"), None, None)

    _, kwargs = execute_spy.call_args
    assert kwargs["is_single_binary"] is True


def test_evaluate_ticker_single_binary_false_for_ladder_strategy(chdir_tmp, mocker):
    a = _FakeStrategy(
        "a", series_tickers=["SERIESA"], single_binary=False,
        evaluate_fn=lambda *a, **kw: [_signal("SERIESA-1")],
    )
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._bankroll = 1000.0
    execute_spy = mocker.patch.object(runner._executor, "execute", return_value=None)

    runner._evaluate_ticker("a", a, "SERIESA-1", _book("SERIESA-1"), None, None)

    _, kwargs = execute_spy.call_args
    assert kwargs["is_single_binary"] is False


# ── Heartbeat schema ──────────────────────────────────────────────────────────

def test_write_heartbeat_has_per_strategy_sections(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    b = _FakeStrategy("b", series_tickers=["SERIESB"])
    runner = Runner({"a": a, "b": b}, {"a": 0.6, "b": 0.4}, _cfg())
    runner._bankroll = 1234.0
    runner._eval_cycles["a"] = 5
    runner._session_signals["b"] = 2

    runner._write_heartbeat()

    from pathlib import Path
    import json
    payload = json.loads(Path("kbot_heartbeat.json").read_text())
    assert set(payload["strategies"]) == {"a", "b"}
    assert payload["strategies"]["a"]["allocation"] == pytest.approx(0.6)
    assert payload["strategies"]["a"]["eval_cycles"] == 5
    assert payload["strategies"]["b"]["session_signals"] == 2
    assert payload["strategies"]["a"]["gate_level"] == "backtest"  # unregistered -> LEVELS[0]
    assert payload["strategies"]["a"]["paused"] is False
    assert payload["bankroll"] == pytest.approx(1234.0)
    assert "driver_exposure" in payload
    runner.close()


def test_runner_startup_restores_adverse_selection_flags(chdir_tmp):
    """
    A restarted Runner must pick the flag back up from the persisted markouts,
    so effective_min_net_edge is multiplied from the first evaluation rather
    than only after the next fill's markout lands.
    """
    from kbot.analytics import adverse_selection as adv

    adv.clear_all_flags()
    try:
        cfg = _cfg()
        # Seed a bad markout history into the same events DB the Runner opens.
        seed = Telemetry(cfg)
        for i in range(cfg.adverse_selection_window):
            seed.log_markout({
                "fill_event_id": f"seed-{i}", "ticker": "KX-1", "strategy": "a",
                "side": "yes", "fill_price": 0.50, "mid_60s": 0.4463,
                "markout_60s": -0.0537,
            })
        seed.close()
        assert adv.is_flagged("a") is False  # fresh process: nothing in memory

        runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0}, cfg)

        assert adv.is_flagged("a") is True
        runner.close()
    finally:
        adv.clear_all_flags()


def _age_heartbeat(seconds: float) -> None:
    """Write kbot_heartbeat.json with an mtime `seconds` in the past."""
    import os
    from pathlib import Path
    path = Path("kbot_heartbeat.json")
    path.write_text("{}")
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_deadman_exits_when_heartbeat_file_is_stale(chdir_tmp, mocker):
    exit_spy = mocker.patch("kbot.runner.os._exit")
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0}, _cfg())
    _age_heartbeat(1200.0)  # > 900s threshold

    # Loop started 2h ago, so the startup baseline can't mask the staleness.
    runner._check_heartbeat_freshness(started=time.time() - 7200.0)

    exit_spy.assert_called_once_with(1)
    runner.close()


def test_deadman_does_not_exit_when_heartbeat_is_fresh(chdir_tmp, mocker):
    exit_spy = mocker.patch("kbot.runner.os._exit")
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0}, _cfg())
    _age_heartbeat(30.0)

    runner._check_heartbeat_freshness(started=time.time() - 7200.0)

    exit_spy.assert_not_called()
    runner.close()


def test_deadman_does_not_exit_on_stale_file_from_a_previous_run(chdir_tmp, mocker):
    """
    The anti-crash-loop guard. kbot_heartbeat.json outlives the process that
    wrote it and a fresh run doesn't write its first one for ~5 minutes, so a
    just-started process inheriting an ancient file must NOT be killed -- doing
    so would make every restart after a freeze exit at 60s, forever.
    """
    exit_spy = mocker.patch("kbot.runner.os._exit")
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0}, _cfg())
    _age_heartbeat(25000.0)  # ~7h stale, left by the frozen run we just replaced

    runner._check_heartbeat_freshness(started=time.time() - 60.0)  # we booted 60s ago

    exit_spy.assert_not_called()
    runner.close()


def test_deadman_does_not_exit_when_heartbeat_file_is_missing_at_startup(chdir_tmp, mocker):
    exit_spy = mocker.patch("kbot.runner.os._exit")
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0}, _cfg())
    # No heartbeat file at all (first ever run) -- the `started` baseline governs.

    runner._check_heartbeat_freshness(started=time.time() - 60.0)

    exit_spy.assert_not_called()
    runner.close()


def test_deadman_exits_when_heartbeat_never_appears(chdir_tmp, mocker):
    """A loop that froze before its first heartbeat write is still a freeze."""
    exit_spy = mocker.patch("kbot.runner.os._exit")
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0}, _cfg())

    runner._check_heartbeat_freshness(started=time.time() - 1200.0)

    exit_spy.assert_called_once_with(1)
    runner.close()


def test_write_heartbeat_includes_active_tickers_count(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["KX15"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner._active_tickers = {"KX15-1", "KX15-2", "KX15-3"}

    runner._write_heartbeat()

    from pathlib import Path
    import json
    payload = json.loads(Path("kbot_heartbeat.json").read_text())
    assert payload["active_tickers_count"] == 3
    runner.close()


def test_write_heartbeat_reflects_advisory_pause(chdir_tmp, mocker):
    mocker.patch("kbot.runner.Runner._send_alert")
    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    a = _FakeStrategy("a", series_tickers=["SERIESA"], evaluate_fn=_boom)
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    for _ in range(3):
        runner._safe_evaluate("a", a, _book("SERIESA-1"), None, None, 1000.0)

    runner._write_heartbeat()

    from pathlib import Path
    import json
    payload = json.loads(Path("kbot_heartbeat.json").read_text())
    assert payload["strategies"]["a"]["advisory_paused"] is True
    runner.close()


def test_write_heartbeat_reflects_gate_level_and_pause(chdir_tmp):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    with GateRegistry() as reg:
        reg.register_backtest("a", [1.0, 2.0], notes="ok")
        reg.promote("a")  # backtest -> paper

    runner._write_heartbeat()

    from pathlib import Path
    import json
    payload = json.loads(Path("kbot_heartbeat.json").read_text())
    assert payload["strategies"]["a"]["gate_level"] == "paper"
    assert payload["strategies"]["a"]["paused"] is False
    runner.close()


# ── Post-resolution scanner wiring (2026-07) ──────────────────────────────────

def test_runner_constructs_post_resolution_scanner(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    assert runner._post_resolution is not None
    runner.close()


def test_runner_post_resolution_shares_books_reference(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    assert runner._post_resolution._books is runner._books
    runner.close()


def test_runner_post_resolution_shares_executor_and_telemetry(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    assert runner._post_resolution._executor is runner._executor
    assert runner._post_resolution._telemetry is runner._telemetry
    runner.close()


def test_runner_post_resolution_bankroll_provider_uses_allocation(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner = Runner({"a": a}, {"a": 0.4}, _cfg())
    runner._bankroll = 1000.0
    assert runner._post_resolution._bankroll_provider("a") == pytest.approx(400.0)
    runner.close()


# ── Series-aware vol window: no cross-contamination in the runner (2026-07) ──

def test_run_cycle_computes_vol_window_per_ticker_not_per_strategy(chdir_tmp, mocker):
    """
    The specific regression this exists for: ONE strategy instance trading
    BOTH KXBTC and KXBTC15M in the SAME cycle must get a DIFFERENT
    vol_window_seconds passed to _estimate_spot_vol per ticker -- proves
    vol is no longer computed once per strategy per cycle and reused
    across tickers with different contract horizons.
    """
    from kbot.strategy.btc_hourly import BTCHourlyStrategy

    cfg = _cfg()
    strategy = BTCHourlyStrategy(cfg)
    runner = Runner({"btc_hourly": strategy}, {"btc_hourly": 1.0}, cfg)
    runner._bankroll = 1000.0
    runner._feeds["btc_spot"]._history.append((time.time(), 65000.0))
    runner._books["KXBTC-26JUL0317-T48000"] = _book("KXBTC-26JUL0317-T48000")
    runner._books["KXBTC15M-26JUL060115-15"] = _book("KXBTC15M-26JUL060115-15")

    spy = mocker.spy(runner, "_estimate_spot_vol")
    runner._run_cycle_once()

    calls_by_ticker = {}
    for call in spy.call_args_list:
        # _feed_inputs_for is called once per (strategy, ticker); capture
        # the window_seconds kwarg passed through to _estimate_spot_vol.
        calls_by_ticker.setdefault("window_seconds", []).append(call.kwargs.get("window_seconds"))

    assert 3600.0 in calls_by_ticker["window_seconds"]
    assert 900.0 in calls_by_ticker["window_seconds"]
    runner.close()


def test_run_cycle_15m_ticker_gets_vol_multiplier_15m_override(chdir_tmp, mocker):
    from kbot.strategy.btc_hourly import BTCHourlyStrategy

    cfg = _cfg(vol_multiplier_15m=1.25)
    strategy = BTCHourlyStrategy(cfg)
    runner = Runner({"btc_hourly": strategy}, {"btc_hourly": 1.0}, cfg)
    runner._bankroll = 1000.0
    runner._feeds["btc_spot"]._history.append((time.time(), 65000.0))
    runner._books["KXBTC15M-26JUL060115-15"] = _book("KXBTC15M-26JUL060115-15")

    spy = mocker.spy(runner, "_estimate_spot_vol")
    runner._run_cycle_once()

    overrides = [call.kwargs.get("vol_multiplier_override") for call in spy.call_args_list]
    assert 1.25 in overrides


def test_run_cycle_hourly_ticker_gets_no_multiplier_override(chdir_tmp, mocker):
    from kbot.strategy.btc_hourly import BTCHourlyStrategy

    cfg = _cfg(vol_multiplier_15m=1.25)
    strategy = BTCHourlyStrategy(cfg)
    runner = Runner({"btc_hourly": strategy}, {"btc_hourly": 1.0}, cfg)
    runner._bankroll = 1000.0
    runner._feeds["btc_spot"]._history.append((time.time(), 65000.0))
    runner._books["KXBTC-26JUL0317-T48000"] = _book("KXBTC-26JUL0317-T48000")

    spy = mocker.spy(runner, "_estimate_spot_vol")
    runner._run_cycle_once()

    overrides = [call.kwargs.get("vol_multiplier_override") for call in spy.call_args_list]
    assert overrides == [None]


# ── Single-strategy regression (PaperTrader) ──────────────────────────────────

def test_papertrader_uses_allocation_of_one(chdir_tmp):
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    strategy = BTCHourlyStrategy(_cfg())
    pt = PaperTrader(strategy, [], _cfg())
    assert pt._runner._allocations == {"btc_hourly": 1.0}
    pt.close()


def test_papertrader_single_strategy_full_bankroll_passed_through(chdir_tmp, mocker):
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    strategy = BTCHourlyStrategy(_cfg())
    pt = PaperTrader(strategy, [], _cfg())
    pt._runner._bankroll = 850.0
    evaluate_spy = mocker.patch.object(pt._strategy, "evaluate", return_value=[])

    pt._evaluate_ticker("SERIESA-1", _book("KXBTC-1"), None, None)

    assert evaluate_spy.call_args.kwargs["bankroll"] == pytest.approx(850.0)  # 1.0 alloc -> full bankroll
    pt.close()


def test_papertrader_and_equivalent_single_strategy_runner_agree_on_sizing(chdir_tmp):
    from kbot.strategy.btc_hourly import BTCHourlyStrategy

    cfg = _cfg()
    strategy_for_pt = BTCHourlyStrategy(cfg)
    pt = PaperTrader(strategy_for_pt, [], cfg)
    pt._runner._bankroll = 1000.0

    strategy_for_runner = BTCHourlyStrategy(cfg)
    runner = Runner({"btc_hourly": strategy_for_runner}, {"btc_hourly": 1.0}, cfg)
    runner._bankroll = 1000.0

    ticker = "KXBTC-26JUL0317-T48000"
    book = _book(ticker, yes_ask=0.4, no_ask=0.55)

    pt_result = pt._strategy.evaluate(book, btc_price=48000.0, btc_vol=0.5, bankroll=1000.0)
    runner_result = strategy_for_runner.evaluate(book, btc_price=48000.0, btc_vol=0.5, bankroll=1000.0)

    assert [r.contracts for r in pt_result] == [r.contracts for r in runner_result]
    pt.close()
    runner.close()


# ── Position tracker persistence (2026-07: survive a restart) ────────────────

def test_runner_tracker_persists_across_restart(chdir_tmp):
    a = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner1 = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner1._tracker.open_position("SERIESA-1", Side.YES)
    runner1.close()  # simulate process exit

    # A brand-new Runner in the same working directory -- simulates a restart.
    a2 = _FakeStrategy("a", series_tickers=["SERIESA"])
    runner2 = Runner({"a": a2}, {"a": 1.0}, _cfg())
    assert runner2._tracker.is_held("SERIESA-1", Side.YES)
    runner2.close()


def test_runner_close_closes_tracker(chdir_tmp):
    a = _FakeStrategy("a")
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    runner.close()
    with pytest.raises(Exception):
        runner._tracker.open_position("TICK", Side.YES)  # connection closed -- must raise, not silently no-op


# ── Multi-underlying feed wiring (2026-07: KXETH15M) ─────────────────────────
#
# These are the regression tests for the failure mode the ETH build uncovered:
# the runner used to hardcode "btc_spot" in _feed_inputs_for, _on_spot_price
# and the vol estimator, so a strategy declaring any OTHER spot feed received
# (None, None) and skipped every evaluation SILENTLY -- no exception, no log,
# just a strategy that never trades. Nothing in the suite would have caught it.

def _eth_strategy():
    from kbot.strategy.eth_15m import ETH15MStrategy
    return ETH15MStrategy(_cfg())


def test_eth_spot_builds_an_eth_feed(chdir_tmp):
    from kbot.feeds.eth import ETHFeed
    s = _eth_strategy()
    runner = Runner({"kxeth15m": s}, {"kxeth15m": 1.0}, _cfg())
    assert isinstance(runner._feeds["eth_spot"], ETHFeed)
    assert "btc_spot" not in runner._feeds  # an ETH-only process polls only ETH
    runner.close()


def test_eth_strategy_actually_receives_price_and_vol_from_its_feed(chdir_tmp):
    """
    THE gotcha-#4 regression. Before the runner was generalised this returned
    (None, None) and ETH's evaluate() bailed on its first line forever.
    """
    s = _eth_strategy()
    runner = Runner({"kxeth15m": s}, {"kxeth15m": 1.0}, _cfg())
    runner._feeds["eth_spot"]._history.append((time.time(), 1860.71))

    price, vol = runner._feed_inputs_for(s, "KXETH15M-26JUL311800-00")
    assert price == pytest.approx(1860.71)
    assert vol is not None and vol > 0
    runner.close()


def test_eth_price_comes_from_the_eth_feed_not_a_btc_one(chdir_tmp):
    """
    A strategy must be priced off ITS OWN underlying. Handing ETH a BTC price
    would be silently, catastrophically wrong rather than an error.
    """
    from kbot.feeds.btc import BTCFeed
    s = _eth_strategy()
    runner = Runner({"kxeth15m": s}, {"kxeth15m": 1.0}, _cfg())
    runner._feeds["eth_spot"]._history.append((time.time(), 1860.71))
    # Plant a BTC feed alongside it, as a co-tenant process would have.
    btc = BTCFeed(poll_interval=2.0)
    btc._history.append((time.time(), 65000.0))
    runner._feeds["btc_spot"] = btc

    price, _vol = runner._feed_inputs_for(s, "KXETH15M-26JUL311800-00")
    assert price == pytest.approx(1860.71)
    runner.close()


def test_eth_evaluate_gets_the_eth_price_through_a_full_cycle(chdir_tmp):
    """End-to-end through _run_cycle_once, not just the helper in isolation."""
    s = _FakeStrategy("eth", series_tickers=["KXETH15M"], required_feeds=["eth_spot"],
                      spot_feed_name="eth_spot")
    runner = Runner({"eth": s}, {"eth": 1.0}, _cfg())
    runner._bankroll = 1000.0
    runner._feeds["eth_spot"]._history.append((time.time(), 1860.71))
    runner._books["KXETH15M-26JUL311800-00"] = _book("KXETH15M-26JUL311800-00")

    runner._run_cycle_once()

    assert s.evaluate_calls
    assert s.evaluate_calls[0]["btc_price"] == pytest.approx(1860.71)
    runner.close()


def test_spot_trigger_only_re_evaluates_strategies_on_that_feed(chdir_tmp):
    """An ETH tick must not fire a BTC strategy's off-cycle evaluation."""
    eth = _FakeStrategy("eth", series_tickers=["KXETH15M"], required_feeds=["eth_spot"],
                        spot_feed_name="eth_spot")
    btc = _FakeStrategy("btc", series_tickers=["KXBTC15M"], required_feeds=["btc_spot"],
                        spot_feed_name="btc_spot")
    runner = Runner({"eth": eth, "btc": btc}, {"eth": 0.5, "btc": 0.5}, _cfg())
    runner._bankroll = 1000.0
    runner._books["KXETH15M-26JUL311800-00"] = _book("KXETH15M-26JUL311800-00")
    runner._books["KXBTC15M-26JUL060115-15"] = _book("KXBTC15M-26JUL060115-15")

    # A +100bps ETH move, large enough to trip the trigger.
    now = time.time()
    runner._feeds["eth_spot"]._history.append((now - 1.0, 1800.0))
    runner._feeds["eth_spot"]._history.append((now, 1818.0))

    runner._on_spot_price("eth_spot", 1818.0, now)

    assert [c["ticker"] for c in eth.evaluate_calls] == ["KXETH15M-26JUL311800-00"]
    assert btc.evaluate_calls == []  # BTC untouched by an ETH tick
    runner.close()


def test_each_feed_callback_reports_its_own_name(chdir_tmp):
    """
    Each feed's on_price must report ITS OWN name. The default-arg capture in
    Runner.__init__ is what guarantees that -- a bare closure over the loop
    variable would leave every feed calling back with the last name built.
    """
    seen = []
    eth = _FakeStrategy("eth", series_tickers=["KXETH15M"], required_feeds=["eth_spot"],
                        spot_feed_name="eth_spot")
    btc = _FakeStrategy("btc", series_tickers=["KXBTC15M"], required_feeds=["btc_spot"],
                        spot_feed_name="btc_spot")
    runner = Runner({"eth": eth, "btc": btc}, {"eth": 0.5, "btc": 0.5}, _cfg())
    runner._on_spot_price = lambda name, p, t: seen.append((name, p))

    runner._feeds["eth_spot"]._on_price(1860.71, time.time())
    runner._feeds["btc_spot"]._on_price(65000.0, time.time())

    assert seen == [("eth_spot", 1860.71), ("btc_spot", 65000.0)]
    runner.close()


def test_vol_estimate_uses_only_the_strategys_own_books(chdir_tmp):
    """
    The ladder-implied vol fit must not see another underlying's books.
    Fitting BTC's ladder and handing the sigma to an ETH contract would be an
    invisible mispricing.
    """
    import kbot.runner as runner_mod
    from kbot.vol import VolEstimate

    seen_books = {}

    def _fake_estimate(books, spot, now_ts, **kwargs):
        seen_books.update(books)
        return VolEstimate(sigma=0.5, source="fallback")

    s = _FakeStrategy("eth", series_tickers=["KXETH15M"], required_feeds=["eth_spot"],
                      spot_feed_name="eth_spot",
                      vol_model=(lambda t: None, lambda *a: 0.5, lambda *a: 0.5))
    runner = Runner({"eth": s}, {"eth": 1.0}, _cfg())
    runner._feeds["eth_spot"]._history.append((time.time(), 1860.71))
    runner._books["KXETH15M-26JUL311800-00"] = _book("KXETH15M-26JUL311800-00")
    runner._books["KXBTC15M-26JUL060115-15"] = _book("KXBTC15M-26JUL060115-15")

    import kbot.vol
    original = kbot.vol.estimate_vol
    kbot.vol.estimate_vol = _fake_estimate
    try:
        runner._estimate_spot_vol(s, 1860.71, time.time())
    finally:
        kbot.vol.estimate_vol = original

    assert set(seen_books) == {"KXETH15M-26JUL311800-00"}
    runner.close()


def test_strategy_without_a_spot_feed_still_gets_no_inputs(chdir_tmp):
    """Regression guard on the generalisation: base_rate-style strategies
    (no spot underlying) must keep receiving (None, None)."""
    a = _FakeStrategy("a", series_tickers=["KXCPI"], required_feeds=[])
    runner = Runner({"a": a}, {"a": 1.0}, _cfg())
    assert runner._feed_inputs_for(a, "KXCPI-1") == (None, None)
    runner.close()


# ── Calibrator-domain isolation (the hard blocker) ───────────────────────────

def test_refuses_to_load_btc_and_eth_strategies_in_one_process(chdir_tmp):
    """
    kbot.learning.retrain fits ONE global champion over every outcome in the
    events DB, and ModelRegistry.get_champion() returns ONE row. Co-loading
    two calibrated series would fit BTC's calibrator on BTC+ETH trades mixed.
    The deployment fix is separate services; this check makes it non-optional.
    """
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    from kbot.strategy.eth_15m import ETH15MStrategy

    btc = BTCHourlyStrategy(_cfg(), series_tickers=["KXBTC15M"], name="kxbtc15m")
    eth = ETH15MStrategy(_cfg())
    with pytest.raises(ValueError, match="different series in one"):
        Runner({"kxbtc15m": btc, "kxeth15m": eth}, {"kxbtc15m": 0.5, "kxeth15m": 0.5}, _cfg())


def test_single_calibrated_strategy_loads_fine(chdir_tmp):
    from kbot.strategy.eth_15m import ETH15MStrategy
    eth = ETH15MStrategy(_cfg())
    runner = Runner({"kxeth15m": eth}, {"kxeth15m": 1.0}, _cfg())
    assert "kxeth15m" in runner._strategies
    runner.close()


def test_uncalibrated_strategy_may_co_load_with_a_calibrated_one(chdir_tmp):
    """base_rate has no calibrator domain -- it must stay co-loadable."""
    from kbot.strategy.eth_15m import ETH15MStrategy
    eth = ETH15MStrategy(_cfg())
    other = _FakeStrategy("base_rate", series_tickers=["KXCPI"])
    runner = Runner({"kxeth15m": eth, "base_rate": other},
                    {"kxeth15m": 0.5, "base_rate": 0.5}, _cfg())
    assert set(runner._strategies) == {"kxeth15m", "base_rate"}
    runner.close()


# ── Configurable heartbeat path (2026-07: co-tenant service collision) ───────
#
# Two services on one host shared ./kbot_heartbeat.json whenever they shared a
# working directory. Each then kept the OTHER's dead-man's switch permanently
# satisfied -- a real freeze would never be caught -- and `kbot status` showed
# whichever process wrote last. Both failure modes are entirely silent.

def test_heartbeat_default_path_is_unchanged(chdir_tmp):
    """BTC with no env override must behave byte-identically to before."""
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0}, _cfg())
    assert runner._heartbeat_path == Path("kbot_heartbeat.json")
    runner.close()


def test_heartbeat_written_to_the_configured_path(chdir_tmp):
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0},
                    _cfg(heartbeat_path="eth/kbot_eth_heartbeat.json"))
    Path("eth").mkdir()
    runner._write_heartbeat()

    assert Path("eth/kbot_eth_heartbeat.json").exists()
    assert not Path("kbot_heartbeat.json").exists()  # nothing at the old default
    runner.close()


def test_heartbeat_creates_a_missing_parent_directory(chdir_tmp):
    """
    A configured path into a not-yet-existing directory must not leave the
    process with NO heartbeat file -- the dead-man's switch reads that as
    'frozen' and force-exits, turning a path typo into a restart loop.
    """
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0},
                    _cfg(heartbeat_path="state/nested/hb.json"))
    assert not Path("state").exists()

    runner._write_heartbeat()

    assert Path("state/nested/hb.json").exists()
    runner.close()


def test_writer_and_deadman_reader_agree_on_the_configured_path(chdir_tmp, mocker):
    """
    THE invariant. If the dead-man's switch watched a path the writer doesn't
    write, it would see a permanently missing file and force-exit a healthy
    process -- so this asserts the reader is satisfied by the writer's OWN
    output at a NON-default path, with nothing at the default location.
    """
    exit_spy = mocker.patch("kbot.runner.os._exit")
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0},
                    _cfg(heartbeat_path="custom_heartbeat.json"))

    runner._write_heartbeat()
    assert Path("custom_heartbeat.json").exists()
    assert not Path("kbot_heartbeat.json").exists()

    # Booted 2h ago, so only the file the writer just produced can save it.
    runner._check_heartbeat_freshness(started=time.time() - 7200.0)
    exit_spy.assert_not_called()
    runner.close()


def test_deadman_reads_the_configured_path_not_the_default(chdir_tmp, mocker):
    """
    The converse: a fresh file at the DEFAULT path must not satisfy a switch
    configured to watch elsewhere. Without this, the collision bug would look
    'fixed' while the switch still keyed on the shared file.
    """
    import os as _os
    exit_spy = mocker.patch("kbot.runner.os._exit")
    runner = Runner({"a": _FakeStrategy("a")}, {"a": 1.0},
                    _cfg(heartbeat_path="custom_heartbeat.json"))

    # Fresh heartbeat at the OLD default -- the co-tenant service's file.
    Path("kbot_heartbeat.json").write_text("{}")
    # Configured path exists but is ancient.
    stale = Path("custom_heartbeat.json")
    stale.write_text("{}")
    old = time.time() - 1200.0
    _os.utime(stale, (old, old))

    runner._check_heartbeat_freshness(started=time.time() - 7200.0)
    exit_spy.assert_called_once_with(1)  # another service's file must not count
    runner.close()


def test_two_runners_with_different_heartbeat_paths_do_not_collide(chdir_tmp):
    """
    The deployment scenario: BTC and ETH each write their own file, and each
    reads back its OWN payload -- neither overwrites the other.
    """
    btc = Runner({"a": _FakeStrategy("a")}, {"a": 1.0},
                 _cfg(heartbeat_path="btc_heartbeat.json"))
    eth = Runner({"b": _FakeStrategy("b")}, {"b": 1.0},
                 _cfg(heartbeat_path="eth_heartbeat.json"))

    assert btc._heartbeat_path != eth._heartbeat_path

    btc._write_heartbeat()
    eth._write_heartbeat()

    btc_payload = json.loads(Path("btc_heartbeat.json").read_text())
    eth_payload = json.loads(Path("eth_heartbeat.json").read_text())
    assert set(btc_payload["strategies"]) == {"a"}
    assert set(eth_payload["strategies"]) == {"b"}   # ETH did not clobber BTC
    btc.close()
    eth.close()
