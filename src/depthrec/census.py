"""
The independent REST census: the denominator, and the only authority on whether
an opportunity existed.

The WebSocket subscription set must NEVER define the denominator. If it did,
every market the recorder failed to discover or failed to subscribe to would
vanish from the population instead of being counted UNAVAILABLE, and the
measured depth clearance would be biased upward by exactly the recorder's own
unreliability -- the failure section 4 exists to prevent.

Two passes, both hitting GET /markets, both recorded request by request:

  live      every 60 s, `series_ticker` + status=open. Catches rotation
            (KXBTC15M/KXETH15M mint a new binary every quarter hour) fast enough
            that a market is in the census well before its T-7 moment.
  reconcile every 15 min, `series_ticker` + `min_close_ts`/`max_close_ts` over a
            window around now, with NO status filter. Recovers anything the live
            pass missed -- a market created and closed between two live ticks, a
            market that briefly reported a status the live filter excludes, or a
            live pass that simply failed.

A discovery failure is a `census_request` row with ok=0 and the error text. It
never removes a market: `upsert_census_market` has no delete path at all.

Both passes read the raw JSON body (get_markets_without_preload_content) rather
than the SDK's typed model. The typed path raises on settled markets -- its
`status` enum has no "finalized" member, which is what Kalshi actually returns
-- so a typed census would throw away precisely the markets whose close time has
passed. kbot.kalshi_client.get_market documents the same trap.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

import structlog

from depthrec import frozen
from depthrec.store import Store

log = structlog.get_logger(__name__)

_PAGE_LIMIT = 1000
_MAX_PAGES = 50
"""Paging guard. 50 pages x 1000 is far above any plausible open-market count
for these series; hitting it is logged as a census failure rather than silently
truncating the denominator."""

PASS_LIVE = "live"
PASS_RECONCILE = "reconcile"


def parse_close_ts(value: Any) -> int | None:
    """
    Kalshi `close_time` -> unix seconds.

    Accepts both the ISO-8601 string the raw JSON carries and the datetime the
    SDK would produce, because the census reads raw JSON but tests hand it
    either. Returns None rather than raising: a market with an unparseable close
    time still belongs in the census, it just cannot have a T-7 moment computed
    for it, and that is recorded as such.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


@dataclass
class CensusEntry:
    """One market the REST census knows about."""

    ticker: str
    series: str
    close_ts: int | None
    event_ticker: str
    status: str
    first_seen_ts: float
    last_seen_ts: float
    discovered_by: str
    market: dict[str, Any] = field(default_factory=dict)

    @property
    def target_ts(self) -> int | None:
        if self.close_ts is None:
            return None
        return self.close_ts - frozen.TARGET_OFFSET_SECONDS


class RestCensus:
    """
    Maintains the independent census for `series`, persisting every request,
    every response, every discovered ticker and every universe change.

    Holds a KalshiClient only for its authenticated SDK handle and its read
    token bucket. It calls three read methods and nothing else.
    """

    def __init__(self, *, client: Any, store: Store, series: Iterable[str]) -> None:
        self._client = client
        self._store = store
        self.series = tuple(series)
        self.entries: dict[str, CensusEntry] = {}
        self.last_live_ts: float | None = None
        self.last_reconcile_ts: float | None = None
        self.live_failures = 0
        self.reconcile_failures = 0

    # -- Passes ---------------------------------------------------------------

    def run_live_pass(self, *, now: float | None = None) -> list[CensusEntry]:
        """
        status=open sweep of every census series. Returns the markets newly
        added to the census by this pass.
        """
        now = time.time() if now is None else now
        added: list[CensusEntry] = []
        for series in self.series:
            params = {
                "series_ticker": series,
                "status": frozen.UNIVERSE_OPEN_STATUS,
                "limit": _PAGE_LIMIT,
            }
            markets = self._fetch_all(PASS_LIVE, series, params, now=now)
            if markets is None:
                self.live_failures += 1
                continue
            added.extend(self._ingest(series, markets, now=now, discovered_by=PASS_LIVE))
        self.last_live_ts = now
        return added

    def run_reconcile_pass(self, *, now: float | None = None) -> list[CensusEntry]:
        """
        series_ticker + close-time range sweep, no status filter.

        The window straddles now so it recovers both markets that already closed
        since the last pass and markets that will close soon. No status filter
        because the point is to see markets the live pass's filter could have
        excluded.
        """
        now = time.time() if now is None else now
        min_close = int(now - frozen.CENSUS_RECONCILE_LOOKBACK_SECONDS)
        max_close = int(now + frozen.CENSUS_RECONCILE_LOOKAHEAD_SECONDS)
        added: list[CensusEntry] = []
        for series in self.series:
            params = {
                "series_ticker": series,
                "min_close_ts": min_close,
                "max_close_ts": max_close,
                "limit": _PAGE_LIMIT,
            }
            markets = self._fetch_all(PASS_RECONCILE, series, params, now=now)
            if markets is None:
                self.reconcile_failures += 1
                continue
            added.extend(self._ingest(series, markets, now=now, discovered_by=PASS_RECONCILE))
        self.last_reconcile_ts = now
        return added

    # -- Internals ------------------------------------------------------------

    def _fetch_all(
        self, pass_kind: str, series: str, params: dict[str, Any], *, now: float
    ) -> list[dict[str, Any]] | None:
        """
        Page through GET /markets. Returns None on ANY failure.

        None, not a partial list: a half-read page set would look like a shrunk
        universe, and a shrunk universe is the one thing the census must never
        report by accident.
        """
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(_MAX_PAGES):
            call = dict(params)
            if cursor:
                call["cursor"] = cursor
            started = time.time()
            try:
                self._client._read_bucket.acquire()
                raw = self._client._markets_api.get_markets_without_preload_content(**call)
                body = raw.read()
                status_code = getattr(raw, "status", None)
                payload = json.loads(body)
            except Exception as exc:  # noqa: BLE001
                self._store.record_census_request(
                    ts=started, pass_kind=pass_kind, series=series, method="GET",
                    url="/markets", params=call, ok=False, error=f"{type(exc).__name__}: {exc}",
                )
                log.error(
                    "depthrec.census.request_failed",
                    pass_kind=pass_kind, series=series, page=page, error=str(exc),
                )
                return None

            markets = payload.get("markets") or []
            body_text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
            self._store.record_census_request(
                ts=started, pass_kind=pass_kind, series=series, method="GET",
                url="/markets", params=call, ok=True, http_status=status_code,
                n_markets=len(markets),
                response_sha256=hashlib.sha256(body_text.encode()).hexdigest(),
                response=body_text,
            )
            collected.extend(markets)
            cursor = payload.get("cursor") or None
            if not cursor or not markets:
                return collected

        self._store.record_census_request(
            ts=now, pass_kind=pass_kind, series=series, method="GET", url="/markets",
            params=params, ok=False,
            error=f"paging guard hit after {_MAX_PAGES} pages -- refusing to report a "
                  f"possibly truncated universe",
        )
        log.error("depthrec.census.paging_guard", pass_kind=pass_kind, series=series)
        return None

    def _ingest(
        self, series: str, markets: list[dict[str, Any]], *, now: float, discovered_by: str
    ) -> list[CensusEntry]:
        added: list[CensusEntry] = []
        for market in markets:
            ticker = market.get("ticker")
            if not ticker:
                continue
            close_ts = parse_close_ts(market.get("close_time"))
            status = str(market.get("status") or "")
            event_ticker = str(market.get("event_ticker") or "")

            outcome = self._store.upsert_census_market(
                ts=now, ticker=ticker, series=series, close_ts=close_ts,
                event_ticker=event_ticker, status=status,
                discovered_by=discovered_by, market=market,
            )
            existing = self.entries.get(ticker)
            if existing is None:
                entry = CensusEntry(
                    ticker=ticker, series=series, close_ts=close_ts,
                    event_ticker=event_ticker, status=status,
                    first_seen_ts=now, last_seen_ts=now,
                    discovered_by=discovered_by, market=market,
                )
                self.entries[ticker] = entry
                added.append(entry)
                self._store.record_census_change(
                    ts=now, ticker=ticker, series=series, change="discovered",
                    detail=f"pass={discovered_by} status={status} close_ts={close_ts}",
                )
                log.info(
                    "depthrec.census.discovered",
                    ticker=ticker, series=series, close_ts=close_ts,
                    status=status, pass_kind=discovered_by,
                )
            else:
                if existing.close_ts != close_ts:
                    self._store.record_census_change(
                        ts=now, ticker=ticker, series=series, change="close_ts_changed",
                        detail=f"{existing.close_ts} -> {close_ts} (pass={discovered_by})",
                    )
                    log.warning(
                        "depthrec.census.close_ts_changed",
                        ticker=ticker, was=existing.close_ts, now=close_ts,
                    )
                    existing.close_ts = close_ts
                if existing.status != status:
                    self._store.record_census_change(
                        ts=now, ticker=ticker, series=series, change="status_changed",
                        detail=f"{existing.status} -> {status} (pass={discovered_by})",
                    )
                    existing.status = status
                existing.last_seen_ts = now
                existing.market = market
                if outcome == "new":
                    # The store had no row but memory did: a resumed run.
                    added.append(existing)
        return added

    # -- Views ----------------------------------------------------------------

    def due_for_classification(self, *, now: float, lead_seconds: int) -> list[CensusEntry]:
        """
        Markets whose T-7 moment is within `lead_seconds` and still ahead of now.

        Classification needs candles through T-7, so it runs shortly BEFORE the
        target and the observation is taken AT the target.
        """
        out = []
        for entry in self.entries.values():
            target = entry.target_ts
            if target is None:
                continue
            if now <= target <= now + lead_seconds:
                out.append(entry)
        return sorted(out, key=lambda e: e.target_ts or 0)

    def all_with_target(self) -> list[CensusEntry]:
        return [e for e in self.entries.values() if e.target_ts is not None]

    def summary(self) -> dict[str, Any]:
        return {
            "series": list(self.series),
            "markets": len(self.entries),
            "live_failures": self.live_failures,
            "reconcile_failures": self.reconcile_failures,
            "last_live_ts": self.last_live_ts,
            "last_reconcile_ts": self.last_reconcile_ts,
        }
