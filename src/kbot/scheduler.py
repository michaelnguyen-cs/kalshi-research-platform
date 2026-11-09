"""
APScheduler-based daemon — runs research + evaluate in a single long-lived process.

Scheduled jobs:
  - research   every RESEARCH_INTERVAL_HOURS
  - evaluate   every LEARN_EVAL_INTERVAL_HOURS

Retrain stays manual (or weekly) by default so model changes are deliberate.

Windows note: if you don't want a persistent terminal, configure Windows Task
Scheduler to run these commands on their intervals instead:
  kbot research --once
  kbot learn --evaluate
"""

from __future__ import annotations

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler

from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)


def _research_job(cfg: Settings) -> None:
    from kbot.alerts import Alerter
    from kbot.research.agent import ResearchAgent
    from kbot.research.flags import update_from_digest
    from kbot.research.sinks import write_digest
    from kbot.sheets import SheetsClient
    from kbot.telemetry import Telemetry

    log.info("scheduler.research.start")
    try:
        agent   = ResearchAgent(cfg)
        alerter = Alerter(cfg)
        sheets  = SheetsClient(cfg)
        with Telemetry(cfg) as tel:
            for digest in agent.run_all():
                write_digest(digest, telemetry=tel, sheets=sheets, alerter=alerter)
                update_from_digest(digest)
        log.info("scheduler.research.done")
    except Exception as exc:  # noqa: BLE001
        log.error("scheduler.research.error", error=str(exc)[:300])


def _current_balance(cfg: Settings) -> float | None:
    """Current account balance in dollars for the Live P&L tab, or None. Never raises."""
    from kbot.kalshi_client import KalshiClient
    from kbot.live_pnl import balance_dollars

    try:
        with KalshiClient(cfg) as client:
            return balance_dollars(client.get_balance())
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduler.balance_unavailable", error=str(exc)[:200])
        return None


def _evaluate_job(cfg: Settings) -> None:
    from kbot.learning.evaluate import performance_report, write_report
    from kbot.sheets import SheetsClient
    from kbot.telemetry import Telemetry

    log.info("scheduler.evaluate.start")
    try:
        # Strategy-scoped so this deployment's tabs are its own -- see
        # kbot.sheets.tab_for.
        sheets = SheetsClient.for_settings(cfg)
        with Telemetry(cfg) as tel:
            report = performance_report(tel, cfg=cfg)
            write_report(report, tel, sheets=sheets, balance=_current_balance(cfg))
        log.info(
            "scheduler.evaluate.done",
            trades=report.get("total_trades"),
            brier=report.get("brier_score"),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("scheduler.evaluate.error", error=str(exc)[:300])


def start_daemon(cfg: Settings | None = None) -> None:
    """
    Start the blocking APScheduler daemon.  Ctrl-C to stop.

    Windows alternative: use Task Scheduler to run:
      kbot research --once       (every RESEARCH_INTERVAL_HOURS)
      kbot learn --evaluate      (every LEARN_EVAL_INTERVAL_HOURS)
    without a persistent terminal.
    """
    cfg = cfg or _default_settings

    if not cfg.dry_run:
        log.warning("scheduler.live_mode_active")

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        _research_job,
        trigger="interval",
        hours=cfg.research_interval_hours,
        args=[cfg],
        id="research",
        name="Research agent",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _evaluate_job,
        trigger="interval",
        hours=cfg.learn_eval_interval_hours,
        args=[cfg],
        id="evaluate",
        name="Learning evaluate",
        misfire_grace_time=300,
    )

    log.info(
        "scheduler.daemon.starting",
        research_interval_h=cfg.research_interval_hours,
        eval_interval_h=cfg.learn_eval_interval_hours,
        dry_run=cfg.dry_run,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler.daemon.stopped")
