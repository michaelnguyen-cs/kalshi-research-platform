# Thread A — Fee Audit Pre-Registration

**Date written:** 2026-08-22
**Status:** written before any fee code was read for the purpose of changing it, and before any recomputation.
**Relationship to prior work:** this note does not replace the 2026-08-21 frozen result or the §9 amendment in `KILL_CRITERIA.md`. It records a specification gap discovered on 2026-08-22 and pre-registers the scope of a correction audit.

---

## 1. Why this note exists

On 2026-08-21 the Thread A longshot rule was re-run after a fee-model correction, producing the result preserved in §3. On 2026-08-22 two read-only provenance checks established:

1. `C = 10` is frozen into the Thread A computation — it is not a flag, not a diagnostic, and it produced the recorded numbers.
2. The frozen study never consistently specified the **actual submitted quantity**. The computation is unambiguous; the execution estimand is under-specified.

This note preserves that ambiguity rather than resolving it in either direction, and bounds what the subsequent audit is permitted to change.

---

## 2. Provenance of the frozen inputs

The frozen input to the primary calculation was untracked and existed in a single copy on the VM. It has been preserved off-box and hash-verified.

| File | SHA-256 | Notes |
|---|---|---|
| `data/calib_full.csv` | `31dbf3069cdae4acdea6acb405cae7f38036331900fb190741cd2c0ad58b77fa` | 3.4M, mtime 2026-08-21 20:48 UTC. 33,948 obs / 5,658 markets |
| `data/oos_20260821_corrected.txt` | `d63b6621909de73e1001c200ed775d778e7df5d3c21f5dcc77bcbb84e3fae190` | frozen output |

Copies: `~/frozen_backup/` on the VM (with `SHA256SUMS`), and `C:\dev\frozen_backup\` off-box. The Windows copy was verified byte-identical via `Get-FileHash`.

**Any recomputation must verify the input against these hashes before running.** If the hash does not match, the run is not comparable to the frozen result and must be reported as such.

Repository state at the time of writing: `main` at `ccc087b`.

**Pin commit — verified 2026-08-22.** `KILL_CRITERIA.md` §2 names commit `6ca4101` ("Make the longshot rule an input, not a sweep result") as the code pin covering both scripts. Verified: `6ca4101` is the last commit to touch `scripts/test_longshot_rule.py`. It does not itself touch `scripts/market_calibration_study.py`, whose last-touching commit is `b6a5566` ("Correct the fee model to Kalshi's order-level ceiling") one hour earlier — but `b6a5566` is an ancestor of `6ca4101`, and that file's blob is identical (`e8dd06f`) at `b6a5566`, `6ca4101` and `HEAD`. The pin is therefore correct as a **tree** pin: the code that would run at the pinned commit is the code that produced the §3 result. The earlier `9c6850c` and `5b6e95f` citations were superseded pin moves recorded in §2 itself, not a conflict. No correction required.

---

## 3. The frozen as-run result (preserved unchanged)

From `data/oos_20260821_corrected.txt`:

```
RULE: buy favorite   n= 1,873  win  87.7%  mean EV  +0.0164  total  +30.72
                     95% CI [+0.0018, +0.0304]
```

- Fee as computed: `ceil(100 * 0.07 * C * P * (1-P)) / 100` per **order** at `C = 10`, divided back to per contract.
- Measured crossing cost: `0.40c` (from `check_longshot_spread.py`, also run at C=10).
- Net of crossing cost: `+0.0124` per contract.
- Sample target derived from that net figure: approximately 5,080 firing markets.

This block is the historical record. Nothing in the audit may overwrite it.

---

## 4. The specification gap

### What is unambiguous

The gross P&L primitive (`test_longshot_rule.py:176`) is `(1.0 if won else 0.0) - entry`. It is homogeneous of degree 1 in quantity, so it is numerically identical whether it represents one contract or one-tenth of a ten-contract order. Quantity appears in the calculation in exactly one place: the fee term at `:177`, evaluated at `C = 10`. `_CONTRACTS_PER_ORDER` occurs at `:122`, `:177`, `:537`, `:538`, `:704` and never multiplies any P&L. `summarize` reports `total_ev` as a plain sum of per-contract values (`+30.72 ≈ 0.0164 × 1,873`); the `$307.20` that 1,873 ten-contract orders would produce appears nowhere. The script contains no bankroll, capital, or exposure term.

### What is ambiguous

The frozen text contains two unreconciled statements, fifteen lines apart in the same docstring:

- `test_longshot_rule.py:62` — "C = 10 contracts per order, the size"
- `test_longshot_rule.py:77` — "Every trade is one contract. Thin markets count as much as deep ones."

The second is repeated at `:811` and in the frozen output's caveats as "size is one contract", in both cases inside a list of *unmodeled* limitations, without qualifying whether "one contract" means an order quantity or a per-contract normalization.

Both PASS statements (`KILL_CRITERIA.md:88-90` and the script's own check at `:786-787`) are expressed in EV/contract units with `C=10` qualifying *the fee*, and are silent on submitted quantity. Live sizing is governed elsewhere by Kelly / `max_position_pct` (`KILL_CRITERIA.md:213-216`), which does not mention `C`.

### Two readings, neither of which was frozen

- **Interpretation A** — each firing market is a literal one-contract order. The C=10 fee is then wrong; the correct fee is the C=1 fee.
- **Interpretation B** — each firing market is a ten-contract order whose P&L is reported per contract. The C=10 fee is then exactly right.

The computation cannot distinguish them. The prose supports both.

### An unsupported indifference claim in the frozen document

`KILL_CRITERIA.md:79-82` justifies `C=10` in part by asserting it "moves the answer by less than 0.03c and is not doing any work." The only supporting evidence is `tests/test_order_fee.py:44-50`, which compares C=10 against C=200. That bounds the distance to the asymptote, not the distance to C=1. The C=10-vs-C=1 gap is roughly 0.50c at P=0.70, 0.80c at P=0.80, and 0.30c at P=0.90 — non-constant, and of the same order as the entire edge estimate. **The narrow numerical statement — that C=10 sits close to the large-C fee asymptote — is true. What is not established is the broader conclusion that the choice of C "is not doing any work", because the frozen study left actual submitted quantity ambiguous and never tested the materially different C=1 case.**

---

## 5. Observation on the current PASS bar

Recorded here because it is a consequence of numbers that already exist, not of any new computation.

The §2 PASS bar requires the 95% CI lower bound to remain above zero on EV/contract **net of** the order-level fee and net of the measured 0.40c crossing cost. Applying the crossing cost to the frozen CI, under the frozen convention of treating the measured 0.40c as a fixed subtraction with no sampling uncertainty of its own:

```
[+0.0018, +0.0304] − 0.0040  →  [−0.0022, +0.0264]
```

The lower bound is negative. Under Interpretation A the fee is larger, so the result is worse. **Thread A does not currently clear its PASS bar under either interpretation.**

This is not a kill. "Does not currently pass" means the continuation rules apply and more data is required. It does mean that the choice between A and B is not load-bearing for pass/fail — it bears only on how much more data is required.

**Verified 2026-08-22 against `data/oos_20260821_corrected.txt`.** The file records n = 1,873 (of 2,830 OOS markets at T-7min, 66.2%), win rate 87.7%, mean EV +0.0164/contract, total +30.72, 95% CI [+0.0018, +0.0304] from 10,000 percentile bootstrap resamples, seed 20260820. The subtraction above is arithmetically correct; mean EV nets to +0.0124.

**Discrepancy in the frozen record, recorded here.** The frozen output carries a `*** PASS ***` verdict, but the crossing cost does not appear anywhere in that file — the run is explicitly mid-priced ("prices are mids, so fills are optimistic"), and the 0.40c figure exists only in `KILL_CRITERIA.md` §2, measured separately on 300 tickers via `check_longshot_spread.py`. The file's PASS therefore evaluates the fee condition but not the crossing-cost condition. §2's PASS bar requires both. Under §2's FAIL clause — "anything else, including a CI that narrowly straddles zero" — the crossing-cost-netted interval straddles zero. **The `*** PASS ***` in the frozen output is not a §2 PASS.** This is recorded as a discrepancy in the existing record, not as a new result; the frozen output itself is preserved unchanged.

**Open question, not resolved here.** Whether a flat 0.40c subtraction is the correct way to net the crossing cost is not established. Re-bootstrapping per trade at executable prices, rather than shifting a mid-priced interval by a constant, could move the lower bound in either direction. The audit's IS/OOS crossing-cost work in §7 supersedes this constant-shift treatment.

---

## 6. What the audit may and may not do

### In scope — auditing what was actually run

The as-run C=10 calculation may be checked for correctness against Kalshi's actual fee treatment. This does not reinterpret the study; it verifies the arithmetic the study performed.

1. **Δ_rounding** — is the order-level ceiling implemented correctly at C=10?
2. **Δ_multiplier** — first enumerate the unique series actually represented in `data/calib_full.csv`. The audit follows the dataset, not the assumption that it contains only KXBTC15M and KXETH15M. For every series so represented, query `GET /series/fee_changes?series_ticker=...&show_historical=true`, which returns previous and upcoming fee changes with `series_ticker`, `fee_type`, `fee_multiplier` and `scheduled_ts`. Establish the regime in force at 2026-07-22 from the latest change with `scheduled_ts` at or before the sample start, then apply any change whose `scheduled_ts` falls within 2026-07-22 to 2026-08-21. Do not infer fee history from `last_updated_ts` — that is a general update field, not a fee-effective timestamp. Do not apply today's multiplier retrospectively, and do not use any secondary source. If no fee-change record exists at or before the sample start, do not invent the opening regime: use the current Series metadata and the full change history only insofar as they logically identify the historical value, and otherwise record the opening regime as **unverifiable**. An empty result from the endpoint is not evidence that today's value was in force.
3. **Δ_waiver** — was `fee_waiver_expiration_time` active on any market in the sample? It did not appear in sampled API responses; confirm for the actual markets.

Report each term's contribution separately. A single aggregate delta is not acceptable, because two errors pushing opposite ways would cancel and be reported as "nothing was wrong."

### Also in scope — sensitivity, reported side by side

Both interpretations are to be reported together:

| Reported quantity | Meaning |
|---|---|
| Frozen as-run result | exactly what the 2026-08-21 code produced, C=10 fee normalization |
| Interpretation A sensitivity | if "size is one contract" means an actual one-contract order |
| Interpretation B sensitivity | if C=10 means a real ten-contract order, EV normalized per contract |

**Neither interpretation is designated primary.** Neither may be selected on the basis of which produces the more favorable number. The 2026-08-21 as-run result remains the historical record regardless of what the sensitivities show.

### Out of scope

- Changing the signal definition, threshold, direction, market selection, filters, or the IS/OOS partition.
- Retroactively designating A or B as the primary estimand.
- Changing `C` to obtain a different answer. `KILL_CRITERIA.md:97` binds in **both** directions; a rule that only constrains favorable changes is not a rule.
- Any settlement-strategy work.

### Δ_other stopping rule

If the audit surfaces a material error that is **not** a fee error — an implementation or study-definition bug that changes what the frozen study measured — the recomputation halts. Document the bug and its expected direction. Any rerun becomes a **new** pre-registered study with its own dated note. The 2026-08-21 result stands as reported until then. A correction may restate a cost; it may not restate the experiment.

---

## 7. Additional required outputs

1. **Separate IS/OOS crossing costs.** The 0.40c figure came from a 300-ticker sample spread across the full calendar window, so it straddles the 2026-08-06 split and is a single pooled constant. Reused in both columns it is identical by construction and carries no information. Since candles carry bid and ask prices, recompute half-spread separately for the IS and OOS halves across all firing markets, then report Δ_generalization both gross and net. State explicitly whether the "OOS > IS" reassurance survives once costs are allowed to differ across the split.

2. **Required sample, recomputed from trade-level P&Ls.** Not by rescaling the old CI. The fee correction varies with entry price, so it moves variance as well as the mean. Any figure derived by scaling (for example the ~5,080 vs ~14,300 comparison discussed on 2026-08-22) is an order-of-magnitude sanity check only, never the reported requirement. Report: corrected mean, 95% CI, current firing markets, additional firing markets required, and estimated calendar time at the observed firing rate. State plainly whether resolution remains operationally feasible.

---

## 8. Acceptable outcomes

All four are acceptable and will be reported whichever occurs. No outcome is to be treated as the one being hoped for.

- **A — nothing was wrong.** ΔEV ≈ 0; the as-run result survives scrutiny unchanged. This is a successful audit, not a wasted effort.
- **B — costs were overstated.** EV increases. Thread A resolves sooner. This is **not** validation of the rule; the PASS bar is unchanged.
- **C — costs were understated.** EV decreases. Required sample grows, possibly to the point where Thread A is not resolvable in practice. That is itself a result.
- **D — errors offset.** Individual errors exist but sum to approximately zero. Fix the methodology anyway and preserve the per-term decomposition, so the record shows two errors that cancelled rather than no errors at all.

---

## 9. Forward rule

No future Thread A continuation study, and no new pre-registered study in this project, may proceed without separately and explicitly specifying:

1. **Actual contracts submitted per signal**, and
2. **The quantity used in the fee computation.**

Where these differ, the reason must be stated. This clause exists because the 2026-08-21 study failed to separately and unambiguously specify actual submitted quantity and fee-computation quantity, and that ambiguity was not discovered until after the result had been recorded and acted upon.

---

## 10. Record of what was known when this note was written

Written after the two provenance checks and before any fee code was modified or any recomputation run. The specification ambiguity in §4 was discovered before the audit, not after seeing its results. The PASS-bar observation in §5 follows from figures already present in the 2026-08-21 frozen output.

The settlement-averaging mechanism identified in conversation on 2026-08-21 is recorded elsewhere with its actual provenance — it was proposed as a replacement after the latency thread was killed. Its admissibility under `KILL_CRITERIA.md` is deliberately left undecided and is not addressed by this note.
