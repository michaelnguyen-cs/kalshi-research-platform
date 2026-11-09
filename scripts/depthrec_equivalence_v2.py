#!/usr/bin/env python
"""
Run section 8-v2, the event-paired decoder equivalence test.

    # record exactly 60 minutes, then evaluate what was recorded
    python scripts/depthrec_equivalence_v2.py --record

    # evaluate an archive that already exists, changing nothing
    python scripts/depthrec_equivalence_v2.py --evaluate /mnt/depthrec/equiv_v2.sqlite

Implements research/THREAD_A_EQUIVALENCE_V2_2026-08-23.md, frozen at commit
5a0b4ca, with its section 12 amendment frozen at a46f39f. Every parameter comes
from depthrec.frozen and none is exposed as a
flag: a bound you can pass on the command line is a bound you can change after
seeing a result. There is no --minutes, no --tolerance and no --floor.

Recording and evaluation are separate steps on purpose. Section 5's cutoff is
defined against the whole archive's maximum exchange timestamp and section 8
criterion 5 counts across the whole run, so neither is knowable while recording;
splitting them also means the verdict cannot influence what was recorded.

Exit codes: 0 PASS, 1 FAIL, 2 either insufficiency, 3 could not run.
Section 12's INSUFFICIENT_BOUNDARY_EVIDENCE shares 2 with ordinary
INSUFFICIENT: both mean the decoder is not validated, section 9 may not
start, and the run repeats unchanged. The verdict string distinguishes
them; the amendment says nothing about exit codes and none was invented.

READ-ONLY against Kalshi. No order is ever placed.

--------------------------------------------------------------------------
DOCUMENT GAPS
--------------------------------------------------------------------------
Four places where the frozen document does not determine behaviour literally.
Each is resolved here in the fail-closed direction, and each is reported rather
than being quietly settled at implementation time.

1. AN EPOCH IN WHICH ONE STREAM CARRIES NO DELTA. Section 4.1 says that if no
   baseline is found within the head bound the run FAILS. Taken literally that
   fails an epoch with nothing in it -- there is no pair to compare and no
   assertion to make. Such an epoch is recorded as empty and contributes
   nothing; the section 4.1 failure is raised only when both streams have
   deltas and no baseline emerges within the bound.

2. THE OPENING SNAPSHOT BURST. Section 6 says a new snapshot on either stream
   ends the current epoch for every market on that subscription. Read without
   qualification, the 3,472 opening snapshots would each terminate every epoch
   and no comparison could ever start. Section 8.0 times the run from "both
   streams have established their initial valid epochs", so a market's FIRST
   snapshot on a stream is treated as establishing that epoch and a LATER one as
   ending it.

3. A ONE-SIDED RESIDUAL IN A NORMALLY COMPLETED FINAL EPOCH. Section 6.1
   explicitly does not cover that boundary, and section 5 defines no exclusion
   class for events that survive its cutoff. A residual there means the
   inter-stream delivery lag exceeded the 2,000 ms the guard band was sized for.
   Treated as a FAIL pending investigation (UNMATCHED_TAIL_OUTSIDE_GUARD_BAND),
   because section 3 fails any differing block outside the three named boundary
   regions and there is no class to absorb this one.

4. WHETHER THE BASELINE EVENT IS ITSELF A PAIRED EVENT. Section 4 says the
   books being identical at the baseline "is the first assertion, not an
   exclusion", so it is counted as the first paired event rather than consumed.
   Counting it the other way loses exactly one event per market and epoch; on
   the calibration probe that is the difference between 236,489 and 233,807, and
   236,489 is the figure section 8.1's own table records.
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
from depthrec.equivalence_v2 import (  # noqa: E402
    EquivalenceV2Recorder,
    evaluate_archive,
)
from depthrec.session import SubscriptionRejected  # noqa: E402
from kbot.config import settings  # noqa: E402
from kbot.log_config import configure_logging  # noqa: E402

DEFAULT_DB = Path("/mnt/depthrec/depthrec_equivalence_v2.sqlite")

EXIT = {
    frozen.V2_RESULT_PASS: 0,
    frozen.V2_RESULT_FAIL: 1,
    frozen.V2_RESULT_INSUFFICIENT: 2,
    # Section 12 adds the verdict but says nothing about exit codes. It shares
    # 2 with ordinary INSUFFICIENT because both mean the same operationally --
    # the decoder is not validated, section 9 may not start, and the run is
    # repeated unchanged. The verdict string distinguishes them in stdout and
    # in the JSON; inventing a fourth code would be a change the amendment did
    # not make.
    frozen.V2_RESULT_INSUFFICIENT_BOUNDARY: 2,
}


def _report(result) -> None:
    print()
    print(result.summary())
    print()
    print(json.dumps(result.as_dict(), indent=2, default=str))
    if result.failures:
        print()
        print(f"first {min(20, len(result.failures))} failure(s):")
        for f in result.failures[:20]:
            print(f"  {f}")
    if result.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY:
        print()
        print("BOUNDARY-EVIDENCE INSUFFICIENCY (section 12). Criteria 1-3 are "
              "clean; not enough clean comparable evidence was obtained. This "
              "does NOT validate the decoder and may not start section 9. It "
              "may be rerun unchanged in the next pre-declared window.")
        print(f"  run-level  sum(E)/sum(D) = {result.run_excluded:,} / "
              f"{result.run_denominator:,} = {result.run_fraction:.4%}  "
              f"({'BREACH' if result.run_cap_breached else 'within 1%'}, "
              f"unadjusted)")
        print(f"  reconnects : {result.reconnects or 'none'}")
        print(f"  markets over the resolution-adjusted per-market cap: "
              f"{len(result.boundary_breaches):,}")
        shown = sorted(result.boundary_breaches,
                       key=lambda b: -b.fraction)[:20]
        for b in shown:
            print(f"    {b.market:46s} p={b.paired:<7,} E={b.excluded:<6,} "
                  f"D={b.d:<8,} D_eff={b.d_effective:<8,} "
                  f"{b.fraction:.4%} > allowance {b.allowance:,.1f}")
            print(f"      classes {b.classes}  epochs {b.epochs} "
                  f"(interrupted {b.interrupted_epochs})")
        if len(result.boundary_breaches) > len(shown):
            print(f"    ... {len(result.boundary_breaches) - len(shown)} more")
        if result.unmet_floors:
            print("  criterion 5 floors ALSO unmet (reported alongside, not "
                  "instead):")
            for u in result.unmet_floors:
                print(f"    {u}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--record", action="store_true",
                    help=f"record exactly {frozen.V2_RUN_MINUTES} minutes, then evaluate")
    ap.add_argument("--evaluate", type=Path, default=None, metavar="DB",
                    help="evaluate an existing archive and exit")
    ap.add_argument("--run-id", default=None,
                    help="evaluate one run inside a multi-run archive")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    configure_logging(args.verbose)

    print("protocol            : section 8-v2, "
          "research/THREAD_A_EQUIVALENCE_V2_2026-08-23.md @ 5a0b4ca, "
          "section 12 amendment @ a46f39f")
    print(f"duration            : exactly {frozen.V2_RUN_MINUTES} continuous minutes "
          f"(section 8.0, fixed, not a minimum)")
    print(f"guard band          : ts_ms <= archive_max - "
          f"{frozen.V2_GUARD_BAND_MS} ms, exact")
    print(f"head search bound   : {frozen.V2_HEAD_SEARCH_POSITIONS} positions")
    print(f"epoch tail bound    : {frozen.V2_EPOCH_TAIL_MAX_EVENTS} events")
    print(f"epoch qualifies at  : {frozen.V2_EPOCH_MIN_PAIRED_EVENTS} paired events")
    print(f"boundary-evidence   : per market E / max(D, "
          f"{frozen.V2_BOUNDARY_MIN_DENOMINATOR:,}) <= "
          f"{frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION:.0%}, i.e. "
          f"E <= max(20, 1% of D); run level sum(E)/sum(D) <= "
          f"{frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION:.0%} unadjusted")
    print("verdict precedence  : any FAIL -> FAIL; else caps breached -> "
          "INSUFFICIENT_BOUNDARY_EVIDENCE;")
    print("                      else floors unmet -> INSUFFICIENT; else PASS")
    print(f"floors              : {frozen.V2_MIN_QUALIFYING_EVENTS:,} qualifying "
          f"events, {frozen.V2_MIN_QUALIFYING_MARKETS} qualifying markets")

    if args.evaluate is not None:
        if not args.evaluate.exists():
            print(f"no such archive: {args.evaluate}", file=sys.stderr)
            return 3
        result = evaluate_archive(args.evaluate, args.run_id)
        _report(result)
        return EXIT[result.verdict]

    if not args.record:
        print("\nnothing to do: pass --record to run the test, or --evaluate DB "
              "to score an existing archive.", file=sys.stderr)
        return 3

    try:
        frozen.assert_frozen_config(
            rule_threshold=frozen.RULE_THRESHOLD,
            slice_minutes=frozen.RULE_SLICE_MINUTES,
            x_contracts=frozen.X_CONTRACTS)
        report = integrity.verify_or_raise(
            expected_study=ph.EXPECTED_STUDY,
            expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST)
    except Exception as exc:  # noqa: BLE001
        print(f"PRECONDITION FAILED: {exc}", file=sys.stderr)
        return 3
    print("pinned classifier   :", json.dumps(report.log_fields()))
    print("ws host             :", settings.ws_url)

    try:
        recorder = EquivalenceV2Recorder(db_path=args.db)
    except Exception as exc:  # noqa: BLE001
        print(f"STARTUP FAILED CLOSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    try:
        run_id = asyncio.run(recorder.record())
    except SubscriptionRejected as exc:
        print("SUBSCRIPTION REJECTED BY SERVER -- stopping.", file=sys.stderr)
        print(f"  markets attempted  : {len(recorder.tickers)}", file=sys.stderr)
        print(f"  error code         : {exc.code}", file=sys.stderr)
        print(f"  market/buffer limit: {exc.is_market_limit}", file=sys.stderr)
        print(f"  raw payload        : {json.dumps(exc.payload)}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"RECORDING FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(f"\nrecorded run_id     : {run_id}")
    print(f"database            : {args.db}")
    result = evaluate_archive(args.db, run_id)
    _report(result)
    return EXIT[result.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
