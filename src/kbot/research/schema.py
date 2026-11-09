"""
Pydantic schema for a single research digest.

The agent returns one ResearchDigest per topic.  The schema is strict:
the agent is instructed to emit only valid JSON matching this structure,
so the orchestrator can parse and route it without any prose handling.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """One factual claim found by the agent, with its evidence quality."""
    claim: str
    source_url: str = ""
    impact: Literal["low", "medium", "high"] = "low"


class RiskFlag(BaseModel):
    """A machine-readable advisory flag and the action it implies."""
    flag: str   # snake_case, e.g. "api_breaking_change_detected"
    action: str  # human-readable recommendation


class ResearchDigest(BaseModel):
    """
    Structured output from one research agent run on one topic.

    Designed to be machine-parseable: no free-form prose outside
    the designated string fields.
    """
    topic: str
    generated_utc: datetime
    summary: str = Field(description="1-3 sentence factual summary")
    findings: list[Finding] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    recommended_human_action: str = ""

    def has_high_impact(self) -> bool:
        return any(f.impact == "high" for f in self.findings)

    def is_actionable(self) -> bool:
        """True if the digest warrants a human alert."""
        return self.has_high_impact() or bool(self.risk_flags)
