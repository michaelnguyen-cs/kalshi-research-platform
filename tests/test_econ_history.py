"""Tests for kbot.feeds.econ_history — base-rate math, smoothing, conditioning, calendar."""

from __future__ import annotations

import pytest

from kbot.feeds.econ_history import (
    FOMC_BUCKETS,
    FomcMeeting,
    MonthlyPrint,
    fomc_base_rates,
    fomc_bucket,
    laplace_smooth,
    laplace_smooth_binary,
    load_cpi_history,
    load_fomc_history,
    load_payrolls_history,
    load_release_calendar,
    mom_threshold_base_rate,
    next_release_for,
    release_for_period,
    trend_bucket,
)


# ── Laplace smoothing ─────────────────────────────────────────────────────────

def test_laplace_smooth_uniform_when_no_data():
    result = laplace_smooth({}, ["a", "b", "c"], alpha=1.0)
    assert result == pytest.approx({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})


def test_laplace_smooth_matches_manual_calc():
    counts = {"a": 8, "b": 2}
    result = laplace_smooth(counts, ["a", "b"], alpha=1.0)
    # (8+1)/(10+2)=0.75, (2+1)/(10+2)=0.25
    assert result["a"] == pytest.approx(0.75)
    assert result["b"] == pytest.approx(0.25)


def test_laplace_smooth_never_zero_or_one():
    counts = {"a": 100, "b": 0}
    result = laplace_smooth(counts, ["a", "b"], alpha=1.0)
    assert result["b"] > 0.0
    assert result["a"] < 1.0


def test_laplace_smooth_sums_to_one():
    counts = {"a": 3, "b": 5, "c": 0}
    result = laplace_smooth(counts, ["a", "b", "c"], alpha=1.0)
    assert sum(result.values()) == pytest.approx(1.0)


def test_laplace_smooth_binary_matches_manual_calc():
    # (7+1)/(10+2) = 0.6667
    assert laplace_smooth_binary(7, 10, alpha=1.0) == pytest.approx(8 / 12)


def test_laplace_smooth_binary_never_zero_with_no_successes():
    assert laplace_smooth_binary(0, 20, alpha=1.0) > 0.0


def test_laplace_smooth_binary_never_one_with_all_successes():
    assert laplace_smooth_binary(20, 20, alpha=1.0) < 1.0


# ── FOMC bucket mapping ────────────────────────────────────────────────────────

def _meeting(change_bps: int, decision: str | None = None) -> FomcMeeting:
    if decision is None:
        decision = "hold" if change_bps == 0 else ("hike" if change_bps > 0 else "cut")
    return FomcMeeting(meeting_date="2020-01-01", prior_upper_rate=1.0, new_upper_rate=1.0 + change_bps / 100, decision=decision, change_bps=change_bps)


def test_fomc_bucket_hold():
    assert fomc_bucket(_meeting(0)) == "H0"


def test_fomc_bucket_hike25():
    assert fomc_bucket(_meeting(25)) == "H25"


def test_fomc_bucket_hike_large():
    assert fomc_bucket(_meeting(50)) == "H26"
    assert fomc_bucket(_meeting(75)) == "H26"


def test_fomc_bucket_cut25():
    assert fomc_bucket(_meeting(-25)) == "C25"


def test_fomc_bucket_cut_large():
    assert fomc_bucket(_meeting(-50)) == "C26"
    assert fomc_bucket(_meeting(-100)) == "C26"


# ── FOMC base rates (unconditional + conditional) ────────────────────────────

def _history(decisions: list[str]) -> list[FomcMeeting]:
    bps_map = {"hold": 0, "cut": -25, "hike": 25}
    return [_meeting(bps_map[d], decision=d) for d in decisions]


def test_fomc_base_rates_unconditional_sums_to_one():
    history = _history(["hold"] * 6 + ["cut"] * 3 + ["hike"] * 1)
    rates = fomc_base_rates(history)
    assert set(rates.keys()) == set(FOMC_BUCKETS)
    assert sum(rates.values()) == pytest.approx(1.0)


def test_fomc_base_rates_unconditional_matches_dominant_bucket():
    history = _history(["hold"] * 9 + ["hike"])
    rates = fomc_base_rates(history)
    assert rates["H0"] > rates["H25"]
    assert rates["H0"] > 0.5


def test_fomc_base_rates_conditional_on_trailing_holds():
    # After 3 consecutive holds, historically it stayed a hold every time in
    # this synthetic set -- conditional rate should exceed the unconditional.
    history = _history(["cut"] * 3 + ["hold"] * 3 + ["hold"] + ["hold"] * 3 + ["hold"] + ["hold"] * 3 + ["hold"])
    unconditional = fomc_base_rates(history)
    conditional = fomc_base_rates(history, trailing_pattern=("hold", "hold", "hold"))
    assert conditional["H0"] >= unconditional["H0"]


def test_fomc_base_rates_falls_back_when_no_precedent_matches():
    # A trailing pattern that never occurred in history should fall back to
    # the unconditional distribution rather than returning nonsense/empty.
    history = _history(["hold"] * 10)
    rates = fomc_base_rates(history, trailing_pattern=("cut", "hike", "cut"))
    unconditional = fomc_base_rates(history)
    assert rates == unconditional


def test_fomc_base_rates_conditional_requires_exact_length_match():
    history = _history(["hold", "hold", "cut", "hold", "hold", "hold"])
    # trailing_pattern of length 2 should only look at the immediately
    # preceding 2 decisions, independent of a longer history.
    rates = fomc_base_rates(history, trailing_pattern=("hold", "hold"))
    assert set(rates.keys()) == set(FOMC_BUCKETS)


# ── trend_bucket ──────────────────────────────────────────────────────────────

def _print(month: str, mom_pct: float | None) -> MonthlyPrint:
    return MonthlyPrint(month=month, value=100.0, mom_change=mom_pct or 0.0, mom_pct_change=mom_pct, yoy_pct_change=None)


def test_trend_bucket_rising():
    history = [_print(f"2020-{m:02d}-01", 0.3) for m in range(1, 5)]
    assert trend_bucket(history, index=4, window=3) == "rising"


def test_trend_bucket_falling():
    history = [_print(f"2020-{m:02d}-01", -0.3) for m in range(1, 5)]
    assert trend_bucket(history, index=4, window=3) == "falling"


def test_trend_bucket_flat():
    history = [_print(f"2020-{m:02d}-01", 0.01) for m in range(1, 5)]
    assert trend_bucket(history, index=4, window=3) == "flat"


def test_trend_bucket_is_causal_excludes_current_index():
    # The value AT index 3 is a huge outlier; trend_bucket(history, 3, ...)
    # must not see it (only history[0:3]), so it should stay flat/rising
    # from the earlier small values, not be dragged by the outlier at index 3.
    history = [_print("2020-01-01", 0.01), _print("2020-02-01", 0.01), _print("2020-03-01", 0.01), _print("2020-04-01", 50.0)]
    assert trend_bucket(history, index=3, window=3) == "flat"


def test_trend_bucket_empty_window_defaults_flat():
    assert trend_bucket([], index=0, window=3) == "flat"


# ── mom_threshold_base_rate ────────────────────────────────────────────────────

def test_mom_threshold_base_rate_basic():
    # 7 of 10 months exceed 0.2%
    history = [_print(f"2020-{m:02d}-01", 0.3) for m in range(1, 8)] + [_print(f"2020-{m:02d}-01", 0.1) for m in range(8, 11)]
    rate = mom_threshold_base_rate(history, 0.2)
    assert rate == pytest.approx(laplace_smooth_binary(7, 10))


def test_mom_threshold_base_rate_direction_less():
    history = [_print(f"2020-{m:02d}-01", -0.5) for m in range(1, 4)] + [_print(f"2020-{m:02d}-01", 0.5) for m in range(4, 7)]
    rate = mom_threshold_base_rate(history, 0.0, direction="less")
    assert rate == pytest.approx(laplace_smooth_binary(3, 6))


def test_mom_threshold_base_rate_raw_value_field():
    history = [MonthlyPrint(month=f"2020-{m:02d}-01", value=100.0, mom_change=150.0, mom_pct_change=1.0, yoy_pct_change=None) for m in range(1, 4)]
    history += [MonthlyPrint(month=f"2020-{m:02d}-01", value=100.0, mom_change=-50.0, mom_pct_change=-0.5, yoy_pct_change=None) for m in range(4, 7)]
    # raw mom_change > 100 true for 3/6 months
    rate = mom_threshold_base_rate(history, 100.0, value_field="mom_change")
    assert rate == pytest.approx(laplace_smooth_binary(3, 6))


def test_mom_threshold_base_rate_condition_trend_filters():
    rising = [_print(f"2020-{m:02d}-01", 0.3) for m in range(1, 5)]  # all rising trend + all hits
    falling = [_print(f"2021-{m:02d}-01", -0.3) for m in range(1, 5)]  # all falling trend + all misses
    history = rising + falling
    rate_rising = mom_threshold_base_rate(history, 0.0, condition_trend="rising")
    rate_falling = mom_threshold_base_rate(history, 0.0, condition_trend="falling")
    assert rate_rising > rate_falling


def test_mom_threshold_base_rate_condition_trend_falls_back_when_no_match():
    history = [_print(f"2020-{m:02d}-01", 0.3) for m in range(1, 8)]  # all rising, no "falling" precedent
    rate_falling = mom_threshold_base_rate(history, 0.2, condition_trend="falling")
    rate_unconditional = mom_threshold_base_rate(history, 0.2)
    assert rate_falling == pytest.approx(rate_unconditional)


def test_mom_threshold_base_rate_ignores_none_values():
    history = [_print("2020-01-01", None), _print("2020-02-01", 0.5), _print("2020-03-01", 0.5)]
    rate = mom_threshold_base_rate(history, 0.2)
    assert rate == pytest.approx(laplace_smooth_binary(2, 2))


# ── Loaders (real shipped CSVs) ──────────────────────────────────────────────

def test_load_fomc_history_nonempty_and_sorted():
    history = load_fomc_history()
    assert len(history) > 100  # since-2000 history, ~8 meetings/year
    dates = [m.meeting_date for m in history]
    assert dates == sorted(dates)


def test_load_fomc_history_decisions_are_valid_labels():
    history = load_fomc_history()
    assert all(m.decision in ("hold", "cut", "hike") for m in history)


def test_load_cpi_history_nonempty_and_sorted():
    history = load_cpi_history()
    assert len(history) > 100
    months = [p.month for p in history]
    assert months == sorted(months)


def test_load_payrolls_history_nonempty_and_sorted():
    history = load_payrolls_history()
    assert len(history) > 100
    months = [p.month for p in history]
    assert months == sorted(months)


def test_load_release_calendar_nonempty_and_sorted():
    calendar = load_release_calendar()
    assert len(calendar) > 0
    ts = [e.release_ts for e in calendar]
    assert ts == sorted(ts)
    assert all(e.event_type in ("fomc", "cpi", "jobs") for e in calendar)


def test_release_for_period_finds_known_entry():
    calendar = load_release_calendar()
    event = release_for_period(calendar, "fomc", "2026-07")
    assert event is not None
    assert event.data_period == "2026-07"


def test_release_for_period_returns_none_for_unknown_period():
    calendar = load_release_calendar()
    assert release_for_period(calendar, "fomc", "1999-01") is None


def test_next_release_for_returns_soonest_upcoming():
    calendar = load_release_calendar()
    event = next_release_for(calendar, "cpi", as_of_ts=0.0)  # epoch -- everything is "upcoming"
    assert event is not None
    assert event.event_type == "cpi"
    # Should be the earliest cpi event in the calendar.
    cpi_events = [e for e in calendar if e.event_type == "cpi"]
    assert event.release_ts == min(e.release_ts for e in cpi_events)


def test_next_release_for_returns_none_past_calendar_end():
    calendar = load_release_calendar()
    far_future = max(e.release_ts for e in calendar) + 86400 * 365
    assert next_release_for(calendar, "fomc", as_of_ts=far_future) is None
