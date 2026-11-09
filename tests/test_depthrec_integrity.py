"""
Pinned-classifier integrity, and the frozen configuration no hash can cover.

The two checks are asymmetric on purpose:

  market_calibration_study.py   whole-file (git blob + sha256). Byte-identical
                                at the section 2 frozen commit and at HEAD.
  test_longshot_rule.py         element-wise over the protected functions plus
                                the transitive closure of what they reference.
                                The file legitimately changed after the freeze
                                (413d88b, e4391ac) while the firing functions
                                stayed semantically frozen; a whole-file hash
                                would fail on those and invite someone to
                                "refresh" the expected value.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import frozen, integrity  # noqa: E402
from depthrec import pinned_hashes as ph  # noqa: E402

REPO = integrity.REPO_ROOT
FROZEN_COMMIT = "6ca4101"


def git_show(rel: str, commit: str = FROZEN_COMMIT) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), "show", f"{commit}:{rel}"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")


# -- The installed classifier ------------------------------------------------

def test_the_installed_classifier_matches_its_committed_expected_version():
    report = integrity.verify_or_raise(
        expected_study=ph.EXPECTED_STUDY,
        expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST,
    )
    assert report.ok
    assert report.longshot_digest == ph.EXPECTED_LONGSHOT_DIGEST


def test_the_study_pin_is_the_blob_named_in_the_brief():
    assert ph.EXPECTED_STUDY["git_blob"].startswith("e8dd06f")
    assert integrity.file_digests(integrity.STUDY_PATH)["git_blob"] == \
        ph.EXPECTED_STUDY["git_blob"]


def test_expected_values_were_generated_against_the_section_2_frozen_commit():
    full = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", FROZEN_COMMIT],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert ph.FROZEN_COMMIT == full, (
        "the expected hashes must come from the frozen commit, never from HEAD"
    )


def test_line_ending_normalisation_reproduces_the_git_blob():
    """
    This repo checks out CRLF (core.autocrlf=true) while the blob is LF. Hashing
    raw disk bytes would fail the pin on every Windows checkout -- a false alarm
    that trains people to regenerate the expected values.
    """
    disk = integrity.STUDY_PATH.read_bytes()
    assert b"\r\n" in disk or b"\r\n" not in disk        # either checkout style
    normalised = integrity.normalise_eol(disk)
    assert b"\r\n" not in normalised
    assert integrity.git_blob_id(normalised) == ph.EXPECTED_STUDY["git_blob"]


# -- The element-wise scheme -------------------------------------------------

def test_protected_elements_are_identical_at_the_frozen_commit_and_at_head():
    """The claim the whole element-wise scheme rests on."""
    frozen_manifest = integrity.protected_manifest(git_show("scripts/test_longshot_rule.py"))
    head_manifest = integrity.protected_manifest(
        integrity.LONGSHOT_PATH.read_text(encoding="utf-8")
    )
    assert head_manifest == frozen_manifest


def test_the_whole_longshot_file_really_did_change_after_the_freeze():
    """
    If this ever fails, the element-wise scheme has stopped being necessary --
    but it must not be silently swapped for a whole-file hash without someone
    noticing, so the asymmetry is asserted rather than assumed.
    """
    frozen_src = git_show("scripts/test_longshot_rule.py")
    head_src = integrity.LONGSHOT_PATH.read_text(encoding="utf-8")
    assert integrity.normalise_eol(head_src.encode()) != frozen_src.encode()


def test_the_closure_covers_the_named_constants_and_what_it_reaches():
    manifest = ph.EXPECTED_LONGSHOT_MANIFEST
    for seed in integrity.PROTECTED_SEEDS:
        assert seed in manifest, seed
    # Reached transitively from favorite_trades/qualifying, not named directly.
    for reached in ("side_won", "trade_ev", "fee_per_contract", "Observation"):
        assert reached in manifest, reached


@pytest.mark.parametrize("mutation, description", [
    ("_CONTRACTS_PER_ORDER = 10", "_CONTRACTS_PER_ORDER = 200"),
    ("_FEE_COEFFICIENT = 0.07", "_FEE_COEFFICIENT = 0.08"),
    ("if yes_price >= 0.5:", "if yes_price > 0.5:"),
    ("if entry < threshold:", "if entry <= threshold:"),
    ("_MIN_INSAMPLE_TRADES = 100", "_MIN_INSAMPLE_TRADES = 3"),
])
def test_a_semantic_change_to_any_protected_element_fails_closed(mutation, description):
    src = integrity.LONGSHOT_PATH.read_text(encoding="utf-8")
    assert mutation in src, f"anchor {mutation!r} not found; update the test"
    mutated = integrity.protected_manifest(src.replace(mutation, description, 1))
    assert mutated != ph.EXPECTED_LONGSHOT_MANIFEST, description


def test_a_changed_default_argument_value_fails_closed():
    """Explicitly NOT normalised away: a different default is a different rule."""
    src = integrity.LONGSHOT_PATH.read_text(encoding="utf-8")
    anchor = "def trade_ev(entry: float, won: bool, coefficient: float = _FEE_COEFFICIENT)"
    assert anchor in src
    mutated = integrity.protected_manifest(
        src.replace(anchor, "def trade_ev(entry: float, won: bool, coefficient: float = 0.09)", 1)
    )
    assert mutated["trade_ev"] != ph.EXPECTED_LONGSHOT_MANIFEST["trade_ev"]


def test_comments_docstrings_and_formatting_are_normalised_away():
    src = integrity.LONGSHOT_PATH.read_text(encoding="utf-8")
    reformatted = src.replace(
        '''def side_won(side: str, settled_yes: bool) -> bool:
    return settled_yes if side == "YES" else not settled_yes''',
        '''def side_won(side: str, settled_yes: bool) -> bool:
    """A docstring that was not here before."""
    # and a comment
    return (
        settled_yes
        if side == "YES"
        else not settled_yes
    )''',
        1,
    )
    assert reformatted != src
    assert integrity.protected_manifest(reformatted)["side_won"] == \
        ph.EXPECTED_LONGSHOT_MANIFEST["side_won"]


def test_removing_a_protected_element_fails_closed():
    src = integrity.LONGSHOT_PATH.read_text(encoding="utf-8")
    stripped = src.replace("_THRESHOLDS = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]", "", 1)
    manifest = integrity.protected_manifest(stripped)
    assert "_THRESHOLDS" not in manifest


def test_a_changed_study_file_fails_closed(tmp_path):
    doctored = tmp_path / "market_calibration_study.py"
    src = integrity.STUDY_PATH.read_text(encoding="utf-8")
    doctored.write_text(
        src.replace("coefficient: float = 0.07", "coefficient: float = 0.08"),
        encoding="utf-8",
    )
    report = integrity.verify(
        expected_study=ph.EXPECTED_STUDY,
        expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST,
        study_path=doctored,
        longshot_path=integrity.LONGSHOT_PATH,
    )
    assert not report.ok
    assert any("pinned WHOLE and has changed" in p for p in report.problems)


def test_verify_or_raise_refuses_to_start_on_a_mismatch():
    bogus = dict(ph.EXPECTED_STUDY, sha256="0" * 64)
    with pytest.raises(integrity.IntegrityError, match="refusing to record"):
        integrity.verify_or_raise(
            expected_study=bogus,
            expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST,
        )


# -- The three parameters no hash covers ---------------------------------------

def test_the_frozen_three_are_exactly_the_documents_values():
    assert frozen.RULE_THRESHOLD == 0.70
    assert frozen.RULE_SLICE_MINUTES == 7
    assert frozen.X_CONTRACTS == 10
    assert frozen.TARGET_OFFSET_SECONDS == 420
    frozen.assert_frozen_config(rule_threshold=0.70, slice_minutes=7, x_contracts=10)


@pytest.mark.parametrize("kwargs", [
    {"rule_threshold": 0.75, "slice_minutes": 7, "x_contracts": 10},
    {"rule_threshold": 0.70, "slice_minutes": 5, "x_contracts": 10},
    {"rule_threshold": 0.70, "slice_minutes": 7, "x_contracts": 1},
    {"rule_threshold": 0.7000001, "slice_minutes": 7, "x_contracts": 10},
])
def test_any_other_value_fails_closed(kwargs):
    with pytest.raises(frozen.FrozenConfigError):
        frozen.assert_frozen_config(**kwargs)


def test_the_frozen_universe_is_the_twelve_series_in_the_document():
    assert frozen.VALIDATION_UNIVERSE_SERIES == (
        "KXBTC15M", "KXETH15M",
        "KXMLBHRR", "KXMLBTB", "KXMLBHIT", "KXMLBRBI", "KXMLBHR",
        "KXGOLDH", "KXWTIH", "KXSILVERH", "KXPLATINUMH", "KXPALLADIUMH",
    )
    assert frozen.CENSUS_SERIES == ("KXBTC15M", "KXETH15M")


def test_the_equivalence_protocol_is_the_documents():
    assert frozen.EQUIV_MIN_MINUTES == 60
    assert frozen.EQUIV_MAX_MINUTES == 240
    assert frozen.EQUIV_MIN_COMPARISONS == 10_000
    assert frozen.EQUIV_GRID_HZ == 1.0
    assert frozen.EQUIV_SKEW_TOLERANCE_MS == 500
    assert frozen.EQUIV_PASS_AGREEMENT == 0.999


def test_the_four_section_four_reasons_are_all_itemised():
    assert set(frozen.UNAVAILABLE_REASONS) == {
        "UNAVAILABLE_SCHEDULED_MAINTENANCE",
        "UNAVAILABLE_DISCONNECT",
        "UNAVAILABLE_SEQUENCE_GAP",
        "UNAVAILABLE_NO_SNAPSHOT",
        "UNAVAILABLE_SESSION_STALE",
        "UNAVAILABLE_OTHER",
    }
    assert frozen.BUCKETS == ("UNAVAILABLE", "NON_TRADEABLE", "ELIGIBLE")
