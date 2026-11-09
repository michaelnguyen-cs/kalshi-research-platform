#!/usr/bin/env python
"""
THROWAWAY DIAGNOSTIC -- capture the real shape of Kalshi's orderbook_delta.

Why this exists
---------------
kbot.runner._apply_delta reads the SNAPSHOT's keys (no_dollars_fp /
yes_dollars_fp) out of orderbook_delta messages. If the delta payload does
not carry those keys, _update_side() rebuilds the identical level list and
every delta is a silent no-op -- each book freezes at its subscribe-time
snapshot for the contract's whole life.

The runtime logs can't settle the key names: the websockets debug logger
truncates the middle of every ~259-byte delta frame
(`'{"type":"orderbook_delta","sid":1,"seq":2,"msg"...s_ms":...}}' [259 bytes]`).
This script prints the payloads in full so the fix can be written against
the real shape instead of a guess.

Read-only: subscribes to one ticker on its own WS session, prints, exits.
It places no orders, writes no files, and touches no database. Running it
does not disturb a running kbot process (a separate WS session is fine).

Usage
-----
    python scripts/capture_delta.py
    python scripts/capture_delta.py --ticker KXBTC15M-26AUG061530-30
    python scripts/capture_delta.py --deltas 5 --timeout 120

Output is the first orderbook_snapshot and the first N orderbook_delta
messages, pretty-printed and untruncated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Allow running straight from a checkout without an editable install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kbot.config import settings  # noqa: E402
from kbot.kalshi_client import KalshiClient  # noqa: E402
from kbot.ws_client import WSClient  # noqa: E402

SERIES = "KXBTC15M"


def _quiet_logs() -> None:
    """
    Silence the library debug chatter so the captured payloads are the only
    thing on stdout. The websockets frame logger is exactly what truncates
    the delta bodies, so it is noise here, not signal.
    """
    logging.basicConfig(level=logging.WARNING)
    for name in ("websockets", "websockets.client", "httpx", "httpcore", "kbot"):
        logging.getLogger(name).setLevel(logging.WARNING)


def pick_ticker() -> str:
    """
    One currently-open KXBTC15M market ticker.

    Uses the same discovery call the runner uses
    (KalshiClient.get_events(series_ticker=..., status="open")), then prefers
    the EARLIEST close_time still in the future -- that's the window actively
    trading right now, which is the one emitting deltas. Falls back to the
    first ticker alphabetically if no usable close_time is present.
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

    def close_key(m: dict):
        # close_time is an ISO-8601 string on the market record; lexical sort
        # is chronological for ISO-8601 with a fixed offset, which is all the
        # ordering we need here.
        return str(m.get("close_time") or m.get("expiration_time") or "")

    dated = [m for m in markets if close_key(m)]
    chosen = min(dated, key=close_key) if dated else sorted(markets, key=lambda m: m["ticker"])[0]

    print(f"# open {SERIES} markets: {sorted(m['ticker'] for m in markets)}")
    print(f"# chosen ticker: {chosen['ticker']}  close_time={chosen.get('close_time')}")
    return chosen["ticker"]


async def capture(ticker: str, want_deltas: int, timeout: float) -> int:
    """
    Subscribe to `ticker` on the orderbook_delta channel and print the first
    snapshot plus the first `want_deltas` deltas in full. Returns an exit code.

    WSClient hands the handler the already-parsed dict, so what's printed is a
    re-serialisation rather than the byte-exact frame -- lossless for key
    names, nesting and value types, which is what the fix needs. Raw byte
    lengths are printed alongside so they can be checked against the
    255-260 byte deltas / 4503 byte snapshot seen in the logs.
    """
    done = asyncio.Event()
    seen = {"snapshot": 0, "delta": 0}

    def on_message(msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "orderbook_snapshot" and seen["snapshot"] == 0:
            seen["snapshot"] += 1
            body = json.dumps(msg)
            print("\n" + "=" * 72)
            print(f"ORDERBOOK_SNAPSHOT  ({len(body)} bytes re-serialised)")
            print("=" * 72)
            print(json.dumps(msg, indent=2))
        elif msg_type == "orderbook_delta" and seen["delta"] < want_deltas:
            seen["delta"] += 1
            body = json.dumps(msg)
            print("\n" + "=" * 72)
            print(f"ORDERBOOK_DELTA #{seen['delta']}  ({len(body)} bytes re-serialised)")
            print("=" * 72)
            print(json.dumps(msg, indent=2))
            print("\n  msg keys: " + repr(sorted(msg.get("msg", {}).keys())))

        if seen["snapshot"] >= 1 and seen["delta"] >= want_deltas:
            done.set()

    client = WSClient(
        settings,
        tickers=[ticker],
        channels=["orderbook_delta"],
        on_message=on_message,
    )

    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print(
            f"\n# TIMEOUT after {timeout:.0f}s -- captured "
            f"{seen['snapshot']} snapshot(s), {seen['delta']} delta(s).",
            file=sys.stderr,
        )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    if seen["delta"] == 0:
        print(
            "# No deltas captured. The window may be settled or untraded -- "
            "retry against an actively-trading ticker.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", help=f"Market ticker (default: auto-pick an open {SERIES} window)")
    ap.add_argument("--deltas", type=int, default=3, help="How many delta messages to capture (default 3)")
    ap.add_argument("--timeout", type=float, default=90.0, help="Give up after this many seconds (default 90)")
    args = ap.parse_args()

    _quiet_logs()
    ticker = args.ticker or pick_ticker()
    return asyncio.run(capture(ticker, args.deltas, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
