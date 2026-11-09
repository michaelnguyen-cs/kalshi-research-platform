"""
Collection Universe v1, the frozen predicate, and Frozen Rules 1 and 2.

The properties pinned here are the ones that decide what gets banked:

  * the predicate is exact, case-sensitive equality against "Pro Baseball", and
    is NOT widened to the Q1 adjacent values;
  * a series is admitted from the FIRST COMPLETE census that sees it qualify,
    with immutable evidence including a SHA-256;
  * inclusion is append-only -- later metadata changes produce an observation,
    never an eviction;
  * an INCOMPLETE (Gate 3) census applies ZERO additions and ZERO removals;
  * a later COMPLETE census recovers a series an earlier one could not read;
  * discovery has no prefilter, so a qualifying series outside every obvious MLB
    ticker/tag/category namespace is found on the same footing as one inside it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from threadc import frozen  # noqa: E402
from threadc.discovery import run_discovery_census  # noqa: E402
from threadc.universe import AppendOnlyUniverse, Q1_STATUS, UniverseError  # noqa: E402
from threadc_fakes import FakeAPI, event  # noqa: E402

V1_COUNT = frozen.EXPECTED_V1_QUALIFYING_COUNT


# -- The predicate -----------------------------------------------------------


def test_predicate_is_exact_case_sensitive_equality():
    assert frozen.qualifies({"Pro Baseball"})
    assert frozen.qualifies({None, "Pro Baseball"})
    for near_miss in ("pro baseball", "PRO BASEBALL", "Pro  Baseball", "Pro Baseball ",
                      "MLB", "Professional Baseball"):
        assert not frozen.qualifies({near_miss}), near_miss


def test_q1_adjacent_values_do_not_qualify_and_q1_stays_unresolved():
    for adjacent in ("All-Star", "Home Run Derby", "Pro Baseball Draft",
                     "Pro Baseball Spring Training"):
        assert not frozen.qualifies({adjacent}), adjacent
    assert Q1_STATUS["status"] == "UNRESOLVED"
    assert Q1_STATUS["predicate_widened"] is False


def test_rival_baseball_competitions_do_not_qualify():
    for other in ("Korea KBO", "Japan NPB", "Mexico LMB", "College Baseball",
                  "College Softball", "World Baseball Classic",
                  "World Baseball Classic Exhibition", "Minor League Baseball"):
        assert not frozen.qualifies({other}), other


# -- The committed artifact --------------------------------------------------


def test_v1_artifact_hash_is_checked(tmp_path, monkeypatch):
    u = AppendOnlyUniverse(tmp_path)
    assert len(u.members) == V1_COUNT
    assert u.identity()["collection_universe_v1_sha256"] == frozen.UNIVERSE_V1_SHA256

    monkeypatch.setattr(frozen, "UNIVERSE_V1_SHA256", "0" * 64)
    with pytest.raises(UniverseError, match="published as"):
        AppendOnlyUniverse(tmp_path / "other")


# -- Discovery has no prefilter ----------------------------------------------


def test_discovery_finds_qualifiers_outside_obvious_mlb_namespaces(tmp_path):
    """
    KXCITYMLBEXPAND is tagged Hockey and KXMLBMENTION sits in category Mentions;
    both carry "Pro Baseball". A ticker, tag or category prefilter would lose
    them, so the census evaluates the predicate over the entire catalog.
    """
    api = FakeAPI(
        catalog=["ZZQUIRKY", "KXHOCKEYISH", "KXNOTHING"],
        events={
            "ZZQUIRKY": [event("ZZQUIRKY", "Pro Baseball")],
            "KXHOCKEYISH": [event("KXHOCKEYISH", "Pro Baseball")],
            "KXNOTHING": [event("KXNOTHING", "Korea KBO")],
        },
    )
    result = run_discovery_census(api, threads=2)
    assert result.complete
    assert set(result.qualifying()) == {"ZZQUIRKY", "KXHOCKEYISH"}
    # Every series in the catalog was probed -- nothing was filtered out first.
    probed = {p for (path, p) in
              [(c[0], c[1].get("series_ticker")) for c in api.calls] if path}
    assert {"ZZQUIRKY", "KXHOCKEYISH", "KXNOTHING"} <= probed


def test_existential_over_events_not_universal(tmp_path):
    """A series with some null-competition events and one Pro Baseball event
    qualifies -- all 30 KXMLBWINS-* series look exactly like this."""
    api = FakeAPI(
        catalog=["KXMLBWINSLIKE"],
        events={"KXMLBWINSLIKE": [
            event("KXMLBWINSLIKE", None, 1),
            event("KXMLBWINSLIKE", None, 2),
            event("KXMLBWINSLIKE", "Pro Baseball", 3),
        ]},
    )
    result = run_discovery_census(api, threads=1)
    assert result.complete and "KXMLBWINSLIKE" in result.qualifying()


# -- Frozen Rule 1 -----------------------------------------------------------


def test_rule_1_admits_a_newly_qualifying_series_with_full_evidence(tmp_path):
    api = FakeAPI(
        catalog=["KXMLBWS"],
        events={"KXMLBWS": [event("KXMLBWS", "Pro Baseball", 7)]},
    )
    u = AppendOnlyUniverse(tmp_path)
    assert "KXMLBWS" not in u.members  # quarantined in v1: no event metadata yet

    change = u.apply_census(run_discovery_census(api, threads=1))
    assert change.applied and change.added == ["KXMLBWS"]
    assert len(u.members) == V1_COUNT + 1
    assert u.version == 2 and u.addition_log_position == 1

    line = json.loads(
        (tmp_path / "universe" / "universe_additions.jsonl")
        .read_text(encoding="utf-8").strip()
    )
    assert line["series_ticker"] == "KXMLBWS"
    assert line["discovery_utc"] and line["discovery_ts"] > 0
    assert line["qualifying_event_tickers"] == ["KXMLBWS-EV07"]
    assert "/events?series_ticker=KXMLBWS" in line["api_endpoint"]
    assert line["predicate_bearing_metadata"]["matched_value"] == "Pro Baseball"
    assert line["backfill"].startswith("none")

    evidence = tmp_path / line["evidence_ref"]
    assert evidence.exists()
    assert frozen.sha256_text(evidence.read_text(encoding="utf-8")) == line["evidence_sha256"]


def test_admission_survives_a_restart_by_replaying_the_log(tmp_path):
    api = FakeAPI(catalog=["KXNEWMLB"],
                  events={"KXNEWMLB": [event("KXNEWMLB", "Pro Baseball")]})
    AppendOnlyUniverse(tmp_path).apply_census(run_discovery_census(api, threads=1))

    reopened = AppendOnlyUniverse(tmp_path)
    assert "KXNEWMLB" in reopened.members
    assert reopened.version == 2 and len(reopened.members) == V1_COUNT + 1


def test_state_file_cannot_shrink_the_universe(tmp_path):
    """The addition log is the record of authority; a truncated index cannot
    quietly evict a member."""
    api = FakeAPI(catalog=["KXNEWMLB"],
                  events={"KXNEWMLB": [event("KXNEWMLB", "Pro Baseball")]})
    AppendOnlyUniverse(tmp_path).apply_census(run_discovery_census(api, threads=1))
    (tmp_path / "universe" / "universe_state.json").write_text("{}", encoding="utf-8")
    assert "KXNEWMLB" in AppendOnlyUniverse(tmp_path).members


# -- Frozen Rule 2 -----------------------------------------------------------


def test_inclusion_is_append_only_after_a_later_metadata_change(tmp_path):
    api = FakeAPI(catalog=["KXNEWMLB"],
                  events={"KXNEWMLB": [event("KXNEWMLB", "Pro Baseball")]})
    u = AppendOnlyUniverse(tmp_path)
    u.apply_census(run_discovery_census(api, threads=1))
    assert "KXNEWMLB" in u.members
    before = len(u.members)

    # Kalshi relabels the series afterwards. It stays in.
    api.events["KXNEWMLB"] = [event("KXNEWMLB", "Korea KBO")]
    change = u.apply_census(run_discovery_census(api, threads=1))
    assert change.applied
    assert change.added == []
    assert "KXNEWMLB" in change.observations
    assert "KXNEWMLB" in u.members and len(u.members) == before

    obs = [json.loads(x) for x in (tmp_path / "universe" / "universe_observations.jsonl")
           .read_text(encoding="utf-8").strip().splitlines()]
    kinds = {o["kind"] for o in obs}
    assert "member_no_longer_satisfies_predicate" in kinds
    assert all("append-only" in o.get("action", "") for o in obs
               if o["kind"] == "member_no_longer_satisfies_predicate")


# -- Gate 3 ------------------------------------------------------------------


def test_incomplete_census_applies_zero_changes(tmp_path):
    api = FakeAPI(
        catalog=["KXNEWMLB", "KXFLAKY"],
        events={
            "KXNEWMLB": [event("KXNEWMLB", "Pro Baseball")],
            "KXFLAKY": [event("KXFLAKY", "Pro Baseball")],
        },
        unreadable={"KXFLAKY"},
    )
    u = AppendOnlyUniverse(tmp_path)
    result = run_discovery_census(api, threads=2)

    assert result.status == frozen.CENSUS_INCOMPLETE
    assert result.unreadable == ["KXFLAKY"]

    change = u.apply_census(result)
    assert change.applied is False
    assert change.added == [] and change.observations == []
    # KXNEWMLB qualified in this very census and is still NOT admitted: an
    # incomplete census may not alter the universe at all.
    assert "KXNEWMLB" not in u.members
    assert len(u.members) == V1_COUNT
    assert not (tmp_path / "universe" / "universe_additions.jsonl").exists()


def test_later_complete_census_recovers_the_unreadable_series(tmp_path):
    api = FakeAPI(
        catalog=["KXNEWMLB", "KXFLAKY"],
        events={
            "KXNEWMLB": [event("KXNEWMLB", "Pro Baseball")],
            "KXFLAKY": [event("KXFLAKY", "Pro Baseball", 3)],
        },
        unreadable={"KXFLAKY"},
    )
    u = AppendOnlyUniverse(tmp_path)
    u.apply_census(run_discovery_census(api, threads=2))
    assert len(u.members) == V1_COUNT

    api.unreadable.clear()  # the transient failure clears
    change = u.apply_census(run_discovery_census(api, threads=2))
    assert change.applied
    assert change.added == ["KXFLAKY", "KXNEWMLB"]
    assert len(u.members) == V1_COUNT + 2
    assert u.addition_log_position == 2


def test_an_incomplete_census_is_still_recorded(tmp_path):
    api = FakeAPI(catalog=["KXFLAKY"], events={}, unreadable={"KXFLAKY"})
    u = AppendOnlyUniverse(tmp_path)
    result = run_discovery_census(api, threads=1)
    u.apply_census(result)

    rec = result.record()
    assert rec["status"] == frozen.CENSUS_INCOMPLETE
    assert rec["gate_3_complete_evidence"] == frozen.CENSUS_INCOMPLETE
    assert rec["series_unreadable"] == 1

    obs = [json.loads(x) for x in (tmp_path / "universe" / "universe_observations.jsonl")
           .read_text(encoding="utf-8").strip().splitlines()]
    assert obs[0]["kind"] == "census_incomplete_no_changes_applied"


def test_catalog_failure_is_incomplete_not_an_exception(tmp_path):
    class Broken(FakeAPI):
        def get_raw(self, path, params=None):
            if path == "/series":
                raise RuntimeError("boom")
            return super().get_raw(path, params)

    result = run_discovery_census(Broken(catalog=[]), threads=1)
    assert result.status == frozen.CENSUS_INCOMPLETE
    assert "boom" in result.error
