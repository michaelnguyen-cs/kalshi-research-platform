# KILL_CRITERIA.md

**Pre-registered. Written 2026-08-20, before any of the four remaining tests were run.**

Commit this. Do not edit it after seeing any result the document governs. If a
number below turns out to be wrong, the correct response is to record the
failure, not to move the number. An amendment is only legitimate if it is made
before the relevant test is executed, is committed as a separate dated entry
below, and states what prompted it.

---

## 0. Purpose

Five hypotheses have already been killed by evidence (lag, direction
prediction, market miscalibration, cross-venue arb, ETH-specific edge). Four
threads remain open. This document fixes, in advance, what each of them has to
do to survive and what happens when they all fail.

The reason this exists: the project is now at the stage where every remaining
lead is weak, and weak leads are exactly where a motivated researcher finds
signal that isn't there. Bankroll is ~$<BANKROLL>. A true null is a better outcome
than a fake win.

---

## 1. Standing rules (apply to every test below)

1. **No threshold, slice, window, or bucket definition changes after seeing
   output.** If a rule is frozen, it is frozen including the code path that
   computes its slices.
2. **Every backtest number requires an out-of-sample check and a
   multiple-comparisons correction.** In-sample point estimates are reported
   for context only and never justify a decision.
3. **Controls are fee-drag baselines.** Beating the random-side, cheaper-side,
   or longshot control is not evidence — `fee(P) == fee(1-P)` makes the
   random-side control an arithmetic identity. The binding test is always a
   95% CI excluding zero.
4. **Every EV number must clear fees AND spread.** A mid is not a fill.
   Candle-derived mids produce an upper bound, never a tradeable estimate.
5. **If a result implies free money, it is a bug until proven otherwise.**
   This has been true three times out of three.
6. **One look per test.** No interim peeking at an accumulating dataset. If a
   look happens anyway, it counts as the test and its result stands.
7. **A pass is a candidate, not a conclusion.** See §7.

---

## 2. Thread A — favorite-longshot frozen rule

**Rule (frozen, unchanged):** buy the favorite (side priced >= 0.50) at price
>= 0.70, at the T-7min slice.

**Code pin:** commit `6ca4101` ("Make the longshot rule an input, not a sweep
result") — the first commit whose tree takes the rule as an input argument
(`--rule-threshold 0.70 --rule-slice 7`) instead of re-deriving it from an
in-sample argmax on every run. It also carries the order-level fee in both
`test_longshot_rule.py` and `market_calibration_study.py`. (Moved from
`5b6e95f` on 2026-08-21, itself moved from `9c6850c` earlier that day under the
§9 fee-correction authorization. This move ENFORCES this section rather than
altering it: the pinned rule below is unchanged, and the new pin is what makes
the code read that rule from this document instead of re-choosing one. See §9,
2026-08-21.)

If the horizon-relative slice refactor lands in `market_calibration_study.py`
or `scan_all_series.py`, this test runs against the pinned commit or a forked
helper — not against `main`.

**Precondition — SATISFIED 2026-08-20.** Median half-spread at the T-7 slice is
**0.50c** (mean 0.43c, max 1.50c); cost of crossing is **0.40c/contract**,
measured on 300 tickers sampled evenly across 2026-07-22 to 2026-08-21 via
`scripts/check_longshot_spread.py`. Zero rows dropped for a missing causal
candle or a one-sided quote. The rule survives the spread: mid EV +1.29c
against executable EV +0.89c on that sample. That sample straddles the
in-sample/out-of-sample split, so only the 0.40c crossing cost transfers.

**Order size (pre-committed 2026-08-21, before any re-run):** **C = 10
contracts per order.** Required because an order-level fee ceiling is undefined
without an order size. Chosen as a size actually tradeable at a $<BANKROLL> bankroll;
at P=0.70 the per-contract fee is 1.50c at C=10 against a 1.47c asymptote, so
the choice moves the answer by less than 0.03c and is not doing any work.

**Data:** markets with `close_ts` strictly after 2026-08-21 00:00 UTC. Target
set in §9.

**Evaluation date:** set in §9. One evaluation.

**PASS:** 95% bootstrap CI lower bound > 0 on EV/contract, net of the
order-level fee at C=10 and net of the measured 0.40c crossing cost,
clustered appropriately.

**FAIL:** anything else, including a CI that narrowly straddles zero.

**Explicitly not permitted:** changing the price threshold, changing the slice,
adding a filter, splitting by series post hoc to find a subset that passes,
changing C after seeing output, or extending the collection window because the
CI is "close."

---

## 3. Thread B — slow-crypto news response

**Script:** `analyze_crypto_slow.py` against the 24h `record_crypto_slow.py`
recording.

**PASS:** the script's own pre-registered response criterion is met AND the
shock-clustered bootstrap CI excludes zero. Note that criterion 2 takes trade
direction with hindsight, so it is an upper bound — it can only ever
contribute to a FAIL being decisive, never to a PASS being convincing.

**FAIL:** criterion not met, or CI includes zero.

**INSUFFICIENT:** fewer than 30 shocks. One re-run of the recording is
permitted. If the second run also lands under 30 shocks, record FAIL.

**RESULT 2026-08-22 — FAIL, both hypotheses.** Recording 2026-08-21T01:22Z to
2026-08-22T01:22Z, 8,604 poll cycles, 519 Kalshi markets, 106 shocks. Criterion
4 (>= 30 shocks) PASSED, so this is a verdict and not INSUFFICIENT. Full
output: `data/crypto_slow_analysis_20260822.txt`, sha256
`482a85471b6a5f55f4893a6aea6c3b872c0fd39b930a2e6594b2ba594bc39b0b`
(committed from the VM as `bb79f50`).

- **H1 news-response lag: FAIL on criteria 1, 2 and 3.** Median Kalshi mid move
  after a shock is 0.00c at +1m, +5m, +15m, +30m and +60m (n~30,400 each),
  reaching 0.50c only at +360m. Median tradeable edge at shock+60s is 0.00c,
  95% CI [0.00, 0.00] clustered on shock. Median size at that price is 0.0
  contracts. The reaction-time sweep is flat at 0.00c across 0s/60s/300s/900s.

- **H2 cross-venue spread: FAIL.** 0 of 70 HIGH-confidence pairs survived
  resolution equivalence; B5 (different settlement source and quote currency)
  applied to all 70, B4 to 64, B7 to 6, B6 to 4. Kalshi settles on CF
  Benchmarks trimmed mean in USD, Polymarket on Binance 1-minute candles in
  USDT, with observation windows starting hundreds to thousands of hours apart.
  The observed gaps are the price of two different contracts.

**DESIGN NOTE, independent of every criterion.** These series settle
monthly-to-annually, so a 24-hour recording contains approximately zero settled
outcomes. Distinguishing the observed 0.5c edge from zero at 80% power against a
binary payout's ~50c per-contract standard deviation requires ~78,400 settled
trades, which at ~519 live markets per settlement round is on the order of 151
years. Profitability was never measurable on these series. A PASS would have
been unfalsifiable. Future threads must check measurability before the
recording, not after.

**NOT PURSUED, recorded so it is not rediscovered as novel.** The pooled 0.00c
median hides dispersion — 180 of 519 markets never moved their mid, while
KXBTCPRICE (9.50c), KXZECMAXMON (5.00c) and KXBTCMAXMON (3.50c) do reprice at
+60m. Selecting those after a failed test is the post-hoc move section 6
forbids, and it would still fail criterion 3 and the measurability wall above.

---

## 4. Thread C — exchange-wide scan (hourly commodities, MLB props)

**Script:** `scan_all_series.py`.

**Scheduling note (not a criterion):** MLB regular season ends late September.
Scan MLB now to bank the historical data. The tradeable target is the hourly
commodities, which run year round.

**PASS requires all four:**
1. Survives Benjamini-Hochberg FDR at q = 0.10, with the total number of tests
   reported alongside the result.
2. Holds in both halves of the time split (discovery half and confirmation
   half), same direction, both nominally positive after fees and spread.
3. Clears the liquidity gate: median size at touch **>= 20 contracts** at the
   traded price, via `--probe-liquidity` at comparable time-to-close. Same bar
   the lag study pre-registered, where only 18.3% of gap observations cleared
   it. **Caveat:** the historical depth numbers came from a parser that
   truncated fractional quantities in both directions, so this statistic must
   be recomputed from `kalshi_raw` before the gate is applied.
4. **Forward out-of-sample.** A scan hit is a candidate. It must be re-tested,
   frozen and unchanged, on data generated strictly after the scan date,
   against the same CI bar as Thread A.

**Per-series fee note:** `fee_multiplier` is per-series and is not always 1
(observed values: 0, 0.5, 1). Read `fee_type` and `fee_multiplier` from
`GET /trade-api/v2/series/{ticker}` for every scanned series rather than
assuming 0.07. No historical fee endpoint exists, so record the values at scan
time.

**FAIL:** any of the four not met.

---

## 5. Thread D — same-market arb within Kalshi

**Status:** measurement only. Frequency, size, and duration are currently
unknown; one detection has been observed (2026-08-08, KXETH15M).

**Prerequisite:** a book-parsing script built on a single tested
`read_book()` helper with a hard `assert ask >= bid`. The stored
`best_bid`/`best_ask` columns are known bad (mean spread -0.344) and must not
be used. Reconstruct from the raw messages in `kalshi_raw`.

**Fee reality:** recompute at the order-level ceiling with C = 10, not the
per-contract form. The originally logged 1.5c net on the 0.52 + 0.43 example
used the wrong fee form and must be redone.

**PASS requires all four:**
1. Frequency **>= 5 opportunities per trading day**, measured across at least
   3 separate days. One detection is an anecdote.
2. Size **>= 20 contracts** resting on BOTH legs simultaneously — a
   one-contract phantom level is not an arb.
3. Net **>= $0.01 per contract** after both legs' fees at the actual observed
   prices.
4. Persistence **>= 2 seconds**, measured against your own round-trip
   latency, with both legs fillable. Single-leg fills leave you directionally
   exposed, which is the thing this whole project has failed at.

**FAIL:** any of the four not met.

---

## 6. The kill condition

**If Threads A, B, C, and D all record FAIL, the thesis is dead.**

The thesis being killed is specific: *"a retail participant with this tooling
and this bankroll can find and harvest a statistical edge on Kalshi."*

On a kill, exactly two options remain:

- **(a) Market making on slow markets** (weather, monthly economic releases) —
  a structurally different bet: getting paid the spread for providing
  liquidity rather than out-predicting anyone. This is a *new project* with
  its own pre-registration document, not a continuation of this one. It may
  only begin after a kill, and only if the scan results indicate where.
- **(b) Stop.**

**Not permitted on a kill:** generating a fifth hypothesis by inspecting the
four failures for something that "almost worked." That is the same
post-hoc-selection failure mode as the 46 flagged calibration buckets, run at
the level of the whole project.

---

## 7. Capital gate (applies even to a PASS)

A PASS on any thread does not authorize real money. Before any live capital
moves, all of the following must be true:

1. The vol estimator is either fixed or explicitly bypassed for the strategy in
   question. A wrong pricer is a liability regardless of whether the edge is
   real.
2. The champion calibrator is refit or cleared. It was fit against a fair_p
   compressed near 0.5 and learned a steep amplifying map; applying it to a
   corrected fair_p double-counts.
3. `max_position_pct` is checked. Smaller sigma means larger claimed edge means
   larger Kelly sizes — the deploy risk pulls opposite to the de-selection.
4. A paper run on the fixed code, on live books, with fill assumptions that are
   pessimistic rather than optimistic. Same-process sim has historically shown
   ~16 points of fill flattery versus live.
5. `systemctl is-enabled kbot` reflects intent. Stopped is not disabled.
   *(Checked 2026-08-20: was `enabled` while stopped; now disabled.)*
6. The five-item integrity patch is applied and tested: fixed-point
   quantities, fail-closed on a stale Kalshi book, sequence-gap invalidation,
   explicit `use_yes_price=False`, and no fabricated final-five-minute
   evaluation rows.
7. `src/kbot/edge.py` uses the same order-level fee function as the research
   scripts. It currently uses a third, unrounded form with no ceiling.

---

## 8. Project-level stall clause

If, by **2026-12-31**, fewer than three of the four threads have been executed
to a recorded PASS or FAIL, that is itself a result: the project is consuming
attention without producing decisions. Record it here and treat §6 as
triggered.

---

## 9. Amendments

*(Dated entries only. Each must state what prompted it and must predate the
test it affects.)*

- **2026-08-21 — fee model correction.** `fee_per_contract` in
  `market_calibration_study.py` fixes the contract count at 1 inside the
  ceiling. Kalshi's ceiling applies at the **order** level with the contract
  count inside it. In this rule's firing zone the wrong form overstates fees by
  roughly 0.4–0.9c/contract (at P=0.70: 2.00c charged against 1.47c true; at
  P=0.80: 2.00c against 1.12c), which is larger than the measured crossing cost
  and comparable to the whole edge estimate. Prompted by Kalshi's published
  schedule and `docs.kalshi.com/getting_started/fee_rounding`, not by any
  result.

  **Consequences, all decided before the corrected test is run:**
  - Order size pre-committed at **C = 10** (§2).
  - `fee_per_contract` will be corrected in place, the pin moved to the
    resulting commit, and the frozen OOS test re-run. **The rule itself does
    not change** — favorite, price >= 0.70, T-7min slice. Only the fee
    arithmetic changes, from wrong to right.
  - The corrected OOS point estimate **replaces** the +1.14c figure and is
    subject to the same unchanged CI bar.
  - Sample target and evaluation date are set once the corrected OOS estimate
    exists, by the formula: required sample = current sample x (current CI
    half-width / corrected point estimate)^2. Not chosen by inspection.

  **Scope of the corrected re-run (decided 2026-08-21, before the corrected
  run):** re-running the frozen rule on the EXISTING `calib_full.csv` window
  with corrected fees is a recomputation of an already-failed test, not a new
  look, and does not consume section 1.6's one look. It does NOT constitute a
  PASS regardless of outcome — section 2 requires data with `close_ts` after
  2026-08-21. A corrected CI that excludes zero on existing data shrinks the
  October sample requirement via the section 9 formula but does not substitute
  for the October test.

  **Verified, no action needed:** `KXBTC15M` and `KXETH15M` both read
  `fee_type: quadratic`, `fee_multiplier: 1` as of `last_updated_ts`
  2026-08-04, so the 0.07 base rate is correct for both studied series. No
  historical fee endpoint exists, so the multiplier during the study window
  cannot be verified — recorded as a known, accepted gap.

- **2026-08-21 — the rule became an input, not a sweep result.** Procedural
  failure recorded. `test_longshot_rule.py` re-derived its rule from an
  in-sample sweep on every run rather than reading the pinned rule. Under the
  corrected fee the argmax moved from >= 0.70 to >= 0.55 and the script
  reported an out-of-sample PASS on the 0.55 rule (n=2,615, +0.0160, CI
  [+0.0021, +0.0304]). That result is NOT a Thread A pass: section 2
  pre-registers >= 0.70 and forbids changing the threshold. The 0.55
  out-of-sample result has now been seen, which is an unrecoverable partial
  look at the out-of-sample half; it is recorded here rather than discarded.
  The rule is now an input argument so the document governs the code. The
  pre-registered >= 0.70 rule has not yet been evaluated out-of-sample under
  corrected fees.

- **2026-08-21 — pre-registered rule evaluated under corrected fees.**
  Pre-registered rule evaluated under corrected fees on the existing window (a
  recomputation, not the October test). Buy the favorite at >= 0.70, T-7min:
  n=1,873, win 87.7%, mean EV +0.0164 at the mid, 95% CI [+0.0018, +0.0304].
  Net of the 0.40c crossing cost measured 2026-08-20: +0.0124, CI [-0.0022,
  +0.0264]. The lower bound is negative, so section 2's PASS is NOT met. The
  script prints PASS because it does not subtract the crossing cost; section 2
  requires both.

  **Sample target set.** The section 9 formula gives 1,873 x (0.0143/0.0124)^2
  = ~2,492 firing markets, but that is a 50%-power threshold and section 1.6
  permits one look. Target is therefore set at ~5,080 firing markets (~7,670
  total, ~80% power), evaluation date 2026-10-05, whichever is later. This
  adjustment makes passing harder, not easier. Command:
  `python scripts/test_longshot_rule.py --rule-threshold 0.70 --rule-slice 7`,
  with the crossing cost subtracted by hand.

---

## 10. Sign-off

Frozen by: __________  Date: __________  Commit: __________
