"""
Thread C's frozen values, in one place so none of them can drift.

Everything here is either quoted verbatim from
research/THREADC_COLLECTION_UNIVERSE_V1.md, or is an operational constant with
no research content. Nothing in this file depends on anything observed in a
book, and nothing here selects a market for its expected interestingness.

Values that are OPERATOR CONFIGURATION rather than frozen constants -- the
collection end timestamp, the disk reserve R, the universe-census interval --
deliberately do not appear here. They live in threadc.config and are supplied
from a file, so changing one is not a code change.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from depthrec import frozen as _depthrec_frozen

REPO_ROOT = Path(__file__).resolve().parents[2]

DOC_REFERENCE = "research/THREADC_COLLECTION_UNIVERSE_V1.md @ 565053d"

FIDELITY_DOC_REFERENCE = "research/THREADC_FIDELITY_BOUNDARY_V1.md"
"""What this bank preserves and what it cannot answer, written before a single
collected message was inspected.

Carried into the startup record and every segment manifest so a reader who finds
an archive years from now is told its limits by the archive itself, rather than
having to infer them from which channels happen to appear in the frames."""

# -- Collection Universe v1: the committed artifacts --------------------------

UNIVERSE_V1_DIR = REPO_ROOT / "research" / "threadc_universe_v1"
UNIVERSE_V1_JSON = UNIVERSE_V1_DIR / "universe_v1.json"
UNIVERSE_V1_MANIFEST_MD = REPO_ROOT / "research" / "THREADC_COLLECTION_UNIVERSE_V1.md"

UNIVERSE_V1_ID = "THREADC_COLLECTION_UNIVERSE_V1"

UNIVERSE_V1_SHA256 = "a076b6a674aea966520583f820f27aea9b617acb2e23f829928572e6d28fd93e"
"""SHA-256 of research/threadc_universe_v1/universe_v1.json as published in that
directory's SHA256SUMS. Checked at startup: a recorder that silently banked a
different universe than the one the manifest describes would make every segment
manifest's universe identifier a lie. .gitattributes pins the artifact to LF so
this hash survives a Windows checkout."""

UNIVERSE_V1_SERIES_CATALOG_SHA256 = (
    "4a4034c66c686891f362c1d613457b61126d7177724054a6163698b8515aca0e"
)
"""SHA-256 of the uncompressed GET /series body that produced v1. Carried into
segment manifests as provenance; not re-fetched by the recorder."""

EXPECTED_V1_QUALIFYING_COUNT = 138

# -- The frozen inclusion predicate -------------------------------------------

MLB_COMPETITION = "Pro Baseball"
"""Kalshi's own value for MLB in events[].product_metadata.competition. The
literal string "MLB" never appears in that field."""

PREDICATE_FIELD = "events[].product_metadata.competition"
PREDICATE_ENDPOINT = "/events?series_ticker=<ticker>&limit=200"
PREDICATE_PROSE = (
    "A series S qualifies iff at least one event E returned by "
    "GET /events?series_ticker=<S.series_ticker> satisfies "
    'E["product_metadata"]["competition"] == "Pro Baseball" '
    "under exact, case-sensitive string equality."
)


def qualifies(competitions: set) -> bool:
    """
    THE INCLUSION PREDICATE. Identical to scripts/threadc_collection_universe.py.

    `competitions` is the set of product_metadata.competition values observed
    across every event of one series. Exact, case-sensitive equality against
    Kalshi's own value for MLB -- no ticker parsing, no title, tag or category
    matching, no substring logic, no prefilter.

    Q1 (All-Star / Home Run Derby / Pro Baseball Draft / Pro Baseball Spring
    Training) is NOT widened into this predicate and remains unresolved; see
    threadc.universe.Q1_STATUS.
    """
    return MLB_COMPETITION in competitions


# -- Segment states -----------------------------------------------------------

SEGMENT_OPEN = "OPEN"
SEGMENT_COMPLETE = "COMPLETE"
SEGMENT_INCOMPLETE_CRASH = "INCOMPLETE_CRASH"
SEGMENT_HALTED_LOW_DISK = "HALTED_LOW_DISK"

SEGMENT_STATES = (
    SEGMENT_OPEN,
    SEGMENT_COMPLETE,
    SEGMENT_INCOMPLETE_CRASH,
    SEGMENT_HALTED_LOW_DISK,
)

TERMINAL_SEGMENT_STATES = (
    SEGMENT_COMPLETE,
    SEGMENT_INCOMPLETE_CRASH,
    SEGMENT_HALTED_LOW_DISK,
)
"""A segment in one of these states is finalized. It is never reopened,
rewritten, renamed or relabelled -- a later process that finds one leaves it
exactly as it is and opens a new segment instead."""

# Manifest close reasons. Operational only: none of them describes data.
CLOSE_ROTATION_UTC_MIDNIGHT = "rotation_utc_midnight"
CLOSE_COLLECTION_END = "frozen_collection_end"
CLOSE_LOW_DISK = "low_disk_reserve"
CLOSE_OPERATOR_SIGNAL = "operator_signal"
CLOSE_CAPACITY_RUN_END = "capacity_run_end"
CLOSE_ORPHAN_FOUND = "orphan_recovered_after_unexpected_death"

# -- Census outcomes ----------------------------------------------------------

CENSUS_COMPLETE = "COMPLETE"
CENSUS_INCOMPLETE = "INCOMPLETE"
"""Gate 3 of the manifest. Only a COMPLETE discovery census may alter the
append-only universe. An INCOMPLETE one is recorded, applies zero additions and
zero removals, and never interrupts live recording."""

# -- Run modes ----------------------------------------------------------------

MODE_PRODUCTION = "production"
MODE_CAPACITY = "capacity"
"""The one-hour high-activity capacity measurement writes MODE_CAPACITY into
every manifest it produces and into its own archive directory, so its segments
can never be mistaken for production collection."""

# -- Exit statuses ------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_STARTUP_FAILED_CLOSED = 3
EXIT_TERMINAL_NO_RESTART = 75
"""Intentional terminal conditions that systemd must NOT restart: the low-disk
halt and the frozen collection-end stop. 75 is EX_TEMPFAIL from sysexits.h,
paired with RestartPreventExitStatus=75 in the unit.

There is no pre-existing project convention for a non-restart exit status --
docs/CLOUD_MIGRATION.md's kbot.service is a bare Restart=always with no
RestartPreventExitStatus, and scripts/depthrec_record.py uses 0/1/3 with no
systemd unit at all. So nothing is being silently changed here; 75 is new, and
3 (startup failed closed) is kept from depthrec_record."""

# -- WebSocket ----------------------------------------------------------------

USE_YES_PRICE = True
"""One canonical representation, canonical YES. There is no A/B equivalence
stream in Thread C: the dual-stream decoder test belongs to Thread A and its
verdict is already recorded. Sent EXPLICITLY on every subscribe."""

TICKER_CHANNEL = "ticker"
TRADE_CHANNEL = "trade"
CHANNELS = (TICKER_CHANNEL, TRADE_CHANNEL)
"""The two public channels this bank preserves, verbatim and in full.

NOT `orderbook_delta`. The full live-evening capacity test measured the raw
order-book design at ~3.4+ GiB/hour with 10,270 markets on one healthy session
-- COMPLETE, integrity ok, zero errors, zero reconnects, zero error-26, ~402 MB
RSS, ~88% of one OCPU. The design works; it does not fit the available storage
for 24/7 collection through the collection end. That was an OPERATIONAL capacity
failure, decided from byte counts alone: no book contents were inspected.

`ticker` preserves the DISPLAYED STATE AT THE TOUCH -- both touch prices, both
displayed touch sizes, last price, volume, open interest, dollar volume, dollar
open interest, last trade size and ts_ms. `trade` preserves every public print.
Between them they answer "was at least X displayed at the touch at time T", and
they cannot answer anything about the ladder away from the touch. The losses are
enumerated in research/THREADC_FIDELITY_BOUNDARY_V1.md and must not be worked
around by narrowing markets, game times, time-to-close windows or horizons.

THIS TUPLE IS THE CEILING, AND ONLY A CODE CHANGE HERE RAISES IT. A run --
production or measurement -- may select a narrower SUBSET of it through the
`channels` config key (threadc.config._parse_channels); that key can only ever
subtract, never adds, and never edits this tuple. Absent, it means this pair.

THE PRODUCTION COLLECTION SELECTS `ticker` ONLY. Both branches of the
precommitted fallback hierarchy were measured over the same bracketed hour, the
same universe and the same storage schema: ticker+trade wrote 373,444,608
bytes/hour and ticker-only wrote 267,526,144 bytes/hour. Branch 1 does not fit
the storage available for the collection window, so the hierarchy moves to
Branch 2 and config/threadc.json says so explicitly rather than leaving
production on the absent-key default. Every run's actual selection is written
into its startup record, its readiness line, its ws_session rows and its OPEN
and final manifests, so an archive is never read as having banked a channel it
did not."""

CHANNELS_SEND_NO_SNAPSHOT_ON_SUBSCRIBE = True
"""Established live, not assumed from documentation.

`orderbook_delta` answers a subscribe with an `orderbook_snapshot` for every
market. `ticker` and `trade` answer with nothing: a market whose quotes have not
moved and which has not traded stays silent indefinitely. So the absence of a
message after a subscribe carries NO information about state, and the recorder
must not treat it as "unchanged". See ThreadCRecorder's initial-state banking:
GET /markets carries the same displayed touch fields the ticker channel does, so
an explicit state is banked from REST at every subscription establishment."""

# -- Operational cadences reused from the proven Thread A recorder ------------
#
# Imported rather than restated so there is one definition. See depthrec.frozen:
# 60 s live discovery, 900 s reconcile. Thread C does not invent a market-census
# cadence of its own.

MARKET_CENSUS_LIVE_INTERVAL_SECONDS = _depthrec_frozen.CENSUS_DISCOVERY_INTERVAL_SECONDS
MARKET_CENSUS_RECONCILE_INTERVAL_SECONDS = _depthrec_frozen.CENSUS_RECONCILE_INTERVAL_SECONDS

LIVE_MARKET_STATUS = _depthrec_frozen.LIVE_MARKET_STATUS
"""The status a market CARRIES in a Kalshi response body while it is live:
"active". Not "open" -- that is the query filter the live census pass sends
(_depthrec_frozen.UNIVERSE_OPEN_STATUS), and the two vocabularies are disjoint.
Imported rather than restated so there is one definition."""

# -- Full-universe Rule 1 discovery cadence -----------------------------------

DISCOVERY_DAILY_TARGET_UTC = "09:00:00"
"""Wall-clock UTC time the once-per-UTC-day full-catalog sweep targets.

Anchored to the clock, never to process start: an interval measured from startup
shifts a little on every restart and drifts permanently, so a reboot would move
the cadence for good. 09:00 UTC is the operator's configured target and is
overridable in config/threadc.json without a code change; this constant is the
fallback so a config that omits it (the capacity config, where discovery is
disabled outright) still parses.

If the recorder was down at the target, that day's sweep runs once as soon as
live recording is back. The catch-up is inherently today-only -- a discovery
census is a snapshot of what the exchange says now and cannot be taken
retroactively for a day already past."""

DISCOVERY_ATTEMPT_MIN_INTERVAL_SECONDS = 3600
"""FROZEN. Minimum seconds between full-universe sweep ATTEMPT STARTS, however
the previous attempt ended -- complete, incomplete, or killed mid-sweep.

Not configurable, and enforced against a timestamp persisted outside process
memory (threadc.schedule.DISCOVERY_STATE_FILE) that is written BEFORE a sweep
begins. With Restart=always and RestartSec=5, a process that dies four minutes
into a 25-minute, ~13,800-request sweep would otherwise return with an empty
memory and start a fresh sweep every five seconds. Persisting the intent to
sweep rather than the completion of one is what makes the guard survive the
crash it exists to guard against."""

DISCOVERY_DISABLED_IN_CAPACITY_MODE = True
"""Capacity mode performs NO Rule 1 discovery, structurally.

The one-hour capacity measurement starts from the verified current append-only
universe and exists only to measure ticker/trade storage throughput and whether that
universe can actually be subscribed. A live-evening capacity run started after
the daily target would otherwise immediately perform that day's "missed" sweep
-- 25 minutes of REST load, CPU and response bodies landing inside the very hour
whose bytes, CPU and RAM are being measured. See
threadc.recorder.CapacityModeViolation: the entry point raises rather than
relying on the loop never calling it."""

UNSUBSCRIBE_LAG_SECONDS = 120
"""Keep a market subscribed this long after its close time, so the frames around
a close are not cut off by census timing. Copied from
depthrec.recorder.UNSUBSCRIBE_LAG_SECONDS.

Note what is deliberately NOT copied: depthrec's SUBSCRIBE_LEAD_SECONDS = 1800.
That constant exists to put a market on the wire shortly before its T-7 research
moment, and a research horizon is exactly what Thread C must not choose. Thread
C subscribes to every market the census reports as open, with no lead-time
window at all -- continuous 24/7 collection of the whole universe."""

ROTATION_CHECK_SECONDS = 1.0
"""How often the rotation clock is examined. Bounds how late a 00:00:00 UTC
writer swap can be; no frame is dropped either way, since the swap is a
reference change made on the event loop between two submits."""

DISK_CHECK_INTERVAL_SECONDS = 60.0
"""How often free filesystem space is sampled. Frequent enough that the reserve
is hit long before the disk is full, cheap enough to ignore."""

# -- The corrected capacity measurement ---------------------------------------

CAPACITY_MEASUREMENT_SECONDS = 3600.0
"""The measured storage interval: exactly one hour, and nothing else in it.

The first capacity run measured from process start, which put setup, the
initial market census and the establishment of ~10,000 subscriptions inside the
interval whose bytes were being divided by an hour. Those are real costs, but
they are ONE-OFF costs, and including them makes B_1h describe a startup rather
than a steady state.

So the measured window now begins only after subscriptions are established, runs
for exactly this many seconds, and is bracketed by two filesystem-usage
readings. B_1h,disk is the difference between them -- not a payload-byte
counter, not a rate extrapolated from a shorter interval."""

CAPACITY_ESTABLISH_TIMEOUT_SECONDS = 900.0
"""How long the measurement waits for every shard's subscription to be
established before giving up. Exceeding it aborts the measurement rather than
starting the clock on a half-subscribed universe, which would silently
understate the rate."""

CAPACITY_RSS_SAMPLE_SECONDS = 10.0
"""How often resident memory is sampled inside the window, so max RSS describes
the measured hour rather than the whole process lifetime including startup."""

SESSION_CLOSE_TIMEOUT_SECONDS = 10.0
SHUTDOWN_GRACE_SECONDS = 30.0
WRITER_DRAIN_TIMEOUT_SECONDS = 120.0
"""Finalization must not hang forever, but it must be given long enough to drain
a full queue: a segment that is not drained is not COMPLETE."""


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    """SHA-256 of a file, streamed. Segment archives are far too large to slurp."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "DOC_REFERENCE", "UNIVERSE_V1_ID", "UNIVERSE_V1_JSON", "UNIVERSE_V1_SHA256",
    "MLB_COMPETITION", "qualifies", "SEGMENT_STATES", "TERMINAL_SEGMENT_STATES",
    "CENSUS_COMPLETE", "CENSUS_INCOMPLETE", "EXIT_TERMINAL_NO_RESTART",
    "USE_YES_PRICE", "CHANNELS", "TICKER_CHANNEL", "TRADE_CHANNEL",
    "sha256_file", "sha256_text",
]
