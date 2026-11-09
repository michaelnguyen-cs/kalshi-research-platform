"""
The frozen daily human health check: exactly four facts, and nothing else.

  1. Is the Thread C recorder service active?
  2. Did yesterday's archive segments finalize successfully with COMPLETE?
  3. Is filesystem free space greater than the configured reserve R?
  4. Did yesterday's segments actually bank any raw frames at all?

That is the whole check. It is deliberately not a dashboard: no prices, no
spreads, no depth distributions, no per-market volume or activity, no returns
and no outcomes appear here or can be reached from here. What it may show --
service state, segment states, byte counts, frame counts and free space -- is
operational, and a frame count is a property of the recorder, not of a market.

Fact 2 is per-DAY, not per-segment: a UTC day is healthy when it produced at
least one segment and every segment for that day is COMPLETE. A day containing
an INCOMPLETE_CRASH or HALTED_LOW_DISK segment is reported unhealthy even though
the other segments finished cleanly, because a gap in that day's coverage is
exactly what the operator needs to see.

Fact 4 exists because facts 1-3 are all satisfied by a recorder that banks
NOTHING. A subscription set that stayed empty still rotates at 00:00 UTC, still
finalizes COMPLETE, still leaves free space alone -- so 63 days of empty
archives would have passed every check. It is one aggregate: the total raw frame
count over the day's segments, summed from the manifests. Deliberately not
per-market, per-series or per-price -- "the recorder wrote frames" is an
operational fact about the process, and any breakdown of it would be research
data this check must never show.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from threadc import disk, frozen, segment as seg_mod

DEFAULT_SERVICE = "threadc-recorder.service"


@dataclass
class HealthReport:
    """Four facts plus the evidence for each. Nothing else."""

    service_name: str
    service_active: bool
    service_detail: str
    day: str
    day_segments_complete: bool
    day_detail: str
    day_segments: list[dict[str, Any]] = field(default_factory=list)
    disk_ok: bool = False
    disk_status: dict[str, Any] = field(default_factory=dict)
    day_frames_ok: bool = False
    day_raw_frame_count: int = 0
    day_frames_detail: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.service_active
            and self.day_segments_complete
            and self.disk_ok
            and self.day_frames_ok
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fact_1_service_active": {
                "service": self.service_name,
                "active": self.service_active,
                "detail": self.service_detail,
            },
            "fact_2_segments_complete": {
                "utc_day": self.day,
                "all_complete": self.day_segments_complete,
                "detail": self.day_detail,
                "segments": self.day_segments,
            },
            "fact_3_free_space_above_reserve": {
                "ok": self.disk_ok,
                **self.disk_status,
            },
            "fact_4_raw_frames_banked": {
                "utc_day": self.day,
                "any_frames": self.day_frames_ok,
                "raw_frame_count": self.day_raw_frame_count,
                "detail": self.day_frames_detail,
            },
        }

    def render(self) -> str:
        tick = lambda b: "OK  " if b else "FAIL"  # noqa: E731
        lines = [
            f"Thread C daily health check   {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
            "",
            f"[{tick(self.service_active)}] 1. recorder service active   "
            f"{self.service_name}: {self.service_detail}",
            f"[{tick(self.day_segments_complete)}] 2. {self.day} segments COMPLETE   "
            f"{self.day_detail}",
            f"[{tick(self.disk_ok)}] 3. free space above reserve  "
            f"free {self.disk_status.get('free_gib')} GiB, "
            f"reserve {self.disk_status.get('reserve_gib')} GiB",
            f"[{tick(self.day_frames_ok)}] 4. {self.day} raw frames banked  "
            f"{self.day_frames_detail}",
        ]
        if self.day_segments:
            lines.append("")
            for s in self.day_segments:
                lines.append(
                    f"      {s['filename']:<40} {s['segment_state']:<18} "
                    f"{s.get('raw_frame_count', 0):>12,} frames  "
                    f"{s.get('byte_size', 0) / 1024**2:>10,.1f} MiB  "
                    f"integrity={s.get('integrity_check')}"
                )
        lines.append("")
        lines.append("OVERALL: " + ("HEALTHY" if self.ok else "ATTENTION REQUIRED"))
        return "\n".join(lines)


def service_active(name: str = DEFAULT_SERVICE) -> tuple[bool, str]:
    """
    `systemctl is-active`. "activating" is the RestartSec backoff after a crash
    and is reported as not-active on purpose -- the operator should see it.
    """
    exe = shutil.which("systemctl")
    if exe is None:
        return False, "systemctl not available on this host"
    try:
        proc = subprocess.run(
            [exe, "is-active", name], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not query systemd: {exc}"
    state = (proc.stdout or proc.stderr or "").strip() or "unknown"
    return state == "active", state


def yesterday_utc(now: float | None = None) -> str:
    ref = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(
        now, tz=timezone.utc
    )
    return (ref - timedelta(days=1)).strftime("%Y-%m-%d")


def day_segments(archive_dir: Path, day: str) -> list[dict[str, Any]]:
    return [m for m in seg_mod.list_manifests(archive_dir) if m.get("utc_date") == day]


def check_day(archive_dir: Path, day: str) -> tuple[bool, str, list[dict[str, Any]]]:
    segments = day_segments(archive_dir, day)
    if not segments:
        return False, f"no segments found for {day}", []
    bad = [s for s in segments if s.get("segment_state") != frozen.SEGMENT_COMPLETE]
    if bad:
        states = ", ".join(f"{s['filename']}={s['segment_state']}" for s in bad)
        return False, f"{len(bad)} of {len(segments)} not COMPLETE: {states}", segments
    return True, f"{len(segments)} segment(s), all COMPLETE", segments


def check_day_frames(
    segments: list[dict[str, Any]], day: str
) -> tuple[bool, str, int]:
    """
    Fact 4: the aggregate raw frame count over `day`'s finalized segments.

    One number for the whole day. A day whose segments all finalized COMPLETE
    but banked nothing is NOT healthy: COMPLETE says the segment closed cleanly,
    which an empty recorder does perfectly well every midnight.
    """
    total = sum(int(s.get("raw_frame_count") or 0) for s in segments)
    if not segments:
        return False, f"no segments found for {day}", 0
    if total <= 0:
        return (
            False,
            f"{len(segments)} segment(s) finalized but banked 0 raw frames "
            f"-- the recorder ran and stored nothing",
            0,
        )
    return True, f"{total:,} raw frames across {len(segments)} segment(s)", total


def report(
    archive_dir: Path,
    reserve_bytes: int,
    *,
    service: str = DEFAULT_SERVICE,
    day: str | None = None,
    now: float | None = None,
) -> HealthReport:
    day = day or yesterday_utc(now)
    active, detail = service_active(service)
    complete, day_detail, segments = check_day(Path(archive_dir), day)
    frames_ok, frames_detail, frame_count = check_day_frames(segments, day)
    status = disk.check(Path(archive_dir), reserve_bytes)
    return HealthReport(
        service_name=service,
        service_active=active,
        service_detail=detail,
        day=day,
        day_segments_complete=complete,
        day_detail=day_detail,
        day_segments=[
            {
                "filename": s.get("filename"),
                "segment_state": s.get("segment_state"),
                "close_reason": s.get("close_reason"),
                "raw_frame_count": s.get("raw_frame_count", 0),
                "byte_size": s.get("byte_size", 0),
                "integrity_check": s.get("integrity_check"),
                "sha256": s.get("sha256"),
                "any_census_incomplete": s.get("any_census_incomplete"),
            }
            for s in segments
        ],
        disk_ok=not status.below_reserve,
        disk_status=status.summary(),
        day_frames_ok=frames_ok,
        day_raw_frame_count=frame_count,
        day_frames_detail=frames_detail,
    )


__all__ = ["HealthReport", "report", "service_active", "check_day",
           "check_day_frames", "yesterday_utc", "DEFAULT_SERVICE"]
