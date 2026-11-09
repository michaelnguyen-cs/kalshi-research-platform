"""
Operator configuration, and the rules that make one value of it irreversible.

Three things here are deliberately NOT constants in code, because the frozen
spec says changing them must not require a code change:

  collection_end_utc      when collection stops. Initially 2026-11-05T12:00:00Z,
                          derived from MLB's published postseason schedule as of
                          2026-09-03. May be moved LATER by an operator; may
                          never move earlier. The recorder does not fetch, parse
                          or monitor the MLB schedule -- it reads this value and
                          nothing else.
  disk_reserve_bytes      the production reserve R = max(10 GiB, 48 x B_1h).
                          B_1h comes from the one-hour capacity run. It has now
                          happened, on both branches of the fallback hierarchy,
                          and production is set from the Branch-2 measurement.
                          Still REQUIRED and still without a default: a guessed
                          R would be a fabricated measurement.
  discovery_daily_target_utc
                          the wall-clock UTC time the once-per-UTC-day
                          full-universe Rule 1 sweep targets. Operator-set to
                          09:00:00. Anchored to the clock rather than to process
                          start so a reboot cannot shift the cadence; the
                          scheduling and the frozen restart-storm guard live in
                          threadc.schedule.

A fourth key, `channels`, selects WHICH of the frozen channels a run subscribes
to -- see _parse_channels. It may only ever name a non-empty SUBSET of
threadc.frozen.CHANNELS: it can subtract, never add, and its absence means the
frozen pair. It was capacity-measurement-only while the fallback hierarchy was
still being measured; both branches have now been measured, and production
selects on the result, so the key is honoured in production mode too. What it
cannot do is widen what the bank preserves -- adding a channel changes the
fidelity boundary and the storage schema, and remains a code change to the
frozen tuple.

The monotonic collection-end rule is enforced against a persisted high-water
mark in the archive directory, not against the config file, because the config
file is exactly the thing an operator edits. Every accepted change is appended
to an operational change log.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from threadc import frozen, schedule

CONFIG_ENV_VAR = "THREADC_CONFIG"
DEFAULT_CONFIG_PATH = frozen.REPO_ROOT / "config" / "threadc.json"

COLLECTION_END_STATE_FILE = "collection_end.state.json"
CONFIG_CHANGE_LOG_FILE = "config_changes.jsonl"

MIN_DISK_RESERVE_BYTES = 10 * 1024**3
"""The floor of the frozen reserve formula R = max(10 GiB, 48 x B_1h). The 10
GiB half of that max is stated in the spec and is not a measurement, so it can
be enforced now; the 48 x B_1h half cannot, and is not guessed."""


class ConfigError(RuntimeError):
    """Configuration that must not be run with. Always fails closed at startup."""


def _parse_utc(value: Any, field_name: str) -> float:
    """
    An ISO-8601 UTC timestamp -> unix seconds.

    A naive string is REFUSED rather than assumed to be UTC. The collection end
    is the one value that ends the collection; reading '2026-11-05 12:00:00' in
    local time on a machine whose timezone nobody checked would move the frozen
    end by hours in whichever direction the host happened to be configured.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name}: expected an ISO-8601 UTC timestamp, got {value!r}")
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"{field_name}: {value!r} is not ISO-8601 ({exc})") from exc
    if dt.tzinfo is None:
        raise ConfigError(
            f"{field_name}: {value!r} has no timezone. Write it with an explicit "
            f"trailing 'Z' or a UTC offset -- a naive timestamp would silently mean "
            f"whatever the host's local timezone happens to be, which would move a "
            f"frozen end date by the host's offset."
        )
    return dt.astimezone(timezone.utc).timestamp()


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ThreadCConfig:
    """One resolved configuration. Immutable for the life of a process."""

    archive_dir: Path
    collection_end_ts: float
    disk_reserve_bytes: int
    discovery_daily_target_utc: str
    ws_shards: int
    max_markets_per_shard: int
    channels: tuple[str, ...] = frozen.CHANNELS
    mode: str = frozen.MODE_PRODUCTION
    source_path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def collection_end_iso(self) -> str:
        return iso_utc(self.collection_end_ts)

    @property
    def discovery_enabled(self) -> bool:
        """
        Rule 1 full-universe discovery runs in production mode only.

        Capacity mode starts from the verified current append-only universe and
        measures ticker/trade throughput and whether that universe can be
        subscribed. A capacity run started after the daily target would
        otherwise immediately perform that day's missed 25-minute sweep and put
        its REST load, CPU and response bodies inside the very hour being
        measured. This is not a config key: it is a property of the mode, so it
        cannot be switched on for a capacity run by editing a file.
        """
        return self.mode == frozen.MODE_PRODUCTION

    @property
    def channels_are_frozen_pair(self) -> bool:
        """
        True iff this run banks the whole frozen pair.

        NOT a statement about whether the run is production: production selects
        ticker only, on the Branch-2 measurement, so the production collection
        reports False here. What it says is that a narrower set was selected,
        and every archive such a run produces says so in its own manifest -- so
        a segment recorded on one channel can never be read years later as a
        segment that was recorded on two and happened to be quiet.
        """
        return tuple(self.channels) == tuple(frozen.CHANNELS)

    def summary(self) -> dict[str, Any]:
        """Operational only. No market, price or activity information exists here."""
        return {
            "archive_dir": str(self.archive_dir),
            "collection_end_utc": self.collection_end_iso,
            "collection_end_ts": self.collection_end_ts,
            "disk_reserve_bytes": self.disk_reserve_bytes,
            "discovery_daily_target_utc": self.discovery_daily_target_utc,
            "discovery_attempt_min_interval_seconds":
                frozen.DISCOVERY_ATTEMPT_MIN_INTERVAL_SECONDS,
            "discovery_enabled": self.discovery_enabled,
            "market_census_live_interval_seconds": frozen.MARKET_CENSUS_LIVE_INTERVAL_SECONDS,
            "market_census_reconcile_interval_seconds":
                frozen.MARKET_CENSUS_RECONCILE_INTERVAL_SECONDS,
            "ws_shards": self.ws_shards,
            "max_markets_per_shard": self.max_markets_per_shard,
            "channels": list(self.channels),
            "channels_are_frozen_pair": self.channels_are_frozen_pair,
            "mode": self.mode,
            "source_path": str(self.source_path) if self.source_path else None,
        }


def _require(data: dict[str, Any], key: str, path: Path | None) -> Any:
    if key not in data or data[key] is None:
        where = f" in {path}" if path else ""
        raise ConfigError(
            f"required configuration key {key!r} is missing{where}. It has no default "
            f"on purpose -- see threadc.config's module docstring."
        )
    return data[key]


CHANNELS_KEY = "channels"


def _parse_channels(data: dict[str, Any], mode: str, path: Path | None) -> tuple[str, ...]:
    """
    Which of the frozen channels this run subscribes to.

    Absent -- in either mode -- means the frozen pair in threadc.frozen.CHANNELS,
    unchanged. A configuration that says nothing about channels therefore banks
    what the bank was designed to bank, and no edit is needed to get that.

    What it may select is a non-empty SUBSET of the frozen pair, and nothing
    else. A key that could ADD a channel would be a fidelity widening dressed as
    configuration -- and `orderbook_delta`, the one channel anybody would reach
    for, is exactly the design the storage budget already rejected. So the
    selection can only ever subtract, it never edits frozen.CHANNELS, and the
    order is normalized to the frozen order so two configs naming the same
    channels produce identical manifests.

    WHY PRODUCTION MAY NOW SELECT. This key was capacity-measurement-only, and
    refused outright in production, while the precommitted fallback hierarchy
    was still unmeasured: at that point a narrowed production run would have
    been a fidelity decision taken from a config file against no evidence.
    Both branches have since been measured on the same bracketed hour, the same
    universe and the same storage schema:

        Branch 1  ticker+trade   B_1h,disk = 373,444,608 bytes/hour
        Branch 2  ticker only    B_1h,disk = 267,526,144 bytes/hour

    Branch 1 does not fit the 137 GB available for the collection window, so the
    hierarchy moves to Branch 2 and production runs ticker-only. Leaving the key
    refused in production would not have preserved anything: it would have meant
    production silently starting on Branch 1 -- the branch the measurement
    rejected -- because that is what the absent-key default is. So the selection
    is made explicitly, in config/threadc.json, where it is reviewable beside the
    reserve it was measured with, and it is written into the startup record, the
    readiness line, the ws_session rows and the OPEN and final manifests -- the
    same manifests the daily health check reads -- so no archive can be read as
    having banked a channel it did not. The health check's own output stays at
    its four facts and does not repeat the names: a channel called `ticker` is
    indistinguishable there from a market ticker, which that output must never
    carry.

    What did NOT move: the ceiling. frozen.CHANNELS is still the maximum this
    bank preserves and still the only thing a widening would have to edit, under
    review against research/THREADC_FIDELITY_BOUNDARY_V1.md.

    ONE THING THIS REFUSAL ALSO USED TO CATCH is gone with it: running
    config/threadc.capacity.json WITHOUT --capacity used to fail here, because
    that file carries a channels key. It no longer does. What still keeps the
    two apart is what always did the real work -- a separate archive_dir, so no
    capacity segment can land beside a production one; mode=capacity stamped
    into every manifest; a close_reason of capacity_run_end; and the capacity
    config not being the systemd unit's ExecStart. A production start from the
    capacity file would also collect against that file's deliberately far-future
    collection end, in the capacity archive, which is visible in the first
    manifest it writes.
    """
    if CHANNELS_KEY not in data or data[CHANNELS_KEY] is None:
        return tuple(frozen.CHANNELS)

    value = data[CHANNELS_KEY]
    where = f" in {path}" if path else ""
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise ConfigError(
            f"{CHANNELS_KEY}{where}: expected a non-empty list of channel names drawn "
            f"from {list(frozen.CHANNELS)}, got {value!r}"
        )

    selected: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{CHANNELS_KEY}: {item!r} is not a channel name")
        name = item.strip()
        if name not in frozen.CHANNELS:
            raise ConfigError(
                f"{CHANNELS_KEY}{where}: {name!r} is not one of the frozen channels "
                f"{list(frozen.CHANNELS)}. This key may only select a SUBSET of what "
                f"the bank already preserves; adding a channel changes the fidelity "
                f"boundary and the storage schema, and is a code change to "
                f"threadc.frozen.CHANNELS -- not an edit to a {mode} config file."
            )
        if name in selected:
            raise ConfigError(f"{CHANNELS_KEY}: {name!r} is listed more than once")
        selected.append(name)

    # Frozen order, so the selection is a set and not an ordering decision.
    return tuple(c for c in frozen.CHANNELS if c in selected)


def load(
    path: Path | None = None,
    *,
    mode: str = frozen.MODE_PRODUCTION,
    overrides: dict[str, Any] | None = None,
) -> ThreadCConfig:
    """
    Read, validate and freeze one configuration.

    `overrides` exists for the capacity run and for tests, which supply an
    archive directory on the command line. It cannot relax a required key and it
    cannot move the collection end earlier -- both checks run after it is
    applied.
    """
    path = Path(path) if path is not None else Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH))
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected a JSON object at the top level")
    elif overrides is None:
        raise ConfigError(
            f"no Thread C configuration at {path}. Set {CONFIG_ENV_VAR} or pass --config."
        )
    data = {**data, **(overrides or {})}

    if mode not in (frozen.MODE_PRODUCTION, frozen.MODE_CAPACITY):
        raise ConfigError(f"unknown run mode {mode!r}")

    archive_dir = Path(str(_require(data, "archive_dir", path))).expanduser()
    collection_end_ts = _parse_utc(
        _require(data, "collection_end_utc", path), "collection_end_utc"
    )

    reserve = _require(data, "disk_reserve_bytes", path)
    try:
        disk_reserve_bytes = int(reserve)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"disk_reserve_bytes: {reserve!r} is not an integer") from exc
    if disk_reserve_bytes < MIN_DISK_RESERVE_BYTES:
        raise ConfigError(
            f"disk_reserve_bytes = {disk_reserve_bytes} is below the frozen floor of "
            f"{MIN_DISK_RESERVE_BYTES} bytes (10 GiB). The frozen reserve is "
            f"R = max(10 GiB, 48 x B_1h); nothing below 10 GiB satisfies it."
        )

    target = str(data.get("discovery_daily_target_utc")
                 or frozen.DISCOVERY_DAILY_TARGET_UTC)
    try:
        schedule.parse_hms(target)
    except schedule.ScheduleError as exc:
        raise ConfigError(f"discovery_daily_target_utc: {exc}") from exc

    ws_shards = int(data.get("ws_shards", 1))
    if ws_shards < 1:
        raise ConfigError("ws_shards must be at least 1")
    max_per_shard = int(data.get("max_markets_per_shard", 0))
    if max_per_shard < 0:
        raise ConfigError("max_markets_per_shard must not be negative")

    channels = _parse_channels(data, mode, path)

    return ThreadCConfig(
        archive_dir=archive_dir,
        collection_end_ts=collection_end_ts,
        disk_reserve_bytes=disk_reserve_bytes,
        discovery_daily_target_utc=target,
        ws_shards=ws_shards,
        max_markets_per_shard=max_per_shard,
        channels=channels,
        mode=mode,
        source_path=path if path.exists() else None,
        raw=data,
    )


# -- The monotonic collection-end rule ---------------------------------------


def enforce_monotonic_collection_end(cfg: ThreadCConfig) -> dict[str, Any]:
    """
    Refuse a configured collection end earlier than one already accepted, and
    log every accepted change.

    The high-water mark lives beside the archive, not in the config file: the
    config file is the thing being edited, so a rule enforced only there could be
    undone by the same edit it is meant to catch. An operator who genuinely needs
    to move the end earlier has to delete the state file deliberately, which
    leaves a visible hole in the change log rather than a silent shortening.

    Returns the change record (or the unchanged record) for logging. Never
    contains anything but timestamps and the config path.
    """
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    state_path = cfg.archive_dir / COLLECTION_END_STATE_FILE
    now = time.time()

    previous: dict[str, Any] | None = None
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{state_path} is corrupt ({exc}). It records the highest collection end "
                f"ever accepted; refusing to run rather than silently forgetting it."
            ) from exc

    if previous is not None:
        prior_ts = float(previous["collection_end_ts"])
        if cfg.collection_end_ts < prior_ts:
            raise ConfigError(
                f"configured collection_end_utc {cfg.collection_end_iso} is EARLIER than "
                f"the already-accepted {iso_utc(prior_ts)}. The frozen rule allows the "
                f"end to move later and never earlier."
            )
        if cfg.collection_end_ts == prior_ts:
            return {"change": "unchanged", "collection_end_utc": cfg.collection_end_iso}

    record = {
        "ts": now,
        "utc": iso_utc(now),
        "change": "collection_end_set" if previous is None else "collection_end_extended",
        "previous_collection_end_utc": iso_utc(float(previous["collection_end_ts"]))
        if previous else None,
        "collection_end_utc": cfg.collection_end_iso,
        "collection_end_ts": cfg.collection_end_ts,
        "config_path": str(cfg.source_path) if cfg.source_path else None,
        "rule": (
            "collection continues through the fifth calendar day after the latest "
            "postseason game date currently published by MLB; the configured end may "
            "move later and never earlier"
        ),
    }
    state_path.write_text(
        json.dumps(
            {"collection_end_ts": cfg.collection_end_ts,
             "collection_end_utc": cfg.collection_end_iso,
             "updated_ts": now, "updated_utc": iso_utc(now)},
            indent=1, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    append_change_log(cfg.archive_dir, record)
    return record


def append_change_log(archive_dir: Path, record: dict[str, Any]) -> None:
    """Append one operational configuration-change record. Append-only, never rewritten."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    with open(archive_dir / CONFIG_CHANGE_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


__all__ = [
    "ThreadCConfig", "ConfigError", "load", "enforce_monotonic_collection_end",
    "append_change_log", "iso_utc", "MIN_DISK_RESERVE_BYTES",
    "CONFIG_ENV_VAR", "DEFAULT_CONFIG_PATH", "CHANNELS_KEY",
]
