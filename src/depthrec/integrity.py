"""
Pinned-classifier integrity. Fail closed before a single row is recorded.

The firing classifier spans two pinned sources and they need different checks:

  scripts/market_calibration_study.py
      candle_at_or_before, sort_candles, yes_price_from_candle, sample_market,
      the fee helpers. This file has not changed since the KILL_CRITERIA.md
      section 2 pin, so it is checked as a WHOLE FILE: Git blob id and SHA-256.

  scripts/test_longshot_rule.py
      favorite_side, qualifying, favorite_trades. A whole-file hash is WRONG
      here: the file received unrelated post-freeze diagnostic and display
      changes (413d88b, e4391ac) while the firing functions stayed semantically
      frozen. A whole-file check would fail on those and invite someone to
      "refresh" the expected value, which is how a pin quietly stops pinning.
      Instead we hash the NORMALISED AST of each protected element plus the
      transitive closure of every module-level constant, helper, class and
      import those elements reference.

Normalisation drops formatting, comments and docstrings. It does NOT drop
default argument values: `def f(x, coefficient=0.07)` and
`def f(x, coefficient=0.08)` hash differently, which is the entire point.

The expected values live in depthrec.pinned_hashes, generated ONCE by
scripts/depthrec_gen_pinned_hashes.py against the section 2 frozen commit and
committed. They are never regenerated from HEAD at recorder startup -- a check
that derives its expectation from the code it is checking verifies nothing.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

STUDY_PATH = REPO_ROOT / "scripts" / "market_calibration_study.py"
LONGSHOT_PATH = REPO_ROOT / "scripts" / "test_longshot_rule.py"

PROTECTED_FUNCTIONS = ("favorite_side", "qualifying", "favorite_trades")
"""The firing functions in scripts/test_longshot_rule.py."""

PROTECTED_CONSTANTS = (
    "_THRESHOLDS",
    "_BUCKET_WIDTH",
    "_MIN_INSAMPLE_TRADES",
    "_CONTRACTS_PER_ORDER",
    "_FEE_COEFFICIENT",
)
"""Named explicitly in the recorder's brief. Some are not reachable from the
three protected functions (_THRESHOLDS, _BUCKET_WIDTH, _MIN_INSAMPLE_TRADES
govern the sweep, not the frozen rule) but they are pinned anyway: a change to
them means the file's rule-selection semantics moved, and the recorder should
stop and let a human look."""

PROTECTED_SEEDS = PROTECTED_FUNCTIONS + PROTECTED_CONSTANTS

NORMALISATION_VERSION = "depthrec-ast-1"
"""Bumping this invalidates every committed hash on purpose. Do not bump it to
make a failing check pass."""


class IntegrityError(RuntimeError):
    """A pinned source differs from its frozen expected version."""


# -- Whole-file hashing -------------------------------------------------------


def git_blob_id(data: bytes) -> str:
    """
    Git's own blob object id for `data` -- sha1("blob <len>\\0" + data).

    Computed locally rather than shelled out to `git hash-object` so the check
    works on a worktree with no git binary and cannot be fooled by a dirty
    index. Matches `git rev-parse HEAD:<path>` for an unmodified file.
    """
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def normalise_eol(data: bytes) -> bytes:
    """
    CRLF -> LF. Nothing else.

    This repo is checked out with core.autocrlf=true, so the working copy of a
    pinned script is byte-for-byte CRLF while the Git blob it came from is LF.
    Hashing the raw disk bytes would therefore fail the pin on every Windows
    checkout -- a false alarm that would train someone to regenerate the
    expected values, which is exactly the failure mode this module exists to
    prevent. Normalising to LF reproduces the Git blob id exactly, on any
    platform, and cannot hide a change to any Python token.
    """
    return data.replace(b"\r\n", b"\n")


def file_digests(path: Path) -> dict[str, str]:
    """
    Digests for a whole-file pin, over LF-normalised content.

    `raw_sha256` is the unnormalised disk content, reported but never compared:
    it differs by checkout platform and is only there so a log line records
    what was literally on disk.
    """
    raw = path.read_bytes()
    data = normalise_eol(raw)
    try:
        shown = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        # A path outside the repo -- a test fixture, or a checkout somewhere
        # unexpected. Report it in full rather than crash on the tidy label.
        shown = str(path)
    return {
        "path": shown,
        "bytes": str(len(data)),
        "git_blob": git_blob_id(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }


# -- AST normalisation and the protected-element closure ----------------------


def _top_level_index(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """
    Module-level name -> the node(s) that bind it.

    A list, not a single node, because a name can legitimately be bound more
    than once at module level; every binding is hashed, so a second assignment
    that shadows the first cannot slip past.
    """
    index: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            index.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    index.setdefault(target.id, []).append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            index.setdefault(node.target.id, []).append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                index.setdefault(bound, []).append(node)
    return index


def _referenced_names(node: ast.AST) -> set[str]:
    """
    Every bare name and attribute name mentioned anywhere inside `node`.

    Attribute names are included so `market_calibration_study.fee_per_contract`
    pulls in the import that binds the module, and so a helper reached through
    an attribute is not missed. Over-collecting is safe: names that are not
    module-level simply do not resolve.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def protected_closure(tree: ast.Module, seeds: tuple[str, ...]) -> dict[str, list[ast.AST]]:
    """
    Transitive closure over module-level bindings reachable from `seeds`.

    Fixed point over: start with the seeds, and for every node already in the
    set add every module-level name it references. Terminates because the
    module-level name set is finite and names are never removed.
    """
    index = _top_level_index(tree)
    collected: dict[str, list[ast.AST]] = {}
    pending = list(seeds)
    while pending:
        name = pending.pop()
        if name in collected or name not in index:
            continue
        nodes = index[name]
        collected[name] = nodes
        for node in nodes:
            for ref in _referenced_names(node):
                if ref not in collected and ref in index:
                    pending.append(ref)
    return collected


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """
    A deep copy of `node` with every docstring removed.

    Comments never reach the AST at all, so dropping docstrings is the whole of
    "normalise away comments and docstrings". Nothing else about the body is
    touched -- in particular default argument values, constant values and
    operator structure all survive into the hash.
    """
    clone = copy.deepcopy(node)
    for child in ast.walk(clone):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(child, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                child.body = body[1:] or [ast.Pass()]
    return clone


def _normalised_dump(node: ast.AST) -> str:
    """
    Structural, formatting-free serialisation of one protected element.

    ast.dump with include_attributes=False drops line numbers and column
    offsets, so reindenting or rewrapping a function does not change the hash.
    Field names are annotated so a positional-vs-keyword restructuring of the
    tree cannot collide.
    """
    return ast.dump(_strip_docstrings(node), annotate_fields=True, include_attributes=False)


def _element_hash(node: ast.AST) -> str:
    return hashlib.sha256(_normalised_dump(node).encode("utf-8")).hexdigest()


def protected_manifest(source: str) -> dict[str, list[str]]:
    """
    element name -> [hash per binding], for every element in the closure.

    Per-element rather than one aggregate hash so a failure can name exactly
    which function or constant moved.
    """
    tree = ast.parse(source)
    closure = protected_closure(tree, PROTECTED_SEEDS)
    return {name: [_element_hash(n) for n in nodes] for name, nodes in sorted(closure.items())}


def manifest_digest(manifest: dict[str, list[str]]) -> str:
    """One SHA-256 over the whole manifest, for a single-line log field."""
    payload = NORMALISATION_VERSION + "\n" + "\n".join(
        f"{name}:{','.join(hashes)}" for name, hashes in sorted(manifest.items())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def protected_sources(source: str) -> dict[str, list[str]]:
    """
    element name -> [normalised source text], the human-readable audit trail.

    Committed alongside the hashes so a reviewer can see WHAT was pinned
    without re-running anything. Not used by the check itself: ast.unparse
    output can shift between Python versions, and a cosmetic shift there must
    not be able to fail a recorder.
    """
    tree = ast.parse(source)
    closure = protected_closure(tree, PROTECTED_SEEDS)
    return {
        name: [ast.unparse(_strip_docstrings(n)) for n in nodes]
        for name, nodes in sorted(closure.items())
    }


# -- Verification -------------------------------------------------------------


@dataclass
class IntegrityReport:
    """What the check found. `ok` False means the recorder must not start."""

    ok: bool
    study: dict[str, str]
    longshot_manifest: dict[str, list[str]]
    longshot_digest: str
    problems: list[str] = field(default_factory=list)
    python_version: str = field(default_factory=lambda: ".".join(map(str, sys.version_info[:3])))

    def log_fields(self) -> dict[str, object]:
        return {
            "study_git_blob": self.study["git_blob"],
            "study_sha256": self.study["sha256"],
            "longshot_manifest_digest": self.longshot_digest,
            "longshot_elements": len(self.longshot_manifest),
            "normalisation": NORMALISATION_VERSION,
            "python_version": self.python_version,
            "ok": self.ok,
        }


def verify(
    *,
    expected_study: dict[str, str],
    expected_longshot_manifest: dict[str, list[str]],
    study_path: Path = STUDY_PATH,
    longshot_path: Path = LONGSHOT_PATH,
) -> IntegrityReport:
    """
    Compare both pinned sources against their committed expected versions.

    Never raises for a mismatch -- it returns a report naming every difference,
    so a caller can log the whole picture before refusing to start. Only a
    missing or unparseable file raises.
    """
    problems: list[str] = []

    study = file_digests(study_path)
    for key in ("git_blob", "sha256"):
        want = expected_study.get(key)
        if want is None:
            problems.append(f"expected_study is missing {key!r}")
        elif study[key] != want:
            problems.append(
                f"{study['path']}: {key} is {study[key]} but the frozen expected "
                f"version is {want} -- this file is pinned WHOLE and has changed"
            )

    manifest = protected_manifest(longshot_path.read_text(encoding="utf-8"))
    digest = manifest_digest(manifest)

    missing = sorted(set(expected_longshot_manifest) - set(manifest))
    added = sorted(set(manifest) - set(expected_longshot_manifest))
    for name in missing:
        problems.append(
            f"{longshot_path.name}: protected element {name!r} is gone from the "
            f"closure -- the firing code no longer references what it used to"
        )
    for name in added:
        problems.append(
            f"{longshot_path.name}: protected element {name!r} appeared in the "
            f"closure -- the firing code now references something new"
        )
    for name in sorted(set(manifest) & set(expected_longshot_manifest)):
        if manifest[name] != expected_longshot_manifest[name]:
            problems.append(
                f"{longshot_path.name}: protected element {name!r} differs from "
                f"the frozen expected version "
                f"(got {manifest[name]}, expected {expected_longshot_manifest[name]})"
            )

    for seed in PROTECTED_SEEDS:
        if seed not in manifest:
            problems.append(
                f"{longshot_path.name}: seed {seed!r} is not defined at module "
                f"level -- the classifier cannot be pinned"
            )

    return IntegrityReport(
        ok=not problems,
        study=study,
        longshot_manifest=manifest,
        longshot_digest=digest,
        problems=problems,
    )


def verify_or_raise(
    *,
    expected_study: dict[str, str],
    expected_longshot_manifest: dict[str, list[str]],
    study_path: Path = STUDY_PATH,
    longshot_path: Path = LONGSHOT_PATH,
) -> IntegrityReport:
    """
    verify(), raising IntegrityError on any difference. Fail closed.

    The message deliberately does not suggest regenerating the expected values.
    If the pinned classifier really did move, the correct response is to stop
    and decide whether the research is still measuring what it claims to.
    """
    report = verify(
        expected_study=expected_study,
        expected_longshot_manifest=expected_longshot_manifest,
        study_path=study_path,
        longshot_path=longshot_path,
    )
    if not report.ok:
        raise IntegrityError(
            "pinned classifier integrity check FAILED; refusing to record.\n  "
            + "\n  ".join(report.problems)
        )
    return report


# -- Recorder provenance ------------------------------------------------------


def recorder_git_commit() -> str:
    """
    The recorder's own Git commit, stamped into every persisted row (section 7).

    Returns "<sha>-dirty" when the worktree has uncommitted changes, and
    "unknown" when git is unavailable. A dirty marker is not fatal here --
    section 10 governs when a version change is allowed -- but it is recorded so
    a row can never claim provenance it does not have.
    """
    try:
        sha = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if not sha:
        return "unknown"
    return f"{sha}-dirty" if dirty else sha
