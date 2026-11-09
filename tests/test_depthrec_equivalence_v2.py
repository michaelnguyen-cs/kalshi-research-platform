"""
Section 8-v2, rule by rule, against synthetic archives.

Each test pins one clause of research/THREAD_A_EQUIVALENCE_V2_2026-08-23.md
(frozen at 5a0b4ca) and names it. The fixtures are minimal ws_raw archives, not
captured traffic: a rule is easier to falsify when the archive contains only the
events the rule is about.

Prices and quantities are given in CANONICAL YES integers throughout and the
builder emits whichever wire form the stream would carry -- stream B's no-side
prices are written as 1 - p, which is the one documented difference between the
two subscriptions and the thing the test exists to check.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.equivalence_v2 import (  # noqa: E402
    boundary_allowance,
    canonical_price,
    evaluate_archive,
    market_breaches_boundary_cap,
)

RUN = "v2-test"
BASE_TS = 1_700_000_000_000        # exchange ms


class Archive:
    """A minimal ws_raw archive, written the way the recorder writes one."""

    DDL = """
    CREATE TABLE ws_raw (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        stream_label TEXT NOT NULL, session_epoch INTEGER NOT NULL,
        direction TEXT NOT NULL, recv_ts REAL NOT NULL, raw TEXT,
        codec INTEGER NOT NULL DEFAULT 0, zdict_id INTEGER, payload BLOB,
        raw_sha256 BLOB, raw_len INTEGER);
    CREATE TABLE raw_dictionary (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created_ts REAL,
        first_frame_id INTEGER, frames_sampled INTEGER, zdict BLOB, sha256 TEXT);
    CREATE TABLE health_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stream_label TEXT,
        session_epoch INTEGER, ts REAL, kind TEXT, reason TEXT, detail TEXT,
        git_commit TEXT);
    """

    def __init__(self, path: Path) -> None:
        self.con = sqlite3.connect(path)
        self.con.executescript(self.DDL)
        self.seq = {"A": 0, "B": 0}
        self.recv = 1000.0

    # -- frame plumbing ----------------------------------------------------

    def _write(self, stream: str, direction: str, obj: dict) -> None:
        text = json.dumps(obj)
        data = text.encode()
        self.recv += 0.001
        self.con.execute(
            "INSERT INTO ws_raw(run_id, stream_label, session_epoch, direction,"
            " recv_ts, raw, codec, zdict_id, payload, raw_sha256, raw_len)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (RUN, stream, 1, direction, self.recv, None, 0, None, data,
             hashlib.sha256(data).digest(), len(data)),
        )

    def _next_seq(self, stream: str) -> int:
        self.seq[stream] += 1
        return self.seq[stream]

    # -- events ------------------------------------------------------------

    def subscribe(self, tickers: list[str]) -> None:
        for s in ("A", "B"):
            self._write(s, "out", {
                "id": 1, "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"],
                           "market_tickers": tickers,
                           "use_yes_price": s == "A"}})

    def snapshot(self, stream: str, ticker: str, bids: dict[int, int],
                 asks: dict[int, int]) -> None:
        """bids/asks keyed by CANONICAL yes-leg price."""
        yes = [[f"{p / 10000:.4f}", f"{q / 100:.2f}"] for p, q in sorted(bids.items())]
        no = []
        for p, q in sorted(asks.items()):
            wire = p if stream == "A" else 10000 - p
            no.append([f"{wire / 10000:.4f}", f"{q / 100:.2f}"])
        self._write(stream, "in", {
            "type": "orderbook_snapshot", "sid": 1, "seq": self._next_seq(stream),
            "msg": {"market_ticker": ticker, "yes_dollars_fp": yes,
                    "no_dollars_fp": no}})

    def delta(self, stream: str, ticker: str, side: str, price: int,
              delta: int, ts_ms: int) -> None:
        """price is CANONICAL yes-leg; delta is hundredths of a contract."""
        wire = price if (side == "yes" or stream == "A") else 10000 - price
        self._write(stream, "in", {
            "type": "orderbook_delta", "sid": 1, "seq": self._next_seq(stream),
            "msg": {"market_ticker": ticker, "side": side,
                    "price_dollars": f"{wire / 10000:.4f}",
                    "delta_fp": f"{delta / 100:.2f}", "ts_ms": ts_ms}})

    def sequence_gap(self, stream: str, skip: int = 5) -> None:
        self.seq[stream] += skip

    def disconnect(self, stream: str) -> None:
        self.recv += 0.001
        self.con.execute(
            "INSERT INTO health_event(run_id, stream_label, session_epoch, ts,"
            " kind, reason, detail, git_commit) VALUES (?,?,?,?,?,?,?,?)",
            (RUN, stream, 1, self.recv, "disconnect", "", "", "x"))

    def close(self) -> None:
        self.con.commit()
        self.con.close()


def build(tmp_path, fn, name="v2.sqlite") -> Path:
    p = tmp_path / name
    a = Archive(p)
    fn(a)
    a.close()
    return p


def paired_run(a: Archive, ticker="MKT", n=120, *, start_ts=BASE_TS):
    """A clean market: identical snapshots, n identical deltas on both streams."""
    a.snapshot("A", ticker, {5000: 100}, {6000: 200})
    a.snapshot("B", ticker, {5000: 100}, {6000: 200})
    for k in range(n):
        for s in ("A", "B"):
            a.delta(s, ticker, "no", 6000, 1, start_ts + k)


def tail_setter(a: Archive, ticker="TAILSET"):
    """
    Pushes archive_max_ts_ms far enough past the payload that section 5's exact
    2,000 ms cutoff keeps the payload. Its own last two events land inside the
    guard band and are excluded, which is what a real archive tail looks like.

    It carries 100 clean paired events first, so its own guard-band exclusion is
    2 / (2*100 + 2) = 0.99% and stays under section 5's 1% cap. A market whose
    ONLY events fall inside the guard band would be 2/2 = 100% and would fail
    the run on its own -- see DOCUMENT FINDINGS in the report.
    """
    a.snapshot("A", ticker, {5000: 100}, {6000: 200})
    a.snapshot("B", ticker, {5000: 100}, {6000: 200})
    for k in range(100):
        for s in ("A", "B"):
            a.delta(s, ticker, "no", 6000, 1, BASE_TS + k)
    for s in ("A", "B"):
        a.delta(s, ticker, "yes", 5000, 1, BASE_TS + 9000)


def pairs_for(result, market: str) -> int:
    """Paired events for one market, summed over its epochs."""
    return sum(e.paired for e in result.epochs if e.market == market)


def qualifying_set(result) -> set:
    return {e.market for e in result.epochs if e.qualifies}


# -- Frozen parameters ------------------------------------------------------------


def test_every_v2_parameter_matches_the_document():
    """The values in §4.1, §5, §6, §8 and §8.0, transcribed."""
    assert frozen.V2_GUARD_BAND_MS == 2000
    assert frozen.V2_HEAD_SEARCH_POSITIONS == 1000
    assert frozen.V2_EPOCH_TAIL_MAX_EVENTS == 1000
    assert frozen.V2_EPOCH_MIN_PAIRED_EVENTS == 100
    assert frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION == 0.01
    assert frozen.V2_MIN_QUALIFYING_EVENTS == 600_000
    assert frozen.V2_MIN_QUALIFYING_MARKETS == 60
    assert frozen.V2_RUN_MINUTES == 60


def test_v1_parameters_are_untouched():
    """v2 retires §8-v1; it does not edit it."""
    assert frozen.EQUIV_SKEW_TOLERANCE_MS == 500
    assert frozen.EQUIV_GRID_HZ == 1.0
    assert frozen.EQUIV_PASS_AGREEMENT == 0.999
    assert frozen.EQUIV_MIN_COMPARISONS == 10_000


# -- §2/§7: the canonical transform and full-ladder comparison ---------------------


def test_the_no_side_transform_is_the_documented_one():
    assert canonical_price("0.3200", "no", use_yes_price=False) == 6800
    assert canonical_price("0.6800", "no", use_yes_price=True) == 6800
    assert canonical_price("0.6800", "yes", use_yes_price=False) == 6800


def test_a_clean_market_produces_no_failures(tmp_path):
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        paired_run(a, "MKT", n=120)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == []
    assert pairs_for(r, "MKT") == 120
    assert "MKT" in qualifying_set(r)


def test_a_deep_level_divergence_is_caught_not_only_top_of_book(tmp_path):
    """
    §7. The differing level is at 3000, far below the best bid, so a
    top-of-book comparison would pass it.
    """
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100, 3000: 50}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100, 3000: 50}, {6000: 200})
        for s in ("A", "B"):
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS)
        # B's second delta moves a deep bid by a different amount.
        a.delta("A", "MKT", "yes", 3000, 7, BASE_TS + 1)
        a.delta("B", "MKT", "yes", 3000, 9, BASE_TS + 1)
        for s in ("A", "B"):
            a.delta(s, "MKT", "yes", 5000, 1, BASE_TS + 5000)
    r = evaluate_archive(build(tmp_path, fn))
    kinds = [f.kind for f in r.failures]
    assert "ALIGNMENT_VERIFICATION" in kinds or "BOOK_DIVERGENCE" in kinds
    assert r.verdict == frozen.V2_RESULT_FAIL


# -- §4/§4.1: the baseline ---------------------------------------------------------


def test_the_head_block_is_excluded_and_the_baseline_event_is_paired(tmp_path):
    """
    §4: events baked into one stream's snapshot arrive as deltas on the other.
    They are EXCLUDED_PRE_BASELINE. §4 also says the baseline is "the first
    assertion, not an exclusion", so the baseline event itself is paired.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        # B's snapshot is one event older than A's: A already has the +5 at 6000.
        a.snapshot("A", "MKT", {5000: 100}, {6000: 205})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        a.delta("B", "MKT", "no", 6000, 5, BASE_TS - 1)      # B-only head event
        # 200 pairs, so the single excluded record is 1/401 = 0.25% and stays
        # well under section 5's cap -- this test is about the baseline, not it.
        for k in range(200):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == []
    assert r.excluded_pre_baseline == 1
    assert pairs_for(r, "MKT") == 200     # baseline event included


def test_no_baseline_inside_the_head_bound_fails_and_is_not_widened(tmp_path):
    """
    §4.1. The bound is 1,000 positions on either stream. Beyond it the market is
    reported and the run FAILS pending investigation -- it is not excluded and
    the search is not widened.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        # The two books never agree: B's snapshot carries a level A never has.
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100, 4000: 77}, {6000: 200})
        for k in range(20):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == ["NO_BASELINE"]
    assert r.verdict == frozen.V2_RESULT_FAIL
    assert pairs_for(r, "MKT") == 0


def test_the_baseline_minimises_the_total_excluded_head(tmp_path):
    """
    §4.1's objective: minimise i + j, not i alone. Both streams carry one
    private head event, so the baseline sits at (1, 1) and excludes exactly two.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        a.delta("A", "MKT", "yes", 5000, 3, BASE_TS - 2)     # A-only
        a.delta("B", "MKT", "yes", 5000, 3, BASE_TS - 1)     # B-only, same effect
        for k in range(200):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == []
    assert r.excluded_pre_baseline == 2
    assert pairs_for(r, "MKT") == 200


# -- §5: guard band and exclusion cap ----------------------------------------------


def test_the_guard_band_cutoff_is_exactly_2000ms_and_global(tmp_path):
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for s in ("A", "B"):
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS)             # kept
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 3000)      # kept, == cutoff
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 3001)      # excluded
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000)      # sets the max
    r = evaluate_archive(build(tmp_path, fn))
    assert r.archive_max_ts_ms == BASE_TS + 5000
    assert r.guard_cutoff_ts_ms == BASE_TS + 3000
    assert r.excluded_guard_band == 4          # two events, two streams


def test_the_exclusion_cap_is_computed_in_stream_delta_units(tmp_path):
    """
    §5. One included paired event is TWO stream-delta records. A market with
    100 pairs and one excluded pair is 2 / (200 + 2) = 0.99% at run level, just
    under the cap; counting pairs against pairs would give 1/101 and a different
    answer, and counting the exclusion as one record would give 1/201.
    """
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(100):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for s in ("A", "B"):                      # excluded by the guard band
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.paired_events == 100
    assert r.excluded_guard_band == 2
    assert [f.kind for f in r.failures] == []     # 2/202 = 0.990% <= 1%


def test_a_run_level_breach_is_insufficiency_not_failure(tmp_path):
    """
    §12 clause 2 and the precedence table. Two more excluded pairs take the run
    to 4/204 = 1.96% unadjusted, over the run-level cap, while the per-market
    test passes it (E=4 <= 20). Criteria 1-3 are clean, so the outcome is
    INSUFFICIENT_BOUNDARY_EVIDENCE and NOT a failure.
    """
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(100):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for s in ("A", "B"):
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 4999)
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.failures == []
    assert r.boundary_breaches == []          # 4 <= max(20, 1% of 204)
    assert r.run_cap_breached is True
    assert r.run_excluded == 4 and r.run_denominator == 204
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY


def test_the_cap_is_aggregate_across_the_three_exclusion_classes(tmp_path):
    """
    §5: "three separate 0.9% exclusions must not quietly total 2.7%". Two
    classes are counted into one E here -- 11 pre-baseline records and 12
    guard-band records -- and 23 > max(20, 1% of D) only because they are added
    together. Either alone would be under the allowance.
    """
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 211})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(11):                                    # pre-baseline: 11
            a.delta("B", "MKT", "no", 6000, 1, BASE_TS - 20 + k)
        for k in range(200):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(6):                                     # guard band: 12
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.failures == []
    assert len(r.boundary_breaches) == 1
    b = r.boundary_breaches[0]
    assert b.classes["EXCLUDED_PRE_BASELINE"] == 11
    assert b.classes["EXCLUDED_GUARD_BAND"] == 12
    assert b.excluded == 23                    # > 20, only in aggregate
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY


# -- §6/§6.1: epochs ---------------------------------------------------------------


def test_an_epoch_below_100_paired_events_does_not_qualify(tmp_path):
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        paired_run(a, "MKT", n=99)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert pairs_for(r, "MKT") == 99
    assert "MKT" not in qualifying_set(r)


def test_an_epoch_at_exactly_100_paired_events_qualifies(tmp_path):
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        paired_run(a, "MKT", n=100)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert pairs_for(r, "MKT") == 100
    assert "MKT" in qualifying_set(r)


def test_an_interrupted_epoch_tail_is_excluded_contiguously(tmp_path):
    """
    §6.1. A disconnect ends the epoch; the leading stream's unmatched suffix is
    the one permitted contiguous exclusion.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(200):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        a.delta("A", "MKT", "no", 6000, 1, BASE_TS + 300)  # A leads by two
        a.delta("A", "MKT", "no", 6000, 1, BASE_TS + 301)
        a.disconnect("A")
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == []
    assert r.excluded_epoch_tail == 2
    assert pairs_for(r, "MKT") == 200


def test_a_mismatch_before_exhaustion_fails_and_is_not_re_aligned(tmp_path):
    """
    §6.1. There IS a later duplicate tuple that would "fix" the alignment by
    skipping one event. The positional-prefix rule forbids that: any mismatch
    before either stream is exhausted is a FAIL.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 500}, {6000: 500})
        a.snapshot("B", "MKT", {5000: 500}, {6000: 500})
        for s in ("A", "B"):
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS)
        a.delta("A", "MKT", "no", 6000, 2, BASE_TS + 1)    # A has an extra event
        for k in range(2, 8):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == ["ALIGNMENT_VERIFICATION"]
    assert r.verdict == frozen.V2_RESULT_FAIL


def test_a_normally_completed_epoch_gets_no_tail_exclusion(tmp_path):
    """
    §6.1 explicitly does not apply to a normally completed final epoch, whose
    boundary is §5's exact cutoff. A residual one-sided block there means the
    delivery lag exceeded the 2,000 ms the guard band was sized for.
    """
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(10):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        a.delta("A", "MKT", "no", 6000, 1, BASE_TS + 20)   # inside the kept range
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == ["UNMATCHED_TAIL_OUTSIDE_GUARD_BAND"]


def test_a_sequence_gap_ends_the_epoch(tmp_path):
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(10):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        a.sequence_gap("A")
        for k in range(10, 20):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    # More than one epoch for MKT, and the post-gap one has no fresh snapshot,
    # so it re-establishes nothing and contributes nothing.
    mkt_epochs = [e for e in r.epochs if e.market == "MKT"]
    assert len(mkt_epochs) >= 2
    assert pairs_for(r, "MKT") == 10


# -- §8: verdicts ------------------------------------------------------------------


def test_a_clean_run_below_the_floors_is_insufficient_not_pass(tmp_path):
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        paired_run(a, "MKT", n=120)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert not r.failures
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT


def test_a_divergence_fails_even_when_the_floors_are_met(monkeypatch, tmp_path):
    """§8: "any single divergence is a FAIL", and a FAIL is never converted."""
    monkeypatch.setattr(frozen, "V2_MIN_QUALIFYING_EVENTS", 1)
    monkeypatch.setattr(frozen, "V2_MIN_QUALIFYING_MARKETS", 1)

    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(150):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        a.delta("A", "MKT", "yes", 5000, 4, BASE_TS + 200)
        a.delta("B", "MKT", "yes", 5000, 6, BASE_TS + 200)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.verdict == frozen.V2_RESULT_FAIL


def test_meeting_both_floors_with_no_failure_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(frozen, "V2_MIN_QUALIFYING_EVENTS", 100)
    monkeypatch.setattr(frozen, "V2_MIN_QUALIFYING_MARKETS", 1)

    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        paired_run(a, "MKT", n=120)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.verdict == frozen.V2_RESULT_PASS


def test_a_market_set_mismatch_fails_criterion_6(tmp_path):
    def fn(a):
        a._write("A", "out", {"id": 1, "cmd": "subscribe", "params": {
            "market_tickers": ["MKT", "EXTRA"], "use_yes_price": True}})
        a._write("B", "out", {"id": 1, "cmd": "subscribe", "params": {
            "market_tickers": ["MKT"], "use_yes_price": False}})
        paired_run(a, "MKT", n=120)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert "MARKET_SET_MISMATCH" in [f.kind for f in r.failures]
    assert r.same_market_set is False


# -- Cross-check against the preserved calibration probe ---------------------------

EVIDENCE = Path("<EVIDENCE_DIR>/equivalence_probe_2026-08-23/verify/"
                "equivprobe.sqlite")


@pytest.mark.skipif(not EVIDENCE.exists(), reason="preserved evidence not present")
def test_the_module_reproduces_the_documents_own_calibration_table():
    """
    §8.1's table was produced by research/.../recon16_v2crit5.py, which shares
    no code with this module and used only tuple verification. This module
    reaches the same four counts through the full §4.1 baseline search and §7
    full-ladder comparison. Agreement across two implementations is the point.
    """
    r = evaluate_archive(EVIDENCE)
    assert r.paired_events == 236_489
    assert r.markets_with_any_pair == 2_682
    assert r.qualifying_markets == 259
    assert r.qualifying_events == 179_888
    assert r.excluded_pre_baseline == 44
    assert r.excluded_guard_band == 6_080
    # No divergence, no misalignment, no baseline failure anywhere in the probe.
    # Under the section 12 amendment the boundary exclusions are no longer a
    # failure at all, so this is now an exact assertion rather than a subset one.
    assert r.failures == []
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY


# -- §12: boundary-evidence sufficiency --------------------------------------------

# The amendment's own worked-cases table, verbatim.
#   p, E, D, expected breach
WORKED_CASES = [
    (20, 2, 42, False),          # 2 <= 20
    (90, 2, 182, False),         # 2 <= 20
    (99, 2, 200, False),         # 2 <= 20
    (30, 6, 66, False),          # 6 <= 20, one reconnect
    (50, 100, 200, True),        # 100 > 20
    (10, 200, 220, True),        # 200 > 20
    (0, 199, 199, True),         # entirely excluded still fails
    (5_000, 150, 10_150, True),  # 150 > 101.5, proportional 1% still applies
]


@pytest.mark.parametrize("p,e,d,breach", WORKED_CASES)
def test_the_amendments_worked_cases(p, e, d, breach):
    """§12's table, row by row."""
    assert 2 * p + e == d, "the table's own D must be 2p + E"
    assert market_breaches_boundary_cap(p, e) is breach


@pytest.mark.parametrize("p,e,d,breach", WORKED_CASES)
def test_the_two_statements_of_the_rule_agree(p, e, d, breach):
    """
    §12 gives the rule twice -- `E / max(D, 2000) > 1%` and "no market may have
    E > max(20, 0.01 x D)" -- and calls them equivalent. They must not drift.
    """
    assert (e > boundary_allowance(d)) is breach


def test_the_allowance_is_a_denominator_floor_not_an_exemption():
    """
    §12: "a small market does not get unlimited boundary-exclusion immunity",
    and a large one is still held to the proportional 1%.
    """
    assert boundary_allowance(0) == 20
    assert boundary_allowance(2_000) == 20
    assert boundary_allowance(10_150) == pytest.approx(101.5)
    assert market_breaches_boundary_cap(0, 21) is True         # tiny, still fails
    assert market_breaches_boundary_cap(0, 20) is False
    # D includes E: with p=100,000 the breach point is E > 0.01*(200,000+E),
    # i.e. E > 2,020.2 -- not E > 2,000.
    assert market_breaches_boundary_cap(100_000, 2_021) is True    # large, 1%
    assert market_breaches_boundary_cap(100_000, 2_020) is False


def test_a_low_volume_market_with_one_boundary_loss_no_longer_breaches(tmp_path):
    """
    The pathology §12 was written for: p < 99 with E = 2 used to breach at
    2/(2p+2) > 1% regardless of decoder behaviour. Now 2 <= 20.
    """
    def fn(a):
        a.subscribe(["SMALL", "TAILSET"])
        a.snapshot("A", "SMALL", {5000: 100}, {6000: 200})
        a.snapshot("B", "SMALL", {5000: 100}, {6000: 200})
        for k in range(20):                     # p = 20, well under 99
            for s in ("A", "B"):
                a.delta(s, "SMALL", "no", 6000, 1, BASE_TS + k)
        for s in ("A", "B"):                    # E = 2
            a.delta(s, "SMALL", "no", 6000, 1, BASE_TS + 9000)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.failures == []
    assert r.boundary_breaches == []
    assert pairs_for(r, "SMALL") == 20


def test_the_run_level_cap_applies_no_denominator_floor(tmp_path):
    """
    §12 clause 2: the run-level test is UNADJUSTED. A whole run small enough
    that every market passes the floored per-market test can still breach in
    aggregate.
    """
    def fn(a):
        a.subscribe(["M1", "M2"])
        for tk in ("M1", "M2"):
            a.snapshot("A", tk, {5000: 100}, {6000: 200})
            a.snapshot("B", tk, {5000: 100}, {6000: 200})
            for k in range(50):
                for s in ("A", "B"):
                    a.delta(s, tk, "no", 6000, 1, BASE_TS + k)
            for s in ("A", "B"):
                a.delta(s, tk, "no", 6000, 1, BASE_TS + 5000)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.boundary_breaches == []            # 2 <= 20 for each market
    assert r.run_excluded == 4
    assert r.run_denominator == 204             # 2*100 + 4
    assert r.run_cap_breached is True           # 4/204 = 1.96%, unadjusted
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY


# -- §12: verdict precedence, every branch -----------------------------------------


def test_precedence_pass(monkeypatch, tmp_path):
    """No FAIL, no cap breach, floors met -> PASS."""
    monkeypatch.setattr(frozen, "V2_MIN_QUALIFYING_EVENTS", 100)
    monkeypatch.setattr(frozen, "V2_MIN_QUALIFYING_MARKETS", 1)

    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        paired_run(a, "MKT", n=300)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert (r.failures, r.boundary_breaches, r.run_cap_breached) == ([], [], False)
    assert r.verdict == frozen.V2_RESULT_PASS


def test_precedence_ordinary_insufficient(tmp_path):
    """No FAIL, no cap breach, floors unmet -> INSUFFICIENT."""
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        paired_run(a, "MKT", n=300)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert (r.failures, r.boundary_breaches, r.run_cap_breached) == ([], [], False)
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT
    assert r.unmet_floors


def test_precedence_boundary_insufficiency_outranks_criterion_5(tmp_path):
    """
    §12: with both insufficiencies present, INSUFFICIENT_BOUNDARY_EVIDENCE is
    the primary verdict AND the unmet criterion-5 floors are also reported.
    """
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(50):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(4):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.failures == []
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY
    assert r.unmet_floors, "the criterion 5 floors must still be reported"
    assert any("qualifying events" in u for u in r.unmet_floors)


def test_precedence_fail_outranks_a_cap_breach(tmp_path):
    """
    §12 clause 1. A decoder divergence alongside a breach is FAIL. "A FAIL is
    never converted to INSUFFICIENT."
    """
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 500}, {6000: 500})
        a.snapshot("B", "MKT", {5000: 500}, {6000: 500})
        for s in ("A", "B"):
            a.delta(s, "MKT", "no", 6000, 1, BASE_TS)
        a.delta("A", "MKT", "yes", 5000, 4, BASE_TS + 1)     # divergence
        a.delta("B", "MKT", "yes", 5000, 6, BASE_TS + 1)
        for k in range(30):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.failures, "a decoder divergence must be recorded"
    assert r.verdict == frozen.V2_RESULT_FAIL


def test_a_criterion_6_failure_with_a_cap_breach_is_fail_not_insufficiency(tmp_path):
    """
    The case §12's precedence section was written for: "a criterion-6 failure
    occurring alongside a cap breach could be reported as insufficiency and hide
    the real failure". It must be FAIL.
    """
    def fn(a):
        a._write("A", "out", {"id": 1, "cmd": "subscribe", "params": {
            "market_tickers": ["MKT", "EXTRA"], "use_yes_price": True}})
        a._write("B", "out", {"id": 1, "cmd": "subscribe", "params": {
            "market_tickers": ["MKT"], "use_yes_price": False}})
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(50):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(4):                       # forces a run-level breach
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.run_cap_breached is True, "the fixture must also breach a cap"
    assert [f.kind for f in r.failures] == ["MARKET_SET_MISMATCH"]
    assert r.verdict == frozen.V2_RESULT_FAIL


def test_a_head_bound_failure_outranks_a_cap_breach(tmp_path):
    """§4.1's head bound is a FAIL condition under §12 clause 1."""
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100, 4000: 77}, {6000: 200})
        for k in range(50):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(4):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == ["NO_BASELINE"]
    assert r.verdict == frozen.V2_RESULT_FAIL


def test_an_unmatched_tail_outside_the_guard_band_outranks_a_cap_breach(tmp_path):
    """A differing block outside the permitted regions is a FAIL condition."""
    def fn(a):
        a.subscribe(["MKT", "TAILSET"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(30):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        a.delta("A", "MKT", "no", 6000, 1, BASE_TS + 40)
        tail_setter(a)
    r = evaluate_archive(build(tmp_path, fn))
    assert [f.kind for f in r.failures] == ["UNMATCHED_TAIL_OUTSIDE_GUARD_BAND"]
    assert r.verdict == frozen.V2_RESULT_FAIL


def test_a_boundary_insufficiency_reports_what_section_12_asks_for(tmp_path):
    """
    §12: "Report the affected markets, boundary classes, reconnect counts and
    exclusion fractions."
    """
    def fn(a):
        a.subscribe(["MKT"])
        a.snapshot("A", "MKT", {5000: 100}, {6000: 200})
        a.snapshot("B", "MKT", {5000: 100}, {6000: 200})
        for k in range(50):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + k)
        for k in range(15):
            for s in ("A", "B"):
                a.delta(s, "MKT", "no", 6000, 1, BASE_TS + 5000 + k)
    r = evaluate_archive(build(tmp_path, fn))
    assert r.verdict == frozen.V2_RESULT_INSUFFICIENT_BOUNDARY
    b = r.boundary_breaches[0]
    assert b.market == "MKT"                                   # affected market
    assert set(b.classes) == {"EXCLUDED_PRE_BASELINE",         # boundary classes
                              "EXCLUDED_GUARD_BAND",
                              "EXCLUDED_EPOCH_TAIL"}
    assert b.fraction > frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION   # fraction
    assert isinstance(r.reconnects, dict)                      # reconnect counts
    d = r.as_dict()["boundary_evidence"]
    assert d["run_cap_breached"] is True
    assert d["per_market_breaches"][0]["market"] == "MKT"
    assert d["min_denominator"] == 2000


def test_the_threshold_and_the_other_v2_parameters_are_unchanged():
    """
    §12: "The 1% threshold itself is unchanged, and no other §8-v2 parameter
    changes".
    """
    assert frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION == 0.01
    assert frozen.V2_GUARD_BAND_MS == 2000
    assert frozen.V2_HEAD_SEARCH_POSITIONS == 1000
    assert frozen.V2_EPOCH_TAIL_MAX_EVENTS == 1000
    assert frozen.V2_EPOCH_MIN_PAIRED_EVENTS == 100
    assert frozen.V2_MIN_QUALIFYING_EVENTS == 600_000
    assert frozen.V2_MIN_QUALIFYING_MARKETS == 60
    assert frozen.V2_RUN_MINUTES == 60
    assert frozen.V2_BOUNDARY_MIN_DENOMINATOR == 2000
