# Thread A — Amendment, 2026-08-22

**Type:** methodological correction to how the frozen result was costed. Not a new experiment, not a change to the rule.

**What is unchanged:** the frozen rule (buy the favorite at ≥0.70 at the T-7 slice), the sample, the IS/OOS partition, the bootstrap methodology, and the §2 PASS bar. None of these were touched.

**Scope of this amendment:** it corrects the cost model applied to the frozen result and records what that correction does. It does not overwrite the 2026-08-21 record, which is preserved in §1.

---

## 1. The frozen record, preserved

From `data/oos_20260821_corrected.txt` (sha256 `d63b6621…3fae190`), produced from `data/calib_full.csv` (sha256 `31dbf306…d58b77fa`):

```
RULE: buy favorite   n= 1,873  win  87.7%  mean EV  +0.0164  total  +30.72
                     95% CI [+0.0018, +0.0304]
bootstrap: 10,000 resamples, percentile method, seed 20260820
```

After the pre-registered flat 0.40c crossing adjustment: mean **+0.0124**, implied CI **[−0.0022, +0.0264]**.

This block stands as the historical record. Nothing below replaces it.

Note for the record: the frozen output printed `*** PASS ***`, but that was not a §2 PASS. §2 requires the CI lower bound above zero net of *both* the fee and the measured crossing cost; that run netted the fee only. The crossing cost appears nowhere in the file — the run is explicitly mid-priced.

## 2. The corrected result

Computed on the same 1,873 firing observations, the same frozen bootstrap (`test_longshot_rule.py:361-371`, 10,000 percentile resamples, seed 20260820, i.i.d. over trades, no clustering), with the cost model corrected as decomposed in §3.

| Quantity | C = 1 | C = 10 |
|---|---:|---:|
| n | 1,873 | 1,873 |
| corrected mean | +0.0127 (+1.2741c) | +0.0128 (+1.2787c) |
| 95% CI | [−0.0018, +0.0268] | [−0.0018, +0.0268] |
| CI half-width | 0.0143 | 0.0143 |
| lower bound > 0 | **No** | **No** |

**The §2 PASS criterion is not met.** The lower bound is negative under both fee-quantity sensitivities.

Neither C is designated primary. `C=10` is a fee-normalization sensitivity evaluated at an observed marginal top-of-book price; historical order-book depth is unavailable for this window, so it is **not** a proven ten-contract execution backtest.

**Replication check performed before applying any correction:** rebuilding the frozen pre-cross vector and running the pinned bootstrap reproduced n=1,873, mean +0.0164, CI [+0.0018, +0.0304] exactly.

## 3. Per-term decomposition

Baseline: the frozen +1.2400c (mean +1.6400c less the flat 0.4000c crossing). Cents per contract. Components are reported separately and deliberately not netted into one aggregate.

| Term | C = 10 | C = 1 |
|---|---:|---:|
| Frozen baseline | +1.2400 | +1.2400 |
| Fee-precision correction at midpoint | **+0.0500** | +0.0456 |
| Δ_multiplier (series level) | **0** | 0 |
| Δ_waiver | *undetermined — no numeric delta* | *undetermined* |
| Measured crossing vs the flat 0.40c | **−0.0304** | −0.0304 |
| Fee relief, fee at top_ask not midpoint | **+0.0191** | +0.0188 |
| **Total quantified change** | **+0.0387** | +0.0341 |
| **Corrected mean** | **+1.2787** | +1.2741 |

Upward components total +0.0691 against −0.0304 downward (C=10). Reporting only the net would conceal that two errors ran in opposite directions.

Caveat on the precision term at C=1: it bundles two changes — cent→centicent precision *and* the order quantity moving from 10 to 1. It is a pure precision term only at C=10, where the frozen baseline is also C=10.

### What each correction rests on

- **Fee precision.** The frozen model ceiled the fee to the cent. Five real KXBTC15M taker fills from 2026-08-09/10 — inside the sample window — returned `fee_cost` exactly equal to `ceil_to_$0.0001(0.07 × C × P × (1−P))`, and the production balance carries sub-cent precision (`balance_dollars` = <BALANCE>, four decimal places), which is inconsistent with a $0.01 target. The cent ceiling overstated the fee.
- **Δ_multiplier = 0 at series level.** Kalshi's changelog states that `GET /series/fee_changes` with `show_historical=true` returns all fee changes, previous and upcoming. Both frozen series return empty histories and current metadata reports `fee_multiplier` 1. This does not cover event-level overrides — see §4.
- **Measured crossing.** The frozen 0.40c came from a separate ~300-ticker sample stratified across both halves. Reconstructing the causal T-7 book for all 1,873 firing observations gives a mean half-spread of 0.4304c out-of-sample. The reconstruction reproduced the frozen midpoint on all 1,873 rows at four decimals, with the candle end landing exactly on T-7.
- **Fee relief.** Evaluating the fee at the executable price rather than the midpoint lowers it, since P(1−P) declines above 0.5.

## 4. Outcome and preserved residuals

**Formal outcome: D** — real errors in opposing directions that partly offset. The net direction is favorable, but D is the faithful classification of the mechanism, and §8-D requires the decomposition be preserved so the record shows errors that cancelled rather than no errors at all.

Two residuals remain open and are **not** resolved by that letter:

1. **Δ_waiver — formally undetermined, low qualitative residual risk.** `fee_waiver_expiration_time` was absent on all 1,873 firing markets and never observed populated on any of ~36,000 market records checked. But Kalshi's OpenAPI defines the field in ten words and specifies neither what null means nor whether the field persists after expiry, so absence cannot be promoted to a positive finding of inactivity.
2. **Event-level fee overrides — unquantified residual.** `EventData` carries `fee_type_override` and `fee_multiplier_override`, which supersede the series-level fee through a separate endpoint. All 1,873 frozen events were queried and returned zero override records, with a working positive control. But historical retention of that endpoint is undocumented, so the negative sweep is not a lifetime-completeness proof.

No retrieved evidence indicates either residual materially worsens the estimate. A fee waiver, if present, would reduce costs; an adverse event-level fee override remains logically possible but was not observed in the 1,873-event sweep. Neither residual is closed.

## 5. Cross-half cost comparison

The frozen study applied a single flat 0.40c to both halves. Measured separately:

| Quantity | IS | OOS |
|---|---:|---:|
| n firing | 1,951 | 1,873 |
| mean half-spread | 0.3922c | 0.4304c |
| corrected mean net (C=10) | +0.6202c | +1.2787c |

The pooled constant sat between the two, overcharging the in-sample half by 0.0078c and undercharging out-of-sample by 0.0304c. The crossing-cost component alone therefore reduces the OOS-minus-IS gap by 0.0382c relative to using the same flat 0.40c in both halves.

**Δ_generalization remains directionally intact after cost correction** — out-of-sample exceeds in-sample by 0.6585c on a rule selected in-sample. This is *not* strong statistical reassurance: with sd ≈ 32c on ~1,900 observations per half, the standard error of the difference is roughly 1.04c, so the observed gap is about 0.63 standard errors. Descriptively encouraging, statistically noisy. It is a robustness check, not a significance claim.

## 6. Methodological sensitivity, not changed post hoc

The frozen `bootstrap_ci` resamples trades i.i.d. with no clustering, while KXBTC15M and KXETH15M close on the same quarter-hour. If within-timestamp covariance between the two series is positive, the i.i.d. bootstrap understates uncertainty. That covariance has not been measured, so no claim is made about the direction or magnitude.

This is a property of the frozen methodology and was used unchanged. A read-only diagnostic may measure the paired covariance later without altering this result. Cluster-aware inference is recorded on the methodology backlog as a next-generation study-design question, not applied here.

## 7. Required sample

`KILL_CRITERIA.md` §9 and the fee-audit note §7 specify incompatible methods, and no algorithm exists in code. The resolution is recorded separately in `research/THREAD_A_REQUIRED_N_2026-08-22.md`, which fixes the methodology before the official calculation is executed — while disclosing that the corrected effect and the approximate consequences of the candidate methods were already known when it was written.

That calculation has now been executed once using §3 exactly as committed in `bd9d9e2`. No parameter was varied and it was not re-run.

| Quantity | C = 1 | C = 10 |
|---|---:|---:|
| mu (c/contract) | 1.2740896957 | 1.2787148959 |
| s, ddof=1 (c/contract) | 32.0638889019 | 32.0639100267 |
| (z₀.₉₇₅ + z₀.₈₀)·s/mu | 70.5050127115 | 70.2500374876 |
| **n_required** | **4,971** | **4,936** |
| additional beyond n=1,873 | 3,098 | 3,063 |
| calendar days @ 125.56/day | 24.67 | 24.39 |

Both outputs fall inside the approximations disclosed in the required-n note's §2 ("roughly 4.9k", "roughly 3.1k", "roughly 25 days") before the methodology was fixed.

**These are planning targets, not resolution points.** All three §3 caveats attach and are not softened by having a number: this is a normal approximation, not an exact power calculation for the percentile-bootstrap PASS procedure, so reaching n_required does not establish 80% bootstrap power; it is conditional on stationarity, which for a trading edge is the assumption most likely to fail; and it is an unblinded internal sample-size re-estimation using mu and s from the same 1,873 markets that will remain in the combined sample, so the nominal α and power are planning approximations rather than a formal error-rate guarantee.

The §2 PASS bar is unchanged and remains unmet.

## 8. Where this leaves Thread A

The corrected estimate is **+1.28c per contract with a 95% CI of approximately [−0.18c, +2.68c]**. The point estimate is positive; the interval straddles zero. Thread A does not clear its §2 PASS bar. This amendment does not establish a negative EV either: the point estimate remains positive, but unresolved.

The audit did what it was written to do: it found genuine cost-model errors, quantified them term by term, established that they largely cancel, and demonstrated that correcting them neither rescues nor kills the signal. What remains is the same problem the frozen result had — a positive estimated edge with uncertainty too wide to resolve it.
