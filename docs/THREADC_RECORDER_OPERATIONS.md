# Thread C ticker/trade banking recorder — operations

Operational data banking, and nothing else. This recorder collects neutral raw
evidence: the untouched Kalshi WebSocket frames needed to reconstruct MLB order
books later, plus the census and universe records establishing which markets
existed and when the recorder learned of them.

It contains no research analysis. There is no strategy, no settlement lookup, no
price summary, no spread or depth statistic, no market-level activity summary and
no hypothesis-dependent slicing anywhere in `src/threadc/`. Every decision it
makes comes from the clock, the filesystem, or official exchange metadata — never
from anything observed in a book.

It is read-only against Kalshi and has **no order path**: nothing under
`src/threadc/` imports `KalshiClient` (the class owning `place_order()` /
`cancel_order()`), and the HTTP layer issues GET only against a four-entry
allowlist — `/series`, `/series/{ticker}`, `/events`, `/markets`. Both properties
are tested in `tests/test_threadc_isolation.py`.

---

## 1. Every value is now supplied

`config/threadc.json` no longer carries a `null`. Both values that were
deliberately left unsupplied have been supplied from measurement or from a
recorded decision, and the recorder still **fails closed at startup** if either
is removed.

| key | value | where it came from |
|---|---|---|
| `disk_reserve_bytes` | `12841254912` | The frozen reserve is `R = max(10 GiB, 48 × B_1h)`. `B_1h` is the **Branch-2** capacity hour — ticker-only, `B_1h,disk = 267,526,144` bytes/hour, the selection this configuration actually collects on. `48 × 267,526,144 = 12,841,254,912`, above the 10 GiB floor. Not recomputed from the Branch-1 hour or from any later hour: the formula is frozen against that measurement. |
| `channels` | `["ticker"]` | Branch 2 of the precommitted fallback hierarchy (§6). Written explicitly because absence means the frozen pair, which is Branch 1 — the branch the measurement rejected. |
| `discovery_daily_target_utc` | `09:00:00` | Previously an open design point, resolved to once per UTC day at 09:00 UTC (§7). |

The frozen collection end is set as it always was.

---

## 2. Collection universe

**Initial universe:** the committed Collection Universe v1 — the 138 series
qualifying under the frozen predicate
(`research/THREADC_COLLECTION_UNIVERSE_V1.md`). The artifact
`research/threadc_universe_v1/universe_v1.json` is SHA-256-checked at startup
against the value published in that directory's `SHA256SUMS`; a mismatch fails
closed rather than warning, because every segment manifest claims that identifier
and hash.

**Frozen predicate.** A series S qualifies iff at least one event E returned by
`GET /events?series_ticker=<S.series_ticker>` satisfies

```
E["product_metadata"]["competition"] == "Pro Baseball"
```

under exact, case-sensitive equality. No ticker prefix, title, tag, category or
settlement source is consulted, and discovery applies **no prefilter** — the
predicate is evaluated over the entire ~13,800-series catalog, so a qualifying
series outside every obvious MLB namespace (`KXCITYMLBEXPAND`, tagged Hockey;
`KXMLBMENTION`, category Mentions) is found on the same footing as one inside it.

**Q1 remains UNRESOLVED.** The MLB-adjacent competition values — All-Star, Home
Run Derby, Pro Baseball Draft, Pro Baseball Spring Training — are **not** admitted
and the predicate is **not** widened. Those competitions fall outside this
collection window: the configured end precedes the next All-Star Game, Home Run
Derby, Draft and Spring Training, so nothing bearing those values can be observed
between production start and the frozen end, and Q1 therefore does not affect what
can be collected. No further scope judgment about Q1 is made here. The status is
written verbatim into every segment manifest (`threadc.universe.Q1_STATUS`).

**Rule 1 — automatic admission.** Any series whose official event metadata
satisfies the predicate is admitted from the **first COMPLETE census** that sees
it. This covers series Kalshi creates later *and* the 18 series v1 quarantined for
having no event metadata at all — which includes the postseason products this
collection exists for: `KXMLBWS`, `KXWSAL`, `KXWSNL`, `KXMLBALCSMVP`,
`KXMLBNLCSMVP`. On admission the recorder appends an immutable discovery record to
`<archive_dir>/universe/universe_additions.jsonl` carrying the discovery UTC
timestamp, the series ticker, the exact predicate-bearing metadata, the qualifying
event identifier(s), the API endpoint, a preserved raw metadata snapshot under
`universe/evidence/<sha256>.json`, and the SHA-256 of that evidence. It then
re-censuses and subscribes without waiting for a restart. **No backfill** — prior
history is unavailable and is recorded as unavailable, never interpolated.

**Rule 2 — append-only inclusion.** Nothing is ever removed. A member whose
metadata Kalshi later edits stays in through the collection end; the change is
written to `universe/universe_observations.jsonl` as an observation, never applied
as an eviction.

Membership is rebuilt by replaying the addition log, not by trusting the derived
`universe_state.json` index, so a truncated or hand-edited index cannot quietly
shrink the universe.

---

## 3. Data stream

One canonical representation: **canonical YES, `use_yes_price: true`**, sent
explicitly on every subscribe. There is no A/B equivalence stream — that test
belongs to Thread A and its verdict is already recorded.

Channels: the production collection subscribes to **`ticker` only**.
`threadc.frozen.CHANNELS` remains `("ticker", "trade")` — that tuple is the
ceiling of what this bank may preserve, and only a code change raises it — and
`config/threadc.json` selects a subset of it with `"channels": ["ticker"]`. The
key can subtract and never add; a name outside the frozen pair is refused at
startup, and removing the key restores the pair. The selection is written into
the startup record, the readiness line, each segment's `ws_session` rows and the
OPEN and final manifests, so a ticker-only segment is never read later as a
two-channel segment in which nothing traded. The daily health check does not
repeat it: that output is pinned to four facts and to no token that reads as
market data, and a channel named `ticker` is indistinguishable there from a
market ticker.

**Why ticker-only.** Both branches of the precommitted fallback hierarchy were
measured on the bracketed hour, the same universe and the same storage schema:
Branch 1 (`ticker`+`trade`) wrote **373,444,608 bytes/hour** and Branch 2
(`ticker` only) wrote **267,526,144 bytes/hour**. Branch 1 does not fit the
137 GB free for the collection window, so collection proceeds on Branch 2. What
that drops is every public print: `trade` is not banked, and no reconstruction
of executions from this archive is possible.

Every frame, in and out, is stored before any parsing, with the receive
timestamp, shard label, session epoch and direction. Nothing is sampled,
throttled, coalesced or selected by market state. Settlement outcomes are never
fetched.

**Why not `orderbook_delta`.** The full live-evening capacity test measured that
design at ~3.4+ GiB/hour with 10,270 markets on one healthy session — COMPLETE,
integrity ok, zero errors, zero reconnects, zero error-26, ~402 MB RSS, ~88% of
one OCPU. The design works; it does not fit the available storage for 24/7
collection through the collection end. That was an **operational capacity
failure decided from byte counts alone** — no book contents were inspected.

**What that costs.** `ticker` preserves the *displayed state at the touch*, not
the executable ladder. It can support "was at least X displayed at the best
bid/ask at time T", subject to the ordinary distinction between a historical
displayed quote and a guaranteed live fill. It cannot support full L2 ladders,
quantities away from the touch, queue position, every cancellation away from the
touch, deep-book OFI, L2 microprice, or the average fill price of an order that
walks past the touch. Those losses are enumerated in
`research/THREADC_FIDELITY_BOUNDARY_V1.md`, which also forbids compensating for
them by narrowing markets, game times, time-to-close windows or horizons.

**Initial state.** `orderbook_delta` answered a subscribe with an
`orderbook_snapshot` per market. `ticker` and `trade` answer with *nothing* and
stay silent until something changes, so silence carries no information about
state. `GET /markets` carries the same displayed touch fields the ticker channel
does — `yes_bid_dollars`, `yes_ask_dollars`, `yes_bid_size_fp`,
`yes_ask_size_fp`, `last_price_dollars`, `volume_fp`, `open_interest_fp` — so the
recorder banks an explicit state from the market census it already runs, at every
subscription establishment, at every reconnect, at every daily rotation, and for
every market added to a live subscription. Those rows land in the append-only
`initial_state` table, stamped with shard, connection epoch and reason; the
manifest reports the aggregate. A market with no REST body yet is *counted as
missing*, never filled in from anywhere.

Collection is continuous 24/7 over every market the census reports open. "Open"
there is the REST *query filter* the live census pass sends; the market bodies
that come back carry `"status": "active"`, and `?status=active` is rejected 400
`invalid status filter`. The two vocabularies are disjoint, so the subscription
set tests a censused entry's status against `LIVE_MARKET_STATUS = "active"`
(`threadc.frozen`), never against the filter word. There is
**no T−10/T−5/T−2 slicing and no lead-time window at all** — Thread A's
`SUBSCRIBE_LEAD_SECONDS = 1800` is deliberately *not* reused, because a lead time
is a research horizon. A market that closed stays on the wire for
`UNSUBSCRIBE_LAG_SECONDS = 120` so the frames either side of a close are not cut
off by census timing.

**Sharding.** `shard = sha256(ticker)[:16] % ws_shards`. Deterministic, so which
shard carried a market is reproducible from the archive alone; stable under
growth, so admitting a series moves nothing; exclusive, so no market can be
double-subscribed. Occupancy above `max_markets_per_shard` is *reported*, never
auto-fixed — the recorder does not re-partition itself or bisect against the
server to discover Kalshi's unpublished subscription limit. Every assignment,
subscribe and unsubscribe is written to `shard_assignment`; a market whose shard
changes is logged as a `shard_change` health event on both labels.

---

## 4. Archives, rotation and manifests

### Layout

```
<archive_dir>/                          e.g. /mnt/threadc
├── segments/
│   ├── threadc-2026-09-04-seg001.sqlite
│   ├── threadc-2026-09-04-seg001.manifest.json
│   ├── threadc-2026-09-04-seg002.sqlite        ← a restart adds a segment,
│   └── threadc-2026-09-04-seg002.manifest.json    it never rewrites seg001
├── universe/
│   ├── universe_state.json                     derived index
│   ├── universe_additions.jsonl                append-only record of authority
│   ├── universe_observations.jsonl             Rule 2 observations, incomplete censuses
│   └── evidence/<sha256>.json                  preserved metadata per admission
├── collection_end.state.json                   monotonic high-water mark
├── discovery_schedule.state.json               restart-storm guard + accepted days
├── config_changes.jsonl                        every accepted config change
└── process_starts.jsonl                        every process start / restart
```

### Startup ordering — banking never waits for discovery

On every start and every restart, in this order:

1. **The append-only universe**, seeded from the hash-verified committed
   Collection Universe v1 plus every prior Rule 1 admission replayed from
   `universe_additions.jsonl`. No network call is involved.
2. **Orphan recovery**, then a new segment and its frame writer.
3. **The ordinary known-universe market census** (`RestCensus` live +
   reconcile), so the first subscribe covers everything already open rather than
   trickling in over a minute.
4. **The WebSocket sessions** — raw recording begins. `_ws_recording_started` is
   set here.
5. **Only then** may the ~13,800-request full-universe Rule 1 sweep run, and it
   runs asynchronously on a worker thread.

The discovery loop `await`s `_ws_recording_started` before it evaluates the
schedule at all, so this is an enforced invariant rather than an accident of the
order in which tasks were created — including in the awkward case, a restart at
14:00 with the 09:00 target long past and a catch-up sweep already due.
`tests/test_threadc_recorder.py::test_a_slow_full_universe_discovery_does_not_delay_ws_recording`
pins it with a deliberately slow catalog probe.

### Rotation

One continuous process and one set of WebSocket connections across day
boundaries. At 00:00:00 UTC the **archive writer** is rotated internally: the new
segment and its writer are fully open *before* `self._writer` is reassigned, and
the outgoing writer is stopped only afterwards on a worker thread. The swap is one
assignment on the event loop between two submits, so no incoming message is
deliberately dropped and no frame is ever handed to a stopped writer. The sockets
are not disconnected and the systemd service is not restarted.

### Segment states

`OPEN` · `COMPLETE` · `INCOMPLETE_CRASH` · `HALTED_LOW_DISK`

**COMPLETE is earned, not asserted.** Finalization runs, in order: collect
operational metadata → drain the frame queue → `PRAGMA wal_checkpoint(TRUNCATE)` →
clean database close → `PRAGMA integrity_check` → SHA-256 of the finished file →
manifest. If any step fails, the segment is written as `INCOMPLETE_CRASH` with
`finalization_failed` appended to the close reason — not as a normal finished day.

A segment already in a terminal state is **never** reopened, rewritten, renamed or
overwritten: `open_segment()` takes the next unused index, and `finalize()` raises
`SegmentImmutable` on a terminal manifest. A segment found `OPEN` at startup is an
orphan from a dead process: it is preserved, its frame count, integrity check and
SHA-256 are recorded, and it is explicitly labelled `INCOMPLETE_CRASH`.

### Manifest contents

Per finalized segment: filename · segment state · close reason · UTC start · UTC
end · byte size · aggregate raw frame count · SHA-256 · integrity-check result ·
per-step finalization result · recorder git commit · Collection Universe v1
identifier and hash · current universe version and addition-log position · WS
session/shard/reconnect metadata sufficient to identify gaps · whether any census
during the segment was incomplete · shard plan · run mode.

No prices, depth statistics, per-market activity, returns or outcomes — enforced
by `tests/test_threadc_segment.py::test_manifest_contains_no_research_data`.

Close reasons: `rotation_utc_midnight`, `frozen_collection_end`,
`low_disk_reserve`, `operator_signal`, `capacity_run_end`,
`orphan_recovered_after_unexpected_death`.

---

## 5. Collection end, low disk, systemd

### Collection end

Configuration, not code. Initial value **`2026-11-05T12:00:00Z`**, derived from
MLB's published postseason schedule as of 2026-09-03: collection continues through
the fifth calendar day after the latest currently-published postseason game date.

The frozen rule is enforced mechanically. The end **may move later** and **may
never move earlier**: the highest value ever accepted is persisted in
`<archive_dir>/collection_end.state.json`, checked at every start, and every
accepted change is appended to `config_changes.jsonl`. The check lives beside the
archive rather than in the config file, because the config file is the thing being
edited. The recorder never fetches, parses or monitors the MLB schedule — it reads
one timestamp. Changing it requires no code change.

At the configured end the recorder finalizes `COMPLETE` with close reason
`frozen_collection_end` and exits **75**, which systemd does not restart.

### Low disk

Free space is sampled every 60 s (and once at startup, before anything is
opened). At or **below** the configured R the recorder stops accepting writes,
finalizes the current segment `HALTED_LOW_DISK`, logs the halt prominently and
exits **75**. It does not wait for the disk to reach zero: a filesystem that fills
mid-transaction corrupts the archive it exists to preserve.

If the reserve is already breached at startup, nothing is opened at all and the
process exits 75 immediately — creating a segment on a full disk would fail, and a
failed open is an ordinary error that systemd *would* restart, which is the crash
loop the halt exists to prevent.

### Exit statuses

| code | meaning | systemd |
|---|---|---|
| 0 | clean stop (operator signal, completed capacity run) | restarts (`Restart=always`) |
| 1 | unexpected failure | restarts |
| 3 | startup failed closed (bad config, bad universe artifact hash) | restarts |
| **75** | **intentional terminal stop** — low-disk halt or frozen collection end | **does not restart** |

There was no pre-existing project convention for a non-restart exit status:
`docs/CLOUD_MIGRATION.md`'s `kbot.service` is a bare `Restart=always` with no
`RestartPreventExitStatus`, and `scripts/depthrec_record.py` uses 0/1/3 with no
unit at all. 75 (`EX_TEMPFAIL`) is new here; 3 is kept from `depthrec_record`.

### Install

```bash
sudo cp deploy/threadc-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable threadc-recorder.service
sudo systemctl start threadc-recorder.service          # NOT before §1 and §6
```

---

## 6. One-hour capacity measurement

Run by hand, in its own archive directory, **before** production:

```bash
python scripts/threadc_record.py --config config/threadc.capacity.json \
    --capacity --report
```

**The measured hour is bracketed, not counted from process start.** The first
capacity run measured from startup, so setup, the initial census and the
establishment of ~10,000 subscriptions all landed inside the interval whose bytes
were divided by an hour — real costs, but one-off ones, which made `B_1h`
describe a startup rather than a steady state. The order is now strict, and each
step is a mark in the archive:

1. wait until every shard subscription is established;
2. drain the initial-state banking — also setup;
3. read filesystem usage, CPU and the message counters;
4. mark `measurement_start`;
5. collect for exactly 3,600 seconds (`frozen.CAPACITY_MEASUREMENT_SECONDS`);
6. mark `measurement_end`;
7. read filesystem usage, CPU and the counters again.

`B_1h,disk` is `measurement_disk_growth_bytes` — the difference between the two
filesystem readings. It is never extrapolated from a shorter interval and never
taken from a logical payload-byte counter. If the subscriptions are not
established within `CAPACITY_ESTABLISH_TIMEOUT_SECONDS` the measurement is
**aborted rather than started**: a window run on a half-subscribed universe would
understate the rate, and an understated `B_1h` sizes the reserve too small.

It cannot be confused with production collection: a different `archive_dir`,
`mode=capacity` in every manifest, a close reason of `capacity_run_end`, exit 0
rather than 75, and it is not the systemd unit's `ExecStart`. The production
health check reads only the production archive and never sees it.

Its reserve is the **10 GiB floor** of `R = max(10 GiB, 48 × B_1h)` — the half of
the formula that is stated and needs no measurement — not an estimate of R.

**Rule 1 discovery is disabled in capacity mode, structurally.** It is off
because of the run *mode*, not because of a config value, so there is no key an
operator could flip: `ThreadCConfig.discovery_enabled` is false for
`mode=capacity`, the discovery loop exits immediately, and
`ThreadCRecorder.run_discovery_once()` raises `CapacityModeViolation` if called
at all — even with `force=True`.

Why it must be off: the capacity run is a live-evening measurement and will
normally start well after the 09:00 UTC production target. With discovery enabled
the recorder would immediately treat that day's sweep as *missed* and perform it
— ~25 minutes and ~13,800 authenticated requests, whose REST load, CPU, memory
and response bodies would land inside the very hour whose bytes, CPU and RAM are
being measured. `B_1h` is meant to describe the steady-state write path, so the
sweep is excluded rather than measured. A capacity run also writes nothing to
`discovery_schedule.state.json`, so it cannot consume the production
restart-storm budget either.

The run therefore starts from the verified current append-only universe and
measures exactly two things: ticker/trade storage throughput, and whether that
universe can actually be held on the wire.

The report contains only: the window and its two disk readings, aggregate message
counts by protocol type (ticker, trade, total), raw frames written in the window,
CPU consumed during the window, peak RSS sampled inside the window, reconnects
and errors during the window, plus two aggregate subscription facts —

* `peak_simultaneous_subscribed_markets` — the high-water mark of markets on the
  wire across all shards. An aggregate count with no identities and no
  per-market anything;
* `subscription_limit_error_26_count` — whether Kalshi's "subscription market
  limit exceeded" (AsyncAPI code 26) was returned, and how many times. Counted
  from the `server_error` health event `depthrec.session` records immediately
  before it raises, so each occurrence is seen exactly once per shard.

Nothing market-level, no prices, no spreads, no depth, no outcomes — the full set
of permitted keys is pinned by `tests/test_threadc_capacity.py`.

Then compute `R = max(10 GiB, 48 × bytes_written_stored)` by hand and write it
into `config/threadc.json` as `disk_reserve_bytes`.

**This has been done.** Two bracketed hours were run, one per branch of the
fallback hierarchy:

| branch | channels | `B_1h,disk` |
|---|---|---|
| 1 | `ticker`, `trade` | 373,444,608 bytes/hour |
| 2 | `ticker` | 267,526,144 bytes/hour |

Branch 1 does not fit the 137 GB free for the collection window, so production
collects on Branch 2 and its reserve is sized against Branch 2:
`R = max(10 GiB, 48 × 267,526,144) = 12,841,254,912`, now in
`config/threadc.json` beside the `["ticker"]` selection it was measured with. R
is not recomputed from a different hour — the formula is frozen against that
measurement, and re-sizing it means running the bracketed hour again and saying
which run the new number came from.

---

## 7. Census, discovery cadence, and one shared REST budget

**Market census and subscription refresh — reused unchanged.**
`depthrec.census.RestCensus` runs as-is at its own frozen cadence: a 60 s
`status=open` live pass and a 900 s reconcile pass with no status filter over a
close-time window straddling now. It is given a read-only client adapter exposing
exactly the two attributes it touches (`_read_bucket.acquire()` and
`_markets_api.get_markets_without_preload_content`), so the proven mechanism is
reused without ever constructing an order-capable object. A failed pass is a
`census_request` row with `ok=0`; the census has no delete path.

**Full-universe Rule 1 discovery — once per UTC day, targeted at 09:00 UTC.**
`RestCensus` refreshes *markets within known series*; it never discovers new
series. `scripts/threadc_collection_universe.py` performs the full-catalog
`GET /series` + per-series `/events` sweep, but as a one-shot script with no
cadence. The recorder therefore schedules that sweep itself, in
`threadc.schedule`, on rules that are anchored to the clock and persisted to
disk:

* **Once per UTC calendar day**, targeted at `discovery_daily_target_utc`
  (`09:00:00` in the shipped production config). Anchored to wall-clock UTC and
  **not** to an interval from process start: an interval measured from startup
  shifts a little on every restart and drifts permanently after a few, so a
  reboot would silently move the cadence. A reboot at 03:00 now changes nothing.
* **09:00 UTC** is 05:00 ET — after the previous evening's MLB games have
  settled and hours before the earliest first pitch — so a ~25-minute,
  ~13,800-request sweep competes with live book traffic as little as the
  schedule allows. It runs on a worker thread and never interrupts recording
  either way.
* **A missed target is caught up once.** If the recorder was down at 09:00 and
  comes back at 14:00, that day's sweep runs once, as soon as live recording is
  restored (§4 startup ordering). The catch-up is inherently *today-only*: a
  discovery census is a snapshot of what the exchange says now and cannot be
  taken retroactively for a day already past.
* **One accepted sweep per UTC day.** Only a COMPLETE census accepts the day. An
  INCOMPLETE one leaves it unaccepted so it is retried — the one case where more
  than one sweep runs for a day, and exactly the explicit retry the frozen rule
  allows.

**Restart-storm guard — frozen at 3600 s.**
`frozen.DISCOVERY_ATTEMPT_MIN_INTERVAL_SECONDS = 3600` is not configurable. No
sweep may start within an hour of the previous attempt's **start**, however that
attempt ended — complete, incomplete, or killed mid-sweep.

The timestamp is persisted at
**`<archive_dir>/discovery_schedule.state.json`**, and it is written **before**
the sweep begins, not after it finishes. That ordering is the whole point: with
`Restart=always` and `RestartSec=5`, a process that dies four minutes into a
25-minute sweep would otherwise return with an empty memory and start a fresh
sweep every five seconds. Recording the *intent* to sweep, rather than the
completion of one, is what makes the guard survive the crash it exists to guard
against. A corrupt state file fails closed rather than being discarded —
discarding it would silently remove the guard.

The same file records `accepted_days`, so "already done today" also survives a
restart.

**One shared REST read budget.**
The full-catalog discovery sweep and `RestCensus` do **not** each get a nominal
20 requests/second. Both reach the exchange through the *same*
`threadc.readonly.ReadOnlyAPI` instance, and every request — from either path —
passes through the single `TokenBucket` inside `ReadOnlyAPI.get_raw()`. The
coordination is three concrete facts:

1. `ThreadCRecorder` constructs exactly one `ReadOnlyAPI` and hands that same
   object both to `RestCensus` (wrapped in `CensusClientAdapter`) and to
   `run_discovery_census`.
2. `RestCensus` acquires `client._read_bucket` before each call; that one is a
   deliberate **no-op** (`_NullBucket`), so the census does not double-charge a
   second, independent limiter. The real limiter is reached one layer down, in
   `get_raw`, which is also where the 14 discovery probe threads charge theirs.
3. `TokenBucket.acquire()` **reserves** a slot under its lock and sleeps to an
   absolute deadline. The shape used by `KalshiClient._TokenBucket` and by
   `scripts/threadc_collection_universe.py` instead releases the lock and lets
   every blocked caller sleep a wait computed from the same instant — so N
   threads arriving together all wake at once and the effective rate overshoots
   by roughly the concurrency. That is tolerable for a one-shot script with a
   single consumer; it is not tolerable when a 14-thread probe and the census
   thread share one budget, which is why Thread C's bucket queues waiters one
   slot apart. `tests/test_threadc_recorder.py` pins the aggregate bound.

The consequence to expect in production: while a sweep is running, the 60 s
market census shares the same 20/s budget and its passes may take longer. That
is the intended trade — one aggregate budget, not two.

**Gate 3 during production — an incomplete census never stops recording.** If any
series is unreadable, missing its probe, or exhausts its retries, the census is
`INCOMPLETE`. It then: applies **zero** additions and **zero** removals; is logged
with its errors; is persisted as an operational record in `discovery_census` and
in `universe_observations.jsonl`; leaves the UTC day unaccepted so it is retried
after the guard interval; sets `any_census_incomplete: true` in the segment
manifest; and **live recording continues uninterrupted on the entire
last-known-good append-only universe**. A later COMPLETE census recovers whatever
the earlier one could not read. A transient REST or metadata failure never causes
an already-covered live market to stop being recorded.

---

## 8. Daily health check

```bash
python scripts/threadc_health.py --config config/threadc.json
```

Exactly four facts, exit 0 when all four hold:

1. Is the Thread C recorder service active?
2. Did yesterday's archive segments finalize successfully with `COMPLETE`?
3. Is filesystem free space greater than the configured R?
4. Did yesterday's segments bank any raw frames at all?

A UTC day is healthy only when it produced at least one segment and *every*
segment for that day is `COMPLETE` — one `INCOMPLETE_CRASH` beside two clean ones
reads as unhealthy, because that gap is exactly what the operator needs to see.

Fact 4 is the aggregate raw frame count over the day's segments, summed from the
manifests, and a day totalling **0 is not healthy**. Facts 1–3 are all satisfied
by a recorder that banks nothing: an empty subscription set still rotates at
00:00 UTC, still finalizes `COMPLETE`, and still leaves free space alone, so 63
days of empty archives would have passed every check. It is one number for the
day and stays that way — a per-market, per-series or per-price breakdown would
turn the operational check into research output.

It opens no database and modifies nothing. No price, spread, depth, market
activity, return or outcome is displayed or reachable. Add `--json` for
monitoring. Other operational logs may carry aggregate bytes, aggregate frame
counts, CPU/RAM, disk usage, connection state, reconnects, errors, service
starts/restarts and census status — and nothing else.

---

## 9. Reading an archive back

Each segment is self-describing: compression dictionaries live in the database,
not in a package constant, and every row carries a SHA-256 of the **uncompressed**
frame, so a read is also a proof that the archive reproduces the exact bytes that
arrived.

```python
import sqlite3
from depthrec.store import read_raw_text

conn = sqlite3.connect("threadc-2026-09-04-seg001.sqlite")
conn.row_factory = sqlite3.Row
zdicts = {r["id"]: bytes(r["zdict"])
          for r in conn.execute("SELECT id, zdict FROM raw_dictionary")}
for row in conn.execute("SELECT * FROM ws_raw ORDER BY id"):
    frame = read_raw_text(row, zdicts)   # raises if the bytes do not match
```

Tables: `ws_raw` · `raw_dictionary` · `ws_session` · `health_event` ·
`census_request` · `census_market` · `census_change` · `discovery_census` ·
`shard_assignment` · `recorder_process` · `meta`.

---

## 10. Tests

```bash
pytest tests/test_threadc_*.py -q
```

`test_threadc_config.py` · `test_threadc_universe.py` · `test_threadc_segment.py` ·
`test_threadc_recorder.py` · `test_threadc_schedule.py` ·
`test_threadc_isolation.py` · `test_threadc_health.py` · `test_threadc_capacity.py`
