"""
Tests for real-money P&L accounting: kbot.live_pnl, the is_live stamp that
feeds it, and the strategy-scoped sheet tabs it writes to.

The properties under test are the ones that cost real money if wrong:

  1. is_live is decided at ORDER-PLACEMENT time and persisted, so no later
     config flip or gate demotion can relabel a trade.
  2. The migration defaults every pre-existing row to paper, and never to live.
  3. Paper trades cannot reach a Live P&L tab.
  4. Pushing twice appends nothing the second time -- the running total is a
     function of the event log, not of how many times evaluate ran.
  5. Each strategy writes its own tabs, and BTC's keep their original names.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import gspread
import pytest

from kbot.config import Settings
from kbot.execution import Executor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.live_pnl import (
    build_live_pnl_rows,
    is_live_outcome,
    live_outcomes,
    push_live_pnl,
)
from kbot.models.types import Side, SizingResult
from kbot.outcomes import build_trade_outcome, record_ticker_outcomes
from kbot.position_tracker import PositionTracker
from kbot.sheets import (
    TAB_CALIBRATION,
    TAB_LIVE_PNL,
    TAB_PERFORMANCE,
    TAB_TRADE_LOG,
    SheetsClient,
    _HEADERS,
    tab_for,
)

import kbot.risk as risk_module


# ── Fixtures / helpers ───────────────────────────────────────────────────────

def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env=kwargs.get("kalshi_env", "demo"),
        dry_run=kwargs.get("dry_run", True),
        min_net_edge=kwargs.get("min_net_edge", 0.04),
    )


def _result(ticker="KXBTC15M-T1", side=Side.YES, contracts=5, price=0.60) -> SizingResult:
    return SizingResult(
        ticker=ticker,
        side=side,
        contracts=contracts,
        price_dollars=price,
        net_edge_dollars=0.05,
        kelly_fraction_used=0.1,
    )


@pytest.fixture(autouse=True)
def reset_kill_switch():
    risk_module._KILL_SWITCH_TRIPPED = False
    yield
    risk_module._KILL_SWITCH_TRIPPED = False


def _client_placing(dry_run: bool, mocker) -> MagicMock:
    client = mocker.MagicMock(spec=KalshiClient)

    def _place(*, ticker, side, count, price_dollars, **kwargs):
        # Complete fill at the requested price -- fill_count must be non-zero
        # or the executor correctly declines to open a position at all.
        return OrderAck(
            client_order_id="cid-1",
            order_id=None if dry_run else "oid-1",
            dry_run=dry_run,
            fill_count_units=count * 100,
            remaining_count_units=0,
            average_fill_price=price_dollars,
        )

    client.place_order.side_effect = _place
    return client


def _outcome_event(
    ticker: str,
    side: str = "yes",
    pnl: float = 1.0,
    ts: float = 1_700_000_000.0,
    is_live: bool = True,
    strategy: str | None = "kxbtc15m",
    contracts: int = 5,
    fill_price: float = 0.60,
) -> dict:
    return {
        "ts": ts,
        "event_type": "trade_outcome",
        "ticker": ticker,
        "dry_run": not is_live,
        "payload": {
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "fill_price": fill_price,
            "pnl_dollars": pnl,
            "strategy": strategy,
            "is_live": is_live,
        },
    }


def _telemetry_with(events: list[dict]) -> MagicMock:
    tel = MagicMock()
    tel.recent.side_effect = lambda event_type=None, limit=100: list(events)
    return tel


# ── 1. is_live is stamped at placement time and persisted ────────────────────

class TestIsLiveStampedAtExecution:
    def test_live_order_persists_is_live_true(self, mocker, tmp_path):
        tracker = PositionTracker(tmp_path / "pos.db")
        executor = Executor(
            _client_placing(dry_run=False, mocker=mocker),
            _cfg(kalshi_env="prod", dry_run=False),
            live_confirmed=True,
            tracker=tracker,
        )

        assert executor.execute(_result()) is not None

        rows = tracker.open_positions_with_exposure()
        assert len(rows) == 1
        assert rows[0]["is_live"] is True
        tracker.close()

    def test_dry_run_order_persists_is_live_false(self, mocker, tmp_path):
        tracker = PositionTracker(tmp_path / "pos.db")
        executor = Executor(
            _client_placing(dry_run=True, mocker=mocker), _cfg(), tracker=tracker
        )

        assert executor.execute(_result()) is not None

        assert tracker.open_positions_with_exposure()[0]["is_live"] is False
        tracker.close()

    def test_flag_follows_the_ack_not_the_config(self, mocker, tmp_path):
        """
        The stamp records what place_order actually DID. If the ack says
        dry-run, the position is paper even though cfg says live -- config is
        read at some arbitrary later moment, the ack is the moment itself.
        """
        tracker = PositionTracker(tmp_path / "pos.db")
        executor = Executor(
            _client_placing(dry_run=True, mocker=mocker),
            _cfg(kalshi_env="prod", dry_run=False),
            live_confirmed=True,
            tracker=tracker,
        )

        executor.execute(_result())

        assert tracker.open_positions_with_exposure()[0]["is_live"] is False
        tracker.close()

    def test_stamp_survives_the_v2_fill_fields(self, mocker, tmp_path):
        """
        The V2 ack carries fill_count / average_fill_price alongside dry_run.
        is_live must still come from dry_run alone -- a filled order is not
        evidence of real money, and a real order is live whether or not the
        exchange reported an average price.
        """
        tracker = PositionTracker(tmp_path / "pos.db")
        client = mocker.MagicMock(spec=KalshiClient)
        client.place_order.return_value = OrderAck(
            client_order_id="cid-1", order_id="oid-1", dry_run=False,
            fill_count_units=500, remaining_count_units=0, average_fill_price=None,
        )
        executor = Executor(
            client, _cfg(kalshi_env="prod", dry_run=False),
            live_confirmed=True, tracker=tracker,
        )

        assert executor.execute(_result()) is not None

        row = tracker.open_positions_with_exposure()[0]
        assert row["is_live"] is True
        assert row["price"] == pytest.approx(0.60)  # fell back to the limit price
        tracker.close()

    def test_stamp_survives_a_restart(self, tmp_path):
        """A new tracker over the same file must read the flag back unchanged."""
        db = tmp_path / "pos.db"
        first = PositionTracker(db)
        first.open_position("KXBTC15M-T1", "yes", contracts_units=300, price=0.5, is_live=True)
        first.close()

        second = PositionTracker(db)
        assert second.open_positions_with_exposure()[0]["is_live"] is True
        second.close()

    def test_default_is_paper_when_caller_does_not_know(self, tmp_path):
        tracker = PositionTracker(tmp_path / "pos.db")
        tracker.open_position("KXBTC15M-T1", "yes", contracts_units=300, price=0.5)
        assert tracker.open_positions_with_exposure()[0]["is_live"] is False
        tracker.close()


# ── 2. Migration defaults existing rows to paper ─────────────────────────────

class TestMigrationDefaultsIsLive:
    def _legacy_db(self, path) -> None:
        """A positions table as it existed before is_live, with a row in it."""
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE positions (
                ticker    TEXT NOT NULL,
                side      TEXT NOT NULL,
                contracts INTEGER NOT NULL DEFAULT 0,
                price     REAL NOT NULL DEFAULT 0,
                driver    TEXT NOT NULL DEFAULT 'other',
                PRIMARY KEY (ticker, side)
            );
            """
        )
        conn.execute(
            "INSERT INTO positions (ticker, side, contracts, price, driver) "
            "VALUES ('KXBTC15M-OLD', 'yes', 4, 0.42, 'crypto_price')"
        )
        conn.commit()
        conn.close()

    def test_column_added_to_legacy_table(self, tmp_path):
        db = tmp_path / "pos.db"
        self._legacy_db(db)

        tracker = PositionTracker(db)
        cols = {r[1] for r in tracker._conn.execute("PRAGMA table_info(positions)").fetchall()}
        assert "is_live" in cols
        tracker.close()

    def test_pre_existing_rows_default_to_paper(self, tmp_path):
        """Every trade predating real trading was paper. Never backfill to 1."""
        db = tmp_path / "pos.db"
        self._legacy_db(db)

        tracker = PositionTracker(db)
        rows = tracker.open_positions_with_exposure()
        assert len(rows) == 1
        assert rows[0]["is_live"] is False
        tracker.close()

    def test_legacy_row_keeps_its_other_columns(self, tmp_path):
        db = tmp_path / "pos.db"
        self._legacy_db(db)

        tracker = PositionTracker(db)
        row = tracker.open_positions_with_exposure()[0]
        assert (row["contracts_units"], row["price"], row["driver"]) == (400, 0.42, "crypto_price")
        tracker.close()

    def test_migration_is_idempotent(self, tmp_path):
        db = tmp_path / "pos.db"
        self._legacy_db(db)

        PositionTracker(db).close()
        tracker = PositionTracker(db)   # second open must not fail on duplicate column
        assert tracker.open_positions_with_exposure()[0]["is_live"] is False
        tracker.close()


# ── 3. The flag reaches the outcome record ───────────────────────────────────

class TestOutcomeCarriesIsLive:
    _market = {"result": "yes"}

    def test_outcome_payload_records_live(self):
        payload = build_trade_outcome(
            "KXBTC15M-T1", "yes", 5, 0.60, self._market, "kxbtc15m", is_live=True
        )
        assert payload["is_live"] is True

    def test_outcome_defaults_to_paper(self):
        payload = build_trade_outcome(
            "KXBTC15M-T1", "yes", 5, 0.60, self._market, "kxbtc15m"
        )
        assert payload["is_live"] is False

    def test_record_ticker_outcomes_reads_the_position_stamp(self):
        tel = MagicMock()
        positions = [
            {"ticker": "KXBTC15M-T1", "side": "yes", "contracts": 5,
             "price": 0.60, "is_live": True},
            {"ticker": "KXBTC15M-T1", "side": "no", "contracts": 2,
             "price": 0.40, "is_live": False},
        ]

        recorded = record_ticker_outcomes(
            "KXBTC15M-T1", positions, self._market, tel, "kxbtc15m"
        )

        assert recorded == 2
        flags = [c.args[1]["is_live"] for c in tel.log_trade_outcome.call_args_list]
        assert flags == [True, False]


# ── 4. Paper never reaches Live P&L ──────────────────────────────────────────

class TestPaperExcluded:
    def test_is_live_outcome_requires_the_flag(self):
        assert is_live_outcome({"is_live": True}) is True
        assert is_live_outcome({"is_live": False}) is False
        assert is_live_outcome({}) is False           # missing -> paper
        assert is_live_outcome({"is_live": None}) is False

    def test_dry_run_column_does_not_make_a_trade_live(self):
        """
        events.dry_run is the writing PROCESS's config, not a per-order fact --
        a CLI backfill stamps its own shell's DRY_RUN. Only the payload flag counts.
        """
        ev = _outcome_event("KXBTC15M-T1", is_live=False)
        ev["dry_run"] = False   # process claims live; the trade was not

        assert live_outcomes(_telemetry_with([ev])) == []

    def test_only_live_outcomes_selected(self):
        events = [
            _outcome_event("KXBTC15M-A", ts=1.0, is_live=True, pnl=1.0),
            _outcome_event("KXBTC15M-B", ts=2.0, is_live=False, pnl=99.0),
            _outcome_event("KXBTC15M-C", ts=3.0, is_live=True, pnl=2.0),
        ]

        selected = live_outcomes(_telemetry_with(events))

        assert [o["payload"]["ticker"] for o in selected] == ["KXBTC15M-A", "KXBTC15M-C"]

    def test_paper_pnl_never_enters_the_running_total(self):
        events = [
            _outcome_event("KXBTC15M-A", ts=1.0, is_live=True, pnl=1.5),
            _outcome_event("KXBTC15M-B", ts=2.0, is_live=False, pnl=1000.0),
        ]

        rows = build_live_pnl_rows(live_outcomes(_telemetry_with(events)))

        assert len(rows) == 1
        assert rows[0]["cumulative_realized_pnl"] == 1.5

    def test_no_live_trades_creates_no_tab(self):
        """A paper deployment must not even touch the sheet."""
        sheets = MagicMock()
        sheets.strategy = "kxeth15m"
        events = [_outcome_event("KXETH15M-A", is_live=False, strategy="kxeth15m")]

        counts = push_live_pnl(_telemetry_with(events), sheets)

        sheets.upsert_live_pnl.assert_not_called()
        assert counts == {"appended": 0, "updated": 0, "unchanged": 0}

    def test_demotion_cannot_relabel_a_logged_trade(self):
        """
        Selection reads only the persisted placement-time flag, so nothing that
        changes afterwards -- gate level, DRY_RUN -- can alter what is or isn't
        real money in the history.
        """
        ev = _outcome_event("KXBTC15M-A", is_live=True, pnl=2.0)
        tel = _telemetry_with([ev])

        before = build_live_pnl_rows(live_outcomes(tel))
        ev["dry_run"] = True                      # process demoted to paper
        after = build_live_pnl_rows(live_outcomes(tel))

        assert before == after
        assert len(after) == 1


# ── 5. Row building: ordering, cumulative, drift ─────────────────────────────

class TestRowBuilding:
    def test_rows_are_chronological_and_cumulative(self):
        events = [
            _outcome_event("KXBTC15M-C", ts=3.0, pnl=-0.5),
            _outcome_event("KXBTC15M-A", ts=1.0, pnl=2.0),
            _outcome_event("KXBTC15M-B", ts=2.0, pnl=1.0),
        ]

        rows = build_live_pnl_rows(live_outcomes(_telemetry_with(events)))

        assert [r["ticker"] for r in rows] == ["KXBTC15M-A", "KXBTC15M-B", "KXBTC15M-C"]
        assert [r["cumulative_realized_pnl"] for r in rows] == [2.0, 3.0, 2.5]

    def test_duplicate_outcomes_counted_once(self):
        """
        A trade recorded twice (e.g. settlement plus a later backfill) must not
        double-count -- (ticker, side) is one trade.
        """
        events = [
            _outcome_event("KXBTC15M-A", side="yes", ts=1.0, pnl=2.0),
            _outcome_event("KXBTC15M-A", side="yes", ts=5.0, pnl=2.0),
        ]

        rows = build_live_pnl_rows(live_outcomes(_telemetry_with(events)))

        assert len(rows) == 1
        assert rows[0]["cumulative_realized_pnl"] == 2.0

    def test_opposite_sides_are_distinct_trades(self):
        events = [
            _outcome_event("KXBTC15M-A", side="yes", ts=1.0, pnl=2.0),
            _outcome_event("KXBTC15M-A", side="no", ts=2.0, pnl=1.0),
        ]

        rows = build_live_pnl_rows(live_outcomes(_telemetry_with(events)))

        assert len(rows) == 2
        assert rows[-1]["cumulative_realized_pnl"] == 3.0

    def test_drift_column_is_balance_minus_cumulative(self):
        events = [
            _outcome_event("KXBTC15M-A", ts=1.0, pnl=2.0),
            _outcome_event("KXBTC15M-B", ts=2.0, pnl=1.0),
        ]

        rows = build_live_pnl_rows(live_outcomes(_telemetry_with(events)), balance=100.0)

        assert rows[0]["balance_minus_cumulative"] == 98.0
        assert rows[1]["balance_minus_cumulative"] == 97.0

    def test_missing_balance_leaves_cells_blank_not_zero(self):
        rows = build_live_pnl_rows(
            live_outcomes(_telemetry_with([_outcome_event("KXBTC15M-A")])), balance=None
        )
        assert rows[0]["kalshi_balance"] == ""
        assert rows[0]["balance_minus_cumulative"] == ""

    def test_strategy_filter_excludes_other_strategies(self):
        events = [
            _outcome_event("KXBTC15M-A", ts=1.0, strategy="kxbtc15m"),
            _outcome_event("KXETH15M-A", ts=2.0, strategy="kxeth15m"),
        ]

        selected = live_outcomes(_telemetry_with(events), strategy="kxbtc15m")

        assert [o["payload"]["ticker"] for o in selected] == ["KXBTC15M-A"]


# ── 6. Idempotent upsert into the sheet ──────────────────────────────────────

class _FakeWorksheet:
    """A worksheet that actually stores rows, so a second push sees the first."""

    def __init__(self) -> None:
        self.rows: list[list] = []
        self.append_calls = 0

    def get_all_values(self) -> list[list]:
        return [list(r) for r in self.rows]

    def row_values(self, _index: int) -> list:
        return list(self.rows[0]) if self.rows else []

    def update(self, cell: str, values: list[list]) -> None:
        start = int(cell[1:]) - 1
        for offset, row in enumerate(values):
            while len(self.rows) <= start + offset:
                self.rows.append([])
            self.rows[start + offset] = [str(v) for v in row]

    def append_rows(self, rows: list[list], value_input_option: str = "RAW") -> None:
        self.append_calls += 1
        self.rows.extend([str(v) for v in row] for row in rows)

    def append_row(self, row: list, value_input_option: str = "RAW") -> None:
        self.append_rows([row])

    def clear(self) -> None:
        self.rows = []


def _live_client(strategy: str = "kxbtc15m") -> tuple[SheetsClient, _FakeWorksheet]:
    ws = _FakeWorksheet()
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = ws
    return SheetsClient(strategy=strategy, _spreadsheet=spreadsheet), ws


class TestUpsertIdempotency:
    def _rows(self, balance: float | None = 100.0) -> list[dict]:
        events = [
            _outcome_event("KXBTC15M-A", ts=1.0, pnl=2.0),
            _outcome_event("KXBTC15M-B", ts=2.0, pnl=-0.5),
        ]
        return build_live_pnl_rows(live_outcomes(_telemetry_with(events)), balance=balance)

    def test_first_push_appends_every_row(self):
        client, ws = _live_client()

        counts = client.upsert_live_pnl(self._rows())

        assert counts["appended"] == 2
        assert len(ws.rows) == 3   # header + 2 trades

    def test_second_push_appends_nothing(self):
        client, ws = _live_client()
        rows = self._rows()

        client.upsert_live_pnl(rows)
        counts = client.upsert_live_pnl(rows)

        assert counts["appended"] == 0
        assert counts["updated"] == 0
        assert counts["unchanged"] == 2
        assert len(ws.rows) == 3

    def test_running_total_not_inflated_by_repeated_pushes(self):
        client, ws = _live_client()
        rows = self._rows()

        for _ in range(5):
            client.upsert_live_pnl(rows)

        headers = _HEADERS[TAB_LIVE_PNL]
        cum_col = headers.index("cumulative_realized_pnl")
        data = ws.rows[1:]
        assert len(data) == 2
        assert [float(r[cum_col]) for r in data] == [2.0, 1.5]

    def test_new_trade_appends_only_the_new_row(self):
        client, ws = _live_client()
        client.upsert_live_pnl(self._rows())

        events = [
            _outcome_event("KXBTC15M-A", ts=1.0, pnl=2.0),
            _outcome_event("KXBTC15M-B", ts=2.0, pnl=-0.5),
            _outcome_event("KXBTC15M-C", ts=3.0, pnl=1.0),
        ]
        grown = build_live_pnl_rows(live_outcomes(_telemetry_with(events)), balance=100.0)
        counts = client.upsert_live_pnl(grown)

        assert counts["appended"] == 1
        assert len(ws.rows) == 4

    def test_recorded_balance_is_preserved_on_later_pushes(self):
        """
        kalshi_balance is a point-in-time observation that cannot be
        reconstructed later -- a re-push must not overwrite it with today's.
        """
        client, ws = _live_client()
        client.upsert_live_pnl(self._rows(balance=100.0))

        client.upsert_live_pnl(self._rows(balance=250.0))

        bal_col = _HEADERS[TAB_LIVE_PNL].index("kalshi_balance")
        assert [r[bal_col] for r in ws.rows[1:]] == ["100.0", "100.0"]

    def test_header_written_once(self):
        client, ws = _live_client()
        rows = self._rows()

        client.upsert_live_pnl(rows)
        client.upsert_live_pnl(rows)

        assert ws.rows[0] == _HEADERS[TAB_LIVE_PNL]
        assert len(ws.rows) == 3

    def test_empty_rows_is_a_noop(self):
        client, ws = _live_client()
        assert client.upsert_live_pnl([]) == {"appended": 0, "updated": 0, "unchanged": 0}
        assert ws.rows == []

    def test_push_twice_end_to_end_via_push_live_pnl(self):
        """The whole path -- select, build, upsert -- run twice."""
        client, ws = _live_client()
        tel = _telemetry_with([
            _outcome_event("KXBTC15M-A", ts=1.0, pnl=2.0),
            _outcome_event("KXBTC15M-PAPER", ts=2.0, pnl=50.0, is_live=False),
        ])

        push_live_pnl(tel, client, balance=100.0)
        push_live_pnl(tel, client, balance=100.0)

        assert len(ws.rows) == 2   # header + the single real trade
        assert "KXBTC15M-PAPER" not in [r[1] for r in ws.rows[1:]]

    def test_upsert_never_raises_on_api_failure(self):
        spreadsheet = MagicMock()
        spreadsheet.worksheet.side_effect = RuntimeError("quota exceeded")
        spreadsheet.add_worksheet.side_effect = RuntimeError("quota exceeded")
        client = SheetsClient(strategy="kxbtc15m", _spreadsheet=spreadsheet)

        assert client.upsert_live_pnl(self._rows()) == {
            "appended": 0, "updated": 0, "unchanged": 0
        }


# ── 7. Strategy -> tab mapping ───────────────────────────────────────────────

class TestStrategyTabMapping:
    def test_btc_tabs_keep_their_original_names(self):
        assert tab_for(TAB_CALIBRATION, "kxbtc15m") == "Calibration"
        assert tab_for(TAB_PERFORMANCE, "kxbtc15m") == "Performance"

    def test_eth_gets_its_own_tabs(self):
        assert tab_for(TAB_CALIBRATION, "kxeth15m") == "ETH Calibration"
        assert tab_for(TAB_PERFORMANCE, "kxeth15m") == "ETH Performance"

    def test_live_pnl_tabs_are_per_strategy(self):
        assert tab_for(TAB_LIVE_PNL, "kxbtc15m") == "BTC Live P&L"
        assert tab_for(TAB_LIVE_PNL, "kxeth15m") == "ETH Live P&L"

    def test_btc_and_eth_never_share_a_scoped_tab(self):
        for base in (TAB_CALIBRATION, TAB_PERFORMANCE, TAB_LIVE_PNL):
            assert tab_for(base, "kxbtc15m") != tab_for(base, "kxeth15m")

    def test_shared_tabs_are_not_scoped(self):
        assert tab_for(TAB_TRADE_LOG, "kxeth15m") == TAB_TRADE_LOG

    def test_unknown_and_none_strategy_fall_back_to_base_names(self):
        assert tab_for(TAB_CALIBRATION, None) == TAB_CALIBRATION
        assert tab_for(TAB_CALIBRATION, "something_else") == TAB_CALIBRATION

    def test_client_writes_calibration_to_its_scoped_tab(self):
        ws = _FakeWorksheet()
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        client = SheetsClient(strategy="kxeth15m", _spreadsheet=spreadsheet)

        client.push_calibration([{"bucket_label": "0-10%", "count": 5}])

        spreadsheet.worksheet.assert_called_with("ETH Calibration")

    def test_client_writes_live_pnl_to_its_scoped_tab(self):
        ws = _FakeWorksheet()
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        client = SheetsClient(strategy="kxbtc15m", _spreadsheet=spreadsheet)

        client.upsert_live_pnl(
            build_live_pnl_rows(live_outcomes(_telemetry_with([_outcome_event("T1")])))
        )

        spreadsheet.worksheet.assert_called_with("BTC Live P&L")

    def test_for_settings_derives_the_strategy_from_config(self):
        cfg = Settings(strategy_allocations="kxeth15m:1.0")
        assert SheetsClient.for_settings(cfg, _spreadsheet=MagicMock()).strategy == "kxeth15m"

    def test_for_settings_falls_back_when_multi_strategy(self):
        cfg = Settings(strategy_allocations="kxbtc15m:0.5,kxeth15m:0.5")
        assert SheetsClient.for_settings(cfg, _spreadsheet=MagicMock()).strategy is None

    def test_default_client_is_unscoped(self):
        """Existing callers that pass no strategy keep the original tab names."""
        assert SheetsClient(_spreadsheet=MagicMock()).tab(TAB_CALIBRATION) == TAB_CALIBRATION


# ── 8. Create-if-missing tolerates an existing tab ───────────────────────────

class TestTabCreationTolerance:
    def test_existing_tab_is_reused(self):
        ws = _FakeWorksheet()
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        client = SheetsClient(_spreadsheet=spreadsheet)

        assert client._get_or_create_worksheet("Calibration") is ws
        spreadsheet.add_worksheet.assert_not_called()

    def test_concurrent_create_is_not_an_error(self):
        """
        The other deployment can create the tab between our lookup and our
        create. Previously that APIError killed the whole push.
        """
        ws = _FakeWorksheet()
        calls = {"n": 0}

        def _worksheet(title):
            calls["n"] += 1
            if calls["n"] == 1:
                raise gspread.exceptions.WorksheetNotFound(title)
            return ws   # the other process created it in the meantime

        spreadsheet = MagicMock()
        spreadsheet.worksheet.side_effect = _worksheet
        spreadsheet.add_worksheet.side_effect = RuntimeError("already exists")
        client = SheetsClient(_spreadsheet=spreadsheet)

        assert client._get_or_create_worksheet("BTC Live P&L") is ws

    def test_genuine_create_failure_still_raises(self):
        spreadsheet = MagicMock()
        spreadsheet.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("x")
        spreadsheet.add_worksheet.side_effect = RuntimeError("network down")
        client = SheetsClient(_spreadsheet=spreadsheet)

        with pytest.raises(RuntimeError):
            client._get_or_create_worksheet("BTC Live P&L")
