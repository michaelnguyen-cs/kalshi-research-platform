"""
Logging setup for kbot: structlog routed through the stdlib logging system so
every event can reach handlers, plus an optional size+time rotating file
handler for the long-running paper/live service.

Why route structlog through stdlib at all: structlog's default PrintLogger
writes straight to stdout and bypasses logging.Handler entirely, so a file
handler attached to the root logger would never see the app's own events. The
paper service used to rely on the supervisor redirecting that stdout into a
plain file -- which grew without bound (logs/ reached 23 GB, single files over
5 GB). Owning the file inside the app with SizedTimedRotatingFileHandler caps
each file and prunes old ones so log volume stays bounded no matter how chatty
a session is.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import structlog

# Per-file byte ceiling and how many rotated files to keep. 100 MB * 7 backups
# + the live file bounds the log directory at well under 1 GB.
MAX_BYTES: int = 100 * 1024 * 1024  # 100 MB
BACKUP_COUNT: int = 7               # keep the last 7 rotated files (~7 days)
DEFAULT_LOG_PATH: Path = Path("logs") / "paper.log"

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _next_midnight_ts(now: float | None = None) -> float:
    """Unix timestamp of the next local midnight after `now`."""
    now_dt = datetime.fromtimestamp(time.time() if now is None else now)
    tomorrow = (now_dt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.timestamp()


class SizedTimedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Size-capped file handler that ALSO rolls over daily at midnight.

    Built on RotatingFileHandler, not TimedRotatingFileHandler, on purpose:
    the timed handler names each backup by date, and a SECOND size-triggered
    rollover within the same day resolves to the same backup name and silently
    fails to rotate -- so a chatty day blows straight past the size cap (this
    was observed directly). RotatingFileHandler uses numeric suffixes
    (paper.log.1 .. paper.log.N) and rotates reliably on every call.

    A rollover fires when the next record would push the file past `max_bytes`
    OR when the local day rolls over, whichever comes first. `backup_count`
    rotated files are kept, so with the dominant daily cadence that's the last
    ~7 days; an unusually chatty stretch trades a little calendar depth for the
    hard guarantee that no single file exceeds `max_bytes` and total volume
    stays bounded (~(backup_count + 1) * max_bytes). That guarantee is the
    whole point -- logs/ reached 23 GB, single files over 5 GB, under the old
    unbounded stdout redirect.
    """

    def __init__(
        self,
        filename: str | os.PathLike[str],
        *,
        max_bytes: int = MAX_BYTES,
        backup_count: int = BACKUP_COUNT,
        encoding: str = "utf-8",
    ) -> None:
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            str(filename),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
        )
        self._next_rollover_ts = _next_midnight_ts()

    def shouldRollover(self, record: logging.LogRecord) -> int:  # noqa: N802 (stdlib name)
        # Size-based trigger from RotatingFileHandler.
        if super().shouldRollover(record):
            return 1
        # Daily trigger: the local day has rolled over since the last rotation.
        if time.time() >= self._next_rollover_ts:
            return 1
        return 0

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        super().doRollover()
        self._next_rollover_ts = _next_midnight_ts()


def _build_formatter() -> structlog.stdlib.ProcessorFormatter:
    """One renderer shared by every handler: structlog's console renderer,
    colourless (files and redirected stdout shouldn't carry ANSI codes)."""
    return structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain runs on records from plain stdlib loggers
        # (httpx, websockets, apscheduler, ...) so they render like ours.
        foreign_pre_chain=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt=_TIMESTAMP_FMT),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )


def configure_logging(verbose: bool, *, log_file: str | os.PathLike[str] | None = None) -> None:
    """
    Configure structlog + stdlib logging. Idempotent: replaces root handlers.

    When `log_file` is given, a SizedTimedRotatingFileHandler is added so the
    process writes bounded, rotating logs to that path in addition to stdout.
    """
    level = logging.DEBUG if verbose else logging.INFO

    # Force line-buffered UTF-8 stdout so output isn't lost when redirected.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt=_TIMESTAMP_FMT),
            # Hand off to stdlib logging; the ProcessorFormatter renders.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_build_formatter())
    root.addHandler(console)

    if log_file is not None:
        add_rotating_file_handler(log_file)


def add_rotating_file_handler(
    path: str | os.PathLike[str] = DEFAULT_LOG_PATH,
) -> SizedTimedRotatingFileHandler:
    """
    Attach a SizedTimedRotatingFileHandler for `path` to the root logger.

    Idempotent per path: if one is already installed for the same resolved
    file, that existing handler is returned rather than adding a duplicate
    (so the long-running service can call this safely once at startup).
    """
    root = logging.getLogger()
    resolved = Path(path).resolve()
    for handler in root.handlers:
        if (
            isinstance(handler, SizedTimedRotatingFileHandler)
            and Path(handler.baseFilename).resolve() == resolved
        ):
            return handler

    handler = SizedTimedRotatingFileHandler(path)
    handler.setFormatter(_build_formatter())
    root.addHandler(handler)
    return handler
