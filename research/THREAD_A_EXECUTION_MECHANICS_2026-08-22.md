# THREAD_A_EXECUTION_MECHANICS_2026-08-22.md

**Frozen 2026-08-22, before any depth-feasibility data has been recorded or
inspected.**

This document fixes the execution mechanics needed to *measure whether Thread A
is feasible*. It is deliberately **not** the confirmatory pre-registration.
KILL_CRITERIA.md §2 and §9 are rewritten only after the feasibility pilot
reports, and nothing here may be revised on the basis of what that pilot shows.

---

## 0. Why this exists

Thread A's pilot (the 1,873-trade Aug 6–21 out-of-sample half) is priced at
candle **midpoints**. A mid is not a fill. The confirmatory test must instead
use the price actually payable and must establish that the intended order could
have been filled at that price.

Candles carry `yes_bid`/`yes_ask` **prices** but no **sizes**, so depth cannot
be reconstructed retrospectively. Depth must therefore be **recorded live**,
which makes the recorder a prerequisite for the confirmatory window rather than
an analysis step within it.

The purpose of the feasibility pilot is to answer one nuisance question:
*what fraction of otherwise-firing signals are actually executable at an
already-frozen order size?* Everything below exists to make that measurement
honest.

---

## 1. Order quantity

**X = 10 contracts per firing market.**

X was selected on 2026-08-22, before any T−7 depth-clearance data existed,
on three grounds independent of the forthcoming liquidity result:

1. **Continuity.** The pilot's entire fee model is already C = 10, and
   KILL_CRITERIA.md §2 names it.
2. **Bankroll.** At roughly $0.80 × 10 ≈ $8 per position, a ~$<BANKROLL> bankroll
   supports roughly 13–14 concurrent positions before fees and any cash
   buffer.
3. **Fee economics.** At X = 1 the taker fee is 2.00c at P = 0.70 against
   1.47c at order level. The X=10-versus-X=1 gap runs about 0.50c at P = 0.70,
   0.90c at P = 0.80 and 0.30c at P = 0.90 — large enough that X = 1 would
   plausibly erase the entire edge estimate.

X trades fee efficiency against eligibility: a larger X improves the fee per
contract but reduces the fraction of books that can fill the order. Freezing X
before measuring that trade-off is the point.

**X may not be changed on the basis of observed depth clearance.** If X = 10
proves infeasible, that is a feasibility finding to be recorded, not a
parameter to retune.

---

## 2. Executable price

A signal's entry price is the **best executable ask on the side the frozen rule
buys**, taken from the reconstructed book at the T−7 selection moment defined
in §3. It is not a midpoint, not a last trade, and not an average.

No partial-fill substitution, order-size reduction, walking to an alternative
price level, or post-hoc depth threshold is permitted.

---

## 3. Book validity and T−7 selection

**Target time** = `close_ts − 420s`.

**Book state**: use the reconstructed book as it existed at or immediately
before target time.

**A book is VALID only if** it originated from a fresh `orderbook_snapshot` on
the current WebSocket session **and** every applicable delta through target
time arrived in valid sequence with no unrepaired gap. A disconnect or a
sequence gap invalidates the book until a new snapshot is received.

**Session health**: at target time the WebSocket session must independently be
alive — a Kalshi Ping/Pong or other authenticated session activity within the
preceding **30 seconds**. Kalshi sends Ping frames every 10 seconds, so this is
a test of the *connection*, not of market activity.

**Absence of a market-specific book update within 30 seconds does not make a
book stale.** An unchanged book remains valid while session and sequence state
remain valid. Quiet markets are disproportionately the illiquid ones;
classifying them as unrecorded would remove them from the denominator and
inflate the measured clearance rate in the favourable direction.

*If the recorder is implemented by polling rather than a continuous
subscription, staleness is bounded by the poll cadence by construction and this
rule must be restated in those terms. That is a recorder-design fact, not a
result, so restating it is permitted — but only before the feasibility clock
starts.*

---

## 4. Classification of every otherwise-firing signal

Exactly three buckets, which must sum to every signal the frozen rule would
fire on. **The first two are never merged.**

- **UNAVAILABLE** — the recording or session could not establish trustworthy
  state. Itemised by reason:
  - `UNAVAILABLE_SCHEDULED_MAINTENANCE` (Kalshi's Thursday 03:00–05:00 ET
    window; trading is paused and disconnections are expected)
  - `UNAVAILABLE_DISCONNECT`
  - `UNAVAILABLE_SEQUENCE_GAP`
  - `UNAVAILABLE_NO_SNAPSHOT` (startup, or market not yet subscribed)
  - `UNAVAILABLE_SESSION_STALE`
  - `UNAVAILABLE_OTHER`
- **NON-TRADEABLE** — trustworthy state existed, but the X = 10 order could not
  execute: no executable ask, or `size_at_ask < 10`.
- **ELIGIBLE** — trustworthy state, an executable ask, and displayed size ≥ 10
  contracts.

Data-quality failure and genuine illiquidity are different things. Recorder
downtime must never be allowed to depress the measured depth-clearance rate.

Scheduled-maintenance signals are **ineligible by design** for the eventual
strategy, since the trade could not have been placed — but they remain in the
raw census so downtime cannot be hidden by deleting known-bad hours.

---

## 5. Reported quantities

- **Raw availability** = VALID / ALL signal opportunities
- **Technical availability** = VALID / (ALL − SCHEDULED_MAINTENANCE)
- **Depth clearance** `D` = #(VALID ∧ size@ask ≥ 10) / #VALID
- **Effective executable yield** `Y` = #(VALID ∧ size@ask ≥ 10) / #(ALL
  otherwise-firing signals)

`Y` is the number that determines how quickly usable observations accumulate,
and therefore whether the confirmatory window is reachable at all.

---

## 6. Price convention

**The research recorder subscribes to Kalshi orderbook WebSocket channels with
`use_yes_price: true`, explicitly. No API default is relied upon.**

Kalshi documents that `orderbook_snapshot`/`orderbook_delta` use different
YES/NO price scales by default, that the default is planned to flip to
YES-scale, and that explicit `false` will eventually stop preserving the legacy
convention once the flag is removed. Across a multi-month frozen recording, an
unchanged codebase could otherwise silently change semantics.

All book reconstruction and `read_book()` semantics use the unified YES-leg
price scale. NO economics are derived **algebraically** from the same canonical
book rather than by maintaining a second crossed convention.

**The production trading bot is not migrated.** It keeps `use_yes_price:
False`. Migrating it as part of this task is unnecessary coupling and another
way to contaminate the research project.

Subscription parameters, API host, and the recorder Git commit are logged at
recorder startup.

---

## 7. Recorder contract

Every persisted record carries:

- market ticker, series, `close_ts`
- snapshot timestamp; exchange timestamp where available **and** local receive
  timestamp
- sequence and subscription identifiers
- YES price/size levels (canonical scale)
- reconstructed best bid/ask and size at each
- validity state: VALID, or the specific UNAVAILABLE reason
- the **recorder Git commit**, stamped into every row
- the raw WebSocket message or raw book state needed for independent
  reconstruction

Raw messages are retained so that, if the derived parser is later questioned,
the frozen data can be replayed through an independently written decoder. A
derived `ask=…, size=…` row alone is not sufficient — this project has hit
book-convention bugs three separate times.

`read_book()` enforces, as hard invariants after reconstruction:

- `yes_bid <= yes_ask`
- prices within valid range
- quantities non-negative
- no duplicate or crossed levels after applying deltas
- sequence and session state VALID

Any sequence gap or disconnect invalidates the reconstructed book until a fresh
snapshot repairs it. **A pre-gap book is never carried forward as though
current.**

Health gaps and restart events are logged.

---

## 8. Pre-window equivalence test

Before the recorder is production-ready it must pass a dual-stream equivalence
test:

- Subscription **A**: `use_yes_price: true`
- Subscription **B**: `use_yes_price: false`
- Run on **separate WebSocket connections**. Each subscribe creates its own
  subscription identifier and sequence state; mixing conventions on one session
  invites exactly the state confusion this test exists to rule out. Confirm the
  account permits two concurrent connections.
- Transform B into YES coordinates and compare reconstructed top-of-book prices
  and sizes market by market.

### Equivalence-test sampling protocol

Every parameter below is frozen before the first comparison and may not be
altered in response to an equivalence result.

**Duration.** The test begins only after both connections are healthy and both
decoders have received fresh snapshots. It runs for a minimum of **60
continuous minutes**. After 60 minutes it stops at the first point when
**≥ 10,000 matched market-grid comparisons** have been obtained, with an
absolute cap of **240 minutes**. Fewer than 10,000 matched comparisons at 240
minutes is **INSUFFICIENT**, and the test reruns without changing the universe,
the grid, the skew tolerance, the duration rules, or the pass bar.

**Validation universe.** The test uses the recorder's full production
subscription universe rather than Thread A's alone, because it validates the
decoder rather than the strategy. Wider coverage is desirable: it exercises
more price and tick regimes, including deci-cent prices above $0.90.

The universe is **all currently open markets in exactly these series**, frozen
here and not modifiable in response to any equivalence result:

| Purpose | Series |
|---|---|
| Thread A | `KXBTC15M`, `KXETH15M` |
| Thread C — MLB props (perishable; season ends late September) | `KXMLBHRR`, `KXMLBTB`, `KXMLBHIT`, `KXMLBRBI`, `KXMLBHR` |
| Thread C — hourly commodities (year-round) | `KXGOLDH`, `KXWTIH`, `KXSILVERH`, `KXPLATINUMH`, `KXPALLADIUMH` |

Individual market tickers within these series rotate (MLB games daily, hourly
commodities hourly, crypto quarter-hourly), so the frozen object is the
**series list plus the predicate "currently open"**, not a ticker enumeration.

**Connection limits.** N is the documented Kalshi API or account subscription
limit — it is read from the documentation, never chosen. If N binds, eligible
markets are sorted by `(series_ticker ASC, market_ticker ASC)` and the first N
are subscribed. **No selection may depend on observed decoder agreement.**

*Note on sizing: 60 minutes on a 1 Hz grid yields 3,600 timestamps, and
KXBTC15M/KXETH15M run one binary per event — roughly two open markets at a
time, or about 7,200 comparisons. The broader universe is what makes 10,000
reachable, which is why it is frozen here rather than added later.*

**Grid.** Reconstructed books are compared on a fixed **1 Hz UTC time grid**.
At each grid timestamp, a market is compared only if **both** streams are
VALID. Both streams are transformed to the same canonical YES-scale
representation before comparison.

**Skew tolerance.** A disagreement is classified as **explainable by timing
skew** only if the stream that is behind reaches the other stream's canonical
top-of-book state within **500 ms**, with no intervening contradictory
top-of-book state.

**PASS requires both:**

1. ≥ 99.9% of matched comparisons agree exactly on canonical best bid
   price/size and best ask price/size; **and**
2. every remaining disagreement is explainable by the frozen ≤ 500 ms
   timing-skew rule.

Any unexplained disagreement is a **FAIL**, and the production feasibility
clock may not start.

Because condition 2 fails on *any* unexplained disagreement, the 99.9%
threshold is not permission to tolerate parser errors. It permits benign
asynchronous observation and nothing else.

The B stream is retired once the test passes. Production is frozen on `true`.

**Validation data is engineering/test data and is excluded from all feasibility
and confirmatory samples.**

---

## 9. Feasibility pilot

**Duration: exactly three complete UTC days**, fixed in advance so the
measurement cannot be stopped at a moment when clearance happens to look
favourable.

**Start condition:** the three days begin only after the recorder has passed
all acceptance tests, including §8. Testing and debugging days do not count.
The clock start is **logged and committed** so it cannot drift retrospectively.

**May be inspected:**

- recorder health and uptime
- raw availability and technical availability, itemised by reason
- depth clearance `D`
- effective executable yield `Y`

**May not be inspected:** settlements, EV, winners or losers, markouts,
series-specific profitability, alternative thresholds, alternative order sizes,
or individual "interesting" cases.

Estimating a nuisance quantity — what fraction of otherwise-firing observations
are executable at an already-frozen order size — is legitimate feasibility
planning. Optimising the trading rule on those days would consume them as
another pilot, and the pilot data is already spent once.

**Sustained unavailability is a feasibility finding in its own right**, not a
nuisance to be patched mid-window. The three 2-hour study recordings showed
Kalshi WebSocket gaps covering 22.9%, 40.6% and 22.9% of their windows; if
anything similar persists, a large fraction of T−7 moments are UNAVAILABLE and
the accumulation rate suffers accordingly.

---

## 10. Recorder version changes

- **During the feasibility pilot:** a recorder bug may be fixed, but doing so
  **restarts the three days from zero**.
- **During the confirmatory window:** any change affecting book semantics,
  validity classification, snapshot/delta reconstruction, price or size
  interpretation, or T−7 extraction **ends that dataset**. A newly frozen
  version and a fresh start boundary are required.
- Purely operational changes that provably do not alter recorded semantics (log
  rotation, disk management) are documented but still avoided during
  confirmation unless genuinely necessary.

---

## 11. What this document does not do

It does not set the confirmatory sample size, the evaluation date, the stopping
rule, or the PASS bar. Those go into KILL_CRITERIA.md §2 and §9, written **after**
the feasibility pilot reports and informed only by availability, `D` and `Y` —
never by any profitability figure.

If the feasibility pilot shows that the required number of eligible
observations cannot realistically be reached before the **KILL_CRITERIA.md §8**
stall deadline of 2026-12-31, **the depth rule is not weakened to save the schedule.** The
feasibility problem is recorded, and the decision is whether Thread A fails,
stalls, or whether a substantively different strategy deserves its own new
pre-registration.

---

## 12. Sign-off

Frozen by: __________  Date: 2026-08-22

Git provenance: the commit that first introduces this file. A document
cannot contain its own final SHA — writing one in would change it — so the
Git history is the immutable record.

---

## 13. Amendments

Appended entries only. Nothing above this line is edited in place: §8's
thresholds, grid, skew tolerance and pass bar stand exactly as frozen, and an
amendment that changed one of them after seeing a result would not be an
amendment, it would be the failure this document exists to prevent.

### 2026-08-22 — §8 clarification: what "continuous" duration means

**Status:** clarification of an ambiguous term. Changes no threshold, no grid,
no skew tolerance, no pass bar, no universe, and no duration rule.

**Circumstances.** §8 requires the equivalence test to run for "a minimum of 60
continuous minutes". A pre-acceptance smoke run of the recorder on 2026-08-22
lost both WebSocket connections simultaneously about 62 seconds in and took
roughly 50 seconds to re-establish, which made the reading of "continuous"
load-bearing: under a strict socket-uptime reading the test could become
unrunnable on a connection that behaves the way §9 already documents Kalshi's
behaving (22.9%, 40.6% and 22.9% gaps across three 2-hour study recordings).
This clarification is therefore made **after** a smoke run had exhibited a
simultaneous connection loss, and **before** the formal §8 acceptance run. No
equivalence comparison has been performed under the frozen protocol at the time
of writing, and no equivalence result of any kind informed the wording below.

**Clarification.**

"Continuous minutes" means continuous wall-clock execution of the
acceptance-test process, without manual pause, restart, or alteration of
parameters. A WebSocket disconnect does not reset the 60-minute duration clock.

While either stream is not VALID, that market-grid cell is unmatched and does
not enter the matched-comparison count. After a disconnect or sequence
invalidation, that stream may resume comparisons only after obtaining a fresh
snapshot and returning to VALID state.

Network disconnects, reconnect durations, and unmatched-grid fractions are
logged and reported separately. They are recorder-availability diagnostics, not
decoder-equivalence failures. The ≥ 10,000 matched-comparison requirement and
the 240-minute cap still apply, so a connection that disconnects frequently
yields INSUFFICIENT rather than a PASS.

**Why this reading.** §8 tests decoder equivalence; connection availability is
measured separately by the §9 feasibility framework. Requiring 60 uninterrupted
socket minutes would conflate two distinct hypotheses — and would let a network
problem masquerade as a parser verdict in either direction, blocking a correct
decoder or, worse, inviting the duration rule to be relaxed later under
schedule pressure. The reading is also the conservative one for the PASS bar:
unmatched cells are excluded from the numerator *and* the denominator, so a
flaky connection cannot manufacture agreement — it can only run the clock down
to the 240-minute cap and return INSUFFICIENT, which is the outcome §8 already
prescribes for too few comparisons.

### 2026-08-22 — §9 pilot start rule

**Status:** procedural rule fixing when the three-day feasibility window begins.
Changes no threshold, no grid, no skew tolerance, no pass bar, no duration, and
nothing about what is measured. §9 already fixes the window at exactly three
complete UTC days; this fixes only when it starts, and fixes it in advance so
the choice can never be made in response to conditions.

**Readiness time R.** R is the timestamp at which the last objective
prerequisite is *actually satisfied* — the maximum of:

1. the formal §8 PASS result row's completion timestamp;
2. the disk-capacity measurement and projection;
3. the deployment of the required recorder commit to the production host.

The prerequisites are ordered so §8 completes last.

**Preflight is NOT a prerequisite and does not set R.** It is a go/no-go gate
run in the hour before the scheduled boundary. A preflight failure is an
operational blocker, not an input that moves R.

The readiness record states those timestamps with their evidence and is
committed immediately afterward. **The Git commit timestamp does not determine
R and cannot move the window.**

**The window.** The feasibility window begins at the first 00:00:00 UTC boundary
strictly after R and runs exactly three complete UTC days — except that if R
falls within 30 minutes before a boundary, the start is the *following*
00:00:00 UTC.

The start is never chosen to include or exclude scheduled maintenance, market
conditions, weekday or weekend, or observed liquidity. Scheduled-maintenance
signals remain in ALL but can never be eligible, so selecting the window around
them would mechanically inflate `Y`.

**Blockers.** Any operational blocker preventing that start must be documented
*before* the scheduled boundary, naming the specific fault with contemporaneous
evidence. The new R is the timestamp at which the final blocker-clearing
prerequisite becomes satisfied — evidenced by the fix commit SHA, its deployment
timestamp, and the specific failed check now passing. Not by a declaration that
it is cleared.

**A §8 PASS validates only the implementation deployed when it ran.** If
anything substantive changes afterward — recorder semantics, decoder behaviour,
persistence logic, validity handling — that PASS no longer applies: the fix is
deployed, §8 is rerun, and the newer §8 completion becomes the new R. Purely
operational changes already permitted above are exempt.
