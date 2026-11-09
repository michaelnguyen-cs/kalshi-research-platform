"""
The only way this package talks to Kalshi: signed GET, against four paths.

Deliberately NOT kbot.kalshi_client.KalshiClient. That class owns place_order()
and cancel_order(), and a data-banking recorder must not have such a method in
scope at all -- "no order path" should be a structural property, not a promise.
scripts/threadc_collection_universe.py made the same choice for the same reason
and this module is its runtime sibling.

Raw httpx rather than the Kalshi SDK, for three reasons the SDK cannot meet:

  * the exact response BYTES are needed, because discovery evidence is hashed
    and a re-serialized dict hashes to something Kalshi never sent;
  * the SDK's typed models are known to raise on real responses -- its market
    `status` enum has no "finalized" member, which is what the exchange actually
    returns for settled markets (see kbot.kalshi_client.get_market);
  * a path allowlist can be enforced here and cannot be enforced there.

CensusClientAdapter exists so depthrec.census.RestCensus -- the proven market
census and subscription-refresh mechanism -- runs UNCHANGED on top of this
read-only layer. RestCensus touches exactly two attributes of its client,
`_read_bucket.acquire()` and `_markets_api.get_markets_without_preload_content`,
so supplying those two is enough to reuse it without importing an order path.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import httpx
import structlog

from kbot.auth import kalshi_headers, load_private_key
from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)

ALLOWED_PATHS = (
    re.compile(r"^/series$"),
    re.compile(r"^/series/[^/]+$"),
    re.compile(r"^/events$"),
    re.compile(r"^/markets$"),
)
"""The complete set of endpoints this recorder may call. /series and /events are
the inclusion predicate's evidence; /markets is the market census. Anything else
-- an order path above all -- raises before a request is built."""

MAX_ATTEMPTS = 7
INITIAL_BACKOFF = 2.0
"""Matches scripts/threadc_collection_universe.py. Measured live 2026-09-03: at
14 concurrent probes against a nominal 20/s read budget, 46 of 13,772 series
still exhausted 4 attempts on transient HTTP 429. A probe that gives up looks
exactly like "this series has no Pro Baseball events", which would silently drop
a real member, so the retry budget is generous and a series that still cannot be
read makes the whole census INCOMPLETE rather than shrinking the universe."""

PROBE_THREADS = 14
EVENTS_PAGE_LIMIT = 200
EVENTS_MAX_PAGES = 200


class ReadOnlyViolation(RuntimeError):
    """An attempt to call something outside the read allowlist."""


class TokenBucket:
    """
    Thread-safe token bucket that bounds the AGGREGATE rate across every caller.

    Deliberately not the shape used by KalshiClient._TokenBucket and by
    scripts/threadc_collection_universe.py. Those release the lock, then each
    blocked caller sleeps a wait computed from the same instant -- so N threads
    that arrive together all sleep roughly the same short interval and all
    proceed at once, and the effective rate overshoots the nominal one by about
    the concurrency. That is tolerable for a one-shot script with a single
    consumer. It is not tolerable here: the 14-thread discovery probe and the
    market census thread share this one limiter, and the whole point of sharing
    it is that their combined rate stays inside one budget.

    So a caller that cannot be served immediately RESERVES its slot under the
    lock -- the refill clock is advanced past the grant -- and then sleeps until
    that absolute deadline. Waiters therefore queue one slot apart instead of
    waking together, and 30 requests against a 20/s bucket take 0.5 s no matter
    how many threads asked.
    """

    def __init__(self, rate: float) -> None:
        self._rate = max(1.0, float(rate))
        self._capacity = self._rate
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self._capacity, self._tokens + (now - self._last) * self._rate
            )
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                deadline = None
            else:
                # Charge the slot now and push the refill clock past it, so the
                # next caller computes its wait from this reservation rather
                # than from the same instant.
                deficit = 1.0 - self._tokens
                self._tokens = 0.0
                self._last = now + deficit / self._rate
                deadline = self._last
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)


class RawResponse:
    """
    The shape depthrec.census.RestCensus expects back from the SDK's
    `*_without_preload_content` call: `.read()` giving bytes and `.status`.
    """

    __slots__ = ("_body", "status")

    def __init__(self, body: bytes, status: int) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body


class ReadOnlyAPI:
    """Signed, GET-only, path-allowlisted Kalshi reads."""

    def __init__(self, cfg: Settings | None = None, timeout: float = 60.0) -> None:
        self._cfg = cfg or _default_settings
        self._base = self._cfg.base_url.rstrip("/")
        self._prefix = httpx.URL(self._cfg.base_url).path.rstrip("/")
        self._timeout = timeout
        self._local = threading.local()
        self._count_lock = threading.Lock()
        self.request_count = 0
        self.error_count = 0

        self._cfg.require_credentials()
        self._key_id = self._cfg.kalshi_key_id
        self._private_key = load_private_key(self._cfg.private_key_path)
        self.read_bucket = TokenBucket(getattr(self._cfg, "rate_limit_reads", 20))

    # -- Identity, for manifests ---------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def api_env(self) -> str:
        return self._cfg.kalshi_env

    # -- Requests -------------------------------------------------------------

    def _client(self) -> httpx.Client:
        c = getattr(self._local, "client", None)
        if c is None:
            c = httpx.Client(timeout=self._timeout)
            self._local.client = c
        return c

    def get_raw(self, path: str, params: dict | None = None) -> tuple[bytes, int]:
        """One signed GET against an allowlisted read path; returns (bytes, status)."""
        if not any(rx.match(path) for rx in ALLOWED_PATHS):
            raise ReadOnlyViolation(
                f"refusing {path!r}: threadc is read-only and calls only "
                f"{[rx.pattern for rx in ALLOWED_PATHS]}"
            )
        self.read_bucket.acquire()
        headers = kalshi_headers("GET", self._prefix + path, self._key_id, self._private_key)
        resp = self._client().get(self._base + path, headers=headers, params=params or {})
        with self._count_lock:
            self.request_count += 1
        resp.raise_for_status()
        return resp.content, resp.status_code

    def get(self, path: str, params: dict | None = None) -> dict:
        body, _ = self.get_raw(path, params)
        return json.loads(body)

    def get_retrying(self, path: str, params: dict | None = None) -> dict:
        """GET with backoff on transport errors, 5xx and 429; other 4xx raise at once."""
        backoff = INITIAL_BACKOFF
        last: Exception | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                return self.get(path, params)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code != 429 and code < 500:
                    raise
                last = exc
            except (httpx.TransportError, json.JSONDecodeError) as exc:
                last = exc
            with self._count_lock:
                self.error_count += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        assert last is not None
        raise last

    def close(self) -> None:
        c = getattr(self._local, "client", None)
        if c is not None:
            c.close()
            self._local.client = None


# -- Reusing depthrec.census.RestCensus unchanged ----------------------------


class _MarketsApiAdapter:
    """The two-method surface RestCensus reaches for, backed by ReadOnlyAPI."""

    def __init__(self, api: ReadOnlyAPI) -> None:
        self._api = api

    def get_markets_without_preload_content(self, **params: Any) -> RawResponse:
        clean = {k: v for k, v in params.items() if v is not None}
        body, status = self._api.get_raw("/markets", clean)
        return RawResponse(body, status)


class _NullBucket:
    """
    RestCensus acquires the client's read bucket before each call; ReadOnlyAPI
    already acquires its own inside get_raw(). Acquiring twice would halve the
    effective read rate for no benefit, so this one is a no-op and the real
    limiter stays in exactly one place.
    """

    def acquire(self) -> None:  # pragma: no cover - trivial
        return None


class CensusClientAdapter:
    """
    A stand-in for KalshiClient carrying ONLY what depthrec.census.RestCensus
    uses, so that census -- and its frozen 60 s / 900 s cadence -- is reused
    without ever constructing an object that can place an order.
    """

    def __init__(self, api: ReadOnlyAPI) -> None:
        self.api = api
        self._read_bucket = _NullBucket()
        self._markets_api = _MarketsApiAdapter(api)

    def close(self) -> None:
        self.api.close()


__all__ = [
    "ReadOnlyAPI", "CensusClientAdapter", "ReadOnlyViolation", "TokenBucket",
    "ALLOWED_PATHS", "RawResponse", "PROBE_THREADS", "EVENTS_PAGE_LIMIT",
    "EVENTS_MAX_PAGES",
]
