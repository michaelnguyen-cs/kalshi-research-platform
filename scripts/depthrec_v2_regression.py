#!/usr/bin/env python
"""
Field-by-field regression of the streaming section 8-v2 evaluator against a
frozen formal-run result.

    python scripts/depthrec_v2_regression.py \
        --archive /mnt/depthrec/S8V2_FORMAL_20260823_IMMUTABLE.sqlite \
        --baseline data/s8v2_verdict_20260823.txt \
        --sha256 eb8e27a6d3ed601063a6bae6ceacb51f76261b89588d2059f515a968d6a8b182

WHAT THIS IS FOR

The streaming evaluator is a refactor of storage and iteration only. Its claim
is not "close enough", it is that for the same archive it produces the SAME
complete structured result as the frozen evaluator. That claim is only worth
something if it is checked mechanically, so this compares the two parsed JSON
objects recursively and exits non-zero on any difference, naming the path of
every field that differs.

WHY IT PARSES RATHER THAN DIFFING TEXT

The committed baselines are the frozen evaluator's whole stdout: a protocol
banner, a one-line summary, the JSON result, then a human-readable section 12
report. The banner and the report are formatting, and `run_id` aside the JSON is
the artefact. So the JSON object is located inside the file, parsed, and compared
as a structure. Nothing outside it is compared and nothing inside it is exempt.

THE BASELINES ARE NOT REGENERATED. This reads the committed files. It never
runs the frozen evaluator and never writes to the archive; the archive is opened
read-only and its SHA-256 can be checked here before anything else happens.

Exit codes: 0 identical, 1 one or more fields differ, 3 could not run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec.equivalence_v2 import evaluate_archive  # noqa: E402

_MAX_REPORTED = 200


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def extract_json(text: str) -> dict[str, Any]:
    """
    The structured result out of a verdict file.

    The frozen CLI prints it with `json.dumps(..., indent=2)`, so it is the first
    line in the file that is a bare `{`. `raw_decode` then stops exactly at the
    end of that object, which is why the trailing human-readable section 12
    report does not have to be stripped first. A file that is nothing but the
    JSON works too.
    """
    decoder = json.JSONDecoder()
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.strip().lstrip("﻿") == "{":
            obj, _end = decoder.raw_decode(text, pos + line.index("{"))
            if not isinstance(obj, dict):
                raise ValueError("the JSON found is not an object")
            return obj
        pos += len(line)
    raise ValueError("no JSON object found in the file")


def normalise(result_dict: dict[str, Any]) -> dict[str, Any]:
    """
    A live result as JSON sees it.

    `as_dict()` holds Python tuples (anomalous_markets) and sets nothing; the
    baseline holds whatever `json.dumps(..., default=str)` made of them. Round
    tripping through the same encoder is what makes the comparison a comparison
    of the artefact rather than of Python types.
    """
    return json.loads(json.dumps(result_dict, default=str))


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Every leaf on which `a` and `b` disagree, by path. `a` is the new result."""
    where = path or "<root>"
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
        and not isinstance(a, bool) and not isinstance(b, bool)
    ):
        return [f"{where}: type {type(a).__name__} != {type(b).__name__} "
                f"(new={a!r}, baseline={b!r})"]
    if isinstance(a, dict):
        out: list[str] = []
        for k in a:
            if k not in b:
                out.append(f"{where}.{k}: present in new, absent from baseline")
        for k in b:
            if k not in a:
                out.append(f"{where}.{k}: absent from new, present in baseline")
        for k in a:
            if k in b:
                out.extend(diff(a[k], b[k], f"{path}.{k}" if path else str(k)))
        return out
    if isinstance(a, list):
        out = []
        if len(a) != len(b):
            out.append(f"{where}: length {len(a)} != {len(b)}")
        for i in range(min(len(a), len(b))):
            out.extend(diff(a[i], b[i], f"{path}[{i}]"))
        return out
    if a != b:
        return [f"{where}: new={a!r} baseline={b!r}"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", type=Path, required=True,
                    help="the immutable archive to evaluate")
    ap.add_argument("--baseline", type=Path, required=True,
                    help="the committed frozen-evaluator verdict file")
    ap.add_argument("--run-id", default=None,
                    help="evaluate one run inside a multi-run archive")
    ap.add_argument("--sha256", default=None,
                    help="fail before evaluating unless the archive hashes to this")
    ap.add_argument("--emit", type=Path, default=None,
                    help="write the new evaluator's JSON result here")
    args = ap.parse_args()

    if not args.archive.exists():
        print(f"no such archive: {args.archive}", file=sys.stderr)
        return 3
    if not args.baseline.exists():
        print(f"no such baseline: {args.baseline}", file=sys.stderr)
        return 3

    if args.sha256:
        print(f"hashing {args.archive} ...", flush=True)
        got = sha256_file(args.archive)
        print(f"archive sha256      : {got}")
        if got.lower() != args.sha256.strip().lower():
            print(f"ARCHIVE HASH MISMATCH: expected {args.sha256}", file=sys.stderr)
            return 3
        print("archive sha256      : matches the pinned value")

    try:
        baseline = extract_json(args.baseline.read_text(encoding="utf-8",
                                                        errors="replace"))
    except Exception as exc:  # noqa: BLE001
        print(f"could not read the baseline result: {exc}", file=sys.stderr)
        return 3

    t0 = time.time()
    try:
        result = evaluate_archive(args.archive, args.run_id)
    except Exception as exc:  # noqa: BLE001
        print(f"EVALUATION FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    elapsed = time.time() - t0

    new = normalise(result.as_dict())
    if args.emit:
        args.emit.write_text(json.dumps(new, indent=2), encoding="utf-8")

    print()
    print(f"elapsed             : {elapsed:.1f} s")
    print(f"new verdict         : {result.verdict}")
    print(f"baseline verdict    : {baseline.get('verdict')}")
    print(f"new paired events   : {result.paired_events:,}")
    print(f"baseline paired     : {baseline.get('paired_events')}")
    print(f"baseline file       : {args.baseline}")
    print()

    differences = diff(new, baseline)
    if not differences:
        print(f"IDENTICAL: all {len(new)} top-level fields and everything under "
              f"them match the frozen result field for field.")
        return 0

    print(f"DIFFERENT: {len(differences)} field(s) differ.")
    for line in differences[:_MAX_REPORTED]:
        print(f"  {line}")
    if len(differences) > _MAX_REPORTED:
        print(f"  ... {len(differences) - _MAX_REPORTED} more")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
