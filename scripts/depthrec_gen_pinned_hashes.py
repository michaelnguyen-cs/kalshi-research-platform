#!/usr/bin/env python
"""
Generate src/depthrec/pinned_hashes.py from the KILL_CRITERIA.md section 2
frozen commit. Run ONCE. Do not wire this into recorder startup.

    python scripts/depthrec_gen_pinned_hashes.py --write

The expected values must come from the frozen commit's trees, never from the
working copy. A check whose expectation is regenerated from HEAD tautologically
verifies whatever code happens to be installed -- it would pass on a classifier
someone rewrote this morning. So this utility reads both pinned files out of
`git show <commit>:<path>` and refuses to look at disk.

There is one deliberate asymmetry, and it is the whole reason the two files are
handled differently:

  market_calibration_study.py is pinned WHOLE (git blob + sha256). It is
  byte-identical at the frozen commit and at HEAD, so a whole-file pin costs
  nothing and catches everything.

  test_longshot_rule.py is pinned ELEMENT-WISE. It legitimately changed after
  the freeze (413d88b, e4391ac added clustered-inference diagnostics and
  display) while every firing function stayed semantically frozen. This utility
  hashes the frozen commit's version of the protected elements; the recorder
  hashes the working copy's; they must match. Both are printed so the
  difference is visible rather than asserted.

READ-ONLY. Touches git and, with --write, one generated file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import integrity  # noqa: E402

# KILL_CRITERIA.md section 2 code pin. Also the commit the frozen mechanics
# document names as the classifier's provenance.
FROZEN_COMMIT = "6ca4101"

OUTPUT = Path(__file__).resolve().parents[1] / "src" / "depthrec" / "pinned_hashes.py"

_STUDY_REL = "scripts/market_calibration_study.py"
_LONGSHOT_REL = "scripts/test_longshot_rule.py"


def git_show(commit: str, rel_path: str) -> bytes:
    """Raw bytes of `rel_path` as of `commit`. Bytes, not text: the git blob id
    and the sha256 are both over bytes, and a text round-trip through the
    platform newline convention would change both on Windows."""
    proc = subprocess.run(
        ["git", "-C", str(integrity.REPO_ROOT), "show", f"{commit}:{rel_path}"],
        capture_output=True, check=True,
    )
    return proc.stdout


def resolve(commit: str) -> str:
    return subprocess.run(
        ["git", "-C", str(integrity.REPO_ROOT), "rev-parse", commit],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def render(payload: dict) -> str:
    body = json.dumps(payload, indent=4, sort_keys=True)
    generated = payload["generated_utc"]
    commit = payload["frozen_commit"]
    return f'''"""
Expected pinned-classifier hashes. GENERATED -- do not hand-edit.

Produced by scripts/depthrec_gen_pinned_hashes.py against the KILL_CRITERIA.md
section 2 frozen commit {commit} on {generated}.

These values are committed constants. depthrec.integrity compares the installed
classifier against them; it never recomputes them from HEAD. If a check fails,
the pinned classifier moved -- that is a finding to investigate, not a reason to
re-run the generator.

PINNED_SOURCES below is the human-readable audit trail: the normalised source of
every protected element as it stood at the frozen commit. It is not used by the
check (ast.unparse output can shift between Python versions and a cosmetic shift
must not be able to fail a recorder); it is there so a reviewer can see what was
pinned without running anything.
"""

from __future__ import annotations

_MANIFEST = {body}

FROZEN_COMMIT: str = _MANIFEST["frozen_commit"]
GENERATED_UTC: str = _MANIFEST["generated_utc"]
GENERATOR_PYTHON: str = _MANIFEST["generator_python"]
NORMALISATION_VERSION: str = _MANIFEST["normalisation_version"]

EXPECTED_STUDY: dict[str, str] = _MANIFEST["expected_study"]
"""Whole-file pin for scripts/market_calibration_study.py."""

EXPECTED_LONGSHOT_MANIFEST: dict[str, list[str]] = _MANIFEST["expected_longshot_manifest"]
"""Per-element pin for the protected functions in scripts/test_longshot_rule.py
plus the transitive closure of everything they reference."""

EXPECTED_LONGSHOT_DIGEST: str = _MANIFEST["expected_longshot_digest"]
"""One digest over the whole manifest, for a single-line startup log field."""

PROTECTED_SEEDS: list[str] = _MANIFEST["protected_seeds"]
PINNED_SOURCES: dict[str, list[str]] = _MANIFEST["pinned_sources"]
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", default=FROZEN_COMMIT,
                    help=f"frozen commit to pin against (default {FROZEN_COMMIT})")
    ap.add_argument("--write", action="store_true",
                    help=f"write {OUTPUT.name} (default: print a diff-able summary only)")
    args = ap.parse_args()

    commit_full = resolve(args.commit)

    study_bytes = git_show(args.commit, _STUDY_REL)
    longshot_bytes = git_show(args.commit, _LONGSHOT_REL)

    # LF-normalised, matching what the recorder hashes off disk. `git show`
    # already emits LF here, but normalising explicitly means the generator and
    # the checker cannot drift apart on a differently configured checkout.
    study_norm = integrity.normalise_eol(study_bytes)
    expected_study = {
        "path": _STUDY_REL,
        "bytes": str(len(study_norm)),
        "git_blob": integrity.git_blob_id(study_norm),
        "sha256": hashlib.sha256(study_norm).hexdigest(),
    }

    longshot_src = longshot_bytes.decode("utf-8")
    manifest = integrity.protected_manifest(longshot_src)
    digest = integrity.manifest_digest(manifest)
    sources = integrity.protected_sources(longshot_src)

    payload = {
        "frozen_commit": commit_full,
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "generator_python": ".".join(map(str, sys.version_info[:3])),
        "normalisation_version": integrity.NORMALISATION_VERSION,
        "expected_study": expected_study,
        "expected_longshot_manifest": manifest,
        "expected_longshot_digest": digest,
        "protected_seeds": list(integrity.PROTECTED_SEEDS),
        "pinned_sources": sources,
    }

    print(f"frozen commit      : {commit_full}")
    print(f"{_STUDY_REL}")
    print(f"  git blob         : {expected_study['git_blob']}")
    print(f"  sha256           : {expected_study['sha256']}")
    print(f"{_LONGSHOT_REL}")
    print(f"  manifest digest  : {digest}")
    print(f"  protected closure: {len(manifest)} elements")
    for name, hashes in manifest.items():
        seed = " (seed)" if name in integrity.PROTECTED_SEEDS else ""
        print(f"    {name:22s} {','.join(h[:12] for h in hashes)}{seed}")

    # The point of the element-wise scheme, demonstrated rather than asserted:
    # show that the working copy's protected elements still match the frozen
    # commit's even though the file as a whole does not.
    head_src = integrity.LONGSHOT_PATH.read_text(encoding="utf-8")
    head_manifest = integrity.protected_manifest(head_src)
    same_file = integrity.git_blob_id(head_src.encode("utf-8")) == integrity.git_blob_id(longshot_bytes)
    print(f"\nworking copy vs frozen commit:")
    print(f"  whole file identical      : {same_file}")
    print(f"  protected elements identical: {head_manifest == manifest}")
    if head_manifest != manifest:
        for name in sorted(set(head_manifest) | set(manifest)):
            if head_manifest.get(name) != manifest.get(name):
                print(f"    DIFFERS: {name}")

    if args.write:
        OUTPUT.write_text(render(payload), encoding="utf-8")
        print(f"\nwrote {OUTPUT}")
    else:
        print(f"\n(dry run; pass --write to update {OUTPUT.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
