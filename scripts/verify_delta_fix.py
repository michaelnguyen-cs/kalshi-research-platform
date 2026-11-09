#!/usr/bin/env python
"""
THROWAWAY DIAGNOSTIC -- prove the fixed _apply_delta actually moves the book.

Why this exists
---------------
capture_delta.py established the real V2 delta shape (a single signed level:
price_dollars / delta_fp / side) and runner._apply_delta was rewritten against
it. That's a code-reading argument, not evidence. The failure mode being fixed
was SILENT: the old snapshot-shaped handler rebuilt an identical level list on
every delta, so a frozen book and a working one look the same from the logs.

This script closes that loop on live data. It builds the book with the runner's
own snapshot handler (_parse_orderbook), feeds every live delta to the runner's
own _apply_delta, and prints best_yes / best_no / mid AFTER each one. Nothing
here reimplements the book logic -- both functions are imported from
kbot.runner, so a regression there fails this script.

Reading the result
------------------
    >1 distinct best_yes / best_no / mid over the run  -> deltas are landing
    exactly 1 distinct value while deltas stream        -> book is frozen; broken

Rejected deltas count too: _apply_delta returns False and logs a warning on an
unrecognised shape, and this script tallies those separately -- a run that is
"100 deltas, 100 rejected, 1 distinct mid" is a shape regression, not a quiet
market.

Read-only: its own WS session, no orders, no DB writes, no files touched. Safe
to run alongside a live kbot process (a second WS session is fine).

Usage
-----
    python scripts/verify_delta_fix.py
    python scripts/verify_delta_fix.py --seconds 120
    python scripts/verify_delta_fix.py --ticker KXBTC15M-26AUG061530-30

Exit code 0 = fix proven (book moved), 1 = not proven (frozen, or no data).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Allow running straight from a checkout without an editable install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kbot.config import settings  # noqa: E402
from kbot.kalshi_client import KalshiClient  # noqa: E402
from kbot.models.types import OrderBook  # noqa: E402

# THE POINT OF THIS SCRIPT: the production functions, not copies of them.
from kbot.runner import _apply_delta, _parse_orderbook  # noqa: E402
from kbot.ws_client import WSClient  # noqa: E402

SERIES = "KXBTC15M"


def _quiet_logs() -> None:
    """
    Silence library chatter, but leave `kbot` at WARNING: _apply_delta's
    rejection warnings are signal here, not noise.
    """
    logging.basicConfig(level=logging.WARNING)
    for name in ("websockets", "websockets.client", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("kbot").setLevel(logging.WARNING)


def pick_ticker() -> str:
    """
    One currently-open KXBTC15M ticker, discovered exactly as capture_delta.py
    (and the runner) does: get_events(series_ticker=..., status="open"), then
    the EARLIEST close_time still ahead -- the window trading right now, which
    is the one emitting deltas.
    """
    with KalshiClient(settings) as client:
        events = client.get_events(series_ticker=SERIES, status="open", limit=5)

    markets: list[dict] = []
    for event in events or []:
        for m in event.get("markets", []):
            if m.get("ticker"):
                markets.append(m)

    if not markets:
        raise SystemExit(
            f"No open {SERIES} markets returned by get_events. "
            f"Pass one explicitly: --ticker {SERIES}-..."
        )

    def close_key(m: dict) -> str:
        # ISO-8601 with a fixed offset sorts chronologically as a string.
        return str(m.get("close_time") or m.get("expiration_time") or "")

    dated = [m for m in markets if close_key(m)]
    chosen = min(dated, key=close_key) if dated else sorted(markets, key=lambda m: m["ticker"])[0]

    print(f"# open {SERIES} markets: {sorted(m['ticker'] for m in markets)}")
    print(f"# chosen ticker: {chosen['ticker']}  close_time={chosen.get('close_time')}")
    return chosen["ticker"]


def _fmt(value: float | None) -> str:
    return "  --  " if value is None else f"{value:.4f}"


def _quote(book: OrderBook) -> tuple[float | None, float | None, float | None]:
    """(best_yes ask, best_no ask, mid) -- straight off the model's own accessors."""
    best_y = book.best_yes()
    best_n = book.best_no()
    return (
        best_y.price if best_y else None,
        best_n.price if best_n else None,
        book.mid_price(),
    )


class Verifier:
    """
    Mirrors Runner._on_ws_message's book maintenance for a single ticker:
    snapshot -> _parse_orderbook (replace), delta -> _apply_delta (mutate),
    deltas before the first snapshot dropped. Everything else it adds is
    observation.
    """

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.book: OrderBook | None = None
        self.snapshots = 0
        self.applied = 0
        self.rejected = 0
        self.orphaned = 0  # deltas that arrived before any snapshot
        self.seen_yes: set[float | None] = set()
        self.seen_no: set[float | None] = set()
        self.seen_mid: set[float | None] = set()

    def on_message(self, msg: dict) -> None:
        msg_type = msg.get("type")
        data = msg.get("msg", {}) or {}
        seq = msg.get("seq")

        if msg_type == "orderbook_snapshot":
            # Same call the runner makes: self._books[ticker] = _parse_orderbook(...)
            self.book = _parse_orderbook(data.get("market_ticker", self.ticker), data)
            self.snapshots += 1
            yes, no, mid = _quote(self.book)
            self._record(yes, no, mid)
            print(
                f"\n[seq {str(seq):>6}] SNAPSHOT #{self.snapshots}  "
                f"levels: yes={len(self.book.yes)} no={len(self.book.no)}  "
                f"-> best_yes={_fmt(yes)}  best_no={_fmt(no)}  mid={_fmt(mid)}"
            )
            if self.snapshots > 1:
                print("            (re-snapshot: book replaced, not mutated)")
            return

        if msg_type != "orderbook_delta":
            return

        if self.book is None:
            self.orphaned += 1
            return

        price = data.get("price_dollars")
        delta_fp = data.get("delta_fp")
        side = data.get("side")

        # THE CALL UNDER TEST -- production _apply_delta, production book object.
        ok = _apply_delta(self.book, data)
        if ok:
            self.applied += 1
        else:
            self.rejected += 1

        yes, no, mid = _quote(self.book)
        self._record(yes, no, mid)
        print(
            f"[seq {str(seq):>6}] delta side={str(side):<3} "
            f"price={str(price):>8} delta_fp={str(delta_fp):>10} "
            f"{'ok ' if ok else 'REJ'} -> "
            f"best_yes={_fmt(yes)}  best_no={_fmt(no)}  mid={_fmt(mid)}"
        )

    def _record(self, yes: float | None, no: float | None, mid: float | None) -> None:
        self.seen_yes.add(yes)
        self.seen_no.add(no)
        self.seen_mid.add(mid)


async def verify(ticker: str, seconds: float) -> int:
    v = Verifier(ticker)
    client = WSClient(
        settings,
        tickers=[ticker],
        channels=["orderbook_delta"],
        on_message=v.on_message,
    )

    print(f"\n# subscribing to {ticker} for {seconds:.0f}s (orderbook_delta channel)")
    print("# every line below is the book state AFTER the real _apply_delta ran\n")

    started = time.monotonic()
    task = asyncio.create_task(client.run())
    try:
        # Wait out the clock, but bail early if the WS task dies on its own.
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if done:
            exc = task.exception()
            if exc is not None:
                print(f"\n# WS session ended early: {exc!r}", file=sys.stderr)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    elapsed = time.monotonic() - started
    return _summarise(v, elapsed)


def _summarise(v: Verifier, elapsed: float) -> int:
    # A distinct-count of 1 is only meaningful against a stream of deltas that
    # were actually accepted, so both numbers are reported side by side.
    n_yes, n_no, n_mid = len(v.seen_yes), len(v.seen_no), len(v.seen_mid)

    print("\n" + "=" * 72)
    print(f"SUMMARY  {v.ticker}   {elapsed:.0f}s")
    print("=" * 72)
    print(f"  snapshots received        : {v.snapshots}")
    print(f"  deltas APPLIED (returned True) : {v.applied}")
    print(f"  deltas REJECTED (bad shape)    : {v.rejected}")
    if v.orphaned:
        print(f"  deltas dropped pre-snapshot    : {v.orphaned}")
    print()
    print(f"  distinct best_yes values  : {n_yes}   {sorted(x for x in v.seen_yes if x is not None)}")
    print(f"  distinct best_no  values  : {n_no}   {sorted(x for x in v.seen_no if x is not None)}")
    print(f"  distinct mid      values  : {n_mid}   {sorted(x for x in v.seen_mid if x is not None)}")
    print()

    if v.applied == 0:
        print("  VERDICT: INCONCLUSIVE -- no delta was applied.")
        if v.rejected:
            print(f"           {v.rejected} delta(s) rejected on shape: _apply_delta does NOT")
            print("           match the wire format. Re-run scripts/capture_delta.py.")
        else:
            print("           No deltas arrived at all -- untraded or settled window.")
            print("           Re-run against an actively-trading ticker.")
        return 1

    # Only re-snapshots can move the book without a delta, so require movement
    # that a single snapshot alone can't explain.
    moved = max(n_yes, n_no, n_mid) > 1
    if moved and v.snapshots <= 1:
        print(f"  VERDICT: FIX PROVEN -- {v.applied} deltas mutated the book from a single")
        print("           snapshot. best_yes/best_no/mid took multiple distinct values.")
        return 0
    if moved:
        print(f"  VERDICT: FIX PROVEN -- {v.applied} deltas applied and the quote moved.")
        print(f"           NOTE: {v.snapshots} snapshots arrived; re-run until a single-snapshot")
        print("           window is captured if you want deltas as the sole cause.")
        return 0

    print(f"  VERDICT: NOT PROVEN -- {v.applied} deltas applied but best_yes/best_no/mid")
    print("           never changed. Either the book is frozen (the bug is still")
    print("           live) or every delta touched a level behind the top of book.")
    print("           Re-run for longer before concluding it's broken.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--ticker", help=f"Market ticker (default: auto-pick an open {SERIES} window)")
    ap.add_argument("--seconds", type=float, default=60.0, help="How long to observe (default 60)")
    args = ap.parse_args()

    _quiet_logs()
    ticker = args.ticker or pick_ticker()
    return asyncio.run(verify(ticker, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
