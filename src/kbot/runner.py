"""
Multi-strategy paper/live trading runner (Phase B2).

Replaces the single-strategy PaperTrader loop with one that can drive many
gated strategies through a single shared engine: one WS connection (union
of every loaded strategy's series_tickers), one set of feed singletons
(shared by every strategy that declares needing one via required_feeds),
one shared Executor/PortfolioRisk/PositionTracker/GateRegistry/Telemetry —
but each strategy is sized against its own bankroll allocation, gated
independently (its own gate level/pause state), and isolated from the
others' failures.

kbot.paper.PaperTrader is now a thin single-strategy wrapper over this
(constructs a Runner with one strategy at allocation 1.0) so existing
single-strategy behavior is unchanged -- see its module docstring.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import structlog

from kbot.analytics.adverse_selection import restore_flags_from_history
from kbot.config import Settings, settings as _default_settings
from kbot.execution import Executor
from kbot.feeds.btc import BTCFeed
from kbot.feeds.eth import ETHFeed
from kbot.gates import LEVELS, GateRegistry, check_kill_fast
from kbot.kalshi_client import KalshiClient
from kbot.models.types import OrderBook, PriceLevel
from kbot.position_tracker import PositionTracker
from kbot.quantity import units_from_fixed_point
from kbot.risk_portfolio import PortfolioRisk
from kbot.scanners.consistency import (
    monotonicity_violation_identity,
    monotonicity_violation_to_row,
    same_market_arb_identity,
    same_market_arb_to_row,
    scan_monotonicity,
    scan_same_market_arb,
)
from kbot.scanners.post_resolution import PostResolutionScanner
from kbot.sheets import SheetsClient
from kbot.startup_guard import assert_startup_safe
from kbot.strategy.base import Strategy
from kbot.telemetry import Telemetry
from kbot.ws_client import WSClient

log = structlog.get_logger(__name__)

# Log evaluations where net_edge is within this many dollars of the signal threshold.
_EVAL_LOG_THRESHOLD: float = 0.10

# Write heartbeat file + DB event every this many strategy-loop cycles (~5 min at 5s/cycle).
_HEARTBEAT_CYCLES: int = 60

# The heartbeat path is NOT a module constant: it comes from
# Settings.heartbeat_path (env HEARTBEAT_PATH, default kbot_heartbeat.json) via
# Runner._heartbeat_path, which both the writer and the dead-man's-switch
# reader resolve through. It was a hardcoded relative Path here, which made
# two services on one host share a heartbeat file whenever they shared a
# working directory -- each keeping the other's dead-man's switch satisfied.

# Internal dead-man's switch: if the heartbeat FILE goes this stale while the
# process is still alive, the async loop has frozen without crashing and the
# external watchdog (scripts/watchdog.ps1, which only restarts on process exit)
# will never recover it -- observed as 7+ hour silent outages. The switch checks
# the file's mtime rather than an in-memory timestamp so a freeze inside the
# heartbeat write itself is caught too, and calls os._exit to bypass asyncio
# teardown (a frozen loop cannot be relied on to unwind cleanly).
_DEADMAN_CHECK_INTERVAL: float = 60.0
_DEADMAN_MAX_HEARTBEAT_AGE: float = 900.0

# Exception isolation: a strategy that raises this many times within this
# window gets advisory-paused (evaluate() no longer called for it) so one
# broken strategy can't take down the others; the gate registry's own
# pause state is untouched -- this is a separate, in-process circuit
# breaker, not a gate demotion.
_FAILURE_WINDOW_SECONDS: float = 300.0
_FAILURE_THRESHOLD: int = 3

# Only these gate levels may ever route a real (non-dry-run) order. This
# check is layered ON TOP of execution.py's own dry_run/live_confirmed
# gauntlet (kept there, unbypassed) -- it exists so a runner started with
# multiple strategies of different maturity can never let an unvetted one
# place a live order just because the process overall is in live mode.
_LIVE_APPROVED_LEVELS = ("live_small", "live_full")


def _build_feed(name: str, cfg: Settings, on_price) -> Any:
    """
    Construct the shared singleton for a required_feeds name.

    Retention is sized for the LARGEST vol_window_seconds any consumer might
    request (see BTCFeed's own docstring) -- these feeds are shared across
    every strategy/ticker needing them, and different tickers can need
    different windows (KXBTC15M's 900s vs KXBTC's 3600s -- see
    Strategy.vol_window_seconds); each caller filters down to its own shorter
    window via history_within(), the feed itself just needs to retain enough
    raw history for whichever is largest.
    """
    largest_window = max(cfg.vol_window_seconds_hourly, cfg.vol_window_seconds_15m)
    if name == "btc_spot":
        return BTCFeed(
            poll_interval=cfg.btc_poll_interval_seconds,
            vol_window_seconds=largest_window,
            on_price=on_price,
        )
    if name == "eth_spot":
        return ETHFeed(
            poll_interval=cfg.eth_poll_interval_seconds,
            vol_window_seconds=largest_window,
            on_price=on_price,
        )
    raise ValueError(f"Unknown required feed {name!r} -- add it to kbot.runner._build_feed")


def _assert_isolated_domains(strategies: dict[str, Strategy]) -> None:
    """
    Refuse to load two strategies that each own a CALIBRATOR DOMAIN into one
    process.

    A calibrator domain is a series that carries its own fitted isotonic
    calibrator (Strategy.series_15m on the lognormal strategies). The
    constraint exists because kbot.learning.retrain.run_champion_challenger
    fits ONE global champion over load_outcomes(telemetry) -- every outcome in
    the events DB, unfiltered by strategy -- and kbot.learning.registry
    .ModelRegistry.get_champion() returns ONE global champion row. Two such
    strategies sharing a process therefore share an events DB and a registry,
    and the next retrain would fit BTC's champion calibrator on BTC+ETH trades
    mixed, corrupting the calibrator that gates BTC's live sizing.

    The fix is deployment-level (a separate service with its own
    EVENTS_DB_PATH/GATES_DB_PATH/REGISTRY_DB_PATH -- see Settings), and this
    check is what makes that non-optional rather than a documented convention
    someone can forget. Strategies with no calibrator domain (e.g. base_rate)
    are unaffected and can still be co-loaded with any one of them.
    """
    domains: dict[str, str] = {}
    for name, strategy in strategies.items():
        domain = getattr(strategy, "series_15m", None)
        if domain is not None:
            domains[name] = domain

    if len(set(domains.values())) > 1:
        pairs = ", ".join(f"{n} ({d})" for n, d in sorted(domains.items()))
        raise ValueError(
            f"Refusing to run calibrated strategies from different series in one "
            f"process: {pairs}. They would share one events DB and one model "
            f"registry, and the next `kbot learn --retrain` would fit a single "
            f"champion calibrator across both series' trades -- silently "
            f"corrupting each. Run them as separate services with their own "
            f"EVENTS_DB_PATH, GATES_DB_PATH and REGISTRY_DB_PATH."
        )


class Runner:
    """
    Drives N strategies through one shared feed/execution engine.

    Parameters
    ----------
    strategies:
        name -> instantiated Strategy. Must have exactly the same key set
        as `allocations`.
    allocations:
        name -> fraction of bankroll that strategy's Kelly sizing is
        computed against (see kbot.config.parse_strategy_allocations).
        Portfolio-level driver caps (risk_portfolio.py) still apply on top,
        against the FULL bankroll -- allocations only scale the per-
        strategy sizing input, not the shared risk caps.
    cfg:
        Settings (defaults to the module singleton).
    tickers:
        Explicit ticker list (bypasses auto-discovery). If None, the
        runner discovers the nearest open event's markets for every
        series any loaded strategy declares via series_tickers and
        subscribes to the union.
    live_confirmed:
        Must be True for live (non-dry-run) trading -- mirrors
        Executor's own flag. Defaults to False, matching PaperTrader's
        "always dry-run" guarantee: unless the caller explicitly opts in
        (the CLI's --live confirmation prompt sets this), cfg.dry_run
        must be True or construction raises.
    """

    def __init__(
        self,
        strategies: dict[str, Strategy],
        allocations: dict[str, float],
        cfg: Settings | None = None,
        *,
        tickers: list[str] | None = None,
        live_confirmed: bool = False,
    ) -> None:
        self._cfg = cfg or _default_settings

        # FIRST statement after cfg resolution, on purpose. Runner.__init__ is
        # the one place every order-capable path funnels through (PaperTrader
        # wraps it; kbot.cli's --strategies and --live paths build it directly),
        # and the order-submitting objects -- KalshiClient below, and the
        # Executor it is handed to -- are both constructed further down this
        # same method. Everything between here and them (the allocations
        # cross-check, the dry_run/live_confirmed gauntlet,
        # _assert_isolated_domains) runs AFTER the guard so that no ambiguous
        # DRY_RUN and no unresolvable series can reach a live client, whatever
        # order those later checks are edited into. See kbot.startup_guard.
        assert_startup_safe(strategies, self._cfg, tickers=tickers)

        missing = set(allocations) - set(strategies)
        if missing:
            raise ValueError(f"allocations name(s) {sorted(missing)} have no matching loaded strategy")
        extra = set(strategies) - set(allocations)
        if extra:
            raise ValueError(f"loaded strategy name(s) {sorted(extra)} have no allocations entry")

        if not self._cfg.dry_run and not live_confirmed:
            raise RuntimeError(
                "Runner requires DRY_RUN=true unless live_confirmed=True is explicitly passed "
                "(set by the CLI only after the --live confirmation prompt)."
            )

        _assert_isolated_domains(strategies)

        self._strategies = strategies
        self._allocations = allocations

        self._client = KalshiClient(self._cfg)
        # Explicit real path (not PositionTracker's ephemeral ":memory:"
        # default) so open positions survive a process restart -- same
        # kbot_*.db-in-CWD convention as Telemetry/GateRegistry below.
        # expiry_provider lets the tracker drop already-settled positions on
        # load, so a restart never inherits a stale (ticker, side) guard for a
        # window that has already closed (_ticker_expiry resolves the expiry
        # via the loaded strategies -- set above at self._strategies).
        self._tracker = PositionTracker(
            db_path=self._cfg.positions_db_path, expiry_provider=self._ticker_expiry
        )
        self._telemetry = Telemetry(self._cfg)

        # Settled tickers whose outcome could not be recorded yet: retry count
        # per ticker, and the set _rediscover_once must re-sweep. See
        # expire_market -- a position is never cleared before its outcome is
        # recorded, so these hold the trades still owed an outcome.
        self._unresolved_expiries: dict[str, int] = {}
        self._retry_expiries: set[str] = set()

        # BOOK VALIDITY INVARIANT: membership in this dict IS validity.
        # A ticker is present only while we hold a book built from a snapshot
        # received on the CURRENT WS session and kept current by an unbroken
        # run of deltas. Anything that breaks that chain -- disconnect,
        # reconnect, server/subscription error, sequence gap, a delta that
        # cannot be applied -- removes the entry immediately.
        #
        # Validity is enforced structurally rather than by a flag every reader
        # has to remember to check: every consumer (evaluation loops, the
        # executor's book_provider, the consistency scanners, the
        # post-resolution scanner) reads this dict, so an absent book cannot be
        # evaluated or traded by construction. MUTATE it, never rebind it --
        # PostResolutionScanner and book_provider hold references to this exact
        # object.
        self._books: dict[str, OrderBook] = {}
        self._book_invalidations = 0
        self._last_invalidation: str | None = None

        self._portfolio_risk = PortfolioRisk(self._cfg)
        # Rebuild driver exposure from the (already stale-swept) positions DB:
        # PortfolioRisk is in-memory, so without this a restart would show $0
        # exposure while the positions DB still holds open entries -- the cap
        # would then let the book over-fill past its true committed exposure.
        self._reconstruct_exposure_from_positions()
        # Positions the startup sweep just deleted for being past expiry may
        # never have had their outcome recorded -- a crash or redeploy between
        # a fill and its window closing lands exactly here. Record them now,
        # while the snapshot taken before the DELETE is still in hand.
        self._record_swept_on_load_outcomes()
        # Same class of problem: the adverse-selection flag cache is in-memory
        # too, so without this a restart resets every strategy to "not flagged"
        # and drops the min_net_edge multiplier until the next fill's markout
        # lands, however bad the persisted markout history is.
        restore_flags_from_history(self._telemetry, sorted(self._strategies), self._cfg)
        self._gates = GateRegistry()
        # Scoped to this process's strategy when it runs exactly one, so its
        # Calibration/Performance/Live P&L tabs are its own rather than shared
        # with the other deployment (see kbot.sheets.tab_for). Taken from the
        # loaded strategies rather than config -- they're what this process
        # actually trades.
        self._sheets = SheetsClient(
            self._cfg,
            strategy=next(iter(self._strategies)) if len(self._strategies) == 1 else None,
        )

        self._executor = Executor(
            self._client,
            self._cfg,
            live_confirmed=live_confirmed,
            tracker=self._tracker,
            telemetry=self._telemetry,
            book_provider=self._books.get,
            portfolio_risk=self._portfolio_risk,
            bankroll_provider=lambda: self._bankroll,
        )

        # Feed singletons: one per distinct required_feeds name, shared by
        # every strategy that declares needing it (so two strategies both
        # wanting "btc_spot" only poll Coinbase once).
        # Each feed's callback is bound to its OWN name, so a tick is always
        # measured against the feed it came from and routed only to the
        # strategies that consume that feed (the default-arg capture is
        # deliberate -- a bare closure over `feed_name` would leave every feed
        # calling back with the last name in the loop).
        self._feeds: dict[str, Any] = {}
        for strategy in self._strategies.values():
            for feed_name in strategy.required_feeds:
                if feed_name not in self._feeds:
                    self._feeds[feed_name] = _build_feed(
                        feed_name,
                        self._cfg,
                        lambda p, t, n=feed_name: self._on_spot_price(n, p, t),
                    )

        self._explicit_tickers = tickers
        self._bankroll: float = 0.0

        # Live WS client + the subscription set it's tracking. Both are set in
        # run() once the connection is built; declared here so introspection
        # (and the re-discovery task) have a stable attribute to read. The set
        # is the runner's own mirror of ws_client.active_tickers, kept in sync
        # by _rediscover_once as windows open and expire.
        self._ws_client: WSClient | None = None
        self._active_tickers: set[str] = set()

        # Per-strategy session counters (heartbeat content).
        self._eval_cycles: dict[str, int] = {name: 0 for name in self._strategies}
        self._session_signals: dict[str, int] = {name: 0 for name in self._strategies}
        self._session_evals_logged: dict[str, int] = {name: 0 for name in self._strategies}
        self._session_spot_triggers: int = 0
        self._global_cycle_count: int = 0  # heartbeat write cadence, not per-strategy

        # Exception isolation.
        self._failure_timestamps: dict[str, list[float]] = {name: [] for name in self._strategies}
        self._advisory_paused: set[str] = set()

        # Arb-log dedupe: detection key -> the fingerprint last written for it.
        # Rebuilt from the live detections on every scan, so it holds one entry
        # per detection CURRENTLY standing and cannot grow without bound.
        self._arb_seen: dict[tuple, str] = {}

        # Post-resolution repricing scanner (MARKET_RESEARCH.md item 3):
        # shares books/executor/telemetry with the main loop, runs its own
        # faster background cycle -- see kbot.scanners.post_resolution.
        self._post_resolution = PostResolutionScanner(
            self._books,
            self._strategies,
            self._executor,
            self._telemetry,
            bankroll_provider=lambda name: self._bankroll * self._allocations[name],
            cfg=self._cfg,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def _heartbeat_path(self) -> Path:
        """
        This runner's heartbeat file (Settings.heartbeat_path, env
        HEARTBEAT_PATH; default kbot_heartbeat.json).

        The SINGLE resolution point for both the writer (_write_heartbeat) and
        the dead-man's-switch reader (_check_heartbeat_freshness) -- they must
        never diverge, or the switch would watch a file nothing writes and
        force-exit a perfectly healthy process every _DEADMAN_CHECK_INTERVAL.
        Read from cfg on each access rather than snapshotted in __init__ so a
        test (or a caller mutating cfg) can't leave the two halves disagreeing.
        """
        return Path(self._cfg.heartbeat_path)

    def _resolve_bankroll(self) -> float:
        """
        Bankroll used as the Kelly-sizing reference for every strategy.

        In paper/dry-run mode, cfg.paper_bankroll_override (when set) replaces
        the real Kalshi account balance -- a tiny demo balance (e.g. $12)
        rounds almost every Kelly size down to 0 contracts, starving paper
        trading of the fills it needs to build a track record toward the paper
        gate. The override NEVER applies in live mode (cfg.dry_run False):
        real capital is always sized against the real balance, no exceptions.
        """
        if self._cfg.dry_run and self._cfg.paper_bankroll_override is not None:
            bankroll = float(self._cfg.paper_bankroll_override)
            log.info("runner.paper_bankroll_override", bankroll=bankroll)
            return bankroll
        try:
            bal = self._client.get_balance()
            bankroll = float(bal.get("balance", 0.0)) / 100.0
            log.info("runner.balance", bankroll=bankroll)
            return bankroll
        except Exception as exc:  # noqa: BLE001
            log.warning("runner.balance_unavailable", error=str(exc))
            return 1000.0  # fallback for testing

    async def run(self) -> None:
        """Start all feeds and run every strategy's loop until cancelled."""
        log.info(
            "runner.starting",
            strategies=sorted(self._strategies),
            allocations=self._allocations,
            feeds=sorted(self._feeds),
        )

        self._bankroll = self._resolve_bankroll()

        tickers = self._explicit_tickers if self._explicit_tickers is not None else self._discover_tickers()

        ws = WSClient(
            self._cfg,
            tickers=tickers,
            channels=["orderbook_delta", "ticker"],
            on_message=self._on_ws_message,
            on_invalidate=self._invalidate_all_books,
        )
        self._ws_client = ws
        self._active_tickers = set(tickers)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(ws.run(), name="ws_feed")
            tg.create_task(self._strategy_loop(), name="strategy_loop")
            tg.create_task(self._deadman_loop(), name="deadman_switch")
            tg.create_task(self._post_resolution.run(), name="post_resolution_scanner")
            if self._cfg.ticker_rediscovery_interval_seconds > 0:
                tg.create_task(self._rediscovery_loop(), name="ticker_rediscovery")
            for feed_name, feed in self._feeds.items():
                tg.create_task(feed.run(), name=f"feed_{feed_name}")
                # Tick stream as a SIBLING of the poll, not a replacement:
                # both write the same history deque, and the poll is what
                # keeps the feed alive whenever the socket is down.
                if self._cfg.btc_ws_enabled and hasattr(feed, "run_ws"):
                    tg.create_task(feed.run_ws(), name=f"feed_{feed_name}_ws")

    def _reconstruct_exposure_from_positions(self) -> None:
        """
        Replay the driver exposure of every still-LIVE persisted position into
        PortfolioRisk on startup, so the in-memory exposure matches the
        positions DB after a restart.

        Only positions with a KNOWN, FUTURE expiry are reconstructed. This is
        the guard against phantom exposure: a position whose window has already
        settled (or whose ticker no loaded strategy can resolve an expiry for --
        _ticker_expiry returns None, e.g. an orphan from a retired strategy)
        must NOT contribute exposure, because nothing will ever release it
        (expire_market only fires for tickers in active discovery), so it would
        permanently inflate the driver bucket and eventually block all trades.
        The startup stale-sweep already deletes known-past-expiry rows before
        this runs; this future-expiry check additionally excludes unknown-expiry
        orphans that the sweep conservatively keeps. Rows with contracts/price
        of 0 (guard-only opens) contribute nothing either way.
        """
        now = time.time()
        rows = self._tracker.open_positions_with_exposure()
        reconstructed = 0
        skipped: list[str] = []
        for pos in rows:
            ticker = pos["ticker"]
            if pos["contracts_units"] <= 0 or pos["price"] <= 0:
                continue
            expiry = self._ticker_expiry(ticker)
            if expiry is None or expiry <= now:
                # Expired or unresolvable -> not a live position; skip its exposure.
                skipped.append(ticker)
                continue
            self._portfolio_risk.update_exposure(
                pos["ticker"], pos["driver"], pos["contracts_units"], pos["price"],
                action="open",
            )
            reconstructed += 1

        log.info(
            "runner.exposure_reconstructed",
            rows_in_db=len(rows),
            reconstructed=reconstructed,
            skipped_not_live=len(skipped),
            skipped_tickers=skipped[:20],
            driver_totals={k: round(v, 2) for k, v in self._portfolio_risk.exposure_summary().items()},
        )

    # A settled market whose outcome cannot be recorded is retried on later
    # rediscovery passes rather than dropped. This bounds that retry so a market
    # that will NEVER resolve (delisted, voided, an exchange record that never
    # appears) cannot pin its driver exposure open forever and eventually block
    # all trading. At the 600s default rediscovery interval this is ~1 hour.
    _MAX_UNRESOLVED_EXPIRY_ATTEMPTS = 6

    def _record_swept_on_load_outcomes(self) -> None:
        """
        Record outcomes for positions the tracker's startup stale-sweep cleared.

        The sweep deletes any position whose window already closed, so a
        process that restarts between a fill and that window's expiry loses the
        position row without ever recording a trade_outcome -- the trade then
        exists at Kalshi, and nowhere in our accounting. For a real-money fill
        that is money missing from the Live P&L tab.

        Best-effort and never raises: this runs in __init__, and a settlement
        hiccup must not stop the bot from starting. Anything it cannot record
        is still recoverable via `kbot reconcile-live`, which rebuilds from the
        position_open events.
        """
        rows = getattr(self._tracker, "expired_on_load_rows", None)
        if not rows:
            return
        by_ticker: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_ticker[row["ticker"]].append(row)

        for ticker, positions in by_ticker.items():
            try:
                recorded = self._record_outcomes(ticker, positions)
                log.info(
                    "runner.swept_on_load_outcomes_recorded",
                    ticker=ticker, recorded=recorded, positions=len(positions),
                    live=sum(1 for p in positions if p.get("is_live")),
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "runner.swept_on_load_recording_failed",
                    ticker=ticker, error=str(exc)[:150],
                )

    def expire_market(self, ticker: str) -> None:
        """
        Mark all open positions for `ticker` as closed (market resolved/expired).

        A REAL-MONEY position is only cleared once its outcome has actually
        been RECORDED. Previously the clear was unconditional, so a settlement
        that could not be resolved at that instant -- the exchange had not
        published the result yet, or get_market failed -- destroyed the only
        record the outcome could ever be rebuilt from, and the trade vanished
        from P&L entirely. Now it stays put and the next rediscovery pass
        retries it, up to _MAX_UNRESOLVED_EXPIRY_ATTEMPTS.

        PAPER positions keep the original behaviour and are cleared either way.
        Deferring them would buy nothing -- no money is at stake and the paper
        gate tolerates a missing sample -- while holding their driver exposure
        open would throttle the very paper trading the gate needs volume from.
        """
        # Snapshot the positions (with contracts/price) BEFORE clearing them,
        # so we can record their settled outcomes.
        positions = [
            p for p in self._tracker.open_positions_with_exposure() if p["ticker"] == ticker
        ]
        recorded = self._record_outcomes(ticker, positions)

        has_live = any(p.get("is_live") for p in positions)
        if has_live and recorded < len(positions):
            attempts = self._unresolved_expiries.get(ticker, 0) + 1
            self._unresolved_expiries[ticker] = attempts
            if attempts < self._MAX_UNRESOLVED_EXPIRY_ATTEMPTS:
                log.warning(
                    "runner.expiry_deferred_unrecorded",
                    ticker=ticker, recorded=recorded, positions=len(positions),
                    attempt=attempts, max_attempts=self._MAX_UNRESOLVED_EXPIRY_ATTEMPTS,
                )
                # Leave the position AND its exposure in place; the ticker is
                # re-added to the expiry sweep by _rediscover_once next pass.
                self._retry_expiries.add(ticker)
                return
            log.error(
                "runner.expiry_forced_unrecorded",
                ticker=ticker, recorded=recorded, positions=len(positions),
                attempts=attempts,
                hint="run `kbot reconcile-live` to recover any real-money trade",
            )

        self._unresolved_expiries.pop(ticker, None)
        self._retry_expiries.discard(ticker)
        cleared = self._tracker.expire(ticker)
        # Release the ticker's driver exposure so the driver cap frees up when
        # a window settles -- without this, exposure only ever grew (opens
        # added, expiry never subtracted) and eventually blocked all trades.
        self._portfolio_risk.release_exposure(ticker)
        for side_val in cleared:
            self._telemetry.log_position_closed(ticker, side_val, {"reason": "market_expired"})
            log.info("runner.position_expired", ticker=ticker, side=side_val)

    def _record_outcomes(self, ticker: str, positions: list[dict]) -> int:
        """
        Fetch the settled market and log a trade_outcome for each held position
        on `ticker` (this is what feeds resolved_paper_trades / the paper gate /
        kill-fast / learn). Never raises -- a settlement fetch or recording
        hiccup must not crash the expiry path or the trading loop.

        Returns HOW MANY outcomes were recorded, so expire_market can tell a
        completed settlement from a failed one and keep the position for retry
        instead of destroying an unrecorded trade.

        `client` is passed through so REAL-MONEY positions resolve against
        Kalshi's own settled position record rather than the local model --
        see kbot.outcomes.record_ticker_outcomes. Paper positions are
        unaffected. A live position is therefore recordable even when
        get_market comes back empty, so the market fetch failing is no longer
        an early return.
        """
        if not positions:
            return 0
        try:
            from kbot.outcomes import record_ticker_outcomes, strategy_for_ticker

            market = self._client.get_market(ticker)
            if not market:
                log.warning("runner.outcome_market_unavailable", ticker=ticker)
                market = {}
                if not any(p.get("is_live") for p in positions):
                    # Paper positions have no other source of truth.
                    return 0
            strategy = next(
                (name for name, s in self._strategies.items() if s.owns_ticker(ticker)),
                strategy_for_ticker(ticker),
            )
            n = record_ticker_outcomes(
                ticker, positions, market, self._telemetry, strategy, self._cfg,
                client=self._client,
            )
            log.info("runner.outcomes_recorded", ticker=ticker, count=n, strategy=strategy)
            if n and any(p.get("is_live") for p in positions):
                self._push_live_pnl(strategy)
            return n
        except Exception as exc:  # noqa: BLE001
            log.error("runner.outcome_recording_failed", ticker=ticker, error=str(exc)[:150])
            return 0

    def _push_live_pnl(self, strategy: str | None) -> None:
        """
        Update the real-money P&L tab right after a real trade settles.

        Only called when a position that was opened with REAL MONEY (its
        placement-time is_live stamp) has just been recorded as settled, so a
        paper deployment never touches the sheet from this path.

        The push rebuilds the full row set from the event log and upserts by
        trade, so it is safe to run here as often as trades settle AND to run
        again from `kbot learn --evaluate` -- the two paths converge on the
        same tab instead of double-counting. Never raises: P&L reporting must
        not be able to break the expiry path.
        """
        # A process running several strategies has no single-owner Live P&L
        # tab: the rows would be filtered to one strategy but the running total
        # written into a tab the others also write. Leave it to `kbot learn
        # --evaluate`, which is per-deployment, rather than write a total that
        # means nothing.
        if self._sheets.strategy != strategy:
            log.info(
                "runner.live_pnl_push_skipped_unscoped",
                strategy=strategy, sheet_scope=self._sheets.strategy,
            )
            return

        try:
            from kbot.live_pnl import balance_dollars, push_live_pnl

            balance = None
            try:
                balance = balance_dollars(self._client.get_balance())
            except Exception as exc:  # noqa: BLE001
                log.warning("runner.live_pnl_balance_unavailable", error=str(exc)[:150])

            counts = push_live_pnl(
                self._telemetry, self._sheets, strategy=strategy, balance=balance
            )
            log.info("runner.live_pnl_pushed", strategy=strategy, **counts)
        except Exception as exc:  # noqa: BLE001
            log.error("runner.live_pnl_push_failed", strategy=strategy, error=str(exc)[:200])

    def close(self) -> None:
        self._client.close()
        self._telemetry.close()
        self._gates.close()
        self._tracker.close()

    # ── Ticker discovery ─────────────────────────────────────────────────────

    def _all_series(self) -> list[str]:
        seen: list[str] = []
        for strategy in self._strategies.values():
            for series in strategy.series_tickers:
                if series not in seen:
                    seen.append(series)
        return seen

    def _discover_tickers(self) -> list[str]:
        """
        Union of ALL currently-open events' markets for every series any
        loaded strategy declares. Also feeds each discovered market's raw
        dict to every strategy that owns it via ingest_market_metadata --
        some series (e.g. KXBTC15M) don't encode everything evaluate()
        needs in the ticker string itself (see kbot.strategy.btc_hourly's
        module docstring), so this is the live path that supplies it.

        2026-07: returns EVERY open event's markets, not just the nearest
        one. For a fast-rolling series like KXBTC15M (a fresh 15-minute
        window every 15 min, with the next window opening before the
        current closes) this means both the current and the just-opened
        next window are picked up on the same pass -- so _rediscover_once
        (which calls this on a timer) subscribes to a new window within its
        first few minutes rather than only once it becomes the nearest.
        """
        discovered: set[str] = set()
        for series in self._all_series():
            try:
                events = self._client.get_events(series_ticker=series, status="open", limit=5)
            except Exception as exc:  # noqa: BLE001
                log.warning("runner.ticker_discovery_failed", series=series, error=str(exc))
                continue
            if not events:
                log.warning("runner.no_open_events", series=series)
                continue
            for event in events:
                for m in event.get("markets", []):
                    t = m.get("ticker")
                    if not t:
                        continue
                    discovered.add(t)
                    for strategy in self._strategies.values():
                        if strategy.owns_ticker(t):
                            strategy.ingest_market_metadata(t, m)
        return sorted(discovered)

    def _ticker_expiry(self, ticker: str) -> float | None:
        """
        Settlement timestamp for `ticker` per its owning strategy's parser,
        or None if no loaded strategy owns it or can derive an expiry.
        Used by _rediscover_once to prune already-settled windows.
        """
        for strategy in self._strategies.values():
            if strategy.owns_ticker(ticker):
                expiry = strategy.expiry_ts(ticker)
                if expiry is not None:
                    return expiry
        return None

    # ── WS / book maintenance ────────────────────────────────────────────────

    def _invalidate_all_books(self, reason: str) -> None:
        """
        Drop every book. Called by WSClient the moment it can no longer vouch
        for the stream: disconnect, reconnect, server/subscription error, or a
        sequence gap.

        Fail CLOSED -- after this, nothing is evaluated or traded on any ticker
        until a fresh snapshot rebuilds it on the new session. Clearing in
        place (not rebinding) is required: the executor's book_provider and
        PostResolutionScanner hold a reference to this dict.
        """
        dropped = len(self._books)
        self._books.clear()
        self._book_invalidations += 1
        self._last_invalidation = reason
        if dropped:
            log.warning("runner.books_invalidated", reason=reason, dropped=dropped)
        else:
            log.info("runner.books_invalidated", reason=reason, dropped=0)

    def _invalidate_book(self, ticker: str, reason: str) -> None:
        """Drop ONE ticker's book; it resumes on its next snapshot."""
        if self._books.pop(ticker, None) is not None:
            self._book_invalidations += 1
            self._last_invalidation = f"{ticker}:{reason}"
            log.warning("runner.book_invalidated", ticker=ticker, reason=reason)

    def _on_ws_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        data = msg.get("msg", {})

        if msg_type == "orderbook_snapshot":
            ticker = data.get("market_ticker", "")
            # A snapshot is the ONLY thing that makes a ticker valid, and it is
            # necessarily on the current session: WSClient invalidates before
            # any message of a new session can be dispatched.
            self._books[ticker] = _parse_orderbook(ticker, data)
        elif msg_type == "orderbook_delta":
            ticker = data.get("market_ticker", "")
            if ticker in self._books:
                try:
                    _apply_delta(self._books[ticker], data)
                except BookInvalidated as exc:
                    # No partial repair: the book and the exchange disagree by
                    # an unknown amount, so drop it and wait for a snapshot.
                    self._invalidate_book(ticker, f"delta_inconsistent:{exc}")
                    return
            else:
                pass  # no valid book for this ticker; wait for a snapshot
        else:
            return

        self._run_consistency_scan()

    def _run_consistency_scan(self) -> None:
        """
        Intra-Kalshi consistency scan (Source 6). same_market_arb is
        ticker-agnostic (checked once, not per-strategy, to avoid
        duplicate detections); monotonicity needs each strategy's own
        ticker parser, so it runs once per strategy. Alert + log only,
        never auto-executes. Never raises -- a scanner bug must not crash
        the WS loop.

        This runs on EVERY WS book message, and the scanners are stateless:
        a detection that stands for a thousand messages is returned a
        thousand times. Only detections that are new, or whose prices have
        moved since the row we last wrote, reach the Arb Log -- see
        _log_new_detections. Without that, a single standing violation
        writes a row per message, which is exactly how the tab reached
        Google's 10M-cell workbook cap.
        """
        current: dict[tuple, str] = {}

        try:
            same_market = scan_same_market_arb(self._books, self._cfg)
        except Exception as exc:  # noqa: BLE001
            log.error("runner.consistency_scan_failed", check="same_market_arb", error=str(exc))
            same_market = []
        self._log_new_detections(
            same_market, same_market_arb_identity, same_market_arb_to_row, current
        )

        for name, strategy in self._strategies.items():
            try:
                violations = scan_monotonicity(self._books, strategy.parse_strike)
            except Exception as exc:  # noqa: BLE001
                log.error("runner.consistency_scan_failed", check="monotonicity", strategy=name, error=str(exc))
                continue
            self._log_new_detections(
                violations, monotonicity_violation_identity,
                monotonicity_violation_to_row, current,
            )

        # Replace rather than merge: a key absent from this scan means the
        # condition has cleared, and forgetting it is what lets a genuine
        # RE-occurrence later be logged as the new event it is.
        #
        # A scan that raised contributes nothing to `current`, so its
        # detections are forgotten and re-logged once on the next successful
        # pass. That is the right way round -- one duplicate row after a
        # scanner error beats silently dropping a violation.
        self._arb_seen = current

    def _log_new_detections(
        self,
        detections: list[Any],
        identify: Callable[[Any], tuple[tuple, str]],
        to_row: Callable[[Any], dict[str, Any]],
        current: dict[tuple, str],
    ) -> None:
        """
        Write an Arb Log row for each detection in `detections` that is new or
        whose fingerprint has changed, recording every one of them in
        `current` (the next scan's `_arb_seen`).

        `current` accumulates across both checks within one scan, so it is
        passed in rather than returned -- the caller owns the whole-scan view
        and swaps it in only once every check has contributed.
        """
        for detection in detections:
            key, fingerprint = identify(detection)
            current[key] = fingerprint
            if self._arb_seen.get(key) == fingerprint:
                continue   # unchanged since the row we already wrote
            self._sheets.append_arb_log(to_row(detection))

    # ── Strategy evaluation loops ────────────────────────────────────────────

    async def _strategy_loop(self) -> None:
        """Evaluate every strategy on each ticker it owns, every 5 seconds (fallback heartbeat cycle)."""
        while True:
            await asyncio.sleep(5.0)
            self._run_cycle_once()

    # ── Periodic ticker re-discovery ─────────────────────────────────────────

    async def _rediscovery_loop(self) -> None:
        """
        Every cfg.ticker_rediscovery_interval_seconds, pick up newly-opened
        windows and drop settled ones -- so a fast-rolling series (KXBTC15M:
        a new 15-minute window every 15 min) is covered continuously instead
        of only for the window(s) present at startup.
        """
        interval = self._cfg.ticker_rediscovery_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self._rediscover_once()
            except Exception as exc:  # noqa: BLE001
                # A discovery/subscribe hiccup must never kill the live loop.
                log.error("runner.rediscovery_failed", error=str(exc))

    async def _rediscover_once(self) -> None:
        """
        One re-discovery pass: subscribe to newly-opened windows on the
        existing WS connection (no reconnect) and prune settled ones.

        Runs off the strategies' declared series, NOT the startup ticker
        list -- so it works even when the runner was given explicit startup
        tickers (the single-strategy `kbot paper --series` path, which the
        live watchdog uses, seeds explicit tickers but still needs rolling
        coverage).
        """
        if self._ws_client is None:
            return

        discovered = set(self._discover_tickers())
        now = time.time()

        new = sorted(discovered - self._active_tickers)
        # Prune any active ticker whose window has already settled. Only prune
        # on a KNOWN past expiry -- a None expiry (unparseable / unowned) is
        # left alone rather than dropped on a guess.
        expired = sorted(
            {
                t for t in self._active_tickers
                if (exp := self._ticker_expiry(t)) is not None and exp < now
            }
            # Tickers whose outcome could not be recorded on a previous pass.
            # They were already dropped from _active_tickers, so without this
            # they would never be swept again and their position would sit in
            # the DB unrecorded until a restart deleted it.
            | self._retry_expiries
        )

        added: list[str] = []
        if new:
            added = await self._ws_client.subscribe_additional(new)
            self._active_tickers.update(added)

        if expired:
            self._ws_client.prune_tickers(expired)
            self._active_tickers.difference_update(expired)
            for t in expired:
                # Stop evaluating a settled market; expire any open position.
                self._books.pop(t, None)
                self.expire_market(t)

        log.info(
            "runner.rediscovery",
            new_added=len(added),
            expired_removed=len(expired),
            active_tickers=len(self._active_tickers),
        )

    def _run_cycle_once(self) -> None:
        """
        One fixed-interval evaluation pass across every loaded strategy.
        Factored out of _strategy_loop so it can be driven synchronously in
        tests without an asyncio event loop.
        """
        for name, strategy in self._strategies.items():
            for ticker, book in list(self._books.items()):
                if not strategy.owns_ticker(ticker):
                    continue
                # btc_vol is computed PER TICKER (not once per strategy): a
                # strategy spanning series with different contract horizons
                # (KXBTC vs KXBTC15M) needs a different vol_window_seconds
                # per ticker -- see Strategy.vol_window_seconds. btc_price
                # itself doesn't vary by ticker.
                btc_price, btc_vol = self._feed_inputs_for(strategy, ticker)
                self._evaluate_ticker(name, strategy, ticker, book, btc_price, btc_vol)

            check_kill_fast(name, self._telemetry, self._gates, self._cfg)
            self._eval_cycles[name] += 1

        self._global_cycle_count += 1
        if self._global_cycle_count % _HEARTBEAT_CYCLES == 0:
            self._write_heartbeat()

    def _on_spot_price(self, feed_name: str, price: float, ts: float) -> None:
        """
        Spot-feed callback (Source 6 event-driven spot trigger), shared by
        every strategy whose spot_feed_name is `feed_name` -- one fast move
        fires an off-cycle re-evaluation for all of them, not just one.

        `feed_name` is bound per-feed at construction (see __init__) so an
        ETH tick is checked against the ETH feed and re-evaluates only ETH
        strategies. Without it, a second feed's ticks would be measured
        against the BTC feed's history and routed to BTC's strategies.
        """
        feed = self._feeds.get(feed_name)
        if feed is None or not feed.has_spot_trigger(self._cfg.spot_trigger_bps, window_seconds=3.0, now=ts):
            return

        self._session_spot_triggers += 1
        log.info(
            "runner.spot_trigger",
            feed=feed_name,
            price=price,
            move_bps=round(feed.spot_move_bps(3.0, now=ts) or 0.0, 2),
        )
        for name, strategy in self._strategies.items():
            if strategy.spot_feed_name != feed_name:
                continue
            for ticker, book in list(self._books.items()):
                if not strategy.owns_ticker(ticker):
                    continue
                if not strategy.is_near_atm(ticker, price):
                    continue
                spot_vol = self._estimate_spot_vol(
                    strategy, price, ts,
                    window_seconds=strategy.vol_window_seconds(ticker),
                    vol_multiplier_override=strategy.vol_multiplier(ticker),
                    vol_floor_override=strategy.vol_floor(ticker),
                )
                self._evaluate_ticker(name, strategy, ticker, book, price, spot_vol)

    def _on_btc_price(self, price: float, ts: float) -> None:
        """Back-compat alias for the BTC spot trigger (kbot.paper.PaperTrader
        calls this directly on the single-strategy path)."""
        self._on_spot_price("btc_spot", price, ts)

    def _spot_feed_for(self, strategy: Strategy) -> Any | None:
        """
        This strategy's spot feed instance, or None if it has no spot
        underlying (or declared one the runner never built).

        Resolved from strategy.spot_feed_name rather than a hardcoded
        "btc_spot" -- that hardcoding was the reason a second spot-driven
        strategy would have silently received no price at all.
        """
        feed_name = strategy.spot_feed_name
        if feed_name is None:
            return None
        return self._feeds.get(feed_name)

    def _feed_inputs_for(self, strategy: Strategy, ticker: str) -> tuple[float | None, float | None]:
        feed = self._spot_feed_for(strategy)
        if feed is None:
            return None, None
        price = feed.price
        vol = self._estimate_spot_vol(
            strategy, price, time.time(),
            window_seconds=strategy.vol_window_seconds(ticker),
            vol_multiplier_override=strategy.vol_multiplier(ticker),
            vol_floor_override=strategy.vol_floor(ticker),
        )
        return price, vol

    def _estimate_spot_vol(
        self,
        strategy: Strategy,
        price: float | None,
        now_ts: float,
        *,
        window_seconds: float | None = None,
        vol_multiplier_override: float | None = None,
        vol_floor_override: float | None = None,
    ) -> float | None:
        """
        2026-07 calibration fix: prefer ladder-implied vol (back-solved
        from the current near-ATM strike ladder, which reprices every
        tick) over our own forecast; fall back to a realized-vol estimate
        (EWMA over the strategy's own spot-tick history) when the ladder
        can't be fit. See kbot.vol for the full story.

        `window_seconds`/`vol_multiplier_override`/`vol_floor_override`, if given, make the
        fallback path series-aware (2026-07: KXBTC15M's 15-minute
        contracts need a much shorter EWMA window and independent
        multiplier than KXBTC's 60-minute ones -- see
        Strategy.vol_window_seconds/vol_multiplier).

        Everything underlying-specific now comes from `strategy`: its own
        spot feed, its own pricing functions (vol_model_functions), and
        its OWN books. The books filter matters as soon as more than one
        underlying is in play -- fitting BTC's strike ladder and handing
        the resulting sigma to an ETH contract would be a silent, entirely
        invisible mispricing. (KXETH15M/KXBTC15M are single-binary series
        with no range ladder, so in practice the fit finds nothing and
        both fall through to their own EWMA -- the filter is what keeps
        that true if a laddered series is ever added.)
        """
        if price is None:
            return None
        from kbot.vol import estimate_vol, ewma_vol

        feed = self._spot_feed_for(strategy)
        if feed is None:
            return None

        model = strategy.vol_model_functions()
        if model is None:
            return None
        parse_ticker, lognormal_prob, lognormal_range_prob = model

        own_books = {t: b for t, b in self._books.items() if strategy.owns_ticker(t)}
        ewma = ewma_vol(feed.history, window_seconds=window_seconds)
        result = estimate_vol(
            own_books, price, now_ts,
            parse_ticker=parse_ticker, lognormal_prob=lognormal_prob, lognormal_range_prob=lognormal_range_prob,
            ewma=ewma, parkinson=None, cfg=self._cfg,
            vol_multiplier_override=vol_multiplier_override,
            vol_floor_override=vol_floor_override,
        )
        log.debug(
            "runner.vol_estimate",
            strategy=strategy.name, source=result.source, sigma=round(result.sigma, 4),
            floored=result.floored,
        )
        return result.sigma

    def _safe_evaluate(
        self,
        name: str,
        strategy: Strategy,
        book: OrderBook,
        btc_price: float | None,
        btc_vol: float | None,
        bankroll: float,
        current_time: float | None = None,
    ) -> list:
        """
        evaluate() isolated: an exception here must not kill the runner or
        the other strategies. 3+ exceptions within 5 minutes advisory-
        pauses this strategy (stops calling evaluate() for it) while
        everyone else keeps running -- a separate, in-process circuit
        breaker from the gate registry's own pause state.
        """
        if name in self._advisory_paused:
            return []
        try:
            return strategy.evaluate(book, btc_price=btc_price, btc_vol=btc_vol, bankroll=bankroll, current_time=current_time)
        except Exception as exc:  # noqa: BLE001
            now = time.time()
            fails = [t for t in self._failure_timestamps[name] if now - t <= _FAILURE_WINDOW_SECONDS]
            fails.append(now)
            self._failure_timestamps[name] = fails
            log.error("runner.strategy_evaluate_failed", strategy=name, ticker=book.ticker, error=str(exc))

            if len(fails) >= _FAILURE_THRESHOLD and name not in self._advisory_paused:
                self._advisory_paused.add(name)
                log.critical("runner.strategy_advisory_paused", strategy=name, failures=len(fails))
                self._send_alert(
                    f"ADVISORY PAUSE: strategy '{name}' raised {len(fails)} exceptions within "
                    f"{int(_FAILURE_WINDOW_SECONDS)}s in evaluate() -- isolated in-process; "
                    f"other strategies continue running. Restart the process to clear."
                )
            return []

    def _send_alert(self, message: str) -> None:
        try:
            from kbot.alerts import Alerter
            with Alerter(self._cfg) as alerter:
                alerter.send_critical(message)
        except Exception as exc:  # noqa: BLE001
            log.error("runner.alert_failed", error=str(exc))

    def _gate_check(self, name: str) -> str | None:
        """
        None if `name` may execute this signal, else the skip reason.

        LIVE mode: only gate levels in _LIVE_APPROVED_LEVELS may ever route
        a real order, and a pause always blocks regardless of
        paused_allows_dry_run (that setting exists for dry-run forward-
        testing continuity, never for live risk). Paper/dry-run mode: a
        pause blocks unless paused_allows_dry_run is set (the default) --
        that's how forward evidence keeps accumulating for a strategy
        under review.
        """
        gate = self._gates.get_gate(name)
        level = gate.level if gate else LEVELS[0]
        is_paused = gate.paused if gate else False

        if not self._cfg.dry_run:
            if level not in _LIVE_APPROVED_LEVELS:
                return "not_live_approved"
            if is_paused:
                return "gate_paused"
            return None

        if is_paused and not self._cfg.paused_allows_dry_run:
            return "gate_paused"
        return None

    def _evaluate_ticker(
        self,
        name: str,
        strategy: Strategy,
        ticker: str,
        book: OrderBook,
        btc_price: float | None,
        btc_vol: float | None,
    ) -> None:
        """Evaluate one (strategy, ticker) pair and act on any signal."""
        alloc_bankroll = self._bankroll * self._allocations[name]
        results = self._safe_evaluate(name, strategy, book, btc_price, btc_vol, alloc_bankroll)

        for r in results:
            is_signal = r.contracts > 0

            # A stand-down never priced anything: its price/fair_p/net_edge are
            # placeholder zeros. Emitting them as an `evaluation` row wrote a
            # measurement that was never taken -- an ask of 0, a fair value of
            # 0 and an edge of exactly 0 -- and every one of those rows landed
            # in the same table as real evaluations, dragging any average
            # computed over them toward zero. Log the skip under its own event
            # type, with the unmeasured fields explicitly null.
            if not r.priced:
                self._telemetry.log_event(
                    "skip",
                    payload={
                        "strategy": name,
                        "side": r.side.value,
                        "ask": None,
                        "fair_p": None,
                        "net_edge": None,
                        "contracts": 0,
                        "skip_reason": r.skip_reason,
                        "btc_price": btc_price,
                        "vol": round(btc_vol, 4) if btc_vol is not None else None,
                        "bankroll": alloc_bankroll,
                    },
                    ticker=ticker,
                )
                continue

            near_threshold = r.net_edge_dollars > -_EVAL_LOG_THRESHOLD

            if is_signal or near_threshold:
                event_type = "signal" if is_signal else "evaluation"
                self._telemetry.log_event(
                    event_type,
                    payload={
                        "strategy": name,
                        "side": r.side.value,
                        "ask": round(r.price_dollars, 4),
                        "fair_p": round(r.fair_p, 4),
                        "net_edge": round(r.net_edge_dollars, 4),
                        "contracts": r.contracts,
                        "skip_reason": r.skip_reason,
                        "btc_price": btc_price,
                        "vol": round(btc_vol, 4) if btc_vol is not None else None,
                        "bankroll": alloc_bankroll,
                    },
                    ticker=ticker,
                )
                self._session_evals_logged[name] += 1

            if not is_signal:
                continue

            if self._tracker.is_held(ticker, r.side):
                self._telemetry.log_event(
                    "skip",
                    payload={
                        "strategy": name, "skip_reason": "position_already_held",
                        "side": r.side.value, "contracts": r.contracts,
                        "net_edge": round(r.net_edge_dollars, 4),
                    },
                    ticker=ticker,
                )
                continue

            gate_skip_reason = self._gate_check(name)
            if gate_skip_reason is not None:
                self._telemetry.log_event(
                    "skip",
                    payload={
                        "strategy": name, "skip_reason": gate_skip_reason,
                        "side": r.side.value, "contracts": r.contracts,
                        "net_edge": round(r.net_edge_dollars, 4),
                    },
                    ticker=ticker,
                )
                continue

            self._session_signals[name] += 1
            ack = self._executor.execute(
                r, strategy=name, is_single_binary=strategy.is_single_binary(ticker),
            )
            if ack is not None:
                # Log what FILLED, not what was requested. Orders are sent
                # immediate_or_cancel and can fill partially, and
                # kbot.outcomes.backfill_outcomes rebuilds settled outcomes
                # from this event when the positions DB is unavailable -- a
                # requested count here would settle contracts that never
                # existed and diverge from the tracker's own record.
                filled = ack.fill_contracts
                fill_price = (
                    r.price_dollars if ack.average_fill_price is None
                    else ack.average_fill_price
                )
                self._telemetry.log_position_open(
                    ticker, r.side.value,
                    {"strategy": name, "contracts": filled, "price": fill_price,
                     # The ack's own field names, alongside the contracts/price
                     # aliases every existing reader uses. Recording the fill
                     # under the name the exchange and OrderAck use it under is
                     # what makes a fill auditable straight from the event log
                     # -- see log_order below.
                     # `fill_count` stays in CONTRACTS so existing readers and
                     # json_extract queries keep working; `fill_count_units` is
                     # the exact exchange-reported size alongside it.
                     "fill_count": ack.fill_contracts,
                     "fill_count_units": ack.fill_count_units,
                     "average_fill_price": ack.average_fill_price,
                     "net_edge": r.net_edge_dollars, "fair_p": r.fair_p,
                     "raw_fair_p": r.raw_fair_p,
                     # Placement-time real-money stamp, same source as the one
                     # the tracker persists (OrderAck.dry_run). The tracker's
                     # copy is what settlement reads; this one is what
                     # kbot.outcomes.backfill_outcomes reads when it has to
                     # reconstruct outcomes from the event log instead.
                     "is_live": not ack.dry_run},
                )
                self._telemetry.log_order(ticker, {
                    "strategy": name,
                    "client_order_id": ack.client_order_id,
                    "order_id": ack.order_id,
                    "dry_run": ack.dry_run,
                    "contracts": filled,
                    "requested_contracts": r.contracts,
                    "price": fill_price,
                    # What the V2 order response actually reported, under the
                    # OrderAck/exchange field names. `contracts`/`price` above
                    # are the same two numbers under the aliases the rest of the
                    # codebase reads; these are what an operator querying
                    # json_extract(payload,'$.fill_count') expects to find, and
                    # their absence made every fill unauditable from the DB.
                    # remaining_count was never recorded anywhere before, so a
                    # partial fill left no trace of what was cancelled.
                    "fill_count": ack.fill_contracts,
                    "fill_count_units": ack.fill_count_units,
                    "average_fill_price": ack.average_fill_price,
                    "remaining_count": ack.remaining_contracts,
                    "remaining_count_units": ack.remaining_count_units,
                    "net_edge": r.net_edge_dollars,
                })

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def _write_heartbeat(self) -> None:
        strategies_section: dict[str, Any] = {}
        for name in self._strategies:
            gate = self._gates.get_gate(name)
            strategies_section[name] = {
                "allocation": self._allocations[name],
                "eval_cycles": self._eval_cycles[name],
                "session_signals": self._session_signals[name],
                "session_evals_logged": self._session_evals_logged[name],
                "gate_level": gate.level if gate else LEVELS[0],
                "paused": gate.paused if gate else False,
                "advisory_paused": name in self._advisory_paused,
            }

        # Display-only spot snapshot, taken from whichever spot-driven strategy
        # this process runs (exactly one, per _assert_isolated_domains). The
        # payload keys stay "btc_price"/"vol_annualised" regardless of
        # underlying -- `kbot status` reads them by name, and an ETH service
        # reporting its ETH spot under those keys is strictly better than
        # reporting nothing. "spot_feed" disambiguates which one it is.
        spot_strategy = next(
            (s for s in self._strategies.values() if self._spot_feed_for(s) is not None),
            None,
        )
        spot_feed = self._spot_feed_for(spot_strategy) if spot_strategy is not None else None
        spot_price = spot_feed.price if spot_feed is not None else None
        payload: dict[str, Any] = {
            "ts": time.time(),
            "pid": os.getpid(),
            "spot_feed": spot_strategy.spot_feed_name if spot_strategy is not None else None,
            "btc_price": spot_price,
            "vol_annualised": (
                self._estimate_spot_vol(spot_strategy, spot_price, time.time())
                if spot_strategy is not None else None
            ),
            "books_count": len(self._books),   # == count of VALID books
            "book_invalidations": self._book_invalidations,
            "last_invalidation": self._last_invalidation,
            "active_tickers_count": len(self._active_tickers),
            "session_spot_triggers": self._session_spot_triggers,
            "bankroll": self._bankroll,
            "driver_exposure": self._portfolio_risk.exposure_summary(),
            "strategies": strategies_section,
        }
        try:
            # Create the parent directory first. Once heartbeat_path became
            # configurable, a path into a not-yet-existing directory (e.g.
            # /opt/kbot-eth/state/hb.json before that dir is made) would fail
            # here, log a warning, and leave NO file -- which the dead-man's
            # switch reads as "frozen" and force-exits on, every
            # _DEADMAN_MAX_HEARTBEAT_AGE, into a Restart=always loop. A
            # misconfigured path must not be able to kill a healthy process.
            # No-op for the default (parent is "."), so BTC is unaffected.
            parent = self._heartbeat_path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.write_text(json.dumps(payload))
        except OSError as exc:
            log.warning(
                "runner.heartbeat_write_error",
                path=str(self._heartbeat_path), error=str(exc),
            )
        self._telemetry.log_event("heartbeat", payload=payload)
        log.debug("runner.heartbeat", strategies=sorted(self._strategies))

    # ── Dead-man's switch ─────────────────────────────────────────────────────

    async def _deadman_loop(self) -> None:
        """Force-exit the process if the heartbeat file stops advancing."""
        started = time.time()
        while True:
            await asyncio.sleep(_DEADMAN_CHECK_INTERVAL)
            self._check_heartbeat_freshness(started)

    def _check_heartbeat_freshness(self, started: float) -> None:
        """
        Exit(1) if the heartbeat file is older than _DEADMAN_MAX_HEARTBEAT_AGE.

        `started` is when this loop began; staleness is measured against the
        LATER of it and the file's mtime. That baseline is what stops a restart
        loop: kbot_heartbeat.json outlives the process that wrote it, and a
        fresh run doesn't write its first heartbeat for ~5 minutes
        (_HEARTBEAT_CYCLES * 5s), so a leftover file from the frozen run it is
        replacing would otherwise trip this switch 60s into every restart --
        with the freeze arming the trap for its own recovery.
        """
        try:
            last_write = self._heartbeat_path.stat().st_mtime
        except OSError:
            last_write = 0.0  # missing/unreadable -> lean on the `started` baseline

        age = time.time() - max(last_write, started)
        if age <= _DEADMAN_MAX_HEARTBEAT_AGE:
            return

        log.error(
            "paper.loop_frozen detected, forcing exit",
            heartbeat_age_seconds=round(age, 1),
            max_age_seconds=_DEADMAN_MAX_HEARTBEAT_AGE,
            pid=os.getpid(),
        )
        # os._exit skips atexit hooks and buffer flushing, and the watchdog
        # block-buffers our stdout into a log file -- flush by hand or the one
        # line explaining the exit dies with the process.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 -- nothing left to salvage; exit regardless
                pass
        os._exit(1)


# ── Orderbook helpers (shared with paper.py) ─────────────────────────────────

class BookInvalidated(Exception):
    """
    Raised by _apply_delta when a delta cannot be applied to a book that is
    still trustworthy -- currently, a level driven negative.

    Callers must drop the book and wait for a fresh snapshot. There is no
    partial repair: the size of the discrepancy is unknown, so any local fix
    is a guess dressed up as data.
    """



def _parse_orderbook(ticker: str, data: dict) -> OrderBook:
    # Kalshi WS convention (verified live):
    #   no_dollars_fp  = NO bids  → YES ask = 1 - no_bid_price
    #   yes_dollars_fp = YES bids → NO ask  = 1 - yes_bid_price
    def _levels(raw: list) -> list[PriceLevel]:
        levels = []
        for item in raw or []:
            try:
                ask = round(1.0 - float(item[0]), 10)
                # Integer hundredths of a contract. The old int(float(...))
                # truncated toward zero, so a resting 0.50 contracts read as
                # an empty level and vanished from the book entirely.
                qty_units = units_from_fixed_point(item[1])
                if qty_units is None or qty_units < 0:
                    continue
                if 0.0 < ask < 1.0:
                    levels.append(PriceLevel(price=ask, quantity_units=qty_units))
            except (IndexError, ValueError, TypeError):
                pass
        return levels

    return OrderBook(
        ticker=ticker,
        yes=_levels(data.get("no_dollars_fp", [])),
        no=_levels(data.get("yes_dollars_fp", [])),
    )


def _apply_delta(book: OrderBook, data: dict) -> bool:
    """
    Apply one SIGNED single-level update from an `orderbook_delta` message.

    Live V2 delta shape (verified on the wire):
        {"market_ticker": ..., "market_id": ..., "price_dollars": "0.9830",
         "delta_fp": "-104.00", "side": "yes", "ts": ..., "ts_ms": ...}

    A delta carries ONE level, not the snapshot's `yes_dollars_fp` /
    `no_dollars_fp` level arrays, and `delta_fp` is a signed CHANGE to the
    resting size -- not the new size. new = old + delta_fp; a result <= 0
    removes the level.

    Returns True if the book was updated, False if `data` was not a
    well-formed delta. A rejected message is always logged: silently
    no-op'ing on an unrecognised shape is what let the old snapshot-shaped
    handler freeze every book between snapshots.
    """
    ticker = data.get("market_ticker") or book.ticker
    price_raw = data.get("price_dollars")
    delta_raw = data.get("delta_fp")
    side = data.get("side")

    if price_raw is None or delta_raw is None or side not in ("yes", "no"):
        log.warning(
            "ws.orderbook_delta unrecognised shape, ignoring",
            ticker=ticker,
            keys=sorted(str(k) for k in data),
            side=side,
        )
        return False

    try:
        # Parsed exactly like the snapshot path: a dollar-string BID price
        # inverted to an ask, and a fixed-point size read as integer hundredths
        # of a contract. int(float(...)) truncated toward zero here too, which
        # was worse than on the snapshot path: the book is INCREMENTAL, so a
        # truncated removal left phantom size resting forever.
        ask = round(1.0 - float(price_raw), 10)
        change = units_from_fixed_point(delta_raw)
        if change is None:
            raise ValueError(f"unparseable delta_fp {delta_raw!r}")
    except (TypeError, ValueError):
        log.warning(
            "ws.orderbook_delta unparseable values, ignoring",
            ticker=ticker,
            price_dollars=price_raw,
            delta_fp=delta_raw,
            side=side,
        )
        return False

    if not 0.0 < ask < 1.0:
        # bid of 0.00 or 1.00 -> ask outside (0,1); PriceLevel would reject it
        # and _parse_orderbook drops the same levels from snapshots.
        log.warning(
            "ws.orderbook_delta out-of-range price, ignoring",
            ticker=ticker,
            price_dollars=price_raw,
            side=side,
        )
        return False

    # The yes/no crossing mirrors _parse_orderbook and IS intentional: both
    # feeds quote BIDS, while OrderBook stores ASKS. A resting NO bid at p is
    # the offer that fills a YES buy at 1-p, so side="no" updates book.yes and
    # side="yes" updates book.no. Applying the delta uncrossed would move the
    # opposite side of the book the snapshot built.
    levels = book.yes if side == "no" else book.no

    price_map = {lvl.price: lvl.quantity_units for lvl in levels}
    new_size = price_map.get(ask, 0) + change
    if new_size < 0:
        # A level cannot hold negative size. Either we missed a message or the
        # book we are applying to is not the one the exchange thinks it is
        # sending deltas for. Silently clamping to zero (what the old code did
        # by treating <= 0 alike) hides that and keeps trading a book known to
        # be wrong -- so refuse the whole book instead.
        log.error(
            "ws.orderbook_delta drove a level negative -- invalidating book",
            ticker=ticker,
            price=ask,
            side=side,
            resting_units=price_map.get(ask, 0),
            delta_units=change,
            result_units=new_size,
        )
        raise BookInvalidated(
            f"{ticker}: level {ask} would go to {new_size} units"
        )
    if new_size > 0:
        price_map[ask] = new_size
    else:
        # Exactly zero is a legitimate full removal, not a fault.
        price_map.pop(ask, None)
    updated = [PriceLevel(price=p, quantity_units=q) for p, q in price_map.items()]

    if side == "no":
        book.yes = updated
    else:
        book.no = updated
    return True
