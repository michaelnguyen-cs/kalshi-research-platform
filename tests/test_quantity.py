"""
Integer-hundredths contract quantities (kbot.quantity, integrity patch item 1).

The bug being pinned: int(float(x)) truncates TOWARD ZERO in both directions,
so a fractional resting size read as smaller than it is, and a fractional
delta removed less than it should -- permanently, because the book is
incremental.
"""

from __future__ import annotations

import pytest

from kbot.quantity import (
    UNITS_PER_CONTRACT,
    contracts_to_units,
    units_from_fixed_point,
    units_to_contracts,
)


@pytest.mark.parametrize("raw,expected", [
    ("104.00", 10400),
    ("0.50", 50),
    ("0.07", 7),
    ("394.25", 39425),
    ("-104.50", -10450),
    ("0.00", 0),
    (50, 5000),
    (0.5, 50),
])
def test_fixed_point_parsing_is_exact(raw, expected):
    assert units_from_fixed_point(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-number", "nan", "inf", True, False, object()])
def test_unusable_values_return_none(raw):
    assert units_from_fixed_point(raw) is None


def test_the_truncation_bug_is_gone():
    """int(float("0.50")) == 0 -- a real resting order read as no order."""
    assert int(float("0.50")) == 0          # the old behaviour, for contrast
    assert units_from_fixed_point("0.50") == 50


def test_negative_truncation_bug_is_gone():
    """int(float("-104.50")) == -104: removes 104, leaves 0.5 resting forever."""
    assert int(float("-104.50")) == -104     # the old behaviour, for contrast
    assert units_from_fixed_point("-104.50") == -10450


def test_decimal_parsing_avoids_binary_float_error():
    """float("0.07") * 100 == 7.000000000000001; Decimal makes it exactly 7."""
    assert units_from_fixed_point("0.07") == 7
    assert units_from_fixed_point("0.29") == 29
    assert units_from_fixed_point("1.15") == 115


def test_round_trip_through_contracts():
    for units in (0, 1, 50, 7, 10400, 39425):
        assert contracts_to_units(units_to_contracts(units)) == units


def test_units_to_contracts_is_exact_for_whole_contracts():
    assert units_to_contracts(100) == 1.0
    assert units_to_contracts(500) == 5.0
    assert units_to_contracts(50) == 0.5


def test_summing_units_has_no_drift():
    """
    The reason sizes are integers: 100 additions of 0.01 contracts in float
    do not land on 1.0, but 100 additions of 1 unit land on exactly 100.
    """
    assert sum(units_from_fixed_point("0.01") for _ in range(100)) == UNITS_PER_CONTRACT
    assert sum(0.01 for _ in range(100)) != 1.0


def test_more_precision_than_hundredths_is_rounded_not_truncated():
    """The exchange does not send these; rounding beats truncating if it does."""
    assert units_from_fixed_point("0.005") == 0     # banker's rounding, to even
    assert units_from_fixed_point("0.015") == 2
    assert units_from_fixed_point("1.004") == 100
    assert units_from_fixed_point("1.006") == 101
