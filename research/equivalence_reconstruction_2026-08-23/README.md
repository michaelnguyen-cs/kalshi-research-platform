Note: hashes below refer to the original files in the private repository; paths in this public mirror were scrubbed and final.csv was removed, so they will not verify here.

# Independent reconstruction of the §8 engineering probe, 2026-08-23

An independent characterisation of the disagreements in the 240-second
dual-stream equivalence **engineering probe** of 2026-08-23. This is not a
formal §8 acceptance look and constitutes neither a PASS nor a FAIL. No frozen
parameter was changed by any of this work.

## Source

| | |
|---|---|
| SQLite run id | `equiv-1787444250-7246b8dd` |
| `git_commit` in the rows | `8b3c712354dce09fe0c8ed0f62a6786ee6f58af1-dirty` — see *Provenance* |
| schema version | 5 |
| raw frame window | 2026-08-23T00:17:36.850Z → 2026-08-23T00:21:37.623Z |
| grid window | 2026-08-23T00:17:38.614Z → 2026-08-23T00:21:36.614Z (239 ticks) |
| markets | 3,472 |
| harness | `equiv_storage_probe.py` (preserved in the evidence set) |

**Evidence path:** `<EVIDENCE_DIR>\equivalence_probe_2026-08-23\`
(outside the git tree — 272 MiB, and the deploy copies the repo wholesale).
`source/equivprobe.sqlite` sha256
`072616b1b67ea291fd5ce127d53bb3786fa07cc5bcb0d3c216f5cdca50e2ce93`, 284,827,648
bytes. Sidecar inventory, hashes, the convenience archive and the full
provenance record are in that directory's `README.md` and `MANIFEST.sha256`.

## Why the probe's own labels are not the input

The probe harness mirrors `EquivalenceTest.run()`'s compare loop but never calls
`store.record_comparison()` for a disagreement, so only the 828,423 agreeing
cells were persisted. The reported 1,071/314 split lived only in stdout, which
was not retained. The disagreement *population* is still exact:

    3,472 × 239 = 829,808 grid cells − 828,423 persisted = 1,385

No stored `resolution` label is read anywhere in this pipeline, and the
production resolver is never called to decide an answer.

## Independence

Nothing is imported from `depthrec`. Frame storage is undone with stdlib `zlib`
against the dictionary blobs in the database's own `raw_dictionary` table, and
every frame is checked against its stored SHA-256 of the *original* bytes —
486,048 of 486,048 verified. JSON framing, sid/sequence validity, the
yes/no → canonical-YES transform, snapshot replacement, delta application, book
invariants, top-of-book extraction and the validity state machine are all
reimplemented here against the wire format.

Validation: at the recovered per-tick evaluation instant the reconstruction
reproduces **all 828,423 persisted cells exactly** — both streams, all four
fields.

## Stages

Run stage 1 first; later stages consume its pickle. Paths come from
`DEPTHREC_EVIDENCE_DB` and `DEPTHREC_RECON_WORK`, both defaulting into the
evidence directory above.

| stage | what it does |
|---|---|
| `recon1_decode.py` | Decodes both streams from `ws_raw` into per-(stream, market) canonical top-of-book timelines. Verifies every frame's SHA-256 and asserts per-stream `recv_ts` monotonicity. Reports sequence gaps, replays, book errors, frame-gap maxima. |
| `recon2_grid.py` | Rebuilds the 1 Hz grid and scans a global evaluation offset against the persisted cells' top-of-book *values*. |
| `recon3_cut.py` | Recovers each tick's evaluation instant exactly, as the feasible interval where every persisted cell reproduces **and** no unpersisted cell reconstructs as a two-sided agreement. Emits the 1,385 unpersisted cells. |
| `recon5_variants.py` | Runs five candidate resolver implementations over the same timeline to find which reproduces the probe's reported split. |
| `recon6_final.py` | **Final classification.** Frozen rule over the complete timeline vs an emulation of production's bounded ring. Writes `final.csv`. Supersedes a withdrawn stage 4 (see below). |
| `recon7_sens.py` | Sensitivity of the classification to the two instants the probe never recorded. |
| `recon8_agg.py` | Aggregates: type, direction, delay distribution, concentration, validity, follow-up. |
| `recon9_lag.py` | Temporal clustering of the residual against per-second frame rates. |
| `recon10_delivery.py` | **Superseded, retained for the audit trail.** Attempts to pair the streams on per-sid `seq`; refuses when the pairing check fails (B received 36 messages A did not, so the counters diverge). |
| `recon11_delivery.py` | Delivery-lag measurement that does work: exchange-stamped latency, plus per-market event-for-event pairing with verification. |
| `recon12_trace.py` | Fully expanded raw-frame traces for a representative case of each class, plus the category-1 robustness check. |
| `recon13_unpaired.py` | Aligns the three markets that did not pair and reports each differing block with surrounding raw frames. |
| `recon14_snapbase.py` | Tests the snapshot-baseline hypothesis by comparing **full ladders**, not just tops. |
| `recon15_btclevels.py` | Per-level, over-time difference trace for `KXBTC15M`, separating transient lag from persistent divergence. |
| `recon16_v2crit5.py` | Measures the proposed §8-v2 criterion 5 against this probe. See *Stage 16* below. |

### Stage 4 — what it was, and why the numbering skips it

`recon4_classify.py` was the original classifier: it declared the category 4 /
category 5 follow-up boundary and classified all 1,385 disagreements under the
frozen skew rule, writing a `disagreements.csv`.

It was wrong. Its convergence search recorded an intervening contradictory top
but did not **abort** on one — it kept scanning and returned the first state
matching the target. So it applied "reached within 500 ms" instead of the frozen
rule's "reached within 500 ms **with no intervening contradictory top**", which
is a materially more permissive rule than the one the document freezes. Its
category 3 count was structurally unreachable as a result: any arrival inside
500 ms was claimed by the "explained" branch before the contradiction branch
could see it, so it reported 0 category 3 by construction rather than by
measurement.

The numbers it produced — 1,183 explained / 202 residual — are **wrong and are
not used anywhere in this package**. The correct figures under the frozen rule
are 906 explained / 479 residual, from `recon6_final.py`.

The stage was deleted rather than edited in place, and `recon6_final.py` was
written to replace it, so that no file in this hashed package has ever silently
changed meaning under a stable name. Its output, `disagreements.csv`, was
deleted with it and is superseded by `final.csv`. The gap in the numbering is
deliberate and this section is the record of it.

### Stage 16 — §8-v2 criterion 5

`recon16_v2crit5.py` measures the proposed §8-v2 criterion 5 against this probe:
it applies the proposed §4 per-market baseline and §5 exchange-time guard band,
derives the §6 epoch structure from the archive rather than assuming it, pairs
book events positionally within each market with the content key verified at
every position, and reports the paired-event and distinct-market counts plus a
simple 240 s → 60 min rate extrapolation.

It is measurement only: it recommends no threshold and changes no protocol. It
is committed here because its outputs determine §8-v2's frozen thresholds, and
a threshold whose derivation is not in version control is not auditable.

Load-bearing outputs, for checking that a future run of this stage still agrees:

| quantity | value |
|---|---|
| positionally paired book events after §4 and §5 | 236,489 |
| distinct markets contributing ≥1 paired event | 2,682 |
| distinct markets qualifying at ≥100 paired events per epoch | 259 |
| paired events inside those qualifying epochs | 179,888 |
| markets failing position-by-position key verification | 0 |

Provenance: recovered intact from the session scratchpad that produced those
figures (as-recovered sha256
`23067710908dcf2ce7ddcc31202abbfdfdae8a95aac72714775cc7a140992a1e`, 10,541
bytes). The committed copy differs from it in exactly two places, neither in the
measurement logic: it adopts the `DEPTHREC_EVIDENCE_DB` / `DEPTHREC_RECON_WORK`
preamble the other stages use, and it writes its per-market JSON into the work
directory instead of the current directory. Re-run from this directory against
the preserved evidence set, it reproduces all four counts above.

## Results

Category 4 / category 5 boundary, declared before classifying: **≥ 5.000 s** of
retained evidence on both streams after the comparison instant counts as
adequate observation. It never binds here — every one of the 1,385 converges,
minimum follow-up 1.912 s.

| class | count |
|---|---|
| explained ≤500 ms by both the frozen rule and production | 906 |
| **1** — production missed a valid ≤500 ms convergence | **0** |
| **2** — late convergence (>500 ms) | **202** |
| **3** — disqualified by an intervening contradiction | **100** |
| **4** — persistent, adequately observed | **0** |
| **5** — right-censored | **0** |
| production explained beyond what the frozen rule permits | **177** |
| total | **1,385** |

Category 1 is 0 at every resolve delay from 0.50 s to 1.00 s.

Category 2 delays: min 501.4, p25 523.0, median 639.4, p75 718.5, p90 802.1,
p95 808.1, p99 810.1, max 811.1 ms. Buckets — 500–750 ms 175 (86.6%),
750–1000 ms 27 (13.4%), 1–2 s 0, 2–5 s 0, >5 s 0. Laggard: B in 201 of 202.

Validity: both streams legitimately VALID in **all 1,385**; zero cells that
should have been unmatched; zero validity-state defects. Zero sequence gaps,
zero reconnects, zero subscription updates in the run.

Root cause, measured rather than inferred: B-minus-A per-event delivery lag over
97,754 paired events is median +1.0 ms with a one-sided tail — p95 +276.6,
p99 +630.8, p999 +1117.7, max +1705.6 ms. 2.36% of events reached B more than
500 ms after A; **zero** reached A more than 500 ms after B. This is transport
asymmetry between two concurrent WebSocket connections, not decoder
disagreement: every disagreement converges to the other stream's exact canonical
state, and the raw frames confirm the coordinate transform (A `side=no
price=0.1500` ↔ B `side=no price=0.8500`, same exchange `ts_ms`, 1.057 s apart).

### Reconciliation against the probe's reported split

The emulation of production's resolver gives 1,083 explained / 302 residual
against the reported 1,071 / 314. Over the plausible range of the two instants
the probe never recorded — the per-tick evaluation instant and the resolve
instant — it spans 302–309 residual. The remaining 5–12 cases out of 1,385 are
not reproducible from persisted data, because the ring ran on monotonic
timestamps that no artifact retains. Stated as a limit, not resolved.

### The three unpaired markets

Of 2,682 markets carrying deltas, 2,679 pair event-for-event. The three that do
not differ **only at the head and tail of the archive**, for two distinct
mechanisms, neither of which is a decoder or representation difference:

* **Different snapshot baselines (head).** The exchange takes each subscription's
  snapshot at its own instant. Events between the earlier snapshot instant and
  the later one are delivered to the earlier-snapshotted stream as deltas and are
  already baked into the other's snapshot.
  `KXBTC15M-26AUG222030-30`: 43 B-only deltas at `recv≈1787444258.072`,
  `ts_ms` 1787444255557–564; A's snapshot carries 102 bid / 108 ask levels
  against B's 104 / 107.
  `KXMLBHRR-26AUG222040MINSD-MINKCLEMENS2-1`: 1 B-only delta, `ts_ms`
  1787444255564. Note B's snapshot was *delivered* 141.7 ms after A's yet
  reflects an *earlier* exchange state — delivery order does not order snapshot
  instants.
* **Recording truncation (tail).** A-only deltas in the final ~1 ms before the
  sessions were cancelled, with no counterpart because the lagging stream had not
  received them when the run ended.
  `KXBTC15M`: 6 A-only deltas at `recv` 1787444497.6203–.6213.
  `KXETH15M-26AUG222030-30`: 2 A-only deltas at `recv` 1787444497.6213.

Net counts check out exactly: KXBTC15M 43 − 6 = +37; KXETH15M 0 − 2 = −2;
KXMLBHRR +1 − 0 = +1.

Confirmation by full-ladder comparison (`recon14`, `recon15`): KXETH15M and
KXMLBHRR are **identical at every level at every probe**. KXBTC15M is identical
at 18 of 24 probes; the 6 differing probes each fall while one stream is
mid-burst, the differing level sets share no member between probes, and no level
differs at more than 2 of 24 probes. There is no persistent divergence.

Mechanisms ruled out, explicitly:

* **Duplicate event keys** — real (1,881 non-unique keys on A for KXBTC15M,
  1,888 on B; 757/757 for KXETH15M) but not the cause: KXMLBHRR has **zero**
  duplicates and still failed to pair, and duplicate confusion would scatter the
  differing blocks rather than confine them to the two extremes.
* **Timestamp collision** — real (up to 80 events share one `ts_ms` on KXBTC15M,
  40 on KXETH15M, 4 on KXMLBHRR) but non-misaligning: the sequences match
  contiguously outside the head and tail blocks.
* **Missing message** — ruled out: zero sequence gaps on either stream, every
  per-sid sequence contiguous.
* **Ordering** — ruled out: difflib finds only 1–2 differing blocks per market,
  both at the extremes, with no transpositions.
* **Market-specific representation difference** — ruled out by the full-ladder
  comparison above.

#### What this requires of the replacement test's matching rules

These follow from the mechanism, not from choosing whichever variant makes all
2,682 pair:

1. **Do not treat `(ts_ms, side, price, delta)` as a unique key.** Up to 80
   events share a `ts_ms` and ~1.7% of keys repeat within a market. Pair
   positionally within a market and use the key only to *verify* alignment.
2. **Baseline per market on the later of the two snapshot instants.** Compare
   only from the point at which both streams hold an established book. Head
   differences are a legitimate consequence of independent snapshots.
3. **End the comparison window before the archive's last frames**, by at least
   the maximum observed inter-stream delivery lag (p99 630.8 ms, max 1,705.6 ms
   here). A trailing guard band of ≥2 s covers this run. Without it, the leading
   stream's trailing frames manufacture differences.
4. **Verify alignment by contiguity.** A correct pairing yields differing blocks
   only at the two extremes; scattered blocks indicate genuine reordering or
   loss and should fail rather than be reconciled away.

## Provenance of the source run

The rows say `8b3c712…-dirty`. **The run must not be described as commit
`8b3c712`** — the tree carried uncommitted changes and the contemporaneous
`git status` was never captured. The executed diff was reconstructed by replaying
the contemporaneous patch scripts onto a clean extract of `8b3c712`; it touches
exactly `src/depthrec/equivalence.py` and `src/depthrec/session.py`. The
reconstructed `session.py` is byte-identical to `773c44d`, and the reconstructed
`equivalence.py` differs from `773c44d` only in `HISTORY_MAX_ENTRIES`
(1024 at probe time, 2048 as committed) — behaviourally inert here, since the
largest ring occupancy in any 2.0 s window was 904 entries and the cap never bit.
The patch, the base HEAD, the patch scripts and the confidence basis, including
its residual uncertainty, are in the evidence directory's `README.md` and
`provenance/`.

## `final.csv`

One row per reconstructed disagreement; 31 columns.

| | |
|---|---|
| rows (excluding header) | 1,385 |
| size | 319,989 bytes |
| sha256 | `61594455275d65915d198d114bae24b41ae18f8eb3182570e33ac254e0f82851` |

## Stage hashes

| file | sha256 |
|---|---|
| `recon1_decode.py` | `bd52f9043261cef61f9c018ddd1318314d9cd9a18c992a10ca7b96bead4c8504` |
| `recon2_grid.py` | `e49a089beaf3a5b6e062192bce0f8be0339ba9b08cb57085dd1c00e7b2a39ef2` |
| `recon3_cut.py` | `1fea1de3da905d991d671fc852338f4fcb0b2e86c885a4f053551ea6d370b10f` |
| `recon5_variants.py` | `0c941cb568179b08782a8fa4fe8ca7fd3a648a4c933020311e54a3cd2c40c9c4` |
| `recon6_final.py` | `adaf87ed450ad75f105b3c7ab3a78fdcc318930e5e8f1d91702f15438841c72b` |
| `recon7_sens.py` | `7c63449ba7f461fb03db8ed783f2ae66c70fc710e1f97ec7e31aa143c601c117` |
| `recon8_agg.py` | `8e237e8fb0952b631a6f38f19353b34314d715ea6fed2a3854d7a5043fbd0460` |
| `recon9_lag.py` | `59701bdb905f2254e687a916c19cc0a1bfe43ea6f35917cb3a07ee36956db07c` |
| `recon10_delivery.py` | `6ded9db940ff42d1bf0405d2f34338fa0ababa3d0597ccaf92d22123374ddbfd` |
| `recon11_delivery.py` | `52dc155b934ad7c7fee40bbbc6b1640aea0c96b7aeb69b008479e78b816d22a9` |
| `recon12_trace.py` | `5adbf795b8c51aab636cbdf5c6a81871f2600142d4c300683d87d1590f53522c` |
| `recon13_unpaired.py` | `f0f5ecdfc970a49cc9a04c1f19d9f5b6e43fbf497c1a922b57090ab4b878b802` |
| `recon14_snapbase.py` | `124f70a683f8548a35f9dcedc07b5acd1c84aee4a49d3873a93614ca8965c4b2` |
| `recon15_btclevels.py` | `5dab6e564db9170696615b006d176e648ac2b06aa020d9f154e20fd7dd17e1d2` |
| `recon16_v2crit5.py` | `feafcc07ce72ec7aceafee48271cadbba9ab8e4802db189249e1b78bcf8eb58b` |
| `.gitattributes` | `a7cdfc94b62e04b5fb1f40970874790f6b30dbaead2b328f9f712344c9893861` |

Hashes are of the **LF-canonical bytes**: `.gitattributes` here sets `* -text` so git stores and checks these files out verbatim on every platform, and `recon6_final.py` writes `final.csv` with an explicit LF terminator rather than the platform default, so a rerun on either OS produces the same bytes and the same digest.

Immutable once committed. Any change to a stage must land as a new stage and a
new row here, not as an edit — the hashes above are what make the reported
numbers checkable.

## Follow-on

The 177-case class is an implementation defect in `StreamHistory.reaches()`,
fixed separately to fail closed when the ring cannot witness the comparison
instant. The traced real case and its synthetic regression fixture are described
in the evidence README. That fix makes the test stricter and changes no frozen
parameter.
