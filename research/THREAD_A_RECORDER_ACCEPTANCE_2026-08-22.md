# Thread A research book recorder — acceptance plan and build notes

Companion to `THREAD_A_EXECUTION_MECHANICS_2026-08-22.md` (frozen at `e7c8c87`).
That document is frozen; this one is not, and nothing here amends it. This
records **how** the recorder implements it, **what was found** while building
it, and **what has to pass** before the §9 feasibility clock may start.

Recorder introduced at `f9960f5`.

---

## 1. What was built

| Path | Role |
|---|---|
| `src/depthrec/frozen.py` | Every frozen parameter, transcribed section by section. `assert_frozen_config()` fails closed. |
| `src/depthrec/integrity.py` | Pinned-classifier integrity: whole-file for the study, normalised-AST closure for the longshot script. |
| `src/depthrec/pinned_hashes.py` | **Generated once** against `6ca4101`, committed. Never regenerated at startup. |
| `src/depthrec/book.py` | Canonical YES-scale book: snapshot, sequenced deltas, §7 invariants. Integer prices/quantities. |
| `src/depthrec/session.py` | Research WebSocket session: explicit `use_yes_price`, Ping/Pong observation, per-sid sequence, `read_book()`. |
| `src/depthrec/census.py` | Independent REST census: 60 s live discovery + 15 min close-time-range reconciliation. |
| `src/depthrec/classify.py` | Thin adapter over the **imported** pinned classifier. |
| `src/depthrec/store.py` | SQLite persistence, including raw frame retention. |
| `src/depthrec/recorder.py` | Orchestration and the §5 report. |
| `src/depthrec/equivalence.py` | §8's dual-stream equivalence test. |
| `scripts/depthrec_gen_pinned_hashes.py` | The hash utility. Run once, against the frozen commit. |
| `scripts/depthrec_record.py` | Run the recorder. |
| `scripts/depthrec_equivalence.py` | Run §8, or `--check-only` / `--probe-universe`. |
| `scripts/depthrec_report.py` | Rebuild §5 offline from a database; `--verify-replay`. |
| `scripts/depthrec_capture_fixture.py` | Capture the classifier regression fixture. Run once. |
| `tests/test_depthrec_replay.py` | Pins the raw-replay decoder against the live one. |

`depthrec` is a separate package from `kbot`. §6 keeps the production bot on
`use_yes_price: False`; the recorder is frozen on `True`. Nothing in `depthrec`
imports `kbot.execution`, `kbot.runner` or `kbot.ws_client`, and nothing in
`kbot` imports `depthrec`. `tests/test_depthrec_isolation.py` enforces all of
that, plus that the production bot has not been migrated.

---

## 2. N — the market-subscription limit

**N is NOT DOCUMENTED.**

Checked, 2026-08-22:

* `docs.kalshi.com/asyncapi.yaml` — `market_tickers` carries `minItems: 1` and
  **no `maxItems`**. The only `maxItems` in the whole spec are the `sids: [1]`
  constraints on `update_subscription`.
* Error code **26**, "Subscription market limit exceeded — Adding markets would
  exceed the per-subscription market limit", exists. The limit it refers to
  **carries no published number**, in the AsyncAPI or anywhere in the docs.
* Error code **25**, "Subscription buffer overflow", likewise carries no number.
* `docs.kalshi.com/getting_started/rate_limits` documents token buckets only.
  Its "200" is the Basic tier's **read refill rate in tokens per second** — not
  connections, not markets.
* `docs.kalshi.com/websockets/websocket-connection` and
  `.../orderbook-updates` state no connection or subscription counts.

The widely repeated "200 default WebSocket connections per user" is a
**connection** allowance from third-party write-ups, not an orderbook_delta
**market-subscription** limit, and it is not N. Neither number was used.

**What was attempted instead.** The entire frozen universe was subscribed, once:

```
$ python scripts/depthrec_equivalence.py --probe-universe 90
open markets in the frozen universe: 619
  KXBTC15M 1   KXETH15M 1   KXMLBHRR 180  KXMLBTB 161  KXMLBHIT 122
  KXMLBRBI 82  KXMLBHR 72   KXGOLDH 0     KXWTIH 0     KXSILVERH 0
  KXPLATINUMH 0  KXPALLADIUMH 0
server ACCEPTED all 619 markets on one subscription per stream.
  stream A: 619 books seeded, 619 VALID after 90s
  stream B: 619 books seeded, 619 VALID after 90s
```

619 markets, on **two concurrent connections**, one `orderbook_delta`
subscription each, accepted without error. §8's limit branch does not activate
and the frozen note needs no change.

The five hourly commodity series showed 0 open markets — a Saturday. They rotate
hourly on weekdays and the universe predicate picks them up automatically. The
count of 619 is a snapshot, not a ceiling; the highest count actually attempted
is what is reported.

`--probe-universe` attempts the whole universe **once** and never retries with
fewer. If the server rejects, the exact payload is printed and the process
stops: bisecting for a workable N would make the subscription set depend on the
server's behaviour rather than on the documentation.

---

## 3. The independent denominator

`REST /markets` is authoritative for whether an opportunity existed. Successful
WebSocket subscription never is. The census is a separate object from the
subscription machinery and is written and read without reference to it.

**Two passes, both persisted request by request.**

| | cadence | query | purpose |
|---|---|---|---|
| live | **60 s**, frozen | `series_ticker` + `status=open` | catch rotation |
| reconcile | 15 min | `series_ticker` + `min_close_ts`/`max_close_ts` (±6 h), **no status filter** | recover what live missed |

Every request, response, failure, discovered ticker and universe change is
timestamped and stored (`census_request`, `census_market`, `census_change`),
with the response body and its SHA-256.

* A discovery failure is a `census_request` row with `ok=0` and the error text.
  `upsert_census_market` has **no delete path**, so nothing can be silently
  removed.
* A failure part-way through paging discards the whole page set rather than
  ingesting a partial one — a half-read universe looks like a shrunk universe.
* A market that stops appearing keeps its row and its `first_seen_ts`.
* Both passes read raw JSON (`get_markets_without_preload_content`). The SDK's
  typed model raises on `status="finalized"`, which is what settled markets
  actually return, so a typed census would throw away exactly the markets whose
  close time has passed.

**Rotation.** The frozen object is the series list plus "currently open", not a
ticker enumeration. `_sync_subscription` recomputes the desired set from the
census on every discovery tick and issues `update_subscription`
`add_markets`/`delete_markets` on the live socket; the authoritative ticker list
lives in the session, so a change survives the next reconnect even if the live
update could not be sent. Markets are subscribed 30 minutes before close and
dropped 2 minutes after. For KXBTC15M/KXETH15M the binding constraint is
discovery, not that lead: only one market per series is open at a time and it
opens roughly 15 minutes before close, so a market is subscribed within 60 s of
opening — some 7 minutes before its T−7.

**Sample window.** A market whose T−7 moment predates the recorder is *outside
the sample*, not an UNAVAILABLE observation; the six-hour reconciliation
look-back routinely finds such markets and counting them would fill the
denominator with startup artifacts. `window_start_ts`/`window_end_ts` make the
boundary explicit and are logged into `recorder_run`. For the §9 pilot they are
set to the three complete UTC days and committed.

---

## 4. Firing classification

The classifier is **imported**, never re-derived. The chain is the pilot's own,
entry point for entry point:

```
market_calibration_study.sample_market(series, ticker, close_ts, candles, _, [7])
  -> sort_candles / candle_at_or_before / yes_price_from_candle
market_calibration_study.write_csv(...)          # the study's own serialisation
test_longshot_rule.load_observations(csv)        # the pilot's own CSV parser
test_longshot_rule.qualifying(obs, 0.70, 7)      # the firing set
test_longshot_rule.favorite_side(yes_price)      # the side the rule buys
```

REST 1-minute `yes_bid`/`yes_ask` candlesticks are the source, matching the
pilot's 99.4%-`mid` price source computed at
`market_calibration_study.py:169`.

**The CSV round-trip is deliberate.** The pilot's firing decision was made on
observations loaded from `data/calib_full.csv`, and `write_csv` serialises the
price as `f"{yes_price:.4f}"`. A raw mid of `0.10500000000000001` is `0.105` by
the time the threshold sees it, and at the threshold the two can disagree about
whether the rule fires at all. `load_observations` also drops degenerate
0.00/1.00 prices. Both are classifier semantics and both stay in the pinned
code. The adapter reports the post-CSV price, because that is the number the
decision was made on.

**Settlement.** `write_csv` needs a settled outcome for its `ev_cheaper_side`
column, which the firing decision never reads. At T−7 the outcome does not
exist, so a placeholder is passed.
`classify.assert_settlement_is_irrelevant()` re-runs the whole pinned chain
under both settlement values and requires an identical firing set; the
regression test calls it on all 80 fixture markets.

**Halting.** If the pinned classifier cannot be imported and applied literally,
`ClassifierUnavailable` is raised and the run stops. A candle-fetch failure is
*not* a non-firing verdict — the market is retried, because recording it as "did
not fire" would quietly shrink the denominator.

**Once firing, always counted.** A market established as firing enters ALL
regardless of whether the recorder discovered or subscribed to it. No
trustworthy state at T−7 yields the applicable `UNAVAILABLE_*` reason — usually
`UNAVAILABLE_NO_SNAPSHOT` — and never a drop.
`test_a_firing_signal_with_no_observation_at_all_is_no_snapshot_not_a_drop`
pins that.

### Regression fixture

`tests/fixtures/depthrec_classifier_pilot.json` — 80 settled Thread A markets,
real Kalshi 1-minute candle JSON, T−7 prices spanning 0.0505 to 0.9545, 53
firing (22 YES-side favorites, 31 NO-side). Three things must agree:

1. the pilot's pipeline over the fixture, batched into one CSV as the pilot ran
   it, given each market's **real** settlement;
2. `depthrec.classify`, market by market, given the **placeholder**;
3. the firing set **committed in the fixture**, computed once at capture time.

(1) vs (2) is the required demonstration. (3) stops both from drifting together.

---

## 5. Pinned-classifier integrity

Two files, two different checks, for a reason.

**`scripts/market_calibration_study.py` — whole file.**
Git blob `e8dd06f88f3c4247bcff0d41810ba33030188846`, SHA-256
`6e14cf6921efc083bb6242993be8eaa91854e4f8481a98899a92c3ecf3983517`. Identical at
`6ca4101` and at HEAD.

**`scripts/test_longshot_rule.py` — element-wise.** A whole-file hash would be
wrong: the file took unrelated post-freeze diagnostic and display changes
(`413d88b`, `e4391ac`) while the firing functions stayed semantically frozen, so
a whole-file check would fail on those and train someone to "refresh" the
expected value.

Protected seeds: `favorite_side`, `qualifying`, `favorite_trades`,
`_THRESHOLDS`, `_BUCKET_WIDTH`, `_MIN_INSAMPLE_TRADES`, `_CONTRACTS_PER_ORDER`,
`_FEE_COEFFICIENT`. The transitive closure over module-level bindings adds
`side_won`, `trade_ev`, `fee_per_contract` (the import binding) and
`Observation` — **12 elements**, and the closure is identical at `6ca4101` and
at HEAD.

| element | hash (first 12) |
|---|---|
| `Observation` | `24a3bf7a4793` |
| `_BUCKET_WIDTH` | `60be23c7e155` |
| `_CONTRACTS_PER_ORDER` | `f63e3f55a63a` |
| `_FEE_COEFFICIENT` | `a791cfd52673` |
| `_MIN_INSAMPLE_TRADES` | `0a8732df495c` |
| `_THRESHOLDS` | `4ed26a3dfc60` |
| `favorite_side` | `b8fb3f715b1b` |
| `favorite_trades` | `68027aeb24e6` |
| `fee_per_contract` | `a058aa51fac8` |
| `qualifying` | `4887355064e0` |
| `side_won` | `20cafdb46de7` |
| `trade_ev` | `0ede50969151` |

Manifest digest `c7ef132efb26d21a62f2527bfc6f081900fe15486cc6d5fd0381644caaeb0d81`.
Full 64-character hashes and the normalised source of every element are in
`src/depthrec/pinned_hashes.py`.

Normalisation strips docstrings (comments never reach the AST) and all
formatting, via `ast.dump(..., include_attributes=False)`. It does **not** strip
default argument values — a changed default is a changed rule, and there is a
test for exactly that.

Line endings are normalised to LF before whole-file hashing. This repo checks
out CRLF (`core.autocrlf=true`) while the blob is LF; hashing raw disk bytes
would fail the pin on every Windows checkout, which is the same false alarm that
teaches people to regenerate expected values.

**The expected values were generated once**, by
`scripts/depthrec_gen_pinned_hashes.py`, from `git show 6ca4101:<path>` — never
from the working copy — and committed.
`test_expected_values_were_generated_against_the_section_2_frozen_commit`
asserts `pinned_hashes.FROZEN_COMMIT` really is `6ca4101`'s full SHA.

**The three parameters no hash covers.** `--rule-threshold 0.70`,
`--rule-slice 7` and X = 10 are command-line arguments to the pinned scripts,
not module defaults there. They are supplied as frozen configuration from
`depthrec.frozen`, logged at startup, and re-checked by `assert_frozen_config()`
with exact equality — `0.7000001` fails.

---

## 6. Persistence and raw retention

One SQLite file per run, WAL, `synchronous=NORMAL`. Every derived row carries
the **recorder Git commit** (`"<sha>"` or `"<sha>-dirty"`).

| table | contents |
|---|---|
| `recorder_run` | run id, start/end, Git commit, API env, base and WS URLs, frozen config, integrity result, document reference |
| `ws_session` | one row per connection: stream label, session epoch, `use_yes_price`, channels, **full subscribe params**, WS URL |
| `ws_raw` | **every frame, in and out, untouched, before parsing**, with receive timestamp |
| `health_event` | connects, disconnects, sequence gaps, replays, decode failures, census failures, restarts |
| `census_request` | every REST call: params, ok/failed, HTTP status, error, market count, response body + SHA-256 |
| `census_market` | the denominator: ticker, series, `close_ts`, status, first/last seen, which pass found it, full market JSON |
| `census_change` | discovered / close_ts_changed / status_changed / out_of_window, timestamped |
| `classification` | firing verdict, price, price source, side, entry, the causal candle's end, **the raw candle JSON the classifier saw** + its SHA-256, and which pinned entry points were called |
| `signal_observation` | the book at T−7: validity or `UNAVAILABLE_*` reason, whether subscribed, X, top of book, **both sides' executable ask and displayed size**, **the whole reconstructed book**, sid, snapshot and last seq, snapshot/last-update/exchange timestamps, session activity age, last ping, and the `ws_raw` id range |
| `equivalence_comparison` / `equivalence_result` | §8, one row per matched grid cell plus the verdict and its parameters |

A derived `ask=…, size=…` row alone is not sufficient — this project has hit
book-convention bugs three separate times — so `signal_observation` points back
into `ws_raw` by id and `scripts/depthrec_report.py --verify-replay` rebuilds
every observation's book from those frames alone and compares. Swapping in an
independently written decoder makes the same harness the independent check §7
asks for.

`signal_observation` carries **no bucket column**, deliberately. See §6a.

---

## 6a. The executable ask is side-dependent — a bug the smoke run caught

§2: the entry price is "the best executable ask **on the side the frozen rule
buys**". The first working recorder took the best ask unconditionally. That is
right for a YES-side signal and **wrong for every NO-side one**, and the frozen
rule buys NO whenever the YES price is below 0.50 — 31 of the 80 fixture markets,
and both markets the first smoke run observed.

In the canonical yes-leg book:

* buying **YES** lifts a resting NO bid, which *is* the ask ladder. Executable
  ask = best ask; size = the size at that ask.
* buying **NO** lifts a resting YES bid. A YES bid at `p` is an offer to sell NO
  at `1 − p`, so executable ask = `1 − best_bid` and the size is the size at that
  **bid** level.

The two sides read opposite ladders, and their depths are unrelated: the first
smoke run's KXBTC15M observation showed 3,922.85 contracts at the YES ask of
0.17 and would have been called ELIGIBLE, when the rule buys NO at 0.83 and the
depth that matters is whatever sits behind the best YES bid. `book.TopOfBook.
executable_ask(side)` now derives both, algebraically from the one canonical
book as §6 requires, and `signal_observation` stores both.

Because the side comes from the classifier — which cannot run until the T−7
candle exists — the §4 bucket cannot be decided at observation time.
`signal_observation` is therefore side-agnostic and bucket-free, and
`recorder.bucket_for()` derives ALL / VALID / NON-TRADEABLE / ELIGIBLE at report
time from the join. `scripts/depthrec_report.py` imports the same function, so an
offline reconstruction cannot disagree with the run that produced the data. A
side effect worth having: the book observation is recorded before the firing
verdict is known and cannot be selected on it.

---

## 7. How state is invalidated

* **Sequence gaps** are tracked per **subscription id**, not per ticker: one
  subscription multiplexes every ticker onto a single counter, so a per-ticker
  view of `seq` is full of legitimate holes. A skip invalidates **every book on
  that subscription**; a repeated or older `seq` is recorded and dropped.
* Only a **fresh `orderbook_snapshot` on the current session** repairs a book.
  Later deltas do not. A pre-gap book is never carried forward.
* Each book records the **session epoch** it was built in. A reconnect
  increments the epoch and invalidates everything; a book from an older epoch
  reads `UNAVAILABLE_DISCONNECT` even if it is still in memory.
* A delta arriving with no snapshot behind it yields `UNAVAILABLE_NO_SNAPSHOT`
  rather than a base book invented from the delta.
* A book that breaks a §7 invariant — crossed, negative, duplicate level,
  out-of-range price — yields `UNAVAILABLE_OTHER`.

**Reason precedence in `read_book()`**, in order: scheduled maintenance →
disconnected → no snapshot → the book's own invalidation → session stale →
invariant failure → VALID. Maintenance outranks everything because Kalshi pauses
trading Thursday 03:00–05:00 ET and disconnections there are expected; those
signals stay in the raw census so downtime cannot be hidden by deleting
known-bad hours.

**Session health** comes from actual received frames. `process_event` is
overridden on the connection class to timestamp PING and PONG control frames,
which `websockets` otherwise answers invisibly. Kalshi pings every 10 s, so the
30 s window tests the connection. **Absence of a market-specific update never
makes a book stale** — an unchanged book stays VALID, and there is a test that
holds a book valid across an hour of silence with only session heartbeats.

---

## 8. Acceptance-test plan

Steps 1–4 have been executed; 5–6 have not.

| # | Check | How | Status |
|---|---|---|---|
| 1 | Frozen config and pinned-classifier integrity fail closed | `pytest tests/test_depthrec_integrity.py` (25 tests) | **PASS** |
| 2 | Adapter selects the pilot's signals on identical data | `pytest tests/test_depthrec_classifier.py` (9 tests, 80 real markets) | **PASS** |
| 3 | Book, session, census, bucketing, replay, shutdown, equivalence machinery | `pytest tests/test_depthrec_*.py` (140 tests) | **PASS** |
| 3b | No regression in the bot | `pytest tests/` — 1,506 tests | **PASS** |
| 4a | Account permits two concurrent WebSocket connections | `depthrec_equivalence.py --check-only` | **PASS** |
| 4b | Server accepts the entire frozen universe | `depthrec_equivalence.py --probe-universe 90` | **PASS**, 619 markets |
| 4c | Recorder end to end against live Kalshi | `depthrec_record.py --minutes 26 --smoke` | **PASS**, see §9 |
| 4d | Every observation's book rebuilds from `ws_raw` alone | `depthrec_report.py --verify-replay` | **PASS**, 4/4 |
| 4e | A duration-bounded run exits cleanly and writes its report | `depthrec_record.py --minutes 2 --smoke` | **PASS**, exit 0 at 124 s |
| 5 | **§8 equivalence test** | `depthrec_equivalence.py` — ≥60 min, stop at ≥10,000 matched comparisons, cap 240 min, 1 Hz grid, ≤500 ms skew, ≥99.9% **and** zero unexplained | **NOT RUN** |
| 6 | **§9 feasibility pilot** | `depthrec_record.py` for exactly three complete UTC days, clock start logged and committed | **NOT STARTED** |

Steps 5 and 6 are wall-clock bound: §8 takes 60–240 minutes of continuous
healthy dual-stream recording, and §9 takes three days. §9 may not begin until
§8 passes, and a recorder bug fixed during §9 restarts the three days from zero.

`--minutes` exists for smoke and acceptance runs only. The pilot clock is
started deliberately, with `window_start_ts`/`window_end_ts` set to the three
UTC days, logged into `recorder_run` and committed, so it cannot drift
retrospectively.

---

## 9. Results so far

**Test suite.** 1,506 tests pass, of which 140 are new. No existing test
changed.

**Two concurrent connections** — confirmed against production.

**Full universe subscription** — 619 markets accepted on each of two
connections; all 619 books VALID on both streams after 90 s. §8's limit branch
does not activate.

**Decoder agreement — SMOKE ONLY, not the §8 result.** A 150-second
instrumented run over the same 619 markets, both conventions, 1 Hz grid:

```
matched=61900  agreed=61900  rate=1.000000  skew_explained=0  unexplained=0
```

61,900 matched market-grid comparisons, exact agreement on canonical best bid
price/size and best ask price/size, zero disagreements of either kind. This is
**not** a §8 PASS: §8 requires ≥60 *continuous* minutes and this run was
interrupted (below). It is evidence that the `1 − p` no-side transform and the
snapshot/delta reconstruction are right, and that the equivalence harness works
on live data.

**Recorder end to end, 26 minutes on production, 11:03–11:29 UTC.** Four T−7
moments across KXBTC15M and KXETH15M, all VALID:

| ticker | T−7 | YES ask / size | NO ask / size | verdict |
|---|---|---|---|---|
| KXBTC15M-…0715-15 | 11:08:00 | 0.56 / 112.72 | 0.45 / 5854.56 | not firing (0.565) |
| KXETH15M-…0715-15 | 11:08:00 | 0.65 / 216.46 | 0.36 / 697.0 | not firing (0.645) |
| KXBTC15M-…0730-30 | 11:23:00 | 0.32 / 7036.69 | 0.69 / **10.0** | **firing, NO, ELIGIBLE** |
| KXETH15M-…0730-30 | 11:23:00 | 0.55 / 38.0 | 0.46 / 16.0 | not firing (0.535) |

The one firing signal is the argument for §6a in a single row. `yes_price=0.295`
means the favorite is **NO at 0.705** ≥ 0.70, so the rule buys NO, and the
executable depth is the **10.0** contracts behind the best YES bid — ELIGIBLE by
exactly zero margin. The side-blind reading would have reported 7,036.69, a
factor of 700 out, on the very first firing signal the recorder ever produced.

Also demonstrated in that window: mid-session rotation
(`update_subscription add_markets n=2` at 11:15, `delete_markets n=2` at 11:17)
without a reconnect; a candle fetch that timed out at 11:10:50, was retried, and
succeeded at 11:12 rather than being recorded as "did not fire"; a WebSocket
reconnect at 11:09–11:12 carrying the session from epoch 1 to epoch 5; 280
census requests with **0** failures over 715 markets; and 835,436 raw frames
retained.

Offline reconstruction of the same store with `depthrec_report.py`:
`all_otherwise_firing 1`, buckets `{UNAVAILABLE 0, NON_TRADEABLE 0, ELIGIBLE 1}`,
`buckets_sum_to_all true`, `firing_by_side {NO: 1}`, D = 1.0, Y = 1.0. Those
figures are from 26 minutes of a Saturday and mean nothing about feasibility —
they are here to show the join reconstructs.

**Raw replay — and a divergence it caught.** `--verify-replay` rebuilds every
observation's book from `ws_raw` alone. First attempt: 2 of 4 skipped. The
verifier had applied sequence bookkeeping only to the two orderbook message
types, but `ok` responses to `update_subscription` carry a `seq` on the same
per-subscription counter, so the two rotation acks looked like forward jumps and
falsely broke the session. The bug was in the **verifier**, not the recorder —
but the mechanism did its job: two decoders disagreed and the disagreement
surfaced. After the fix, **4/4 agree, 0 mismatches, 0 skipped**, across a
session-epoch boundary and two mid-session subscription updates.
`tests/test_depthrec_replay.py` pins it, including that a real gap still breaks
a book and that a wrong stored book is still reported as a mismatch.

**Frame volume — corrected 2026-08-22 after the shutdown deadlock was
understood.** The figures first recorded here (835k frames, 450–520/s) were read
off a database while its recorder was **still running**: the shutdown deadlock
meant that process never exited, and it kept recording for 9.8 hours past its
26-minute deadline. They were never 26-minute figures.

Measured properly, by filtering `ws_raw` to `started_ts + 1560s`:

| | in the intended 26 min | after the deadline (leak) | total |
|---|---:|---:|---:|
| frames | 660,961 | 399,867 | 1,060,828 |
| raw JSON bytes | 173,708,640 | 105,198,590 | 278,907,230 |

**423.7 frames/s**, 262.8 bytes/frame, from four to six crypto binaries.
660,939 of the 660,961 in-window frames are `orderbook_delta` — Kalshi's
15-minute crypto markets requote almost continuously near expiry.

Arithmetic from that measured rate, clearly labelled as projection and not
measurement: three days at 423.7 frames/s is ~1.10 × 10⁸ frames and ~28.9 GB of
raw JSON text. The finished 10.2-hour store holds 278,907,230 text bytes in a
429,764,608-byte file, a 1.54× storage factor, so ~**45 GB** for the main file —
before the WAL, and for Thread A's two series alone. The pilot also subscribes
the other ten §8 series as they come within the 30-minute lead, so 45 GB is a
floor, not a ceiling. Provision disk before starting §9; this is one of the
three prerequisites the §13 pilot-start rule requires evidence for.

**WAL matters as much as the file.** `smoke2.sqlite` finished as a 429,764,608-byte
main file *plus* a 394,696,032-byte uncheckpointed WAL — 824 MB on disk for a
logical database of 429,858,816 bytes (104,946 pages × 4,096). The WAL is mostly
superseded frames for pages already written; a checkpoint collapses it. A
recorder killed rather than closed leaves that WAL behind, so the pilot's disk
budget must cover roughly twice the logical size unless checkpoints are forced.

**Shutdown deadlock — and a wrong first diagnosis.** The first 26-minute run
reached its deadline and then never exited; it had to be killed, and it never
printed its summary.

My first diagnosis was blocking I/O, and it was **wrong**. The actual cause is a
plain `asyncio` misuse: `asyncio.wait(tasks, return_when=FIRST_EXCEPTION)`
returns early only when a task *raises*. When tasks merely finish normally it
waits for **all** of them — and `session.run()` never finishes on its own,
because its reconnect loop exits only once `session.stop()` has been called, and
that call lived in the `finally` block that was never reached. So the deadline
task set `_stop`, the three loops exited, and the await blocked forever with the
socket still reconnecting. A verification run made that unmistakable: the
process logged `deadline_reached` and then **reconnected 81 seconds later**.

Fixed by waiting on `FIRST_COMPLETED` and stopping the session first in
teardown, both bounded (`SESSION_CLOSE_TIMEOUT_SECONDS`,
`SHUTDOWN_GRACE_SECONDS`). Verified live: exit code 0 at 124 s against a 120 s
deadline, with the §5 report written. Two regression tests cover it and both
were confirmed to **fail on the pre-fix code** — they hang until their own
`wait_for` times out.

A second, smaller bug surfaced once shutdown worked at all: `--smoke` called
`report()` after teardown had closed the store. The report is now computed
during teardown and cached in `final_report`.

**Blocking I/O on the event loop — a real defect, separately.** The census and
the classifier were calling the Kalshi SDK synchronously **on the asyncio event
loop**, and `kalshi_client._TokenBucket.acquire()` does a plain `time.sleep()`
while urllib3 retries a dropped keep-alive. That did not cause the shutdown
hang, but it would have delayed a T−7 reading — the one measurement here that
cannot be retried. Every blocking Kalshi call now runs in a worker thread
(`asyncio.to_thread`) and the store is thread-safe behind an explicit lock with
`check_same_thread=False`.

§10 makes a recorder bug fixed during the pilot restart the three days from
zero, which is why both had to be found now rather than on day two.

**Connection stability — a finding, not a nuisance.** The smoke run lost both
connections simultaneously about 62 seconds in ("no close frame received or
sent"), then took ~50 seconds to re-establish through two handshake timeouts.
Frame rate at the time was ~14/s per stream and the SQLite writes were nowhere
near saturating, so this was not self-inflicted back-pressure. It is consistent
with §9's note that three earlier 2-hour study recordings showed Kalshi
WebSocket gaps covering 22.9%, 40.6% and 22.9% of their windows. §9 is explicit
that sustained unavailability is a feasibility finding in its own right; the
recorder records it as `UNAVAILABLE_DISCONNECT` and does not paper over it.
It also means §8's "60 *continuous* minutes" may itself be hard to obtain, which
is worth knowing before the clock starts.

---

## 10. Where the document could not be followed literally

Everything in the frozen document was implementable as written. Three places
needed a decision the document does not make, each recorded here rather than
resolved silently:

1. **When classification runs.** §3 fixes the T−7 book moment; it does not say
   when the *firing* verdict is computed. The 1-minute candle ending at T−7 does
   not exist until T−7, so the book is read **at** T−7 and the classifier runs
   90 seconds **after**, joined by ticker. The book observation is therefore
   taken before the firing verdict is known.

2. **The sample window.** §9 fixes the pilot at three complete UTC days but
   nothing states what happens to a market whose T−7 predates the recorder. The
   reconciliation pass looks six hours back by design, so these appear
   constantly. They are recorded as `out_of_window` in `census_change` and
   excluded from ALL — they are outside the sample, not UNAVAILABLE.

3. **Live subscription lead.** §8's *validation universe* is all currently open
   markets in the twelve series, and the equivalence test subscribes to exactly
   that. The *recorder's* live subscription set is narrower: markets within 30
   minutes of close. That is a recorder-design fact (§3 permits restating
   recorder-design facts before the clock starts), it does not touch the
   denominator, and for Thread A it is not binding — only one market per series
   is open at a time and it opens roughly 15 minutes before close.

One thing the document assumes that turned out **not** to hold: §8 says "N is
the documented Kalshi API or account subscription limit — it is read from the
documentation, never chosen." There is no such documented number for
orderbook_delta market subscriptions. Since the server accepted the whole
universe, the branch is moot and the note is left frozen as it stands.
