"""
Periodic performance report from the trade journal.

Answers: "Are we actually improving?"  Reads trade_outcome events from
SQLite, computes calibration metrics and P&L, and writes the results back
to SQLite + optionally to the Calibration/Daily Summary sheet tabs.

A trade_outcome event is logged whenever a position is closed.  Its payload:
  {ticker, side, entry_price, model_prob, net_edge_dollars, contracts,
   pnl_dollars, kelly_fraction_used, strategy, fee_dollars (optional)}
"""

from __future__ import annotations

from typing import Any

import numpy as np
import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.learning.calibration import brier_score, reliability_table

log = structlog.get_logger(__name__)


def load_outcomes(telemetry: Any, limit: int = 10_000) -> list[dict]:
    """Return all trade_outcome events as payload dicts, oldest first."""
    events = telemetry.recent(event_type="trade_outcome", limit=limit)
    # recent() returns newest-first; reverse for chronological order
    return [e["payload"] for e in reversed(events)]


def performance_report(telemetry: Any, cfg: Settings | None = None) -> dict:
    """
    Compute calibration + performance metrics from trade_outcome events.

    Returns a dict safe to log and print.  All numeric fields are rounded.
    """
    cfg = cfg or _default_settings
    outcomes = load_outcomes(telemetry)

    if not outcomes:
        return {
            "total_trades":    0,
            "wins":            0,
            "losses":          0,
            "win_rate":        None,
            "gross_pnl":       0.0,
            "net_pnl":         0.0,
            "brier_score":     None,
            "calibration_buckets": [],
            "avg_predicted_edge":  None,
            "avg_realized_pnl":    None,
            "edge_prediction_err": None,
            "note": "no_trade_outcomes_yet",
        }

    pnls        = [o.get("pnl_dollars", 0.0) for o in outcomes]
    model_probs = [o.get("model_prob", 0.5)   for o in outcomes]
    # Only trades that actually recorded a predicted edge -- defaulting the
    # missing ones to 0.0 dragged avg_predicted_edge to $0.00 for as long as
    # the field wasn't being persisted (and would still understate it for
    # legacy rows predating the fix).
    pred_edges  = [o["net_edge_dollars"] for o in outcomes if o.get("net_edge_dollars") is not None]
    won         = [1 if p > 0 else 0 for p in pnls]

    total    = len(outcomes)
    n_wins   = sum(won)
    gross    = round(float(np.sum(pnls)), 4)
    # pnl_dollars is net-of-fee by convention
    net      = gross

    bs: float | None = None
    buckets: list[dict] = []
    if total >= cfg.calibration_min_samples:
        bs = round(brier_score(model_probs, won), 6)
        buckets = reliability_table(model_probs, won)

    avg_pred_edge = round(float(np.mean(pred_edges)), 4) if pred_edges else None
    avg_pnl       = round(net / total, 4) if total > 0 else None
    edge_err      = (
        round(avg_pred_edge - avg_pnl, 4)
        if (avg_pred_edge is not None and avg_pnl is not None)
        else None
    )

    # P&L by strategy
    by_strategy: dict[str, dict] = {}
    for o, pnl in zip(outcomes, pnls):
        strat = o.get("strategy", "unknown")
        if strat not in by_strategy:
            by_strategy[strat] = {"n": 0, "pnl": 0.0}
        by_strategy[strat]["n"]   += 1
        by_strategy[strat]["pnl"] = round(by_strategy[strat]["pnl"] + pnl, 4)

    return {
        "total_trades":        total,
        "wins":                n_wins,
        "losses":              total - n_wins,
        "win_rate":            round(n_wins / total, 4) if total > 0 else None,
        "gross_pnl":           gross,
        "net_pnl":             net,
        "brier_score":         bs,
        "calibration_buckets": buckets,
        "avg_predicted_edge":  avg_pred_edge,
        "avg_realized_pnl":    avg_pnl,
        "edge_prediction_err": edge_err,
        "by_strategy":         by_strategy,
    }


def write_report(
    report: dict,
    telemetry: Any,
    sheets: Any = None,
    *,
    balance: float | None = None,
) -> None:
    """
    Persist the performance report to SQLite and optionally push calibration
    buckets, the performance summary and real-money P&L to the Google Sheet.

    `balance` is the current Kalshi account balance, stamped on Live P&L rows
    appended by this run so the drift between the bot's own running total and
    the real account is visible. Omitted -> those cells are left empty rather
    than filled with a placeholder.

    Every sheet push here is independently guarded: a failure in one must not
    stop the others, and none of them may propagate to the caller. All three
    are also idempotent -- calibration and performance are clear-and-rewrite,
    Live P&L upserts by trade -- so re-running evaluate never duplicates a row
    or inflates the running total.
    """
    try:
        payload = {k: v for k, v in report.items() if k != "calibration_buckets"}
        telemetry.log_event("evaluate_report", payload)
        log.info(
            "evaluate.report_written",
            total_trades=report.get("total_trades"),
            brier=report.get("brier_score"),
            net_pnl=report.get("net_pnl"),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("evaluate.write_failed", error=str(exc)[:200])

    if sheets is None:
        return

    if report.get("calibration_buckets"):
        try:
            sheets.push_calibration(report["calibration_buckets"])
        except Exception as exc:  # noqa: BLE001
            log.error("evaluate.sheets_push_failed", error=str(exc)[:200])

    try:
        sheets.push_performance(report)
    except Exception as exc:  # noqa: BLE001
        log.error("evaluate.performance_push_failed", error=str(exc)[:200])

    try:
        from kbot.live_pnl import push_live_pnl
        counts = push_live_pnl(telemetry, sheets, balance=balance)
        if counts.get("appended") or counts.get("updated"):
            log.info("evaluate.live_pnl_pushed", **counts)
    except Exception as exc:  # noqa: BLE001
        log.error("evaluate.live_pnl_push_failed", error=str(exc)[:200])
