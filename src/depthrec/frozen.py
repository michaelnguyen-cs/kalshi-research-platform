"""
Every parameter this recorder is frozen on, transcribed from
research/THREAD_A_EXECUTION_MECHANICS_2026-08-22.md (frozen at commit e7c8c87).

Nothing in this module may be changed on the basis of a recorded result. The
values below are the document's, section by section; each carries the section
it comes from so a reviewer can check the transcription against the source
rather than against a comment's paraphrase.

The three parameters that no source hash can cover -- the >= 0.70 threshold,
the T-7 slice and the order size X -- are command-line arguments to the pinned
pilot scripts, not module defaults there. They are supplied from here as frozen
configuration and re-checked at startup by assert_frozen_config().
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# -- Section 1: order quantity ------------------------------------------------

X_CONTRACTS = 10
"""Section 1. Order quantity per firing market. May not be changed on the basis
of observed depth clearance -- an infeasible X is a finding, not a parameter."""

# -- Sections 2/3: the frozen rule and its selection moment -------------------

RULE_THRESHOLD = 0.70
"""KILL_CRITERIA.md section 2. Buy the favorite when its price is >= this."""

RULE_SLICE_MINUTES = 7
"""KILL_CRITERIA.md section 2. The T-7min selection slice."""

TARGET_OFFSET_SECONDS = 420
"""Section 3. Target time = close_ts - 420s. The document states this in
seconds; kept separate from RULE_SLICE_MINUTES so each transcription is
independently checkable, and cross-checked in assert_frozen_config()."""

SESSION_ACTIVITY_MAX_AGE_SECONDS = 30.0
"""Section 3. At target time the session must have shown Ping/Pong or other
authenticated activity within the preceding 30 seconds."""

KALSHI_PING_INTERVAL_SECONDS = 10.0
"""Section 3, corroborated by docs.kalshi.com/websockets/connection-keep-alive:
"Kalshi sends Ping frames (0x9) every 10 seconds with body `heartbeat`"."""

# -- Section 4: classification buckets ----------------------------------------

VALIDITY_VALID = "VALID"

UNAVAILABLE_SCHEDULED_MAINTENANCE = "UNAVAILABLE_SCHEDULED_MAINTENANCE"
UNAVAILABLE_DISCONNECT = "UNAVAILABLE_DISCONNECT"
UNAVAILABLE_SEQUENCE_GAP = "UNAVAILABLE_SEQUENCE_GAP"
UNAVAILABLE_NO_SNAPSHOT = "UNAVAILABLE_NO_SNAPSHOT"
UNAVAILABLE_SESSION_STALE = "UNAVAILABLE_SESSION_STALE"
UNAVAILABLE_OTHER = "UNAVAILABLE_OTHER"

UNAVAILABLE_REASONS = (
    UNAVAILABLE_SCHEDULED_MAINTENANCE,
    UNAVAILABLE_DISCONNECT,
    UNAVAILABLE_SEQUENCE_GAP,
    UNAVAILABLE_NO_SNAPSHOT,
    UNAVAILABLE_SESSION_STALE,
    UNAVAILABLE_OTHER,
)
"""Section 4. The itemised UNAVAILABLE reasons, exactly as enumerated.
UNAVAILABLE and NON-TRADEABLE are never merged."""

BUCKET_UNAVAILABLE = "UNAVAILABLE"
BUCKET_NON_TRADEABLE = "NON_TRADEABLE"
BUCKET_ELIGIBLE = "ELIGIBLE"

BUCKETS = (BUCKET_UNAVAILABLE, BUCKET_NON_TRADEABLE, BUCKET_ELIGIBLE)
"""Section 4. Exactly three buckets, summing to every otherwise-firing signal."""

# -- Section 4: scheduled maintenance -----------------------------------------

MAINTENANCE_TZ = ZoneInfo("America/New_York")
MAINTENANCE_WEEKDAY = 3            # Thursday (Monday == 0)
MAINTENANCE_START_HOUR = 3         # 03:00 ET
MAINTENANCE_END_HOUR = 5           # 05:00 ET


def in_scheduled_maintenance(ts: float) -> bool:
    """
    True if unix second `ts` falls in Kalshi's Thursday 03:00-05:00 ET
    maintenance window (section 4). Evaluated in America/New_York so the window
    tracks the DST shift rather than drifting an hour twice a year.

    Half-open [03:00, 05:00): a target time at exactly 05:00:00 ET is outside.
    """
    local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(MAINTENANCE_TZ)
    if local.weekday() != MAINTENANCE_WEEKDAY:
        return False
    return MAINTENANCE_START_HOUR <= local.hour < MAINTENANCE_END_HOUR


# -- Section 6: price convention ----------------------------------------------

USE_YES_PRICE = True
"""Section 6. Sent EXPLICITLY on every research subscribe. No API default is
relied upon: Kalshi documents that the default will flip to true and that the
flag will later be removed, so an unchanged codebase could otherwise silently
change semantics mid-recording."""

ORDERBOOK_CHANNEL = "orderbook_delta"
RESEARCH_CHANNELS = (ORDERBOOK_CHANNEL,)

# -- Section 8: validation universe -------------------------------------------

THREAD_A_SERIES = ("KXBTC15M", "KXETH15M")

THREAD_C_MLB_SERIES = ("KXMLBHRR", "KXMLBTB", "KXMLBHIT", "KXMLBRBI", "KXMLBHR")

THREAD_C_COMMODITY_SERIES = (
    "KXGOLDH", "KXWTIH", "KXSILVERH", "KXPLATINUMH", "KXPALLADIUMH",
)

VALIDATION_UNIVERSE_SERIES = (
    THREAD_A_SERIES + THREAD_C_MLB_SERIES + THREAD_C_COMMODITY_SERIES
)
"""Section 8. The frozen object is this series list plus the predicate
"currently open", NOT a ticker enumeration -- individual tickers rotate (MLB
games daily, hourly commodities hourly, crypto quarter-hourly)."""

assert len(VALIDATION_UNIVERSE_SERIES) == 12

UNIVERSE_OPEN_STATUS = "open"
"""The REST `status` QUERY FILTER implementing section 8's "currently open"
predicate. A value sent to Kalshi, never a value received from it -- see
LIVE_MARKET_STATUS below."""

LIVE_MARKET_STATUS = "active"
"""The `status` a live, tradeable market CARRIES in a Kalshi response body.

Kalshi's two market-status vocabularies are disjoint, and confusing them is a
silent failure rather than an error:

  * the query filter accepts "open" (and "closed", "settled", "unopened");
    `GET /markets?status=active` is rejected 400 "invalid status filter";
  * the returned body carries "active" for a live market -- and "finalized" for
    a settled one, which is not even in the SDK's status enum (see
    kbot.kalshi_client.get_market). No market body ever carries "open".

Verified live 2026-09-05 against api.elections.kalshi.com: `?status=open&limit=200`
returned 200 markets, every one of them `"status": "active"`.

Nothing in Thread A compares a status against this: depthrec.census sends
UNIVERSE_OPEN_STATUS as a filter and stores whatever status comes back without
ever testing it. This constant exists for consumers of `CensusEntry.status`,
which is a RETURNED status."""

CENSUS_SERIES = THREAD_A_SERIES
"""The independent REST denominator covers Thread A's two series. The wider
section 8 universe exists to validate the decoder, not to define the
denominator."""

# -- Section 8: equivalence-test sampling protocol ----------------------------

EQUIV_MIN_MINUTES = 60
"""Section 8. Minimum continuous run length once both streams are healthy."""

EQUIV_MAX_MINUTES = 240
"""Section 8. Absolute cap. Fewer than EQUIV_MIN_COMPARISONS at this point is
INSUFFICIENT and the test reruns unchanged."""

EQUIV_MIN_COMPARISONS = 10_000
"""Section 8. Matched market-grid comparisons required before the test may
stop (checked only after EQUIV_MIN_MINUTES)."""

EQUIV_GRID_HZ = 1.0
"""Section 8. Fixed 1 Hz UTC time grid."""

EQUIV_SKEW_TOLERANCE_MS = 500
"""Section 8. A disagreement is explainable by timing skew only if the stream
that is behind reaches the other's canonical top-of-book within 500 ms, with no
intervening contradictory top-of-book state."""

EQUIV_PASS_AGREEMENT = 0.999
"""Section 8 condition 1. Not permission to tolerate parser errors: condition 2
fails on ANY unexplained disagreement."""

EQUIV_RESULT_PASS = "PASS"
EQUIV_RESULT_FAIL = "FAIL"
EQUIV_RESULT_INSUFFICIENT = "INSUFFICIENT"

# -- Section 8-v2: event-paired equivalence test ------------------------------
#
# Transcribed from research/THREAD_A_EQUIVALENCE_V2_2026-08-23.md, frozen at
# commit 5a0b4ca. That document retires the §8 above and replaces it; the v1
# constants stay because depthrec.equivalence still implements v1 and is kept
# for the record, not because v1 may still be run for acceptance.
#
# None of these may be changed on the basis of a recorded result. They are the
# document's values, and the section each comes from is named so a reviewer can
# check the transcription against the source.

V2_GUARD_BAND_MS = 2000
"""Section 5. Comparison includes only events with
`ts_ms <= archive_max_ts_ms - 2000`. EXACTLY 2000 ms, not a minimum: "at least
2 seconds" would permit widening the band after seeing data."""

V2_HEAD_SEARCH_POSITIONS = 1000
"""Section 4.1. The baseline search is capped at the first 1,000 delta
positions on either stream. Beyond it the run FAILS pending investigation --
the market is not excluded and the search is not widened, so that a genuine
interior divergence cannot be absorbed by searching forward far enough to find
some later point where the books happen to agree."""

V2_EPOCH_TAIL_MAX_EVENTS = 1000
"""Section 6.1. More than 1,000 events on either stream excluded as an
interrupted-epoch tail FAILS pending investigation."""

V2_EPOCH_MIN_PAIRED_EVENTS = 100
"""Section 6. An epoch contributes to the evidence total only if it contains at
least this many paired events. Shorter epochs are reported but do not count."""

V2_MAX_BOUNDARY_EXCLUSION_FRACTION = 0.01
"""Section 5. Aggregate over all three EXCLUDED_* classes, in stream-delta
units, with D = 2 * included_paired_events + E. Aggregate rather than per class,
so three separate 0.9% exclusions cannot quietly total 2.7%.

The threshold itself is unchanged by the 2026-08-23 amendment; what that
amendment adds is a denominator floor for the PER-MARKET test and a separate
UNADJUSTED run-level test. See V2_BOUNDARY_MIN_DENOMINATOR."""

V2_BOUNDARY_MIN_DENOMINATOR = 2000
"""Section 12, 2026-08-23 amendment. The per-market test uses

    D_effective = max(D, 2000)      breach when E / D_effective > 1%

which is exactly "no market may have E > max(20, 0.01 * D)". It is a
DENOMINATOR FLOOR, not an on/off switch: a market that is entirely excluded
still fails, and a large market is still held to the proportional 1%.

Why 20 records. Boundary artefacts scale with the number of boundary REGIONS a
market has, not with its size -- one for the archive tail, two more per
reconnect. An allowance of 2 would fail a low-volume market for experiencing an
ordinary reconnect, recreating the contradiction section 6.1 exists to prevent,
because section 9 makes reconnects availability diagnostics and never
equivalence failures. Twenty is roughly ten ordinary paired boundary losses: not
statistically estimated, an engineering resolution allowance, and it CANNOT
convert a decoder divergence into a pass -- it only decides whether enough
comparable evidence was obtained.

The RUN-LEVEL cap applies no floor: sum(E) / sum(D) over all markets must not
exceed V2_MAX_BOUNDARY_EXCLUSION_FRACTION."""

V2_MIN_QUALIFYING_EVENTS = 600_000
"""Section 8 criterion 5, derived in section 8.1 from the frozen threshold rule:
25% of the preserved probe's 2,698,320 qualifying-epoch events extrapolated to
60 minutes = 674,580, floored to the nearest 100,000."""

V2_MIN_QUALIFYING_MARKETS = 60
"""Section 8 criterion 5, section 8.1: 25% of the probe's 259 qualifying
markets = 64.75, floored to the nearest 10."""

V2_RUN_MINUTES = 60
"""Section 8.0. EXACTLY 60 continuous wall-clock minutes, timed from the moment
both streams have established their initial valid epochs. Fixed, not a minimum:
otherwise a run could be stopped the moment the floors are reached, minimising
exposure and therefore the chance of encountering a divergence."""

V2_RESULT_PASS = "PASS"
V2_RESULT_FAIL = "FAIL"
V2_RESULT_INSUFFICIENT = "INSUFFICIENT"

V2_RESULT_INSUFFICIENT_BOUNDARY = "INSUFFICIENT_BOUNDARY_EVIDENCE"
"""Section 12, 2026-08-23 amendment. A boundary-exclusion cap was breached while
criteria 1-3 were clean: not enough clean comparable evidence was obtained,
which is a different kind of thing from the decoders disagreeing. It does not
validate the decoder and may not start section 9. It may be rerun unchanged.

Section 12's precedence, exactly:

    any FAIL condition          ->  FAIL
    else caps breached          ->  INSUFFICIENT_BOUNDARY_EVIDENCE
    else 600k / 60 not reached  ->  INSUFFICIENT
    else                        ->  PASS

A FAIL is never converted. FAIL conditions are criteria 1-3, criterion 6, the
section 4.1 head bound, the section 6.1 interrupted-tail bound, and any
differing block outside the permitted boundary regions."""

# -- Section 9: feasibility pilot ---------------------------------------------

PILOT_DAYS = 3
"""Section 9. Exactly three complete UTC days, fixed in advance."""

# -- Independent REST census cadence ------------------------------------------

CENSUS_DISCOVERY_INTERVAL_SECONDS = 60
"""Live REST discovery cadence, frozen. Discovery must not be retuned in
response to what it finds. A discovery failure is logged and never silently
removes a market from the census."""

CENSUS_RECONCILE_INTERVAL_SECONDS = 900
"""Cadence of the second, independent reconciliation pass: re-enumerate by
series_ticker plus close-time range so a market missed by live discovery is
still recovered."""

CENSUS_RECONCILE_LOOKBACK_SECONDS = 6 * 3600
CENSUS_RECONCILE_LOOKAHEAD_SECONDS = 6 * 3600
"""Close-time range the reconciliation pass sweeps around now."""

CENSUS_CANDLE_LEAD_SECONDS = 12 * 60
"""How far before close_ts the classifier's 1-minute candles are fetched. Wide
enough that candle_at_or_before() has a causal candle at T-7 even when the
market traded thinly; the pinned code decides which candle is used."""

# -- Fail-closed startup check ------------------------------------------------


class FrozenConfigError(RuntimeError):
    """Raised when a frozen parameter is not exactly its frozen value."""


def frozen_config_summary() -> dict[str, object]:
    """The three no-hash-covers-them parameters, for the startup log line."""
    return {
        "rule_threshold": RULE_THRESHOLD,
        "slice_minutes": RULE_SLICE_MINUTES,
        "x_contracts": X_CONTRACTS,
    }


def assert_frozen_config(
    *,
    rule_threshold: float,
    slice_minutes: int,
    x_contracts: int,
) -> None:
    """
    Fail closed unless the three parameters equal exactly their frozen values.

    These three are command-line arguments to the pinned scripts, not module
    defaults there, so NO source hash covers them. They are supplied explicitly
    and re-checked here. Exact equality, not a tolerance: 0.7000001 is a
    different rule.
    """
    problems = []
    if rule_threshold != RULE_THRESHOLD:
        problems.append(f"rule_threshold={rule_threshold!r} != {RULE_THRESHOLD!r}")
    if slice_minutes != RULE_SLICE_MINUTES:
        problems.append(f"slice_minutes={slice_minutes!r} != {RULE_SLICE_MINUTES!r}")
    if x_contracts != X_CONTRACTS:
        problems.append(f"x_contracts={x_contracts!r} != {X_CONTRACTS!r}")
    if RULE_SLICE_MINUTES * 60 != TARGET_OFFSET_SECONDS:
        problems.append(
            f"TARGET_OFFSET_SECONDS={TARGET_OFFSET_SECONDS} does not equal "
            f"{RULE_SLICE_MINUTES} * 60 -- the document states both; they must agree"
        )
    if problems:
        raise FrozenConfigError(
            "frozen configuration mismatch (refusing to record): " + "; ".join(problems)
        )
