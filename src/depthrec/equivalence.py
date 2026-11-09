"""
Section 8's dual-stream equivalence test, as a runnable acceptance check.

Every parameter is taken from depthrec.frozen, which transcribes the document.
None of them may be altered in response to an equivalence result, so none of
them is a function argument here.

    Subscription A   use_yes_price: true
    Subscription B   use_yes_price: false
    separate WebSocket connections, one subscription each -- each subscribe
    creates its own sid and sequence state, and mixing conventions on one
    session invites exactly the state confusion this test rules out.

B is transformed into YES coordinates by depthrec.book._canonical_price, which
applies `1 - p` to no-side prices when use_yes_price is false and nothing else.
Both streams therefore run through the SAME decoder with one documented
difference, so a disagreement is a disagreement about the wire convention rather
than about two hand-written parsers.

Sampling protocol, frozen:
  duration      >= 60 continuous minutes; then stop at the first grid tick with
                >= 10,000 matched comparisons; hard cap 240 minutes.
                < 10,000 at 240 minutes is INSUFFICIENT and the test reruns
                without changing the universe, grid, tolerance, duration rules
                or pass bar.
  universe      all currently open markets in the twelve frozen series.
  grid          1 Hz UTC. A market is compared only if BOTH streams are VALID.
  skew          a disagreement is explainable only if the stream that is behind
                reaches the other's canonical top-of-book within 500 ms with no
                intervening contradictory top-of-book state.
  PASS          >= 99.9% exact agreement AND zero unexplained disagreements.

Section 13 amendment, 2026-08-22 -- what "continuous" means:

  "Continuous minutes" is continuous wall-clock execution of THIS PROCESS,
  without manual pause, restart, or alteration of parameters. A WebSocket
  disconnect does NOT reset the 60-minute clock, so `started` below is stamped
  once and never touched again.

  While either stream is not VALID, that market-grid cell is unmatched: it
  enters neither the numerator nor the denominator of the agreement rate. After
  a disconnect or sequence invalidation a stream resumes comparing only once a
  fresh snapshot has returned it to VALID -- which read_book() enforces for
  free, since nothing but a snapshot clears an invalidation.

  Disconnects, reconnect durations and the unmatched-grid fraction are collected
  in AvailabilityDiagnostics and reported SEPARATELY. They are recorder-
  availability facts, not decoder-equivalence failures, and none of them can
  move the pass bar. A connection that drops constantly cannot manufacture a
  PASS: it simply fails to accumulate 10,000 matched comparisons inside the
  240-minute cap and returns INSUFFICIENT.

  Section 8 tests the DECODER. Connection availability is section 9's
  measurement. Demanding 60 uninterrupted socket minutes here would conflate
  the two hypotheses.

The 99.9% bar is not permission to tolerate parser errors: condition 2 fails on
any unexplained disagreement, so the bar only ever excuses benign asynchronous
observation.

Validation data is engineering/test data and is excluded from all feasibility
and confirmatory samples -- it is written to its own database file, never to a
pilot store.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from depthrec import frozen, integrity
from depthrec import pinned_hashes as ph
from depthrec.book import TopOfBook
from depthrec.census import RestCensus
from depthrec.rawwriter import RawWriter
from depthrec.session import ReadResult, ResearchSession, SubscriptionRejected
from depthrec.store import RunContext, Store
from kbot.config import Settings, settings as _default_settings
from kbot.kalshi_client import KalshiClient

log = structlog.get_logger(__name__)

RESOLUTION_AGREE = "agree"
RESOLUTION_SKEW = "explained_by_skew"
RESOLUTION_UNEXPLAINED = "unexplained"

HISTORY_SECONDS = 2.0
"""How much per-market top-of-book history each stream keeps, in MONOTONIC
seconds. Four times the frozen 500 ms skew tolerance, so the evidence window is
never the limiting factor, and short enough that the rings stay small."""

HISTORY_MAX_ENTRIES = 2048
"""Hard cap per (stream, ticker), as protection against a pathological burst.

Sized so it cannot truncate the 500 ms evidence window under supported load.
The busiest single ticker measured today -- KXBTC15M in its final quarter hour
-- ran 206 frames/s, and only frames that CHANGE the top of book become
entries, so the 500 ms window needs at most ~103 entries. The cap is ~20x that.
It is a memory guard, not a sampling policy: if it ever bites, the ring is
discarding evidence and that is worth knowing, so it is counted.

Total memory stays trivial because the rings also expire by time: across every
ticker and both streams the steady-state entry count is bounded by
HISTORY_SECONDS x the aggregate top-of-book change rate, about 3,500 entries at
the measured 1,731 frames/s.

Nothing here is persisted. At the measured 1,731 frames/s across both streams,
240 minutes of persisted event history would be roughly 25 million rows and
would recreate a storage problem this has nothing to do with."""


def _key(top: TopOfBook | None) -> tuple | None:
    if top is None:
        return None
    return (top.bid_price, top.bid_qty, top.ask_price, top.ask_qty)


@dataclass
class StreamHistory:
    """
    A bounded in-memory ring of canonical top-of-book states for one market on
    one stream.

    Appended to on every book-CHANGING orderbook_snapshot/orderbook_delta, at
    the receive instant -- not sampled on the 1 Hz comparison grid. That
    distinction is the whole fix: the frozen rule asks whether a lagging stream
    reached the other's state within 500 ms, and observations one second apart
    cannot answer a 500 ms question. Sampled at 1 Hz, every genuine sub-second
    skew was classified UNEXPLAINED, and section 8 fails on any unexplained
    disagreement -- so the test would have failed spuriously on fast books.

    The 1 Hz grid is untouched and remains the only source of matched
    comparison cells. This ring exists solely to evaluate the already-frozen
    skew rule.

    Entries are (monotonic_ts, wall_ts, canonical_state). Elapsed time is
    measured on the monotonic clock because a wall clock can step under NTP and
    the 500 ms rule is an elapsed-time test; the wall clock is retained for
    provenance and logging.
    """

    entries: deque = field(default_factory=deque)
    dropped_to_cap: int = 0

    def observe(self, mono_ts: float, wall_ts: float, state: tuple | None) -> None:
        """
        Record a state change. O(1) append, amortised O(1) expiry.

        Identical consecutive states are collapsed: a delta that leaves the top
        of book untouched is not a new observation, and keeping it would fill
        the ring with noise on a deep book whose inner levels churn.
        """
        if self.entries and self.entries[-1][2] == state:
            return
        self.entries.append((mono_ts, wall_ts, state))
        cutoff = mono_ts - HISTORY_SECONDS
        # Amortised O(1): each entry is popped at most once.
        while len(self.entries) > 1 and self.entries[0][0] < cutoff:
            self.entries.popleft()
        while len(self.entries) > HISTORY_MAX_ENTRIES:
            self.entries.popleft()
            self.dropped_to_cap += 1

    def reaches(self, target: tuple | None, *, after: float, within_ms: float) -> float | None:
        """
        Milliseconds until this stream first shows `target`, or None.

        `after` is a MONOTONIC timestamp. Returns None if any state other than
        the one it started in appears before `target` does -- that is the
        "intervening contradictory top-of-book state" the frozen rule excludes.

        FAILS CLOSED when the ring holds no entry at or before `after`. That
        happens whenever a market's top of book has been unchanged for longer
        than HISTORY_SECONDS: the entry describing the comparison instant is
        expired by the next append, and the earliest survivor is a state from
        AFTER the comparison. Before 2026-08-23 the contradiction check was
        simply skipped in that case, because `seen_start` stayed False -- so a
        run of states could be walked through and the first one matching
        `target` still returned a skew explanation. That made the rule strictly
        more permissive than the frozen document on exactly the quiet markets
        that make up most of the universe: an engineering probe found 177 of
        1,385 disagreements explained this way, none of which the frozen rule
        permits (evidence: <EVIDENCE_DIR>/equivalence_probe_2026-08-23).

        Seeding `started_in` from the earliest survivor is NOT the fix. That
        entry post-dates the comparison instant, so treating it as the state
        the stream started in invents evidence about an interval the ring no
        longer holds. With no witness to the comparison instant the only honest
        answer is "cannot explain", which is also what the pre-registered
        burden requires: section 8 fails on any unexplained disagreement, so
        the resolver must never guess in the direction of passing.
        """
        limit = after + within_ms / 1000.0
        started_in: tuple | None = None
        seen_start = False
        for mono_ts, _wall_ts, state in self.entries:
            if mono_ts <= after:
                started_in = state
                seen_start = True
                continue
            if not seen_start:
                return None
            if mono_ts > limit:
                return None
            if state == target:
                return (mono_ts - after) * 1000.0
            if state != started_in:
                return None
        return None


@dataclass
class AvailabilityDiagnostics:
    """
    Recorder-availability facts about the run, kept strictly apart from the
    verdict.

    Per the 2026-08-22 section 8 clarification: network disconnects, reconnect
    durations and unmatched-grid fractions are logged and reported separately.
    They are recorder-availability diagnostics, NOT decoder-equivalence
    failures, and nothing here can move the pass bar. A flaky connection cannot
    manufacture agreement -- an unmatched cell leaves the numerator and the
    denominator alike -- it can only run the clock down to the 240-minute cap
    and return INSUFFICIENT.
    """

    ticks: int = 0
    grid_cells: int = 0
    unmatched_cells: int = 0
    disconnects: dict[str, int] = field(default_factory=dict)
    reconnect_seconds: dict[str, list[float]] = field(default_factory=dict)
    history_entries_dropped_to_cap: int = 0

    @property
    def unmatched_fraction(self) -> float:
        return self.unmatched_cells / self.grid_cells if self.grid_cells else 0.0

    def as_dict(self) -> dict[str, Any]:
        durations = {
            label: {
                "count": len(v),
                "total_s": round(sum(v), 3),
                "max_s": round(max(v), 3) if v else 0.0,
            }
            for label, v in sorted(self.reconnect_seconds.items())
        }
        return {
            "ticks": self.ticks,
            "grid_cells": self.grid_cells,
            "unmatched_cells": self.unmatched_cells,
            "unmatched_fraction": round(self.unmatched_fraction, 6),
            "disconnects": dict(sorted(self.disconnects.items())),
            "reconnects": durations,
            "history_entries_dropped_to_cap": self.history_entries_dropped_to_cap,
        }

    def summary(self) -> str:
        return (
            f"availability diagnostics (NOT part of the verdict): "
            f"{self.ticks} grid ticks, {self.grid_cells} cells, "
            f"{self.unmatched_cells} unmatched ({self.unmatched_fraction:.4%}), "
            f"disconnects {dict(sorted(self.disconnects.items())) or '{}'}"
        )


@dataclass
class EquivalenceResult:
    verdict: str
    comparisons: int
    agreements: int
    disagreements: int
    skew_explained: int
    unexplained: int
    agreement_rate: float
    minutes: float
    started_ts: float
    ended_ts: float
    unexplained_examples: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: AvailabilityDiagnostics = field(default_factory=AvailabilityDiagnostics)

    @property
    def passed(self) -> bool:
        return self.verdict == frozen.EQUIV_RESULT_PASS

    def summary(self) -> str:
        return (
            f"{self.verdict}: {self.comparisons} matched comparisons over "
            f"{self.minutes:.1f} min, agreement {self.agreement_rate:.6f}, "
            f"{self.skew_explained} explained by <= {frozen.EQUIV_SKEW_TOLERANCE_MS} ms skew, "
            f"{self.unexplained} unexplained"
        )


class EquivalenceTest:
    """Runs the section 8 acceptance check end to end."""

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
        self.run_id = run_id or f"equiv-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.store = Store(Path(db_path), RunContext(self.run_id, self.git_commit))
        self.client = KalshiClient(self._cfg)
        self.census = RestCensus(
            client=self.client, store=self.store, series=frozen.VALIDATION_UNIVERSE_SERIES
        )
        self.raw_writer = RawWriter(self.store, compress=compress_raw)
        self.session_a: ResearchSession | None = None
        self.session_b: ResearchSession | None = None
        self.tickers: list[str] = []
        self._history: dict[tuple[str, str], StreamHistory] = {}
        self._diag = AvailabilityDiagnostics()
        self._down_since: dict[str, float] = {}
        self._stop = asyncio.Event()

    # -- Universe --------------------------------------------------------------

    def build_universe(self) -> list[str]:
        """
        All currently open markets in the twelve frozen series, sorted
        (series_ticker ASC, market_ticker ASC).

        The sort is section 8's tie-break for the case where N binds. It is
        applied unconditionally so the attempted order is deterministic and
        recorded whether or not any limit ever appears -- and so that no
        selection can depend on observed decoder agreement.
        """
        self.census.run_live_pass()
        entries = sorted(
            self.census.entries.values(), key=lambda e: (e.series, e.ticker)
        )
        self.tickers = [e.ticker for e in entries]
        log.info(
            "depthrec.equiv.universe",
            series=len(frozen.VALIDATION_UNIVERSE_SERIES),
            markets=len(self.tickers),
            by_series={
                s: sum(1 for e in entries if e.series == s)
                for s in frozen.VALIDATION_UNIVERSE_SERIES
            },
        )
        return self.tickers

    # -- Run -------------------------------------------------------------------

    async def run(self) -> EquivalenceResult:
        self.build_universe()
        if not self.tickers:
            raise RuntimeError(
                "no open markets in the frozen validation universe -- the test "
                "cannot run, and the universe may not be changed to make it run"
            )

        self.session_a = ResearchSession(
            tickers=self.tickers, use_yes_price=True, label="A", cfg=self._cfg,
            raw_sink=self._sink("A"), health_sink=self._health("A"),
            on_book_state=self._book_state("A"),
        )
        self.session_b = ResearchSession(
            tickers=self.tickers, use_yes_price=False, label="B", cfg=self._cfg,
            raw_sink=self._sink("B"), health_sink=self._health("B"),
            on_book_state=self._book_state("B"),
        )

        self.raw_writer.start()
        tasks = [
            asyncio.create_task(self.session_a.run(), name="A"),
            asyncio.create_task(self.session_b.run(), name="B"),
        ]
        try:
            await self._await_both_healthy(tasks)
            result = await self._compare_loop(tasks)
        finally:
            self._stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for session in (self.session_a, self.session_b):
                if session is not None:
                    await session.stop()
            self.raw_writer.stop()
            self.client.close()

        self.store.record_equivalence_result({
            "started_ts": result.started_ts,
            "ended_ts": result.ended_ts,
            "minutes": result.minutes,
            "comparisons": result.comparisons,
            "agreements": result.agreements,
            "disagreements": result.disagreements,
            "skew_explained": result.skew_explained,
            "unexplained": result.unexplained,
            "agreement_rate": result.agreement_rate,
            "verdict": result.verdict,
            "diagnostics": json.dumps(result.diagnostics.as_dict(), sort_keys=True),
            "params": json.dumps({
                "min_minutes": frozen.EQUIV_MIN_MINUTES,
                "max_minutes": frozen.EQUIV_MAX_MINUTES,
                "min_comparisons": frozen.EQUIV_MIN_COMPARISONS,
                "grid_hz": frozen.EQUIV_GRID_HZ,
                "skew_tolerance_ms": frozen.EQUIV_SKEW_TOLERANCE_MS,
                "pass_agreement": frozen.EQUIV_PASS_AGREEMENT,
                "universe_series": list(frozen.VALIDATION_UNIVERSE_SERIES),
                "markets_attempted": len(self.tickers),
            }, sort_keys=True),
        })
        self.store.close()
        return result

    async def _await_both_healthy(self, tasks: list[asyncio.Task]) -> None:
        """
        Section 8: the clock starts only once BOTH connections are healthy and
        BOTH decoders have received fresh snapshots.
        """
        log.info("depthrec.equiv.waiting_for_health")
        while True:
            self._raise_if_dead(tasks)
            a, b = self.session_a, self.session_b
            if a and b and a.connected and b.connected:
                ready_a = sum(1 for t in a.tickers if a.read_book(t).is_valid)
                ready_b = sum(1 for t in b.tickers if b.read_book(t).is_valid)
                if ready_a and ready_b:
                    log.info(
                        "depthrec.equiv.healthy",
                        valid_a=ready_a, valid_b=ready_b, markets=len(self.tickers),
                    )
                    return
            await asyncio.sleep(0.5)

    async def _compare_loop(self, tasks: list[asyncio.Task]) -> EquivalenceResult:
        # Stamped ONCE. Per the 2026-08-22 section 13 amendment, a WebSocket
        # disconnect does not reset the duration clock -- "continuous" is
        # continuous execution of this process, not continuous socket uptime.
        started = time.time()
        period = 1.0 / frozen.EQUIV_GRID_HZ
        # Align to the 1 Hz UTC grid rather than to an arbitrary start instant.
        next_tick = math.floor(started / period) * period + period

        comparisons = agreements = skew_explained = unexplained = 0
        examples: list[dict[str, Any]] = []

        while True:
            self._raise_if_dead(tasks)
            now = time.time()
            if now < next_tick:
                await asyncio.sleep(min(0.05, next_tick - now))
                continue
            grid_ts = next_tick
            next_tick += period

            n, agree, unmatched, pending = self._compare_at(grid_ts)
            comparisons += n
            agreements += agree
            self._diag.ticks += 1
            self._diag.grid_cells += len(self.tickers)
            self._diag.unmatched_cells += unmatched

            # A disagreement gets EQUIV_SKEW_TOLERANCE_MS of wall clock to
            # resolve before it is judged. Waiting is part of the frozen rule,
            # not leniency: the rule asks whether the lagging stream catches up.
            if pending:
                await asyncio.sleep(frozen.EQUIV_SKEW_TOLERANCE_MS / 1000.0 + 0.05)
                for item in pending:
                    resolution, skew_ms = self._resolve(item, grid_ts)
                    if resolution == RESOLUTION_SKEW:
                        skew_explained += 1
                    else:
                        unexplained += 1
                        if len(examples) < 20:
                            examples.append({**item, "grid_ts": grid_ts})
                    self.store.record_comparison({
                        "grid_ts": grid_ts, "ticker": item["ticker"], "agree": 0,
                        **_ab_columns(item["a"], item["b"]),
                        "resolution": resolution, "skew_ms": skew_ms,
                    })

            elapsed_min = (time.time() - started) / 60.0
            if elapsed_min >= frozen.EQUIV_MIN_MINUTES:
                if comparisons >= frozen.EQUIV_MIN_COMPARISONS:
                    break
                if elapsed_min >= frozen.EQUIV_MAX_MINUTES:
                    break

        ended = time.time()
        self._diag.history_entries_dropped_to_cap = sum(
            h.dropped_to_cap for h in self._history.values()
        )
        disagreements = skew_explained + unexplained
        rate = agreements / comparisons if comparisons else 0.0

        if comparisons < frozen.EQUIV_MIN_COMPARISONS:
            verdict = frozen.EQUIV_RESULT_INSUFFICIENT
        elif unexplained == 0 and rate >= frozen.EQUIV_PASS_AGREEMENT:
            verdict = frozen.EQUIV_RESULT_PASS
        else:
            verdict = frozen.EQUIV_RESULT_FAIL

        return EquivalenceResult(
            verdict=verdict, comparisons=comparisons, agreements=agreements,
            disagreements=disagreements, skew_explained=skew_explained,
            unexplained=unexplained, agreement_rate=rate,
            minutes=(ended - started) / 60.0, started_ts=started, ended_ts=ended,
            unexplained_examples=examples, diagnostics=self._diag,
        )

    def _compare_at(self, grid_ts: float) -> tuple[int, int, int, list[dict[str, Any]]]:
        """One grid tick. Returns (matched, agreed, unmatched, to resolve)."""
        a, b = self.session_a, self.session_b
        assert a is not None and b is not None
        matched = agreed = unmatched = 0
        pending: list[dict[str, Any]] = []
        agree_rows: list[dict[str, Any]] = []
        now = time.time()
        now_mono = time.monotonic()

        for ticker in self.tickers:
            ra: ReadResult = a.read_book(ticker, at_ts=now, include_levels=False)
            rb: ReadResult = b.read_book(ticker, at_ts=now, include_levels=False)
            ka, kb = _key(ra.top), _key(rb.top)
            # No history is recorded here. The grid is for COMPARISON cells; the
            # rings are fed by book events, which is the only resolution at
            # which a 500 ms rule can be evaluated.

            # Section 8: compared only if BOTH streams are VALID at this tick.
            # Per the 2026-08-22 clarification, an unmatched cell is an
            # availability fact, not a disagreement: it enters neither the
            # numerator nor the denominator of the agreement rate.
            if not (ra.is_valid and rb.is_valid):
                unmatched += 1
                continue
            matched += 1
            if ka == kb:
                agreed += 1
                agree_rows.append({
                    "grid_ts": grid_ts, "ticker": ticker, "agree": 1,
                    **_ab_columns(ka, kb), "resolution": RESOLUTION_AGREE, "skew_ms": None,
                })
            else:
                pending.append({"ticker": ticker, "a": ka, "b": kb,
                                "at": now, "at_mono": now_mono})
        self.store.record_comparisons(agree_rows)
        return matched, agreed, unmatched, pending

    def _resolve(self, item: dict[str, Any], grid_ts: float) -> tuple[str, float | None]:
        """
        Apply the frozen skew rule to one disagreement.

        Either stream may be the one behind, so both directions are tried; the
        rule is about observation latency, not about which convention is
        authoritative.
        """
        ticker = item["ticker"]
        at = item["at_mono"]
        a_hist = self._history.get(("A", ticker), StreamHistory())
        b_hist = self._history.get(("B", ticker), StreamHistory())
        tol = float(frozen.EQUIV_SKEW_TOLERANCE_MS)

        b_catches_up = b_hist.reaches(item["a"], after=at, within_ms=tol)
        if b_catches_up is not None:
            return RESOLUTION_SKEW, b_catches_up
        a_catches_up = a_hist.reaches(item["b"], after=at, within_ms=tol)
        if a_catches_up is not None:
            return RESOLUTION_SKEW, a_catches_up
        log.error(
            "depthrec.equiv.unexplained",
            ticker=ticker, grid_ts=grid_ts, wall_at=item["at"],
            a=item["a"], b=item["b"],
        )
        return RESOLUTION_UNEXPLAINED, None

    # -- Plumbing ---------------------------------------------------------------

    def _book_state(self, label: str):
        """
        Feed one stream's book changes into its per-ticker ring.

        On the hot path at the measured 1,731 frames/s across both streams, so
        it does a dict lookup and an O(1) deque append and nothing else.
        """
        history = self._history

        def sink(ticker: str, top, wall_ts: float, mono_ts: float) -> None:
            key = (label, ticker)
            ring = history.get(key)
            if ring is None:
                ring = history[key] = StreamHistory()
            ring.observe(mono_ts, wall_ts, _key(top))

        return sink

    def _sink(self, label: str):
        def sink(direction: str, recv_ts: float, raw: str) -> None:
            session = self.session_a if label == "A" else self.session_b
            epoch = session.session_epoch if session else 0
            # Section 8 retains BOTH streams' frames at full recorder fidelity,
            # through the same compressing writer the recorder uses.
            self.raw_writer.submit(
                stream_label=label, session_epoch=epoch,
                direction=direction, recv_ts=recv_ts, raw=raw,
            )
        return sink

    def _health(self, label: str):
        def sink(kind: str, ts: float, reason: str, detail: str) -> None:
            session = self.session_a if label == "A" else self.session_b
            epoch = session.session_epoch if session else 0
            self.store.record_health(
                stream_label=label, session_epoch=epoch, ts=ts,
                kind=kind, reason=reason, detail=detail,
            )
            # Availability bookkeeping. A disconnect starts a stopwatch; the
            # `subscribed` ack -- not the bare TCP connect -- stops it, because
            # a stream is not comparing again until the server has accepted its
            # subscription and a fresh snapshot can arrive.
            if kind == "disconnect":
                self._diag.disconnects[label] = self._diag.disconnects.get(label, 0) + 1
                self._down_since.setdefault(label, ts)
            elif kind == "subscribed":
                started = self._down_since.pop(label, None)
                if started is not None:
                    self._diag.reconnect_seconds.setdefault(label, []).append(ts - started)
        return sink

    @staticmethod
    def _raise_if_dead(tasks: list[asyncio.Task]) -> None:
        for task in tasks:
            if task.done():
                exc = task.exception()
                if isinstance(exc, SubscriptionRejected):
                    raise exc
                if exc is not None:
                    raise exc
                raise RuntimeError(f"stream {task.get_name()} ended unexpectedly")


def _ab_columns(a: tuple | None, b: tuple | None) -> dict[str, Any]:
    ea = a or (None, None, None, None)
    eb = b or (None, None, None, None)
    return {
        "a_bid": ea[0], "a_bid_qty": ea[1], "a_ask": ea[2], "a_ask_qty": ea[3],
        "b_bid": eb[0], "b_bid_qty": eb[1], "b_ask": eb[2], "b_ask_qty": eb[3],
    }
