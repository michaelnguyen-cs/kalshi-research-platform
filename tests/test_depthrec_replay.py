"""
Raw replay: rebuilding a persisted book from `ws_raw` alone.

Section 7 keeps the untouched frames so that "if the derived parser is later
questioned, the frozen data can be replayed through an independently written
decoder". These tests exercise that path end to end on a store built frame by
frame, and pin the one thing that made the first live attempt disagree.

That first attempt skipped sequence bookkeeping for anything that was not an
orderbook message. `ok` responses to `update_subscription` carry a `seq` on the
same per-subscription counter, so two mid-session add/delete_markets acks looked
like two forward jumps and falsely broke every book in the session. The live
path counts them; a decoder that counts differently is not an independent
check, it is a second bug.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from depthrec.store import RunContext, Store  # noqa: E402


def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "depthrec_report", ROOT / "scripts" / "depthrec_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rep = _load_report_module()


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "replay.sqlite", RunContext("run-1", "cafe"))
    s.record_run(
        started_ts=0.0, api_env="prod", base_url="b", ws_url="w",
        frozen_config={}, integrity={}, doc_reference="d",
    )
    s.record_session(
        session_id="s1", stream_label="A", session_epoch=1, use_yes_price=True,
        channels=["orderbook_delta"], subscribe_params={}, ws_url="w", opened_ts=0.0,
    )
    yield s
    s.close()


def feed(store: Store, msg: dict, *, epoch: int = 1) -> int:
    return store.record_raw(
        stream_label="A", session_epoch=epoch, direction="in",
        recv_ts=0.0, raw=json.dumps(msg),
    )


def snapshot(seq, ticker="T1", yes=None, no=None, sid=1):
    body = {"market_ticker": ticker}
    if yes:
        body["yes_dollars_fp"] = yes
    if no:
        body["no_dollars_fp"] = no
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": body}


def delta(seq, ticker="T1", side="no", price="0.7000", qty="5.00", sid=1):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "side": side,
                    "price_dollars": price, "delta_fp": qty}}


def observe(store: Store, ticker: str, raw_last_id: int, book: dict, validity="VALID"):
    store.record_observation({
        "ticker": ticker, "series": "KXBTC15M", "close_ts": 1_787_393_700,
        "target_ts": 1_787_393_280, "observed_ts": 0.0, "validity": validity,
        "unavailable_reason": "", "detail": "", "subscribed": 1, "x_contracts": 10,
        "book_json": json.dumps(book), "raw_last_id": raw_last_id,
        "stream_label": "A", "session_epoch": 1,
    })


def verify(store: Store) -> dict:
    conn = rep.connect(store.path)
    try:
        return rep.verify_replay(conn, "run-1")
    finally:
        conn.close()


def test_a_book_rebuilds_exactly_from_the_raw_frames(store):
    feed(store, snapshot(1, yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]]))
    feed(store, delta(2, price="0.7000", qty="12.00"))
    last = feed(store, delta(3, side="yes", price="0.6900", qty="7.00"))
    observe(store, "T1", last, {
        "bids": [[6900, 700], [6800, 500]],
        "asks": [[7000, 1200], [7100, 4000]],
    })
    result = verify(store)
    assert result["checked"] == 1 and result["agree"] == 1
    assert result["skipped_no_replayed_book"] == 0 and result["mismatches"] == 0
    # Every frame is decoded AND its stored checksum re-verified on the way past.
    assert result["frames_decoded_and_checksum_verified"] == 3


def test_an_ok_ack_between_deltas_does_not_fabricate_a_gap(store):
    """
    The regression. `ok` carries a seq on the subscription's counter; ignoring
    it makes the following delta look like seq+2 and breaks every book in the
    session -- which is exactly what the first live verification did.
    """
    feed(store, snapshot(1, yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]]))
    feed(store, {"type": "ok", "id": 2, "sid": 1, "seq": 2,
                 "msg": {"market_tickers": ["T1", "T2"]}})
    last = feed(store, delta(3, price="0.7000", qty="12.00"))
    observe(store, "T1", last, {
        "bids": [[6800, 500]], "asks": [[7000, 1200], [7100, 4000]],
    })
    result = verify(store)
    assert result["skipped_no_replayed_book"] == 0, "the ok ack broke the session"
    assert result["agree"] == 1 and result["mismatches"] == 0


def test_a_real_sequence_gap_still_breaks_the_book(store):
    """The fix must not have disarmed gap detection."""
    feed(store, snapshot(1, yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]]))
    last = feed(store, delta(9, price="0.7000", qty="12.00"))   # 2..8 missing
    observe(store, "T1", last, {"bids": [[6800, 500]], "asks": [[7100, 4000]]})
    result = verify(store)
    assert result["skipped_no_replayed_book"] == 1
    assert result["agree"] == 0


def test_a_replayed_older_seq_does_not_rewind_the_counter(store):
    """
    A repeat is not a gap, and the counter must not move backwards -- rewinding
    turns the next normal message into a fabricated jump.
    """
    feed(store, snapshot(1, yes=[["0.6800", "5.00"]], no=[["0.7100", "40.00"]]))
    feed(store, delta(2, price="0.7000", qty="12.00"))
    feed(store, delta(2, price="0.7000", qty="99.00"))          # replay, ignored
    last = feed(store, delta(3, side="yes", price="0.6900", qty="7.00"))
    observe(store, "T1", last, {
        "bids": [[6900, 700], [6800, 500]],
        "asks": [[7000, 1200], [7100, 4000]],
    })
    result = verify(store)
    assert result["skipped_no_replayed_book"] == 0
    assert result["agree"] == 1 and result["mismatches"] == 0


def test_a_wrong_stored_book_is_reported_as_a_mismatch(store):
    """
    The check has to be able to fail. If a derived row ever disagrees with the
    frames it came from, that must surface rather than pass quietly.
    """
    feed(store, snapshot(1, yes=[["0.6800", "5.00"]]))
    last = feed(store, delta(2, side="yes", price="0.6900", qty="7.00"))
    observe(store, "T1", last, {"bids": [[6900, 700]], "asks": []})  # missing 6800
    result = verify(store)
    assert result["mismatches"] == 1
    assert result["examples"][0]["ticker"] == "T1"


def test_a_snapshot_repairs_a_gapped_book_in_replay_too(store):
    feed(store, snapshot(1, yes=[["0.6800", "5.00"]]))
    feed(store, delta(9, side="yes", price="0.6700", qty="3.00"))    # gap
    last = feed(store, snapshot(10, yes=[["0.5000", "2.00"]], no=[["0.9000", "1.00"]]))
    observe(store, "T1", last, {"bids": [[5000, 200]], "asks": [[9000, 100]]})
    result = verify(store)
    assert result["skipped_no_replayed_book"] == 0
    assert result["agree"] == 1
