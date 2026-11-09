"""
Background writer for the raw frame archive.

Two things must never happen on the asyncio event loop: compressing a frame, and
inserting it into SQLite. The loop is consuming the WebSocket and taking T-7
readings, and a T-7 reading is the one measurement in this project that cannot be
retried. At the measured 424 frames/s the previous design did 424 synchronous
SQLite inserts per second directly on the loop.

So the loop's entire contribution is now: take a lock, increment a counter,
append to a list. Everything else -- SHA-256, compression, batching, the INSERT,
the periodic checkpoint -- happens on one worker thread.

Row ids are assigned by the SUBMITTER, not by SQLite, because `signal_observation`
records a `raw_first_id`/`raw_last_id` range and the observation is written long
before the writer thread has necessarily flushed those frames. Pre-assigning
keeps that range exact without making the loop wait for a database round trip.

Backpressure, not loss: if the queue ever grows past `max_pending` the submitter
blocks. Dropping frames would silently destroy the raw evidence section 7 exists
to preserve, and a recorder that quietly thins its own archive is worse than one
that stutters. The writer measured 10,066 frames/s against a 424 frames/s feed,
so this is a guard, not an expected path -- and if it does fire it is recorded
as a health event rather than passing unnoticed.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import structlog

from depthrec import rawcodec
from depthrec.store import Store

log = structlog.get_logger(__name__)

BATCH_MAX_ROWS = 500
"""Frames per INSERT. Batching is what keeps WAL churn proportional to data
rather than to frame count."""

BATCH_MAX_SECONDS = 0.25
"""Longest a frame waits in the queue before being written anyway."""

CHECKPOINT_INTERVAL_SECONDS = 30.0
"""How often the writer forces a TRUNCATE checkpoint. wal_autocheckpoint alone
bounds when a checkpoint is ATTEMPTED, not the size of the file on disk: a
passive checkpoint rewinds the WAL for reuse but never shrinks it, so the file
keeps its high-water mark forever. TRUNCATE is what actually returns the space."""

MAX_PENDING = 200_000
"""~50 MB of queued frames at 262 bytes. Two minutes of backlog at the observed
rate; far beyond anything the writer should ever accumulate."""


class RawWriter:
    """Owns the ws_raw archive for one run."""

    def __init__(
        self,
        store: Store,
        *,
        compress: bool = True,
        health: Callable[[str, str], None] | None = None,
        max_pending: int = MAX_PENDING,
        checkpoint_interval: float = CHECKPOINT_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._compress = compress
        self._health = health
        self._max_pending = max_pending
        self._checkpoint_interval = checkpoint_interval

        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending: list[tuple] = []
        self._next_id = store.max_raw_id() + 1
        self._stopped = False
        self._thread: threading.Thread | None = None

        # Dictionary state, touched only by the writer thread.
        self._zdict: bytes | None = None
        self._zdict_id: int | None = None
        self._samples: list[bytes] = []
        self._sampling = self._compress
        self._since_refresh = 0
        self.dictionaries = 0

        self.frames_written = 0
        self.bytes_raw = 0
        self.bytes_stored = 0
        self.checkpoints = 0
        self.max_queue_depth = 0
        self.backpressure_events = 0

    # -- Producer side (called on the event loop) -----------------------------

    def submit(
        self, *, stream_label: str, session_epoch: int, direction: str,
        recv_ts: float, raw: str,
    ) -> int:
        """
        Queue one frame and return the id it will be written under.

        This is the whole of the event loop's involvement. No compression, no
        hashing, no SQLite.
        """
        payload = raw.encode("utf-8")
        while True:
            with self._wake:
                if self._stopped:
                    raise RuntimeError("RawWriter is stopped; cannot accept frames")
                if len(self._pending) < self._max_pending:
                    row_id = self._next_id
                    self._next_id += 1
                    self._pending.append(
                        (row_id, stream_label, session_epoch, direction, recv_ts, payload)
                    )
                    depth = len(self._pending)
                    self.max_queue_depth = max(self.max_queue_depth, depth)
                    self._wake.notify()
                    return row_id
                self.backpressure_events += 1
            # Queue full. Block rather than drop -- see the module docstring.
            log.error("depthrec.rawwriter.backpressure", pending=self._max_pending)
            if self._health:
                self._health("raw_backpressure", f"queue at {self._max_pending} frames")
            time.sleep(0.05)

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="depthrec-rawwriter", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Drain, checkpoint, and join. Bounded: teardown must not hang."""
        with self._wake:
            self._stopped = True
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("depthrec.rawwriter.stop_timeout", timeout=timeout)

    # -- Writer thread ---------------------------------------------------------

    def _run(self) -> None:
        last_checkpoint = time.monotonic()
        while True:
            with self._wake:
                if not self._pending and not self._stopped:
                    self._wake.wait(timeout=BATCH_MAX_SECONDS)
                batch = self._pending[:BATCH_MAX_ROWS]
                del self._pending[: len(batch)]
                done = self._stopped and not self._pending
            if batch:
                try:
                    self._write(batch)
                except Exception as exc:  # noqa: BLE001
                    log.error("depthrec.rawwriter.write_failed", error=str(exc), n=len(batch))
                    if self._health:
                        self._health("raw_write_failed", str(exc))
            now = time.monotonic()
            if now - last_checkpoint >= self._checkpoint_interval or done:
                self._checkpoint()
                last_checkpoint = now
            if done:
                return

    def _write(self, batch: list[tuple]) -> None:
        # Sample frames uncompressed until there are enough for a dictionary
        # drawn from this run's own current traffic. Repeats on refresh, so the
        # dictionary tracks ticker rotation instead of decaying against it.
        if self._sampling:
            for row in batch:
                if len(self._samples) < rawcodec.DICT_SAMPLE_FRAMES:
                    self._samples.append(row[5])
            if len(self._samples) >= rawcodec.DICT_SAMPLE_FRAMES:
                self._zdict = rawcodec.build_dictionary(self._samples)
                self._zdict_id = self._store.add_raw_dictionary(
                    self._zdict, created_ts=time.time(),
                    first_frame_id=batch[0][0], frames_sampled=len(self._samples),
                )
                self._sampling = False
                self._samples = []
                self._since_refresh = 0
                self.dictionaries += 1
                log.info(
                    "depthrec.rawwriter.dictionary_built",
                    zdict_id=self._zdict_id, bytes=len(self._zdict),
                    sha256=rawcodec.sha256_hex(self._zdict)[:16],
                )
        elif self._compress:
            self._since_refresh += len(batch)
            if self._since_refresh >= rawcodec.DICT_REFRESH_FRAMES:
                # Start sampling again. The CURRENT dictionary stays in use, and
                # stays referenced by every row that used it, until the
                # replacement is ready -- so no frame is ever left undecodable.
                self._sampling = True
                self._samples = []

        rows = []
        for row_id, label, epoch, direction, recv_ts, payload in batch:
            codec, stored = rawcodec.compress(payload, self._zdict if self._compress else None)
            rows.append((
                row_id, self._store.ctx.run_id, label, epoch, direction, recv_ts,
                codec, self._zdict_id if codec != rawcodec.CODEC_NONE else None,
                stored, rawcodec.sha256_bytes(payload), len(payload),
            ))
            self.bytes_raw += len(payload)
            self.bytes_stored += len(stored)
        self._store.insert_raw_batch(rows)
        self.frames_written += len(rows)

    def _checkpoint(self) -> None:
        try:
            self._store.checkpoint_truncate()
            self.checkpoints += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("depthrec.rawwriter.checkpoint_failed", error=str(exc))

    # -- Reporting --------------------------------------------------------------

    def stats(self) -> dict[str, object]:
        ratio = (self.bytes_raw / self.bytes_stored) if self.bytes_stored else None
        return {
            "frames_written": self.frames_written,
            "bytes_raw": self.bytes_raw,
            "bytes_stored": self.bytes_stored,
            "compression_ratio": round(ratio, 3) if ratio else None,
            "checkpoints": self.checkpoints,
            "max_queue_depth": self.max_queue_depth,
            "backpressure_events": self.backpressure_events,
            "codec": rawcodec.CODEC_NAMES[
                rawcodec.CODEC_ZLIB_DICT if self._compress else rawcodec.CODEC_NONE],
            "dictionaries": self.dictionaries,
            "zdict_bytes": len(self._zdict) if self._zdict else 0,
        }
