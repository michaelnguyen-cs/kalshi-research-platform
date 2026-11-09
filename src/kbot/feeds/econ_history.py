"""
Historical macro data loaders + empirical base-rate computation for the C5
base-rate divergence strategy (MARKET_RESEARCH.md Source 4, the MAXIMUS
pivot). Retail-dominated macro markets (FOMC, CPI, jobs) systematically
misprice against decades of historical base rates because retail anchors on
narrative/recency; this module supplies the "decades of clean data" side of
that trade.

Data sources (see scripts/fetch_econ_history.py for provenance):
  data/econ_fomc_history.csv       -- every FOMC decision since 2000 (real
                                       FRED DFEDTAR/DFEDTARU data; meeting
                                       DATES are a documented best-effort
                                       calendar, but decisions/rates are read
                                       directly from the real series).
  data/econ_cpi_history.csv        -- monthly CPI MoM/YoY since 2000 (FRED
                                       CPIAUCSL).
  data/econ_payrolls_history.csv   -- monthly nonfarm payrolls MoM since 2000
                                       (FRED PAYEMS).
  data/econ_release_calendar_2026.csv -- scheduled FOMC/CPI/jobs release
                                       datetimes (UTC), 2026 plus H2 2025 (for
                                       backtest continuity), for entry-window
                                       gating. Best-effort estimate from the
                                       standard FOMC Tue/Wed cadence and BLS's
                                       usual CPI (~2nd week)/jobs (1st Friday)
                                       schedule -- verify against
                                       federalreserve.gov and bls.gov before
                                       trusting it for live entries.

Smoothing: Laplace (add-alpha) smoothing throughout, not Wilson -- it
generalizes cleanly to the FOMC 5-way outcome bucket (Wilson is specifically
a binomial-proportion interval) and gives one consistent smoothing rule for
both the discrete FOMC buckets and the binary CPI/payrolls threshold checks.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

# The 5 KXFEDDECISION outcome buckets, in the exact suffixes used by the
# real ticker format (confirmed live 2026-07): C26 (cut >25bp), C25 (cut
# exactly 25bp), H0 (hold), H25 (hike exactly 25bp), H26 (hike >25bp).
FOMC_BUCKETS: tuple[str, ...] = ("C26", "C25", "H0", "H25", "H26")


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class FomcMeeting:
    meeting_date: str          # ISO date
    prior_upper_rate: float
    new_upper_rate: float
    decision: str               # "hold" | "cut" | "hike"
    change_bps: int


@dataclass
class MonthlyPrint:
    month: str                  # ISO date (first of month)
    value: float                 # index level / employment level
    mom_change: float
    mom_pct_change: float | None
    yoy_pct_change: float | None


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_fomc_history(path: Path | None = None) -> list[FomcMeeting]:
    path = path or (_DATA_DIR / "econ_fomc_history.csv")
    meetings = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            meetings.append(FomcMeeting(
                meeting_date=row["meeting_date"],
                prior_upper_rate=float(row["prior_upper_rate"]),
                new_upper_rate=float(row["new_upper_rate"]),
                decision=row["decision"],
                change_bps=int(row["change_bps"]),
            ))
    return sorted(meetings, key=lambda m: m.meeting_date)


def _load_monthly_csv(path: Path, value_field: str) -> list[MonthlyPrint]:
    prints = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            mom_pct = row["mom_pct_change"]
            yoy_pct = row["yoy_pct_change"]
            prints.append(MonthlyPrint(
                month=row["month"],
                value=float(row[value_field]),
                mom_change=float(row["mom_change"]),
                mom_pct_change=float(mom_pct) if mom_pct not in ("", None) else None,
                yoy_pct_change=float(yoy_pct) if yoy_pct not in ("", None) else None,
            ))
    return sorted(prints, key=lambda p: p.month)


def load_cpi_history(path: Path | None = None) -> list[MonthlyPrint]:
    return _load_monthly_csv(path or (_DATA_DIR / "econ_cpi_history.csv"), "index_value")


def load_payrolls_history(path: Path | None = None) -> list[MonthlyPrint]:
    return _load_monthly_csv(path or (_DATA_DIR / "econ_payrolls_history.csv"), "level_thousands")


# ── Release calendar ──────────────────────────────────────────────────────────

@dataclass
class ReleaseEvent:
    event_type: str              # "fomc" | "cpi" | "jobs"
    data_period: str              # "YYYY-MM" -- the period this release covers/decides
    release_ts: float             # Unix seconds, UTC
    description: str


def load_release_calendar(path: Path | None = None) -> list[ReleaseEvent]:
    path = path or (_DATA_DIR / "econ_release_calendar_2026.csv")
    events = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            dt = datetime.fromisoformat(row["release_datetime_utc"]).replace(tzinfo=timezone.utc)
            events.append(ReleaseEvent(
                event_type=row["event_type"],
                data_period=row["data_period"],
                release_ts=dt.timestamp(),
                description=row["description"],
            ))
    return sorted(events, key=lambda e: e.release_ts)


def next_release_for(
    calendar: list[ReleaseEvent],
    event_type: str,
    as_of_ts: float,
) -> ReleaseEvent | None:
    """The next `event_type` release at or after `as_of_ts`, or None if there isn't one in the calendar."""
    upcoming = [e for e in calendar if e.event_type == event_type and e.release_ts >= as_of_ts]
    return min(upcoming, key=lambda e: e.release_ts) if upcoming else None


def release_for_period(
    calendar: list[ReleaseEvent],
    event_type: str,
    data_period: str,
) -> ReleaseEvent | None:
    """The `event_type` release covering `data_period` ("YYYY-MM"), or None if not in the calendar."""
    for e in calendar:
        if e.event_type == event_type and e.data_period == data_period:
            return e
    return None


# ── Laplace smoothing ─────────────────────────────────────────────────────────

def laplace_smooth(counts: dict[str, int], all_keys: list[str] | tuple[str, ...], alpha: float = 1.0) -> dict[str, float]:
    """
    Add-alpha smoothed empirical distribution over `all_keys`, so a bucket
    with zero historical occurrences doesn't get claimed as literally
    impossible (0%) and a bucket with 100% of occurrences doesn't get
    claimed as a certainty (100%) -- both are overconfident claims from a
    finite, often small, sample.
    """
    total = sum(counts.get(k, 0) for k in all_keys)
    denom = total + alpha * len(all_keys)
    return {k: (counts.get(k, 0) + alpha) / denom for k in all_keys}


def laplace_smooth_binary(successes: int, total: int, alpha: float = 1.0) -> float:
    """Smoothed P(success) for a binary outcome: (successes + alpha) / (total + 2*alpha)."""
    return (successes + alpha) / (total + 2 * alpha)


# ── FOMC base rate ────────────────────────────────────────────────────────────

def fomc_bucket(meeting: FomcMeeting) -> str:
    """Map a meeting's change_bps to one of FOMC_BUCKETS."""
    bps = meeting.change_bps
    if bps == 0:
        return "H0"
    if bps == 25:
        return "H25"
    if bps >= 50:
        return "H26"
    if bps == -25:
        return "C25"
    return "C26"  # bps <= -50


def fomc_base_rates(
    history: list[FomcMeeting],
    trailing_pattern: tuple[str, ...] | None = None,
    alpha: float = 1.0,
) -> dict[str, float]:
    """
    Smoothed empirical distribution over FOMC_BUCKETS.

    If `trailing_pattern` is given (a tuple of "hold"/"cut"/"hike" labels,
    e.g. ("hold", "hold", "hold") for "the last 3 decisions were holds"),
    only historical meetings immediately following that exact trailing
    sequence are counted -- this is the "conditional base rate" the C5 spec
    calls for (2-3 documented conditions max, no curve-fitting: this is a
    single objective, data-backed condition, not a tuned parameter). Falls
    back to the unconditional distribution if no historical precedent
    matches the pattern (or none is given).
    """
    if trailing_pattern:
        k = len(trailing_pattern)
        matches = [
            history[i] for i in range(k, len(history))
            if tuple(m.decision for m in history[i - k:i]) == trailing_pattern
        ]
        pool = matches if matches else history
    else:
        pool = history

    counts: dict[str, int] = {}
    for m in pool:
        b = fomc_bucket(m)
        counts[b] = counts.get(b, 0) + 1
    return laplace_smooth(counts, FOMC_BUCKETS, alpha=alpha)


# ── CPI / payrolls threshold base rate ────────────────────────────────────────

def trend_bucket(history: list[MonthlyPrint], index: int, window: int = 3, flat_epsilon: float = 0.05) -> str:
    """
    "rising" / "falling" / "flat" trend label from the average mom_pct_change
    over the `window` months strictly before `history[index]` (never
    includes the print being predicted -- this must stay a look-ahead-free,
    pre-release condition).
    """
    start = max(0, index - window)
    trailing = [p.mom_pct_change for p in history[start:index] if p.mom_pct_change is not None]
    if not trailing:
        return "flat"
    avg = sum(trailing) / len(trailing)
    if avg > flat_epsilon:
        return "rising"
    if avg < -flat_epsilon:
        return "falling"
    return "flat"


def mom_threshold_base_rate(
    history: list[MonthlyPrint],
    threshold: float,
    *,
    value_field: str = "mom_pct_change",
    direction: str = "greater",
    condition_trend: str | None = None,
    window: int = 3,
    alpha: float = 1.0,
) -> float:
    """
    Smoothed P(value > threshold) (or < threshold if direction="less"),
    matching KXCPI/KXPAYROLLS's "greater than X" ticker structure.

    `value_field` selects which field of MonthlyPrint to compare against
    `threshold`: "mom_pct_change" (percent -- KXCPI's strikes are MoM %
    points, e.g. "0.2" meaning 0.2%) or "mom_change" (raw units --
    KXPAYROLLS's strikes are raw job counts; MonthlyPrint.mom_change for
    payrolls is in THOUSANDS since PAYEMS itself is reported in thousands,
    so callers must convert a raw-job-count strike to thousands before
    calling this, e.g. strike_raw_jobs / 1000.0).

    If `condition_trend` is given ("rising"/"falling"/"flat"), only months
    whose OWN trailing-3-month trend (computed causally, not including the
    month itself) matches are counted -- the "trailing 3-month trend"
    condition from the C5 spec. Falls back to the unconditional rate if no
    historical month matches the trend bucket.
    """
    def _hits(pool: list[MonthlyPrint]) -> tuple[int, int]:
        successes, total = 0, 0
        for p in pool:
            value = getattr(p, value_field)
            if value is None:
                continue
            total += 1
            is_hit = value > threshold if direction == "greater" else value < threshold
            if is_hit:
                successes += 1
        return successes, total

    if condition_trend is not None:
        matching = [
            history[i] for i in range(len(history))
            if trend_bucket(history, i, window=window) == condition_trend
        ]
        successes, total = _hits(matching)
        if total == 0:
            successes, total = _hits(history)
    else:
        successes, total = _hits(history)

    return laplace_smooth_binary(successes, total, alpha=alpha)
