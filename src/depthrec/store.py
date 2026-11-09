"""
Persistence. One SQLite file per recording run; append-only in practice.

Section 7 requires every persisted record to carry the recorder's Git commit and
requires the RAW WebSocket message to be retained alongside anything derived
from it -- "a derived `ask=..., size=...` row alone is not sufficient", because
this project has hit book-convention bugs three separate times. So `ws_raw`
holds the untouched frame text and every derived row points back into it by id.
An independently written decoder can be run over `ws_raw` alone and its output
compared against `signal_observation` without trusting a line of this package.

SQLite rather than JSONL because four different readers (census, classifier,
book observations, equivalence) need to be joined by ticker and time, and
because the repo already runs several sqlite stores. WAL mode, synchronous
NORMAL: a recorder that blocks on fsync for every delta will fall behind the
feed, and losing the last few frames of a crashed process is a health event the
recorder already records rather than a silent data corruption.

Nothing here is ever UPDATEd except a census row's last-seen bookkeeping. A
discovery failure never deletes a market: section 4 counts missing state as
UNAVAILABLE, and a recorder that erases its own denominator would inflate every
rate it reports.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 6

WAL_AUTOCHECKPOINT_PAGES = 4000
"""16 MiB at the 4 KiB page size. Larger than the 1000-page default so a
high-rate feed is not checkpointing constantly, small enough to bound the
backlog."""

JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
"""Hard cap on the WAL file after any checkpoint."""

CENSUS_CODEC_NONE = 0
CENSUS_CODEC_ZLIB = 1
"""Census response bodies use plain zlib -- no preset dictionary. They are
hundreds of kilobytes each, so DEFLATE builds its own window and reaches 28x;
the dictionary trick only matters for the 262-byte WebSocket frames."""

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per recorder process start. Section 6: subscription parameters, API
-- host and the recorder Git commit are logged at startup.
CREATE TABLE IF NOT EXISTS recorder_run (
    run_id            TEXT PRIMARY KEY,
    started_ts        REAL NOT NULL,
    ended_ts          REAL,
    git_commit        TEXT NOT NULL,
    api_env           TEXT NOT NULL,
    base_url          TEXT NOT NULL,
    ws_url            TEXT NOT NULL,
    frozen_config     TEXT NOT NULL,
    integrity         TEXT NOT NULL,
    doc_reference     TEXT NOT NULL,
    notes             TEXT NOT NULL DEFAULT ''
);

-- One row per WebSocket connection attempt that reached the subscribe step.
CREATE TABLE IF NOT EXISTS ws_session (
    session_id        TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    stream_label      TEXT NOT NULL,
    session_epoch     INTEGER NOT NULL,
    use_yes_price     INTEGER NOT NULL,
    channels          TEXT NOT NULL,
    subscribe_params  TEXT NOT NULL,
    ws_url            TEXT NOT NULL,
    opened_ts         REAL NOT NULL,
    closed_ts         REAL,
    git_commit        TEXT NOT NULL
);

-- Raw frames, in and out, before any parsing. The replay substrate.
--
-- Stored COMPRESSED (see depthrec.rawcodec). `payload` holds the codec's bytes;
-- `raw_sha256` is over the UNCOMPRESSED frame, so a reader can prove
-- decompression reproduced the original byte for byte -- which is the thing
-- section 7 actually promises to have kept. `codec` is per row, so a warm-up
-- frame written before the run's dictionary existed sits happily beside a
-- compressed one. `raw` remains for databases written before schema 3 and for
-- runs recorded with compression disabled.
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
-- Deliberately NO index on ws_raw. Measured at 660,961 rows, an index on
-- (run_id, recv_ts) cost roughly 40 bytes per row against a 77-byte compressed
-- payload. Replay reads the table in `id` order, which is the rowid and free;
-- the offline reporter touches ws_raw once per run. A time index can be built
-- on a finished archive in seconds if some later analysis wants one, and the
-- three-day pilot does not have the disk to carry it speculatively.

-- Compression dictionaries, versioned. Frames reference the dictionary they
-- were encoded against, so refreshing one never strands older frames -- and the
-- archive still decodes with nothing but this file.
CREATE TABLE IF NOT EXISTS raw_dictionary (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    created_ts     REAL NOT NULL,
    first_frame_id INTEGER,
    frames_sampled INTEGER NOT NULL,
    zdict          BLOB NOT NULL,
    sha256         TEXT NOT NULL
);

-- Connects, disconnects, sequence gaps, restarts, decode failures.
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

-- Every REST call the independent census made, and what came back. A failure
-- is a row here, not a silent removal from census_market.
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
    -- The response body, COMPRESSED. Measured on a 12-minute universe run:
    -- 146 responses averaging 685 KB came to 100 MB, 61% of the whole database
    -- and far more than every WebSocket frame combined. These bodies are large
    -- enough that plain DEFLATE works properly on them -- 28x, against the 5x a
    -- 262-byte frame manages -- so no dictionary is needed. Content dedup was
    -- measured and rejected: consecutive polls differ in volumes and timestamps,
    -- so only 87 of 146 bodies were distinct and dedup saved 0%.
    -- response_sha256 is over the UNCOMPRESSED text and remains the audit anchor.
    response_codec  INTEGER NOT NULL DEFAULT 0,
    response_z      BLOB,
    response_len    INTEGER,
    response        TEXT NOT NULL DEFAULT '',
    git_commit      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_census_req_ts ON census_request(run_id, ts);

-- The independent denominator. REST /markets is authoritative for whether an
-- opportunity existed; successful WS subscription never is.
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

-- Every change to the census universe, timestamped.
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

-- Firing classification for every REST-enumerated market, from the imported
-- pinned classifier. Persisted so ALL / VALID / NON-TRADEABLE / ELIGIBLE can be
-- reconstructed from raw inputs.
CREATE TABLE IF NOT EXISTS classification (
    run_id            TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    series            TEXT NOT NULL,
    close_ts          INTEGER NOT NULL,
    target_ts         INTEGER NOT NULL,
    classified_ts     REAL NOT NULL,
    is_firing         INTEGER NOT NULL,
    not_firing_reason TEXT NOT NULL DEFAULT '',
    yes_price         REAL,
    price_source      TEXT NOT NULL DEFAULT '',
    side              TEXT NOT NULL DEFAULT '',
    entry_price       REAL,
    candle_end_ts     INTEGER,
    candles_json      TEXT NOT NULL,
    candles_sha256    TEXT NOT NULL,
    classifier_pin    TEXT NOT NULL,
    git_commit        TEXT NOT NULL,
    PRIMARY KEY (run_id, ticker)
);

-- One row per census market at its T-7 moment: the book, and whether it was
-- trustworthy. Deliberately side-AGNOSTIC and bucket-free: the side the frozen
-- rule buys comes from the classifier, which cannot run until the T-7 candle
-- exists. Recording both sides' executable ask here and deriving the section 4
-- bucket at report time keeps the book observation independent of the firing
-- verdict -- evidence gathered before the verdict is known cannot be selected
-- on the verdict.
CREATE TABLE IF NOT EXISTS signal_observation (
    run_id            TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    series            TEXT NOT NULL,
    close_ts          INTEGER NOT NULL,
    target_ts         INTEGER NOT NULL,
    observed_ts       REAL NOT NULL,
    validity          TEXT NOT NULL,
    unavailable_reason TEXT NOT NULL DEFAULT '',
    detail            TEXT NOT NULL DEFAULT '',
    subscribed        INTEGER NOT NULL,
    x_contracts       INTEGER NOT NULL,
    best_bid          REAL,
    bid_size          REAL,
    best_ask          REAL,
    ask_size          REAL,
    -- Section 2's executable ask per side. YES lifts the ask ladder; NO lifts
    -- the bid ladder at 1 - p. Sizes are in the API's fixed point (x100).
    yes_ask_price     REAL,
    yes_ask_size_fp   INTEGER,
    no_ask_price      REAL,
    no_ask_size_fp    INTEGER,
    stream_label      TEXT NOT NULL DEFAULT '',
    session_epoch     INTEGER,
    sid               TEXT NOT NULL DEFAULT '',
    snapshot_seq      INTEGER,
    last_seq          INTEGER,
    snapshot_recv_ts  REAL,
    last_update_recv_ts REAL,
    last_update_exchange_ts_ms INTEGER,
    session_activity_ts REAL,
    session_activity_age REAL,
    last_ping_ts      REAL,
    -- The full reconstructed ladder, both sides, in canonical integers. Present
    -- even when validity is not VALID -- an invalidated book is still evidence
    -- about what went wrong -- but it is the book the recorder LAST held, not a
    -- trustworthy T-7 state, and only the VALID rows may be read as the latter.
    book_json         TEXT NOT NULL DEFAULT '',
    -- Where in ws_raw an independent decoder should start and stop replaying to
    -- reproduce this row: the market's snapshot frame, and the last frame
    -- received before the observation.
    raw_first_id      INTEGER,
    raw_last_id       INTEGER,
    git_commit        TEXT NOT NULL,
    PRIMARY KEY (run_id, ticker)
);
CREATE INDEX IF NOT EXISTS ix_signal_target ON signal_observation(run_id, target_ts);

-- Section 8 dual-stream equivalence comparisons, one row per matched grid cell.
CREATE TABLE IF NOT EXISTS equivalence_comparison (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    grid_ts       REAL NOT NULL,
    ticker        TEXT NOT NULL,
    agree         INTEGER NOT NULL,
    a_bid         INTEGER, a_bid_qty INTEGER, a_ask INTEGER, a_ask_qty INTEGER,
    b_bid         INTEGER, b_bid_qty INTEGER, b_ask INTEGER, b_ask_qty INTEGER,
    resolution    TEXT NOT NULL DEFAULT '',
    skew_ms       REAL,
    git_commit    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_equiv_grid ON equivalence_comparison(run_id, grid_ts);

-- Where a run's raw evidence went when it was archived off this host, and the
-- proof the copy is faithful. Section 7 keeps raw frames as provenance for the
-- verdict; "excluded from the statistical sample" does not cancel that, so raw
-- is never simply deleted -- it is archived, verified, and only then dropped
-- from the local file. This row is what remains, and it is what lets someone
-- find and re-verify the evidence later.
CREATE TABLE IF NOT EXISTS raw_archive (
    run_id            TEXT NOT NULL,
    archived_ts       REAL NOT NULL,
    archive_path      TEXT NOT NULL,
    archive_sha256    TEXT NOT NULL,
    source_sha256     TEXT NOT NULL,
    reread_sha256     TEXT NOT NULL,
    raw_rows          INTEGER NOT NULL,
    raw_bytes         INTEGER NOT NULL,
    dictionaries      INTEGER NOT NULL,
    verdict           TEXT NOT NULL,
    replay_result     TEXT NOT NULL,
    purged_ts         REAL,
    git_commit        TEXT NOT NULL,
    PRIMARY KEY (run_id, archived_ts)
);

CREATE TABLE IF NOT EXISTS equivalence_result (
    run_id        TEXT PRIMARY KEY,
    started_ts    REAL NOT NULL,
    ended_ts      REAL NOT NULL,
    minutes       REAL NOT NULL,
    comparisons   INTEGER NOT NULL,
    agreements    INTEGER NOT NULL,
    disagreements INTEGER NOT NULL,
    skew_explained INTEGER NOT NULL,
    unexplained   INTEGER NOT NULL,
    agreement_rate REAL NOT NULL,
    verdict       TEXT NOT NULL,
    params        TEXT NOT NULL,
    -- Recorder-availability diagnostics: grid cells, unmatched fraction,
    -- disconnects per stream, reconnect durations. Reported SEPARATELY from the
    -- verdict per the 2026-08-22 section 8 clarification -- these are not
    -- decoder-equivalence failures and never move the pass bar.
    diagnostics   TEXT NOT NULL DEFAULT '{}',
    git_commit    TEXT NOT NULL
);
"""


@dataclass
class RunContext:
    """Identity stamped onto every row this process writes."""

    run_id: str
    git_commit: str


class Store:
    """
    The recorder's SQLite handle. Not thread-safe; the recorder is single-
    threaded asyncio and passes one Store around.
    """

    def __init__(self, path: Path, ctx: RunContext) -> None:
        self.path = Path(path)
        self.ctx = ctx
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus an explicit lock, because the recorder
        # runs every blocking Kalshi REST call in a worker thread: the SDK's
        # token bucket does a plain time.sleep() and urllib3 retries can block
        # for minutes, and neither may be allowed to stall the event loop that
        # is consuming the WebSocket and taking T-7 readings. One lock around
        # every statement is enough -- there is exactly one connection, and
        # nothing here needs cross-statement transactions except the batched
        # comparison insert, which holds the lock for its whole BEGIN/COMMIT.
        self.conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            # Bound the WAL. Two separate mechanisms, both needed:
            #   wal_autocheckpoint -- how many WAL pages accumulate before a
            #     checkpoint is ATTEMPTED. Attempted, not guaranteed: a passive
            #     checkpoint is skipped when a reader holds it back.
            #   journal_size_limit -- how large the WAL FILE may remain after a
            #     checkpoint. Without it the file keeps its high-water mark
            #     forever, because a passive checkpoint rewinds the WAL for
            #     reuse and never shrinks it. That is how a 26-minute recording
            #     ended up next to a 395 MB WAL.
            # RawWriter additionally forces a TRUNCATE checkpoint periodically.
            self.conn.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
            self.conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT_BYTES}")
            self._exec(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- Locked access --------------------------------------------------------
    #
    # Every statement goes through one of these three. The lock is what makes
    # check_same_thread=False safe: the census and the classifier run in worker
    # threads while the event loop keeps consuming the WebSocket, so two
    # threads really can reach this connection at the same moment.

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a read and materialise it inside the lock."""
        with self._lock:
            return list(self.conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    # -- Run and session bookkeeping -----------------------------------------

    def record_run(
        self,
        *,
        started_ts: float,
        api_env: str,
        base_url: str,
        ws_url: str,
        frozen_config: dict[str, Any],
        integrity: dict[str, Any],
        doc_reference: str,
        notes: str = "",
    ) -> None:
        self._exec(
            "INSERT OR REPLACE INTO recorder_run"
            "(run_id, started_ts, git_commit, api_env, base_url, ws_url,"
            " frozen_config, integrity, doc_reference, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self.ctx.run_id, started_ts, self.ctx.git_commit, api_env, base_url, ws_url,
                _j(frozen_config), _j(integrity), doc_reference, notes,
            ),
        )

    def close_run(self, ended_ts: float) -> None:
        self._exec(
            "UPDATE recorder_run SET ended_ts = ? WHERE run_id = ?",
            (ended_ts, self.ctx.run_id),
        )

    def record_session(
        self,
        *,
        session_id: str,
        stream_label: str,
        session_epoch: int,
        use_yes_price: bool,
        channels: Iterable[str],
        subscribe_params: dict[str, Any],
        ws_url: str,
        opened_ts: float,
    ) -> None:
        self._exec(
            "INSERT OR REPLACE INTO ws_session"
            "(session_id, run_id, stream_label, session_epoch, use_yes_price, channels,"
            " subscribe_params, ws_url, opened_ts, git_commit) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, self.ctx.run_id, stream_label, session_epoch,
                int(use_yes_price), _j(list(channels)), _j(subscribe_params), ws_url,
                opened_ts, self.ctx.git_commit,
            ),
        )

    # -- Raw and health -------------------------------------------------------

    def record_raw(
        self, *, stream_label: str, session_epoch: int, direction: str, recv_ts: float, raw: str
    ) -> int:
        """
        Write one uncompressed frame synchronously.

        Kept for tests and for compression-disabled runs. The recorder and the
        equivalence test both go through depthrec.rawwriter.RawWriter instead,
        which batches and compresses off the event loop.
        """
        payload = raw.encode("utf-8")
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO ws_raw(run_id, stream_label, session_epoch, direction,"
                " recv_ts, raw, codec, payload, raw_sha256, raw_len)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.ctx.run_id, stream_label, session_epoch, direction, recv_ts,
                 raw, 0, payload, hashlib.sha256(payload).digest(), len(payload)),
            )
            return int(cur.lastrowid)

    _RAW_COLS = (
        "id", "run_id", "stream_label", "session_epoch", "direction", "recv_ts",
        "codec", "zdict_id", "payload", "raw_sha256", "raw_len",
    )

    def insert_raw_batch(self, rows: list[tuple]) -> None:
        """
        Insert a batch of pre-encoded frames in ONE transaction.

        Batching is what keeps WAL growth proportional to the data rather than
        to the frame count: 500 frames in one commit is one WAL frame set
        instead of 500. Called only from the writer thread.
        """
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

    # -- Raw archive self-description -----------------------------------------

    def add_raw_dictionary(
        self, zdict: bytes, *, created_ts: float, first_frame_id: int, frames_sampled: int
    ) -> int:
        """
        Persist one compression dictionary INTO the database it decodes, and
        return its id.

        The archive must be self-describing: a reader a year from now needs
        nothing but the file. Keeping the dictionary in a package constant would
        make the data depend on a code version, which is exactly the coupling
        the raw archive exists to avoid.
        """
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO raw_dictionary(run_id, created_ts, first_frame_id,"
                " frames_sampled, zdict, sha256) VALUES (?,?,?,?,?,?)",
                (self.ctx.run_id, created_ts, first_frame_id, frames_sampled,
                 zdict, hashlib.sha256(zdict).hexdigest()),
            )
            return int(cur.lastrowid)

    def raw_dictionaries(self) -> dict[int, bytes]:
        """id -> dictionary bytes, enough to decode any row in this archive."""
        return {r["id"]: bytes(r["zdict"])
                for r in self.query("SELECT id, zdict FROM raw_dictionary")}

    def raw_text(self, row, *, zdicts=None, verify: bool = True) -> str:
        """
        The original frame text for one `ws_raw` row, whatever codec wrote it.

        `verify=True` re-checks the stored SHA-256 of the UNCOMPRESSED payload,
        so every read is also a proof that the archive still reproduces the
        exact bytes that arrived. Readers scanning millions of rows may turn it
        off; the replay verifier leaves it on.
        """
        return read_raw_text(
            row, zdicts if zdicts is not None else self.raw_dictionaries(), verify=verify
        )

    # -- WAL control -----------------------------------------------------------

    def checkpoint_truncate(self) -> tuple[int, int, int]:
        """
        Force a TRUNCATE checkpoint and return sqlite's (busy, log, checkpointed).

        `wal_autocheckpoint` decides when a checkpoint is ATTEMPTED; it does not
        bound the file. A passive checkpoint rewinds the WAL so its space is
        reused, but never shortens it, so the file keeps its high-water mark for
        the life of the run -- which is how a 26-minute recording ended up
        beside a 395 MB WAL. TRUNCATE is what returns the space to the
        filesystem.

        A non-zero `busy` means a reader held the checkpoint back; that is
        reported rather than retried, because the next interval will try again.
        """
        with self._lock:
            row = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)

    def wal_settings(self) -> dict[str, object]:
        return {
            "journal_mode": self.query_one("PRAGMA journal_mode")[0],
            "wal_autocheckpoint_pages": self.query_one("PRAGMA wal_autocheckpoint")[0],
            "journal_size_limit_bytes": self.query_one("PRAGMA journal_size_limit")[0],
            "synchronous": self.query_one("PRAGMA synchronous")[0],
            "page_size": self.query_one("PRAGMA page_size")[0],
        }

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

    # -- Census ---------------------------------------------------------------

    def record_census_request(
        self, *, ts: float, pass_kind: str, series: str, method: str, url: str,
        params: dict[str, Any], ok: bool, http_status: int | None = None,
        error: str = "", n_markets: int | None = None, response_sha256: str = "",
        response: str = "",
    ) -> int:
        """
        Record one census REST call, compressing the response body.

        Called from the census worker thread, never the event loop, so the
        compression cost is off the critical path -- the same reason the frame
        writer has its own thread.
        """
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
        """
        Insert or refresh one census row. Returns "new", "updated" or "seen".

        Never deletes and never narrows: a market that stops appearing in
        discovery keeps its row and its first_seen_ts. Section 4's denominator is
        every opportunity that existed, not every opportunity still visible.
        """
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

    def census_markets(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM census_market WHERE run_id=? ORDER BY close_ts, ticker",
            (self.ctx.run_id,),
        )

    def has_classification(self, ticker: str) -> bool:
        return self.query_one(
            "SELECT 1 FROM classification WHERE run_id=? AND ticker=?",
            (self.ctx.run_id, ticker),
        ) is not None

    def has_observation(self, ticker: str) -> bool:
        return self.query_one(
            "SELECT 1 FROM signal_observation WHERE run_id=? AND ticker=?",
            (self.ctx.run_id, ticker),
        ) is not None

    # -- Classification and observation --------------------------------------

    def record_classification(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["run_id"] = self.ctx.run_id
        row["git_commit"] = self.ctx.git_commit
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self._exec(
            f"INSERT OR REPLACE INTO classification({cols}) VALUES ({marks})",
            tuple(row.values()),
        )

    def record_observation(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["run_id"] = self.ctx.run_id
        row["git_commit"] = self.ctx.git_commit
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self._exec(
            f"INSERT OR REPLACE INTO signal_observation({cols}) VALUES ({marks})",
            tuple(row.values()),
        )

    def last_raw_id(self) -> int | None:
        row = self.query_one(
            "SELECT MAX(id) AS m FROM ws_raw WHERE run_id=?", (self.ctx.run_id,)
        )
        return None if row is None else row["m"]

    # -- Equivalence ----------------------------------------------------------

    _COMPARISON_COLS = (
        "run_id", "grid_ts", "ticker", "agree",
        "a_bid", "a_bid_qty", "a_ask", "a_ask_qty",
        "b_bid", "b_bid_qty", "b_ask", "b_ask_qty",
        "resolution", "skew_ms", "git_commit",
    )

    def record_comparisons(self, rows: list[dict[str, Any]]) -> None:
        """
        Insert a grid tick's comparisons in one statement.

        Batched because the section 8 universe is ~619 markets on a 1 Hz grid
        for up to four hours: one autocommit INSERT per market per tick is
        ~619 transactions a second on the event loop, and the comparison loop
        cannot afford to fall behind the grid it is sampling.
        """
        if not rows:
            return
        marks = ", ".join("?" * len(self._COMPARISON_COLS))
        payload = [
            tuple(
                self.ctx.run_id if col == "run_id"
                else self.ctx.git_commit if col == "git_commit"
                else row.get(col)
                for col in self._COMPARISON_COLS
            )
            for row in rows
        ]
        # One transaction, and the lock held across it: a BEGIN from this
        # thread and an INSERT from another would otherwise interleave.
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(
                    f"INSERT INTO equivalence_comparison({', '.join(self._COMPARISON_COLS)})"
                    f" VALUES ({marks})",
                    payload,
                )
            finally:
                self.conn.execute("COMMIT")

    def record_comparison(self, row: dict[str, Any]) -> None:
        self.record_comparisons([row])

    def record_equivalence_result(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["run_id"] = self.ctx.run_id
        row["git_commit"] = self.ctx.git_commit
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self._exec(
            f"INSERT OR REPLACE INTO equivalence_result({cols}) VALUES ({marks})",
            tuple(row.values()),
        )

    # -- Reporting ------------------------------------------------------------

    def validity_counts(self) -> dict[str, int]:
        """
        Observations by validity. NOT section 4 buckets -- those need the side,
        and the side comes from the classifier. See DepthRecorder.report().
        """
        rows = self.query(
            "SELECT validity, COUNT(*) AS n FROM signal_observation WHERE run_id=?"
            " GROUP BY validity", (self.ctx.run_id,),
        )
        return {r["validity"]: r["n"] for r in rows}

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            finally:
                self.conn.close()


def _j(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def read_raw_text(row: Any, zdicts: dict[int, bytes] | None, *, verify: bool = True) -> str:
    """
    Decode one `ws_raw` row to its original frame text, with no Store needed.

    A module-level function so an offline reader -- or an independently written
    decoder -- can consume the archive with nothing but sqlite3 and this file.
    Handles all three shapes a row can take:

      pre-schema-3 rows      `raw` populated, `payload` NULL
      uncompressed rows      codec 'none'
      compressed rows        codec 'zlib-zdict', needs the run's dictionary

    Import placement note: `rawcodec` is imported lazily so `store` stays free
    of package-internal imports at module scope and can be read standalone.
    """
    from depthrec import rawcodec

    keys = row.keys()
    codec = row["codec"] if "codec" in keys else rawcodec.CODEC_NONE
    payload = row["payload"] if "payload" in keys else None
    if payload is None:
        # Written before schema 3, or by a path that only filled `raw`.
        return row["raw"]
    zid = row["zdict_id"] if "zdict_id" in keys else None
    zdict = (zdicts or {}).get(zid) if zid is not None else None
    expected = row["raw_sha256"] if "raw_sha256" in keys else b""
    if verify:
        data = rawcodec.verify(codec, payload, zdict, expected)
    else:
        data = rawcodec.decompress(codec, payload, zdict)
    return data.decode("utf-8")


def read_census_response(row: Any, *, verify: bool = True) -> str:
    """
    The original census response body for one `census_request` row.

    Handles pre-schema-5 rows, which stored the body as uncompressed TEXT.
    `response_sha256` is over the uncompressed text either way, so the audit
    anchor is unchanged by compression.
    """
    keys = row.keys()
    blob = row["response_z"] if "response_z" in keys else None
    if blob is None:
        return row["response"] if "response" in keys else ""
    codec = row["response_codec"] if "response_codec" in keys else CENSUS_CODEC_NONE
    data = zlib.decompress(blob) if codec == CENSUS_CODEC_ZLIB else bytes(blob)
    text = data.decode("utf-8")
    if verify:
        want = row["response_sha256"] if "response_sha256" in keys else ""
        if want and hashlib.sha256(data).hexdigest() != want:
            raise ValueError(
                f"census response checksum mismatch on request id "
                f"{row['id'] if 'id' in keys else '?'}"
            )
    return text
