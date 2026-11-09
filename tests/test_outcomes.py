"""Tests for kbot.outcomes — settled-market resolution + trade-outcome P&L."""

from __future__ import annotations

import pytest

from kbot.config import Settings
from kbot.outcomes import (
    backfill_fair_p,
    backfill_outcomes,
    build_trade_outcome,
    compute_outcome,
    record_ticker_outcomes,
    resolved_yes,
    strategy_for_ticker,
)
from kbot.telemetry import Telemetry


def _cfg(**kw) -> Settings:
    return Settings(kalshi_key_id="test", dry_run=True, fee_coefficient=kw.get("fee_coefficient", 0.07))


# ── resolved_yes ─────────────────────────────────────────────────────────────

def test_resolved_yes_from_result_field():
    assert resolved_yes({"result": "yes"}) is True
    assert resolved_yes({"result": "no"}) is False
    assert resolved_yes({"result": "YES"}) is True  # case-insensitive


def test_resolved_yes_falls_back_to_expiration_value_vs_strike():
    # A real KXBTC15M-shaped market: greater_or_equal, exp_value >= floor -> YES.
    m = {"result": "", "expiration_value": "63962.01", "floor_strike": 63919.37,
         "strike_type": "greater_or_equal"}
    assert resolved_yes(m) is True

    m2 = {"result": "", "expiration_value": "63900.00", "floor_strike": 63919.37,
          "strike_type": "greater_or_equal"}
    assert resolved_yes(m2) is False  # below floor -> NO


def test_resolved_yes_none_when_unsettled():
    assert resolved_yes({"result": "", "expiration_value": ""}) is None
    assert resolved_yes({}) is None


def test_resolved_yes_expiration_value_is_a_price_not_a_payout():
    # Guards the spec correction: expiration_value is the settlement PRICE
    # (~$63k), never 1.0/0.0. Treating it as a payout would resolve wrong.
    m = {"expiration_value": "63962.01", "floor_strike": 63919.37, "strike_type": "greater_or_equal"}
    assert float(m["expiration_value"]) > 1000  # sanity: it's a price
    assert resolved_yes(m) is True


# ── compute_outcome: won/lost, fee, net_pnl ──────────────────────────────────

def test_compute_outcome_yes_won():
    # yes @ 0.36, 96 contracts, market resolved YES.
    out = compute_outcome("yes", 96, 0.36, is_yes_resolved=True, cfg=_cfg())
    assert out["won"] is True
    assert out["gross_pnl"] == pytest.approx(96.0)
    # fee = 0.07 * 96 * 0.36 * 0.64
    assert out["fee"] == pytest.approx(0.07 * 96 * 0.36 * 0.64)
    # net = payout - cost - fee = 96 - (96*0.36) - fee
    assert out["net_pnl"] == pytest.approx(96.0 - 96 * 0.36 - out["fee"])
    assert out["net_pnl"] > 0


def test_compute_outcome_yes_lost():
    out = compute_outcome("yes", 96, 0.36, is_yes_resolved=False, cfg=_cfg())
    assert out["won"] is False
    assert out["gross_pnl"] == 0.0
    # net = -cost - fee  (the entry premium IS lost, not just the fee)
    assert out["net_pnl"] == pytest.approx(-96 * 0.36 - out["fee"])
    assert out["net_pnl"] < 0


def test_compute_outcome_no_side_wins_when_market_resolves_no():
    out = compute_outcome("no", 50, 0.60, is_yes_resolved=False, cfg=_cfg())
    assert out["won"] is True
    assert out["net_pnl"] == pytest.approx(50.0 - 50 * 0.60 - out["fee"])


def test_compute_outcome_no_side_loses_when_market_resolves_yes():
    out = compute_outcome("no", 50, 0.60, is_yes_resolved=True, cfg=_cfg())
    assert out["won"] is False
    assert out["net_pnl"] == pytest.approx(-50 * 0.60 - out["fee"])


def test_compute_outcome_matches_backtest_pnl_convention():
    # Directly mirror kbot.backtest: cost, fee, then payout - cost - fee.
    contracts, fill = 100, 0.32
    cfg = _cfg()
    cost = contracts * fill
    fee = cfg.fee_coefficient * contracts * fill * (1 - fill)
    won = compute_outcome("yes", contracts, fill, True, cfg)
    lost = compute_outcome("yes", contracts, fill, False, cfg)
    assert won["net_pnl"] == pytest.approx(contracts * 1.0 - cost - fee)
    assert lost["net_pnl"] == pytest.approx(-cost - fee)


def test_compute_outcome_respects_fee_coefficient():
    hi = compute_outcome("yes", 10, 0.5, True, _cfg(fee_coefficient=0.10))
    lo = compute_outcome("yes", 10, 0.5, True, _cfg(fee_coefficient=0.05))
    assert hi["fee"] > lo["fee"]


# ── build_trade_outcome ──────────────────────────────────────────────────────

def test_build_trade_outcome_payload_fields():
    market = {"result": "yes", "expiration_value": "63962.01"}
    p = build_trade_outcome("KXBTC15M-X", "yes", 96, 0.36, market, "kxbtc15m", _cfg())
    assert p["ticker"] == "KXBTC15M-X"
    assert p["side"] == "yes" and p["contracts"] == 96 and p["fill_price"] == 0.36
    assert p["expiration_value"] == "63962.01" and p["result"] == "yes"
    assert p["resolved_yes"] is True and p["won"] is True
    assert p["pnl_dollars"] == p["net_pnl"]     # gate/kill-fast field mirrors net_pnl
    assert p["strategy"] == "kxbtc15m"


def test_build_trade_outcome_none_when_unresolved():
    assert build_trade_outcome("T", "yes", 10, 0.5, {"result": ""}, "kxbtc15m", _cfg()) is None


def test_strategy_for_ticker_prefixes():
    assert strategy_for_ticker("KXBTC15M-26JUL100400-00") == "kxbtc15m"
    assert strategy_for_ticker("KXETH15M-26JUL311800-00") == "kxeth15m"
    assert strategy_for_ticker("KXBTC-26JUL0317-B61750") == "btc_hourly"
    assert strategy_for_ticker("KXOTHER-1") is None


# ── record_ticker_outcomes ───────────────────────────────────────────────────

def test_record_ticker_outcomes_logs_events(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    positions = [
        {"side": "yes", "contracts": 96, "price": 0.36},
        {"side": "no", "contracts": 76, "price": 0.65},
    ]
    n = record_ticker_outcomes("KXBTC15M-X", positions, {"result": "yes"}, tel, "kxbtc15m", _cfg())
    assert n == 2
    trades = tel.resolved_paper_trades("kxbtc15m")
    assert len(trades) == 2
    # yes side won (market YES), no side lost.
    by_side = {t["side"]: t for t in trades}
    assert by_side["yes"]["won"] is True and by_side["no"]["won"] is False
    tel.close()


def test_record_ticker_outcomes_skips_unresolved(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    n = record_ticker_outcomes("T", [{"side": "yes", "contracts": 10, "price": 0.5}],
                               {"result": ""}, tel, "kxbtc15m", _cfg())
    assert n == 0
    assert tel.resolved_paper_trades("kxbtc15m") == []
    tel.close()


# ── backfill_outcomes ────────────────────────────────────────────────────────

class _FakeClient:
    """Serves canned settled-market dicts; counts get_market calls."""

    def __init__(self, markets):
        self._markets = markets
        self.calls = 0

    def get_market(self, ticker):
        self.calls += 1
        return self._markets.get(ticker, {})


def test_backfill_records_outcomes_from_position_open_events(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    tel.log_position_open("KXBTC15M-A", "yes", {"strategy": "kxbtc15m", "contracts": 96, "price": 0.36})
    tel.log_position_open("KXBTC15M-A", "no", {"strategy": "kxbtc15m", "contracts": 76, "price": 0.65})

    client = _FakeClient({"KXBTC15M-A": {"result": "yes"}})
    summary = backfill_outcomes(client, tel, _cfg())

    assert summary["recorded"] == 2
    assert summary["per_strategy"] == {"kxbtc15m": 2}
    assert client.calls == 1  # one fetch per unique ticker (cached)
    assert len(tel.resolved_paper_trades("kxbtc15m")) == 2
    tel.close()


def test_backfill_is_idempotent(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    tel.log_position_open("KXBTC15M-A", "yes", {"strategy": "kxbtc15m", "contracts": 10, "price": 0.4})
    client = _FakeClient({"KXBTC15M-A": {"result": "yes"}})

    first = backfill_outcomes(client, tel, _cfg())
    second = backfill_outcomes(client, tel, _cfg())  # re-run

    assert first["recorded"] == 1
    assert second["recorded"] == 0 and second["skipped_existing"] == 1
    assert len(tel.resolved_paper_trades("kxbtc15m")) == 1  # not duplicated
    tel.close()


def test_backfill_infers_strategy_from_ticker_when_missing(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    # strategy field absent -> inferred from the KXBTC15M- prefix.
    tel.log_position_open("KXBTC15M-A", "yes", {"contracts": 10, "price": 0.4})
    client = _FakeClient({"KXBTC15M-A": {"result": "yes"}})
    summary = backfill_outcomes(client, tel, _cfg())
    assert summary["per_strategy"] == {"kxbtc15m": 1}
    tel.close()


def test_backfill_skips_unresolvable_markets(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    tel.log_position_open("KXBTC15M-A", "yes", {"strategy": "kxbtc15m", "contracts": 10, "price": 0.4})
    client = _FakeClient({"KXBTC15M-A": {"result": ""}})  # not settled
    summary = backfill_outcomes(client, tel, _cfg())
    assert summary["recorded"] == 0 and summary["skipped_unresolved"] == 1
    tel.close()


# ── fair_p wiring: position_open -> trade_outcome (2026-07-12) ────────────────

def test_build_trade_outcome_carries_fair_p():
    p = build_trade_outcome("T", "yes", 10, 0.4, {"result": "yes"}, "kxbtc15m", _cfg(), fair_p=0.62)
    assert p["fair_p"] == 0.62
    assert p["model_prob"] == 0.62  # what kbot.learning.evaluate buckets on


def test_build_trade_outcome_omits_fair_p_when_absent():
    p = build_trade_outcome("T", "yes", 10, 0.4, {"result": "yes"}, "kxbtc15m", _cfg())
    assert "fair_p" not in p and "model_prob" not in p


def test_record_ticker_outcomes_looks_up_fair_p_from_position_open(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    # A position_open event carrying fair_p (the live path stores it there).
    tel.log_position_open("KXBTC15M-X", "yes", {"strategy": "kxbtc15m", "contracts": 96, "price": 0.36, "fair_p": 0.58})
    # Tracker positions don't include fair_p -> recorder must join to the event.
    positions = [{"side": "yes", "contracts": 96, "price": 0.36}]
    record_ticker_outcomes("KXBTC15M-X", positions, {"result": "yes"}, tel, "kxbtc15m", _cfg())
    outcome = tel.resolved_paper_trades("kxbtc15m")[0]
    assert outcome["fair_p"] == 0.58
    tel.close()


def test_backfill_carries_fair_p_from_position_open(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    tel.log_position_open("KXBTC15M-A", "yes", {"strategy": "kxbtc15m", "contracts": 10, "price": 0.4, "fair_p": 0.55})
    backfill_outcomes(_FakeClient({"KXBTC15M-A": {"result": "yes"}}), tel, _cfg())
    assert tel.resolved_paper_trades("kxbtc15m")[0]["fair_p"] == 0.55
    tel.close()


def test_position_open_fair_p_lookup(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    tel.log_position_open("T", "yes", {"fair_p": 0.7})
    tel.log_position_open("T", "no", {})  # no fair_p
    assert tel.position_open_fair_p("T", "yes") == pytest.approx(0.7)
    assert tel.position_open_fair_p("T", "no") is None
    assert tel.position_open_fair_p("MISSING", "yes") is None
    tel.close()


def test_backfill_fair_p_updates_existing_outcomes(tmp_path):
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    # A position_open WITH fair_p, and a trade_outcome WITHOUT it (older order).
    tel.log_position_open("KXBTC15M-A", "yes", {"strategy": "kxbtc15m", "contracts": 10, "price": 0.4, "fair_p": 0.61})
    tel.log_trade_outcome("KXBTC15M-A", {"side": "yes", "strategy": "kxbtc15m", "pnl_dollars": 5.0, "won": True})

    summary = backfill_fair_p(tel)

    assert summary["updated"] == 1
    assert tel.resolved_paper_trades("kxbtc15m")[0]["fair_p"] == pytest.approx(0.61)
    # Idempotent: a second run updates nothing.
    assert backfill_fair_p(tel)["updated"] == 0
    tel.close()


def test_backfill_fair_p_leaves_outcomes_without_source_asis(tmp_path):
    # The historical case: position_open has no fair_p -> outcome stays as-is.
    tel = Telemetry(_cfg(), db_path=tmp_path / "e.db")
    tel.log_position_open("KXBTC15M-A", "yes", {"strategy": "kxbtc15m", "contracts": 10, "price": 0.4})  # no fair_p
    tel.log_trade_outcome("KXBTC15M-A", {"side": "yes", "strategy": "kxbtc15m", "pnl_dollars": 5.0})

    summary = backfill_fair_p(tel)

    assert summary["updated"] == 0 and summary["no_source"] == 1
    assert "fair_p" not in tel.resolved_paper_trades("kxbtc15m")[0]
    tel.close()
