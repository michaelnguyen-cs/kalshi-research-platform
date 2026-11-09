"""
Tests for per-market position tracking (PositionTracker + Executor integration).

Required scenarios:
  a) Second signal on same ticker+side is skipped.
  b) Signal on same ticker but opposite side is allowed.
  c) Position clears correctly after expiry (other tickers unaffected).
  d) Live execution path refuses to re-enter a held position.
"""

from __future__ import annotations

import time

import pytest

import kbot.risk as risk_module
from kbot.config import Settings
from kbot.execution import Executor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.models.types import Side, SizingResult
from kbot.position_tracker import PositionTracker


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg(**kw) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env=kw.get("kalshi_env", "demo"),
        dry_run=kw.get("dry_run", True),
        min_net_edge=kw.get("min_net_edge", 0.04),
    )


def _result(ticker="BTC-2024", side=Side.YES, contracts=5, net_edge=0.05) -> SizingResult:
    return SizingResult(
        ticker=ticker,
        side=side,
        contracts=contracts,
        price_dollars=0.60,
        net_edge_dollars=net_edge,
        kelly_fraction_used=0.1,
    )


@pytest.fixture(autouse=True)
def reset_kill_switch():
    risk_module._KILL_SWITCH_TRIPPED = False
    yield
    risk_module._KILL_SWITCH_TRIPPED = False


def _filled_ack(client_order_id: str, order_id: str, dry_run: bool):
    """
    place_order side_effect simulating a COMPLETE fill at the requested price.

    Orders go out immediate_or_cancel, so an ack with fill_count 0 means
    nothing traded and the executor opens no position -- a fixture that omits
    the fill fields exercises the no-fill path, not the happy one.
    """
    def _place(*, ticker, side, count, price_dollars, **kwargs):
        return OrderAck(
            client_order_id=client_order_id, order_id=order_id, dry_run=dry_run,
            fill_count_units=count * 100, remaining_count_units=0, average_fill_price=price_dollars,
        )
    return _place


@pytest.fixture()
def mock_client(mocker):
    client = mocker.MagicMock(spec=KalshiClient)
    client.place_order.side_effect = _filled_ack("cid-1", "oid-1", True)
    return client


# ── PositionTracker unit tests ────────────────────────────────────────────────

class TestPositionTrackerBasics:
    def test_empty_tracker_not_held(self):
        tracker = PositionTracker()
        assert not tracker.is_held("TICK", Side.YES)
        assert not tracker.is_held("TICK", Side.NO)

    def test_open_position_marks_held(self):
        tracker = PositionTracker()
        tracker.open_position("TICK", Side.YES)
        assert tracker.is_held("TICK", Side.YES)

    def test_open_position_does_not_mark_opposite_side(self):
        tracker = PositionTracker()
        tracker.open_position("TICK", Side.YES)
        assert not tracker.is_held("TICK", Side.NO)

    def test_open_position_does_not_mark_other_ticker(self):
        tracker = PositionTracker()
        tracker.open_position("TICK-A", Side.YES)
        assert not tracker.is_held("TICK-B", Side.YES)

    def test_close_position_clears_held(self):
        tracker = PositionTracker()
        tracker.open_position("TICK", Side.YES)
        tracker.close_position("TICK", Side.YES)
        assert not tracker.is_held("TICK", Side.YES)

    def test_close_position_noop_on_unheld(self):
        tracker = PositionTracker()
        tracker.close_position("TICK", Side.YES)   # must not raise
        assert not tracker.is_held("TICK", Side.YES)

    def test_held_positions_snapshot(self):
        tracker = PositionTracker()
        tracker.open_position("A", Side.YES)
        tracker.open_position("B", Side.NO)
        snap = tracker.held_positions()
        assert ("A", "yes") in snap
        assert ("B", "no")  in snap
        assert len(snap) == 2

    def test_accepts_string_side(self):
        tracker = PositionTracker()
        tracker.open_position("TICK", "yes")
        assert tracker.is_held("TICK", "yes")
        assert tracker.is_held("TICK", Side.YES)    # enum and string are equivalent


# ── (a) Second signal on same ticker+side is skipped ─────────────────────────

class TestDuplicateSignalSkipped:
    def test_executor_blocks_second_entry_same_side(self, mock_client):
        """
        A fresh Executor places the first order; the position is recorded;
        a second call for the same ticker+side is blocked.
        """
        tracker  = PositionTracker()
        executor = Executor(mock_client, _cfg(), tracker=tracker)

        first = executor.execute(_result())
        assert first is not None
        mock_client.place_order.assert_called_once()

        second = executor.execute(_result())
        assert second is None
        # place_order must NOT have been called a second time
        mock_client.place_order.assert_called_once()

    def test_executor_registers_position_after_success(self, mock_client):
        tracker  = PositionTracker()
        executor = Executor(mock_client, _cfg(), tracker=tracker)

        assert not tracker.is_held("BTC-2024", Side.YES)
        executor.execute(_result())
        assert tracker.is_held("BTC-2024", Side.YES)

    def test_pre_populated_tracker_blocks_immediately(self, mock_client):
        """If the tracker already holds the position, the very first call is blocked."""
        tracker = PositionTracker()
        tracker.open_position("BTC-2024", Side.YES)
        executor = Executor(mock_client, _cfg(), tracker=tracker)

        ack = executor.execute(_result())
        assert ack is None
        mock_client.place_order.assert_not_called()


# ── (b) Opposite side on same ticker is allowed ───────────────────────────────

class TestOpposideSideAllowed:
    def test_yes_held_does_not_block_no(self, mock_client):
        """Holding YES on a ticker must not block a NO signal on the same ticker."""
        tracker = PositionTracker()
        tracker.open_position("BTC-2024", Side.YES)
        executor = Executor(mock_client, _cfg(), tracker=tracker)

        ack = executor.execute(_result(side=Side.NO))
        assert ack is not None
        mock_client.place_order.assert_called_once()

    def test_no_held_does_not_block_yes(self, mock_client):
        tracker = PositionTracker()
        tracker.open_position("BTC-2024", Side.NO)
        executor = Executor(mock_client, _cfg(), tracker=tracker)

        ack = executor.execute(_result(side=Side.YES))
        assert ack is not None
        mock_client.place_order.assert_called_once()

    def test_different_ticker_not_blocked(self, mock_client):
        """Holding TICK-A/YES must not block TICK-B/YES."""
        tracker = PositionTracker()
        tracker.open_position("TICK-A", Side.YES)
        executor = Executor(mock_client, _cfg(), tracker=tracker)

        ack = executor.execute(_result(ticker="TICK-B", side=Side.YES))
        assert ack is not None
        mock_client.place_order.assert_called_once()


# ── (c) Position clears after expiry ─────────────────────────────────────────

class TestExpiry:
    def test_expire_clears_position(self):
        tracker = PositionTracker()
        tracker.open_position("TICK", Side.YES)
        tracker.expire("TICK")
        assert not tracker.is_held("TICK", Side.YES)

    def test_expire_clears_both_sides(self):
        tracker = PositionTracker()
        tracker.open_position("TICK", Side.YES)
        tracker.open_position("TICK", Side.NO)
        cleared = tracker.expire("TICK")
        assert set(cleared) == {"yes", "no"}
        assert not tracker.is_held("TICK", Side.YES)
        assert not tracker.is_held("TICK", Side.NO)

    def test_expire_returns_cleared_sides(self):
        tracker = PositionTracker()
        tracker.open_position("TICK", Side.YES)
        cleared = tracker.expire("TICK")
        assert cleared == ["yes"]

    def test_expire_does_not_touch_other_tickers(self):
        tracker = PositionTracker()
        tracker.open_position("TICK-A", Side.YES)
        tracker.open_position("TICK-B", Side.YES)
        tracker.expire("TICK-A")
        assert not tracker.is_held("TICK-A", Side.YES)
        assert tracker.is_held("TICK-B", Side.YES)   # unaffected

    def test_expire_empty_ticker_is_noop(self):
        tracker = PositionTracker()
        cleared = tracker.expire("DOES-NOT-EXIST")
        assert cleared == []

    def test_after_expiry_new_position_allowed(self, mock_client):
        """After expire(), the executor can open a fresh position on that ticker."""
        tracker  = PositionTracker()
        executor = Executor(mock_client, _cfg(), tracker=tracker)

        # First entry
        executor.execute(_result())
        assert tracker.is_held("BTC-2024", Side.YES)

        # Market expires
        tracker.expire("BTC-2024")
        assert not tracker.is_held("BTC-2024", Side.YES)

        # Re-entry is now allowed
        ack = executor.execute(_result())
        assert ack is not None
        assert mock_client.place_order.call_count == 2


# ── (d) Live execution path refuses to re-enter a held position ───────────────

class TestLivePathPositionGuard:
    """
    In live mode (dry_run=False, kalshi_env=prod, live_confirmed=True) the
    position guard must still block duplicate entries.  This test verifies
    that the guard fires BEFORE the live-mode gauntlet is reached — so a
    position check is not accidentally short-circuited by the live checks.
    """

    @pytest.fixture()
    def live_client(self, mocker):
        client = mocker.MagicMock(spec=KalshiClient)
        client.place_order.side_effect = _filled_ack("cid-live", "oid-live", False)
        return client

    def test_live_blocks_duplicate_position(self, live_client):
        cfg     = _cfg(kalshi_env="prod", dry_run=False)
        tracker = PositionTracker()
        tracker.open_position("BTC-2024", Side.YES)
        executor = Executor(live_client, cfg, live_confirmed=True, tracker=tracker)

        ack = executor.execute(_result())
        assert ack is None
        live_client.place_order.assert_not_called()

    def test_live_allows_first_entry(self, live_client):
        cfg      = _cfg(kalshi_env="prod", dry_run=False)
        tracker  = PositionTracker()
        executor = Executor(live_client, cfg, live_confirmed=True, tracker=tracker)

        ack = executor.execute(_result())
        assert ack is not None
        live_client.place_order.assert_called_once()

    def test_live_second_entry_blocked_after_first(self, live_client):
        cfg      = _cfg(kalshi_env="prod", dry_run=False)
        tracker  = PositionTracker()
        executor = Executor(live_client, cfg, live_confirmed=True, tracker=tracker)

        executor.execute(_result())
        live_client.place_order.assert_called_once()

        ack = executor.execute(_result())
        assert ack is None
        # Still only called once
        live_client.place_order.assert_called_once()

    def test_live_opposite_side_still_allowed(self, live_client):
        """YES position held → NO on the same ticker should still go through."""
        cfg      = _cfg(kalshi_env="prod", dry_run=False)
        tracker  = PositionTracker()
        tracker.open_position("BTC-2024", Side.YES)
        executor = Executor(live_client, cfg, live_confirmed=True, tracker=tracker)

        ack = executor.execute(_result(side=Side.NO))
        assert ack is not None
        live_client.place_order.assert_called_once()

    def test_live_position_guard_comment_exists(self):
        """
        Regression guard: verify the mandatory safety comment is present in the
        source code so it cannot be accidentally deleted.
        """
        import inspect
        import kbot.execution as exec_mod
        src = inspect.getsource(exec_mod)
        assert "DO NOT remove this check before going live" in src


# ── Persistence (2026-07: survive a process restart) ─────────────────────────

class TestPersistence:
    def test_default_constructor_is_ephemeral(self):
        """
        Bare PositionTracker() defaults to ":memory:" -- unchanged from the
        original pure in-memory-set behavior, and critically does NOT share
        state with another bare instance (many tests/call sites across the
        codebase construct PositionTracker() with no path and must stay
        isolated from each other).
        """
        a = PositionTracker()
        a.open_position("TICK", Side.YES)

        b = PositionTracker()
        assert not b.is_held("TICK", Side.YES)

    def test_position_survives_across_instances_with_same_db_path(self, tmp_path):
        db_path = tmp_path / "positions.db"
        tracker1 = PositionTracker(db_path=db_path)
        tracker1.open_position("KXBTC-26JUL0317-T48000", Side.YES)
        tracker1.close()

        # A fresh instance pointed at the same file -- simulates a restart.
        tracker2 = PositionTracker(db_path=db_path)
        assert tracker2.is_held("KXBTC-26JUL0317-T48000", Side.YES)
        tracker2.close()

    def test_multiple_positions_survive_restart(self, tmp_path):
        db_path = tmp_path / "positions.db"
        tracker1 = PositionTracker(db_path=db_path)
        tracker1.open_position("A", Side.YES)
        tracker1.open_position("B", Side.NO)
        tracker1.close()

        tracker2 = PositionTracker(db_path=db_path)
        snap = set(tracker2.held_positions())
        assert snap == {("A", "yes"), ("B", "no")}
        tracker2.close()

    def test_close_position_removes_row_before_restart(self, tmp_path):
        db_path = tmp_path / "positions.db"
        tracker1 = PositionTracker(db_path=db_path)
        tracker1.open_position("TICK", Side.YES)
        tracker1.close_position("TICK", Side.YES)
        tracker1.close()

        tracker2 = PositionTracker(db_path=db_path)
        assert not tracker2.is_held("TICK", Side.YES)
        tracker2.close()

    def test_expire_removes_rows_before_restart(self, tmp_path):
        db_path = tmp_path / "positions.db"
        tracker1 = PositionTracker(db_path=db_path)
        tracker1.open_position("TICK", Side.YES)
        tracker1.open_position("TICK", Side.NO)
        tracker1.open_position("OTHER", Side.YES)
        tracker1.expire("TICK")
        tracker1.close()

        tracker2 = PositionTracker(db_path=db_path)
        assert not tracker2.is_held("TICK", Side.YES)
        assert not tracker2.is_held("TICK", Side.NO)
        assert tracker2.is_held("OTHER", Side.YES)  # unaffected
        tracker2.close()

    def test_reopening_same_ticker_side_does_not_duplicate_row(self, tmp_path):
        """
        The scenario from the bug report: a duplicate signal on a contract
        already held. open_position() is idempotent (INSERT OR IGNORE) so a
        stray duplicate call doesn't corrupt the persisted state.
        """
        db_path = tmp_path / "positions.db"
        tracker = PositionTracker(db_path=db_path)
        tracker.open_position("TICK", Side.YES)
        tracker.open_position("TICK", Side.YES)  # duplicate -- must not raise or duplicate the row
        assert tracker.held_positions() == [("TICK", "yes")]
        tracker.close()

    def test_context_manager_closes_connection(self, tmp_path):
        db_path = tmp_path / "positions.db"
        with PositionTracker(db_path=db_path) as tracker:
            tracker.open_position("TICK", Side.YES)
        # Connection is closed; a fresh instance still sees the persisted row.
        tracker2 = PositionTracker(db_path=db_path)
        assert tracker2.is_held("TICK", Side.YES)
        tracker2.close()

    def test_executor_position_guard_survives_tracker_restart(self, mock_client, tmp_path):
        """
        End-to-end: Executor blocks a duplicate signal using a tracker that
        was reloaded from disk, not one that held the position in memory
        the whole time -- this is the actual bug scenario (restart, then a
        duplicate signal on an already-held contract).
        """
        db_path = tmp_path / "positions.db"
        tracker1 = PositionTracker(db_path=db_path)
        tracker1.open_position("BTC-2024", Side.YES)
        tracker1.close()  # simulate process exit

        tracker2 = PositionTracker(db_path=db_path)  # simulate process restart
        executor = Executor(mock_client, _cfg(), tracker=tracker2)

        ack = executor.execute(_result())
        assert ack is None  # blocked -- position was reloaded from disk, not lost on restart
        mock_client.place_order.assert_not_called()
        tracker2.close()


class TestStaleExpiryCleanupOnLoad:
    """
    2026-07: PositionTracker.__init__ can auto-expire positions whose ticker
    has already settled (expiry_provider(ticker) < now), so a restart never
    inherits a stale (ticker, side) guard for a closed window. Positions with
    a future or unknown expiry are left untouched.
    """

    def test_stale_position_expired_on_load(self, tmp_path):
        db_path = tmp_path / "positions.db"
        t1 = PositionTracker(db_path=db_path)
        t1.open_position("SETTLED", Side.YES)
        t1.close()

        # Provider says SETTLED expired an hour ago.
        provider = lambda tk: (time.time() - 3600.0) if tk == "SETTLED" else None
        t2 = PositionTracker(db_path=db_path, expiry_provider=provider)
        assert not t2.is_held("SETTLED", Side.YES)          # cleared
        assert t2.expired_on_load == [("SETTLED", "yes")]
        t2.close()

        # And the DELETE persisted: a third load (no provider) doesn't see it.
        t3 = PositionTracker(db_path=db_path)
        assert not t3.is_held("SETTLED", Side.YES)
        t3.close()

    def test_live_position_kept_on_load(self, tmp_path):
        db_path = tmp_path / "positions.db"
        t1 = PositionTracker(db_path=db_path)
        t1.open_position("LIVE", Side.NO)
        t1.close()

        provider = lambda tk: time.time() + 3600.0  # expires in the future
        t2 = PositionTracker(db_path=db_path, expiry_provider=provider)
        assert t2.is_held("LIVE", Side.NO)                  # not expired -> kept
        assert t2.expired_on_load == []
        t2.close()

    def test_unknown_expiry_position_kept_on_load(self, tmp_path):
        # provider returns None (unparseable / unowned) -> never expire on a guess.
        db_path = tmp_path / "positions.db"
        t1 = PositionTracker(db_path=db_path)
        t1.open_position("MYSTERY", Side.YES)
        t1.close()

        t2 = PositionTracker(db_path=db_path, expiry_provider=lambda tk: None)
        assert t2.is_held("MYSTERY", Side.YES)
        assert t2.expired_on_load == []
        t2.close()

    def test_mixed_positions_only_stale_cleared(self, tmp_path):
        db_path = tmp_path / "positions.db"
        t1 = PositionTracker(db_path=db_path)
        t1.open_position("OLD", Side.YES)
        t1.open_position("NEW", Side.NO)
        t1.open_position("UNKNOWN", Side.YES)
        t1.close()

        now = time.time()
        def provider(tk):
            return {"OLD": now - 60.0, "NEW": now + 60.0, "UNKNOWN": None}[tk]

        t2 = PositionTracker(db_path=db_path, expiry_provider=provider)
        assert not t2.is_held("OLD", Side.YES)          # stale -> cleared
        assert t2.is_held("NEW", Side.NO)               # future -> kept
        assert t2.is_held("UNKNOWN", Side.YES)          # unknown -> kept
        assert t2.expired_on_load == [("OLD", "yes")]
        t2.close()

    def test_no_provider_means_no_cleanup(self, tmp_path):
        # Backward compat: without a provider, nothing is expired on load.
        db_path = tmp_path / "positions.db"
        t1 = PositionTracker(db_path=db_path)
        t1.open_position("SETTLED", Side.YES)
        t1.close()

        t2 = PositionTracker(db_path=db_path)  # no expiry_provider
        assert t2.is_held("SETTLED", Side.YES)
        assert t2.expired_on_load == []
        t2.close()


class TestExposurePersistence:
    """
    2026-07: positions persist contracts/price/driver so PortfolioRisk can be
    reconstructed on restart (see open_positions_with_exposure).
    """

    def test_open_position_persists_exposure_fields(self, tmp_path):
        db_path = tmp_path / "positions.db"
        t = PositionTracker(db_path=db_path)
        t.open_position("KXBTC15M-A", Side.YES, contracts_units=9600, price=0.36, driver="crypto_price")
        rows = t.open_positions_with_exposure()
        assert len(rows) == 1
        r = rows[0]
        assert (r["ticker"], r["side"], r["contracts_units"], r["driver"]) == ("KXBTC15M-A", "yes", 9600, "crypto_price")
        assert r["price"] == pytest.approx(0.36)
        t.close()

    def test_exposure_fields_survive_restart(self, tmp_path):
        db_path = tmp_path / "positions.db"
        t1 = PositionTracker(db_path=db_path)
        t1.open_position("T", Side.NO, contracts_units=5000, price=0.40, driver="crypto_price")
        t1.close()

        t2 = PositionTracker(db_path=db_path)  # restart
        r = t2.open_positions_with_exposure()[0]
        assert r["contracts_units"] == 5000 and r["price"] == pytest.approx(0.40) and r["driver"] == "crypto_price"
        t2.close()

    def test_open_position_defaults_to_zero_exposure(self, tmp_path):
        # Guard-only opens (no exposure args) persist zero -> contribute nothing
        # to reconstruction. Backward compatible with old callers.
        db_path = tmp_path / "positions.db"
        t = PositionTracker(db_path=db_path)
        t.open_position("T", Side.YES)
        r = t.open_positions_with_exposure()[0]
        assert r["contracts_units"] == 0 and r["price"] == 0.0 and r["driver"] == "other"
        t.close()

    def test_expired_stale_position_excluded_from_exposure_rows(self, tmp_path):
        import time
        db_path = tmp_path / "positions.db"
        t1 = PositionTracker(db_path=db_path)
        t1.open_position("SETTLED", Side.YES, contracts_units=1000, price=0.5, driver="crypto_price")
        t1.open_position("LIVE", Side.NO, contracts_units=2000, price=0.5, driver="crypto_price")
        t1.close()

        provider = lambda tk: (time.time() - 60.0) if tk == "SETTLED" else time.time() + 60.0
        t2 = PositionTracker(db_path=db_path, expiry_provider=provider)
        tickers = {r["ticker"] for r in t2.open_positions_with_exposure()}
        assert tickers == {"LIVE"}  # SETTLED was swept before reconstruction would read it
        t2.close()
