"""
When the full-universe Rule 1 discovery sweep may run.

Two independent rules, both persisted outside process memory because both must
survive a crash, a restart and a reboot.

CADENCE -- once per UTC calendar day, targeted at a wall-clock UTC time.

    The schedule is anchored to the clock, not to process start. A recorder that
    slept for an interval measured from startup would shift its cadence a little
    on every restart and drift permanently; anchoring to 09:00 UTC means a
    reboot at 03:00 changes nothing about when the sweep happens.

    If the recorder was down at the target time, that day's discovery is run
    once as soon as live recording is back up -- the catch-up is inherently
    "today only", because a discovery census is a snapshot of what the exchange
    says right now and cannot be taken retroactively for a day already past.

    A UTC day is marked accepted only by a COMPLETE census. An INCOMPLETE one
    leaves the day unaccepted, so the day is retried -- which is the one case
    where more than one sweep runs for the same UTC day, and it is exactly the
    explicit retry the frozen rule allows.

RESTART-STORM GUARD -- no new sweep within 3600 s of the previous ATTEMPT START,
regardless of how that attempt ended.

    The timestamp is written to disk BEFORE the sweep begins, not after it
    finishes. That ordering is the whole point: with Restart=always and
    RestartSec=5, a process that dies four minutes into a 25-minute sweep would
    otherwise come back with an empty memory and start a fresh sweep every five
    seconds. Recording the intent to sweep, rather than the completion of one,
    is what makes the guard survive the crash it exists to guard against.

Nothing here reads a price, a book or a market. It reads a clock and a small
JSON file.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from threadc import frozen

log = structlog.get_logger(__name__)

DISCOVERY_STATE_FILE = "discovery_schedule.state.json"

MAX_SLEEP_SECONDS = 3600.0
"""Longest the discovery loop sleeps before re-evaluating. Bounds how stale a
computed wait can become if the wall clock is stepped by NTP."""

ATTEMPT_HISTORY_LIMIT = 200
"""Attempts kept in the state file. Enough to cover the whole collection window
at one accepted sweep a day plus retries; the authoritative per-day record is
`accepted_days`, which is never trimmed."""

STATUS_IN_PROGRESS = "IN_PROGRESS"


class ScheduleError(RuntimeError):
    """The persisted schedule state cannot be trusted. Fails closed."""


def parse_hms(value: str) -> tuple[int, int, int]:
    """'09:00:00' -> (9, 0, 0). Refuses anything it cannot read exactly."""
    parts = str(value).strip().split(":")
    if len(parts) not in (2, 3):
        raise ScheduleError(f"discovery target {value!r} is not HH:MM or HH:MM:SS")
    try:
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ScheduleError(f"discovery target {value!r} is not numeric") from exc
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ScheduleError(f"discovery target {value!r} is out of range")
    return h, m, s


@dataclass(frozen=True)
class DiscoveryDecision:
    """Whether a sweep may start now, why, and when to look again."""

    due: bool
    reason: str
    utc_day: str
    target_ts: float
    next_check_seconds: float

    def log_fields(self) -> dict[str, Any]:
        return {
            "due": self.due, "reason": self.reason, "utc_day": self.utc_day,
            "next_check_seconds": round(self.next_check_seconds, 1),
        }


class DiscoverySchedule:
    """
    The persisted discovery schedule for one archive.

    The state file is small and is rewritten atomically. It is the only thing
    standing between a crash loop and a fresh 25-minute, 13,800-request sweep
    every five seconds, so a corrupt one fails closed rather than being
    discarded -- discarding it would silently remove the guard.
    """

    def __init__(
        self,
        archive_dir: Path,
        *,
        target_utc: str = frozen.DISCOVERY_DAILY_TARGET_UTC,
        min_attempt_interval: float = frozen.DISCOVERY_ATTEMPT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.target_utc = target_utc
        self.target_hms = parse_hms(target_utc)
        self.min_attempt_interval = float(min_attempt_interval)
        self.state_path = self.archive_dir / DISCOVERY_STATE_FILE
        self._state = self._load()

    # -- Persistence ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"accepted_days": {}, "attempts": [],
                    "last_attempt_started_ts": None}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScheduleError(
                f"{self.state_path} is unreadable ({exc}). It carries the "
                f"restart-storm guard; refusing to run rather than starting a "
                f"full-catalog sweep with no guard at all."
            ) from exc
        if not isinstance(data, dict):
            raise ScheduleError(f"{self.state_path}: expected a JSON object")
        data.setdefault("accepted_days", {})
        data.setdefault("attempts", [])
        data.setdefault("last_attempt_started_ts", None)
        return data

    def _write(self) -> None:
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._state, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(self.state_path)

    # -- Views ----------------------------------------------------------------

    @property
    def last_attempt_started_ts(self) -> float | None:
        v = self._state.get("last_attempt_started_ts")
        return float(v) if v is not None else None

    @property
    def accepted_days(self) -> dict[str, Any]:
        return dict(self._state.get("accepted_days") or {})

    def day_is_accepted(self, utc_day: str) -> bool:
        return utc_day in (self._state.get("accepted_days") or {})

    def target_ts_for_day(self, utc_day: str) -> float:
        h, m, s = self.target_hms
        day = datetime.strptime(utc_day, "%Y-%m-%d").replace(
            hour=h, minute=m, second=s, tzinfo=timezone.utc
        )
        return day.timestamp()

    def summary(self) -> dict[str, Any]:
        """Operational only. Carried into manifests and the startup record."""
        return {
            "discovery_daily_target_utc": self.target_utc,
            "discovery_attempt_min_interval_seconds": self.min_attempt_interval,
            "discovery_state_file": str(self.state_path),
            "last_attempt_started_utc": self._state.get("last_attempt_started_utc"),
            "accepted_days": sorted(self.accepted_days)[-7:],
            "accepted_day_count": len(self.accepted_days),
        }

    # -- The decision ---------------------------------------------------------

    def decide(self, now: float | None = None) -> DiscoveryDecision:
        """May a full-universe sweep start right now, and when to look again."""
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        target = self.target_ts_for_day(day)

        def result(due: bool, reason: str, wait: float) -> DiscoveryDecision:
            return DiscoveryDecision(
                due=due, reason=reason, utc_day=day, target_ts=target,
                next_check_seconds=max(0.0, min(wait, MAX_SLEEP_SECONDS)),
            )

        if self.day_is_accepted(day):
            tomorrow = self.target_ts_for_day(
                (datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=1))
                .strftime("%Y-%m-%d")
            )
            return result(False, "already accepted for this UTC day", tomorrow - now)

        if now < target:
            return result(False, "before today's target time", target - now)

        # At or past today's target with no accepted census: due, whether that
        # is the ordinary daily run or a catch-up after the recorder was down.
        last = self.last_attempt_started_ts
        if last is not None:
            since = now - last
            if since < self.min_attempt_interval:
                return result(
                    False,
                    f"restart-storm guard: {self.min_attempt_interval:.0f}s minimum "
                    f"between attempt starts, {since:.0f}s elapsed",
                    self.min_attempt_interval - since,
                )
        reason = ("daily target reached" if now - target < self.min_attempt_interval
                  else "catch-up for a missed target on this UTC day")
        return result(True, reason, 0.0)

    def blocked_by_guard(self, now: float | None = None) -> float:
        """Seconds still owed to the restart-storm guard, 0 when clear."""
        now = time.time() if now is None else now
        last = self.last_attempt_started_ts
        if last is None:
            return 0.0
        return max(0.0, self.min_attempt_interval - (now - last))

    # -- Recording ------------------------------------------------------------

    def record_attempt_start(self, now: float, census_id: str, utc_day: str) -> None:
        """
        Persist the intent to sweep, BEFORE the sweep begins.

        Written first on purpose: a process that dies mid-sweep must come back to
        a guard that already knows a sweep was started, or Restart=always turns
        a crash into a 13,800-request sweep every RestartSec.
        """
        iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        self._state["last_attempt_started_ts"] = now
        self._state["last_attempt_started_utc"] = iso
        self._state["last_attempt_census_id"] = census_id
        self._state["last_attempt_status"] = STATUS_IN_PROGRESS
        attempts = list(self._state.get("attempts") or [])
        attempts.append({
            "census_id": census_id, "utc_day": utc_day,
            "started_ts": now, "started_utc": iso, "status": STATUS_IN_PROGRESS,
        })
        self._state["attempts"] = attempts[-ATTEMPT_HISTORY_LIMIT:]
        self._write()
        log.info(
            "threadc.discovery.attempt_started",
            census_id=census_id, utc_day=utc_day, state_file=str(self.state_path),
        )

    def record_result(
        self, now: float, census_id: str, status: str, utc_day: str
    ) -> None:
        """
        Record how an attempt ended. Only a COMPLETE census accepts the UTC day.

        An INCOMPLETE one deliberately leaves the day unaccepted so it is retried
        -- subject to the guard, which is why a retry storm is impossible.
        """
        iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        self._state["last_attempt_status"] = status
        for attempt in reversed(self._state.get("attempts") or []):
            if attempt.get("census_id") == census_id:
                attempt["status"] = status
                attempt["finished_ts"] = now
                attempt["finished_utc"] = iso
                break
        if status == frozen.CENSUS_COMPLETE:
            self._state.setdefault("accepted_days", {})[utc_day] = {
                "census_id": census_id, "accepted_ts": now, "accepted_utc": iso,
            }
        self._write()
        log.info(
            "threadc.discovery.attempt_finished",
            census_id=census_id, utc_day=utc_day, status=status,
            day_accepted=self.day_is_accepted(utc_day),
        )


__all__ = [
    "DiscoverySchedule", "DiscoveryDecision", "ScheduleError", "parse_hms",
    "DISCOVERY_STATE_FILE", "MAX_SLEEP_SECONDS",
]
