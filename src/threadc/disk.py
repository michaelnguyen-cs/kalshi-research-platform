"""
Free-space monitoring and the low-disk halt.

The production reserve is frozen as

    R = max(10 GiB, 48 x B_1h)

where B_1h is bytes written in one hour, measured in the one-hour high-activity
capacity run. That run has not happened, so R is NOT computed here and is not
guessed anywhere in this package: it is supplied as configuration
(threadc.config.disk_reserve_bytes) and the recorder refuses to start without it.

When free space is at or below R the recorder stops accepting new writes,
finalizes the current segment as HALTED_LOW_DISK, logs the halt prominently and
exits 75 -- which systemd is configured not to restart. It does not wait for the
disk to reach zero: a filesystem that fills while SQLite is mid-transaction
corrupts the archive it was supposed to preserve, and 48 hours of headroom is
what makes the halt an operational message to a human rather than a race.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiskStatus:
    """One sample of the archive filesystem. No data content, only capacity."""

    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    reserve_bytes: int

    @property
    def below_reserve(self) -> bool:
        """At or below R. 'At' counts: the spec's threshold is inclusive."""
        return self.free_bytes <= self.reserve_bytes

    @property
    def headroom_bytes(self) -> int:
        return self.free_bytes - self.reserve_bytes

    def summary(self) -> dict[str, object]:
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "free_gib": round(self.free_bytes / 1024**3, 2),
            "reserve_bytes": self.reserve_bytes,
            "reserve_gib": round(self.reserve_bytes / 1024**3, 2),
            "headroom_bytes": self.headroom_bytes,
            "below_reserve": self.below_reserve,
        }


def check(path: Path, reserve_bytes: int) -> DiskStatus:
    """
    Sample the filesystem holding `path`.

    Measures the nearest existing ancestor, so a check made before the archive
    directory exists still reports the filesystem the archive will land on
    rather than raising.
    """
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    usage = shutil.disk_usage(str(p))
    return DiskStatus(
        path=str(path),
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        reserve_bytes=int(reserve_bytes),
    )


__all__ = ["DiskStatus", "check"]
