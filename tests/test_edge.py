"""Tests for kbot.edge — fee formula and edge gate."""

import pytest

from kbot.config import Settings
from kbot.edge import (
    MAKER_FEE_SPECIAL_SERIES,
    fee_per_contract,
    half_spread_dollars,
    is_special_event_market,
    maker_fee_dollars_for_market,
    net_edge,
    net_edge_maker_for_market,
    passes_edge_gate,
    passes_price_floor,
    series_ticker_prefix,
)
from kbot.models.types import Side, SizingResult


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env="demo",
        dry_run=True,
        fee_coefficient=kwargs.get("fee_coefficient", 0.07),
        assumed_slippage=kwargs.get("assumed_slippage", 0.005),
        min_net_edge=kwargs.get("min_net_edge", 0.04),
        taker_price_floor=kwargs.get("taker_price_floor", 0.15),
        maker_fee_default=kwargs.get("maker_fee_default", 0.0),
    )


# ── fee_per_contract ──────────────────────────────────────────────────────────

def test_fee_symmetry():
    # fee(p) should equal fee(1-p) — Kalshi pricing is symmetric
    assert abs(fee_per_contract(0.30, _cfg()) - fee_per_contract(0.70, _cfg())) < 1e-9


def test_fee_maximum_at_midpoint():
    # P*(1-P) is maximised at P=0.5
    fee_50 = fee_per_contract(0.50, _cfg())
    fee_30 = fee_per_contract(0.30, _cfg())
    fee_70 = fee_per_contract(0.70, _cfg())
    assert fee_50 > fee_30
    assert fee_50 > fee_70


def test_fee_formula_exact():
    # coeff=0.07, p=0.60 → 0.07 * 0.60 * 0.40 = 0.0168
    assert abs(fee_per_contract(0.60, _cfg(fee_coefficient=0.07)) - 0.0168) < 1e-9


def test_fee_respects_coefficient():
    fee_default = fee_per_contract(0.50, _cfg(fee_coefficient=0.07))
    fee_double = fee_per_contract(0.50, _cfg(fee_coefficient=0.14))
    assert abs(fee_double - 2 * fee_default) < 1e-9


# ── net_edge ──────────────────────────────────────────────────────────────────

def test_net_edge_positive_when_fair_exceeds_ask():
    # fair_p = 0.75, ask = 0.60 → gross = 0.15, fee ≈ 0.017, slip = 0.005 → net ≈ 0.128
    edge = net_edge(0.75, 0.60, _cfg())
    assert edge > 0


def test_net_edge_negative_when_at_money():
    # fair_p == ask → gross = 0, net < 0 after fees + slippage
    edge = net_edge(0.60, 0.60, _cfg())
    assert edge < 0


def test_net_edge_exact():
    cfg = _cfg(fee_coefficient=0.07, assumed_slippage=0.005)
    # gross = 0.75 - 0.60 = 0.15
    # fee   = 0.07 * 0.60 * 0.40 = 0.0168
    # net   = 0.15 - 0.0168 - 0.005 = 0.1282
    expected = 0.75 - 0.60 - 0.07 * 0.60 * 0.40 - 0.005
    assert abs(net_edge(0.75, 0.60, cfg) - expected) < 1e-9


# ── passes_edge_gate ──────────────────────────────────────────────────────────

def _result(contracts=1, net_edge_dollars=0.05) -> SizingResult:
    return SizingResult(
        ticker="X",
        side=Side.YES,
        contracts=contracts,
        price_dollars=0.60,
        net_edge_dollars=net_edge_dollars,
        kelly_fraction_used=0.1,
    )


def test_gate_passes_when_edge_above_min():
    assert passes_edge_gate(_result(net_edge_dollars=0.05), _cfg(min_net_edge=0.04))


def test_gate_blocks_when_edge_below_min():
    assert not passes_edge_gate(_result(net_edge_dollars=0.03), _cfg(min_net_edge=0.04))


def test_gate_blocks_when_edge_exactly_at_min():
    # Equal to min is fine — we use >=
    assert passes_edge_gate(_result(net_edge_dollars=0.04), _cfg(min_net_edge=0.04))


def test_gate_blocks_when_zero_contracts():
    assert not passes_edge_gate(_result(contracts=0, net_edge_dollars=0.10), _cfg())


def test_gate_blocks_negative_edge_regardless_of_contracts():
    assert not passes_edge_gate(_result(contracts=5, net_edge_dollars=-0.01), _cfg())


# ── half_spread_dollars / spread-aware gate (2026-07 calibration audit) ──────

def test_half_spread_zero_for_efficient_book():
    # yes_ask + no_ask == 1.0 -> no spread.
    assert half_spread_dollars(0.60, 0.40) == 0.0


def test_half_spread_positive_for_wide_book():
    # yes_ask + no_ask = 1.06 -> spread = 0.06, half = 0.03.
    assert half_spread_dollars(0.46, 0.60) == pytest.approx(0.03)


def test_half_spread_clamped_to_zero_when_book_sums_below_one():
    # A crossed/tight book (sum < 1) must not loosen the gate.
    assert half_spread_dollars(0.40, 0.40) == 0.0


def test_gate_passes_with_zero_half_spread_matches_default_behavior():
    assert passes_edge_gate(_result(net_edge_dollars=0.05), _cfg(min_net_edge=0.04), half_spread=0.0)


def test_gate_blocked_by_half_spread_even_when_above_flat_min():
    # net_edge=0.05 clears min_net_edge=0.04 alone, but not 0.04 + 0.03 half_spread.
    assert not passes_edge_gate(_result(net_edge_dollars=0.05), _cfg(min_net_edge=0.04), half_spread=0.03)


def test_gate_passes_when_edge_clears_min_plus_half_spread():
    assert passes_edge_gate(_result(net_edge_dollars=0.08), _cfg(min_net_edge=0.04), half_spread=0.03)


# ── RULE R1: taker price floor ──────────────────────────────────────────────────

def test_taker_below_floor_blocked():
    assert not passes_price_floor(0.10, "KXBTC-26JUL0317-T48000", _cfg(taker_price_floor=0.15))


def test_taker_at_floor_allowed():
    # Equal to the floor is fine — we use >=
    assert passes_price_floor(0.15, "KXBTC-26JUL0317-T48000", _cfg(taker_price_floor=0.15))


def test_taker_above_floor_allowed():
    assert passes_price_floor(0.50, "KXBTC-26JUL0317-T48000", _cfg(taker_price_floor=0.15))


def test_maker_below_floor_exempt_on_standard_market():
    # Standard markets charge 0% maker fee -- resting below the floor is fine.
    assert passes_price_floor(
        0.05, "KXBTC-26JUL0317-T48000", _cfg(taker_price_floor=0.15), is_maker=True
    )


@pytest.fixture()
def special_event_series():
    MAKER_FEE_SPECIAL_SERIES.add("KXSPECIAL")
    yield "KXSPECIAL"
    MAKER_FEE_SPECIAL_SERIES.discard("KXSPECIAL")


def test_maker_below_floor_blocked_on_special_event_market(special_event_series):
    ticker = f"{special_event_series}-26JUL03-YES"
    assert not passes_price_floor(0.05, ticker, _cfg(taker_price_floor=0.15), is_maker=True)


def test_maker_above_floor_allowed_on_special_event_market(special_event_series):
    ticker = f"{special_event_series}-26JUL03-YES"
    assert passes_price_floor(0.50, ticker, _cfg(taker_price_floor=0.15), is_maker=True)


# ── Per-market maker fee (Source 7 / V2) ────────────────────────────────────────

def test_series_ticker_prefix():
    assert series_ticker_prefix("KXBTC-26JUL0317-T48000") == "KXBTC"
    assert series_ticker_prefix("NOSEPARATOR") == "NOSEPARATOR"


def test_is_special_event_market(special_event_series):
    assert is_special_event_market(f"{special_event_series}-26JUL03-YES")
    assert not is_special_event_market("KXBTC-26JUL0317-T48000")


def test_standard_market_uses_default_coefficient():
    cfg = _cfg(maker_fee_default=0.0)
    fee = maker_fee_dollars_for_market("KXBTC-26JUL0317-T48000", 0.50, cfg)
    assert fee == 0.0

    cfg_nonzero = _cfg(maker_fee_default=0.02)
    fee_nonzero = maker_fee_dollars_for_market("KXBTC-26JUL0317-T48000", 0.50, cfg_nonzero)
    assert fee_nonzero == pytest.approx(0.02 * 0.50 * 0.50)


def test_special_event_market_uses_flat_pct_not_coefficient(special_event_series):
    # Special-event fee ignores maker_fee_default entirely -- flat 0.25% of price.
    cfg = _cfg(maker_fee_default=0.5)  # deliberately large, to prove it's ignored
    ticker = f"{special_event_series}-26JUL03-YES"
    fee = maker_fee_dollars_for_market(ticker, 0.60, cfg)
    assert fee == pytest.approx(0.0025 * 0.60)


def test_net_edge_maker_for_market_matches_manual_calc():
    cfg = _cfg(maker_fee_default=0.0, assumed_slippage=0.005)
    edge = net_edge_maker_for_market(0.72, 0.61, "KXBTC-26JUL0317-T48000", cfg)
    expected = (0.72 - 0.61) - 0.0 - 0.005
    assert edge == pytest.approx(expected)


def test_net_edge_maker_for_market_special_event_lower_than_standard(special_event_series):
    cfg = _cfg(maker_fee_default=0.0, assumed_slippage=0.005)
    standard_edge = net_edge_maker_for_market(0.72, 0.61, "KXBTC-26JUL0317-T48000", cfg)
    special_ticker = f"{special_event_series}-26JUL03-YES"
    special_edge = net_edge_maker_for_market(0.72, 0.61, special_ticker, cfg)
    assert special_edge < standard_edge
