"""Tests for kbot.models.types — OrderBook helpers and validation."""

import pytest
from pydantic import ValidationError

from kbot.models.types import (
    Action,
    OrderBook,
    OrderIntent,
    PriceLevel,
    Side,
    SizingResult,
)


# ── PriceLevel ────────────────────────────────────────────────────────────────

def test_price_level_rejects_zero_price():
    with pytest.raises(ValidationError):
        PriceLevel(price=0.0, quantity_units=1000)


def test_price_level_rejects_one_price():
    with pytest.raises(ValidationError):
        PriceLevel(price=1.0, quantity_units=1000)


def test_price_level_rejects_negative_quantity():
    with pytest.raises(ValidationError):
        PriceLevel(price=0.50, quantity=-1)


# ── OrderBook ─────────────────────────────────────────────────────────────────

def _book(yes_prices=(), no_prices=()):
    return OrderBook(
        ticker="TEST-TICKER",
        yes=[PriceLevel(price=p, quantity_units=10000) for p in yes_prices],
        no=[PriceLevel(price=p, quantity_units=10000) for p in no_prices],
    )


def test_best_yes_returns_cheapest():
    # YES side stores ask prices; best = cheapest (min)
    book = _book(yes_prices=[0.40, 0.55, 0.50])
    assert book.best_yes().price == 0.40


def test_best_no_returns_cheapest():
    # NO side stores ask prices; best = cheapest (min)
    book = _book(no_prices=[0.30, 0.45, 0.40])
    assert book.best_no().price == 0.30


def test_best_yes_empty_returns_none():
    assert _book().best_yes() is None


def test_best_no_empty_returns_none():
    assert _book().best_no() is None


def test_mid_price_symmetry():
    # YES ask = 0.60, NO ask = 0.40 → mid = (0.60 + 0.60) / 2 = 0.60
    book = _book(yes_prices=[0.60], no_prices=[0.40])
    assert abs(book.mid_price() - 0.60) < 1e-9


def test_mid_price_skewed():
    # YES ask = 0.70, NO ask = 0.45 → implied YES bid = 1 - 0.45 = 0.55
    # mid = (0.70 + 0.55) / 2 = 0.625
    book = _book(yes_prices=[0.70], no_prices=[0.45])
    assert abs(book.mid_price() - 0.625) < 1e-9


def test_mid_price_none_when_no_sides():
    assert _book().mid_price() is None

def test_mid_price_none_when_one_side_missing():
    assert _book(yes_prices=[0.60]).mid_price() is None


# ── OrderIntent ───────────────────────────────────────────────────────────────

def test_order_intent_valid():
    intent = OrderIntent(ticker="X", side=Side.YES, count=10, price_dollars=0.65)
    assert intent.count == 10
    assert intent.price_dollars == 0.65


def test_order_intent_rejects_zero_contracts():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="X", side=Side.YES, count=0, price_dollars=0.65)


def test_order_intent_rejects_negative_price():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="X", side=Side.YES, count=1, price_dollars=-0.1)


def test_order_intent_rejects_price_gte_one():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="X", side=Side.YES, count=1, price_dollars=1.0)


def test_order_intent_rounds_price():
    intent = OrderIntent(ticker="X", side=Side.YES, count=1, price_dollars=0.654321)
    assert intent.price_dollars == round(0.654321, 4)
