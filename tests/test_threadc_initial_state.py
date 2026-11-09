"""
Explicit initial state: what makes the ticker/trade bank reconstructible.

The old design got its starting point for free -- `orderbook_delta` answers a
subscribe with an `orderbook_snapshot` for every market. `ticker` and `trade`
answer with nothing, and a market whose quotes have not moved and which has not
traded stays silent for as long as that remains true. So "no message yet" says
nothing at all about state, and a reader reconstructing the bank at an arbitrary
timestamp would have nothing to start from.

These tests pin the replacement: at every subscription establishment, and for
every market added to a live subscription, the recorder banks the displayed
state Kalshi's own REST market body reported -- append-only, epoch-stamped, and
never invented when the body is missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from threadc import config as tc, frozen  # noqa: E402
from threadc.recorder import INITIAL_STATE_FIELDS, ThreadCRecorder  # noqa: E402
from threadc.recorder import _market_state_projection  # noqa: E402
from threadc_fakes import FakeAPI, FakeSession, ample_disk, event, market  # noqa: E402


NO_CONFIG_FILE = Path(__file__).with_name("no-such-threadc-config.json")
"""A path that does not exist, so threadc.config.load() builds the configuration
from the overrides alone.

Passing None here would resolve to config/threadc.json and quietly layer these
overrides on top of the committed production file -- including its ticker-only
channel selection, which is a production decision and not this test's subject."""


def recorder(tmp_path, markets=None, mode=frozen.MODE_PRODUCTION):
    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": markets if markets is not None else [market("KXMLBGAME", 1)]},
    )
    cfg = tc.load(NO_CONFIG_FILE, mode=mode, overrides={
        "archive_dir": str(tmp_path / "archive"),
        "collection_end_utc": "2099-01-01T00:00:00Z",
        "disk_reserve_bytes": 10 * 1024**3,
        "discovery_daily_target_utc": "00:00:00",
    })
    return ThreadCRecorder(
        cfg, api=api, git_commit="deadbeef",
        session_factory=lambda *, tickers, label: FakeSession(tickers=tickers, label=label),
        disk_check=ample_disk(),
    )


def rows(rec):
    return rec.segment.store.query(
        "SELECT * FROM initial_state ORDER BY id"
    )


# -- The projection -----------------------------------------------------------

def test_the_projection_covers_every_displayed_ticker_field():
    """
    The initial state must carry what the `ticker` channel itself carries --
    otherwise the first ticker message would be the first time a reader learns
    the displayed touch size, and everything before it would be unknown.
    """
    body = market("KXMLBGAME", 1)
    projected = _market_state_projection(body)
    for required in (
        "yes_bid_dollars", "yes_ask_dollars",
        "yes_bid_size_fp", "yes_ask_size_fp",
        "last_price_dollars", "volume_fp", "open_interest_fp",
    ):
        assert required in projected


def test_the_projection_keeps_kalshis_values_verbatim():
    """Nothing is renamed, rescaled, combined or derived. It is a record."""
    body = market("KXMLBGAME", 1)
    projected = _market_state_projection(body)
    for key, value in projected.items():
        assert body[key] == value
    assert set(projected) <= set(INITIAL_STATE_FIELDS)


def test_a_field_kalshi_did_not_send_is_absent_not_defaulted():
    body = market("KXMLBGAME", 1)
    del body["yes_bid_size_fp"]
    projected = _market_state_projection(body)
    assert "yes_bid_size_fp" not in projected
    assert "yes_ask_size_fp" in projected


# -- Banking at establishment -------------------------------------------------

async def test_an_explicit_state_is_banked_when_the_subscription_comes_up(tmp_path):
    rec = recorder(tmp_path)
    await rec.run(stop_after_seconds=0.2)

    banked = rec.status.initial_states_banked
    assert banked == 1
    assert rec.status.initial_state_failures == 0


async def test_the_banked_state_is_epoch_and_shard_stamped(tmp_path):
    rec = recorder(tmp_path)

    captured = {}

    async def check():
        while not rec.sessions:
            await __import__("asyncio").sleep(0.01)
        while not rows(rec):
            await __import__("asyncio").sleep(0.01)
        captured["rows"] = [dict(r) for r in rows(rec)]

    import asyncio
    task = asyncio.ensure_future(check())
    await rec.run(stop_after_seconds=0.5)
    await task

    row = captured["rows"][0]
    assert row["shard_label"] == "S00"
    assert row["session_epoch"] == 1
    assert row["reason"] == "subscription_established"
    assert row["source"] == "rest_market_census"
    assert row["observed_ts"] is not None
    state = json.loads(row["state_json"])
    assert state["yes_bid_size_fp"] == "10.00"


async def test_a_reconnect_banks_a_fresh_state_rather_than_reusing_the_old_one(tmp_path):
    """
    Append-only, and per epoch. A book carried across a reconnect would be a
    claim about a period nobody observed.
    """
    rec = recorder(tmp_path)
    import asyncio

    async def reconnect_once():
        while not rec.sessions:
            await asyncio.sleep(0.01)
        session = next(iter(rec.sessions.values()))
        while session.session_epoch < 1:
            await asyncio.sleep(0.01)
        session.connect()          # epoch 2
        await asyncio.sleep(0.2)

    task = asyncio.ensure_future(reconnect_once())
    await rec.run(stop_after_seconds=0.6)
    await task

    assert rec.status.initial_states_banked == 2


async def test_a_market_added_to_a_live_subscription_gets_its_own_state(tmp_path):
    """
    A newly opened market gets no snapshot either -- neither channel sends one --
    so it needs the same explicit state as one present at establishment.
    """
    rec = recorder(tmp_path)
    import asyncio

    seen = {}

    async def add_a_market():
        while not rec.sessions:
            await asyncio.sleep(0.01)
        rec.api.markets["KXMLBGAME"].append(market("KXMLBGAME", 2))
        await asyncio.to_thread(rec.census.run_live_pass)
        await rec._sync_subscription()
        # Read while the segment is still open: finalization closes it.
        seen["reasons"] = {r["reason"] for r in rows(rec)}

    task = asyncio.ensure_future(add_a_market())
    await rec.run(stop_after_seconds=0.6)
    await task

    assert rec.status.initial_states_banked == 2
    assert seen["reasons"] == {"subscription_established", "market_added"}


# -- Failure is recorded, never invented --------------------------------------

async def test_a_market_with_no_rest_body_is_counted_not_fabricated(tmp_path):
    rec = recorder(tmp_path)
    import asyncio

    async def bank_an_unknown_market():
        while not rec.sessions or rec.segment is None:
            await asyncio.sleep(0.01)
        await rec._bank_initial_state(
            "S00", 1, 0.0, reason="test", tickers=["KXMLBGAME-NEVER-CENSUSED"],
        )

    task = asyncio.ensure_future(bank_an_unknown_market())
    await rec.run(stop_after_seconds=0.4)
    await task

    assert rec.status.initial_state_failures >= 1


# -- The manifest -------------------------------------------------------------

async def test_the_manifest_reports_the_initial_state_aggregate(tmp_path):
    rec = recorder(tmp_path)
    await rec.run(stop_after_seconds=0.2)

    summary = rec.final_manifest["initial_state"]
    assert summary["total"] == 1
    assert summary["by_reason"]["subscription_established"] == 1
    assert summary["establishments"][0]["session_epoch"] == 1
    # Aggregate only: no ticker, no price, no size anywhere in the manifest.
    text = json.dumps(rec.final_manifest).lower()
    assert "yes_bid_size_fp".lower() not in text
    assert "kxmlbgame-m01" not in text


async def test_the_manifest_names_the_channels_actually_banked(tmp_path):
    rec = recorder(tmp_path)
    await rec.run(stop_after_seconds=0.2)
    assert rec.final_manifest["channels"] == ["ticker", "trade"]


async def test_a_rotated_segment_gets_its_own_starting_state(tmp_path):
    """
    A rotation is not a reconnect: the sockets keep running and neither channel
    resends anything. Every segment is meant to be independently readable, so
    the new one must not depend on the previous file for its starting state.
    """
    rec = recorder(tmp_path)
    import asyncio

    seen = {}

    async def rotate_once():
        while not rec.sessions or rec.segment is None:
            await asyncio.sleep(0.01)
        await rec.rotate()
        seen["rows"] = [dict(r) for r in rows(rec)]

    task = asyncio.ensure_future(rotate_once())
    await rec.run(stop_after_seconds=0.6)
    await task

    # Read from the NEW segment: it carries a state of its own, banked at the
    # rotation rather than inherited from the file that was just finalized.
    assert "segment_rotation" in {r["reason"] for r in seen["rows"]}
    assert rec.status.initial_states_banked >= 2
