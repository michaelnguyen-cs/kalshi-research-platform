"""
Research topic registry.

Each TopicConfig describes what to search for and which advisory flags
to raise when high-impact findings are returned.

Adding a new topic: append a TopicConfig to ALL_TOPICS.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TopicConfig:
    topic_id: str
    display_name: str
    search_guidance: str
    # Advisory flags to set in research.flags when this topic returns
    # any finding with impact="high".
    advisory_flags_on_high: list[str] = field(default_factory=list)


# ── Canonical topic IDs ───────────────────────────────────────────────────────

TOPIC_API_CHANGES     = "kalshi_api_changes"
TOPIC_MACRO_CONTEXT   = "macro_crypto_context"
TOPIC_MARKET_NEWS     = "market_event_news"
TOPIC_EXCHANGE_STATUS = "exchange_operational_status"

# ── Topic definitions ─────────────────────────────────────────────────────────

ALL_TOPICS: list[TopicConfig] = [
    TopicConfig(
        topic_id=TOPIC_API_CHANGES,
        display_name="Kalshi API / Platform Changes",
        search_guidance=(
            "Search for recent changes to the Kalshi prediction market trading API: "
            "breaking changes, new authentication requirements, modified endpoints, "
            "fee schedule updates, new market categories, or changelog entries. "
            "Check Kalshi developer documentation and any GitHub repositories. "
            "This is the highest-priority topic — silent API changes can break the bot "
            "without any obvious error until an order is attempted."
        ),
        advisory_flags_on_high=["api_change_detected"],
    ),
    TopicConfig(
        topic_id=TOPIC_MACRO_CONTEXT,
        display_name="Macro / Crypto Context",
        search_guidance=(
            "Search for major macro and crypto market events in the last 24 hours that "
            "could affect prediction market pricing: BTC or ETH price moves over 5%, "
            "Federal Reserve announcements or CPI/PPI data releases, spot ETF news, "
            "major exchange insolvency or hack, large liquidation cascades. "
            "Focus on concrete events, not analyst opinion."
        ),
        advisory_flags_on_high=["high_impact_event_imminent"],
    ),
    TopicConfig(
        topic_id=TOPIC_MARKET_NEWS,
        display_name="Per-Market Event News",
        search_guidance=(
            "Search for material news about events commonly traded on Kalshi: "
            "upcoming BTC price events, scheduled economic data releases in the next 48h "
            "(jobs report, CPI, FOMC), election or political events, weather events. "
            "Identify news that could cause sudden probability shifts on open markets."
        ),
        advisory_flags_on_high=["high_impact_event_imminent"],
    ),
    TopicConfig(
        topic_id=TOPIC_EXCHANGE_STATUS,
        display_name="Exchange / Operational Status",
        search_guidance=(
            "Search for Kalshi exchange status: current outages, scheduled maintenance "
            "windows, API rate limit changes, trading suspension notices, or regulatory "
            "actions against Kalshi. Check Kalshi's status page and recent announcements "
            "on their official channels."
        ),
        advisory_flags_on_high=["exchange_incident"],
    ),
]

TOPICS_BY_ID: dict[str, TopicConfig] = {t.topic_id: t for t in ALL_TOPICS}
