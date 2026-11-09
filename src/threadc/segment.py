"""
Segments: independently finalizable archives, and the manifests that describe
them.

One UTC day is one or more SEGMENTS. Normally exactly one, rotated internally at
00:00:00 UTC while the process and its WebSocket connections keep running. A
crash or an operational restart during a day produces a further segment for that
date rather than any rewriting of what is already on disk -- which is the whole
reason segments are numbered rather than named after the day alone.

A segment is in exactly one state:

  OPEN               being written right now
  COMPLETE           finalized cleanly (rotation, capacity-run end, operator
                     signal, or the frozen collection end)
  INCOMPLETE_CRASH   found OPEN by a later process, i.e. its writer died
  HALTED_LOW_DISK    finalized because free space reached the configured reserve

COMPLETE is earned, not asserted. It requires, in order: writes stopped; the
frame queue drained; a TRUNCATE WAL checkpoint; a clean database close; SQLite
PRAGMA integrity_check; SHA-256 of the finished file; and only then the manifest
written with state COMPLETE. If any of those fails the segment does not become
COMPLETE, and the manifest says which step failed.

Immutability. A segment whose manifest is already in a terminal state is never
reopened, rewritten, renamed, relabelled or overwritten -- open_segment() picks
the next unused index instead, and finalize refuses to touch a terminal manifest.

Nothing in a manifest describes data. There are no prices, no depth statistics,
no per-market activity, no returns and no outcomes: byte counts, frame counts,
timestamps, hashes, connection metadata and census status only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from threadc import frozen

log = structlog.get_logger(__name__)

SEGMENTS_SUBDIR = "segments"
SEGMENT_RE = re.compile(r"^threadc-(\d{4}-\d{2}-\d{2})-seg(\d{3})$")

SCHEMA_VERSION = 2

WAL_AUTOCHECKPOINT_PAGES = 4000
JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

CENSUS_CODEC_NONE = 0
CENSUS_CODEC_ZLIB = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per recorder process that wrote into this segment. Normally one; a
-- segment can only ever have one writer, but the row records which process it
-- was so a restart gap is attributable.
CREATE TABLE IF NOT EXISTS recorder_process (
    run_id        TEXT PRIMARY KEY,
    started_ts    REAL NOT NULL,
    ended_ts      REAL,
    git_commit    TEXT NOT NULL,
    mode          TEXT NOT NULL,
    api_env       TEXT NOT NULL,
    base_url      TEXT NOT NULL,
    ws_url        TEXT NOT NULL,
    config        TEXT NOT NULL,
    universe      TEXT NOT NULL,
    doc_reference TEXT NOT NULL
);

-- One row per WebSocket connection epoch, per shard. Enough to tell a
-- reconnect gap from a quiet market when the archive is read later.
CREATE TABLE IF NOT EXISTS ws_session (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    shard_label      TEXT NOT NULL,
    session_epoch    INTEGER NOT NULL,
    use_yes_price    INTEGER NOT NULL,
    channels         TEXT NOT NULL,
    subscribe_params TEXT NOT NULL,
    ws_url           TEXT NOT NULL,
    opened_ts        REAL NOT NULL,
    closed_ts        REAL,
    git_commit       TEXT NOT NULL
);

-- The raw frames, in and out, before any parsing. Identical layout to
-- depthrec's ws_raw so depthrec.rawwriter.RawWriter and
-- depthrec.store.read_raw_text work against this archive unchanged.
CREATE TABLE IF NOT EXISTS ws_raw (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    stream_label  TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    direction     TEXT NOT NULL,
    recv_ts       REAL NOT NULL,
    raw           TEXT,
    codec         INTEGER NOT NULL DEFAULT 0,
    zdict_id      INTEGER,
    payload       BLOB,
    raw_sha256    BLOB,
    raw_len       INTEGER
);

CREATE TABLE IF NOT EXISTS raw_dictionary (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    created_ts     REAL NOT NULL,
    first_frame_id INTEGER,
    frames_sampled INTEGER NOT NULL,
    zdict          BLOB NOT NULL,
    sha256         TEXT NOT NULL
);

-- Connects, disconnects, sequence gaps, shard changes, backpressure, census
-- errors, rotation and disk checks. Operational only.
CREATE TABLE IF NOT EXISTS health_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    stream_label  TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    ts            REAL NOT NULL,
    kind          TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    git_commit    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_health_ts ON health_event(run_id, ts);

-- Every market-census REST call and its response body, compressed. A failure is
-- a row here with ok=0, never a removal from census_market.
CREATE TABLE IF NOT EXISTS census_request (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    ts              REAL NOT NULL,
    pass_kind       TEXT NOT NULL,
    series          TEXT NOT NULL DEFAULT '',
    method          TEXT NOT NULL,
    url             TEXT NOT NULL,
    params          TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    http_status     INTEGER,
    error           TEXT NOT NULL DEFAULT '',
    n_markets       INTEGER,
    response_sha256 TEXT NOT NULL DEFAULT '',
    response_codec  INTEGER NOT NULL DEFAULT 0,
    response_z      BLOB,
    response_len    INTEGER,
    response        TEXT NOT NULL DEFAULT '',
    git_commit      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_census_req_ts ON census_request(run_id, ts);

-- Which eligible markets existed and when they became known to the recorder.
-- Never deleted and never narrowed.
CREATE TABLE IF NOT EXISTS census_market (
    run_id        TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    series        TEXT NOT NULL,
    close_ts      INTEGER,
    event_ticker  TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',
    first_seen_ts REAL NOT NULL,
    last_seen_ts  REAL NOT NULL,
    discovered_by TEXT NOT NULL,
    market_json   TEXT NOT NULL,
    git_commit    TEXT NOT NULL,
    PRIMARY KEY (run_id, ticker)
);
CREATE INDEX IF NOT EXISTS ix_census_close ON census_market(run_id, close_ts);

CREATE TABLE IF NOT EXISTS census_change (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    ts         REAL NOT NULL,
    ticker     TEXT NOT NULL,
    series     TEXT NOT NULL,
    change     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    git_commit TEXT NOT NULL
);

-- Every full-catalog discovery census that ran while this segment was open,
-- COMPLETE or INCOMPLETE, with the universe change it did or did not apply.
CREATE TABLE IF NOT EXISTS discovery_census (
    census_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    started_ts      REAL NOT NULL,
    finished_ts     REAL NOT NULL,
    status          TEXT NOT NULL,
    series_examined INTEGER NOT NULL,
    series_unreadable INTEGER NOT NULL,
    additions       INTEGER NOT NULL,
    removals        INTEGER NOT NULL,
    record          TEXT NOT NULL,
    change          TEXT NOT NULL,
    git_commit      TEXT NOT NULL
);

-- Which markets the recorder subscribed to, on which shard, and when the shard
-- assignment changed. Makes a transient double-subscription during a handoff
-- explicitly detectable downstream.
CREATE TABLE IF NOT EXISTS shard_assignment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ts           REAL NOT NULL,
    ticker       TEXT NOT NULL,
    shard_label  TEXT NOT NULL,
    action       TEXT NOT NULL,
    session_epoch INTEGER,
    detail       TEXT NOT NULL DEFAULT '',
    git_commit   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shard_ts ON shard_assignment(run_id, ts);

-- The explicit displayed state at every subscription establishment.
--
-- The `ticker` and `trade` channels send NOTHING on subscribe: a market whose
-- quotes have not moved and which has not traded stays silent indefinitely. So
-- unlike the orderbook_delta design, "no message yet" carries no information
-- about state, and a reader reconstructing the bank at an arbitrary timestamp
-- would have nothing to start from. This table is that starting point: for each
-- shard, connection epoch and market, the displayed touch state Kalshi's REST
-- market body reported at the moment the subscription was established.
--
-- Append-only, like everything else here. A market subscribed across three
-- connection epochs has three rows, never one row rewritten. The projection
-- keeps Kalshi's own field names and values verbatim -- nothing is renamed,
-- rescaled or derived -- and the complete market body it came from is preserved
-- alongside it in census_market and in the compressed census_request response.
CREATE TABLE IF NOT EXISTS initial_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    ts            REAL NOT NULL,
    shard_label   TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    source        TEXT NOT NULL,
    observed_ts   REAL,
    state_json    TEXT NOT NULL,
    git_commit    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_initial_state
    ON initial_state(run_id, session_epoch, ticker);
"""


def _j(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class SegmentContext:
    """Identity stamped onto every row a segment holds."""

    run_id: str
    git_commit: str


class SegmentStore:
    """
    One segment's SQLite archive.

    Implements exactly the surface depthrec.rawwriter.RawWriter and
    depthrec.census.RestCensus reach for, so both are reused unchanged rather
    than reimplemented -- the frame writer and the census are the two pieces of
    this system that are already proven in production.

    check_same_thread=False plus one RLock, because the writer thread and the
    census worker thread both reach this connection while the event loop keeps
    consuming the WebSocket.
    """

    def __init__(self, path: Path, ctx: SegmentContext) -> None:
        self.path = Path(path)
        self.ctx = ctx
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.closed = False
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
            self.conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT_BYTES}")
            self._exec(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- Locked access --------------------------------------------------------

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def set_meta(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, value if isinstance(value, str) else _j(value)),
        )

    # -- Process and session records ------------------------------------------

    def record_process(self, **kw: Any) -> None:
        self._exec(
            "INSERT OR REPLACE INTO recorder_process(run_id, started_ts, ended_ts,"
            " git_commit, mode, api_env, base_url, ws_url, config, universe, doc_reference)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.ctx.run_id, kw["started_ts"], kw.get("ended_ts"), self.ctx.git_commit,
             kw["mode"], kw["api_env"], kw["base_url"], kw["ws_url"],
             _j(kw["config"]), _j(kw["universe"]), frozen.DOC_REFERENCE),
        )

    def close_process(self, ended_ts: float) -> None:
        self._exec(
            "UPDATE recorder_process SET ended_ts=? WHERE run_id=?",
            (ended_ts, self.ctx.run_id),
        )

    def record_session(self, **kw: Any) -> None:
        self._exec(
            "INSERT INTO ws_session(run_id, shard_label, session_epoch, use_yes_price,"
            " channels, subscribe_params, ws_url, opened_ts, closed_ts, git_commit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.ctx.run_id, kw["shard_label"], kw["session_epoch"],
             int(kw["use_yes_price"]), _j(kw["channels"]), _j(kw.get("subscribe_params") or {}),
             kw["ws_url"], kw["opened_ts"], kw.get("closed_ts"), self.ctx.git_commit),
        )

    def record_shard_assignment(
        self, *, ts: float, ticker: str, shard_label: str, action: str,
        session_epoch: int | None = None, detail: str = "",
    ) -> None:
        self._exec(
            "INSERT INTO shard_assignment(run_id, ts, ticker, shard_label, action,"
            " session_epoch, detail, git_commit) VALUES (?,?,?,?,?,?,?,?)",
            (self.ctx.run_id, ts, ticker, shard_label, action, session_epoch,
             detail[:2000], self.ctx.git_commit),
        )

    # -- Initial state --------------------------------------------------------

    def record_initial_state(self, rows: list[tuple]) -> int:
        """
        Bank one batch of explicit initial states.

        Rows are (ts, shard_label, session_epoch, reason, ticker, source,
        observed_ts, state_json). Batched because an establishment covers the
        whole subscription set at once.
        """
        if not rows:
            return 0
        payload = [
            (self.ctx.run_id, ts, shard, epoch, reason, ticker, source,
             observed_ts, state_json, self.ctx.git_commit)
            for (ts, shard, epoch, reason, ticker, source, observed_ts, state_json) in rows
        ]
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(
                    "INSERT INTO initial_state(run_id, ts, shard_label, session_epoch,"
                    " reason, ticker, source, observed_ts, state_json, git_commit)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    payload,
                )
            finally:
                self.conn.execute("COMMIT")
        return len(payload)

    def initial_state_count(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM initial_state")
        return int(row["n"]) if row else 0

    def initial_state_summary(self) -> dict[str, Any]:
        """
        How many explicit initial states were banked, by reason and epoch.

        Aggregate counts only -- never a ticker, never a price, never a size.
        """
        by_reason = {
            r["reason"]: int(r["n"])
            for r in self.query(
                "SELECT reason, COUNT(*) AS n FROM initial_state WHERE run_id=?"
                " GROUP BY reason", (self.ctx.run_id,)
            )
        }
        epochs = [
            {
                "shard_label": r["shard_label"],
                "session_epoch": int(r["session_epoch"]),
                "markets": int(r["n"]),
                "first_ts": r["first_ts"],
            }
            for r in self.query(
                "SELECT shard_label, session_epoch, COUNT(*) AS n, MIN(ts) AS first_ts"
                " FROM initial_state WHERE run_id=? GROUP BY shard_label, session_epoch"
                " ORDER BY session_epoch", (self.ctx.run_id,)
            )
        ]
        return {"total": sum(by_reason.values()), "by_reason": by_reason,
                "establishments": epochs}

    # -- The RawWriter surface ------------------------------------------------

    _RAW_COLS = (
        "id", "run_id", "stream_label", "session_epoch", "direction", "recv_ts",
        "codec", "zdict_id", "payload", "raw_sha256", "raw_len",
    )

    def insert_raw_batch(self, rows: list[tuple]) -> None:
        if not rows:
            return
        marks = ", ".join("?" * len(self._RAW_COLS))
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(
                    f"INSERT INTO ws_raw({', '.join(self._RAW_COLS)}) VALUES ({marks})", rows
                )
            finally:
                self.conn.execute("COMMIT")

    def max_raw_id(self) -> int:
        row = self.query_one("SELECT COALESCE(MAX(id), 0) AS m FROM ws_raw")
        return int(row["m"]) if row else 0

    def raw_frame_count(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM ws_raw")
        return int(row["n"]) if row else 0

    def raw_bytes(self) -> int:
        row = self.query_one("SELECT COALESCE(SUM(raw_len), 0) AS n FROM ws_raw")
        return int(row["n"]) if row else 0

    def add_raw_dictionary(
        self, zdict: bytes, *, created_ts: float, first_frame_id: int, frames_sampled: int
    ) -> int:
        import hashlib
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO raw_dictionary(run_id, created_ts, first_frame_id,"
                " frames_sampled, zdict, sha256) VALUES (?,?,?,?,?,?)",
                (self.ctx.run_id, created_ts, first_frame_id, frames_sampled,
                 zdict, hashlib.sha256(zdict).hexdigest()),
            )
            return int(cur.lastrowid)

    def raw_dictionaries(self) -> dict[int, bytes]:
        return {r["id"]: bytes(r["zdict"])
                for r in self.query("SELECT id, zdict FROM raw_dictionary")}

    def checkpoint_truncate(self) -> tuple[int, int, int]:
        with self._lock:
            if self.closed:
                return (0, 0, 0)
            row = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)

    # -- Health ---------------------------------------------------------------

    def record_health(
        self, *, stream_label: str, session_epoch: int, ts: float, kind: str,
        reason: str = "", detail: str = "",
    ) -> None:
        self._exec(
            "INSERT INTO health_event(run_id, stream_label, session_epoch, ts, kind,"
            " reason, detail, git_commit) VALUES (?,?,?,?,?,?,?,?)",
            (self.ctx.run_id, stream_label, session_epoch, ts, kind, reason,
             detail[:4000], self.ctx.git_commit),
        )

    def health_counts(self) -> dict[str, int]:
        return {r["kind"]: int(r["n"]) for r in self.query(
            "SELECT kind, COUNT(*) AS n FROM health_event WHERE run_id=? GROUP BY kind",
            (self.ctx.run_id,),
        )}

    # -- The RestCensus surface -----------------------------------------------

    def record_census_request(
        self, *, ts: float, pass_kind: str, series: str, method: str, url: str,
        params: dict[str, Any], ok: bool, http_status: int | None = None,
        error: str = "", n_markets: int | None = None, response_sha256: str = "",
        response: str = "",
    ) -> int:
        body = response.encode("utf-8") if response else b""
        codec, blob = (CENSUS_CODEC_ZLIB, zlib.compress(body, 9)) if body else (0, None)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO census_request(run_id, ts, pass_kind, series, method, url, params,"
                " ok, http_status, error, n_markets, response_sha256, response_codec,"
                " response_z, response_len, git_commit)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.ctx.run_id, ts, pass_kind, series, method, url, _j(params), int(ok),
                 http_status, error[:4000], n_markets, response_sha256, codec,
                 blob, len(body), self.ctx.git_commit),
            )
            return int(cur.lastrowid)

    def upsert_census_market(
        self, *, ts: float, ticker: str, series: str, close_ts: int | None,
        event_ticker: str, status: str, discovered_by: str, market: dict[str, Any],
    ) -> str:
        row = self.query_one(
            "SELECT close_ts, status FROM census_market WHERE run_id=? AND ticker=?",
            (self.ctx.run_id, ticker),
        )
        if row is None:
            self._exec(
                "INSERT INTO census_market(run_id, ticker, series, close_ts, event_ticker,"
                " status, first_seen_ts, last_seen_ts, discovered_by, market_json, git_commit)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.ctx.run_id, ticker, series, close_ts, event_ticker, status, ts, ts,
                 discovered_by, _j(market), self.ctx.git_commit),
            )
            return "new"
        changed = (row["close_ts"] != close_ts) or (row["status"] != status)
        self._exec(
            "UPDATE census_market SET last_seen_ts=?, status=?, close_ts=?,"
            " market_json=? WHERE run_id=? AND ticker=?",
            (ts, status, close_ts, _j(market), self.ctx.run_id, ticker),
        )
        return "updated" if changed else "seen"

    def record_census_change(
        self, *, ts: float, ticker: str, series: str, change: str, detail: str = ""
    ) -> None:
        self._exec(
            "INSERT INTO census_change(run_id, ts, ticker, series, change, detail, git_commit)"
            " VALUES (?,?,?,?,?,?,?)",
            (self.ctx.run_id, ts, ticker, series, change, detail[:2000], self.ctx.git_commit),
        )

    # -- Discovery census -----------------------------------------------------

    def record_discovery_census(self, record: dict[str, Any], change: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO discovery_census(census_id, run_id, started_ts,"
            " finished_ts, status, series_examined, series_unreadable, additions,"
            " removals, record, change, git_commit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["census_id"], self.ctx.run_id, record["started_ts"],
             record["finished_ts"], record["status"], record["series_examined"],
             record["series_unreadable"], len(change.get("added") or []), 0,
             _j(record), _j(change), self.ctx.git_commit),
        )

    def any_census_incomplete(self) -> bool:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM discovery_census WHERE status<>?",
            (frozen.CENSUS_COMPLETE,),
        )
        return bool(row and int(row["n"]))

    def discovery_census_summary(self) -> dict[str, Any]:
        rows = self.query(
            "SELECT status, COUNT(*) AS n FROM discovery_census GROUP BY status"
        )
        return {r["status"]: int(r["n"]) for r in rows}

    def census_request_failures(self) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM census_request WHERE run_id=? AND ok=0",
            (self.ctx.run_id,),
        )
        return int(row["n"]) if row else 0

    # -- Session metadata for the manifest ------------------------------------

    def session_metadata(self) -> dict[str, Any]:
        """
        Enough WS operational metadata to identify gaps: every connection epoch
        per shard, and the health-event tally. No market data of any kind.
        """
        sessions = [
            {
                "shard_label": r["shard_label"],
                "session_epoch": int(r["session_epoch"]),
                "opened_ts": r["opened_ts"],
                "opened_utc": iso(r["opened_ts"]),
                "closed_ts": r["closed_ts"],
                "use_yes_price": bool(r["use_yes_price"]),
                "channels": json.loads(r["channels"]),
                "subscribed_market_count": len(
                    (json.loads(r["subscribe_params"]) or {}).get("market_tickers") or []
                ),
            }
            for r in self.query(
                "SELECT * FROM ws_session WHERE run_id=? ORDER BY opened_ts", (self.ctx.run_id,)
            )
        ]
        health = self.health_counts()
        return {
            "sessions": sessions,
            "shards": sorted({s["shard_label"] for s in sessions}),
            "connection_epochs": len(sessions),
            "reconnects": max(0, len(sessions) - len({s["shard_label"] for s in sessions})),
            "health_event_counts": health,
            "disconnects": health.get("disconnect", 0),
            "sequence_gaps": health.get("sequence_gap", 0),
            "shard_changes": health.get("shard_change", 0),
        }

    # -- Integrity and close --------------------------------------------------

    def integrity_check(self) -> str:
        with self._lock:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            try:
                self.conn.commit()
            finally:
                self.conn.close()
                self.closed = True


# -- Segment files and manifests ---------------------------------------------


class SegmentImmutable(RuntimeError):
    """An attempt to rewrite a segment that is already finalized."""


@dataclass
class Segment:
    """One archive file plus its manifest. The manifest is the record of state."""

    name: str
    utc_date: str
    index: int
    db_path: Path
    manifest_path: Path
    started_ts: float
    run_id: str
    git_commit: str
    mode: str
    channels: tuple[str, ...] = frozen.CHANNELS
    """What the run that opened this segment actually subscribed to.

    Carried on the segment rather than read from threadc.frozen at finalization,
    so a capacity measurement that selected a narrower subset writes its own
    selection into its own manifest. A ticker-only archive whose manifest claimed
    the frozen pair would read, years later, as a two-channel archive in which
    nothing ever traded."""

    state: str = frozen.SEGMENT_OPEN
    store: SegmentStore | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def segments_dir(archive_dir: Path) -> Path:
    return Path(archive_dir) / SEGMENTS_SUBDIR


def _segment_name(utc_date: str, index: int) -> str:
    return f"threadc-{utc_date}-seg{index:03d}"


def utc_date_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def next_utc_midnight(ts: float) -> float:
    """The next 00:00:00 UTC strictly after `ts`."""
    day = datetime.fromtimestamp(ts, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return day.timestamp() + 86400.0


def read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_manifests(archive_dir: Path) -> list[dict[str, Any]]:
    """Every manifest on disk, newest first by start time."""
    out = []
    for p in sorted(segments_dir(archive_dir).glob("*.manifest.json")):
        m = read_manifest(p)
        if m:
            m["_manifest_path"] = str(p)
            out.append(m)
    out.sort(key=lambda m: m.get("utc_start_ts") or 0, reverse=True)
    return out


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def open_segment(
    archive_dir: Path,
    *,
    run_id: str,
    git_commit: str,
    mode: str,
    now: float | None = None,
    universe_identity: dict[str, Any] | None = None,
    channels: tuple[str, ...] | list[str] = frozen.CHANNELS,
) -> Segment:
    """
    Open a new segment for the current UTC day at the next unused index.

    The index is never reused. A day that already has a finalized seg001 gets
    seg002, so an operational restart adds a segment instead of touching one
    that is already on disk and possibly already hash-published.
    """
    now = time.time() if now is None else now
    d = segments_dir(archive_dir)
    d.mkdir(parents=True, exist_ok=True)
    date = utc_date_of(now)

    index = 1
    while (d / f"{_segment_name(date, index)}.sqlite").exists() or (
        d / f"{_segment_name(date, index)}.manifest.json"
    ).exists():
        index += 1

    name = _segment_name(date, index)
    seg = Segment(
        name=name, utc_date=date, index=index,
        db_path=d / f"{name}.sqlite",
        manifest_path=d / f"{name}.manifest.json",
        started_ts=now, run_id=run_id, git_commit=git_commit, mode=mode,
        channels=tuple(channels),
    )
    seg.store = SegmentStore(seg.db_path, SegmentContext(run_id, git_commit))
    seg.store.set_meta("segment_name", name)
    seg.store.set_meta("utc_date", date)
    seg.store.set_meta("run_id", run_id)
    seg.store.set_meta("mode", mode)
    seg.store.set_meta("channels", list(seg.channels))

    _write_manifest(seg.manifest_path, {
        "filename": f"{name}.sqlite",
        "segment_name": name,
        "segment_state": frozen.SEGMENT_OPEN,
        "close_reason": "",
        "utc_date": date,
        "segment_index": index,
        "utc_start": iso(now),
        "utc_start_ts": now,
        "utc_end": None,
        "utc_end_ts": None,
        "run_id": run_id,
        "recorder_git_commit": git_commit,
        "mode": mode,
        "channels": list(seg.channels),
        "doc_reference": frozen.DOC_REFERENCE,
        "schema_version": SCHEMA_VERSION,
        **(universe_identity or {}),
    })
    log.info("threadc.segment.open", segment=name, path=str(seg.db_path), mode=mode)
    return seg


def finalize(
    seg: Segment,
    *,
    state: str,
    close_reason: str,
    universe_identity: dict[str, Any],
    drain_timeout: float = frozen.WRITER_DRAIN_TIMEOUT_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """
    Finalize one segment and write its manifest.

    In order, and COMPLETE only if every step succeeds:
      1. collect the operational metadata while the database is still open
      2. TRUNCATE WAL checkpoint
      3. clean close
      4. SQLite PRAGMA integrity_check (reopened read-only)
      5. SHA-256 of the finished file
      6. manifest written with the resulting state

    The caller is responsible for having stopped the frame writer first --
    finalize must never race a writer that is still inserting.
    """
    now = time.time() if now is None else now
    if state not in frozen.TERMINAL_SEGMENT_STATES:
        raise ValueError(f"{state!r} is not a terminal segment state")

    existing = read_manifest(seg.manifest_path)
    if existing and existing.get("segment_state") in frozen.TERMINAL_SEGMENT_STATES:
        raise SegmentImmutable(
            f"{seg.manifest_path} is already {existing['segment_state']}; a finalized "
            f"segment is never rewritten"
        )

    store = seg.store
    steps: dict[str, Any] = {}
    frames = 0
    raw_bytes = 0
    session_meta: dict[str, Any] = {}
    initial_state: dict[str, Any] = {}
    census_incomplete = False
    census_summary: dict[str, Any] = {}
    census_request_failures = 0

    if store is not None and not store.closed:
        try:
            frames = store.raw_frame_count()
            raw_bytes = store.raw_bytes()
            session_meta = store.session_metadata()
            initial_state = store.initial_state_summary()
            census_incomplete = store.any_census_incomplete()
            census_summary = store.discovery_census_summary()
            census_request_failures = store.census_request_failures()
            store.close_process(now)
            steps["metadata"] = "ok"
        except Exception as exc:  # noqa: BLE001
            steps["metadata"] = f"failed: {type(exc).__name__}: {exc}"
        try:
            store.checkpoint_truncate()
            steps["wal_checkpoint_truncate"] = "ok"
        except Exception as exc:  # noqa: BLE001
            steps["wal_checkpoint_truncate"] = f"failed: {type(exc).__name__}: {exc}"
        try:
            store.close()
            steps["database_close"] = "ok"
        except Exception as exc:  # noqa: BLE001
            steps["database_close"] = f"failed: {type(exc).__name__}: {exc}"
    else:
        steps["metadata"] = "skipped: no open store (orphan recovery)"

    integrity = "not_run"
    try:
        # Reopened read-WRITE deliberately, even though nothing is written. A
        # read-only connection cannot delete the -wal file when it closes, so a
        # read-only integrity check leaves a stray sidecar beside a segment that
        # is supposed to be self-contained -- and the SHA-256 taken next would
        # then cover only half of what is on disk.
        conn = sqlite3.connect(str(seg.db_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = str(row[0]) if row else "unknown"
            if not frames:
                frames = int(conn.execute("SELECT COUNT(*) FROM ws_raw").fetchone()[0])
            if not raw_bytes:
                raw_bytes = int(
                    conn.execute("SELECT COALESCE(SUM(raw_len),0) FROM ws_raw").fetchone()[0]
                )
        finally:
            conn.close()
        steps["integrity_check"] = "ok" if integrity == "ok" else f"result: {integrity}"
    except Exception as exc:  # noqa: BLE001
        integrity = f"failed: {type(exc).__name__}: {exc}"
        steps["integrity_check"] = integrity

    try:
        digest = frozen.sha256_file(seg.db_path)
        size = seg.db_path.stat().st_size
        steps["sha256"] = "ok"
    except Exception as exc:  # noqa: BLE001
        digest, size = "", 0
        steps["sha256"] = f"failed: {type(exc).__name__}: {exc}"

    failed = [k for k, v in steps.items() if isinstance(v, str) and v.startswith("failed")]
    final_state = state
    if state == frozen.SEGMENT_COMPLETE and (failed or integrity != "ok"):
        # COMPLETE is earned. A segment that could not be checkpointed, closed,
        # integrity-checked or hashed is not a normal finished day and must not
        # be labelled as one.
        final_state = frozen.SEGMENT_INCOMPLETE_CRASH
        close_reason = f"{close_reason}; finalization_failed"

    manifest = {
        "filename": f"{seg.name}.sqlite",
        "segment_name": seg.name,
        "segment_state": final_state,
        "close_reason": close_reason,
        "utc_date": seg.utc_date,
        "segment_index": seg.index,
        "utc_start": iso(seg.started_ts),
        "utc_start_ts": seg.started_ts,
        "utc_end": iso(now),
        "utc_end_ts": now,
        "byte_size": size,
        "raw_frame_count": frames,
        "raw_uncompressed_bytes": raw_bytes,
        "sha256": digest,
        "integrity_check": integrity,
        "finalization_steps": steps,
        "run_id": seg.run_id,
        "recorder_git_commit": seg.git_commit,
        "mode": seg.mode,
        "doc_reference": frozen.DOC_REFERENCE,
        "schema_version": SCHEMA_VERSION,
        "ws_operational": session_meta,
        "channels": list(seg.channels),
        "fidelity_doc_reference": frozen.FIDELITY_DOC_REFERENCE,
        "initial_state": initial_state,
        "any_census_incomplete": census_incomplete,
        "discovery_census_counts": census_summary,
        "market_census_request_failures": census_request_failures,
        **universe_identity,
        **seg.extra,
    }
    _write_manifest(seg.manifest_path, manifest)
    seg.state = final_state
    log.info(
        "threadc.segment.finalized", segment=seg.name, state=final_state,
        close_reason=close_reason, frames=frames, bytes=size,
        integrity=integrity, sha256=digest[:16],
    )
    return manifest


def recover_orphans(
    archive_dir: Path,
    *,
    universe_identity: dict[str, Any],
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Classify every segment left OPEN by a process that died.

    Called at startup, before a new segment is opened. An orphan is preserved
    exactly as it is and explicitly labelled INCOMPLETE_CRASH -- never silently
    relabelled as a normal complete day, and never overwritten. Its manifest is
    completed with whatever the file can still tell us: frame count, size,
    integrity check and SHA-256, so the archive stays verifiable even though the
    day it belongs to is not whole.
    """
    exclude = exclude or set()
    recovered = []
    for path in sorted(segments_dir(archive_dir).glob("*.manifest.json")):
        m = read_manifest(path)
        if not m or m.get("segment_state") != frozen.SEGMENT_OPEN:
            continue
        name = m.get("segment_name") or path.name.replace(".manifest.json", "")
        if name in exclude:
            continue
        db_path = path.parent / f"{name}.sqlite"
        if not db_path.exists():
            continue
        seg = Segment(
            name=name, utc_date=m.get("utc_date", ""), index=int(m.get("segment_index", 0)),
            db_path=db_path, manifest_path=path,
            started_ts=float(m.get("utc_start_ts") or 0.0),
            run_id=m.get("run_id", ""), git_commit=m.get("recorder_git_commit", ""),
            mode=m.get("mode", frozen.MODE_PRODUCTION),
            channels=tuple(m.get("channels") or frozen.CHANNELS),
            store=None,
        )
        log.warning("threadc.segment.orphan_found", segment=name, path=str(db_path))
        manifest = finalize(
            seg, state=frozen.SEGMENT_INCOMPLETE_CRASH,
            close_reason=frozen.CLOSE_ORPHAN_FOUND,
            universe_identity=universe_identity,
        )
        recovered.append(manifest)
    return recovered


__all__ = [
    "Segment", "SegmentStore", "SegmentContext", "SegmentImmutable",
    "open_segment", "finalize", "recover_orphans", "list_manifests",
    "read_manifest", "segments_dir", "utc_date_of", "next_utc_midnight", "iso",
]
