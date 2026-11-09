"""
Public-mirror skips.

This repository is a sanitized public mirror. Two things the private repository
has are deliberately absent here:

  * data/ -- historical econ release CSVs the base-rate strategy loads at
    construction time;
  * git history -- the depthrec integrity tests compare files against the
    pre-registered frozen commit (6ca4101) with `git show`.

The tests that need them are skipped, never deleted, and only when the resource
is actually missing, so they still run in full in the private repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_FROZEN_COMMIT = "6ca4101"

_NEEDS_DATA = {
    "tests/test_base_rate.py::test_evaluate_allowed_within_entry_window_produces_signal",
    "tests/test_base_rate.py::test_evaluate_blocked_after_release_has_passed",
    "tests/test_base_rate.py::test_evaluate_blocked_beyond_entry_window",
    "tests/test_base_rate.py::test_evaluate_blocked_within_stand_down_minutes",
    "tests/test_base_rate.py::test_evaluate_cpi_ticker_within_entry_window",
    "tests/test_base_rate.py::test_evaluate_event_driver_is_us_macro",
    "tests/test_base_rate.py::test_evaluate_name_is_base_rate",
    "tests/test_base_rate.py::test_evaluate_payrolls_ticker_within_entry_window",
    "tests/test_base_rate.py::test_evaluate_tradeable_band_blocks_extreme_price",
    "tests/test_base_rate.py::test_evaluate_unknown_release_period_returns_empty",
    "tests/test_base_rate.py::test_evaluate_unparseable_ticker_returns_empty",
    "tests/test_cli.py::test_resolve_all_loads_every_configured_strategy",
    "tests/test_econ_history.py::test_load_cpi_history_nonempty_and_sorted",
    "tests/test_econ_history.py::test_load_fomc_history_decisions_are_valid_labels",
    "tests/test_econ_history.py::test_load_fomc_history_nonempty_and_sorted",
    "tests/test_econ_history.py::test_load_payrolls_history_nonempty_and_sorted",
    "tests/test_econ_history.py::test_load_release_calendar_nonempty_and_sorted",
    "tests/test_econ_history.py::test_next_release_for_returns_none_past_calendar_end",
    "tests/test_econ_history.py::test_next_release_for_returns_soonest_upcoming",
    "tests/test_econ_history.py::test_release_for_period_finds_known_entry",
    "tests/test_econ_history.py::test_release_for_period_returns_none_for_unknown_period",
}

_NEEDS_GIT_HISTORY = {
    "tests/test_depthrec_integrity.py::test_expected_values_were_generated_against_the_section_2_frozen_commit",
    "tests/test_depthrec_integrity.py::test_protected_elements_are_identical_at_the_frozen_commit_and_at_head",
    "tests/test_depthrec_integrity.py::test_the_whole_longshot_file_really_did_change_after_the_freeze",
}

_ECON_FILES = (
    "econ_release_calendar_2026.csv",
    "econ_fomc_history.csv",
    "econ_cpi_history.csv",
    "econ_payrolls_history.csv",
)


def _have_econ_data() -> bool:
    return all((_REPO / "data" / f).is_file() for f in _ECON_FILES)


def _have_frozen_commit() -> bool:
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO), "rev-parse", "--verify", "--quiet",
             f"{_FROZEN_COMMIT}^{{commit}}"],
            capture_output=True,
        ).returncode == 0
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    have_data = _have_econ_data()
    have_git = _have_frozen_commit()
    skip_data = pytest.mark.skip(
        reason="public mirror: requires private historical econ CSVs under data/")
    skip_git = pytest.mark.skip(
        reason=f"public mirror: requires private git history (frozen commit {_FROZEN_COMMIT})")
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if not have_data and nodeid in _NEEDS_DATA:
            item.add_marker(skip_data)
        elif not have_git and nodeid in _NEEDS_GIT_HISTORY:
            item.add_marker(skip_git)
