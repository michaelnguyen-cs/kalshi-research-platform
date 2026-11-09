"""
Tests for kbot.sheets.

Strategy: inject a MagicMock spreadsheet into SheetsClient so no real
Sheets API calls are made.  gspread must be importable (it is — it's in
our deps) for WorksheetNotFound to be referenceable in the module under test.

Three requirements verified:
  1. ensure_tabs() is idempotent — no extra creates/writes on repeated calls.
  2. append_trade() adds exactly one row per call.
  3. Sheets failures do not propagate to the caller.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, call, patch

import pytest
import gspread

from kbot.sheets import (
    SheetsClient,
    TAB_TRADE_LOG,
    TAB_DAILY_SUMMARY,
    TAB_CALIBRATION,
    TAB_SKIPPED,
    TAB_RESEARCH,
    TAB_PERFORMANCE,
    TAB_LIVE_PNL,
    _ENSURE_TABS,
    _HEADERS,
    _SYNC_EVENT_TYPES,
    _SKIP_NEAR_MISS_THRESHOLD,
    _SYNC_BATCH_SIZE,
    _event_to_trade_row,
    _event_to_skip_row,
    tab_for,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_worksheet(headers: list[str] | None = None) -> MagicMock:
    """Return a mock Worksheet whose row_values(1) returns `headers`."""
    ws = MagicMock()
    ws.row_values.return_value = headers or []
    ws.get_all_values.return_value = []
    ws.append_row.return_value = {}
    ws.append_rows.return_value = {}   # plural — used by sync_all batch writer
    ws.update.return_value = {}
    ws.clear.return_value = {}
    return ws


def _make_spreadsheet(existing_tabs: set[str] | None = None) -> tuple[MagicMock, dict[str, MagicMock]]:
    """
    Return (spreadsheet_mock, {tab_name: worksheet_mock}).

    Tabs in `existing_tabs` are returned by spreadsheet.worksheet();
    all others raise WorksheetNotFound, causing add_worksheet to be called.
    """
    existing_tabs = existing_tabs or set()
    worksheets: dict[str, MagicMock] = {
        title: _make_worksheet() for title in _HEADERS
    }

    def _worksheet(title: str) -> MagicMock:
        if title in existing_tabs:
            return worksheets[title]
        raise gspread.exceptions.WorksheetNotFound(title)

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = _worksheet
    spreadsheet.add_worksheet.side_effect = lambda title, rows, cols: worksheets[title]
    return spreadsheet, worksheets


def _client(spreadsheet: MagicMock) -> SheetsClient:
    return SheetsClient(_spreadsheet=spreadsheet)


# ── ensure_tabs: tabs already exist, correct headers → no creates, no writes ──

class TestEnsureTabsIdempotent:
    """
    When all tabs exist and already have correct headers, calling ensure_tabs()
    twice must NOT call add_worksheet or update at all.
    """

    def _spreadsheet_all_exist_correct_headers(self) -> tuple[MagicMock, dict]:
        """All tabs exist; row_values(1) already returns the right headers."""
        worksheets = {
            title: _make_worksheet(headers=headers)
            for title, headers in _HEADERS.items()
        }

        def _worksheet(title: str) -> MagicMock:
            return worksheets[title]

        spreadsheet = MagicMock()
        spreadsheet.worksheet.side_effect = _worksheet
        spreadsheet.add_worksheet.side_effect = AssertionError("add_worksheet should not be called")
        return spreadsheet, worksheets

    def test_no_tab_created_when_all_exist(self):
        spreadsheet, _ = self._spreadsheet_all_exist_correct_headers()
        client = _client(spreadsheet)

        client.ensure_tabs()

        spreadsheet.add_worksheet.assert_not_called()

    def test_no_header_write_when_headers_match(self):
        spreadsheet, worksheets = self._spreadsheet_all_exist_correct_headers()
        client = _client(spreadsheet)

        client.ensure_tabs()

        for ws in worksheets.values():
            ws.update.assert_not_called()

    def test_second_call_is_also_a_noop(self):
        spreadsheet, worksheets = self._spreadsheet_all_exist_correct_headers()
        client = _client(spreadsheet)

        client.ensure_tabs()
        client.ensure_tabs()

        spreadsheet.add_worksheet.assert_not_called()
        for ws in worksheets.values():
            ws.update.assert_not_called()

    def test_missing_tab_is_created(self):
        """Tabs in existing_tabs exist; all others must be created."""
        spreadsheet, worksheets = _make_spreadsheet(existing_tabs={TAB_TRADE_LOG})
        client = _client(spreadsheet)

        client.ensure_tabs()

        # add_worksheet called for every ensured tab that wasn't in existing_tabs
        expected_created = set(_ENSURE_TABS) - {TAB_TRADE_LOG}
        assert spreadsheet.add_worksheet.call_count == len(expected_created)

    def test_live_pnl_tab_not_created_up_front(self):
        """
        The real-money tab must only appear once a real trade lands in it --
        an empty "Live P&L" tab on a paper deployment reads as "we went live".
        """
        spreadsheet, _ = _make_spreadsheet(existing_tabs=set())
        client = _client(spreadsheet)

        client.ensure_tabs()

        created = {c.kwargs.get("title") for c in spreadsheet.add_worksheet.call_args_list}
        assert TAB_LIVE_PNL not in created

    def test_headers_written_for_new_tabs(self):
        """A freshly created tab (empty row_values) gets its header row written."""
        spreadsheet, worksheets = _make_spreadsheet(existing_tabs=set())
        client = _client(spreadsheet)

        client.ensure_tabs()

        for title in _ENSURE_TABS:
            worksheets[title].update.assert_called_once_with("A1", [_HEADERS[title]])

    def test_headers_written_when_row_mismatch(self):
        """If row 1 has wrong content, headers are re-written."""
        worksheets = {
            title: _make_worksheet(headers=["wrong", "headers"])
            for title in _HEADERS
        }

        def _worksheet(title: str) -> MagicMock:
            return worksheets[title]

        spreadsheet = MagicMock()
        spreadsheet.worksheet.side_effect = _worksheet
        client = _client(spreadsheet)

        client.ensure_tabs()

        for title in _ENSURE_TABS:
            worksheets[title].update.assert_called_once_with("A1", [_HEADERS[title]])


# ── append_trade: exactly one row appended ────────────────────────────────────

class TestAppendTrade:
    def _setup(self) -> tuple[SheetsClient, MagicMock]:
        ws = _make_worksheet()
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        return _client(spreadsheet), ws

    def test_appends_exactly_one_row(self):
        client, ws = self._setup()
        trade = {"ts_utc": "2024-01-01T00:00:00+00:00", "ticker": "KXBTC-24JAN01-T30000", "contracts": 5}

        client.append_trade(trade)

        ws.append_row.assert_called_once()

    def test_row_values_map_to_headers(self):
        client, ws = self._setup()
        trade = {
            "ts_utc":              "2024-01-01T12:00:00+00:00",
            "event_type":          "fill",
            "ticker":              "KXBTC-24JAN01-T30000",
            "dry_run":             True,
            "side":                "yes",
            "contracts":           10,
            "price_dollars":       0.55,
            "net_edge_dollars":    0.06,
            "kelly_fraction_used": 0.25,
            "capped":              False,
            "order_id":            "oid-001",
        }

        client.append_trade(trade)

        appended_row = ws.append_row.call_args[0][0]
        headers = _HEADERS[TAB_TRADE_LOG]
        assert len(appended_row) == len(headers)
        assert appended_row[headers.index("ticker")] == "KXBTC-24JAN01-T30000"
        assert appended_row[headers.index("contracts")] == 10
        assert appended_row[headers.index("side")] == "yes"

    def test_missing_fields_become_empty_string(self):
        client, ws = self._setup()

        client.append_trade({"ts_utc": "2024-01-01T00:00:00+00:00"})

        appended_row = ws.append_row.call_args[0][0]
        headers = _HEADERS[TAB_TRADE_LOG]
        # Every column that wasn't in the dict should be ""
        assert appended_row[headers.index("contracts")] == ""
        assert appended_row[headers.index("order_id")] == ""

    def test_two_calls_append_two_rows(self):
        client, ws = self._setup()
        trade = {"ts_utc": "2024-01-01T00:00:00+00:00", "ticker": "T1"}

        client.append_trade(trade)
        client.append_trade(trade)

        assert ws.append_row.call_count == 2


# ── append_skip ───────────────────────────────────────────────────────────────

class TestAppendSkip:
    def _setup(self) -> tuple[SheetsClient, MagicMock]:
        ws = _make_worksheet()
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        return _client(spreadsheet), ws

    def test_appends_exactly_one_row(self):
        client, ws = self._setup()
        skip = {"ts_utc": "2024-01-01T00:00:00+00:00", "ticker": "T1", "reason_skipped": "edge_gate"}

        client.append_skip(skip)

        ws.append_row.assert_called_once()

    def test_row_has_correct_column_count(self):
        client, ws = self._setup()

        client.append_skip({"ts_utc": "t"})

        row = ws.append_row.call_args[0][0]
        assert len(row) == len(_HEADERS[TAB_SKIPPED])


# ── Sheets failures must never raise into the caller ──────────────────────────

class TestFailureIsolation:
    """
    If any gspread call raises, the method must log the error and return
    normally — the trading loop must not see the exception.
    """

    def _exploding_spreadsheet(self, exc: Exception) -> MagicMock:
        spreadsheet = MagicMock()
        ws = MagicMock()
        ws.append_row.side_effect = exc
        ws.row_values.side_effect = exc
        ws.get_all_values.side_effect = exc
        ws.update.side_effect = exc
        ws.clear.side_effect = exc
        spreadsheet.worksheet.return_value = ws
        spreadsheet.add_worksheet.return_value = ws
        return spreadsheet

    def test_append_trade_does_not_raise_on_api_error(self):
        spreadsheet = self._exploding_spreadsheet(RuntimeError("simulated API failure"))
        client = _client(spreadsheet)

        # Must not raise
        client.append_trade({"ticker": "T1"})

    def test_append_skip_does_not_raise_on_api_error(self):
        spreadsheet = self._exploding_spreadsheet(RuntimeError("simulated API failure"))
        client = _client(spreadsheet)

        client.append_skip({"ticker": "T1"})

    def test_ensure_tabs_does_not_raise_on_api_error(self):
        spreadsheet = MagicMock()
        spreadsheet.worksheet.side_effect = RuntimeError("network down")
        spreadsheet.add_worksheet.side_effect = RuntimeError("network down")
        client = _client(spreadsheet)

        client.ensure_tabs()  # must not raise

    def test_upsert_daily_summary_does_not_raise(self):
        spreadsheet = self._exploding_spreadsheet(RuntimeError("quota exceeded"))
        client = _client(spreadsheet)

        client.upsert_daily_summary(date(2024, 1, 1), {"date": "2024-01-01"})

    def test_push_calibration_does_not_raise(self):
        spreadsheet = self._exploding_spreadsheet(RuntimeError("quota exceeded"))
        client = _client(spreadsheet)

        client.push_calibration([{"bucket_label": "0-10%", "count": 5}])

    def test_append_research_does_not_raise(self):
        spreadsheet = self._exploding_spreadsheet(RuntimeError("timeout"))
        client = _client(spreadsheet)

        client.append_research({"topic": "API changes", "ts_utc": "2024-01-01T00:00:00+00:00"})


# ── upsert_daily_summary ──────────────────────────────────────────────────────

class TestUpsertDailySummary:
    def _setup(self, existing_rows: list[list] | None = None) -> tuple[SheetsClient, MagicMock]:
        ws = _make_worksheet()
        ws.get_all_values.return_value = existing_rows or []
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        return _client(spreadsheet), ws

    def test_appends_when_date_not_present(self):
        client, ws = self._setup(existing_rows=[[*_HEADERS[TAB_DAILY_SUMMARY]]])
        client.upsert_daily_summary(date(2024, 1, 1), {"date": "2024-01-01", "num_trades": 3})

        ws.append_row.assert_called_once()
        ws.update.assert_not_called()

    def test_updates_in_place_when_date_exists(self):
        existing = [
            _HEADERS[TAB_DAILY_SUMMARY],
            ["2024-01-01", "2", "", "", "", "", "", "", "", "", ""],
        ]
        client, ws = self._setup(existing_rows=existing)

        client.upsert_daily_summary(date(2024, 1, 1), {"date": "2024-01-01", "num_trades": 5})

        ws.update.assert_called_once()
        ws.append_row.assert_not_called()


# ── push_calibration ──────────────────────────────────────────────────────────

class TestPushCalibration:
    def test_clears_then_writes_headers_and_data(self):
        ws = _make_worksheet()
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        client = _client(spreadsheet)

        buckets = [
            {"bucket_label": "0-10%", "bucket_low": 0.0, "bucket_high": 0.1,
             "count": 10, "realized_win_rate": 0.05, "brier_contribution": 0.02},
            {"bucket_label": "10-20%", "bucket_low": 0.1, "bucket_high": 0.2,
             "count": 8, "realized_win_rate": 0.12, "brier_contribution": 0.01},
        ]

        client.push_calibration(buckets)

        ws.clear.assert_called_once()
        ws.update.assert_called_once()
        written = ws.update.call_args[0][1]   # second positional arg = values
        assert written[0] == _HEADERS[TAB_CALIBRATION]  # header row first
        assert len(written) == 3               # header + 2 data rows


# ── Helper function tests ─────────────────────────────────────────────────────

class TestHelpers:
    def test_event_to_trade_row_maps_price_alias(self):
        """payload['price'] should map to trade_row['price_dollars']."""
        ev = {
            "ts": 1704067200.0,
            "event_type": "order",
            "ticker": "T1",
            "dry_run": True,
            "payload": {"contracts": 5, "price": 0.60},
        }
        row = _event_to_trade_row(ev)
        assert row["price_dollars"] == 0.60
        assert row["contracts"] == 5

    def test_event_to_trade_row_prefers_price_dollars(self):
        ev = {
            "ts": 1704067200.0,
            "event_type": "fill",
            "ticker": "T1",
            "dry_run": False,
            "payload": {"price_dollars": 0.55, "price": 0.60},
        }
        row = _event_to_trade_row(ev)
        assert row["price_dollars"] == 0.55

    def test_event_to_skip_row(self):
        ev = {
            "ts": 1704067200.0,
            "event_type": "skip",
            "ticker": "T2",
            "dry_run": True,
            "payload": {"model_prob": 0.65, "market_price": 0.60, "reason_skipped": "edge_gate"},
        }
        row = _event_to_skip_row(ev)
        assert row["ticker"] == "T2"
        assert row["model_prob"] == 0.65
        assert row["reason_skipped"] == "edge_gate"


# ── sync_all tests ────────────────────────────────────────────────────────────

def _make_event(
    event_type: str,
    ticker: str = "TICK",
    ts: float = 1704067200.0,
    **payload,
) -> dict:
    return {
        "ts": ts,
        "event_type": event_type,
        "ticker": ticker,
        "dry_run": True,
        "payload": payload,
    }


def _mock_telemetry(events_by_type: dict | None = None) -> MagicMock:
    """Return a Telemetry mock with canned responses for recent() and recent_types()."""
    events = events_by_type or {}
    tel = MagicMock()

    def _recent(event_type=None, limit=100):
        return list(events.get(event_type, []))

    def _recent_types(types, limit=10_000):
        result = []
        for t in types:
            result.extend(events.get(t, []))
        return result

    tel.recent.side_effect = _recent
    tel.recent_types.side_effect = _recent_types
    return tel


def _sync_all_client() -> tuple[SheetsClient, dict[str, MagicMock]]:
    """Return a SheetsClient and its worksheet mocks with all tabs pre-existing."""
    spreadsheet, worksheets = _make_spreadsheet(existing_tabs=set(_HEADERS))
    # Give each worksheet correct headers so ensure_tabs is a no-op.
    for title, ws in worksheets.items():
        ws.row_values.return_value = _HEADERS[title]
    return _client(spreadsheet), worksheets


class TestSyncAll:
    # ── Event type filter ─────────────────────────────────────────────────────

    def test_queries_only_trade_event_types(self):
        """recent_types() must be called with exactly _SYNC_EVENT_TYPES."""
        tel = _mock_telemetry()
        client, _ = _sync_all_client()

        client.sync_all(tel)

        # The first recent_types call should be for the trade log.
        first_call_types = set(tel.recent_types.call_args_list[0][0][0])
        assert first_call_types == set(_SYNC_EVENT_TYPES)

    def test_evaluation_events_never_queried(self):
        """sync_all must never call recent('evaluation') or include it in recent_types."""
        tel = _mock_telemetry()
        client, _ = _sync_all_client()

        client.sync_all(tel)

        # recent() calls should not include 'evaluation' or 'heartbeat'
        for call_args in tel.recent.call_args_list:
            etype = call_args[1].get("event_type") or (call_args[0][0] if call_args[0] else None)
            assert etype not in ("evaluation", "heartbeat"), (
                f"sync_all must not query event_type={etype!r}"
            )
        # recent_types calls must not include those types either
        for call_args in tel.recent_types.call_args_list:
            types = set(call_args[0][0])
            assert "evaluation" not in types
            assert "heartbeat" not in types

    def test_signal_events_reach_trade_log(self):
        """Signal events must appear in the Trade Log batch write."""
        events = {
            "signal": [_make_event("signal", "TICK", contracts=5, side="yes")],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_TRADE_LOG]
        assert ws.append_rows.called
        rows_written = ws.append_rows.call_args[0][0]
        assert len(rows_written) == 1

    def test_position_events_reach_trade_log(self):
        """position_open and position_closed must reach the Trade Log."""
        events = {
            "position_open":   [_make_event("position_open",   "TICK", side="yes")],
            "position_closed": [_make_event("position_closed", "TICK", side="yes")],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_TRADE_LOG]
        assert ws.append_rows.called
        rows_written = ws.append_rows.call_args[0][0]
        assert len(rows_written) == 2

    # ── Skip near-miss filter ─────────────────────────────────────────────────

    def test_far_otm_skips_excluded(self):
        """Skips with net_edge <= -0.02 must not appear in Skipped Signals."""
        events = {
            "skip": [_make_event("skip", "TICK", net_edge=-0.15, skip_reason="edge_below_min")],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_SKIPPED]
        ws.append_rows.assert_not_called()

    def test_near_miss_skips_included(self):
        """Skips with net_edge > -0.02 must appear in Skipped Signals."""
        events = {
            "skip": [_make_event("skip", "TICK", net_edge=-0.01, skip_reason="edge_below_min")],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_SKIPPED]
        assert ws.append_rows.called
        rows_written = ws.append_rows.call_args[0][0]
        assert len(rows_written) == 1

    def test_skip_at_exact_threshold_excluded(self):
        """net_edge == -0.02 is at the threshold and must be excluded."""
        events = {
            "skip": [_make_event("skip", "TICK", net_edge=_SKIP_NEAR_MISS_THRESHOLD)],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_SKIPPED]
        ws.append_rows.assert_not_called()

    def test_skip_with_no_net_edge_included(self):
        """Skips without a net_edge field (e.g. position_already_held) are always kept."""
        events = {
            "skip": [_make_event("skip", "TICK", skip_reason="position_already_held")],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_SKIPPED]
        assert ws.append_rows.called

    def test_mixed_skips_only_near_misses_written(self):
        """Only near-miss skips survive the filter when both types are present."""
        events = {
            "skip": [
                _make_event("skip", "A", ts=1.0, net_edge=-0.20),  # far-OTM → excluded
                _make_event("skip", "B", ts=2.0, net_edge=-0.01),  # near-miss → included
                _make_event("skip", "C", ts=3.0, net_edge=0.00),   # on the edge → included
                _make_event("skip", "D", ts=4.0),                  # no net_edge → included
            ]
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_SKIPPED]
        assert ws.append_rows.called
        rows_written = ws.append_rows.call_args[0][0]
        assert len(rows_written) == 3   # B, C, D — A is excluded

    # ── Batch writing ─────────────────────────────────────────────────────────

    def test_single_batch_uses_append_rows_not_append_row(self):
        """sync_all must use append_rows() (plural API) for batch efficiency."""
        events = {
            "signal": [_make_event("signal", "TICK", ts=float(i)) for i in range(10)],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_TRADE_LOG]
        assert ws.append_rows.called, "Expected append_rows (plural) to be called"
        ws.append_row.assert_not_called()

    def test_rows_split_into_correct_batch_count(self, mocker):
        """With _SYNC_BATCH_SIZE rows + 1 extra, expect exactly 2 batch calls."""
        mocker.patch("kbot.sheets.time.sleep")   # don't actually sleep in tests

        n_rows = _SYNC_BATCH_SIZE + 1
        events = {
            "signal": [
                _make_event("signal", "TICK", ts=float(i)) for i in range(n_rows)
            ],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        ws = worksheets[TAB_TRADE_LOG]
        assert ws.append_rows.call_count == 2

    def test_sleep_called_between_batches(self, mocker):
        """A 1-second sleep must occur between batches (not after the last one)."""
        mock_sleep = mocker.patch("kbot.sheets.time.sleep")

        n_rows = _SYNC_BATCH_SIZE * 3   # 3 full batches → 2 inter-batch sleeps
        events = {
            "signal": [
                _make_event("signal", "TICK", ts=float(i)) for i in range(n_rows)
            ],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        # sleep should have been called between batches, not after the last one
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert len(sleep_calls) == 2
        assert all(s == 1.0 for s in sleep_calls)

    def test_no_sleep_for_single_batch(self, mocker):
        """No inter-batch sleep when all rows fit in one batch."""
        mock_sleep = mocker.patch("kbot.sheets.time.sleep")

        events = {
            "signal": [_make_event("signal", "TICK", ts=float(i)) for i in range(5)],
        }
        tel = _mock_telemetry(events)
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        mock_sleep.assert_not_called()

    # ── No rows → no API calls ─────────────────────────────────────────────────

    def test_empty_events_no_write_calls(self):
        """sync_all with no events must not call append_rows on any worksheet."""
        tel = _mock_telemetry({})
        client, worksheets = _sync_all_client()

        client.sync_all(tel)

        for ws in worksheets.values():
            ws.append_rows.assert_not_called()
