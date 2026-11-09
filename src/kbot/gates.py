"""
Strategy gate registry — RULE R3 kill-fast (MARKET_RESEARCH.md Source 4).

The MAXIMUS/North Lake Labs postmortem (0 wins, 32 losses on a weather
strategy nearly identical to our C1 spec) is explicit: "kill at 10, not 32."
Any strategy that goes 0-for-10 on resolved trades, at any point, is
auto-paused — demoted one gate level, an advisory alert fired, and it stays
paused until a human runs `kbot gates promote <strategy>`. This is
deliberately NOT self-healing: a losing streak does not get to argue its
way back in without a person looking at it.

The second trigger is a backtest-relative check: once a strategy has 20
resolved live trades, if its average realized P&L per trade over those 20
is below the 5th percentile of its OWN backtest per-trade P&L distribution,
that's a sign live performance is diverging badly from what backtesting
promised — also auto-paused. The backtest distribution is persisted via
register_backtest() at backtest-gate time (i.e. when a strategy is promoted
from backtest to paper), so this check has something to compare against.

Gate levels (lowest to highest): backtest -> paper -> live_small -> live_full.
demote() moves down one level and pauses; promote() moves up one level and
un-pauses. Auto-demotion always pauses regardless of which level it lands
on — "demoted" and "paused" happen together for a kill-fast trigger.

This module never raises into a caller: check_kill_fast() catches and logs
any internal failure, because a monitoring bug must never crash the trading
loop it's supposed to be protecting.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.telemetry import Telemetry

log = structlog.get_logger(__name__)

# Ordered gate levels, lowest to highest.
LEVELS: tuple[str, ...] = ("backtest", "paper", "live_small", "live_full")

# RULE R3 thresholds.
_ZERO_FOR_N = 10          # 0-for-10 resolved trades -> auto-pause
_PERCENTILE_CHECK_N = 20  # evaluate the backtest-percentile rule at this many resolved trades
_BACKTEST_PERCENTILE = 5  # 5th percentile of the strategy's own backtest P&L distribution

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS strategy_gates (
    strategy               TEXT PRIMARY KEY,
    level                  TEXT NOT NULL,
    paused                 INTEGER NOT NULL DEFAULT 0,
    backtest_pnl_summary   TEXT,
    notes                  TEXT,
    updated_at             REAL NOT NULL,
    backtest_registered_at REAL
);
"""

# Paper-gate promotion thresholds (backtest -> paper), enforced by
# check_paper_promotion(). Deliberately conservative: paper trading is the
# only place a strategy proves out on live prices before real money, so the
# bar to leave it must be earned, not clicked past.
_PAPER_MIN_DAYS = 14.0             # calendar days of paper trading since backtest registration
_PAPER_MIN_RESOLVED_TRADES = 50    # resolved (settled) paper trades required
_KILL_FAST_LOOKBACK_DAYS = 7.0     # a kill-fast trip this recently blocks promotion
_SECONDS_PER_DAY = 86400.0


# ── Percentile helpers ───────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy-default style) on pre-sorted values."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100.0) * (len(sorted_values) - 1)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[int(f)] * (c - k) + sorted_values[int(c)] * (k - f)


def pnl_distribution_summary(pnl_series: list[float]) -> dict:
    """Summary stats (n, mean, p5/p25/p50/p75, min, max) of a P&L series."""
    if not pnl_series:
        return {"n": 0}
    values = sorted(pnl_series)
    return {
        "n": len(values),
        "mean": sum(values) / len(values),
        "p5": _percentile(values, 5),
        "p25": _percentile(values, 25),
        "p50": _percentile(values, 50),
        "p75": _percentile(values, 75),
        "min": values[0],
        "max": values[-1],
    }


# ── Registry ─────────────────────────────────────────────────────────────────

@dataclass
class StrategyGate:
    strategy: str
    level: str
    paused: bool
    backtest_pnl_summary: dict | None
    notes: str
    updated_at: float
    backtest_registered_at: float | None = None

    @property
    def backtest_pnl_p5(self) -> float | None:
        if not self.backtest_pnl_summary:
            return None
        return self.backtest_pnl_summary.get("p5")


class GateRegistry:
    """
    Per-strategy gate level + pause state + backtest P&L distribution.

    Separate SQLite file (kbot_gates.db) — gate state is operational
    configuration, not part of the append-only event log.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        # Explicit db_path wins; otherwise cfg.gates_db_path (default
        # "kbot_gates.db" -- unchanged from when it was hardcoded here), so a
        # second deployment gets its own promotion clock and pause state.
        from kbot.config import settings as _cfg
        path = Path(db_path) if db_path else Path(_cfg.gates_db_path)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_CREATE_SQL)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """
        Add columns introduced after a DB was first created. CREATE TABLE IF
        NOT EXISTS won't alter an existing table, so a gates.db from before
        backtest_registered_at existed needs the column added and backfilled.
        Existing rows are anchored to their updated_at (the best available
        proxy for when they were last registered) so the 14-day paper-gate
        clock has a sane, non-null start for strategies registered earlier.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(strategy_gates)").fetchall()}
        if "backtest_registered_at" not in cols:
            self._conn.execute("ALTER TABLE strategy_gates ADD COLUMN backtest_registered_at REAL")
            self._conn.execute(
                "UPDATE strategy_gates SET backtest_registered_at = updated_at "
                "WHERE backtest_registered_at IS NULL"
            )

    # ── Writes ───────────────────────────────────────────────────────────────

    def register_backtest(self, strategy: str, pnl_series: list[float], notes: str = "") -> None:
        """
        Persist `strategy`'s backtest per-trade P&L distribution — this is
        what the RULE R3 percentile check compares live performance to.
        Call this at backtest-gate time (when a strategy is first promoted
        out of "backtest").
        """
        summary = pnl_distribution_summary(pnl_series)
        gate = self.get_gate(strategy)
        self._upsert(
            strategy,
            level=gate.level if gate else LEVELS[0],
            paused=gate.paused if gate else False,
            backtest_pnl_summary=summary,
            notes=notes or (gate.notes if gate else ""),
            # Anchor the paper-gate 14-day clock at the FIRST backtest
            # registration; _upsert COALESCEs so re-registering (e.g. to
            # refresh the P&L distribution) never resets it.
            backtest_registered_at=time.time(),
        )
        log.info(
            "gates.backtest_registered",
            strategy=strategy,
            n=summary.get("n"),
            p5=summary.get("p5"),
        )

    def demote(self, strategy: str, reason: str) -> str:
        """Move `strategy` down one gate level and pause it. Returns the new level."""
        gate = self.get_gate(strategy)
        current = gate.level if gate else LEVELS[0]
        idx = LEVELS.index(current) if current in LEVELS else 0
        new_level = LEVELS[max(0, idx - 1)]
        self._upsert(
            strategy,
            level=new_level,
            paused=True,
            backtest_pnl_summary=gate.backtest_pnl_summary if gate else None,
            notes=f"kill_fast:{reason}",
        )
        log.critical(
            "gates.demoted", strategy=strategy, from_level=current, to_level=new_level, reason=reason
        )
        return new_level

    def promote(self, strategy: str) -> str:
        """Manually move `strategy` up one gate level and clear the pause. Returns the new level."""
        gate = self.get_gate(strategy)
        current = gate.level if gate else LEVELS[0]
        idx = LEVELS.index(current) if current in LEVELS else 0
        new_level = LEVELS[min(len(LEVELS) - 1, idx + 1)]
        self._upsert(
            strategy,
            level=new_level,
            paused=False,
            backtest_pnl_summary=gate.backtest_pnl_summary if gate else None,
            notes="manual_promote",
        )
        log.info("gates.promoted", strategy=strategy, to_level=new_level)
        return new_level

    # ── Reads ────────────────────────────────────────────────────────────────

    def get_gate(self, strategy: str) -> StrategyGate | None:
        row = self._conn.execute(
            "SELECT strategy, level, paused, backtest_pnl_summary, notes, updated_at, backtest_registered_at "
            "FROM strategy_gates WHERE strategy = ?",
            (strategy,),
        ).fetchone()
        return self._row_to_gate(row) if row else None

    def is_paused(self, strategy: str) -> bool:
        gate = self.get_gate(strategy)
        return gate.paused if gate else False

    def list_gates(self) -> list[StrategyGate]:
        rows = self._conn.execute(
            "SELECT strategy, level, paused, backtest_pnl_summary, notes, updated_at, backtest_registered_at "
            "FROM strategy_gates ORDER BY strategy"
        ).fetchall()
        return [self._row_to_gate(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "GateRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _upsert(
        self,
        strategy: str,
        *,
        level: str,
        paused: bool,
        backtest_pnl_summary: dict | None,
        notes: str,
        backtest_registered_at: float | None = None,
    ) -> None:
        # backtest_registered_at is COALESCEd, not overwritten: only
        # register_backtest passes a value, and it's kept from the FIRST time
        # (so demote/promote/note edits never move the paper-gate clock).
        self._conn.execute(
            """
            INSERT INTO strategy_gates
                (strategy, level, paused, backtest_pnl_summary, notes, updated_at, backtest_registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy) DO UPDATE SET
                level = excluded.level,
                paused = excluded.paused,
                backtest_pnl_summary = excluded.backtest_pnl_summary,
                notes = excluded.notes,
                updated_at = excluded.updated_at,
                backtest_registered_at =
                    COALESCE(strategy_gates.backtest_registered_at, excluded.backtest_registered_at)
            """,
            (
                strategy,
                level,
                int(paused),
                json.dumps(backtest_pnl_summary) if backtest_pnl_summary is not None else None,
                notes,
                time.time(),
                backtest_registered_at,
            ),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_gate(row: tuple) -> StrategyGate:
        strategy, level, paused, summary_json, notes, updated_at, backtest_registered_at = row
        return StrategyGate(
            strategy=strategy,
            level=level,
            paused=bool(paused),
            backtest_pnl_summary=json.loads(summary_json) if summary_json else None,
            notes=notes or "",
            updated_at=updated_at,
            backtest_registered_at=backtest_registered_at,
        )


# ── Kill-fast evaluation ─────────────────────────────────────────────────────

def check_kill_fast(
    strategy: str,
    telemetry: Telemetry,
    registry: GateRegistry,
    cfg: Settings | None = None,
) -> str | None:
    """
    Evaluate RULE R3 for `strategy` against its resolved trade_outcome
    history. Demotes + fires an advisory alert if a kill-fast condition
    fires.

    Returns the trigger reason ("zero_for_ten" / "below_backtest_p5") if it
    just triggered, else None (including "already paused" and "no data yet").
    Never raises.
    """
    cfg = cfg or _default_settings
    try:
        if registry.is_paused(strategy):
            return None  # already paused -- nothing new to trigger

        trades = telemetry.resolved_trades(strategy, limit=1000)  # oldest first

        if len(trades) >= _ZERO_FOR_N:
            last_n = trades[-_ZERO_FOR_N:]
            if all(float(t.get("pnl_dollars", 0.0)) <= 0 for t in last_n):
                _trigger(strategy, registry, telemetry, cfg, "zero_for_ten")
                return "zero_for_ten"

        if len(trades) >= _PERCENTILE_CHECK_N:
            gate = registry.get_gate(strategy)
            p5 = gate.backtest_pnl_p5 if gate else None
            if p5 is not None:
                last_n = trades[-_PERCENTILE_CHECK_N:]
                avg_pnl = sum(float(t.get("pnl_dollars", 0.0)) for t in last_n) / len(last_n)
                if avg_pnl < p5:
                    _trigger(strategy, registry, telemetry, cfg, "below_backtest_p5")
                    return "below_backtest_p5"

        return None
    except Exception as exc:  # noqa: BLE001 — monitoring must never crash the trading loop
        log.error("gates.kill_fast_check_failed", strategy=strategy, error=str(exc))
        return None


def _trigger(strategy: str, registry: GateRegistry, telemetry: Telemetry, cfg: Settings, reason: str) -> None:
    new_level = registry.demote(strategy, reason)
    # Persist the trip so check_paper_promotion can enforce "no kill-fast in
    # the last 7 days" from a separate process (the CLI). The demote() note is
    # overwritten by later writes and only reflects the latest state, so it
    # can't serve as a trip history -- this append-only event can.
    try:
        telemetry.log_event("kill_fast", {"strategy": strategy, "reason": reason, "demoted_to": new_level})
    except Exception as exc:  # noqa: BLE001
        log.error("gates.kill_fast_log_failed", strategy=strategy, error=str(exc))
    message = (
        f"KILL-FAST: strategy '{strategy}' auto-paused (reason={reason}, "
        f"demoted to '{new_level}'). Run `kbot gates promote {strategy}` after review."
    )
    try:
        from kbot.alerts import Alerter
        with Alerter(cfg) as alerter:
            alerter.send_critical(message)
    except Exception as exc:  # noqa: BLE001
        log.error("gates.alert_failed", strategy=strategy, error=str(exc))


# ── Paper-gate promotion check (backtest -> paper) ───────────────────────────

@dataclass
class PromotionCheck:
    """Result of check_paper_promotion: passed, plus every failed criterion."""
    passed: bool
    failures: list[str]


def check_paper_promotion(
    strategy: str,
    registry: GateRegistry,
    telemetry: Telemetry,
    cfg: Settings | None = None,
    *,
    now: float | None = None,
) -> PromotionCheck:
    """
    Verify ALL backtest->paper promotion criteria for `strategy`. Returns a
    PromotionCheck whose `failures` names exactly which criteria did not pass
    (empty iff promotion is allowed). Purely read-only -- it never changes the
    gate; the caller decides what to do with the result.

    Criteria (all required):
      1. >= 14 calendar days since the strategy was registered at backtest
         (gate.backtest_registered_at, falling back to updated_at for rows
         predating that column).
      2. >= 50 resolved paper trades (settled dry-run trade_outcomes).
      3. Positive fee-adjusted paper P&L over those trades (pnl_dollars is
         net-of-fee by the codebase's convention -- see learning/evaluate.py).
      4. No kill-fast trip in the last 7 days (persisted kill_fast events).
      5. adverse_selection_detected not currently set (recomputed from the
         persisted markout history, so it's valid from a fresh CLI process --
         the in-memory is_flagged() would always read False there).
    """
    cfg = cfg or _default_settings
    now = time.time() if now is None else now
    failures: list[str] = []

    # 1. Time in backtest/paper.
    gate = registry.get_gate(strategy)
    registered_at = None
    if gate is not None:
        registered_at = gate.backtest_registered_at if gate.backtest_registered_at is not None else gate.updated_at
    if registered_at is None:
        failures.append("no backtest registration on record (register_backtest was never called)")
    else:
        days = (now - registered_at) / _SECONDS_PER_DAY
        if days < _PAPER_MIN_DAYS:
            failures.append(
                f"only {days:.1f} of {_PAPER_MIN_DAYS:.0f} required days since backtest registration"
            )

    # 2 & 3. Resolved paper trades and their P&L.
    trades = telemetry.resolved_paper_trades(strategy)
    n = len(trades)
    if n < _PAPER_MIN_RESOLVED_TRADES:
        failures.append(f"only {n} of {_PAPER_MIN_RESOLVED_TRADES} required resolved paper trades")
    total_pnl = sum(float(t.get("pnl_dollars", 0.0)) for t in trades)
    if total_pnl <= 0.0:
        failures.append(f"fee-adjusted paper P&L is {total_pnl:+.2f} (must be > 0)")

    # 4. Kill-fast trips in the last 7 days.
    since = now - _KILL_FAST_LOOKBACK_DAYS * _SECONDS_PER_DAY
    recent_trips = [
        e for e in telemetry.recent_types(["kill_fast"])
        if e["payload"].get("strategy") == strategy and e["ts"] >= since
    ]
    if recent_trips:
        failures.append(
            f"{len(recent_trips)} kill-fast trip(s) in the last {_KILL_FAST_LOOKBACK_DAYS:.0f} days"
        )

    # 5. Adverse-selection flag (recomputed from persisted markouts).
    try:
        from kbot.analytics.adverse_selection import evaluate_flag
        if evaluate_flag(telemetry, strategy, cfg):
            failures.append("adverse_selection_detected flag is currently set")
    except Exception as exc:  # noqa: BLE001 -- never silently pass the gate on an analytics error
        failures.append(f"could not evaluate adverse-selection flag ({type(exc).__name__})")

    return PromotionCheck(passed=not failures, failures=failures)
