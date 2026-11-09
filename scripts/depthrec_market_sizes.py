#!/usr/bin/env python
"""
How the section 8-v2 streaming evaluator's decoded records are distributed
across markets, for one archive.

    python scripts/depthrec_market_sizes.py \
        /mnt/depthrec/S8V2_FORMAL_20260823_IMMUTABLE.sqlite

WHAT THIS IS FOR

The streaming evaluator holds one market at a time, so its peak resident set
should be set by the LARGEST single market, not by the size of the archive. The
two formal runs do not obviously behave that way: run 1 peaked at 392 MB on
4,375,632 frames and run 2 at 532 MB on 6,371,948 frames -- memory 1.36x for
frames 1.46x, which is much closer to linear in the archive than an
O(one market) architecture predicts.

That is a question about the input, not about the evaluator, and the market-size
distribution answers it. If the largest market grew by roughly the factor the
archive did, the observed scaling is what O(largest market) looks like on these
two archives and there is nothing to explain. If it did not, something outside
one market's event lists is holding memory, and this report says so by
elimination.

This is a DIAGNOSTIC. It computes no verdict, evaluates nothing, applies no
section 8-v2 rule, and writes nothing anywhere -- not to the archive, which is
opened read-only through the evaluator's own reader, and not to a spill file,
since the market index is replaced here by a counter rather than being built on
disk.

HOW THE ATTRIBUTION IS THE EVALUATOR'S

`ws_raw` has no market column: the ticker lives inside the frame payload, so
attributing a frame to a market REQUIRES decoding it. Decoding it a second way
would measure a second parser, so nothing here parses anything.

`equivalence_v2._scan_archive` is the evaluator's own first pass. It reads every
frame through `read_raw_text` with verification on, decides which frames carry a
market record at all, and calls `index.add(ticker, row_id)` once per record it
attributes. This script runs THAT function, unmodified, and passes it:

  * `_CountingIndex` in place of `_MarketIndex` -- the same two methods the scan
    calls, counting per market instead of spilling row ids to disk; and
  * `_ObservedArchive`, a `_Archive` subclass whose only addition is that it
    remembers the `stream_label` of the frame currently being scanned, so a
    record can be attributed to stream A or B without re-reading the frame.

Every decision about what a record is and which market it belongs to is
therefore made by production code. The count is the number of `add` calls, not
the number of frames: a frame carrying records for several markets contributes
one to each, which is the quantity the second pass would materialise. In this
archive format the two happen to coincide -- one orderbook frame carries one
market -- but counting calls rather than frames is what makes that a measurement
instead of an assumption.

A market's count is exactly the number of events the evaluator's second pass
builds for it, that is len(snaps[A]) + len(snaps[B]) + len(deltas[A]) +
len(deltas[B]) from `_market_events`. That sum is the memory claim.

Exit codes: 0 reported, 3 could not run.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec.equivalence_v2 import _Archive, _scan_archive  # noqa: E402

STREAMS = ("A", "B")
_MAX_TICKER_COL = 44


class _ObservedArchive(_Archive):
    """
    The evaluator's read-only archive reader, plus the label of the frame the
    first pass is currently on.

    `_scan_archive` hands the index a ticker and a row id and nothing else, but
    the stream a record arrived on is already sitting in the row it just read.
    Yielding through this generator records the label at the moment the row is
    handed over, which is strictly before any `add` for that row. Nothing else
    changes: the connection, its read-only URI, the compression dictionaries and
    the query are the base class's.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.current_label: str | None = None
        self.rows_scanned = 0
        self.outbound = 0

    def scan_rows(self, run_id: str):
        for row in super().scan_rows(run_id):
            self.current_label = row["stream_label"]
            self.rows_scanned += 1
            if row["direction"] == "out":
                self.outbound += 1
            yield row


class _CountingIndex:
    """
    A `_MarketIndex` stand-in that counts.

    `_scan_archive` uses exactly two methods of the index -- `add` and `finish`
    -- and never reads a row id back during the scan. Counting per (market,
    stream) instead of writing (market, row id) pairs to a temporary database
    leaves the scan's decisions untouched while keeping this script free of the
    spill file entirely. Resident state is one entry per market, the bound the
    evaluator's own market universe already has.
    """

    def __init__(self, arch: _ObservedArchive) -> None:
        self._arch = arch
        self.total: Counter[str] = Counter()
        self.by_stream: dict[str, Counter[str]] = {}
        self.records = 0

    def add(self, market: str, rid: int) -> None:
        self.total[market] += 1
        self.records += 1
        per = self.by_stream.get(market)
        if per is None:
            per = self.by_stream[market] = Counter()
        per[self._arch.current_label] += 1

    def finish(self) -> None:
        """The scan calls this to index the spill file. There isn't one."""


@dataclass
class MarketSizes:
    """What one archive scan measured. Every field is a count, not a judgement."""

    archive: Path
    run_id: str
    rows_scanned: int = 0
    outbound_frames: int = 0
    inbound_frames: int = 0
    records: int = 0
    total: Counter[str] = field(default_factory=Counter)
    by_stream: dict[str, Counter[str]] = field(default_factory=dict)
    elapsed: float = 0.0

    @property
    def markets(self) -> int:
        return len(self.total)

    @property
    def max_count(self) -> int:
        return max(self.total.values()) if self.total else 0

    def share(self, count: int) -> float:
        return (100.0 * count / self.records) if self.records else 0.0

    def ranked(self, top: int) -> list[tuple[str, int, dict[str, int]]]:
        """
        Markets by descending record count.

        Ties break on the ticker so the report is reproducible: two markets with
        the same count must not swap places between runs of this script.
        """
        order = sorted(self.total.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(m, n, dict(self.by_stream.get(m, {}))) for m, n in order[:top]]

    def labels(self) -> list[str]:
        seen: set[str] = set()
        for per in self.by_stream.values():
            seen.update(k for k in per if k is not None)
        return sorted(seen)


def scan_market_sizes(db_path: Path, run_id: str | None = None) -> MarketSizes:
    """One read-only pass over the archive, through the evaluator's first pass."""
    arch = _ObservedArchive(Path(db_path))
    try:
        index = _CountingIndex(arch)
        t0 = time.time()
        scan = _scan_archive(arch, index, run_id)
        return MarketSizes(
            archive=Path(db_path),
            run_id=scan.run_id,
            rows_scanned=arch.rows_scanned,
            outbound_frames=arch.outbound,
            inbound_frames=scan.frames,
            records=index.records,
            total=index.total,
            by_stream=index.by_stream,
            elapsed=time.time() - t0,
        )
    finally:
        arch.close()


def render(sizes: MarketSizes, top: int) -> str:
    rows = sizes.ranked(top)
    labels = sizes.labels()
    shown = [s for s in STREAMS if s in labels] or labels
    width = min(max([len(m) for m, _n, _p in rows] + [6]), _MAX_TICKER_COL)

    out: list[str] = []
    add = out.append
    add(f"archive             : {sizes.archive}")
    add(f"run_id              : {sizes.run_id}")
    add(f"scan elapsed        : {sizes.elapsed:.1f} s")
    add("")
    add(f"ws_raw rows in run  : {sizes.rows_scanned:,}")
    add(f"  outbound frames   : {sizes.outbound_frames:,}  (subscriptions)")
    add(f"  raw ws_raw frames : {sizes.inbound_frames:,}  "
        f"(the evaluator's `frames`)")
    add("")
    add(f"decoded records     : {sizes.records:,}  (attributed to a market)")
    add(f"distinct markets    : {sizes.markets:,}")
    add(f"max single market   : {sizes.max_count:,}  "
        f"({sizes.share(sizes.max_count):.4f}% of decoded records)")
    if sizes.markets:
        add(f"mean per market     : {sizes.records / sizes.markets:,.1f}")
    add("")

    head = f"{'rank':>4}  {'market':<{width}}  {'total':>12}"
    for lab in shown:
        head += f"  {'stream ' + lab:>12}"
    head += f"  {'% decoded':>10}"
    add(head)
    add("-" * len(head))
    for i, (market, n, per) in enumerate(rows, 1):
        name = market if len(market) <= width else market[:width - 1] + "~"
        line = f"{i:>4}  {name:<{width}}  {n:>12,}"
        for lab in shown:
            line += f"  {per.get(lab, 0):>12,}"
        line += f"  {sizes.share(n):>9.4f}%"
        add(line)

    other = [lab for lab in labels if lab not in shown]
    if other:
        add("")
        add(f"note: records also carried stream label(s) {', '.join(other)}")
    add("")
    add(f"top {len(rows)} share      : "
        f"{sizes.share(sum(n for _m, n, _p in rows)):.4f}% of decoded records")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Per-market decoded record counts for one depthrec archive "
                    "(read-only diagnostic; computes no verdict).")
    ap.add_argument("archive", type=Path, help="path to the archive")
    ap.add_argument("--run-id", default=None,
                    help="run to scan; default is the archive's first run")
    ap.add_argument("--top", type=int, default=20,
                    help="markets to list, ranked by record count (default 20)")
    args = ap.parse_args(argv)

    if not args.archive.exists():
        print(f"no such archive: {args.archive}", file=sys.stderr)
        return 3
    if args.top < 1:
        print("--top must be at least 1", file=sys.stderr)
        return 3

    try:
        sizes = scan_market_sizes(args.archive, args.run_id)
    except Exception as exc:  # noqa: BLE001
        print(f"SCAN FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    print(render(sizes, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
