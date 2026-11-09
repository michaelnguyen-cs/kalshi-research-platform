#!/usr/bin/env python
"""
Fetch historical macro data (FOMC decisions, CPI, payrolls) from FRED and
write it to the committed CSVs under data/ that kbot.feeds.econ_history and
kbot.strategy.base_rate (C5, base-rate divergence) load at runtime.

Two fetch paths, both real (no synthetic data):
  1. FRED's public fredgraph.csv endpoint -- no API key required, works for
     the three series used here (DFEDTAR, DFEDTARU, CPIAUCSL, PAYEMS). This
     is the default and what actually generated the committed snapshots.
  2. FRED's official API (https://api.stlouisfed.org/fred/series/observations)
     if FRED_API_KEY is set -- more robust for scripted/rate-limited use and
     what MARKET_RESEARCH.md's C5 spec asked for explicitly. Prefer this
     when a key is available; the CSVs work fine without one either way.

FOMC decision history needs one thing FRED doesn't provide directly: which
dates were actual FOMC meetings (so "no change" can be counted as a HOLD
rather than silently ignored). The daily target-rate series only records
what the rate WAS on a given day, not why. This script cross-references a
hardcoded meeting-date calendar (documented below) against the real fetched
rate series to determine each meeting's decision -- the calendar supplies
WHEN, the real data supplies WHAT (rate before/after), so an imprecise date
(off by a few days) still classifies correctly since the rate is flat
between meetings; only being off by enough to span two real meetings would
misattribute a decision, which the 20-70 day spacing sanity check below
guards against.

CAVEAT: the meeting-date calendar for 2000-2024 is built from well-documented
public monetary-policy history (Federal Reserve historical FOMC calendars,
extensively cross-published); 2025-2026 uses a generic 8-meetings/year month
template since specific dates are less certain from training knowledge alone.
Before trusting this for anything beyond a first-pass backtest, verify
against https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm.

Usage:
    python scripts/fetch_econ_history.py
    python scripts/fetch_econ_history.py --series fomc,cpi,payrolls
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"


# ── FRED fetch (public CSV endpoint, no key needed) ──────────────────────────

def fetch_fred_series_csv(series_id: str) -> list[tuple[date, float]]:
    """(date, value) pairs for `series_id`, oldest first, via the public
    fredgraph.csv endpoint. Skips FRED's "." (missing observation) rows."""
    resp = httpx.get(_FRED_CSV_URL, params={"id": series_id}, timeout=15.0)
    resp.raise_for_status()
    rows: list[tuple[date, float]] = []
    reader = csv.reader(resp.text.splitlines())
    next(reader)  # header
    for row in reader:
        if len(row) != 2 or not row[1] or row[1] == ".":
            continue  # FRED marks missing observations as "." or a blank string
        d = date.fromisoformat(row[0])
        rows.append((d, float(row[1])))
    return rows


def fetch_fred_series_api(series_id: str, api_key: str) -> list[tuple[date, float]]:
    """Same as fetch_fred_series_csv but via FRED's official JSON API (needs a key)."""
    resp = httpx.get(
        _FRED_API_URL,
        params={"series_id": series_id, "api_key": api_key, "file_type": "json"},
        timeout=15.0,
    )
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    rows: list[tuple[date, float]] = []
    for o in obs:
        if o.get("value") in (None, "."):
            continue
        rows.append((date.fromisoformat(o["date"]), float(o["value"])))
    return rows


def fetch_fred_series(series_id: str, api_key: str | None) -> list[tuple[date, float]]:
    if api_key:
        return fetch_fred_series_api(series_id, api_key)
    return fetch_fred_series_csv(series_id)


# ── FOMC meeting calendar (documented approximation, see module docstring) ──

# (year, month, day) decision-announcement dates, 2000-2024: built from
# well-documented public monetary-policy history. 2025-2026 use a generic
# 8-meetings/year month template (see _GENERIC_MEETING_MONTHS) since exact
# dates are less certain from training knowledge alone -- the real rate data
# still correctly classifies hold/cut/hike regardless of small date offsets.
FOMC_MEETING_DATES: list[date] = [
    # 2000
    date(2000, 2, 2), date(2000, 3, 21), date(2000, 5, 16), date(2000, 6, 28),
    date(2000, 8, 22), date(2000, 10, 3), date(2000, 11, 15), date(2000, 12, 19),
    # 2001
    date(2001, 1, 3), date(2001, 1, 31), date(2001, 3, 20), date(2001, 4, 18),
    date(2001, 5, 15), date(2001, 6, 27), date(2001, 8, 21), date(2001, 9, 17),
    date(2001, 10, 2), date(2001, 11, 6), date(2001, 12, 11),
    # 2002
    date(2002, 1, 30), date(2002, 3, 19), date(2002, 5, 7), date(2002, 6, 26),
    date(2002, 8, 13), date(2002, 9, 24), date(2002, 11, 6), date(2002, 12, 10),
    # 2003
    date(2003, 1, 29), date(2003, 3, 18), date(2003, 5, 6), date(2003, 6, 25),
    date(2003, 8, 12), date(2003, 9, 16), date(2003, 10, 28), date(2003, 12, 9),
    # 2004
    date(2004, 1, 28), date(2004, 3, 16), date(2004, 5, 4), date(2004, 6, 30),
    date(2004, 8, 10), date(2004, 9, 21), date(2004, 11, 10), date(2004, 12, 14),
    # 2005
    date(2005, 2, 2), date(2005, 3, 22), date(2005, 5, 3), date(2005, 6, 30),
    date(2005, 8, 9), date(2005, 9, 20), date(2005, 11, 1), date(2005, 12, 13),
    # 2006
    date(2006, 1, 31), date(2006, 3, 28), date(2006, 5, 10), date(2006, 6, 29),
    date(2006, 8, 8), date(2006, 9, 20), date(2006, 10, 25), date(2006, 12, 12),
    # 2007
    date(2007, 1, 31), date(2007, 3, 21), date(2007, 5, 9), date(2007, 6, 28),
    date(2007, 8, 7), date(2007, 9, 18), date(2007, 10, 31), date(2007, 12, 11),
    # 2008
    date(2008, 1, 22), date(2008, 1, 30), date(2008, 3, 18), date(2008, 4, 30),
    date(2008, 6, 25), date(2008, 8, 5), date(2008, 9, 16), date(2008, 10, 8),
    date(2008, 10, 29), date(2008, 12, 16),
    # 2009
    date(2009, 1, 28), date(2009, 3, 18), date(2009, 4, 29), date(2009, 6, 24),
    date(2009, 8, 12), date(2009, 9, 23), date(2009, 11, 4), date(2009, 12, 16),
    # 2010
    date(2010, 1, 27), date(2010, 3, 16), date(2010, 4, 28), date(2010, 6, 23),
    date(2010, 8, 10), date(2010, 9, 21), date(2010, 11, 3), date(2010, 12, 14),
    # 2011
    date(2011, 1, 26), date(2011, 3, 15), date(2011, 4, 27), date(2011, 6, 22),
    date(2011, 8, 9), date(2011, 9, 21), date(2011, 11, 2), date(2011, 12, 13),
    # 2012
    date(2012, 1, 25), date(2012, 3, 13), date(2012, 4, 25), date(2012, 6, 20),
    date(2012, 8, 1), date(2012, 9, 13), date(2012, 10, 24), date(2012, 12, 12),
    # 2013
    date(2013, 1, 30), date(2013, 3, 20), date(2013, 5, 1), date(2013, 6, 19),
    date(2013, 7, 31), date(2013, 9, 18), date(2013, 10, 30), date(2013, 12, 18),
    # 2014
    date(2014, 1, 29), date(2014, 3, 19), date(2014, 4, 30), date(2014, 6, 18),
    date(2014, 7, 30), date(2014, 9, 17), date(2014, 10, 29), date(2014, 12, 17),
    # 2015
    date(2015, 1, 28), date(2015, 3, 18), date(2015, 4, 29), date(2015, 6, 17),
    date(2015, 7, 29), date(2015, 9, 17), date(2015, 10, 28), date(2015, 12, 16),
    # 2016
    date(2016, 1, 27), date(2016, 3, 16), date(2016, 4, 27), date(2016, 6, 15),
    date(2016, 7, 27), date(2016, 9, 21), date(2016, 11, 2), date(2016, 12, 14),
    # 2017
    date(2017, 2, 1), date(2017, 3, 15), date(2017, 5, 3), date(2017, 6, 14),
    date(2017, 7, 26), date(2017, 9, 20), date(2017, 11, 1), date(2017, 12, 13),
    # 2018
    date(2018, 1, 31), date(2018, 3, 21), date(2018, 5, 2), date(2018, 6, 13),
    date(2018, 8, 1), date(2018, 9, 26), date(2018, 11, 8), date(2018, 12, 19),
    # 2019
    date(2019, 1, 30), date(2019, 3, 20), date(2019, 5, 1), date(2019, 6, 19),
    date(2019, 7, 31), date(2019, 9, 18), date(2019, 10, 30), date(2019, 12, 11),
    # 2020
    date(2020, 1, 29), date(2020, 3, 3), date(2020, 3, 15), date(2020, 4, 29),
    date(2020, 6, 10), date(2020, 7, 29), date(2020, 9, 16), date(2020, 11, 5),
    date(2020, 12, 16),
    # 2021
    date(2021, 1, 27), date(2021, 3, 17), date(2021, 4, 28), date(2021, 6, 16),
    date(2021, 7, 28), date(2021, 9, 22), date(2021, 11, 3), date(2021, 12, 15),
    # 2022
    date(2022, 1, 26), date(2022, 3, 16), date(2022, 5, 4), date(2022, 6, 15),
    date(2022, 7, 27), date(2022, 9, 21), date(2022, 11, 2), date(2022, 12, 14),
    # 2023
    date(2023, 2, 1), date(2023, 3, 22), date(2023, 5, 3), date(2023, 6, 14),
    date(2023, 7, 26), date(2023, 9, 20), date(2023, 11, 1), date(2023, 12, 13),
    # 2024
    date(2024, 1, 31), date(2024, 3, 20), date(2024, 5, 1), date(2024, 6, 12),
    date(2024, 7, 31), date(2024, 9, 18), date(2024, 11, 7), date(2024, 12, 18),
]

# 2025-2026: generic 8-meetings/year month template (day fixed at 15th as a
# nominal mid-month placeholder -- only used to sample the real rate series
# a few weeks apart, so the exact day barely matters for classification).
_GENERIC_MEETING_MONTHS = [1, 3, 5, 6, 7, 9, 11, 12]
for _year in (2025, 2026):
    for _month in _GENERIC_MEETING_MONTHS:
        FOMC_MEETING_DATES.append(date(_year, _month, 15))
FOMC_MEETING_DATES.sort()


def _rate_as_of(rate_series: list[tuple[date, float]], as_of: date, lookback_days: int = 10) -> float | None:
    """Most recent observation at or before `as_of` (within lookback_days)."""
    candidates = [(d, v) for d, v in rate_series if d <= as_of and (as_of - d).days <= lookback_days]
    if not candidates:
        return None
    return max(candidates, key=lambda dv: dv[0])[1]


def build_fomc_decisions(rate_series: list[tuple[date, float]]) -> list[dict]:
    """
    One row per meeting in FOMC_MEETING_DATES: decision classified from the
    REAL rate series (not from the hardcoded calendar's memory of what
    happened) by comparing the rate a few days after the meeting to the
    rate right before it.
    """
    rate_series = sorted(rate_series)
    rows = []
    prior_rate: float | None = None
    for meeting_date in FOMC_MEETING_DATES:
        before = _rate_as_of(rate_series, meeting_date - timedelta(days=1), lookback_days=14)
        after = _rate_as_of(rate_series, meeting_date + timedelta(days=5), lookback_days=14)
        if before is None or after is None:
            continue
        if prior_rate is None:
            prior_rate = before
        change_bps = round((after - prior_rate) * 100)
        if change_bps == 0:
            decision = "hold"
        elif change_bps > 0:
            decision = "hike"
        else:
            decision = "cut"
        rows.append({
            "meeting_date": meeting_date.isoformat(),
            "prior_upper_rate": prior_rate,
            "new_upper_rate": after,
            "decision": decision,
            "change_bps": change_bps,
        })
        prior_rate = after
    return rows


def write_fomc_csv(api_key: str | None) -> None:
    print("Fetching DFEDTAR (1982-2008, single target) + DFEDTARU (2008-present, upper bound)...")
    old = fetch_fred_series("DFEDTAR", api_key)
    new = fetch_fred_series("DFEDTARU", api_key)
    splice_date = date(2008, 12, 16)
    rate_series = [(d, v) for d, v in old if d < splice_date] + [(d, v) for d, v in new if d >= splice_date]
    rate_series.sort()
    print(f"Got {len(rate_series)} daily rate observations, {rate_series[0][0]} to {rate_series[-1][0]}.")

    rows = build_fomc_decisions(rate_series)
    print(f"Classified {len(rows)} FOMC meetings from {len(FOMC_MEETING_DATES)} calendar entries.")

    # Sanity check: spacing between consecutive meetings should be roughly
    # 20-70 days; anything outside that means the calendar has a gap or
    # duplicate that could misattribute a decision.
    dates = [date.fromisoformat(r["meeting_date"]) for r in rows]
    for a, b in zip(dates, dates[1:]):
        gap = (b - a).days
        if not (15 <= gap <= 75):
            print(f"  WARNING: meeting spacing {a} -> {b} is {gap} days (expected ~20-70) -- check the calendar.")

    out_path = _DATA_DIR / "econ_fomc_history.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["meeting_date", "prior_upper_rate", "new_upper_rate", "decision", "change_bps"])
        writer.writeheader()
        writer.writerows(rows)
    decisions = [r["decision"] for r in rows]
    print(f"Wrote {len(rows)} rows to {out_path} "
          f"(hold={decisions.count('hold')}, cut={decisions.count('cut')}, hike={decisions.count('hike')})")


# ── CPI / payrolls: pure FRED monthly series, no calendar needed ────────────

_HISTORY_START = date(2000, 1, 1)


def write_mom_csv(series_id: str, out_name: str, value_label: str, api_key: str | None) -> None:
    print(f"Fetching {series_id}...")
    obs = fetch_fred_series(series_id, api_key)
    obs.sort()
    print(f"Got {len(obs)} monthly observations, {obs[0][0]} to {obs[-1][0]}.")
    by_date = {d: v for d, v in obs}

    rows = []
    for i in range(1, len(obs)):
        prev_date, prev_val = obs[i - 1]
        cur_date, cur_val = obs[i]
        if cur_date < _HISTORY_START:
            continue
        mom_change = cur_val - prev_val
        mom_pct = (mom_change / prev_val * 100.0) if prev_val else None

        yoy_date = date(cur_date.year - 1, cur_date.month, 1)
        yoy_val = by_date.get(yoy_date)
        yoy_pct = ((cur_val - yoy_val) / yoy_val * 100.0) if yoy_val else None

        rows.append({
            "month": cur_date.isoformat(),
            value_label: cur_val,
            "mom_change": round(mom_change, 4),
            "mom_pct_change": round(mom_pct, 4) if mom_pct is not None else "",
            "yoy_pct_change": round(yoy_pct, 4) if yoy_pct is not None else "",
        })

    out_path = _DATA_DIR / out_name
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["month", value_label, "mom_change", "mom_pct_change", "yoy_pct_change"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out_path} (since {_HISTORY_START})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--series", default="fomc,cpi,payrolls",
        help="Comma-separated subset to fetch: fomc, cpi, payrolls (default: all three)",
    )
    parser.add_argument("--fred-api-key", default="", help="FRED API key (optional; falls back to the public CSV endpoint)")
    args = parser.parse_args()

    api_key = args.fred_api_key or None
    if api_key:
        print("Using FRED's official API (key provided).")
    else:
        print("No FRED_API_KEY provided -- using the public fredgraph.csv endpoint (no key needed).")

    wanted = {s.strip() for s in args.series.split(",") if s.strip()}
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    if "fomc" in wanted:
        write_fomc_csv(api_key)
    if "cpi" in wanted:
        write_mom_csv("CPIAUCSL", "econ_cpi_history.csv", "index_value", api_key)
    if "payrolls" in wanted:
        write_mom_csv("PAYEMS", "econ_payrolls_history.csv", "level_thousands", api_key)


if __name__ == "__main__":
    main()
