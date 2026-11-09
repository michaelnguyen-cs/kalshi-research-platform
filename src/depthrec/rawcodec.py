"""
Compression for the raw WebSocket frame archive.

Section 7 requires the untouched frame to be retained so a questioned parser can
be replayed against it. It does not require those bytes to sit on disk
uncompressed, and at the measured 424 frames/s a three-day pilot does not fit on
the production VM. So frames are compressed at rest and decompressed on read,
with the guarantee that decompression reproduces the ORIGINAL BYTES EXACTLY --
enforced by a SHA-256 of the uncompressed payload stored alongside every row.

Codec choice, measured on 60,000 real frames from smoke2.sqlite (mean 262 bytes):

    zlib L1  .. L9, no dictionary      1.25x
    zlib L1 + 32 KiB preset dictionary 5.26x
    zlib L6 + 32 KiB preset dictionary 5.76x
    zlib L9 + 32 KiB preset dictionary 5.89x

Plain zlib is near-useless here: a 262-byte JSON frame is far too short for
DEFLATE to build a window, so it never finds the repetition that makes these
frames compressible. A preset dictionary hands it that window up front. Since
every frame in a run shares a schema, a ticker set and a price format, a
dictionary sampled from the run's own opening frames captures almost all of it.

The dictionary is built ADAPTIVELY from the first frames of each run and stored
in that database's `meta` table, so the archive is self-describing: it carries
everything needed to decode itself, with no dependency on a constant in some
future version of this package. Frames received before the dictionary exists are
stored with codec "none". Every row records its own codec, so a single database
can mix them and still decode.

zstandard would be faster with better dictionary support, but it is not
installed and this is stdlib-only by preference: a research archive that needs a
third-party library to be readable in a year is a worse archive.
"""

from __future__ import annotations

import hashlib
import zlib

CODEC_NONE = 0
CODEC_ZLIB_DICT = 1

CODEC_NAMES = {CODEC_NONE: "none", CODEC_ZLIB_DICT: "zlib-zdict"}
"""Stored as a small INTEGER, not a string: the codec name as TEXT cost 10
bytes on every one of 660,961 rows in the first measurement, against a 77-byte
compressed payload. The mapping is written into the database's `meta` table so
the archive stays self-describing."""

ZLIB_LEVEL = 9
"""Level 9 measured 10,066 frames/s single-threaded -- 24x the observed 424
frames/s feed rate -- so the extra ratio over L6 is free. Compression runs in a
worker thread regardless (see depthrec.rawwriter)."""

DICT_SAMPLE_FRAMES = 512
"""Frames collected before a dictionary is built. At 424 frames/s that is about
1.2 seconds, stored uncompressed."""

DICT_REFRESH_FRAMES = 150_000
"""Rebuild the dictionary every this many frames.

Measured need: a single dictionary taken from the first 512 frames of a
26-minute run compressed the whole run at 3.41x, against 5.89x when the
dictionary and the sample came from the same stretch of traffic. The difference
is ticker rotation -- KXBTC15M/KXETH15M mint a new market every quarter hour and
the ticker string is a large share of every frame, so a dictionary from minute
zero is progressively less use. Over three days a fixed dictionary would decay
much further. Refreshing keeps it matched to current traffic; each dictionary is
stored and referenced per row, so old frames still decode."""

DICT_MAX_BYTES = 32768
"""zlib's window is 32 KiB; a preset dictionary longer than that is truncated to
its LAST 32 KiB by zlib itself, so there is nothing to gain by storing more."""


def build_dictionary(samples: list[bytes]) -> bytes:
    """
    A preset dictionary from `samples`, capped at zlib's usable window.

    The tail is kept rather than the head: zlib matches against the END of the
    dictionary first, so the most representative material should sit last. The
    samples are simply concatenated -- a real dictionary trainer (zstd's) would
    do better, but this measured 5.89x and the frames are highly self-similar.
    """
    blob = b"".join(samples)
    return blob[-DICT_MAX_BYTES:] if len(blob) > DICT_MAX_BYTES else blob


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_bytes(payload: bytes) -> bytes:
    """
    The 32 raw digest bytes, not 64 characters of hex.

    Hex doubled the cost of the strongest guarantee in the archive: 64 B/row of
    checksum against a 77 B/row compressed payload. A BLOB is the same SHA-256.
    """
    return hashlib.sha256(payload).digest()


def compress(payload: bytes, zdict: bytes | None) -> tuple[int, bytes]:
    """
    (codec, stored bytes) for one frame.

    Returns CODEC_NONE unchanged when there is no dictionary yet, so warm-up
    frames cost nothing but their own size and stay trivially readable.
    """
    if not zdict:
        return CODEC_NONE, payload
    obj = zlib.compressobj(ZLIB_LEVEL, zlib.DEFLATED, zlib.MAX_WBITS, 9, 0, zdict)
    return CODEC_ZLIB_DICT, obj.compress(payload) + obj.flush()


def decompress(codec: int, stored: bytes, zdict: bytes | None) -> bytes:
    """
    The original frame bytes.

    Raises rather than returning something plausible-but-wrong: a raw archive
    that silently decodes to the wrong bytes is worse than one that refuses.
    """
    if codec == CODEC_NONE:
        return bytes(stored)
    if codec == CODEC_ZLIB_DICT:
        if not zdict:
            raise ValueError(
                "row is zlib-zdict encoded but this database carries no "
                "raw_zdict in meta -- the archive cannot decode itself"
            )
        obj = zlib.decompressobj(zdict=zdict)
        return obj.decompress(stored) + obj.flush()
    raise ValueError(f"unknown raw codec {codec!r}")


def verify(codec: int, stored: bytes, zdict: bytes | None, expected_sha256: bytes | str) -> bytes:
    """
    Decompress and prove the result is byte-for-byte the original.

    The checksum is over the UNCOMPRESSED payload, which is the thing section 7
    promises to have retained. Checking the compressed blob instead would prove
    only that the compressed bytes survived, which is not the claim.
    """
    payload = decompress(codec, stored, zdict)
    if not expected_sha256:
        return payload
    if isinstance(expected_sha256, str):
        got, want = sha256_hex(payload), expected_sha256
    else:
        got, want = sha256_bytes(payload), bytes(expected_sha256)
    if got != want:
        raise ValueError(
            f"raw frame checksum mismatch: stored sha256 {want!r}, decompressed "
            f"to {got!r} ({len(payload)} bytes) -- the archive does not "
            f"reproduce the original frame"
        )
    return payload
