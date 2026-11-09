"""
Research orchestrator — runs web-search queries via the Anthropic API and
returns structured ResearchDigest objects.

ARCHITECTURE: this module MUST NOT import from kbot.execution.
The research agent is advisory only and cannot place trades.
This constraint is enforced by tests/test_research_isolation.py.

"Self-learning" design note: the research agent's findings are advisory.
LLM sentiment is NOT wired into sizing or the edge calc in v1.  Any future
experiment where research feeds into the model must go through the same
backtest/validation gate as any parameter change, behind a config flag
that defaults to off.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.research.schema import Finding, ResearchDigest, RiskFlag
from kbot.research.topics import ALL_TOPICS, TopicConfig

log = structlog.get_logger(__name__)

# ── Prompt ────────────────────────────────────────────────────────────────────

_SCHEMA_HINT = """{
  "topic": "<topic_id string>",
  "generated_utc": "<ISO 8601 timestamp>",
  "summary": "<1-3 sentence factual summary>",
  "findings": [
    {"claim": "<specific claim>", "source_url": "<url or empty>", "impact": "low|medium|high"}
  ],
  "risk_flags": [
    {"flag": "<snake_case_flag>", "action": "<recommended action>"}
  ],
  "recommended_human_action": "<clear action for the operator>"
}"""

_SYSTEM_PROMPT = f"""You are a financial research assistant for a prediction market trading bot.
Your task: search for recent information relevant to the topic, then return a structured report.

Use the web_search tool to find up-to-date information.

CRITICAL: Return ONLY the JSON object below — no markdown, no code fences, no preamble, no prose.
The entire response must be valid JSON parseable by json.loads().

Schema:
{_SCHEMA_HINT}

Rules:
- impact must be exactly "low", "medium", or "high"
- Only mark impact="high" for concrete evidence of a significant event (not speculation)
- findings must be factual claims from your search, not inferences
- risk_flags snake_case names — e.g. "api_breaking_change", "exchange_outage"
- If nothing significant found, return empty findings and risk_flags arrays
- recommended_human_action must be a concrete instruction, not vague"""


class ResearchAgent:
    """
    Runs one Anthropic API call per topic with web search enabled,
    parses the JSON response into a ResearchDigest, and returns it.

    Cannot trade — imports nothing from kbot.execution.
    """

    def __init__(self, cfg: Settings | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._client: Any = None

    # ── Client init ───────────────────────────────────────────────────────────

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import anthropic
        if not self._cfg.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set in .env — research agent cannot run."
            )
        self._client = anthropic.Anthropic(api_key=self._cfg.anthropic_api_key)
        return self._client

    # ── Public API ────────────────────────────────────────────────────────────

    def run_topic(self, topic: TopicConfig) -> ResearchDigest | None:
        """
        Run a single topic.  Returns None on any error — never raises.
        """
        try:
            client = self._get_client()
            model = getattr(self._cfg, "research_model", "claude-sonnet-4-6")
            user_content = (
                f"Topic ID: {topic.topic_id}\n"
                f"Topic name: {topic.display_name}\n\n"
                f"Search guidance:\n{topic.search_guidance}\n\n"
                "Search now and return the JSON digest."
            )
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=_SYSTEM_PROMPT,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 4,
                }],
                messages=[{"role": "user", "content": user_content}],
            )
            return self._parse_response(response, topic)
        except Exception as exc:  # noqa: BLE001
            log.error("research.agent.topic_failed", topic=topic.topic_id, error=str(exc)[:300])
            return None

    def run_all(self) -> list[ResearchDigest]:
        """
        Run every topic in ALL_TOPICS.  Per-topic failures are logged and skipped.
        """
        results: list[ResearchDigest] = []
        for topic in ALL_TOPICS:
            log.info("research.agent.topic_start", topic=topic.topic_id)
            digest = self.run_topic(topic)
            if digest is not None:
                results.append(digest)
                log.info(
                    "research.agent.topic_done",
                    topic=topic.topic_id,
                    findings=len(digest.findings),
                    flags=len(digest.risk_flags),
                    high_impact=digest.has_high_impact(),
                )
            else:
                log.warning("research.agent.topic_skipped", topic=topic.topic_id)
        log.info("research.agent.run_all_done", ok=len(results), total=len(ALL_TOPICS))
        return results

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, response: Any, topic: TopicConfig) -> ResearchDigest | None:
        """
        Extract the last text block from an Anthropic response and parse it
        as a ResearchDigest JSON.  Returns a fallback digest on parse failure
        so callers always get a usable object.
        """
        text_blocks = [b.text for b in response.content if hasattr(b, "text")]
        if not text_blocks:
            log.warning("research.agent.no_text", topic=topic.topic_id)
            return _fallback(topic, "empty response")

        raw = text_blocks[-1].strip()

        # Strip markdown code fences in case the model ignored the instruction
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE)
        raw = raw.strip()

        # Extract outermost JSON object if there is surrounding text
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            log.warning("research.agent.no_json", topic=topic.topic_id, preview=raw[:150])
            return _fallback(topic, f"no JSON found: {raw[:80]}")

        try:
            data = json.loads(m.group(0))
            data.setdefault("topic", topic.topic_id)
            data.setdefault("generated_utc", datetime.now(tz=timezone.utc).isoformat())
            return ResearchDigest.model_validate(data)
        except Exception as exc:
            log.warning(
                "research.agent.parse_error",
                topic=topic.topic_id,
                error=str(exc)[:200],
                preview=raw[:150],
            )
            return _fallback(topic, f"parse error: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fallback(topic: TopicConfig, note: str) -> ResearchDigest:
    """Return a minimal valid digest when the API response cannot be parsed."""
    return ResearchDigest(
        topic=topic.topic_id,
        generated_utc=datetime.now(tz=timezone.utc),
        summary=f"[Research agent parse failure] {note}",
        findings=[],
        risk_flags=[],
        recommended_human_action="Check research agent logs.",
    )
