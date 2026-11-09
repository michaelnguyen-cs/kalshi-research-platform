"""
Tests for the fill-recording write path and the settle -> is_live -> Live P&L
chain that a real-money trade travels.

Covers the two failures that let a settled, real, winning trade go missing
from money accounting entirely:

  1. The order/position_open events recorded the fill under `contracts`/`price`
     only, so `json_extract(payload,'$.fill_count')` was empty for every order
     ever placed and a partial fill left no record of what was cancelled.
  2. expire_market cleared the position whether or not its outcome had been
     recorded, destroying the only record the trade could be rebuilt from.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from kbot.config import Settings
from kbot.kalshi_client import OrderAck
from kbot.live_pnl import build_live_pnl_rows, live_outcomes
from kbot.models.types import Side
from kbot.outcomes import reconcile_live_outcomes
from kbot.position_tracker import PositionTracker
from kbot.telemetry import Telemetry

from tests.test_live_pnl import _FakeWorksheet, _live_client
from tests.test_live_settlement import WINNER, _FakeClient
from tests.test_runner import (  # noqa: F401
    _FakeStrategy,
    _book,
    _cfg as _runner_cfg,
    _signal,
    chdir_tmp,
)


def _cfg(**kw) -> Settings:
    return Settings(kalshi_key_id="test", dry_run=True, fee_coefficient=0.07, **kw)


# ── 1. Fill fields reach the event payload ───────────────────────────────────

class TestFillFieldsPersist:
    """
    The V2 response's fill_count / average_fill_price must land in the event
    payload under those names -- the names the exchange, the OrderAck and any
    operator querying the events DB all use.
    """

    def _drive_signal(self, mocker, ack: OrderAck, requested: int = 7):
        """
        Run the REAL runner path: strategy emits a signal -> executor returns
        `ack` -> runner writes its order / position_open events.
        """
        from kbot.runner import Runner

        ticker = "KXBTC15M-A"
        signal = _signal(ticker, contracts=requested)
        strategy = _FakeStrategy(
            "kxbtc15m", series_tickers=["KXBTC15M"],
            evaluate_fn=lambda *a, **kw: [signal],
        )
        runner = Runner({"kxbtc15m": strategy}, {"kxbtc15m": 1.0}, _runner_cfg())
        mocker.patch.object(runner._executor, "execute", return_value=ack)
        mocker.patch.object(runner._gates, "get_gate", return_value=None)
        mocker.patch.object(runner, "_gate_check", return_value=None)

        runner._evaluate_ticker("kxbtc15m", strategy, ticker, _book(ticker), None, None)
        return runner

    def test_order_event_carries_fill_count_and_average_fill_price(self, chdir_tmp, mocker):
        ack = OrderAck(
            client_order_id="coid", order_id="oid", dry_run=False,
            fill_count_units=700, remaining_count_units=0, average_fill_price=0.45, raw={},
        )
        runner = self._drive_signal(mocker, ack, requested=7)

        payload = runner._telemetry.recent(event_type="order")[0]["payload"]
        assert payload["fill_count"] == 7
        assert payload["average_fill_price"] == pytest.approx(0.45)
        assert payload["remaining_count"] == 0
        # The pre-existing aliases still carry the same two numbers, so every
        # existing reader (backfill_outcomes, the tracker) is unaffected.
        assert payload["contracts"] == 7
        assert payload["price"] == pytest.approx(0.45)
        runner.close()

    def test_partial_fill_records_what_was_cancelled(self, chdir_tmp, mocker):
        ack = OrderAck(
            client_order_id="coid", order_id="oid", dry_run=False,
            fill_count_units=300, remaining_count_units=700, average_fill_price=0.58, raw={},
        )
        runner = self._drive_signal(mocker, ack, requested=10)

        payload = runner._telemetry.recent(event_type="order")[0]["payload"]
        assert payload["fill_count"] == 3
        assert payload["remaining_count"] == 7
        assert payload["requested_contracts"] == 10
        runner.close()

    def test_position_open_event_carries_the_ack_fill_fields(self, chdir_tmp, mocker):
        ack = OrderAck(
            client_order_id="coid", order_id="oid", dry_run=False,
            fill_count_units=700, remaining_count_units=0, average_fill_price=0.45, raw={},
        )
        runner = self._drive_signal(mocker, ack, requested=7)

        payload = runner._telemetry.recent(event_type="position_open")[0]["payload"]
        assert payload["fill_count"] == 7
        assert payload["average_fill_price"] == pytest.approx(0.45)
        assert payload["is_live"] is True
        runner.close()

    def test_position_open_carries_fill_fields_for_reconciliation(self, tmp_path):
        """
        reconcile_live_outcomes rebuilds from position_open events, so the
        fill must be recoverable from them alone.
        """
        tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
        tel.log_position_open(WINNER["ticker"], "yes", {
            "strategy": "kxbtc15m", "contracts": 7, "price": 0.45,
            "fill_count": 7, "average_fill_price": 0.45, "is_live": True,
        })

        payload = tel.recent(event_type="position_open")[0]["payload"]
        assert payload["fill_count"] == 7
        assert payload["average_fill_price"] == pytest.approx(0.45)
        assert payload["is_live"] is True
        tel.close()


# ── 2. A settled live fill reaches the Live P&L tab ──────────────────────────

class TestSettledLiveFillUpserts:
    """
    The end-to-end shape of the reported bug: four live trades already in the
    tab, a fifth settled winner missing. Reconciling must append exactly one
    row, and re-running must append none.
    """

    def _four_existing(self, tel):
        for i, pnl in enumerate([1.10, -0.80, 2.40, -0.30], start=1):
            tel.log_trade_outcome(f"KXBTC15M-OLD{i}", {
                "ticker": f"KXBTC15M-OLD{i}", "side": "yes", "contracts": 5,
                "fill_price": 0.5, "pnl_dollars": pnl, "is_live": True,
                "strategy": "kxbtc15m",
            })

    def test_missing_winner_appends_exactly_one_row(self, tmp_path):
        tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
        self._four_existing(tel)
        tel.log_position_open(WINNER["ticker"], "yes", {
            "strategy": "kxbtc15m", "contracts": 7, "price": 0.45, "is_live": True,
        })
        sheets, ws = _live_client()

        # The tab as it stands today: four rows, the winner absent.
        sheets.upsert_live_pnl(build_live_pnl_rows(live_outcomes(tel, "kxbtc15m"), balance=97.10))
        assert len(ws.rows) == 5  # header + 4

        reconcile_live_outcomes(
            _FakeClient(markets={WINNER["ticker"]: {"result": "yes"}}), tel, _cfg(),
        )
        counts = sheets.upsert_live_pnl(
            build_live_pnl_rows(live_outcomes(tel, "kxbtc15m"), balance=100.83)
        )

        assert counts["appended"] == 1
        assert counts["unchanged"] == 4
        assert len(ws.rows) == 6
        tel.close()

    def test_rerunning_the_upsert_is_idempotent(self, tmp_path):
        tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
        self._four_existing(tel)
        tel.log_position_open(WINNER["ticker"], "yes", {
            "strategy": "kxbtc15m", "contracts": 7, "price": 0.45, "is_live": True,
        })
        sheets, ws = _live_client()
        client = _FakeClient(markets={WINNER["ticker"]: {"result": "yes"}})

        for _ in range(3):
            reconcile_live_outcomes(client, tel, _cfg())
            counts = sheets.upsert_live_pnl(
                build_live_pnl_rows(live_outcomes(tel, "kxbtc15m"), balance=100.83)
            )

        assert counts["appended"] == 0
        assert counts["unchanged"] == 5
        assert len(ws.rows) == 6  # header + 5, never more
        tel.close()

    def test_cumulative_total_reconciles_to_the_real_balance(self, tmp_path):
        """
        The point of the whole exercise: the running total plus the starting
        balance must land on the balance Kalshi actually reports.
        """
        tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
        tel.log_position_open(WINNER["ticker"], "yes", {
            "strategy": "kxbtc15m", "contracts": 7, "price": 0.45, "is_live": True,
        })
        reconcile_live_outcomes(
            _FakeClient(markets={WINNER["ticker"]: {"result": "yes"}}), tel, _cfg(),
        )

        rows = build_live_pnl_rows(live_outcomes(tel, "kxbtc15m"), balance=100.83)
        assert rows[-1]["cumulative_realized_pnl"] == pytest.approx(3.73)
        assert 97.10 + rows[-1]["cumulative_realized_pnl"] == pytest.approx(100.83)
        tel.close()


# ── 3. An unrecorded position is not destroyed ───────────────────────────────

class TestUnrecordedPositionsSurvive:
    def test_expiry_keeps_the_position_when_no_outcome_was_recorded(self, chdir_tmp, mocker):
        from kbot.runner import Runner

        ticker = "KXBTC15M-26JUL060115-15"
        strategy = _FakeStrategy("a", series_tickers=["KXBTC15M"],
                                 expiries={ticker: time.time() - 60.0})
        runner = Runner({"a": strategy}, {"a": 1.0}, _runner_cfg())
        runner._tracker.open_position(ticker, Side.YES, contracts_units=700, price=0.45, is_live=True)
        # Nothing can resolve it yet: no market, and Kalshi has not settled it.
        mocker.patch.object(runner._client, "get_market", return_value={})
        mocker.patch.object(runner._client, "get_positions", return_value=[])

        runner.expire_market(ticker)

        assert runner._tracker.is_held(ticker, Side.YES), "unrecorded position was destroyed"
        assert ticker in runner._retry_expiries
        runner.close()

    def test_position_is_cleared_once_the_outcome_records(self, chdir_tmp, mocker):
        from kbot.runner import Runner

        ticker = WINNER["ticker"]
        strategy = _FakeStrategy("kxbtc15m", series_tickers=["KXBTC15M"],
                                 expiries={ticker: time.time() - 60.0})
        runner = Runner({"kxbtc15m": strategy}, {"kxbtc15m": 1.0}, _runner_cfg())
        runner._tracker.open_position(ticker, Side.YES, contracts_units=700, price=0.45, is_live=True)
        mocker.patch.object(runner._client, "get_market", return_value={"result": "yes"})
        mocker.patch.object(runner._client, "get_positions", return_value=[WINNER])
        mocker.patch.object(runner, "_push_live_pnl")

        runner.expire_market(ticker)

        assert not runner._tracker.is_held(ticker, Side.YES)
        assert ticker not in runner._retry_expiries
        outcomes = live_outcomes(runner._telemetry, "kxbtc15m")
        assert len(outcomes) == 1
        assert outcomes[0]["payload"]["pnl_dollars"] == pytest.approx(3.73)
        runner.close()

    def test_retry_is_bounded_so_exposure_cannot_wedge(self, chdir_tmp, mocker):
        """
        A market that will never resolve must eventually release, or its driver
        exposure pins the cap shut and blocks all future trading.
        """
        from kbot.runner import Runner

        ticker = "KXBTC15M-26JUL060115-15"
        strategy = _FakeStrategy("a", series_tickers=["KXBTC15M"],
                                 expiries={ticker: time.time() - 60.0})
        runner = Runner({"a": strategy}, {"a": 1.0}, _runner_cfg())
        runner._tracker.open_position(ticker, Side.YES, contracts_units=700, price=0.45, is_live=True)
        mocker.patch.object(runner._client, "get_market", return_value={})
        mocker.patch.object(runner._client, "get_positions", return_value=[])

        for _ in range(Runner._MAX_UNRESOLVED_EXPIRY_ATTEMPTS):
            runner.expire_market(ticker)

        assert not runner._tracker.is_held(ticker, Side.YES)
        assert ticker not in runner._retry_expiries
        runner.close()

    def test_swept_on_load_positions_keep_their_fill_details(self, chdir_tmp):
        """
        The restart case: a position deleted by the startup stale-sweep must
        still be recoverable, with contracts/price/is_live intact.
        """
        ticker = "KXBTC15M-26JUL060115-15"
        seed = PositionTracker(db_path="kbot_positions.db")
        seed.open_position(ticker, Side.YES, contracts_units=700, price=0.45, is_live=True)
        seed.close()

        tracker = PositionTracker(
            db_path="kbot_positions.db",
            expiry_provider=lambda t: time.time() - 3600.0 if t == ticker else None,
        )

        assert tracker.expired_on_load == [(ticker, "yes")]
        assert tracker.expired_on_load_rows == [{
            "ticker": ticker, "side": "yes", "contracts_units": 700, "price": 0.45,
            "driver": "other", "is_live": True,
        }]
        tracker.close()

    def test_restart_records_the_outcome_it_would_have_lost(self, chdir_tmp, mocker):
        from kbot.runner import Runner

        ticker = WINNER["ticker"]
        seed = PositionTracker(db_path="kbot_positions.db")
        seed.open_position(ticker, Side.YES, contracts_units=700, price=0.45, is_live=True)
        seed.close()

        strategy = _FakeStrategy("kxbtc15m", series_tickers=["KXBTC15M"],
                                 expiries={ticker: time.time() - 3600.0})
        mocker.patch(
            "kbot.kalshi_client.KalshiClient.get_market", return_value={"result": "yes"},
        )
        mocker.patch(
            "kbot.kalshi_client.KalshiClient.get_positions", return_value=[WINNER],
        )
        mocker.patch.object(Runner, "_push_live_pnl")

        runner = Runner({"kxbtc15m": strategy}, {"kxbtc15m": 1.0}, _runner_cfg())

        outcomes = live_outcomes(runner._telemetry, "kxbtc15m")
        assert len(outcomes) == 1, "restart lost a settled real-money trade"
        assert outcomes[0]["payload"]["pnl_dollars"] == pytest.approx(3.73)
        runner.close()
