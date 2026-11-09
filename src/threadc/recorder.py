"""
The Thread C recorder: one long-lived process banking raw MLB ticker and trade
messages.

What it does, and nothing else:

  * keeps one WebSocket subscription set (canonical YES, use_yes_price=true) on
    the public `ticker` and `trade` channels, covering every open market in the
    append-only collection universe, sharded deterministically across as many
    connections as configuration asks for;
  * writes every raw frame, in and out, to the current segment archive;
  * banks an EXPLICIT initial state, from the REST market census, every time a
    subscription is established -- because neither channel sends anything on
    subscribe, so silence is not a statement about state;
  * runs the proven market census at its proven cadence to follow rotation and
    refresh the subscription;
  * runs the full-catalog discovery census to operate Frozen Rule 1;
  * rotates archive writers internally at 00:00:00 UTC without stopping the
    process or the sockets;
  * halts on low disk and stops at the configured collection end.

What it deliberately does NOT do: read a price, summarise a book, measure a
spread or a depth, count per-market activity, fetch a settlement, or slice
anything by anything. The recorder never inspects accumulated book contents to
make a decision -- every decision it makes comes from the clock, the filesystem,
or official exchange metadata.

Three ordering rules hold the design together.

  Frames go to whichever writer `self._writer` points at, and that pointer is
  only ever reassigned on the event loop between two submits. Rotation is
  therefore a reference swap: the outgoing writer is stopped only after the
  swap, on a worker thread, so not one incoming message is deliberately
  dropped and no frame is ever handed to a stopped writer.

  A discovery census NEVER stops recording. An incomplete one applies zero
  additions and zero removals, is written down as an operational record, and the
  entire last-known-good universe keeps being recorded without interruption.

  Blocking REST work always runs in a worker thread. The Kalshi read limiter
  sleeps and httpx retries can block for many seconds; on the event loop that
  would stall the WebSocket consumer and lose frames the archive exists to keep.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog

from depthrec import integrity
from depthrec.census import RestCensus
from depthrec.rawwriter import RawWriter

from threadc import config as tc_config
from threadc import disk, frozen, segment as seg_mod, shard
from threadc.discovery import DiscoveryResult, run_discovery_census
from threadc.readonly import ALLOWED_PATHS, CensusClientAdapter, ReadOnlyAPI
from threadc.schedule import DiscoverySchedule
from threadc.session import SubscriptionRejected, TickerBankSession
from threadc.universe import AppendOnlyUniverse, Q1_STATUS

log = structlog.get_logger(__name__)

PROCESS_LOG_FILE = "process_starts.jsonl"

LIVE_MARKET_STATUS = frozen.LIVE_MARKET_STATUS
"""The status a censused market must CARRY to be on the wire: "active".

Emphatically NOT the word the census sends as its `status` query filter. Kalshi
takes "open" as a filter value and gives "active" back in the market body; the
vocabularies are disjoint, so comparing a returned status against the filter
word matches nothing at all. That is a silent failure, not an error: a live
5-minute capacity smoke censused 6,947 markets, subscribed to none of them, and
finalized COMPLETE with zero frames banked.

The live census pass filters on the query word already; the reconcile pass does
not, which is how a market that briefly reported some other status is still
recorded as having existed."""


class TerminalStop(Exception):
    """
    An intentional terminal condition: stop, finalize, and do not be restarted.

    Carries the segment state and close reason the current segment must be
    finalized with, plus the process exit status.
    """

    def __init__(self, *, reason: str, segment_state: str, exit_code: int, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.segment_state = segment_state
        self.exit_code = exit_code
        self.detail = detail


class CapacityModeViolation(RuntimeError):
    """
    A Rule 1 full-universe sweep was requested in capacity mode.

    Raised at the entry point rather than relied upon never being called: the
    one-hour capacity measurement must not spend 25 minutes and ~13,800 REST
    requests inside the hour whose bytes, CPU and RAM it is measuring, and
    "the loop happens not to call it" is a weaker guarantee than a refusal.
    """


@dataclass
class RecorderStatus:
    """Operational counters only. Nothing here describes a market."""

    run_id: str
    started_ts: float
    mode: str
    segments_opened: int = 0
    segments_finalized: list[str] = field(default_factory=list)
    rotations: int = 0
    frames_written: int = 0
    bytes_stored: int = 0
    bytes_raw: int = 0
    discovery_censuses: int = 0
    discovery_incomplete: int = 0
    universe_additions: list[str] = field(default_factory=list)
    reconnects: int = 0
    errors: int = 0
    peak_subscribed_markets: int = 0
    subscription_limit_errors: int = 0
    initial_states_banked: int = 0
    initial_state_failures: int = 0
    exit_code: int = frozen.EXIT_OK
    stop_reason: str = ""


class _CensusStoreProxy:
    """
    Forwards depthrec.census.RestCensus's writes to whichever segment store is
    current.

    RestCensus binds one store for its lifetime, but Thread C's store changes at
    every UTC midnight while the census keeps running. Rather than rebuilding
    the census -- which would throw away its in-memory market table and make
    every market look newly discovered at 00:00 every day -- the store it writes
    through is indirected here.
    """

    def __init__(self, store: seg_mod.SegmentStore) -> None:
        self.current = store

    def record_census_request(self, **kw: Any) -> int:
        return self.current.record_census_request(**kw)

    def upsert_census_market(self, **kw: Any) -> str:
        return self.current.upsert_census_market(**kw)

    def record_census_change(self, **kw: Any) -> None:
        self.current.record_census_change(**kw)


INITIAL_STATE_FIELDS = (
    "ticker",
    "status",
    "close_time",
    "updated_time",
    "yes_bid_dollars",
    "yes_ask_dollars",
    "yes_bid_size_fp",
    "yes_ask_size_fp",
    "no_bid_dollars",
    "no_ask_dollars",
    "last_price_dollars",
    "previous_price_dollars",
    "volume_fp",
    "volume_24h_fp",
    "open_interest_fp",
    "liquidity_dollars",
)
"""The fields carried into an explicit initial state, by Kalshi's own names.

Chosen to cover every displayed field the `ticker` channel itself carries -- both
touch prices, both displayed touch sizes, last price, volume and open interest --
plus the market's status and close time, so a reader can tell a live market from
one that had already closed when the subscription came up. A field Kalshi did not
send is simply absent; nothing is defaulted, and no value is transformed."""


def _market_state_projection(market: dict[str, Any]) -> dict[str, Any]:
    """The initial-state projection of one REST market body. Verbatim values."""
    return {k: market[k] for k in INITIAL_STATE_FIELDS if k in market}


@dataclass
class CapacityMeasurement:
    """
    One corrected capacity measurement: the bracketed hour and its two readings.

    Setup, the initial census and subscription establishment all happen BEFORE
    `started_ts`. What is measured is the steady state, which is what B_1h is
    supposed to describe.
    """

    window_seconds: float
    ready_ts: float | None = None
    started_ts: float | None = None
    ended_ts: float | None = None
    disk_start: disk.DiskStatus | None = None
    disk_end: disk.DiskStatus | None = None
    archive_bytes_start: int | None = None
    archive_bytes_end: int | None = None
    cpu_start: Any = None
    cpu_end: Any = None
    counts_start: dict[str, int] = field(default_factory=dict)
    counts_end: dict[str, int] = field(default_factory=dict)
    frames_start: int = 0
    frames_end: int = 0
    max_rss_window_bytes: int | None = None
    reconnects_start: int = 0
    errors_start: int = 0
    subscription_limit_errors_start: int = 0
    complete: bool = False
    abort_reason: str = ""

    @property
    def elapsed(self) -> float | None:
        if self.started_ts is None or self.ended_ts is None:
            return None
        return self.ended_ts - self.started_ts

    def message_delta(self) -> dict[str, int]:
        """Messages received inside the window, by type. Aggregate only."""
        return {
            k: self.counts_end.get(k, 0) - self.counts_start.get(k, 0)
            for k in sorted(set(self.counts_end) | set(self.counts_start))
        }


class ThreadCRecorder:
    """One recorder process. Read-only against Kalshi; there is no order path."""

    def __init__(
        self,
        cfg: tc_config.ThreadCConfig,
        *,
        api: Any = None,
        session_factory: Callable[..., Any] | None = None,
        universe: AppendOnlyUniverse | None = None,
        run_id: str | None = None,
        now_fn: Callable[[], float] = time.time,
        git_commit: str | None = None,
        disk_check: Callable[[Path, int], disk.DiskStatus] | None = None,
    ) -> None:
        self.cfg = cfg
        self.now = now_fn
        self._disk_check = disk_check or disk.check
        self.run_id = run_id or f"threadc-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.git_commit = git_commit or integrity.recorder_git_commit()

        # Fails closed on a bad artifact hash before anything is opened.
        self.universe = universe if universe is not None else AppendOnlyUniverse(cfg.archive_dir)

        self.api = api if api is not None else ReadOnlyAPI()
        self.census = RestCensus(
            client=CensusClientAdapter(self.api), store=None, series=self.universe.series
        )

        self.status = RecorderStatus(
            run_id=self.run_id, started_ts=self.now(), mode=cfg.mode
        )
        self._session_factory = session_factory or self._default_session_factory

        self.segment: seg_mod.Segment | None = None
        self._writer: RawWriter | None = None
        self._census_proxy: _CensusStoreProxy | None = None
        self.sessions: dict[str, Any] = {}
        self._shard_plan = shard.plan(
            [], cfg.ws_shards, cfg.max_markets_per_shard
        )
        self._assigned: dict[str, str] = {}
        self._recorded_epochs: set[tuple[str, int]] = set()
        self._empty_subscription_noted = False
        self.schedule = DiscoverySchedule(
            cfg.archive_dir, target_utc=cfg.discovery_daily_target_utc
        )
        self._stop = asyncio.Event()
        # Set once the WebSocket session tasks are live. The discovery loop waits
        # on it, so a ~13,800-request full-catalog sweep can never sit in front
        # of the first frame being banked -- on a cold start or on a restart.
        self._ws_recording_started = asyncio.Event()
        # Set once EVERY shard has an established subscription on every selected
        # channel -- both of the frozen pair, or the one production selects.
        # The corrected capacity measurement waits on this before starting its
        # clock, so setup and subscription establishment fall outside the
        # measured hour instead of inside it.
        self._subscriptions_established = asyncio.Event()
        self._established_shards: set[str] = set()
        self._initial_state_tasks: set[asyncio.Task] = set()
        self.capacity: CapacityMeasurement | None = None
        self._terminal: TerminalStop | None = None
        self._start_disk: disk.DiskStatus | None = None
        self.final_manifest: dict[str, Any] | None = None

    # -- Startup ---------------------------------------------------------------

    def startup_record(self) -> dict[str, Any]:
        """Operational startup facts. No market information of any kind."""
        return {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "started_ts": self.status.started_ts,
            "started_utc": tc_config.iso_utc(self.status.started_ts),
            "git_commit": self.git_commit,
            "mode": self.cfg.mode,
            "doc": frozen.DOC_REFERENCE,
            "fidelity_doc": frozen.FIDELITY_DOC_REFERENCE,
            "api_env": getattr(self.api, "api_env", ""),
            "base_url": getattr(self.api, "base_url", ""),
            "ws_url": self._ws_url(),
            "channels": list(self.cfg.channels),
            "channels_are_frozen_pair": self.cfg.channels_are_frozen_pair,
            "use_yes_price": frozen.USE_YES_PRICE,
            "config": self.cfg.summary(),
            "discovery_schedule": self.schedule.summary(),
            "universe": self.universe.identity(),
            "q1": Q1_STATUS,
            "read_only_endpoints": [rx.pattern for rx in ALLOWED_PATHS],
        }

    def _log_process_start(self, record: dict[str, Any]) -> None:
        """
        Append this process start to the archive's process log.

        Every start and restart is recorded, so a gap in collection is
        attributable to a specific restart rather than having to be inferred
        from the absence of frames.
        """
        self.cfg.archive_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.archive_dir / PROCESS_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def _open_segment(self, *, now: float | None = None) -> seg_mod.Segment:
        seg = seg_mod.open_segment(
            self.cfg.archive_dir, run_id=self.run_id, git_commit=self.git_commit,
            mode=self.cfg.mode, now=now if now is not None else self.now(),
            universe_identity=self.universe.identity(),
            channels=self.cfg.channels,
        )
        assert seg.store is not None
        seg.store.record_process(
            started_ts=self.status.started_ts, mode=self.cfg.mode,
            api_env=getattr(self.api, "api_env", ""),
            base_url=getattr(self.api, "base_url", ""),
            ws_url=self._ws_url(), config=self.cfg.summary(),
            universe=self.universe.identity(),
        )
        seg.store.set_meta("shard_plan", self._shard_plan.summary())
        self.segment = seg
        self.status.segments_opened += 1
        if self._census_proxy is None:
            self._census_proxy = _CensusStoreProxy(seg.store)
            self.census._store = self._census_proxy
        else:
            self._census_proxy.current = seg.store
        return seg

    def _ws_url(self) -> str:
        cfg = getattr(self.api, "_cfg", None)
        return getattr(cfg, "ws_url", "") if cfg is not None else ""

    # -- Sinks ------------------------------------------------------------------

    def _raw_sink(self, label: str, session: Any) -> Callable[[str, float, str], None]:
        def sink(direction: str, recv_ts: float, raw: str) -> None:
            writer = self._writer
            if writer is None:
                return
            writer.submit(
                stream_label=label, session_epoch=session.session_epoch,
                direction=direction, recv_ts=recv_ts, raw=raw,
            )
        return sink

    def _health_sink(self, label: str, session: Any) -> Callable[[str, float, str, str], None]:
        def sink(kind: str, ts: float, reason: str, detail: str) -> None:
            seg = self.segment
            if seg is None or seg.store is None or seg.store.closed:
                return
            seg.store.record_health(
                stream_label=label, session_epoch=session.session_epoch,
                ts=ts, kind=kind, reason=reason, detail=detail,
            )
            if kind == "connect":
                self._record_session_row(label, session, ts)
                if session.session_epoch > 1:
                    self.status.reconnects += 1
            elif kind in ("disconnect", "server_error", "parse_error", "book_error"):
                self.status.errors += 1
                if kind == "server_error" and _is_subscription_limit(detail):
                    # Counted here rather than only where SubscriptionRejected is
                    # caught: depthrec.session records this health event on every
                    # shard that sees the error, immediately before raising, so
                    # this path sees each occurrence exactly once.
                    self.status.subscription_limit_errors += 1
                    log.error(
                        "threadc.subscription_limit_error_26",
                        shard=label, occurrences=self.status.subscription_limit_errors,
                    )
        return sink

    def _record_session_row(self, label: str, session: Any, ts: float) -> None:
        seg = self.segment
        if seg is None or seg.store is None or seg.store.closed:
            return
        key = (label, int(session.session_epoch))
        if key in self._recorded_epochs:
            return
        self._recorded_epochs.add(key)
        seg.store.record_session(
            shard_label=label, session_epoch=session.session_epoch,
            use_yes_price=frozen.USE_YES_PRICE, channels=list(self.cfg.channels),
            subscribe_params=session.subscribe_params, ws_url=self._ws_url(),
            opened_ts=ts,
        )

    def _default_session_factory(self, *, tickers: list[str], label: str) -> Any:
        # The selection is read here rather than in _make_session so an injected
        # factory keeps its two-argument signature: the channels a session
        # subscribes to are the real session's business, and every test double
        # stays constructible exactly as before.
        return TickerBankSession(
            tickers=tickers, label=label, channels=self.cfg.channels
        )

    def _make_session(self, label: str, tickers: list[str]) -> Any:
        session = self._session_factory(tickers=tickers, label=label)
        session._raw_sink = self._raw_sink(label, session)
        session._health_sink = self._health_sink(label, session)
        session._on_epoch_established = self._on_epoch_established
        return session

    # -- Explicit initial state -------------------------------------------------

    def _on_epoch_established(self, label: str, epoch: int, ts: float) -> None:
        """
        A shard's subscription is live on every channel.

        Called from the session's dispatch, i.e. on the event loop, so the
        banking itself is handed to a task rather than done inline -- the
        consumer must not stop reading the socket to write ~7,700 rows.
        """
        self._established_shards.add(label)
        if set(self._established_shards) >= set(self.sessions):
            self._subscriptions_established.set()
        task = asyncio.ensure_future(
            self._bank_initial_state(label, epoch, ts, reason="subscription_established")
        )
        self._initial_state_tasks.add(task)
        task.add_done_callback(self._initial_state_tasks.discard)

    async def _bank_initial_state(
        self, label: str, epoch: int, ts: float, *,
        reason: str, tickers: list[str] | None = None,
    ) -> int:
        """
        Write the explicit displayed state for a shard's markets, from REST.

        Neither `ticker` nor `trade` sends anything on subscribe, so without this
        the archive would begin every connection epoch with no state at all and
        no way to distinguish "unchanged" from "never told". The state comes from
        the market census's own bodies -- the same GET /markets responses already
        banked in full -- so this costs no additional request and cannot fail
        because the exchange was busy.

        Projection only, with Kalshi's own field names and values kept verbatim.
        Nothing is renamed, rescaled, combined or derived here: this is a record
        of what was displayed, not a reading of it.
        """
        session = self.sessions.get(label)
        if session is None:
            return 0
        wanted = list(tickers) if tickers is not None else list(session.tickers)
        seg = self.segment
        if seg is None or seg.store is None or seg.store.closed or not wanted:
            return 0

        rows = []
        for ticker in wanted:
            entry = self.census.entries.get(ticker)
            if entry is None or not entry.market:
                # No REST body for this market yet. Recorded as a failure count
                # rather than filled in from anywhere: an invented initial state
                # is worse than a missing one.
                self.status.initial_state_failures += 1
                continue
            rows.append((
                ts, label, epoch, reason, ticker, "rest_market_census",
                entry.last_seen_ts,
                json.dumps(_market_state_projection(entry.market), sort_keys=True),
            ))

        def write() -> int:
            store = seg.store
            if store is None or store.closed:
                return 0
            return store.record_initial_state(rows)

        try:
            written = await asyncio.to_thread(write)
        except Exception as exc:  # noqa: BLE001
            # Never fatal. A missing initial state is a documented hole in one
            # epoch; a recorder that dies trying to write one loses the hour.
            self.status.errors += 1
            log.error("threadc.initial_state.failed", shard=label, error=str(exc))
            self._health("initial_state_failed", f"{label} epoch={epoch}: {exc}")
            return 0

        self.status.initial_states_banked += written
        log.info(
            "threadc.initial_state.banked",
            shard=label, epoch=epoch, reason=reason, markets=written,
            missing=len(wanted) - written,
        )
        return written

    # -- Run --------------------------------------------------------------------

    async def run(
        self,
        *,
        capacity_window_seconds: float | None = None,
        stop_after_seconds: float | None = None,
    ) -> RecorderStatus:
        """
        Run until something stops it.

        `capacity_window_seconds` requests the corrected capacity measurement and
        is refused outside capacity mode -- a production process must not be
        bounded by a measurement window.

        `stop_after_seconds` is a plain deadline with no measurement attached. It
        exists for tests, which need a recorder that comes up fully and then
        stops; nothing in production passes it.
        """
        if capacity_window_seconds is not None:
            if self.cfg.mode != frozen.MODE_CAPACITY:
                raise CapacityModeViolation(
                    f"a capacity measurement window was requested in "
                    f"{self.cfg.mode!r} mode; the measurement bounds a measurement "
                    f"run, never a production collection"
                )
            self.capacity = CapacityMeasurement(window_seconds=capacity_window_seconds)

        record = self.startup_record()
        log.info("threadc.startup", **{k: v for k, v in record.items() if k != "universe"})
        self._log_process_start(record)

        # Checked BEFORE anything is opened. A disk already below the reserve
        # must not have a fresh segment created on it just to be finalized: on a
        # genuinely full filesystem that open would fail, and a failed open is an
        # ordinary error, which systemd restarts -- the crash loop the halt
        # exists to prevent. Exiting 75 with nothing opened is not restarted.
        self._start_disk = self._disk_check(self.cfg.archive_dir, self.cfg.disk_reserve_bytes)
        if self._start_disk.below_reserve:
            raise TerminalStop(
                reason=frozen.CLOSE_LOW_DISK, segment_state=frozen.SEGMENT_HALTED_LOW_DISK,
                exit_code=frozen.EXIT_TERMINAL_NO_RESTART,
                detail=(
                    f"free {self._start_disk.free_bytes} <= reserve "
                    f"{self.cfg.disk_reserve_bytes} at startup; nothing was opened"
                ),
            )

        # Any segment left OPEN by a dead process is preserved and labelled
        # INCOMPLETE_CRASH before a new one is opened, never silently reused.
        orphans = seg_mod.recover_orphans(
            self.cfg.archive_dir, universe_identity=self.universe.identity()
        )
        for m in orphans:
            log.warning(
                "threadc.orphan_recovered", segment=m["segment_name"],
                state=m["segment_state"], frames=m.get("raw_frame_count"),
            )

        self._open_segment()
        self._start_writer()

        # STARTUP ORDERING. Live banking comes first, always.
        #
        #   1. the append-only universe, seeded from the hash-verified committed
        #      Collection Universe v1 plus every prior Rule 1 admission replayed
        #      from the addition log -- no network call needed;
        #   2. the ordinary known-universe market census, so the first subscribe
        #      covers everything already open rather than trickling in over a
        #      minute;
        #   3. the WebSocket sessions, and raw recording begins.
        #
        # The ~13,800-request full-universe Rule 1 sweep runs only AFTER all of
        # that, asynchronously, gated on _ws_recording_started. A cold start or
        # a restart therefore never waits ~25 minutes for discovery before the
        # first frame is banked.
        await asyncio.to_thread(self.census.run_live_pass)
        await asyncio.to_thread(self.census.run_reconcile_pass)
        self._rebuild_shards()

        tasks = [
            asyncio.create_task(self._market_census_loop(), name="market-census"),
            asyncio.create_task(self._market_reconcile_loop(), name="market-reconcile"),
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._rotation_loop(), name="rotation"),
            asyncio.create_task(self._disk_loop(), name="disk"),
            asyncio.create_task(self._collection_end_loop(), name="collection-end"),
        ]
        tasks += [
            asyncio.create_task(s.run(), name=f"ws-{label}")
            for label, s in self.sessions.items()
        ]
        # Banking is up. Only now may a full-universe sweep be considered.
        self._ws_recording_started.set()
        log.info(
            "threadc.ws_recording_started",
            shards=sorted(self.sessions), markets=self._subscribed_market_count(),
            startup_seconds=round(self.now() - self.status.started_ts, 3),
            # The readiness line says WHAT is now being banked, not only that
            # something is. Production collects ticker-only on the Branch-2
            # measurement, and an operator reading "recording started" has to be
            # able to see which selection came up without opening a manifest.
            channels=list(self.cfg.channels),
            channels_are_frozen_pair=self.cfg.channels_are_frozen_pair,
        )
        if capacity_window_seconds is not None:
            tasks.append(
                asyncio.create_task(self._capacity_measurement(), name="capacity")
            )
        if stop_after_seconds is not None:
            tasks.append(
                asyncio.create_task(self._deadline(stop_after_seconds), name="deadline")
            )

        close_state = frozen.SEGMENT_COMPLETE
        close_reason = frozen.CLOSE_OPERATOR_SIGNAL
        try:
            # FIRST_COMPLETED, not FIRST_EXCEPTION: a WebSocket session's
            # reconnect loop never ends on its own, so waiting for every task to
            # finish would hang forever after a deadline fires normally.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    raise exc
            if self._terminal is not None:
                raise self._terminal
            if capacity_window_seconds is not None or self.cfg.mode == frozen.MODE_CAPACITY:
                close_reason = frozen.CLOSE_CAPACITY_RUN_END
        except TerminalStop as stop:
            close_state, close_reason = stop.segment_state, stop.reason
            self.status.exit_code = stop.exit_code
            self.status.stop_reason = f"{stop.reason}: {stop.detail}" if stop.detail else stop.reason
            log.error(
                "threadc.terminal_stop", reason=stop.reason, detail=stop.detail,
                exit_code=stop.exit_code, segment_state=stop.segment_state,
            )
        except SubscriptionRejected as exc:
            self.status.errors += 1
            self.status.exit_code = frozen.EXIT_FAILURE
            self.status.stop_reason = f"subscription_rejected code={exc.code}"
            close_state = frozen.SEGMENT_INCOMPLETE_CRASH
            close_reason = "subscription_rejected"
            log.error(
                "threadc.subscription_rejected", code=exc.code,
                is_market_limit=exc.is_market_limit, payload=exc.payload,
                shards=self._shard_plan.summary(),
                subscription_limit_errors=self.status.subscription_limit_errors,
            )
        except BaseException:
            # An unexpected failure is not a finished day. Labelling it COMPLETE
            # would hide a gap behind the one state the health check treats as
            # healthy, so the segment is finalized as what it actually is and
            # the exception is re-raised for systemd to restart on.
            close_state = frozen.SEGMENT_INCOMPLETE_CRASH
            close_reason = "unexpected_failure"
            self.status.exit_code = frozen.EXIT_FAILURE
            raise
        finally:
            await self._teardown(tasks, state=close_state, close_reason=close_reason)
        return self.status

    async def _teardown(self, tasks: list, *, state: str, close_reason: str) -> None:
        self._stop.set()
        for label, session in self.sessions.items():
            try:
                await asyncio.wait_for(
                    session.stop(), timeout=frozen.SESSION_CLOSE_TIMEOUT_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - includes TimeoutError
                log.warning("threadc.session_close_failed", shard=label, error=str(exc))
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=frozen.SHUTDOWN_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("threadc.shutdown_timeout", seconds=frozen.SHUTDOWN_GRACE_SECONDS)
        self.final_manifest = await asyncio.to_thread(
            self._finalize_current, state, close_reason
        )
        try:
            self.api.close()
        except Exception:  # noqa: BLE001
            pass

    def _start_writer(self) -> None:
        seg = self.segment
        assert seg is not None and seg.store is not None
        writer = RawWriter(
            seg.store,
            health=lambda kind, detail: seg.store.record_health(
                stream_label="writer", session_epoch=0, ts=time.time(),
                kind=kind, reason="", detail=detail,
            ),
        )
        writer.start()
        self._writer = writer

    def _finalize_current(self, state: str, close_reason: str) -> dict[str, Any] | None:
        """Drain the writer, then finalize. Runs on a worker thread."""
        seg, writer = self.segment, self._writer
        self._writer = None
        if writer is not None:
            self.status.frames_written += writer.frames_written
            self.status.bytes_stored += writer.bytes_stored
            self.status.bytes_raw += writer.bytes_raw
            writer.stop(timeout=frozen.WRITER_DRAIN_TIMEOUT_SECONDS)
        if seg is None:
            return None
        seg.extra["shard_plan"] = self._shard_plan.summary()
        manifest = seg_mod.finalize(
            seg, state=state, close_reason=close_reason,
            universe_identity=self.universe.identity(),
        )
        self.status.segments_finalized.append(
            f"{manifest['segment_name']}:{manifest['segment_state']}"
        )
        return manifest

    async def _capacity_measurement(self) -> None:
        """
        The corrected capacity measurement: an exactly bracketed hour.

        The first capacity run measured from process start, so setup, the
        initial market census and the establishment of ~10,000 subscriptions all
        landed inside the interval whose bytes were divided by an hour. Those
        costs are real but one-off, and including them makes B_1h describe a
        startup rather than the steady state it is meant to size storage for.

        So the order here is strict, and each step is a mark in the archive:

          1. wait until every shard subscription is established;
          2. drain the initial-state banking -- also setup;
          3. read filesystem usage, CPU and the message counters;
          4. mark measurement_start;
          5. collect for exactly `window_seconds`, touching nothing;
          6. mark measurement_end;
          7. read filesystem usage, CPU and the counters again.

        B_1h,disk is the difference between the two filesystem readings. It is
        never extrapolated from a shorter interval and never taken from a logical
        payload-byte counter -- the previous report offered both, and both can
        disagree with what the filesystem actually consumed.
        """
        m = self.capacity
        assert m is not None
        try:
            await asyncio.wait_for(
                self._subscriptions_established.wait(),
                timeout=frozen.CAPACITY_ESTABLISH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            m.abort_reason = (
                f"subscriptions were not established within "
                f"{frozen.CAPACITY_ESTABLISH_TIMEOUT_SECONDS}s; the measurement was "
                f"not started rather than run on a half-subscribed universe"
            )
            self.status.errors += 1
            log.error("threadc.capacity.establish_timeout", detail=m.abort_reason)
            self._health("capacity_establish_timeout", m.abort_reason)
            self._stop.set()
            return
        if self._stop.is_set():
            m.abort_reason = "stopped before the measurement could start"
            return

        # The initial-state write is setup too, and it is a few thousand rows.
        pending = list(self._initial_state_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        m.ready_ts = self.now()
        m.disk_start = self._disk_check(self.cfg.archive_dir, self.cfg.disk_reserve_bytes)
        m.archive_bytes_start = _archive_bytes(self.cfg.archive_dir)
        m.cpu_start = os.times()
        m.counts_start = self._message_counts()
        m.frames_start = self._frames_so_far()
        m.reconnects_start = self.status.reconnects
        m.errors_start = self.status.errors
        m.subscription_limit_errors_start = self.status.subscription_limit_errors
        m.started_ts = self.now()
        log.info(
            "threadc.capacity.measurement_start",
            window_seconds=m.window_seconds,
            subscribed_markets=self._subscribed_market_count(),
            setup_seconds=round(m.started_ts - self.status.started_ts, 3),
            free_bytes=m.disk_start.free_bytes,
        )
        self._health(
            "capacity_measurement_start",
            f"window={m.window_seconds}s free_bytes={m.disk_start.free_bytes} "
            f"setup_seconds={round(m.started_ts - self.status.started_ts, 3)}",
        )

        rss_task = asyncio.ensure_future(self._sample_rss(m))
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=m.window_seconds)
            m.abort_reason = "stopped before the measurement window elapsed"
        except asyncio.TimeoutError:
            m.complete = True
        finally:
            rss_task.cancel()
            m.ended_ts = self.now()
            m.cpu_end = os.times()
            m.counts_end = self._message_counts()
            m.frames_end = self._frames_so_far()
            m.disk_end = self._disk_check(self.cfg.archive_dir, self.cfg.disk_reserve_bytes)
            m.archive_bytes_end = _archive_bytes(self.cfg.archive_dir)
            self._health(
                "capacity_measurement_end",
                f"complete={m.complete} elapsed={m.elapsed} "
                f"free_bytes={m.disk_end.free_bytes}",
            )
            log.info(
                "threadc.capacity.measurement_end",
                complete=m.complete, elapsed_seconds=m.elapsed,
                disk_growth_bytes=(
                    m.disk_start.free_bytes - m.disk_end.free_bytes
                    if m.disk_start else None
                ),
            )
        self._stop.set()

    async def _deadline(self, seconds: float) -> None:
        """
        Stop after `seconds`, measuring nothing.

        Used by tests that need a fully started recorder to come back down. The
        capacity measurement does NOT use this: its window is bracketed by two
        filesystem readings and begins only once subscriptions are established.
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            self._stop.set()

    async def _sample_rss(self, m: CapacityMeasurement) -> None:
        """
        Track peak resident memory INSIDE the window.

        `ru_maxrss` is a process-lifetime high-water mark, so on its own it would
        report the startup peak -- including the census and the subscription
        burst -- as if it were the steady-state cost of an hour of collection.
        """
        while True:
            rss = _current_rss_bytes()
            if rss is not None:
                m.max_rss_window_bytes = max(m.max_rss_window_bytes or 0, rss)
            await asyncio.sleep(frozen.CAPACITY_RSS_SAMPLE_SECONDS)

    def _message_counts(self) -> dict[str, int]:
        """Aggregate WS message counts by TYPE across every shard."""
        total: dict[str, int] = {}
        for session in self.sessions.values():
            for kind, n in getattr(session, "message_counts", {}).items():
                total[kind] = total.get(kind, 0) + n
        return total

    def _frames_so_far(self) -> int:
        """Frames written including the writer that has not been stopped yet."""
        writer = self._writer
        return self.status.frames_written + (writer.frames_written if writer else 0)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    # -- Market census and subscription refresh --------------------------------

    async def _market_census_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(frozen.MARKET_CENSUS_LIVE_INTERVAL_SECONDS)
            if self._stop.is_set():
                return
            try:
                await asyncio.to_thread(self.census.run_live_pass)
                await self._sync_subscription()
            except Exception as exc:  # noqa: BLE001
                # A failed discovery cycle is a health event, never a shrunk
                # universe: the census has no delete path.
                self.status.errors += 1
                log.error("threadc.market_census.error", error=str(exc))
                self._health("market_census_error", str(exc))

    async def _market_reconcile_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(frozen.MARKET_CENSUS_RECONCILE_INTERVAL_SECONDS)
            if self._stop.is_set():
                return
            try:
                await asyncio.to_thread(self.census.run_reconcile_pass)
                await self._sync_subscription()
            except Exception as exc:  # noqa: BLE001
                self.status.errors += 1
                log.error("threadc.market_reconcile.error", error=str(exc))
                self._health("market_reconcile_error", str(exc))

    def wanted_markets(self, *, now: float | None = None) -> set[str]:
        """
        Every market in the universe that is open right now, plus those that
        closed within the unsubscribe lag.

        No lead-time window and no horizon: Thread C collects continuously and
        does not choose when a market becomes interesting.
        """
        now = self.now() if now is None else now
        want: set[str] = set()
        for entry in self.census.entries.values():
            if entry.status == LIVE_MARKET_STATUS:
                want.add(entry.ticker)
                continue
            # A market the census has seen close stays on the wire for the
            # unsubscribe lag, so the frames either side of a close are not cut
            # off by census timing. Anything else -- unopened, long settled -- is
            # recorded in the census as having existed but is not subscribed.
            if entry.close_ts is not None and 0 <= now - entry.close_ts <= frozen.UNSUBSCRIBE_LAG_SECONDS:
                want.add(entry.ticker)
        return want

    def _check_subscription_not_empty(self, want: set[str]) -> None:
        """
        A non-empty census that puts nothing on the wire is a defect, not a
        quiet night.

        Aggregate counts only -- how many markets the census knows of and how
        many are wanted, never which. This is the exact shape of the status
        mismatch that let a 5-minute smoke census 6,947 markets, subscribe to
        none, and finalize COMPLETE, so it is written down where an operator
        looking at the segment will see it. Recorded on the transition rather
        than on every census pass: the condition persists, and a health event a
        minute would bury everything else in the table.
        """
        censused = len(self.census.entries)
        if censused and not want:
            if not self._empty_subscription_noted:
                self._empty_subscription_noted = True
                log.error(
                    "threadc.subscription_set_empty",
                    censused_markets=censused, wanted_markets=0,
                    live_market_status=LIVE_MARKET_STATUS,
                )
                self._health(
                    "subscription_set_empty",
                    f"census knows {censused} market(s), 0 wanted -- nothing is on "
                    f"the wire while markets are being censused",
                )
        elif want:
            self._empty_subscription_noted = False

    def _rebuild_shards(self) -> None:
        """Build the shard plan and the sessions for it. Startup only."""
        want = self.wanted_markets()
        self._check_subscription_not_empty(want)
        self._shard_plan = shard.plan(
            sorted(want), self.cfg.ws_shards, self.cfg.max_markets_per_shard
        )
        over = self._shard_plan.over_capacity()
        if over:
            log.error(
                "threadc.shard.over_capacity", shards=over,
                occupancy=self._shard_plan.occupancy,
                max_markets_per_shard=self.cfg.max_markets_per_shard,
                note="raise ws_shards; the recorder does not re-partition itself",
            )
            self._health("shard_over_capacity", json.dumps(self._shard_plan.occupancy))
        for label, tickers in sorted(self._shard_plan.assignment.items()):
            self.sessions[label] = self._make_session(label, list(tickers))
            for t in tickers:
                self._assigned[t] = label
            self._log_shard(label, tickers, "assign")
        self._note_peak_subscribed()
        if self.segment is not None and self.segment.store is not None:
            self.segment.store.set_meta("shard_plan", self._shard_plan.summary())

    def _subscribed_market_count(self) -> int:
        """Total markets currently on the wire across every shard."""
        return sum(len(s.tickers) for s in self.sessions.values())

    def _note_peak_subscribed(self) -> int:
        """
        Track the high-water mark of simultaneously subscribed markets.

        An aggregate count and nothing else -- no identities, no per-market
        anything. It exists so the capacity run can say whether the universe can
        actually be held on the wire.
        """
        total = self._subscribed_market_count()
        self.status.peak_subscribed_markets = max(
            self.status.peak_subscribed_markets, total
        )
        return total

    def _log_shard(self, label: str, tickers: list[str], action: str, detail: str = "") -> None:
        seg = self.segment
        if seg is None or seg.store is None or seg.store.closed:
            return
        now = self.now()
        session = self.sessions.get(label)
        epoch = int(getattr(session, "session_epoch", 0) or 0)
        for t in tickers:
            seg.store.record_shard_assignment(
                ts=now, ticker=t, shard_label=label, action=action,
                session_epoch=epoch, detail=detail,
            )

    async def _sync_subscription(self) -> None:
        """
        Bring each shard's subscription in line with what REST says is open.

        Every market belongs to exactly one shard, by hash, so a market is only
        ever added to one connection. A market whose shard changes -- which can
        only happen if ws_shards is reconfigured across a restart -- is logged as
        a shard_change on both labels.
        """
        want = self.wanted_markets()
        self._check_subscription_not_empty(want)
        self._shard_plan = shard.plan(
            sorted(want), self.cfg.ws_shards, self.cfg.max_markets_per_shard
        )
        for label, session in sorted(self.sessions.items()):
            target = set(self._shard_plan.assignment.get(label, []))
            have = set(session.tickers)
            add = sorted(target - have)
            remove = sorted(have - target)
            if not add and not remove:
                continue
            await session.update_markets(add, remove)
            if add:
                self._log_shard(label, add, "subscribe")
                for t in add:
                    prior = self._assigned.get(t)
                    if prior is not None and prior != label:
                        self._health(
                            "shard_change", f"{t}: {prior} -> {label}"
                        )
                    self._assigned[t] = label
                # A market added to a LIVE subscription gets no snapshot either
                # -- neither channel sends one -- so its explicit initial state
                # is banked here for exactly the same reason it is banked at
                # subscription establishment.
                await self._bank_initial_state(
                    label, int(getattr(session, "session_epoch", 0) or 0),
                    self.now(), reason="market_added", tickers=add,
                )
            if remove:
                self._log_shard(label, remove, "unsubscribe")
        self._note_peak_subscribed()

    # -- Frozen Rule 1: the full-catalog discovery census ----------------------

    async def _discovery_loop(self) -> None:
        """
        Once per UTC calendar day, targeted at the configured UTC time.

        Never before live banking is up, never in capacity mode, never twice for
        an accepted UTC day, and never within the frozen restart-storm interval
        of the previous attempt's start. Every one of those rules is enforced in
        threadc.schedule against state persisted outside this process.
        """
        if not self.cfg.discovery_enabled:
            log.info(
                "threadc.discovery.disabled",
                mode=self.cfg.mode,
                reason=(
                    "Rule 1 full-universe discovery does not run in capacity mode: "
                    "the run starts from the verified current append-only universe "
                    "and a missed-target sweep would put ~25 minutes of REST load, "
                    "CPU and response bodies inside the hour being measured"
                ),
            )
            await self._stop.wait()
            return

        # Gate on live banking, not on a timer. This is what makes "startup does
        # not wait for discovery" a property rather than an accident of the
        # order in which tasks happened to be created.
        await self._ws_recording_started.wait()

        while not self._stop.is_set():
            decision = self.schedule.decide(self.now())
            log.debug("threadc.discovery.decision", **decision.log_fields())
            if not decision.due:
                await self._sleep(max(1.0, decision.next_check_seconds))
                continue
            log.info("threadc.discovery.due", **decision.log_fields())
            try:
                await self.run_discovery_once()
            except Exception as exc:  # noqa: BLE001
                # A discovery failure must NEVER take the live recorder down.
                # The attempt start is already persisted, so the guard holds and
                # this cannot become a tight retry loop.
                self.status.errors += 1
                log.error("threadc.discovery.loop_error", error=str(exc))
                self._health("discovery_error", str(exc))
                await self._sleep(max(1.0, self.schedule.blocked_by_guard(self.now())))

    async def run_discovery_once(self, *, force: bool = False) -> DiscoveryResult | None:
        """
        One full-catalog census, applied only if it is COMPLETE.

        Refuses outright in capacity mode. Otherwise the restart-storm guard is
        checked and the attempt start is PERSISTED BEFORE the sweep begins, so a
        crash four minutes into a 25-minute sweep comes back to a guard that
        already knows a sweep was started -- which is what stops Restart=always
        from launching a fresh sweep every RestartSec.

        The sweep itself runs entirely in a worker thread: it is ~13,800
        authenticated reads over many minutes, and the WebSocket consumer never
        waits on it.
        """
        if not self.cfg.discovery_enabled:
            raise CapacityModeViolation(
                f"Rule 1 full-universe discovery is disabled in {self.cfg.mode!r} mode; "
                f"a capacity run starts from the current append-only universe and "
                f"must not spend a sweep inside the hour it is measuring"
            )

        now = self.now()
        if not force:
            owed = self.schedule.blocked_by_guard(now)
            if owed > 0:
                log.warning(
                    "threadc.discovery.guard_blocked", seconds_owed=round(owed, 1),
                    min_interval=frozen.DISCOVERY_ATTEMPT_MIN_INTERVAL_SECONDS,
                )
                return None

        utc_day = seg_mod.utc_date_of(now)
        census_id = f"census-{int(now)}-{uuid.uuid4().hex[:6]}"
        # Persisted FIRST. See threadc.schedule.
        self.schedule.record_attempt_start(now, census_id, utc_day)

        result = await asyncio.to_thread(
            run_discovery_census, self.api, census_id=census_id,
            should_stop=self._stop.is_set,
        )
        change = self.universe.apply_census(result)
        self.schedule.record_result(self.now(), census_id, result.status, utc_day)

        self.status.discovery_censuses += 1
        if not result.complete:
            self.status.discovery_incomplete += 1
            self._health(
                "discovery_census_incomplete",
                f"census={result.census_id} unreadable={len(result.unreadable)} "
                f"additions=0 removals=0 -- recording continues on "
                f"{len(self.universe.members)} series; UTC day {utc_day} stays "
                f"unaccepted and is retried after the guard interval",
            )
        seg = self.segment
        if seg is not None and seg.store is not None and not seg.store.closed:
            seg.store.record_discovery_census(
                result.record(),
                {"added": change.added, "observations": change.observations,
                 "applied": change.applied, "census_status": change.census_status,
                 "utc_day": utc_day,
                 "universe_version_before": change.version_before,
                 "universe_version_after": change.version_after},
            )
        if change.added:
            self.status.universe_additions.extend(change.added)
            # Rule 1 is operational: a newly admitted series is censused and on
            # the wire without waiting for a restart.
            self.census.series = tuple(self.universe.series)
            await asyncio.to_thread(self.census.run_live_pass)
            await self._sync_subscription()
        return result

    # -- Internal daily UTC rotation -------------------------------------------

    async def _rotation_loop(self) -> None:
        assert self.segment is not None
        boundary = seg_mod.next_utc_midnight(self.segment.started_ts)
        while not self._stop.is_set():
            await self._sleep(frozen.ROTATION_CHECK_SECONDS)
            if self._stop.is_set():
                return
            now = self.now()
            if now < boundary:
                continue
            try:
                await self.rotate(now=now)
            except Exception as exc:  # noqa: BLE001
                self.status.errors += 1
                log.error("threadc.rotation.failed", error=str(exc))
                self._health("rotation_failed", str(exc))
            boundary = seg_mod.next_utc_midnight(now)

    async def rotate(self, *, now: float | None = None) -> dict[str, Any] | None:
        """
        Swap archive writers at a UTC day boundary without touching the sockets.

        The order is what makes this lossless: the new segment and its writer are
        fully open BEFORE `self._writer` is reassigned, and the outgoing writer
        is stopped only afterwards, on a worker thread. Between those two points
        the event loop never yields, so every frame goes to exactly one live
        writer and none is deliberately dropped. The WebSocket connections are
        not disconnected and the systemd service is not restarted.
        """
        now = self.now() if now is None else now
        old_seg, old_writer = self.segment, self._writer
        if old_seg is None:
            return None

        new_seg = self._open_segment(now=now)
        new_writer = RawWriter(
            new_seg.store,
            health=lambda kind, detail: new_seg.store.record_health(
                stream_label="writer", session_epoch=0, ts=time.time(),
                kind=kind, reason="", detail=detail,
            ),
        )
        new_writer.start()

        # The swap. One assignment, on the event loop, between two submits.
        self._writer = new_writer
        self.status.rotations += 1

        # Carry the live connection epochs into the new segment so its manifest
        # can identify gaps without reference to the previous file.
        self._recorded_epochs.clear()
        for label, session in sorted(self.sessions.items()):
            if getattr(session, "connected", False):
                self._record_session_row(label, session, now)
        new_seg.store.record_health(
            stream_label="rotation", session_epoch=0, ts=now,
            kind="rotation_in", reason="",
            detail=f"continued from {old_seg.name} without WS restart",
        )

        # A rotation is not a reconnect: the sockets keep running and neither
        # channel will resend anything. Without this, a reader who opens the NEW
        # segment alone would have no state at 00:00 UTC and would have to go
        # back to the previous file to find one -- and every segment here is
        # meant to be independently readable. So each segment gets its own
        # explicit starting state, at the cost of one REST-sourced projection per
        # market per day.
        for label in sorted(self.sessions):
            session = self.sessions[label]
            await self._bank_initial_state(
                label, int(getattr(session, "session_epoch", 0) or 0), now,
                reason="segment_rotation",
            )

        def close_old() -> dict[str, Any]:
            if old_writer is not None:
                self.status.frames_written += old_writer.frames_written
                self.status.bytes_stored += old_writer.bytes_stored
                self.status.bytes_raw += old_writer.bytes_raw
                old_writer.stop(timeout=frozen.WRITER_DRAIN_TIMEOUT_SECONDS)
            old_seg.extra["shard_plan"] = self._shard_plan.summary()
            old_seg.extra["rotated_to"] = new_seg.name
            return seg_mod.finalize(
                old_seg, state=frozen.SEGMENT_COMPLETE,
                close_reason=frozen.CLOSE_ROTATION_UTC_MIDNIGHT,
                universe_identity=self.universe.identity(), now=now,
            )

        manifest = await asyncio.to_thread(close_old)
        self.status.segments_finalized.append(
            f"{manifest['segment_name']}:{manifest['segment_state']}"
        )
        log.info(
            "threadc.rotated", closed=old_seg.name, opened=new_seg.name,
            state=manifest["segment_state"], frames=manifest["raw_frame_count"],
        )
        return manifest

    # -- Low disk ---------------------------------------------------------------

    async def _disk_loop(self) -> None:
        # Checks FIRST, then sleeps. A breach that appears between the startup
        # check and the first interval would otherwise go a whole interval
        # unnoticed, and the point of a 48-hour reserve is that the halt is an
        # operational message to a human rather than a race against the disk.
        while not self._stop.is_set():
            status = self._disk_check(self.cfg.archive_dir, self.cfg.disk_reserve_bytes)
            if status.below_reserve:
                log.error(
                    "threadc.LOW_DISK_HALT", **status.summary(),
                    action="stopping writes, finalizing HALTED_LOW_DISK, exit 75",
                )
                self._health("low_disk_halt", json.dumps(status.summary()))
                self._terminal = TerminalStop(
                    reason=frozen.CLOSE_LOW_DISK,
                    segment_state=frozen.SEGMENT_HALTED_LOW_DISK,
                    exit_code=frozen.EXIT_TERMINAL_NO_RESTART,
                    detail=(
                        f"free {status.free_bytes} <= reserve {status.reserve_bytes}"
                    ),
                )
                self._stop.set()
                return
            await self._sleep(frozen.DISK_CHECK_INTERVAL_SECONDS)

    # -- Frozen collection end --------------------------------------------------

    async def _collection_end_loop(self) -> None:
        """
        Stop cleanly at the configured collection end.

        The recorder does not fetch, parse or monitor the MLB schedule. It reads
        one configured timestamp and compares it to the clock; moving that
        timestamp later is an operator configuration change, not a code change.
        The capacity run has no collection end of its own and is bounded by its
        own deadline instead.
        """
        if self.cfg.mode == frozen.MODE_CAPACITY:
            await self._stop.wait()
            return
        while not self._stop.is_set():
            remaining = self.cfg.collection_end_ts - self.now()
            if remaining <= 0:
                log.info(
                    "threadc.collection_end_reached",
                    collection_end_utc=self.cfg.collection_end_iso,
                    action="finalizing COMPLETE and exiting 75 (no restart)",
                )
                self._health(
                    "collection_end", f"configured end {self.cfg.collection_end_iso} reached"
                )
                self._terminal = TerminalStop(
                    reason=frozen.CLOSE_COLLECTION_END,
                    segment_state=frozen.SEGMENT_COMPLETE,
                    exit_code=frozen.EXIT_TERMINAL_NO_RESTART,
                    detail=f"configured collection end {self.cfg.collection_end_iso}",
                )
                self._stop.set()
                return
            await self._sleep(min(remaining, frozen.ROTATION_CHECK_SECONDS * 30))

    # -- Helpers ----------------------------------------------------------------

    def _health(self, kind: str, detail: str) -> None:
        seg = self.segment
        if seg is None or seg.store is None or seg.store.closed:
            return
        seg.store.record_health(
            stream_label="recorder", session_epoch=0, ts=self.now(),
            kind=kind, reason="", detail=detail,
        )

    def request_stop(self, reason: str = frozen.CLOSE_OPERATOR_SIGNAL) -> None:
        """Ask for a clean stop. Used by SIGTERM/SIGINT handling."""
        log.info("threadc.stop_requested", reason=reason)
        self._stop.set()

    # -- Capacity measurement report --------------------------------------------

    def capacity_report(self) -> dict[str, Any]:
        """
        The ONLY quantities the corrected capacity measurement may report.

        Every number here describes the bracketed measurement window, not the
        process: setup, the initial census and subscription establishment happen
        before `measurement_start` and are excluded by construction rather than
        subtracted afterwards.

        There is deliberately no market-level anything -- no per-market counts,
        no prices, no spreads, no depth, no outcomes. A capacity run measures the
        recorder, not the exchange. The message counts are by protocol TYPE
        across the whole subscription, which is an aggregate property of the
        feed and carries no market identity.
        """
        m = self.capacity
        if m is None or m.started_ts is None or m.ended_ts is None:
            return {
                "mode": self.cfg.mode,
                "run_id": self.run_id,
                "measurement_complete": False,
                "abort_reason": (
                    m.abort_reason if m else "no capacity measurement was requested"
                ),
            }

        counts = m.message_delta()
        cpu_user = (
            (m.cpu_end.user + m.cpu_end.children_user)
            - (m.cpu_start.user + m.cpu_start.children_user)
        )
        cpu_system = (
            (m.cpu_end.system + m.cpu_end.children_system)
            - (m.cpu_start.system + m.cpu_start.children_system)
        )
        growth = m.disk_start.free_bytes - m.disk_end.free_bytes
        archive_growth = (
            None
            if m.archive_bytes_start is None or m.archive_bytes_end is None
            else m.archive_bytes_end - m.archive_bytes_start
        )
        return {
            "mode": self.cfg.mode,
            "run_id": self.run_id,
            "measurement_complete": m.complete,
            "abort_reason": m.abort_reason,
            "measurement_start_ts": m.started_ts,
            "measurement_start_utc": tc_config.iso_utc(m.started_ts),
            "measurement_end_ts": m.ended_ts,
            "measurement_end_utc": tc_config.iso_utc(m.ended_ts),
            "measurement_window_seconds": round(m.elapsed or 0.0, 3),
            "setup_seconds_excluded": round(m.started_ts - self.status.started_ts, 3),

            # B_1h,disk. The difference between two filesystem readings taken at
            # the two marks -- not a payload counter, not an extrapolation.
            "measurement_disk_growth_bytes": growth,
            "disk_free_at_measurement_start_bytes": m.disk_start.free_bytes,
            "disk_free_at_measurement_end_bytes": m.disk_end.free_bytes,
            "archive_dir_growth_bytes": archive_growth,

            "ticker_message_count": counts.get(frozen.TICKER_CHANNEL, 0),
            "trade_message_count": counts.get(frozen.TRADE_CHANNEL, 0),
            "total_ws_message_count": sum(counts.values()),
            "ws_message_counts_by_type": counts,
            "raw_frames_written_in_window": m.frames_end - m.frames_start,

            "peak_simultaneous_subscribed_markets": self.status.peak_subscribed_markets,
            "subscription_limit_error_26_count": (
                self.status.subscription_limit_errors - m.subscription_limit_errors_start
            ),
            "reconnects": self.status.reconnects - m.reconnects_start,
            "errors": self.status.errors - m.errors_start,

            "cpu_user_seconds": round(cpu_user, 3),
            "cpu_system_seconds": round(cpu_system, 3),
            "max_rss_bytes_in_window": m.max_rss_window_bytes,
            "max_rss_bytes_process_lifetime": _max_rss_bytes(),

            "channels": list(self.cfg.channels),
            "rule_1_discovery_performed": False,
            "segments": self.status.segments_finalized,
            "notes": [
                "B_1h,disk is measurement_disk_growth_bytes over "
                "measurement_window_seconds. Storage planning uses exact bytes and "
                "the actual remaining timestamps; nothing here is extrapolated from "
                "startup time or from a logical payload-byte counter.",
                "Both filesystem readings are taken at an arbitrary point in the "
                "frame writer's checkpoint cycle, so each carries up to one "
                "checkpoint interval of not-yet-truncated WAL. The bias is bounded "
                "by that interval and does not accumulate over the window.",
                "This process reports the measured rate and nothing else. What "
                "storage decision follows from it is a human judgement made "
                "outside this process, on the predeclared hierarchy.",
            ],
        }


def _archive_bytes(archive_dir: Path) -> int | None:
    """
    Total bytes of every file under the archive directory.

    A cross-check on the filesystem reading, not a replacement for it: free space
    also moves for reasons that have nothing to do with this recorder, and this
    number says how much of the movement was the archive itself.
    """
    try:
        return sum(
            f.stat().st_size for f in Path(archive_dir).rglob("*") if f.is_file()
        )
    except OSError:
        return None


def _current_rss_bytes() -> int | None:
    """
    Resident set size RIGHT NOW, stdlib only. None where unavailable.

    Deliberately not ru_maxrss: that is a lifetime high-water mark and would
    report the startup peak as the cost of the measured hour.
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


_SUBSCRIPTION_LIMIT_CODES = (26, 25)

_TRUNCATED_CODE_RE = re.compile(
    '"code"' + r"[ ]*:[ ]*2[56](?![0-9])"
)
"""Fallback matcher for a server_error payload truncated mid-object."""
"""AsyncAPI 26 is "Subscription market limit exceeded"; 25 is the buffer
overflow depthrec.session treats alongside it. Kalshi publishes the codes but no
number for the limit itself, and this recorder does not go looking for one."""


def _is_subscription_limit(detail: str) -> bool:
    """
    Does a server_error health detail carry subscription-limit code 26 (or 25)?

    depthrec.session records the raw error frame text, truncated at 1000 chars,
    immediately before raising, so the ordinary case parses as JSON. The pattern
    fallback covers a payload long enough to have been cut mid-object: a
    truncated frame must not silently drop the count to zero.

    The pattern uses a negative lookahead rather than \b so that "code": 265
    cannot match, and so the source carries no backslash escape that a shell or
    an editor could mangle -- which is exactly how this check was silently
    broken once already.
    """
    if not detail:
        return False
    try:
        msg = (json.loads(detail).get("msg") or {})
        return msg.get("code") in _SUBSCRIPTION_LIMIT_CODES
    except (json.JSONDecodeError, AttributeError, TypeError):
        return bool(_TRUNCATED_CODE_RE.search(detail))


def _max_rss_bytes() -> int | None:
    """Peak RSS, stdlib only. None where the platform does not offer it."""
    try:
        import resource  # POSIX only
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes; macOS reports bytes.
    return int(usage) if sys.platform == "darwin" else int(usage) * 1024


__all__ = ["ThreadCRecorder", "RecorderStatus", "TerminalStop", "PROCESS_LOG_FILE"]
