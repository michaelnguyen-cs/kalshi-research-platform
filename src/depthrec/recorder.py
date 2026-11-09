"""
The recorder. Ties the independent census, the pinned classifier and the
canonical book together and writes the section 4 verdict for every
otherwise-firing signal.

Order of operations at a market's T-7 moment, and why it is that order:

  at  T-7        read_book() -- the book AS IT WAS at the target moment. This
                 cannot be deferred: the book is a live object and the T-7 state
                 is gone a second later.
  after T-7      fetch 1-minute candles through T-7 and run the pinned
                 classifier. This cannot be advanced: the candle ending at T-7
                 does not exist until T-7, and the pilot's price comes from that
                 candle.

The two are joined by ticker at report time. A market therefore gets a book
observation whether or not it turns out to fire, which is deliberate -- the
observation is evidence about the recorder, and evidence gathered before the
firing verdict is known cannot be selected on the verdict.

`signal_observation` is deliberately side-agnostic and carries no bucket. It
records the book and whether it was trustworthy, plus BOTH sides' executable ask
and displayed size. Section 2's executable price is the ask "on the side the
frozen rule buys", and the frozen rule buys NO whenever the YES price is below
0.50 -- which on the pilot's own data is most of the time. Buying NO lifts the
BID ladder at 1 - p, so a NO-side signal's depth lives on the opposite ladder
from a YES-side signal's.

ALL / VALID / NON-TRADEABLE / ELIGIBLE are therefore formed at report time, by
joining `classification` (which supplies the side) against `signal_observation`
(which supplies both candidates) through `bucket_for()`. Everything needed to
redo that join is persisted, including the raw candles the classifier saw and
the raw WebSocket frames the book was built from.

Once a market is independently established as firing it is IN, regardless of
whether the recorder discovered or subscribed to it. No trustworthy state at
T-7 means the applicable UNAVAILABLE_* reason -- usually
UNAVAILABLE_NO_SNAPSHOT -- and never a drop.
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

from depthrec import classify, frozen, integrity
from depthrec import pinned_hashes as ph
from depthrec.book import contracts_to_qty, price_dollars, qty_contracts
from depthrec.census import RestCensus
from depthrec.rawwriter import RawWriter
from depthrec.session import ReadResult, ResearchSession, SubscriptionRejected
from depthrec.store import RunContext, Store
from kbot.config import Settings, settings as _default_settings
from kbot.kalshi_client import KalshiClient

log = structlog.get_logger(__name__)

DOC_REFERENCE = "research/THREAD_A_EXECUTION_MECHANICS_2026-08-22.md @ e7c8c87"

TICK_SECONDS = 0.1
"""How often the T-7 scheduler wakes. A book observation is taken at the first
tick at or after target_ts, so this bounds the observation's lag at 100 ms."""

CLASSIFY_DELAY_SECONDS = 90
"""How long after T-7 the classifier runs. The 1-minute candle ending at T-7 is
published at T-7; this margin covers publication lag and one retry without ever
letting the classifier see a candle that ends after T-7 (the pinned
candle_at_or_before is strictly causal regardless)."""

SUBSCRIBE_LEAD_SECONDS = 1800
"""Subscribe to a market this far before its close. Comfortably ahead of T-7 so
a snapshot has time to arrive; markets are added as discovery finds them."""

UNSUBSCRIBE_LAG_SECONDS = 120
"""Drop a market this long after its close."""

SESSION_CLOSE_TIMEOUT_SECONDS = 10.0
"""How long teardown waits for the WebSocket session to close cleanly."""

SHUTDOWN_GRACE_SECONDS = 30.0
"""How long shutdown waits for worker threads before giving up on them and
writing the summary anyway. A thread inside a blocking Kalshi call cannot be
cancelled, and an unbounded wait means a run can end with no report at all."""


def bucket_for(
    *,
    validity: str | None,
    unavailable_reason: str | None,
    side: str,
    exec_price: float | None,
    exec_size_fp: int | None,
    x_contracts: int,
) -> tuple[str, str, str]:
    """
    Section 4's verdict for one otherwise-firing signal.

    Returns (bucket, reason, detail). The single place the three buckets are
    decided, shared by the live report and by scripts/depthrec_report.py so an
    offline reconstruction cannot disagree with the run that produced the data.

    `exec_price`/`exec_size_fp` are the executable ask and displayed size ON THE
    SIDE THE RULE BUYS, taken from the observation's per-side columns. A NO-side
    signal reads the bid ladder at 1 - p; reading the YES ask's depth for it
    would measure the wrong queue.

    UNAVAILABLE and NON-TRADEABLE are never merged: the first means the recorder
    could not establish trustworthy state, the second means it could and the
    order still would not have executed.
    """
    if validity is None:
        # Firing per REST plus the pinned classifier, with no observation at
        # all. Section 4: the applicable reason, never a drop.
        return (
            frozen.BUCKET_UNAVAILABLE,
            frozen.UNAVAILABLE_NO_SNAPSHOT,
            "no recorder state for this market at its T-7 moment",
        )
    if validity != frozen.VALIDITY_VALID:
        return (
            frozen.BUCKET_UNAVAILABLE,
            unavailable_reason or frozen.UNAVAILABLE_OTHER,
            "",
        )
    if not side:
        return (
            frozen.BUCKET_UNAVAILABLE,
            frozen.UNAVAILABLE_OTHER,
            "firing signal with no recorded side -- classification is incomplete",
        )
    if exec_price is None or exec_size_fp is None:
        return (
            frozen.BUCKET_NON_TRADEABLE, "",
            f"no executable ask on the {side} side, which is the side the rule buys",
        )
    if exec_size_fp < contracts_to_qty(x_contracts):
        return (
            frozen.BUCKET_NON_TRADEABLE, "",
            f"size_at_ask {qty_contracts(exec_size_fp)} on the {side} side "
            f"< X = {x_contracts}",
        )
    return frozen.BUCKET_ELIGIBLE, "", ""


@dataclass
class RecorderStatus:
    started_ts: float
    observations: int = 0
    classifications: int = 0
    firing: int = 0
    classifier_failures: int = 0
    candle_failures: int = 0
    buckets: dict[str, int] = field(default_factory=dict)


class DepthRecorder:
    """
    One recording run: one store, one WebSocket session, two REST censuses.

    The Thread A census is the denominator and is kept separate from the
    universe census on purpose -- the denominator must be derivable without
    reference to what the recorder managed to subscribe to.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        cfg: Settings | None = None,
        rule_threshold: float = frozen.RULE_THRESHOLD,
        slice_minutes: int = frozen.RULE_SLICE_MINUTES,
        x_contracts: int = frozen.X_CONTRACTS,
        run_id: str | None = None,
        subscribe_universe: bool = True,
        window_start_ts: float | None = None,
        window_end_ts: float | None = None,
        compress_raw: bool = True,
    ) -> None:
        self._cfg = cfg or _default_settings

        # Fail closed BEFORE anything is opened, connected to, or written.
        frozen.assert_frozen_config(
            rule_threshold=rule_threshold,
            slice_minutes=slice_minutes,
            x_contracts=x_contracts,
        )
        self.rule_threshold = rule_threshold
        self.slice_minutes = slice_minutes
        self.x_contracts = x_contracts

        self.integrity_report = integrity.verify_or_raise(
            expected_study=ph.EXPECTED_STUDY,
            expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST,
        )
        self.git_commit = integrity.recorder_git_commit()

        self.run_id = run_id or f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.store = Store(Path(db_path), RunContext(self.run_id, self.git_commit))

        self.client = KalshiClient(self._cfg)
        self.denominator_census = RestCensus(
            client=self.client, store=self.store, series=frozen.CENSUS_SERIES
        )
        universe_series = tuple(
            s for s in frozen.VALIDATION_UNIVERSE_SERIES if s not in frozen.CENSUS_SERIES
        )
        self.universe_census = RestCensus(
            client=self.client, store=self.store, series=universe_series
        )
        self.subscribe_universe = subscribe_universe

        self.session = ResearchSession(
            tickers=[],
            use_yes_price=frozen.USE_YES_PRICE,
            label="A",
            cfg=self._cfg,
            raw_sink=self._raw_sink,
            health_sink=self._health_sink,
            on_book_update=self._on_book_update,
        )

        # Compression and the SQLite insert both happen on this writer's thread,
        # never on the event loop. See depthrec.rawwriter.
        self.raw_writer = RawWriter(
            self.store, compress=compress_raw,
            health=lambda kind, detail: self._health_sink(kind, time.time(), "", detail),
        )

        self.status = RecorderStatus(started_ts=time.time())

        # The sample window, by TARGET time. A market whose T-7 moment happened
        # before this recorder existed is not an UNAVAILABLE observation -- it is
        # outside the sample entirely, and counting it would fill the
        # denominator with startup artifacts. The reconciliation pass looks back
        # six hours precisely so it can recover markets the live pass missed, and
        # most of what it finds is already past its target.
        #
        # For the section 9 pilot these are set explicitly to the three complete
        # UTC days, logged and committed, so the boundary cannot drift
        # retrospectively.
        self.window_start_ts = (
            self.status.started_ts if window_start_ts is None else window_start_ts
        )
        self.window_end_ts = window_end_ts
        self._out_of_window: set[str] = set()
        self._last_raw_id: int | None = None
        self._snapshot_raw_id: dict[str, int] = {}
        self._observed: set[str] = set()
        self._classified: set[str] = set()
        self._pending_classify: dict[str, float] = {}
        self._classify_tasks: set[asyncio.Task] = set()
        self._classifier_error: classify.ClassifierUnavailable | None = None
        # Filled in by run()'s teardown, while the store is still open.
        self.final_report: dict[str, Any] | None = None
        self._subscribed_epoch_logged: set[int] = set()
        self._stop = asyncio.Event()

    # -- Startup ---------------------------------------------------------------

    def log_startup(self) -> dict[str, Any]:
        """
        Section 6's startup record: subscription parameters, API host, recorder
        Git commit -- plus the three frozen values no hash can cover and the
        pinned-classifier integrity result.
        """
        record = {
            "run_id": self.run_id,
            "doc": DOC_REFERENCE,
            "git_commit": self.git_commit,
            "api_env": self._cfg.kalshi_env,
            "base_url": self._cfg.base_url,
            "ws_url": self._cfg.ws_url,
            "channels": list(frozen.RESEARCH_CHANNELS),
            "use_yes_price": frozen.USE_YES_PRICE,
            "rule_threshold": self.rule_threshold,
            "slice_minutes": self.slice_minutes,
            "x_contracts": self.x_contracts,
            "target_offset_seconds": frozen.TARGET_OFFSET_SECONDS,
            "session_activity_max_age_s": frozen.SESSION_ACTIVITY_MAX_AGE_SECONDS,
            "census_series": list(frozen.CENSUS_SERIES),
            "census_interval_s": frozen.CENSUS_DISCOVERY_INTERVAL_SECONDS,
            "universe_series": list(frozen.VALIDATION_UNIVERSE_SERIES),
            "window_start_ts": self.window_start_ts,
            "window_end_ts": self.window_end_ts,
            "classifier_pin": classify.classifier_pin(),
            "pinned_frozen_commit": ph.FROZEN_COMMIT,
            **self.integrity_report.log_fields(),
        }
        log.info("depthrec.startup", **record)
        self.store.record_run(
            started_ts=self.status.started_ts,
            api_env=self._cfg.kalshi_env,
            base_url=self._cfg.base_url,
            ws_url=self._cfg.ws_url,
            frozen_config={
                **frozen.frozen_config_summary(),
                "target_offset_seconds": frozen.TARGET_OFFSET_SECONDS,
                "use_yes_price": frozen.USE_YES_PRICE,
                "session_activity_max_age_s": frozen.SESSION_ACTIVITY_MAX_AGE_SECONDS,
                "census_series": list(frozen.CENSUS_SERIES),
                "census_interval_s": frozen.CENSUS_DISCOVERY_INTERVAL_SECONDS,
                "universe_series": list(frozen.VALIDATION_UNIVERSE_SERIES),
                "window_start_ts": self.window_start_ts,
                "window_end_ts": self.window_end_ts,
            },
            integrity={
                **self.integrity_report.log_fields(),
                "pinned_frozen_commit": ph.FROZEN_COMMIT,
                "expected_longshot_digest": ph.EXPECTED_LONGSHOT_DIGEST,
                "classifier_pin": classify.classifier_pin(),
            },
            doc_reference=DOC_REFERENCE,
        )
        return record

    # -- Sinks -----------------------------------------------------------------

    def _raw_sink(self, direction: str, recv_ts: float, raw: str) -> None:
        # Runs ON the event loop, so it does the minimum possible: hand the
        # frame to the writer thread and take back the id it will be stored
        # under. No hashing, no compression, no SQLite.
        self._last_raw_id = self.raw_writer.submit(
            stream_label=self.session.label,
            session_epoch=self.session.session_epoch,
            direction=direction, recv_ts=recv_ts, raw=raw,
        )

    def _health_sink(self, kind: str, ts: float, reason: str, detail: str) -> None:
        self.store.record_health(
            stream_label=self.session.label,
            session_epoch=self.session.session_epoch,
            ts=ts, kind=kind, reason=reason, detail=detail,
        )
        if kind == "subscribed":
            # Written on the ack rather than on connect: only then are the
            # subscription id AND the parameters the server actually accepted
            # both known. Section 6 requires the subscription parameters on the
            # record; section 7 requires the subscription identifier.
            self.store.record_session(
                session_id=f"{self.run_id}:{self.session.label}:{self.session.session_epoch}",
                stream_label=self.session.label,
                session_epoch=self.session.session_epoch,
                use_yes_price=self.session.use_yes_price,
                channels=frozen.RESEARCH_CHANNELS,
                subscribe_params=self.session.subscribe_params or {},
                ws_url=self._cfg.ws_url,
                opened_ts=self.session.connected_since or ts,
            )

    def _on_book_update(self, ticker: str, recv_ts: float) -> None:
        state = self.session._books.get(ticker)
        if state is not None and state.book.updates_applied == 0:
            # The frame just recorded was this market's snapshot: remember where
            # in ws_raw an independent decoder should start replaying.
            self._snapshot_raw_id[ticker] = self._last_raw_id or 0

    # -- Main loop -------------------------------------------------------------

    async def run(self, *, duration_seconds: float | None = None) -> RecorderStatus:
        self.log_startup()
        self.raw_writer.start()

        # Seed both censuses before connecting, so the first subscribe covers
        # everything already open rather than trickling in over a minute.
        await asyncio.to_thread(self.denominator_census.run_live_pass)
        await asyncio.to_thread(self.denominator_census.run_reconcile_pass)
        if self.subscribe_universe:
            await asyncio.to_thread(self.universe_census.run_live_pass)

        await self._sync_subscription()

        tasks = [
            asyncio.create_task(self.session.run(), name="ws"),
            asyncio.create_task(self._census_loop(), name="census"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
            asyncio.create_task(self._tick_loop(), name="tick"),
        ]
        if duration_seconds is not None:
            tasks.append(asyncio.create_task(self._deadline(duration_seconds), name="deadline"))

        try:
            # FIRST_COMPLETED, not FIRST_EXCEPTION.
            #
            # FIRST_EXCEPTION returns early only when a task RAISES. When tasks
            # merely finish normally it waits for ALL of them -- and
            # session.run() never finishes on its own: its reconnect loop exits
            # only once session.stop() has been called, and that call lives in
            # the teardown below. So the deadline task would set _stop, return
            # normally, and then this await would block forever with the socket
            # still happily reconnecting. That is exactly how a 26-minute smoke
            # run reached its deadline, kept running, reconnected 81 seconds
            # later, and had to be killed without ever writing its summary.
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    if isinstance(exc, SubscriptionRejected):
                        log.error(
                            "depthrec.subscription_rejected",
                            code=exc.code,
                            is_market_limit=exc.is_market_limit,
                            payload=exc.payload,
                            markets_attempted=len(self.session.tickers),
                        )
                    raise exc
        finally:
            self._stop.set()
            # Stop the session FIRST. It is the one task that will not end on
            # _stop alone, and closing it cleanly is better than cancelling it
            # mid-`async with connect(...)`. Bounded, because a close that hangs
            # must not cost the run its report.
            try:
                await asyncio.wait_for(
                    self.session.stop(), timeout=SESSION_CLOSE_TIMEOUT_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - includes TimeoutError
                log.warning("depthrec.session_close_failed", error=str(exc))

            for task in tasks + list(self._classify_tasks):
                task.cancel()
            # Bounded too. asyncio.to_thread cannot be interrupted, so a worker
            # already inside a blocking Kalshi call finishes on its own clock.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, *self._classify_tasks, return_exceptions=True),
                    timeout=SHUTDOWN_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                log.warning("depthrec.shutdown_timeout", seconds=SHUTDOWN_GRACE_SECONDS)
            # Drain the frame queue and force a final checkpoint before any
            # reporting: the archive must be complete on disk before the run
            # claims to have finished.
            self.raw_writer.stop()

            # Compute the report BEFORE closing the store, and keep it: the
            # caller wants to print it, and by then the connection is gone.
            self.final_report = self.report()
            self.status.buckets = dict(self.final_report["buckets"])
            self.store.close_run(time.time())
            self.store.close()
            self.client.close()
        return self.status

    async def _deadline(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            log.info("depthrec.deadline_reached", seconds=seconds)
            self._stop.set()

    async def _census_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(frozen.CENSUS_DISCOVERY_INTERVAL_SECONDS)
            if self._stop.is_set():
                return
            try:
                # In a thread, ALWAYS. The Kalshi SDK's read token bucket does a
                # plain time.sleep() and its urllib3 retries can block for
                # minutes on a dropped keep-alive; running that on the event
                # loop would stall the WebSocket consumer and push a T-7 reading
                # past the moment it is supposed to capture.
                await asyncio.to_thread(self.denominator_census.run_live_pass)
                if self.subscribe_universe:
                    await asyncio.to_thread(self.universe_census.run_live_pass)
                await self._sync_subscription()
            except Exception as exc:  # noqa: BLE001
                # A failed discovery cycle is a health event, never a shrunk
                # universe: the census has no delete path.
                log.error("depthrec.census.loop_error", error=str(exc))
                self._health_sink("census_error", time.time(), "", str(exc))

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(frozen.CENSUS_RECONCILE_INTERVAL_SECONDS)
            if self._stop.is_set():
                return
            try:
                await asyncio.to_thread(self.denominator_census.run_reconcile_pass)
                await self._sync_subscription()
            except Exception as exc:  # noqa: BLE001
                log.error("depthrec.census.reconcile_error", error=str(exc))
                self._health_sink("reconcile_error", time.time(), "", str(exc))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _sync_subscription(self) -> None:
        """Bring the WS subscription set in line with what REST says is open."""
        now = time.time()
        want: set[str] = set()
        censuses = [self.denominator_census]
        if self.subscribe_universe:
            censuses.append(self.universe_census)
        for census in censuses:
            for entry in census.entries.values():
                if entry.close_ts is None:
                    continue
                if now - UNSUBSCRIBE_LAG_SECONDS <= entry.close_ts <= now + SUBSCRIBE_LEAD_SECONDS:
                    want.add(entry.ticker)
        have = set(self.session.tickers)
        add = sorted(want - have)
        remove = sorted(have - want)
        if add or remove:
            await self.session.update_markets(add, remove)

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            if self._classifier_error is not None:
                # Raised from a worker thread; re-raised here so run() unwinds
                # rather than the recorder carrying on with a classifier it
                # cannot apply.
                raise self._classifier_error
            now = time.time()
            try:
                self._observe_due(now)
                self._classify_due(now)
            except Exception as exc:  # noqa: BLE001
                log.error("depthrec.tick_error", error=str(exc))
                self._health_sink("tick_error", time.time(), "", str(exc))
            await asyncio.sleep(TICK_SECONDS)

    # -- T-7 observation --------------------------------------------------------

    def in_window(self, target_ts: int) -> bool:
        """Is this market's T-7 moment inside the sample window?"""
        if target_ts < self.window_start_ts:
            return False
        return self.window_end_ts is None or target_ts < self.window_end_ts

    def _observe_due(self, now: float) -> None:
        for entry in self.denominator_census.all_with_target():
            target = entry.target_ts
            if target is None or entry.ticker in self._observed:
                continue
            if not self.in_window(target):
                # Recorded once as out of window, then ignored. It stays in the
                # raw census -- section 4 keeps everything there -- but it never
                # enters ALL, because its T-7 moment was not in this sample.
                if entry.ticker not in self._out_of_window:
                    self._out_of_window.add(entry.ticker)
                    self.store.record_census_change(
                        ts=now, ticker=entry.ticker, series=entry.series,
                        change="out_of_window",
                        detail=f"target_ts={target} outside "
                               f"[{self.window_start_ts}, {self.window_end_ts})",
                    )
                continue
            if now < target:
                continue
            self._observe(entry.ticker, entry.series, entry.close_ts, target, now)
            self._observed.add(entry.ticker)
            self._pending_classify[entry.ticker] = target + CLASSIFY_DELAY_SECONDS

    def _observe(
        self, ticker: str, series: str, close_ts: int | None, target_ts: int, now: float
    ) -> None:
        subscribed = ticker in set(self.session.tickers)
        read: ReadResult = self.session.read_book(ticker, at_ts=now)
        top = read.top

        # BOTH sides' executable ask, because the side the frozen rule buys is
        # not known yet -- it comes from the classifier, which cannot run until
        # the T-7 candle exists. Section 4's bucket is derived from whichever of
        # these two the verdict turns out to need.
        yes_price = yes_size = no_price = no_size = None
        if top is not None:
            yes_price, yes_size = top.executable_ask("YES")
            no_price, no_size = top.executable_ask("NO")

        self.store.record_observation({
            "ticker": ticker,
            "series": series,
            "close_ts": close_ts or 0,
            "target_ts": target_ts,
            "observed_ts": now,
            "validity": read.validity,
            "unavailable_reason": read.reason or "",
            "detail": read.detail,
            "subscribed": int(subscribed),
            "x_contracts": self.x_contracts,
            "best_bid": price_dollars(top.bid_price) if top and top.bid_price else None,
            "bid_size": qty_contracts(top.bid_qty) if top and top.bid_qty else None,
            "best_ask": price_dollars(top.ask_price) if top and top.ask_price else None,
            "ask_size": qty_contracts(top.ask_qty) if top and top.ask_qty else None,
            "yes_ask_price": price_dollars(yes_price) if yes_price else None,
            "yes_ask_size_fp": yes_size,
            "no_ask_price": price_dollars(no_price) if no_price else None,
            "no_ask_size_fp": no_size,
            "stream_label": self.session.label,
            "session_epoch": read.session_epoch,
            "sid": str(read.sid or ""),
            "snapshot_seq": read.snapshot_seq,
            "last_seq": read.last_seq,
            "snapshot_recv_ts": read.snapshot_recv_ts,
            "last_update_recv_ts": read.last_update_recv_ts,
            "last_update_exchange_ts_ms": read.last_update_exchange_ts_ms,
            "session_activity_ts": read.session_activity_ts,
            "session_activity_age": read.session_activity_age,
            "last_ping_ts": read.last_ping_ts,
            "book_json": json.dumps({"bids": read.bids, "asks": read.asks}),
            "raw_first_id": self._snapshot_raw_id.get(ticker),
            "raw_last_id": self._last_raw_id,
        })
        self.status.observations += 1
        log.info(
            "depthrec.observed",
            ticker=ticker, target_ts=target_ts, validity=read.validity,
            reason=read.reason or "", subscribed=subscribed,
            yes_ask=price_dollars(yes_price) if yes_price else None,
            yes_size=qty_contracts(yes_size) if yes_size else None,
            no_ask=price_dollars(no_price) if no_price else None,
            no_size=qty_contracts(no_size) if no_size else None,
        )

    # -- Firing classification ---------------------------------------------------

    def _classify_due(self, now: float) -> None:
        """
        Dispatch due classifications to worker threads.

        Never inline: classification fetches candles over HTTP through the same
        blocking, sleeping client the census uses. Running it on the event loop
        would let a slow candle fetch delay the next market's T-7 reading, and
        the T-7 reading is the one thing here that cannot be retried.
        """
        for ticker, due in list(self._pending_classify.items()):
            if now < due:
                continue
            del self._pending_classify[ticker]
            entry = self.denominator_census.entries.get(ticker)
            if entry is None or entry.close_ts is None:
                continue
            task = asyncio.create_task(
                asyncio.to_thread(
                    self._classify, entry.ticker, entry.series, entry.close_ts, now
                ),
                name=f"classify:{ticker}",
            )
            self._classify_tasks.add(task)
            task.add_done_callback(self._classify_done)

    def _classify_done(self, task: asyncio.Task) -> None:
        self._classify_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, classify.ClassifierUnavailable):
            # The pinned classifier could not be applied literally. That halts
            # the pipeline; carry it back to the tick loop so run() unwinds.
            self._classifier_error = exc
        elif exc is not None:
            log.error("depthrec.classify.task_failed", error=str(exc))
            self._health_sink("classify_task_failed", time.time(), "", str(exc))

    def _classify(self, ticker: str, series: str, close_ts: int, now: float) -> None:
        target_ts = classify.target_ts_for(close_ts)
        try:
            candles = self.client.get_candlesticks(
                series, ticker,
                start_ts=close_ts - frozen.CENSUS_CANDLE_LEAD_SECONDS,
                end_ts=target_ts,
                period_interval=1,
            )
        except Exception as exc:  # noqa: BLE001
            # A candle fetch failure is NOT a non-firing verdict. The market
            # stays unclassified and is retried on the next pass; recording it
            # as "did not fire" would quietly shrink the denominator.
            self.status.candle_failures += 1
            self._pending_classify[ticker] = now + 60
            log.error("depthrec.classify.candles_failed", ticker=ticker, error=str(exc))
            self._health_sink("candles_failed", now, "", f"{ticker}: {exc}")
            return

        try:
            result = classify.classify_market(
                series=series, ticker=ticker, close_ts=close_ts, candles=candles,
                threshold=self.rule_threshold, slice_minutes=self.slice_minutes,
            )
        except classify.ClassifierUnavailable as exc:
            # The pinned classifier could not be applied literally. That halts
            # the pipeline -- it is not a depth or EV question, and there is no
            # alternative to fall back to.
            self.status.classifier_failures += 1
            log.error("depthrec.classify.unavailable", ticker=ticker, error=str(exc))
            self._health_sink("classifier_unavailable", now, "", f"{ticker}: {exc}")
            raise

        self.store.record_classification({
            "ticker": ticker,
            "series": series,
            "close_ts": close_ts,
            "target_ts": target_ts,
            "classified_ts": now,
            "is_firing": int(result.is_firing),
            "not_firing_reason": result.not_firing_reason,
            "yes_price": result.yes_price,
            "price_source": result.price_source,
            "side": result.side,
            "entry_price": result.entry_price,
            "candle_end_ts": result.candle_end_ts,
            "candles_json": json.dumps(result.candles),
            "candles_sha256": result.candles_sha256,
            "classifier_pin": json.dumps(classify.classifier_pin(), sort_keys=True),
        })
        self._classified.add(ticker)
        self.status.classifications += 1
        if result.is_firing:
            self.status.firing += 1
        log.info(
            "depthrec.classified",
            ticker=ticker, is_firing=result.is_firing, yes_price=result.yes_price,
            price_source=result.price_source, side=result.side,
            reason=result.not_firing_reason,
        )

    # -- Reporting ---------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """
        Section 5's quantities, over the join of classification and observation.

        Only the four inspectable quantities: raw availability, technical
        availability, D and Y, plus the itemised UNAVAILABLE reasons and
        recorder health. Nothing about settlements, EV, winners or losers.
        """
        rows = self.store.query(
            "SELECT c.ticker, c.series, c.side, o.validity, o.unavailable_reason,"
            "       o.subscribed, o.yes_ask_price, o.yes_ask_size_fp,"
            "       o.no_ask_price, o.no_ask_size_fp"
            "  FROM classification c"
            "  LEFT JOIN signal_observation o"
            "    ON o.run_id = c.run_id AND o.ticker = c.ticker"
            " WHERE c.run_id = ? AND c.is_firing = 1",
            (self.run_id,),
        )

        buckets: dict[str, int] = {b: 0 for b in frozen.BUCKETS}
        reasons: dict[str, int] = {r: 0 for r in frozen.UNAVAILABLE_REASONS}
        by_side: dict[str, int] = {}
        for row in rows:
            side = row["side"] or ""
            by_side[side or "(none)"] = by_side.get(side or "(none)", 0) + 1
            price = row["no_ask_price"] if side == "NO" else row["yes_ask_price"]
            size = row["no_ask_size_fp"] if side == "NO" else row["yes_ask_size_fp"]
            bucket, reason, _ = bucket_for(
                validity=row["validity"], unavailable_reason=row["unavailable_reason"],
                side=side, exec_price=price, exec_size_fp=size,
                x_contracts=self.x_contracts,
            )
            buckets[bucket] = buckets.get(bucket, 0) + 1
            if bucket == frozen.BUCKET_UNAVAILABLE:
                reasons[reason] = reasons.get(reason, 0) + 1

        all_signals = len(rows)
        valid = buckets[frozen.BUCKET_NON_TRADEABLE] + buckets[frozen.BUCKET_ELIGIBLE]
        eligible = buckets[frozen.BUCKET_ELIGIBLE]
        maintenance = reasons.get(frozen.UNAVAILABLE_SCHEDULED_MAINTENANCE, 0)

        return {
            "run_id": self.run_id,
            "git_commit": self.git_commit,
            "all_otherwise_firing": all_signals,
            "buckets": buckets,
            "unavailable_by_reason": reasons,
            "raw_availability": (valid / all_signals) if all_signals else None,
            "technical_availability": (
                valid / (all_signals - maintenance)
                if all_signals - maintenance > 0 else None
            ),
            "depth_clearance_D": (eligible / valid) if valid else None,
            "effective_executable_yield_Y": (eligible / all_signals) if all_signals else None,
            "firing_by_side": by_side,
            "census": {
                "denominator": self.denominator_census.summary(),
                "universe": self.universe_census.summary(),
            },
            "raw_archive": self.raw_writer.stats(),
            "wal": self.store.wal_settings(),
            "health": {
                "observations": self.status.observations,
                "classifications": self.status.classifications,
                "classifier_failures": self.status.classifier_failures,
                "candle_failures": self.status.candle_failures,
                "subscribed_markets": len(self.session.tickers),
            },
        }
