"""Tests for kbot.analytics.adverse_selection (Part 3, Phase A2)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import gspread
import pytest

from kbot.analytics import adverse_selection as adv
from kbot.config import Settings
from kbot.execution import Executor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.models.types import Side, SizingResult
from kbot.sheets import TAB_MARKOUT, SheetsClient, _HEADERS
from kbot.telemetry import Telemetry


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        dry_run=kwargs.get("dry_run", True),
        min_net_edge=kwargs.get("min_net_edge", 0.04),
        adverse_selection_window=kwargs.get("adverse_selection_window", 30),
        adverse_selection_edge_multiplier=kwargs.get("adverse_selection_edge_multiplier", 1.5),
    )


@pytest.fixture(autouse=True)
def reset_flags():
    adv.clear_all_flags()
    yield
    adv.clear_all_flags()


@pytest.fixture()
def telemetry(tmp_path) -> Telemetry:
    t = Telemetry(db_path=tmp_path / "events.db")
    yield t
    t.close()


def _seed_markouts(
    telemetry: Telemetry,
    strategy: str,
    markout_values: list[float],
    *,
    ticker: str = "TICK",
    side: Side = Side.YES,
    fill_price: float = 0.50,
) -> None:
    for i, v in enumerate(markout_values):
        telemetry.log_markout({
            "fill_event_id": f"fid-{strategy}-{i}-{time.time_ns()}",
            "ticker": ticker,
            "strategy": strategy,
            "side": side.value,
            "fill_price": fill_price,
            "mid_60s": fill_price + v,
            "markout_60s": v,
        })


# ── 1 & 2. markout() sign correctness ───────────────────────────────────────────

def test_markout_sign_yes_buy_price_up_is_positive():
    m = adv.markout(fill_price=0.60, mid_price=0.65, side=Side.YES)
    assert m == pytest.approx(0.05)
    assert m > 0


def test_markout_sign_no_buy_price_up_is_negative():
    m = adv.markout(fill_price=0.60, mid_price=0.65, side=Side.NO)
    assert m == pytest.approx(-0.05)
    assert m < 0


# ── 3. Flag trips when rolling mean < -0.01 ─────────────────────────────────────

def test_flag_trips_below_threshold(telemetry):
    cfg = _cfg(adverse_selection_window=3)
    _seed_markouts(telemetry, "stratA", [-0.02, -0.015, -0.02])

    flagged = adv.evaluate_flag(telemetry, "stratA", cfg)

    assert flagged is True
    assert adv.is_flagged("stratA") is True


# ── 4. Flag clears when rolling mean recovers above -0.005 ─────────────────────

def test_flag_clears_on_recovery(telemetry):
    cfg = _cfg(adverse_selection_window=3)
    _seed_markouts(telemetry, "stratA", [-0.02, -0.015, -0.02])
    adv.evaluate_flag(telemetry, "stratA", cfg)
    assert adv.is_flagged("stratA") is True

    # Newest 3 fills (window=3) now recover well above the clear threshold.
    _seed_markouts(telemetry, "stratA", [0.01, 0.01, 0.01])
    flagged = adv.evaluate_flag(telemetry, "stratA", cfg)

    assert flagged is False
    assert adv.is_flagged("stratA") is False


def test_flag_dead_band_leaves_state_unchanged(telemetry):
    # Between -0.01 and -0.005 -- neither trips nor clears.
    cfg = _cfg(adverse_selection_window=3)
    _seed_markouts(telemetry, "stratA", [-0.007, -0.007, -0.007])

    flagged = adv.evaluate_flag(telemetry, "stratA", cfg)

    assert flagged is False  # started False, dead band doesn't trip it


# ── 5 & 6. Edge multiplier follows the flag ─────────────────────────────────────

def test_edge_multiplier_applied_when_flagged(telemetry):
    cfg = _cfg(min_net_edge=0.04, adverse_selection_edge_multiplier=1.5, adverse_selection_window=3)
    _seed_markouts(telemetry, "stratA", [-0.02, -0.02, -0.02])
    adv.evaluate_flag(telemetry, "stratA", cfg)
    assert adv.is_flagged("stratA") is True

    assert adv.effective_min_net_edge("stratA", cfg) == pytest.approx(0.06)


def test_edge_multiplier_removed_when_cleared(telemetry):
    cfg = _cfg(min_net_edge=0.04, adverse_selection_edge_multiplier=1.5, adverse_selection_window=3)
    _seed_markouts(telemetry, "stratA", [-0.02, -0.02, -0.02])
    adv.evaluate_flag(telemetry, "stratA", cfg)
    assert adv.effective_min_net_edge("stratA", cfg) == pytest.approx(0.06)

    _seed_markouts(telemetry, "stratA", [0.01, 0.01, 0.01])
    adv.evaluate_flag(telemetry, "stratA", cfg)

    assert adv.is_flagged("stratA") is False
    assert adv.effective_min_net_edge("stratA", cfg) == pytest.approx(0.04)


def test_executor_applies_adverse_selection_multiplier(mocker):
    cfg = _cfg(min_net_edge=0.04, adverse_selection_edge_multiplier=1.5)
    client = mocker.MagicMock(spec=KalshiClient)
    client.place_order.side_effect = lambda *, ticker, side, count, price_dollars, **kw: OrderAck(
        client_order_id="cid", order_id="oid", dry_run=True,
        fill_count_units=count * 100, remaining_count_units=0, average_fill_price=price_dollars,
    )
    executor = Executor(client, cfg)

    result = SizingResult(
        ticker="TICK", side=Side.YES, contracts=5, price_dollars=0.50,
        fair_p=0.55, net_edge_dollars=0.05, kelly_fraction_used=0.1,
    )

    # 0.05 clears the base 0.04 threshold but not the flagged 0.06 threshold.
    adv._set_flag("stratX", True)
    ack = executor.execute(result, strategy="stratX")
    assert ack is None
    client.place_order.assert_not_called()

    adv.clear_all_flags()
    ack2 = executor.execute(result, strategy="stratX")
    assert ack2 is not None
    client.place_order.assert_called_once()


# ── 7. Recording failures never raise into the caller ──────────────────────────

def test_record_markout_60s_failure_does_not_raise(mocker):
    fake_telemetry = mocker.MagicMock()
    fake_telemetry.log_markout.side_effect = RuntimeError("db locked")

    adv.record_markout_60s(
        fake_telemetry,
        fill_event_id="fid-1",
        ticker="TICK",
        strategy="stratX",
        side=Side.YES,
        fill_price=0.50,
        mid_60s=0.55,
    )  # must not raise


def test_record_markout_300s_failure_does_not_raise(mocker):
    fake_telemetry = mocker.MagicMock()
    fake_telemetry.update_markout_300s.side_effect = RuntimeError("db locked")

    adv.record_markout_300s(
        fake_telemetry,
        fill_event_id="fid-1",
        side=Side.YES,
        fill_price=0.50,
        mid_300s=0.55,
    )  # must not raise


def test_schedule_markout_survives_broken_book_provider(telemetry):
    def _bad_provider(ticker: str):
        raise RuntimeError("feed down")

    # Should not raise even though the worker thread's book lookups fail.
    adv.schedule_markout(
        telemetry=telemetry,
        fill_event_id="fid-1",
        ticker="TICK",
        strategy="stratX",
        side=Side.YES,
        fill_price=0.5,
        book_provider=_bad_provider,
        delays=(0.01, 0.02),
    )
    time.sleep(0.15)  # let the daemon thread finish both legs


# ── 8. Minimum sample size before flagging (no false positives) ────────────────

def test_no_false_positive_with_too_few_fills(telemetry):
    cfg = _cfg(adverse_selection_window=5)
    _seed_markouts(telemetry, "stratB", [-0.5, -0.5])  # only 2 of 5 required

    flagged = adv.evaluate_flag(telemetry, "stratB", cfg)

    assert flagged is False
    assert adv.is_flagged("stratB") is False
    # rolling_mean_markout itself still reports the partial mean -- only the
    # flag evaluation enforces the minimum sample size.
    assert adv.rolling_mean_markout(telemetry, "stratB", window=5) == pytest.approx(-0.5)


# ── 9. Markout tab created by ensure_tabs() ─────────────────────────────────────

def _make_worksheet(headers=None) -> MagicMock:
    ws = MagicMock()
    ws.row_values.return_value = headers or []
    ws.get_all_values.return_value = []
    ws.append_row.return_value = {}
    ws.append_rows.return_value = {}
    ws.update.return_value = {}
    ws.clear.return_value = {}
    return ws


def test_markout_tab_created_by_ensure_tabs():
    worksheets = {title: _make_worksheet() for title in _HEADERS}

    def _worksheet(title: str):
        raise gspread.exceptions.WorksheetNotFound(title)

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = _worksheet
    spreadsheet.add_worksheet.side_effect = lambda title, rows, cols: worksheets[title]

    client = SheetsClient(_spreadsheet=spreadsheet)
    client.ensure_tabs()

    created_titles = [c.kwargs.get("title") for c in spreadsheet.add_worksheet.call_args_list]
    assert TAB_MARKOUT in created_titles
    worksheets[TAB_MARKOUT].update.assert_called_once_with("A1", [_HEADERS[TAB_MARKOUT]])


def test_push_markout_summary_writes_rows():
    worksheets = {title: _make_worksheet() for title in _HEADERS}
    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = lambda title: worksheets[title]

    client = SheetsClient(_spreadsheet=spreadsheet)
    client.push_markout_summary([
        {
            "strategy": "stratA",
            "avg_markout_60s": -0.02,
            "pct_negative_60s": 0.7,
            "flag_state": True,
            "sample_size": 30,
            "updated_at_utc": "2026-07-02T00:00:00+00:00",
        }
    ])

    worksheets[TAB_MARKOUT].clear.assert_called_once()
    worksheets[TAB_MARKOUT].update.assert_called_once()


# ── strategy_summary() used by kbot status ──────────────────────────────────────

def test_strategy_summary_flags_from_persisted_history_without_prior_evaluate(telemetry):
    """
    The reported bug: `kbot status` is a fresh process, so _STRATEGY_FLAGS is
    empty and nothing has called evaluate_flag() yet. The summary must still
    report FLAGGED off the persisted markouts rather than "ok".
    """
    cfg = _cfg(adverse_selection_window=30)
    _seed_markouts(telemetry, "kxbtc15m", [-0.0537] * 30)
    assert adv.is_flagged("kxbtc15m") is False  # nothing in memory, as after a restart

    summary = adv.strategy_summary(telemetry, "kxbtc15m", cfg)

    assert summary["flag_state"] is True
    assert summary["sample_size"] == 30
    assert summary["avg_markout_60s"] == pytest.approx(-0.0537)


def test_strategy_summary_stays_ok_on_healthy_persisted_history(telemetry):
    cfg = _cfg(adverse_selection_window=30)
    _seed_markouts(telemetry, "kxbtc15m", [0.02] * 30)

    summary = adv.strategy_summary(telemetry, "kxbtc15m", cfg)

    assert summary["flag_state"] is False
    assert summary["avg_markout_60s"] == pytest.approx(0.02)


def test_strategy_summary_below_window_does_not_trip(telemetry):
    """Under the sample-size floor the flag must stay put, however bad the mean."""
    cfg = _cfg(adverse_selection_window=30)
    _seed_markouts(telemetry, "kxbtc15m", [-0.20] * 5)

    summary = adv.strategy_summary(telemetry, "kxbtc15m", cfg)

    assert summary["flag_state"] is False
    assert summary["sample_size"] == 5


# ── NULL markout over-fetch ─────────────────────────────────────────────────────

def _seed_null_markouts(telemetry: Telemetry, strategy: str, count: int) -> None:
    """Fills whose +60s book snapshot was unavailable: row persists, markout NULL."""
    for i in range(count):
        telemetry.log_markout({
            "fill_event_id": f"null-{strategy}-{i}-{time.time_ns()}",
            "ticker": "TICK", "strategy": strategy, "side": "yes",
            "fill_price": 0.50, "mid_60s": None, "markout_60s": None,
        })


def test_null_markouts_do_not_shrink_sample_below_window(telemetry):
    """The reported jam: book-less fills must not hide the bad history behind them."""
    cfg = _cfg(adverse_selection_window=30)
    _seed_markouts(telemetry, "kxbtc15m", [-0.0537] * 100)
    _seed_null_markouts(telemetry, "kxbtc15m", 3)  # newest rows have no markout

    summary = adv.strategy_summary(telemetry, "kxbtc15m", cfg)

    assert summary["sample_size"] == 30  # was 27 -> under the floor -> never tripped
    assert summary["flag_state"] is True
    assert summary["avg_markout_60s"] == pytest.approx(-0.0537)


def test_over_fetch_takes_the_newest_non_null_values(telemetry):
    """Order is preserved: the newest `window` non-NULL values win, not the oldest."""
    cfg = _cfg(adverse_selection_window=3)
    _seed_markouts(telemetry, "s", [-0.50, -0.50, -0.50])  # old, bad
    _seed_null_markouts(telemetry, "s", 2)
    _seed_markouts(telemetry, "s", [0.02, 0.02, 0.02])      # new, healthy

    summary = adv.strategy_summary(telemetry, "s", cfg)

    assert summary["sample_size"] == 3
    assert summary["avg_markout_60s"] == pytest.approx(0.02)
    assert summary["flag_state"] is False


def test_over_fetch_is_bounded_at_three_windows(telemetry):
    """
    Beyond window*3 rows we stop looking: a sample that gappy is not trustworthy,
    and the bound also stops the mean reaching arbitrarily far back in time.
    """
    cfg = _cfg(adverse_selection_window=3)
    _seed_markouts(telemetry, "s", [-0.0537] * 30)
    _seed_null_markouts(telemetry, "s", 9)  # 9 NULLs = the whole window*3 lookback

    summary = adv.strategy_summary(telemetry, "s", cfg)

    assert summary["sample_size"] == 0
    assert summary["flag_state"] is False  # not enough data -> unchanged, not tripped


def test_partial_nulls_within_over_fetch_still_evaluate(telemetry):
    cfg = _cfg(adverse_selection_window=3)
    _seed_markouts(telemetry, "s", [-0.02] * 3)
    _seed_null_markouts(telemetry, "s", 4)  # inside window*3=9, so we reach past them

    summary = adv.strategy_summary(telemetry, "s", cfg)

    assert summary["sample_size"] == 3
    assert summary["flag_state"] is True


def test_rolling_mean_and_pct_negative_ignore_null_rows(telemetry):
    cfg = _cfg(adverse_selection_window=4)
    _seed_markouts(telemetry, "s", [-0.02, -0.02, 0.02, 0.02])
    _seed_null_markouts(telemetry, "s", 2)

    assert adv.rolling_mean_markout(telemetry, "s", window=4) == pytest.approx(0.0)
    assert adv.pct_negative(telemetry, "s", window=4) == pytest.approx(0.5)


# ── restore_flags_from_history() — startup warm-up ──────────────────────────────

def test_restore_flags_from_history_seeds_flag_after_restart(telemetry):
    cfg = _cfg(adverse_selection_window=30)
    _seed_markouts(telemetry, "kxbtc15m", [-0.0537] * 30)
    _seed_markouts(telemetry, "healthy", [0.03] * 30)

    restored = adv.restore_flags_from_history(telemetry, ["kxbtc15m", "healthy"], cfg)

    assert restored == {"kxbtc15m": True, "healthy": False}
    assert adv.is_flagged("kxbtc15m") is True
    assert adv.is_flagged("healthy") is False


def test_restore_flags_applies_edge_multiplier_immediately(telemetry):
    """The point of the warm-up: the edge gate is stricter from the first eval."""
    cfg = _cfg(adverse_selection_window=30, min_net_edge=0.04,
               adverse_selection_edge_multiplier=1.5)
    _seed_markouts(telemetry, "kxbtc15m", [-0.0537] * 30)

    assert adv.effective_min_net_edge("kxbtc15m", cfg) == pytest.approx(0.04)  # stale: unprotected
    adv.restore_flags_from_history(telemetry, ["kxbtc15m"], cfg)
    assert adv.effective_min_net_edge("kxbtc15m", cfg) == pytest.approx(0.06)  # 0.04 * 1.5


def test_restore_flags_never_raises_on_telemetry_error(telemetry, mocker):
    """An analytics failure must not stop the engine from starting."""
    cfg = _cfg(adverse_selection_window=30)
    mocker.patch.object(
        telemetry, "recent_markouts", side_effect=RuntimeError("db is gone")
    )

    restored = adv.restore_flags_from_history(telemetry, ["kxbtc15m"], cfg)

    assert restored == {}
    assert adv.is_flagged("kxbtc15m") is False


def test_restore_flags_with_no_history_is_a_noop(telemetry):
    cfg = _cfg(adverse_selection_window=30)

    restored = adv.restore_flags_from_history(telemetry, ["never-traded"], cfg)

    assert restored == {"never-traded": False}


def test_strategy_summary_reports_flag_and_stats(telemetry):
    cfg = _cfg(adverse_selection_window=3)
    _seed_markouts(telemetry, "stratA", [-0.02, -0.02, -0.02])
    adv.evaluate_flag(telemetry, "stratA", cfg)

    summary = adv.strategy_summary(telemetry, "stratA", cfg)

    assert summary["flag_state"] is True
    assert summary["sample_size"] == 3
    assert summary["avg_markout_60s"] == pytest.approx(-0.02)
    assert summary["pct_negative_60s"] == pytest.approx(1.0)
