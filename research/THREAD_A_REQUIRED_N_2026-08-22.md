# Thread A — Required-Sample Methodology Amendment

**Date written:** 2026-08-22
**Status:** methodology fixed before the official required-sample calculation is executed, but *after* the corrected OOS effect and the approximate consequences of the candidate methods were known. This is a prospective calculation rule, not a blinded pre-registration.
**Supersedes:** the required-sample formula in `KILL_CRITERIA.md` §9. See §2 below.

---

## 1. Why this note exists

Two committed documents specify incompatible methods for computing Thread A's required sample.

`KILL_CRITERIA.md` §9 (lines 262-263) states:

> required sample = current sample × (current CI half-width / corrected point estimate)², **Not chosen by inspection.**

`research/THREAD_A_FEE_AUDIT_2026-08-22.md` §7 item 2 states the opposite:

> Required sample, recomputed from trade-level P&Ls. **Not by rescaling the old CI.** … Any figure derived by scaling … is an order-of-magnitude sanity check only, never the reported requirement.

§9's formula *is* a scaling formula. §7 forbids scaling and specifies no replacement — no power target, no alpha, no estimator, no stopping rule. No required-sample algorithm exists in code either (`grep` for `required.*sample`, `required_n`, `power`, `sample_size` across `test_longshot_rule.py`, `market_calibration_study.py` and `check_longshot_spread.py` returns zero hits).

The audit's bootstrap step therefore stopped rather than choose between them. That was correct: selecting a methodology after the corrected estimate exists is exactly the post-hoc move both documents were written to prevent.

## 2. Disclosure of the stakes, before fixing the method

This document is written with knowledge of what each candidate method produces. Pretending otherwise would be a fiction, and an undisclosed one would be worse than the disclosure.

| Method | Total required | Additional beyond n=1,873 | Approx. calendar time |
|---|---|---|---|
| §9 scaling formula | ~2,339 | ~466 | under 4 days |
| §3 below (80% power) | roughly 4.9k | roughly 3.1k | roughly 25 days |

The adopted methodology implies materially more additional observations than §9's scaling formula. That consequence was known before the methodology was fixed.

The reason for adopting it does not depend on the corrected estimate: **§9's formula solves for the sample at which the confidence interval just touches zero, which is approximately 50% power.** The operative planning target already in the record (~5,080 markets) was produced by adjusting §9's output upward toward 80% power — recorded as a decision with its result, not as a reproducible formula. So 80% power was already the intended standard; §3 makes it explicit and reproducible rather than establishing a new one.

Noted for transparency after the choice, and *not* a reason for it: the approximate result of §3 happens to fall near that previously operative ~5,080 target.

## 3. The adopted methodology

Required sample is computed from the **corrected out-of-sample trade-level P&L vector**, not from any prior confidence interval:

```
n_required = ceil( ( (z_0.975 + z_0.80) * s / mu )^2 )
```

where:

| Term | Definition |
|---|---|
| `mu` | mean of the corrected OOS trade-level P&L vector, cents per contract |
| `s` | sample standard deviation of that same vector |
| `z_0.975` | 1.959964 — two-sided α = 0.05, matching the 95% CI convention already in use |
| `z_0.80` | 0.841621 — 80% power |
| unit | one firing market (one T-7 firing observation), matching the frozen i.i.d. bootstrap's inferential unit |

Interpretation: a normal-approximation planning sample size targeting approximately 80% power to detect an effect of the observed corrected magnitude at two-sided α = 0.05. It is **not** an exact power calculation for the percentile-bootstrap §2 PASS procedure. The eventual PASS determination remains the frozen bootstrap criterion, not this normal approximation. Reaching n_required does not establish that the study had 80% bootstrap power.

**This is a planning sample size, not a guaranteed resolution point.** It is conditional on the future firing-market P&L distribution having approximately the same mean and variance as the corrected OOS sample, and on the frozen i.i.d. inferential assumption remaining applicable. Effect decay, regime change, or dependence between observations can change the realized power. For a trading edge, stationarity is precisely the assumption most likely to fail — reaching the computed n is a condition for resolution, not a promise of it.

**Adaptive-design caveat.** `n_required` is selected using the observed mean and variance of the existing 1,873 OOS firing markets, which are expected to remain in the eventual combined sample. This is therefore an unblinded internal sample-size re-estimation, not a fixed-N design chosen before observing outcomes. The nominal α and power in §3 are planning approximations and should not be interpreted as a formal error-rate guarantee for the eventual adaptive combined-sample test. The §2 PASS rule itself remains unchanged.

### Reported for both fee-quantity sensitivities

`C=1` and `C=10`, neither designated primary, consistent with the audit note. `C=10` remains a fee-normalization sensitivity at an observed marginal top-of-book price; historical depth is unavailable, so it is not a proven ten-contract execution backtest.

### Calendar conversion

Using the firing rate measured over the frozen OOS window, declared here before use:

- Window 2026-08-06T22:15:00Z → 2026-08-21T20:15:00Z, span 14.9167 days
- 2,830 markets, of which 1,873 fired (66.18%)
- **125.56 firing markets per day**

Calendar time = (n_required − 1,873) / 125.56, reported in days. Any change to the firing-rate denominator or window requires amending this note first.

## 4. What this calculation may not do

- It may not change the threshold, slice, direction, market selection, filters, or the IS/OOS partition.
- It may not be re-run with different power or alpha after seeing its output. One run.
- It may not split by series to find a subset with a smaller requirement.
- It may not be used to argue that Thread A has passed. The §2 PASS bar is unchanged and is not met: corrected mean +0.0128 with 95% CI [−0.0018, +0.0268], lower bound negative.
- It may not silently adopt cluster-aware inference. See §5.

## 5. Known methodological limitation, deliberately not fixed here

The frozen `bootstrap_ci` (`test_longshot_rule.py:361-371`) resamples trades i.i.d. with no clustering, while KXBTC15M and KXETH15M close on the same quarter-hour. If within-timestamp covariance between the two series is positive, the i.i.d. bootstrap understates sampling variance. That covariance has **not** been measured, so no claim is made here about the direction or size of the effect.

This is a property of the frozen methodology and is not changed post hoc. Changing the inferential unit now would be new methodology introduced after seeing the result. A read-only diagnostic measuring the paired covariance may be run later without altering the frozen result, and cluster-aware inference is recorded on the methodology backlog as a next-generation study-design question.

The required-sample calculation in §3 uses the same i.i.d. unit as the frozen analysis, for consistency with it.

## 6. Amendment record

This note supersedes `KILL_CRITERIA.md` §9's required-sample formula for Thread A. §9's other provisions are unaffected. The superseded formula is preserved above in §1 and its approximate output in §2, so the change is visible rather than silent.

The precise required sample under §3 has not been calculated at the time of writing. It is to be computed once, after this note is committed, and not re-run with different parameters after its output is seen.

Nothing else about Thread A's frozen rule, sample, or PASS bar is altered by this note.
