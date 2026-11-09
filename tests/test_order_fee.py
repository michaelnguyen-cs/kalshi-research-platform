"""
Tests for the order-level Kalshi fee model in scripts/market_calibration_study.py.

The exchange applies the ceiling ONCE PER ORDER with the contract count inside
it, not once per contract. See KILL_CRITERIA.md section 9 (2026-08-21).
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from market_calibration_study import (  # noqa: E402
    fee_per_contract,
    observation_ev,
    order_fee,
)


# ── The three pre-registered reference points, all at P = 0.70 ────────────────
# raw = 0.07 * C * 0.70 * 0.30 = 0.0147 * C dollars, then ceil to the cent.

def test_fee_at_p070_c10_is_1_50_cents_per_contract():
    """C=10: raw 14.7c -> ceil 15c for the order -> 1.50c/contract."""
    assert order_fee(0.70, 10) == pytest.approx(0.15)
    assert fee_per_contract(0.70, 10) == pytest.approx(0.015)


def test_fee_at_p070_c200_is_1_47_cents_per_contract():
    """C=200: raw 294.0c is already exact -> 1.47c/contract, the asymptote."""
    assert order_fee(0.70, 200) == pytest.approx(2.94)
    assert fee_per_contract(0.70, 200) == pytest.approx(0.0147)


def test_fee_at_p070_c1_is_2_00_cents_per_contract():
    """C=1: raw 1.47c -> ceil 2c. This is the old, wrong per-contract form."""
    assert order_fee(0.70, 1) == pytest.approx(0.02)
    assert fee_per_contract(0.70, 1) == pytest.approx(0.02)


def test_c10_sits_between_c1_and_the_asymptote():
    """The pre-committed C=10 is within 0.03c of the C=200 asymptote."""
    c1 = fee_per_contract(0.70, 1)
    c10 = fee_per_contract(0.70, 10)
    c200 = fee_per_contract(0.70, 200)
    assert c200 < c10 < c1
    assert c10 - c200 == pytest.approx(0.0003)


# ── Structural properties ────────────────────────────────────────────────────

def test_ceiling_is_applied_once_per_order_not_per_contract():
    """The whole point: an order is cheaper per contract than C separate ones."""
    for price in (0.03, 0.05, 0.12, 0.30, 0.70, 0.95):
        pooled = order_fee(price, 50)
        separately = 50 * order_fee(price, 1)
        assert pooled <= separately


def test_fee_is_symmetric_in_price():
    """fee(P) == fee(1-P), which is why the random-side control is an identity."""
    for price in (0.05, 0.20, 0.35, 0.49):
        assert order_fee(price, 10) == pytest.approx(order_fee(1.0 - price, 10))


def test_per_contract_fee_is_bounded_by_raw_plus_one_cent_over_c():
    """
    The per-contract fee is NOT monotone in C -- the ceiling makes it bounce
    (at P=0.15 it is 0.90c at C=10 but 0.92c at C=25). The invariant that
    actually holds is that the rounding overhead is at most one cent spread
    across the order, so the per-contract fee converges to the raw rate as 1/C.
    """
    for price in (0.05, 0.15, 0.70):
        raw = 0.07 * price * (1.0 - price)
        for c in (1, 2, 5, 10, 25, 100, 500):
            fee = fee_per_contract(price, c)
            assert raw - 1e-12 <= fee <= raw + 0.01 / c + 1e-12


def test_large_orders_converge_to_the_raw_rate():
    for price in (0.05, 0.15, 0.70):
        raw = 0.07 * price * (1.0 - price)
        assert fee_per_contract(price, 10_000) == pytest.approx(raw, abs=1e-6)


def test_fee_never_falls_below_the_unrounded_true_cost():
    """Rounding is UP: the charge can never undercharge versus the raw formula."""
    for price in (0.07, 0.23, 0.61, 0.88):
        for c in (1, 3, 10, 77, 200):
            raw = 0.07 * c * price * (1.0 - price)
            assert order_fee(price, c) >= raw - 1e-12


def test_result_is_always_a_whole_number_of_cents():
    for price in (0.11, 0.44, 0.70, 0.93):
        for c in (1, 10, 200):
            cents = order_fee(price, c) * 100.0
            assert abs(cents - round(cents)) < 1e-9


# ── The 1e-9 nudge: an exact-cent fee must not round up a whole extra cent ────

def test_exact_cent_fee_does_not_round_up_an_extra_cent():
    """C=200 at P=0.70 lands on exactly 294c; float error must not make it 295."""
    raw_cents = 0.07 * 200 * 0.70 * 0.30 * 100.0
    assert math.ceil(raw_cents) >= 294  # naive ceil may overshoot on binary float
    assert order_fee(0.70, 200) == pytest.approx(2.94)


# ── The per-series multiplier ────────────────────────────────────────────────

def test_multiplier_scales_the_fee():
    """fee_multiplier is per-series and NOT always 1 (0, 0.5 and 1 all occur)."""
    assert order_fee(0.70, 10, multiplier=0.5) == pytest.approx(0.08)  # 7.35c -> 8c
    assert order_fee(0.70, 10, multiplier=0.0) == pytest.approx(0.0)


def test_zero_multiplier_series_pay_nothing():
    for price in (0.05, 0.50, 0.95):
        assert order_fee(price, 10, multiplier=0.0) == 0.0


# ── contracts is required and validated ──────────────────────────────────────

def test_contracts_has_no_default():
    """A silent contracts=1 default is exactly the bug this replaced."""
    with pytest.raises(TypeError):
        order_fee(0.70)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        fee_per_contract(0.70)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [0, -1, -10])
def test_non_positive_contract_counts_are_rejected(bad):
    with pytest.raises(ValueError):
        order_fee(0.70, bad)


# ── observation_ev threads the order size through ────────────────────────────

def test_observation_ev_charges_the_order_level_fee():
    """Cheaper side of a 0.70 market is NO at 0.30; fee is symmetric, so 1.50c."""
    ev_win = observation_ev(0.70, settled_yes=False, coefficient=0.07, contracts=10)
    assert ev_win == pytest.approx(1.0 - 0.30 - 0.015)

    ev_loss = observation_ev(0.70, settled_yes=True, coefficient=0.07, contracts=10)
    assert ev_loss == pytest.approx(0.0 - 0.30 - 0.015)


def test_observation_ev_requires_contracts_explicitly():
    with pytest.raises(TypeError):
        observation_ev(0.70, False, 0.07)  # type: ignore[call-arg]


def test_larger_orders_never_have_worse_ev():
    """The correction can only improve EV -- the old form overcharged."""
    for price in (0.72, 0.85, 0.95):
        small = observation_ev(price, True, 0.07, 1)
        big = observation_ev(price, True, 0.07, 10)
        assert big >= small
