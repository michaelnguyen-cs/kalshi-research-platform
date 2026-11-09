"""
The independent REST census: it defines the denominator, and nothing about the
WebSocket may touch it.

The properties under test are the ones that keep the measured clearance rate
honest:

  * a discovery failure never removes a market
  * the reconciliation pass recovers a market the live pass missed
  * every request, response and failure is timestamped and persisted
  * a market whose close time moves is recorded as changing, not overwritten
    silently
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen  # noqa: E402
from depthrec.census import PASS_LIVE, PASS_RECONCILE, RestCensus, parse_close_ts  # noqa: E402
from depthrec.store import RunContext, Store, read_census_response  # noqa: E402


class FakeBucket:
    def acquire(self) -> None:
        pass


class FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class FakeMarketsApi:
    """
    Stands in for the SDK's raw markets endpoint. `script` maps a call
    signature to either a payload dict or an Exception to raise.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: list = []

    def get_markets_without_preload_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if self.responses else {"markets": []}
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class FakeClient:
    def __init__(self) -> None:
        self._read_bucket = FakeBucket()
        self._markets_api = FakeMarketsApi()


def market(ticker: str, close_iso: str, status: str = "active") -> dict:
    return {
        "ticker": ticker, "close_time": close_iso, "status": status,
        "event_ticker": ticker.rsplit("-", 1)[0],
    }


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "census.sqlite", RunContext("run-1", "deadbeef"))
    yield s
    s.close()


@pytest.fixture
def census(store) -> RestCensus:
    return RestCensus(client=FakeClient(), store=store, series=("KXBTC15M",))


# -- Discovery ------------------------------------------------------------------

def test_live_pass_discovers_and_persists_every_market(census, store):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z"),
                     market("KXBTC15M-B", "2026-08-22T10:30:00Z")]},
    ]
    added = census.run_live_pass(now=1000.0)
    assert {e.ticker for e in added} == {"KXBTC15M-A", "KXBTC15M-B"}

    rows = store.census_markets()
    assert {r["ticker"] for r in rows} == {"KXBTC15M-A", "KXBTC15M-B"}
    assert all(r["first_seen_ts"] == 1000.0 for r in rows)
    assert all(r["discovered_by"] == PASS_LIVE for r in rows)

    reqs = list(store.conn.execute("SELECT * FROM census_request"))
    assert len(reqs) == 1
    assert reqs[0]["ok"] == 1
    assert reqs[0]["ts"] > 0
    assert reqs[0]["n_markets"] == 2
    assert reqs[0]["response_sha256"]
    # The body is stored compressed; read_census_response also re-checks the
    # sha256 of the uncompressed text, so this asserts fidelity as well as shape.
    assert json.loads(read_census_response(reqs[0]))["markets"]
    assert json.loads(reqs[0]["params"])["series_ticker"] == "KXBTC15M"


def test_a_discovery_failure_is_recorded_and_removes_nothing(census, store):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z")]},
        RuntimeError("connection reset by peer"),
    ]
    census.run_live_pass(now=1000.0)
    census.run_live_pass(now=1060.0)

    assert census.live_failures == 1
    assert set(census.entries) == {"KXBTC15M-A"}
    assert {r["ticker"] for r in store.census_markets()} == {"KXBTC15M-A"}

    failures = list(store.conn.execute("SELECT * FROM census_request WHERE ok = 0"))
    assert len(failures) == 1
    assert "connection reset by peer" in failures[0]["error"]
    assert failures[0]["ts"] > 0


def test_a_market_that_stops_appearing_stays_in_the_census(census, store):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z")]},
        {"markets": []},
    ]
    census.run_live_pass(now=1000.0)
    census.run_live_pass(now=1060.0)
    assert set(census.entries) == {"KXBTC15M-A"}
    assert census.entries["KXBTC15M-A"].first_seen_ts == 1000.0


def test_reconcile_recovers_a_market_the_live_pass_missed(census, store):
    census._client._markets_api.responses = [
        {"markets": []},                                       # live: sees nothing
        {"markets": [market("KXBTC15M-MISSED", "2026-08-22T10:15:00Z", "finalized")]},
    ]
    assert census.run_live_pass(now=1000.0) == []
    added = census.run_reconcile_pass(now=1010.0)
    assert [e.ticker for e in added] == ["KXBTC15M-MISSED"]
    assert census.entries["KXBTC15M-MISSED"].discovered_by == PASS_RECONCILE


def test_reconcile_queries_by_series_and_close_time_range(census):
    census._client._markets_api.responses = [{"markets": []}]
    census.run_reconcile_pass(now=2_000_000.0)
    call = census._client._markets_api.calls[-1]
    assert call["series_ticker"] == "KXBTC15M"
    assert call["min_close_ts"] == int(2_000_000 - frozen.CENSUS_RECONCILE_LOOKBACK_SECONDS)
    assert call["max_close_ts"] == int(2_000_000 + frozen.CENSUS_RECONCILE_LOOKAHEAD_SECONDS)
    # No status filter: the point is to see what the live filter could exclude.
    assert "status" not in call


def test_live_pass_asks_only_for_currently_open_markets(census):
    census._client._markets_api.responses = [{"markets": []}]
    census.run_live_pass(now=1000.0)
    call = census._client._markets_api.calls[-1]
    assert call["status"] == frozen.UNIVERSE_OPEN_STATUS
    assert call["series_ticker"] == "KXBTC15M"


def test_paging_follows_the_cursor(census, store):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z")], "cursor": "c1"},
        {"markets": [market("KXBTC15M-B", "2026-08-22T10:30:00Z")], "cursor": ""},
    ]
    census.run_live_pass(now=1000.0)
    assert set(census.entries) == {"KXBTC15M-A", "KXBTC15M-B"}
    assert census._client._markets_api.calls[1]["cursor"] == "c1"


def test_a_failure_mid_paging_discards_the_partial_page_set(census, store):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z")], "cursor": "c1"},
        RuntimeError("boom"),
    ]
    census.run_live_pass(now=1000.0)
    # Nothing ingested: a half-read universe would look like a shrunk one.
    assert census.entries == {}
    assert census.live_failures == 1


# -- Change tracking ---------------------------------------------------------------

def test_a_moved_close_time_is_recorded_as_a_change(census, store):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z")]},
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:20:00Z")]},
    ]
    census.run_live_pass(now=1000.0)
    census.run_live_pass(now=1060.0)
    changes = [dict(r) for r in store.conn.execute(
        "SELECT * FROM census_change WHERE change='close_ts_changed'"
    )]
    assert len(changes) == 1
    assert changes[0]["ts"] == 1060.0
    assert census.entries["KXBTC15M-A"].close_ts == parse_close_ts("2026-08-22T10:20:00Z")


def test_a_status_change_is_recorded(census, store):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z", "active")]},
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z", "closed")]},
    ]
    census.run_live_pass(now=1000.0)
    census.run_live_pass(now=1060.0)
    changes = [r["change"] for r in store.conn.execute("SELECT * FROM census_change")]
    assert "status_changed" in changes


# -- Targets ------------------------------------------------------------------------

def test_target_time_is_close_minus_420_seconds(census):
    census._client._markets_api.responses = [
        {"markets": [market("KXBTC15M-A", "2026-08-22T10:15:00Z")]},
    ]
    census.run_live_pass(now=1000.0)
    entry = census.entries["KXBTC15M-A"]
    assert entry.target_ts == entry.close_ts - 420


def test_a_market_with_no_close_time_still_enters_the_census(census, store):
    census._client._markets_api.responses = [
        {"markets": [{"ticker": "KXBTC15M-X", "status": "active"}]},
    ]
    census.run_live_pass(now=1000.0)
    assert "KXBTC15M-X" in census.entries
    assert census.entries["KXBTC15M-X"].target_ts is None
    assert census.all_with_target() == []


def test_parse_close_ts_tolerates_the_shapes_kalshi_returns():
    assert parse_close_ts("2026-08-22T10:15:00Z") == parse_close_ts("2026-08-22T10:15:00+00:00")
    assert parse_close_ts(None) is None
    assert parse_close_ts("not a time") is None
    assert parse_close_ts(1787393700) == 1787393700
