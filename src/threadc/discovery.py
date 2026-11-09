"""
The full-catalog discovery census: which series satisfy the frozen predicate,
right now, and whether the answer can be trusted.

This is Frozen Rule 1's engine. It is the same census
scripts/threadc_collection_universe.py ran to produce Collection Universe v1,
lifted into the recorder so it can run repeatedly:

  1. GET /series -- the ENTIRE catalog, ~13.8k series, with no prefilter.
     Prefiltering on ticker prefix, tag or category would be far cheaper and
     would silently lose real members: KXCITYMLBEXPAND carries "Pro Baseball"
     while tagged ['Hockey'], and KXMLBMENTION carries it from category
     "Mentions". So the sweep is total, and a qualifying series outside every
     obvious MLB namespace is found on the same footing as one inside it.
  2. GET /events?series_ticker=<S> for every series, paged to exhaustion, and
     collect every product_metadata.competition value it carries.
  3. Apply threadc.frozen.qualifies() -- exact, case-sensitive equality against
     "Pro Baseball".

GATE 3, COMPLETE EVIDENCE. A series the census could not read is not a series
with no MLB events, but downstream the two are indistinguishable. So if any
series in the catalog is unreadable -- missing its probe, exhausted its retries,
or errored -- the census is INCOMPLETE. An INCOMPLETE census applies ZERO
additions and ZERO removals and is recorded as an operational fact. It does
NOT stop the recorder: the live collection continues on the entire
last-known-good append-only universe, and discovery is retried on a later
census. A transient REST failure must never cause already-covered live markets
to stop being recorded.

Nothing here reads a price, a book, a volume or a settlement. The only field
that decides anything is events[].product_metadata.competition.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import structlog

from threadc import frozen
from threadc.readonly import EVENTS_MAX_PAGES, EVENTS_PAGE_LIMIT, PROBE_THREADS

log = structlog.get_logger(__name__)

SERIES_ENDPOINT = "/series"
EVENTS_ENDPOINT = "/events"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SeriesProbe:
    """Every competition value one series' events carry, plus how it was read."""

    series_ticker: str
    n_events: int = 0
    pages: int = 0
    competitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None
    probed_at: str = ""

    @property
    def competition_values(self) -> set:
        return {slot["competition"] for slot in self.competitions.values()}

    @property
    def qualifies(self) -> bool:
        return frozen.qualifies(self.competition_values)

    def evidence(self) -> dict[str, Any]:
        """
        The exact predicate-bearing metadata, verbatim, for the discovery record.

        Keeps the matched value, the matched field, the counts and one sample
        event ticker per observed value -- everything needed to re-check the
        admission by hand against a fresh API call.
        """
        return {
            "series_ticker": self.series_ticker,
            "matched_field": frozen.PREDICATE_FIELD,
            "matched_value": frozen.MLB_COMPETITION,
            "predicate": frozen.PREDICATE_PROSE,
            "endpoint": f"/events?series_ticker={self.series_ticker}&limit={EVENTS_PAGE_LIMIT}",
            "n_events_scanned": self.n_events,
            "event_pages_scanned": self.pages,
            "competition_values_observed": self.competitions,
            "probed_at_utc": self.probed_at,
        }

    def qualifying_event_tickers(self) -> list[str]:
        """The event identifier(s) whose metadata satisfied the predicate."""
        out = []
        for slot in self.competitions.values():
            if slot.get("competition") == frozen.MLB_COMPETITION:
                sample = slot.get("sample_event_ticker")
                if sample:
                    out.append(sample)
        return out


@dataclass
class DiscoveryResult:
    """One full-catalog census. Immutable evidence, whether it succeeded or not."""

    census_id: str
    started_ts: float
    finished_ts: float
    status: str
    series_examined: int
    catalog_sha256: str
    catalog_retrieved_at: str
    base_url: str
    probes: dict[str, SeriesProbe] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)
    error: str = ""
    requests_issued: int = 0

    @property
    def complete(self) -> bool:
        return self.status == frozen.CENSUS_COMPLETE

    def qualifying(self) -> dict[str, SeriesProbe]:
        return {t: p for t, p in self.probes.items() if p.error is None and p.qualifies}

    def record(self) -> dict[str, Any]:
        """The persisted census record. Operational and metadata only."""
        return {
            "census_id": self.census_id,
            "started_ts": self.started_ts,
            "started_utc": datetime.fromtimestamp(
                self.started_ts, tz=timezone.utc).isoformat(),
            "finished_ts": self.finished_ts,
            "finished_utc": datetime.fromtimestamp(
                self.finished_ts, tz=timezone.utc).isoformat(),
            "status": self.status,
            "gate_3_complete_evidence": self.status,
            "series_examined": self.series_examined,
            "series_qualifying": len(self.qualifying()),
            "series_unreadable": len(self.unreadable),
            "unreadable_series": sorted(self.unreadable)[:500],
            "catalog_sha256": self.catalog_sha256,
            "catalog_retrieved_at_utc": self.catalog_retrieved_at,
            "catalog_endpoint": f"GET {self.base_url}{SERIES_ENDPOINT}",
            "events_endpoint": f"GET {self.base_url}{EVENTS_ENDPOINT}",
            "requests_issued": self.requests_issued,
            "prefilter": "none -- the predicate is evaluated over the entire catalog",
            "predicate": frozen.PREDICATE_PROSE,
            "error": self.error,
        }


def probe_series_events(api: Any, series_ticker: str) -> SeriesProbe:
    """
    Every product_metadata.competition value carried by one series' events.

    Pages to exhaustion: a series whose first page is all-null but whose later
    pages carry "Pro Baseball" must not be misjudged on a truncated read. Counts
    and one sample event ticker per value are kept so a discovery record can
    quote its evidence verbatim.
    """
    probe = SeriesProbe(series_ticker=series_ticker, probed_at=_utc_now_iso())
    cursor: str | None = None
    while probe.pages < EVENTS_MAX_PAGES:
        params: dict[str, Any] = {
            "series_ticker": series_ticker, "limit": EVENTS_PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        data = api.get_retrying(EVENTS_ENDPOINT, params)
        probe.pages += 1
        events = data.get("events") or []
        for ev in events:
            probe.n_events += 1
            pm = ev.get("product_metadata") or {}
            comp = pm.get("competition")
            scope = pm.get("competition_scope")
            key = json.dumps(comp)  # null and the string "null" cannot collide
            slot = probe.competitions.setdefault(key, {
                "competition": comp, "count": 0, "scopes": {},
                "sample_event_ticker": ev.get("event_ticker"),
            })
            slot["count"] += 1
            skey = json.dumps(scope)
            slot["scopes"][skey] = slot["scopes"].get(skey, 0) + 1
        nxt = data.get("cursor") or None
        if not nxt or nxt == cursor or not events:
            break
        cursor = nxt
    return probe


def run_discovery_census(
    api: Any,
    *,
    census_id: str | None = None,
    threads: int = PROBE_THREADS,
    should_stop: Callable[[], bool] | None = None,
) -> DiscoveryResult:
    """
    One full-catalog census. Returns COMPLETE or INCOMPLETE; never raises for a
    transport failure, because a failed census is an operational record and not
    a reason to stop recording.

    `should_stop` lets a shutdown abandon a 25-minute census promptly. An
    abandoned census is INCOMPLETE by construction -- it has not read every
    series -- so it can never smuggle a partial universe through.
    """
    started = time.time()
    census_id = census_id or f"census-{int(started)}"
    base_url = getattr(api, "base_url", "")
    started_requests = getattr(api, "request_count", 0)

    def result(status: str, **kw: Any) -> DiscoveryResult:
        return DiscoveryResult(
            census_id=census_id, started_ts=started, finished_ts=time.time(),
            status=status, base_url=base_url,
            requests_issued=getattr(api, "request_count", 0) - started_requests,
            **kw,
        )

    # -- Stage 1: the whole catalog, byte-exact --------------------------------
    try:
        retrieved_at = _utc_now_iso()
        raw, _status = api.get_raw(SERIES_ENDPOINT)
        catalog_sha = hashlib.sha256(raw).hexdigest()
        catalog = json.loads(raw).get("series") or []
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        log.error("threadc.discovery.catalog_failed", error=str(exc))
        return result(
            frozen.CENSUS_INCOMPLETE, series_examined=0, catalog_sha256="",
            catalog_retrieved_at="", error=f"{type(exc).__name__}: {exc}",
        )

    tickers = [s.get("series_ticker") for s in catalog if s.get("series_ticker")]
    log.info(
        "threadc.discovery.catalog", census_id=census_id, series=len(tickers),
        sha256=catalog_sha[:16], prefilter="none",
    )

    # -- Stage 2: the event-metadata probe, no prefilter -----------------------
    probes: dict[str, SeriesProbe] = {}
    lock = threading.Lock()
    aborted = False

    def work(ticker: str) -> SeriesProbe:
        if should_stop is not None and should_stop():
            return SeriesProbe(
                series_ticker=ticker, probed_at=_utc_now_iso(),
                error="census abandoned before this series was probed",
            )
        try:
            return probe_series_events(api, ticker)
        except Exception as exc:  # noqa: BLE001 - an unreadable series, recorded
            return SeriesProbe(
                series_ticker=ticker, probed_at=_utc_now_iso(),
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )

    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        for probe in pool.map(work, tickers):
            with lock:
                probes[probe.series_ticker] = probe

    if should_stop is not None and should_stop():
        aborted = True

    # -- Stage 3: Gate 3, complete evidence ------------------------------------
    unreadable = [t for t in tickers if t not in probes or probes[t].error is not None]
    if unreadable or aborted:
        log.warning(
            "threadc.discovery.incomplete", census_id=census_id,
            unreadable=len(unreadable), aborted=aborted, series=len(tickers),
            note="zero universe changes applied; live recording continues",
        )
        return result(
            frozen.CENSUS_INCOMPLETE, series_examined=len(tickers),
            catalog_sha256=catalog_sha, catalog_retrieved_at=retrieved_at,
            probes=probes, unreadable=unreadable,
            error=(
                "census abandoned during shutdown"
                if aborted and not unreadable
                else f"{len(unreadable)} series could not be read"
            ),
        )

    out = result(
        frozen.CENSUS_COMPLETE, series_examined=len(tickers),
        catalog_sha256=catalog_sha, catalog_retrieved_at=retrieved_at,
        probes=probes, unreadable=[],
    )
    log.info(
        "threadc.discovery.complete", census_id=census_id,
        series=len(tickers), qualifying=len(out.qualifying()),
        seconds=round(out.finished_ts - out.started_ts, 1),
    )
    return out


__all__ = [
    "DiscoveryResult", "SeriesProbe", "run_discovery_census",
    "probe_series_events", "SERIES_ENDPOINT", "EVENTS_ENDPOINT",
]
