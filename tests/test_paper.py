"""
Tests for kbot.paper.PaperTrader — event-driven spot trigger wiring
(Phase A4 item 5). Constructing a PaperTrader is side-effect-free besides
opening local SQLite files (kbot_events.db / kbot_gates.db), so tests chdir
into tmp_path to keep those out of the repo. .run() (which touches the
network/WS) is never called here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kbot.config import Settings
from kbot.models.types import OrderBook, PriceLevel, Side, SizingResult
from kbot.paper import PaperTrader
from kbot.strategy.btc_hourly import BTCHourlyStrategy


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="",
        dry_run=True,
        spot_trigger_bps=kwargs.get("spot_trigger_bps", 10.0),
        btc_poll_interval_seconds=kwargs.get("btc_poll_interval_seconds", 2.0),
        paused_allows_dry_run=kwargs.get("paused_allows_dry_run", True),
    )


def _ticker(strike: int, mkt_type: str = "B", days_ahead: int = 7) -> str:
    et = ZoneInfo("America/New_York")
    dt = (datetime.now(tz=timezone.utc) + timedelta(days=days_ahead)).astimezone(et)
    return f"KXBTC-{dt.year - 2000:02d}{dt.strftime('%b').upper()}{dt.day:02d}{dt.hour:02d}-{mkt_type}{strike}"


def _book(ticker: str) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=0.50, quantity_units=1000)],
        no=[PriceLevel(price=0.48, quantity_units=1000)],
    )


@pytest.fixture()
def paper_trader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep kbot_events.db / kbot_gates.db out of the repo
    strategy = BTCHourlyStrategy(_cfg())
    pt = PaperTrader(strategy, [], _cfg())
    yield pt
    pt.close()


def test_on_btc_price_no_trigger_below_threshold(paper_trader, mocker):
    evaluate_spy = mocker.patch.object(paper_trader._strategy, "evaluate", return_value=[])
    near = _ticker(65000)
    paper_trader._books[near] = _book(near)

    # Mirror what BTCFeed._fetch_once() does: append the new sample BEFORE
    # invoking the on_price callback.
    paper_trader._btc_feed._history.append((0.0, 65000.0))
    paper_trader._btc_feed._history.append((1.0, 65001.0))
    paper_trader._on_btc_price(65001.0, 1.0)  # ~0.15 bps move -- well below 10 bps default

    evaluate_spy.assert_not_called()
    assert paper_trader._session_spot_triggers == 0


def test_on_btc_price_triggers_and_filters_to_near_atm(paper_trader, mocker):
    evaluate_spy = mocker.patch.object(paper_trader._strategy, "evaluate", return_value=[])
    near = _ticker(65000)  # within 2 strikes ($1000) of a 65100 spot
    far = _ticker(90000)   # far outside near-ATM window
    paper_trader._books[near] = _book(near)
    paper_trader._books[far] = _book(far)

    paper_trader._btc_feed._history.append((0.0, 65000.0))
    paper_trader._btc_feed._history.append((1.0, 65100.0))
    paper_trader._on_btc_price(65100.0, 1.0)  # +15.4 bps -- above the 10 bps default threshold

    assert paper_trader._session_spot_triggers == 1
    evaluated_tickers = {call.args[0].ticker for call in evaluate_spy.call_args_list}
    assert near in evaluated_tickers
    assert far not in evaluated_tickers


def test_on_btc_price_respects_custom_threshold(tmp_path, monkeypatch, mocker):
    monkeypatch.chdir(tmp_path)
    strategy = BTCHourlyStrategy(_cfg())
    pt = PaperTrader(strategy, [], _cfg(spot_trigger_bps=1000.0))  # very high bar
    evaluate_spy = mocker.patch.object(pt._strategy, "evaluate", return_value=[])
    ticker = _ticker(65000)
    pt._books[ticker] = _book(ticker)

    pt._btc_feed._history.append((0.0, 65000.0))
    pt._btc_feed._history.append((1.0, 65100.0))
    pt._on_btc_price(65100.0, 1.0)  # ~15 bps -- below the 1000 bps custom threshold

    evaluate_spy.assert_not_called()
    pt.close()


# ── Consistency scanner wiring (Phase A4 item 7) ────────────────────────────────

def test_run_consistency_scan_pushes_arb_detections_to_sheets(paper_trader, mocker):
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    ticker = _ticker(65000, mkt_type="T")
    paper_trader._books[ticker] = OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=0.40, quantity_units=1000)],
        no=[PriceLevel(price=0.40, quantity_units=1000)],
    )

    paper_trader._run_consistency_scan()

    append_spy.assert_called_once()
    row = append_spy.call_args[0][0]
    assert row["check_type"] == "same_market_arb"
    assert row["ticker_a"] == ticker


def test_run_consistency_scan_no_op_when_no_detections(paper_trader, mocker):
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    ticker = _ticker(65000, mkt_type="T")
    paper_trader._books[ticker] = _book(ticker)  # yes=0.50, no=0.48 -- sums to 0.98, no arb

    paper_trader._run_consistency_scan()

    append_spy.assert_not_called()


# ── Arb-log dedupe ──────────────────────────────────────────────────────────────
#
# The scan runs on every WS book message and the scanners are stateless, so a
# standing detection is re-reported on every message. These pin that only new
# or changed detections reach the sheet -- the unbounded row growth that put
# the workbook over Google's 10M-cell cap.

def _arb_book(ticker: str, yes: float, no: float) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes, quantity_units=1000)],
        no=[PriceLevel(price=no, quantity_units=1000)],
    )


def test_standing_detection_is_logged_once_across_many_scans(paper_trader, mocker):
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    ticker = _ticker(65000, mkt_type="T")
    paper_trader._books[ticker] = _arb_book(ticker, 0.40, 0.40)

    for _ in range(50):
        paper_trader._run_consistency_scan()

    append_spy.assert_called_once()


def test_changed_prices_relog_the_same_detection(paper_trader, mocker):
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    ticker = _ticker(65000, mkt_type="T")

    paper_trader._books[ticker] = _arb_book(ticker, 0.40, 0.40)
    paper_trader._run_consistency_scan()
    paper_trader._books[ticker] = _arb_book(ticker, 0.35, 0.40)   # moved a nickel
    paper_trader._run_consistency_scan()

    assert append_spy.call_count == 2


def test_sub_cent_price_noise_does_not_relog(paper_trader, mocker):
    """Kalshi quotes in whole cents; sub-cent wobble is not a new detection."""
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    ticker = _ticker(65000, mkt_type="T")

    paper_trader._books[ticker] = _arb_book(ticker, 0.40, 0.40)
    paper_trader._run_consistency_scan()
    paper_trader._books[ticker] = _arb_book(ticker, 0.400001, 0.40)
    paper_trader._run_consistency_scan()

    append_spy.assert_called_once()


def test_cleared_detection_is_logged_again_when_it_recurs(paper_trader, mocker):
    """Forgetting a cleared detection is what makes a genuine re-occurrence a new event."""
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    ticker = _ticker(65000, mkt_type="T")

    paper_trader._books[ticker] = _arb_book(ticker, 0.40, 0.40)
    paper_trader._run_consistency_scan()
    paper_trader._books[ticker] = _arb_book(ticker, 0.50, 0.50)   # arb gone
    paper_trader._run_consistency_scan()
    paper_trader._books[ticker] = _arb_book(ticker, 0.40, 0.40)   # back, same prices
    paper_trader._run_consistency_scan()

    assert append_spy.call_count == 2


def test_distinct_detections_are_tracked_independently(paper_trader, mocker):
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    first, second = _ticker(65000, mkt_type="T"), _ticker(66000, mkt_type="T")

    paper_trader._books[first] = _arb_book(first, 0.40, 0.40)
    paper_trader._run_consistency_scan()
    paper_trader._books[second] = _arb_book(second, 0.40, 0.40)
    paper_trader._run_consistency_scan()

    assert append_spy.call_count == 2
    assert [c[0][0]["ticker_a"] for c in append_spy.call_args_list] == [first, second]


def test_standing_monotonicity_violation_is_logged_once(paper_trader, mocker):
    # Integer-strike T tickers parse as the "below" ladder (YES if BTC < strike),
    # where the YES ask must NOT fall as the strike rises -- "below 66k" is the
    # easier outcome, so it cannot be cheaper than "below 65k".
    # Priced so YES+NO sums above $1: this is a monotonicity test, not an arb one.
    append_spy = mocker.patch.object(paper_trader._sheets, "append_arb_log")
    low, high = _ticker(65000, mkt_type="T"), _ticker(66000, mkt_type="T")
    paper_trader._books[low] = _arb_book(low, 0.40, 0.65)
    paper_trader._books[high] = _arb_book(high, 0.30, 0.75)

    for _ in range(20):
        paper_trader._run_consistency_scan()

    append_spy.assert_called_once()
    assert append_spy.call_args[0][0]["check_type"] == "monotonicity"


def test_arb_seen_holds_only_currently_standing_detections(paper_trader, mocker):
    """The dedupe map is rebuilt each scan, so it can't grow without bound."""
    mocker.patch.object(paper_trader._sheets, "append_arb_log")
    ticker = _ticker(65000, mkt_type="T")

    paper_trader._books[ticker] = _arb_book(ticker, 0.40, 0.40)
    paper_trader._run_consistency_scan()
    assert len(paper_trader._runner._arb_seen) == 1

    paper_trader._books[ticker] = _arb_book(ticker, 0.50, 0.50)   # detection clears
    paper_trader._run_consistency_scan()
    assert paper_trader._runner._arb_seen == {}


def test_on_ws_message_snapshot_triggers_consistency_scan(paper_trader, mocker):
    # PaperTrader._on_ws_message delegates to the underlying Runner, which
    # calls its OWN _run_consistency_scan -- patch that (not the wrapper's
    # pass-through method of the same name) to actually intercept the call.
    scan_spy = mocker.patch.object(paper_trader._runner, "_run_consistency_scan")

    paper_trader._on_ws_message({
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": "TICK", "no_dollars_fp": [], "yes_dollars_fp": []},
    })

    scan_spy.assert_called_once()


def test_on_ws_message_unrelated_type_does_not_scan(paper_trader, mocker):
    scan_spy = mocker.patch.object(paper_trader._runner, "_run_consistency_scan")

    paper_trader._on_ws_message({"type": "subscribed", "msg": {}})

    scan_spy.assert_not_called()


# ── Gate pause vs. dry-run (2026-07: paused strategies still forward-test in paper) ──

def _signal(ticker: str) -> SizingResult:
    return SizingResult(
        ticker=ticker, side=Side.YES, contracts=10, price_dollars=0.5,
        fair_p=0.6, net_edge_dollars=0.1, kelly_fraction_used=0.1,
    )


def _fake_gate(level="backtest", paused=False):
    from kbot.gates import StrategyGate
    return StrategyGate(strategy="x", level=level, paused=paused, backtest_pnl_summary=None, notes="", updated_at=0.0)


def test_evaluate_ticker_executes_when_paused_and_dry_run_allowed(paper_trader, mocker):
    # Default paused_allows_dry_run=True -- a gate-paused strategy must
    # still execute (dry-run) in paper so forward evidence keeps accumulating.
    mocker.patch.object(paper_trader._strategy, "evaluate", return_value=[_signal("TICK")])
    mocker.patch.object(paper_trader._gates, "get_gate", return_value=_fake_gate(paused=True))
    execute_spy = mocker.patch.object(paper_trader._executor, "execute", return_value=None)

    paper_trader._evaluate_ticker("TICK", _book("TICK"), 65000.0, 0.8)

    execute_spy.assert_called_once()


def test_evaluate_ticker_blocks_when_paused_and_dry_run_disallowed(tmp_path, monkeypatch, mocker):
    monkeypatch.chdir(tmp_path)
    strategy = BTCHourlyStrategy(_cfg())
    pt = PaperTrader(strategy, [], _cfg(paused_allows_dry_run=False))
    mocker.patch.object(pt._strategy, "evaluate", return_value=[_signal("TICK")])
    mocker.patch.object(pt._gates, "get_gate", return_value=_fake_gate(paused=True))
    execute_spy = mocker.patch.object(pt._executor, "execute", return_value=None)

    pt._evaluate_ticker("TICK", _book("TICK"), 65000.0, 0.8)

    execute_spy.assert_not_called()
    pt.close()


def test_evaluate_ticker_executes_normally_when_not_paused(paper_trader, mocker):
    mocker.patch.object(paper_trader._strategy, "evaluate", return_value=[_signal("TICK")])
    mocker.patch.object(paper_trader._gates, "get_gate", return_value=_fake_gate(paused=False))
    execute_spy = mocker.patch.object(paper_trader._executor, "execute", return_value=None)

    paper_trader._evaluate_ticker("TICK", _book("TICK"), 65000.0, 0.8)

    execute_spy.assert_called_once()
