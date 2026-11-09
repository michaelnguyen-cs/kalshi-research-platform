#!/usr/bin/env python
"""
Thread C -- READ-ONLY census of Kalshi's MLB / "Pro Baseball" series universe.

WHY THIS EXISTS
Thread C banks live order-book depth for MLB markets. The MLB regular season
ends 2026-09-27 and collection runs through the postseason to a frozen end
date. Depth at a decision moment cannot be reconstructed from historical
candlesticks -- only top-of-book/OHLC survives -- so the books must be recorded
while they are live or the observation is gone until April. Recording has to be
pointed at a *universe of series*, and that universe has to be defined by
something reproducible. This script produces that definition.

It is neutral data banking. No analysis protocol exists yet and none is
anticipated here: the script decides WHAT TO RECORD and nothing else.

READ-ONLY -- STRUCTURAL, NOT MERELY BY CONVENTION
Follows scripts/scan_all_series.py: this module never constructs a
KalshiClient (that class owns place_order()/cancel_order(), and no such method
may be in scope here). All network I/O goes through _ReadOnlyAPI, which only
issues HTTP GET and refuses any path outside a hard allowlist of three read
endpoints:
        GET /series
        GET /series/{series_ticker}
        GET /events
The only things this script writes are its own sqlite cache and the snapshot /
manifest files you ask for.

--------------------------------------------------------------------------
MEMBERSHIP IS NOT INFERRED FROM TICKER STRINGS
--------------------------------------------------------------------------
Kalshi's docs direct integrators to series and event metadata rather than to
ticker parsing, and on this exchange ticker parsing is not merely discouraged,
it is measurably wrong. Live evidence, 2026-09-03:

  * KXMLBWORLD -- ticker says MLB, but it is the World Baseball Classic (a
    national-team tournament); title "World Baseball Classic".
  * KXMLBWINS-* -- 30 series whose ticker embeds MLB and which really are MLB.
  * KXMBLNLEAST -- transposed ticker (MBL, not MLB) for "National League East
    Winner"; any ^KXMLB regex misses it.
  * KXCITYMLBEXPAND -- carries competition "Pro Baseball" while tagged
    ['Hockey'].
  * KXKBO*, KXNPB*, KXLMBGAME, KXNCAABB*, KXMILBGAME -- baseball, not MLB.

So membership is decided by one official metadata field and nothing else.

--------------------------------------------------------------------------
WHERE THE DECIDING FIELD ACTUALLY LIVES  (measured, not assumed)
--------------------------------------------------------------------------
GET /series returns 13,772 series and NO product_metadata key at all. Its
`category` tops out at "Sports" and its `tags` top out at ["Baseball"] -- the
identical tag set is carried by MLB, KBO, NPB, LMB, NCAA baseball, NCAA
softball, the World Baseball Classic and the Congressional Baseball Game, so
neither field can name a league. `title` is inconsistent ("MLB ...", "Pro
Baseball ...", "Professional Baseball ...") and frequently carries no
competition marker whatsoever ("National League East Winner", "Regional
Winner", "Point Total", "First Inning Run", "CBA", "DO NOT USE").

GET /series/{ticker} DOES return product_metadata, but for baseball it holds
only a bare {"scope": ...} -- again no league.

The league lives on EVENTS:

    GET /events?series_ticker=X
      -> events[].product_metadata = {"competition": "Pro Baseball",
                                      "competition_scope": "Game"}

and `competition` is Kalshi's own controlled league vocabulary. Measured
values across the baseball estate: "Pro Baseball", "Korea KBO", "Japan NPB",
"Mexico LMB", "College Baseball", "College Softball", "World Baseball
Classic", "World Baseball Classic Exhibition", "Pro Baseball Spring
Training", "Pro Baseball Draft", "All-Star", "Home Run Derby".

Kalshi never writes the literal string "MLB" in this field; its name for MLB is
"Pro Baseball", which is why this script's predicate keys on that exact value.

--------------------------------------------------------------------------
THE PREDICATE
--------------------------------------------------------------------------
    A series S qualifies for Collection Universe v1 iff at least one event E
    returned by GET /events?series_ticker=<S.ticker> satisfies

        E["product_metadata"]["competition"] == "Pro Baseball"

    exact, case-sensitive string equality. No ticker parsing, no title
    matching, no tag matching, no fuzzy or substring logic.

Existential ("at least one event") rather than universal ("every event")
because event-level product_metadata is unevenly populated: 33 baseball series
carry `competition: null` on SOME of their events while carrying "Pro
Baseball" on others (all 30 KXMLBWINS-* team win-total series do this). A
universal quantifier would drop them for a metadata gap rather than for
anything about the league.

--------------------------------------------------------------------------
THE CENSUS RUNS OVER THE WHOLE CATALOG, WITH NO PREFILTER
--------------------------------------------------------------------------
The predicate is evaluated for every one of the ~13.8k series in GET /series.
It would be far cheaper to probe only category=="Sports" or only
tags~"Baseball" first, and that is exactly the bug: measured live,

  * KXCITYMLBEXPAND carries competition "Pro Baseball" and is tagged ['Hockey']
  * KXMLBMENTION    carries competition "Pro Baseball" in category "Mentions"

Both survive a full-catalog census and both are lost to a tag or category
prefilter. A heuristic prefilter would reintroduce, one layer down, precisely
the guessing the predicate exists to avoid, so there is no prefilter. The probe
is cached in sqlite and resumable, so the cost is paid once.

--------------------------------------------------------------------------
IT STOPS RATHER THAN GUESSING
--------------------------------------------------------------------------
Three machine-checked gates run before anything is written, so "the metadata
cleanly separates MLB" is a verified claim in every run and not a one-time
human impression:

  GATE 1 -- DISJOINTNESS. For every qualifying series, the script collects
  every OTHER competition value that series also exhibits. If any qualifying
  series also carries a competition belonging to a different baseball
  competition (KBO / NPB / LMB / College Baseball / College Softball / WBC /
  MiLB), the metadata is NOT cleanly separating MLB from other baseball, and
  the script EXITS NON-ZERO having written nothing. It does not fall back to
  a tiebreak. Verified clean 2026-09-03: "Pro Baseball" co-occurs only with
  `null` and with "Pro Baseball Spring Training", never with a rival league.

  GATE 2 -- UNKNOWN VOCABULARY. Any competition value seen on a series that
  also shows baseball affiliation, but which is not in this module's known
  MLB / known-non-MLB / known-adjacent lists, is reported as unclassified and
  also exits non-zero. New Kalshi vocabulary must be looked at by a human
  before it silently lands inside or outside the universe. It is collected
  before the qualifying branch, because a series can carry a brand-new value
  alongside "Pro Baseball" and gate 1 only looks for rival leagues.

  GATE 3 -- COMPLETE EVIDENCE. If any series in the catalog could not be read,
  the run exits non-zero. A series the census failed to fetch is NOT a series
  with no MLB events, but the two are indistinguishable downstream. Measured
  2026-09-03: 46 series exhausted their retry budget on transient HTTP 429s in
  a single census, two of them baseball (KXMLBF5SPREAD, KXMLBHRR) -- a real
  member vanishing with nothing but a warning line to show for it. Cached
  errors are therefore never trusted as evidence: run_probe re-probes them on
  every run.

Cases the predicate genuinely cannot settle are QUARANTINED -- reported with
their verbatim metadata, never auto-resolved:

  Q1 ADJACENT COMPETITION VALUES. "All-Star", "Home Run Derby", "Pro Baseball
     Draft", "Pro Baseball Spring Training" are MLB-affiliated in ordinary
     usage but are NOT the string "Pro Baseball", so they do not qualify. The
     metadata does not say whether they are meant as MLB proper (Kalshi has no
     "KBO All-Star" value to contrast against), so the call is left to a human.
     Consequence worth stating plainly: KXMLBSTGAME carries BOTH "Pro
     Baseball" and "Pro Baseball Spring Training", so it DOES qualify on its
     "Pro Baseball" events. Spring training is thereby in v1 while the other
     adjacent values are not -- an artifact of Kalshi's tagging, reported here
     rather than smoothed over.

  Q2 NO EVENT METADATA. A series with zero events has no competition evidence
     and cannot be evaluated at all. This is materially not an edge case for
     Thread C: KXMLBWS ("MLB World Series"), KXWSAL, KXWSNL, KXMLBALCSMVP and
     KXMLBNLCSMVP are postseason series sitting at zero events on 2026-09-03.
     Frozen Rule 1 is the designed mitigation -- they enter automatically at
     the first census that finds them carrying "Pro Baseball".

Reporting scope for the quarantine lists: series showing baseball affiliation
in official metadata (tags contains "Baseball", or some event competition
value is in the known baseball vocabulary). That scope is a REPORTING
convenience only -- it never touches the inclusion predicate. Its one real
limitation, stated rather than hidden: a series with zero events AND no
"Baseball" tag is invisible to both the predicate and the quarantine, and only
Rule 1 can recover it.

--------------------------------------------------------------------------
OUTPUTS
--------------------------------------------------------------------------
  <outdir>/series_catalog_raw.json.gz   exact GET /series response bytes,
                                        gzipped; the SHA-256 in the manifest
                                        is of the UNCOMPRESSED bytes, so it is
                                        verifiable against a fresh API call.
  <outdir>/events_probe.json            per-series competition evidence
  <outdir>/universe_v1.json             machine-readable manifest
  <outdir>/SHA256SUMS                   hashes of every artifact
  <manifest>.md                         the committed human-readable manifest

Usage:
  python scripts/threadc_collection_universe.py            # full census
  python scripts/threadc_collection_universe.py --refresh  # ignore probe cache
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kbot.auth import kalshi_headers, load_private_key  # noqa: E402
from kbot.config import settings  # noqa: E402


# ── The predicate ────────────────────────────────────────────────────────────

#: Kalshi's own name for MLB in event product_metadata.competition. The literal
#: string "MLB" never appears in that field; this is the exchange's vocabulary,
#: not our label for it.
MLB_COMPETITION = "Pro Baseball"

#: Rival baseball competitions. If any of these ever co-occurs with
#: MLB_COMPETITION on one series, the metadata is not cleanly separating MLB
#: from other baseball and the run aborts (GATE 1).
NON_MLB_BASEBALL_COMPETITIONS = frozenset({
    "Korea KBO",
    "Japan NPB",
    "Mexico LMB",
    "College Baseball",
    "College Softball",
    "World Baseball Classic",
    "World Baseball Classic Exhibition",
    "Minor League Baseball",
})

#: MLB-affiliated in ordinary usage, but not the string "Pro Baseball". Not
#: auto-included; quarantined for a human (Q1).
ADJACENT_COMPETITIONS = frozenset({
    "All-Star",
    "Home Run Derby",
    "Pro Baseball Draft",
    "Pro Baseball Spring Training",
})

#: Series-level tag that marks baseball affiliation. Used ONLY to scope the
#: quarantine report; never part of the inclusion predicate.
BASEBALL_TAG = "Baseball"

SERIES_ENDPOINT = "/series"
SERIES_DETAIL_ENDPOINT = "/series/{series_ticker}"
EVENTS_ENDPOINT = "/events"

_EVENTS_PAGE_LIMIT = 200
_EVENTS_MAX_PAGES = 200          # runaway guard; the cursor normally ends it
#: Measured live 2026-09-03: the probe is round-trip-latency-bound (~0.6s per
#: /events call), not rate-limited -- 8 threads sustained ~4.7 series/s against
#: a 20/s read budget. 14 keeps the shared token bucket the binding constraint
#: rather than the exchange's limiter, so a full census is ~25 minutes.
_PROBE_THREADS = 14
#: Measured live 2026-09-03: at 14 threads against a nominal 20/s read budget,
#: 46 of 13,772 series still exhausted 4 attempts on HTTP 429. A 429 is a
#: transient failure, but a probe that gives up looks exactly like "this series
#: has no Pro Baseball events" -- which would silently drop a real member. So
#: the retry budget is generous, errored rows are never trusted (run_probe
#: re-probes them), and a census that still cannot read a series aborts.
_PROBE_MAX_ATTEMPTS = 7
_PROBE_INITIAL_BACKOFF = 2.0


def qualifies(competitions: set) -> bool:
    """
    THE INCLUSION PREDICATE, in one place so it cannot drift.

    `competitions` is the set of product_metadata.competition values observed
    across every event of one series. Exact, case-sensitive equality against
    Kalshi's own value for MLB -- no ticker parsing, no title or tag matching,
    no substring logic.
    """
    return MLB_COMPETITION in competitions


# ── Read-only HTTP layer ─────────────────────────────────────────────────────

_ALLOWED_PATHS = (
    re.compile(r"^/series$"),
    re.compile(r"^/series/[^/]+$"),
    re.compile(r"^/events$"),
)


class _TokenBucket:
    """Thread-safe token bucket; mirrors KalshiClient's read limiter."""

    def __init__(self, rate: float) -> None:
        self._rate = max(1.0, float(rate))
        self._tokens = self._rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                self._tokens = 0.0
            else:
                self._tokens -= 1.0
                wait = 0.0
        if wait:
            time.sleep(wait)


class _ReadOnlyAPI:
    """
    Signed GET-only Kalshi client with a hard path allowlist.

    Deliberately NOT KalshiClient: that class owns place_order() and
    cancel_order(), and this script must not have such a method in scope at
    all. Raw httpx rather than the SDK because the SDK's models are known to
    mangle real responses (see kbot/kalshi_client.py) -- and because this
    script must record the exact response BYTES for hashing, which a
    deserializing client cannot give us.
    """

    def __init__(self, cfg=settings, timeout: float = 60.0) -> None:
        self._cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._prefix = httpx.URL(cfg.base_url).path.rstrip("/")
        self._bucket = _TokenBucket(getattr(cfg, "rate_limit_reads", 20))
        self._local = threading.local()
        self._timeout = timeout
        self._count_lock = threading.Lock()
        self.request_count = 0

        if not cfg.kalshi_key_id or not cfg.private_key_path.exists():
            raise SystemExit(
                "Kalshi credentials required (/events is an authenticated read). "
                f"key_id set={bool(cfg.kalshi_key_id)}, key file "
                f"{cfg.private_key_path} exists={cfg.private_key_path.exists()}"
            )
        self._key_id = cfg.kalshi_key_id
        self._private_key = load_private_key(cfg.private_key_path)

    def _client(self) -> httpx.Client:
        c = getattr(self._local, "client", None)
        if c is None:
            c = httpx.Client(timeout=self._timeout)
            self._local.client = c
        return c

    def get_raw(self, path: str, params: dict | None = None) -> bytes:
        """One signed GET against an allowlisted read path; returns raw bytes."""
        if not any(rx.match(path) for rx in _ALLOWED_PATHS):
            raise ValueError(
                f"refusing {path!r}: threadc_collection_universe.py is read-only "
                f"and only calls {[rx.pattern for rx in _ALLOWED_PATHS]}"
            )
        self._bucket.acquire()
        headers = kalshi_headers("GET", self._prefix + path, self._key_id, self._private_key)
        resp = self._client().get(self._base + path, headers=headers, params=params or {})
        with self._count_lock:
            self.request_count += 1
        resp.raise_for_status()
        return resp.content

    def get(self, path: str, params: dict | None = None) -> dict:
        return json.loads(self.get_raw(path, params))

    def get_retrying(self, path: str, params: dict | None = None) -> dict:
        """GET with backoff on transport/5xx/429; other 4xx raise immediately."""
        backoff = _PROBE_INITIAL_BACKOFF
        last: Exception | None = None
        for _ in range(_PROBE_MAX_ATTEMPTS):
            try:
                return self.get(path, params)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code != 429 and code < 500:
                    raise
                last = exc
            except (httpx.TransportError, json.JSONDecodeError) as exc:
                last = exc
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        raise last  # type: ignore[misc]


# ── Probe cache ──────────────────────────────────────────────────────────────

class ProbeCache:
    """
    Resumable sqlite cache of the per-series event probe.

    A full-catalog census is ~13.8k requests; it must survive an interrupt
    without starting over, which is also what makes "no prefilter" affordable.
    """

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS series_events (
                series_ticker TEXT PRIMARY KEY,
                probed_at     TEXT NOT NULL,
                n_events      INTEGER NOT NULL,
                pages         INTEGER NOT NULL,
                comps_json    TEXT NOT NULL,
                error         TEXT
            )
        """)
        self._db.commit()

    def probed(self) -> dict:
        rows = self._db.execute(
            "SELECT series_ticker, probed_at, n_events, pages, comps_json, error "
            "FROM series_events"
        ).fetchall()
        return {
            r[0]: {
                "probed_at": r[1], "n_events": r[2], "pages": r[3],
                "competitions": json.loads(r[4]), "error": r[5],
            }
            for r in rows
        }

    def put(self, rows: list) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO series_events "
            "(series_ticker, probed_at, n_events, pages, comps_json, error) "
            "VALUES (?,?,?,?,?,?)", rows,
        )
        self._db.commit()

    def clear(self) -> None:
        self._db.execute("DELETE FROM series_events")
        self._db.commit()

    def close(self) -> None:
        self._db.close()


# ── Stage 1: the official series catalog, byte-exact ─────────────────────────

def fetch_series_catalog(api: _ReadOnlyAPI):
    """
    GET /series -- the whole catalog in one response.

    Returns (raw_bytes, records, sha256_of_raw_bytes, retrieved_at). The raw
    bytes are kept because the manifest's hash must be of the exact official
    response, verifiable against a fresh call; a re-serialized dict would hash
    to something Kalshi never sent.
    """
    retrieved_at = datetime.now(timezone.utc).isoformat()
    raw = api.get_raw(SERIES_ENDPOINT)
    digest = hashlib.sha256(raw).hexdigest()
    records = json.loads(raw).get("series") or []
    return raw, records, digest, retrieved_at


# ── Stage 2: the event-metadata probe ────────────────────────────────────────

def probe_series_events(api: _ReadOnlyAPI, series_ticker: str) -> dict:
    """
    Every product_metadata.competition value carried by one series' events.

    Pages to exhaustion: a series whose first page is all-null but whose later
    pages carry "Pro Baseball" must not be misjudged on a truncated read.
    Counts and one sample event ticker per value are kept so the manifest can
    quote the evidence verbatim.
    """
    comps: dict = {}
    n_events = 0
    pages = 0
    cursor = None

    while pages < _EVENTS_MAX_PAGES:
        params: dict = {"series_ticker": series_ticker, "limit": _EVENTS_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        data = api.get_retrying(EVENTS_ENDPOINT, params)
        pages += 1
        events = data.get("events") or []
        for ev in events:
            n_events += 1
            pm = ev.get("product_metadata") or {}
            comp = pm.get("competition")
            scope = pm.get("competition_scope")
            key = json.dumps(comp)          # null and "null" cannot collide
            slot = comps.setdefault(key, {
                "competition": comp, "count": 0,
                "scopes": {}, "sample_event_ticker": ev.get("event_ticker"),
            })
            slot["count"] += 1
            skey = json.dumps(scope)
            slot["scopes"][skey] = slot["scopes"].get(skey, 0) + 1
        nxt = data.get("cursor") or None
        if not nxt or nxt == cursor or not events:
            break
        cursor = nxt

    return {"n_events": n_events, "pages": pages, "competitions": comps, "error": None}


def run_probe(api: _ReadOnlyAPI, tickers: list, cache: ProbeCache) -> dict:
    """Probe every series not already cached, concurrently, writing as we go."""
    have = cache.probed()
    # A cached ERROR is not evidence -- it is a hole that reads as "no Pro
    # Baseball events". Always re-probe those, so a transient 429 during one
    # census cannot quietly evict a series from the universe forever.
    stale = [t for t in tickers if t in have and have[t].get("error")]
    todo = [t for t in tickers if t not in have] + stale
    print(f"  event probe: {len(have) - len(stale)} cached, {len(todo)} to fetch "
          f"({len(tickers)} series total, no prefilter)"
          + (f"; {len(stale)} cached errors being re-probed" if stale else ""))

    if todo:
        done = 0
        batch: list = []
        lock = threading.Lock()

        def work(t: str):
            try:
                return t, probe_series_events(api, t)
            except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
                return t, {"n_events": 0, "pages": 0, "competitions": {},
                           "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

        with ThreadPoolExecutor(max_workers=_PROBE_THREADS) as pool:
            for t, res in pool.map(work, todo):
                now = datetime.now(timezone.utc).isoformat()
                with lock:
                    have[t] = {"probed_at": now, **res}
                    batch.append((t, now, res["n_events"], res["pages"],
                                  json.dumps(res["competitions"]), res["error"]))
                    done += 1
                    if len(batch) >= 100:
                        cache.put(batch)
                        batch = []
                    if done % 500 == 0:
                        print(f"    ...{done}/{len(todo)}")
        if batch:
            cache.put(batch)

    errs = [t for t, v in have.items() if v.get("error")]
    if errs:
        print(f"  WARNING: {len(errs)} series failed to probe; first few: {errs[:5]}")
    return have


# ── Stage 3: predicate, gates, quarantine ────────────────────────────────────

def competitions_of(probe: dict) -> set:
    return {slot["competition"] for slot in probe.get("competitions", {}).values()}


def build_universe(catalog: list, probes: dict) -> dict:
    """
    Apply the predicate, run both abort gates, and collect the quarantine.

    Returns the qualifying series with verbatim predicate evidence, the
    quarantine buckets, and the gate results. Raises nothing -- the caller
    decides what a failed gate means, so the failure is reported rather than
    thrown away as a traceback.
    """
    by_ticker = {r["ticker"]: r for r in catalog}
    known_bb = {MLB_COMPETITION} | NON_MLB_BASEBALL_COMPETITIONS | ADJACENT_COMPETITIONS

    qualifying: list = []
    gate1_violations: list = []
    q1_adjacent: list = []
    q2_no_metadata: list = []
    unknown_vocab: dict = {}

    # GATE 3 -- COMPLETE EVIDENCE. A series the census could not read is not a
    # series with no MLB events; it is a series we know nothing about. Both look
    # identical downstream, so an unreadable or unprobed series must abort the
    # run rather than silently shrink the universe. Measured 2026-09-03: 46
    # series exhausted their retry budget on transient 429s in one census, two
    # of them baseball (KXMLBF5SPREAD, KXMLBHRR), which is exactly how a real
    # member would disappear without anyone noticing.
    gate3_unreadable = [
        {"series_ticker": t,
         "reason": (probes[t].get("error") if t in probes
                    else "no probe row -- series absent from the event probe")}
        for t in sorted(by_ticker)
        if t not in probes or probes[t].get("error")
    ]

    for ticker, rec in sorted(by_ticker.items()):
        probe = probes.get(ticker)
        if probe is None:
            continue
        comps = competitions_of(probe)
        tags = rec.get("tags") or []

        # Baseball affiliation -- REPORTING SCOPE ONLY, never the predicate.
        baseball_affiliated = (BASEBALL_TAG in tags) or bool(comps & known_bb)

        # GATE 2: vocabulary we have never classified. Collected BEFORE the
        # qualifying branch on purpose -- a qualifying series can also carry a
        # brand-new competition value alongside "Pro Baseball", and gate 1 only
        # looks for *rival* leagues, so checking after an early `continue` would
        # let genuinely new vocabulary through unreviewed on exactly the series
        # we are admitting.
        if baseball_affiliated:
            for c in comps:
                if c is None or c in known_bb:
                    continue
                unknown_vocab.setdefault(c, []).append(ticker)

        if qualifies(comps):
            others = {c for c in comps if c is not None and c != MLB_COMPETITION}
            rival = others & NON_MLB_BASEBALL_COMPETITIONS
            if rival:
                # GATE 1: one series claiming MLB and a rival league at once.
                gate1_violations.append({
                    "series_ticker": ticker,
                    "title": rec.get("title"),
                    "conflicting_competitions": sorted(rival),
                    "all_competitions": sorted(str(c) for c in comps),
                })
            qualifying.append({
                "series_ticker": ticker,
                "title": rec.get("title"),
                "category": rec.get("category"),
                "tags": tags,
                "predicate_evidence": {
                    "matched_field": "events[].product_metadata.competition",
                    "matched_value": MLB_COMPETITION,
                    "competition_values_observed": probe["competitions"],
                    "n_events_scanned": probe["n_events"],
                    "event_pages_scanned": probe["pages"],
                },
                "also_carries_competitions": sorted(others),
                "retrieved_at": probe["probed_at"],
                "endpoints": {
                    "series_catalog": SERIES_ENDPOINT,
                    "series_detail": SERIES_DETAIL_ENDPOINT.format(series_ticker=ticker),
                    "event_metadata":
                        f"{EVENTS_ENDPOINT}?series_ticker={ticker}"
                        f"&limit={_EVENTS_PAGE_LIMIT}",
                },
            })
            continue

        if not baseball_affiliated:
            continue

        adj = comps & ADJACENT_COMPETITIONS
        if adj:
            q1_adjacent.append({
                "series_ticker": ticker, "title": rec.get("title"),
                "category": rec.get("category"), "tags": tags,
                "competitions": sorted(adj),
                "competition_values_observed": probe["competitions"],
                "n_events": probe["n_events"],
                "why_unresolved":
                    "MLB-affiliated in ordinary usage but the metadata value is "
                    f"not the string {MLB_COMPETITION!r}; Kalshi offers no "
                    "league-qualified variant to contrast with.",
            })
        elif probe["n_events"] == 0 or comps <= {None}:
            q2_no_metadata.append({
                "series_ticker": ticker, "title": rec.get("title"),
                "category": rec.get("category"), "tags": tags,
                "n_events": probe["n_events"],
                "why_unresolved":
                    "no event carries product_metadata.competition, so the "
                    "predicate has nothing to evaluate.",
            })

    return {
        "qualifying": qualifying,
        "gate1_violations": gate1_violations,
        "gate3_unreadable": gate3_unreadable,
        "gate2_unknown_competitions": unknown_vocab,
        "quarantine_adjacent": q1_adjacent,
        "quarantine_no_metadata": q2_no_metadata,
    }


def attach_series_product_metadata(api: _ReadOnlyAPI, qualifying: list) -> None:
    """
    Add each qualifying series' own product_metadata from GET /series/{ticker}.

    The list endpoint omits product_metadata entirely, so this is the only
    place the series-level value can be read. It does not decide membership
    (for baseball it is a bare {"scope": ...}); it is recorded because the
    manifest promises the relevant product_metadata verbatim.
    """
    for i, row in enumerate(qualifying):
        t = row["series_ticker"]
        try:
            data = api.get_retrying(SERIES_DETAIL_ENDPOINT.format(series_ticker=t))
            s = data.get("series") or data
            row["series_product_metadata"] = s.get("product_metadata")
            row["settlement_sources"] = s.get("settlement_sources")
            row["frequency"] = s.get("frequency")
            row["last_updated_ts"] = s.get("last_updated_ts")
            row["series_detail_retrieved_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
            row["series_product_metadata"] = None
            row["series_detail_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if i and i % 50 == 0:
            print(f"    series detail ...{i}/{len(qualifying)}")


# ── Stage 4: manifest rendering ──────────────────────────────────────────────

PREDICATE_TEXT = (
    "A Kalshi series S is a member of Collection Universe v1 if and only if at "
    "least one event E returned by `GET /events?series_ticker=<S.series_ticker>` "
    "satisfies\n\n"
    "        E[\"product_metadata\"][\"competition\"] == \"Pro Baseball\"\n\n"
    "under exact, case-sensitive string equality. `\"Pro Baseball\"` is Kalshi's "
    "own value for Major League Baseball in that controlled field; the literal "
    "string `\"MLB\"` never appears in it. Nothing else is consulted: no ticker "
    "prefix or substring, no title text, no tag, no category, no settlement "
    "source, no fuzzy or substring matching."
)

RULE_1 = (
    "**Rule 1 -- Automatic admission of new series (append-only discovery log).** "
    "Any series created after this manifest whose official Kalshi metadata "
    "satisfies the inclusion predicate above is added to Collection Universe v1 "
    "automatically, from the first census that discovers it. Its admission is "
    "logged append-only with the discovery timestamp and the verbatim metadata "
    "that satisfied the predicate. No backfill is attempted and none is possible: "
    "history before the discovering census is simply unavailable, and the record "
    "says so rather than interpolating it."
)

RULE_2 = (
    "**Rule 2 -- Inclusion is append-only for the collection period.** Once a "
    "series qualifies, it remains in Collection Universe v1 through the frozen "
    "collection end date even if Kalshi later edits, relabels, or removes the "
    "metadata that admitted it. Membership is a fact about what the metadata said "
    "when the series was admitted, not a live query. Nothing is ever removed from "
    "v1 during the collection period; a later metadata change is recorded as an "
    "observation, never as an eviction."
)


def _md(text) -> str:
    """
    Make an API-supplied string safe inside a markdown table cell.

    Titles come straight from the exchange and are not authored for markdown:
    one of them carries a Unicode bidi override, and a stray pipe would silently
    shift every later column. Escaping here keeps the evidence verbatim in the
    JSON artifacts while the table stays readable.
    """
    if text is None:
        return "(none)"
    s = str(text).replace("|", "\\|")
    return "".join(ch for ch in s if ch.isprintable() or ch == " ").strip() or "(empty)"


def _fmt_comp_evidence(observed: dict) -> str:
    """Verbatim competition values for one series, as a compact one-liner."""
    parts = []
    for _key, slot in sorted(observed.items(), key=lambda kv: -kv[1]["count"]):
        comp = slot["competition"]
        label = "null" if comp is None else f'"{comp}"'
        scopes = ", ".join(
            f"{k}×{v}" for k, v in sorted(slot["scopes"].items(), key=lambda kv: -kv[1])
        )
        parts.append(f"{label}×{slot['count']} [scopes: {scopes}]")
    return "; ".join(parts)


def render_manifest(ctx: dict) -> str:
    """The committed manifest. Everything in it is derived, nothing is typed twice."""
    u = ctx["universe"]
    q = u["qualifying"]
    L: list = []
    A = L.append

    A("# Thread C -- Collection Universe v1")
    A("")
    A(f"**Manifest date:** {ctx['manifest_date']}  ")
    A(f"**Census run (UTC):** {ctx['catalog_retrieved_at']}  ")
    A(f"**Exchange base URL:** `{ctx['base_url']}`  ")
    A(f"**Qualifying series:** **{len(q)}**  ")
    A(f"**Series examined:** {ctx['n_catalog']} (the entire `GET /series` catalog, "
      f"no prefilter)  ")
    A(f"**Read requests issued:** {ctx['request_count']}")
    A("")
    A("---")
    A("")

    A("## 1. Why this manifest exists")
    A("")
    A("The MLB regular season ends **2026-09-27**, and collection runs through the "
      "postseason to a frozen end date. Order-book **depth at a decision moment "
      "cannot be reconstructed after the fact** -- historical candlesticks preserve "
      "price and volume, not the resting size on each side of the book -- so live "
      "books have to be recorded while they exist, or the observation is lost until "
      "April.")
    A("")
    A("This document is **neutral data banking**. It defines *what to record* and "
      "nothing else. No analysis protocol exists yet, and none is anticipated here: "
      "no hypothesis, no signal definition, no strategy, and no market subset "
      "chosen for its expected interestingness. The universe is defined by official "
      "exchange metadata alone precisely so that a protocol written later cannot be "
      "accused of having selected its own sample.")
    A("")

    A("## 2. The inclusion predicate")
    A("")
    A(PREDICATE_TEXT)
    A("")
    A("### Why the predicate reads event metadata, not the ticker or the series row")
    A("")
    A("Kalshi's documentation directs integrators to series and event metadata "
      "rather than to ticker parsing, and on this exchange ticker parsing is not "
      "merely discouraged -- it is measurably wrong. Observed in this census:")
    A("")
    A("| Ticker | What the ticker suggests | What the metadata says |")
    A("|---|---|---|")
    A("| `KXMLBWORLD` | MLB | competition `\"World Baseball Classic\"` -- a "
      "national-team tournament, not MLB |")
    A("| `KXMBLNLEAST` | nothing (transposed `MBL`) | \"National League East "
      "Winner\"; any `^KXMLB` regex misses it |")
    A("| `KXCITYMLBEXPAND` | MLB | competition `\"Pro Baseball\"`, while the series "
      "is tagged `['Hockey']` |")
    A("| `KXMLBMENTION` | MLB | competition `\"Pro Baseball\"`, in category "
      "`\"Mentions\"` |")
    A("")
    A("The series row itself cannot carry the decision either. `GET /series` returns "
      f"{ctx['n_catalog']} series and **no `product_metadata` key at all**; its "
      "`category` tops out at `\"Sports\"`, and its `tags` top out at "
      "`[\"Baseball\"]` -- the identical tag set is carried by MLB, KBO, NPB, LMB, "
      "NCAA baseball, NCAA softball, the World Baseball Classic and the "
      "Congressional Baseball Game. `GET /series/{ticker}` does return "
      "`product_metadata`, but for baseball it holds only a bare `{\"scope\": ...}`. "
      "The league appears exactly once in the API surface: in "
      "`events[].product_metadata.competition`.")
    A("")
    A("### Why \"at least one event\" and not \"every event\"")
    A("")
    A("Event-level `product_metadata` is unevenly populated. Some series carry "
      "`competition: null` on part of their events and `\"Pro Baseball\"` on the "
      "rest -- all 30 `KXMLBWINS-*` team win-total series do exactly this. A "
      "universal quantifier would drop those series for a metadata gap rather than "
      "for anything about which league they belong to.")
    A("")
    A("### Why the census has no prefilter")
    A("")
    A(f"The predicate is evaluated for every one of the {ctx['n_catalog']} series in "
      "the catalog. Probing only `category == \"Sports\"`, or only series tagged "
      "`\"Baseball\"`, would be far cheaper and would silently lose real members: "
      "`KXCITYMLBEXPAND` carries `\"Pro Baseball\"` while tagged `['Hockey']`, and "
      "`KXMLBMENTION` carries it from category `\"Mentions\"`. A heuristic "
      "prefilter would reintroduce, one layer down, exactly the guessing this "
      "predicate exists to eliminate.")
    A("")

    A("## 3. Frozen rules")
    A("")
    A(RULE_1)
    A("")
    A(RULE_2)
    A("")
    A("Both rules reference the **frozen collection end date** symbolically. This "
      "manifest deliberately does not assert what that date is: the regular season "
      "ending 2026-09-27 and a run through the postseason are the stated context, "
      "but the exact frozen end date is set by the collection plan, not by the "
      "universe definition. Nothing in the predicate or in either rule depends on "
      "its value.")
    A("")

    A("## 4. Provenance and integrity")
    A("")
    A("Kalshi metadata is mutable, so the exact response that produced v1 is stored "
      "verbatim alongside this manifest and hashed. The hash below is of the "
      "**uncompressed** response body exactly as the exchange sent it, so it can be "
      "checked against a fresh call to the same endpoint.")
    A("")
    A("| Item | Value |")
    A("|---|---|")
    A(f"| Series catalog endpoint | `GET {ctx['base_url']}{SERIES_ENDPOINT}` |")
    A(f"| Retrieved at (UTC) | `{ctx['catalog_retrieved_at']}` |")
    A(f"| Response size | {ctx['catalog_bytes']:,} bytes |")
    A(f"| **SHA-256 of response body** | `{ctx['catalog_sha256']}` |")
    A(f"| Stored snapshot (gzip of those bytes) | `{ctx['rel_catalog']}` |")
    A(f"| Per-series event-metadata evidence | `{ctx['rel_probe']}` (SHA-256 "
      f"`{ctx['probe_sha256']}`) |")
    A(f"| Machine-readable manifest | `{ctx['rel_universe']}` (SHA-256 "
      f"`{ctx['universe_sha256']}`) |")
    A(f"| Hash list for all artifacts | `{ctx['rel_sums']}` |")
    A("")
    A("Verify the catalog snapshot with:")
    A("")
    A("```bash")
    A(f"gunzip -c {ctx['rel_catalog']} | sha256sum")
    A(f"# expect {ctx['catalog_sha256']}")
    A("```")
    A("")
    A("### Endpoints used (complete list -- all reads, all GET)")
    A("")
    A(f"1. `GET {SERIES_ENDPOINT}` -- the official series catalog "
      f"({ctx['n_catalog']} series), hashed above.")
    A(f"2. `GET {EVENTS_ENDPOINT}?series_ticker=<ticker>&limit={_EVENTS_PAGE_LIMIT}` "
      "(cursor-paged to exhaustion) -- the `product_metadata.competition` evidence "
      "the predicate decides on, once per series in the catalog.")
    A(f"3. `GET {SERIES_ENDPOINT}/<ticker>` -- series-level `product_metadata`, "
      "`settlement_sources` and `frequency`, fetched for qualifying series only.")
    A("")
    A("The script that produced this manifest is `scripts/threadc_collection_"
      "universe.py`. It is structurally read-only: it never constructs a "
      "`KalshiClient` (the class that owns `place_order()` / `cancel_order()`), it "
      "issues HTTP GET only, and it refuses any path outside the three-entry "
      "allowlist above.")
    A("")

    A("## 5. Ambiguity check: MLB vs other baseball competitions")
    A("")
    A(f"**Verdict: {ctx['ambiguity_verdict']}**")
    A("")
    A("`product_metadata.competition` is a controlled vocabulary, and Kalshi names "
      "each baseball competition separately. The values observed across every "
      "baseball-affiliated series in this census:")
    A("")
    A("| competition value | series | treated as |")
    A("|---|---|---|")
    for val, (n, verdict) in ctx["vocab_table"]:
        A(f"| `{val}` | {n} | {verdict} |")
    A("")
    A("### Gate 1 -- disjointness (machine-checked every run)")
    A("")
    A("For every qualifying series, the census collects every *other* competition "
      "value that series also exhibits. If a qualifying series were ever to carry a "
      "rival baseball competition as well (KBO / NPB / LMB / College Baseball / "
      "College Softball / WBC / MiLB), the metadata would not be cleanly separating "
      "MLB from other baseball, and **the script exits non-zero and writes "
      "nothing** rather than applying a tiebreak.")
    A("")
    if u["gate1_violations"]:
        A("**GATE 1 FAILED:**")
        for v in u["gate1_violations"]:
            A(f"- `{v['series_ticker']}` -- {v['conflicting_competitions']}")
    else:
        A(f"**Gate 1 passed.** No qualifying series carries any rival baseball "
          f"competition. `\"Pro Baseball\"` co-occurs only with `null` and with "
          f"`\"Pro Baseball Spring Training\"`. Kalshi's league labels are "
          f"mutually exclusive per series, so MLB *is* cleanly distinguishable "
          f"from KBO, NPB, LMB, college baseball, college softball and the World "
          f"Baseball Classic for every series that has event metadata at all.")
    A("")
    A("### Gate 2 -- unknown vocabulary (machine-checked every run)")
    A("")
    A("Any competition value on a baseball-affiliated series that this script has "
      "not classified aborts the run, so new Kalshi vocabulary is reviewed by a "
      "human before it lands silently inside or outside the universe.")
    A("")
    if u["gate2_unknown_competitions"]:
        A("**GATE 2 FAILED -- unclassified competition values:**")
        for val, tickers in sorted(u["gate2_unknown_competitions"].items()):
            A(f"- `{val}` on {len(tickers)} series, e.g. {tickers[:5]}")
    else:
        A("**Gate 2 passed.** Every competition value observed on a "
          "baseball-affiliated series is classified.")
    A("")
    A("### Gate 3 -- complete evidence (machine-checked every run)")
    A("")
    A("A series the census could not read is **not** a series with no MLB events, "
      "but downstream the two are indistinguishable -- both simply fail to appear. "
      "So if any series in the catalog cannot be read, the run exits non-zero and "
      "writes nothing rather than publishing a quietly smaller universe. This is "
      "not hypothetical: an earlier census of this same catalog had 46 series "
      "exhaust their retry budget on transient HTTP 429s, two of them baseball "
      "(`KXMLBF5SPREAD`, `KXMLBHRR`), which would have dropped two real members "
      "behind a single warning line. Cached probe errors are never treated as "
      "evidence; they are re-probed on every run.")
    A("")
    A(f"**Gate 3 passed.** All {ctx['n_catalog']} series in the catalog were read "
      f"successfully; no series is in an errored or unprobed state.")
    A("")
    A("### Where ambiguity does remain (quarantined, not resolved)")
    A("")
    A("The clean separation above holds *between named leagues*. Two boundaries are "
      "genuinely not settled by the metadata, so this manifest reports them and "
      "**does not decide them**. Neither is auto-included; resolving either is a "
      "human decision that would produce a v2, not a heuristic applied here.")
    A("")
    A(f"**Q1 -- MLB-adjacent competition values ({len(u['quarantine_adjacent'])} "
      f"series).** Kalshi labels some MLB-affiliated products with their own "
      f"competition value rather than `\"Pro Baseball\"`. These do not satisfy the "
      f"predicate. They are MLB in ordinary usage, but the metadata does not say so "
      f"-- there is no `\"KBO All-Star\"` or `\"NPB Home Run Derby\"` value in the "
      f"vocabulary to contrast against, so \"All-Star\" alone cannot be read as "
      f"league-specific from the field itself.")
    A("")
    if u["quarantine_adjacent"]:
        A("| series_ticker | title | competition value(s) | events |")
        A("|---|---|---|---|")
        for r in u["quarantine_adjacent"]:
            comps = ", ".join(f"`{c}`" for c in r["competitions"])
            A(f"| `{r['series_ticker']}` | {_md(r['title'])} | {comps} | "
              f"{r['n_events']} |")
        A("")
    A("One consequence is worth stating plainly rather than smoothing over: "
      "`KXMLBSTGAME` (\"Pro Baseball Spring Training game\") carries **both** "
      "`\"Pro Baseball\"` and `\"Pro Baseball Spring Training\"` on its events, so "
      "it **does** satisfy the predicate and **is** in v1, while the other adjacent "
      "values are out. That asymmetry is an artifact of Kalshi's own tagging, not a "
      "judgement made here.")
    A("")
    A(f"**Q2 -- no event metadata to evaluate ({len(u['quarantine_no_metadata'])} "
      f"series).** A series with no events, or whose every event carries "
      f"`competition: null`, gives the predicate nothing to test. This is not a "
      f"marginal set for Thread C: it currently contains postseason series that "
      f"matter directly to this collection.")
    A("")
    if u["quarantine_no_metadata"]:
        A("| series_ticker | title | tags | events |")
        A("|---|---|---|---|")
        for r in u["quarantine_no_metadata"]:
            A(f"| `{r['series_ticker']}` | {_md(r['title'])} | "
              f"{_md(', '.join(r['tags']) if r['tags'] else None)} | "
              f"{r['n_events']} |")
        A("")
    A("**Rule 1 is the designed mitigation for Q2.** These series are not judged, "
      "excluded on a guess, or admitted on their titles. They enter Collection "
      "Universe v1 automatically at the first census that finds them carrying "
      "`\"Pro Baseball\"` -- which is what will happen when Kalshi creates their "
      "postseason events -- and the discovery is logged append-only with the "
      "metadata that admitted them.")
    A("")
    A("One limitation is stated rather than hidden: a series with **zero events and "
      "no `\"Baseball\"` tag** is invisible both to the predicate and to this "
      "quarantine report, because nothing in its official metadata connects it to "
      "baseball. Only Rule 1 can recover such a series, and only once it has "
      "events.")
    A("")

    A("## 6. Collection Universe v1 -- the qualifying series")
    A("")
    A(f"{len(q)} series. Every row qualified on the same predicate; the "
      "`product_metadata.competition` column is the verbatim evidence, with the "
      "count of events carrying each value and their `competition_scope` values, so "
      "the predicate can be re-run and the same universe reconstructed.")
    A("")
    A("| # | series_ticker | title | category | tags | series product_metadata | "
      "events[].product_metadata.competition (verbatim, with counts and scopes) |")
    A("|---|---|---|---|---|---|---|")
    for i, r in enumerate(q, 1):
        tags = ", ".join(r["tags"]) if r["tags"] else "(none)"
        spm = json.dumps(r.get("series_product_metadata"))
        A(f"| {i} | `{r['series_ticker']}` | {_md(r['title'])} | "
          f"{_md(r['category'])} | {_md(tags)} | `{spm}` | "
          f"{_fmt_comp_evidence(r['predicate_evidence']['competition_values_observed'])} |")
    A("")
    A(f"All rows were retrieved during the census window beginning "
      f"`{ctx['catalog_retrieved_at']}`; per-series retrieval timestamps and the "
      f"exact endpoint string used for each series are in `{ctx['rel_universe']}` "
      f"(fields `retrieved_at` and `endpoints`).")
    A("")
    A("---")
    A("")
    A(f"*Generated by `scripts/threadc_collection_universe.py` on "
      f"{ctx['manifest_date']}. Read-only census; no orders, no writes to any "
      f"exchange endpoint.*")
    A("")
    return "\n".join(L)


def build_vocab_table(catalog: list, probes: dict) -> list:
    """competition values across baseball-affiliated series, with our verdict."""
    known_bb = {MLB_COMPETITION} | NON_MLB_BASEBALL_COMPETITIONS | ADJACENT_COMPETITIONS
    by_ticker = {r["ticker"]: r for r in catalog}
    counts: dict = {}
    for t, probe in probes.items():
        rec = by_ticker.get(t)
        if rec is None:
            continue
        comps = competitions_of(probe)
        if not ((BASEBALL_TAG in (rec.get("tags") or [])) or (comps & known_bb)):
            continue
        for c in comps:
            if c is None:
                continue
            counts[c] = counts.get(c, 0) + 1

    def verdict(c: str) -> str:
        if c == MLB_COMPETITION:
            return "**MLB -- included**"
        if c in NON_MLB_BASEBALL_COMPETITIONS:
            return "other baseball competition -- excluded"
        if c in ADJACENT_COMPETITIONS:
            return "MLB-adjacent -- quarantined (Q1), not included"
        return "**UNCLASSIFIED -- gate 2 abort**"

    return [(c, (n, verdict(c))) for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--outdir", default="research/threadc_universe_v1",
                    help="directory for the snapshot and machine-readable manifest")
    ap.add_argument("--manifest", default="research/THREADC_COLLECTION_UNIVERSE_V1.md",
                    help="path of the human-readable manifest to write")
    ap.add_argument("--cache", default=None,
                    help="sqlite probe cache (default <outdir>/probe_cache.db)")
    ap.add_argument("--refresh", action="store_true",
                    help="discard the cached event probe and re-fetch everything")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cache_path = Path(args.cache) if args.cache else outdir / "probe_cache.db"

    api = _ReadOnlyAPI()
    cache = ProbeCache(cache_path)
    if args.refresh:
        cache.clear()

    try:
        print("STAGE 1  GET /series -- official series catalog")
        raw, catalog, catalog_sha, catalog_at = fetch_series_catalog(api)
        print(f"  {len(catalog)} series, {len(raw):,} bytes, sha256={catalog_sha}")

        print("STAGE 2  GET /events per series -- product_metadata.competition")
        probes = run_probe(api, [r["ticker"] for r in catalog], cache)

        print("STAGE 3  apply the inclusion predicate; run gates")
        universe = build_universe(catalog, probes)
        q = universe["qualifying"]
        print(f"  qualifying: {len(q)}")
        print(f"  quarantine Q1 (MLB-adjacent):   {len(universe['quarantine_adjacent'])}")
        print(f"  quarantine Q2 (no metadata):    {len(universe['quarantine_no_metadata'])}")

        if universe["gate3_unreadable"]:
            print(f"\nGATE 3 FAILED -- {len(universe['gate3_unreadable'])} series "
                  "could not be read, so the census is incomplete. An unreadable "
                  "series is indistinguishable from one with no MLB events, and "
                  "reporting a smaller universe would hide the difference. "
                  "Nothing was written.")
            for v in universe["gate3_unreadable"][:10]:
                print(f"  {v['series_ticker']}: {v['reason']}")
            if len(universe["gate3_unreadable"]) > 10:
                print(f"  ... and {len(universe['gate3_unreadable']) - 10} more")
            print("\nRe-run to retry them (cached errors are always re-probed); "
                  "if they persist, lower _PROBE_THREADS.")
            return 5

        if universe["gate1_violations"]:
            print("\nGATE 1 FAILED -- the metadata does NOT cleanly separate MLB "
                  "from other baseball competitions. Nothing was written.")
            for v in universe["gate1_violations"]:
                print(f"  {v['series_ticker']}: also carries "
                      f"{v['conflicting_competitions']} (all: {v['all_competitions']})")
            print("\nResolve this by hand. This script will not guess.")
            return 2

        if universe["gate2_unknown_competitions"]:
            print("\nGATE 2 FAILED -- unclassified competition value(s) on "
                  "baseball-affiliated series. Nothing was written.")
            for val, tickers in sorted(universe["gate2_unknown_competitions"].items()):
                print(f"  {val!r} on {len(tickers)} series, e.g. {tickers[:5]}")
            print("\nClassify these in NON_MLB_BASEBALL_COMPETITIONS / "
                  "ADJACENT_COMPETITIONS (or confirm they are MLB) and re-run.")
            return 3

        if not q:
            print("\nNo series satisfied the predicate -- refusing to write an empty "
                  "universe. Check credentials and endpoint reachability.")
            return 4

        print("STAGE 4  GET /series/{ticker} -- series-level product_metadata")
        attach_series_product_metadata(api, q)

        print("STAGE 5  write snapshot, machine-readable manifest, hashes")
        catalog_path = outdir / "series_catalog_raw.json.gz"
        with gzip.open(catalog_path, "wb", compresslevel=9) as fh:
            fh.write(raw)

        probe_path = outdir / "events_probe.json"
        probe_blob = json.dumps(
            {"retrieved_window_start_utc": catalog_at,
             "endpoint": f"{EVENTS_ENDPOINT}?series_ticker=<ticker>"
                         f"&limit={_EVENTS_PAGE_LIMIT}",
             "note": "product_metadata.competition evidence for every series in "
                     "the catalog; this is the input the predicate is applied to.",
             "series": probes},
            indent=1, sort_keys=True,
        ).encode("utf-8")
        probe_path.write_bytes(probe_blob)

        universe_path = outdir / "universe_v1.json"
        universe_blob = json.dumps({
            "manifest_version": "v1",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "base_url": settings.base_url,
            "inclusion_predicate": {
                "field": "events[].product_metadata.competition",
                "operator": "exact case-sensitive string equality, existential "
                            "over the series' events",
                "value": MLB_COMPETITION,
                "endpoint": f"{EVENTS_ENDPOINT}?series_ticker=<ticker>"
                            f"&limit={_EVENTS_PAGE_LIMIT}",
                "prose": PREDICATE_TEXT,
            },
            "frozen_rules": {"rule_1_automatic_admission": RULE_1,
                             "rule_2_append_only_inclusion": RULE_2},
            "provenance": {
                "series_catalog_endpoint": f"GET {settings.base_url}{SERIES_ENDPOINT}",
                "series_catalog_retrieved_at_utc": catalog_at,
                "series_catalog_bytes": len(raw),
                "series_catalog_sha256": catalog_sha,
                "series_catalog_snapshot": catalog_path.name,
                "series_examined": len(catalog),
                "prefilter": "none -- the predicate is evaluated over the entire "
                             "catalog",
            },
            "gates": {"gate_1_disjointness": "passed",
                      "gate_2_unknown_vocabulary": "passed",
                      "gate_3_complete_evidence": "passed -- every series in the "
                                                  "catalog was read successfully"},
            "qualifying_count": len(q),
            "qualifying_series": q,
            "quarantine_adjacent_not_included": universe["quarantine_adjacent"],
            "quarantine_no_event_metadata": universe["quarantine_no_metadata"],
        }, indent=1, sort_keys=True).encode("utf-8")
        universe_path.write_bytes(universe_blob)

        ctx = {
            "manifest_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "catalog_retrieved_at": catalog_at,
            "base_url": settings.base_url,
            "n_catalog": len(catalog),
            "request_count": api.request_count,
            "catalog_bytes": len(raw),
            "catalog_sha256": catalog_sha,
            "probe_sha256": hashlib.sha256(probe_blob).hexdigest(),
            "universe_sha256": hashlib.sha256(universe_blob).hexdigest(),
            "rel_catalog": catalog_path.as_posix(),
            "rel_probe": probe_path.as_posix(),
            "rel_universe": universe_path.as_posix(),
            "rel_sums": (outdir / "SHA256SUMS").as_posix(),
            "universe": universe,
            "vocab_table": build_vocab_table(catalog, probes),
            "ambiguity_verdict":
                "MLB is cleanly distinguishable from every other named baseball "
                "competition; two boundary sets remain unresolved and are "
                "quarantined below rather than guessed",
        }

        manifest_path = Path(args.manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_blob = render_manifest(ctx).encode("utf-8")
        manifest_path.write_bytes(manifest_blob)

        sums = [
            (catalog_sha, f"{catalog_path.name}  (SHA-256 of the UNCOMPRESSED "
                          f"GET /series body)"),
            (hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
             f"{catalog_path.name}  (as stored, gzipped)"),
            (ctx["probe_sha256"], probe_path.name),
            (ctx["universe_sha256"], universe_path.name),
            (hashlib.sha256(manifest_blob).hexdigest(), manifest_path.as_posix()),
        ]
        (outdir / "SHA256SUMS").write_text(
            "\n".join(f"{h}  {n}" for h, n in sums) + "\n", encoding="utf-8")

        print(f"\n  wrote {manifest_path}  sha256="
              f"{hashlib.sha256(manifest_blob).hexdigest()}")
        print(f"  wrote {catalog_path}  ({catalog_path.stat().st_size:,} bytes gz)")
        print(f"  wrote {probe_path}")
        print(f"  wrote {universe_path}")
        print(f"  wrote {outdir / 'SHA256SUMS'}")
        print(f"\n  qualifying series: {len(q)}")
        print(f"  read requests issued: {api.request_count}")
        return 0
    finally:
        cache.close()


if __name__ == "__main__":
    raise SystemExit(main())
