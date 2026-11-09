"""
scripts/depthrec_market_sizes.py: the per-market record counts it reports are
the evaluator's own attribution.

The script exists to answer a memory question, so the only thing it has to get
right is the count: for every market, the number of decoded records the
streaming evaluator would associate with that market. It gets that by running
the evaluator's own first pass with a counting index in place of the on-disk
one, so these tests check the substitution rather than re-deriving the counts --
a test that counted tickers itself would be the second parser the script was
written to avoid.

Three things are pinned: the counts equal what the production `_MarketIndex`
recorded, they equal the number of events the production second pass actually
builds for that market, and the A/B split sums back to the total on an archive
where the two streams deliberately disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import depthrec_market_sizes as ms  # noqa: E402
from depthrec import equivalence_v2 as ev2  # noqa: E402
from depthrec.equivalence_v2 import evaluate_archive  # noqa: E402
from tests.test_depthrec_equivalence_v2 import (  # noqa: E402
    BASE_TS,
    Archive,
    build,
    paired_run,
    tail_setter,
)


def f_lopsided(a: Archive) -> None:
    """
    Three markets of very different sizes, one stream deliberately louder than
    the other, and frames that carry no market record at all.

    BIG gets 400 deltas per stream, MID 150, SMALL 8, so the ranking is not the
    ticker order, the frame order or the subscription order. BIG also gets 25
    extra stream-A deltas, so an A/B split that just halved the total would be
    caught. The `error` frame and the unknown message type are frames the
    evaluator reads but attributes to no market.
    """
    a.subscribe(["MID", "BIG", "SMALL"])
    paired_run(a, "BIG", n=400)
    paired_run(a, "MID", n=150)
    paired_run(a, "SMALL", n=8)
    for k in range(25):
        a.delta("A", "BIG", "no", 6000, 1, BASE_TS + 500 + k)
    a._write("A", "in", {"type": "error", "sid": 1, "seq": 0, "msg": {}})
    a._write("B", "in", {"type": "ticker_v2", "sid": 1, "seq": 0,
                         "msg": {"market_ticker": "BIG"}})
    tail_setter(a)


def scan_with_production_index(db: Path):
    """The same archive through the real `_Archive` and the real `_MarketIndex`."""
    arch = ev2._Archive(db)
    index = ev2._MarketIndex()
    try:
        scan = ev2._scan_archive(arch, index, None)
        counts = {m: sum(len(chunk) for chunk in index.rid_chunks(m))
                  for m in index.markets()}
        events = {}
        for m in index.markets():
            snaps, deltas = ev2._market_events(arch, index, m)
            events[m] = (len(snaps["A"]) + len(snaps["B"])
                         + len(deltas["A"]) + len(deltas["B"]))
        return scan, counts, events
    finally:
        index.close()
        arch.close()


@pytest.fixture
def db(tmp_path):
    return build(tmp_path, f_lopsided)


# -- Attribution ---------------------------------------------------------------------


def test_the_counts_are_the_production_market_indexs_counts(db):
    """
    The substitution claim. `_CountingIndex` replaces `_MarketIndex` inside the
    evaluator's first pass, so market for market it must have been handed
    exactly what the real index was handed -- same market set, same number of
    records each.
    """
    _scan, counts, _events = scan_with_production_index(db)
    sizes = ms.scan_market_sizes(db)

    assert dict(sizes.total) == counts
    assert sizes.markets == len(counts)
    assert sizes.records == sum(counts.values())
    assert sizes.max_count == max(counts.values())


def test_a_markets_count_is_the_events_the_second_pass_builds_for_it(db):
    """
    The count is the memory-relevant quantity, not merely an index size: it is
    len(snaps[A]) + len(snaps[B]) + len(deltas[A]) + len(deltas[B]) for that
    market, which is the list the evaluator holds while the market is live.
    """
    _scan, _counts, events = scan_with_production_index(db)
    sizes = ms.scan_market_sizes(db)

    assert dict(sizes.total) == events


def test_the_frame_count_is_the_evaluators_own_frames(db):
    """
    The reported raw frame count is the number the memory measurements were
    quoted against -- `V2Result.frames` -- and outbound subscription frames are
    counted apart from it and attributed to no market.
    """
    sizes = ms.scan_market_sizes(db)
    result = evaluate_archive(db)

    assert sizes.inbound_frames == result.frames
    assert sizes.outbound_frames == 2                    # one subscribe per stream
    assert sizes.rows_scanned == sizes.inbound_frames + sizes.outbound_frames
    assert sizes.run_id == result.run_id
    # The error frame and the unknown type were read and attributed to nothing.
    assert sizes.records < sizes.inbound_frames


def test_the_stream_split_sums_to_the_total_on_a_lopsided_archive(db):
    """
    A record is attributed to the stream whose frame carried it. BIG's 25 extra
    stream-A deltas make a split that assumed symmetry visibly wrong.
    """
    sizes = ms.scan_market_sizes(db)

    for market, total in sizes.total.items():
        per = sizes.by_stream[market]
        assert sum(per.values()) == total
        assert set(per) <= {"A", "B"}

    big = sizes.by_stream["BIG"]
    assert big["A"] == 1 + 400 + 25                      # snapshot + deltas + extra
    assert big["B"] == 1 + 400
    assert sizes.by_stream["SMALL"] == {"A": 9, "B": 9}
    assert sizes.labels() == ["A", "B"]


# -- Reporting -------------------------------------------------------------------


def test_the_ranking_is_by_count_then_ticker_and_truncates(db):
    """
    Ranking is descending by count with the ticker as a reproducible tie-break,
    and `--top` truncates the list without changing the totals it is drawn from.
    """
    sizes = ms.scan_market_sizes(db)
    ranked = sizes.ranked(20)

    assert [m for m, _n, _p in ranked][:3] == ["BIG", "MID", "TAILSET"]
    assert [n for _m, n, _p in ranked] == sorted(
        (n for _m, n, _p in ranked), reverse=True)
    assert ranked[0][1] == sizes.max_count
    assert len(sizes.ranked(2)) == 2
    assert sizes.ranked(2) == ranked[:2]

    sizes.total["TIE_B"] = sizes.total["TIE_A"] = sizes.total["SMALL"]
    tied = [m for m, _n, _p in sizes.ranked(20)]
    assert tied.index("TIE_A") < tied.index("TIE_B")


def test_the_percentages_are_shares_of_the_decoded_records(db):
    sizes = ms.scan_market_sizes(db)

    assert sizes.share(sizes.records) == pytest.approx(100.0)
    assert sum(sizes.share(n) for n in sizes.total.values()) == pytest.approx(100.0)
    assert ms.MarketSizes(archive=db, run_id="x").share(0) == 0.0


def test_the_report_names_every_top_market_and_its_counts(db):
    """The rendered table is the deliverable; the numbers must reach it."""
    sizes = ms.scan_market_sizes(db)
    text = ms.render(sizes, 20)

    assert "BIG" in text and "MID" in text and "SMALL" in text
    assert f"{sizes.max_count:,}" in text
    assert f"{sizes.inbound_frames:,}" in text
    assert "stream A" in text and "stream B" in text


# -- Read-only ---------------------------------------------------------------------


def test_the_archive_is_opened_read_only_and_nothing_spills(db, tmp_path,
                                                            monkeypatch):
    """
    A diagnostic that can write to an immutable archive is not read-only, and
    the counting index exists so that no spill file is created either.
    """
    spill = tmp_path / "spill"
    spill.mkdir()
    monkeypatch.setenv("DEPTHREC_V2_SPILL_DIR", str(spill))

    arch = ms._ObservedArchive(db)
    try:
        with pytest.raises(Exception):
            arch.con.execute("DELETE FROM ws_raw")
    finally:
        arch.close()

    ms.scan_market_sizes(db)
    assert list(spill.iterdir()) == []


def test_the_cli_reports_and_rejects_a_missing_archive(db, capsys):
    assert ms.main([str(db), "--top", "2"]) == 0
    assert "distinct markets" in capsys.readouterr().out
    assert ms.main([str(db.parent / "nope.sqlite")]) == 3
