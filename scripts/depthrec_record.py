#!/usr/bin/env python
"""
Run the Thread A research book recorder.

    python scripts/depthrec_record.py --db /mnt/depthrec/depthrec_pilot.sqlite
    python scripts/depthrec_record.py --db /tmp/smoke.sqlite --minutes 5 --smoke

The section 9 feasibility pilot is exactly three complete UTC days and starts
only after every acceptance test, including section 8, has passed. This script
does not enforce that boundary and must not be used to sneak up on it: a
--minutes run is a smoke run, and the pilot clock is started deliberately, logged
and committed.

  --minutes   stop after N minutes. Smoke and acceptance runs only.
  --smoke     also print the section 5 report at exit. Only the four inspectable
              quantities appear there -- availability, D, Y and health. Nothing
              about settlements, EV, winners or losers is computed anywhere in
              this package.
  --no-universe
              subscribe to Thread A's two series only, skipping the other ten.
              Reduces the smoke-run footprint; the pilot runs WITH the universe
              because section 8 validates the recorder's full production
              subscription set.

Startup fails closed on: a frozen parameter that is not exactly its frozen
value, or a pinned classifier that differs from its committed expected version.

READ-ONLY against Kalshi. No order is ever placed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.recorder import DepthRecorder  # noqa: E402
from depthrec.session import SubscriptionRejected  # noqa: E402
from kbot.log_config import configure_logging  # noqa: E402

# The recorder writes to the dedicated 100 GB block volume mounted at
# /mnt/depthrec, not to the repository's data/ directory on the boot volume.
# The mount is proven to survive a reboot from fstab. Retention is unchanged.
DEFAULT_DB = Path("/mnt/depthrec/depthrec_pilot.sqlite")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--minutes", type=float, default=None,
                    help="stop after N minutes (smoke/acceptance runs only)")
    ap.add_argument("--smoke", action="store_true", help="print the section 5 report at exit")
    ap.add_argument("--no-universe", action="store_true",
                    help="subscribe to Thread A's series only")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    configure_logging(args.verbose)

    try:
        recorder = DepthRecorder(
            db_path=args.db,
            subscribe_universe=not args.no_universe,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"STARTUP FAILED CLOSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    duration = None if args.minutes is None else args.minutes * 60.0
    try:
        asyncio.run(recorder.run(duration_seconds=duration))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    except SubscriptionRejected as exc:
        print("SUBSCRIPTION REJECTED BY SERVER -- stopping.", file=sys.stderr)
        print(f"  markets attempted  : {len(recorder.session.tickers)}", file=sys.stderr)
        print(f"  error code         : {exc.code}", file=sys.stderr)
        print(f"  market/buffer limit: {exc.is_market_limit}", file=sys.stderr)
        print(f"  raw payload        : {json.dumps(exc.payload)}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"RECORDER FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.smoke:
        # The report is computed during teardown, while the store is still
        # open; recomputing it here would hit a closed database.
        report = recorder.final_report
        if report is None:
            print("no report: the run did not reach teardown", file=sys.stderr)
        else:
            print(json.dumps(report, indent=2, default=str))
    print(f"run_id  : {recorder.run_id}")
    print(f"database: {args.db}")
    print(f"pilot   : section 9 requires exactly {frozen.PILOT_DAYS} complete UTC days, "
          f"started only after section 8 passes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
