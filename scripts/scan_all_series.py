#!/usr/bin/env python
"""
Exchange-wide, READ-ONLY calibration scan of Kalshi's own prices.

WHY THIS EXISTS
scripts/market_calibration_study.py asked "are KXBTC15M / KXETH15M prices
miscalibrated?" over ~2,880 settled markets and answered NO -- nothing
exploitable after fees. Rather than guess at the next series to test, this
script runs THE SAME analysis across every series on the exchange that has
enough settled history to support it, and lets the data nominate candidates.

Scanning everything is statistically dangerous in a way scanning one series is
not: test 800 (series, slice, bucket) cells at alpha=0.05 and ~40 will look
"significant" with no edge whatsoever. Two mandatory defences are built in and
cannot be switched off:
  * Benjamini-Hochberg FDR control over the whole family of discovery tests,
    with the raw p, the FDR-adjusted p, and the total test count all printed.
  * A strict time split: candidates are DISCOVERED on the earlier half of each
    series' history and must independently CONFIRM on the later half, which the
    discovery step never touched. A bucket that only shows up in one half is
    reported as unconfirmed and is not a finding.

READ-ONLY -- STRUCTURAL, NOT JUST BY CONVENTION
This script never constructs a KalshiClient (or any other object that owns an
order method). Every byte of network I/O goes through _ReadOnlyAPI.get(), which
  * only ever issues HTTP GET, and
  * refuses any path not matching its allowlist of five read endpoints:
        GET /markets
        GET /markets/{ticker}
        GET /markets/{ticker}/orderbook
        GET /series
        GET /series/{s}/markets/{m}/candlesticks
Adding a write would mean defeating both checks on purpose. Don't. The only
things this script writes are its own sqlite cache and the CSV/JSON you ask for.

1. SERIES ENUMERATION -- what endpoint, and why it is complete
   The existing get_events() paging is NOT a reliable census: /events paged
   without a series filter misses series outright (KXBTC15M does not appear in
   its results), so "the set of series" cannot be recovered from it.

   Primary source used here: a full sweep of GET /markets with
   status=settled + min_close_ts/max_close_ts + cursor paging to exhaustion,
   grouped by each market's own `series_ticker` field (falling back to the
   event-ticker prefix when that field is absent).

   Why this is complete for the question being asked: the question is "which
   series have >= --min-settled settled markets in the window". A series with
   even one settled market in the window has a row in that listing, because the
   listing IS the set of settled markets in the window. There is no series-level
   catalog to be missing from -- the series set is derived from the market set,
   not looked up. So the only way to miss a series is to truncate the sweep,
   which is why the sweep pages until the API stops returning a cursor, records
   its cursor chain in the cache so an interrupted sweep resumes exactly where
   it stopped, and refuses to silently accept a repeated cursor.

   One deliberate exclusion, and it is large: --exclude-series-regex defaults to
   ^KXMVE, which drops KXMVECROSSCATEGORY. Measured live 2026-08, that single
   multivariate-parlay series is ~61,000 settled markets PER DAY -- about 99% of
   the exchange by market count and ~300MB of cached JSON per day, which makes a
   30-day scan impossible on a normal box. Those markets are still PAGED and
   COUNTED (so the census stays truthful and the summary prints what was
   skipped), just not stored or analysed. They are also combinatorial baskets
   built from other markets rather than independent prices, so their calibration
   is not really a separate question. Pass --exclude-series-regex '' to include
   them and budget accordingly.

   Cross-check (reporting only, never a filter): GET /series returns Kalshi's
   own series catalog. The scan prints catalog size, how many catalog series the
   sweep saw, and how many it did not. Catalog-only series are expected and fine
   (a series with no settled market in the window has nothing to study); a
   catalog much larger than the sweep in an unexplained way is your signal that
   the sweep truncated.

2. PER-SERIES ANALYSIS (identical in method to market_calibration_study.py --
   the price, fee, bucket and EV helpers are IMPORTED from it rather than
   reimplemented, so the two studies cannot drift apart)
   For each series with >= --min-settled settled markets: pull candlesticks
   around each market's close, sample the YES price at fixed minutes before
   close (--slices, default 10/5/2), bucket into 5-cent buckets, and compare the
   realized settle rate to the implied probability. EV per contract is reported
   after the Kalshi taker fee, ceil(100 * 0.07 * P * (1-P)) / 100 per contract,
   for BOTH sides (they differ only through the payout, since P*(1-P) is
   symmetric so the fee is identical for YES at P and NO at 1-P).

3. STATISTICS -- and the correlation trap
   The null is not "a fair coin at the bucket midpoint": each observation has
   its own sampled price p_i, so the null is Poisson-binomial over {p_i}.
   Two p-values are computed per cell:
     naive  -- treats observations as independent. Exact Poisson-binomial DP
               for n <= --exact-max, normal approximation with continuity
               correction above it.
     robust -- a cluster-robust score test, S = sum of (y_i - p_i), with
               variance from squared cluster totals (x C/(C-1)), clustering by
               event (or by day with --cluster day).
   The robust one is what gets reported and FDR-corrected, because the naive one
   is simply wrong here: strikes inside one event share an underlying, so their
   errors are correlated and independence inflates significance. The ratio
   between the two is printed as a design-effect diagnostic -- if it is large,
   any single-series study that used the naive test was fooling itself.

4. LIQUIDITY
   Historical resting depth is NOT recoverable: candlesticks carry yes_bid /
   yes_ask prices but no sizes, and a settled market's orderbook is empty. So
   two different things are reported, labelled as such and never conflated:
     * the candle volume/open-interest fields for the sampled period -- free,
       historical, and already cached. NOTE these come back non-integer
       (96904.06), so they are NOT contract counts; treat them as a relative
       activity measure, not as size you can lift.
     * median size resting at the touch -- a LIVE snapshot (--probe-liquidity),
       taken from currently-open markets of the flagged series at a comparable
       time-to-close. It answers "is this tradeable at size today", not "what
       was resting 3 weeks ago". n_books is printed so 2 samples never read as
       an established fact.

5. CACHING / COST
   Every raw response (market pages, candles, series catalog, probed books) is
   cached verbatim in sqlite, so a rerun costs zero network calls and you can
   iterate on the ANALYSIS without re-paying for the FETCH. Reads are token-
   bucketed at cfg.rate_limit_reads, sleep --sleep between markets, and retry
   429 with exponential backoff -- same policy as market_calibration_study.py
   and pull_history.py.

   The window end is snapped to the UTC day (--align) so repeated runs share one
   cache key. Without that, time.time() would make every run a new window and
   silently re-page the entire exchange, which is the expensive half.

   A full uncached exchange-wide scan is LARGE. Start with --enumerate-only
   (one cheap sweep, no candles) to see the landscape, then scan. Budget caps
   (--max-markets-per-series, --max-candle-calls) are honoured, and anything
   they drop is printed explicitly -- a truncated scan must never read like a
   complete one.

Usage:
    python scripts/scan_all_series.py --days 30 --enumerate-only
    python scripts/scan_all_series.py --days 30
    python scripts/scan_all_series.py --days 60 --min-settled 200 --probe-liquidity
    python scripts/scan_all_series.py --days 30 --csv out/scan.csv --json out/scan.json

CAVEATS -- read before trusting a number out of this
  - Settlement comes from Kalshi's own record only (`result`, else
    `expiration_value` vs the market's strike, via
    pull_history.resolved_yes_from_market). No spot fallback: this study is
    about the exchange's pricing vs its own settlement.
  - Mid is not a fill. EV assumes you buy AT the sampled price; crossing a real
    spread is worse. Every EV here is an upper bound.
  - Fixed minute slices mean different things across series. T-10min of a 15-
    minute crypto market is 2/3 of its life; T-10min of a monthly market is the
    last 0.02% of it. Compare within a series, not across.
  - Survivorship: a market with no two-sided quote and no trade contributes no
    sample. Thin markets are under-represented; coverage is printed per slice.
  - Multi-strike series structurally settle mostly NO (one strike wins per
    event). That is not miscalibration; the prices know it. Watch the diff, not
    the settle rate.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kbot.auth import kalshi_headers, load_private_key  # noqa: E402
from kbot.config import settings  # noqa: E402

# The price / fee / bucket semantics are IMPORTED, never re-derived: "the same
# analysis market_calibration_study.py does" has to mean literally the same
# code, or the two studies quietly diverge on the details that matter (what an
# all-zero bid means, whether the fee is rounded up, where a bucket edge sits).
from market_calibration_study import (  # noqa: E402
    _BUCKET_WIDTH,
    _N_BUCKETS,
    bucket_index,
    bucket_label,
    bucket_midpoint,
    candle_at_or_before,
    fee_per_contract,
    parse_iso_ts,
    sort_candles,
    yes_price_from_candle,
)
from pull_history import resolved_yes_from_market  # noqa: E402

_DEFAULT_SLICES = [10, 5, 2]
_MARKETS_PER_PAGE = 1000          # API max for GET /markets
_SWEEP_MAX_PAGES = 100_000        # runaway guard only; the cursor normally ends it

_CANDLES_MAX_ATTEMPTS = 5
_CANDLES_INITIAL_BACKOFF_SECONDS = 2.0
_CANDLES_MAX_BACKOFF_SECONDS = 30.0

_PROGRESS_PRINT_EVERY = 250
_SWEEP_PROGRESS_EVERY = 25

# Measured live against external-api.kalshi.com, 2026-08: ~1.4 markets/second at
# --sleep 0.05, i.e. round-trip latency (~0.6s) dominates, NOT the sleep and not
# the client-side token bucket. Used only to give --enumerate-only an estimate
# that is not a fantasy; lowering --sleep will not make a big scan much faster.
_SECONDS_PER_CANDLE_FETCH = 0.6

# Candidate values for the /markets status filter, tried in order. The codebase
# already documents that settled markets come back as status "finalized" from
# some endpoints (see KalshiClient.get_market), so hardcoding one spelling is a
# known way to get a confidently empty result set.
_STATUS_CANDIDATES = ["settled", "finalized", None]


# ── Read-only HTTP layer ─────────────────────────────────────────────────────

_ALLOWED_PATHS = (
    re.compile(r"^/markets$"),
    re.compile(r"^/markets/[^/]+$"),
    re.compile(r"^/markets/[^/]+/orderbook$"),
    re.compile(r"^/series$"),
    re.compile(r"^/series/[^/]+/markets/[^/]+/candlesticks$"),
)


class _TokenBucket:
    """Single-threaded token bucket; mirrors KalshiClient's read limiter."""

    def __init__(self, rate: float) -> None:
        self._rate = max(1.0, float(rate))
        self._tokens = self._rate
        self._last = time.monotonic()

    def acquire(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens < 1.0:
            time.sleep((1.0 - self._tokens) / self._rate)
            self._tokens = 0.0
        else:
            self._tokens -= 1.0


class _ReadOnlyAPI:
    """
    Signed GET-only Kalshi client with a hard path allowlist.

    Deliberately NOT KalshiClient: that class owns place_order() and
    cancel_order(), and this script must not have such a method in scope at all.
    Raw httpx (rather than the SDK) is also required for correctness, not just
    purity -- the SDK Market model rejects the status values settled markets
    actually return, and its Candlestick model silently deserializes the real
    response to all-None. Both quirks are documented in kbot/kalshi_client.py.

    Requests are counted so the run can report exactly what it cost.
    """

    def __init__(self, cfg=settings, timeout: float = 30.0) -> None:
        self._cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._prefix = httpx.URL(cfg.base_url).path.rstrip("/")
        self._bucket = _TokenBucket(getattr(cfg, "rate_limit_reads", 20))
        self._http = httpx.Client(timeout=timeout)
        self.request_count = 0

        if not cfg.kalshi_key_id or not cfg.private_key_path.exists():
            raise SystemExit(
                "Kalshi credentials required (candlesticks are an authenticated read). "
                f"key_id set={bool(cfg.kalshi_key_id)}, key file {cfg.private_key_path} "
                f"exists={cfg.private_key_path.exists()}"
            )
        self._key_id = cfg.kalshi_key_id
        self._private_key = load_private_key(cfg.private_key_path)

    def get(self, path: str, params: dict | None = None) -> dict:
        """One signed GET against an allowlisted read path. Raises on HTTP error."""
        if not any(rx.match(path) for rx in _ALLOWED_PATHS):
            raise ValueError(
                f"refusing {path!r}: scan_all_series.py is read-only and only calls "
                f"{[rx.pattern for rx in _ALLOWED_PATHS]}"
            )
        self._bucket.acquire()
        headers = kalshi_headers("GET", self._prefix + path, self._key_id, self._private_key)
        resp = self._http.get(self._base + path, headers=headers, params=params or {})
        self.request_count += 1
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._http.close()


# ── Cache ────────────────────────────────────────────────────────────────────

class ScanCache:
    """
    Local sqlite cache of raw API responses.

    Raw JSON is stored verbatim so re-analysis never means refetching. Two
    derived columns (series, close_ts) and one precomputed one (settled_yes) are
    stored alongside it purely so the "which series have enough settled markets"
    question is a SQL aggregate -- an exchange-wide sweep is hundreds of
    thousands of markets and must never all be deserialized into memory at once.

    Sweep progress (the cursor chain) is persisted too: the market sweep is the
    long pole, and an interrupted run has to resume rather than restart.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS scan_markets (
                ticker      TEXT PRIMARY KEY,
                series      TEXT NOT NULL,
                event       TEXT,
                close_ts    INTEGER,
                settled_yes INTEGER,          -- 1 / 0 / NULL(unresolved)
                raw_json    TEXT NOT NULL,
                fetched_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scan_markets_series_close
                ON scan_markets (series, close_ts);
            CREATE INDEX IF NOT EXISTS idx_scan_markets_close
                ON scan_markets (close_ts);

            CREATE TABLE IF NOT EXISTS scan_candles (
                ticker          TEXT NOT NULL,
                start_ts        INTEGER NOT NULL,
                end_ts          INTEGER NOT NULL,
                period_interval INTEGER NOT NULL,
                raw_json        TEXT NOT NULL,
                fetched_at      INTEGER NOT NULL,
                PRIMARY KEY (ticker, start_ts, end_ts, period_interval)
            );

            CREATE TABLE IF NOT EXISTS scan_excluded (
                series      TEXT PRIMARY KEY,
                n_markets   INTEGER NOT NULL,
                n_settled   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_sweep (
                window_key  TEXT PRIMARY KEY,
                cursor      TEXT,
                pages       INTEGER NOT NULL,
                complete    INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_series_catalog (
                ticker     TEXT PRIMARY KEY,
                raw_json   TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_books (
                ticker     TEXT NOT NULL,
                probed_at  INTEGER NOT NULL,
                raw_json   TEXT NOT NULL,
                PRIMARY KEY (ticker, probed_at)
            );
            """
        )
        self._db.commit()

    # -- markets ------------------------------------------------------------
    def put_markets(self, rows: list[tuple]) -> None:
        """rows: (ticker, series, event, close_ts, settled_yes, raw_json, fetched_at)"""
        self._db.executemany(
            "INSERT OR REPLACE INTO scan_markets "
            "(ticker, series, event, close_ts, settled_yes, raw_json, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        self._db.commit()

    def bump_excluded(self, counts: dict[str, tuple[int, int]]) -> None:
        """Accumulate (markets, settled) for series skipped at sweep time."""
        self._db.executemany(
            "INSERT INTO scan_excluded (series, n_markets, n_settled) VALUES (?,?,?) "
            "ON CONFLICT(series) DO UPDATE SET n_markets = n_markets + excluded.n_markets, "
            "n_settled = n_settled + excluded.n_settled",
            [(series, total, settled) for series, (total, settled) in counts.items()],
        )
        self._db.commit()

    def excluded_counts(self) -> dict[str, tuple[int, int]]:
        rows = self._db.execute(
            "SELECT series, n_markets, n_settled FROM scan_excluded ORDER BY n_settled DESC"
        ).fetchall()
        return {series: (total, settled) for series, total, settled in rows}

    def settled_counts_by_series(self, start_ts: int, end_ts: int) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT series, COUNT(*) FROM scan_markets "
            "WHERE close_ts BETWEEN ? AND ? AND settled_yes IS NOT NULL "
            "GROUP BY series",
            (start_ts, end_ts),
        ).fetchall()
        return {series: count for series, count in rows}

    def unresolved_count(self, start_ts: int, end_ts: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM scan_markets "
            "WHERE close_ts BETWEEN ? AND ? AND settled_yes IS NULL",
            (start_ts, end_ts),
        ).fetchone()
        return int(row[0]) if row else 0

    def total_markets(self, start_ts: int, end_ts: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM scan_markets WHERE close_ts BETWEEN ? AND ?",
            (start_ts, end_ts),
        ).fetchone()
        return int(row[0]) if row else 0

    def settled_markets_for_series(self, series: str, start_ts: int, end_ts: int) -> list[tuple]:
        """[(ticker, event, close_ts, settled_yes, raw_dict)] ordered by close_ts."""
        rows = self._db.execute(
            "SELECT ticker, event, close_ts, settled_yes, raw_json FROM scan_markets "
            "WHERE series = ? AND close_ts BETWEEN ? AND ? AND settled_yes IS NOT NULL "
            "ORDER BY close_ts ASC",
            (series, start_ts, end_ts),
        ).fetchall()
        return [(t, e, c, bool(s), json.loads(raw)) for t, e, c, s, raw in rows]

    # -- candles ------------------------------------------------------------
    def get_candles(self, ticker: str, start_ts: int, end_ts: int, period: int) -> list[dict] | None:
        row = self._db.execute(
            "SELECT raw_json FROM scan_candles "
            "WHERE ticker=? AND start_ts=? AND end_ts=? AND period_interval=?",
            (ticker, start_ts, end_ts, period),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put_candles(self, ticker: str, start_ts: int, end_ts: int, period: int,
                    candles: list[dict]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO scan_candles "
            "(ticker, start_ts, end_ts, period_interval, raw_json, fetched_at) VALUES (?,?,?,?,?,?)",
            (ticker, start_ts, end_ts, period, json.dumps(candles), int(time.time())),
        )
        self._db.commit()

    # -- sweep progress -----------------------------------------------------
    def get_sweep(self, window_key: str) -> tuple[str | None, int, bool]:
        row = self._db.execute(
            "SELECT cursor, pages, complete FROM scan_sweep WHERE window_key = ?",
            (window_key,),
        ).fetchone()
        if not row:
            return None, 0, False
        return row[0], int(row[1]), bool(row[2])

    def put_sweep(self, window_key: str, cursor: str | None, pages: int, complete: bool) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO scan_sweep (window_key, cursor, pages, complete, updated_at) "
            "VALUES (?,?,?,?,?)",
            (window_key, cursor, pages, int(complete), int(time.time())),
        )
        self._db.commit()

    def clear_sweep(self, window_key: str) -> None:
        self._db.execute("DELETE FROM scan_sweep WHERE window_key = ?", (window_key,))
        self._db.commit()

    # -- series catalog -----------------------------------------------------
    def get_catalog(self) -> list[dict]:
        rows = self._db.execute("SELECT raw_json FROM scan_series_catalog").fetchall()
        return [json.loads(r[0]) for r in rows]

    def put_catalog(self, series_records: list[dict]) -> None:
        now = int(time.time())
        self._db.executemany(
            "INSERT OR REPLACE INTO scan_series_catalog (ticker, raw_json, fetched_at) VALUES (?,?,?)",
            [(s.get("ticker") or "", json.dumps(s), now) for s in series_records if s.get("ticker")],
        )
        self._db.commit()

    # -- probed books -------------------------------------------------------
    def put_book(self, ticker: str, probed_at: int, book: dict) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO scan_books (ticker, probed_at, raw_json) VALUES (?,?,?)",
            (ticker, probed_at, json.dumps(book)),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


# ── Enumeration ──────────────────────────────────────────────────────────────

def series_of(market: dict) -> str:
    """
    Series ticker for a market record.

    Prefers the API field; falls back to the event-ticker prefix, which is the
    documented Kalshi convention (event_ticker = SERIES-DATECODE...). Returns ""
    when neither is available, and those markets are counted and reported rather
    than silently bucketed under an empty series.
    """
    explicit = market.get("series_ticker")
    if explicit:
        return str(explicit)
    stem = market.get("event_ticker") or market.get("ticker") or ""
    return str(stem).split("-")[0] if stem else ""


def fetch_series_catalog(api: _ReadOnlyAPI, cache: ScanCache, *, refresh: bool) -> list[dict]:
    """
    Kalshi's own series catalog via GET /series.

    Used ONLY as a cross-check on the market sweep (see this module's
    docstring); it never filters what gets scanned, because a catalog entry says
    nothing about whether that series has settled markets in our window. Failure
    here is non-fatal -- losing the cross-check must not lose the scan.
    """
    if not refresh:
        cached = cache.get_catalog()
        if cached:
            return cached
    try:
        data = api.get("/series")
    except Exception as exc:  # noqa: BLE001 -- cross-check only
        print(f"  WARNING: GET /series failed ({type(exc).__name__}: {str(exc)[:120]}); "
              f"continuing without the catalog cross-check.")
        return []
    records = data.get("series") or []
    cache.put_catalog(records)
    return records


def _probe_status_filter(api: _ReadOnlyAPI, start_ts: int, end_ts: int) -> str | None:
    """
    Pick the status spelling GET /markets actually honours for settled markets.

    Tries "settled", then "finalized", then no filter at all. Necessary because
    the exchange returns "finalized" for settled markets on some routes (see
    KalshiClient.get_market) and a wrong-but-accepted status value yields an
    empty result set with a 200 -- a silent zero, the worst possible failure for
    a census.
    """
    for status in _STATUS_CANDIDATES:
        params = {"limit": 10, "min_close_ts": start_ts, "max_close_ts": end_ts}
        if status:
            params["status"] = status
        try:
            data = api.get("/markets", params)
        except httpx.HTTPStatusError as exc:
            print(f"  status={status!r} rejected (HTTP {exc.response.status_code}); trying next.")
            continue
        markets = data.get("markets") or []
        if markets:
            label = status or "(no status filter -- settlement decided client-side)"
            print(f"  status filter: {label} -- first page returned {len(markets)} markets.")
            return status
        print(f"  status={status!r} accepted but returned 0 markets; trying next.")
    raise SystemExit(
        "GET /markets returned no markets for this window under any status filter. "
        "Check credentials and that --days covers a period with settled markets."
    )


def sweep_settled_markets(
    api: _ReadOnlyAPI,
    cache: ScanCache,
    start_ts: int,
    end_ts: int,
    *,
    refresh: bool,
    page_limit: int,
    exclude_rx: re.Pattern | None,
) -> dict:
    """
    Page GET /markets to exhaustion over [start_ts, end_ts] and cache every
    market, tagged with its series and its settled outcome.

    `exclude_rx` series are still PAGED and COUNTED (the census stays honest and
    the counts are reported) but are not stored, because one of them will
    otherwise eat the machine: KXMVECROSSCATEGORY, the multivariate parlay
    product, ships ~61k settled markets PER DAY -- 99% of the exchange by market
    count and ~300MB of cached JSON per day. It is also the least interesting
    thing here: those are combinatorial baskets of other markets, not an
    independent opinion whose calibration means anything on its own.

    This is the census the whole scan rests on, so it is deliberately paranoid:
      * the cursor chain is persisted after every page, so an interrupted sweep
        resumes at the exact page it died on instead of restarting;
      * a repeated cursor aborts the sweep loudly (an API that loops its cursor
        would otherwise spin forever, or worse, look complete);
      * completion is recorded only when the API itself stops handing out a
        cursor. Anything else leaves the sweep marked incomplete, and the
        summary says so.

    Returns a dict of sweep statistics for the report.
    """
    # The exclusion pattern is part of the cache key: widening it later must not
    # be served by a sweep that never fetched those series in the first place.
    window_key = f"{start_ts}:{end_ts}:{exclude_rx.pattern if exclude_rx else ''}:v1"
    if refresh:
        cache.clear_sweep(window_key)

    cursor, pages_done, complete = cache.get_sweep(window_key)
    if complete and not refresh:
        print(f"  sweep already complete in cache ({pages_done} pages); no network calls. "
              f"Use --refresh to redo it.")
        return {"pages": pages_done, "complete": True, "from_cache": True,
                "new_markets": 0, "status_filter": "cached", "no_series": 0}

    if pages_done:
        print(f"  resuming interrupted sweep at page {pages_done + 1} "
              f"(cursor {'present' if cursor else 'absent'}).")

    status = _probe_status_filter(api, start_ts, end_ts)
    seen_cursors: set[str] = set()
    total_new = 0
    no_series = 0
    kept_settled = 0

    while pages_done < _SWEEP_MAX_PAGES:
        params = {"limit": page_limit, "min_close_ts": start_ts, "max_close_ts": end_ts}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor

        data = api.get("/markets", params)
        page = data.get("markets") or []
        pages_done += 1

        rows = []
        excluded_page: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        now = int(time.time())
        for market in page:
            ticker = market.get("ticker")
            if not ticker:
                continue
            close_ts = parse_iso_ts(market.get("close_time"))
            if close_ts is None or not (start_ts <= close_ts <= end_ts):
                # The API filter should make this impossible; belt-and-braces so
                # a server-side filter change cannot widen the window silently.
                continue
            series = series_of(market)
            if not series:
                no_series += 1
                continue
            if exclude_rx is not None and exclude_rx.search(series):
                bucket = excluded_page[series]
                bucket[0] += 1
                bucket[1] += int(resolved_yes_from_market(market) is not None)
                continue
            settled = resolved_yes_from_market(market)
            if settled is not None:
                kept_settled += 1
            rows.append((ticker, series, market.get("event_ticker"), close_ts,
                         None if settled is None else int(settled),
                         json.dumps(market), now))

        if rows:
            cache.put_markets(rows)
            total_new += len(rows)
        if excluded_page:
            cache.bump_excluded({s: (v[0], v[1]) for s, v in excluded_page.items()})

        next_cursor = data.get("cursor") or None
        if next_cursor and next_cursor in seen_cursors:
            print(f"  WARNING: cursor repeated at page {pages_done} -- aborting sweep as "
                  f"INCOMPLETE rather than looping.")
            cache.put_sweep(window_key, None, pages_done, False)
            return {"pages": pages_done, "complete": False, "from_cache": False,
                    "new_markets": total_new, "status_filter": status, "no_series": no_series,
                    "abort_reason": "repeated cursor"}
        if next_cursor:
            seen_cursors.add(next_cursor)

        finished = not next_cursor or not page
        cache.put_sweep(window_key, next_cursor, pages_done, finished)
        cursor = next_cursor

        if pages_done % _SWEEP_PROGRESS_EVERY == 0:
            print(f"  page {pages_done}: {total_new} markets cached "
                  f"({kept_settled} with a settled outcome)...")

        if finished:
            print(f"  sweep complete: {pages_done} pages, {total_new} markets cached, "
                  f"{kept_settled} settled.")
            return {"pages": pages_done, "complete": True, "from_cache": False,
                    "new_markets": total_new, "status_filter": status, "no_series": no_series}

    print(f"  WARNING: hit the {_SWEEP_MAX_PAGES}-page runaway guard -- sweep INCOMPLETE.")
    cache.put_sweep(window_key, cursor, pages_done, False)
    return {"pages": pages_done, "complete": False, "from_cache": False,
            "new_markets": total_new, "status_filter": status, "no_series": no_series,
            "abort_reason": "page guard"}


def enumerate_named_series(
    api: _ReadOnlyAPI,
    cache: ScanCache,
    series_list: list[str],
    start_ts: int,
    end_ts: int,
    *,
    refresh: bool,
    page_limit: int,
) -> dict:
    """
    Targeted enumeration of specific series via GET /markets?series_ticker=...

    The fast path for --series: once the census has told you which series are
    worth a look, re-scanning one of them should not mean paging the entire
    exchange again. This is NOT a census and is labelled as such in the output --
    it can only find what you already named.
    """
    pages_total = 0
    stored = 0
    incomplete: list[str] = []
    status: str | None = None
    status_probed = False

    for series in series_list:
        window_key = f"{start_ts}:{end_ts}:series={series}:v1"
        if refresh:
            cache.clear_sweep(window_key)
        cursor, pages_done, complete = cache.get_sweep(window_key)
        if complete and not refresh:
            print(f"  [{series}] already enumerated in cache ({pages_done} pages).")
            pages_total += pages_done
            continue

        # Probed lazily, and only once: a fully cached rerun should cost exactly
        # zero network calls, which is the whole promise of the cache.
        if not status_probed:
            status = _probe_status_filter(api, start_ts, end_ts)
            status_probed = True

        seen_cursors: set[str] = set()
        while True:
            params = {"limit": page_limit, "min_close_ts": start_ts, "max_close_ts": end_ts,
                      "series_ticker": series}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            data = api.get("/markets", params)
            page = data.get("markets") or []
            pages_done += 1
            pages_total += 1

            rows = []
            now = int(time.time())
            for market in page:
                ticker = market.get("ticker")
                close_ts = parse_iso_ts(market.get("close_time"))
                if not ticker or close_ts is None or not (start_ts <= close_ts <= end_ts):
                    continue
                settled = resolved_yes_from_market(market)
                rows.append((ticker, series_of(market) or series, market.get("event_ticker"),
                             close_ts, None if settled is None else int(settled),
                             json.dumps(market), now))
            if rows:
                cache.put_markets(rows)
                stored += len(rows)

            next_cursor = data.get("cursor") or None
            if next_cursor and next_cursor in seen_cursors:
                print(f"  [{series}] WARNING: cursor repeated -- marking INCOMPLETE.")
                incomplete.append(series)
                cache.put_sweep(window_key, None, pages_done, False)
                break
            if next_cursor:
                seen_cursors.add(next_cursor)
            finished = not next_cursor or not page
            cache.put_sweep(window_key, next_cursor, pages_done, finished)
            cursor = next_cursor
            if finished:
                print(f"  [{series}] {pages_done} pages enumerated.")
                break

    return {"pages": pages_total, "complete": not incomplete,
            "from_cache": not status_probed,
            "new_markets": stored, "status_filter": status or "cached", "no_series": 0,
            "targeted": True,
            "abort_reason": f"incomplete for {incomplete}" if incomplete else None}


# ── Candles ──────────────────────────────────────────────────────────────────

def choose_period_interval(max_slice_minutes: int) -> int:
    """
    Candle granularity for the widest slice requested.

    1-minute candles are what makes a "T-5min price" mean anything, but the API
    caps a response at 5000 candles, so a slice measured in days has to drop to
    hourly. The cost is staleness, which is measured and reported per sample
    rather than assumed away.
    """
    span_minutes = max_slice_minutes + 5
    if span_minutes <= 4000:
        return 1
    if span_minutes <= 4000 * 60:
        return 60
    return 1440


def fetch_candles_cached(
    api: _ReadOnlyAPI,
    cache: ScanCache,
    series: str,
    ticker: str,
    start_ts: int,
    end_ts: int,
    period: int,
    *,
    refresh: bool,
    sleep_seconds: float,
) -> tuple[list[dict], bool]:
    """
    (candles, made_network_call) for one market, cached.

    Empty results are cached: "this market never had candles" is a stable fact
    about a settled market, and re-asking every rerun would spend the entire
    rate-limit budget on exactly the markets with no data. 429s back off
    exponentially and, if still limited after all attempts, return empty WITHOUT
    caching so a later rerun picks the market up. Same policy as
    market_calibration_study.py.
    """
    if not refresh:
        cached = cache.get_candles(ticker, start_ts, end_ts, period)
        if cached is not None:
            return cached, False

    path = f"/series/{series}/markets/{ticker}/candlesticks"
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period}
    backoff = _CANDLES_INITIAL_BACKOFF_SECONDS

    for attempt in range(1, _CANDLES_MAX_ATTEMPTS + 1):
        try:
            candles = (api.get(path, params) or {}).get("candlesticks", [])
            cache.put_candles(ticker, start_ts, end_ts, period, candles)
            if sleep_seconds:
                time.sleep(sleep_seconds)
            return candles, True
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code != 429:
                # 404s are normal here (a series whose candles route differs, a
                # market too old to have them); cache the empty answer so the
                # next run does not pay for it again.
                cache.put_candles(ticker, start_ts, end_ts, period, [])
                return [], True
            if attempt == _CANDLES_MAX_ATTEMPTS:
                print(f"  WARNING: {ticker} still rate-limited after {_CANDLES_MAX_ATTEMPTS} "
                      f"attempts -- skipping (not cached; rerun will retry).")
                return [], True
            time.sleep(backoff)
            backoff = min(backoff * 2, _CANDLES_MAX_BACKOFF_SECONDS)
        except Exception as exc:  # noqa: BLE001 -- one bad market must not kill the scan
            print(f"  WARNING: {ticker} candles failed "
                  f"({type(exc).__name__}: {str(exc)[:100]}) -- skipping.")
            return [], True
    return [], True


# ── Sampling ─────────────────────────────────────────────────────────────────

def _fixed_point(raw: object) -> float | None:
    """Kalshi fixed-point string ("10.00") to float, or None."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Observation:
    """One (market, time-slice) price sample paired with its settled outcome."""

    series: str
    ticker: str
    event: str
    close_ts: int
    slice_minutes: int
    yes_price: float
    source: str            # "mid" | "trade"
    settled_yes: bool
    staleness_s: int       # target_ts - end_period_ts of the candle actually used
    # Raw candle fields for the sampled period. Confirmed live 2026-08: both come
    # back non-integer, so neither is a contract count -- volume_fp fluctuates
    # period to period (a per-period figure) while open_interest_fp rises
    # monotonically (cumulative). Useful as relative activity, never as depth.
    volume_fp: float
    open_interest_fp: float


def sample_market(
    series: str,
    ticker: str,
    event: str,
    close_ts: int,
    candles: list[dict],
    settled_yes: bool,
    slices: list[int],
    max_staleness_s: int,
) -> tuple[list[Observation], dict[str, int]]:
    """
    One Observation per slice that has a usable, causal, fresh-enough price.

    Staleness is the reason this returns rejection counts rather than just
    silently skipping: the candle used for T-5min is "the latest one ending at
    or before T-5min", which for a market that stopped quoting hours earlier can
    be an ancient price wearing a T-5min label. That failure mode produces a
    beautiful fake edge (stale price vs known-ish outcome), so it is measured,
    capped, and reported.
    """
    ordered = sort_candles(candles)
    observations: list[Observation] = []
    rejects = {"no_candle": 0, "no_quote": 0, "stale": 0}

    for minutes in slices:
        target = close_ts - minutes * 60
        candle = candle_at_or_before(ordered, target)
        if candle is None:
            rejects["no_candle"] += 1
            continue
        staleness = int(target - (candle.get("end_period_ts") or 0))
        if staleness > max_staleness_s:
            rejects["stale"] += 1
            continue
        price, source = yes_price_from_candle(candle)
        if price is None:
            rejects["no_quote"] += 1
            continue
        observations.append(Observation(
            series=series,
            ticker=ticker,
            event=event or ticker,
            close_ts=close_ts,
            slice_minutes=minutes,
            yes_price=price,
            source=source,
            settled_yes=settled_yes,
            staleness_s=max(0, staleness),
            volume_fp=_fixed_point(candle.get("volume_fp")) or 0.0,
            open_interest_fp=_fixed_point(candle.get("open_interest_fp")) or 0.0,
        ))
    return observations, rejects


# ── Statistics ───────────────────────────────────────────────────────────────

def normal_sf(z: float) -> float:
    """P(Z > z) for a standard normal, via erfc (no scipy needed, exact enough)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def poisson_binomial_pmf(ps: list[float]) -> list[float]:
    """
    Full distribution of the number of successes for independent Bernoullis with
    DIFFERENT probabilities -- the correct null here, since every observation
    carries its own market price. O(n^2), so callers cap n.
    """
    dp = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dp) + 1)
        for i, v in enumerate(dp):
            if v == 0.0:
                continue
            nxt[i] += v * (1.0 - p)
            nxt[i + 1] += v * p
        dp = nxt
    return dp


def naive_pvalue(ps: list[float], successes: int, exact_max: int) -> float:
    """
    Two-sided p for "were there really `successes` YES settlements?" ASSUMING
    INDEPENDENCE -- exact Poisson-binomial when small, normal approximation with
    continuity correction when large.

    Reported only as a foil to the cluster-robust p. Independence is false here
    (strikes within an event share an underlying), so this number is optimistic
    by construction and exists to show by how much.
    """
    n = len(ps)
    if n == 0:
        return 1.0
    if n <= exact_max:
        pmf = poisson_binomial_pmf(ps)
        lower = sum(pmf[: successes + 1])
        upper = sum(pmf[successes:])
        return min(1.0, 2.0 * min(lower, upper))

    mu = sum(ps)
    var = sum(p * (1.0 - p) for p in ps)
    if var <= 0.0:
        return 1.0
    sd = math.sqrt(var)
    if successes > mu:
        z = (successes - 0.5 - mu) / sd
    elif successes < mu:
        z = (successes + 0.5 - mu) / sd
    else:
        return 1.0
    return min(1.0, 2.0 * normal_sf(abs(z)))


def cluster_robust_test(
    residuals_by_cluster: dict[str, list[float]],
    direction: int = 0,
    floor_variance: float = 0.0,
) -> tuple[float, float, float]:
    """
    (z, p, se) for H0: mean(y_i - p_i) == 0, robust to arbitrary correlation
    WITHIN clusters.

    S = sum of all residuals; Var(S) is estimated as the sum of squared
    per-cluster residual totals, scaled by C/(C-1) for the usual small-C
    correction. This is the standard cluster-robust score test, and it is the
    honest one for this data: two strikes of the same BTC event are not two
    independent draws, and treating them as such is how a scan of this size
    manufactures findings.

    direction: 0 two-sided, +1 one-sided "realized above implied", -1 below.
    Used for the confirmation half, where the direction is already pinned by
    discovery and a two-sided test would waste half the alpha.

    floor_variance: the model variance under the null, sum of p_i(1-p_i). The
    empirical estimate is floored at it, and that floor is not a nicety -- it is
    load-bearing. In an extreme bucket where nothing happens (30 samples priced
    at 0.99 that all settle YES), every residual is the same tiny number, the
    empirical variance collapses to a fraction of the truth, and the test
    reports p ~ 1e-4 for what is really a perfectly calibrated bucket. Observed
    live on KXETH15M before this floor existed: design effect 0.14, two
    "confirmed" findings worth +0.09 cents each. Clustering can only ADD
    variance to the independent null, so any estimate BELOW it is sampling
    noise, and taking the max is both principled and conservative.
    """
    totals = [sum(v) for v in residuals_by_cluster.values()]
    n_clusters = len(totals)
    if n_clusters < 2:
        return 0.0, 1.0, float("inf")

    S = sum(totals)
    var = sum(t * t for t in totals) * (n_clusters / (n_clusters - 1.0))
    var = max(var, floor_variance)
    if var <= 0.0:
        return 0.0, 1.0, float("inf")

    se = math.sqrt(var)
    z = S / se
    if direction > 0:
        p = normal_sf(z)
    elif direction < 0:
        p = normal_sf(-z)
    else:
        p = min(1.0, 2.0 * normal_sf(abs(z)))
    return z, p, se


def benjamini_hochberg(pvalues: list[float], q: float) -> tuple[list[float], list[bool]]:
    """
    (adjusted p-values, pass/fail at level q) under Benjamini-Hochberg.

    Mandatory here: this scan runs hundreds to thousands of tests, so at
    alpha=0.05 roughly one in twenty cells is "significant" with no edge at all.
    BH controls the expected share of FALSE discoveries among the ones reported,
    which is exactly the question being asked ("of the buckets you are showing
    me, how many are noise?").
    """
    m = len(pvalues)
    if m == 0:
        return [], []
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [1.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        idx = order[rank - 1]
        running_min = min(running_min, pvalues[idx] * m / rank)
        adjusted[idx] = min(1.0, running_min)
    return adjusted, [adjusted[i] <= q for i in range(m)]


# ── Cell construction ────────────────────────────────────────────────────────

# Order size inside the order-level fee ceiling. Matches the C = 10 that
# KILL_CRITERIA.md section 2 pre-commits for Thread A, so the two scans price
# fees identically. Thread C (section 4) additionally requires reading
# fee_type/fee_multiplier per series -- not yet wired in here.
_CONTRACTS_PER_ORDER = 10


def side_ev(observations: list[Observation], side: str) -> float:
    """
    Mean after-fee P&L per contract from buying `side` at the sampled price and
    holding to settlement. Fee is the order-level ceiling at
    _CONTRACTS_PER_ORDER contracts divided by that count, exactly as the
    exchange charges it (imported from market_calibration_study).

    Note fee(P) == fee(1-P), so the two sides differ only in payout -- which is
    why a bucket can never have positive EV on both sides.
    """
    if not observations:
        return 0.0
    total = 0.0
    for o in observations:
        entry = o.yes_price if side == "YES" else 1.0 - o.yes_price
        won = o.settled_yes if side == "YES" else not o.settled_yes
        total += (1.0 if won else 0.0) - entry - fee_per_contract(entry, _CONTRACTS_PER_ORDER)
    return total / len(observations)


def cluster_key(obs: Observation, mode: str) -> str:
    """Cluster label for the robust variance: the event, or the UTC day."""
    if mode == "day":
        return f"{obs.series}:{datetime.fromtimestamp(obs.close_ts, tz=timezone.utc):%Y-%m-%d}"
    if mode == "none":
        return f"{obs.ticker}:{obs.slice_minutes}"
    return obs.event


def build_cell(
    observations: list[Observation],
    cluster_mode: str,
    exact_max: int,
    direction: int = 0,
) -> dict:
    """
    All the statistics for one (series, slice, bucket) x (half) cell.

    `implied` is the MEAN SAMPLED PRICE, not the bucket midpoint: the midpoint is
    a labelling convention, and testing a lopsided bucket against its midpoint
    invents a deviation that is really just where inside the bucket the prices
    sat. The midpoint is reported alongside so the lopsidedness stays visible.
    """
    n = len(observations)
    prices = [o.yes_price for o in observations]
    successes = sum(1 for o in observations if o.settled_yes)
    implied = sum(prices) / n
    realized = successes / n

    clusters: dict[str, list[float]] = defaultdict(list)
    for o in observations:
        clusters[cluster_key(o, cluster_mode)].append((1.0 if o.settled_yes else 0.0) - o.yes_price)

    # Model variance under the null, which doubles as the floor for the
    # empirical one (see cluster_robust_test).
    model_variance = sum(p * (1.0 - p) for p in prices)
    se_naive = math.sqrt(model_variance) or float("inf")
    raw_cluster_var = sum(
        sum(v) ** 2 for v in clusters.values()
    ) * (len(clusters) / max(1.0, len(clusters) - 1.0))
    z, p_robust, se = cluster_robust_test(clusters, direction=direction,
                                          floor_variance=model_variance)
    p_naive = naive_pvalue(prices, successes, exact_max)

    ev_yes = side_ev(observations, "YES")
    ev_no = side_ev(observations, "NO")
    best_side = "YES" if ev_yes >= ev_no else "NO"

    return {
        "n": n,
        "n_clusters": len(clusters),
        "n_markets": len({o.ticker for o in observations}),
        "successes": successes,
        "implied": implied,
        "realized": realized,
        "diff": realized - implied,
        "z": z,
        "p_robust": p_robust,
        "p_naive": p_naive,
        "se_robust": se,
        "se_naive": se_naive,
        # Reported BEFORE flooring, so the diagnostic stays honest about what the
        # data actually said; `variance_floored` says when the floor bound.
        "design_effect": (math.sqrt(raw_cluster_var) / se_naive)
                         if se_naive not in (0.0, float("inf")) else float("nan"),
        "variance_floored": raw_cluster_var < model_variance,
        "expected_successes": sum(prices),
        "expected_failures": sum(1.0 - p for p in prices),
        "ev_yes": ev_yes,
        "ev_no": ev_no,
        "ev_best": max(ev_yes, ev_no),
        "best_side": best_side,
        "median_volume_fp": statistics.median([o.volume_fp for o in observations]),
        "median_open_interest_fp": statistics.median([o.open_interest_fp for o in observations]),
        "mid_share": sum(1 for o in observations if o.source == "mid") / n,
        "median_staleness_s": statistics.median([o.staleness_s for o in observations]),
        "first_close": min(o.close_ts for o in observations),
        "last_close": max(o.close_ts for o in observations),
    }


def split_halves(observations: list[Observation]) -> tuple[list[Observation], list[Observation]]:
    """
    (earlier half, later half) of one series, split at the median close time.

    The split is by MARKET CLOSE TIME, per series, and is computed before any
    test is run. Discovery only ever sees the earlier half; the later half is
    untouched until a candidate exists to confirm. Splitting per series (rather
    than at one global date) keeps both halves populated for series that only
    started listing partway through the window.
    """
    ordered = sorted(observations, key=lambda o: (o.close_ts, o.ticker, o.slice_minutes))
    if len(ordered) < 2:
        return ordered, []

    # Split at the median DISTINCT close time, not the median observation. A
    # series whose markets share close times (multi-strike events all expiring
    # together) can put well over half its observations on a single timestamp,
    # and splitting by observation index would then hand the entire series to
    # one half and leave the other empty -- silently disabling the out-of-sample
    # check for exactly the series with the most strikes per event.
    distinct = sorted({o.close_ts for o in ordered})
    if len(distinct) < 2:
        return ordered, []          # one close time: no time split is possible
    boundary = distinct[len(distinct) // 2]

    # A market never straddles the split: everything closing at or after the
    # boundary goes to the later half, so no market contributes to both.
    earlier = [o for o in ordered if o.close_ts < boundary]
    later = [o for o in ordered if o.close_ts >= boundary]
    return earlier, later


# ── Liquidity ────────────────────────────────────────────────────────────────

def _level_price_dollars(raw: object) -> float | None:
    """
    A book level price as dollars, whether the API sent cents or dollar strings.

    Kalshi has shipped both encodings on different routes, and guessing wrong
    silently turns every price into 0.0 or 47.0 -- both of which would land no
    book in any bucket and quietly report "no liquidity data" forever. Anything
    above 1.5 is unambiguously cents (a probability cannot be).
    """
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value > 1.5:
        value /= 100.0
    return value if 0.0 <= value <= 1.0 else None


def parse_book(raw: dict) -> tuple[float | None, int, float | None, int]:
    """
    (yes_bid, yes_bid_size, yes_ask, yes_ask_size) from a raw orderbook payload.

    Kalshi books are two one-sided ladders: `yes` holds bids to buy YES, `no`
    holds bids to buy NO. The best YES ASK is therefore derived from the best NO
    BID (yes_ask = 1 - no_bid), and the size resting at the YES ask is the size
    of that NO bid -- that is the depth you would actually lift to buy YES.
    """
    book = raw.get("orderbook") or raw
    yes_levels = book.get("yes") or []
    no_levels = book.get("no") or []

    def best(levels: list) -> tuple[float | None, int]:
        best_price, best_size = None, 0
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price = _level_price_dollars(level[0])
            if price is None:
                continue
            try:
                size = int(float(level[1]))
            except (TypeError, ValueError):
                continue
            if best_price is None or price > best_price:
                best_price, best_size = price, size
        return best_price, best_size

    yes_bid, yes_bid_size = best(yes_levels)
    no_bid, no_bid_size = best(no_levels)
    yes_ask = None if no_bid is None else 1.0 - no_bid
    return yes_bid, yes_bid_size, yes_ask, no_bid_size


def probe_series_liquidity(
    api: _ReadOnlyAPI,
    cache: ScanCache,
    series: str,
    slice_minutes: int,
    *,
    max_books: int,
    tolerance_s: int,
    debug: bool,
) -> list[dict]:
    """
    LIVE snapshot of resting depth for currently-open markets of `series`, taken
    at a comparable time-to-close to `slice_minutes`.

    This is a proxy and is labelled as one everywhere it is printed. Historical
    touch depth genuinely cannot be recovered: candlesticks carry bid/ask PRICES
    but no sizes, and a settled market's book is empty. So the honest available
    answer is "what is resting in this series today at that point in a market's
    life", which is what you actually need to decide whether an edge is
    tradeable at size or is three contracts deep.
    """
    try:
        data = api.get("/markets", {"series_ticker": series, "status": "open", "limit": 1000})
    except Exception as exc:  # noqa: BLE001 -- liquidity is a nice-to-have, never fatal
        print(f"    liquidity probe: could not list open {series} markets "
              f"({type(exc).__name__}); skipping.")
        return []

    now = int(time.time())
    target = slice_minutes * 60
    candidates = []
    for market in data.get("markets") or []:
        close_ts = parse_iso_ts(market.get("close_time"))
        ticker = market.get("ticker")
        if close_ts is None or not ticker:
            continue
        ttc = close_ts - now
        if ttc <= 0 or abs(ttc - target) > tolerance_s:
            continue
        candidates.append((abs(ttc - target), ttc, ticker))

    candidates.sort()
    samples: list[dict] = []
    for _, ttc, ticker in candidates[:max_books]:
        try:
            raw = api.get(f"/markets/{ticker}/orderbook", {"depth": 10})
        except Exception:  # noqa: BLE001
            continue
        cache.put_book(ticker, now, raw)
        if debug and not samples:
            print(f"    liquidity probe: raw book sample for {ticker}: "
                  f"{json.dumps(raw)[:300]}")
        yes_bid, yes_bid_size, yes_ask, yes_ask_size = parse_book(raw)
        if yes_bid is None or yes_ask is None:
            continue
        mid = (yes_bid + yes_ask) / 2.0
        if not (0.0 < mid < 1.0):
            continue
        samples.append({
            "ticker": ticker,
            "seconds_to_close": ttc,
            "bucket": bucket_index(mid),
            "mid": mid,
            "spread": yes_ask - yes_bid,
            # Size you would lift to BUY that side, i.e. the resting opposite bid.
            "size_to_buy_yes": yes_ask_size,
            "size_to_buy_no": yes_bid_size,
        })
    return samples


def liquidity_for_row(samples: list[dict], bucket: int, side: str) -> dict:
    """Median resting depth at the touch for one flagged bucket/side, plus n."""
    key = "size_to_buy_yes" if side == "YES" else "size_to_buy_no"
    in_bucket = [s[key] for s in samples if s["bucket"] == bucket]
    if not in_bucket:
        # Fall back to the whole series so a flagged row still says something,
        # clearly marked as not bucket-specific.
        allsizes = [s[key] for s in samples]
        if not allsizes:
            return {"median_touch": None, "n_books": 0, "bucket_specific": False}
        return {"median_touch": statistics.median(allsizes), "n_books": len(allsizes),
                "bucket_specific": False}
    return {"median_touch": statistics.median(in_bucket), "n_books": len(in_bucket),
            "bucket_specific": True}


# ── Sanity diagnostics ───────────────────────────────────────────────────────

def _percentiles(values: list[float], points=(0, 10, 50, 90, 100)) -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    out = []
    for pct in points:
        idx = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * (len(ordered) - 1)))))
        out.append(f"p{pct}={ordered[idx]:.4g}")
    return "  ".join(out)


def print_sanity(observations: list[Observation], slices: list[int]) -> None:
    """
    Distributions of the intermediates, so a degenerate pipeline is loud.

    Every fake edge this script could produce shows up here first: a stale price
    labelled as T-5min, a bucket that is 100% last-trade prints, a series whose
    settle rate is 0.00 because its outcomes were misparsed, a global
    calibration curve that does not track the diagonal. Read this section before
    reading the findings.
    """
    print(f"\n{'=' * 118}")
    print("=== SANITY DISTRIBUTIONS  (read these before the findings)")
    print(f"{'=' * 118}")

    n = len(observations)
    series_set = {o.series for o in observations}
    print(f"  observations           : {n:,} over {len(series_set)} series, "
          f"{len({o.ticker for o in observations}):,} settled markets")

    mids = sum(1 for o in observations if o.source == "mid")
    print(f"  price source           : {mids / n * 100:.1f}% two-sided mid, "
          f"{(n - mids) / n * 100:.1f}% last trade")
    print("                           (a bucket built mostly from last trades is a "
          "traded-price distribution, not a quoted one -- selection bias lives here)")

    print(f"  candle staleness (s)   : {_percentiles([float(o.staleness_s) for o in observations])}")
    print("                           (seconds between the candle used and the nominal slice "
          "time; large values mean the price is not really a T-N price)")

    print(f"  candle volume_fp       : {_percentiles([o.volume_fp for o in observations])}")
    print(f"  candle open_interest_fp: {_percentiles([o.open_interest_fp for o in observations])}")
    print("                           (raw candle fields, non-integer -- relative activity, NOT "
          "contract counts; only the live touch probe reports real size)")

    print("\n  per-slice coverage:")
    print(f"    {'slice':>8}{'obs':>10}{'markets':>10}{'mid%':>8}{'mean px':>10}"
          f"{'settle rate':>13}{'diff':>9}")
    for minutes in slices:
        subset = [o for o in observations if o.slice_minutes == minutes]
        if not subset:
            print(f"    {'T-' + str(minutes):>8}{'0':>10}")
            continue
        mean_price = sum(o.yes_price for o in subset) / len(subset)
        rate = sum(1 for o in subset if o.settled_yes) / len(subset)
        share_mid = sum(1 for o in subset if o.source == "mid") / len(subset)
        print(f"    {'T-' + str(minutes):>8}{len(subset):>10,}"
              f"{len({o.ticker for o in subset}):>10,}{share_mid * 100:>7.0f}%"
              f"{mean_price:>10.3f}{rate:>13.3f}{rate - mean_price:>+9.3f}")

    print("\n  GLOBAL calibration curve (all series pooled -- should track the diagonal;")
    print("  if it does not, suspect the pipeline before believing an exchange-wide bias):")
    print(f"    {'bucket':<12}{'n':>10}{'mean px':>10}{'realized':>10}{'diff':>9}")
    for index in range(_N_BUCKETS):
        subset = [o for o in observations if bucket_index(o.yes_price) == index]
        if not subset:
            continue
        mean_price = sum(o.yes_price for o in subset) / len(subset)
        rate = sum(1 for o in subset if o.settled_yes) / len(subset)
        print(f"    {bucket_label(index):<12}{len(subset):>10,}{mean_price:>10.3f}"
              f"{rate:>10.3f}{rate - mean_price:>+9.3f}")

    by_series: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        by_series[o.series].append(o)
    rates = [(s, sum(1 for o in v if o.settled_yes) / len(v),
              sum(o.yes_price for o in v) / len(v), len(v)) for s, v in by_series.items()]
    degenerate = [r for r in rates if r[1] in (0.0, 1.0)]
    print(f"\n  observations per series: {_percentiles([float(len(v)) for v in by_series.values()])}")
    print(f"  series whose settle rate is exactly 0.00 or 1.00: {len(degenerate)}")
    if degenerate:
        print("    (all-one-way settlement is normal for some structures, but it is also "
              "what a broken outcome parse looks like -- spot-check one:)")
        for series, rate, price, count in degenerate[:5]:
            print(f"      {series:<24} n={count:<8,} settle_rate={rate:.2f}  mean_price={price:.3f}")


# ── Discovery / confirmation ─────────────────────────────────────────────────

def run_analysis(
    observations: list[Observation],
    *,
    min_n: int,
    min_clusters: int,
    cluster_mode: str,
    exact_max: int,
    alpha: float,
    fdr_q: float,
    min_expected: float,
) -> dict:
    """
    The two-stage test. Discovery on the earlier half of each series, then
    confirmation of the survivors on the later half, which discovery never saw.

    Order matters and is enforced by construction: the halves are split first,
    every discovery p-value is computed and FDR-adjusted over the FULL discovery
    family (not just the interesting-looking cells -- that would be the same
    selection bias in a different coat), and only then is the later half touched.
    """
    by_series: dict[str, list[Observation]] = defaultdict(list)
    for obs in observations:
        by_series[obs.series].append(obs)

    discovery: list[dict] = []
    later_halves: dict[str, list[Observation]] = {}
    series_with_tests: set[str] = set()
    split_info: dict[str, dict] = {}
    skipped_low_expected = 0
    skipped_few_clusters = 0

    for series, series_obs in sorted(by_series.items()):
        earlier, later = split_halves(series_obs)
        later_halves[series] = later
        split_info[series] = {
            "n_earlier": len(earlier),
            "n_later": len(later),
            "boundary_ts": min((o.close_ts for o in later), default=None),
        }
        if not earlier:
            continue

        cells: dict[tuple[int, int], list[Observation]] = defaultdict(list)
        for obs in earlier:
            cells[(obs.slice_minutes, bucket_index(obs.yes_price))].append(obs)

        for (slice_minutes, bucket), group in sorted(cells.items()):
            if len(group) < min_n:
                continue
            cell = build_cell(group, cluster_mode, exact_max)
            if cell["n_clusters"] < min_clusters:
                skipped_few_clusters += 1
                continue
            # A normal approximation is meaningless when the null expects almost
            # no events either way: 27 samples priced at 1c expect 0.3 YES
            # settlements, and "0 observed vs 0.3 expected" is not evidence of
            # anything. Without this gate the 0-5c and 95-100c buckets generate
            # findings on essentially no information -- and those are the buckets
            # where after-fee EV is smallest anyway.
            if (cell["expected_successes"] < min_expected
                    or cell["expected_failures"] < min_expected):
                skipped_low_expected += 1
                continue
            series_with_tests.add(series)
            discovery.append({
                "series": series,
                "slice": slice_minutes,
                "bucket": bucket,
                **cell,
            })

    # FDR over the entire discovery family -- every test run, no pre-filtering.
    p_values = [row["p_robust"] for row in discovery]
    adjusted, passed = benjamini_hochberg(p_values, fdr_q)
    for row, p_adj, ok in zip(discovery, adjusted, passed):
        row["p_fdr"] = p_adj
        row["passes_fdr"] = ok
        row["passes_raw"] = row["p_robust"] < alpha

    # ---- confirmation: only now do we look at the later half ----------------
    candidates = [row for row in discovery if row["passes_fdr"]]
    confirmations: list[dict] = []
    for row in candidates:
        direction = 1 if row["diff"] > 0 else -1
        later = later_halves.get(row["series"], [])
        group = [o for o in later
                 if o.slice_minutes == row["slice"] and bucket_index(o.yes_price) == row["bucket"]]
        entry = {"key": (row["series"], row["slice"], row["bucket"]), "direction": direction}
        if len(group) < min_n:
            entry.update({"status": "insufficient_holdout", "n": len(group),
                          "p_robust": None, "cell": None})
            confirmations.append(entry)
            continue
        cell = build_cell(group, cluster_mode, exact_max, direction=direction)
        if cell["n_clusters"] < min_clusters:
            entry.update({"status": "too_few_clusters", "n": cell["n"],
                          "p_robust": None, "cell": cell})
            confirmations.append(entry)
            continue
        if (cell["expected_successes"] < min_expected
                or cell["expected_failures"] < min_expected):
            entry.update({"status": "too_few_expected", "n": cell["n"],
                          "p_robust": None, "cell": cell})
            confirmations.append(entry)
            continue
        entry.update({"status": "tested", "n": cell["n"], "p_robust": cell["p_robust"],
                      "cell": cell})
        confirmations.append(entry)

    tested = [c for c in confirmations if c["status"] == "tested"]
    conf_adjusted, conf_passed = benjamini_hochberg([c["p_robust"] for c in tested], fdr_q)
    for entry, p_adj, ok in zip(tested, conf_adjusted, conf_passed):
        entry["p_fdr"] = p_adj
        # Same direction AND still significant out of sample. A sign flip is the
        # single most common way a discovered bucket dies, so it is checked
        # explicitly rather than left to the one-sided p alone.
        same_sign = (entry["cell"]["diff"] > 0) == (entry["direction"] > 0)
        entry["confirmed"] = bool(ok and same_sign)
        entry["same_sign"] = same_sign

    by_key = {c["key"]: c for c in confirmations}
    for row in discovery:
        row["confirmation"] = by_key.get((row["series"], row["slice"], row["bucket"]))

    return {
        "discovery": discovery,
        "confirmations": confirmations,
        "split_info": split_info,
        "series_with_tests": series_with_tests,
        "n_tests": len(discovery),
        "skipped_low_expected": skipped_low_expected,
        "skipped_few_clusters": skipped_few_clusters,
        "n_raw_flagged": sum(1 for r in discovery if r["passes_raw"]),
        "n_fdr_flagged": len(candidates),
        "n_confirmed": sum(1 for c in confirmations if c.get("confirmed")),
    }


# ── Reporting ────────────────────────────────────────────────────────────────

def print_time_split(result: dict, limit: int = 25) -> None:
    """
    Where each series was cut, and how much data landed on each side.

    Printed because the whole out-of-sample claim rests on this cut: a series
    whose later half is tiny cannot confirm anything, and that has to be visible
    rather than showing up as a mysterious "insufficient_holdout".
    """
    info = {s: v for s, v in result["split_info"].items() if s in result["series_with_tests"]}
    if not info:
        return
    print(f"\n{'=' * 118}")
    print("=== TIME SPLIT (discovery = earlier half, confirmation = later half)")
    print(f"{'=' * 118}")
    print(f"  {'series':<28}{'split at (UTC)':>22}{'obs earlier':>14}{'obs later':>12}{'balance':>10}")
    print(f"  {'-' * 88}")
    for series, entry in sorted(info.items(), key=lambda kv: -(kv[1]['n_earlier'] + kv[1]['n_later']))[:limit]:
        boundary = entry["boundary_ts"]
        stamp = (datetime.fromtimestamp(boundary, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                 if boundary else "n/a")
        total = entry["n_earlier"] + entry["n_later"]
        share = entry["n_earlier"] / total if total else 0.0
        print(f"  {series[:28]:<28}{stamp:>22}{entry['n_earlier']:>14,}"
              f"{entry['n_later']:>12,}{share:>9.0%}")
    if len(info) > limit:
        print(f"  ... and {len(info) - limit} more series")


def print_multiplicity(result: dict, alpha: float, fdr_q: float) -> None:
    """Make the multiple-comparisons inflation impossible to overlook."""
    m = result["n_tests"]
    expected_false = m * alpha
    print(f"\n{'=' * 118}")
    print("=== MULTIPLE COMPARISONS")
    print(f"{'=' * 118}")
    print(f"  tests run (series x slice x bucket cells, discovery half only) : {m}")
    print(f"  expected false positives at raw alpha={alpha:.3f} if NOTHING is "
          f"miscalibrated : {expected_false:.1f}")
    print(f"  cells with raw p < {alpha:.3f}                                       : "
          f"{result['n_raw_flagged']}")
    if expected_false > 0:
        ratio = result["n_raw_flagged"] / expected_false
        verdict = ("i.e. no more than chance would give you" if ratio <= 1.0
                   else f"i.e. {ratio:.1f}x what chance alone would give you")
        print(f"  {'':<64}  {verdict}")
    print(f"  cells surviving Benjamini-Hochberg at q={fdr_q:.2f}                  : "
          f"{result['n_fdr_flagged']}")
    print(f"  of those, confirmed out of sample on the held-out later half   : "
          f"{result['n_confirmed']}")
    print("\n  BH controls the expected FRACTION of false positives among the cells it lets")
    print(f"  through, at {fdr_q:.0%}. It does NOT promise every survivor is real -- that is what")
    print("  the out-of-sample column is for.")


def print_design_effect(result: dict) -> None:
    """How much the independence assumption would have inflated significance."""
    effects = [r["design_effect"] for r in result["discovery"]
               if r["design_effect"] == r["design_effect"] and math.isfinite(r["design_effect"])]
    clusters = [float(r["n_clusters"]) for r in result["discovery"]]
    naive_flagged = sum(1 for r in result["discovery"] if r["p_naive"] < 0.05)
    robust_flagged = sum(1 for r in result["discovery"] if r["p_robust"] < 0.05)
    print("\n  clustering diagnostic:")
    print(f"    clusters per test      : {_percentiles(clusters)}")
    print(f"    cells whose empirical cluster variance fell below the model null and was")
    print(f"      floored to it (extreme buckets; would fake a tiny p) : "
          f"{sum(1 for r in result['discovery'] if r.get('variance_floored'))}")
    print(f"    design effect (SE_robust / SE_independent) : {_percentiles(effects)}")
    print(f"    cells with p < 0.05 assuming independence  : {naive_flagged}")
    print(f"    cells with p < 0.05 clustered (reported)   : {robust_flagged}")
    print("    A design effect above 1 means observations inside a cluster move together and")
    print("    the independent p-values were too small. The clustered ones are what is used.")


_ROW_HEADER = (
    f"  {'series':<22}{'slice':>7}{'bucket':>12}{'n':>7}{'implied':>9}{'realized':>10}"
    f"{'diff':>8}{'EV/ct':>9}{'side':>5}{'raw p':>10}{'FDR p':>10}{'oos':>6}"
    f"{'n_oos':>7}{'p_oos':>10}{'touch':>8}{'volfp':>8}"
)


def _format_row(row: dict, liquidity: dict | None) -> str:
    conf = row.get("confirmation") or {}
    oos = "yes" if conf.get("confirmed") else ("NO" if conf.get("status") == "tested" else "-")
    n_oos = conf.get("n") or 0
    p_oos = conf.get("p_fdr")
    touch = "n/a" if not liquidity or liquidity.get("median_touch") is None else (
        f"{liquidity['median_touch']:.0f}" + ("" if liquidity.get("bucket_specific") else "*")
    )
    return (
        f"  {row['series'][:22]:<22}{'T-' + str(row['slice']):>7}"
        f"{bucket_label(row['bucket']):>12}{row['n']:>7}{row['implied']:>9.3f}"
        f"{row['realized']:>10.3f}{row['diff']:>+8.3f}{row['ev_best']:>+9.4f}"
        f"{row['best_side']:>5}{row['p_robust']:>10.2e}{row['p_fdr']:>10.2e}{oos:>6}"
        f"{n_oos:>7}"
        f"{('n/a' if p_oos is None else f'{p_oos:.2e}'):>10}"
        f"{touch:>8}{row['median_volume_fp']:>8.0f}"
    )


def print_findings(result: dict, liquidity: dict, alpha: float, flag_threshold: float,
                   show_all: bool) -> list[dict]:
    """Discovery table, then the held-out confirmation table, then the ranked result."""
    discovery = result["discovery"]

    print(f"\n{'=' * 118}")
    print("=== STAGE 1: DISCOVERY (earlier half of each series only)")
    print(f"{'=' * 118}")
    shown = [r for r in discovery if (r["passes_raw"] if show_all else r["passes_fdr"])]
    if not shown:
        print(f"  No cell survived {'raw alpha' if show_all else 'BH correction'}. "
              f"That is the null result: at this resolution, prices look calibrated.")
    else:
        label = "raw p < alpha" if show_all else "surviving BH"
        print(f"  {len(shown)} cells {label}, ranked by FDR-adjusted p:")
        print(_ROW_HEADER)
        print(f"  {'-' * 148}")
        for row in sorted(shown, key=lambda r: r["p_fdr"]):
            print(_format_row(row, liquidity.get((row["series"], row["slice"], row["bucket"]))))

    print(f"\n{'=' * 118}")
    print("=== STAGE 2: CONFIRMATION (later half -- never touched during discovery)")
    print(f"{'=' * 118}")
    confirmations = result["confirmations"]
    if not confirmations:
        print("  Nothing to confirm: no cell survived discovery.")
    else:
        print(f"  {'series':<22}{'slice':>7}{'bucket':>12}{'status':>20}{'n_oos':>8}"
              f"{'diff_disc':>11}{'diff_oos':>10}{'sign':>7}{'p_oos':>10}{'FDR p_oos':>11}")
        print(f"  {'-' * 118}")
        disc_by_key = {(r["series"], r["slice"], r["bucket"]): r for r in discovery}
        for entry in sorted(confirmations, key=lambda c: (not c.get("confirmed"), c["key"])):
            series, slice_minutes, bucket = entry["key"]
            disc = disc_by_key[entry["key"]]
            cell = entry.get("cell")
            diff_oos = f"{cell['diff']:+.3f}" if cell else "n/a"
            sign = ("same" if entry.get("same_sign") else "FLIP") if cell else "n/a"
            p_oos = entry.get("p_robust")
            p_fdr = entry.get("p_fdr")
            status = "CONFIRMED" if entry.get("confirmed") else entry["status"]
            print(f"  {series[:22]:<22}{'T-' + str(slice_minutes):>7}"
                  f"{bucket_label(bucket):>12}{status:>20}{entry.get('n') or 0:>8}"
                  f"{disc['diff']:>+11.3f}{diff_oos:>10}{sign:>7}"
                  f"{('n/a' if p_oos is None else f'{p_oos:.2e}'):>10}"
                  f"{('n/a' if p_fdr is None else f'{p_fdr:.2e}'):>11}")
        print("\n  p_oos is ONE-SIDED in the direction discovery found -- the direction was")
        print("  fixed before this half was read, so spending alpha on both tails would be")
        print("  wasteful. A sign flip fails confirmation regardless of its p-value.")

    print(f"\n{'=' * 118}")
    print("=== RANKED RESULT: cells that survived BOTH halves")
    print(f"{'=' * 118}")
    final = [r for r in discovery
             if (r.get("confirmation") or {}).get("confirmed")]
    final.sort(key=lambda r: (-abs(r["ev_best"]), r["p_fdr"]))
    if not final:
        print("  NONE. No (series, slice, bucket) was both FDR-significant on the earlier half")
        print("  and independently significant, in the same direction, on the later half.")
        print("  Read that as: this scan found no durable, exploitable miscalibration.")
    else:
        print(_ROW_HEADER)
        print(f"  {'-' * 148}")
        for row in final:
            print(_format_row(row, liquidity.get((row["series"], row["slice"], row["bucket"]))))
        material = [r for r in final
                    if abs(r["diff"]) >= flag_threshold and r["ev_best"] > 0.0]
        print(f"\n  Of those, {len(material)} are economically material "
              f"(|diff| >= {flag_threshold:.3f} AND after-fee EV > 0).")
        print("  EV/ct is after the Kalshi taker fee and assumes you buy AT the sampled mid;")
        print("  crossing a real spread makes it worse. It is an upper bound, always.")
        print("  touch = median contracts resting at the touch, LIVE snapshot (see --probe-")
        print("  liquidity); * means the sample was series-wide, not specific to that bucket.")
    return final


# ── Collection driver ────────────────────────────────────────────────────────

def subsample_evenly(markets: list, cap: int) -> tuple[list, int]:
    """
    (kept, dropped) -- at most `cap` markets, spread EVENLY across the window.

    Taking the most recent `cap` (which market_calibration_study.py does, and
    which is right when you are studying one series) would be fatal here: the
    time split needs both halves populated, and a recency-biased sample can
    leave the discovery half empty or, worse, silently narrow. Striding keeps
    the calendar coverage and only thins the density.
    """
    if not cap or len(markets) <= cap:
        return markets, 0
    stride = len(markets) / cap
    idxs = sorted({int(i * stride) for i in range(cap)})
    kept = [markets[i] for i in idxs if i < len(markets)]
    return kept, len(markets) - len(kept)


def collect_observations(
    api: _ReadOnlyAPI,
    cache: ScanCache,
    series_list: list[str],
    counts: dict[str, int],
    start_ts: int,
    end_ts: int,
    *,
    slices: list[int],
    period: int,
    max_staleness_s: int,
    max_markets_per_series: int,
    max_candle_calls: int,
    refresh: bool,
    sleep_seconds: float,
) -> tuple[list[Observation], dict]:
    """Sample every qualifying series, honouring the fetch budget out loud."""
    span_minutes = max(slices) + 2 * period
    observations: list[Observation] = []
    stats = {
        "series_attempted": 0,
        "series_truncated_by_budget": [],
        "markets_considered": 0,
        "markets_dropped_by_cap": 0,
        "markets_sampled": 0,
        "markets_no_candles": 0,
        "rejects": defaultdict(int),
        "candle_calls": 0,
        "budget_exhausted": False,
    }

    for series in series_list:
        if stats["budget_exhausted"]:
            stats["series_truncated_by_budget"].append(series)
            continue

        markets = cache.settled_markets_for_series(series, start_ts, end_ts)
        if not markets:
            continue
        stats["series_attempted"] += 1
        kept, dropped = subsample_evenly(markets, max_markets_per_series)
        stats["markets_considered"] += len(markets)
        stats["markets_dropped_by_cap"] += dropped

        print(f"\n--- {series}  ({counts.get(series, len(markets)):,} settled in window; "
              f"sampling {len(kept):,}"
              f"{f', dropping {dropped:,} by --max-markets-per-series' if dropped else ''})")

        sampled_here = 0
        for ticker, event, close_ts, settled_yes, _raw in kept:
            if max_candle_calls and stats["candle_calls"] >= max_candle_calls:
                stats["budget_exhausted"] = True
                stats["series_truncated_by_budget"].append(f"{series} (partial)")
                print(f"  BUDGET: hit --max-candle-calls {max_candle_calls} inside {series}; "
                      f"stopping fetch. Results below are TRUNCATED.")
                break

            candles, called = fetch_candles_cached(
                api, cache, series, ticker,
                close_ts - span_minutes * 60, close_ts, period,
                refresh=refresh, sleep_seconds=sleep_seconds,
            )
            if called:
                stats["candle_calls"] += 1
            if not candles:
                stats["markets_no_candles"] += 1
                continue

            market_obs, rejects = sample_market(
                series, ticker, event or ticker, close_ts, candles,
                settled_yes, slices, max_staleness_s,
            )
            for reason, count in rejects.items():
                stats["rejects"][reason] += count
            if market_obs:
                observations.extend(market_obs)
                sampled_here += 1
                stats["markets_sampled"] += 1

            if sampled_here and sampled_here % _PROGRESS_PRINT_EVERY == 0:
                print(f"  {sampled_here:,}/{len(kept):,} markets sampled "
                      f"({len(observations):,} observations so far)")

        print(f"  {series}: {sampled_here:,} markets yielded samples.")

    return observations, stats


# ── Output files ─────────────────────────────────────────────────────────────

_CSV_COLUMNS = [
    "series", "slice_minutes", "bucket", "n", "n_markets", "n_clusters",
    "implied_mean_price", "implied_bucket_mid", "realized", "diff",
    "ev_yes", "ev_no", "ev_best", "best_side",
    "p_raw_clustered", "p_fdr", "p_naive_independent", "design_effect",
    "passes_raw", "passes_fdr", "held_out_of_sample",
    "n_out_of_sample", "diff_out_of_sample", "p_out_of_sample", "p_fdr_out_of_sample",
    "median_touch_size_live", "touch_bucket_specific", "n_books",
    "median_volume_fp", "median_open_interest_fp", "mid_share", "median_staleness_s",
    "first_close_iso", "last_close_iso",
]


def _csv_record(row: dict, liquidity: dict | None) -> dict:
    conf = row.get("confirmation") or {}
    cell = conf.get("cell") or {}
    liq = liquidity or {}
    iso = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()  # noqa: E731
    return {
        "series": row["series"],
        "slice_minutes": row["slice"],
        "bucket": bucket_label(row["bucket"]),
        "n": row["n"],
        "n_markets": row["n_markets"],
        "n_clusters": row["n_clusters"],
        "implied_mean_price": f"{row['implied']:.4f}",
        "implied_bucket_mid": f"{bucket_midpoint(row['bucket']):.4f}",
        "realized": f"{row['realized']:.4f}",
        "diff": f"{row['diff']:+.4f}",
        "ev_yes": f"{row['ev_yes']:+.4f}",
        "ev_no": f"{row['ev_no']:+.4f}",
        "ev_best": f"{row['ev_best']:+.4f}",
        "best_side": row["best_side"],
        "p_raw_clustered": f"{row['p_robust']:.6g}",
        "p_fdr": f"{row['p_fdr']:.6g}",
        "p_naive_independent": f"{row['p_naive']:.6g}",
        "design_effect": f"{row['design_effect']:.3f}",
        "passes_raw": int(row["passes_raw"]),
        "passes_fdr": int(row["passes_fdr"]),
        "held_out_of_sample": "yes" if conf.get("confirmed") else
                              ("no" if conf.get("status") == "tested" else "not_tested"),
        "n_out_of_sample": conf.get("n") or 0,
        "diff_out_of_sample": f"{cell['diff']:+.4f}" if cell else "",
        "p_out_of_sample": f"{conf['p_robust']:.6g}" if conf.get("p_robust") is not None else "",
        "p_fdr_out_of_sample": f"{conf['p_fdr']:.6g}" if conf.get("p_fdr") is not None else "",
        "median_touch_size_live": "" if liq.get("median_touch") is None else f"{liq['median_touch']:.0f}",
        "touch_bucket_specific": int(bool(liq.get("bucket_specific"))) if liq else "",
        "n_books": liq.get("n_books", ""),
        "median_volume_fp": f"{row['median_volume_fp']:.1f}",
        "median_open_interest_fp": f"{row['median_open_interest_fp']:.1f}",
        "mid_share": f"{row['mid_share']:.3f}",
        "median_staleness_s": f"{row['median_staleness_s']:.0f}",
        "first_close_iso": iso(row["first_close"]),
        "last_close_iso": iso(row["last_close"]),
    }


def write_csv(path: Path, result: dict, liquidity: dict) -> int:
    """
    Every cell TESTED, not just the flagged ones.

    Dumping only the winners would make the file itself a selection-biased
    sample: you could never recompute the correction, re-rank at a different q,
    or see how ordinary the winners look next to the rest.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for row in sorted(result["discovery"], key=lambda r: r["p_fdr"]):
            key = (row["series"], row["slice"], row["bucket"])
            writer.writerow(_csv_record(row, liquidity.get(key)))
    return len(result["discovery"])


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)


def print_summary(
    *,
    sweep: dict,
    catalog_size: int,
    catalog_seen: int,
    total_markets: int,
    total_settled: int,
    unresolved: int,
    counts: dict[str, int],
    min_settled: int,
    qualifying: list[str],
    collection: dict,
    result: dict,
    final_rows: list[dict],
    alpha: float,
    fdr_q: float,
    requests_made: int,
    excluded: dict[str, tuple[int, int]],
    exclude_pattern: str,
) -> None:
    print(f"\n{'=' * 118}")
    print("=== SUMMARY")
    print(f"{'=' * 118}")
    print(f"  series seen in the settled-market sweep      : {len(counts)}")
    if excluded:
        print(f"  series excluded by regex {exclude_pattern!r:<12}        : {len(excluded)} "
              f"({sum(v[1] for v in excluded.values()):,} settled markets, counted not scanned)")
    print(f"  series in Kalshi /series catalog             : "
          f"{catalog_size if catalog_size else 'unavailable'}"
          + (f"  ({catalog_seen} of them appear in the sweep; the rest have no settled "
             f"market in this window)" if catalog_size else ""))
    print(f"  markets in window                            : {total_markets:,} "
          f"({total_settled:,} settled, {unresolved:,} not resolvable from Kalshi fields)")
    if sweep.get("targeted"):
        print(f"  series named via --series (min-settled NOT applied) : {len(qualifying)}")
    else:
        print(f"  series with >= {min_settled} settled markets          : {len(qualifying)}")
    print(f"  series actually sampled                      : {collection['series_attempted']}")
    print(f"  series producing >= 1 testable cell           : {len(result['series_with_tests'])}")
    print(f"  markets sampled                              : {collection['markets_sampled']:,} "
          f"of {collection['markets_considered']:,} considered")
    if collection["markets_dropped_by_cap"]:
        print(f"    dropped by --max-markets-per-series        : "
              f"{collection['markets_dropped_by_cap']:,} (evenly across the window, not the tail)")
    print(f"    yielded no candles                         : {collection['markets_no_candles']:,}")
    rejects = collection["rejects"]
    print(f"    slice samples rejected                     : "
          f"{rejects.get('no_candle', 0):,} no candle, {rejects.get('no_quote', 0):,} no quote, "
          f"{rejects.get('stale', 0):,} too stale")
    print()
    print(f"  tests run                                    : {result['n_tests']}")
    print(f"    cells skipped, too few clusters            : {result['skipped_few_clusters']}")
    print(f"    cells skipped, null expects < --min-expected: {result['skipped_low_expected']}")
    print(f"  buckets flagged BEFORE correction (p<{alpha})   : {result['n_raw_flagged']} "
          f"(chance alone would give ~{result['n_tests'] * alpha:.1f})")
    print(f"  buckets flagged AFTER BH FDR (q={fdr_q:.2f})        : {result['n_fdr_flagged']}")
    print(f"  buckets CONFIRMED out of sample              : {result['n_confirmed']}")
    material = [r for r in final_rows if r["ev_best"] > 0.0]
    print(f"    of which after-fee EV/contract > 0         : {len(material)}")
    print()
    print(f"  network requests this run                    : {requests_made:,} "
          f"(a rerun with the same window is ~0 -- everything is cached)")
    print(f"  candle fetches this run                      : {collection['candle_calls']:,}")

    truncations = []
    if not sweep.get("complete"):
        truncations.append(f"the market sweep did NOT complete ({sweep.get('abort_reason', 'interrupted')}) "
                           f"-- series may be missing entirely; rerun to resume")
    if collection["budget_exhausted"]:
        truncations.append(f"--max-candle-calls was hit; {len(collection['series_truncated_by_budget'])} "
                           f"series were skipped or partially sampled: "
                           f"{', '.join(collection['series_truncated_by_budget'][:8])}"
                           + (" ..." if len(collection["series_truncated_by_budget"]) > 8 else ""))
    if sweep.get("no_series"):
        truncations.append(f"{sweep['no_series']:,} markets had no derivable series ticker and were dropped")
    if truncations:
        print(f"\n  !! COVERAGE CAVEATS -- this run is not a complete scan:")
        for note in truncations:
            print(f"     - {note}")
    else:
        print("\n  Coverage: sweep complete, no budget truncation. This is a full scan of the window.")


# ── Entry point ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--days", type=int, default=30,
                        help="Days of settled history to scan (default 30)")
    parser.add_argument("--min-settled", type=int, default=200,
                        help="Minimum settled markets in the window for a series to be scanned "
                             "(default 200)")
    parser.add_argument("--series", action="append", default=None,
                        help="Restrict the scan to these series (repeatable). Skips the "
                             "exchange-wide census and enumerates just these -- much faster, but "
                             "it can only find what you name. Default: every series that clears "
                             "--min-settled.")
    parser.add_argument("--exclude-series-regex", default=r"^KXMVE",
                        help="Series matching this regex are counted but never stored or scanned "
                             r"(default '^KXMVE'). KXMVECROSSCATEGORY alone is ~61k settled "
                             "markets PER DAY -- 99%% of the exchange by count and ~300MB/day of "
                             "cache -- and it is a combinatorial parlay basket, not an independent "
                             "price. Pass '' to include everything.")
    parser.add_argument("--slices", default=",".join(str(s) for s in _DEFAULT_SLICES),
                        help="Comma-separated minutes-before-close to sample (default 10,5,2)")
    parser.add_argument("--enumerate-only", action="store_true",
                        help="Do the market sweep and print the series landscape, then stop. "
                             "Cheap; run this first to see what a full scan would cost.")

    parser.add_argument("--align", choices=["day", "hour", "none"], default="day",
                        help="Snap the window end to the UTC day (default), the hour, or not at "
                             "all. The cache is keyed by window, so 'none' means every run "
                             "re-enumerates the exchange from scratch -- use it only when you "
                             "genuinely need the last few hours included.")
    parser.add_argument("--cache", type=Path, default=Path("data/scan_all_series.sqlite"),
                        help="Local sqlite cache of raw API responses "
                             "(default data/scan_all_series.sqlite)")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the cache and refetch everything")
    parser.add_argument("--sleep", type=float, default=0.25,
                        help="Seconds to sleep between candle fetches (default 0.25)")

    parser.add_argument("--min-n", type=int, default=100,
                        help="Minimum observations in a cell before it is tested, in EACH half "
                             "(default 100)")
    parser.add_argument("--min-clusters", type=int, default=30,
                        help="Minimum independent clusters in a cell (default 30). The robust "
                             "variance is unreliable below this.")
    parser.add_argument("--cluster", choices=["event", "day", "none"], default="event",
                        help="Clustering unit for the robust variance (default event). 'day' is "
                             "stricter; 'none' assumes independence and WILL overstate "
                             "significance -- diagnostic use only.")
    parser.add_argument("--alpha", type=float, default=0.05,
                        help="Uncorrected significance level, for the before-correction count "
                             "(default 0.05)")
    parser.add_argument("--fdr-q", type=float, default=0.05,
                        help="Benjamini-Hochberg target false discovery rate (default 0.05)")
    parser.add_argument("--flag-threshold", type=float, default=0.02,
                        help="|realized - implied| at or above which a confirmed cell is called "
                             "economically material (default 0.02, roughly the fee at 50c)")
    parser.add_argument("--min-expected", type=float, default=5.0,
                        help="Minimum expected YES settlements AND expected NO settlements under "
                             "the null before a cell is tested (default 5). Stops the 0-5c and "
                             "95-100c buckets producing findings on no information.")
    parser.add_argument("--exact-max", type=int, default=400,
                        help="Cell size up to which the naive p-value uses the exact "
                             "Poisson-binomial DP instead of a normal approximation (default 400)")
    parser.add_argument("--max-staleness", type=int, default=None,
                        help="Drop a slice sample if the candle backing it ended more than this "
                             "many seconds before the slice time (default: 3 candle periods)")

    parser.add_argument("--max-markets-per-series", type=int, default=2000,
                        help="Cap markets sampled per series, spread evenly across the window "
                             "(0 = no cap, default 2000)")
    parser.add_argument("--max-candle-calls", type=int, default=0,
                        help="Global cap on candle NETWORK fetches this run (0 = no cap). Cache "
                             "hits do not count, so a resumed run picks up where it stopped.")

    parser.add_argument("--probe-liquidity", action="store_true",
                        help="After the analysis, take a LIVE orderbook snapshot for confirmed "
                             "cells to report median size resting at the touch")
    parser.add_argument("--liquidity-books", type=int, default=25,
                        help="Max live books to probe per (series, slice) (default 25)")
    parser.add_argument("--liquidity-tolerance", type=int, default=300,
                        help="Seconds of slack around the slice time when picking live markets "
                             "to probe (default 300)")
    parser.add_argument("--debug-book", action="store_true",
                        help="Print one raw orderbook payload, to verify the price encoding")

    parser.add_argument("--show-all-raw", action="store_true",
                        help="In the discovery table, show every cell with raw p < alpha, not "
                             "just BH survivors (this is the inflated list -- for comparison)")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Write every tested cell (not just the flagged ones) to CSV")
    parser.add_argument("--json", type=Path, default=None, dest="json_path",
                        help="Write the machine-readable summary to JSON")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        slices = sorted({int(s) for s in args.slices.split(",") if s.strip()}, reverse=True)
    except ValueError:
        parser.error(f"--slices must be comma-separated integers, got {args.slices!r}")
    if not slices:
        parser.error("--slices must name at least one time slice")

    # Snap the window end to a stable boundary. Using time.time() directly makes
    # every run a DIFFERENT window, which silently gives the enumeration a fresh
    # cache key each time -- and re-paging the whole exchange costs hours, so
    # "cached" has to mean cached across runs, not just within one. Day
    # alignment deliberately excludes the partial current day; markets closing
    # today are mostly unsettled anyway.
    now = int(time.time())
    if args.align == "day":
        end_ts = now - (now % 86400)
    elif args.align == "hour":
        end_ts = now - (now % 3600)
    else:
        end_ts = now
    start_ts = end_ts - args.days * 86400
    period = choose_period_interval(max(slices))
    max_staleness = args.max_staleness if args.max_staleness is not None else 3 * period * 60

    print("Kalshi exchange-wide calibration scan -- READ-ONLY (GET requests only)")
    print(f"  window   : {datetime.fromtimestamp(start_ts, tz=timezone.utc):%Y-%m-%d %H:%M} -> "
          f"{datetime.fromtimestamp(end_ts, tz=timezone.utc):%Y-%m-%d %H:%M} UTC ({args.days} days, "
          f"--align {args.align})")
    print(f"  slices   : {', '.join('T-' + str(s) + 'min' for s in slices)} "
          f"({period}-minute candles, staleness cap {max_staleness}s)")
    print(f"  gates    : >= {args.min_settled} settled markets/series, >= {args.min_n} obs and "
          f">= {args.min_clusters} clusters/cell")
    try:
        exclude_rx = re.compile(args.exclude_series_regex) if args.exclude_series_regex else None
    except re.error as exc:
        parser.error(f"--exclude-series-regex is not a valid regex: {exc}")
    print(f"  excluded : {args.exclude_series_regex or '(nothing)'}")
    print(f"  stats    : cluster={args.cluster}, alpha={args.alpha}, BH q={args.fdr_q}")
    print(f"  cache    : {args.cache}{' (REFRESHING)' if args.refresh else ''}")

    cache = ScanCache(args.cache)
    api = _ReadOnlyAPI(settings)
    liquidity: dict[tuple, dict] = {}

    try:
        # ---- stage 0: enumeration ------------------------------------------
        print(f"\n{'=' * 118}")
        if args.series:
            print(f"=== ENUMERATION: targeted -- {len(args.series)} named series "
                  f"(NOT a census; only what you named can be found)")
            print(f"{'=' * 118}")
            sweep = enumerate_named_series(
                api, cache, args.series, start_ts, end_ts,
                refresh=args.refresh, page_limit=_MARKETS_PER_PAGE,
            )
        else:
            print("=== ENUMERATION: sweeping GET /markets for every settled market in the window")
            print(f"{'=' * 118}")
            sweep = sweep_settled_markets(
                api, cache, start_ts, end_ts, refresh=args.refresh,
                page_limit=_MARKETS_PER_PAGE, exclude_rx=exclude_rx,
            )
        counts = cache.settled_counts_by_series(start_ts, end_ts)
        total_markets = cache.total_markets(start_ts, end_ts)
        total_settled = sum(counts.values())
        unresolved = cache.unresolved_count(start_ts, end_ts)

        catalog = fetch_series_catalog(api, cache, refresh=args.refresh)
        catalog_tickers = {str(s.get("ticker")) for s in catalog if s.get("ticker")}
        catalog_seen = len(catalog_tickers & set(counts))

        print(f"\n  settled markets : {total_settled:,} across {len(counts)} series "
              f"({unresolved:,} markets in window had no resolvable outcome and are excluded)")

        excluded = cache.excluded_counts() if not args.series else {}
        if excluded:
            total_excluded = sum(v[1] for v in excluded.values())
            print(f"  EXCLUDED by --exclude-series-regex {args.exclude_series_regex!r}: "
                  f"{total_excluded:,} settled markets in {len(excluded)} series, counted but "
                  f"not scanned:")
            for series, (n_all, n_settled) in list(excluded.items())[:5]:
                print(f"    {series:<28}{n_settled:>12,} settled  ({n_all:,} total)")
            print("    (these were paged and counted, so the census above is still honest about "
                  "what exists; pass --exclude-series-regex '' to scan them too)")
        if catalog_tickers:
            print(f"  catalog cross-check : /series lists {len(catalog_tickers)} series; "
                  f"{catalog_seen} appear in the sweep, "
                  f"{len(catalog_tickers - set(counts))} do not (expected: no settled market "
                  f"in this window). Sweep-only series: {len(set(counts) - catalog_tickers)}.")

        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        print(f"\n  top 25 series by settled markets in window:")
        for series, count in ranked[:25]:
            print(f"    {series:<28}{count:>8,}")

        qualifying = [s for s, c in ranked if c >= args.min_settled]
        if args.series:
            requested = set(args.series)
            missing = requested - set(counts)
            if missing:
                print(f"\n  NOTE: requested series absent from the sweep: {sorted(missing)}")
            qualifying = [s for s in ranked_keys(ranked) if s in requested]
        print(f"\n  {len(qualifying)} series clear --min-settled {args.min_settled}"
              + (f" (restricted by --series to {len(qualifying)})" if args.series else ""))

        if args.enumerate_only:
            est = sum(min(counts[s], args.max_markets_per_series or counts[s]) for s in qualifying)
            print(f"\n  --enumerate-only: stopping here. A full scan would fetch candles for "
                  f"~{est:,} markets")
            print(f"  (~{est * (args.sleep + _SECONDS_PER_CANDLE_FETCH) / 3600:.1f} h uncached at "
                  f"--sleep {args.sleep} -- round-trip latency dominates, so lowering --sleep "
                  f"barely helps -- and ~0 on any rerun, since everything is cached).")
            return

        if not qualifying:
            print("\nNo series has enough settled history to study. Widen --days or lower "
                  "--min-settled.")
            return

        # ---- stage 1: sampling ---------------------------------------------
        print(f"\n{'=' * 118}")
        print(f"=== SAMPLING {len(qualifying)} series")
        print(f"{'=' * 118}")
        observations, collection = collect_observations(
            api, cache, qualifying, counts, start_ts, end_ts,
            slices=slices, period=period, max_staleness_s=max_staleness,
            max_markets_per_series=args.max_markets_per_series,
            max_candle_calls=args.max_candle_calls,
            refresh=args.refresh, sleep_seconds=args.sleep,
        )

        if not observations:
            print("\nNo usable observations. Check credentials, and that the candlestick route "
                  "returns data for these series.")
            return

        # ---- stage 2: sanity, then the two-stage test ----------------------
        print_sanity(observations, slices)

        result = run_analysis(
            observations,
            min_n=args.min_n, min_clusters=args.min_clusters, cluster_mode=args.cluster,
            exact_max=args.exact_max, alpha=args.alpha, fdr_q=args.fdr_q,
            min_expected=args.min_expected,
        )
        print_time_split(result)
        print_multiplicity(result, args.alpha, args.fdr_q)
        print_design_effect(result)

        # ---- stage 3: liquidity for the survivors --------------------------
        confirmed_rows = [r for r in result["discovery"]
                          if (r.get("confirmation") or {}).get("confirmed")]
        if args.probe_liquidity and confirmed_rows:
            print(f"\n  probing LIVE orderbooks for {len(confirmed_rows)} confirmed cells...")
            per_series_slice: dict[tuple[str, int], list[dict]] = {}
            for row in confirmed_rows:
                key = (row["series"], row["slice"])
                if key not in per_series_slice:
                    per_series_slice[key] = probe_series_liquidity(
                        api, cache, row["series"], row["slice"],
                        max_books=args.liquidity_books,
                        tolerance_s=args.liquidity_tolerance,
                        debug=args.debug_book,
                    )
                liquidity[(row["series"], row["slice"], row["bucket"])] = liquidity_for_row(
                    per_series_slice[key], row["bucket"], row["best_side"]
                )
        elif confirmed_rows:
            print("\n  (liquidity not probed -- pass --probe-liquidity for live touch depth; "
                  "the volfp column below is a raw candle activity field, not resting size)")

        final_rows = print_findings(
            result, liquidity, args.alpha, args.flag_threshold, args.show_all_raw
        )
        print_summary(
            sweep=sweep, catalog_size=len(catalog_tickers), catalog_seen=catalog_seen,
            total_markets=total_markets, total_settled=total_settled, unresolved=unresolved,
            counts=counts, min_settled=args.min_settled, qualifying=qualifying,
            collection=collection, result=result, final_rows=final_rows,
            alpha=args.alpha, fdr_q=args.fdr_q, requests_made=api.request_count,
            excluded=excluded, exclude_pattern=args.exclude_series_regex,
        )

        if args.csv:
            written = write_csv(args.csv, result, liquidity)
            print(f"\nWrote {written} tested cells to {args.csv}")
        if args.json_path:
            write_json(args.json_path, {
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "window": {"start_ts": start_ts, "end_ts": end_ts, "days": args.days},
                "params": vars(args) | {"slices": slices, "period_interval": period,
                                        "max_staleness_s": max_staleness},
                "sweep": sweep,
                "series_settled_counts": counts,
                "qualifying_series": qualifying,
                "collection": {k: (dict(v) if isinstance(v, defaultdict) else v)
                               for k, v in collection.items()},
                "n_tests": result["n_tests"],
                "n_raw_flagged": result["n_raw_flagged"],
                "n_fdr_flagged": result["n_fdr_flagged"],
                "n_confirmed": result["n_confirmed"],
                "cells": [_csv_record(r, liquidity.get((r["series"], r["slice"], r["bucket"])))
                          for r in sorted(result["discovery"], key=lambda r: r["p_fdr"])],
            })
            print(f"Wrote machine-readable summary to {args.json_path}")

    finally:
        api.close()
        cache.close()


def ranked_keys(ranked: list[tuple[str, int]]) -> list[str]:
    """Series tickers in descending settled-market order."""
    return [series for series, _ in ranked]


if __name__ == "__main__":
    main()
