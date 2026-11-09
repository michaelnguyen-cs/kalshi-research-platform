"""
Thin REST wrapper around the Kalshi API.

Reads  → SDK (MarketsApi / PortfolioApi); retried on 429 / 5xx with
          exponential backoff + jitter.
Writes → httpx + standalone kalshi_headers() signer; V2 endpoints;
          dollar-string prices; NEVER retried (a double-submit can
          double a position).
DRY_RUN → place_order / cancel_order log + return a simulated ack;
           zero network write calls.

Price note: the SDK's CreateOrderRequest uses integer cents (1–99).
The March-2026 API update requires dollar strings (e.g. "0.6500").
We bypass the SDK for all writes and send dollar strings ourselves.

Order-write V2 migration (2026-08)
──────────────────────────────────
Kalshi retired POST /portfolio/orders (410 Gone) in favour of
POST /portfolio/events/orders, and DELETE /portfolio/orders/{id} in
favour of DELETE /portfolio/events/orders/{id}. The new create shape
differs in three ways that matter for correctness:

  1. `side` is the BOOK side of the YES leg, not the contract we want:
     "bid" = buy YES, "ask" = sell YES. There is no "no" side. Buying NO
     is short YES, so a NO order goes out as an ASK at (1 - price) —
     see to_v2_side_and_price(). Getting this backwards places the
     opposite position at a price we already judged bad, so the
     conversion lives in one tested function and nowhere else.
  2. `count` and `price` are fixed-point STRINGS ("10.00", "0.5600").
  3. The response is FLAT (order_id / fill_count / remaining_count /
     average_fill_price at the top level), where the old shape nested
     everything under "order". average_fill_price comes back in YES
     terms because the order was quoted on the YES book — it is
     converted back to the caller's own side by from_v2_fill_price()
     so that every downstream consumer (position DB, P&L, markouts)
     keeps seeing prices in the denomination it already assumes.

Orders are sent immediate_or_cancel: the bot only ever places
marketable orders at a limit price and wants a fill now or nothing.
That admits PARTIAL fills, so OrderAck carries fill_count_units and callers
must record what actually filled, not what was requested.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

import kalshi_python
from kalshi_python.exceptions import ApiException

from kbot.auth import build_sdk_client, kalshi_headers, load_private_key
from kbot.config import Settings, settings as _default_settings
from kbot.quantity import UNITS_PER_CONTRACT, units_from_fixed_point

log = structlog.get_logger(__name__)

# Retryable HTTP status codes for read paths only.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

# ── V2 order constants ────────────────────────────────────────────────────────
# The bot only ever places marketable orders at a limit price ("take it now or
# skip"), so IOC is the matching time-in-force: it crosses immediately against
# whatever is available and cancels the rest instead of resting on the book.
# good_till_canceled must never be the default here — a resting order the bot
# does not manage would sit exposed after the loop has moved on.
TIF_IMMEDIATE_OR_CANCEL = "immediate_or_cancel"
TIF_FILL_OR_KILL = "fill_or_kill"
TIF_GOOD_TILL_CANCELED = "good_till_canceled"

# Single trading account, taker-only flow: if a self-trade would ever occur,
# cancel the incoming aggressive order rather than our own resting liquidity.
STP_TAKER_AT_CROSS = "taker_at_cross"

# Fixed-point decimal places required by the V2 create-order body.
_PRICE_DECIMALS = 4
_COUNT_DECIMALS = 2


# ── YES/NO ↔ bid/ask translation ─────────────────────────────────────────────
# The single place the bot's (side, price) model is converted to the exchange's
# YES-book model. Kept module-level and pure so it is directly testable.

def to_v2_side_and_price(side: str, price_dollars: float) -> tuple[str, float]:
    """
    Translate the bot's (yes|no, side-denominated price) into V2's
    (bid|ask, YES-denominated price).

        buy YES @ p  →  ("bid", p)          buy the YES leg outright
        buy NO  @ p  →  ("ask", 1 - p)      sell the YES leg; short YES == long NO

    The NO case is the high-risk one. Buying NO at $0.30 means paying $0.30
    for a contract that pays $1 when YES does NOT happen. That is identical
    to selling YES at $0.70: you collect $0.70, post $1.00 collateral, and
    your net outlay is $0.30 per contract. Hence ask @ 0.70.

    Sending "bid" at (1 - p) instead — buying YES at 0.70 — would open the
    OPPOSITE position, at a price the strategy had just judged too rich
    (it wanted NO at 0.30, i.e. it believes YES is worth less than 0.70).
    """
    normalized = side.lower().strip()
    if not 0.0 < price_dollars < 1.0:
        raise ValueError(
            f"price_dollars must be strictly between 0 and 1; got {price_dollars!r}"
        )
    if normalized == "yes":
        return "bid", round(price_dollars, _PRICE_DECIMALS)
    if normalized == "no":
        return "ask", round(1.0 - price_dollars, _PRICE_DECIMALS)
    raise ValueError(f"side must be 'yes' or 'no'; got {side!r}")


def from_v2_fill_price(side: str, yes_price: float) -> float:
    """
    Inverse of to_v2_side_and_price's price leg: convert a YES-denominated
    fill price from the exchange back into the caller's own side.

    Every downstream consumer of a fill price — outcomes.compute_outcome
    (cost = contracts * fill_price), live_pnl, markouts — assumes the price
    is quoted in the side actually traded. A NO fill reported at its raw YES
    price would overstate the cost basis of every NO trade.
    """
    normalized = side.lower().strip()
    if normalized == "yes":
        return round(yes_price, _PRICE_DECIMALS)
    if normalized == "no":
        return round(1.0 - yes_price, _PRICE_DECIMALS)
    raise ValueError(f"side must be 'yes' or 'no'; got {side!r}")


def _parse_fixed_point(raw: Any) -> float | None:
    """Parse a V2 fixed-point string ("10.00") to float; None if unusable."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_count_units(raw: Any, default_units: int) -> int:
    """
    Parse a V2 fixed-point contract count to integer HUNDREDTHS of a contract
    (see kbot.quantity), falling back to `default_units`.

    Replaces the old _parse_count, which returned int(round(value)) and so
    reported a 0.5-contract partial fill as either 0 or 1 whole contract. That
    number is the cost basis for the position, the input to exposure, and what
    settlement pays out against -- rounding it invented or destroyed size at
    every one of those points.
    """
    units = units_from_fixed_point(raw)
    return default_units if units is None else units


# ── Simple token-bucket rate limiter ─────────────────────────────────────────

class _TokenBucket:
    """Single-threaded token bucket; not multi-process safe."""

    def __init__(self, rate: float) -> None:
        self._rate = rate       # tokens / second
        self._tokens = rate
        self._last = time.monotonic()

    def acquire(self) -> None:
        now = time.monotonic()
        self._tokens = min(
            self._rate,
            self._tokens + (now - self._last) * self._rate,
        )
        self._last = now
        if self._tokens < 1.0:
            time.sleep((1.0 - self._tokens) / self._rate)
            self._tokens = 0.0
        else:
            self._tokens -= 1.0


# ── Return type for order operations ─────────────────────────────────────────

@dataclass
class OrderAck:
    """
    What place_order actually did.

    `dry_run` is the real-money question's answer at the one moment it has a
    definite one; kbot.execution stamps it onto the position as `is_live`.
    Never re-derive it from config downstream.

    Fill fields describe the IMMEDIATE outcome of an immediate_or_cancel
    order, so they are final — there is no later fill to wait for:
      fill_count_units      size actually filled, in integer HUNDREDTHS of a
                            contract (0 = nothing traded). See kbot.quantity:
                            Kalshi reports this as a fixed-point string and it
                            is NOT always a whole number of contracts.
      remaining_count_units size cancelled unfilled, same units.
      average_fill_price  volume-weighted fill price, in the CALLER'S side
                          denomination (NO trades are converted back from
                          the YES-quoted value the exchange returns).
                          None when nothing filled.
    In dry-run these simulate a complete fill at the requested price, so the
    paper path records positions exactly as it did before the V2 migration.
    """
    client_order_id: str
    order_id: str | None     # None in dry-run
    dry_run: bool
    fill_count_units: int = 0
    remaining_count_units: int = 0
    average_fill_price: float | None = None
    raw: dict = field(default_factory=dict)

    @property
    def fill_contracts(self) -> float:
        """Filled size in contracts, for dollar math, logging and display."""
        return self.fill_count_units / UNITS_PER_CONTRACT

    @property
    def remaining_contracts(self) -> float:
        """Unfilled size in contracts, for dollar math, logging and display."""
        return self.remaining_count_units / UNITS_PER_CONTRACT


# ── Client ────────────────────────────────────────────────────────────────────

class KalshiClient:
    """
    Single entry-point for all Kalshi REST interactions.

    Instantiate once; inject into strategy / execution / paper modules.
    Do not access the SDK or httpx objects outside this class — that breaks
    the swap-point if the underlying SDK changes.
    """

    def __init__(self, cfg: Settings | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._read_bucket = _TokenBucket(self._cfg.rate_limit_reads)
        self._write_bucket = _TokenBucket(self._cfg.rate_limit_writes)

        # Derive the URL-path prefix used in RSA-PSS signatures from base_url.
        # e.g. "https://demo-api.kalshi.co/trade-api/v2" → "/trade-api/v2"
        self._api_path_prefix: str = urlparse(self._cfg.base_url).path

        # Set up SDK (reads) — degrade gracefully if credentials are absent
        # so `kbot markets` works with no .env at all.
        self._markets_api: kalshi_python.MarketsApi
        self._series_api: kalshi_python.SeriesApi
        self._portfolio_api: kalshi_python.PortfolioApi
        self._authed = False

        if self._cfg.kalshi_key_id and self._cfg.private_key_path.exists():
            try:
                api_client = build_sdk_client(self._cfg)
                self._markets_api = kalshi_python.MarketsApi(api_client)
                self._series_api = kalshi_python.SeriesApi(api_client)
                self._portfolio_api = kalshi_python.PortfolioApi(api_client)
                self._authed = True
                self._private_key = load_private_key(self._cfg.private_key_path)
            except Exception as exc:
                log.warning("auth.setup_failed", error=str(exc))
                self._authed = False
                self._private_key = None
                self._markets_api = self._public_markets_api()
                self._series_api = self._public_series_api()
                self._portfolio_api = None  # type: ignore[assignment]
        else:
            self._markets_api = self._public_markets_api()
            self._series_api = self._public_series_api()
            self._portfolio_api = None  # type: ignore[assignment]
            self._private_key = None

        self._http = httpx.Client(timeout=10.0)

    # ── Public / read methods ─────────────────────────────────────────────────

    def get_markets(
        self,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict]:
        """List markets. No credentials required."""
        def _call() -> list[dict]:
            self._read_bucket.acquire()
            resp = self._markets_api.get_markets(
                status=status, limit=limit, cursor=cursor
            )
            return [m.model_dump() for m in (resp.markets or [])]

        return self._read_with_retry(_call)

    def get_market(self, ticker: str) -> dict:
        """
        Raw market record for `ticker`. No credentials required.

        Uses the without-preload (raw JSON) SDK path deliberately: the SDK's
        Market model rejects the `status="finalized"` that SETTLED markets
        return (its enum only allows initialized/active/closed/settled/
        determined), so the normal typed getter raises a ValidationError on
        exactly the settled markets whose `result`/`expiration_value` we need
        for outcome recording. Returns the market sub-dict (empty on absence).
        """
        def _call() -> dict:
            self._read_bucket.acquire()
            raw = self._markets_api.get_market_without_preload_content(ticker=ticker)
            data = json.loads(raw.read())
            return data.get("market", {}) or {}

        return self._read_with_retry(_call)

    def get_series(self, series_ticker: str) -> dict:
        """
        Raw series record for `series_ticker`. No credentials required.

        Route: GET /series/{series_ticker}

        Exists for the fee model: the record carries `fee_type` (observed
        "quadratic" and "quadratic_with_maker_fees") and `fee_multiplier`,
        which is NOT always 1 -- 0, 0.5 and 1 all occur across live series.
        Assuming a flat 0.07 is therefore wrong for some series.

        Uses the without-preload (raw JSON) path for the same reason as
        get_market: the SDK's Series model declares only ticker/title/category/
        status, so the typed getter silently DROPS both fee fields.

        Kalshi exposes no historical fee endpoint -- `last_updated_ts` is a
        single "last touched" stamp with no per-field history. Record the
        values at the time of use; they cannot be recovered afterwards.

        Returns the series sub-dict (empty on absence).
        """
        def _call() -> dict:
            self._read_bucket.acquire()
            # get_series_by_ticker_*, NOT get_series_* -- in this SDK the
            # latter is the LIST endpoint and takes `status`, not a ticker.
            raw = self._series_api.get_series_by_ticker_without_preload_content(
                ticker=series_ticker
            )
            data = json.loads(raw.read())
            return data.get("series", {}) or {}

        return self._read_with_retry(_call)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Return orderbook for `ticker`. No credentials required."""
        def _call() -> dict:
            self._read_bucket.acquire()
            resp = self._markets_api.get_market_orderbook(ticker=ticker, depth=depth)
            return resp.orderbook.model_dump() if resp.orderbook else {}

        return self._read_with_retry(_call)

    def get_balance(self) -> dict:
        """Return portfolio balance. Credentials required."""
        self._require_auth()

        def _call() -> dict:
            self._read_bucket.acquire()
            resp = self._portfolio_api.get_balance()
            return resp.model_dump()

        return self._read_with_retry(_call)

    def get_positions(
        self,
        ticker: str | None = None,
        *,
        settlement_status: str | None = "all",
        limit: int = 200,
        max_pages: int = 0,
    ) -> list[dict]:
        """
        Portfolio positions, including SETTLED ones. Credentials required.

        Route: GET /portfolio/positions

        Uses raw httpx + kalshi_headers rather than the SDK, for two reasons:

          * The SDK path was BROKEN. It read `resp.market_positions`, but
            GetPositionsResponse's field is `positions` — pydantic raises
            AttributeError on every call, so this method could never have
            returned a position. (Confirmed against the installed SDK, 2026-08.)
          * The SDK's get_positions exposes no `settlement_status` parameter,
            so it cannot ask for settled positions — which are the only ones
            that carry a realized outcome. See kbot.live_settlement.

        `settlement_status` is "all" by default so a market that has already
        settled still comes back; a settled position is exactly what live
        outcome reconciliation needs. The parameter is dropped and the request
        retried once if the API rejects it (400), so an API that doesn't know
        the filter degrades to its default rather than hard-failing the
        reconciliation.

        `max_pages` 0 (default) follows the cursor to the end — a caller asking
        for one ticker's settled position must not silently miss it because it
        fell on page two. Pass a positive value to bound the walk.

        Each returned dict carries Kalshi's own accounting for that market:
        `market_result` ("yes"/"no", empty until settled), `realized_pnl`,
        `fees_paid`, `total_cost` (all in CENTS), and `position` (signed net
        contracts, 0 once settled). kbot.live_settlement converts and
        interprets these; nothing else should read the raw cent fields.
        """
        self._require_auth()

        endpoint = "/portfolio/positions"
        sign_path = self._api_path_prefix + endpoint

        def _page(cursor: str | None, with_settlement: bool) -> httpx.Response:
            self._read_bucket.acquire()
            headers = kalshi_headers(
                "GET", sign_path, self._cfg.kalshi_key_id, self._private_key
            )
            params: dict[str, Any] = {"limit": limit}
            if ticker:
                params["ticker"] = ticker
            if with_settlement and settlement_status:
                params["settlement_status"] = settlement_status
            if cursor:
                params["cursor"] = cursor
            return self._http.get(
                self._cfg.base_url + endpoint, headers=headers, params=params
            )

        all_positions: list[dict] = []
        cursor: str | None = None
        page = 0
        send_settlement = True

        while True:
            page += 1
            resp = _page(cursor, send_settlement)
            if resp.status_code == 400 and send_settlement:
                # The filter isn't supported here — fall back to the API's
                # default rather than losing the whole reconciliation.
                log.warning(
                    "positions.settlement_status_rejected",
                    settlement_status=settlement_status,
                    body=resp.text[:200],
                )
                send_settlement = False
                page -= 1
                continue
            resp.raise_for_status()
            data = resp.json()

            # "positions" is the current field; "market_positions" is accepted
            # because the previous SDK-based implementation named it that and a
            # mismatch here is precisely the failure being fixed.
            page_positions = data.get("positions")
            if page_positions is None:
                page_positions = data.get("market_positions") or []
            all_positions.extend(page_positions)

            cursor = data.get("cursor") or None
            if not cursor or not page_positions:
                break
            if max_pages and page >= max_pages:
                break

        return all_positions

    def get_events(
        self,
        series_ticker: str | None = None,
        status: str | None = None,
        limit: int = 50,
        with_nested_markets: bool = True,
        *,
        max_pages: int = 1,
    ) -> list[dict]:
        """
        Return events (optionally filtered by series_ticker and status).

        Uses raw httpx + RSA-PSS signing; bypasses the SDK because the SDK's
        deserialization crashes on newer status enum values.

        The API paginates via a `cursor` field in the response, which this
        method ignored entirely until 2026-07 -- every call silently
        returned just the first page (<= `limit` events) with no error or
        warning, so a caller trying to build a multi-day history window
        (scripts/pull_history.py) got nowhere near the depth it asked for.
        `max_pages` controls how many pages to follow (default 1 preserves
        the old single-page behavior for existing single-page callers);
        pass a higher value, or 0 to follow the cursor until the API stops
        returning one, to page back further into history.
        Credentials required.
        """
        self._require_auth()

        endpoint = "/events"
        sign_path = self._api_path_prefix + endpoint

        all_events: list[dict] = []
        cursor: str | None = None
        page = 0
        while True:
            page += 1
            self._read_bucket.acquire()
            headers = kalshi_headers(
                "GET", sign_path, self._cfg.kalshi_key_id, self._private_key
            )

            params: dict[str, Any] = {"limit": limit}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if status:
                params["status"] = status
            if with_nested_markets:
                params["with_nested_markets"] = "true"
            if cursor:
                params["cursor"] = cursor

            resp = self._http.get(self._cfg.base_url + endpoint, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            page_events = data.get("events", [])
            all_events.extend(page_events)

            cursor = data.get("cursor") or None
            if not cursor or not page_events:
                break
            if max_pages and page >= max_pages:
                break

        return all_events

    def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> list[dict]:
        """
        Return raw OHLC candlesticks for `market_ticker` in `series_ticker`.

        Route: GET /series/{series_ticker}/markets/{market_ticker}/candlesticks

        Uses raw httpx + kalshi_headers, bypassing the SDK: the SDK's
        Candlestick model (flat start_ts/end_ts/open/high/low/close/volume
        floats) does NOT match the real response shape below — every field
        silently deserializes to None, no error raised. Confirmed live
        2026-07 by comparing this method's raw JSON against
        MarketsApi.get_market_candlesticks(...).model_dump().

        Real response shape per candle (confirmed live, 2026-07):
            end_period_ts: int   (no start_ts field — only the period end)
            price: {mean_dollars, open_dollars, high_dollars, low_dollars,
                    close_dollars, previous_dollars} — actual trade prices;
                    {} or just previous_dollars if nothing traded that period.
            yes_ask: {open_dollars, high_dollars, low_dollars, close_dollars}
                    — DOLLAR STRINGS ("0.0100"), not cents.
            yes_bid: same shape as yes_ask. All-zero strings ("0.0000") mean
                    no resting bid was observed that period, not that the
                    bid price was literally zero — treat as missing data.
            volume_fp / open_interest_fp: fixed-point string numbers.
            No separate no_ask/no_bid field exists — derive via
            no_ask = 1 - yes_bid (Kalshi's YES/NO price identity).

        Required params: start_ts and end_ts are mandatory on the live API
        despite the SDK marking them optional (confirmed via 400 "Query
        argument ... is required" when omitted). period_interval must be
        1 (minute), 60 (hour), or 1440 (day) — any other value 400s with a
        oneof validation error; validated client-side here for a clearer
        error message.

        The API caps each response at 5000 candlesticks — callers must
        chunk wide time ranges themselves (scripts/pull_history.py does).
        Credentials required. Not retried (matches get_events()'s policy
        for hand-rolled httpx reads).
        """
        self._require_auth()
        if period_interval not in (1, 60, 1440):
            raise ValueError(
                f"period_interval must be 1, 60, or 1440 (minute/hour/day); got {period_interval!r}"
            )

        self._read_bucket.acquire()

        endpoint = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
        sign_path = self._api_path_prefix + endpoint
        headers = kalshi_headers(
            "GET", sign_path, self._cfg.kalshi_key_id, self._private_key
        )
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}

        resp = self._http.get(self._cfg.base_url + endpoint, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json().get("candlesticks", [])

    # ── Order writes ──────────────────────────────────────────────────────────
    # All writes go through httpx (not the SDK) so we control:
    #   • the endpoint path (V2)
    #   • the price format (dollar strings, not integer cents)
    #   • the retry policy (none — double-submission risk)

    def place_order(
        self,
        *,
        ticker: str,
        side: str,                  # "yes" or "no" — the bot's own model
        count: int,
        price_dollars: float,       # price of THAT side, e.g. NO @ 0.30
        client_order_id: str | None = None,
        time_in_force: str = TIF_IMMEDIATE_OR_CANCEL,
        self_trade_prevention_type: str = STP_TAKER_AT_CROSS,
    ) -> OrderAck:
        """
        Place an order via POST /portfolio/events/orders (V2).

        Takes the bot's (yes|no, side-denominated dollar price) and converts
        it to the exchange's YES-book (bid|ask, YES price) — see
        to_v2_side_and_price. A NO order goes out as an ask at (1 - price).

        Defaults to immediate_or_cancel: cross now against resting liquidity,
        cancel whatever does not fill. Partial fills are therefore possible
        and are reported in the returned ack's fill_count_units — callers must
        record the FILLED quantity, never the requested one.

        DRY_RUN=True → logs the would-be order and returns a simulated ack
                       with a complete fill at the requested price.
        LIVE         → single POST, no retry on any error.
        """
        if count <= 0:
            raise ValueError(f"count must be positive; got {count!r}")

        coid = client_order_id or str(uuid.uuid4())
        # Raises on a malformed side or an out-of-range price, before anything
        # touches the network.
        v2_side, v2_price = to_v2_side_and_price(side, price_dollars)

        payload: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": coid,
            "side": v2_side,                             # "bid" | "ask"
            "count": f"{count:.{_COUNT_DECIMALS}f}",     # "10.00"
            "price": f"{v2_price:.{_PRICE_DECIMALS}f}",  # "0.7000"
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
        }

        if self._cfg.dry_run:
            log.info(
                "dry_run.order",
                ticker=ticker,
                side=side,
                v2_side=v2_side,
                count=count,
                price=price_dollars,
                v2_price=payload["price"],
                client_order_id=coid,
            )
            return OrderAck(
                client_order_id=coid,
                order_id=None,
                dry_run=True,
                fill_count_units=count * UNITS_PER_CONTRACT,
                remaining_count_units=0,
                average_fill_price=round(price_dollars, _PRICE_DECIMALS),
                raw={"simulated": True, **payload},
            )

        self._require_auth()
        self._write_bucket.acquire()

        endpoint = "/portfolio/events/orders"
        sign_path = self._api_path_prefix + endpoint
        headers = kalshi_headers(
            "POST", sign_path, self._cfg.kalshi_key_id, self._private_key
        )

        log.info(
            "order.placing",
            ticker=ticker,
            side=side,
            v2_side=v2_side,
            count=count,
            price=price_dollars,
            v2_price=payload["price"],
            time_in_force=time_in_force,
            client_order_id=coid,
        )
        resp = self._http.post(
            self._cfg.base_url + endpoint,
            headers=headers,
            json=payload,
        )

        if resp.status_code not in (200, 201):
            log.error(
                "order.rejected",
                status=resp.status_code,
                body=resp.text[:500],
                client_order_id=coid,
            )
            # Never retry — surface for manual review.
            resp.raise_for_status()

        data = resp.json()

        # V2 returns these flat. The nested fallback keeps a response that
        # still wraps them under "order" from silently yielding order_id=None,
        # which would strand the order as unidentifiable for cancellation.
        nested = data.get("order") or {}
        order_id = data.get("order_id") or nested.get("order_id")

        fill_count_units = _parse_count_units(
            data.get("fill_count", nested.get("fill_count")), 0
        )
        remaining_count_units = _parse_count_units(
            data.get("remaining_count", nested.get("remaining_count")),
            max(count * UNITS_PER_CONTRACT - fill_count_units, 0),
        )

        avg_yes = _parse_fixed_point(
            data.get("average_fill_price", nested.get("average_fill_price"))
        )
        if fill_count_units <= 0:
            average_fill_price = None
        elif avg_yes is None:
            # Filled, but the exchange did not report an average. Fall back to
            # the limit price: an IOC never fills worse than its limit, so this
            # is a conservative cost basis rather than a fabricated one.
            average_fill_price = round(price_dollars, _PRICE_DECIMALS)
        else:
            average_fill_price = from_v2_fill_price(side, avg_yes)

        log.info(
            "order.placed",
            order_id=order_id,
            client_order_id=coid,
            requested_count=count,
            fill_count_units=fill_count_units,
            remaining_count_units=remaining_count_units,
            average_fill_price=average_fill_price,
        )
        if fill_count_units <= 0:
            log.warning(
                "order.unfilled",
                ticker=ticker,
                side=side,
                client_order_id=coid,
                order_id=order_id,
                reason="immediate_or_cancel_no_liquidity",
            )
        elif fill_count_units < count * UNITS_PER_CONTRACT:
            log.warning(
                "order.partial_fill",
                ticker=ticker,
                side=side,
                client_order_id=coid,
                requested_count=count,
                fill_count_units=fill_count_units,
            )

        return OrderAck(
            client_order_id=coid,
            order_id=order_id,
            dry_run=False,
            fill_count_units=fill_count_units,
            remaining_count_units=remaining_count_units,
            average_fill_price=average_fill_price,
            raw=data,
        )

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel by order_id.

        DRY_RUN=True → logs and returns a simulated ack.
        No retry on failure.
        """
        if self._cfg.dry_run:
            log.info("dry_run.cancel", order_id=order_id)
            return {"simulated": True, "order_id": order_id}

        self._require_auth()
        self._write_bucket.acquire()

        # V2 path — DELETE /portfolio/orders/{id} was deprecated alongside the
        # create route (410 Gone). Not reachable from the live loop today
        # (MakerExecutor, its only caller, is not wired into Runner), migrated
        # here so it cannot 410 the moment it is.
        endpoint = f"/portfolio/events/orders/{order_id}"
        sign_path = self._api_path_prefix + endpoint
        headers = kalshi_headers(
            "DELETE", sign_path, self._cfg.kalshi_key_id, self._private_key
        )

        log.info("order.cancelling", order_id=order_id)
        resp = self._http.delete(self._cfg.base_url + endpoint, headers=headers)

        if resp.status_code not in (200, 204):
            log.error(
                "cancel.rejected",
                status=resp.status_code,
                body=resp.text[:500],
                order_id=order_id,
            )
            resp.raise_for_status()

        log.info("order.cancelled", order_id=order_id)
        return resp.json() if resp.content else {}

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _public_markets_api() -> kalshi_python.MarketsApi:
        no_auth = kalshi_python.Configuration(
            host="https://api.elections.kalshi.com/trade-api/v2"
        )
        return kalshi_python.MarketsApi(kalshi_python.ApiClient(no_auth))

    @staticmethod
    def _public_series_api() -> kalshi_python.SeriesApi:
        no_auth = kalshi_python.Configuration(
            host="https://api.elections.kalshi.com/trade-api/v2"
        )
        return kalshi_python.SeriesApi(kalshi_python.ApiClient(no_auth))

    def _require_auth(self) -> None:
        if not self._authed:
            raise RuntimeError(
                "Authed endpoint called without credentials. "
                "Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env."
            )

    @staticmethod
    def _read_with_retry(fn, max_attempts: int = 4):
        """
        Call `fn()` up to `max_attempts` times, backing off on retryable errors.
        Only for idempotent GETs — never wrap write calls with this.
        """
        for attempt in range(max_attempts):
            try:
                return fn()
            except ApiException as exc:
                if exc.status in _RETRYABLE and attempt < max_attempts - 1:
                    wait = (2 ** attempt) * (0.5 + random.random() * 0.5)
                    log.warning(
                        "sdk.retrying",
                        status=exc.status,
                        attempt=attempt + 1,
                        wait_s=round(wait, 2),
                    )
                    time.sleep(wait)
                else:
                    raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _RETRYABLE and attempt < max_attempts - 1:
                    wait = (2 ** attempt) * (0.5 + random.random() * 0.5)
                    log.warning(
                        "http.retrying",
                        status=exc.response.status_code,
                        attempt=attempt + 1,
                        wait_s=round(wait, 2),
                    )
                    time.sleep(wait)
                else:
                    raise
        # unreachable, but satisfies type checkers
        raise RuntimeError("_read_with_retry exhausted without return or raise")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
