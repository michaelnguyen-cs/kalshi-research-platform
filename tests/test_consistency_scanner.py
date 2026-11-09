"""Tests for kbot.scanners.consistency — same-market arb + monotonicity checks."""

from __future__ import annotations

from unittest.mock import MagicMock

import gspread
import pytest

from kbot.config import Settings
from kbot.models.types import OrderBook, PriceLevel
from kbot.scanners.consistency import (
    MonotonicityViolation,
    SameMarketArb,
    monotonicity_violation_to_row,
    same_market_arb_to_row,
    scan_monotonicity,
    scan_same_market_arb,
    run_consistency_scan,
)
from kbot.sheets import (
    TAB_ARB_LOG,
    SheetsClient,
    _ARB_LOG_MAX_CELLS,
    _HEADERS,
    arb_log_disabled,
    reset_arb_log_disabled,
)


@pytest.fixture(autouse=True)
def _rearm_arb_log():
    """
    The arb-log kill switch is process-wide by design, so it has to be reset
    around every test here -- before, so an earlier test can't pre-disable the
    writer under test, and after, so a disabled writer can't leak into another
    module's tests as a silent no-op.
    """
    reset_arb_log_disabled()
    yield
    reset_arb_log_disabled()


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        dry_run=True,
        fee_coefficient=kwargs.get("fee_coefficient", 0.07),
    )


def _book(ticker: str, yes_ask: float, no_ask: float) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes_ask, quantity_units=10000)],
        no=[PriceLevel(price=no_ask, quantity_units=10000)],
    )


# ── scan_same_market_arb ─────────────────────────────────────────────────────────

def test_same_market_arb_detected_when_sum_below_one_net_of_fees():
    # yes_ask=0.45, no_ask=0.45 -> gross = 1 - 0.90 = 0.10, well above fees.
    books = {"TICK": _book("TICK", 0.45, 0.45)}
    hits = scan_same_market_arb(books, _cfg())
    assert len(hits) == 1
    assert hits[0].ticker == "TICK"
    assert hits[0].net_dollars > 0


def test_same_market_arb_not_detected_when_sum_at_one():
    # yes_ask + no_ask == 1.0 exactly -> no gross edge, fees make it negative.
    books = {"TICK": _book("TICK", 0.50, 0.50)}
    hits = scan_same_market_arb(books, _cfg())
    assert hits == []


def test_same_market_arb_not_detected_when_fees_eat_small_gross_edge():
    # gross = 1 - 0.99 = 0.01, but taker fees at these prices exceed 1c.
    books = {"TICK": _book("TICK", 0.495, 0.495)}
    hits = scan_same_market_arb(books, _cfg(fee_coefficient=0.5))  # inflate fees
    assert hits == []


def test_same_market_arb_skips_book_with_missing_side():
    books = {"TICK": OrderBook(ticker="TICK", yes=[PriceLevel(price=0.3, quantity_units=1000)], no=[])}
    hits = scan_same_market_arb(books, _cfg())
    assert hits == []


def test_same_market_arb_scans_multiple_tickers_independently():
    books = {
        "ARB": _book("ARB", 0.40, 0.40),      # arb
        "FAIR": _book("FAIR", 0.55, 0.47),     # no arb (sums > 1)
    }
    hits = scan_same_market_arb(books, _cfg())
    assert {h.ticker for h in hits} == {"ARB"}


# ── scan_monotonicity ─────────────────────────────────────────────────────────────

def _above_parser(prices: dict[str, tuple[float, str, str]]):
    """ticker_parser stub: ticker -> (strike, direction, group_key) from a lookup dict."""
    def _parse(ticker: str):
        return prices.get(ticker)
    return _parse


def test_monotonicity_violation_detected_for_above_direction():
    # "above 60k" priced BELOW "above 61k" -- the higher (harder) bar should
    # never cost MORE, so yes higher strike ask > lower strike ask is a violation.
    books = {
        "T60000": _book("T60000", 0.30, 0.68),
        "T61000": _book("T61000", 0.45, 0.53),  # priced higher despite harder bar
    }
    parser = _above_parser({
        "T60000": (60000.0, "above", "expiry-1"),
        "T61000": (61000.0, "above", "expiry-1"),
    })
    violations = scan_monotonicity(books, parser)
    assert len(violations) == 1
    v = violations[0]
    assert v.lower_strike_ticker == "T60000"
    assert v.higher_strike_ticker == "T61000"
    assert v.direction == "above"


def test_monotonicity_no_violation_for_correctly_ordered_above():
    books = {
        "T60000": _book("T60000", 0.45, 0.53),
        "T61000": _book("T61000", 0.30, 0.68),  # cheaper -- correctly ordered
    }
    parser = _above_parser({
        "T60000": (60000.0, "above", "expiry-1"),
        "T61000": (61000.0, "above", "expiry-1"),
    })
    assert scan_monotonicity(books, parser) == []


def test_monotonicity_violation_detected_for_below_direction():
    # "below K" should get MORE expensive as K increases (easier to clear).
    books = {
        "T60000": _book("T60000", 0.45, 0.53),
        "T61000": _book("T61000", 0.30, 0.68),  # cheaper despite easier bar -- violation
    }
    parser = _above_parser({
        "T60000": (60000.0, "below", "expiry-1"),
        "T61000": (61000.0, "below", "expiry-1"),
    })
    violations = scan_monotonicity(books, parser)
    assert len(violations) == 1
    assert violations[0].direction == "below"


def test_monotonicity_ignores_range_direction():
    books = {
        "B60000": _book("B60000", 0.30, 0.68),
        "B61000": _book("B61000", 0.90, 0.08),
    }
    parser = _above_parser({
        "B60000": (60000.0, "range", "expiry-1"),
        "B61000": (61000.0, "range", "expiry-1"),
    })
    assert scan_monotonicity(books, parser) == []


def test_monotonicity_does_not_compare_across_different_groups():
    # Same direction, different expiry -- must not be compared.
    books = {
        "T60000-E1": _book("T60000-E1", 0.30, 0.68),
        "T59000-E2": _book("T59000-E2", 0.90, 0.08),  # would violate if same group
    }
    parser = _above_parser({
        "T60000-E1": (60000.0, "above", "expiry-1"),
        "T59000-E2": (59000.0, "above", "expiry-2"),
    })
    assert scan_monotonicity(books, parser) == []


def test_monotonicity_skips_unparseable_tickers():
    books = {"WEIRD": _book("WEIRD", 0.5, 0.5)}
    parser = _above_parser({})  # returns None for everything
    assert scan_monotonicity(books, parser) == []


# ── run_consistency_scan ───────────────────────────────────────────────────────────

def test_run_consistency_scan_combines_both_checks():
    books = {
        "ARB": _book("ARB", 0.40, 0.40),
        "T60000": _book("T60000", 0.30, 0.68),
        "T61000": _book("T61000", 0.45, 0.53),
    }
    parser = _above_parser({
        "T60000": (60000.0, "above", "expiry-1"),
        "T61000": (61000.0, "above", "expiry-1"),
    })
    result = run_consistency_scan(books, parser, _cfg())
    assert len(result["same_market_arb"]) == 1
    assert len(result["monotonicity"]) == 1


def test_run_consistency_scan_without_parser_skips_monotonicity():
    books = {"ARB": _book("ARB", 0.40, 0.40)}
    result = run_consistency_scan(books, ticker_parser=None, cfg=_cfg())
    assert len(result["same_market_arb"]) == 1
    assert result["monotonicity"] == []


def test_run_consistency_scan_never_raises_on_broken_parser():
    books = {"TICK": _book("TICK", 0.30, 0.68)}

    def _broken_parser(ticker):
        raise RuntimeError("boom")

    result = run_consistency_scan(books, _broken_parser, _cfg())  # must not raise
    assert result["monotonicity"] == []


def test_run_consistency_scan_empty_books():
    result = run_consistency_scan({}, None, _cfg())
    assert result == {"same_market_arb": [], "monotonicity": []}


# ── Sheet row builders ───────────────────────────────────────────────────────────

def test_same_market_arb_to_row_shape():
    hit = SameMarketArb(ticker="TICK", yes_ask=0.4, no_ask=0.4, gross_dollars=0.2, net_dollars=0.15)
    row = same_market_arb_to_row(hit)
    assert row["check_type"] == "same_market_arb"
    assert row["ticker_a"] == "TICK"
    assert row["ticker_b"] == ""
    assert row["net_dollars"] == 0.15
    assert "ts_utc" in row


def test_monotonicity_violation_to_row_shape():
    v = MonotonicityViolation(
        group_key="expiry-1", direction="above",
        lower_strike_ticker="T60000", lower_strike=60000.0, lower_strike_yes_ask=0.30,
        higher_strike_ticker="T61000", higher_strike=61000.0, higher_strike_yes_ask=0.45,
    )
    row = monotonicity_violation_to_row(v)
    assert row["check_type"] == "monotonicity"
    assert row["ticker_a"] == "T60000"
    assert row["ticker_b"] == "T61000"
    assert "direction=above" in row["detail"]
    assert "ts_utc" in row


# ── Arb Log sheet tab ───────────────────────────────────────────────────────────

def _make_worksheet(headers=None) -> MagicMock:
    ws = MagicMock()
    ws.row_values.return_value = headers or []
    ws.get_all_values.return_value = []
    ws.append_row.return_value = {}
    ws.append_rows.return_value = {}
    ws.update.return_value = {}
    ws.clear.return_value = {}
    return ws


def test_arb_log_tab_created_by_ensure_tabs():
    worksheets = {title: _make_worksheet() for title in _HEADERS}

    def _worksheet(title: str):
        raise gspread.exceptions.WorksheetNotFound(title)

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = _worksheet
    spreadsheet.add_worksheet.side_effect = lambda title, rows, cols: worksheets[title]

    client = SheetsClient(_spreadsheet=spreadsheet)
    client.ensure_tabs()

    created_titles = [c.kwargs.get("title") for c in spreadsheet.add_worksheet.call_args_list]
    assert TAB_ARB_LOG in created_titles
    worksheets[TAB_ARB_LOG].update.assert_called_once_with("A1", [_HEADERS[TAB_ARB_LOG]])


def test_append_arb_log_writes_one_row():
    ws = _make_worksheet()
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = ws

    client = SheetsClient(_spreadsheet=spreadsheet)
    client.append_arb_log(same_market_arb_to_row(
        SameMarketArb(ticker="TICK", yes_ask=0.4, no_ask=0.4, gross_dollars=0.2, net_dollars=0.15)
    ))

    ws.append_row.assert_called_once()


def test_append_arb_log_failure_does_not_raise():
    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = RuntimeError("sheets down")

    client = SheetsClient(_spreadsheet=spreadsheet)
    client.append_arb_log({"check_type": "same_market_arb"})  # must not raise


def test_transport_failure_leaves_the_writer_armed():
    """A local/transport error may succeed next time — only Google refusing latches."""
    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = RuntimeError("connection reset")

    client = SheetsClient(_spreadsheet=spreadsheet)
    client.append_arb_log({"check_type": "same_market_arb"})

    assert not arb_log_disabled()


# ── Arb Log cell-cap kill switch ────────────────────────────────────────────────

_CELL_LIMIT_MESSAGE = (
    "This action would increase the number of cells in the workbook "
    "above the limit of 10000000 cells."
)


def _api_error(code: int, message: str) -> gspread.exceptions.APIError:
    """A real gspread APIError shaped like the one Google returns."""
    response = MagicMock()
    response.status_code = code
    response.json.return_value = {
        "error": {"code": code, "message": message, "status": "INVALID_ARGUMENT"}
    }
    return gspread.exceptions.APIError(response)


def _client_with(ws: MagicMock) -> tuple[SheetsClient, MagicMock]:
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = ws
    return SheetsClient(_spreadsheet=spreadsheet), spreadsheet


def test_cell_limit_error_disables_the_writer_and_does_not_raise():
    ws = _make_worksheet()
    ws.append_row.side_effect = _api_error(400, _CELL_LIMIT_MESSAGE)
    client, _ = _client_with(ws)

    client.append_arb_log({"check_type": "same_market_arb"})  # must not raise

    assert arb_log_disabled()


def test_disabled_writer_makes_no_further_api_calls():
    """
    The whole point of the latch: the WS path calls this on every book update,
    and each attempt is two blocking round-trips on the event loop.
    """
    ws = _make_worksheet()
    ws.append_row.side_effect = _api_error(400, _CELL_LIMIT_MESSAGE)
    client, spreadsheet = _client_with(ws)

    for _ in range(25):
        client.append_arb_log({"check_type": "same_market_arb"})

    assert ws.append_row.call_count == 1       # one attempt, then never again
    assert spreadsheet.worksheet.call_count == 1   # not even the metadata fetch


def test_disabled_arb_log_does_not_affect_other_tabs():
    """Calibration / Performance / Live P&L / Trade Log must keep writing."""
    arb_ws, trade_ws = _make_worksheet(), _make_worksheet()
    arb_ws.append_row.side_effect = _api_error(400, _CELL_LIMIT_MESSAGE)

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = lambda title: (
        arb_ws if title == TAB_ARB_LOG else trade_ws
    )
    client = SheetsClient(_spreadsheet=spreadsheet)

    client.append_arb_log({"check_type": "same_market_arb"})
    assert arb_log_disabled()

    client.append_trade({"ticker": "TICK", "event_type": "order"})
    trade_ws.append_row.assert_called_once()


def test_append_skipped_when_tab_already_near_the_cell_cap():
    ws = _make_worksheet()
    ws.col_count = 26
    ws.row_count = (_ARB_LOG_MAX_CELLS // 26) + 1
    client, _ = _client_with(ws)

    client.append_arb_log({"check_type": "same_market_arb"})

    ws.append_row.assert_not_called()
    assert arb_log_disabled()


def test_append_proceeds_when_grid_size_is_unknown():
    """An unreported grid size means 'unknown', never 'over the limit'."""
    ws = _make_worksheet()   # MagicMock attrs are not ints -> size unknown
    client, _ = _client_with(ws)

    client.append_arb_log({"check_type": "same_market_arb"})

    ws.append_row.assert_called_once()
    assert not arb_log_disabled()
