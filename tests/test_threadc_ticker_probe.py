"""
The ticker size-only emission probe's matching logic.

These tests fix the meaning of the words the probe reports. A "qualifying
opportunity", a "match" and each of the three ambiguities are defined here by
example, so a later change to the matcher that quietly widens what counts as
evidence fails a test rather than producing a more confident number.

Every timestamp is supplied explicitly: the matcher advances time only from
observed receive timestamps, so these are exact replays, not timing-dependent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threadc import ticker_probe as tp  # noqa: E402


# -- Builders -----------------------------------------------------------------

def top(bid_p=5000, bid_q=1000, ask_p=5200, ask_q=800) -> tp.Top:
    return tp.Top(bid_price=bid_p, bid_qty=bid_q, ask_price=ask_p, ask_qty=ask_q)


def ticker_msg(
    *, bid="0.5000", ask="0.5200", bid_size="10.00", ask_size="8.00",
    price="0.5100", volume="100.00", oi="50.00", dollar_volume=51,
    dollar_oi=25, last_trade="1.00",
) -> dict:
    return {
        "market_ticker": "M",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "yes_bid_size_fp": bid_size,
        "yes_ask_size_fp": ask_size,
        "price_dollars": price,
        "volume_fp": volume,
        "open_interest_fp": oi,
        "dollar_volume": dollar_volume,
        "dollar_open_interest": dollar_oi,
        "last_trade_size_fp": last_trade,
    }


def feed(matcher: tp.StreamingMatcher, events) -> tp.ProbeAnalysis:
    """Replay (kind, payload, ts) triples, then close every window."""
    last = 0.0
    for kind, payload, ts in events:
        last = ts
        if kind == "top":
            matcher.on_top("M", payload, ts)
        else:
            matcher.on_ticker("M", payload, ts)
    return matcher.flush(last + 10 * tp.MATCH_WINDOW_POST_SECONDS)


# -- What counts as an opportunity --------------------------------------------

def test_size_only_change_at_touch_is_an_opportunity():
    before, after = top(bid_q=1000), top(bid_q=400)
    assert tp.is_opportunity(before, after)
    assert tp.size_changed_sides(before, after) == ["bid"]


def test_price_change_is_not_an_opportunity():
    assert not tp.is_opportunity(top(bid_p=5000), top(bid_p=4900))


def test_price_change_on_the_other_side_disqualifies_a_size_change():
    # The ask moving would independently trigger a ticker message, so a bid-size
    # change alongside it is not usable evidence.
    assert not tp.is_opportunity(top(bid_q=1000, ask_p=5200), top(bid_q=400, ask_p=5300))


def test_both_touch_sizes_changing_is_one_opportunity_over_two_sides():
    before, after = top(bid_q=1000, ask_q=800), top(bid_q=400, ask_q=900)
    assert tp.size_changed_sides(before, after) == ["bid", "ask"]


def test_side_appearing_from_empty_is_not_a_size_only_change():
    before = tp.Top(bid_price=None, bid_qty=None, ask_price=5200, ask_qty=800)
    after = top(bid_p=5000, bid_q=1000)
    assert not tp.is_opportunity(before, after)


def test_deeper_book_change_produces_no_transition_at_all():
    matcher = tp.StreamingMatcher()
    unchanged = top()
    result = feed(matcher, [("top", unchanged, 1.0), ("top", unchanged, 2.0)])
    assert result.top_of_book_transitions == 0
    assert result.qualifying_opportunities == 0


# -- The three unambiguous outcomes -------------------------------------------

def test_matched_when_a_clean_ticker_carries_the_new_size():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),   # establishes the diff base
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid_size="4.00"), 5.1),
    ])
    assert result.outcomes[tp.OUTCOME_MATCHED] == 1
    assert result.unambiguous_opportunities == 1
    assert result.match_rate == 1.0


def test_no_ticker_update_when_the_window_is_silent():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid_size="4.00"), 5.0 + tp.MATCH_WINDOW_POST_SECONDS + 0.5),
    ])
    assert result.outcomes[tp.OUTCOME_NO_TICKER] == 1
    assert result.matched == 0


def test_ticker_present_but_not_carrying_the_new_size():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        # Volume moved; the size field still shows the stale 10.00.
        ("ticker", ticker_msg(bid_size="10.00", volume="101.00"), 5.1),
    ])
    assert result.outcomes[tp.OUTCOME_TICKER_WITHOUT_SIZE] == 1
    assert result.matched == 0


def test_ticker_just_before_the_book_event_still_counts():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("ticker", ticker_msg(bid_size="4.00"), 5.0 - tp.MATCH_WINDOW_PRE_SECONDS / 2),
        ("top", top(bid_q=400), 5.0),
    ])
    assert result.outcomes[tp.OUTCOME_MATCHED] == 1


# -- The three ambiguities ----------------------------------------------------

def test_adjacent_book_event_makes_the_opportunity_ambiguous():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),          # the opportunity
        ("top", top(bid_q=400, ask_p=5300), 5.5),  # inside its window
        ("ticker", ticker_msg(bid_size="4.00"), 5.6),
    ])
    assert result.outcomes[tp.AMBIGUOUS_ADJACENT_BOOK_EVENT] == 1
    assert result.matched == 0
    assert result.unambiguous_opportunities == 0


def test_first_ticker_for_a_market_cannot_be_diffed():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid_size="4.00"), 5.1),   # the market's first ticker
    ])
    assert result.outcomes[tp.AMBIGUOUS_NO_PRIOR_TICKER] == 1
    assert result.matched == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", "0.5300"),
        ("volume", "101.00"),
        ("oi", "51.00"),
        ("dollar_volume", 52),
        ("dollar_oi", 26),
        ("last_trade", "2.00"),
    ],
)
def test_any_other_changed_field_makes_it_ambiguous(field, value):
    """
    A ticker message that carries the new size is NOT evidence if anything else
    changed too -- that other field could have triggered the message on its own.
    """
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid_size="4.00", **{field: value}), 5.1),
    ])
    assert result.outcomes[tp.AMBIGUOUS_OTHER_FIELD_CHANGED] == 1
    assert result.matched == 0


def test_a_touch_price_change_in_the_ticker_is_also_ambiguous():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid="0.5000", bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid="0.4900", bid_size="4.00"), 5.1),
    ])
    assert result.outcomes[tp.AMBIGUOUS_OTHER_FIELD_CHANGED] == 1


def test_a_clean_candidate_wins_over_a_dirty_one_in_the_same_window():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid_size="4.00", volume="101.00"), 5.1),  # dirty
        ("ticker", ticker_msg(bid_size="4.00", volume="101.00"), 5.2),  # clean vs 5.1
    ])
    assert result.outcomes[tp.OUTCOME_MATCHED] == 1


def test_outcomes_partition_the_opportunity_set():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid_size="4.00"), 5.1),
        ("top", top(bid_q=700), 20.0),
    ])
    assert result.qualifying_opportunities == 2
    assert (
        result.unambiguous_opportunities + result.ambiguous_opportunities
        == result.qualifying_opportunities
    )


# -- Verdicts -----------------------------------------------------------------

def test_too_few_opportunities_is_insufficient_not_a_conclusion():
    analysis = tp.ProbeAnalysis()
    analysis.outcomes[tp.OUTCOME_MATCHED] = tp.MIN_UNAMBIGUOUS_OPPORTUNITIES - 1
    assert analysis.verdict() == tp.VERDICT_INSUFFICIENT


def test_a_clean_sweep_confirms():
    analysis = tp.ProbeAnalysis()
    analysis.outcomes[tp.OUTCOME_MATCHED] = tp.MIN_UNAMBIGUOUS_OPPORTUNITIES
    assert analysis.verdict() == tp.VERDICT_CONFIRMED


def test_a_silent_ticker_refutes():
    analysis = tp.ProbeAnalysis()
    analysis.outcomes[tp.OUTCOME_NO_TICKER] = tp.MIN_UNAMBIGUOUS_OPPORTUNITIES
    assert analysis.verdict() == tp.VERDICT_REFUTED
    assert analysis.match_rate == 0.0


def test_a_middling_rate_is_indeterminate_not_a_pass():
    analysis = tp.ProbeAnalysis()
    analysis.outcomes[tp.OUTCOME_MATCHED] = 97
    analysis.outcomes[tp.OUTCOME_NO_TICKER] = 3
    assert analysis.verdict() == tp.VERDICT_INDETERMINATE


def test_ambiguous_opportunities_never_reach_the_denominator():
    analysis = tp.ProbeAnalysis()
    analysis.outcomes[tp.OUTCOME_MATCHED] = 10
    analysis.outcomes[tp.AMBIGUOUS_OTHER_FIELD_CHANGED] = 10_000
    assert analysis.unambiguous_opportunities == 10
    assert analysis.verdict() == tp.VERDICT_INSUFFICIENT


# -- The report ---------------------------------------------------------------

def test_report_carries_no_market_identity():
    matcher = tp.StreamingMatcher()
    result = feed(matcher, [
        ("ticker", ticker_msg(bid_size="10.00"), 0.5),
        ("top", top(bid_q=1000), 1.0),
        ("top", top(bid_q=400), 5.0),
        ("ticker", ticker_msg(bid_size="4.00"), 5.1),
    ])
    text = tp.format_report(result.report())
    assert "market_ticker" not in text
    assert '"M"' not in text
    assert "0.50" not in text


def test_report_counts_are_internally_consistent():
    analysis = tp.ProbeAnalysis()
    analysis.outcomes[tp.OUTCOME_MATCHED] = 120
    analysis.outcomes[tp.OUTCOME_NO_TICKER] = 1
    analysis.outcomes[tp.AMBIGUOUS_ADJACENT_BOOK_EVENT] = 7
    report = analysis.report()
    assert report["qualifying_best_touch_size_only_opportunities"] == 128
    assert report["unambiguous_opportunities"] == 121
    assert report["with_corresponding_ticker_update_carrying_changed_size"] == 120
    assert report["without_corresponding_ticker_update"] == 1
    assert report["ambiguity"]["total"] == 7


# -- Field reduction ----------------------------------------------------------

def test_ticker_sizes_are_read_on_the_canonical_quantity_scale():
    observation, _ = tp.reduce_ticker_message(ticker_msg(bid_size="10.00"), None, 1.0)
    assert observation.bid_size == 1000        # 10 contracts, hundredths
    assert observation.kind == tp.TICKER_NO_PRIOR


def test_an_unreadable_size_field_is_none_not_a_crash():
    observation, _ = tp.reduce_ticker_message(
        ticker_msg(bid_size="not-a-number"), None, 1.0
    )
    assert observation.bid_size is None


def test_identical_consecutive_tickers_are_clean():
    first = ticker_msg()
    _, kept = tp.reduce_ticker_message(first, None, 1.0)
    observation, _ = tp.reduce_ticker_message(ticker_msg(bid_size="4.00"), kept, 2.0)
    assert observation.kind == tp.TICKER_CLEAN


def test_ticker_touch_prices_are_read_on_the_canonical_price_scale():
    assert tp.ticker_touch_prices(ticker_msg()) == (5000, 5200)


# -- Cross-channel convention check -------------------------------------------

def test_touch_price_agreement_is_measured_not_assumed():
    matcher = tp.StreamingMatcher()
    matcher.on_top("M", top(bid_p=5000, bid_q=1000, ask_p=5200, ask_q=800), 1.0)
    matcher.on_ticker("M", ticker_msg(), 1.1)
    result = matcher.flush(20.0)
    assert result.touch_price_comparisons == 2
    assert result.touch_price_disagreements == 0
    assert result.touch_size_comparisons == 2
    assert result.touch_size_disagreements == 0


def test_a_convention_mismatch_shows_up_as_a_disagreement():
    matcher = tp.StreamingMatcher()
    matcher.on_top("M", top(bid_p=5000, ask_p=5200), 1.0)
    # A ticker whose bid/ask are inverted relative to the reconstructed book.
    matcher.on_ticker("M", ticker_msg(bid="0.5200", ask="0.5000"), 1.1)
    result = matcher.flush(20.0)
    assert result.touch_price_disagreements == 2


# -- Bookkeeping --------------------------------------------------------------

def test_reset_books_drops_tops_without_losing_observed_windows():
    matcher = tp.StreamingMatcher()
    matcher.on_ticker("M", ticker_msg(bid_size="10.00"), 0.5)
    matcher.on_top("M", top(bid_q=1000), 1.0)
    matcher.on_top("M", top(bid_q=400), 5.0)
    matcher.on_ticker("M", ticker_msg(bid_size="4.00"), 5.1)
    matcher.reset_books()
    # The next top after a reset is a fresh base, not a transition.
    matcher.on_top("M", top(bid_q=50), 6.0)
    result = matcher.flush(30.0)
    assert result.outcomes[tp.OUTCOME_MATCHED] == 1
    assert result.qualifying_opportunities == 1


def test_recent_ticker_retention_is_bounded():
    matcher = tp.StreamingMatcher()
    for i in range(500):
        matcher.on_ticker("M", ticker_msg(), 1.0 + i)
    assert len(matcher.markets["M"].recent_tickers) <= 16


def test_selection_rule_is_deterministic_and_bounded():
    tickers = [f"KX{i:04d}" for i in range(50)]
    assert tp.select_probe_markets(reversed(tickers), 5) == sorted(tickers)[:5]
    assert tp.select_probe_markets(tickers, 0) == sorted(tickers)


# -- The consumer-backlog guard -----------------------------------------------

def test_warmup_opportunities_are_excluded_not_counted_as_evidence():
    """
    Subscribing delivers a snapshot for every market at once. A consumer
    draining that burst is behind by definition, so nothing inside the warm-up
    may be read as a protocol fact in either direction.
    """
    matcher = tp.StreamingMatcher()
    matcher.note_lag(1000.0, 0.05)          # first message: seeds the warm-up
    matcher.on_ticker("M", ticker_msg(bid_size="10.00"), 1000.1)
    matcher.on_top("M", top(bid_q=1000), 1000.2)
    matcher.on_top("M", top(bid_q=400), 1001.0)
    matcher.on_ticker("M", ticker_msg(bid_size="4.00"), 1001.1)
    result = matcher.flush(1010.0)

    assert result.outcomes[tp.AMBIGUOUS_CONSUMER_BACKLOG] == 1
    assert result.matched == 0
    assert result.unambiguous_opportunities == 0


def test_after_the_warmup_a_clean_window_is_evidence_again():
    matcher = tp.StreamingMatcher()
    matcher.note_lag(1000.0, 0.05)
    after = 1000.0 + tp.WARMUP_SECONDS + 10.0
    matcher.note_lag(after, 0.05)
    matcher.on_ticker("M", ticker_msg(bid_size="10.00"), after)
    matcher.on_top("M", top(bid_q=1000), after + 0.1)
    matcher.on_top("M", top(bid_q=400), after + 1.0)
    matcher.on_ticker("M", ticker_msg(bid_size="4.00"), after + 1.1)
    result = matcher.flush(after + 20.0)

    assert result.outcomes[tp.OUTCOME_MATCHED] == 1


def test_a_lag_excursion_disqualifies_the_windows_it_overlaps():
    matcher = tp.StreamingMatcher()
    base = 1000.0
    matcher.note_lag(base, 0.05)
    after = base + tp.WARMUP_SECONDS + 10.0
    matcher.note_lag(after, 0.05)
    matcher.on_ticker("M", ticker_msg(bid_size="10.00"), after)
    matcher.on_top("M", top(bid_q=1000), after + 0.1)
    matcher.on_top("M", top(bid_q=400), after + 1.0)
    # The consumer falls well behind while this opportunity's window is open.
    matcher.note_lag(after + 1.05, 0.05 + tp.LAG_GUARD_SECONDS + 1.0)
    matcher.on_ticker("M", ticker_msg(bid_size="4.00"), after + 1.1)
    result = matcher.flush(after + 20.0)

    assert result.outcomes[tp.AMBIGUOUS_CONSUMER_BACKLOG] == 1
    assert result.matched == 0


def test_the_lag_baseline_is_the_minimum_not_the_first_sample():
    """
    Absolute lag carries the host's clock offset against Kalshi's. Only
    excursions above the smallest lag ever seen mean this consumer is behind.
    """
    matcher = tp.StreamingMatcher()
    matcher.note_lag(1000.0, 30.0)       # a badly offset clock, not a backlog
    matcher.note_lag(1001.0, 29.9)
    matcher.note_lag(1002.0, 30.0)
    assert matcher.baseline_lag == 29.9
    assert matcher._backlog_open_since is None


def test_backlog_is_reported_and_partitions_with_everything_else():
    analysis = tp.ProbeAnalysis()
    analysis.outcomes[tp.OUTCOME_MATCHED] = 150
    analysis.outcomes[tp.AMBIGUOUS_CONSUMER_BACKLOG] = 4000
    report = analysis.report()
    assert report["ambiguity"][tp.AMBIGUOUS_CONSUMER_BACKLOG] == 4000
    assert report["unambiguous_opportunities"] == 150
    assert analysis.verdict() == tp.VERDICT_CONFIRMED


def test_lag_histogram_summarises_without_keeping_samples():
    hist = tp.LagHistogram()
    for i in range(1000):
        hist.add(0.10 + (i % 5) * 0.01)
    hist.add(9.0)
    assert hist.count == 1001
    assert hist.min_lag == pytest.approx(0.10)
    assert hist.max_lag == pytest.approx(9.0)
    assert hist.spread() is not None
    assert len(hist.buckets) < 20     # buckets, not a growing sample list


def test_exchange_timestamp_prefers_ts_ms():
    assert tp.exchange_ts_seconds({"ts_ms": 1793145600123, "ts": 1793145600}) == \
        pytest.approx(1793145600.123)
    assert tp.exchange_ts_seconds({"ts": 1793145600}) == 1793145600.0
    assert tp.exchange_ts_seconds({}) is None
