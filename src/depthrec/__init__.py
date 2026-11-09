"""
depthrec — Thread A research book recorder.

Implements research/THREAD_A_EXECUTION_MECHANICS_2026-08-22.md (frozen at
commit e7c8c87). Read-only against Kalshi: this package never places, edits or
cancels an order and must never be edited to.

Deliberately a SEPARATE package from `kbot`. The trading bot keeps
`use_yes_price: False` (§6); this recorder is frozen on `True`. Nothing here
imports kbot.execution, and nothing in kbot imports depthrec.
"""

__all__ = ["frozen"]
