"""
Versioned model registry — stores strategy params, fitted calibrators,
and validation metrics for every champion/challenger decision.

Every promoted version is tagged and timestamped.  The first version ever
saved is automatically marked as the immutable baseline so future runs
can always compare against it.

Storage: a separate SQLite file (kbot_registry.db) keeps model metadata
out of the event log.
"""

from __future__ import annotations

import json
import pickle
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS model_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag         TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    params      TEXT    NOT NULL,
    calibrator  BLOB,
    metrics     TEXT    NOT NULL,
    is_champion INTEGER NOT NULL DEFAULT 0,
    is_baseline INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mv_champion ON model_versions (is_champion);
"""


@dataclass
class ModelVersion:
    id: int
    tag: str
    created_at: float
    params: dict
    calibrator: Any        # fitted IsotonicRegression or None
    metrics: dict
    is_champion: bool
    is_baseline: bool


class ModelRegistry:
    """
    Versioned store for strategy params and calibrators.

    All promotions are logged.  The first saved version becomes the immutable
    baseline — it is never overwritten, so we always have a reference point.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        # Explicit db_path wins; otherwise cfg.registry_db_path (default
        # "kbot_registry.db" -- unchanged from when it was hardcoded here).
        # get_champion() returns ONE global champion row, so a deployment that
        # fits its own calibrator MUST have its own registry file.
        from kbot.config import settings as _cfg
        path = Path(db_path) if db_path else Path(_cfg.registry_db_path)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()
        log.info("registry.opened", path=str(path))

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_version(
        self,
        tag: str,
        params: dict,
        calibrator: Any = None,
        metrics: dict | None = None,
        *,
        is_champion: bool = False,
    ) -> int:
        """
        Save a new model version.  Returns the new row ID.

        If this is the first version ever, it is also marked as baseline.
        If is_champion=True, all previous champion flags are cleared first.
        """
        metrics = metrics or {}
        if is_champion:
            self._conn.execute(
                "UPDATE model_versions SET is_champion = 0 WHERE is_champion = 1"
            )

        has_baseline = bool(
            self._conn.execute(
                "SELECT COUNT(*) FROM model_versions WHERE is_baseline = 1"
            ).fetchone()[0]
        )

        calibrator_blob = pickle.dumps(calibrator) if calibrator is not None else None

        cursor = self._conn.execute(
            """
            INSERT INTO model_versions
                (tag, created_at, params, calibrator, metrics, is_champion, is_baseline)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tag,
                time.time(),
                json.dumps(params),
                calibrator_blob,
                json.dumps(metrics),
                int(is_champion),
                int(not has_baseline),  # first version → baseline
            ),
        )
        self._conn.commit()
        version_id = cursor.lastrowid
        log.info(
            "registry.version_saved",
            id=version_id,
            tag=tag,
            is_champion=is_champion,
            is_baseline=not has_baseline,
        )
        return version_id

    def promote(self, version_id: int) -> None:
        """Promote an existing version to champion."""
        self._conn.execute(
            "UPDATE model_versions SET is_champion = 0 WHERE is_champion = 1"
        )
        self._conn.execute(
            "UPDATE model_versions SET is_champion = 1 WHERE id = ?", (version_id,)
        )
        self._conn.commit()
        log.info("registry.promoted", id=version_id)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_champion(self) -> ModelVersion | None:
        row = self._conn.execute(
            "SELECT * FROM model_versions WHERE is_champion = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self._row_to_version(row) if row else None

    def get_baseline(self) -> ModelVersion | None:
        row = self._conn.execute(
            "SELECT * FROM model_versions WHERE is_baseline = 1 ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return self._row_to_version(row) if row else None

    def list_versions(self) -> list[ModelVersion]:
        rows = self._conn.execute(
            "SELECT * FROM model_versions ORDER BY id DESC"
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_version(row: tuple) -> ModelVersion:
        vid, tag, created_at, params_json, cal_blob, metrics_json, is_champ, is_base = row
        calibrator = None
        if cal_blob:
            try:
                calibrator = pickle.loads(cal_blob)
            except Exception as exc:  # noqa: BLE001
                log.warning("registry.calibrator_load_failed", error=str(exc)[:100])
        return ModelVersion(
            id=vid,
            tag=tag,
            created_at=created_at,
            params=json.loads(params_json),
            calibrator=calibrator,
            metrics=json.loads(metrics_json),
            is_champion=bool(is_champ),
            is_baseline=bool(is_base),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ModelRegistry":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
