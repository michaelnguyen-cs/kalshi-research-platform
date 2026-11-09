"""Tests for kbot.log_config -- the size+daily rotating file handler and the
structlog/stdlib wiring that feeds it.

These reconfigure global logging/structlog state, so an autouse fixture restores
structlog defaults and strips root handlers after each test to keep the rest of
the suite (which relies on structlog's default PrintLogger config) clean.
"""

from __future__ import annotations

import glob
import logging
import time
from pathlib import Path

import pytest
import structlog

from kbot import log_config
from kbot.log_config import (
    BACKUP_COUNT,
    DEFAULT_LOG_PATH,
    MAX_BYTES,
    SizedTimedRotatingFileHandler,
    add_rotating_file_handler,
    configure_logging,
)


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    for h in list(root.handlers):
        if h not in saved:
            h.close()
            root.removeHandler(h)
    structlog.reset_defaults()


def _make_handler(tmp_path, *, max_bytes=2000, backup_count=3) -> SizedTimedRotatingFileHandler:
    h = SizedTimedRotatingFileHandler(
        tmp_path / "logs" / "paper.log", max_bytes=max_bytes, backup_count=backup_count
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _isolated_logger(name, handler) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.handlers.clear()
    lg.addHandler(handler)
    return lg


# ── constants ────────────────────────────────────────────────────────────────

def test_defaults_match_requirements():
    assert MAX_BYTES == 100 * 1024 * 1024   # 100 MB
    assert BACKUP_COUNT == 7                 # keep 7
    assert DEFAULT_LOG_PATH == Path("logs") / "paper.log"


# ── size rollover ────────────────────────────────────────────────────────────

def test_no_single_file_exceeds_max_bytes_under_sustained_writes(tmp_path):
    """The core guarantee: repeated size rollovers keep every file bounded.

    Regression against the TimedRotatingFileHandler bug where a second same-day
    size rollover silently no-ops and the live file grows without bound.
    """
    h = _make_handler(tmp_path, max_bytes=2000, backup_count=3)
    lg = _isolated_logger("size_test", h)

    for i in range(500):
        lg.info("x" * 50 + f" line {i}")

    files = glob.glob(str(tmp_path / "logs" / "paper.log*"))
    assert files, "expected at least the live file"
    # A little slack over 2000 for the final record that triggers the roll.
    for f in files:
        assert Path(f).stat().st_size <= 2200, f"{f} exceeded the size cap"


def test_size_rollover_keeps_only_backup_count_files(tmp_path):
    h = _make_handler(tmp_path, max_bytes=2000, backup_count=3)
    lg = _isolated_logger("count_test", h)

    for i in range(500):
        lg.info("x" * 50 + f" line {i}")

    backups = glob.glob(str(tmp_path / "logs" / "paper.log.*"))
    assert len(backups) == 3  # live file + at most 3 numbered backups


def test_total_log_volume_is_bounded(tmp_path):
    """(backup_count + 1) * max_bytes is the ceiling on the whole log dir."""
    h = _make_handler(tmp_path, max_bytes=2000, backup_count=3)
    lg = _isolated_logger("vol_test", h)

    for i in range(2000):
        lg.info("y" * 80 + f" {i}")

    total = sum(Path(f).stat().st_size for f in glob.glob(str(tmp_path / "logs" / "paper.log*")))
    assert total <= (3 + 1) * 2200


# ── daily rollover ───────────────────────────────────────────────────────────

def test_daily_trigger_rolls_over_without_size_pressure(tmp_path):
    h = _make_handler(tmp_path, max_bytes=10**9, backup_count=3)  # size never triggers
    lg = _isolated_logger("daily_test", h)

    h._next_rollover_ts = time.time() - 1  # pretend midnight just passed
    lg.info("first line of the new day")

    assert (tmp_path / "logs" / "paper.log.1").exists()
    assert h._next_rollover_ts > time.time()  # rescheduled to the next midnight


def test_next_midnight_is_in_the_future_and_at_day_boundary():
    now = time.time()
    nxt = log_config._next_midnight_ts(now)
    assert nxt > now
    assert nxt - now <= 24 * 3600 + 1
    from datetime import datetime
    dt = datetime.fromtimestamp(nxt)
    assert (dt.hour, dt.minute, dt.second) == (0, 0, 0)


def test_handler_creates_parent_directory(tmp_path):
    target = tmp_path / "deep" / "nested" / "logs" / "paper.log"
    SizedTimedRotatingFileHandler(target, max_bytes=MAX_BYTES, backup_count=BACKUP_COUNT)
    assert target.parent.is_dir()


# ── structlog / stdlib wiring ────────────────────────────────────────────────

def test_configure_logging_routes_structlog_events_to_file(tmp_path):
    log_path = tmp_path / "logs" / "paper.log"
    configure_logging(verbose=False, log_file=log_path)

    structlog.get_logger("app").info("structlog_event_marker", key="val")
    logging.getLogger("some.dependency").warning("foreign_stdlib_marker")

    content = log_path.read_text()
    assert "structlog_event_marker" in content  # app events reach the file
    assert "foreign_stdlib_marker" in content    # so do plain stdlib logs


def test_configure_logging_without_file_adds_no_rotating_handler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    configure_logging(verbose=False)  # no log_file

    root = logging.getLogger()
    assert not any(isinstance(h, SizedTimedRotatingFileHandler) for h in root.handlers)


def test_add_rotating_file_handler_is_idempotent_per_path(tmp_path):
    path = tmp_path / "logs" / "paper.log"
    configure_logging(verbose=False)

    h1 = add_rotating_file_handler(path)
    h2 = add_rotating_file_handler(path)

    assert h1 is h2
    root = logging.getLogger()
    n = sum(isinstance(h, SizedTimedRotatingFileHandler) for h in root.handlers)
    assert n == 1


def test_verbose_sets_debug_level(tmp_path):
    configure_logging(verbose=True, log_file=tmp_path / "logs" / "paper.log")
    assert logging.getLogger().level == logging.DEBUG
    configure_logging(verbose=False, log_file=tmp_path / "logs" / "paper.log")
    assert logging.getLogger().level == logging.INFO
