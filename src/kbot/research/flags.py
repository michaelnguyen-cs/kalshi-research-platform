"""
Advisory flag state for the research agent.

This module is intentionally a leaf: it imports NOTHING from the trading
stack (not execution, not risk, not edge, not kalshi_client).

Data flow:
  research/agent.py → research/flags.py (sets flags)
  execution.py      → research/flags.py (reads flags, deferred import)

This one-way flow prevents import cycles and keeps the research agent
unable to influence order placement except through the flag read in
execution.py's safety gauntlet.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from kbot.research.schema import ResearchDigest

log = structlog.get_logger(__name__)

# ── Flag registry ─────────────────────────────────────────────────────────────

_LOCK = threading.Lock()

# All valid advisory flags and their initial (clear) state.
_FLAGS: dict[str, bool] = {
    "api_change_detected":        False,  # Kalshi API has a breaking change
    "exchange_incident":          False,  # exchange outage / operational issue
    "high_impact_event_imminent": False,  # major macro/crypto event likely to move prices
}


# ── Write interface (called by research sinks) ────────────────────────────────

def set_flag(name: str, value: bool = True) -> None:
    with _LOCK:
        if name not in _FLAGS:
            log.warning("research.flags.unknown", name=name)
            _FLAGS[name] = value
        else:
            _FLAGS[name] = value
        if value:
            log.warning("research.advisory_flag_raised", flag=name)


def update_from_digest(digest: "ResearchDigest") -> None:
    """
    Translate a ResearchDigest into advisory state.

    Rules applied in order:
    - High-impact finding from a topic that lists advisory_flags_on_high
      → set those flags.
    - risk_flags whose .flag field contains "api" or "breaking"
      → set api_change_detected.
    - risk_flags containing "exchange", "outage", or "incident"
      → set exchange_incident.
    - Any high-impact finding on any topic
      → set high_impact_event_imminent as a catch-all.
    """
    # Import topics here to avoid a top-level circular dependency
    from kbot.research.topics import TOPICS_BY_ID

    topic_cfg = TOPICS_BY_ID.get(digest.topic)
    if topic_cfg and digest.has_high_impact():
        for flag_name in topic_cfg.advisory_flags_on_high:
            set_flag(flag_name, True)

    for rf in digest.risk_flags:
        lower = rf.flag.lower()
        if "api" in lower or "breaking" in lower:
            set_flag("api_change_detected", True)
        if "exchange" in lower or "outage" in lower or "incident" in lower:
            set_flag("exchange_incident", True)

    # Catch-all: any high-impact finding is at minimum a general advisory
    if digest.has_high_impact():
        set_flag("high_impact_event_imminent", True)


# ── Read interface (called by execution.py) ───────────────────────────────────

def get_flag(name: str) -> bool:
    with _LOCK:
        return _FLAGS.get(name, False)


def any_advisory_flag_set() -> bool:
    with _LOCK:
        return any(_FLAGS.values())


def active_flags() -> list[str]:
    with _LOCK:
        return [k for k, v in _FLAGS.items() if v]


# ── Test / reset helper ───────────────────────────────────────────────────────

def clear_all_flags() -> None:
    """Reset all flags to False.  Used in tests and on clean startup."""
    with _LOCK:
        for k in _FLAGS:
            _FLAGS[k] = False
    log.debug("research.flags.cleared")
