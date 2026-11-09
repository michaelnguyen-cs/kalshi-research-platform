"""
Archive-then-purge-local for section 8 raw evidence.

NOT deletion. Section 8 excludes validation data from the feasibility and
confirmatory samples, but section 7 keeps raw frames as the provenance behind a
verdict -- "if the derived parser is later questioned, the frozen data can be
replayed through an independently written decoder". Being outside the
statistical sample does not cancel that audit requirement. So raw frames leave
the production host only by being copied somewhere durable first, and only after
the copy has been proved faithful by an independent re-read.

Five gates, ALL of which must pass before a single row is dropped:

  1. the formal verdict row exists
  2. the database has passed replay verification
  3. an archive of the raw evidence has been copied to a separate durable
     location
  4. the archive's SHA-256 is recorded
  5. the copy's SHA-256 has been independently re-read and matched against the
     source

What stays local: the verdict, the availability diagnostics, the comparison
summary, the archive location, the archive hash, the recorder commit, and the
run metadata. Everything needed to state what was concluded and to go and find
the evidence -- just not the evidence itself.

The archive is a complete copy of the database, not an extract. An extract is a
new artifact that has to be trusted; a byte-identical copy replays through the
same unchanged `depthrec_report.py --verify-replay` as the original.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from depthrec.store import read_raw_text

log = structlog.get_logger(__name__)

CHUNK = 4 * 1024 * 1024


class ArchiveRefused(RuntimeError):
    """A gate did not pass. Nothing was purged."""


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ArchiveReport:
    run_id: str
    gates: list[GateResult] = field(default_factory=list)
    archive_path: str = ""
    source_sha256: str = ""
    archive_sha256: str = ""
    reread_sha256: str = ""
    raw_rows: int = 0
    raw_bytes: int = 0
    dictionaries: int = 0
    verdict: str = ""
    replay_result: str = ""
    purged: bool = False
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def ok(self) -> bool:
        return all(g.passed for g in self.gates)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "gates": [{"gate": g.name, "passed": g.passed, "detail": g.detail}
                      for g in self.gates],
            "all_gates_passed": self.ok,
            "archive_path": self.archive_path,
            "source_sha256": self.source_sha256,
            "archive_sha256": self.archive_sha256,
            "reread_sha256": self.reread_sha256,
            "raw_rows": self.raw_rows,
            "raw_bytes": self.raw_bytes,
            "dictionaries": self.dictionaries,
            "verdict": self.verdict,
            "replay_result": self.replay_result,
            "purged": self.purged,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
        }


def sha256_file(path: Path) -> str:
    """Streamed, so a multi-gigabyte archive does not have to fit in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# -- Gate 1: the verdict ------------------------------------------------------


def read_verdict(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT verdict FROM equivalence_result WHERE run_id=?", (run_id,)
    ).fetchone()
    return row["verdict"] if row and row["verdict"] else None


# -- Gate 2: replay verification -----------------------------------------------


def verify_replay_integrity(conn: sqlite3.Connection, run_id: str) -> tuple[bool, str]:
    """
    Every retained frame decodes and matches its stored SHA-256.

    For a section 8 database this IS the replay verification: there are no T-7
    observations to rebuild, so what has to be proved is that the archive
    reproduces the exact bytes that arrived. `depthrec_report.py --verify-replay`
    performs the observation-level check on recorder databases and calls the same
    decoder; both are run by the CLI before this gate is allowed to pass.
    """
    zdicts = {}
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "raw_dictionary" in tables:
        zdicts = {r["id"]: bytes(r["zdict"])
                  for r in conn.execute("SELECT id, zdict FROM raw_dictionary")}

    have = {r[1] for r in conn.execute("PRAGMA table_info(ws_raw)")}
    cols = [c for c in ("id", "raw", "codec", "zdict_id", "payload",
                        "raw_sha256", "raw_len") if c in have]
    checked = 0
    for row in conn.execute(
        f"SELECT {', '.join(cols)} FROM ws_raw WHERE run_id=? ORDER BY id", (run_id,)
    ):
        try:
            read_raw_text(row, zdicts, verify=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"frame id {row['id']} failed verification: {exc}"
        checked += 1
    if checked == 0:
        return False, "no raw frames present; nothing to archive"
    return True, f"{checked} frames decoded and checksum-verified"


# -- The whole flow -------------------------------------------------------------


def archive_and_purge(
    db_path: Path,
    archive_dir: Path,
    *,
    run_id: str | None = None,
    purge: bool = False,
    allow_same_volume: bool = False,
    git_commit: str = "",
) -> ArchiveReport:
    """
    Run every gate; archive; and purge local raw ONLY if `purge` and all gates
    passed. Returns the report either way -- a refusal is a result, not an
    exception, unless the caller asked to purge.
    """
    db_path = Path(db_path)
    archive_dir = Path(archive_dir)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if run_id is None:
            # An equivalence database that has not produced a verdict yet has no
            # equivalence_result row and no recorder_run row, so fall through to
            # the tables that always exist. Getting this wrong made gate 2
            # report "no raw frames" on a database holding 415,631 of them.
            row = None
            for sql in (
                "SELECT run_id FROM equivalence_result ORDER BY ended_ts DESC LIMIT 1",
                "SELECT run_id FROM recorder_run ORDER BY started_ts DESC LIMIT 1",
                "SELECT run_id FROM ws_session ORDER BY opened_ts DESC LIMIT 1",
                "SELECT run_id FROM ws_raw LIMIT 1",
            ):
                row = conn.execute(sql).fetchone()
                if row is not None:
                    break
            run_id = row["run_id"] if row else ""
        report = ArchiveReport(run_id=run_id)
        report.bytes_before = db_path.stat().st_size

        counts = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(raw_len), 0) b FROM ws_raw WHERE run_id=?",
            (run_id,),
        ).fetchone()
        report.raw_rows, report.raw_bytes = counts["n"], counts["b"]
        drow = conn.execute("SELECT COUNT(*) n FROM raw_dictionary").fetchone()
        report.dictionaries = drow["n"] if drow else 0

        # Gate 1 -- the formal verdict row exists.
        verdict = read_verdict(conn, run_id)
        report.verdict = verdict or ""
        report.gates.append(GateResult(
            "verdict_row_exists", verdict is not None,
            f"verdict={verdict!r}" if verdict
            else "no equivalence_result row: the acceptance verdict this "
                 "evidence supports does not exist yet",
        ))

        # Gate 2 -- replay verification.
        ok, detail = verify_replay_integrity(conn, run_id)
        report.replay_result = detail
        report.gates.append(GateResult("replay_verified", ok, detail))
    finally:
        conn.close()

    # Gate 3 -- copied to a separate durable location.
    archive_path = archive_dir / f"{db_path.stem}.{run_id}.archive.sqlite"
    report.archive_path = str(archive_path)
    separate = _is_separate_location(db_path, archive_dir, allow_same_volume)
    report.gates.append(GateResult(
        "archive_location_separate", separate[0], separate[1],
    ))

    copied = False
    if all(g.passed for g in report.gates):
        archive_dir.mkdir(parents=True, exist_ok=True)
        # Fold the WAL into the main file FIRST. A WAL-mode database is not the
        # single file it looks like: copying `x.sqlite` while `x.sqlite-wal`
        # holds uncommitted-to-main pages archives a torn, older database and
        # the hashes would still match each other perfectly. Checkpointing to
        # TRUNCATE makes the one file authoritative before it is read.
        _checkpoint_source(db_path)
        wal = Path(str(db_path) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise ArchiveRefused(
                f"{wal} still holds {wal.stat().st_size} bytes after a TRUNCATE "
                f"checkpoint -- another process is holding the database open, "
                f"and copying the main file alone would archive a torn database"
            )
        report.source_sha256 = sha256_file(db_path)
        # copy2 preserves mtime; the hash is what actually matters.
        shutil.copy2(db_path, archive_path)
        copied = True
        report.archive_sha256 = report.source_sha256
    report.gates.append(GateResult(
        "archive_copied", copied,
        f"{archive_path}" if copied else "skipped: an earlier gate failed",
    ))

    # Gate 4 -- the archive's SHA-256 is recorded.
    report.gates.append(GateResult(
        "archive_hash_recorded", bool(report.archive_sha256),
        report.archive_sha256 or "no hash",
    ))

    # Gate 5 -- the COPY is independently re-read from disk and matched.
    matched = False
    if copied:
        # Re-read from the file that now exists at the destination, not from
        # any buffer left over from writing it. Anything less proves only that
        # we can remember what we intended to write.
        report.reread_sha256 = sha256_file(archive_path)
        matched = (report.reread_sha256 == report.source_sha256)
    report.gates.append(GateResult(
        "copy_reread_and_matched", matched,
        f"reread={report.reread_sha256[:16]}... source={report.source_sha256[:16]}..."
        if copied else "skipped: nothing was copied",
    ))

    if not report.ok:
        if purge:
            raise ArchiveRefused(
                "refusing to purge; failed gates: "
                + ", ".join(g.name for g in report.gates if not g.passed)
            )
        return report

    _record_archive(db_path, report, git_commit)

    if purge:
        _purge_local_raw(db_path, report)
        report.purged = True
    report.bytes_after = db_path.stat().st_size
    return report


def _is_separate_location(
    db_path: Path, archive_dir: Path, allow_same_volume: bool
) -> tuple[bool, str]:
    """
    The archive must not sit beside the thing it is protecting.

    A copy in the same directory survives neither a disk failure nor the purge
    that is about to free space, so it is refused outright. A copy on the same
    volume is refused too unless explicitly allowed, because "durable" means
    surviving the loss of this host's disk.
    """
    src_dir = db_path.resolve().parent
    dst = archive_dir.resolve()
    if dst == src_dir:
        return False, f"archive dir is the database's own directory ({dst})"
    try:
        same_volume = os.stat(src_dir).st_dev == os.stat(
            dst if dst.exists() else dst.parent if dst.parent.exists() else src_dir
        ).st_dev
    except OSError:
        same_volume = False
    if same_volume and not allow_same_volume:
        return False, (
            f"archive dir {dst} is on the same volume as {src_dir}; a copy that "
            f"dies with this disk is not a durable location. Pass "
            f"--allow-same-volume only for a staging step that will be "
            f"replicated off-host."
        )
    return True, f"{dst}" + (" (same volume, explicitly allowed)" if same_volume else "")


def _checkpoint_source(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


_RAW_ARCHIVE_DDL = """
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
)
"""


def _record_archive(db_path: Path, report: ArchiveReport, git_commit: str) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # Databases written before schema 6 have no raw_archive table. The tool
        # has to work on the archives that already exist, not only on ones
        # created after it was written.
        conn.execute(_RAW_ARCHIVE_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO raw_archive(run_id, archived_ts, archive_path,"
            " archive_sha256, source_sha256, reread_sha256, raw_rows, raw_bytes,"
            " dictionaries, verdict, replay_result, git_commit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (report.run_id, time.time(), report.archive_path, report.archive_sha256,
             report.source_sha256, report.reread_sha256, report.raw_rows,
             report.raw_bytes, report.dictionaries, report.verdict,
             report.replay_result, git_commit),
        )
    finally:
        conn.close()


def _purge_local_raw(db_path: Path, report: ArchiveReport) -> None:
    """
    Drop the raw frames from the LOCAL file only.

    Everything that says what was concluded stays: the verdict, the
    diagnostics, the comparison rows, the run metadata, and the raw_archive row
    pointing at the evidence with its hash. VACUUM is what actually returns the
    space -- a DELETE alone leaves the pages on the freelist, which would make
    the whole exercise pointless.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("DELETE FROM ws_raw WHERE run_id=?", (report.run_id,))
        conn.execute("DELETE FROM raw_dictionary WHERE run_id=?", (report.run_id,))
        conn.execute(
            "UPDATE raw_archive SET purged_ts=? WHERE run_id=? AND archived_ts="
            "(SELECT MAX(archived_ts) FROM raw_archive WHERE run_id=?)",
            (time.time(), report.run_id, report.run_id),
        )
        conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                     " VALUES ('raw_purged_locally', '1')")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    log.info(
        "depthrec.archive.purged_local_raw",
        run_id=report.run_id, rows=report.raw_rows,
        archive=report.archive_path, archive_sha256=report.archive_sha256,
    )
