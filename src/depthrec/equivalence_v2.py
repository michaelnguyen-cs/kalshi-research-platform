"""
Section 8-v2: the event-paired decoder equivalence test.

Transcribed from research/THREAD_A_EQUIVALENCE_V2_2026-08-23.md, frozen at
commit 5a0b4ca. Every parameter comes from depthrec.frozen, which transcribes
that document. None is a function argument, a constructor keyword or a command
line flag: a bound you can pass in is a bound you can change after seeing a
result.

WHAT CHANGED FROM v1, AND WHY IT IS NOT A LOOSENING

v1 compared two independent WebSocket connections on a wall-clock grid with a
500 ms skew tolerance, and therefore measured transport timing rather than
decoder equivalence. v2 pairs the SAME identifiable exchange event across the
two streams and requires the two reconstructions to be identical. Arrival time
leaves the correctness question entirely. The 500 ms figure is not widened; it
becomes irrelevant, and the bar becomes exact -- no tolerance, no percentage.

SHAPE OF THE TEST

Section 5's cutoff is defined against the global maximum exchange timestamp
across the whole archive, and section 8 criterion 5 counts events across the
whole run. Neither is knowable before the recording ends, so v2 is a recording
phase followed by an evaluation phase rather than a streaming judgement:

    record exactly V2_RUN_MINUTES of both streams at recorder fidelity
      -> evaluate_archive() over the resulting ws_raw

The evaluator is a pure function of an archive. It can be re-run over preserved
evidence by anyone, which is the property section 10 leans on.

DECODER UNDER TEST

Both streams are replayed through depthrec.book.CanonicalBook, the production
decoder, with `use_yes_price` true for A and false for B -- that single
documented difference is the thing under test. Using a second, independent
decoder here would test something else; the independent reconstruction at
bda2bb0 is the separate check against a shared mistake, per section 10.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from depthrec import frozen, integrity
from depthrec import pinned_hashes as ph
from depthrec.book import PRICE_SCALE, BookError, CanonicalBook, to_price, to_qty
from depthrec.census import RestCensus
from depthrec.rawwriter import RawWriter
from depthrec.session import ResearchSession
from depthrec.store import RunContext, Store, read_raw_text
from kbot.config import Settings, settings as _default_settings
from kbot.kalshi_client import KalshiClient

log = structlog.get_logger(__name__)

EXCLUDED_PRE_BASELINE = "EXCLUDED_PRE_BASELINE"
EXCLUDED_GUARD_BAND = "EXCLUDED_GUARD_BAND"
EXCLUDED_EPOCH_TAIL = "EXCLUDED_EPOCH_TAIL"


# -- Failures -------------------------------------------------------------------


@dataclass
class Failure:
    """
    One reason the run cannot PASS.

    Every failure class in the document is fatal -- section 8 says "any single
    divergence is a FAIL" -- so these are collected for the report rather than
    weighed. Nothing here is recoverable by re-aligning, skipping or widening.
    """

    kind: str
    market: str
    epoch: int
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.kind} [{self.market} epoch {self.epoch}]: {self.detail}"


# -- Events ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verification:
    """
    Section 3's verification tuple.

    Used ONLY to verify an alignment, never to establish one. The key is not
    unique -- the calibration probe measured up to 80 events sharing a single
    ts_ms -- so a lookup by this tuple can match the wrong occurrence.

    `slots=True` is storage, not semantics: the fields, their order, their
    values, equality and hashing are unchanged. It costs the per-instance
    `__dict__`, which section 4.1's failure evidence used to read directly;
    `key_evidence()` below returns exactly the mapping that `__dict__` did.
    """

    ts_ms: int | None
    side: str
    price: int          # canonical YES, tenths of a cent
    delta: int          # hundredths of a contract


@dataclass(slots=True)
class DeltaEvent:
    raw_id: int
    recv_ts: float
    sid: Any
    seq: int
    ticker: str
    key: Verification
    body: dict[str, Any]


@dataclass(slots=True)
class SnapshotEvent:
    raw_id: int
    recv_ts: float
    sid: Any
    seq: int
    ticker: str
    body: dict[str, Any]


def key_evidence(key: Verification) -> dict[str, Any]:
    """
    A verification tuple as the failure evidence carries it.

    Exactly what `Verification.__dict__` produced before the class grew slots:
    the four fields, in declaration order, with their values untouched. Split
    out so the two failure sites in `_evaluate_epoch` record what they recorded.
    """
    return {"ts_ms": key.ts_ms, "side": key.side,
            "price": key.price, "delta": key.delta}


def canonical_price(raw: Any, side: str, use_yes_price: bool) -> int:
    """
    Wire price -> canonical YES integer price.

    The same one-line transform depthrec.book applies, lifted out because the
    section 3 verification tuple is stated in canonical coordinates and has to
    be built before the book is touched.
    """
    price = to_price(raw)
    if side == "no" and not use_yes_price:
        price = PRICE_SCALE - price
    return price


# -- Book identity ---------------------------------------------------------------


def books_identical(a: CanonicalBook, b: CanonicalBook) -> bool:
    """
    Section 7: the FULL canonical book, every price level and size on both
    ladders, not merely top of book.

    Top-of-book equality would have hidden where the calibration probe's three
    anomalous markets actually differed, which is why the document requires the
    whole ladder.
    """
    return a.bids == b.bids and a.asks == b.asks


def ladder_dump(book: CanonicalBook) -> dict[str, Any]:
    return {
        "bids": [[p, q] for p, q in sorted(book.bids.items(), reverse=True)],
        "asks": [[p, q] for p, q in sorted(book.asks.items())],
    }


def _level_hash(tag: int, price: int, qty: int) -> int:
    return hash((tag, price, qty))


class HashedBook:
    """
    A CanonicalBook plus an incremental multiset hash of its ladders.

    Section 4.1 asks for the earliest (i, j) whose books are identical,
    minimising i + j. Comparing full ladders at every (i, j) is quadratic in the
    1,000-position head bound; hashing each prefix once makes candidate
    generation linear. The hash only PROPOSES a candidate -- the chosen baseline
    is then confirmed by an exact full-ladder comparison, so a hash collision
    cannot admit a baseline the document would not.
    """

    __slots__ = ("book", "use_yes_price", "h")

    def __init__(self, ticker: str, use_yes_price: bool) -> None:
        self.book = CanonicalBook(ticker=ticker)
        self.use_yes_price = use_yes_price
        self.h = 0

    def _rehash(self) -> None:
        h = 0
        for p, q in self.book.bids.items():
            h += _level_hash(0, p, q)
        for p, q in self.book.asks.items():
            h += _level_hash(1, p, q)
        self.h = h & 0xFFFFFFFFFFFFFFFF

    def apply_snapshot(self, ev: SnapshotEvent) -> None:
        self.book.apply_snapshot(
            ev.body, seq=ev.seq, sid=ev.sid, recv_ts=ev.recv_ts,
            use_yes_price=self.use_yes_price,
        )
        self._rehash()

    def apply_delta(self, ev: DeltaEvent) -> None:
        side = ev.body.get("side")
        price = ev.key.price
        ladder = self.book.bids if side == "yes" else self.book.asks
        tag = 0 if side == "yes" else 1
        before = ladder.get(price, 0)
        self.book.apply_delta(
            ev.body, seq=ev.seq, recv_ts=ev.recv_ts,
            use_yes_price=self.use_yes_price,
        )
        after = ladder.get(price, 0)
        if before:
            self.h -= _level_hash(tag, price, before)
        if after:
            self.h += _level_hash(tag, price, after)
        self.h &= 0xFFFFFFFFFFFFFFFF


# -- Section 12: boundary-evidence sufficiency ------------------------------------


def boundary_allowance(d: int) -> float:
    """
    Section 12's per-market allowance: `max(20, 0.01 * D)`.

    The same rule as `E / max(D, 2000) > 1%`, written the way the amendment
    states it second. Both forms are implemented because the amendment gives
    both and calls them equivalent; a test asserts they agree everywhere.
    """
    floor = frozen.V2_BOUNDARY_MIN_DENOMINATOR * frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION
    return max(floor, frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION * d)


def market_breaches_boundary_cap(paired: int, excluded: int) -> bool:
    """
    Section 12 clause 1. D = 2p + E, D_effective = max(D, 2000), breach when
    E / D_effective > 1%.

    A denominator floor, NOT an exemption: a market that is entirely excluded
    still breaches (p=0, E=199 -> 199/2000 = 9.95%), and a large market is still
    held to the proportional 1% (p=5,000, E=150 -> 150 > 101.5).
    """
    d = 2 * paired + excluded
    d_effective = max(d, frozen.V2_BOUNDARY_MIN_DENOMINATOR)
    return excluded / d_effective > frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION


@dataclass
class BoundaryBreach:
    """One market over section 12's resolution-adjusted per-market cap."""

    market: str
    paired: int
    excluded: int
    d: int
    d_effective: int
    fraction: float
    allowance: float
    classes: dict[str, int] = field(default_factory=dict)
    epochs: int = 0
    interrupted_epochs: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market, "paired_events": self.paired,
            "excluded_records": self.excluded, "D": self.d,
            "D_effective": self.d_effective,
            "fraction": round(self.fraction, 6),
            "allowance": round(self.allowance, 3),
            "boundary_classes": self.classes,
            "epochs": self.epochs,
            "interrupted_epochs": self.interrupted_epochs,
        }


# -- Per-epoch evaluation ---------------------------------------------------------


@dataclass
class EpochOutcome:
    market: str
    epoch: int
    paired: int = 0
    excluded_pre_baseline: int = 0
    excluded_guard_band: int = 0
    excluded_epoch_tail: int = 0
    baseline_i: int | None = None
    baseline_j: int | None = None
    interrupted: bool = False
    empty: bool = False

    @property
    def qualifies(self) -> bool:
        return self.paired >= frozen.V2_EPOCH_MIN_PAIRED_EVENTS


def _find_baseline(
    a: list[DeltaEvent], b: list[DeltaEvent], a0: CanonicalBook, b0: CanonicalBook,
    decode_errors: list[tuple[str, int, str]],
) -> tuple[int, int] | None:
    """
    Section 4.1's baseline algorithm, forward only.

    The earliest pair of positions (i, j) at which the verification tuples are
    equal AND the two full canonical books are identical after applying them,
    minimising i + j, ties by lowest i then lowest j, searching no further than
    V2_HEAD_SEARCH_POSITIONS on either stream.

    Returns None if no such pair exists inside the bound. The caller FAILS on
    that; it does not widen the search and does not exclude the market.
    """
    cap = frozen.V2_HEAD_SEARCH_POSITIONS
    ha = HashedBook(a0.ticker, use_yes_price=True)
    ha.book = a0
    ha._rehash()
    hb = HashedBook(b0.ticker, use_yes_price=False)
    hb.book = b0
    hb._rehash()

    # prefix states: (hash, key) after applying position i. A stream whose own
    # snapshot-plus-deltas will not replay is not searched further; the caller
    # fails closed if that leaves no baseline.
    a_states: list[tuple[int, Verification]] = []
    for ev in a[:cap]:
        try:
            ha.apply_delta(ev)
        except BookError as exc:
            decode_errors.append(("A", ev.raw_id, str(exc)))
            break
        a_states.append((ha.h, ev.key))
    b_states: list[tuple[int, Verification]] = []
    for ev in b[:cap]:
        try:
            hb.apply_delta(ev)
        except BookError as exc:
            decode_errors.append(("B", ev.raw_id, str(exc)))
            break
        b_states.append((hb.h, ev.key))

    index: dict[tuple[int, Verification], list[int]] = {}
    for i, st in enumerate(a_states):
        index.setdefault(st, []).append(i)

    best: tuple[int, int, int] | None = None      # (i+j, i, j)
    for j, st in enumerate(b_states):
        for i in index.get(st, ()):
            cand = (i + j, i, j)
            if best is None or cand < best:
                best = cand
    if best is None:
        return None
    return best[1], best[2]


def _evaluate_epoch(
    market: str,
    epoch: int,
    a_snap: SnapshotEvent | None,
    b_snap: SnapshotEvent | None,
    a_deltas: list[DeltaEvent],
    b_deltas: list[DeltaEvent],
    *,
    interrupted: bool,
    failures: list[Failure],
) -> EpochOutcome:
    """
    One market, one epoch, sections 4 through 7.

    Guard-band exclusion has already been applied to a_deltas/b_deltas by the
    caller: section 5's cutoff is global and in exchange time, so it is not this
    function's business and cannot cause a misalignment here.
    """
    out = EpochOutcome(market=market, epoch=epoch, interrupted=interrupted)

    if a_snap is None or b_snap is None:
        # No book on one side: nothing is comparable and nothing is asserted.
        out.empty = True
        return out
    if not a_deltas or not b_deltas:
        # An epoch in which one stream carried no delta has no baseline to find
        # and no pair to compare. It is reported and contributes nothing. See
        # DOCUMENT GAPS in the module docstring of the CLI.
        out.empty = True
        out.excluded_pre_baseline = len(a_deltas) + len(b_deltas)
        return out

    a_book = CanonicalBook(ticker=market)
    b_book = CanonicalBook(ticker=market)
    try:
        a_book.apply_snapshot(
            a_snap.body, seq=a_snap.seq, sid=a_snap.sid, recv_ts=a_snap.recv_ts,
            use_yes_price=True,
        )
        b_book.apply_snapshot(
            b_snap.body, seq=b_snap.seq, sid=b_snap.sid, recv_ts=b_snap.recv_ts,
            use_yes_price=False,
        )
    except BookError as exc:
        failures.append(Failure(
            "SNAPSHOT_DECODE", market, epoch, f"snapshot did not decode: {exc}"))
        out.empty = True
        return out

    decode_errors: list[tuple[str, int, str]] = []
    found = _find_baseline(a_deltas, b_deltas, a_book, b_book, decode_errors)
    if decode_errors:
        for stream, raw_id, detail in decode_errors:
            failures.append(Failure(
                "HEAD_DECODE", market, epoch,
                f"stream {stream} would not replay its own snapshot plus "
                f"deltas during the baseline search: {detail}",
                {"stream": stream, "raw_id": raw_id}))
    if found is None:
        failures.append(Failure(
            "NO_BASELINE", market, epoch,
            f"no baseline within the first {frozen.V2_HEAD_SEARCH_POSITIONS} "
            f"delta positions on either stream "
            f"(A has {len(a_deltas)}, B has {len(b_deltas)})",
            {"a_head": [e.raw_id for e in a_deltas[:10]],
             "b_head": [e.raw_id for e in b_deltas[:10]]},
        ))
        return out
    i, j = found
    out.baseline_i, out.baseline_j = i, j
    out.excluded_pre_baseline = i + j

    # Replay to the baseline and assert section 4's first assertion exactly.
    ra = CanonicalBook(ticker=market)
    rb = CanonicalBook(ticker=market)
    ra.apply_snapshot(a_snap.body, seq=a_snap.seq, sid=a_snap.sid,
                      recv_ts=a_snap.recv_ts, use_yes_price=True)
    rb.apply_snapshot(b_snap.body, seq=b_snap.seq, sid=b_snap.sid,
                      recv_ts=b_snap.recv_ts, use_yes_price=False)
    try:
        for ev in a_deltas[: i + 1]:
            ra.apply_delta(ev.body, seq=ev.seq, recv_ts=ev.recv_ts, use_yes_price=True)
        for ev in b_deltas[: j + 1]:
            rb.apply_delta(ev.body, seq=ev.seq, recv_ts=ev.recv_ts, use_yes_price=False)
    except BookError as exc:
        failures.append(Failure(
            "BASELINE_DECODE", market, epoch, f"replay to baseline failed: {exc}"))
        return out

    if not books_identical(ra, rb):
        failures.append(Failure(
            "BASELINE_DIVERGENCE", market, epoch,
            "the two reconstructed books are not identical at the baseline event",
            {"baseline_i": i, "baseline_j": j,
             "a_raw_id": a_deltas[i].raw_id, "b_raw_id": b_deltas[j].raw_id,
             "a_book": ladder_dump(ra), "b_book": ladder_dump(rb)},
        ))
        return out

    # Section 4: "that is the first assertion, not an exclusion" -- the baseline
    # event is not discarded. Its tuples were required equal to select it and
    # its books were just asserted identical, so it is the first PAIRED event.
    out.paired = 1

    # Section 4.1: from the baseline onward, pairing is strictly positional.
    a_rest = a_deltas[i + 1:]
    b_rest = b_deltas[j + 1:]
    n = min(len(a_rest), len(b_rest))

    for k in range(n):
        ea, eb = a_rest[k], b_rest[k]
        if ea.key != eb.key:
            failures.append(Failure(
                "ALIGNMENT_VERIFICATION", market, epoch,
                f"verification tuple mismatch at positional offset {k} after the "
                f"baseline; not repaired by searching, re-aligning or skipping",
                {"offset": k, "a_raw_id": ea.raw_id, "b_raw_id": eb.raw_id,
                 "a_key": key_evidence(ea.key),
                 "b_key": key_evidence(eb.key)},
            ))
            return out
        try:
            ra.apply_delta(ea.body, seq=ea.seq, recv_ts=ea.recv_ts, use_yes_price=True)
            rb.apply_delta(eb.body, seq=eb.seq, recv_ts=eb.recv_ts, use_yes_price=False)
        except BookError as exc:
            failures.append(Failure(
                "DELTA_DECODE", market, epoch,
                f"delta did not apply at positional offset {k}: {exc}",
                {"offset": k, "a_raw_id": ea.raw_id, "b_raw_id": eb.raw_id},
            ))
            return out
        if not books_identical(ra, rb):
            failures.append(Failure(
                "BOOK_DIVERGENCE", market, epoch,
                f"full canonical books differ after the paired event at "
                f"positional offset {k}",
                {"offset": k, "a_raw_id": ea.raw_id, "b_raw_id": eb.raw_id,
                 "key": key_evidence(ea.key),
                 "a_book": ladder_dump(ra), "b_book": ladder_dump(rb)},
            ))
            return out
        out.paired += 1

    leftover = abs(len(a_rest) - len(b_rest))
    if leftover:
        if not interrupted:
            # Section 5's global cutoff is supposed to clear the archive tail
            # outright, and section 6.1 explicitly does not apply to a normally
            # completed final epoch. A residual one-sided block here means the
            # inter-stream delivery lag exceeded the 2,000 ms the guard band was
            # sized for, which is a finding, not something to absorb.
            failures.append(Failure(
                "UNMATCHED_TAIL_OUTSIDE_GUARD_BAND", market, epoch,
                f"{leftover} unmatched event(s) remain on the longer stream in a "
                f"normally completed epoch, after section 5's exact cutoff; "
                f"section 6.1 does not apply to this boundary",
                {"a_post_baseline": len(a_rest), "b_post_baseline": len(b_rest)},
            ))
            return out
        if leftover > frozen.V2_EPOCH_TAIL_MAX_EVENTS:
            failures.append(Failure(
                "EPOCH_TAIL_TOO_LARGE", market, epoch,
                f"{leftover} events would be excluded as an interrupted-epoch "
                f"tail, above the bound of {frozen.V2_EPOCH_TAIL_MAX_EVENTS}",
                {"a_post_baseline": len(a_rest), "b_post_baseline": len(b_rest)},
            ))
            return out
        out.excluded_epoch_tail = leftover
    return out


# -- Archive evaluation ------------------------------------------------------------


@dataclass
class V2Result:
    verdict: str
    run_id: str
    failures: list[Failure] = field(default_factory=list)
    epochs: list[EpochOutcome] = field(default_factory=list)
    qualifying_events: int = 0
    qualifying_markets: int = 0
    paired_events: int = 0
    markets_with_any_pair: int = 0
    excluded_pre_baseline: int = 0
    excluded_guard_band: int = 0
    excluded_epoch_tail: int = 0
    minutes: float = 0.0
    archive_max_ts_ms: int = 0
    guard_cutoff_ts_ms: int = 0
    frames: int = 0
    same_market_set: bool = True
    anomalous_markets: list[tuple[str, float]] = field(default_factory=list)
    boundary_breaches: list[BoundaryBreach] = field(default_factory=list)
    run_excluded: int = 0
    run_denominator: int = 0
    run_fraction: float = 0.0
    run_cap_breached: bool = False
    unmet_floors: list[str] = field(default_factory=list)
    reconnects: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == frozen.V2_RESULT_PASS

    def summary(self) -> str:
        head = (
            f"{self.verdict}: {self.paired_events:,} paired events, "
            f"{self.qualifying_events:,} in qualifying epochs across "
            f"{self.qualifying_markets:,} markets, over {self.minutes:.1f} min; "
            f"{len(self.failures)} failure(s)"
        )
        if self.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY:
            head += (
                f"; {len(self.boundary_breaches)} market(s) over the "
                f"resolution-adjusted cap, run-level "
                f"{self.run_fraction:.4%} of {self.run_denominator:,} "
                f"stream-delta records "
                f"({'over' if self.run_cap_breached else 'within'} 1%)"
            )
            if self.unmet_floors:
                head += ("; criterion 5 also unmet: "
                         + "; ".join(self.unmet_floors))
        return head

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "run_id": self.run_id,
            "minutes": round(self.minutes, 3),
            "frames": self.frames,
            "paired_events": self.paired_events,
            "markets_with_any_pair": self.markets_with_any_pair,
            "qualifying_events": self.qualifying_events,
            "qualifying_markets": self.qualifying_markets,
            "floors": {
                "events": frozen.V2_MIN_QUALIFYING_EVENTS,
                "markets": frozen.V2_MIN_QUALIFYING_MARKETS,
                "minutes": frozen.V2_RUN_MINUTES,
            },
            "excluded": {
                EXCLUDED_PRE_BASELINE: self.excluded_pre_baseline,
                EXCLUDED_GUARD_BAND: self.excluded_guard_band,
                EXCLUDED_EPOCH_TAIL: self.excluded_epoch_tail,
            },
            "archive_max_ts_ms": self.archive_max_ts_ms,
            "guard_cutoff_ts_ms": self.guard_cutoff_ts_ms,
            "same_market_set": self.same_market_set,
            "anomalous_markets": self.anomalous_markets,
            "boundary_evidence": {
                "per_market_breaches": [b.as_dict() for b in self.boundary_breaches],
                "run_excluded_records": self.run_excluded,
                "run_denominator": self.run_denominator,
                "run_fraction": round(self.run_fraction, 6),
                "run_cap_breached": self.run_cap_breached,
                "reconnects": self.reconnects,
                "min_denominator": frozen.V2_BOUNDARY_MIN_DENOMINATOR,
            },
            "unmet_criterion_5_floors": self.unmet_floors,
            "epochs": len(self.epochs),
            "failures": [
                {"kind": f.kind, "market": f.market, "epoch": f.epoch,
                 "detail": f.detail, "evidence": f.evidence}
                for f in self.failures
            ],
        }


# -- Archive access: two streaming passes -------------------------------------------
#
# WHY THIS IS SHAPED THE WAY IT IS
#
# The frozen loader decoded the whole of ws_raw into
# `deltas[stream][market] -> [DeltaEvent]` and only then started evaluating, so
# peak memory was every event in the archive at once. On formal run 1 that
# reached roughly 5.54 GB of anonymous RSS and the 5.8 GB VM OOM-killed it.
#
# Nothing in section 8-v2 asks for that. Exactly two things are global:
#
#   * section 5's cutoff, which is the maximum exchange timestamp over the WHOLE
#     archive minus the guard band, so it is not knowable until every frame has
#     been read; and
#   * section 6's boundary instants -- a server error, a sequence gap or a
#     disconnect on either stream -- which end the current epoch for every market
#     on that subscription, so a boundary found in one market's frames segments
#     all of them.
#
# Everything else section 8-v2 does is scoped to one market: its snapshots
# establish its epochs, its deltas are paired against its own deltas, and its
# section 12 denominator is its own. So the archive is read twice:
#
#   pass 1  every frame once, in `id` order, keeping only the global scalars, the
#           subscription sets, the boundary instants and an ON-DISK
#           (market -> ws_raw row id) index. No event object survives the loop
#           body.
#   pass 2  the markets in the same sorted order the frozen evaluator used; for
#           each, ONLY that market's frames are decoded, the market is evaluated
#           by the unchanged section 4-7 code, its counts are folded into the run
#           aggregates and its events are dropped before the next market starts.
#
# The second pass re-decodes rather than re-deriving: it reads the same rows
# through the same `read_raw_text` with verification on and builds DeltaEvent and
# SnapshotEvent through the same two helpers the first pass uses. A market's
# event lists in pass 2 are therefore the lists the frozen loader would have
# built for it -- same objects, same fields, same `id` order.

_SPILL_INSERT_BATCH = 20_000        # index rows buffered before a flush
_ROW_FETCH_CHUNK = 500              # ws_raw rows fetched per second-pass query

_SCAN_COLUMNS = ("id, run_id, stream_label, direction, recv_ts, raw, codec, "
                 "zdict_id, payload, raw_sha256")
_FETCH_COLUMNS = ("id, stream_label, recv_ts, raw, codec, zdict_id, payload, "
                  "raw_sha256")


def _make_snapshot_event(row: Any, sid: Any, seq: int, ticker: str,
                         body: dict[str, Any]) -> SnapshotEvent:
    """
    The frozen loader's snapshot row -> SnapshotEvent step, lifted out so both
    passes construct it identically.
    """
    return SnapshotEvent(row["id"], row["recv_ts"], sid, seq, ticker, body)


def _make_delta_event(row: Any, lab: str, sid: Any, seq: int, ticker: str,
                      side: str, body: dict[str, Any]) -> DeltaEvent:
    """
    The frozen loader's delta row -> DeltaEvent step, lifted out so both passes
    construct it identically.

    `canonical_price` and `to_qty` run in the first pass as well as the second
    even though the first pass keeps only the timestamp out of the result: a
    malformed price or size raised out of the frozen loader, and it still must.
    """
    price = canonical_price(body.get("price_dollars"), side,
                            use_yes_price=(lab == "A"))
    key = Verification(body.get("ts_ms"), side, price, to_qty(body.get("delta_fp")))
    return DeltaEvent(row["id"], row["recv_ts"], sid, seq, ticker, key, body)


class _MarketIndex:
    """
    (market -> ws_raw row ids), held on disk.

    Bounded memory is the point, so this is not a dict of lists: at formal-run
    scale that is millions of Python ints and gives most of the leak straight
    back. It is a temporary SQLite table of (market id, row id) pairs, appended
    during the first pass and indexed once at the end. Only the market ->
    small-int mapping stays resident, and that is bounded by the market
    universe -- the same bound the frozen evaluator's `markets` list already had.

    The spill directory can be moved with DEPTHREC_V2_SPILL_DIR; the file is
    removed on close, including on the failure path.
    """

    def __init__(self, spill_dir: str | None = None) -> None:
        import os
        import sqlite3
        import tempfile

        d = (spill_dir or os.environ.get("DEPTHREC_V2_SPILL_DIR")
             or tempfile.gettempdir())
        fd, path = tempfile.mkstemp(prefix="s8v2-index-", suffix=".sqlite", dir=d)
        os.close(fd)
        self.path = Path(path)
        self.con = sqlite3.connect(self.path)
        # Durability is worthless for a file deleted before the call returns, and
        # an unbounded page cache would reintroduce the problem this class exists
        # to solve. `temp_store=FILE` keeps the index build's sort runs on disk
        # rather than in RSS.
        self.con.execute("PRAGMA journal_mode=OFF")
        self.con.execute("PRAGMA synchronous=OFF")
        self.con.execute("PRAGMA cache_size=-16384")
        self.con.execute("PRAGMA temp_store=FILE")
        self.con.execute(
            "CREATE TABLE ev (mid INTEGER NOT NULL, rid INTEGER NOT NULL)")
        self._mids: dict[str, int] = {}
        self._buf: list[tuple[int, int]] = []

    def add(self, market: str, rid: int) -> None:
        mid = self._mids.get(market)
        if mid is None:
            mid = len(self._mids)
            self._mids[market] = mid
        self._buf.append((mid, rid))
        if len(self._buf) >= _SPILL_INSERT_BATCH:
            self._flush()

    def _flush(self) -> None:
        if self._buf:
            self.con.executemany("INSERT INTO ev(mid, rid) VALUES (?,?)", self._buf)
            self._buf.clear()

    def finish(self) -> None:
        self._flush()
        self.con.commit()
        self.con.execute("CREATE INDEX ix_ev ON ev(mid, rid)")
        self.con.commit()

    def markets(self) -> list[str]:
        """
        The market set, in the frozen evaluator's order.

        That was `sorted(set(deltas[A]) | set(deltas[B]) | set(snaps[A]) |
        set(snaps[B]))` -- the union over both streams and both kinds of every
        ticker that produced an event, which is exactly what was indexed here.
        """
        return sorted(self._mids)

    def rid_chunks(self, market: str, size: int | None = None):
        # Read at call time, not bound as a default: the chunk size is a knob a
        # test varies to prove it cannot reach the result.
        size = size or _ROW_FETCH_CHUNK
        mid = self._mids.get(market)
        if mid is None:
            return
        cur = self.con.execute(
            "SELECT rid FROM ev WHERE mid=? ORDER BY rid", (mid,))
        while True:
            rows = cur.fetchmany(size)
            if not rows:
                return
            yield [r[0] for r in rows]

    def close(self) -> None:
        import os

        try:
            self.con.close()
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class _Archive:
    """Read-only access to one archive, plus its compression dictionaries."""

    def __init__(self, db_path: Path) -> None:
        import sqlite3

        self.db_path = Path(db_path)
        self.con = sqlite3.connect(
            f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA cache_size=-16384")
        self.zdicts = {r["id"]: bytes(r["zdict"])
                       for r in self.con.execute(
                           "SELECT id, zdict FROM raw_dictionary")}

    def first_run_id(self) -> str:
        row = self.con.execute(
            "SELECT run_id FROM ws_raw ORDER BY id LIMIT 1").fetchone()
        return row["run_id"] if row else ""

    def scan_rows(self, run_id: str):
        return self.con.execute(
            f"SELECT {_SCAN_COLUMNS} FROM ws_raw WHERE run_id=? ORDER BY id",
            (run_id,))

    def rows_by_id(self, chunks):
        """
        The named rows, in `id` order.

        Each chunk of ids is already ascending and the query orders by `id`, so
        the concatenation is ascending overall -- the order the first pass saw
        them in, and the order the frozen loader appended them in.
        """
        for chunk in chunks:
            placeholders = ",".join("?" * len(chunk))
            yield from self.con.execute(
                f"SELECT {_FETCH_COLUMNS} FROM ws_raw WHERE id IN "
                f"({placeholders}) ORDER BY id", chunk)

    def close(self) -> None:
        self.con.close()


@dataclass
class _Scan:
    """What the first pass keeps. Every field is a run aggregate, not per-event."""

    run_id: str
    boundary_ts: set[float] = field(default_factory=set)
    subscribed: dict[str, set[str]] = field(default_factory=dict)
    archive_max_ts_ms: int = 0
    frames: int = 0
    minutes: float = 0.0
    archive_last_recv: float = 0.0


def _scan_archive(arch: _Archive, index: _MarketIndex, run_id: str | None) -> _Scan:
    """
    Pass 1. Read every frame once and keep only what is global.

    Line for line the frozen loader's body, with two differences and no others:
    an event is written to the on-disk index instead of to a per-market list, and
    a boundary contributes only its instant. The frozen evaluator used the rest
    of the boundary tuple nowhere -- it sorted the list and then read `ts` out of
    it to build `cut_points`, which is itself de-duplicated and re-sorted -- so
    the set of instants determines `cut_points` exactly.
    """
    if run_id is None:
        run_id = arch.first_run_id()

    scan = _Scan(run_id=run_id, subscribed={"A": set(), "B": set()})
    seq_prev: dict[tuple[str, Any], int] = {}
    recv_lo = recv_hi = None

    for row in arch.scan_rows(run_id):
        lab = row["stream_label"]
        text = read_raw_text(row, arch.zdicts, verify=True)
        if row["direction"] == "out":
            try:
                cmd = json.loads(text)
            except json.JSONDecodeError:
                continue
            params = cmd.get("params") or {}
            for t in params.get("market_tickers") or ():
                scan.subscribed[lab].add(t)
            continue
        scan.frames += 1
        ts = row["recv_ts"]
        recv_lo = ts if recv_lo is None else min(recv_lo, ts)
        recv_hi = ts if recv_hi is None else max(recv_hi, ts)
        msg = json.loads(text)
        mt = msg.get("type")
        if mt == "error":
            scan.boundary_ts.add(ts)
            continue
        if mt not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or not isinstance(seq, int) or isinstance(seq, bool):
            continue
        prev = seq_prev.get((lab, sid))
        if prev is not None and seq != prev + 1 and seq > prev:
            scan.boundary_ts.add(ts)
        seq_prev[(lab, sid)] = seq
        body = msg.get("msg") or {}
        tk = body.get("market_ticker")
        if not tk:
            continue
        if mt == "orderbook_snapshot":
            index.add(tk, row["id"])
        else:
            side = body.get("side")
            if side not in ("yes", "no"):
                continue
            ev = _make_delta_event(row, lab, sid, seq, tk, side, body)
            if isinstance(ev.key.ts_ms, int):
                scan.archive_max_ts_ms = max(scan.archive_max_ts_ms, ev.key.ts_ms)
            index.add(tk, row["id"])

    index.finish()
    scan.minutes = 0.0 if recv_lo is None else (recv_hi - recv_lo) / 60.0
    scan.archive_last_recv = recv_hi or 0.0
    return scan


def _share_strings(body: dict[str, Any], cache: dict[str, str]) -> dict[str, Any]:
    """
    One delta body, with its strings drawn from a per-market pool.

    `json.loads` builds a fresh str object for every key and every string value
    of every frame. A delta body is five short strings and an int, and the
    strings are most of it: in the preserved 240 s probe a single KXBTC15M
    market carries 221,305 delta events -- 45% of the whole archive -- all of
    them repeating the same ticker, the same two sides and the same handful of
    key names.

    The dict this returns is EQUAL to the one the frozen loader held: same keys,
    same values, same insertion order. Only the objects are shared, and nothing
    downstream does anything with a body but read values out of it. The pool is
    created per market and dies with it, so unlike sys.intern it cannot grow
    with the archive.
    """
    out: dict[str, Any] = {}
    for k, v in body.items():
        out[cache.setdefault(k, k)] = (
            cache.setdefault(v, v) if type(v) is str else v)
    return out


def _market_events(arch: _Archive, index: _MarketIndex, market: str):
    """
    Pass 2, one market: decode only that market's frames.

    The rows come back in `id` order, which is the order the frozen loader
    appended them in, and the per-stream, per-kind split is the one it made.
    """
    snaps: dict[str, list[SnapshotEvent]] = {"A": [], "B": []}
    deltas: dict[str, list[DeltaEvent]] = {"A": [], "B": []}
    pool: dict[str, str] = {}
    for row in arch.rows_by_id(index.rid_chunks(market)):
        lab = row["stream_label"]
        msg = json.loads(read_raw_text(row, arch.zdicts, verify=True))
        sid, seq = msg.get("sid"), msg.get("seq")
        body = msg.get("msg") or {}
        if msg.get("type") == "orderbook_snapshot":
            # A snapshot body carries the whole ladder rather than five short
            # strings, and there are a handful per market. It is left exactly as
            # decoded.
            snaps[lab].append(_make_snapshot_event(
                row, sid, seq, body.get("market_ticker"), body))
        else:
            body = _share_strings(body, pool)
            deltas[lab].append(_make_delta_event(
                row, lab, sid, seq, body.get("market_ticker"),
                body.get("side"), body))
    return snaps, deltas


def _evaluate_market(
    market: str,
    snaps: dict[str, list[SnapshotEvent]],
    deltas: dict[str, list[DeltaEvent]],
    *,
    cutoff: int,
    boundaries: list[float],
    failures: list[Failure],
) -> tuple[list[EpochOutcome], int, int, int] | None:
    """
    One market: section 5, then section 6's segmentation, then sections 4 to 7
    per epoch.

    Verbatim the frozen per-market body, reading its four event lists from the
    arguments instead of from four archive-wide dictionaries. Returns the
    market's epochs, its paired count, its excluded count and its guard-band
    exclusions -- or None where the frozen code did `continue`, for a market
    without a snapshot on both streams, whose guard-band exclusions the frozen
    evaluator dropped along with everything else about it.
    """
    a_snaps = snaps["A"]
    b_snaps = snaps["B"]
    a_all = deltas["A"]
    b_all = deltas["B"]

    # Section 5, applied first: global, in exchange time, symmetric.
    a_kept = [e for e in a_all
              if isinstance(e.key.ts_ms, int) and e.key.ts_ms <= cutoff]
    b_kept = [e for e in b_all
              if isinstance(e.key.ts_ms, int) and e.key.ts_ms <= cutoff]
    guard_excluded = (len(a_all) - len(a_kept)) + (len(b_all) - len(b_kept))

    # Section 6 epoch segmentation. The initial snapshot of a market on a
    # stream establishes that market's first epoch; a LATER snapshot, a
    # sequence gap or a disconnect on either stream ends it.
    # Section 6. A LATER snapshot on either stream, a sequence gap, or a
    # disconnect ends the current epoch for every market on that
    # subscription. The FIRST snapshot of a market on a stream establishes
    # that stream's book rather than ending an epoch -- section 8.0 times
    # the run from "both streams have established their initial valid
    # epochs", so the opening snapshot burst is the establishment, not a
    # run of terminations.
    cut_points: list[float] = []
    for s in a_snaps[1:] + b_snaps[1:]:
        cut_points.append(s.recv_ts)
    cut_points.extend(boundaries)
    cut_points = sorted(set(cut_points))

    if not a_snaps or not b_snaps:
        return None

    segments: list[tuple[float, float, bool]] = []
    lo = min(a_snaps[0].recv_ts, b_snaps[0].recv_ts)
    for cp in cut_points:
        if cp <= lo:
            continue
        segments.append((lo, cp, True))
        lo = cp
    segments.append((lo, float("inf"), False))

    epochs: list[EpochOutcome] = []
    market_pairs = 0
    market_excluded = guard_excluded
    for idx, (lo, hi, interrupted) in enumerate(segments):
        a_snap = _epoch_snapshot(a_snaps, lo, hi, first=idx == 0)
        b_snap = _epoch_snapshot(b_snaps, lo, hi, first=idx == 0)
        # Each stream's deltas run from ITS OWN snapshot, not from the later
        # of the two. The offset between the two snapshot instants is
        # exactly what section 4's baseline exists to resolve; truncating
        # the earlier stream at the later snapshot would instead drop
        # deltas its own book depends on and break its replay.
        a_from = a_snap.recv_ts if a_snap else lo
        b_from = b_snap.recv_ts if b_snap else lo
        a_seg = [e for e in a_kept if a_from <= e.recv_ts < hi]
        b_seg = [e for e in b_kept if b_from <= e.recv_ts < hi]
        out = _evaluate_epoch(
            market, idx, a_snap, b_snap, a_seg, b_seg,
            interrupted=interrupted, failures=failures)
        out.excluded_guard_band = guard_excluded if idx == 0 else 0
        epochs.append(out)
        market_pairs += out.paired
        market_excluded += out.excluded_pre_baseline + out.excluded_epoch_tail
    return epochs, market_pairs, market_excluded, guard_excluded


def _health_boundaries(db_path: Path, run_id: str) -> list[tuple[float, str, str, str]]:
    """Disconnects and reconnects, from the recorder's own health log."""
    import sqlite3

    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = []
    try:
        rows = con.execute(
            "SELECT ts, stream_label, kind, detail FROM health_event "
            "WHERE run_id=? ORDER BY ts", (run_id,)).fetchall()
    except Exception:  # noqa: BLE001 - an archive without the table
        rows = []
    con.close()
    for r in rows:
        if r["kind"] in ("disconnect", "connect"):
            out.append((r["ts"], r["stream_label"], r["kind"], r["detail"] or ""))
    return out


def evaluate_archive(db_path: Path, run_id: str | None = None) -> V2Result:
    """
    Section 8-v2 evaluated over a recorded archive.

    Pure with respect to the archive: it writes nothing there, and re-running it
    over preserved evidence reproduces the verdict. It does write a temporary
    market index elsewhere -- see _MarketIndex -- which is deleted before the
    call returns.
    """
    arch = _Archive(db_path)
    index = _MarketIndex()
    try:
        scan = _scan_archive(arch, index, run_id)
        run_id = scan.run_id
        subscribed = scan.subscribed

        # The opening connect of each stream establishes the initial epoch rather
        # than ending one. The teardown disconnect is the recorder stopping at
        # section 8.0's 60 minutes, not a transport interruption -- section 6.1
        # says it "does not apply to a normally completed final epoch", whose
        # boundary is section 5's exact cutoff. A disconnect at or after the last
        # archived frame is that teardown and is not an epoch boundary; one
        # before it is a real interruption and is.
        health = _health_boundaries(Path(db_path), run_id)
        boundary_ts = set(scan.boundary_ts)
        boundary_ts.update(
            b[0] for b in health
            if b[2] == "disconnect" and b[0] < scan.archive_last_recv
        )
        boundaries = sorted(boundary_ts)

        # Section 12 asks for reconnect counts on a boundary-evidence
        # insufficiency. The opening connect of each stream is not a reconnect;
        # every later one is.
        seen_connect: set[str] = set()
        reconnects: dict[str, int] = {}
        for ts, lab, kind, _detail in sorted(health):
            if kind != "connect":
                continue
            if lab in seen_connect:
                reconnects[lab] = reconnects.get(lab, 0) + 1
            else:
                seen_connect.add(lab)

        failures: list[Failure] = []
        result = V2Result(verdict=frozen.V2_RESULT_FAIL, run_id=run_id)
        result.frames = scan.frames
        result.reconnects = reconnects
        result.minutes = scan.minutes
        result.archive_max_ts_ms = scan.archive_max_ts_ms
        cutoff = scan.archive_max_ts_ms - frozen.V2_GUARD_BAND_MS
        result.guard_cutoff_ts_ms = cutoff

        # Section 8 criterion 6.
        if subscribed["A"] and subscribed["B"] and subscribed["A"] != subscribed["B"]:
            result.same_market_set = False
            failures.append(Failure(
                "MARKET_SET_MISMATCH", "-", 0,
                f"stream A subscribed {len(subscribed['A'])} markets, B "
                f"{len(subscribed['B'])}; symmetric difference "
                f"{len(subscribed['A'] ^ subscribed['B'])}"))

        markets_with_any_pair = 0
        qualifying_markets: set[str] = set()

        # One market at a time, in the frozen order. Nothing about a market
        # survives its iteration except the run aggregates below, which is the
        # whole of the memory claim.
        for market in index.markets():
            snaps, deltas = _market_events(arch, index, market)
            evaluated = _evaluate_market(
                market, snaps, deltas, cutoff=cutoff, boundaries=boundaries,
                failures=failures)
            del snaps, deltas
            if evaluated is None:
                continue
            epochs, market_pairs, market_excluded, guard_excluded = evaluated

            for out in epochs:
                result.epochs.append(out)
                result.excluded_pre_baseline += out.excluded_pre_baseline
                result.excluded_epoch_tail += out.excluded_epoch_tail
                if out.qualifies:
                    result.qualifying_events += out.paired
                    qualifying_markets.add(market)

            result.excluded_guard_band += guard_excluded
            result.paired_events += market_pairs
            if market_pairs:
                markets_with_any_pair += 1

            # Section 5's cap as amended by section 12: resolution-adjusted, and
            # a boundary-EVIDENCE breach rather than a decoder failure. It is
            # recorded here and weighed against the FAIL conditions by the
            # precedence below.
            d = 2 * market_pairs + market_excluded
            if d:
                d_effective = max(d, frozen.V2_BOUNDARY_MIN_DENOMINATOR)
                frac = market_excluded / d_effective
                if market_breaches_boundary_cap(market_pairs, market_excluded):
                    result.anomalous_markets.append((market, round(frac, 6)))
                    result.boundary_breaches.append(BoundaryBreach(
                        market=market, paired=market_pairs,
                        excluded=market_excluded, d=d, d_effective=d_effective,
                        fraction=frac, allowance=boundary_allowance(d),
                        classes={
                            EXCLUDED_PRE_BASELINE:
                                sum(e.excluded_pre_baseline for e in epochs),
                            EXCLUDED_GUARD_BAND: guard_excluded,
                            EXCLUDED_EPOCH_TAIL:
                                sum(e.excluded_epoch_tail for e in epochs),
                        },
                        epochs=len(epochs),
                        interrupted_epochs=sum(1 for e in epochs if e.interrupted),
                    ))
    finally:
        index.close()
        arch.close()

    result.markets_with_any_pair = markets_with_any_pair
    result.qualifying_markets = len(qualifying_markets)
    result.failures = failures

    # Section 12's run-level cap: the UNADJUSTED aggregate, no denominator
    # floor. Independent of the per-market test, so a run made of many
    # individually-tolerable exclusions is still audited in total.
    result.run_excluded = (result.excluded_pre_baseline
                           + result.excluded_guard_band
                           + result.excluded_epoch_tail)
    result.run_denominator = 2 * result.paired_events + result.run_excluded
    if result.run_denominator:
        result.run_fraction = result.run_excluded / result.run_denominator
        result.run_cap_breached = (
            result.run_fraction > frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION)

    if result.qualifying_events < frozen.V2_MIN_QUALIFYING_EVENTS:
        result.unmet_floors.append(
            f"qualifying events {result.qualifying_events:,} < "
            f"{frozen.V2_MIN_QUALIFYING_EVENTS:,}")
    if result.qualifying_markets < frozen.V2_MIN_QUALIFYING_MARKETS:
        result.unmet_floors.append(
            f"qualifying markets {result.qualifying_markets:,} < "
            f"{frozen.V2_MIN_QUALIFYING_MARKETS:,}")

    # Section 12's precedence, in order and without exception. A FAIL is never
    # converted: every condition section 8-v2 specifies as FAIL or FAIL pending
    # investigation outranks both insufficiencies, so a criterion-6 failure
    # alongside a cap breach reports the failure rather than hiding it behind
    # an insufficiency.
    caps_breached = bool(result.boundary_breaches) or result.run_cap_breached
    if failures:
        result.verdict = frozen.V2_RESULT_FAIL
    elif caps_breached:
        result.verdict = frozen.V2_RESULT_INSUFFICIENT_BOUNDARY
    elif result.unmet_floors:
        result.verdict = frozen.V2_RESULT_INSUFFICIENT
    else:
        result.verdict = frozen.V2_RESULT_PASS
    return result


def _epoch_snapshot(
    snapshots: list[SnapshotEvent], lo: float, hi: float, *, first: bool
) -> SnapshotEvent | None:
    """
    The snapshot that establishes a stream's book for one epoch.

    The initial epoch uses the stream's first snapshot. A later epoch begins
    only after the stream has RE-established a valid book (section 6), so it
    needs a snapshot inside its own window; without one the epoch has no book
    and contributes nothing.
    """
    if first:
        return snapshots[0] if snapshots else None
    for s in snapshots:
        if lo <= s.recv_ts < hi:
            return s
    return None


# -- Recording phase ----------------------------------------------------------------


class EquivalenceV2Recorder:
    """
    Section 8.0's recording phase: exactly V2_RUN_MINUTES of both streams.

    The duration is not a constructor argument. Section 8.0 makes it fixed
    rather than a minimum precisely so a run cannot be stopped the moment the
    criterion 5 floors are reached -- which would minimise exposure and so the
    chance of meeting a divergence -- nor extended until they arrive.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        cfg: Settings | None = None,
        run_id: str | None = None,
        compress_raw: bool = True,
    ) -> None:
        self._cfg = cfg or _default_settings
        frozen.assert_frozen_config(
            rule_threshold=frozen.RULE_THRESHOLD,
            slice_minutes=frozen.RULE_SLICE_MINUTES,
            x_contracts=frozen.X_CONTRACTS,
        )
        self.integrity_report = integrity.verify_or_raise(
            expected_study=ph.EXPECTED_STUDY,
            expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST,
        )
        self.git_commit = integrity.recorder_git_commit()
        self.run_id = run_id or f"equivv2-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.db_path = Path(db_path)
        self.store = Store(self.db_path, RunContext(self.run_id, self.git_commit))
        self.client = KalshiClient(self._cfg)
        self.census = RestCensus(
            client=self.client, store=self.store,
            series=frozen.VALIDATION_UNIVERSE_SERIES)
        self.raw_writer = RawWriter(self.store, compress=compress_raw)
        self.session_a: ResearchSession | None = None
        self.session_b: ResearchSession | None = None
        self.tickers: list[str] = []
        self.established_ts: float | None = None
        self.stopped_ts: float | None = None

    def build_universe(self) -> list[str]:
        """Section 9's carried-over universe rule, unchanged from v1."""
        self.census.run_live_pass()
        entries = sorted(self.census.entries.values(), key=lambda e: (e.series, e.ticker))
        self.tickers = [e.ticker for e in entries]
        return self.tickers

    def _sink(self, label: str):
        def sink(direction: str, recv_ts: float, raw: str) -> None:
            session = self.session_a if label == "A" else self.session_b
            self.raw_writer.submit(
                stream_label=label,
                session_epoch=session.session_epoch if session else 0,
                direction=direction, recv_ts=recv_ts, raw=raw,
            )
        return sink

    def _health(self, label: str):
        def sink(kind: str, ts: float, reason: str, detail: str) -> None:
            session = self.session_a if label == "A" else self.session_b
            self.store.record_health(
                stream_label=label,
                session_epoch=session.session_epoch if session else 0,
                ts=ts, kind=kind, reason=reason, detail=detail,
            )
        return sink

    async def record(self) -> str:
        """
        Record exactly V2_RUN_MINUTES and return the run id. Evaluation is a
        separate step over the archive, so the verdict cannot influence what was
        recorded.
        """
        self.build_universe()
        if not self.tickers:
            raise RuntimeError(
                "no open markets in the frozen validation universe -- the test "
                "cannot run, and the universe may not be changed to make it run")

        self.session_a = ResearchSession(
            tickers=self.tickers, use_yes_price=True, label="A", cfg=self._cfg,
            raw_sink=self._sink("A"), health_sink=self._health("A"))
        self.session_b = ResearchSession(
            tickers=self.tickers, use_yes_price=False, label="B", cfg=self._cfg,
            raw_sink=self._sink("B"), health_sink=self._health("B"))

        self.raw_writer.start()
        tasks = [asyncio.create_task(self.session_a.run(), name="A"),
                 asyncio.create_task(self.session_b.run(), name="B")]
        try:
            await self._await_established(tasks)
            self.established_ts = time.time()
            deadline = self.established_ts + frozen.V2_RUN_MINUTES * 60.0
            log.info("depthrec.equivv2.recording",
                     run_id=self.run_id, markets=len(self.tickers),
                     minutes=frozen.V2_RUN_MINUTES)
            while time.time() < deadline:
                self._raise_if_dead(tasks)
                await asyncio.sleep(min(1.0, deadline - time.time()))
            self.stopped_ts = time.time()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for s in (self.session_a, self.session_b):
                if s is not None:
                    await s.stop()
            self.raw_writer.stop()
            self.store.checkpoint_truncate()
            self.store.close()
            self.client.close()
        return self.run_id

    def _raise_if_dead(self, tasks) -> None:
        for task in tasks:
            if task.done():
                exc = task.exception()
                if exc is not None:
                    raise exc

    async def _await_established(self, tasks) -> None:
        """
        Section 8.0's start instant: both streams have established their initial
        valid epochs. The clock starts here, not at connect.
        """
        while True:
            self._raise_if_dead(tasks)
            a, b = self.session_a, self.session_b
            if a is not None and b is not None and a.connected and b.connected:
                ok_a = sum(1 for t in self.tickers if a.read_book(t).is_valid)
                ok_b = sum(1 for t in self.tickers if b.read_book(t).is_valid)
                if ok_a == len(self.tickers) and ok_b == len(self.tickers):
                    return
            await asyncio.sleep(0.25)
