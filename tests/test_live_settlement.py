"""
Tests for real-money settlement reconciliation.

The property under test throughout: a REAL-MONEY outcome is whatever Kalshi
says it is. The local fee model is an estimate and must never be what a live
trade's P&L is recorded as when the exchange's own figure is available.

The worked example in several tests is the real trade this module was built
for: 7 contracts YES @ $0.45 on KXBTC15M, settled YES, observed as a $97.10 ->
$100.83 balance move (+$3.73).
"""

from __future__ import annotations

import pytest

from kbot.config import Settings
from kbot.live_pnl import live_outcomes
from kbot.live_settlement import (
    find_position,
    net_pnl_dollars,
    reconcile_position,
    settled_result,
)
from kbot.outcomes import (
    build_live_trade_outcome,
    reconcile_live_outcomes,
    record_ticker_outcomes,
)
from kbot.telemetry import Telemetry


def _cfg(**kw) -> Settings:
    return Settings(
        kalshi_key_id="test", dry_run=True,
        fee_coefficient=kw.get("fee_coefficient", 0.07),
    )


# The winning trade, as Kalshi reports it. Cents throughout, realized_pnl
# quoted GROSS of fees (payout 700c - cost 315c = 385c), fees charged 12c.
WINNER = {
    "ticker": "KXBTC15M-26AUG042245-T1",
    "market_result": "yes",
    "realized_pnl": 385,
    "fees_paid": 12,
    "total_cost": 315,
    "position": 0,
}


class _FakeClient:
    """Serves canned Kalshi position + market records."""

    def __init__(self, positions=None, markets=None, fail=False):
        self._positions = positions if positions is not None else [WINNER]
        self._markets = markets or {}
        self._fail = fail
        self.position_calls = 0
        self.market_calls = 0

    def get_positions(self, ticker=None, **kw):
        self.position_calls += 1
        if self._fail:
            raise RuntimeError("kalshi unreachable")
        if ticker is None:
            return list(self._positions)
        return [p for p in self._positions if p.get("ticker") == ticker]

    def get_market(self, ticker):
        self.market_calls += 1
        return self._markets.get(ticker, {})


# ── reading Kalshi's position record ─────────────────────────────────────────

def test_settled_result_reads_market_result():
    assert settled_result({"market_result": "yes"}) is True
    assert settled_result({"market_result": "NO"}) is False


def test_unsettled_market_is_none_not_no():
    """An empty market_result means "not settled yet", never NO."""
    assert settled_result({"market_result": ""}) is None
    assert settled_result({}) is None


def test_find_position_matches_ticker():
    other = {"ticker": "OTHER", "market_result": "no"}
    assert find_position([other, WINNER], WINNER["ticker"]) is WINNER
    assert find_position([other], WINNER["ticker"]) is None


# ── P&L basis detection ──────────────────────────────────────────────────────

def test_gross_realized_pnl_has_fees_subtracted():
    """realized_pnl matching payout-minus-cost is gross; fees still come off."""
    pnl, basis = net_pnl_dollars(WINNER, won=True, contracts=7, fill_price=0.45)
    assert basis == "kalshi_gross_minus_fees"
    assert pnl == pytest.approx(3.73)  # 3.85 - 0.12, the observed balance move


def test_already_net_realized_pnl_is_used_as_is():
    """An exchange quoting realized_pnl net of fees must not be double-charged."""
    net_style = {**WINNER, "realized_pnl": 373}
    pnl, basis = net_pnl_dollars(net_style, won=True, contracts=7, fill_price=0.45)
    assert basis == "kalshi_net"
    assert pnl == pytest.approx(3.73)


def test_ambiguous_basis_errs_toward_under_reporting():
    """Neither reading matches -> subtract fees, never inflate a real total."""
    odd = {**WINNER, "realized_pnl": 900}
    pnl, basis = net_pnl_dollars(odd, won=True, contracts=7, fill_price=0.45)
    assert basis == "kalshi_ambiguous"
    assert pnl == pytest.approx(9.00 - 0.12)


def test_missing_realized_pnl_yields_unavailable():
    """No figure -> caller falls back, rather than record a fabricated zero."""
    pnl, basis = net_pnl_dollars(
        {"fees_paid": 12}, won=True, contracts=7, fill_price=0.45,
    )
    assert pnl is None and basis == "unavailable"


def test_reconcile_position_returns_none_until_settled():
    unsettled = {**WINNER, "market_result": ""}
    assert reconcile_position(
        unsettled, side="yes", contracts=7, fill_price=0.45,
    ) is None


def test_reconcile_marks_a_losing_no_side_correctly():
    """Kalshi's result decides won/lost -- our side only interprets it."""
    rec = reconcile_position(WINNER, side="no", contracts=7, fill_price=0.45)
    assert rec["resolved_yes"] is True
    assert rec["won"] is False


# ── live outcomes use Kalshi, paper outcomes stay local ──────────────────────

def test_live_outcome_pnl_comes_from_kalshi_not_the_fee_model():
    """
    The headline requirement: a live trade's recorded P&L is the exchange's
    realized figure, and the local estimate is kept only for comparison.
    """
    client = _FakeClient(markets={WINNER["ticker"]: {"result": "yes"}})
    payload = build_live_trade_outcome(
        client, WINNER["ticker"], "yes", 7, 0.45,
        {"result": "yes"}, "kxbtc15m", _cfg(),
    )

    assert payload["pnl_source"] == "kalshi_gross_minus_fees"
    assert payload["pnl_dollars"] == pytest.approx(3.73)
    assert payload["fee"] == pytest.approx(0.12)   # charged, not modelled
    assert payload["is_live"] is True
    assert payload["won"] is True
    # The local model's answer is retained but is NOT what got recorded.
    assert payload["local_fee_estimate"] == pytest.approx(0.121275)
    assert payload["local_pnl_estimate"] != payload["pnl_dollars"]


def test_paper_outcome_never_calls_kalshi_and_keeps_local_pnl(tmp_path):
    """Requirement: paper resolution behaviour is unchanged."""
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    client = _FakeClient()

    n = record_ticker_outcomes(
        "KXBTC15M-A",
        [{"side": "yes", "contracts": 7, "price": 0.45, "is_live": False}],
        {"result": "yes"}, tel, "kxbtc15m", _cfg(), client=client,
    )

    assert n == 1
    assert client.position_calls == 0  # the exchange was never consulted
    trade = tel.resolved_paper_trades("kxbtc15m")[0]
    assert trade["pnl_source"] == "local_model"
    assert trade["pnl_dollars"] == pytest.approx(3.728725)  # the modelled figure
    tel.close()


def test_live_position_is_reconciled_against_kalshi(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    client = _FakeClient()

    n = record_ticker_outcomes(
        WINNER["ticker"],
        [{"side": "yes", "contracts": 7, "price": 0.45, "is_live": True}],
        {"result": "yes"}, tel, "kxbtc15m", _cfg(), client=client,
    )

    assert n == 1 and client.position_calls == 1
    outcomes = live_outcomes(tel, strategy="kxbtc15m")
    assert len(outcomes) == 1
    assert outcomes[0]["payload"]["pnl_dollars"] == pytest.approx(3.73)
    tel.close()


def test_live_falls_back_to_local_estimate_when_kalshi_unreachable():
    """A reconciliation outage degrades to an estimate, clearly labelled."""
    client = _FakeClient(fail=True)
    payload = build_live_trade_outcome(
        client, WINNER["ticker"], "yes", 7, 0.45,
        {"result": "yes"}, "kxbtc15m", _cfg(),
    )

    assert payload["pnl_source"] == "local_estimate"
    assert payload["is_live"] is True
    assert payload["pnl_dollars"] == pytest.approx(3.728725)


def test_unsettled_live_market_records_nothing(tmp_path):
    """
    Not settled at Kalshi and not resolvable locally -> no outcome at all, so
    the caller retries instead of recording a guess.
    """
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    client = _FakeClient(positions=[{**WINNER, "market_result": ""}])

    n = record_ticker_outcomes(
        WINNER["ticker"],
        [{"side": "yes", "contracts": 7, "price": 0.45, "is_live": True}],
        {"result": ""}, tel, "kxbtc15m", _cfg(), client=client,
    )

    assert n == 0
    assert tel.recent(event_type="trade_outcome") == []
    tel.close()


# ── the reconcile path ───────────────────────────────────────────────────────

def _live_open(tel, ticker=WINNER["ticker"], side="yes"):
    tel.log_position_open(ticker, side, {
        "strategy": "kxbtc15m", "contracts": 7, "price": 0.45,
        "fill_count": 7, "average_fill_price": 0.45,
        "net_edge": 0.0534, "fair_p": 0.55, "is_live": True,
    })


def test_reconcile_records_the_missing_live_win(tmp_path):
    """The trade that was filled and settled but never reached Live P&L."""
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    _live_open(tel)
    client = _FakeClient(markets={WINNER["ticker"]: {"result": "yes"}})

    summary = reconcile_live_outcomes(client, tel, _cfg())

    assert summary["recorded"] == 1
    assert summary["per_source"] == {"kalshi_gross_minus_fees": 1}
    outcomes = live_outcomes(tel, strategy="kxbtc15m")
    assert len(outcomes) == 1
    assert outcomes[0]["payload"]["pnl_dollars"] == pytest.approx(3.73)
    assert outcomes[0]["payload"]["is_live"] is True
    tel.close()


def test_reconcile_is_idempotent(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    _live_open(tel)
    client = _FakeClient(markets={WINNER["ticker"]: {"result": "yes"}})

    first = reconcile_live_outcomes(client, tel, _cfg())
    second = reconcile_live_outcomes(client, tel, _cfg())
    third = reconcile_live_outcomes(client, tel, _cfg())

    assert first["recorded"] == 1
    assert second["recorded"] == 0 and second["skipped_existing"] == 1
    assert third["recorded"] == 0
    # One outcome, therefore one Live P&L row, however often this is re-run.
    assert len(live_outcomes(tel, strategy="kxbtc15m")) == 1
    tel.close()


def test_reconcile_never_touches_an_existing_outcome(tmp_path):
    """Existing rows are left alone -- this cannot duplicate settled trades."""
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    _live_open(tel)
    tel.log_trade_outcome(WINNER["ticker"], {
        "ticker": WINNER["ticker"], "side": "yes", "contracts": 7,
        "fill_price": 0.45, "pnl_dollars": 3.99, "is_live": True,
        "strategy": "kxbtc15m",
    })

    summary = reconcile_live_outcomes(_FakeClient(), tel, _cfg())

    assert summary["recorded"] == 0 and summary["skipped_existing"] == 1
    outcomes = live_outcomes(tel, strategy="kxbtc15m")
    assert len(outcomes) == 1
    assert outcomes[0]["payload"]["pnl_dollars"] == 3.99  # untouched
    tel.close()


def test_reconcile_ignores_paper_position_opens(tmp_path):
    """Only is_live opens are reconciled; paper never reaches this path."""
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    tel.log_position_open("KXBTC15M-PAPER", "yes", {
        "strategy": "kxbtc15m", "contracts": 7, "price": 0.45, "is_live": False,
    })
    client = _FakeClient()

    summary = reconcile_live_outcomes(client, tel, _cfg())

    assert summary["recorded"] == 0
    assert client.position_calls == 0
    tel.close()


def test_reconcile_reports_but_does_not_repair_mislabelled_by_default(tmp_path):
    """
    A live open whose outcome says paper is surfaced, not silently rewritten:
    flipping is_live is the one edit that could fabricate a real trade.
    """
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    _live_open(tel)
    tel.log_trade_outcome(WINNER["ticker"], {
        "ticker": WINNER["ticker"], "side": "yes", "contracts": 7,
        "fill_price": 0.45, "pnl_dollars": 3.73, "is_live": False,
        "strategy": "kxbtc15m",
    })

    summary = reconcile_live_outcomes(_FakeClient(), tel, _cfg())
    assert summary["mislabelled_paper"] == 1
    assert summary["repaired_is_live"] == 0
    assert live_outcomes(tel, strategy="kxbtc15m") == []

    repaired = reconcile_live_outcomes(_FakeClient(), tel, _cfg(), repair_is_live=True)
    assert repaired["repaired_is_live"] == 1
    rows = live_outcomes(tel, strategy="kxbtc15m")
    assert len(rows) == 1
    assert rows[0]["payload"]["is_live_repaired"] is True
    tel.close()


def test_repair_does_not_duplicate_the_row(tmp_path):
    """Repair edits in place; it must not add a second outcome."""
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    _live_open(tel)
    tel.log_trade_outcome(WINNER["ticker"], {
        "ticker": WINNER["ticker"], "side": "yes", "contracts": 7,
        "fill_price": 0.45, "pnl_dollars": 3.73, "is_live": False,
        "strategy": "kxbtc15m",
    })

    reconcile_live_outcomes(_FakeClient(), tel, _cfg(), repair_is_live=True)
    reconcile_live_outcomes(_FakeClient(), tel, _cfg(), repair_is_live=True)

    assert len(tel.recent(event_type="trade_outcome")) == 1
    assert len(live_outcomes(tel, strategy="kxbtc15m")) == 1
    tel.close()
