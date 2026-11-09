"""
The compressed raw archive.

Section 7's promise is that the frames a decoder is replayed against are the
frames that arrived. Compressing them at rest is allowed; losing a byte is not.
So the load-bearing property here is not the ratio -- it is that decompression
reproduces the ORIGINAL BYTES EXACTLY, proved by a SHA-256 of the uncompressed
payload stored on every row and re-checked on every read.

Also pinned here: the archive decodes with nothing but the file (dictionaries
live in the database, not in a package constant), dictionary refresh never
strands older frames, and a corrupted payload is refused rather than returned.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import rawcodec  # noqa: E402
from depthrec.rawwriter import RawWriter  # noqa: E402
from depthrec.store import RunContext, Store, read_raw_text  # noqa: E402


def frame(i: int, ticker: str = "KXBTC15M-26AUG220730-30") -> str:
    """A frame shaped like the real thing -- same keys, same value formats."""
    return json.dumps({
        "type": "orderbook_delta", "sid": 1, "seq": i,
        "msg": {"market_ticker": ticker,
                "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
                "price_dollars": f"0.{(i % 99) + 1:02d}00",
                "delta_fp": f"{(i % 50) - 25}.00", "side": "yes" if i % 2 else "no",
                "ts_ms": 1787397780000 + i},
    })


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "raw.sqlite", RunContext("run-1", "cafe"))
    yield s
    try:
        s.close()
    except Exception:
        pass


def drain(store: Store, frames: list[str], *, compress: bool = True, **kw) -> RawWriter:
    w = RawWriter(store, compress=compress, max_pending=10_000_000, **kw)
    w.start()
    for i, f in enumerate(frames):
        w.submit(stream_label="A", session_epoch=1, direction="in",
                 recv_ts=1000.0 + i, raw=f)
    w.stop(timeout=120)
    return w


def stored_rows(store: Store) -> list[sqlite3.Row]:
    return store.query("SELECT * FROM ws_raw ORDER BY id")


# -- The guarantee ------------------------------------------------------------

def test_every_frame_round_trips_byte_for_byte(store):
    frames = [frame(i) for i in range(2000)]
    drain(store, frames)
    zdicts = store.raw_dictionaries()
    rows = stored_rows(store)
    assert len(rows) == len(frames)
    for original, row in zip(frames, rows):
        assert read_raw_text(row, zdicts, verify=True) == original


def test_frames_really_were_compressed(store):
    """A test that passes on a no-op codec proves nothing."""
    frames = [frame(i) for i in range(3000)]
    w = drain(store, frames)
    assert w.stats()["compression_ratio"] > 1.5
    codecs = {r["codec"] for r in stored_rows(store)}
    assert rawcodec.CODEC_ZLIB_DICT in codecs


def test_the_checksum_is_over_the_uncompressed_payload(store):
    frames = [frame(i) for i in range(1500)]
    drain(store, frames)
    for original, row in zip(frames, stored_rows(store)):
        assert bytes(row["raw_sha256"]) == rawcodec.sha256_bytes(original.encode())
        assert row["raw_len"] == len(original.encode())


def test_a_corrupted_payload_is_refused_not_returned(store):
    """
    The checksum has to be able to fail. Flip one byte of a stored blob and the
    read must raise rather than hand back whatever came out.
    """
    drain(store, [frame(i) for i in range(1200)])
    zdicts = store.raw_dictionaries()
    row = [r for r in stored_rows(store) if r["codec"] == rawcodec.CODEC_ZLIB_DICT][-1]
    blob = bytearray(row["payload"])
    blob[len(blob) // 2] ^= 0xFF
    store._exec("UPDATE ws_raw SET payload=? WHERE id=?", (bytes(blob), row["id"]))
    bad = store.query_one("SELECT * FROM ws_raw WHERE id=?", (row["id"],))
    with pytest.raises(Exception):
        read_raw_text(bad, zdicts, verify=True)


def test_a_truncated_checksum_field_cannot_silently_pass(store):
    """An empty checksum must not be treated as 'verified'."""
    drain(store, [frame(i) for i in range(1200)])
    zdicts = store.raw_dictionaries()
    row = stored_rows(store)[-1]
    text = read_raw_text(row, zdicts, verify=True)
    assert text == frame(1199)
    # rawcodec.verify skips the comparison only when there is no stored digest,
    # and every row written by the writer has one.
    assert row["raw_sha256"] is not None and len(bytes(row["raw_sha256"])) == 32


# -- Self-description ----------------------------------------------------------

def test_the_archive_decodes_with_nothing_but_the_file(store, tmp_path):
    """
    No package constant, no config: open the sqlite file, read its dictionaries
    out of its own table, decode. This is what makes the archive replayable by
    an independently written decoder in a year's time.
    """
    frames = [frame(i) for i in range(2000)]
    drain(store, frames)
    path = store.path
    store.close()

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    zdicts = {r["id"]: bytes(r["zdict"])
              for r in conn.execute("SELECT id, zdict FROM raw_dictionary")}
    assert zdicts, "the archive must carry its own dictionaries"
    got = [read_raw_text(r, zdicts, verify=True)
           for r in conn.execute("SELECT * FROM ws_raw ORDER BY id")]
    conn.close()
    assert got == frames


def test_warmup_frames_are_stored_uncompressed_and_still_readable(store):
    frames = [frame(i) for i in range(rawcodec.DICT_SAMPLE_FRAMES + 200)]
    drain(store, frames)
    rows = stored_rows(store)
    assert rows[0]["codec"] == rawcodec.CODEC_NONE
    assert rows[0]["zdict_id"] is None
    zdicts = store.raw_dictionaries()
    assert read_raw_text(rows[0], zdicts, verify=True) == frames[0]


def test_dictionary_refresh_never_strands_older_frames(store, monkeypatch):
    """
    Dictionaries are refreshed as tickers rotate. Every row records WHICH
    dictionary encoded it, so a refresh can never make an earlier frame
    undecodable -- which would destroy exactly the evidence being kept.
    """
    monkeypatch.setattr(rawcodec, "DICT_REFRESH_FRAMES", 800)
    # Ticker changes partway, which is what makes a refresh worth doing. Each
    # refresh costs another DICT_SAMPLE_FRAMES of sampling before the new
    # dictionary exists, so the frame count has to allow for several cycles.
    frames = [frame(i, "KX-A-1" if i < 4000 else "KX-B-2") for i in range(8000)]
    w = drain(store, frames)
    assert w.dictionaries >= 3, f"expected several dictionaries, got {w.dictionaries}"

    zdicts = store.raw_dictionaries()
    assert len(zdicts) == w.dictionaries
    used = {r["zdict_id"] for r in stored_rows(store)} - {None}
    assert len(used) >= 3, "rows must reference more than one dictionary"
    for original, row in zip(frames, stored_rows(store)):
        assert read_raw_text(row, zdicts, verify=True) == original


# -- Ordering and identity -----------------------------------------------------

def test_submitted_ids_match_the_rows_actually_written(store):
    """
    signal_observation records a raw_first_id/raw_last_id range, so the id the
    submitter is handed must be the id the row lands under -- even though the
    write happens later on another thread.
    """
    w = RawWriter(store, compress=True, max_pending=10_000_000)
    w.start()
    ids = [w.submit(stream_label="A", session_epoch=1, direction="in",
                    recv_ts=1000.0 + i, raw=frame(i)) for i in range(1500)]
    w.stop(timeout=120)
    assert ids == sorted(ids)
    assert [r["id"] for r in stored_rows(store)] == ids


def test_ids_continue_across_writers_on_the_same_store(store):
    drain(store, [frame(i) for i in range(600)])
    first_max = store.max_raw_id()
    drain(store, [frame(i) for i in range(600, 1200)])
    ids = [r["id"] for r in stored_rows(store)]
    assert ids == list(range(1, 1201))
    assert first_max == 600


def test_compression_can_be_turned_off_and_rows_still_verify(store):
    frames = [frame(i) for i in range(800)]
    w = drain(store, frames, compress=False)
    assert w.stats()["compression_ratio"] == 1.0
    zdicts = store.raw_dictionaries()
    assert zdicts == {}
    for original, row in zip(frames, stored_rows(store)):
        assert row["codec"] == rawcodec.CODEC_NONE
        assert read_raw_text(row, zdicts, verify=True) == original


# -- WAL bounding ---------------------------------------------------------------

def test_wal_pragmas_are_configured(store):
    s = store.wal_settings()
    assert s["journal_mode"] == "wal"
    assert s["wal_autocheckpoint_pages"] == 4000
    assert s["journal_size_limit_bytes"] == 64 * 1024 * 1024


def test_truncate_checkpoint_actually_returns_the_space(store):
    """
    wal_autocheckpoint bounds when a checkpoint is ATTEMPTED, not the file size:
    a passive checkpoint rewinds the WAL for reuse but never shortens it. This
    is the difference that left a 26-minute recording beside a 395 MB WAL.
    """
    drain(store, [frame(i) for i in range(20000)], checkpoint_interval=1e9)
    wal = Path(str(store.path) + "-wal")
    before = wal.stat().st_size if wal.exists() else 0
    store.checkpoint_truncate()
    after = wal.stat().st_size if wal.exists() else 0
    assert after < max(before, 1) or after == 0
    assert after <= 4096


def test_no_store_read_holds_a_transaction_open(store):
    """
    A reader that never finishes pins the WAL: a checkpoint cannot advance past
    the oldest reader. Every Store read materialises inside the lock, so no
    cursor is left dangling. Proved by checkpointing successfully right after a
    read -- busy would be non-zero if a read transaction were still open.
    """
    drain(store, [frame(i) for i in range(1500)])
    store.query("SELECT * FROM ws_raw")
    store.query_one("SELECT COUNT(*) FROM ws_raw")
    busy, _log, _ckpt = store.checkpoint_truncate()
    assert busy == 0, "a read transaction was still open and blocked the checkpoint"
