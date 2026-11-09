"""
SQLite event log for orders, fills, and P&L snapshots.

All writes are append-only.  The file is created on first use at the path
set by EVENTS_DB_PATH (default: ./kbot_events.db).

Schema is intentionally flat — one row per event, typed as JSON blobs for
flexibility.  Use the CLI `kbot log` command or any SQLite viewer to query.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    event_type  TEXT    NOT NULL,
    ticker      TEXT,
    dry_run     INTEGER NOT NULL DEFAULT 1,
    payload     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_tick ON events (ticker);

CREATE TABLE IF NOT EXISTS research_digests (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                       REAL    NOT NULL,
    topic                    TEXT    NOT NULL,
    generated_utc            TEXT    NOT NULL,
    summary                  TEXT,
    findings                 TEXT,
    risk_flags               TEXT,
    recommended_human_action TEXT,
    raw_json                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_ts    ON research_digests (ts);
CREATE INDEX IF NOT EXISTS idx_research_topic ON research_digests (topic);

CREATE TABLE IF NOT EXISTS markouts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_event_id TEXT    NOT NULL,
    ticker        TEXT    NOT NULL,
    strategy      TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    fill_price    REAL    NOT NULL,
    mid_at_fill   REAL,
    mid_60s       REAL,
    mid_300s      REAL,
    markout_60s   REAL,
    markout_300s  REAL,
    recorded_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markouts_strategy ON markouts (strategy);
CREATE INDEX IF NOT EXISTS idx_markouts_fill_id  ON markouts (fill_event_id);
"""


class Telemetry:
    """
    Append-only SQLite event log.

    Thread-safe for single-process use (SQLite write serialisation).
    Not safe for concurrent multi-process writes to the same file.
    """

    def __init__(self, cfg: Settings | None = None, db_path: str | Path | None = None) -> None:
        self._cfg = cfg or _default_settings
        # Explicit db_path wins; otherwise cfg.events_db_path (default
        # "kbot_events.db" -- unchanged from when it was hardcoded here). The
        # ETH deployment points this at its own file so its outcomes never
        # enter BTC's retrain corpus -- see Settings.events_db_path.
        path = Path(db_path) if db_path else Path(self._cfg.events_db_path)
        self._db_path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_CREATE_SQL)
        self._migrate()
        self._conn.commit()
        log.info("telemetry.opened", path=str(path))

    def _migrate(self) -> None:
        """
        Add columns introduced after a DB was first created (CREATE TABLE IF
        NOT EXISTS won't alter an existing table -- same pattern as
        kbot.gates.GateRegistry._migrate). mid_at_fill records the book mid
        at fill time so markouts measure post-fill mid DRIFT rather than
        drift-plus-half-spread (see kbot.analytics.adverse_selection); rows
        predating the column stay NULL and keep their fill-price-referenced
        markout values.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(markouts)").fetchall()}
        if "mid_at_fill" not in cols:
            self._conn.execute("ALTER TABLE markouts ADD COLUMN mid_at_fill REAL")

    # ── Write helpers ─────────────────────────────────────────────────────────

    def log_order(self, ticker: str, payload: dict[str, Any]) -> None:
        self._insert("order", ticker=ticker, payload=payload)

    def log_fill(self, ticker: str, payload: dict[str, Any]) -> None:
        self._insert("fill", ticker=ticker, payload=payload)

    def log_pnl(self, payload: dict[str, Any]) -> None:
        self._insert("pnl", ticker=None, payload=payload)

    def log_kill_switch(self, payload: dict[str, Any]) -> None:
        self._insert("kill_switch", ticker=None, payload=payload)

    def log_event(self, event_type: str, payload: dict[str, Any], ticker: str | None = None) -> None:
        self._insert(event_type, ticker=ticker, payload=payload)

    def log_position_open(
        self, ticker: str, side: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Record that a position was opened (enables reconstruction from logs)."""
        self._insert("position_open", ticker=ticker, payload={"side": side, **(payload or {})})

    def log_position_closed(
        self, ticker: str, side: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Record that a position was closed or expired."""
        self._insert("position_closed", ticker=ticker, payload={"side": side, **(payload or {})})

    def log_trade_outcome(self, ticker: str, payload: dict[str, Any]) -> None:
        """
        Record the realized outcome of a closed position.

        Expected payload keys (all optional — store what you have):
          ticker, side, entry_price, model_prob, net_edge_dollars,
          contracts, pnl_dollars, kelly_fraction_used, strategy, fee_dollars
        """
        self._insert("trade_outcome", ticker=ticker, payload=payload)

    def resolved_trades(self, strategy: str, limit: int = 1000) -> list[dict]:
        """
        Resolved trade_outcome payloads for `strategy`, oldest first (so
        `result[-10:]` is "the last 10 resolved trades" — used by RULE R3
        kill-fast, see kbot.gates).
        """
        rows = self._conn.execute(
            "SELECT ts, payload FROM events "
            "WHERE event_type = 'trade_outcome' AND json_extract(payload, '$.strategy') = ? "
            "ORDER BY id DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        trades = [json.loads(r[1]) for r in rows]
        trades.reverse()  # DESC query, newest-first -> reverse to oldest-first
        return trades

    def position_open_field(self, ticker: str, side: str, field: str) -> float | None:
        """
        The named numeric field recorded on the most recent position_open
        event for (ticker, side), or None if absent (older records predate
        the field). Used to carry fill-time model values (fair_p, net_edge,
        raw_fair_p) forward into trade_outcome events so `kbot learn
        --evaluate` and retrain can read them alongside the realized outcome.
        """
        rows = self._conn.execute(
            "SELECT payload FROM events "
            "WHERE event_type = 'position_open' AND ticker = ? "
            "AND json_extract(payload, '$.side') = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticker, side),
        ).fetchall()
        if not rows:
            return None
        val = json.loads(rows[0][0]).get(field)
        return float(val) if val is not None else None

    def position_open_fair_p(self, ticker: str, side: str) -> float | None:
        """The model fair_p from the most recent position_open for (ticker, side)."""
        return self.position_open_field(ticker, side, "fair_p")

    def resolved_paper_trades(self, strategy: str, limit: int = 10_000) -> list[dict]:
        """
        Resolved (settled) PAPER trade_outcome payloads for `strategy`:
        trade_outcome events recorded with dry_run=1. Oldest first.

        This is what the paper-gate promotion check counts and sums P&L over
        (see kbot.gates.check_paper_promotion) -- a resolved paper trade is a
        dry-run order whose market has since settled, which is exactly what a
        trade_outcome event records.
        """
        rows = self._conn.execute(
            "SELECT payload FROM events "
            "WHERE event_type = 'trade_outcome' AND dry_run = 1 "
            "AND json_extract(payload, '$.strategy') = ? "
            "ORDER BY id DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        trades = [json.loads(r[0]) for r in rows]
        trades.reverse()
        return trades

    def log_research_digest(
        self,
        topic: str,
        generated_utc: str,
        summary: str,
        findings: list[dict],
        risk_flags: list[dict],
        recommended_human_action: str,
    ) -> None:
        raw = json.dumps({
            "topic": topic,
            "generated_utc": generated_utc,
            "summary": summary,
            "findings": findings,
            "risk_flags": risk_flags,
            "recommended_human_action": recommended_human_action,
        })
        try:
            self._conn.execute(
                """
                INSERT INTO research_digests
                    (ts, topic, generated_utc, summary, findings,
                     risk_flags, recommended_human_action, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(), topic, generated_utc, summary,
                    json.dumps(findings), json.dumps(risk_flags),
                    recommended_human_action, raw,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.error("telemetry.research_write_error", error=str(exc))

    # ── Adverse-selection markouts ───────────────────────────────────────────

    def log_markout(self, payload: dict[str, Any]) -> None:
        """
        Insert a new markout row (called at fill+60s).

        Required keys: fill_event_id, ticker, strategy, side, fill_price.
        Optional: mid_at_fill, mid_60s, mid_300s, markout_60s, markout_300s.
        Never raises — write errors are logged and swallowed so a recording
        bug can never crash the trading loop.
        """
        try:
            self._conn.execute(
                """
                INSERT INTO markouts
                    (fill_event_id, ticker, strategy, side, fill_price,
                     mid_at_fill, mid_60s, mid_300s, markout_60s, markout_300s, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["fill_event_id"],
                    payload["ticker"],
                    payload["strategy"],
                    payload["side"],
                    payload["fill_price"],
                    payload.get("mid_at_fill"),
                    payload.get("mid_60s"),
                    payload.get("mid_300s"),
                    payload.get("markout_60s"),
                    payload.get("markout_300s"),
                    time.time(),
                ),
            )
            self._conn.commit()
        except (sqlite3.Error, KeyError) as exc:
            log.error("telemetry.markout_write_error", error=str(exc))

    def update_markout_300s(
        self,
        fill_event_id: str,
        mid_300s: float | None,
        markout_300s: float | None,
    ) -> None:
        """Fill in the +300s columns of the most recent row for `fill_event_id`. Never raises."""
        try:
            self._conn.execute(
                "UPDATE markouts SET mid_300s = ?, markout_300s = ? "
                "WHERE id = (SELECT id FROM markouts WHERE fill_event_id = ? "
                "ORDER BY id DESC LIMIT 1)",
                (mid_300s, markout_300s, fill_event_id),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.error("telemetry.markout_update_error", error=str(exc))

    def recent_markouts(self, strategy: str, limit: int = 30) -> list[dict]:
        """Most recent markout rows for `strategy`, newest first."""
        rows = self._conn.execute(
            "SELECT fill_event_id, ticker, strategy, side, fill_price, "
            "mid_at_fill, mid_60s, mid_300s, markout_60s, markout_300s, recorded_at "
            "FROM markouts WHERE strategy = ? ORDER BY id DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        cols = (
            "fill_event_id", "ticker", "strategy", "side", "fill_price",
            "mid_at_fill", "mid_60s", "mid_300s", "markout_60s", "markout_300s", "recorded_at",
        )
        return [dict(zip(cols, r)) for r in rows]

    def distinct_markout_strategies(self) -> list[str]:
        """All strategy names that have at least one markout row."""
        rows = self._conn.execute("SELECT DISTINCT strategy FROM markouts").fetchall()
        return [r[0] for r in rows]

    # ── Query helpers ─────────────────────────────────────────────────────────

    def recent(self, event_type: str | None = None, limit: int = 100) -> list[dict]:
        """Return the most recent events, optionally filtered by type."""
        if event_type:
            rows = self._conn.execute(
                "SELECT ts, event_type, ticker, dry_run, payload "
                "FROM events WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT ts, event_type, ticker, dry_run, payload "
                "FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": r[0],
                "event_type": r[1],
                "ticker": r[2],
                "dry_run": bool(r[3]),
                "payload": json.loads(r[4]),
            }
            for r in rows
        ]

    def recent_types(
        self,
        event_types: list[str] | tuple[str, ...],
        limit: int = 10_000,
    ) -> list[dict]:
        """Return the most recent events matching any of the given event types."""
        if not event_types:
            return []
        placeholders = ",".join("?" * len(event_types))
        rows = self._conn.execute(
            f"SELECT ts, event_type, ticker, dry_run, payload "
            f"FROM events WHERE event_type IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT ?",
            (*event_types, limit),
        ).fetchall()
        return [
            {
                "ts": r[0],
                "event_type": r[1],
                "ticker": r[2],
                "dry_run": bool(r[3]),
                "payload": json.loads(r[4]),
            }
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Telemetry":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _insert(self, event_type: str, *, ticker: str | None, payload: dict[str, Any]) -> None:
        try:
            self._conn.execute(
                "INSERT INTO events (ts, event_type, ticker, dry_run, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    event_type,
                    ticker,
                    int(self._cfg.dry_run),
                    json.dumps(payload),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.error("telemetry.write_error", error=str(exc))
