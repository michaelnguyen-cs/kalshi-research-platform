# THREAD_A_EQUIVALENCE_V2_2026-08-23.md

**Pre-registered 2026-08-23, before any §8-v2 acceptance data has been collected
or inspected.**

Amends `research/THREAD_A_EXECUTION_MECHANICS_2026-08-22.md` (frozen at
`e7c8c87`) by retiring its §8 and replacing it. Nothing else in that document
changes. §9's feasibility pilot still may not begin until this test passes.

---

## 1. Why §8-v1 is retired

**§8-v1 was retired before its formal acceptance look**, because a
pre-acceptance engineering probe demonstrated that its wall-clock comparison
design is confounded by asymmetric delivery timing between independent
WebSocket connections.

§8-v1 compared two independent connections on a fixed 1 Hz wall-clock grid and
allowed a ≤500 ms skew tolerance. The 240-second probe of 2026-08-23
(run `equiv-1787444250-7246b8dd`) produced:

- 829,808 matched grid cells, of which 828,423 agreed exactly — **99.8331%**,
  below §8-v1's 99.9% condition-1 bar.
- Independently measured B-minus-A delivery skew of median +1.0 ms, p95
  +276.6 ms, p99 +630.8 ms, p999 +1117.7 ms, max +1705.6 ms. **2.36% of events
  reached B more than 500 ms after A; zero events reached A more than 500 ms
  after B.**

**These are diagnostic results from an engineering probe. They are not a formal
§8 PASS or FAIL, and no formal acceptance look was consumed.**

The same probe supplies strong positive evidence that the decoders themselves
agree. Of 1,385 disagreements, independently reconstructed from raw frames by a
decoder sharing no code with the production implementation:

| category | count |
|---|---|
| explained ≤500 ms under the frozen rule | 906 |
| production missed a valid ≤500 ms convergence | 0 |
| late convergence (>500 ms) | 202 |
| disqualified by intervening contradiction | 100 |
| **persistent, adequately observed** | **0** |
| right-censored | 0 |
| production explained beyond what the frozen rule permits | 177 |

**Every one of the 1,385 converged exactly**, minimum follow-up 1.9 s against a
5.0 s adequacy threshold declared in advance. Every disagreement converged to
A's state. All 1,385 had both streams legitimately VALID; zero validity-state
defects.

The conclusion is that §8-v1 measures **transport timing**, not **decoder
equivalence** — the construct it was written to test. The correct response is a
construct-valid replacement, not a wider tolerance. **The 500 ms figure is not
being loosened; it is being made irrelevant.**

The 177-case resolver defect found by the same reconstruction was fixed
fail-closed at `85424d5` and is unrelated to this retirement.

**Evidence.** Source database and sidecars preserved at
`<EVIDENCE_DIR>\equivalence_probe_2026-08-23\`, hashed before any SQLite
open: `equivprobe.sqlite` 284,827,648 B sha256 `072616b1…2ce93`; `-wal` 0 B;
`-shm` 32,768 B. Reconstruction stages, `final.csv` and README committed at
`bda2bb0`. Probe code provenance: `8b3c712` plus a reconstructed two-file
uncommitted diff preserved as `provenance/probe_dirty_8b3c712.patch`; the
reconstruction is inference from contemporaneous artifacts rather than a
recorded diff, and that limitation is stated rather than papered over.

---

## 2. The invariant under test

Given the same identifiable exchange-generated book event, represented through
stream **A** (`use_yes_price: true`) and stream **B** (`use_yes_price: false`),
transforming both into canonical YES coordinates must yield **identical event
semantics and identical reconstructed book state**.

This removes TCP arrival timing from the correctness question entirely.

---

## 3. Pairing rule

Derived from the measured mechanism of the three markets that failed naive
pairing in the probe, **not** from whichever variant makes all markets pair.

**Events are paired positionally within a market, within an epoch.** The tuple
`(exchange ts_ms, side, canonical price, delta_fp)` is used **only to verify
alignment**, never to establish it.

The key is not unique and must not be treated as one: the probe measured 1,881
of 1,888 events on one market sharing a key with another, and up to 80 events
sharing a single `ts_ms`. A market with **zero** duplicate keys still failed
naive pairing, so uniqueness is neither present nor sufficient.

**Alignment is verified by contiguity.** If a differing block appears anywhere
other than the boundary regions defined in §4 (epoch head), §5 (archive tail) and
§6.1 (interrupted-epoch tail), the run **FAILS**. Differing blocks are never
reconciled, re-aligned, or skipped.

---

## 4. Baseline: where comparison starts

The exchange snapshots each subscription at its own instant. Events falling
between the two snapshot instants arrive at the earlier-snapshotted stream as
**deltas** while already being baked into the other stream's **snapshot**. Those
events cannot be compared event-wise and are not evidence of disagreement.

**Delivery order does not order snapshot instants.** The probe observed B's
snapshot delivered 141.7 ms after A's while reflecting an *earlier* exchange
state. Arrival time therefore must not be used to decide which baseline is
later.

**Rule.** For each market and epoch, comparison begins at the **first exchange
event that both streams received as a delta**. Everything before that point is
baked into at least one snapshot and is excluded from comparison, recorded as
`EXCLUDED_PRE_BASELINE` with a count.

At the baseline event, the two reconstructed books must already be **identical**
— that is the first assertion, not an exclusion. If they are not, the run FAILS
and the divergence is reported with both ladders in full.

### 4.1 Baseline identification algorithm

Because the verification tuple is not unique and the two head blocks can be
unequal, "the first event both streams received as a delta" is not
self-executing. The algorithm is frozen here rather than chosen at
implementation time.

Within each market and epoch, searching **forward only** through the
**pre-baseline head region**, find the earliest pair of A and B delta positions
`(i, j)` for which **both**:

1. their verification tuples `(exchange ts_ms, side, canonical price, delta_fp)`
   are equal; and
2. after applying those events, the two **full canonical books are identical**.

Among candidates, choose the one **minimising `i + j`** — the total number of
excluded head events. Ties are resolved by lowest `i`, then lowest `j`.

That event is the baseline. **From that point onward, pairing is strictly
positional. No later re-alignment, resynchronisation or skipping is permitted,
and any subsequent verification mismatch is a FAIL.**

**Head-region bound.** The search is capped at the first **1,000 delta positions
on either stream**. If no baseline is found within that bound, the market is
reported and the run **FAILS pending investigation** — it is not excluded and the
search is not widened. The bound exists so that a genuine interior divergence
cannot be absorbed by searching far enough forward to find some later point where
the books happen to agree. It is more than twenty times the largest head block
observed in the calibration probe (43 events, on `KXBTC15M`), so it is not
expected to bind.

---

## 5. Guard band: where comparison ends

At the tail of a recording, the lagging stream has not yet received events the
leading stream has. The probe observed exactly this: 6 and 2 A-only deltas in
the final ~1 ms before session cancellation.

**Rule.** Comparison includes only events with

    exchange ts_ms <= archive_max_ts_ms − 2000

where `archive_max_ts_ms` is the **global maximum exchange timestamp across all
archived deltas on either stream**. The cutoff is **exactly 2000 ms**, not a
minimum — "at least 2 seconds" would permit widening the band after seeing data.
Two seconds exceeds the maximum inter-stream delivery lag measured in the probe
(1,705.6 ms) with margin. Events inside the guard band are excluded and counted
as `EXCLUDED_GUARD_BAND`.

Because the cutoff is applied in exchange time and globally, it removes the same
events from both streams and therefore cannot itself cause a misalignment.

**Aggregate exclusion cap.** All three `EXCLUDED_*` counts are counts of
**individual raw stream-delta records across streams A and B**, not of pairs —
pre-baseline exclusions may be A-only or B-only, and an interrupted-epoch tail is
by construction one-sided. A single **included paired event represents exactly
two stream-delta records**. The fraction must therefore be computed in
stream-delta units on both sides, or it adds pairs to singles.

For each market, let

    E_boundary = EXCLUDED_PRE_BASELINE
               + EXCLUDED_GUARD_BAND
               + EXCLUDED_EPOCH_TAIL          (individual delta records, A and B)

    boundary-exclusion fraction =
        E_boundary / (2 × included_paired_events + E_boundary)

If that fraction exceeds **1%** for any market, the market is reported as
anomalous and the run **FAILS pending investigation**. The cap is aggregate
rather than per class: three separate 0.9% exclusions must not quietly total
2.7%. The exclusions exist to cover known boundary artefacts, not to absorb a
systematic problem.

---

## 6. Epochs

A **sequence gap, disconnect, reconnect, or new snapshot on either stream** ends
the current epoch for every market on that subscription. A new epoch begins only
after both streams have re-established a valid book and a new baseline is
determined per §4.

Events in an interrupted epoch after the gap and before re-baselining are
excluded and counted. **An epoch contributes to the evidence total only if it
contains at least 100 paired events**; shorter epochs are reported but do not
count toward §8's minimum.

### 6.1 Interrupted-epoch tail

§5's guard band protects only the end of the whole archive. An epoch ended by a
disconnect or gap has its **own** tail, produced by the same transport-timing
mechanism: the leading stream holds events the lagging one never received before
the interruption. Without a rule for this, an ordinary reconnect would create an
unmatched suffix block and fail decoder equivalence — contradicting §9, which
states that reconnects are availability diagnostics and never equivalence
failures.

**Rule.** After the §4 baseline is established, alignment remains strictly
positional for the remainder of the epoch. When an epoch is interrupted, compare
A and B at **identical relative positions from that baseline**. There is **no
backward search**.

Let `n = min(post-baseline A deltas, post-baseline B deltas)`. Every verification
tuple at positions `0 … n−1` must agree **exactly**. Any mismatch before either
stream is exhausted is an alignment-verification **FAIL**, and may not be
repaired by searching, re-aligning or skipping — even if some later duplicate
tuple happens to match.

If all positions through `n−1` agree, the remaining events on the longer stream
form the one permitted **contiguous** interrupted-epoch suffix and are recorded as
`EXCLUDED_EPOCH_TAIL`. No interior block may ever be excluded.

    baseline                          baseline
       ↓                                 ↓
    A: 1 2 3 4 5 6 7                  A: 1 2 3 X 5 6
    B: 1 2 3 4 5                      B: 1 2 3 Y 5
                 ↑                             ↑
       6,7 = EXCLUDED_EPOCH_TAIL          mismatch = FAIL

**Bound.** If more than **1,000 events on either stream** would be excluded as
`EXCLUDED_EPOCH_TAIL`, the run **FAILS pending investigation**. An abnormally
large interruption boundary must not be silently discarded.

A backward search for "the last common verification tuple" is deliberately
**not** used: §3 establishes that the tuple is not unique and cannot establish
alignment, so a backward search could match the wrong occurrence and silently
re-align — precisely what §3 forbids. The positional-prefix rule follows directly
from the already-frozen requirement that no re-alignment occurs after the
baseline.

The measured 2-second latency figure is likewise **not** used here. A reconnect
may last far longer than two seconds, and local disconnect detection does not
yield a clean exchange-time boundary.

This rule does not apply to a normally completed final epoch; that boundary
continues to use §5's exact global cutoff.

---

## 7. What is compared

**Every paired event.** After applying each paired event to both reconstructions,
the **full canonical book** — every price level and size on both ladders, not
merely top-of-book — must be identical.

Full-ladder comparison is required because the probe's own diagnosis relied on
it: the three anomalous markets were cleared by comparing every level at every
probe, and top-of-book alone would have hidden where the difference actually sat.

---

## 8. PASS criteria

Because the transform is **deterministic**, the bar is exact. There is no
tolerance and no percentage.

**PASS requires all of:**

1. **Zero** book divergences at any paired event, across all markets and epochs.
2. **Zero** alignment-verification failures — every positionally paired event
   agrees on `(exchange ts_ms, side, canonical price, delta_fp)`.
3. **Zero** baseline-event divergences per §4.
4. **Zero** markets exceeding the 1% exclusion limit per §5.
5. At least **600,000 paired events contributed by qualifying epochs**, spanning
   at least **60 distinct markets that each contribute through at least one
   epoch containing ≥ 100 paired events**, during at least **60 continuous
   minutes** of recording.
6. Both streams subscribed to the **same market set** throughout each epoch.

**Any single divergence is a FAIL.** A deterministic transform that disagrees
once disagrees.

**INSUFFICIENT** if criterion 5 is not met. The run repeats without changing any
parameter above.

### 8.0 Stopping rule

Each formal acceptance run records **exactly 60 continuous wall-clock minutes**,
timed from the moment both streams have established their initial valid epochs.
At 60 minutes the recording stops.

This is fixed, not a minimum. Without it, a run could be stopped the moment the
floors are reached — minimising exposure and therefore the chance of encountering
a divergence — or extended until the floors arrive. The floors in criterion 5
were calibrated specifically for a 60-minute run.

If either the 600,000-event floor or the 60-market floor is not met at 60
minutes, the verdict is **INSUFFICIENT** and the run is repeated unchanged. **A
completed FAIL is never extended, rerun, or converted to INSUFFICIENT.**

### 8.1 How criterion 5's floors were derived

The threshold-setting rule was frozen on 2026-08-23 **before** the underlying
measurements were inspected:

> The paired-event floor equals 25% of the preserved probe's qualifying
> paired-event rate extrapolated from the full 240-second probe wall duration to
> 60 minutes, rounded down to the nearest 100,000 events. The distinct-market
> floor equals 25% of the probe's number of distinct markets qualifying under the
> ≥ 100-paired-events-per-epoch rule, rounded down to the nearest 10 markets. The
> minimum duration remains 60 continuous minutes regardless. These floors are
> engineering degeneracy checks, not statistical power thresholds.

Measured against the preserved probe (`equiv-1787444250-7246b8dd`, 240 s):

| quantity | value |
|---|---|
| positionally paired events after §4 and §5 exclusions | 236,489 |
| markets contributing ≥ 1 qualifying event | 2,682 |
| markets qualifying under the ≥ 100-per-epoch rule | 259 |
| paired events within qualifying epochs | 179,888 |
| qualifying-epoch rate extrapolated to 60 min | 2,698,320 |

**Resolving the input ambiguity.** "Qualifying paired-event rate" admits two
readings — all paired events, or only those within qualifying epochs. Criterion 5
counts *events contributed by qualifying epochs*, so the floor is derived from
the same population it measures. Deriving it from the all-paired rate would set a
floor in units the criterion does not count, which is the same class of error as
the frames-versus-pairs mistake this calculation exists to correct.

25% of 2,698,320 = 674,580, floored to the nearest 100,000 = **600,000**.
25% of 259 = 64.75, floored to the nearest 10 = **60**.

An earlier draft proposed 500,000 events and 200 markets, justified with a frame
count rather than a pair count. The corrected derivation raises the event floor
and lowers the market floor; both now follow mechanically.

### 8.2 Limits of the extrapolation, recorded not corrected for

The probe was a single four-minute window (2026-08-23, 00:17:36–00:21:37 UTC)
containing two crypto markets in their final quarter-hour and a set of live MLB
games. Two properties of that window bear on what the floors measure:

- **Event concentration is extreme.** One market (`KXBTC15M-26AUG222030-30`,
  109,101 events) is 60.6% of all qualifying-epoch events; the top two are 77.6%.
  A paired-event floor therefore mostly measures whether a busy crypto market was
  present, not breadth. The market floor is what supplies breadth, and the two
  floors check different degeneracies deliberately.
- **Market count is MLB-dominated.** Of 259 qualifying markets: KXMLBHR 94,
  KXMLBHIT 88, KXMLBHRR 54, KXMLBTB 18, KXMLBRBI 3, KXBTC15M 1, KXETH15M 1. An
  hour without live baseball would yield far fewer qualifying markets, and the
  MLB regular season ends in late September.

**Consequence.** The acceptance run should be scheduled during a period with live
MLB games. That is an operational scheduling choice and does not affect any PASS
criterion — §8's verdict turns on zero divergences, and criterion 5 governs only
sufficiency. If the floors cannot be reached, the result is INSUFFICIENT and the
run repeats; the floors are not lowered to fit the calendar.

### 8.3 What the probe already demonstrates about §4 and §5

Applying the baseline and guard-band rules to the preserved probe excluded 44
pre-baseline events and 6,080 guard-band events, after which **zero markets
failed position-by-position key verification**. The three markets that defeated
naive pairing now pair cleanly. The two rules between them fully resolve the head
and tail artefacts they were written for.

The probe contained 0 sequence gaps on either stream, one snapshot per market per
stream (3,472 each, all at run start), and no disconnect or reconnect. Each market
therefore carried exactly one epoch, so "per epoch" and "per market" coincided
there. They will not coincide in a run that reconnects, and §6 governs that case.

---

### 8.4 Scheduling

The formal acceptance run is scheduled during a window containing live MLB games
in the frozen validation universe, chosen **solely from published game schedules
and before any acceptance data from that run are inspected**. The window may not
be selected or changed on the basis of decoder agreement, event counts observed
after the run starts, or any other equivalence output.

If a completed run is INSUFFICIENT **solely** because criterion 5 was not
reached, it may be repeated unchanged during the next pre-declared eligible
live-game window. A FAIL is not rerun.

High-traffic windows are not "easier": more events give a deterministic decoder
more opportunities to disagree. The scheduling rule exists because MLB supplied
257 of the 259 qualifying markets in the calibration probe, so an hour without
live MLB is expected to have substantially less qualifying-market breadth. The
probe does not establish that no other hour could reach 60 through other series;
it establishes where the breadth came from in the window that was measured.

## 9. Execution rules carried over from §8-v1

These are unchanged and remain in force:

- Subscription **A** uses `use_yes_price: true`, **B** uses `false`, on
  **separate WebSocket connections**.
- The validation universe is the twelve series enumerated in
  `THREAD_A_EXECUTION_MECHANICS_2026-08-22.md` §8, with the same "currently
  open" predicate and the same deterministic ordering if a subscription limit
  binds.
- Validation data is engineering data, excluded from all feasibility and
  confirmatory samples.
- Raw frames are retained at recorder fidelity and may leave the production host
  only through the archive-then-purge workflow, with all five gates.
- Disconnects, reconnect durations and excluded-event fractions are logged and
  reported **separately** as recorder-availability diagnostics, never as
  equivalence failures.
- A PASS validates only the implementation deployed when it ran. Any substantive
  change afterward voids it; the fix is deployed and the test rerun.

---

## 10. What this test cannot establish

It establishes that the two price-convention decoders produce identical books
from identical exchange events. It does **not** establish that either decoder is
correct in absolute terms — both could share a mistake. The independent
reconstruction at `bda2bb0`, which shares no code with the production
implementation, is the check against that, and it found zero persistent
divergences across 1,385 cases.

It also does not measure transport reliability. That is §9's availability
framework, and the probe's measured skew distribution — p99 +630.8 ms, max
+1,705.6 ms, one-sided toward B — is recorded here as an input to that work
rather than as a defect.

---

## 11. Sign-off

Frozen by: __________  Date: 2026-08-23

Git provenance: the commit that first introduces this file. A document cannot
contain its own final SHA, so the Git history is the immutable record.

---

## 12. Amendments

*Append-only. Nothing above this line is edited. Each entry is dated and states
what prompted it. An amendment made after seeing a result the document governs
would not be an amendment.*

### 2026-08-23 — boundary-exclusion cap resolution

**Prompted by** implementation of §8-v2 at commit `c26ebed`, before any formal
§8-v2 acceptance run. No acceptance data exists.

**The defect.** §5's per-market 1% cap has a discrete-resolution pathology on
low-volume markets. A single ordinary paired boundary event contributes **two**
excluded stream-delta records, so with `E = 2` the fraction `2/(2p+2)` exceeds 1%
whenever `p < 99` — **regardless of decoder behaviour**. Criterion 4 applies to
every market, not only qualifying ones, so this is a FAIL path independent of
criterion 5's floors. On the calibration probe 418 of 2,682 markets tripped it
and it was the only failure class present; 2,413 of 2,682 markets had fewer than
99 paired events. The arithmetic does not depend on run length.

**Resolution.** Let `p` be included paired events and `E` the total excluded
individual stream-delta records from `EXCLUDED_PRE_BASELINE`,
`EXCLUDED_GUARD_BAND` and `EXCLUDED_EPOCH_TAIL`. Let `D = 2p + E`.

1. **Per-market cap, resolution-adjusted.** Define
   `D_effective = max(D, 2000)`. The market fails the boundary-exclusion
   criterion if

       E / D_effective > 1%

   Equivalently: **no market may have `E > max(20, 0.01 × D)`.** The cap is a
   denominator floor, not an on/off switch — a small market does not get
   unlimited boundary-exclusion immunity.

2. **Run-level cap.** Independently, across all markets, the **unadjusted**
   aggregate `ΣE / ΣD` must not exceed **1%**. No denominator floor is applied
   at run level.

**Why an allowance of 20 records.** Boundary artefacts scale with the number of
boundary *regions* a market has, not with its size. A market with no
interruption has one — the archive tail, costing 2 stream-delta records for one
paired loss. Every reconnect adds two more regions: an interrupted-epoch tail on
the old epoch and a fresh pre-baseline head on the new one, each potentially
one-sided and each costing a few records. An allowance of 2 would therefore fail
a low-volume market for experiencing an ordinary reconnect — recreating exactly
the contradiction §6.1 exists to prevent, since §9 states that reconnects are
availability diagnostics and never equivalence failures.

Twenty records is roughly ten ordinary paired boundary losses: enough to absorb
an archive tail plus several reconnect boundaries, and far too few to conceal
systematic exclusion. Worked cases:

| p | E | D | test | outcome |
|---|---|---|---|---|
| 20 | 2 | 42 | 2 ≤ 20 | pass |
| 90 | 2 | 182 | 2 ≤ 20 | pass |
| 99 | 2 | 200 | 2 ≤ 20 | pass |
| 30 | 6 | 66 | 6 ≤ 20 (one reconnect) | pass |
| 50 | 100 | 200 | 100 > 20 | **FAIL** |
| 10 | 200 | 220 | 200 > 20 | **FAIL** |
| 0 | 199 | 199 | 199 > 20 | **FAIL** |
| 5,000 | 150 | 10,150 | 150 > 101.5 | **FAIL** |

The last two rows are why this is a denominator floor rather than an exemption:
a market that is entirely excluded fails, and a large market is still held to
the proportional 1%.

**On the value 20.** It is not statistically estimated and is not claimed to be
a natural constant. It is a pre-formal **engineering resolution allowance**,
chosen so that a handful of legitimate boundary regions cannot make low-volume
markets mechanically unusable. Its role is only to decide whether enough
comparable evidence was obtained. **It cannot convert a decoder divergence into a
pass.**

### Outcome classification — the part that actually resolves the contradiction

Adjusting the allowance alone does **not** remove the conflict with §9. §9 states
that disconnects, reconnects and excluded-event fractions are availability
diagnostics and never equivalence failures — yet a market accumulating enough
entirely legitimate reconnect boundaries could still breach the cap and report
FAIL while criteria 1–3 show zero decoder disagreement. A larger allowance only
moves the point at which that happens.

The two situations are different in kind and must resolve differently:

| what happened | meaning | outcome |
|---|---|---|
| book divergence, positional alignment mismatch, or baseline divergence | **the decoders disagree** | **FAIL** |
| excessive boundary exclusion with criteria 1–3 clean | **not enough clean comparable evidence was obtained** | **INSUFFICIENT_BOUNDARY_EVIDENCE** |

**Rule.** If either boundary-exclusion condition is exceeded **and criteria 1–3
have not failed**, the run is `INSUFFICIENT_BOUNDARY_EVIDENCE`, **not FAIL**.
Report the affected markets, boundary classes, reconnect counts and exclusion
fractions. The result does not validate the decoder and **may not start §9**.

A run with any criterion 1–3 divergence remains **FAIL** regardless of exclusion
counts. **A FAIL is never converted to INSUFFICIENT.**

`INSUFFICIENT_BOUNDARY_EVIDENCE` may be rerun unchanged in the next pre-declared
eligible window. If investigation reveals an implementation defect requiring a
substantive code change, the new implementation is deployed and the acceptance
run starts fresh.

### Verdict precedence

"Criteria 1–3 have not failed" is too narrow: criterion 6, a §4.1 head-bound
failure, a §6.1 interrupted-tail-bound failure, and a differing block outside the
permitted boundary regions are all FAIL conditions too. Without an explicit
hierarchy, a criterion-6 failure occurring alongside a cap breach could be
reported as insufficiency and hide the real failure.

1. **FAIL takes precedence over every insufficiency outcome.** Any condition
   specified anywhere in §8-v2 as FAIL or FAIL pending investigation remains
   FAIL — criteria 1–3, criterion 6, the §4.1 head bound, the §6.1
   interrupted-tail bound, or a differing block outside the permitted boundary
   regions.
2. Only if **no FAIL condition exists**, a breach of either boundary-exclusion
   cap yields `INSUFFICIENT_BOUNDARY_EVIDENCE`.
3. If no FAIL condition and no boundary-evidence insufficiency exists, but
   criterion 5's 600,000-event or 60-market floor is unmet, yield ordinary
   `INSUFFICIENT`.
4. `PASS` requires all remaining PASS conditions.

If both boundary-evidence insufficiency and criterion-5 insufficiency occur in
the same otherwise non-failing run, report `INSUFFICIENT_BOUNDARY_EVIDENCE` as the
primary verdict and **also** report the unmet criterion-5 floors.

    any FAIL condition          →  FAIL
    else caps breached          →  INSUFFICIENT_BOUNDARY_EVIDENCE
    else 600k / 60 not reached  →  INSUFFICIENT
    else                        →  PASS

**Criterion 4 is accordingly read as boundary-evidence sufficiency**: neither the
resolution-adjusted per-market cap nor the unadjusted run-level cap may be
exceeded for PASS. Its breach is not decoder failure.

Worked contrast — six reconnects, decoders agreeing perfectly on every comparable
event, boundary exclusions over the cap: `INSUFFICIENT_BOUNDARY_EVIDENCE`, rerun
unchanged. One interior positional mismatch: **FAIL**, immediately, whatever the
exclusion counts say.

**Scope.** All existing restrictions on **where** boundary exclusions may occur
are unchanged — §4.1's head bound, §5's exact cutoff, §6.1's positional-prefix
rule and one-sided contiguous suffix, and §3's prohibition on differing blocks
outside those regions. **No interior mismatch, book divergence, alignment
failure or baseline divergence may be excluded under this amendment.**

The 1% threshold itself is unchanged, and **no other §8-v2 parameter changes** —
not the guard band, the bounds, the epoch qualification, the floors, the
duration, or the PASS criteria.

**On what this does and does not affect.** This amendment **does not change the
decoder-equivalence PASS standard**. Criteria 1 through 3 still require zero book
divergences, zero alignment-verification failures and zero baseline divergences
on every event eligible for comparison. What changes is only how much
already-mechanistically-justified boundary material may be absent from
comparison — material restricted to contiguous boundary regions, bounded by
§4.1, §5 and §6.1, and separately audited by the run-level cap.
