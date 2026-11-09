"""
The append-only collection universe: what Thread C records, and how it grows.

Seeded from the committed Collection Universe v1 (138 series qualifying under
the frozen predicate), whose artifact is hash-checked at startup so a segment
manifest's universe identifier cannot become a lie.

Two frozen rules, both operational here rather than decorative:

  Rule 1 -- automatic admission. Any series whose official Kalshi event metadata
    satisfies the frozen predicate is admitted from the FIRST COMPLETE census
    that discovers it. That covers series created after v1 and, just as
    importantly, the 18 series v1 quarantined for having no event metadata at
    all -- which includes the postseason products this collection exists for
    (KXMLBWS, KXWSAL, KXWSNL, KXMLBALCSMVP, KXMLBNLCSMVP). Kalshi creates their
    events; the next complete census admits them; the recorder subscribes.
    Every admission appends an immutable discovery record carrying the discovery
    UTC timestamp, the series ticker, the exact predicate-bearing metadata, the
    qualifying event identifier(s), the API endpoint it came from, the raw
    metadata snapshot (stored beside the log and referenced by name), and the
    SHA-256 of that preserved evidence.

  Rule 2 -- append-only inclusion. Nothing is EVER removed. A series whose
    metadata Kalshi later edits, relabels or deletes stays in the universe
    through the collection end; the change is written to the observation log as
    an observation, never applied as an eviction.

No backfill. History before the discovering census is unavailable and is
recorded as unavailable rather than interpolated.

Q1 is not resolved here and is not resolvable here; see Q1_STATUS.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from threadc import frozen
from threadc.discovery import DiscoveryResult, SeriesProbe

log = structlog.get_logger(__name__)

UNIVERSE_SUBDIR = "universe"
STATE_FILE = "universe_state.json"
ADDITIONS_LOG = "universe_additions.jsonl"
OBSERVATIONS_LOG = "universe_observations.jsonl"
EVIDENCE_SUBDIR = "evidence"

Q1_STATUS = {
    "question": (
        "Q1 -- should MLB-adjacent competition values (All-Star, Home Run Derby, "
        "Pro Baseball Draft, Pro Baseball Spring Training) be inside the universe?"
    ),
    "status": "UNRESOLVED",
    "predicate_widened": False,
    "reason": (
        "Those competitions fall outside this collection window: the configured "
        "collection end precedes the next All-Star Game, Home Run Derby, Pro "
        "Baseball Draft and Spring Training, so nothing bearing those values can "
        "be observed between production start and the frozen collection end. Q1 "
        "therefore does not affect what this recorder can collect, and the "
        "recorder makes no scope judgment about it. Resolving Q1 would produce a "
        "Collection Universe v2 and is a human decision, not a recorder decision."
    ),
}
"""Recorded verbatim into every segment manifest. The predicate is NOT widened:
frozen.qualifies() tests exact equality against "Pro Baseball" and nothing else."""


class UniverseError(RuntimeError):
    """The universe cannot be established or would have to be narrowed. Fails closed."""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def load_universe_v1() -> dict[str, Any]:
    """
    The committed Collection Universe v1 artifact, hash-checked.

    A mismatch fails closed rather than warning: every segment manifest claims a
    Collection Universe v1 identifier and hash, and banking two months of data
    against a different file than the one the manifest names would make that
    claim unverifiable after the fact.
    """
    path = frozen.UNIVERSE_V1_JSON
    if not path.exists():
        raise UniverseError(f"Collection Universe v1 artifact missing at {path}")
    digest = frozen.sha256_file(path)
    if digest != frozen.UNIVERSE_V1_SHA256:
        raise UniverseError(
            f"{path} hashes to {digest}, but Collection Universe v1 is published as "
            f"{frozen.UNIVERSE_V1_SHA256}. Refusing to bank against an artifact that "
            f"is not the one the manifest describes. (.gitattributes pins these files "
            f"to LF; a CRLF checkout is the usual cause.)"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    n = len(data.get("qualifying_series") or [])
    if n != frozen.EXPECTED_V1_QUALIFYING_COUNT:
        raise UniverseError(
            f"{path} carries {n} qualifying series; v1 is {frozen.EXPECTED_V1_QUALIFYING_COUNT}"
        )
    return data


@dataclass
class UniverseMember:
    """One series in the append-only universe, and how it got there."""

    series_ticker: str
    admitted_ts: float
    admitted_utc: str
    source: str
    census_id: str = ""
    evidence_sha256: str = ""
    evidence_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "series_ticker": self.series_ticker,
            "admitted_ts": self.admitted_ts,
            "admitted_utc": self.admitted_utc,
            "source": self.source,
            "census_id": self.census_id,
            "evidence_sha256": self.evidence_sha256,
            "evidence_ref": self.evidence_ref,
        }


@dataclass
class UniverseChange:
    """What one census did to the universe. Zero for an INCOMPLETE census."""

    census_id: str
    census_status: str
    added: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    version_before: int = 0
    version_after: int = 0
    applied: bool = False


class AppendOnlyUniverse:
    """
    The live universe: v1 plus every series admitted since, never anything less.

    On-disk layout under <archive_dir>/universe/:

      universe_state.json         current membership, version and log position
      universe_additions.jsonl    immutable append-only discovery records
      universe_observations.jsonl metadata changes seen after admission (Rule 2)
      evidence/<sha256>.json      the preserved raw metadata snapshot per addition

    The state file is a derived index and can be rebuilt from the addition log;
    the addition log is the record of authority and is only ever appended to.
    """

    def __init__(self, archive_dir: Path) -> None:
        self.root = Path(archive_dir) / UNIVERSE_SUBDIR
        self.evidence_dir = self.root / EVIDENCE_SUBDIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

        self.v1 = load_universe_v1()
        self.v1_sha256 = frozen.UNIVERSE_V1_SHA256
        self.members: dict[str, UniverseMember] = {}
        self.version = 0
        self._known_competitions: dict[str, list] = {}

        self._seed_v1()
        self._replay_additions()

    # -- Construction ---------------------------------------------------------

    def _seed_v1(self) -> None:
        generated = self.v1.get("generated_at_utc", "")
        for row in self.v1.get("qualifying_series") or []:
            ticker = row["series_ticker"]
            self.members[ticker] = UniverseMember(
                series_ticker=ticker,
                admitted_ts=0.0,
                admitted_utc=generated,
                source=frozen.UNIVERSE_V1_ID,
                census_id=frozen.UNIVERSE_V1_ID,
                evidence_sha256=self.v1_sha256,
                evidence_ref=str(
                    frozen.UNIVERSE_V1_JSON.relative_to(frozen.REPO_ROOT)
                ).replace("\\", "/"),
            )
        self.version = 1

    def _replay_additions(self) -> None:
        """
        Rebuild everything admitted since v1 from the append-only log.

        Replaying the log rather than trusting universe_state.json means a
        truncated or hand-edited state file cannot quietly shrink the universe:
        the log is what decides membership.
        """
        path = self.root / ADDITIONS_LOG
        if not path.exists():
            self._write_state()
            return
        n = 0
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise UniverseError(
                        f"{path} line {n + 1} is corrupt ({exc}). This log is the record "
                        f"of authority for universe membership; refusing to run with a "
                        f"membership that cannot be reconstructed."
                    ) from exc
                ticker = rec.get("series_ticker")
                if not ticker:
                    continue
                n += 1
                if ticker in self.members:
                    continue
                self.members[ticker] = UniverseMember(
                    series_ticker=ticker,
                    admitted_ts=float(rec.get("discovery_ts", 0.0)),
                    admitted_utc=rec.get("discovery_utc", ""),
                    source=rec.get("source", "rule_1_automatic_admission"),
                    census_id=rec.get("census_id", ""),
                    evidence_sha256=rec.get("evidence_sha256", ""),
                    evidence_ref=rec.get("evidence_ref", ""),
                )
        self.version = 1 + n
        self._write_state()

    # -- Views ----------------------------------------------------------------

    @property
    def series(self) -> list[str]:
        """Every series in the universe, sorted. Never shrinks."""
        return sorted(self.members)

    @property
    def addition_log_position(self) -> int:
        """How many Rule 1 admissions the log holds. Carried into every manifest."""
        return max(0, self.version - 1)

    def identity(self) -> dict[str, Any]:
        """The universe identity block written into every segment manifest."""
        return {
            "collection_universe_id": frozen.UNIVERSE_V1_ID,
            "collection_universe_v1_sha256": self.v1_sha256,
            "collection_universe_v1_series": frozen.EXPECTED_V1_QUALIFYING_COUNT,
            "series_catalog_sha256": frozen.UNIVERSE_V1_SERIES_CATALOG_SHA256,
            "universe_version": self.version,
            "universe_series_count": len(self.members),
            "addition_log_position": self.addition_log_position,
            "addition_log": ADDITIONS_LOG,
            "inclusion_predicate": frozen.PREDICATE_PROSE,
            "inclusion_is_append_only": True,
            "q1": Q1_STATUS,
        }

    # -- Rule 1 ---------------------------------------------------------------

    def apply_census(self, result: DiscoveryResult) -> UniverseChange:
        """
        Admit every newly qualifying series from a COMPLETE census. Do nothing at
        all with an INCOMPLETE one.

        Returns the change record either way, so an INCOMPLETE census is still an
        operational record. Never removes: a member that no longer qualifies
        produces an observation, not an eviction.
        """
        change = UniverseChange(
            census_id=result.census_id,
            census_status=result.status,
            version_before=self.version,
            version_after=self.version,
        )
        if not result.complete:
            log.warning(
                "threadc.universe.census_incomplete",
                census_id=result.census_id, unreadable=len(result.unreadable),
                applied_additions=0, applied_removals=0,
                universe_series=len(self.members),
                note="last-known-good universe unchanged; live recording continues",
            )
            self._append_observation({
                "ts": time.time(), "utc": _iso(time.time()),
                "kind": "census_incomplete_no_changes_applied",
                "census_id": result.census_id,
                "unreadable_series_count": len(result.unreadable),
                "universe_version": self.version,
            })
            return change

        qualifying = result.qualifying()
        for ticker in sorted(qualifying):
            if ticker in self.members:
                continue
            self._admit(ticker, qualifying[ticker], result)
            change.added.append(ticker)

        # Rule 2: record, never evict.
        for ticker, member in sorted(self.members.items()):
            if member.source == frozen.UNIVERSE_V1_ID and ticker not in result.probes:
                continue
            probe = result.probes.get(ticker)
            if probe is None or probe.error is not None:
                continue
            if not probe.qualifies:
                change.observations.append(ticker)
                self._append_observation({
                    "ts": time.time(), "utc": _iso(time.time()),
                    "kind": "member_no_longer_satisfies_predicate",
                    "series_ticker": ticker,
                    "census_id": result.census_id,
                    "competition_values_now": sorted(
                        json.dumps(v) for v in probe.competition_values
                    ),
                    "action": "none -- inclusion is append-only through the collection end",
                })

        change.applied = True
        change.version_after = self.version
        if change.added:
            self._write_state()
            log.info(
                "threadc.universe.admitted", census_id=result.census_id,
                added=change.added, universe_version=self.version,
                universe_series=len(self.members),
            )
        return change

    def _admit(self, ticker: str, probe: SeriesProbe, result: DiscoveryResult) -> None:
        now = time.time()
        evidence = {
            "series_ticker": ticker,
            "discovery_ts": now,
            "discovery_utc": _iso(now),
            "census_id": result.census_id,
            "api_source": {
                "base_url": result.base_url,
                "events_endpoint":
                    f"GET {result.base_url}/events?series_ticker={ticker}&limit=200",
                "series_catalog_endpoint": f"GET {result.base_url}/series",
                "series_catalog_sha256": result.catalog_sha256,
                "series_catalog_retrieved_at_utc": result.catalog_retrieved_at,
            },
            "predicate": frozen.PREDICATE_PROSE,
            "predicate_bearing_metadata": probe.evidence(),
            "qualifying_event_tickers": probe.qualifying_event_tickers(),
            "backfill": (
                "none -- history before this census is unavailable and is not "
                "inferred or interpolated"
            ),
        }
        blob = json.dumps(evidence, indent=1, sort_keys=True) + "\n"
        digest = frozen.sha256_text(blob)
        evidence_path = self.evidence_dir / f"{digest}.json"
        if not evidence_path.exists():
            evidence_path.write_text(blob, encoding="utf-8")

        record = {
            "discovery_ts": now,
            "discovery_utc": _iso(now),
            "series_ticker": ticker,
            "source": "rule_1_automatic_admission",
            "census_id": result.census_id,
            "predicate_bearing_metadata": probe.evidence(),
            "qualifying_event_tickers": probe.qualifying_event_tickers(),
            "api_endpoint": f"GET {result.base_url}/events?series_ticker={ticker}&limit=200",
            "api_source": "Kalshi official event metadata",
            "evidence_ref": f"{UNIVERSE_SUBDIR}/{EVIDENCE_SUBDIR}/{digest}.json",
            "evidence_sha256": digest,
            "backfill": "none -- prior history is unavailable and is not inferred",
        }
        with open(self.root / ADDITIONS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

        self.members[ticker] = UniverseMember(
            series_ticker=ticker, admitted_ts=now, admitted_utc=_iso(now),
            source="rule_1_automatic_admission", census_id=result.census_id,
            evidence_sha256=digest, evidence_ref=record["evidence_ref"],
        )
        self.version += 1

    # -- Persistence ----------------------------------------------------------

    def _append_observation(self, record: dict[str, Any]) -> None:
        with open(self.root / OBSERVATIONS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def _write_state(self) -> None:
        payload = {
            **self.identity(),
            "written_utc": _iso(time.time()),
            "members": [m.as_dict() for m in sorted(
                self.members.values(), key=lambda m: m.series_ticker)],
        }
        tmp = self.root / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.root / STATE_FILE)


__all__ = [
    "AppendOnlyUniverse", "UniverseMember", "UniverseChange", "UniverseError",
    "load_universe_v1", "Q1_STATUS", "ADDITIONS_LOG", "STATE_FILE",
]
