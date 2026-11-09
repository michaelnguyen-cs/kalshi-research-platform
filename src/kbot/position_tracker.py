"""
Per-market position tracker — prevents re-entering a position already held.

Tracks open positions as (ticker, side) pairs, mirrored into a SQLite
positions table so the tracker survives a process restart. Thread-safe so
it can be shared between the asyncio paper-trading loop and any synchronous
callers (e.g. a CLI retrain command that runs in a thread).

2026-07: previously in-memory only, which meant a restart reset the tracker
to empty and produced duplicate signals on contracts already held (the
Executor's position guard had nothing to guard against). On construction,
open positions are now loaded from the positions table before the tracker
starts serving is_held() checks -- see __init__ / _load_from_db().

Separate SQLite file (kbot_positions.db), same pattern as kbot_gates.db --
current mutable state, not part of the append-only telemetry event log.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

import structlog

from kbot.quantity import UNITS_PER_CONTRACT

log = structlog.get_logger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS positions (
    ticker    TEXT NOT NULL,
    side      TEXT NOT NULL,
    contracts INTEGER NOT NULL DEFAULT 0,
    -- Authoritative size, in integer hundredths of a contract (kbot.quantity).
    -- `contracts` above is kept as the whole-contract count for older readers;
    -- it cannot represent a fractional fill, which is why this column exists.
    contracts_units INTEGER NOT NULL DEFAULT 0,
    price     REAL NOT NULL DEFAULT 0,
    driver    TEXT NOT NULL DEFAULT 'other',
    is_live   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, side)
);
"""


class PositionTracker:
    """
    Set of open (ticker, side) positions, persisted to SQLite.

    The same instance is shared between the runner's signal loop and the
    Executor so the guard is applied at both layers (defense in depth).

    Parameters
    ----------
    db_path:
        SQLite file path. Defaults to ":memory:" (ephemeral, no on-disk
        footprint, matching this class's original in-memory-only behavior)
        -- callers that want positions to survive a restart (kbot.runner.Runner)
        pass an explicit real path (e.g. "kbot_positions.db", the same
        convention as kbot.gates.GateRegistry's kbot_gates.db).
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        expiry_provider: Callable[[str], float | None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        path = db_path if db_path is not None else ":memory:"
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_CREATE_SQL)
        self._migrate()
        self._conn.commit()
        self._held: set[tuple[str, str]] = self._load_from_db()
        # Positions cleared by the startup stale-position sweep (see
        # _expire_stale_on_load). Exposed for the caller to log / inspect.
        self.expired_on_load: list[tuple[str, str]] = []
        # The same positions with their full contracts/price/driver/is_live, so
        # a settled-but-unrecorded trade can still get an outcome after the
        # restart that swept it. See _expire_stale_on_load.
        self.expired_on_load_rows: list[dict] = []
        if expiry_provider is not None:
            self._expire_stale_on_load(expiry_provider)

    # ── Queries ───────────────────────────────────────────────────────────────

    def is_held(self, ticker: str, side: object) -> bool:
        """Return True if we already have an open position for ticker+side."""
        with self._lock:
            return self._key(ticker, side) in self._held

    def held_positions(self) -> list[tuple[str, str]]:
        """Snapshot of all currently held (ticker, side) pairs."""
        with self._lock:
            return list(self._held)

    def open_positions_with_exposure(self) -> list[dict]:
        """
        All persisted positions with their recorded (contracts_units, price, driver,
        is_live), so a caller can reconstruct dollar exposure after a restart
        (see kbot.runner.Runner -- PortfolioRisk is in-memory and must be
        rebuilt from the persisted positions on startup). Rows written before
        those columns existed (or via the no-exposure open_position overload)
        carry contracts_units=0/price=0 and contribute zero exposure.

        `is_live` is the REAL-MONEY flag as it was at ORDER-PLACEMENT time
        (see open_position). kbot.runner.expire_market snapshots these rows
        before clearing them, so this is the path by which a settled position's
        real-vs-paper status reaches its trade_outcome event -- see
        kbot.outcomes.record_ticker_outcomes. Rows predating the column read
        back False (every trade before it existed was paper).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT ticker, side, contracts_units, price, driver, is_live FROM positions"
            ).fetchall()
        return [
            {"ticker": t, "side": s, "contracts_units": c, "price": p, "driver": d,
             "is_live": bool(live)}
            for (t, s, c, p, d, live) in rows
        ]

    # ── Mutations ─────────────────────────────────────────────────────────────

    def open_position(
        self,
        ticker: str,
        side: object,
        *,
        contracts_units: int = 0,
        price: float = 0.0,
        driver: str = "other",
        is_live: bool = False,
    ) -> None:
        """
        Record that a new position has been opened (in-memory + persisted).

        contracts_units/price/driver are persisted so PortfolioRisk exposure
        can be reconstructed on restart (see open_positions_with_exposure).
        They default to 0/"other" so existing callers that only track the guard
        keep working (they just contribute no exposure on reconstruction).

        `contracts_units` is in integer hundredths of a contract (kbot.quantity)
        -- a partial fill need not be a whole number of contracts. The legacy
        `contracts` column is written alongside it as the floor in whole
        contracts, for readers that predate the units column; nothing in this
        codebase should read it for accounting.

        `is_live` records whether this position was opened with REAL MONEY, as
        determined AT PLACEMENT TIME by what KalshiClient.place_order actually
        did (OrderAck.dry_run) -- not by the process's current DRY_RUN, and not
        by the strategy's current gate level. Both of those are mutable state
        read at some later time: re-running an evaluate, or demoting a strategy
        to paper, would otherwise silently relabel real trades as paper (and
        vice versa) in the P&L accounting. Defaults False so any caller that
        doesn't know stays on the safe side -- an unstamped position is treated
        as paper and never enters real-money P&L.
        """
        key = self._key(ticker, side)
        with self._lock:
            self._held.add(key)
            self._conn.execute(
                "INSERT OR IGNORE INTO positions "
                "(ticker, side, contracts, contracts_units, price, driver, is_live) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key[0], key[1],
                 int(contracts_units) // UNITS_PER_CONTRACT,
                 int(contracts_units),
                 float(price), driver, int(bool(is_live))),
            )
            self._conn.commit()

    def close_position(self, ticker: str, side: object) -> None:
        """Remove a single ticker+side position (in-memory + persisted)."""
        key = self._key(ticker, side)
        with self._lock:
            self._held.discard(key)
            self._conn.execute(
                "DELETE FROM positions WHERE ticker = ? AND side = ?", key
            )
            self._conn.commit()

    def expire(self, ticker: str) -> list[str]:
        """
        Remove all sides for a ticker (market expired / resolved).

        Returns the list of side values that were cleared so the caller can
        log position_closed events for each.
        """
        with self._lock:
            cleared = [s for (t, s) in self._held if t == ticker]
            for side_val in cleared:
                self._held.discard((ticker, side_val))
            self._conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
            self._conn.commit()
            return cleared

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PositionTracker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        """
        Add the exposure columns (contracts/price/driver) and the real-money
        flag (is_live) to a positions table created before they existed.
        CREATE TABLE IF NOT EXISTS won't alter an existing table, so an older
        kbot_positions.db needs them added; the DEFAULTs mean pre-existing rows
        reconstruct to zero exposure.

        is_live DEFAULT 0 is deliberate and must not be backfilled to 1 for
        any row: every position that predates the column was opened before
        real-money trading began, i.e. was paper. Defaulting to 0 keeps paper
        history out of the real-money P&L; defaulting (or backfilling) to 1
        would fabricate real trades that never happened.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(positions)").fetchall()}
        for name, ddl in (
            ("contracts", "ALTER TABLE positions ADD COLUMN contracts INTEGER NOT NULL DEFAULT 0"),
            ("contracts_units",
             "ALTER TABLE positions ADD COLUMN contracts_units INTEGER NOT NULL DEFAULT 0"),
            ("price", "ALTER TABLE positions ADD COLUMN price REAL NOT NULL DEFAULT 0"),
            ("driver", "ALTER TABLE positions ADD COLUMN driver TEXT NOT NULL DEFAULT 'other'"),
            ("is_live", "ALTER TABLE positions ADD COLUMN is_live INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in cols:
                self._conn.execute(ddl)
                if name == "contracts_units":
                    # One-time backfill. Every row written before this column
                    # existed came from the old whole-contract path, so its
                    # size is exactly contracts * UNITS_PER_CONTRACT -- an
                    # exact conversion, not an estimate. Leaving them at 0
                    # would silently zero the exposure of every open position
                    # across the upgrade.
                    self._conn.execute(
                        "UPDATE positions SET contracts_units = contracts * ?",
                        (UNITS_PER_CONTRACT,),
                    )

    def _load_from_db(self) -> set[tuple[str, str]]:
        rows = self._conn.execute("SELECT ticker, side FROM positions").fetchall()
        return {(ticker, side) for ticker, side in rows}

    def _expire_stale_on_load(self, expiry_provider: Callable[[str], float | None]) -> None:
        """
        On construction, drop any loaded position whose ticker's settlement
        time (per `expiry_provider`) is already in the past.

        A market that has settled cannot be re-traded, so a leftover
        (ticker, side) guard entry for it only blocks NEW positions on the
        NEXT window forever -- e.g. after a crash between a fill and the
        window's expiry, the persisted position would otherwise be inherited
        on restart and never cleared. Positions whose expiry is unknown
        (provider returns None -- unparseable or unowned ticker) are left
        untouched: we never expire on a guess, only on a known past expiry.

        Runs once in __init__ before the tracker is shared, so no lock is
        needed. `expired_on_load` records what was cleared as (ticker, side).

        `expired_on_load_rows` records the SAME positions with their full
        contracts_units/price/driver/is_live, captured before the DELETE. Without it
        a restart in the window between a fill and its expiry silently destroyed
        the only record a trade_outcome could be built from -- for a real-money
        fill that means the trade never reaches P&L at all. kbot.runner reads
        these rows on startup and records their outcomes before they are lost.
        """
        now = time.time()
        stale = [
            (ticker, side)
            for (ticker, side) in self._held
            if (exp := expiry_provider(ticker)) is not None and exp < now
        ]
        if stale:
            placeholders = ",".join("(?,?)" for _ in stale)
            params = [v for pair in stale for v in pair]
            self.expired_on_load_rows = [
                {"ticker": t, "side": s, "contracts_units": c, "price": p,
                 "driver": d, "is_live": bool(live)}
                for (t, s, c, p, d, live) in self._conn.execute(
                    "SELECT ticker, side, contracts_units, price, driver, is_live "
                    f"FROM positions WHERE (ticker, side) IN ({placeholders})",
                    params,
                ).fetchall()
            ]
        for ticker, side in stale:
            self._held.discard((ticker, side))
            self._conn.execute(
                "DELETE FROM positions WHERE ticker = ? AND side = ?", (ticker, side)
            )
        if stale:
            self._conn.commit()
            self.expired_on_load = stale
            log.info(
                "position_tracker.expired_stale_on_load",
                count=len(stale),
                positions=stale,
            )

    @staticmethod
    def _key(ticker: str, side: object) -> tuple[str, str]:
        side_val = side.value if hasattr(side, "value") else str(side)
        return (ticker, side_val)
