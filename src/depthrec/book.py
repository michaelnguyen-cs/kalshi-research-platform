"""
Canonical YES-scale order book: snapshot, sequenced deltas, hard invariants.

Section 6 of the frozen mechanics document: everything here is in the unified
YES-leg price scale, which is what `use_yes_price: true` makes the wire format
deliver. NO economics are derived algebraically from this one book; no second
crossed convention is ever maintained.

Wire format (docs.kalshi.com AsyncAPI, verified 2026-08-22):

    orderbook_snapshot.msg
        yes_dollars_fp: [[price_dollars, contract_count_fp], ...]  (optional)
        no_dollars_fp : [[price_dollars, contract_count_fp], ...]  (optional)
    orderbook_delta.msg
        price_dollars, delta_fp, side ("yes"|"no"), ts_ms (optional)

Both fields carry RESTING BIDS, not offers -- Kalshi's book is quoted as bids on
each leg. Under `use_yes_price: true` the no side is reported in yes-leg
pricing, so a resting NO bid that would fill a YES buy at 0.70 appears at
price_dollars "0.7000". That makes the canonical mapping:

    yes side  ->  the YES BID ladder   (best bid = highest price)
    no side   ->  the YES ASK ladder   (best ask = lowest price)

which is why "size at the best ask" -- the quantity section 4 tests against
X = 10 -- is a size on the NO-side array. Getting this backwards is exactly the
book-convention bug this project has hit three times, so the direction is
asserted by the invariants below and again by the section 8 equivalence test.

Prices and quantities are held as integers, never floats. A price is tenths of
a cent (0.9950 -> 9950); a quantity is hundredths of a contract, matching the
API's 2-decimal fixed point (10 contracts -> 1000). Float arithmetic on deci-
cent price levels rounds two ladders onto the same key eventually, and a book
that silently merges levels is worse than one that crashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

PRICE_SCALE = 10_000
"""Price integers are dollars * 10_000, i.e. tenths of a cent."""

QTY_SCALE = 100
"""Quantity integers are contracts * 100, matching the API's `_fp` 2-decimal
fixed point."""

SIDE_YES = "yes"
SIDE_NO = "no"

MIN_PRICE = 1
MAX_PRICE = PRICE_SCALE - 1
"""Valid canonical price range, exclusive of 0.0000 and 1.0000. A level at
either endpoint is not a tradeable quote."""


class BookError(ValueError):
    """A message could not be decoded, or a decoded book broke an invariant."""


def to_price(raw: Any) -> int:
    """
    Dollar string/number -> integer tenths of a cent.

    Decimal, not float: "0.9950" through float() is 0.9949999999999999 on some
    inputs, and int(round(...)) then depends on the platform's rounding.
    Anything finer than a tenth of a cent is a format this decoder has never
    seen and must not guess at.
    """
    try:
        dec = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BookError(f"unparseable price {raw!r}") from exc
    scaled = dec * PRICE_SCALE
    if scaled != scaled.to_integral_value():
        raise BookError(f"price {raw!r} is finer than a tenth of a cent")
    return int(scaled)


def to_qty(raw: Any) -> int:
    """Fixed-point contract count/delta -> integer hundredths of a contract."""
    try:
        dec = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BookError(f"unparseable quantity {raw!r}") from exc
    scaled = dec * QTY_SCALE
    if scaled != scaled.to_integral_value():
        raise BookError(f"quantity {raw!r} is finer than 0.01 contracts")
    return int(scaled)


def price_dollars(price: int) -> float:
    """Canonical integer price back to dollars, for reporting only."""
    return price / PRICE_SCALE


def qty_contracts(qty: int) -> float:
    """Canonical integer quantity back to contracts, for reporting only."""
    return qty / QTY_SCALE


def contracts_to_qty(contracts: int) -> int:
    """Whole contracts -> the canonical quantity integer (X = 10 -> 1000)."""
    return contracts * QTY_SCALE


@dataclass
class TopOfBook:
    """
    Canonical top of book. `None` on a side means that side is empty.

    Comparison is by value across all four fields, which is precisely what
    section 8 condition 1 asks two streams to agree on.
    """

    bid_price: int | None
    bid_qty: int | None
    ask_price: int | None
    ask_qty: int | None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "best_bid": None if self.bid_price is None else price_dollars(self.bid_price),
            "bid_size": None if self.bid_qty is None else qty_contracts(self.bid_qty),
            "best_ask": None if self.ask_price is None else price_dollars(self.ask_price),
            "ask_size": None if self.ask_qty is None else qty_contracts(self.ask_qty),
        }

    def executable_ask(self, side: str) -> tuple[int | None, int | None]:
        """
        Section 2's "best executable ask on the side the frozen rule buys", with
        its displayed size. `(None, None)` when that side cannot be bought.

        Buying YES lifts a resting NO bid, which in canonical yes-leg pricing IS
        the ask ladder -- so the YES executable ask is the best ask, directly.

        Buying NO lifts a resting YES bid. A YES bid at p is an offer to sell NO
        at 1 - p, so the NO executable ask is `1 - best_bid` and the size behind
        it is the size at that BID level. This is the algebraic derivation
        section 6 requires: one canonical book, no second crossed convention.

        The two sides therefore read opposite ladders. Reporting the YES ask's
        depth for a NO-side signal would measure the wrong queue entirely -- and
        the frozen rule buys NO whenever the YES price is below 0.50, which on
        the pilot's own data is most of the time.
        """
        if side == "YES":
            return self.ask_price, self.ask_qty
        if side == "NO":
            if self.bid_price is None:
                return None, None
            return PRICE_SCALE - self.bid_price, self.bid_qty
        raise BookError(f"unknown side {side!r}; expected YES or NO")


@dataclass
class CanonicalBook:
    """
    One market's reconstructed book in canonical YES-leg coordinates.

    `bids` and `asks` map price -> quantity, both in canonical integers. A level
    is deleted the instant its quantity reaches zero, so an empty level can
    never masquerade as a real one and `len(bids)` is the true depth.
    """

    ticker: str
    bids: dict[int, int] = field(default_factory=dict)
    asks: dict[int, int] = field(default_factory=dict)

    snapshot_seq: int | None = None
    last_seq: int | None = None
    snapshot_recv_ts: float | None = None
    last_update_recv_ts: float | None = None
    last_update_exchange_ts_ms: int | None = None
    sid: Any = None
    updates_applied: int = 0

    def _ladder(self, side: str) -> dict[int, int]:
        if side == SIDE_YES:
            return self.bids
        if side == SIDE_NO:
            return self.asks
        raise BookError(f"unknown side {side!r}")

    def apply_snapshot(
        self,
        msg: dict[str, Any],
        *,
        seq: int,
        sid: Any,
        recv_ts: float,
        use_yes_price: bool,
    ) -> None:
        """
        Replace the entire book from an `orderbook_snapshot`.

        Wholesale replacement, never a merge: a snapshot is the server's
        complete view, and anything surviving from before it is by definition
        state the server no longer believes in.
        """
        self.bids = {}
        self.asks = {}
        for side, key in ((SIDE_YES, "yes_dollars_fp"), (SIDE_NO, "no_dollars_fp")):
            for level in msg.get(key) or ():
                if not isinstance(level, (list, tuple)) or len(level) != 2:
                    raise BookError(f"{self.ticker}: malformed {key} level {level!r}")
                price = _canonical_price(level[0], side=side, use_yes_price=use_yes_price)
                qty = to_qty(level[1])
                if qty < 0:
                    raise BookError(f"{self.ticker}: negative snapshot qty {level!r}")
                if qty == 0:
                    continue
                ladder = self._ladder(side)
                if price in ladder:
                    raise BookError(
                        f"{self.ticker}: duplicate {side} level at {price} in snapshot"
                    )
                ladder[price] = qty

        self.snapshot_seq = seq
        self.last_seq = seq
        self.sid = sid
        self.snapshot_recv_ts = recv_ts
        self.last_update_recv_ts = recv_ts
        self.last_update_exchange_ts_ms = None
        self.updates_applied = 0
        self.assert_invariants()

    def apply_delta(
        self,
        msg: dict[str, Any],
        *,
        seq: int,
        recv_ts: float,
        use_yes_price: bool,
    ) -> None:
        """
        Apply one `orderbook_delta`. Caller must have validated `seq` continuity
        first -- this method trusts that and only enforces book shape.
        """
        side = msg.get("side")
        if side not in (SIDE_YES, SIDE_NO):
            raise BookError(f"{self.ticker}: delta with unknown side {side!r}")
        price = _canonical_price(msg.get("price_dollars"), side=side, use_yes_price=use_yes_price)
        delta = to_qty(msg.get("delta_fp"))

        ladder = self._ladder(side)
        new_qty = ladder.get(price, 0) + delta
        if new_qty < 0:
            raise BookError(
                f"{self.ticker}: delta drove {side} level {price} to {new_qty} "
                f"(had {ladder.get(price, 0)}, delta {delta}) -- the book is not "
                f"the one the exchange is describing"
            )
        if new_qty == 0:
            ladder.pop(price, None)
        else:
            ladder[price] = new_qty

        self.last_seq = seq
        self.last_update_recv_ts = recv_ts
        ts_ms = msg.get("ts_ms")
        self.last_update_exchange_ts_ms = ts_ms if isinstance(ts_ms, int) else None
        self.updates_applied += 1
        self.assert_invariants()

    def top_of_book(self) -> TopOfBook:
        best_bid = max(self.bids) if self.bids else None
        best_ask = min(self.asks) if self.asks else None
        return TopOfBook(
            bid_price=best_bid,
            bid_qty=None if best_bid is None else self.bids[best_bid],
            ask_price=best_ask,
            ask_qty=None if best_ask is None else self.asks[best_ask],
        )

    def assert_invariants(self) -> None:
        """
        Section 7's hard invariants, checked after every reconstruction step.

        Raises rather than returning a flag: a book that violates one of these
        is not a degraded book, it is a wrong book, and the only safe thing to
        do is invalidate it and wait for a fresh snapshot.
        """
        for name, ladder in (("bid", self.bids), ("ask", self.asks)):
            for price, qty in ladder.items():
                if not (MIN_PRICE <= price <= MAX_PRICE):
                    raise BookError(
                        f"{self.ticker}: {name} price {price} outside "
                        f"[{MIN_PRICE}, {MAX_PRICE}]"
                    )
                if qty <= 0:
                    raise BookError(
                        f"{self.ticker}: {name} level {price} has non-positive qty {qty}"
                    )
        overlap = set(self.bids) & set(self.asks)
        if overlap:
            raise BookError(
                f"{self.ticker}: price level(s) {sorted(overlap)} present on both "
                f"sides -- duplicate/crossed levels after applying deltas"
            )
        top = self.top_of_book()
        if top.bid_price is not None and top.ask_price is not None:
            if top.bid_price > top.ask_price:
                raise BookError(
                    f"{self.ticker}: crossed book, yes_bid {top.bid_price} > "
                    f"yes_ask {top.ask_price}"
                )


def _canonical_price(raw: Any, *, side: str, use_yes_price: bool) -> int:
    """
    Wire price -> canonical YES-leg integer price.

    Under `use_yes_price: true` (production, section 6) both sides already
    arrive in yes-leg pricing and this is the identity. Under `false` -- which
    only the section 8 equivalence test's B stream uses -- a no-side price is in
    no-leg terms and its yes-leg equivalent is 1 - p. That single subtraction is
    the entire "transform B into YES coordinates" step of the equivalence test;
    keeping it here means both streams are compared through the same decoder,
    with one documented difference.
    """
    price = to_price(raw)
    if side == SIDE_NO and not use_yes_price:
        price = PRICE_SCALE - price
    return price


def iter_levels(ladder: dict[int, int], *, descending: bool) -> Iterable[tuple[int, int]]:
    """Price levels in book order: bids high-to-low, asks low-to-high."""
    for price in sorted(ladder, reverse=descending):
        yield price, ladder[price]
