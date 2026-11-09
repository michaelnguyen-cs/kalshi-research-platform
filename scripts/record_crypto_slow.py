#!/usr/bin/env python
"""
READ-ONLY capture of SLOW crypto prediction markets on Kalshi and Polymarket,
plus BTC/ETH spot, for two hypotheses that have NOT been tested yet:

  H1 NEWS-RESPONSE LAG  When a crypto shock lands, do slow Kalshi crypto
                        contracts reprice over minutes-to-hours rather than
                        instantly, leaving a tradeable window?
  H2 CROSS-VENUE SPREAD For the same real-world question listed on BOTH
                        Kalshi and Polymarket, how large is the price gap
                        and how long does it persist?

Analysis lives in scripts/analyze_crypto_slow.py.  This script only records.

THIS SCRIPT NEVER PLACES AN ORDER ON EITHER VENUE.  It has no method that
can: both HTTP wrappers expose exactly one verb (`get`) behind a hard path
allowlist, kbot.execution / kbot.execution_maker are never imported, and
KalshiClient (which owns place_order) is deliberately NOT used -- see
_ReadOnlyKalshi.  Polymarket order placement requires an L2 header signed
with a private key this script never loads and a POST this script cannot
emit.  A startup assertion re-checks the import claim at runtime.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
This is a separate build from scripts/scan_all_series.py (untouched) and it
does not re-test anything already killed:
  * The 3-7s Coinbase->Kalshi price lag on 15-minute contracts.  Measured
    over 28M rows / 3 sessions: cross-correlation peak vs a multi-exchange
    composite at -100ms (Kalshi slightly LEADS), median Kalshi mid response
    to a qualifying underlying move 0.00 cents at every horizon out to 10s.
  * Directional prediction (our Brier 0.2474 vs the market's 0.2453).
  * 15-minute miscalibration (none across ~2,880 settled markets).
The fifteen_min / hourly / daily crypto series are therefore EXCLUDED by
default (--exclude-frequency).  The premise here is the opposite one: books
that are human-paced rather than bot-dominated.

WHAT GETS RECORDED
------------------
kalshi_market   one row per slow crypto market ever seen: title, subtitle,
                rules_primary/rules_secondary (the resolution text, verbatim),
                floor_strike / cap_strike / strike_type, close/expiration
                time, series frequency + tags, and the raw JSON.
kalshi_quote    top-of-book per market per poll: yes bid/ask and the SIZE at
                each, plus the derived no-side prices.  One row per market
                per cycle.  This is the primary series both hypotheses use.
kalshi_book     top-N ladder per side for a subset of markets, sampled less
                often (--depth-interval).  Sanity + depth, not the main path.
poly_market     one row per Polymarket market ever polled: question,
                description (the resolution text, verbatim), endDate, slug,
                condition id, YES/NO token ids, raw JSON.
poly_quote      top-of-book per YES token per poll, best bid/ask + sizes,
                plus Polymarket's own book timestamp alongside ours.
pair            every candidate Kalshi<->Polymarket match, its confidence,
                WHICH fields matched, and both resolution texts.  A pair row
                is a claim to be audited, not a fact -- see MATCHING below.
spot            BTC/ETH spot samples (the shock detector's input).
shock           one row per detected shock: asset, timestamp, reference
                price, move in bps, direction.
poll_cycle      per cycle: start/end, request counts, successes, failures.
run             one row per process run (resume audit trail).
event_log       gaps, poll failures, discovery changes, degradations.

LOCAL RECEIVE TIME IS THE COMMON CLOCK, at second resolution or better.
Kalshi and Polymarket both hand us their own timestamps; those are recorded
but never used to align the two venues, because they are two clocks with
unknown skew and the question ("could WE have acted") is about ours.

MATCHING IS THE HARD PART AND IS DELIBERATELY CONSERVATIVE
----------------------------------------------------------
Two contracts with the same headline are not the same contract.  "Will BTC
hit $150k in 2026" resolves YES on a single tick through the strike; "Will
BTC be above $150k on Dec 31 2026" resolves NO on the same tick if it comes
back down.  Treating those as an arbitrage pair is an unhedged directional
bet with extra steps.  So a pair is only proposed when ALL of these agree:

    asset            exactly one asset named on each side, and they are equal
    strike           a numeric threshold parsed from BOTH sides, within
                     --strike-tol relative tolerance
    direction        above / below / range, equal
    style            touch ("hit", "reach", "max") vs terminal ("be above X
                     on DATE"), KNOWN on both sides and equal
    resolution date  within --date-tol-hours

Title similarity is never used, on its own or as a tie-break.  A market with
no parseable numeric strike (KXBTCRESERVE, KXTREASBUYBTC -- event markets,
not price markets) can NEVER reach HIGH confidence here; it is recorded as a
MANUAL candidate with both texts so a human can rule on it, and the analyzer
will only count it if a --pair-review file promotes it.

Confidence levels written to `pair`:
    HIGH    every field above agreed.  Only these feed the H2 verdict.
    MEDIUM  asset + strike + date agreed but style or direction was
            ambiguous on at least one side.  Recorded and polled; reported
            separately; excluded from the verdict.
    MANUAL  no numeric strike on one/both sides.  Recorded, polled, and
            printed for human review.  Excluded unless promoted.
Both venues' resolution texts are stored on the pair row so the analyzer can
print them side by side.  Note that Kalshi's rules_primary for templated
series arrives with unfilled placeholders ("|| Count ||"); title, subtitle
and the strike fields carry the real terms, and all of them are stored.

SHOCKS: SPOT, NOT HEADLINES
---------------------------
A news scraper needs an API, a paywall story, and a judgement call about
what counts as crypto news.  Spot needs none of that and is strictly closer
to the thing that actually moves these books: a shock is a move of at least
--shock-bps over --shock-window seconds in BTC or ETH.  Shocks inside
--shock-refractory seconds of the previous one for the same asset are folded
into it, so one news event does not become forty "independent" observations.
The cost of this choice is honest and stated in the analyzer's limitations:
a pure-narrative shock that does not move spot is invisible to this study.

Spot comes from the EXISTING kbot.feeds BTCFeed / ETHFeed -- the same
Coinbase WS tick path the bot trades on, not a private reimplementation.

POLL RATE
---------
These books are slow, so REST polling is fine and no websocket is needed.
The per-cycle Kalshi cost is one request PER SERIES, not per market:
GET /markets?series_ticker=X&status=open returns top-of-book AND top-of-book
size for every market in that series at once (verified live, 2026-08-20).
At 65 slow crypto series / 469 open markets that is 65 requests per cycle,
comfortably inside the 20 reads/s limit at a 10s interval.  Polymarket costs
one GET /book per paired token per cycle.  Measured live 2026-08-20: a full
cycle (65 series + 67 Polymarket books + 40 depth ladders) takes 5.1s.

DISK: every poll of every market is written, unconditionally -- no
write-on-change dedup.  That is ~469 rows per 10s, i.e. ~4M rows and roughly
250 MB per 24 hours.  The redundancy is deliberate: it is what lets the
analyzer verify the poll cadence and distinguish "the price did not move"
from "we did not look".  Budget the disk, or raise --interval.

USAGE
-----
    python scripts/record_crypto_slow.py --enumerate-only    # what's out there
    python scripts/record_crypto_slow.py --selftest          # one live cycle
    python scripts/record_crypto_slow.py --hours 24
    python scripts/record_crypto_slow.py --hours 24          # again: resumes

Resumable: point it at the same --db and it appends, continues the cycle
counter, reconstructs shock state, and logs the coverage gap between runs so
the analyzer can exclude it instead of interpolating across it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

# Allow running straight from a checkout without an editable install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kbot.auth import kalshi_headers, load_private_key  # noqa: E402
from kbot.config import settings  # noqa: E402
from kbot.feeds.btc import BTCFeed  # noqa: E402
from kbot.feeds.eth import ETHFeed  # noqa: E402
from kbot.log_config import configure_logging  # noqa: E402

# The feeds log one line per WS tick at DEBUG.  BTC alone printed 229 ticks in
# 20 seconds during a selftest; over a 24-hour recording that is roughly a
# million lines of stdout burying every gap and failure this script prints.
# INFO keeps reconnects and stalls (the things worth seeing) and drops ticks.
# httpx logs one INFO line per request, and this script makes ~65 per cycle;
# it gets pushed to WARNING for the same reason.
configure_logging(verbose=False)
for _noisy in ("httpx", "httpcore", "websockets"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Runtime proof of the read-only claim in the docstring.  Importing either of
# these modules would put an order-placing code path in scope; nothing here
# needs them, so their absence is cheap to assert and loud to violate.
for _forbidden in ("kbot.execution", "kbot.execution_maker"):
    if _forbidden in sys.modules:
        raise SystemExit(
            f"{_forbidden} is imported: this script is read-only and must not "
            f"have an order path in scope."
        )


# ═════════════════════════════════════════════════════════════════════════════
# Tunables.  Recording-side only -- none of these is a strategy parameter, and
# none of them is a threshold in the pre-registered verdict (those live in
# analyze_crypto_slow.py and must not be duplicated here).
# ═════════════════════════════════════════════════════════════════════════════

SCHEMA_VERSION = 1

DEFAULT_DB = "data/crypto_slow.sqlite"

# Series frequencies excluded by default: these are the bot-dominated books
# whose lag has already been measured and killed.
DEFAULT_EXCLUDE_FREQUENCY = ("fifteen_min", "hourly", "daily")

# Kalshi series-catalog tags that mark a series as crypto even when its
# category is not "Crypto".  KXBTCRESERVE and KXTREASBUYBTC both live under
# category "Politics" with tag "BTC" -- filtering on category alone silently
# drops exactly the event-driven markets H1 is about.
CRYPTO_TAGS = frozenset({
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ZEC", "HYPE", "NEAR", "LTC",
    "XLM", "AVAX", "DOT", "ADA", "LINK", "TON", "SUI", "TRX", "Crypto",
})

CRYPTO_TITLE_RX = re.compile(
    r"BITCOIN|ETHEREUM|CRYPTO|\bBTC\b|\bETH\b|SOLANA|RIPPLE|\bXRP\b|DOGECOIN"
    r"|STABLECOIN|SATOSHI|ALTCOIN|\bDEFI\b|BINANCE|COINBASE|MICROSTRATEGY",
    re.I,
)

# Asset symbol -> the words that name it.  Used on BOTH venues so the two
# sides are extracted by identical rules.
ASSET_WORDS: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "btc"),
    "ETH": ("ethereum", "ether", "eth"),
    "SOL": ("solana", "sol"),
    "XRP": ("xrp", "ripple"),
    "DOGE": ("dogecoin", "doge"),
    "BNB": ("bnb", "binance coin"),
    "ADA": ("cardano", "ada"),
    "AVAX": ("avalanche", "avax"),
    "LINK": ("chainlink", "link"),
    "LTC": ("litecoin", "ltc"),
    "NEAR": ("near",),
    "HYPE": ("hyperliquid", "hype"),
    "ZEC": ("zcash", "zec"),
    "XLM": ("stellar", "xlm"),
    "DOT": ("polkadot", "dot"),
    "SUI": ("sui",),
    "TON": ("toncoin", "ton"),
    "TRX": ("tron", "trx"),
}

# Touch vs terminal.  This is the distinction that turns a "matched pair" into
# an unhedged bet if you get it wrong, so both sides are scored and a
# disagreement or an ambiguity blocks HIGH confidence.
TOUCH_RX = re.compile(
    r"\b(hit|hits|touch|touches|reach|reaches|at any point|at any time|ever\b"
    r"|one touch|high of|low of|maximum|minimum|max\b|min\b|dip to|peak)\b",
    re.I,
)
TERMINAL_RX = re.compile(
    r"\b(on (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|at (the )?(close|closing|end of)|end of (day|month|year|the)"
    r"|as of\b|closing price|settlement price|at \d{1,2}(:\d{2})?\s?(am|pm|et|utc))\b",
    re.I,
)
ABOVE_RX = re.compile(
    r"\b(above|over|greater|exceed|exceeds|higher than|at least|or more|up to"
    r"|hit|reach|reaches|surpass|\bmore than\b)\b", re.I)
BELOW_RX = re.compile(
    r"\b(below|under|less than|lower than|dip|fall|drop|beneath|at most)\b", re.I)

# "$150k", "$150,000", "150K", "$1.5M", "$0.35".
#
# An explicit money marker -- a leading "$" or a k/m/b magnitude suffix -- is
# REQUIRED.  A bare integer is not a strike: the first version of this regex
# accepted one, read the "2026" in "Will Bitcoin outperform gold in 2026?" as a
# $2,026 strike, and duly proposed it as a match for the "2026" in a Kalshi
# year ticker.  Every such pair agreed on asset, "strike" and date and was
# complete nonsense, which is precisely the failure mode conservative matching
# exists to prevent.
# The magnitude suffix must be glued to the digits and not be the first letter
# of the next word -- without that guard, "$149999.99 by Dec 31" parses the "b"
# of "by" as "billion".
MONEY_RX = re.compile(
    r"(?:\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)([kKmMbB])?(?![A-Za-z0-9])"
    r"|\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)([kKmMbB])(?![A-Za-z0-9]))")
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9}

# Polymarket endpoints.  All public, all read-only, no account and no key.
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
POLY_CRYPTO_TAG_ID = 21          # gamma tag "crypto" (verified live)
POLY_GAMMA_PAGE = 100            # gamma caps `limit` at 100 regardless of ask
POLY_GAMMA_MAX_OFFSET = 2000     # gamma 422s past this; partition instead

# Concurrency.  Kalshi's documented read limit is settings.rate_limit_reads
# (20/s by default); the token bucket enforces the rate and this bounds the
# in-flight count so a stall cannot pile up unbounded sockets.  Polymarket
# publishes no number for GET /book, so it gets a deliberately gentle cap.
KALSHI_CONCURRENCY = 8
POLY_CONCURRENCY = 4

HTTP_TIMEOUT_S = 20.0
HTTP_RETRIES = 3
HTTP_BACKOFF_S = 1.5

# A cycle that overruns its interval by more than this is logged as a gap.
CYCLE_OVERRUN_FACTOR = 2.0
# Depth ladder levels stored per side.
TOP_N_LEVELS = 5


def now_ms() -> int:
    """Local wall-clock milliseconds.  THE common clock for this recording."""
    return int(time.time() * 1000)


def iso(ms: int | float | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def parse_iso_ts(text: str | None) -> int | None:
    """ISO-8601 -> epoch ms, or None.  Tolerates 'Z' and fractional seconds."""
    if not text:
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Gamma occasionally emits more than 6 fractional digits.
        m = re.match(r"^(.*\.\d{6})\d*(\+\d{2}:\d{2}|)$", raw)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(m.group(1) + (m.group(2) or "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fnum(raw: Any) -> float | None:
    """Kalshi/Polymarket hand back numbers as strings; None-safe float()."""
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


# ═════════════════════════════════════════════════════════════════════════════
# Read-only HTTP layers
# ═════════════════════════════════════════════════════════════════════════════

class _TokenBucket:
    """Async token bucket; mirrors KalshiClient's read limiter."""

    def __init__(self, rate: float) -> None:
        self._rate = max(1.0, float(rate))
        self._tokens = self._rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                await asyncio.sleep((1.0 - self._tokens) / self._rate)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


_KALSHI_ALLOWED = (
    re.compile(r"^/series$"),
    re.compile(r"^/series/[^/]+$"),
    re.compile(r"^/markets$"),
    re.compile(r"^/markets/[^/]+$"),
    re.compile(r"^/markets/[^/]+/orderbook$"),
)


class _ReadOnlyKalshi:
    """
    Signed GET-only Kalshi client with a hard path allowlist.

    Deliberately NOT KalshiClient: that class owns place_order() and
    cancel_order(), and this script must not have such a method in scope at
    all.  Raw httpx rather than the SDK for the reason documented in
    kbot/kalshi_client.py -- the SDK's Market model rejects status values that
    real markets return.
    """

    def __init__(self, cfg=settings, *, concurrency: int = KALSHI_CONCURRENCY) -> None:
        self._base = cfg.base_url.rstrip("/")
        self._prefix = httpx.URL(cfg.base_url).path.rstrip("/")
        self._bucket = _TokenBucket(getattr(cfg, "rate_limit_reads", 20))
        self._sem = asyncio.Semaphore(concurrency)
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
        self.request_count = 0
        self.error_count = 0

        if not cfg.kalshi_key_id or not cfg.private_key_path.exists():
            raise SystemExit(
                "Kalshi credentials required (the series catalog and market "
                f"reads are authenticated). key_id set={bool(cfg.kalshi_key_id)}, "
                f"key file {cfg.private_key_path} exists={cfg.private_key_path.exists()}"
            )
        self._key_id = cfg.kalshi_key_id
        self._private_key = load_private_key(cfg.private_key_path)

    async def get(self, path: str, params: dict | None = None) -> dict:
        if not any(rx.match(path) for rx in _KALSHI_ALLOWED):
            raise ValueError(
                f"refusing {path!r}: record_crypto_slow.py is read-only and only "
                f"calls {[rx.pattern for rx in _KALSHI_ALLOWED]}"
            )
        last: Exception | None = None
        for attempt in range(HTTP_RETRIES):
            await self._bucket.acquire()
            async with self._sem:
                try:
                    headers = kalshi_headers(
                        "GET", self._prefix + path, self._key_id, self._private_key)
                    resp = await self._http.get(
                        self._base + path, headers=headers, params=params or {})
                    self.request_count += 1
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    self.error_count += 1
            if attempt + 1 < HTTP_RETRIES:
                await asyncio.sleep(HTTP_BACKOFF_S * (attempt + 1))
        raise RuntimeError(f"kalshi GET {path} failed after {HTTP_RETRIES} attempts: {last}")

    async def aclose(self) -> None:
        await self._http.aclose()


_POLY_ALLOWED = (
    (POLY_GAMMA, re.compile(r"^/markets$")),
    (POLY_GAMMA, re.compile(r"^/public-search$")),
    (POLY_GAMMA, re.compile(r"^/tags/slug/[^/]+$")),
    (POLY_CLOB, re.compile(r"^/book$")),
)


class _ReadOnlyPolymarket:
    """
    GET-only Polymarket client over the two public read APIs.

    No account, no API key, no wallet.  Placing a Polymarket order needs an
    EIP-712 signature from a funded key plus a POST to /order; this class
    holds no key and has no verb but `get`, with a host+path allowlist so a
    future edit cannot quietly widen it.
    """

    def __init__(self, *, concurrency: int = POLY_CONCURRENCY) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._http = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_S,
            headers={"User-Agent": "kalshi-bot-research/record_crypto_slow (read-only)"},
        )
        self.request_count = 0
        self.error_count = 0

    async def get(self, host: str, path: str, params: dict | None = None) -> Any:
        if not any(h == host and rx.match(path) for h, rx in _POLY_ALLOWED):
            raise ValueError(
                f"refusing {host}{path}: record_crypto_slow.py is read-only and "
                f"only calls {[(h, rx.pattern) for h, rx in _POLY_ALLOWED]}"
            )
        last: Exception | None = None
        for attempt in range(HTTP_RETRIES):
            async with self._sem:
                try:
                    resp = await self._http.get(host + path, params=params or {})
                    self.request_count += 1
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    self.error_count += 1
            if attempt + 1 < HTTP_RETRIES:
                await asyncio.sleep(HTTP_BACKOFF_S * (attempt + 1))
        raise RuntimeError(f"polymarket GET {path} failed after {HTTP_RETRIES}: {last}")

    async def aclose(self) -> None:
        await self._http.aclose()


# ═════════════════════════════════════════════════════════════════════════════
# Storage
# ═════════════════════════════════════════════════════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS run (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms  INTEGER NOT NULL,
    ended_ms    INTEGER,
    argv        TEXT NOT NULL,
    n_cycles    INTEGER DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS kalshi_market (
    ticker          TEXT PRIMARY KEY,
    series          TEXT NOT NULL,
    event_ticker    TEXT,
    title           TEXT,
    subtitle        TEXT,
    yes_sub_title   TEXT,
    rules_primary   TEXT,
    rules_secondary TEXT,
    floor_strike    REAL,
    cap_strike      REAL,
    strike_type     TEXT,
    open_ts         INTEGER,
    close_ts        INTEGER,
    expiration_ts   INTEGER,
    frequency       TEXT,
    category        TEXT,
    tags_json       TEXT,
    series_title    TEXT,
    settlement_sources_json TEXT,
    asset           TEXT,
    style           TEXT,
    direction       TEXT,
    first_seen_ms   INTEGER,
    last_seen_ms    INTEGER,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS kalshi_quote (
    ts_ms        INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    ticker       TEXT NOT NULL,
    yes_bid      REAL, yes_bid_size REAL,
    yes_ask      REAL, yes_ask_size REAL,
    no_bid       REAL, no_ask       REAL,
    mid          REAL,
    last_price   REAL,
    open_interest REAL,
    volume       REAL,
    PRIMARY KEY (ticker, ts_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_kq_ts ON kalshi_quote (ts_ms);

CREATE TABLE IF NOT EXISTS kalshi_book (
    ts_ms       INTEGER NOT NULL,
    ticker      TEXT NOT NULL,
    yes_levels  TEXT,
    no_levels   TEXT,
    PRIMARY KEY (ticker, ts_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS poly_market (
    condition_id TEXT PRIMARY KEY,
    market_id    TEXT,
    question     TEXT,
    description  TEXT,
    slug         TEXT,
    end_ts       INTEGER,
    token_yes    TEXT,
    token_no     TEXT,
    outcomes     TEXT,
    volume       REAL,
    liquidity    REAL,
    asset        TEXT,
    style        TEXT,
    direction    TEXT,
    strike       REAL,
    first_seen_ms INTEGER,
    last_seen_ms  INTEGER,
    raw_json     TEXT
);

CREATE TABLE IF NOT EXISTS poly_quote (
    ts_ms        INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    token_id     TEXT NOT NULL,
    yes_bid      REAL, yes_bid_size REAL,
    yes_ask      REAL, yes_ask_size REAL,
    mid          REAL,
    venue_ts_ms  INTEGER,
    tick_size    REAL,
    PRIMARY KEY (token_id, ts_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_pq_ts ON poly_quote (ts_ms);

CREATE TABLE IF NOT EXISTS pair (
    kalshi_ticker    TEXT NOT NULL,
    condition_id     TEXT NOT NULL,
    confidence       TEXT NOT NULL,
    asset            TEXT,
    kalshi_strike    REAL,
    poly_strike      REAL,
    strike_rel_diff  REAL,
    direction        TEXT,
    style            TEXT,
    kalshi_close_ts  INTEGER,
    poly_end_ts      INTEGER,
    date_delta_h     REAL,
    match_json       TEXT,
    blockers_json    TEXT,
    kalshi_text      TEXT,
    poly_text        TEXT,
    created_ms       INTEGER,
    PRIMARY KEY (kalshi_ticker, condition_id)
);

CREATE TABLE IF NOT EXISTS spot (
    ts_ms  INTEGER NOT NULL,
    asset  TEXT NOT NULL,
    price  REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (asset, ts_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS shock (
    shock_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    asset      TEXT NOT NULL,
    ts_ms      INTEGER NOT NULL,
    ref_ts_ms  INTEGER NOT NULL,
    ref_price  REAL NOT NULL,
    price      REAL NOT NULL,
    move_bps   REAL NOT NULL,
    window_s   REAL NOT NULL,
    direction  TEXT NOT NULL,
    run_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_shock_ts ON shock (ts_ms);

CREATE TABLE IF NOT EXISTS poll_cycle (
    seq            INTEGER PRIMARY KEY,
    run_id         INTEGER,
    started_ms     INTEGER NOT NULL,
    ended_ms       INTEGER NOT NULL,
    kalshi_series  INTEGER DEFAULT 0,
    kalshi_ok      INTEGER DEFAULT 0,
    kalshi_fail    INTEGER DEFAULT 0,
    kalshi_quotes  INTEGER DEFAULT 0,
    poly_ok        INTEGER DEFAULT 0,
    poly_fail      INTEGER DEFAULT 0,
    gap_s          REAL
);

CREATE TABLE IF NOT EXISTS event_log (
    ts_ms  INTEGER NOT NULL,
    run_id INTEGER,
    kind   TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log (ts_ms);
"""


class Store:
    """sqlite writer.  One connection, WAL, batched per cycle."""

    def __init__(self, path: Path, *, dry: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.dry = dry
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        cur = self._db.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
        elif int(row[0]) != SCHEMA_VERSION:
            raise SystemExit(
                f"{path} was written by schema v{row[0]}, this script is "
                f"v{SCHEMA_VERSION}. Use a new --db rather than mixing them."
            )
        self.run_id: int | None = None

    # -- run bookkeeping ------------------------------------------------------
    def start_run(self, argv: str) -> int:
        cur = self._db.execute(
            "INSERT INTO run (started_ms, argv) VALUES (?,?)", (now_ms(), argv))
        self.run_id = int(cur.lastrowid)
        return self.run_id

    def end_run(self, n_cycles: int, note: str = "") -> None:
        self._db.execute(
            "UPDATE run SET ended_ms=?, n_cycles=?, note=? WHERE run_id=?",
            (now_ms(), n_cycles, note, self.run_id))

    def last_observation_ms(self) -> int | None:
        row = self._db.execute("SELECT MAX(ended_ms) FROM poll_cycle").fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def next_seq(self) -> int:
        row = self._db.execute("SELECT MAX(seq) FROM poll_cycle").fetchone()
        return (int(row[0]) + 1) if row and row[0] is not None else 1

    def recorded_seconds(self) -> float:
        row = self._db.execute(
            "SELECT SUM(ended_ms - started_ms) FROM run WHERE ended_ms IS NOT NULL"
        ).fetchone()
        return (float(row[0]) / 1000.0) if row and row[0] else 0.0

    def last_shock_ms(self, asset: str) -> int | None:
        row = self._db.execute(
            "SELECT MAX(ts_ms) FROM shock WHERE asset=?", (asset,)).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # -- writes ---------------------------------------------------------------
    def log(self, kind: str, detail: str = "", *, echo: bool = True) -> None:
        if not self.dry:
            self._db.execute(
                "INSERT INTO event_log (ts_ms, run_id, kind, detail) VALUES (?,?,?,?)",
                (now_ms(), self.run_id, kind, detail))
        if echo:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] {kind}: {detail}", flush=True)

    def upsert_kalshi_markets(self, rows: list[tuple]) -> None:
        self._db.executemany(
            "INSERT INTO kalshi_market (ticker, series, event_ticker, title, subtitle,"
            " yes_sub_title, rules_primary, rules_secondary, floor_strike, cap_strike,"
            " strike_type, open_ts, close_ts, expiration_ts, frequency, category,"
            " tags_json, series_title, settlement_sources_json, asset, style,"
            " direction, first_seen_ms, last_seen_ms, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(ticker) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,"
            " rules_primary=excluded.rules_primary, rules_secondary=excluded.rules_secondary,"
            " close_ts=excluded.close_ts, raw_json=excluded.raw_json",
            rows)

    def put_quotes(self, rows: list[tuple]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO kalshi_quote (ts_ms, seq, ticker, yes_bid,"
            " yes_bid_size, yes_ask, yes_ask_size, no_bid, no_ask, mid, last_price,"
            " open_interest, volume) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def put_books(self, rows: list[tuple]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO kalshi_book (ts_ms, ticker, yes_levels, no_levels)"
            " VALUES (?,?,?,?)", rows)

    def upsert_poly_markets(self, rows: list[tuple]) -> None:
        self._db.executemany(
            "INSERT INTO poly_market (condition_id, market_id, question, description,"
            " slug, end_ts, token_yes, token_no, outcomes, volume, liquidity, asset,"
            " style, direction, strike, first_seen_ms, last_seen_ms, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(condition_id) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,"
            " description=excluded.description, volume=excluded.volume,"
            " liquidity=excluded.liquidity, raw_json=excluded.raw_json",
            rows)

    def put_poly_quotes(self, rows: list[tuple]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO poly_quote (ts_ms, seq, condition_id, token_id,"
            " yes_bid, yes_bid_size, yes_ask, yes_ask_size, mid, venue_ts_ms, tick_size)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    def upsert_pairs(self, rows: list[tuple]) -> None:
        self._db.executemany(
            "INSERT INTO pair (kalshi_ticker, condition_id, confidence, asset,"
            " kalshi_strike, poly_strike, strike_rel_diff, direction, style,"
            " kalshi_close_ts, poly_end_ts, date_delta_h, match_json, blockers_json,"
            " kalshi_text, poly_text, created_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(kalshi_ticker, condition_id) DO UPDATE SET"
            " confidence=excluded.confidence, match_json=excluded.match_json,"
            " blockers_json=excluded.blockers_json, kalshi_text=excluded.kalshi_text,"
            " poly_text=excluded.poly_text",
            rows)

    def put_spot(self, rows: list[tuple]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO spot (ts_ms, asset, price, source) VALUES (?,?,?,?)",
            rows)

    def put_shock(self, asset: str, ts_ms: int, ref_ts: int, ref_px: float,
                  px: float, bps: float, window_s: float) -> int:
        cur = self._db.execute(
            "INSERT INTO shock (asset, ts_ms, ref_ts_ms, ref_price, price, move_bps,"
            " window_s, direction, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (asset, ts_ms, ref_ts, ref_px, px, bps, window_s,
             "up" if bps >= 0 else "down", self.run_id))
        return int(cur.lastrowid)

    def put_cycle(self, seq: int, started: int, ended: int, stats: dict, gap_s: float | None) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO poll_cycle (seq, run_id, started_ms, ended_ms,"
            " kalshi_series, kalshi_ok, kalshi_fail, kalshi_quotes, poly_ok, poly_fail,"
            " gap_s) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (seq, self.run_id, started, ended, stats.get("kalshi_series", 0),
             stats.get("kalshi_ok", 0), stats.get("kalshi_fail", 0),
             stats.get("kalshi_quotes", 0), stats.get("poly_ok", 0),
             stats.get("poly_fail", 0), gap_s))

    def quotes_since_seq(self, seq: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM kalshi_quote WHERE seq >= ?", (seq,)).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._db.close()


# ═════════════════════════════════════════════════════════════════════════════
# Semantic extraction -- run with IDENTICAL rules on both venues
# ═════════════════════════════════════════════════════════════════════════════

def extract_asset(text: str, tags: Iterable[str] = ()) -> str | None:
    """
    The single asset named by `text`, or None if zero or several are named.

    "Will SOL flip ETH?" names two and returns None on purpose: a two-asset
    question cannot be matched on "the" underlying, and pretending otherwise
    is how you end up hedging a ratio with an outright.
    """
    tagset = {t.upper() for t in tags}
    found: set[str] = {a for a in ASSET_WORDS if a in tagset}

    low = text.lower()
    for asset, words in ASSET_WORDS.items():
        for w in words:
            if re.search(rf"\b{re.escape(w)}\b", low):
                found.add(asset)
                break
    return next(iter(found)) if len(found) == 1 else None


def extract_strike(text: str) -> float | None:
    """
    The single price threshold named by `text`, or None.

    Several distinct numbers (a range market, or a question that quotes both a
    strike and a date as a bare number) return None rather than a guess.
    Values below 0.0001 or above 1e9 are treated as not-a-price.
    """
    vals: list[float] = []
    for d_raw, d_sfx, s_raw, s_sfx in MONEY_RX.findall(text):
        raw, suffix = (d_raw, d_sfx) if d_raw else (s_raw, s_sfx)
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            v *= _MULT[suffix.lower()]
        if 1e-4 <= v <= 1e9:
            vals.append(v)
    if not vals:
        return None
    uniq = {round(v, 6) for v in vals}
    return vals[0] if len(uniq) == 1 else None


def extract_style(text: str) -> str:
    """'touch' | 'terminal' | 'ambiguous'.  Ambiguity is never resolved by
    guessing -- it blocks a HIGH-confidence match instead."""
    touch = bool(TOUCH_RX.search(text))
    terminal = bool(TERMINAL_RX.search(text))
    if touch and not terminal:
        return "touch"
    if terminal and not touch:
        return "terminal"
    return "ambiguous"


def extract_direction(text: str) -> str:
    above = bool(ABOVE_RX.search(text))
    below = bool(BELOW_RX.search(text))
    if above and not below:
        return "above"
    if below and not above:
        return "below"
    return "ambiguous"


@dataclass
class KalshiMarket:
    ticker: str
    series: str
    event_ticker: str | None
    title: str
    subtitle: str
    yes_sub_title: str
    rules_primary: str
    rules_secondary: str
    floor_strike: float | None
    cap_strike: float | None
    strike_type: str | None
    open_ts: int | None
    close_ts: int | None
    expiration_ts: int | None
    frequency: str | None
    category: str | None
    tags: list[str]
    series_title: str
    settlement_sources: list[dict]
    raw: dict

    @property
    def text(self) -> str:
        """Everything a human would read to decide what this contract means."""
        return " | ".join(x for x in (
            self.series_title, self.title, self.subtitle, self.yes_sub_title,
            self.rules_primary, self.rules_secondary) if x)

    @property
    def asset(self) -> str | None:
        return extract_asset(f"{self.series_title} {self.title}", self.tags)

    @property
    def direction(self) -> str:
        # Kalshi hands us this structurally; trust the field over the prose.
        st = (self.strike_type or "").lower()
        if st.startswith("greater"):
            return "above"
        if st.startswith("less"):
            return "below"
        if st == "between":
            return "range"
        return extract_direction(self.title)

    @property
    def strike(self) -> float | None:
        """The numeric threshold, from the structured fields where possible."""
        if self.strike_type == "between":
            return None            # a range is matched on both bounds, not one
        for v in (self.floor_strike, self.cap_strike):
            if v is not None:
                return float(v)
        return extract_strike(f"{self.title} {self.subtitle}")

    @property
    def style(self) -> str:
        # A "MAX"/"MIN" series ticker is Kalshi's own structural marker for a
        # one-touch contract and beats the prose, which is templated.
        if re.search(r"(MAX|MIN)", self.series):
            return "touch"
        return extract_style(f"{self.series_title} {self.title} {self.rules_primary}")


@dataclass
class PolyMarket:
    condition_id: str
    market_id: str
    question: str
    description: str
    slug: str
    end_ts: int | None
    token_yes: str | None
    token_no: str | None
    outcomes: list[str]
    volume: float
    liquidity: float
    raw: dict

    @property
    def text(self) -> str:
        return f"{self.question} | {self.description}"

    @property
    def asset(self) -> str | None:
        return extract_asset(self.question)

    @property
    def strike(self) -> float | None:
        return extract_strike(self.question)

    @property
    def style(self) -> str:
        return extract_style(self.question)

    @property
    def direction(self) -> str:
        return extract_direction(self.question)


def parse_kalshi_market(raw: dict, series_meta: dict) -> KalshiMarket:
    return KalshiMarket(
        ticker=raw.get("ticker", ""),
        series=series_meta.get("ticker", "") or (raw.get("series_ticker") or ""),
        event_ticker=raw.get("event_ticker"),
        title=raw.get("title") or "",
        subtitle=raw.get("subtitle") or "",
        yes_sub_title=raw.get("yes_sub_title") or "",
        rules_primary=raw.get("rules_primary") or "",
        rules_secondary=raw.get("rules_secondary") or "",
        floor_strike=fnum(raw.get("floor_strike")),
        cap_strike=fnum(raw.get("cap_strike")),
        strike_type=raw.get("strike_type"),
        open_ts=parse_iso_ts(raw.get("open_time")),
        close_ts=parse_iso_ts(raw.get("close_time")),
        expiration_ts=parse_iso_ts(raw.get("expiration_time")),
        frequency=series_meta.get("frequency"),
        category=series_meta.get("category"),
        tags=list(series_meta.get("tags") or []),
        series_title=series_meta.get("title") or "",
        settlement_sources=list(series_meta.get("settlement_sources") or []),
        raw=raw,
    )


def parse_poly_market(raw: dict) -> PolyMarket | None:
    """A gamma market row -> PolyMarket, or None if it is not a YES/NO book."""
    try:
        outcomes = json.loads(raw.get("outcomes") or "[]")
        tokens = json.loads(raw.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    lower = [str(o).strip().lower() for o in outcomes]
    if "yes" not in lower or "no" not in lower:
        # Multi-outcome or team-vs-team: no YES leg to compare against a
        # Kalshi binary, so it is not a candidate at all.
        return None
    yi, ni = lower.index("yes"), lower.index("no")
    return PolyMarket(
        condition_id=raw.get("conditionId") or "",
        market_id=str(raw.get("id") or ""),
        question=raw.get("question") or "",
        description=raw.get("description") or "",
        slug=raw.get("slug") or "",
        end_ts=parse_iso_ts(raw.get("endDate") or raw.get("endDateIso")),
        token_yes=str(tokens[yi]),
        token_no=str(tokens[ni]),
        outcomes=[str(o) for o in outcomes],
        volume=fnum(raw.get("volumeNum")) or fnum(raw.get("volume")) or 0.0,
        liquidity=fnum(raw.get("liquidityNum")) or fnum(raw.get("liquidity")) or 0.0,
        raw=raw,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Matching
# ═════════════════════════════════════════════════════════════════════════════

_STOPWORDS = frozenset("""
a an and any are as at be before by can did do does for from has have how in
is it its of on or the there this to what when which who will with within
market markets price prices resolve resolves yes no
""".split())

# Cap on non-HIGH (human-review) candidates emitted per Kalshi market.  A list
# of 400 "please read these" pairs is a list nobody reads.
MAX_REVIEW_PER_KALSHI = 5


def _significant_overlap(a: str, b: str) -> set[str]:
    """Shared non-stopword tokens.  Used ONLY to bound the manual-review list."""
    def toks(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in _STOPWORDS}
    return toks(a) & toks(b)


@dataclass
class Pair:
    kalshi: KalshiMarket
    poly: PolyMarket
    confidence: str
    matched: dict[str, Any]
    blockers: list[str]

    @property
    def date_delta_h(self) -> float | None:
        if self.kalshi.close_ts is None or self.poly.end_ts is None:
            return None
        return (self.poly.end_ts - self.kalshi.close_ts) / 3_600_000.0


def match_one(k: KalshiMarket, p: PolyMarket, *, strike_tol: float,
              date_tol_h: float) -> Pair | None:
    """
    Propose a pair, or None if the two are not about the same question.

    Every check is a hard gate.  There is no scoring, no weighting, and no
    "close enough on three of four" -- a pair that fails a gate is not
    downgraded, it is discarded, because a downgraded pair still ends up in a
    table someone will eventually read as tradeable.
    """
    ka, pa = k.asset, p.asset
    if not ka or not pa or ka != pa:
        return None

    kd, pd = k.direction, p.direction
    ks, ps = k.strike, p.strike
    kt, pt = k.close_ts, p.end_ts

    matched: dict[str, Any] = {"asset": ka}
    blockers: list[str] = []

    # -- resolution date ------------------------------------------------------
    if kt is None or pt is None:
        return None
    delta_h = abs(pt - kt) / 3_600_000.0
    if delta_h > date_tol_h:
        return None
    matched["date_delta_h"] = round(delta_h, 2)

    # -- strike ---------------------------------------------------------------
    if ks is None or ps is None:
        if ks is not None or ps is not None:
            # A price market on one side and an event market on the other.
            # Not the same question, full stop.
            return None
        # Event markets on BOTH sides (KXBTCRESERVE and friends): there is no
        # threshold to agree on, so this can never be auto-confirmed.  It is
        # surfaced for a human, and the word-overlap gate below exists only to
        # keep that human-review list finite -- overlap is NEVER evidence of a
        # match, and MANUAL pairs are excluded from the verdict unless a
        # --pair-review file promotes them.
        overlap = _significant_overlap(k.title + " " + k.series_title, p.question)
        if len(overlap) < 2:
            return None
        blockers.append("no numeric strike on either side: event market, "
                        "resolution text must be read by a human")
        matched["word_overlap"] = sorted(overlap)
        return Pair(k, p, "MANUAL", matched, blockers)
    rel = abs(ks - ps) / max(abs(ks), 1e-9)
    if rel > strike_tol:
        return None
    matched["strike"] = {"kalshi": ks, "poly": ps, "rel_diff": rel}

    # -- direction ------------------------------------------------------------
    if kd == "ambiguous" or pd == "ambiguous":
        blockers.append(f"direction ambiguous (kalshi={kd}, poly={pd})")
    elif kd != pd:
        return None
    else:
        matched["direction"] = kd

    # -- touch vs terminal ----------------------------------------------------
    kst, pst = k.style, p.style
    if kst == "ambiguous" or pst == "ambiguous":
        blockers.append(f"style ambiguous (kalshi={kst}, poly={pst})")
    elif kst != pst:
        # THE trap this whole function exists for: same asset, same strike,
        # same date, opposite payoff on a wick.
        return None
    else:
        matched["style"] = kst

    confidence = "HIGH" if not blockers else "MEDIUM"
    return Pair(k, p, confidence, matched, blockers)


def match_all(kmarkets: Sequence[KalshiMarket], pmarkets: Sequence[PolyMarket],
              *, strike_tol: float, date_tol_h: float) -> list[Pair]:
    by_asset: dict[str, list[PolyMarket]] = defaultdict(list)
    for p in pmarkets:
        a = p.asset
        if a:
            by_asset[a].append(p)

    pairs: list[Pair] = []
    for k in kmarkets:
        a = k.asset
        if not a:
            continue
        found: list[Pair] = []
        for p in by_asset.get(a, ()):
            got = match_one(k, p, strike_tol=strike_tol, date_tol_h=date_tol_h)
            if got is not None:
                found.append(got)
        # HIGH pairs are never truncated -- they are the verdict's input, and a
        # silently dropped one is a silently wrong sample size.  The lower
        # tiers exist to be read by a human, so they are capped per ticker.
        pairs.extend(g for g in found if g.confidence == "HIGH")
        for tier in ("MEDIUM", "MANUAL"):
            rows = [g for g in found if g.confidence == tier]
            if len(rows) > MAX_REVIEW_PER_KALSHI:
                rows.sort(key=lambda g: -len(g.matched.get("word_overlap") or ()))
                rows = rows[:MAX_REVIEW_PER_KALSHI]
            pairs.extend(rows)
    return pairs


# ═════════════════════════════════════════════════════════════════════════════
# Discovery
# ═════════════════════════════════════════════════════════════════════════════

async def discover_kalshi(api: _ReadOnlyKalshi, *, exclude_frequency: set[str],
                          include_series: set[str], verbose: bool = True
                          ) -> tuple[list[KalshiMarket], dict[str, dict]]:
    """
    Every OPEN market in every SLOW crypto series, with its series metadata.

    Series selection is a union of three independent signals, because no one
    of them is complete:
      1. catalog category == "Crypto"        (272 series, misses event markets)
      2. a crypto tag on the series          (catches KXBTCRESERVE, Politics)
      3. a crypto word in ticker or title    (catches anything mis-tagged)
    plus --include-series, which is always honoured even if all three miss.
    """
    catalog = (await api.get("/series")).get("series") or []
    if verbose:
        print(f"  series catalog: {len(catalog)} series")

    selected: dict[str, dict] = {}
    reasons: dict[str, str] = {}
    for s in catalog:
        ticker = s.get("ticker")
        if not ticker:
            continue
        tags = set(s.get("tags") or [])
        why = None
        if s.get("category") == "Crypto":
            why = "category"
        elif tags & CRYPTO_TAGS:
            why = "tag"
        elif CRYPTO_TITLE_RX.search(f"{ticker} {s.get('title') or ''}"):
            why = "title"
        if why is None:
            continue
        freq = s.get("frequency")
        if freq in exclude_frequency and ticker not in include_series:
            continue
        selected[ticker] = s
        reasons[ticker] = why

    # --include-series wins outright, including over the frequency filter.
    for ticker in include_series:
        if ticker not in selected:
            try:
                s = (await api.get(f"/series/{ticker}")).get("series") or {}
            except Exception as exc:  # noqa: BLE001
                print(f"  !! --include-series {ticker}: {exc}")
                continue
            if s:
                selected[ticker] = s
                reasons[ticker] = "explicit"

    if verbose:
        counts = defaultdict(int)
        for t in selected:
            counts[reasons[t]] += 1
        print(f"  slow crypto series selected: {len(selected)} "
              f"({dict(counts)})")

    markets: list[KalshiMarket] = []
    for ticker, meta in sorted(selected.items()):
        try:
            data = await api.get("/markets", {"series_ticker": ticker,
                                              "status": "open", "limit": 1000})
        except Exception as exc:  # noqa: BLE001
            print(f"  !! /markets series={ticker}: {exc}")
            continue
        for raw in data.get("markets") or []:
            markets.append(parse_kalshi_market(raw, meta))
    return markets, selected


async def discover_polymarket(api: _ReadOnlyPolymarket, *, min_volume: float,
                              assets: Sequence[str], verbose: bool = True
                              ) -> list[PolyMarket]:
    """
    Open Polymarket YES/NO markets that could plausibly pair with a Kalshi
    crypto market.  Two independent sweeps, unioned:

      1. gamma tag_id=21 (crypto) + related_tags, filtered by volume_num_min.
         gamma refuses offsets past ~2000, and the unfiltered crypto tag has
         more markets than that, so the volume floor is load-bearing, not
         cosmetic.  It is also defensible on its own terms: a market with no
         volume has no price to compare.
      2. gamma /public-search per asset name, which reaches markets the tag
         misses (mis-tagged, or tagged only under an event).

    Both sweeps are counted and printed so the coverage claim is auditable.
    """
    seen: dict[str, PolyMarket] = {}
    tag_rows = 0
    offset = 0
    while offset <= POLY_GAMMA_MAX_OFFSET:
        try:
            data = await api.get(POLY_GAMMA, "/markets", {
                "closed": "false", "active": "true", "limit": POLY_GAMMA_PAGE,
                "offset": offset, "tag_id": POLY_CRYPTO_TAG_ID,
                "related_tags": "true", "volume_num_min": min_volume,
            })
        except Exception as exc:  # noqa: BLE001
            print(f"  !! gamma /markets offset={offset}: {exc}")
            break
        if not isinstance(data, list) or not data:
            break
        tag_rows += len(data)
        for raw in data:
            pm = parse_poly_market(raw)
            if pm and pm.condition_id:
                seen.setdefault(pm.condition_id, pm)
        offset += POLY_GAMMA_PAGE

    search_rows = 0
    for asset in assets:
        for word in ASSET_WORDS.get(asset, (asset.lower(),)):
            # public-search returns EVENTS with their nested markets; it reaches
            # markets the crypto tag misses.
            try:
                found = await api.get(POLY_GAMMA, "/public-search",
                                      {"q": word, "limit_per_type": 20})
            except Exception as exc:  # noqa: BLE001
                print(f"  !! gamma /public-search q={word}: {exc}")
                continue
            for ev in (found or {}).get("events", []) if isinstance(found, dict) else []:
                for raw in ev.get("markets") or []:
                    pm = parse_poly_market(raw)
                    if pm and pm.condition_id and pm.condition_id not in seen:
                        search_rows += 1
                        seen[pm.condition_id] = pm

    if verbose:
        print(f"  polymarket: {tag_rows} rows from tag sweep (volume >= "
              f"${min_volume:,.0f}), +{search_rows} unique from keyword search "
              f"-> {len(seen)} YES/NO candidates")
    return list(seen.values())


# ═════════════════════════════════════════════════════════════════════════════
# Quote extraction
# ═════════════════════════════════════════════════════════════════════════════

def kalshi_quote_row(raw: dict, ts_ms: int, seq: int) -> tuple:
    """
    Top-of-book + SIZE from a /markets row.

    Kalshi's own convention (verified live 2026-08-20 against
    /markets/{t}/orderbook for the same ticker): yes_ask_size_fp is the size
    resting on the NO bid at 1 - yes_ask, i.e. exactly the quantity available
    to BUY YES at yes_ask; yes_bid_size_fp is likewise the quantity available
    to BUY NO at 1 - yes_bid.  Both sides of the tradeable price are therefore
    covered without a second request.  Sizes are fractional (Kalshi quotes
    fixed-point contract quantities), so they stay floats.
    """
    yb = fnum(raw.get("yes_bid_dollars"))
    ya = fnum(raw.get("yes_ask_dollars"))
    ybs = fnum(raw.get("yes_bid_size_fp"))
    yas = fnum(raw.get("yes_ask_size_fp"))
    nb = fnum(raw.get("no_bid_dollars"))
    na = fnum(raw.get("no_ask_dollars"))
    # A one-sided book has no mid.  NULL, never a fabricated 0.5 -- an
    # invented mid is indistinguishable from a real one downstream.
    mid = None
    if yb is not None and ya is not None and 0.0 < yb <= ya < 1.0:
        mid = (yb + ya) / 2.0
    return (ts_ms, seq, raw.get("ticker"), yb, ybs, ya, yas, nb, na, mid,
            fnum(raw.get("last_price_dollars")),
            fnum(raw.get("open_interest_fp")), fnum(raw.get("volume_fp")))


def parse_kalshi_ladder(data: dict) -> tuple[list, list]:
    """
    REST orderbook -> (yes_levels, no_levels) as [[price, size], ...] BID
    ladders, best first.

    The response nests under "orderbook_fp" with "yes_dollars"/"no_dollars"
    keys (verified live); older/newer spellings are accepted rather than
    crashing the recorder mid-run.
    """
    ob = data.get("orderbook_fp") or data.get("orderbook") or {}
    def side(*keys: str) -> list:
        for k in keys:
            raw = ob.get(k)
            if raw:
                out = []
                for lvl in raw:
                    px, sz = fnum(lvl[0]), fnum(lvl[1])
                    if px is not None and sz is not None:
                        out.append([px, sz])
                out.sort(key=lambda x: -x[0])   # best bid first
                return out[:TOP_N_LEVELS]
        return []
    return side("yes_dollars", "yes_dollars_fp"), side("no_dollars", "no_dollars_fp")


def poly_quote_row(book: dict, cond_id: str, token: str, ts_ms: int, seq: int) -> tuple:
    """
    Polymarket CLOB book -> top of book.

    Sides are returned worst-first (bids ascending, asks descending), so the
    top of book is taken by min/max rather than by position: relying on the
    ordering would break silently the day it changes.
    """
    def best(levels: Any, want_max: bool) -> tuple[float | None, float | None]:
        rows = []
        for lvl in levels or []:
            px, sz = fnum(lvl.get("price")), fnum(lvl.get("size"))
            if px is not None and sz is not None and sz > 0:
                rows.append((px, sz))
        if not rows:
            return None, None
        return max(rows, key=lambda r: r[0]) if want_max else min(rows, key=lambda r: r[0])

    bid, bid_sz = best(book.get("bids"), True)
    ask, ask_sz = best(book.get("asks"), False)
    mid = (bid + ask) / 2.0 if (bid is not None and ask is not None and bid <= ask) else None
    venue_ts = None
    try:
        venue_ts = int(book.get("timestamp")) if book.get("timestamp") else None
    except (TypeError, ValueError):
        venue_ts = None
    return (ts_ms, seq, cond_id, token, bid, bid_sz, ask, ask_sz, mid,
            venue_ts, fnum(book.get("tick_size")))


# ═════════════════════════════════════════════════════════════════════════════
# Shock detection
# ═════════════════════════════════════════════════════════════════════════════

class ShockWatcher:
    """
    A shock is a move of >= `bps` over `window_s` seconds in one asset's spot.

    Refractory folding: a second qualifying move inside `refractory_s` of the
    last recorded shock for the same asset is NOT a new observation.  Without
    this, a single 80 bps candle emits a shock on every evaluation tick for
    the whole window, and the "n >= 30 independent events" criterion in the
    analyzer becomes n >= 30 samples of one event.
    """

    def __init__(self, store: Store, *, bps: float, window_s: float,
                 refractory_s: float) -> None:
        self._store = store
        self._bps = bps
        self._window_s = window_s
        self._refractory_ms = int(refractory_s * 1000)
        self._last: dict[str, int] = {}
        self.count = 0

    def prime(self, assets: Iterable[str]) -> None:
        """Resume state: a shock recorded 30s before a restart still suppresses."""
        for a in assets:
            last = self._store.last_shock_ms(a)
            if last is not None:
                self._last[a] = last

    def evaluate(self, asset: str, history: Sequence[tuple[float, float]]) -> dict | None:
        if len(history) < 2:
            return None
        ref_ts, ref_px = history[0]
        now_ts, now_px = history[-1]
        if ref_px <= 0:
            return None
        bps = 1e4 * (now_px - ref_px) / ref_px
        if abs(bps) < self._bps:
            return None
        ts_ms = int(now_ts * 1000)
        last = self._last.get(asset)
        if last is not None and ts_ms - last < self._refractory_ms:
            return None
        self._last[asset] = ts_ms
        self.count += 1
        shock_id = self._store.put_shock(
            asset, ts_ms, int(ref_ts * 1000), ref_px, now_px, bps, self._window_s)
        return {"id": shock_id, "asset": asset, "ts_ms": ts_ms, "bps": bps,
                "ref_price": ref_px, "price": now_px}


# ═════════════════════════════════════════════════════════════════════════════
# Recorder
# ═════════════════════════════════════════════════════════════════════════════

class Recorder:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.dry = bool(args.selftest)   # --selftest polls live but writes nothing
        self.store = Store(Path(args.db), dry=self.dry)
        self.kalshi = _ReadOnlyKalshi()
        self.poly = None if args.no_polymarket else _ReadOnlyPolymarket()
        self.seq = self.store.next_seq()
        self.series_meta: dict[str, dict] = {}
        self.kmarkets: list[KalshiMarket] = []
        self.pmarkets: dict[str, PolyMarket] = {}
        self.pairs: list[Pair] = []
        self.poll_series: list[str] = []
        self.poll_tokens: list[tuple[str, str]] = []   # (condition_id, token)
        self.depth_tickers: list[str] = []
        self._last_depth_ms = 0
        self._cycles = 0
        self._stop = False

    # -- discovery ------------------------------------------------------------
    async def discover(self, *, verbose: bool = True) -> None:
        t0 = time.monotonic()
        if verbose:
            print("\nDISCOVERY")
        self.kmarkets, self.series_meta = await discover_kalshi(
            self.kalshi,
            exclude_frequency=set(self.args.exclude_frequency),
            include_series=set(self.args.include_series),
            verbose=verbose,
        )
        by_series: dict[str, int] = defaultdict(int)
        for m in self.kmarkets:
            by_series[m.series] += 1
        self.poll_series = sorted(by_series)

        if verbose:
            print(f"  kalshi open slow-crypto markets: {len(self.kmarkets)} "
                  f"across {len(self.poll_series)} series "
                  f"({len(self.poll_series)} requests per poll cycle)")

        # Depth subset: the markets whose ladders are worth storing.  Ranked by
        # open interest because that is the only field that says "someone is
        # actually holding this", not "someone traded it once in March".
        ranked = sorted(
            self.kmarkets,
            key=lambda m: -(fnum(m.raw.get("open_interest_fp")) or 0.0))
        self.depth_tickers = [m.ticker for m in ranked[: self.args.depth_markets]]

        if self.poly is not None:
            pms = await discover_polymarket(
                self.poly, min_volume=self.args.poly_min_volume,
                assets=self.args.assets, verbose=verbose)
            self.pmarkets = {p.condition_id: p for p in pms}
            self.pairs = match_all(
                self.kmarkets, pms,
                strike_tol=self.args.strike_tol,
                date_tol_h=self.args.date_tol_hours)
            wanted = {p.poly.condition_id for p in self.pairs}
            self.poll_tokens = [
                (cid, self.pmarkets[cid].token_yes)
                for cid in sorted(wanted)
                if self.pmarkets[cid].token_yes
            ]
        if verbose:
            self._print_inventory()
        self.store.log("discovery",
                       f"series={len(self.poll_series)} markets={len(self.kmarkets)} "
                       f"poly_candidates={len(self.pmarkets)} pairs={len(self.pairs)} "
                       f"tokens={len(self.poll_tokens)} took={time.monotonic()-t0:.1f}s",
                       echo=False)

    def _print_inventory(self) -> None:
        print("\nKALSHI SLOW CRYPTO MARKETS FOUND")
        print(f"  {'series':<22} {'freq':<12} {'n':>4}  {'asset':<6} title")
        by_series: dict[str, list[KalshiMarket]] = defaultdict(list)
        for m in self.kmarkets:
            by_series[m.series].append(m)
        for series in sorted(by_series, key=lambda s: -len(by_series[s])):
            ms = by_series[series]
            meta = self.series_meta.get(series, {})
            print(f"  {series:<22} {str(meta.get('frequency')):<12} {len(ms):>4}  "
                  f"{str(ms[0].asset or '-'):<6} {(meta.get('title') or '')[:42]}")
        print(f"  ---- {len(self.kmarkets)} markets, {len(by_series)} series")
        print(f"  depth ladders sampled for the top {len(self.depth_tickers)} "
              f"by open interest, every {self.args.depth_interval}s")

        if self.poly is None:
            print("\nPOLYMARKET: disabled (--no-polymarket); H2 will be "
                  "INSUFFICIENT DATA.")
            return

        print("\nCROSS-VENUE PAIRS PROPOSED")
        counts = defaultdict(int)
        for p in self.pairs:
            counts[p.confidence] += 1
        print(f"  HIGH={counts['HIGH']}  MEDIUM={counts['MEDIUM']}  "
              f"MANUAL={counts['MANUAL']}   "
              f"(only HIGH feeds the H2 verdict)")
        for conf in ("HIGH", "MEDIUM", "MANUAL"):
            rows = [p for p in self.pairs if p.confidence == conf]
            if not rows:
                continue
            print(f"\n  -- {conf} --")
            for p in rows[:40]:
                dd = p.date_delta_h
                print(f"  {p.kalshi.ticker:<38} <-> {p.poly.slug[:44]}")
                print(f"      asset={p.matched.get('asset')} "
                      f"strike k={p.kalshi.strike} p={p.poly.strike} "
                      f"dir={p.kalshi.direction}/{p.poly.direction} "
                      f"style={p.kalshi.style}/{p.poly.style} "
                      f"date_delta={dd if dd is None else round(dd, 1)}h")
                if p.blockers:
                    print(f"      BLOCKERS: {'; '.join(p.blockers)}")
            if len(rows) > 40:
                print(f"      ... {len(rows) - 40} more (all are in the `pair` table)")
        if not counts["HIGH"]:
            print("\n  NOTE: zero HIGH-confidence pairs. That is a RESULT, not a "
                  "bug --\n  it means no crypto question is listed on both venues "
                  "with the same\n  asset, strike, direction, touch/terminal style "
                  "and resolution date.\n  H2 will report INSUFFICIENT DATA rather "
                  "than relaxing the gates.")

    def persist_definitions(self) -> None:
        if self.dry:
            return
        ts = now_ms()
        self.store.upsert_kalshi_markets([
            (m.ticker, m.series, m.event_ticker, m.title, m.subtitle,
             m.yes_sub_title, m.rules_primary, m.rules_secondary, m.floor_strike,
             m.cap_strike, m.strike_type, m.open_ts, m.close_ts, m.expiration_ts,
             m.frequency, m.category, json.dumps(m.tags), m.series_title,
             json.dumps(m.settlement_sources), m.asset, m.style, m.direction,
             ts, ts, json.dumps(m.raw))
            for m in self.kmarkets
        ])
        if self.pmarkets:
            self.store.upsert_poly_markets([
                (p.condition_id, p.market_id, p.question, p.description, p.slug,
                 p.end_ts, p.token_yes, p.token_no, json.dumps(p.outcomes),
                 p.volume, p.liquidity, p.asset, p.style, p.direction, p.strike,
                 ts, ts, json.dumps(p.raw))
                for p in self.pmarkets.values()
            ])
        if self.pairs:
            self.store.upsert_pairs([
                (p.kalshi.ticker, p.poly.condition_id, p.confidence,
                 p.matched.get("asset"), p.kalshi.strike, p.poly.strike,
                 (p.matched.get("strike") or {}).get("rel_diff"),
                 p.matched.get("direction"), p.matched.get("style"),
                 p.kalshi.close_ts, p.poly.end_ts, p.date_delta_h,
                 json.dumps(p.matched), json.dumps(p.blockers),
                 p.kalshi.text, p.poly.text, ts)
                for p in self.pairs
            ])

    # -- polling --------------------------------------------------------------
    async def poll_cycle(self) -> dict:
        seq = self.seq
        self.seq += 1
        started = now_ms()
        stats: dict[str, int] = {
            "kalshi_series": len(self.poll_series), "kalshi_ok": 0,
            "kalshi_fail": 0, "kalshi_quotes": 0, "poly_ok": 0, "poly_fail": 0,
        }

        async def one_series(series: str) -> list[tuple]:
            data = await self.kalshi.get("/markets", {
                "series_ticker": series, "status": "open", "limit": 1000})
            ts = now_ms()
            return [kalshi_quote_row(raw, ts, seq)
                    for raw in (data.get("markets") or [])
                    if raw.get("ticker")]

        results = await asyncio.gather(
            *(one_series(s) for s in self.poll_series), return_exceptions=True)
        quote_rows: list[tuple] = []
        for series, res in zip(self.poll_series, results):
            if isinstance(res, BaseException):
                stats["kalshi_fail"] += 1
                self.store.log("poll_fail", f"kalshi series={series}: {res}",
                               echo=False)
            else:
                stats["kalshi_ok"] += 1
                quote_rows.extend(res)
        stats["kalshi_quotes"] = len(quote_rows)
        if quote_rows and not self.dry:
            self.store.put_quotes(quote_rows)
        if stats["kalshi_fail"] > max(1, len(self.poll_series) // 5):
            self.store.log(
                "coverage_degraded",
                f"cycle {seq}: {stats['kalshi_fail']}/{len(self.poll_series)} "
                f"kalshi series failed -- this cycle is not full coverage")

        # Polymarket: one book per paired YES token.
        if self.poly is not None and self.poll_tokens:
            async def one_token(cid: str, token: str) -> tuple:
                book = await self.poly.get(POLY_CLOB, "/book", {"token_id": token})
                return poly_quote_row(book or {}, cid, token, now_ms(), seq)

            pres = await asyncio.gather(
                *(one_token(c, t) for c, t in self.poll_tokens),
                return_exceptions=True)
            prows: list[tuple] = []
            for (cid, token), res in zip(self.poll_tokens, pres):
                if isinstance(res, BaseException):
                    stats["poly_fail"] += 1
                    self.store.log("poll_fail", f"poly token={token[:16]}: {res}",
                                   echo=False)
                else:
                    stats["poly_ok"] += 1
                    prows.append(res)
            if prows and not self.dry:
                self.store.put_poly_quotes(prows)

        # Depth ladders, on their own slower clock.
        if (self.depth_tickers
                and started - self._last_depth_ms >= self.args.depth_interval * 1000):
            self._last_depth_ms = started

            async def one_book(ticker: str) -> tuple:
                data = await self.kalshi.get(f"/markets/{ticker}/orderbook",
                                             {"depth": TOP_N_LEVELS})
                yes, no = parse_kalshi_ladder(data)
                return (now_ms(), ticker, json.dumps(yes), json.dumps(no))

            bres = await asyncio.gather(
                *(one_book(t) for t in self.depth_tickers), return_exceptions=True)
            brows = [r for r in bres if not isinstance(r, BaseException)]
            nfail = len(bres) - len(brows)
            if nfail:
                self.store.log("poll_fail", f"kalshi depth: {nfail} failed", echo=False)
            if brows and not self.dry:
                self.store.put_books(brows)

        ended = now_ms()
        self._cycles += 1
        return {"seq": seq, "started": started, "ended": ended, "stats": dict(stats)}

    # -- loops ----------------------------------------------------------------
    async def poll_loop(self) -> None:
        interval = self.args.interval
        prev_end: int | None = self.store.last_observation_ms()
        while not self._stop:
            cycle = await self.poll_cycle()
            gap_s = None
            if prev_end is not None:
                gap_s = (cycle["started"] - prev_end) / 1000.0
                if gap_s > interval * CYCLE_OVERRUN_FACTOR:
                    self.store.log(
                        "coverage_gap",
                        f"{gap_s:.1f}s between cycles (interval={interval}s) "
                        f"-- analysis must not interpolate across this")
            self.store.put_cycle(cycle["seq"], cycle["started"], cycle["ended"],
                                 cycle["stats"], gap_s)
            prev_end = cycle["ended"]

            took = (cycle["ended"] - cycle["started"]) / 1000.0
            if took > interval:
                self.store.log(
                    "cycle_overrun",
                    f"cycle {cycle['seq']} took {took:.1f}s > interval "
                    f"{interval}s ({cycle['stats'].get('kalshi_series')} series, "
                    f"{len(self.poll_tokens)} poly tokens) -- effective interval "
                    f"is now {took:.1f}s")
            await asyncio.sleep(max(0.0, interval - took))

    async def spot_loop(self, feeds: dict[str, Any], watcher: ShockWatcher) -> None:
        """Sample spot into the DB and evaluate the shock rule on every sample."""
        interval = self.args.spot_interval
        while not self._stop:
            ts = now_ms()
            rows = []
            for asset, feed in feeds.items():
                px = feed.price
                if px is not None:
                    rows.append((ts, asset, float(px),
                                 "coinbase_ws" if feed.ws_connected else "coinbase_rest"))
                hist = feed.history_within(self.args.shock_window)
                got = watcher.evaluate(asset, hist)
                if got is not None:
                    self.store.log(
                        "shock",
                        f"#{got['id']} {asset} {got['bps']:+.0f}bps in "
                        f"{self.args.shock_window:.0f}s "
                        f"({got['ref_price']:.2f} -> {got['price']:.2f})")
            if rows:
                self.store.put_spot(rows)
            await asyncio.sleep(interval)

    async def rediscover_loop(self) -> None:
        minutes = self.args.rediscover_minutes
        if minutes <= 0:
            return
        while not self._stop:
            await asyncio.sleep(minutes * 60)
            if self._stop:
                return
            before = {m.ticker for m in self.kmarkets}
            try:
                await self.discover(verbose=False)
            except Exception as exc:  # noqa: BLE001
                self.store.log("rediscovery_failed", str(exc))
                continue
            after = {m.ticker for m in self.kmarkets}
            self.persist_definitions()
            added, gone = after - before, before - after
            if added or gone:
                self.store.log("universe_changed",
                               f"+{len(added)} -{len(gone)} markets "
                               f"(now {len(after)})")

    async def heartbeat_loop(self, deadline: float) -> None:
        """One status line a minute: silence for 24h is indistinguishable from
        a hung process, and this study is long enough that it matters."""
        while not self._stop:
            await asyncio.sleep(60)
            if self._stop:
                return
            left = max(0.0, deadline - time.monotonic())
            recent = self.store.quotes_since_seq(max(1, self.seq - 6))
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] cycles={self._cycles} "
                  f"quotes(last~6)={recent} "
                  f"kalshi_req={self.kalshi.request_count} "
                  f"(err {self.kalshi.error_count}) "
                  + (f"poly_req={self.poly.request_count} "
                     f"(err {self.poly.error_count}) " if self.poly else "")
                  + f"remaining={left/3600:.2f}h", flush=True)

    async def aclose(self) -> None:
        await self.kalshi.aclose()
        if self.poly is not None:
            await self.poly.aclose()


# ═════════════════════════════════════════════════════════════════════════════
# Selftest
# ═════════════════════════════════════════════════════════════════════════════

async def selftest(rec: Recorder) -> int:
    print("\nSELFTEST -- discovery + one live poll cycle, nothing written to the DB")
    await rec.discover()
    t0 = time.monotonic()
    cycle = await rec.poll_cycle()
    took = time.monotonic() - t0
    st = cycle["stats"]
    print(f"\n  one cycle: {took:.1f}s   kalshi_ok={st.get('kalshi_ok')} "
          f"kalshi_fail={st.get('kalshi_fail')} quotes={st.get('kalshi_quotes')} "
          f"poly_ok={st.get('poly_ok')} poly_fail={st.get('poly_fail')}")
    if took > rec.args.interval:
        print(f"  !! a cycle costs {took:.1f}s but --interval is "
              f"{rec.args.interval}s: the effective interval will be {took:.1f}s. "
              f"Raise --interval or lower --depth-markets.")
    else:
        print(f"  cycle fits inside --interval {rec.args.interval}s with "
              f"{rec.args.interval - took:.1f}s of headroom.")

    print("\n  spot feed check (20s)...")
    feeds = {"BTC": BTCFeed(poll_interval=5.0), "ETH": ETHFeed(poll_interval=5.0)}
    tasks = [asyncio.create_task(f.run_ws()) for f in feeds.values()]
    tasks += [asyncio.create_task(f.run()) for f in feeds.values()]
    await asyncio.sleep(20)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    ok = True
    for asset, feed in feeds.items():
        n = len(feed.history)
        print(f"    {asset}: price={feed.price} ws_connected={feed.ws_connected} "
              f"ticks={feed.ws_tick_count} history={n}")
        if feed.price is None or n < 2:
            ok = False
    if not ok:
        print("  !! a spot feed produced no usable history: shock detection would "
              "silently observe ZERO shocks. Fix the feed before recording.")
        return 1
    print("\n  SELFTEST PASSED")
    return 0


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="READ-ONLY recorder for slow crypto markets (Kalshi + Polymarket + spot).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB, help="sqlite output; reused = resumed")
    p.add_argument("--hours", type=float, default=24.0, help="record for this long")
    p.add_argument("--interval", type=float, default=10.0, help="poll interval, seconds")
    p.add_argument("--spot-interval", type=float, default=1.0,
                   help="spot sample + shock evaluation interval, seconds")
    p.add_argument("--shock-bps", type=float, default=50.0,
                   help="a shock is a move of at least this many bps")
    p.add_argument("--shock-window", type=float, default=300.0,
                   help="...measured over this many seconds")
    p.add_argument("--shock-refractory", type=float, default=None,
                   help="suppress a new shock within this many seconds of the "
                        "last one for the same asset (default: --shock-window)")
    p.add_argument("--assets", default="BTC,ETH",
                   help="spot assets used as the shock detector")
    p.add_argument("--depth-markets", type=int, default=40,
                   help="how many markets get full ladders (0 disables)")
    p.add_argument("--depth-interval", type=float, default=60.0,
                   help="seconds between ladder samples")
    p.add_argument("--rediscover-minutes", type=float, default=60.0,
                   help="re-enumerate the universe this often (0 disables)")
    p.add_argument("--exclude-frequency", default=",".join(DEFAULT_EXCLUDE_FREQUENCY),
                   help="series frequencies to skip: the fast, bot-dominated books")
    p.add_argument("--include-series", default="",
                   help="comma-separated series to force in, whatever their frequency")
    p.add_argument("--no-polymarket", action="store_true",
                   help="Kalshi + spot only (H1 only; H2 becomes INSUFFICIENT DATA)")
    p.add_argument("--poly-min-volume", type=float, default=10_000.0,
                   help="gamma volume floor for the Polymarket sweep")
    p.add_argument("--strike-tol", type=float, default=0.01,
                   help="relative strike tolerance for a cross-venue match")
    p.add_argument("--date-tol-hours", type=float, default=36.0,
                   help="resolution-date tolerance for a cross-venue match")
    p.add_argument("--enumerate-only", action="store_true",
                   help="discover, print the universe and the proposed pairs, exit")
    p.add_argument("--selftest", action="store_true",
                   help="discovery + one live cycle + a 20s feed check, no writes")
    return p


async def run(args: argparse.Namespace) -> int:
    rec = Recorder(args)
    try:
        if args.selftest:
            return await selftest(rec)

        if args.enumerate_only:
            await rec.discover()
            rec.persist_definitions()
            print("\n--enumerate-only: definitions and proposed pairs written to "
                  f"{args.db}; no quotes recorded.")
            return 0

        run_id = rec.store.start_run(" ".join(sys.argv))
        prior = rec.store.recorded_seconds()
        last_obs = rec.store.last_observation_ms()
        print(f"\nrun_id={run_id}  db={args.db}")
        if last_obs is not None:
            gap_h = (now_ms() - last_obs) / 3_600_000.0
            print(f"  RESUMING: {prior/3600:.2f}h already recorded, last "
                  f"observation {iso(last_obs)} ({gap_h:.2f}h ago)")
            rec.store.log("resume_gap",
                          f"{gap_h*3600:.0f}s of no coverage before this run "
                          f"-- analysis must treat this as a hole, not a flat line")

        await rec.discover()
        rec.persist_definitions()

        if not rec.kmarkets:
            rec.store.log("fatal", "discovery found ZERO slow crypto markets")
            print("\nFATAL: zero slow crypto markets discovered. Nothing to record.")
            return 2

        feeds: dict[str, Any] = {}
        for a in args.assets:
            if a == "BTC":
                feeds["BTC"] = BTCFeed(poll_interval=5.0,
                                       vol_window_seconds=args.shock_window)
            elif a == "ETH":
                feeds["ETH"] = ETHFeed(poll_interval=5.0,
                                       vol_window_seconds=args.shock_window)
            else:
                print(f"  !! no kbot feed for {a}; shock detection will not see it.")
        if not feeds:
            print("\nFATAL: no usable spot feed, so no shock can ever be detected "
                  "and H1 is unanswerable. Aborting rather than recording a "
                  "study that cannot conclude.")
            rec.store.log("fatal", "no spot feed")
            return 2

        watcher = ShockWatcher(rec.store, bps=args.shock_bps,
                               window_s=args.shock_window,
                               refractory_s=args.shock_refractory)
        watcher.prime(feeds)

        deadline = time.monotonic() + args.hours * 3600.0
        print(f"\nRECORDING for {args.hours}h  (interval {args.interval}s, "
              f"shock = {args.shock_bps:.0f}bps / {args.shock_window:.0f}s, "
              f"refractory {args.shock_refractory:.0f}s)")
        print("  Ctrl-C is safe: the DB is committed per cycle and the run "
              "resumes cleanly.\n")

        tasks: dict[asyncio.Task, str] = {}
        for asset, f in feeds.items():
            tasks[asyncio.create_task(f.run_ws())] = f"{asset}_ws"
            tasks[asyncio.create_task(f.run())] = f"{asset}_rest"
        tasks[asyncio.create_task(rec.spot_loop(feeds, watcher))] = "spot_loop"
        tasks[asyncio.create_task(rec.poll_loop())] = "poll_loop"
        if args.rediscover_minutes > 0:
            tasks[asyncio.create_task(rec.rediscover_loop())] = "rediscover_loop"
        tasks[asyncio.create_task(rec.heartbeat_loop(deadline))] = "heartbeat"
        timer = asyncio.create_task(asyncio.sleep(args.hours * 3600.0))
        tasks[timer] = "duration_timer"

        # Supervised, not fire-and-forget: if poll_loop dies at hour 3 of 24,
        # a plain `await sleep(24h)` would keep the process alive and produce a
        # database that looks complete and contains 21 hours of nothing.  The
        # first task to finish ends the run, and which one it was gets printed.
        note = "completed"
        try:
            done, _ = await asyncio.wait(tasks.keys(),
                                         return_when=asyncio.FIRST_COMPLETED)
            first = next(iter(done))
            name = tasks[first]
            exc = first.exception() if not first.cancelled() else None
            if name != "duration_timer" or exc is not None:
                note = f"aborted: {name} exited ({exc!r})" if exc else f"aborted: {name} exited"
                rec.store.log("fatal", note)
                print(f"\n  !! {note} -- ending the run rather than recording "
                      f"a silent hole.")
        except (KeyboardInterrupt, asyncio.CancelledError):
            note = "interrupted"
            print("\n  interrupted -- closing out cleanly.")
        finally:
            rec._stop = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        rec.store.end_run(rec._cycles, note)
        print(f"\nDONE  cycles={rec._cycles}  shocks_this_run={watcher.count}  "
              f"kalshi_requests={rec.kalshi.request_count} "
              f"(errors {rec.kalshi.error_count})"
              + (f"  poly_requests={rec.poly.request_count} "
                 f"(errors {rec.poly.error_count})" if rec.poly else ""))
        print(f"  total recorded across all runs: "
              f"{rec.store.recorded_seconds()/3600:.2f}h")
        print(f"\n  next: python scripts/analyze_crypto_slow.py --db {args.db}")
        return 0
    finally:
        await rec.aclose()
        rec.store.close()


def main() -> int:
    args = build_parser().parse_args()
    args.exclude_frequency = [f.strip() for f in args.exclude_frequency.split(",") if f.strip()]
    args.include_series = [s.strip().upper() for s in args.include_series.split(",") if s.strip()]
    args.assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    if args.shock_refractory is None:
        args.shock_refractory = args.shock_window
    if args.interval < 5.0:
        print("--interval below 5s is pointless on books that move a few times "
              "an hour, and it burns the read limit. Refusing.")
        return 2
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
