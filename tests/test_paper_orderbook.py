"""Tests for paper.py WS orderbook parsing — verified against live Kalshi WS format."""

import pytest
import structlog

from kbot.models.types import OrderBook
from kbot.paper import BookInvalidated, _apply_delta, _parse_orderbook


def _snapshot(ticker="T", no_fp=None, yes_fp=None):
    return {
        "market_ticker": ticker,
        "no_dollars_fp": no_fp or [],
        "yes_dollars_fp": yes_fp or [],
    }


# ── _parse_orderbook ──────────────────────────────────────────────────────────

def test_parse_yes_side_uses_no_dollars_fp():
    # no_dollars_fp = NO bids → inverted = YES ask prices
    data = _snapshot(no_fp=[["0.90", "394.00"]])
    book = _parse_orderbook("T", data)
    assert len(book.yes) == 1
    assert abs(book.yes[0].price - 0.10) < 1e-9
    assert book.yes[0].quantity_units == 39400


def test_parse_no_side_uses_yes_dollars_fp():
    # yes_dollars_fp = YES bids → inverted = NO ask prices
    data = _snapshot(yes_fp=[["0.10", "200.00"]])
    book = _parse_orderbook("T", data)
    assert len(book.no) == 1
    assert abs(book.no[0].price - 0.90) < 1e-9
    assert book.no[0].quantity_units == 20000


def test_parse_multiple_levels():
    data = _snapshot(
        no_fp=[["0.90", "100.00"], ["0.92", "50.00"]],
        yes_fp=[["0.10", "200.00"], ["0.08", "75.00"]],
    )
    book = _parse_orderbook("T", data)
    assert len(book.yes) == 2
    assert len(book.no) == 2


def test_parse_float_string_quantities():
    # Kalshi sends quantities as float strings like "394.00"
    data = _snapshot(no_fp=[["0.90", "394.00"]])
    book = _parse_orderbook("T", data)
    assert book.yes[0].quantity_units == 39400


def test_parse_boundary_prices_excluded():
    # bid=0.0 → ask=1.0, bid=1.0 → ask=0.0 — both out of (0,1) range
    data = _snapshot(no_fp=[["0.0", "10.00"], ["1.0", "10.00"], ["0.90", "10.00"]])
    book = _parse_orderbook("T", data)
    assert len(book.yes) == 1
    assert abs(book.yes[0].price - 0.10) < 1e-9


def test_parse_empty_snapshot():
    book = _parse_orderbook("T", _snapshot())
    assert book.yes == []
    assert book.no == []


def test_parse_missing_keys_graceful():
    # Snapshot with neither key at all
    book = _parse_orderbook("T", {"market_ticker": "T"})
    assert book.yes == []
    assert book.no == []


def test_parse_malformed_item_skipped():
    data = _snapshot(no_fp=[["bad", "10.00"], [None, "10.00"], ["0.90", "10.00"]])
    book = _parse_orderbook("T", data)
    assert len(book.yes) == 1


def test_parse_best_yes_is_cheapest():
    data = _snapshot(no_fp=[["0.95", "100.00"], ["0.90", "50.00"]])
    book = _parse_orderbook("T", data)
    # no_bid=0.95 → yes_ask=0.05; no_bid=0.90 → yes_ask=0.10
    assert abs(book.best_yes().price - 0.05) < 1e-9


# ── _apply_delta ──────────────────────────────────────────────────────────────

def _base_book():
    data = _snapshot(
        no_fp=[["0.90", "100.00"]],
        yes_fp=[["0.10", "200.00"]],
    )
    return _parse_orderbook("T", data)


def _delta(price, delta_fp, side, ticker="T"):
    """A live-shaped orderbook_delta msg: one level, SIGNED size change."""
    return {
        "market_ticker": ticker,
        "market_id": "abc-123",
        "price_dollars": price,
        "delta_fp": delta_fp,
        "side": side,
        "ts": 1754496000,
        "ts_ms": 1754496000123,
    }


def _level_at(levels, price):
    return next(l for l in levels if abs(l.price - price) < 1e-9)


def _warnings(logs):
    """Captured structlog warnings mentioning the delta handler."""
    return [
        e for e in logs
        if e["log_level"] == "warning" and "orderbook_delta" in e["event"]
    ]


def test_delta_real_shape_moves_book_and_mid():
    book = _base_book()
    before_mid = book.mid_price()
    # side="no" is the NO bid book → book.yes (asks). New bid 0.92 → ask 0.08,
    # cheaper than the existing 0.10, so best_yes and the mid both move.
    assert _apply_delta(book, _delta("0.92", "50.00", "no")) is True

    assert _level_at(book.yes, 0.08).quantity_units == 5000
    assert _level_at(book.yes, 0.10).quantity_units == 10000  # untouched
    assert abs(book.best_yes().price - 0.08) < 1e-9
    assert book.mid_price() != before_mid
    assert abs(book.mid_price() - (0.08 + 0.10) / 2.0) < 1e-9


def test_delta_is_signed_not_replacement():
    book = _base_book()
    # 100 resting at yes_ask 0.10; -40 leaves 60, not 40.
    assert _apply_delta(book, _delta("0.90", "-40.00", "no")) is True
    assert _level_at(book.yes, 0.10).quantity_units == 6000

    assert _apply_delta(book, _delta("0.90", "15.00", "no")) is True
    assert _level_at(book.yes, 0.10).quantity_units == 7500


def test_delta_to_zero_removes_level():
    book = _base_book()
    # 100 resting; -100 empties the level, which must disappear (not sit at 0).
    assert _apply_delta(book, _delta("0.90", "-100.00", "no")) is True
    assert book.yes == []
    assert book.best_yes() is None


def test_delta_oversized_negative_invalidates_the_book():
    """
    A delta that would drive a level NEGATIVE means we and the exchange
    disagree about what is resting there. The old code clamped it to zero and
    carried on, which hid the divergence and kept trading a book known to be
    wrong. It must now refuse the book instead, so the caller drops it and
    waits for a fresh snapshot.
    """
    book = _base_book()
    with pytest.raises(BookInvalidated):
        _apply_delta(book, _delta("0.90", "-104.00", "no"))


def test_delta_to_exactly_zero_is_not_an_error():
    """Exact removal is legitimate; only OVERSHOOT signals divergence."""
    book = _base_book()
    assert _apply_delta(book, _delta("0.90", "-100.00", "no")) is True
    assert book.yes == []


def test_delta_yes_side_updates_no_book():
    book = _base_book()
    # side="yes" is the YES bid book → book.no (asks). 200 resting at no_ask
    # 0.90; -200 clears it. The yes side must be untouched.
    assert _apply_delta(book, _delta("0.10", "-200.00", "yes")) is True
    assert book.no == []
    assert len(book.yes) == 1


def test_delta_on_unknown_level_creates_it():
    book = _base_book()
    # Kalshi can send the first delta for a level we have no snapshot row for.
    assert _apply_delta(book, _delta("0.75", "12.00", "no")) is True
    assert _level_at(book.yes, 0.25).quantity_units == 1200


# ── REGRESSION: snapshot-shaped message must never silently no-op ─────────────

def test_snapshot_shape_passed_to_delta_is_rejected():
    """
    The old _apply_delta read yes_dollars_fp/no_dollars_fp — the SNAPSHOT
    key shape. Fed a real delta it found neither key and no-op'd silently,
    so every book froze at its last snapshot. A snapshot-shaped message must
    now be rejected loudly instead.
    """
    book = _base_book()
    before_yes, before_no = list(book.yes), list(book.no)

    snapshot_shaped = {
        "market_ticker": "T",
        "no_dollars_fp": [["0.92", "50.00"]],
        "yes_dollars_fp": [["0.10", "200.00"]],
    }
    with structlog.testing.capture_logs() as logs:
        applied = _apply_delta(book, snapshot_shaped)

    assert applied is False
    assert book.yes == before_yes
    assert book.no == before_no
    assert _warnings(logs), "rejection must be logged, not swallowed"


def test_delta_missing_side_rejected():
    book = _base_book()
    msg = _delta("0.90", "-40.00", "no")
    del msg["side"]
    with structlog.testing.capture_logs() as logs:
        assert _apply_delta(book, msg) is False
    assert _level_at(book.yes, 0.10).quantity_units == 10000
    assert _warnings(logs)


def test_delta_unparseable_values_rejected():
    book = _base_book()
    with structlog.testing.capture_logs() as logs:
        assert _apply_delta(book, _delta("not-a-price", "-40.00", "no")) is False
        assert _apply_delta(book, _delta("0.90", "not-a-size", "no")) is False
    assert _level_at(book.yes, 0.10).quantity_units == 10000
    assert len(_warnings(logs)) == 2


def test_delta_boundary_price_rejected():
    book = _base_book()
    # bid 1.00 → ask 0.00, outside PriceLevel's (0,1) range.
    with structlog.testing.capture_logs() as logs:
        assert _apply_delta(book, _delta("1.00", "10.00", "no")) is False
    assert len(book.yes) == 1
    assert _warnings(logs)


# ── Fixed-point quantities (integer hundredths of a contract) ─────────────────

def test_parse_keeps_fractional_resting_size():
    """
    int(float("0.50")) == 0, so a half-contract level used to vanish from the
    book entirely -- a real resting order read as no order at all.
    """
    book = _parse_orderbook("T", _snapshot(no_fp=[["0.90", "0.50"]]))
    assert len(book.yes) == 1
    assert book.yes[0].quantity_units == 50
    assert book.yes[0].quantity_contracts == 0.5


def test_parse_keeps_fractional_part_of_a_large_size():
    book = _parse_orderbook("T", _snapshot(no_fp=[["0.90", "394.25"]]))
    assert book.yes[0].quantity_units == 39425


def test_delta_keeps_fractional_removal():
    """
    The book is INCREMENTAL, so a truncated removal is worse than a truncated
    snapshot: int(float("-40.50")) == -40 left 0.50 contracts resting forever.
    """
    book = _base_book()
    assert _apply_delta(book, _delta("0.90", "-40.50", "no")) is True
    assert _level_at(book.yes, 0.10).quantity_units == 5950   # 100.00 - 40.50


def test_repeated_fractional_deltas_do_not_accumulate_error():
    """Ten removals of 0.10 contracts must remove exactly 1.00 contract."""
    book = _base_book()
    for _ in range(10):
        assert _apply_delta(book, _delta("0.90", "-0.10", "no")) is True
    assert _level_at(book.yes, 0.10).quantity_units == 9900


def test_fractional_delta_can_empty_a_level_exactly():
    book = _parse_orderbook("T", _snapshot(no_fp=[["0.90", "0.50"]]))
    assert _apply_delta(book, _delta("0.90", "-0.50", "no")) is True
    assert book.yes == []
