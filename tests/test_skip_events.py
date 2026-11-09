"""
Stand-down skips must not be logged as evaluations (integrity patch item 5).

A strategy that declines on the clock never reads a quote or runs the model,
so its SizingResult carries placeholder zeros. Writing those to the evaluation
table records a measurement that was never taken: an ask of 0, a fair value of
0, and an edge of exactly 0 -- and any average over that table is dragged
toward zero by rows that are not observations at all.
"""

from __future__ import annotations

from kbot.models.types import Side, SizingResult


def _stand_down(**kw) -> SizingResult:
    """Exactly the shape the on-the-clock skip paths construct."""
    base = dict(
        ticker="KXBTC15M-T1", side=Side.YES, contracts=0,
        price_dollars=0.0, net_edge_dollars=0.0, kelly_fraction_used=0.0,
        skip_reason="within_5min_of_expiry", event_driver="crypto_price",
        priced=False,
    )
    base.update(kw)
    return SizingResult(**base)


def _priced(**kw) -> SizingResult:
    base = dict(
        ticker="KXBTC15M-T1", side=Side.YES, contracts=0,
        price_dollars=0.62, fair_p=0.58, net_edge_dollars=-0.004,
        kelly_fraction_used=0.0, skip_reason="edge_below_min",
        event_driver="crypto_price",
    )
    base.update(kw)
    return SizingResult(**base)


def test_priced_defaults_to_true():
    """Every existing construction stays a real evaluation."""
    assert _priced().priced is True


def test_stand_down_is_marked_unpriced():
    assert _stand_down().priced is False


def test_the_real_strategy_stand_downs_are_unpriced():
    """The zeros only exist on paths that never looked at the book."""
    from kbot.config import Settings
    from kbot.models.types import OrderBook
    from kbot.strategy.btc_hourly import BTCHourlyStrategy

    cfg = Settings(kalshi_key_id="", dry_run=True)
    strat = BTCHourlyStrategy(cfg)
    book = OrderBook(ticker="KXBTC-26AUG2118-T50000")

    # Expiry 60s out -> inside no_trade_final_minutes.
    expiry = strat.expiry_ts(book.ticker)
    if expiry is None:
        import pytest
        pytest.skip("ticker parser did not yield an expiry for this fixture")

    results = strat.evaluate(
        book, btc_price=50_000.0, btc_vol=0.5, bankroll=100.0,
        current_time=expiry - 60.0,
    )
    stand_downs = [r for r in results if r.skip_reason and "min_of_expiry" in r.skip_reason]
    assert stand_downs, "expected the on-the-clock stand-down to fire"
    for r in stand_downs:
        assert r.priced is False


class _Telemetry:
    def __init__(self):
        self.events = []

    def log_event(self, event_type, payload, ticker=None, **kw):
        self.events.append((event_type, payload, ticker))


def _emit(results):
    """Drive Runner._evaluate_ticker's emit block over `results`."""
    from kbot.runner import Runner

    class _Stub:
        pass

    stub = _Stub()
    stub._telemetry = _Telemetry()
    stub._bankroll = 100.0
    stub._allocations = {"btc": 1.0}
    stub._session_evals_logged = {"btc": 0}
    stub._safe_evaluate = lambda *a, **k: results
    stub._evaluate_ticker = Runner._evaluate_ticker.__get__(stub)
    return stub


def test_stand_down_emits_a_skip_event_with_null_measurements():
    stub = _emit([_stand_down()])
    stub._evaluate_ticker("btc", object(), "KXBTC15M-T1", object(), 50_000.0, 0.5)

    kinds = [e[0] for e in stub._telemetry.events]
    assert kinds == ["skip"]

    payload = stub._telemetry.events[0][1]
    assert payload["ask"] is None
    assert payload["fair_p"] is None
    assert payload["net_edge"] is None
    assert payload["skip_reason"] == "within_5min_of_expiry"


def test_stand_down_never_emits_an_evaluation_row():
    """The specific regression: ask=0, fair_p=0, net_edge=0 as a data point."""
    stub = _emit([_stand_down()])
    stub._evaluate_ticker("btc", object(), "KXBTC15M-T1", object(), 50_000.0, 0.5)

    for kind, payload, _ in stub._telemetry.events:
        assert kind != "evaluation"
        assert payload["ask"] != 0
        assert payload["net_edge"] != 0


def test_a_genuinely_priced_near_miss_is_still_an_evaluation():
    """The fix must not suppress real evaluations -- those numbers are data."""
    stub = _emit([_priced()])
    stub._evaluate_ticker("btc", object(), "KXBTC15M-T1", object(), 50_000.0, 0.5)

    kind, payload, _ = stub._telemetry.events[0]
    assert kind == "evaluation"
    assert payload["ask"] == 0.62
    assert payload["fair_p"] == 0.58


def test_mixed_results_are_routed_independently():
    stub = _emit([_stand_down(), _priced()])
    stub._evaluate_ticker("btc", object(), "KXBTC15M-T1", object(), 50_000.0, 0.5)
    assert [e[0] for e in stub._telemetry.events] == ["skip", "evaluation"]
