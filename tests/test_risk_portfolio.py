"""Tests for kbot.risk_portfolio — event-driver correlation exposure caps."""

from __future__ import annotations

import pytest

from kbot.config import Settings
from kbot.execution import Executor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.models.types import Side, SizingResult
from kbot.research.flags import clear_all_flags, set_flag
from kbot.risk_portfolio import PortfolioRisk


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        dry_run=kwargs.get("dry_run", True),
        min_net_edge=kwargs.get("min_net_edge", 0.04),
        driver_cap_pct=kwargs.get("driver_cap_pct", 0.25),
    )


def _filled_ack(*, ticker, side, count, price_dollars, **kwargs) -> OrderAck:
    """
    place_order stand-in: a COMPLETE fill at the requested price.

    fill_count must echo the (possibly driver-capped) request -- orders go out
    immediate_or_cancel, and an ack reporting 0 fills means nothing traded, so
    the executor opens no position and records no exposure.
    """
    return OrderAck(
        client_order_id="cid", order_id="oid", dry_run=True,
        fill_count_units=count * 100, remaining_count_units=0, average_fill_price=price_dollars,
    )


def _signal(
    ticker="TICK",
    driver="crypto_price",
    contracts=100,
    price=0.50,
    net_edge=0.05,
) -> SizingResult:
    return SizingResult(
        ticker=ticker,
        side=Side.YES,
        contracts=contracts,
        price_dollars=price,
        fair_p=0.55,
        net_edge_dollars=net_edge,
        kelly_fraction_used=0.1,
        event_driver=driver,
    )


@pytest.fixture(autouse=True)
def reset_research_flags():
    clear_all_flags()
    yield
    clear_all_flags()


# ── 1. Empty bucket allows the full signal ──────────────────────────────────────

def test_driver_cap_allows_signal_when_bucket_empty():
    pr = PortfolioRisk(_cfg(driver_cap_pct=0.25))
    # bankroll=1000, cap=25% -> $250 budget, price=0.50 -> 500 contracts fit
    signal = _signal(contracts=100, price=0.50)

    allowed = pr.apply_driver_cap(signal, {}, bankroll=1000.0)

    assert allowed == 100


# ── 2. Partially full bucket clamps contracts ───────────────────────────────────

def test_driver_cap_clamps_when_partially_full():
    pr = PortfolioRisk(_cfg(driver_cap_pct=0.25))
    # $250 budget, $200 already used -> $50 left / $0.50 = 100 contracts
    signal = _signal(contracts=500, price=0.50)

    allowed = pr.apply_driver_cap(
        signal, {"crypto_price": 200.0}, bankroll=1000.0
    )

    assert allowed == 100


# ── 3. Full bucket returns 0 and logs driver_cap ────────────────────────────────

def test_driver_cap_returns_zero_when_bucket_full(mocker):
    log_spy = mocker.patch("kbot.risk_portfolio.log")
    pr = PortfolioRisk(_cfg(driver_cap_pct=0.25))
    signal = _signal(contracts=10, price=0.50)

    allowed = pr.apply_driver_cap(
        signal, {"crypto_price": 250.0}, bankroll=1000.0
    )

    assert allowed == 0
    log_spy.info.assert_called_once()
    _, kwargs = log_spy.info.call_args
    assert kwargs["skip_reason"] == "driver_cap"


# ── 4. Two strategies on the same driver share the bucket ──────────────────────

def test_two_strategies_same_driver_share_bucket():
    pr = PortfolioRisk(_cfg(driver_cap_pct=0.25))

    pr.update_exposure("TICK-A", "crypto_price", 20000, 0.50, action="open")  # $100
    pr.update_exposure("TICK-B", "crypto_price", 20000, 0.50, action="open")  # $100

    assert pr.exposure_summary()["crypto_price"] == pytest.approx(200.0)

    # $250 cap - $200 used = $50 left / $0.50 = 100 contracts, regardless of
    # which "strategy" (ticker) is asking.
    signal = _signal(ticker="TICK-C", driver="crypto_price", contracts=500, price=0.50)
    allowed = pr.apply_driver_cap(signal, pr.exposure_summary(), bankroll=1000.0)
    assert allowed == 100


# ── 5. Different drivers have independent buckets ───────────────────────────────

def test_different_drivers_independent_buckets():
    pr = PortfolioRisk(_cfg(driver_cap_pct=0.25))

    pr.update_exposure("TICK-A", "crypto_price", 50000, 0.50, action="open")  # $250 -> full
    pr.update_exposure("TICK-B", "us_macro", 1000, 0.50, action="open")       # $5 -> nearly empty

    crypto_signal = _signal(ticker="TICK-C", driver="crypto_price", contracts=10, price=0.50)
    macro_signal = _signal(ticker="TICK-D", driver="us_macro", contracts=10, price=0.50)

    assert pr.apply_driver_cap(crypto_signal, pr.exposure_summary(), bankroll=1000.0) == 0
    assert pr.apply_driver_cap(macro_signal, pr.exposure_summary(), bankroll=1000.0) == 10


# ── 6 & 7. high_impact_event_imminent halves mapped drivers only ───────────────

def test_high_impact_flag_halves_mapped_driver_cap():
    pr = PortfolioRisk(_cfg(driver_cap_pct=0.25))
    set_flag("high_impact_event_imminent", True)

    # Halved cap: 12.5% of 1000 = $125 budget / $0.50 = 250 contracts.
    signal = _signal(driver="crypto_price", contracts=1000, price=0.50)
    allowed = pr.apply_driver_cap(signal, {}, bankroll=1000.0)

    assert allowed == 250


def test_unrelated_driver_unaffected_by_flag():
    pr = PortfolioRisk(_cfg(driver_cap_pct=0.25))
    set_flag("high_impact_event_imminent", True)

    # weather_nyc is not in DRIVER_IMPACT_MAP["high_impact_event_imminent"]
    # -> full 25% cap applies, unaffected by the flag.
    signal = _signal(driver="weather_nyc", contracts=1000, price=0.50)
    allowed = pr.apply_driver_cap(signal, {}, bankroll=1000.0)

    assert allowed == 500  # 25% of 1000 = $250 / $0.50 = 500

    assert pr.is_driver_halved("crypto_price") is True
    assert pr.is_driver_halved("weather_nyc") is False


# ── 8. update_exposure(close) releases the ticker's RECORDED exposure ───────────

def test_update_exposure_close_releases_recorded_amount():
    # 2026-07: close now releases the ticker's RECORDED exposure (not a
    # recompute from the passed contracts/price), so the bucket returns to
    # exactly its pre-open value regardless of the close call's arguments.
    pr = PortfolioRisk(_cfg())
    pr.update_exposure("TICK-A", "crypto_price", 20000, 0.50, action="open")  # $100 recorded
    assert pr.exposure_summary()["crypto_price"] == pytest.approx(100.0)

    # Even with mismatched contracts/price on the close, the full recorded
    # $100 is released (ticker-granular), leaving the bucket at 0.
    pr.update_exposure("TICK-A", "crypto_price", 100, 999.0, action="close")

    assert pr.exposure_summary()["crypto_price"] == pytest.approx(0.0)


def test_update_exposure_close_floors_at_zero():
    # A close for a ticker with no recorded exposure can't drive the bucket
    # negative (it's a no-op).
    pr = PortfolioRisk(_cfg())
    pr.update_exposure("TICK-A", "crypto_price", 10000, 0.50, action="open")  # $50
    pr.update_exposure("UNKNOWN", "crypto_price", 50000, 0.50, action="close")  # no recorded exposure

    assert pr.exposure_summary()["crypto_price"] == pytest.approx(50.0)  # untouched
    pr.update_exposure("TICK-A", "crypto_price", 0, 0.0, action="close")  # releases the $50
    assert pr.exposure_summary()["crypto_price"] == 0.0


def test_update_exposure_rejects_bad_action():
    pr = PortfolioRisk(_cfg())
    with pytest.raises(ValueError):
        pr.update_exposure("TICK-A", "crypto_price", 10000, 0.50, action="bogus")


# ── release_exposure (2026-07 driver-exposure leak fix) ──────────────────────

def test_release_exposure_frees_the_ticker_bucket():
    pr = PortfolioRisk(_cfg())
    pr.update_exposure("T1", "crypto_price", 10000, 0.60, action="open")  # $60
    pr.update_exposure("T2", "crypto_price", 10000, 0.40, action="open")  # $40
    assert pr.exposure_summary()["crypto_price"] == pytest.approx(100.0)

    released = pr.release_exposure("T1")

    assert released == pytest.approx(60.0)
    assert pr.exposure_summary()["crypto_price"] == pytest.approx(40.0)  # only T1 freed
    assert "T1" not in pr.ticker_exposure_summary()
    assert pr.ticker_exposure_summary()["T2"] == pytest.approx(40.0)


def test_release_exposure_uses_recorded_amount_not_current_price():
    # The whole point: release exactly what was committed at open, even if a
    # later (hypothetical) price is different -- the bucket returns to 0.
    pr = PortfolioRisk(_cfg())
    pr.update_exposure("T1", "crypto_price", 20000, 0.50, action="open")  # $100 recorded
    assert pr.release_exposure("T1") == pytest.approx(100.0)
    assert pr.exposure_summary()["crypto_price"] == pytest.approx(0.0)


def test_release_exposure_unknown_ticker_is_noop():
    pr = PortfolioRisk(_cfg())
    pr.update_exposure("T1", "crypto_price", 10000, 0.50, action="open")  # $50
    assert pr.release_exposure("NEVER-OPENED") == 0.0
    assert pr.exposure_summary()["crypto_price"] == pytest.approx(50.0)  # untouched


def test_release_exposure_never_goes_negative():
    pr = PortfolioRisk(_cfg())
    pr.update_exposure("T1", "crypto_price", 10000, 0.50, action="open")  # $50
    pr.release_exposure("T1")
    pr.release_exposure("T1")  # double release -- must not drive the bucket negative
    assert pr.exposure_summary()["crypto_price"] == pytest.approx(0.0)


def test_open_then_release_returns_bucket_to_exactly_zero():
    # Repeated open/expire cycles must not leak: this is the exact scenario
    # that had the driver cap fill up permanently.
    pr = PortfolioRisk(_cfg())
    for i in range(20):
        tk = f"KXBTC15M-{i}"
        pr.update_exposure(tk, "crypto_price", 96, 0.36, action="open")
        pr.release_exposure(tk)  # window settles
    assert pr.exposure_summary().get("crypto_price", 0.0) == pytest.approx(0.0)
    assert pr.ticker_exposure_summary() == {}


# ── 9. exposure_summary reports correct dollars per driver ─────────────────────

def test_exposure_summary_reports_correct_dollars():
    pr = PortfolioRisk(_cfg())
    pr.update_exposure("TICK-A", "crypto_price", 10000, 0.60, action="open")   # $60
    pr.update_exposure("TICK-B", "us_macro", 5000, 0.40, action="open")       # $20

    summary = pr.exposure_summary()

    assert summary == {"crypto_price": pytest.approx(60.0), "us_macro": pytest.approx(20.0)}


# ── 10. Executor with portfolio_risk=None is unaffected ────────────────────────

def test_executor_without_portfolio_risk_unchanged(mocker):
    cfg = _cfg(min_net_edge=0.04)
    client = mocker.MagicMock(spec=KalshiClient)
    client.place_order.side_effect = _filled_ack
    executor = Executor(client, cfg)  # no portfolio_risk, no bankroll_provider

    signal = _signal(contracts=1000, price=0.50, net_edge=0.05)
    ack = executor.execute(signal)

    assert ack is not None
    client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=1000, price_dollars=0.50
    )


def test_executor_with_portfolio_risk_but_no_bankroll_provider_unaffected(mocker):
    # portfolio_risk alone (no bankroll_provider) must not clamp -- there's
    # no bankroll to compute a dollar budget against.
    cfg = _cfg(min_net_edge=0.04)
    client = mocker.MagicMock(spec=KalshiClient)
    client.place_order.side_effect = _filled_ack
    pr = PortfolioRisk(cfg)
    pr.update_exposure("OTHER", "crypto_price", 10000000, 0.99, action="open")  # bucket "full"
    executor = Executor(client, cfg, portfolio_risk=pr)

    signal = _signal(contracts=1000, price=0.50, net_edge=0.05)
    ack = executor.execute(signal)

    assert ack is not None
    client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=1000, price_dollars=0.50
    )


def test_executor_with_portfolio_risk_and_bankroll_clamps_and_tracks(mocker):
    cfg = _cfg(min_net_edge=0.04, driver_cap_pct=0.25)
    client = mocker.MagicMock(spec=KalshiClient)
    client.place_order.side_effect = _filled_ack
    pr = PortfolioRisk(cfg)
    executor = Executor(client, cfg, portfolio_risk=pr, bankroll_provider=lambda: 1000.0)

    # $250 cap / $0.50 = 500 contracts max, requested 1000.
    signal = _signal(contracts=1000, price=0.50, net_edge=0.05)
    ack = executor.execute(signal)

    assert ack is not None
    client.place_order.assert_called_once_with(
        ticker="TICK", side="yes", count=500, price_dollars=0.50
    )
    assert pr.exposure_summary()["crypto_price"] == pytest.approx(250.0)


def test_executor_driver_cap_blocks_when_bucket_full(mocker):
    cfg = _cfg(min_net_edge=0.04, driver_cap_pct=0.25)
    client = mocker.MagicMock(spec=KalshiClient)
    pr = PortfolioRisk(cfg)
    pr.update_exposure("OTHER", "crypto_price", 50000, 0.50, action="open")  # $250 -> full
    executor = Executor(client, cfg, portfolio_risk=pr, bankroll_provider=lambda: 1000.0)

    signal = _signal(ticker="NEW", contracts=10, price=0.50, net_edge=0.05)
    ack = executor.execute(signal)

    assert ack is None
    client.place_order.assert_not_called()


def test_executor_close_position_releases_exposure(mocker):
    cfg = _cfg()
    client = mocker.MagicMock(spec=KalshiClient)
    pr = PortfolioRisk(cfg)
    pr.update_exposure("TICK-A", "crypto_price", 20000, 0.50, action="open")  # $100
    executor = Executor(client, cfg, portfolio_risk=pr)

    executor.close_position("TICK-A", Side.YES, driver="crypto_price", contracts=200, price=0.50)

    assert pr.exposure_summary()["crypto_price"] == 0.0
