"""
Canonical YES-scale book: decoding, sequenced deltas and section 7's invariants.

The direction of the mapping is the part worth testing hardest. Under
`use_yes_price: true` the `yes` array is the YES BID ladder and the `no` array is
the YES ASK ladder, so "size at the best ask" -- the quantity section 4 tests
against X = 10 -- lives on the no-side array. This project has hit
book-convention bugs three separate times; getting this backwards would report
bid depth as ask depth and inflate the eligible count.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec.book import (  # noqa: E402
    MAX_PRICE,
    PRICE_SCALE,
    BookError,
    CanonicalBook,
    contracts_to_qty,
    price_dollars,
    qty_contracts,
    to_price,
    to_qty,
)


def snap(yes=None, no=None):
    msg = {"market_ticker": "T"}
    if yes is not None:
        msg["yes_dollars_fp"] = yes
    if no is not None:
        msg["no_dollars_fp"] = no
    return msg


def fresh(yes=None, no=None, *, use_yes_price=True, seq=1):
    book = CanonicalBook(ticker="T")
    book.apply_snapshot(snap(yes, no), seq=seq, sid=7, recv_ts=1000.0,
                        use_yes_price=use_yes_price)
    return book


# -- Scalars ------------------------------------------------------------------

def test_prices_decode_to_tenths_of_a_cent_exactly():
    assert to_price("0.9950") == 9950
    assert to_price("0.0100") == 100
    assert to_price("0.7000") == 7000
    # A deci-cent price above $0.90 is exactly the regime section 8 widened the
    # universe to exercise; float() would round it.
    assert to_price("0.9995") == 9995


def test_prices_finer_than_a_tenth_of_a_cent_are_refused_not_rounded():
    with pytest.raises(BookError):
        to_price("0.99955")


def test_quantities_decode_to_hundredths_of_a_contract():
    assert to_qty("300.00") == 30000
    assert to_qty("-54.00") == -5400
    assert to_qty("0.01") == 1
    assert contracts_to_qty(10) == 1000
    assert qty_contracts(1000) == 10.0


def test_unparseable_scalars_raise_rather_than_default():
    for bad in (None, "", "abc", {}):
        with pytest.raises(BookError):
            to_price(bad)
        with pytest.raises(BookError):
            to_qty(bad)


# -- Snapshot decoding --------------------------------------------------------

def test_yes_array_is_the_bid_ladder_and_no_array_is_the_ask_ladder():
    book = fresh(yes=[["0.6800", "50.00"], ["0.6900", "12.00"]],
                 no=[["0.7100", "40.00"], ["0.7200", "500.00"]])
    top = book.top_of_book()
    assert top.bid_price == 6900 and top.bid_qty == 1200
    assert top.ask_price == 7100 and top.ask_qty == 4000
    assert price_dollars(top.ask_price) == 0.71
    assert qty_contracts(top.ask_qty) == 40.0


def test_executable_ask_reads_the_opposite_ladder_for_each_side():
    """
    Section 2: the best executable ask ON THE SIDE THE FROZEN RULE BUYS.

    Buying YES lifts a resting NO bid, which in canonical yes-leg pricing IS the
    ask ladder. Buying NO lifts a resting YES bid: a YES bid at 0.68 is an offer
    to sell NO at 0.32, and the size behind it is the size at that BID level.
    """
    top = fresh(yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]]).top_of_book()
    assert top.executable_ask("YES") == (7100, 4000)
    assert top.executable_ask("NO") == (10_000 - 6800, 500)


def test_executable_ask_is_none_when_that_side_cannot_be_bought():
    only_bids = fresh(yes=[["0.6800", "5.00"]]).top_of_book()
    assert only_bids.executable_ask("YES") == (None, None)
    assert only_bids.executable_ask("NO") == (3200, 500)

    only_asks = fresh(no=[["0.7100", "40.00"]]).top_of_book()
    assert only_asks.executable_ask("YES") == (7100, 4000)
    assert only_asks.executable_ask("NO") == (None, None)


def test_executable_ask_refuses_an_unknown_side():
    with pytest.raises(BookError):
        fresh(yes=[["0.6800", "5.00"]]).top_of_book().executable_ask("MAYBE")


def test_the_two_sides_prices_are_algebraically_consistent():
    """NO economics come from the same canonical book, not a second convention."""
    top = fresh(yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]]).top_of_book()
    yes_price, _ = top.executable_ask("YES")
    no_price, _ = top.executable_ask("NO")
    # Crossing both sides costs the spread: 0.71 + 0.32 = 1.03 > 1.
    assert yes_price + no_price == PRICE_SCALE + (7100 - 6800)


def test_use_yes_price_false_transforms_the_no_side_into_yes_coordinates():
    """
    The whole of the section 8 'transform B into YES coordinates' step: a
    resting NO bid at 0.29 is an offer that fills a YES buy at 0.71.
    """
    a = fresh(yes=[["0.6900", "12.00"]], no=[["0.7100", "40.00"]], use_yes_price=True)
    b = fresh(yes=[["0.6900", "12.00"]], no=[["0.2900", "40.00"]], use_yes_price=False)
    assert a.top_of_book() == b.top_of_book()


def test_snapshot_replaces_wholesale_and_never_merges():
    book = fresh(yes=[["0.6000", "10.00"]], no=[["0.9000", "10.00"]])
    book.apply_snapshot(snap(yes=[["0.5000", "5.00"]]), seq=2, sid=7, recv_ts=1001.0,
                        use_yes_price=True)
    assert list(book.bids) == [5000]
    assert book.asks == {}


def test_zero_size_levels_never_enter_the_book():
    book = fresh(yes=[["0.6000", "0.00"], ["0.5000", "3.00"]])
    assert list(book.bids) == [5000]


def test_missing_side_key_is_an_empty_side_not_an_error():
    book = fresh(yes=[["0.6000", "10.00"]])
    top = book.top_of_book()
    assert top.ask_price is None and top.ask_qty is None


def test_malformed_level_is_refused():
    with pytest.raises(BookError):
        fresh(yes=[["0.6000"]])
    with pytest.raises(BookError):
        fresh(yes=[["0.6000", "-1.00"]])


# -- Deltas --------------------------------------------------------------------

def test_delta_adds_removes_and_deletes_levels():
    book = fresh(yes=[["0.6000", "10.00"]], no=[["0.7000", "10.00"]])
    book.apply_delta({"side": "no", "price_dollars": "0.6900", "delta_fp": "25.00"},
                     seq=2, recv_ts=1001.0, use_yes_price=True)
    assert book.top_of_book().ask_price == 6900
    book.apply_delta({"side": "no", "price_dollars": "0.6900", "delta_fp": "-25.00"},
                     seq=3, recv_ts=1002.0, use_yes_price=True)
    assert 6900 not in book.asks
    assert book.top_of_book().ask_price == 7000


def test_delta_below_zero_is_a_wrong_book_not_a_clamp():
    book = fresh(yes=[["0.6000", "10.00"]])
    with pytest.raises(BookError, match="not the one the exchange is describing"):
        book.apply_delta({"side": "yes", "price_dollars": "0.6000", "delta_fp": "-11.00"},
                         seq=2, recv_ts=1001.0, use_yes_price=True)


def test_delta_carries_exchange_and_receive_timestamps():
    book = fresh(yes=[["0.6000", "10.00"]])
    book.apply_delta(
        {"side": "yes", "price_dollars": "0.5900", "delta_fp": "5.00", "ts_ms": 1669149841000},
        seq=2, recv_ts=1234.5, use_yes_price=True,
    )
    assert book.last_update_exchange_ts_ms == 1669149841000
    assert book.last_update_recv_ts == 1234.5
    assert book.last_seq == 2
    assert book.snapshot_seq == 1


def test_delta_with_an_unknown_side_is_refused():
    book = fresh(yes=[["0.6000", "10.00"]])
    with pytest.raises(BookError):
        book.apply_delta({"side": "maybe", "price_dollars": "0.6000", "delta_fp": "1.00"},
                         seq=2, recv_ts=1.0, use_yes_price=True)


# -- Section 7 invariants -------------------------------------------------------

def test_crossed_book_is_refused():
    with pytest.raises(BookError, match="crossed book"):
        fresh(yes=[["0.7200", "10.00"]], no=[["0.7100", "10.00"]])


def test_a_delta_that_crosses_the_book_is_refused():
    book = fresh(yes=[["0.6000", "10.00"]], no=[["0.7000", "10.00"]])
    with pytest.raises(BookError, match="crossed book"):
        book.apply_delta({"side": "yes", "price_dollars": "0.8000", "delta_fp": "1.00"},
                         seq=2, recv_ts=1.0, use_yes_price=True)


def test_a_level_on_both_sides_is_refused_as_a_duplicate():
    with pytest.raises(BookError, match="both sides"):
        fresh(yes=[["0.7000", "10.00"]], no=[["0.7000", "10.00"]])


def test_prices_outside_the_valid_range_are_refused():
    with pytest.raises(BookError):
        fresh(yes=[["0.0000", "10.00"]])
    with pytest.raises(BookError):
        fresh(yes=[["1.0000", "10.00"]])
    # The endpoints are exclusive; one tick inside is fine.
    assert fresh(yes=[[f"{MAX_PRICE / PRICE_SCALE:.4f}", "1.00"]]).bids


def test_duplicate_snapshot_level_is_refused():
    with pytest.raises(BookError, match="duplicate"):
        fresh(yes=[["0.6000", "10.00"], ["0.6000", "5.00"]])


def test_an_unchanged_book_stays_intact_and_valid():
    """
    Section 3: absence of a market-specific update does not make a book stale.
    The book object carries no time-based decay at all -- staleness is a session
    property, tested in test_depthrec_session.py.
    """
    book = fresh(yes=[["0.6000", "10.00"]], no=[["0.7000", "10.00"]])
    before = book.top_of_book()
    book.assert_invariants()
    assert book.top_of_book() == before
    assert book.updates_applied == 0
