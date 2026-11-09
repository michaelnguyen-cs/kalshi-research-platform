"""
The streaming section 8-v2 evaluator: same semantics, bounded memory.

The frozen evaluator decoded the whole archive into per-market event lists before
it evaluated anything; this one reads the archive twice, keeps only run-level
aggregates and an on-disk market index between the passes, and holds one market
at a time. That is a change to storage and iteration and to nothing else, so
these tests are about the seams the change created:

  * the market index and the order it hands markets back
  * the two global quantities -- section 5's cutoff and section 6's boundary
    instants -- which are found in one pass and applied in the other
  * per-market and per-epoch state that must not leak across a market boundary
  * batch sizes, which must not be observable in the result

The first block is the strongest of them: every fixture is evaluated by BOTH the
streaming evaluator and a verbatim copy of the frozen one (tests/frozen_v2_reference.py)
and their complete structured results are compared field by field. The rest name
individual clauses so a failure says which one broke.

Nothing here changes, relaxes or reinterprets a section 8-v2 rule. The frozen
parameters are asserted in tests/test_depthrec_equivalence_v2.py and are not
restated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec import equivalence_v2 as ev2  # noqa: E402
from depthrec.book import CanonicalBook  # noqa: E402
from depthrec.equivalence_v2 import (  # noqa: E402
    DeltaEvent,
    Verification,
    _find_baseline,
    evaluate_archive,
    key_evidence,
)
from tests.frozen_v2_reference import evaluate_archive as frozen_evaluate  # noqa: E402
from tests.test_depthrec_equivalence_v2 import (  # noqa: E402
    BASE_TS,
    Archive,
    build,
    paired_run,
    tail_setter,
)

EVIDENCE = Path("<EVIDENCE_DIR>/equivalence_probe_2026-08-23/verify/"
                "equivprobe.sqlite")


def epochs_of(result, market: str):
    return [e for e in result.epochs if e.market == market]


def pairs_for(result, market: str) -> int:
    return sum(e.paired for e in result.epochs if e.market == market)


# -- Fixtures, each exercising one streaming seam ------------------------------------


def f_two_markets_interleaved(a: Archive) -> None:
    """Two clean markets whose frames alternate, written in reverse ticker order."""
    a.subscribe(["ZULU", "ALPHA", "TAILSET"])
    a.snapshot("A", "ZULU", {5000: 100}, {6000: 200})
    a.snapshot("B", "ZULU", {5000: 100}, {6000: 200})
    a.snapshot("A", "ALPHA", {5000: 100}, {6000: 200})
    a.snapshot("B", "ALPHA", {5000: 100}, {6000: 200})
    for k in range(150):
        for s in ("A", "B"):
            a.delta(s, "ZULU", "no", 6000, 1, BASE_TS + k)
        for s in ("A", "B"):
            a.delta(s, "ALPHA", "no", 6000, 1, BASE_TS + k)
    tail_setter(a)


def f_cutoff_set_by_another_market(a: Archive) -> None:
    """
    Section 5's cutoff is global. The market that sets `archive_max_ts_ms` is
    sorted LAST, so a per-market evaluator that had not already read the whole
    archive would use the wrong cutoff for every market before it.
    """
    a.subscribe(["AAA", "ZZZ_SETS_THE_MAX"])
    a.snapshot("A", "AAA", {5000: 100}, {6000: 200})
    a.snapshot("B", "AAA", {5000: 100}, {6000: 200})
    for k in range(120):
        for s in ("A", "B"):
            a.delta(s, "AAA", "no", 6000, 1, BASE_TS + k)
    for s in ("A", "B"):
        a.delta(s, "AAA", "no", 6000, 1, BASE_TS + 9000)     # inside the band
    a.snapshot("A", "ZZZ_SETS_THE_MAX", {5000: 100}, {6000: 200})
    a.snapshot("B", "ZZZ_SETS_THE_MAX", {5000: 100}, {6000: 200})
    for k in range(120):
        for s in ("A", "B"):
            a.delta(s, "ZZZ_SETS_THE_MAX", "no", 6000, 1, BASE_TS + k)
    for s in ("A", "B"):
        a.delta(s, "ZZZ_SETS_THE_MAX", "no", 6000, 1, BASE_TS + 10_000)


def f_gap_in_one_market_segments_all(a: Archive) -> None:
    """
    Section 6: a sequence gap ends the current epoch for every market on that
    subscription. The gap is raised on stream A and the next A frame after it --
    the frame the boundary instant is taken from -- belongs to MKT2. MKT1 is
    evaluated first, never shares a frame with MKT2, and must still be segmented.
    """
    a.subscribe(["MKT1", "MKT2", "TAILSET"])
    for t in ("MKT1", "MKT2"):
        a.snapshot("A", t, {5000: 100}, {6000: 200})
        a.snapshot("B", t, {5000: 100}, {6000: 200})
    for k in range(60):
        for t in ("MKT1", "MKT2"):
            for s in ("A", "B"):
                a.delta(s, t, "no", 6000, 1, BASE_TS + k)
    a.sequence_gap("A")
    for s in ("A", "B"):
        a.delta(s, "MKT2", "no", 6000, 1, BASE_TS + 60)
    for k in range(61, 200):
        for t in ("MKT1", "MKT2"):
            for s in ("A", "B"):
                a.delta(s, t, "no", 6000, 1, BASE_TS + k)
    tail_setter(a)


def f_disconnect_interrupts(a: Archive) -> None:
    """An interrupted epoch with a one-sided tail, plus a clean market beside it."""
    a.subscribe(["MKT", "OTHER", "TAILSET"])
    for t in ("MKT", "OTHER"):
        a.snapshot("A", t, {5000: 100}, {6000: 200})
        a.snapshot("B", t, {5000: 100}, {6000: 200})
    for k in range(150):
        for t in ("MKT", "OTHER"):
            for s in ("A", "B"):
                a.delta(s, t, "no", 6000, 1, BASE_TS + k)
    for k in range(150, 155):
        a.delta("A", "MKT", "no", 6000, 1, BASE_TS + k)      # A only
    a.disconnect("A")
    for k in range(200, 320):
        for t in ("MKT", "OTHER"):
            for s in ("A", "B"):
                a.delta(s, t, "no", 6000, 1, BASE_TS + k)
    tail_setter(a)


def f_head_block_and_divergence(a: Archive) -> None:
    """A pre-baseline head on one market and a deep-ladder divergence on another."""
    a.subscribe(["AAA_HEAD", "BBB_DIVERGE", "TAILSET"])
    a.snapshot("A", "AAA_HEAD", {5000: 100}, {6000: 205})
    a.snapshot("B", "AAA_HEAD", {5000: 100}, {6000: 200})
    a.delta("B", "AAA_HEAD", "no", 6000, 5, BASE_TS - 1)
    for k in range(200):
        for s in ("A", "B"):
            a.delta(s, "AAA_HEAD", "no", 6000, 1, BASE_TS + k)
    a.snapshot("A", "BBB_DIVERGE", {5000: 100, 3000: 50}, {6000: 200})
    a.snapshot("B", "BBB_DIVERGE", {5000: 100, 3000: 50}, {6000: 200})
    for k in range(40):
        for s in ("A", "B"):
            a.delta(s, "BBB_DIVERGE", "no", 6000, 1, BASE_TS + k)
    a.delta("A", "BBB_DIVERGE", "yes", 3000, 7, BASE_TS + 100)
    a.delta("B", "BBB_DIVERGE", "yes", 3000, 9, BASE_TS + 100)
    tail_setter(a)


def f_market_without_a_snapshot_on_b(a: Archive) -> None:
    """
    A market stream A carries and stream B never snapshots. The frozen evaluator
    skipped it whole -- including its guard-band exclusions -- and so must this.
    """
    a.subscribe(["HALF", "MKT", "TAILSET"])
    a.snapshot("A", "HALF", {5000: 100}, {6000: 200})
    for k in range(30):
        a.delta("A", "HALF", "no", 6000, 1, BASE_TS + k)
    a.delta("A", "HALF", "no", 6000, 1, BASE_TS + 9500)
    paired_run(a, "MKT", n=150)
    tail_setter(a)


def f_market_set_mismatch_with_a_breach(a: Archive) -> None:
    """Criterion 6 fails while a per-market cap is also breached."""
    for s, tickers in (("A", ["MKT", "EXTRA"]), ("B", ["MKT"])):
        a._write(s, "out", {
            "id": 1, "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}})
    a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
    a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
    for k in range(30):
        for s in ("A", "B"):
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
    for k in range(40):
        for s in ("A", "B"):
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 9000 + k)


def f_empty_archive(a: Archive) -> None:
    """No frames at all."""
    return None


def f_subscription_only(a: Archive) -> None:
    """Subscriptions and nothing else: markets exist but no market has events."""
    a.subscribe(["MKT", "OTHER"])


SCENARIOS = {
    "two_markets_interleaved": f_two_markets_interleaved,
    "cutoff_set_by_another_market": f_cutoff_set_by_another_market,
    "gap_in_one_market_segments_all": f_gap_in_one_market_segments_all,
    "disconnect_interrupts": f_disconnect_interrupts,
    "head_block_and_divergence": f_head_block_and_divergence,
    "market_without_a_snapshot_on_b": f_market_without_a_snapshot_on_b,
    "market_set_mismatch_with_a_breach": f_market_set_mismatch_with_a_breach,
    "empty_archive": f_empty_archive,
    "subscription_only": f_subscription_only,
}


# -- The differential: streaming == frozen, field by field ---------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_streaming_result_equals_the_frozen_result_field_by_field(name, tmp_path):
    """
    The whole point of the branch, on one fixture at a time.

    `as_dict()` is the artefact the formal runs are accepted against, so equality
    is asserted on the complete structure rather than on the headline counts.
    """
    db = build(tmp_path, SCENARIOS[name], name=f"{name}.sqlite")
    assert evaluate_archive(db).as_dict() == frozen_evaluate(db).as_dict()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_streaming_failure_list_is_the_frozen_failure_list(name, tmp_path):
    """
    Failure ORDER is part of the result: the JSON carries the list, and the list
    is built market by market in the order markets are processed.
    """
    db = build(tmp_path, SCENARIOS[name], name=f"{name}.sqlite")
    new = evaluate_archive(db)
    old = frozen_evaluate(db)
    assert [(f.kind, f.market, f.epoch, f.detail) for f in new.failures] == \
           [(f.kind, f.market, f.epoch, f.detail) for f in old.failures]
    assert [(e.market, e.epoch, e.paired, e.excluded_pre_baseline,
             e.excluded_guard_band, e.excluded_epoch_tail, e.interrupted, e.empty)
            for e in new.epochs] == \
           [(e.market, e.epoch, e.paired, e.excluded_pre_baseline,
             e.excluded_guard_band, e.excluded_epoch_tail, e.interrupted, e.empty)
            for e in old.epochs]


# -- Deterministic market processing order -------------------------------------------


def test_markets_are_processed_in_sorted_ticker_order_whatever_the_frame_order(tmp_path):
    """
    The frozen evaluator iterated `sorted(...)` of the market set. The streaming
    one takes markets from an index built in first-seen order, so the sort has to
    be reasserted -- otherwise the failure list, the epoch list and the breach
    list would come out in archive order.
    """
    def fn(a):
        a.subscribe(["MZZZ", "MAAA", "MMMM", "TAILSET"])
        for t in ("MZZZ", "MAAA", "MMMM"):
            a.snapshot("A", t, {5000: 100}, {6000: 200})
            a.snapshot("B", t, {5000: 100}, {6000: 200})
        for k in range(120):
            for t in ("MZZZ", "MAAA", "MMMM"):
                for s in ("A", "B"):
                    a.delta(s, t, "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    seen = [e.market for e in r.epochs]
    assert seen == sorted(seen)
    assert seen[:4] == ["MAAA", "MMMM", "MZZZ", "TAILSET"]


def test_the_failure_list_follows_the_sorted_market_order(tmp_path):
    """Two markets fail; the failures come out in ticker order, not frame order."""
    def fn(a):
        a.subscribe(["ZED", "ABE"])
        for t in ("ZED", "ABE"):
            # B carries a level A never has: no baseline on either market.
            a.snapshot("A", t, {5000: 100}, {6000: 200})
            a.snapshot("B", t, {5000: 100, 4000: 77}, {6000: 200})
        for k in range(20):
            for t in ("ZED", "ABE"):
                for s in ("A", "B"):
                    a.delta(s, t, "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.market for f in r.failures] == ["ABE", "ZED"]
    assert {f.kind for f in r.failures} == {"NO_BASELINE"}


def test_a_criterion_6_failure_still_precedes_every_per_market_failure(tmp_path):
    """
    MARKET_SET_MISMATCH is raised before the market loop and must stay first,
    whatever the markets are called.
    """
    def fn(a):
        for s, tickers in (("A", ["AAA"]), ("B", ["AAA", "BBB"])):
            a._write(s, "out", {
                "id": 1, "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"],
                           "market_tickers": tickers}})
        a.snapshot("A", "AAA", {5000: 100}, {6000: 200})
        a.snapshot("B", "AAA", {5000: 100, 4000: 77}, {6000: 200})
        for k in range(20):
            for s in ("A", "B"):
                a.delta(s, "AAA", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == ["MARKET_SET_MISMATCH", "NO_BASELINE"]


# -- §4.1: the baseline, its objective, its tie-break and its bound -------------------


def _noop(rid: int, ts_ms: int, price: int) -> DeltaEvent:
    """
    A zero-size delta at a price the book does not carry.

    It leaves both reconstructions untouched, so every prefix of a list of these
    is book-identical to every other and the baseline search is decided purely by
    the verification tuples -- which is what §4.1's objective and tie-break are
    stated in terms of.
    """
    body = {"market_ticker": "MKT", "side": "yes",
            "price_dollars": f"{price / 10000:.4f}", "delta_fp": "0.00",
            "ts_ms": ts_ms}
    return DeltaEvent(rid, 0.0, 1, rid, "MKT",
                      Verification(ts_ms, "yes", price, 0), body)


def _books():
    return CanonicalBook(ticker="MKT"), CanonicalBook(ticker="MKT")


def test_the_baseline_minimises_i_plus_j_not_i():
    """
    §4.1: "minimising i + j". A candidate at (5, 0) has the lower i and the
    higher sum, and must lose to (1, 3).
    """
    a = [_noop(1, 900, 4000), _noop(2, 100, 4000)] + \
        [_noop(3 + n, 800 + n, 4000) for n in range(3)]
    b = [_noop(20, 500, 3000), _noop(21, 501, 3000), _noop(22, 502, 3000),
         _noop(23, 100, 4000)]
    a.append(_noop(30, 500, 3000))                  # matches b[0] at (5, 0)
    a0, b0 = _books()
    assert _find_baseline(a, b, a0, b0, []) == (1, 3)


def test_the_baseline_tie_is_broken_by_the_lowest_i_then_the_lowest_j():
    """§4.1: ties by lowest i, then lowest j. (0, 1) and (1, 0) both sum to 1."""
    a = [_noop(1, 111, 4000), _noop(2, 222, 3000)]
    b = [_noop(3, 222, 3000), _noop(4, 111, 4000)]
    a0, b0 = _books()
    assert _find_baseline(a, b, a0, b0, []) == (0, 1)


def test_a_baseline_at_the_last_searchable_position_is_found():
    """The bound is 1,000 POSITIONS, so index 999 is inside it."""
    p = frozen.V2_HEAD_SEARCH_POSITIONS - 1
    a = [_noop(n, 10_000 + n, 4000) for n in range(p + 40)]
    b = [_noop(1000 + n, 90_000 + n, 3000) for n in range(p)]
    b.append(_noop(9999, 10_000 + p, 4000))
    b.extend(_noop(20_000 + n, 70_000 + n, 3000) for n in range(20))
    a0, b0 = _books()
    assert _find_baseline(a, b, a0, b0, []) == (p, p)


def test_a_baseline_one_position_past_the_bound_is_not_found():
    """
    §4.1's bound is not widened. At index 1,000 the same construction returns
    None, which the caller turns into NO_BASELINE and a FAIL.
    """
    p = frozen.V2_HEAD_SEARCH_POSITIONS
    a = [_noop(n, 10_000 + n, 4000) for n in range(p + 40)]
    b = [_noop(1000 + n, 90_000 + n, 3000) for n in range(p)]
    b.append(_noop(9999, 10_000 + p, 4000))
    b.extend(_noop(20_000 + n, 70_000 + n, 3000) for n in range(20))
    a0, b0 = _books()
    assert _find_baseline(a, b, a0, b0, []) is None


def test_the_positional_prefix_before_the_baseline_is_excluded_not_paired(tmp_path):
    """
    §4: the i + j records before the baseline are EXCLUDED_PRE_BASELINE, and the
    baseline event itself is the first PAIRED event rather than an exclusion.

    A carries two private head events and B one, so the baseline is at (2, 1) and
    exactly three records are excluded. The private events are zero-size deltas
    at an absent price -- book no-ops with distinct verification tuples -- so the
    exclusion is decided by §4.1's objective and not by the ladders diverging.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        a.delta("A", "MKT", "yes", 4000, 0, BASE_TS - 5)
        a.delta("A", "MKT", "yes", 3000, 0, BASE_TS - 4)
        a.delta("B", "MKT", "yes", 2000, 0, BASE_TS - 3)
        for k in range(200):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    db = build(tmp_path, fn)
    r = evaluate_archive(db)
    assert [f.kind for f in r.failures] == []
    e = epochs_of(r, "MKT")[0]
    assert (e.baseline_i, e.baseline_j) == (2, 1)
    assert e.excluded_pre_baseline == 3
    assert e.paired == 200       # the baseline event is the first of them
    assert r.as_dict() == frozen_evaluate(db).as_dict()


# -- §4.1/§7: positional pairing and the full-ladder comparison -----------------------


def test_pairing_after_the_baseline_is_strictly_positional(tmp_path):
    """
    §4.1. A tuple mismatch at a positional offset is a FAIL at that offset; it is
    not repaired by looking ahead, and the offset is reported.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(30):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        a.delta("A", "MKT", "no", 6000, 1, BASE_TS + 500)
        a.delta("B", "MKT", "no", 6000, 1, BASE_TS + 501)      # different ts_ms
        for k in range(30):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 600 + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    kinds = [f.kind for f in r.failures]
    assert kinds == ["ALIGNMENT_VERIFICATION"]
    f = r.failures[0]
    assert f.evidence["offset"] == 29
    assert set(f.evidence["a_key"]) == {"ts_ms", "side", "price", "delta"}
    assert f.evidence["a_key"]["ts_ms"] == BASE_TS + 500
    assert f.evidence["b_key"]["ts_ms"] == BASE_TS + 501


def test_the_full_ladder_is_compared_at_each_offset_and_stops_there(tmp_path):
    """
    §7. The comparison is the FULL canonical book after EVERY paired event, so a
    disagreement three levels below the touch is caught at its own positional
    offset -- not at the end of the epoch, and not only at top of book.

    The offset the failure names and the epoch's paired count together say where
    the walk stopped, which is the part a streaming implementation could get
    wrong by comparing once at the end.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100, 3000: 50}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100, 3000: 50}, {6000: 200})
        for k in range(50):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        a.delta("A", "MKT", "yes", 3000, 7, BASE_TS + 500)   # deep bid, A by +7
        a.delta("B", "MKT", "yes", 3000, 9, BASE_TS + 500)   # deep bid, B by +9
        for k in range(50):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 600 + k)
        tail_setter(a)
    db = build(tmp_path, fn)
    r = evaluate_archive(db)
    assert [f.kind for f in r.failures] == ["ALIGNMENT_VERIFICATION"]
    assert r.failures[0].evidence["offset"] == 49
    assert r.failures[0].evidence["a_key"]["price"] == 3000
    # The baseline event plus the 49 offsets that compared clean, and no more.
    assert epochs_of(r, "MKT")[0].paired == 50
    assert r.verdict == frozen.V2_RESULT_FAIL
    assert r.as_dict() == frozen_evaluate(db).as_dict()


def test_a_mid_run_divergence_stops_that_market_and_not_the_next_one(tmp_path):
    """
    A per-market evaluator must not carry one market's abandoned book into the
    next. AAA diverges at offset 40; BBB behind it is clean and still pairs.
    """
    def fn(a):
        a.subscribe(["AAA", "BBB", "TAILSET"])
        for t in ("AAA", "BBB"):
            a.snapshot("A", t, {5000: 100, 3000: 50}, {6000: 200})
            a.snapshot("B", t, {5000: 100, 3000: 50}, {6000: 200})
        for k in range(40):
            for t in ("AAA", "BBB"):
                for s in ("A", "B"):
                    a.delta(s, t, "no", 6000, 1, BASE_TS + k)
        a.delta("A", "AAA", "yes", 3000, 7, BASE_TS + 100)
        a.delta("B", "AAA", "yes", 3000, 9, BASE_TS + 100)
        for k in range(200):
            for s in ("A", "B"):
                a.delta(s, "BBB", "no", 6000, 1, BASE_TS + 200 + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.market for f in r.failures] == ["AAA"]
    assert pairs_for(r, "BBB") == 240
    assert "BBB" in {e.market for e in r.epochs if e.qualifies}


# -- §6/§6.1: interrupted epochs and their tails -------------------------------------


def test_an_interrupted_epoch_excludes_its_one_sided_tail(tmp_path):
    """
    §6.1. The disconnect ends the epoch; A's seven unmatched events are the one
    permitted contiguous exclusion, and the epoch opened after the disconnect
    re-establishes no book and asserts nothing.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(150):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(150, 157):
            a.delta("A", "MKT", "no", 6000, 1, BASE_TS + k)
        a.disconnect("A")
        for k in range(200, 320):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    db = build(tmp_path, fn)
    r = evaluate_archive(db)
    assert [f.kind for f in r.failures] == []
    mine = epochs_of(r, "MKT")
    assert len(mine) == 2
    assert mine[0].interrupted is True and mine[1].interrupted is False
    assert mine[0].excluded_epoch_tail == 7
    assert mine[0].paired == 150
    assert mine[1].empty is True and mine[1].paired == 0
    assert r.excluded_epoch_tail == 7
    assert r.as_dict() == frozen_evaluate(db).as_dict()


def test_a_tail_at_the_bound_is_excluded_and_one_past_it_fails(tmp_path):
    """§6.1's bound is 1,000 events. 1,000 is excluded; 1,001 is a FAIL."""
    def make(n):
        def fn(a):
            a.subscribe(["MKT", "TAILSET"])
            a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
            a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
            for k in range(150):
                for s in ("A", "B"):
                    a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
            for k in range(n):
                a.delta("A", "MKT", "no", 6000, 1, BASE_TS + 200 + k)
            a.disconnect("A")
            a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
            a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
            for k in range(120):
                for s in ("A", "B"):
                    a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 3000 + k)
            tail_setter(a)
        return fn

    at = evaluate_archive(build(tmp_path, make(frozen.V2_EPOCH_TAIL_MAX_EVENTS),
                                "at.sqlite"))
    assert "EPOCH_TAIL_TOO_LARGE" not in [f.kind for f in at.failures]
    assert at.excluded_epoch_tail == frozen.V2_EPOCH_TAIL_MAX_EVENTS

    over = evaluate_archive(build(tmp_path, make(frozen.V2_EPOCH_TAIL_MAX_EVENTS + 1),
                                  "over.sqlite"))
    assert "EPOCH_TAIL_TOO_LARGE" in [f.kind for f in over.failures]
    assert over.verdict == frozen.V2_RESULT_FAIL


def test_a_boundary_found_in_one_markets_frames_segments_every_market(tmp_path):
    """
    §6. The gap is raised on MKT2's frames; MKT1 is evaluated first and never
    shares a frame with it, so this only works because the boundary instants are
    collected in the first pass and applied to every market in the second.
    """
    r = evaluate_archive(build(tmp_path, f_gap_in_one_market_segments_all))
    assert len(epochs_of(r, "MKT1")) == 2
    assert epochs_of(r, "MKT1")[0].interrupted is True
    assert epochs_of(r, "MKT1")[0].paired == 60
    # The epoch after the boundary has no fresh snapshot on either stream, so it
    # re-establishes nothing and contributes nothing -- §6, unchanged.
    assert epochs_of(r, "MKT1")[1].empty is True
    assert pairs_for(r, "MKT1") == 60 and pairs_for(r, "MKT2") == 60


def test_a_normally_completed_final_epoch_gets_no_tail_exclusion(tmp_path):
    """§6.1 does not apply to a normally completed epoch: a residual is a FAIL."""
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(150):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(150, 153):
            a.delta("A", "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == ["UNMATCHED_TAIL_OUTSIDE_GUARD_BAND"]
    assert r.excluded_epoch_tail == 0
    assert r.verdict == frozen.V2_RESULT_FAIL


# -- §5: the guard band, which is global and known only after the first pass ---------


def test_the_cutoff_comes_from_the_whole_archive_not_from_the_market(tmp_path):
    """
    The market that sets `archive_max_ts_ms` sorts last, so the earlier market's
    guard-band exclusions can only be right if the cutoff was fixed before any
    market was evaluated.
    """
    r = evaluate_archive(build(tmp_path, f_cutoff_set_by_another_market))
    assert r.archive_max_ts_ms == BASE_TS + 10_000
    assert r.guard_cutoff_ts_ms == BASE_TS + 8_000
    # AAA's two 9,000 ms events are inside the band; ZZZ's two 10,000 ms ones too.
    assert r.excluded_guard_band == 4
    assert pairs_for(r, "AAA") == 120


def test_a_market_with_no_snapshot_on_one_stream_contributes_nothing_at_all(tmp_path):
    """
    The frozen evaluator `continue`d before folding that market's guard-band
    exclusions in. That is not obviously right, but it is what the formal runs
    were scored with, so the streaming evaluator drops them too.
    """
    db = build(tmp_path, f_market_without_a_snapshot_on_b)
    r = evaluate_archive(db)
    assert epochs_of(r, "HALF") == []
    assert r.as_dict() == frozen_evaluate(db).as_dict()


# -- §8 criterion 5 and §12's aggregation across markets and epochs ------------------


def test_an_epoch_qualifies_at_exactly_the_threshold_and_not_below(tmp_path):
    def make(n):
        def fn(a):
            a.subscribe(["MKT", "TAILSET"])
            paired_run(a, "MKT", n=n)
            tail_setter(a)
        return fn
    below = evaluate_archive(build(tmp_path, make(
        frozen.V2_EPOCH_MIN_PAIRED_EVENTS - 1), "below.sqlite"))
    at = evaluate_archive(build(tmp_path, make(
        frozen.V2_EPOCH_MIN_PAIRED_EVENTS), "at.sqlite"))
    assert "MKT" not in {e.market for e in below.epochs if e.qualifies}
    assert "MKT" in {e.market for e in at.epochs if e.qualifies}
    assert epochs_of(at, "MKT")[0].paired == frozen.V2_EPOCH_MIN_PAIRED_EVENTS
    assert at.qualifying_events == sum(e.paired for e in at.epochs if e.qualifies)


def test_a_market_with_several_epochs_is_counted_once_in_qualifying_markets(tmp_path):
    """
    `qualifying_markets` is a SET over the whole run, and it is now built while
    markets are folded in one at a time. A market with more than one epoch must
    still land in it once, and `qualifying_events` must be the sum over
    qualifying epochs and nothing else.

    (Under §6 as frozen, only a market's FIRST epoch can carry a book: every
    later segment begins at the last snapshot instant, so the other stream has no
    snapshot inside it. This pins the counting rule, not a second qualifying
    epoch.)
    """
    def fn(a):
        a.subscribe(["MKT", "OTHER", "TAILSET"])
        for t in ("MKT", "OTHER"):
            a.snapshot("A", t, {5000: 100}, {6000: 200})
            a.snapshot("B", t, {5000: 100}, {6000: 200})
        for k in range(150):
            for t in ("MKT", "OTHER"):
                for s in ("A", "B"):
                    a.delta(s, t, "no", 6000, 1, BASE_TS + k)
        a.disconnect("A")
        for k in range(200, 260):
            for t in ("MKT", "OTHER"):
                for s in ("A", "B"):
                    a.delta(s, t, "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    db = build(tmp_path, fn)
    r = evaluate_archive(db)
    assert len(epochs_of(r, "MKT")) > 1
    assert [e.market for e in r.epochs if e.qualifies].count("MKT") == 1
    assert r.qualifying_markets == 3          # MKT, OTHER, TAILSET
    assert r.qualifying_events == sum(e.paired for e in r.epochs if e.qualifies)
    assert r.as_dict() == frozen_evaluate(db).as_dict()


def test_the_per_market_breach_aggregates_that_markets_epochs_only(tmp_path):
    """
    §12's per-market denominator is that market's own. The breaching market has
    two epochs and a clean market sits beside it; the reported classes must sum
    the breaching market's epochs and nothing else.
    """
    def fn(a):
        a.subscribe(["BREACH", "CLEAN", "TAILSET"])
        paired_run(a, "CLEAN", n=300)
        a.snapshot("A", "BREACH", {5000: 100}, {6000: 200})
        a.snapshot("B", "BREACH", {5000: 100}, {6000: 200})
        for k in range(60):
            for s in ("A", "B"):
                a.delta(s, "BREACH", "no", 6000, 1, BASE_TS + k)
        for k in range(30):
            a.delta("A", "BREACH", "no", 6000, 1, BASE_TS + 100 + k)
        a.disconnect("A")
        a.snapshot("A", "BREACH", {5000: 100}, {6000: 200})
        a.snapshot("B", "BREACH", {5000: 100}, {6000: 200})
        for k in range(60):
            for s in ("A", "B"):
                a.delta(s, "BREACH", "no", 6000, 1, BASE_TS + 300 + k)
        tail_setter(a)
    db = build(tmp_path, fn)
    r = evaluate_archive(db)
    assert [b.market for b in r.boundary_breaches] == ["BREACH"]
    b = r.boundary_breaches[0]
    mine = epochs_of(r, "BREACH")
    assert b.epochs == len(mine)
    assert b.classes["EXCLUDED_EPOCH_TAIL"] == sum(
        e.excluded_epoch_tail for e in mine)
    assert b.excluded == 2 * 0 + sum(
        e.excluded_pre_baseline + e.excluded_epoch_tail for e in mine)
    assert r.as_dict() == frozen_evaluate(db).as_dict()


def test_the_run_level_denominator_is_the_sum_over_every_market(tmp_path):
    """
    §12's run-level test is the unadjusted aggregate. Folding each market in as
    it is evaluated must reach the same total as summing at the end.
    """
    def fn(a):
        a.subscribe(["M1", "M2", "M3"])
        for t in ("M1", "M2", "M3"):
            a.snapshot("A", t, {5000: 100}, {6000: 200})
            a.snapshot("B", t, {5000: 100}, {6000: 200})
            for k in range(120):
                for s in ("A", "B"):
                    a.delta(s, t, "no", 6000, 1, BASE_TS + k)
            for k in range(3):
                for s in ("A", "B"):
                    a.delta(s, t, "no", 6000, 1, BASE_TS + 9000 + k)
    db = build(tmp_path, fn)
    r = evaluate_archive(db)
    assert r.run_excluded == (r.excluded_pre_baseline + r.excluded_guard_band
                              + r.excluded_epoch_tail)
    assert r.run_denominator == 2 * r.paired_events + r.run_excluded
    assert r.paired_events == sum(e.paired for e in r.epochs)
    assert r.as_dict() == frozen_evaluate(db).as_dict()


# -- Verdict precedence, unchanged ---------------------------------------------------


def test_a_fail_outranks_a_boundary_insufficiency(tmp_path):
    def fn(a):
        a.subscribe(["AAA_FAIL", "ZZZ_BREACH"])
        a.snapshot("A", "AAA_FAIL", {5000: 100}, {6000: 200})
        a.snapshot("B", "AAA_FAIL", {5000: 100, 4000: 77}, {6000: 200})
        for k in range(20):
            for s in ("A", "B"):
                a.delta(s, "AAA_FAIL", "no", 6000, 1, BASE_TS + k)
        a.snapshot("A", "ZZZ_BREACH", {5000: 100}, {6000: 200})
        a.snapshot("B", "ZZZ_BREACH", {5000: 100}, {6000: 200})
        for k in range(30):
            for s in ("A", "B"):
                a.delta(s, "ZZZ_BREACH", "no", 6000, 1, BASE_TS + k)
        for k in range(40):
            for s in ("A", "B"):
                a.delta(s, "ZZZ_BREACH", "no", 6000, 1, BASE_TS + 9000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.boundary_breaches, "the fixture must also breach the cap"
    assert r.verdict == frozen.V2_RESULT_FAIL


def test_a_boundary_insufficiency_outranks_an_ordinary_one(tmp_path):
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(30):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(40):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 9000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.failures == []
    assert r.unmet_floors, "the floors must also be unmet"
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY


# -- Batching, chunking and the spill are not observable ------------------------------


@pytest.mark.parametrize("batch,chunk", [(1, 1), (3, 2), (7, 5), (20_000, 500)])
def test_the_result_does_not_depend_on_the_index_batch_or_the_fetch_chunk(
        monkeypatch, tmp_path, batch, chunk):
    """
    The two knobs the refactor introduced are the spill's insert batch and the
    second pass's row-fetch chunk. Neither may reach the result -- including the
    degenerate chunk of one, which turns every fetch into its own statement.
    """
    db = build(tmp_path, f_disconnect_interrupts)
    reference = evaluate_archive(db).as_dict()
    monkeypatch.setattr(ev2, "_SPILL_INSERT_BATCH", batch)
    monkeypatch.setattr(ev2, "_ROW_FETCH_CHUNK", chunk)
    assert evaluate_archive(db).as_dict() == reference


def test_the_spill_file_is_removed_and_honours_its_directory(monkeypatch, tmp_path):
    """The index is temporary. It goes where it is told and it does not survive."""
    spill = tmp_path / "spill"
    spill.mkdir()
    monkeypatch.setenv("DEPTHREC_V2_SPILL_DIR", str(spill))
    db = build(tmp_path, f_two_markets_interleaved)
    seen: list[Path] = []
    real_finish = ev2._MarketIndex.finish

    def spy(self):
        seen.append(Path(self.path))
        return real_finish(self)

    monkeypatch.setattr(ev2._MarketIndex, "finish", spy)
    evaluate_archive(db)
    assert seen and seen[0].parent == spill
    assert not seen[0].exists()
    assert list(spill.iterdir()) == []


def test_the_spill_file_is_removed_when_evaluation_raises(monkeypatch, tmp_path):
    """A failure path that leaves the index behind would fill the VM's disk."""
    spill = tmp_path / "spill"
    spill.mkdir()
    monkeypatch.setenv("DEPTHREC_V2_SPILL_DIR", str(spill))
    db = build(tmp_path, f_two_markets_interleaved)

    def boom(*_a, **_k):
        raise RuntimeError("stop here")

    monkeypatch.setattr(ev2, "_market_events", boom)
    with pytest.raises(RuntimeError):
        evaluate_archive(db)
    assert list(spill.iterdir()) == []


def test_the_archive_is_opened_read_only(tmp_path):
    """A regression fixture that the evaluator can write to is not a fixture."""
    db = build(tmp_path, f_two_markets_interleaved)
    arch = ev2._Archive(db)
    try:
        with pytest.raises(Exception):
            arch.con.execute("DELETE FROM ws_raw")
    finally:
        arch.close()


def test_the_first_pass_keeps_no_per_event_state(tmp_path):
    """
    The memory claim in one assertion: after the whole archive has been read, the
    only things the scan is holding are counts, two subscription sets and the
    boundary instants. If an event list reappears here the refactor is undone.
    """
    db = build(tmp_path, f_two_markets_interleaved)
    arch = ev2._Archive(db)
    index = ev2._MarketIndex()
    try:
        scan = ev2._scan_archive(arch, index, None)
        assert scan.frames > 600
        assert vars(scan).keys() == {
            "run_id", "boundary_ts", "subscribed", "archive_max_ts_ms",
            "frames", "minutes", "archive_last_recv"}
        assert all(isinstance(t, float) for t in scan.boundary_ts)
        assert index.markets() == ["ALPHA", "TAILSET", "ZULU"]
    finally:
        index.close()
        arch.close()


# -- Evidence shape, which the JSON carries ------------------------------------------


def test_key_evidence_is_what_the_dataclass_dict_used_to_be():
    """
    Verification grew __slots__, so the failure evidence can no longer read
    `__dict__`. The replacement must produce the same mapping, in the same order,
    with the same values -- the JSON of a FAIL is part of the result.
    """
    key = Verification(1_700_000_000_123, "no", 6800, -250)
    assert key_evidence(key) == {"ts_ms": 1_700_000_000_123, "side": "no",
                                 "price": 6800, "delta": -250}
    assert list(key_evidence(key)) == ["ts_ms", "side", "price", "delta"]
    assert json.dumps(key_evidence(key)) == (
        '{"ts_ms": 1700000000123, "side": "no", "price": 6800, "delta": -250}')


def test_the_pooled_delta_body_equals_the_decoded_one():
    """
    Sharing string objects across a market's delta bodies is storage. The dict
    must still be equal to what json.loads produced, key order included.
    """
    body = {"market_ticker": "MKT", "side": "no", "price_dollars": "0.3200",
            "delta_fp": "1.00", "ts_ms": 1_700_000_000_000}
    pool: dict[str, str] = {}
    first = ev2._share_strings(dict(body), pool)
    second = ev2._share_strings(dict(body), pool)
    assert first == body and second == body
    assert list(first) == list(body)
    assert first["market_ticker"] is second["market_ticker"]
    assert first["ts_ms"] == body["ts_ms"] and isinstance(first["ts_ms"], int)


# -- The preserved 240 s engineering probe -------------------------------------------


@pytest.mark.skipif(not EVIDENCE.exists(), reason="preserved evidence not present")
def test_the_streaming_evaluator_matches_the_frozen_one_on_the_preserved_probe():
    """
    Synthetic fixtures are built to exercise a rule; the probe is 486,048 real
    frames over 3,472 real markets, one of which -- KXBTC15M-26AUG222030-30 --
    carries 221,305 of them on its own. That single market is what sets the
    streaming evaluator's peak, and it is the shape a BTC/ETH pilot would have.
    """
    new = evaluate_archive(EVIDENCE)
    old = frozen_evaluate(EVIDENCE)
    assert new.as_dict() == old.as_dict()
    assert new.paired_events == 236_489
    assert new.markets_with_any_pair == 2_682
    assert new.qualifying_markets == 259
    assert new.qualifying_events == 179_888
