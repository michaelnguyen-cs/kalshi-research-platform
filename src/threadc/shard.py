"""
Deterministic sharding of markets across WebSocket connections.

Kalshi publishes an AsyncAPI error code for "subscription market limit exceeded"
(26) but no number for the limit itself, and Thread A's recorder deliberately
refuses to bisect against the server to find one. Thread C's universe is far
larger than Thread A's two series, so more than one connection may be needed --
and if it is, the assignment must be a function of the ticker, not of arrival
order, so that which shard carried a market is reproducible from the archive
alone.

    shard_index = int(sha256(ticker).hexdigest()[:16], 16) % n_shards

Properties this buys, all of which matter downstream:

  * exactly one active shard per market -- membership is a pure function, so no
    market can be double-subscribed by accident;
  * stable under growth -- admitting a new series or a new market does not move
    any existing market to a different connection, so no reshuffle storm;
  * reproducible offline -- a reader with the ticker and n_shards recomputes the
    assignment without consulting the recorder;
  * even fill -- SHA-256 spreads tickers uniformly, so no shard is the one that
    hits the unpublished limit first.

n_shards is operator configuration and is recorded in every manifest, because
changing it re-partitions the universe. A change is a handoff, not a silent
re-map: every moved market is logged as a shard_change health event carrying both
labels, and every subscribe and unsubscribe is written to shard_assignment with
its shard label, timestamp and connection epoch.

No transient double subscription is ever performed. The assignment is a total
function of (ticker, n_shards), so a market is in exactly one shard at any
moment, and the recorder has no path that puts one on two connections. If a
future handoff ever does need an overlap window, it must be written to
shard_assignment under its own action name so downstream readers can tell it
from duplicate data -- but nothing here emits one today.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable


def shard_index(ticker: str, n_shards: int) -> int:
    """Which shard a market belongs to. Pure, total, and stable under growth."""
    if n_shards < 1:
        raise ValueError("n_shards must be at least 1")
    if n_shards == 1:
        return 0
    digest = hashlib.sha256(ticker.encode("utf-8")).hexdigest()[:16]
    return int(digest, 16) % n_shards


def shard_label(index: int) -> str:
    """The stream label a shard's frames are stored under."""
    return f"S{index:02d}"


def partition(tickers: Iterable[str], n_shards: int) -> dict[str, list[str]]:
    """
    label -> sorted tickers. Every shard label is present even when empty, so a
    reader can tell "this shard carried nothing" from "this shard did not exist".
    """
    out: dict[str, list[str]] = {shard_label(i): [] for i in range(n_shards)}
    for t in tickers:
        out[shard_label(shard_index(t, n_shards))].append(t)
    for v in out.values():
        v.sort()
    return out


@dataclass(frozen=True)
class ShardPlan:
    """The current partition plus the operational facts a manifest needs."""

    n_shards: int
    max_markets_per_shard: int
    assignment: dict[str, list[str]]

    @property
    def occupancy(self) -> dict[str, int]:
        return {label: len(t) for label, t in sorted(self.assignment.items())}

    def over_capacity(self) -> list[str]:
        """
        Shards past the configured cap.

        Reported, never auto-fixed: silently re-partitioning to fit would move
        markets between connections on the recorder's own initiative, which is
        exactly the kind of self-directed search Thread A's session refuses to
        do against the subscribe endpoint. The operator raises ws_shards.
        """
        if self.max_markets_per_shard <= 0:
            return []
        return [
            label for label, n in self.occupancy.items() if n > self.max_markets_per_shard
        ]

    def summary(self) -> dict[str, object]:
        return {
            "n_shards": self.n_shards,
            "max_markets_per_shard": self.max_markets_per_shard,
            "occupancy": self.occupancy,
            "over_capacity": self.over_capacity(),
            "assignment_rule": "sha256(ticker)[:16] % n_shards",
        }


def plan(tickers: Iterable[str], n_shards: int, max_markets_per_shard: int) -> ShardPlan:
    return ShardPlan(
        n_shards=n_shards,
        max_markets_per_shard=max_markets_per_shard,
        assignment=partition(tickers, n_shards),
    )


__all__ = ["shard_index", "shard_label", "partition", "plan", "ShardPlan"]
