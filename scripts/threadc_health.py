#!/usr/bin/env python
"""
The frozen daily Thread C health check: four facts, nothing else.

    python scripts/threadc_health.py --config config/threadc.json

  1. Is the Thread C recorder service active?
  2. Did yesterday's archive segments finalize successfully with COMPLETE?
  3. Is filesystem free space greater than the configured reserve R?
  4. Did yesterday's segments bank any raw frames at all?

Exits 0 when all four hold, 1 otherwise, so it can be a cron or monitoring
check as well as something a human reads.

Fact 4 is one aggregate count for the whole day, and it is what stops an empty
recorder from reading as healthy: a subscription set that stayed empty still
rotates at midnight and still finalizes COMPLETE.

Read-only: it opens no database and touches no archive file. It reads segment
manifests, asks systemd for a service state, and calls statvfs. No price,
spread, depth, market activity, return or outcome is displayed, and none is
reachable from here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threadc import config as tc_config  # noqa: E402
from threadc import health  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--archive-dir", type=Path, default=None)
    ap.add_argument("--reserve-bytes", type=int, default=None)
    ap.add_argument("--service", default=health.DEFAULT_SERVICE)
    ap.add_argument("--day", default=None,
                    help="UTC date YYYY-MM-DD to check (default: yesterday)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    archive_dir = args.archive_dir
    reserve = args.reserve_bytes
    if archive_dir is None or reserve is None:
        try:
            cfg = tc_config.load(args.config)
        except tc_config.ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 1
        archive_dir = archive_dir or cfg.archive_dir
        reserve = reserve if reserve is not None else cfg.disk_reserve_bytes

    rep = health.report(
        Path(archive_dir), int(reserve), service=args.service, day=args.day
    )
    print(json.dumps(rep.as_dict(), indent=2) if args.json else rep.render())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
