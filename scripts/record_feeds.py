#!/usr/bin/env python
"""
READ-ONLY capture of Kalshi 15-minute crypto books alongside the underlying
spot feeds, for the "does Kalshi lag the index, and is the lag tradeable"
study.  Analysis lives in scripts/analyze_lag.py; this script only records.

THIS SCRIPT NEVER PLACES AN ORDER.  It calls exactly four Kalshi endpoints,
all of them reads: get_events (ticker discovery), get_market (floor_strike /
settlement / latency probe), the public orderbook via the WS market-data
channel, and websocket PING (latency probe).  There is no import of
kbot.execution, and no code path that reaches KalshiClient.place_order.

What gets recorded
------------------
underlying_quote  best bid / best ask / mid (and sizes) from each constituent
                  exchange's ORDER BOOK feed, on every book update
underlying_tick   the per-feed price series the analysis consumes.  Feed name
                  "<exchange>" is the QUOTE MID; feed name "<exchange>_trade"
                  is the trade print.  Both carry the exchange's own timestamp
                  AND our LOCAL receive timestamp.
composite         the BRTI PROXY, recomputed on every constituent QUOTE update,
                  stored with the source set that went into it
kalshi_book       one row per orderbook snapshot/delta: best bid, best ask,
                  SIZE at each, mid, top-5 levels per side, the message's
                  ts_ms, and our LOCAL receive timestamp
kalshi_raw        the raw JSON of every Kalshi book message (forensics: if an
                  analysis result looks impossible, the bytes are still here)
kalshi_market     floor_strike / open_time / close_time per ticker, plus the
                  settlement backfilled after the window closes
feed_event        reconnects, gaps, stalls, parse failures, delta rejects
excluded_span     spans that analysis must drop, with the reason
latency_sample    read-endpoint and WS-ping round trips (LOWER BOUND on order
                  latency -- we never send an order to measure the real thing)
ntp_offset        clock offset at start and end of the run

LOCAL RECEIVE TIME IS THE COMMON CLOCK.  Exchange timestamps are recorded but
never used to align feeds: they come from four different clocks with unknown
and varying skew, and the question being asked ("could WE have acted on this")
is a question about our own clock anyway.

BRTI honesty
------------
These contracts settle on CF Benchmarks BRTI, a multi-exchange index computed
from order-book depth with a time-weighted smoothing window -- NOT from last
trade prices, and NOT from Coinbase alone.  I could not identify a free
real-time BRTI feed; CF Benchmarks distributes it commercially.  So this
script records a PROXY: the median QUOTE MID (best bid + best ask) / 2 across
the constituent exchanges below.  A proxy is not the index.  Two consequences
the analysis must respect and does:

  * A measured "lag vs Coinbase" that disappears when measured against the
    composite is index basis, not a tradeable lag.
  * A measured "lag vs composite" may still be proxy error, because the real
    BRTI is depth-weighted and time-smoothed while this proxy is top-of-book
    and instantaneous.  The proxy leads the real index on impulse moves.

WHY QUOTES, NOT TRADES (2026-08-11)
-----------------------------------
The first version of this script built the composite from TRADE prints.  A
552-second smoke test measured coinbase 1.29 ticks/s, bitstamp 0.12/s, kraken
0.13/s, gemini 0.21/s: 88.6% of composite rows had fewer than 3 fresh sources.
A trade feed only ticks when someone trades, so on a quiet venue the composite
was interpolating across 5-8 second holes -- useless for resolving a 500ms-3s
lag.  Book feeds update continuously (measured below), and BRTI is itself
constructed from order-book data, so quotes are both faster AND closer to the
settlement object.  Trade prints are still recorded, as "<exchange>_trade".

Measured 60s, BTC, 2026-08-11, per-message inter-arrival gaps:

    channel                    msgs/s   p50     p95     p99     max
    coinbase/level2_batch      15.24     62ms   125ms   172ms   203ms
    gemini/l2                  40.81      0ms   125ms   250ms   828ms
    bitstamp/order_book         9.02    109ms   203ms   219ms   328ms
    kraken/book depth=10        9.90      0ms   563ms  1328ms  4985ms
    kraken/ticker event=bbo     2.69     31ms  1422ms  5297ms  5532ms

Kraken is the slow constituent on either channel, so it gets the `book` channel
(4x the message rate of ticker/bbo) and its own looser gap threshold.  The
staleness and gap thresholds below are set from THIS table; re-measure with
--selftest before trusting them on a different asset or a different day.

If you have a CF Benchmarks API key, point --brti-ws-url / --brti-rest-url at
the real feed; it is recorded as just another feed named "brti_real" and
analyze_lag.py will prefer it over the proxy automatically.

Constituent exchanges used (all public, no auth):
  coinbase, bitstamp, kraken, gemini
These have been CF constituents; the constituent list and weights are CF's to
change, so re-check it against the current CF Benchmarks methodology before
leaning on the composite.  itBit and LMAX are omitted: I found no free public
market-data socket for either.  Four sources satisfies the ">= 3" requirement.

Usage
-----
    python scripts/record_feeds.py --selftest                 # 60s feed check
    python scripts/record_feeds.py --series KXBTC15M --minutes 120
    python scripts/record_feeds.py --series KXETH15M --minutes 120

--selftest touches no Kalshi endpoint and writes no file: it connects the four
constituent books for 60s and prints the per-feed rate table plus composite
freshness.  Run it before any long recording.

Run the two series as separate processes (separate output files); pooling
happens in analyze_lag.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import sqlite3
import statistics
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets.asyncio.client as ws_asyncio

# Allow running straight from a checkout without an editable install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kbot.config import settings  # noqa: E402
from kbot.kalshi_client import KalshiClient  # noqa: E402
# The REAL book maintenance code, imported rather than reimplemented: a
# recording made with a private copy of _apply_delta would prove nothing about
# the books the bot actually trades on.  (This is the function whose previous
# snapshot-shaped version froze every book -- see runner.py's docstring.)
from kbot.runner import _apply_delta, _parse_orderbook  # noqa: E402

# ── Tunables (recording-side only; none of these are strategy parameters) ─────

GAP_THRESHOLD_S = 2.0          # default; per-feed overrides in FEED_CADENCE
STALL_THRESHOLD_S = 60.0       # ticks arriving but value never changing -> stall
KALSHI_STALL_THRESHOLD_S = 120.0   # book messages arriving but book identical
COMPOSITE_STALENESS_S = 1.5    # a constituent older than this is not "fresh"
COMPOSITE_MIN_SOURCES = 3      # fewer fresh sources -> composite is NULL

# Per-feed gap/stall thresholds, set from the measured table in the module
# docstring.  A single global threshold is wrong for quote feeds: 2s of silence
# is a hard fault on coinbase (max observed gap 203ms) and completely routine on
# kraken (max observed 4985ms).  Using one number either misses coinbase outages
# or drowns the log in false kraken exclusions.
#
# gap_s   ~4x the measured max inter-arrival gap: big enough not to fire on
#         normal quiet, small enough that a real disconnect is caught in ~1s.
# stall_s messages arriving but top-of-book frozen.  Top-of-book changes are
#         RARE in a quiet market (0.07-0.23/s measured), so this must be far
#         looser than the gap threshold or a calm minute reads as a fault.
FEED_CADENCE: dict[str, dict[str, float]] = {
    "coinbase": {"gap_s": 1.0, "stall_s": 120.0},
    "bitstamp": {"gap_s": 1.5, "stall_s": 120.0},
    "gemini":   {"gap_s": 3.0, "stall_s": 120.0},
    "kraken":   {"gap_s": 20.0, "stall_s": 300.0},
}

SELFTEST_SECONDS = 60.0
CROSSED_BOOK_LOG_AT = (1, 10, 100, 1000)   # log a crossed book at these counts
QUOTE_STARVED_PER_S = 1.0      # a quote feed below this is broken, not quiet
FRESHNESS_TARGET_PCT = 10.0    # <3-fresh-source rows above this fails selftest
AGREEMENT_WARN_BPS = 20.0      # cross-feed mid dispersion that means a bad book
DISCOVERY_INTERVAL_S = 60.0    # ticker re-discovery (windows roll every 15 min)
SETTLEMENT_INTERVAL_S = 300.0  # settlement backfill sweep
LATENCY_SAMPLES = 50           # per probe kind, at start and again at end
QUEUE_WARN_DEPTH = 20_000      # writer backlog that means we are losing time
TOP_N_LEVELS = 5               # levels stored per side (maker queue modelling)

NTP_SERVERS = ("time.cloudflare.com", "pool.ntp.org", "time.google.com")

SERIES_ASSET = {"KXBTC15M": "BTC", "KXETH15M": "ETH"}

# Per-exchange symbol for each asset.  A missing entry means that exchange is
# skipped for that asset (and that is reported as a starved feed, loudly).
EXCHANGE_SYMBOLS: dict[str, dict[str, str]] = {
    "coinbase": {"BTC": "BTC-USD", "ETH": "ETH-USD"},
    "bitstamp": {"BTC": "btcusd", "ETH": "ethusd"},
    "kraken":   {"BTC": "BTC/USD", "ETH": "ETH/USD"},
    "gemini":   {"BTC": "BTCUSD", "ETH": "ETHUSD"},
}

CONSTITUENTS = tuple(EXCHANGE_SYMBOLS.keys())


def now_ms() -> int:
    """Local wall-clock milliseconds.  THE common clock for this recording."""
    return int(time.time() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


# ── SQLite writer ────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS underlying_tick (
    local_ts_ms INTEGER NOT NULL,
    feed        TEXT    NOT NULL,
    exch_ts_ms  INTEGER,
    price       REAL    NOT NULL,
    size        REAL
);
CREATE INDEX IF NOT EXISTS ix_tick_feed_ts ON underlying_tick(feed, local_ts_ms);

CREATE TABLE IF NOT EXISTS underlying_quote (
    local_ts_ms INTEGER NOT NULL,
    feed        TEXT    NOT NULL,
    exch_ts_ms  INTEGER,
    bid         REAL    NOT NULL,
    ask         REAL    NOT NULL,
    mid         REAL    NOT NULL,
    bid_size    REAL,
    ask_size    REAL
);
CREATE INDEX IF NOT EXISTS ix_quote_feed_ts ON underlying_quote(feed, local_ts_ms);

CREATE TABLE IF NOT EXISTS composite (
    local_ts_ms  INTEGER NOT NULL,
    median_price REAL,
    mean_price   REAL,
    n_sources    INTEGER NOT NULL,
    sources      TEXT    NOT NULL,
    trigger_feed TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_composite_ts ON composite(local_ts_ms);

CREATE TABLE IF NOT EXISTS kalshi_book (
    local_ts_ms   INTEGER NOT NULL,
    msg_ts_ms     INTEGER,
    ticker        TEXT    NOT NULL,
    msg_type      TEXT    NOT NULL,
    seq           INTEGER,
    best_bid      REAL,
    bid_size      INTEGER,
    best_ask      REAL,
    ask_size      INTEGER,
    mid           REAL,
    yes_levels    TEXT,
    no_levels     TEXT,
    applied       INTEGER,
    changed       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_book_ticker_ts ON kalshi_book(ticker, local_ts_ms);

CREATE TABLE IF NOT EXISTS kalshi_raw (
    local_ts_ms INTEGER NOT NULL,
    ticker      TEXT,
    msg_type    TEXT,
    raw         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kalshi_market (
    ticker            TEXT PRIMARY KEY,
    series            TEXT,
    floor_strike      REAL,
    open_ts_ms        INTEGER,
    close_ts_ms       INTEGER,
    first_seen_ts_ms  INTEGER,
    status            TEXT,
    result            TEXT,
    expiration_value  TEXT,
    settled_ts_ms     INTEGER
);

CREATE TABLE IF NOT EXISTS feed_event (
    local_ts_ms INTEGER NOT NULL,
    feed        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS excluded_span (
    feed        TEXT    NOT NULL,
    start_ts_ms INTEGER NOT NULL,
    end_ts_ms   INTEGER NOT NULL,
    reason      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS latency_sample (
    local_ts_ms INTEGER NOT NULL,
    phase       TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    rtt_ms      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS ntp_offset (
    local_ts_ms INTEGER NOT NULL,
    phase       TEXT    NOT NULL,
    server      TEXT,
    offset_ms   REAL,
    error       TEXT
);
"""

_INSERTS = {
    "underlying_tick": "INSERT INTO underlying_tick VALUES (?,?,?,?,?)",
    "underlying_quote": "INSERT INTO underlying_quote VALUES (?,?,?,?,?,?,?,?)",
    "composite": "INSERT INTO composite VALUES (?,?,?,?,?,?)",
    "kalshi_book": "INSERT INTO kalshi_book VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    "kalshi_raw": "INSERT INTO kalshi_raw VALUES (?,?,?,?)",
    "feed_event": "INSERT INTO feed_event VALUES (?,?,?,?)",
    "excluded_span": "INSERT INTO excluded_span VALUES (?,?,?,?)",
    "latency_sample": "INSERT INTO latency_sample VALUES (?,?,?,?)",
    "ntp_offset": "INSERT INTO ntp_offset VALUES (?,?,?,?,?)",
}


class Recorder:
    """
    Single-writer sqlite sink fed by an asyncio queue.

    One writer, batched commits: sqlite from many coroutines is a lock fight,
    and a lock fight shows up as latency in the WS read loops -- which is the
    exact quantity this recording is trying to measure.
    """

    def __init__(self, path: Path, batch_commit_s: float = 1.0) -> None:
        self.path = path
        self._q: asyncio.Queue[tuple[str, tuple] | None] = asyncio.Queue()
        self._batch_commit_s = batch_commit_s
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.rows_written = 0
        self._warned_depth = False

    def put(self, table: str, row: tuple) -> None:
        self._q.put_nowait((table, row))
        depth = self._q.qsize()
        if depth > QUEUE_WARN_DEPTH and not self._warned_depth:
            self._warned_depth = True
            print(
                f"!! WRITER BACKLOG {depth} rows -- disk cannot keep up with the "
                f"feeds.  Timestamps are still capture-time so data is not wrong, "
                f"but investigate before trusting throughput numbers.",
                flush=True,
            )

    def set_meta(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value))
        )
        self._conn.commit()

    def upsert_market(self, ticker: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=excluded.{k}" for k in fields)
        keys = ["ticker", *fields.keys()]
        placeholders = ",".join("?" * len(keys))
        self._conn.execute(
            f"INSERT INTO kalshi_market ({','.join(keys)}) VALUES ({placeholders}) "
            f"ON CONFLICT(ticker) DO UPDATE SET {cols}",
            (ticker, *fields.values()),
        )
        self._conn.commit()

    def known_markets(self) -> list[tuple]:
        cur = self._conn.execute(
            "SELECT ticker, close_ts_ms, result FROM kalshi_market"
        )
        return cur.fetchall()

    async def run(self) -> None:
        last_commit = time.monotonic()
        try:
            while True:
                item = await self._q.get()
                if item is None:
                    break
                table, row = item
                self._conn.execute(_INSERTS[table], row)
                self.rows_written += 1
                if time.monotonic() - last_commit >= self._batch_commit_s:
                    self._conn.commit()
                    last_commit = time.monotonic()
        finally:
            self._conn.commit()

    async def close(self) -> None:
        # Drain whatever is still queued before shutting the writer down --
        # dropping the tail would silently truncate the recording.
        await self._q.put(None)
        while not self._q.empty():
            await asyncio.sleep(0.05)
        self._conn.commit()
        self._conn.close()


# ── Feed health tracking ─────────────────────────────────────────────────────

@dataclass
class FeedHealth:
    """
    Per-feed liveness/sanity state.  Everything here exists because a silent
    data bug once invalidated months of results: the recording must say when
    it was blind, so the analysis can drop those spans instead of averaging
    through them.
    """
    name: str
    ticks: int = 0
    parse_failures: int = 0
    reconnects: int = 0
    last_tick_ms: int | None = None
    last_value: Any = None
    last_change_ms: int | None = None
    in_gap: bool = False
    gap_start_ms: int | None = None
    in_stall: bool = False
    stall_start_ms: int | None = None
    stall_threshold_s: float = STALL_THRESHOLD_S
    gap_threshold_s: float = GAP_THRESHOLD_S
    excluded_ms: int = 0
    recent_intervals: deque = field(default_factory=lambda: deque(maxlen=500))

    def observe(self, ts_ms: int, value: Any) -> None:
        if self.last_tick_ms is not None:
            self.recent_intervals.append(ts_ms - self.last_tick_ms)
        self.ticks += 1
        self.last_tick_ms = ts_ms
        if value != self.last_value:
            self.last_value = value
            self.last_change_ms = ts_ms


class HealthMonitor:
    """Watches every feed for gaps and stalls; writes excluded spans."""

    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.feeds: dict[str, FeedHealth] = {}

    def register(
        self,
        name: str,
        stall_threshold_s: float = STALL_THRESHOLD_S,
        gap_threshold_s: float = GAP_THRESHOLD_S,
    ) -> FeedHealth:
        h = FeedHealth(
            name=name,
            stall_threshold_s=stall_threshold_s,
            gap_threshold_s=gap_threshold_s,
        )
        self.feeds[name] = h
        return h

    def event(self, feed: str, kind: str, detail: str = "") -> None:
        self.rec.put("feed_event", (now_ms(), feed, kind, detail))
        print(f"[{iso(now_ms())}] {feed}: {kind} {detail}", flush=True)

    def exclude(self, feed: str, start_ms: int, end_ms: int, reason: str) -> None:
        if end_ms <= start_ms:
            return
        self.rec.put("excluded_span", (feed, start_ms, end_ms, reason))
        h = self.feeds.get(feed)
        if h:
            h.excluded_ms += end_ms - start_ms
        print(
            f"[{iso(end_ms)}] EXCLUDED {feed} {(end_ms - start_ms) / 1000:.1f}s "
            f"({reason}) from {iso(start_ms)}",
            flush=True,
        )

    async def run(self, stop_at: float) -> None:
        while time.monotonic() < stop_at:
            await asyncio.sleep(0.5)
            t = now_ms()
            for h in self.feeds.values():
                if h.last_tick_ms is None:
                    continue
                silent_s = (t - h.last_tick_ms) / 1000.0
                if silent_s > h.gap_threshold_s and not h.in_gap:
                    h.in_gap = True
                    h.gap_start_ms = h.last_tick_ms
                    self.event(h.name, "gap_open", f"silent {silent_s:.1f}s")
                elif h.in_gap and silent_s <= h.gap_threshold_s:
                    h.in_gap = False
                    self.exclude(
                        h.name, h.gap_start_ms or t, t, f"gap>{h.gap_threshold_s:g}s"
                    )
                    self.event(h.name, "gap_close", "")

                # Stall: messages ARE arriving but the value never changes.
                # This is the frozen-orderbook signature.
                if h.last_change_ms is not None and not h.in_gap:
                    frozen_s = (t - h.last_change_ms) / 1000.0
                    if frozen_s > h.stall_threshold_s and not h.in_stall:
                        h.in_stall = True
                        h.stall_start_ms = h.last_change_ms
                        self.event(
                            h.name, "stall_open",
                            f"value unchanged {frozen_s:.0f}s while ticks flowed",
                        )
                    elif h.in_stall and frozen_s <= h.stall_threshold_s:
                        h.in_stall = False
                        self.exclude(h.name, h.stall_start_ms or t, t, "stalled_value")
                        self.event(h.name, "stall_close", "")

        # Close out anything still open at the end of the run.
        t = now_ms()
        for h in self.feeds.values():
            if h.in_gap and h.gap_start_ms:
                self.exclude(
                    h.name, h.gap_start_ms, t, f"gap>{h.gap_threshold_s:g}s (open at end)"
                )
            if h.in_stall and h.stall_start_ms:
                self.exclude(h.name, h.stall_start_ms, t, "stalled_value (open at end)")


# ── Underlying constituent feeds ─────────────────────────────────────────────

class Composite:
    """
    The BRTI PROXY: median (and mean) of the constituent exchanges' latest
    QUOTE MIDS, over sources fresh within COMPOSITE_STALENESS_S.

    Median, not mean, as the headline: one exchange showing a stale or
    dislocated quote should not drag the composite, and the real BRTI likewise
    has outlier protection.  Both are stored -- if they disagree materially,
    the analysis will show it, and that disagreement is itself a warning that
    a constituent is misbehaving.

    FRESHNESS IS MESSAGE ARRIVAL, NOT PRICE CHANGE.  A book update that touches
    only deep levels still proves the top of book is unchanged AS OF NOW, so it
    refreshes the source.  Requiring the mid to move instead would mark every
    calm venue stale (top-of-book changes ran 0.07-0.23/s in the measurement)
    and would recreate the exact failure this rewrite fixes.
    """

    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.latest: dict[str, tuple[int, float]] = {}
        self.rows = 0
        self.rows_insufficient = 0
        self.n_histogram: dict[int, int] = {}
        # Running mean per feed, for the cross-feed agreement check.  O(1) and
        # no growth, so it is safe to leave on for a multi-hour recording.
        self.feed_sum: dict[str, float] = {}
        self.feed_count: dict[str, int] = {}

    def update(self, feed: str, ts_ms: int, mid: float) -> tuple[float | None, int]:
        self.latest[feed] = (ts_ms, mid)
        self.feed_sum[feed] = self.feed_sum.get(feed, 0.0) + mid
        self.feed_count[feed] = self.feed_count.get(feed, 0) + 1
        cutoff = ts_ms - int(COMPOSITE_STALENESS_S * 1000)
        fresh = {f: p for f, (t, p) in self.latest.items() if t >= cutoff}
        n = len(fresh)
        self.n_histogram[n] = self.n_histogram.get(n, 0) + 1
        if n >= COMPOSITE_MIN_SOURCES:
            med = statistics.median(fresh.values())
            avg = sum(fresh.values()) / n
        else:
            med = avg = None
            self.rows_insufficient += 1
        self.rows += 1
        self.rec.put(
            "composite",
            (ts_ms, med, avg, n, ",".join(sorted(fresh)), feed),
        )
        return med, n


class QuoteBook:
    """
    Price -> size maps per side, with a cached top of book.

    Coinbase's opening snapshot is ~45k levels and it sends ~15 updates/s, so
    recomputing min(asks)/max(bids) on every message would burn real CPU inside
    the loop whose latency this script exists to measure.  Instead the best is
    cached and only recomputed when an update actually removes or improves it,
    which is the rare case.
    """

    __slots__ = ("bids", "asks", "_best_bid", "_best_ask")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self._best_bid: float | None = None
        self._best_ask: float | None = None

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self._best_bid = self._best_ask = None

    def set(self, is_bid: bool, price: float, size: float) -> None:
        book = self.bids if is_bid else self.asks
        if size <= 0:
            if book.pop(price, None) is not None:
                best = self._best_bid if is_bid else self._best_ask
                if best is not None and price == best:
                    # The level we were quoting just vanished -> must rescan.
                    if is_bid:
                        self._best_bid = None
                    else:
                        self._best_ask = None
            return
        book[price] = size
        if is_bid:
            if self._best_bid is None or price > self._best_bid:
                self._best_bid = price
        else:
            if self._best_ask is None or price < self._best_ask:
                self._best_ask = price

    def trim(self, depth: int) -> None:
        """Keep only the top `depth` levels a side (Kraken's book window)."""
        if len(self.bids) > depth:
            for p in sorted(self.bids, reverse=True)[depth:]:
                del self.bids[p]
            self._best_bid = None
        if len(self.asks) > depth:
            for p in sorted(self.asks)[depth:]:
                del self.asks[p]
            self._best_ask = None

    def top(self) -> tuple[float, float, float, float] | None:
        """-> (bid, ask, bid_size, ask_size), or None if a side is empty."""
        if self._best_bid is None and self.bids:
            self._best_bid = max(self.bids)
        if self._best_ask is None and self.asks:
            self._best_ask = min(self.asks)
        bb, ba = self._best_bid, self._best_ask
        if bb is None or ba is None:
            return None
        return bb, ba, self.bids[bb], self.asks[ba]


KRAKEN_BOOK_DEPTH = 10


class ConstituentFeed:
    """
    One public exchange WS carrying BOTH that exchange's order book and its
    trade prints, on a single connection.

    The book drives the composite; the trades are recorded alongside under the
    feed name "<exchange>_trade" so the old trade-based view stays available
    for comparison without a second socket.

    Three of the four exchanges send incremental book deltas and need a local
    book (QuoteBook).  Bitstamp is the exception: it re-sends the whole top-100
    on every change, so its top of book is read straight off the message.

    Wire formats verified live against each exchange on 2026-08-11; every
    branch is defensive, because a wire-format change must show up as a
    parse-failure count in the QC report, never as a silently empty feed.
    (Silently empty is how the frozen books went unnoticed.)
    """

    def __init__(
        self,
        name: str,
        symbol: str,
        rec: Recorder,
        mon: HealthMonitor,
        comp: Composite,
        record_trades: bool = True,
    ) -> None:
        self.name = name
        self.symbol = symbol
        self.rec = rec
        self.mon = mon
        self.comp = comp
        self.record_trades = record_trades
        cad = FEED_CADENCE.get(name, {})
        self.health = mon.register(
            name,
            stall_threshold_s=cad.get("stall_s", STALL_THRESHOLD_S),
            gap_threshold_s=cad.get("gap_s", GAP_THRESHOLD_S),
        )
        # Trades are recorded but are NOT expected to be timely, so they get a
        # health row with thresholds that will never fire -- a quiet venue is
        # not a fault, it is the reason we stopped using trades.
        self.trade_health = mon.register(
            f"{name}_trade", stall_threshold_s=1e9, gap_threshold_s=1e9
        )
        self.book = QuoteBook()
        self.have_snapshot = False
        self.crossed = 0
        self.quotes = 0

    # -- per-exchange wire details --------------------------------------------

    def url(self) -> str:
        return {
            "coinbase": "wss://ws-feed.exchange.coinbase.com",
            "bitstamp": "wss://ws.bitstamp.net",
            "kraken": "wss://ws.kraken.com/v2",
            "gemini": "wss://api.gemini.com/v2/marketdata",
        }[self.name]

    def subscribe_payloads(self) -> list[dict]:
        """
        Quote channel first, trade channel second, on the same socket.

        coinbase  level2_batch is the UNAUTHENTICATED level2; the server
                  acknowledges it as "level2_50" (50 levels a side).  Plain
                  "level2" requires auth.  "ticker" carries the trade print.
        bitstamp  order_book_<pair> is the top-100 snapshot stream (not
                  diff_order_book, which would need delta bookkeeping for no
                  gain here); live_trades_<pair> carries the prints.
        kraken    book depth=10 rather than ticker/event_trigger=bbo: measured
                  9.9 msg/s vs 2.7, and p95 inter-arrival 563ms vs 1422ms.
                  ticker/bbo publishes ONLY on a BBO change, so a calm minute
                  looks identical to a dead socket.
        gemini    the v2 "l2" subscription carries both l2_updates and trade
                  messages; there is no separate top-of-book channel in v2.
        """
        if self.name == "coinbase":
            return [{
                "type": "subscribe",
                "product_ids": [self.symbol],
                "channels": ["level2_batch"] + (["ticker"] if self.record_trades else []),
            }]
        if self.name == "bitstamp":
            subs = [{"event": "bts:subscribe",
                     "data": {"channel": f"order_book_{self.symbol}"}}]
            if self.record_trades:
                subs.append({"event": "bts:subscribe",
                             "data": {"channel": f"live_trades_{self.symbol}"}})
            return subs
        if self.name == "kraken":
            subs = [{"method": "subscribe",
                     "params": {"channel": "book", "symbol": [self.symbol],
                                "depth": KRAKEN_BOOK_DEPTH, "snapshot": True}}]
            if self.record_trades:
                subs.append({"method": "subscribe",
                             "params": {"channel": "trade", "symbol": [self.symbol]}})
            return subs
        if self.name == "gemini":
            return [{"type": "subscribe",
                     "subscriptions": [{"name": "l2", "symbols": [self.symbol]}]}]
        raise ValueError(self.name)

    # -- message decoding ------------------------------------------------------

    def decode(self, msg: Any) -> tuple[
        tuple[int | None, float, float, float | None, float | None] | None,
        list[tuple[int | None, float, float | None]],
    ]:
        """
        -> (quote, trades) where quote is (exch_ts_ms, bid, ask, bid_sz, ask_sz)
        or None, and trades is [(exch_ts_ms, price, size)].

        Applies book deltas as a side effect.  A message that only touches deep
        levels still returns a quote: that is what keeps the source FRESH.
        """
        no_trades: list[tuple[int | None, float, float | None]] = []
        if not isinstance(msg, dict):
            return None, no_trades

        if self.name == "coinbase":
            mtype = msg.get("type")
            if mtype == "snapshot":
                self.book.clear()
                for p, q in msg.get("bids") or []:
                    self.book.set(True, float(p), float(q))
                for p, q in msg.get("asks") or []:
                    self.book.set(False, float(p), float(q))
                self.have_snapshot = True
            elif mtype == "l2update":
                if not self.have_snapshot:
                    return None, no_trades
                for side, p, q in msg.get("changes") or []:
                    self.book.set(side == "buy", float(p), float(q))
            elif mtype == "ticker" and msg.get("price") is not None:
                return None, [(_iso_to_ms(msg.get("time")), float(msg["price"]),
                               _maybe_float(msg.get("last_size")))]
            else:
                return None, no_trades
            return self._book_quote(_iso_to_ms(msg.get("time"))), no_trades

        if self.name == "bitstamp":
            channel = str(msg.get("channel") or "")
            event = msg.get("event")
            if event == "data" and channel.startswith("order_book_"):
                d = msg.get("data") or {}
                bids, asks = d.get("bids") or [], d.get("asks") or []
                if not bids or not asks:
                    return None, no_trades
                micro = d.get("microtimestamp")
                ets = int(int(micro) // 1000) if micro is not None else None
                # max/min rather than [0]: Bitstamp does send these sorted, but
                # depending on that ordering is a silent-wrong-answer risk for
                # no measurable saving over 100 elements.
                bb = max(bids, key=lambda lvl: float(lvl[0]))
                ba = min(asks, key=lambda lvl: float(lvl[0]))
                self.have_snapshot = True
                return (ets, float(bb[0]), float(ba[0]),
                        _maybe_float(bb[1]), _maybe_float(ba[1])), no_trades
            if event == "trade" and channel.startswith("live_trades_"):
                d = msg.get("data") or {}
                micro = d.get("microtimestamp")
                ets = int(int(micro) // 1000) if micro is not None else None
                return None, [(ets, float(d["price"]), _maybe_float(d.get("amount")))]
            return None, no_trades

        if self.name == "kraken":
            channel, mtype = msg.get("channel"), msg.get("type")
            if channel == "book" and mtype in ("snapshot", "update"):
                if mtype == "snapshot":
                    self.book.clear()
                    self.have_snapshot = True
                elif not self.have_snapshot:
                    return None, no_trades
                ets = None
                for d in msg.get("data") or []:
                    for lvl in d.get("bids") or []:
                        self.book.set(True, float(lvl["price"]), float(lvl["qty"]))
                    for lvl in d.get("asks") or []:
                        self.book.set(False, float(lvl["price"]), float(lvl["qty"]))
                    ets = _iso_to_ms(d.get("timestamp")) or ets
                # Kraken serves a fixed-depth window; without trimming, levels
                # that fell out of it would linger and could out-quote the real
                # best forever.
                self.book.trim(KRAKEN_BOOK_DEPTH)
                return self._book_quote(ets), no_trades
            if channel == "trade" and mtype in ("snapshot", "update"):
                return None, [
                    (_iso_to_ms(t.get("timestamp")), float(t["price"]),
                     _maybe_float(t.get("qty")))
                    for t in msg.get("data") or []
                ]
            return None, no_trades

        if self.name == "gemini":
            mtype = msg.get("type")
            if mtype == "l2_updates":
                for side, p, q in msg.get("changes") or []:
                    self.book.set(side == "buy", float(p), float(q))
                # The FIRST l2_updates also carries a `trades` array of the last
                # 100 HISTORICAL prints.  Recording those as live ticks would
                # backdate 100 trades onto this instant; they are dropped, and
                # only the standalone type=="trade" messages count.
                self.have_snapshot = True
                return self._book_quote(None), no_trades
            if mtype == "trade" and msg.get("price") is not None:
                ets = _maybe_int(msg.get("timestamp"))
                return None, [(ets, float(msg["price"]),
                               _maybe_float(msg.get("quantity")))]
            return None, no_trades

        raise ValueError(self.name)

    def _book_quote(self, exch_ts_ms: int | None):
        top = self.book.top()
        if top is None:
            return None
        bid, ask, bid_sz, ask_sz = top
        return exch_ts_ms, bid, ask, bid_sz, ask_sz

    # -- run loop --------------------------------------------------------------

    async def run(self, stop_at: float) -> None:
        attempt = 0
        while time.monotonic() < stop_at:
            try:
                # max_size=None: Coinbase's opening level2 snapshot is ~45k
                # levels and blows past the 1 MiB default frame limit, which
                # would drop the connection before the first quote.
                async with ws_asyncio.connect(
                    self.url(), open_timeout=20, max_size=None
                ) as ws:
                    self.mon.event(self.name, "ws_connected", self.symbol)
                    # A reconnect invalidates the local book: replay must start
                    # from the new snapshot, never from the pre-gap state.
                    self.book.clear()
                    self.have_snapshot = False
                    for payload in self.subscribe_payloads():
                        await ws.send(json.dumps(payload))
                    attempt = 0
                    async for raw in ws:
                        if time.monotonic() >= stop_at:
                            return
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.reconnects += 1
                self.mon.event(
                    self.name, "ws_reconnect",
                    f"{type(exc).__name__}: {str(exc)[:120]}",
                )
                attempt = min(attempt + 1, 6)
                await asyncio.sleep(min(30.0, 2 ** attempt * 0.5))

    def _handle(self, raw: str | bytes) -> None:
        ts = now_ms()
        try:
            msg = json.loads(raw)
            quote, trades = self.decode(msg)
        except Exception as exc:  # noqa: BLE001
            self.health.parse_failures += 1
            if self.health.parse_failures in CROSSED_BOOK_LOG_AT:
                self.mon.event(
                    self.name, "parse_failure",
                    f"n={self.health.parse_failures} {type(exc).__name__} {str(raw)[:160]}",
                )
            return

        if quote is not None:
            exch_ts, bid, ask, bid_sz, ask_sz = quote
            if not (bid > 0 and ask > 0) or not (math.isfinite(bid) and math.isfinite(ask)):
                self.health.parse_failures += 1
            elif bid > ask:
                # Crossed book = our local replay has diverged from theirs.
                # Drop the quote rather than feed a fictional mid to the index.
                self.crossed += 1
                if self.crossed in CROSSED_BOOK_LOG_AT:
                    self.mon.event(self.name, "crossed_book",
                                   f"n={self.crossed} bid={bid} ask={ask}")
            else:
                mid = (bid + ask) / 2.0
                self.quotes += 1
                self.rec.put("underlying_quote",
                             (ts, self.name, exch_ts, bid, ask, mid, bid_sz, ask_sz))
                # Mirrored into underlying_tick under the plain exchange name so
                # analyze_lag.py's per-feed series is the QUOTE MID with no
                # change on its side.
                self.rec.put("underlying_tick", (ts, self.name, exch_ts, mid, None))
                self.health.observe(ts, (bid, ask))
                self.comp.update(self.name, ts, mid)

        if not self.record_trades:
            return
        for exch_ts, price, size in trades:
            if not (price > 0) or not math.isfinite(price):
                self.trade_health.parse_failures += 1
                continue
            self.rec.put("underlying_tick",
                         (ts, f"{self.name}_trade", exch_ts, price, size))
            self.trade_health.observe(ts, price)


def _maybe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_to_ms(v: Any) -> int | None:
    if not isinstance(v, str):
        return None
    try:
        s = v.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


class BrtiRealFeed:
    """
    Optional REAL BRTI feed, if you have a CF Benchmarks entitlement.

    Recorded under the feed name "brti_real"; analyze_lag.py prefers it over
    the proxy when present.  --brti-rest-url is polled at --brti-poll-s; the
    price is read from the first numeric field found among a few common key
    names, and any failure is a loud parse_failure, not a silent skip.
    """

    def __init__(self, url: str, poll_s: float, rec: Recorder, mon: HealthMonitor) -> None:
        self.url = url
        self.poll_s = poll_s
        self.rec = rec
        self.mon = mon
        self.health = mon.register("brti_real")

    async def run(self, stop_at: float) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < stop_at:
                try:
                    resp = await client.get(self.url)
                    resp.raise_for_status()
                    data = resp.json()
                    price = _dig_price(data)
                    if price is None:
                        raise ValueError(f"no price field in {str(data)[:200]}")
                    ts = now_ms()
                    self.rec.put("underlying_tick", (ts, "brti_real", None, price, None))
                    self.health.observe(ts, price)
                except Exception as exc:  # noqa: BLE001
                    self.health.parse_failures += 1
                    if self.health.parse_failures in (1, 10, 100):
                        self.mon.event("brti_real", "poll_failure", str(exc)[:160])
                await asyncio.sleep(self.poll_s)


def _dig_price(obj: Any, depth: int = 0) -> float | None:
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for key in ("value", "price", "rate", "level", "index", "last"):
            if key in obj:
                v = _maybe_float(obj[key])
                if v and v > 0:
                    return v
        for v in obj.values():
            got = _dig_price(v, depth + 1)
            if got:
                return got
    if isinstance(obj, list) and obj:
        return _dig_price(obj[0], depth + 1)
    return None


# ── Kalshi book recording ────────────────────────────────────────────────────

class KalshiRecorder:
    """
    Maintains live books with the REAL runner code and writes a row per
    message.

    Book convention (from kbot.models.types.OrderBook): both sides are stored
    as ASKS.  best_ask = cheapest YES ask; best_bid = 1 - cheapest NO ask.
    Recording bid/ask/size separately (rather than only the mid) is the whole
    point: mid is not tradeable and paper fills at mid are exactly how the
    last study overstated live results by 16 points.
    """

    def __init__(self, rec: Recorder, mon: HealthMonitor) -> None:
        self.rec = rec
        self.mon = mon
        self.books: dict[str, Any] = {}
        self.health = mon.register("kalshi", stall_threshold_s=KALSHI_STALL_THRESHOLD_S)
        self.snapshots = 0
        self.deltas = 0
        self.delta_rejects = 0
        self.delta_nochange = 0
        self.before_snapshot = 0

    def on_message(self, msg: dict[str, Any]) -> None:
        ts = now_ms()
        msg_type = msg.get("type")
        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
            return
        data = msg.get("msg", {}) or {}
        ticker = data.get("market_ticker", "")

        self.rec.put("kalshi_raw", (ts, ticker, msg_type, json.dumps(msg)))

        applied = 0
        if msg_type == "orderbook_snapshot":
            self.books[ticker] = _parse_orderbook(ticker, data)
            self.snapshots += 1
            applied = 1
        else:
            self.deltas += 1
            book = self.books.get(ticker)
            if book is None:
                self.before_snapshot += 1
                return
            before = self._digest(book)
            applied = 1 if _apply_delta(book, data) else 0
            if not applied:
                self.delta_rejects += 1
                if self.delta_rejects in (1, 10, 100):
                    self.mon.event(
                        "kalshi", "delta_rejected",
                        f"n={self.delta_rejects} keys={sorted(map(str, data))}",
                    )
            elif self._digest(book) == before:
                self.delta_nochange += 1

        book = self.books.get(ticker)
        if book is None:
            return
        bb, bs, ba, asz, mid = self._top(book)
        changed = 1 if (ticker, bb, bs, ba, asz) != getattr(self, "_last_top", None) else 0
        self._last_top = (ticker, bb, bs, ba, asz)

        self.rec.put(
            "kalshi_book",
            (
                ts,
                _maybe_int(data.get("ts_ms")) or _sec_to_ms(data.get("ts")),
                ticker,
                msg_type,
                _maybe_int(msg.get("seq")),
                bb, bs, ba, asz, mid,
                json.dumps(self._levels(book.yes)),
                json.dumps(self._levels(book.no)),
                applied,
                changed,
            ),
        )
        # Feed health keys on the full top-of-book, so a book that keeps
        # receiving deltas while never changing registers as a STALL.
        self.health.observe(ts, (ticker, bb, bs, ba, asz))

    @staticmethod
    def _levels(side: list) -> list[list[float]]:
        best = sorted(side, key=lambda l: l.price)[:TOP_N_LEVELS]
        return [[round(l.price, 4), int(l.quantity)] for l in best]

    @staticmethod
    def _digest(book: Any) -> tuple:
        return (
            tuple(sorted((l.price, l.quantity) for l in book.yes)),
            tuple(sorted((l.price, l.quantity) for l in book.no)),
        )

    @staticmethod
    def _top(book: Any):
        by = book.best_yes()
        bn = book.best_no()
        best_ask = by.price if by else None
        ask_size = by.quantity if by else None
        best_bid = round(1.0 - bn.price, 4) if bn else None
        bid_size = bn.quantity if bn else None
        mid = book.mid_price()
        return best_bid, bid_size, best_ask, ask_size, mid


def _maybe_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _sec_to_ms(v: Any) -> int | None:
    n = _maybe_int(v)
    return n * 1000 if n is not None else None


# ── NTP + latency probes ─────────────────────────────────────────────────────

def ntp_offset_ms(server: str, timeout: float = 5.0) -> float:
    """
    Local-clock offset vs `server`, in ms (positive = local clock ahead).

    Minimal SNTP client rather than a dependency: the recording only needs to
    know whether our clock DRIFTED during the run, because every cross-feed
    conclusion rests on local receive time being a consistent ruler.
    """
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(packet, (server, 123))
        data, _ = s.recvfrom(512)
        t3 = time.time()
    unpacked = struct.unpack("!12I", data[:48])
    ntp_epoch_offset = 2208988800
    t1 = unpacked[8] + float(unpacked[9]) / 2**32 - ntp_epoch_offset   # server recv
    t2 = unpacked[10] + float(unpacked[11]) / 2**32 - ntp_epoch_offset  # server xmit
    offset = ((t1 - t0) + (t2 - t3)) / 2
    return -offset * 1000.0


def log_ntp(rec: Recorder, phase: str) -> None:
    for server in NTP_SERVERS:
        try:
            off = ntp_offset_ms(server)
            rec.put("ntp_offset", (now_ms(), phase, server, off, None))
            print(f"NTP [{phase}] {server}: local clock offset {off:+.1f} ms", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            rec.put("ntp_offset", (now_ms(), phase, server, None, str(exc)[:120]))
    print(
        f"!! NTP [{phase}] UNAVAILABLE from all of {NTP_SERVERS}.  Cross-feed "
        f"timing still works (one local clock) but absolute timestamps are "
        f"unverified and clock DRIFT during the run is unmeasured.",
        flush=True,
    )


async def sample_latency(
    rec: Recorder, client: KalshiClient, ticker: str, ws_holder, phase: str
) -> None:
    """
    LOWER BOUND on order latency.  Two probes:
      read_rtt  -- a GET /markets/{ticker} round trip (auth + TLS + Kalshi
                   read path).  An order goes to a different, slower path
                   (write, risk checks, matching), so this UNDERSTATES it.
      ws_ping   -- websocket PING/PONG on the live market-data socket.
                   Pure network+frame turnaround; understates it further.
    Neither sends an order.  Nothing here can.
    """
    loop = asyncio.get_running_loop()
    for _ in range(LATENCY_SAMPLES):
        t0 = time.perf_counter()
        try:
            await loop.run_in_executor(None, client.get_market, ticker)
            rtt = (time.perf_counter() - t0) * 1000
            rec.put("latency_sample", (now_ms(), phase, "read_rtt", rtt))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)

    ws = getattr(ws_holder, "_ws", None)
    if ws is None:
        print(f"latency[{phase}]: WS not connected, skipping ws_ping samples", flush=True)
        return
    for _ in range(LATENCY_SAMPLES):
        ws = getattr(ws_holder, "_ws", None)
        if ws is None:
            break
        try:
            t0 = time.perf_counter()
            pong = await ws.ping()
            await asyncio.wait_for(pong, timeout=5)
            rec.put("latency_sample", (now_ms(), phase, "ws_ping", (time.perf_counter() - t0) * 1000))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)


# ── Kalshi discovery / settlement (both read-only) ───────────────────────────

async def discovery_loop(
    rec: Recorder, client: KalshiClient, ws_client, series: str, stop_at: float, state: dict
) -> None:
    """
    Subscribe to CONSECUTIVE windows as they open, so the recording spans
    several 15-minute contracts rather than one.  Same call the runner uses.
    """
    loop = asyncio.get_running_loop()
    while time.monotonic() < stop_at:
        try:
            events = await loop.run_in_executor(
                None, lambda: client.get_events(series_ticker=series, status="open", limit=10)
            )
            found: list[str] = []
            for event in events or []:
                for m in event.get("markets", []):
                    t = m.get("ticker")
                    if not t:
                        continue
                    found.append(t)
                    if t not in state["markets"]:
                        state["markets"].add(t)
                        rec.upsert_market(
                            t,
                            series=series,
                            floor_strike=_maybe_float(m.get("floor_strike")),
                            open_ts_ms=_iso_to_ms(m.get("open_time")),
                            close_ts_ms=_iso_to_ms(m.get("close_time")),
                            first_seen_ts_ms=now_ms(),
                            status=str(m.get("status") or ""),
                        )
                        if m.get("floor_strike") is None:
                            print(
                                f"!! {t}: NO floor_strike in the market record.  "
                                f"Implied probability cannot be computed for this "
                                f"window; analysis will drop it.",
                                flush=True,
                            )
            new = await ws_client.subscribe_additional(found)
            if new:
                print(f"[{iso(now_ms())}] subscribed {len(new)} new ticker(s): {new}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"!! discovery failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        await asyncio.sleep(DISCOVERY_INTERVAL_S)


async def settlement_loop(rec: Recorder, client: KalshiClient, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        await asyncio.sleep(SETTLEMENT_INTERVAL_S)
        await backfill_settlements(rec, client)


async def backfill_settlements(rec: Recorder, client: KalshiClient) -> None:
    """
    Read `result` / `expiration_value` for every closed window we recorded.

    Read-only (get_market).  NOTE, because this repo has been bitten by it:
    expiration_value on these markets is a PRICE, not a payout -- the
    settlement decision is `result`.
    """
    loop = asyncio.get_running_loop()
    t = now_ms()
    for ticker, close_ms, result in rec.known_markets():
        if result:
            continue
        if close_ms is None or t < close_ms + 120_000:
            continue
        try:
            m = await loop.run_in_executor(None, client.get_market, ticker)
        except Exception as exc:  # noqa: BLE001
            print(f"!! settlement fetch failed {ticker}: {str(exc)[:120]}", flush=True)
            continue
        res = str(m.get("result") or "").strip().lower()
        if res in ("yes", "no"):
            rec.upsert_market(
                ticker,
                status=str(m.get("status") or ""),
                result=res,
                expiration_value=str(m.get("expiration_value") or ""),
                settled_ts_ms=now_ms(),
            )
            print(f"[{iso(now_ms())}] settled {ticker}: {res}", flush=True)


# ── Final report ─────────────────────────────────────────────────────────────

def final_report(
    mon: HealthMonitor, kal: KalshiRecorder, comp: Composite,
    rec: Recorder, started_ms: int, out: Path, feeds: list["ConstituentFeed"],
) -> None:
    dur_s = (now_ms() - started_ms) / 1000.0
    # Trade feeds are secondary and ARE slow by nature -- that is the finding
    # that motivated the quote rewrite, not a fault to alarm on every run.
    starved = [
        h.name for h in mon.feeds.values()
        if not h.name.endswith("_trade")
        and (h.ticks == 0 or (h.ticks / max(dur_s, 1)) < QUOTE_STARVED_PER_S)
    ]

    print("\n" + "=" * 78)
    if starved:
        print("!! STARVED FEEDS (read this before using the recording) !!")
        for name in starved:
            h = mon.feeds[name]
            print(f"   {name}: {h.ticks} ticks in {dur_s:.0f}s  "
                  f"({h.ticks / max(dur_s,1):.3f}/s), reconnects={h.reconnects}, "
                  f"parse_failures={h.parse_failures}")
        print("   A composite computed without these is a DIFFERENT index.")
    else:
        print("All feeds delivered data.")
    print("=" * 78)

    print(f"\nRECORDING SUMMARY  ({dur_s / 60:.1f} min)  -> {out}")
    print_rate_table(mon, feeds, dur_s)

    print(f"\nKalshi: snapshots={kal.snapshots} deltas={kal.deltas} "
          f"rejected={kal.delta_rejects} no-op={kal.delta_nochange} "
          f"pre-snapshot-dropped={kal.before_snapshot} books={len(kal.books)}")
    if kal.deltas and kal.delta_rejects / kal.deltas > 0.01:
        print("!! >1% of deltas were REJECTED by _apply_delta -- the wire shape may "
              "have changed again.  Do not analyse this recording until resolved.")
    if kal.deltas and kal.delta_nochange / kal.deltas > 0.5:
        print("!! >50% of applied deltas changed nothing.  That is the frozen-book "
              "signature.  Inspect kalshi_raw before trusting any result.")

    print_freshness(comp)
    print_agreement(comp)
    print(f"\nTotal rows written: {rec.rows_written}")
    print("\nNext: python scripts/analyze_lag.py --recordings", out)


def print_rate_table(mon: HealthMonitor, feeds: list["ConstituentFeed"], dur_s: float) -> None:
    """Per-feed tick rate.  Quote feeds first -- they are what the index uses."""
    print(f"{'feed':<16} {'msgs':>8} {'per_s':>8} {'recon':>6} {'parse_fail':>11} "
          f"{'crossed':>8} {'excl_s':>8} {'gap_p50':>8} {'gap_p95':>8}")
    crossed = {f.name: f.crossed for f in feeds}
    for h in mon.feeds.values():
        gaps = sorted(h.recent_intervals)
        p50 = gaps[len(gaps) // 2] if gaps else float("nan")
        p95 = gaps[min(int(len(gaps) * 0.95), len(gaps) - 1)] if gaps else float("nan")
        print(f"{h.name:<16} {h.ticks:>8} {h.ticks / max(dur_s, 1):>8.2f} "
              f"{h.reconnects:>6} {h.parse_failures:>11} "
              f"{crossed.get(h.name, 0):>8} {h.excluded_ms / 1000:>8.1f} "
              f"{p50:>8.0f} {p95:>8.0f}")


def print_freshness(comp: Composite) -> None:
    """
    The diagnostic this whole rewrite is judged on.

    A composite row is only usable when >= COMPOSITE_MIN_SOURCES constituents
    were fresh.  On trade feeds this failed 88.6% of the time.
    """
    pct = 100 * comp.rows_insufficient / max(comp.rows, 1)
    print(f"\nComposite rows={comp.rows}, of which {comp.rows_insufficient} had "
          f"<{COMPOSITE_MIN_SOURCES} fresh sources ({pct:.1f}%)  "
          f"[staleness window {COMPOSITE_STALENESS_S:g}s]")
    if comp.n_histogram:
        dist = "  ".join(
            f"n={n}: {100 * c / max(comp.rows, 1):5.1f}%"
            for n, c in sorted(comp.n_histogram.items())
        )
        print(f"  fresh-source distribution: {dist}")
    if pct > FRESHNESS_TARGET_PCT:
        print(f"!! FRESHNESS {pct:.1f}% EXCEEDS the {FRESHNESS_TARGET_PCT:.0f}% target -- "
              f"the composite is interpolating across holes.  Check the rate "
              f"table above for which feed is starving before analysing.")


def print_agreement(comp: Composite) -> None:
    """
    Cross-feed price agreement -- the check that catches a BROKEN LOCAL BOOK.

    Three of the four feeds are replayed from deltas, so a mis-applied update
    desynchronises our book from the exchange's while every message still
    parses cleanly.  There is no parse failure, no gap, no stall: just a feed
    quoting a price nobody is trading at, dragged into the median.  The only
    thing that exposes it is the other exchanges disagreeing.

    Real inter-venue basis on BTC is order 1 bp.  A feed tens of bps away from
    the cross-feed median is a bug in OUR replay, not a market dislocation.
    """
    means = {
        f: comp.feed_sum[f] / comp.feed_count[f]
        for f in comp.feed_sum if comp.feed_count.get(f)
    }
    if len(means) < 2:
        return
    ref = statistics.median(means.values())
    print(f"\nCross-feed agreement (mean quote mid vs cross-feed median {ref:,.2f}):")
    worst = 0.0
    for f in sorted(means):
        dev_bps = 1e4 * (means[f] - ref) / ref
        worst = max(worst, abs(dev_bps))
        flag = "  <<< CHECK BOOK REPLAY" if abs(dev_bps) > AGREEMENT_WARN_BPS else ""
        print(f"  {f:<12} {means[f]:>14,.2f}  {dev_bps:>+8.2f} bps{flag}")
    if worst > AGREEMENT_WARN_BPS:
        print(f"!! a constituent is {worst:.1f} bps off the others.  That is far more "
              f"than real venue basis; suspect the local book replay for that feed "
              f"before using this recording.")


# ── selftest ─────────────────────────────────────────────────────────────────

async def selftest(asset: str, seconds: float, no_trades: bool) -> int:
    """
    Connect the constituent books only, for `seconds`, and print the two
    numbers that decide whether a long recording is worth starting: the
    per-feed tick rate and the composite freshness.

    Touches NO Kalshi endpoint and writes NO file (sqlite goes to :memory:).
    Exit code is nonzero if a feed starved or freshness missed the target, so
    this is usable as a pre-flight gate in a script.
    """
    rec = Recorder(Path(":memory:"))
    mon = HealthMonitor(rec)
    comp = Composite(rec)
    writer = asyncio.create_task(rec.run(), name="writer")

    feeds = [
        ConstituentFeed(name, EXCHANGE_SYMBOLS[name][asset], rec, mon, comp,
                        record_trades=not no_trades)
        for name in CONSTITUENTS
        if asset in EXCHANGE_SYMBOLS[name]
    ]
    print(f"selftest: {asset} across {len(feeds)} books for {seconds:.0f}s "
          f"(no Kalshi, no file)\n")

    started = now_ms()
    stop_at = time.monotonic() + seconds
    tasks = [asyncio.create_task(f.run(stop_at), name=f"feed_{f.name}") for f in feeds]
    tasks.append(asyncio.create_task(mon.run(stop_at), name="health"))
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        pass
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    dur_s = (now_ms() - started) / 1000.0
    print()
    print_rate_table(mon, feeds, dur_s)
    print_freshness(comp)
    print_agreement(comp)

    starved = [
        h.name for h in mon.feeds.values()
        if not h.name.endswith("_trade")
        and (h.ticks == 0 or h.ticks / max(dur_s, 1) < QUOTE_STARVED_PER_S)
    ]
    pct = 100 * comp.rows_insufficient / max(comp.rows, 1)
    ok = not starved and pct <= FRESHNESS_TARGET_PCT and comp.rows > 0
    if starved:
        print(f"\n!! STARVED QUOTE FEEDS: {starved}")
    print(f"\nselftest: {'PASS' if ok else 'FAIL'}")

    await rec.close()
    writer.cancel()
    return 0 if ok else 1


# ── main ─────────────────────────────────────────────────────────────────────

async def amain(args: argparse.Namespace) -> int:
    series = args.series.upper()
    asset = SERIES_ASSET.get(series)
    if asset is None:
        print(f"unknown series {series}; expected one of {sorted(SERIES_ASSET)}")
        return 2

    out = Path(args.out) if args.out else (
        Path("data") / f"lag_{series}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.sqlite"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print(f"refusing to overwrite existing recording {out}")
        return 2

    rec = Recorder(out)
    mon = HealthMonitor(rec)
    comp = Composite(rec)
    started_ms = now_ms()
    stop_at = time.monotonic() + args.minutes * 60

    rec.set_meta("series", series)
    rec.set_meta("asset", asset)
    rec.set_meta("started_ms", started_ms)
    rec.set_meta("planned_minutes", args.minutes)
    rec.set_meta("constituents", list(CONSTITUENTS))
    rec.set_meta("brti_source", "real" if args.brti_rest_url else "proxy_median_of_quote_mids")
    rec.set_meta("host", socket.gethostname())
    rec.set_meta("pid", os.getpid())
    rec.set_meta("gap_threshold_s", GAP_THRESHOLD_S)
    rec.set_meta("composite_staleness_s", COMPOSITE_STALENESS_S)
    rec.set_meta("composite_min_sources", COMPOSITE_MIN_SOURCES)

    print(f"Recording {series} ({asset}) for {args.minutes} min -> {out}")
    print(f"Constituents: {', '.join(CONSTITUENTS)}"
          + (f" + brti_real({args.brti_rest_url})" if args.brti_rest_url else ""))
    print("READ-ONLY: this process cannot place an order.\n")

    writer_task = asyncio.create_task(rec.run(), name="writer")
    log_ntp(rec, "start")

    client = KalshiClient(settings)
    state = {"markets": set()}

    # Seed the subscription from the currently-open windows so the WS opens
    # already subscribed; the discovery loop then adds each new window.
    loop = asyncio.get_running_loop()
    seed: list[str] = []
    try:
        events = await loop.run_in_executor(
            None, lambda: client.get_events(series_ticker=series, status="open", limit=10)
        )
        for event in events or []:
            for m in event.get("markets", []):
                if m.get("ticker"):
                    seed.append(m["ticker"])
    except Exception as exc:  # noqa: BLE001
        print(f"!! initial discovery failed: {exc}")
    if not seed:
        print("!! no open markets found for this series -- nothing to record.")
        await rec.close()
        return 2
    print(f"seed tickers: {seed}")

    from kbot.ws_client import WSClient

    kal = KalshiRecorder(rec, mon)
    ws_client = WSClient(
        settings,
        tickers=seed,
        channels=["orderbook_delta"],
        on_message=kal.on_message,
    )

    feeds = [
        ConstituentFeed(name, EXCHANGE_SYMBOLS[name][asset], rec, mon, comp,
                        record_trades=not args.no_trades)
        for name in CONSTITUENTS
        if asset in EXCHANGE_SYMBOLS[name]
    ]
    missing = [n for n in CONSTITUENTS if asset not in EXCHANGE_SYMBOLS[n]]
    if missing:
        print(f"!! no {asset} symbol mapped for: {missing} -- composite runs on "
              f"{len(feeds)} sources")

    tasks = [
        asyncio.create_task(ws_client.run(), name="kalshi_ws"),
        asyncio.create_task(mon.run(stop_at), name="health"),
        asyncio.create_task(discovery_loop(rec, client, ws_client, series, stop_at, state),
                            name="discovery"),
        asyncio.create_task(settlement_loop(rec, client, stop_at), name="settlement"),
    ]
    tasks += [asyncio.create_task(f.run(stop_at), name=f"feed_{f.name}") for f in feeds]
    if args.brti_rest_url:
        brti = BrtiRealFeed(args.brti_rest_url, args.brti_poll_s, rec, mon)
        tasks.append(asyncio.create_task(brti.run(stop_at), name="brti_real"))

    await asyncio.sleep(20)  # let the WS settle before probing latency
    await sample_latency(rec, client, seed[0], ws_client, "start")

    try:
        remaining = stop_at - time.monotonic()
        if remaining > 60:
            await asyncio.sleep(remaining - 60)
            await sample_latency(rec, client, seed[0], ws_client, "end")
        remaining = stop_at - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
    except asyncio.CancelledError:
        print("\ninterrupted -- flushing what we have")

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    log_ntp(rec, "end")
    print("\nwaiting for the last windows to settle (backfill pass)...")
    for _ in range(args.settle_passes):
        await backfill_settlements(rec, client)
        unsettled = [t for t, c, r in rec.known_markets() if not r]
        if not unsettled:
            break
        await asyncio.sleep(60)
    unsettled = [t for t, c, r in rec.known_markets() if not r]
    if unsettled:
        print(f"!! {len(unsettled)} window(s) still unsettled: {unsettled}\n"
              f"   analyze_lag.py --fetch-settlement will fill these in later.")

    final_report(mon, kal, comp, rec, started_ms, out, feeds)
    await rec.close()
    writer_task.cancel()
    client.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--series", default="KXBTC15M", help="KXBTC15M or KXETH15M")
    p.add_argument("--minutes", type=float, default=120.0)
    p.add_argument("--out", default=None, help="sqlite path (default data/lag_<series>_<ts>.sqlite)")
    p.add_argument("--brti-rest-url", default=os.environ.get("KBOT_BRTI_REST_URL"),
                   help="real BRTI REST endpoint, if you have a CF Benchmarks key")
    p.add_argument("--brti-poll-s", type=float, default=1.0)
    p.add_argument("--settle-passes", type=int, default=20,
                   help="post-run settlement backfill sweeps, 60s apart")
    p.add_argument("--selftest", action="store_true",
                   help="connect the constituent books for --selftest-seconds, print "
                        "the per-feed rate table and composite freshness, and exit. "
                        "No Kalshi calls, no output file.")
    p.add_argument("--selftest-seconds", type=float, default=SELFTEST_SECONDS)
    p.add_argument("--no-trades", action="store_true",
                   help="skip the secondary trade-print streams (quotes only)")
    args = p.parse_args()

    if args.selftest:
        asset = SERIES_ASSET.get(args.series.upper())
        if asset is None:
            print(f"unknown series {args.series}; expected one of {sorted(SERIES_ASSET)}")
            return 2
        try:
            return asyncio.run(selftest(asset, args.selftest_seconds, args.no_trades))
        except KeyboardInterrupt:
            return 130

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
