#!/usr/bin/env python
"""
Run section 8's dual-stream equivalence test as an acceptance check.

    python scripts/depthrec_equivalence.py --db /mnt/depthrec/depthrec_equivalence.sqlite

Every sampling parameter comes from the frozen document via depthrec.frozen and
none is exposed as a flag -- a duration or tolerance you can pass on the command
line is a duration or tolerance you can change after seeing a result.

Two flags exist, and neither touches the protocol:

  --check-only   verify the frozen configuration, the pinned-classifier
                 integrity and the account's ability to hold two concurrent
                 WebSocket connections, then exit. This is the "confirm the
                 account permits two concurrent connections" precondition.
  --db           where to write. Validation data is engineering/test data and is
                 excluded from every feasibility and confirmatory sample, so it
                 goes to its own file and never to a pilot store.

A WebSocket disconnect does not reset the 60-minute duration clock and is not a
FAIL: per the 2026-08-22 amendment to section 13 of the frozen document, cells
where either stream is not VALID are simply unmatched, and disconnects,
reconnect durations and the unmatched-grid fraction are printed separately as
recorder-availability diagnostics. A connection that drops constantly returns
INSUFFICIENT, never a PASS.

Exit codes: 0 PASS, 1 FAIL, 2 INSUFFICIENT, 3 could not run.

READ-ONLY against Kalshi. No order is ever placed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen, integrity  # noqa: E402
from depthrec import pinned_hashes as ph  # noqa: E402
from depthrec.equivalence import EquivalenceTest  # noqa: E402
from depthrec.session import ResearchSession, SubscriptionRejected  # noqa: E402
from kbot.config import settings  # noqa: E402
from kbot.log_config import configure_logging  # noqa: E402

# Validation data lives on the dedicated /mnt/depthrec block volume, in its
# own file, and never in a pilot store. Path only -- no retention change.
DEFAULT_DB = Path("/mnt/depthrec/depthrec_equivalence.sqlite")


async def _check_two_connections() -> bool:
    """
    Section 8's precondition: confirm the account permits two concurrent
    WebSocket connections.

    Opens both with an EMPTY ticker list so nothing is subscribed and no
    market data flows -- this tests the connection allowance and nothing else.
    """
    a = ResearchSession(tickers=[], use_yes_price=True, label="A")
    b = ResearchSession(tickers=[], use_yes_price=False, label="B")
    tasks = [asyncio.create_task(a.run()), asyncio.create_task(b.run())]
    try:
        for _ in range(100):
            if a.connected and b.connected:
                return True
            for task in tasks:
                if task.done() and task.exception() is not None:
                    raise task.exception()
            await asyncio.sleep(0.2)
        return False
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _probe_universe(db: Path, seconds: float) -> int:
    """
    Attempt the ENTIRE frozen universe on both streams and report what the
    server does with it.

    This is the section 8 limit branch's only legitimate experiment: attempt the
    whole thing once. If the server accepts it, the limit branch never activates
    and the frozen note needs no change. If it rejects, the exact payload is
    printed and this returns non-zero -- it does NOT retry with fewer markets.
    Bisecting for a workable N would make the subscription set depend on the
    server's behaviour instead of on the documentation, which section 8 forbids.
    """
    from depthrec.equivalence import EquivalenceTest  # local: keeps --help fast

    test = EquivalenceTest(db_path=db)
    tickers = test.build_universe()
    by_series: dict[str, int] = {}
    for entry in test.census.entries.values():
        by_series[entry.series] = by_series.get(entry.series, 0) + 1
    print(f"\nopen markets in the frozen universe: {len(tickers)}")
    for series in frozen.VALIDATION_UNIVERSE_SERIES:
        print(f"  {series:14s} {by_series.get(series, 0)}")
    if not tickers:
        print("nothing open; cannot probe", file=sys.stderr)
        return 3

    a = ResearchSession(tickers=tickers, use_yes_price=True, label="A")
    b = ResearchSession(tickers=tickers, use_yes_price=False, label="B")
    tasks = [asyncio.create_task(a.run()), asyncio.create_task(b.run())]
    rejected: SubscriptionRejected | None = None
    try:
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            for task in tasks:
                if task.done():
                    exc = task.exception()
                    if isinstance(exc, SubscriptionRejected):
                        rejected = exc
                        raise exc
                    if exc is not None:
                        raise exc
            await asyncio.sleep(0.5)
    except SubscriptionRejected:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        valid_a = sum(1 for t in tickers if a.read_book(t).is_valid)
        valid_b = sum(1 for t in tickers if b.read_book(t).is_valid)
        books_a = len(a._books)
        books_b = len(b._books)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        test.store.close()

    if rejected is not None:
        print("\nSERVER REJECTED THE FROZEN UNIVERSE -- stopping.", file=sys.stderr)
        print(f"  markets attempted  : {len(tickers)}", file=sys.stderr)
        print(f"  error code         : {rejected.code}", file=sys.stderr)
        print(f"  market/buffer limit: {rejected.is_market_limit}", file=sys.stderr)
        print(f"  raw payload        : {json.dumps(rejected.payload)}", file=sys.stderr)
        print("  Report this payload before choosing any N or amending the "
              "frozen note.", file=sys.stderr)
        return 3

    print(f"\nserver ACCEPTED all {len(tickers)} markets on one subscription per stream.")
    print(f"  stream A: {books_a} books seeded, {valid_a} VALID after {seconds:.0f}s")
    print(f"  stream B: {books_b} books seeded, {valid_b} VALID after {seconds:.0f}s")
    print("  section 8's limit branch does not activate; the frozen note needs "
          "no change.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--check-only", action="store_true",
                    help="verify preconditions and exit without running the test")
    ap.add_argument("--probe-universe", type=float, metavar="SECONDS", default=None,
                    help="attempt the entire frozen universe for SECONDS and report "
                         "whether the server accepts it; never retries with fewer")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    configure_logging(args.verbose)

    try:
        frozen.assert_frozen_config(
            rule_threshold=frozen.RULE_THRESHOLD,
            slice_minutes=frozen.RULE_SLICE_MINUTES,
            x_contracts=frozen.X_CONTRACTS,
        )
        report = integrity.verify_or_raise(
            expected_study=ph.EXPECTED_STUDY,
            expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"PRECONDITION FAILED: {exc}", file=sys.stderr)
        return 3

    print("frozen configuration:", json.dumps(frozen.frozen_config_summary()))
    print("pinned classifier   :", json.dumps(report.log_fields()))
    print("api host            :", settings.base_url)
    print("ws host             :", settings.ws_url)
    print("universe series     :", ", ".join(frozen.VALIDATION_UNIVERSE_SERIES))
    print(
        "protocol            : "
        f">= {frozen.EQUIV_MIN_MINUTES} min, stop at >= {frozen.EQUIV_MIN_COMPARISONS} "
        f"comparisons, cap {frozen.EQUIV_MAX_MINUTES} min, {frozen.EQUIV_GRID_HZ} Hz grid, "
        f"<= {frozen.EQUIV_SKEW_TOLERANCE_MS} ms skew, "
        f"pass >= {frozen.EQUIV_PASS_AGREEMENT}"
    )

    if args.check_only:
        try:
            ok = asyncio.run(_check_two_connections())
        except Exception as exc:  # noqa: BLE001
            print(f"two-connection check FAILED: {exc}", file=sys.stderr)
            return 3
        print(f"two concurrent connections permitted: {ok}")
        return 0 if ok else 3

    if args.probe_universe is not None:
        return asyncio.run(_probe_universe(args.db, args.probe_universe))

    test = EquivalenceTest(db_path=args.db)
    try:
        result = asyncio.run(test.run())
    except SubscriptionRejected as exc:
        # Section 8's limit branch. The server refused the frozen universe:
        # report the exact error and STOP. No N is chosen here, and no smaller
        # universe is tried -- searching for a workable N would make the
        # selection depend on the server's behaviour rather than on the docs.
        print("SUBSCRIPTION REJECTED BY SERVER -- stopping.", file=sys.stderr)
        print(f"  markets attempted : {len(test.tickers)}", file=sys.stderr)
        print(f"  error code        : {exc.code}", file=sys.stderr)
        print(f"  market/buffer limit: {exc.is_market_limit}", file=sys.stderr)
        print(f"  raw payload       : {json.dumps(exc.payload)}", file=sys.stderr)
        print(
            "  Report this payload before choosing any N or amending the frozen note.",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"COULD NOT RUN: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(result.summary())
    for example in result.unexplained_examples:
        print("  unexplained:", json.dumps(example, default=str))

    # Reported SEPARATELY, and after the verdict, so it can never be mistaken
    # for part of it (section 13 amendment, 2026-08-22).
    print()
    print(result.diagnostics.summary())
    print(json.dumps(result.diagnostics.as_dict(), indent=2))
    print(f"database: {args.db}")

    return {
        frozen.EQUIV_RESULT_PASS: 0,
        frozen.EQUIV_RESULT_FAIL: 1,
        frozen.EQUIV_RESULT_INSUFFICIENT: 2,
    }[result.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
