#!/usr/bin/env python
"""
Run the Thread C MLB ticker/trade banking recorder.

Production (this is the systemd unit's ExecStart):

    python scripts/threadc_record.py --config config/threadc.json

One-hour high-activity capacity measurement, by hand, in its own archive:

    python scripts/threadc_record.py --config config/threadc.capacity.json \
        --capacity --report

WHICH CHANNELS a run subscribes to is read from its config's `channels` key,
which may select a non-empty subset of threadc.frozen.CHANNELS and nothing else
-- it can subtract, never add -- and which means the frozen pair when absent.

Both branches of the precommitted fallback hierarchy have been measured on the
bracketed hour: Branch 1 (ticker+trade) wrote 373,444,608 bytes/hour and Branch
2 (ticker only) wrote 267,526,144 bytes/hour. Branch 1 does not fit the storage
available for the collection window, so config/threadc.json selects Branch 2
EXPLICITLY. Leaving the key out would not be neutral: absence means the frozen
pair, which is the branch the measurement rejected.

The measured hour is bracketed, not counted from process start. The run does its
setup, censuses the universe, establishes every subscription and banks the
explicit initial state FIRST; only then does it read filesystem usage, mark
measurement_start, collect for exactly 3,600 seconds, mark measurement_end and
read filesystem usage again. B_1h,disk is the difference between those two
readings -- see threadc.frozen.CAPACITY_MEASUREMENT_SECONDS.

The two modes cannot be confused. A capacity run writes mode=capacity into every
manifest, uses a different archive directory, closes with capacity_run_end, and
exits 0 -- while production closes with frozen_collection_end and exits 75.

Exit statuses:

    0   clean stop (operator signal, or a completed capacity run)
    1   unexpected failure -- systemd restarts
    3   startup failed closed (bad config, bad universe artifact hash)
   75   INTENTIONAL TERMINAL STOP -- systemd must NOT restart. Two causes:
        the low-disk halt, and the frozen collection end.

READ-ONLY against Kalshi. This process has no order path: it never constructs a
KalshiClient, and its HTTP layer refuses any method but GET and any path outside
/series, /series/{ticker}, /events and /markets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kbot.log_config import configure_logging  # noqa: E402
from threadc import config as tc_config  # noqa: E402
from threadc import frozen  # noqa: E402
from threadc.recorder import TerminalStop, ThreadCRecorder  # noqa: E402


def _overrides(args: argparse.Namespace) -> dict:
    over = {}
    if args.archive_dir:
        over["archive_dir"] = str(args.archive_dir)
    if args.disk_reserve_bytes is not None:
        over["disk_reserve_bytes"] = args.disk_reserve_bytes
    if args.universe_census_interval_seconds is not None:
        over["universe_census_interval_seconds"] = args.universe_census_interval_seconds
    if args.shards is not None:
        over["ws_shards"] = args.shards
    return over


async def _run(recorder: ThreadCRecorder, capacity_window: float | None) -> None:
    loop = asyncio.get_running_loop()
    for sig in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, sig, None)
        if handler is None:
            continue
        try:
            loop.add_signal_handler(handler, recorder.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows has no add_signal_handler for these; KeyboardInterrupt
            # still unwinds through asyncio.run and teardown still runs.
            pass
    await recorder.run(capacity_window_seconds=capacity_window)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", type=Path, default=None,
                    help=f"configuration file (default ${tc_config.CONFIG_ENV_VAR} or "
                         f"{tc_config.DEFAULT_CONFIG_PATH})")
    ap.add_argument("--archive-dir", type=Path, default=None,
                    help="override archive_dir")
    ap.add_argument("--capacity", action="store_true",
                    help="run the bracketed capacity measurement and stop cleanly. "
                         "Implies capacity mode; NOT for production.")
    ap.add_argument("--capacity-window-seconds", type=float,
                    default=frozen.CAPACITY_MEASUREMENT_SECONDS,
                    help=f"length of the MEASURED window in seconds (default "
                         f"{frozen.CAPACITY_MEASUREMENT_SECONDS:.0f}). Exposed for "
                         f"rehearsals and tests only -- a reported B_1h must come "
                         f"from the default.")
    ap.add_argument("--disk-reserve-bytes", type=int, default=None,
                    help="override the configured reserve R (bytes)")
    ap.add_argument("--universe-census-interval-seconds", type=float, default=None,
                    help="override the full-catalog discovery census interval")
    ap.add_argument("--shards", type=int, default=None, help="override ws_shards")
    ap.add_argument("--report", action="store_true",
                    help="print the capacity report at exit (capacity runs only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    configure_logging(args.verbose)
    mode = frozen.MODE_CAPACITY if args.capacity else frozen.MODE_PRODUCTION

    try:
        cfg = tc_config.load(args.config, mode=mode, overrides=_overrides(args))
        if mode == frozen.MODE_PRODUCTION:
            change = tc_config.enforce_monotonic_collection_end(cfg)
            if change.get("change") != "unchanged":
                print(f"collection-end configuration change: {json.dumps(change)}",
                      file=sys.stderr)
        recorder = ThreadCRecorder(cfg)
    except TerminalStop as stop:
        print(f"TERMINAL STOP BEFORE START: {stop}", file=sys.stderr)
        return stop.exit_code
    except Exception as exc:  # noqa: BLE001
        print(f"STARTUP FAILED CLOSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return frozen.EXIT_STARTUP_FAILED_CLOSED

    window = args.capacity_window_seconds if args.capacity else None
    try:
        asyncio.run(_run(recorder, window))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    except TerminalStop as stop:
        # Raised before anything was opened (startup disk check).
        print(f"TERMINAL STOP: {stop}", file=sys.stderr)
        return stop.exit_code
    except Exception as exc:  # noqa: BLE001
        print(f"RECORDER FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return frozen.EXIT_FAILURE

    status = recorder.status
    if args.report and mode == frozen.MODE_CAPACITY:
        print(json.dumps(recorder.capacity_report(), indent=2))
    print(f"run_id     : {status.run_id}")
    print(f"mode       : {status.mode}")
    print(f"archive    : {cfg.archive_dir}")
    print(f"channels   : {', '.join(cfg.channels)}"
          f"{'' if cfg.channels_are_frozen_pair else '  (subset of the frozen pair)'}")
    print(f"segments   : {', '.join(status.segments_finalized) or 'none'}")
    print(f"rotations  : {status.rotations}")
    print(f"frames     : {status.frames_written:,}")
    print(f"censuses   : {status.discovery_censuses} "
          f"({status.discovery_incomplete} incomplete)")
    print(f"initial st.: {status.initial_states_banked:,} banked, "
          f"{status.initial_state_failures:,} without a REST body")
    if status.universe_additions:
        print(f"admitted   : {', '.join(status.universe_additions)}")
    if status.stop_reason:
        print(f"stop reason: {status.stop_reason}")
    return status.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
