"""
The frozen section 8-v2 loader and driver, verbatim from main @ 0cb510c.

TEST FIXTURE ONLY. Nothing imports this outside the test suite and nothing may:
it is the whole-archive evaluator whose peak memory OOM-killed formal run 1, and
it exists here for one purpose -- to be run beside the streaming evaluator over
the same synthetic archive and have its complete structured result compared field
by field. "The refactor changed only streaming, iteration, batching and storage"
is a claim; a differential test against the code that was replaced is evidence.

Only the two functions the refactor touched are copied. Everything the refactor
did not touch -- sections 4 to 7, section 12's arithmetic, the result object --
is imported from depthrec.equivalence_v2, so this file cannot silently drift into
being a second implementation of the semantics.

Extracted with:
    git show 0cb510c:src/depthrec/equivalence_v2.py | sed -n '628,715p;737,935p'
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from depthrec import frozen
from depthrec.equivalence_v2 import (
    EXCLUDED_EPOCH_TAIL,
    EXCLUDED_GUARD_BAND,
    EXCLUDED_PRE_BASELINE,
    BoundaryBreach,
    DeltaEvent,
    Failure,
    SnapshotEvent,
    V2Result,
    Verification,
    _epoch_snapshot,
    _evaluate_epoch,
    _health_boundaries,
    boundary_allowance,
    canonical_price,
    market_breaches_boundary_cap,
)
from depthrec.book import to_qty
from depthrec.store import read_raw_text

def _load_stream_events(db_path: Path, run_id: str | None):
    """
    Decode ws_raw into per-stream ordered event lists plus the boundary events
    section 6 keys epochs on.

    Every frame is read through depthrec.store.read_raw_text with verification
    on, so a frame that does not reproduce its stored SHA-256 raises rather than
    being decoded into something plausible.
    """
    import sqlite3

    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    zdicts = {r["id"]: bytes(r["zdict"])
              for r in con.execute("SELECT id, zdict FROM raw_dictionary")}
    if run_id is None:
        row = con.execute(
            "SELECT run_id FROM ws_raw ORDER BY id LIMIT 1").fetchone()
        run_id = row["run_id"] if row else ""

    deltas: dict[str, dict[str, list[DeltaEvent]]] = {"A": {}, "B": {}}
    snaps: dict[str, dict[str, list[SnapshotEvent]]] = {"A": {}, "B": {}}
    boundaries: list[tuple[float, str, str, str]] = []   # (recv_ts, stream, kind, detail)
    subscribed: dict[str, set[str]] = {"A": set(), "B": set()}
    seq_prev: dict[tuple[str, Any], int] = {}
    archive_max_ts_ms = 0
    frames = 0
    recv_lo = recv_hi = None

    for row in con.execute(
        "SELECT id, run_id, stream_label, direction, recv_ts, raw, codec, "
        "zdict_id, payload, raw_sha256 FROM ws_raw WHERE run_id=? ORDER BY id",
        (run_id,),
    ):
        lab = row["stream_label"]
        text = read_raw_text(row, zdicts, verify=True)
        if row["direction"] == "out":
            try:
                cmd = json.loads(text)
            except json.JSONDecodeError:
                continue
            params = cmd.get("params") or {}
            for t in params.get("market_tickers") or ():
                subscribed[lab].add(t)
            continue
        frames += 1
        ts = row["recv_ts"]
        recv_lo = ts if recv_lo is None else min(recv_lo, ts)
        recv_hi = ts if recv_hi is None else max(recv_hi, ts)
        msg = json.loads(text)
        mt = msg.get("type")
        if mt == "error":
            boundaries.append((ts, lab, "server_error", text[:200]))
            continue
        if mt not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or not isinstance(seq, int) or isinstance(seq, bool):
            continue
        prev = seq_prev.get((lab, sid))
        if prev is not None and seq != prev + 1 and seq > prev:
            boundaries.append(
                (ts, lab, "sequence_gap", f"sid={sid} {prev} -> {seq}"))
        seq_prev[(lab, sid)] = seq
        body = msg.get("msg") or {}
        tk = body.get("market_ticker")
        if not tk:
            continue
        if mt == "orderbook_snapshot":
            snaps[lab].setdefault(tk, []).append(
                SnapshotEvent(row["id"], ts, sid, seq, tk, body))
        else:
            side = body.get("side")
            if side not in ("yes", "no"):
                continue
            price = canonical_price(
                body.get("price_dollars"), side, use_yes_price=(lab == "A"))
            key = Verification(body.get("ts_ms"), side, price,
                               to_qty(body.get("delta_fp")))
            if isinstance(key.ts_ms, int):
                archive_max_ts_ms = max(archive_max_ts_ms, key.ts_ms)
            deltas[lab].setdefault(tk, []).append(
                DeltaEvent(row["id"], ts, sid, seq, tk, key, body))
    con.close()
    minutes = 0.0 if recv_lo is None else (recv_hi - recv_lo) / 60.0
    return (run_id, deltas, snaps, boundaries, subscribed, archive_max_ts_ms,
            frames, minutes, recv_hi or 0.0)

def evaluate_archive(db_path: Path, run_id: str | None = None) -> V2Result:
    """
    Section 8-v2 evaluated over a recorded archive.

    Pure with respect to the archive: it writes nothing, and re-running it over
    preserved evidence reproduces the verdict.
    """
    (run_id, deltas, snaps, boundaries, subscribed, archive_max_ts_ms,
     frames, minutes, archive_last_recv) = _load_stream_events(db_path, run_id)

    # The opening connect of each stream establishes the initial epoch rather
    # than ending one. The teardown disconnect is the recorder stopping at
    # section 8.0's 60 minutes, not a transport interruption -- section 6.1 says
    # it "does not apply to a normally completed final epoch", whose boundary is
    # section 5's exact cutoff. A disconnect at or after the last archived frame
    # is that teardown and is not an epoch boundary; one before it is a real
    # interruption and is.
    health = _health_boundaries(Path(db_path), run_id)
    boundaries = boundaries + [
        b for b in health
        if b[2] == "disconnect" and b[0] < archive_last_recv
    ]
    boundaries.sort()

    # Section 12 asks for reconnect counts on a boundary-evidence insufficiency.
    # The opening connect of each stream is not a reconnect; every later one is.
    seen_connect: set[str] = set()
    reconnects: dict[str, int] = {}
    for ts, lab, kind, _detail in sorted(health):
        if kind != "connect":
            continue
        if lab in seen_connect:
            reconnects[lab] = reconnects.get(lab, 0) + 1
        else:
            seen_connect.add(lab)

    failures: list[Failure] = []
    result = V2Result(verdict=frozen.V2_RESULT_FAIL, run_id=run_id)
    result.frames = frames
    result.reconnects = reconnects
    result.minutes = minutes
    result.archive_max_ts_ms = archive_max_ts_ms
    cutoff = archive_max_ts_ms - frozen.V2_GUARD_BAND_MS
    result.guard_cutoff_ts_ms = cutoff

    # Section 8 criterion 6.
    if subscribed["A"] and subscribed["B"] and subscribed["A"] != subscribed["B"]:
        result.same_market_set = False
        failures.append(Failure(
            "MARKET_SET_MISMATCH", "-", 0,
            f"stream A subscribed {len(subscribed['A'])} markets, B "
            f"{len(subscribed['B'])}; symmetric difference "
            f"{len(subscribed['A'] ^ subscribed['B'])}"))

    markets = sorted(set(deltas["A"]) | set(deltas["B"])
                     | set(snaps["A"]) | set(snaps["B"]))
    per_market_pairs: dict[str, int] = {}
    per_market_excluded: dict[str, int] = {}
    qualifying_markets: set[str] = set()

    for market in markets:
        a_snaps = snaps["A"].get(market, [])
        b_snaps = snaps["B"].get(market, [])
        a_all = deltas["A"].get(market, [])
        b_all = deltas["B"].get(market, [])

        # Section 5, applied first: global, in exchange time, symmetric.
        a_kept = [e for e in a_all
                  if isinstance(e.key.ts_ms, int) and e.key.ts_ms <= cutoff]
        b_kept = [e for e in b_all
                  if isinstance(e.key.ts_ms, int) and e.key.ts_ms <= cutoff]
        guard_excluded = (len(a_all) - len(a_kept)) + (len(b_all) - len(b_kept))

        # Section 6 epoch segmentation. The initial snapshot of a market on a
        # stream establishes that market's first epoch; a LATER snapshot, a
        # sequence gap or a disconnect on either stream ends it.
        # Section 6. A LATER snapshot on either stream, a sequence gap, or a
        # disconnect ends the current epoch for every market on that
        # subscription. The FIRST snapshot of a market on a stream establishes
        # that stream's book rather than ending an epoch -- section 8.0 times
        # the run from "both streams have established their initial valid
        # epochs", so the opening snapshot burst is the establishment, not a
        # run of terminations.
        cut_points: list[float] = []
        for s in a_snaps[1:] + b_snaps[1:]:
            cut_points.append(s.recv_ts)
        for ts, _lab, _kind, _detail in boundaries:
            cut_points.append(ts)
        cut_points = sorted(set(cut_points))

        if not a_snaps or not b_snaps:
            continue

        segments: list[tuple[float, float, bool]] = []
        lo = min(a_snaps[0].recv_ts, b_snaps[0].recv_ts)
        for cp in cut_points:
            if cp <= lo:
                continue
            segments.append((lo, cp, True))
            lo = cp
        segments.append((lo, float("inf"), False))

        market_pairs = 0
        market_excluded = guard_excluded
        for idx, (lo, hi, interrupted) in enumerate(segments):
            a_snap = _epoch_snapshot(a_snaps, lo, hi, first=idx == 0)
            b_snap = _epoch_snapshot(b_snaps, lo, hi, first=idx == 0)
            # Each stream's deltas run from ITS OWN snapshot, not from the later
            # of the two. The offset between the two snapshot instants is
            # exactly what section 4's baseline exists to resolve; truncating
            # the earlier stream at the later snapshot would instead drop
            # deltas its own book depends on and break its replay.
            a_from = a_snap.recv_ts if a_snap else lo
            b_from = b_snap.recv_ts if b_snap else lo
            a_seg = [e for e in a_kept if a_from <= e.recv_ts < hi]
            b_seg = [e for e in b_kept if b_from <= e.recv_ts < hi]
            out = _evaluate_epoch(
                market, idx, a_snap, b_snap, a_seg, b_seg,
                interrupted=interrupted, failures=failures)
            out.excluded_guard_band = guard_excluded if idx == 0 else 0
            result.epochs.append(out)
            market_pairs += out.paired
            market_excluded += out.excluded_pre_baseline + out.excluded_epoch_tail
            result.excluded_pre_baseline += out.excluded_pre_baseline
            result.excluded_epoch_tail += out.excluded_epoch_tail
            if out.qualifies:
                result.qualifying_events += out.paired
                qualifying_markets.add(market)

        result.excluded_guard_band += guard_excluded
        result.paired_events += market_pairs
        per_market_pairs[market] = market_pairs
        per_market_excluded[market] = market_excluded

        # Section 5's cap as amended by section 12: resolution-adjusted, and a
        # boundary-EVIDENCE breach rather than a decoder failure. It is recorded
        # here and weighed against the FAIL conditions by the precedence below.
        d = 2 * market_pairs + market_excluded
        if d:
            d_effective = max(d, frozen.V2_BOUNDARY_MIN_DENOMINATOR)
            frac = market_excluded / d_effective
            if market_breaches_boundary_cap(market_pairs, market_excluded):
                mine = [e for e in result.epochs if e.market == market]
                result.anomalous_markets.append((market, round(frac, 6)))
                result.boundary_breaches.append(BoundaryBreach(
                    market=market, paired=market_pairs,
                    excluded=market_excluded, d=d, d_effective=d_effective,
                    fraction=frac, allowance=boundary_allowance(d),
                    classes={
                        EXCLUDED_PRE_BASELINE:
                            sum(e.excluded_pre_baseline for e in mine),
                        EXCLUDED_GUARD_BAND: guard_excluded,
                        EXCLUDED_EPOCH_TAIL:
                            sum(e.excluded_epoch_tail for e in mine),
                    },
                    epochs=len(mine),
                    interrupted_epochs=sum(1 for e in mine if e.interrupted),
                ))

    result.markets_with_any_pair = sum(1 for v in per_market_pairs.values() if v)
    result.qualifying_markets = len(qualifying_markets)
    result.failures = failures

    # Section 12's run-level cap: the UNADJUSTED aggregate, no denominator
    # floor. Independent of the per-market test, so a run made of many
    # individually-tolerable exclusions is still audited in total.
    result.run_excluded = (result.excluded_pre_baseline
                           + result.excluded_guard_band
                           + result.excluded_epoch_tail)
    result.run_denominator = 2 * result.paired_events + result.run_excluded
    if result.run_denominator:
        result.run_fraction = result.run_excluded / result.run_denominator
        result.run_cap_breached = (
            result.run_fraction > frozen.V2_MAX_BOUNDARY_EXCLUSION_FRACTION)

    if result.qualifying_events < frozen.V2_MIN_QUALIFYING_EVENTS:
        result.unmet_floors.append(
            f"qualifying events {result.qualifying_events:,} < "
            f"{frozen.V2_MIN_QUALIFYING_EVENTS:,}")
    if result.qualifying_markets < frozen.V2_MIN_QUALIFYING_MARKETS:
        result.unmet_floors.append(
            f"qualifying markets {result.qualifying_markets:,} < "
            f"{frozen.V2_MIN_QUALIFYING_MARKETS:,}")

    # Section 12's precedence, in order and without exception. A FAIL is never
    # converted: every condition section 8-v2 specifies as FAIL or FAIL pending
    # investigation outranks both insufficiencies, so a criterion-6 failure
    # alongside a cap breach reports the failure rather than hiding it behind
    # an insufficiency.
    caps_breached = bool(result.boundary_breaches) or result.run_cap_breached
    if failures:
        result.verdict = frozen.V2_RESULT_FAIL
    elif caps_breached:
        result.verdict = frozen.V2_RESULT_INSUFFICIENT_BOUNDARY
    elif result.unmet_floors:
        result.verdict = frozen.V2_RESULT_INSUFFICIENT
    else:
        result.verdict = frozen.V2_RESULT_PASS
    return result
