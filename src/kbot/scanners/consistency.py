"""
Intra-Kalshi consistency scanner — pure-math structural checks on the live
books we already stream (MARKET_RESEARCH.md Source 6).

Two checks, both ALERT + LOG ONLY — neither auto-executes. These surface
"structural freebies" (Source 6: the mechanism most automated prediction-
market profit actually comes from), but acting on them safely needs
simultaneous two-leg execution and staleness/liquidity checks this scanner
doesn't do. That's a deliberate scope boundary, not an oversight.

(a) scan_same_market_arb: YES ask + NO ask < $1, net of round-trip taker
    fees, on the SAME market. Buying both sides locks in a $1 payout for
    less than $1 — a guaranteed profit before execution risk.
(b) scan_monotonicity: within one expiry's "above"/"below" strike ladder,
    YES ask must move monotonically with strike (e.g. "above 60k" must be
    priced >= "above 61k" — it's the easier outcome to clear). A violation
    is a logical mispricing between two related contracts.

Both checks are O(n) / O(n log n) over the tickers passed in — negligible
cost even at a high WS message rate, so run_consistency_scan() is meant to
be called on every book-update batch (see paper.py's _on_ws_message).

Both are also STATELESS and re-report every detection still standing on each
call. That makes them cheap and easy to reason about, but it means a caller
that persists detections must dedupe: the same violation is returned on every
message until the book moves. See same_market_arb_identity /
monotonicity_violation_identity for the (key, fingerprint) pair callers use
to tell "still true" apart from "newly true".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.edge import fee_per_contract
from kbot.models.types import OrderBook

log = structlog.get_logger(__name__)


@dataclass
class SameMarketArb:
    ticker: str
    yes_ask: float
    no_ask: float
    gross_dollars: float
    net_dollars: float


@dataclass
class MonotonicityViolation:
    group_key: str
    direction: str
    lower_strike_ticker: str
    lower_strike: float
    lower_strike_yes_ask: float
    higher_strike_ticker: str
    higher_strike: float
    higher_strike_yes_ask: float


# ── Check (a): same-market YES+NO < $1 ──────────────────────────────────────────

def scan_same_market_arb(
    books: dict[str, OrderBook],
    cfg: Settings | None = None,
) -> list[SameMarketArb]:
    """YES ask + NO ask < $1, net of round-trip taker fees, on the same market."""
    cfg = cfg or _default_settings
    hits: list[SameMarketArb] = []
    for ticker, book in books.items():
        best_yes = book.best_yes()
        best_no = book.best_no()
        if best_yes is None or best_no is None:
            continue
        yes_ask, no_ask = best_yes.price, best_no.price
        gross = 1.0 - (yes_ask + no_ask)
        fees = fee_per_contract(yes_ask, cfg) + fee_per_contract(no_ask, cfg)
        net = gross - fees
        if net > 0:
            hits.append(SameMarketArb(
                ticker=ticker, yes_ask=yes_ask, no_ask=no_ask,
                gross_dollars=gross, net_dollars=net,
            ))
    return hits


# ── Check (b): strike-ladder monotonicity ───────────────────────────────────────

def scan_monotonicity(
    books: dict[str, OrderBook],
    ticker_parser: Callable[[str], tuple[float, str, str] | None],
) -> list[MonotonicityViolation]:
    """
    Within each (group_key, direction) strike ladder, YES ask must be
    monotonic in strike:
      "above K": YES ask must NOT increase as K increases (harder to clear
                 a higher bar should never cost more).
      "below K": YES ask must NOT decrease as K increases (easier to clear
                 a higher bar should never cost less).
    Reports every adjacent pair (sorted by strike) that violates this.

    `ticker_parser(ticker) -> (strike, direction, group_key) | None` is
    supplied by the caller (a Strategy that knows its own ticker format,
    e.g. BTCHourlyStrategy.parse_strike) so this module stays strategy-
    agnostic. Unparseable tickers and directions other than "above"/"below"
    are skipped.
    """
    groups: dict[tuple[str, str], list[tuple[float, str, float]]] = {}
    for ticker, book in books.items():
        parsed = ticker_parser(ticker)
        if parsed is None:
            continue
        strike, direction, group_key = parsed
        if direction not in ("above", "below"):
            continue
        best_yes = book.best_yes()
        if best_yes is None:
            continue
        groups.setdefault((group_key, direction), []).append((strike, ticker, best_yes.price))

    violations: list[MonotonicityViolation] = []
    for (group_key, direction), entries in groups.items():
        entries.sort(key=lambda e: e[0])
        for (lo_strike, lo_ticker, lo_price), (hi_strike, hi_ticker, hi_price) in zip(entries, entries[1:]):
            if lo_strike == hi_strike:
                continue
            violated = hi_price > lo_price if direction == "above" else hi_price < lo_price
            if violated:
                violations.append(MonotonicityViolation(
                    group_key=group_key,
                    direction=direction,
                    lower_strike_ticker=lo_ticker,
                    lower_strike=lo_strike,
                    lower_strike_yes_ask=lo_price,
                    higher_strike_ticker=hi_ticker,
                    higher_strike=hi_strike,
                    higher_strike_yes_ask=hi_price,
                ))
    return violations


# ── Combined entry point ─────────────────────────────────────────────────────────

def run_consistency_scan(
    books: dict[str, OrderBook],
    ticker_parser: Callable[[str], tuple[float, str, str] | None] | None = None,
    cfg: Settings | None = None,
) -> dict[str, list[Any]]:
    """
    Run both checks and log a warning for every detection. Never raises —
    a scanner bug must not crash the WS/trading loop it's attached to.

    `ticker_parser` is optional; when None, the monotonicity check is
    skipped entirely (same_market_arb only needs the books themselves).

    Returns {"same_market_arb": [...], "monotonicity": [...]} so callers
    can alert / persist the detections (see paper.py wiring, which pushes
    each hit to the Sheets Arb Log tab).
    """
    cfg = cfg or _default_settings
    same_market: list[SameMarketArb] = []
    monotonicity: list[MonotonicityViolation] = []

    try:
        same_market = scan_same_market_arb(books, cfg)
        for hit in same_market:
            log.warning(
                "scanner.same_market_arb",
                ticker=hit.ticker,
                yes_ask=hit.yes_ask,
                no_ask=hit.no_ask,
                net_dollars=round(hit.net_dollars, 4),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("scanner.same_market_arb_failed", error=str(exc))

    if ticker_parser is not None:
        try:
            monotonicity = scan_monotonicity(books, ticker_parser)
            for v in monotonicity:
                log.warning(
                    "scanner.monotonicity_violation",
                    group=v.group_key,
                    direction=v.direction,
                    lower_ticker=v.lower_strike_ticker,
                    lower_strike=v.lower_strike,
                    lower_price=v.lower_strike_yes_ask,
                    higher_ticker=v.higher_strike_ticker,
                    higher_strike=v.higher_strike,
                    higher_price=v.higher_strike_yes_ask,
                )
        except Exception as exc:  # noqa: BLE001
            log.error("scanner.monotonicity_failed", error=str(exc))

    return {"same_market_arb": same_market, "monotonicity": monotonicity}


# ── Sheet row builders ───────────────────────────────────────────────────────────

def _ts_utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def same_market_arb_to_row(hit: SameMarketArb) -> dict[str, Any]:
    """One kbot.sheets Arb Log row for a same-market arb detection."""
    return {
        "ts_utc": _ts_utc_now(),
        "check_type": "same_market_arb",
        "ticker_a": hit.ticker,
        "ticker_b": "",
        "detail": f"yes_ask={hit.yes_ask} no_ask={hit.no_ask} gross={round(hit.gross_dollars, 4)}",
        "net_dollars": round(hit.net_dollars, 4),
    }


def monotonicity_violation_to_row(v: MonotonicityViolation) -> dict[str, Any]:
    """One kbot.sheets Arb Log row for a monotonicity violation."""
    return {
        "ts_utc": _ts_utc_now(),
        "check_type": "monotonicity",
        "ticker_a": v.lower_strike_ticker,
        "ticker_b": v.higher_strike_ticker,
        "detail": (
            f"direction={v.direction} "
            f"lower_strike={v.lower_strike}@{v.lower_strike_yes_ask} "
            f"higher_strike={v.higher_strike}@{v.higher_strike_yes_ask}"
        ),
        "net_dollars": "",
    }


# ── Detection identity (arb-log dedupe) ──────────────────────────────────────────
#
# These scans run on EVERY book update and re-report every detection that is
# still standing, so "detection found" is not the same event as "something
# happened". A monotonicity violation that persists across a thousand messages
# is one fact, not a thousand -- and logging it a thousand times is what drove
# the Arb Log tab into Google's 10M-cell workbook ceiling.
#
# Each detection therefore gets (key, fingerprint):
#   key         WHICH detection this is -- stable while the condition stands.
#   fingerprint WHAT it currently looks like -- changes when the prices do.
# The caller re-logs only when the key is new or its fingerprint moved (see
# Runner._run_consistency_scan).
#
# Prices are fingerprinted at whole-cent resolution because that is the tick
# Kalshi actually quotes on: sub-cent noise cannot represent a real change in
# the detection, and rounding it away is the difference between "a row per
# genuine move" and "a row per float wobble".

def _cents(price: float) -> int:
    return round(price * 100)


def same_market_arb_identity(hit: SameMarketArb) -> tuple[tuple, str]:
    """(key, fingerprint) for a same-market arb detection."""
    return (
        ("same_market_arb", hit.ticker),
        f"{_cents(hit.yes_ask)}/{_cents(hit.no_ask)}",
    )


def monotonicity_violation_identity(v: MonotonicityViolation) -> tuple[tuple, str]:
    """
    (key, fingerprint) for a monotonicity violation.

    Keyed on the specific PAIR of tickers, not the ladder: two independent
    violations in one group are two facts and must both be logged.
    """
    return (
        ("monotonicity", v.group_key, v.direction,
         v.lower_strike_ticker, v.higher_strike_ticker),
        f"{_cents(v.lower_strike_yes_ask)}/{_cents(v.higher_strike_yes_ask)}",
    )
