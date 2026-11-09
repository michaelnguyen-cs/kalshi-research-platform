# §8-v2 streaming evaluator: VM regression and memory benchmark

Branch `streaming-evaluator`. The §8-v2 evaluator was refactored so its memory is
bounded by one market rather than by the whole archive. §8-v2 is retired; the two
formal archives are being used here only as immutable regression fixtures for the
**old** semantics. Nothing in the protocol, its parameters, its criteria or its
verdicts is changed, and no §8-v3 work belongs on this branch.

Acceptance is **complete JSON equality** with the committed frozen results. The
headline counts below are sanity checks, not the acceptance test.

| | Formal run 1 | Formal run 2 |
|---|---|---|
| archive | `/mnt/depthrec/S8V2_FORMAL_20260823_IMMUTABLE.sqlite` | `/mnt/depthrec/S8V2_FORMAL_RUN2_20260825_IMMUTABLE.sqlite` |
| sha256 | `eb8e27a6d3ed601063a6bae6ceacb51f76261b89588d2059f515a968d6a8b182` | `7537f26257696a3667d153a7fbb9951d6b8c47d38c6c9908617fb8eba9dc39af` |
| baseline | `data/s8v2_verdict_20260823.txt` | `data/s8v2_run2_verdict_20260825.txt` |
| paired events | 2,183,040 | 3,181,043 |
| qualifying markets | 3,036 | 2,033 |
| per-market breaches | 6 | 2 |
| verdict | `INSUFFICIENT_BOUNDARY_EVIDENCE` | `INSUFFICIENT_BOUNDARY_EVIDENCE` |

The baselines are **read**, never regenerated. The frozen evaluator is not run
against these archives again; its complete output is already in git.

## Before anything else: where the spill goes

The first pass writes a temporary `(market -> ws_raw row id)` index and the
second pass reads it back. It must land on **disk**, not on a tmpfs — if `/tmp`
is RAM-backed on this VM, an index the size of the run defeats the whole point of
the refactor.

```bash
export DEPTHREC_V2_SPILL_DIR=/mnt/depthrec/spill      # any disk-backed path
mkdir -p "$DEPTHREC_V2_SPILL_DIR"
findmnt -no FSTYPE --target "$DEPTHREC_V2_SPILL_DIR"   # must not say tmpfs
df -h "$DEPTHREC_V2_SPILL_DIR"
```

Budget roughly **27 bytes per indexed frame**: about 120 MiB for run 1 and about
175 MiB for run 2. The file is deleted before the evaluator returns, on the
failure path too.

## Field-by-field JSON regression

`scripts/depthrec_v2_regression.py` evaluates the archive, pulls the JSON object
out of the committed verdict file, compares the two parsed structures
recursively, prints the path of every differing field and exits non-zero if there
is one. Exit 0 means identical.

Run 1:

```bash
cd /path/to/kalshi-bot && git checkout streaming-evaluator
python scripts/depthrec_v2_regression.py \
  --archive /mnt/depthrec/S8V2_FORMAL_20260823_IMMUTABLE.sqlite \
  --baseline data/s8v2_verdict_20260823.txt \
  --sha256 eb8e27a6d3ed601063a6bae6ceacb51f76261b89588d2059f515a968d6a8b182 \
  --emit /tmp/s8v2_run1_streaming.json
echo "run 1 regression exit: $?"
```

Run 2:

```bash
python scripts/depthrec_v2_regression.py \
  --archive /mnt/depthrec/S8V2_FORMAL_RUN2_20260825_IMMUTABLE.sqlite \
  --baseline data/s8v2_run2_verdict_20260825.txt \
  --sha256 7537f26257696a3667d153a7fbb9951d6b8c47d38c6c9908617fb8eba9dc39af \
  --emit /tmp/s8v2_run2_streaming.json
echo "run 2 regression exit: $?"
```

## Memory benchmark, no swap

Confirm there is no swap first, so the maximum resident set size is a real
ceiling and not a page-out artefact:

```bash
swapon --show        # expect no output
free -m
```

Then the same two commands under `/usr/bin/time -v`. `%M` in its report is the
maximum resident set size in KiB; the full `-v` block also gives elapsed
wall-clock time and the exit status, and the tool prints the verdict and the JSON
equality result on stdout.

Run 1:

```bash
/usr/bin/time -v python scripts/depthrec_v2_regression.py \
  --archive /mnt/depthrec/S8V2_FORMAL_20260823_IMMUTABLE.sqlite \
  --baseline data/s8v2_verdict_20260823.txt \
  2>&1 | tee /tmp/s8v2_run1_time.txt
echo "exit: ${PIPESTATUS[0]}"
```

Run 2:

```bash
/usr/bin/time -v python scripts/depthrec_v2_regression.py \
  --archive /mnt/depthrec/S8V2_FORMAL_RUN2_20260825_IMMUTABLE.sqlite \
  --baseline data/s8v2_run2_verdict_20260825.txt \
  2>&1 | tee /tmp/s8v2_run2_time.txt
echo "exit: ${PIPESTATUS[0]}"
```

Record, per archive: `Maximum resident set size (kbytes)`, `Elapsed (wall clock)
time`, `Exit status`, the printed verdict, and whether the run printed
`IDENTICAL`.

Pass the SHA-256 on the first, un-timed invocation only. Hashing a multi-gigabyte
archive adds minutes of wall clock and reads the whole file into page cache,
neither of which belongs in the memory measurement.

## Test suite

```bash
python -m pytest tests/test_depthrec_equivalence_v2.py tests/test_depthrec_v2_streaming.py -q
python -m pytest tests -q
```

`tests/test_depthrec_v2_streaming.py` runs every fixture through both the
streaming evaluator and `tests/frozen_v2_reference.py` -- a verbatim copy of the
replaced loader and driver -- and compares the complete structured results. Where
the preserved 240 s engineering probe is present it does the same over 486,048
real frames.

## Engineering probe

The probe is the reproduction check, not an acceptance run. Where
`<EVIDENCE_DIR>/equivalence_probe_2026-08-23/verify/equivprobe.sqlite` is present:

```bash
python -m pytest tests/test_depthrec_equivalence_v2.py -q \
  -k the_module_reproduces_the_documents_own_calibration_table
```

It must reproduce §8.1 exactly: 236,489 paired events, 2,682 markets with a pair,
259 qualifying markets, 179,888 qualifying events. Any difference is a
regression.
