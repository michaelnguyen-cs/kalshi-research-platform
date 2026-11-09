#!/usr/bin/env python
"""
Archive a run's raw evidence to a durable location, then optionally purge the
local copy of the frames.

    # check the gates and archive, but keep local raw
    python scripts/depthrec_archive.py --db /mnt/depthrec/depthrec_equivalence.sqlite \
        --archive-dir /mnt/archive/depthrec

    # same, and drop local raw once every gate has passed
    python scripts/depthrec_archive.py --db ... --archive-dir ... --purge

This is NOT deletion. Section 8 excludes validation data from the feasibility
and confirmatory samples, but section 7 keeps raw frames as the provenance
behind a verdict. Being outside the statistical sample does not cancel that, so
frames leave the host only after a verified copy exists elsewhere.

Five gates, all required before anything is dropped:

  1. the formal verdict row exists
  2. the database has passed replay verification
  3. the archive has been copied to a separate durable location
  4. the archive's SHA-256 is recorded
  5. the copy's SHA-256 has been independently re-read from disk and matched

What stays local: verdict, diagnostics, comparison summary, archive location,
archive hash, recorder commit, run metadata. Everything needed to say what was
concluded and to go find the evidence.

Exit codes: 0 all gates passed, 1 a gate failed (nothing purged), 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import integrity  # noqa: E402
from depthrec.archive import ArchiveRefused, archive_and_purge  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--archive-dir", type=Path, required=True,
                    help="a separate durable location; must not be the database's "
                         "own directory")
    ap.add_argument("--run-id", default=None, help="default: the most recent run")
    ap.add_argument("--purge", action="store_true",
                    help="drop local raw frames AFTER every gate passes")
    ap.add_argument("--allow-same-volume", action="store_true",
                    help="permit an archive on the same volume; for a staging step "
                         "that will be replicated off-host")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2

    try:
        report = archive_and_purge(
            args.db, args.archive_dir,
            run_id=args.run_id, purge=args.purge,
            allow_same_volume=args.allow_same_volume,
            git_commit=integrity.recorder_git_commit(),
        )
    except ArchiveRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    for gate in report.gates:
        print(f"  [{'PASS' if gate.passed else 'FAIL'}] {gate.name}: {gate.detail}")
    print()
    print(json.dumps(report.as_dict(), indent=2, default=str))

    if not report.ok:
        print("\nnot all gates passed; nothing was purged.", file=sys.stderr)
        return 1
    if not report.purged:
        print("\ngates passed and the archive is verified. "
              "Local raw frames were KEPT (pass --purge to drop them).")
    else:
        print(f"\nlocal raw purged: {report.bytes_before:,} -> "
              f"{report.bytes_after:,} bytes")
        print(f"evidence lives at {report.archive_path}")
        print(f"sha256 {report.archive_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
