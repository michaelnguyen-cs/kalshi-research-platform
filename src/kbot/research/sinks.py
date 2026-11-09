"""
Research digest sinks — persist each digest and alert if warranted.

ARCHITECTURE: this module imports from telemetry, sheets, alerts.
It MUST NOT import from kbot.execution — the research agent cannot trade.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from kbot.alerts import Alerter
    from kbot.research.schema import ResearchDigest
    from kbot.sheets import SheetsClient
    from kbot.telemetry import Telemetry

log = structlog.get_logger(__name__)


def write_digest(
    digest: "ResearchDigest",
    *,
    telemetry: "Telemetry | None" = None,
    sheets: "SheetsClient | None" = None,
    alerter: "Alerter | None" = None,
) -> None:
    """
    Persist a digest to all configured sinks.

    Any sink failure is logged and absorbed — the caller always returns.
    If any finding has impact=high OR risk_flags is non-empty,
    alerter.send_critical is called.
    """
    if telemetry is not None:
        _write_db(digest, telemetry)
    if sheets is not None:
        _write_sheets(digest, sheets)
    if alerter is not None and digest.is_actionable():
        _alert(digest, alerter)


def _write_db(digest: "ResearchDigest", telemetry: "Telemetry") -> None:
    try:
        telemetry.log_research_digest(
            topic=digest.topic,
            generated_utc=digest.generated_utc.isoformat(),
            summary=digest.summary,
            findings=[f.model_dump() for f in digest.findings],
            risk_flags=[r.model_dump() for r in digest.risk_flags],
            recommended_human_action=digest.recommended_human_action,
        )
        log.debug("research.sink.db_ok", topic=digest.topic)
    except Exception as exc:  # noqa: BLE001
        log.error("research.sink.db_failed", topic=digest.topic, error=str(exc)[:200])


def _write_sheets(digest: "ResearchDigest", sheets: "SheetsClient") -> None:
    try:
        row = {
            "ts_utc":              datetime.now(tz=timezone.utc).isoformat(),
            "topic":               digest.topic,
            "summary":             digest.summary,
            "risk_flags":          [r.model_dump() for r in digest.risk_flags],
            "recommended_action":  digest.recommended_human_action,
            "raw_json":            digest.model_dump_json(),
        }
        sheets.append_research(row)
        log.debug("research.sink.sheets_ok", topic=digest.topic)
    except Exception as exc:  # noqa: BLE001
        log.error("research.sink.sheets_failed", topic=digest.topic, error=str(exc)[:200])


def _alert(digest: "ResearchDigest", alerter: "Alerter") -> None:
    try:
        flags_str = ", ".join(rf.flag for rf in digest.risk_flags) if digest.risk_flags else "—"
        high = [f for f in digest.findings if f.impact == "high"]
        findings_str = "; ".join(f.claim[:120] for f in high[:3]) if high else "—"
        msg = (
            f"[RESEARCH] {digest.topic}\n"
            f"{digest.summary[:200]}\n"
            f"High-impact findings: {findings_str}\n"
            f"Risk flags: {flags_str}\n"
            f"Action: {digest.recommended_human_action[:200]}"
        )
        alerter.send_critical(msg)
        log.warning("research.sink.alert_sent", topic=digest.topic, flags=flags_str)
    except Exception as exc:  # noqa: BLE001
        log.error("research.sink.alert_failed", topic=digest.topic, error=str(exc)[:200])
