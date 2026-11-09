# Thread C -- Collection Universe v1

**Manifest date:** 2026-09-03  
**Census run (UTC):** 2026-09-03T19:54:27.381413+00:00  
**Exchange base URL:** `https://external-api.kalshi.com/trade-api/v2`  
**Qualifying series:** **138**  
**Series examined:** 13788 (the entire `GET /series` catalog, no prefilter)  
**Read requests issued:** 483

---

## 1. Why this manifest exists

The MLB regular season ends **2026-09-27**, and collection runs through the postseason to a frozen end date. Order-book **depth at a decision moment cannot be reconstructed after the fact** -- historical candlesticks preserve price and volume, not the resting size on each side of the book -- so live books have to be recorded while they exist, or the observation is lost until April.

This document is **neutral data banking**. It defines *what to record* and nothing else. No analysis protocol exists yet, and none is anticipated here: no hypothesis, no signal definition, no strategy, and no market subset chosen for its expected interestingness. The universe is defined by official exchange metadata alone precisely so that a protocol written later cannot be accused of having selected its own sample.

## 2. The inclusion predicate

A Kalshi series S is a member of Collection Universe v1 if and only if at least one event E returned by `GET /events?series_ticker=<S.series_ticker>` satisfies

        E["product_metadata"]["competition"] == "Pro Baseball"

under exact, case-sensitive string equality. `"Pro Baseball"` is Kalshi's own value for Major League Baseball in that controlled field; the literal string `"MLB"` never appears in it. Nothing else is consulted: no ticker prefix or substring, no title text, no tag, no category, no settlement source, no fuzzy or substring matching.

### Why the predicate reads event metadata, not the ticker or the series row

Kalshi's documentation directs integrators to series and event metadata rather than to ticker parsing, and on this exchange ticker parsing is not merely discouraged -- it is measurably wrong. Observed in this census:

| Ticker | What the ticker suggests | What the metadata says |
|---|---|---|
| `KXMLBWORLD` | MLB | competition `"World Baseball Classic"` -- a national-team tournament, not MLB |
| `KXMBLNLEAST` | nothing (transposed `MBL`) | "National League East Winner"; any `^KXMLB` regex misses it |
| `KXCITYMLBEXPAND` | MLB | competition `"Pro Baseball"`, while the series is tagged `['Hockey']` |
| `KXMLBMENTION` | MLB | competition `"Pro Baseball"`, in category `"Mentions"` |

The series row itself cannot carry the decision either. `GET /series` returns 13788 series and **no `product_metadata` key at all**; its `category` tops out at `"Sports"`, and its `tags` top out at `["Baseball"]` -- the identical tag set is carried by MLB, KBO, NPB, LMB, NCAA baseball, NCAA softball, the World Baseball Classic and the Congressional Baseball Game. `GET /series/{ticker}` does return `product_metadata`, but for baseball it holds only a bare `{"scope": ...}`. The league appears exactly once in the API surface: in `events[].product_metadata.competition`.

### Why "at least one event" and not "every event"

Event-level `product_metadata` is unevenly populated. Some series carry `competition: null` on part of their events and `"Pro Baseball"` on the rest -- all 30 `KXMLBWINS-*` team win-total series do exactly this. A universal quantifier would drop those series for a metadata gap rather than for anything about which league they belong to.

### Why the census has no prefilter

The predicate is evaluated for every one of the 13788 series in the catalog. Probing only `category == "Sports"`, or only series tagged `"Baseball"`, would be far cheaper and would silently lose real members: `KXCITYMLBEXPAND` carries `"Pro Baseball"` while tagged `['Hockey']`, and `KXMLBMENTION` carries it from category `"Mentions"`. A heuristic prefilter would reintroduce, one layer down, exactly the guessing this predicate exists to eliminate.

## 3. Frozen rules

**Rule 1 -- Automatic admission of new series (append-only discovery log).** Any series created after this manifest whose official Kalshi metadata satisfies the inclusion predicate above is added to Collection Universe v1 automatically, from the first census that discovers it. Its admission is logged append-only with the discovery timestamp and the verbatim metadata that satisfied the predicate. No backfill is attempted and none is possible: history before the discovering census is simply unavailable, and the record says so rather than interpolating it.

**Rule 2 -- Inclusion is append-only for the collection period.** Once a series qualifies, it remains in Collection Universe v1 through the frozen collection end date even if Kalshi later edits, relabels, or removes the metadata that admitted it. Membership is a fact about what the metadata said when the series was admitted, not a live query. Nothing is ever removed from v1 during the collection period; a later metadata change is recorded as an observation, never as an eviction.

Both rules reference the **frozen collection end date** symbolically. This manifest deliberately does not assert what that date is: the regular season ending 2026-09-27 and a run through the postseason are the stated context, but the exact frozen end date is set by the collection plan, not by the universe definition. Nothing in the predicate or in either rule depends on its value.

## 4. Provenance and integrity

Kalshi metadata is mutable, so the exact response that produced v1 is stored verbatim alongside this manifest and hashed. The hash below is of the **uncompressed** response body exactly as the exchange sent it, so it can be checked against a fresh call to the same endpoint.

| Item | Value |
|---|---|
| Series catalog endpoint | `GET https://external-api.kalshi.com/trade-api/v2/series` |
| Retrieved at (UTC) | `2026-09-03T19:54:27.381413+00:00` |
| Response size | 16,525,830 bytes |
| **SHA-256 of response body** | `4a4034c66c686891f362c1d613457b61126d7177724054a6163698b8515aca0e` |
| Stored snapshot (gzip of those bytes) | `research/threadc_universe_v1/series_catalog_raw.json.gz` |
| Per-series event-metadata evidence | `research/threadc_universe_v1/events_probe.json` (SHA-256 `5aadd7c094e9811cd925658ac64eca0ba5c12ab6562b8d85cc7b8e35fa59cb6f`) |
| Machine-readable manifest | `research/threadc_universe_v1/universe_v1.json` (SHA-256 `a076b6a674aea966520583f820f27aea9b617acb2e23f829928572e6d28fd93e`) |
| Hash list for all artifacts | `research/threadc_universe_v1/SHA256SUMS` |

Verify the catalog snapshot with:

```bash
gunzip -c research/threadc_universe_v1/series_catalog_raw.json.gz | sha256sum
# expect 4a4034c66c686891f362c1d613457b61126d7177724054a6163698b8515aca0e
```

### Endpoints used (complete list -- all reads, all GET)

1. `GET /series` -- the official series catalog (13788 series), hashed above.
2. `GET /events?series_ticker=<ticker>&limit=200` (cursor-paged to exhaustion) -- the `product_metadata.competition` evidence the predicate decides on, once per series in the catalog.
3. `GET /series/<ticker>` -- series-level `product_metadata`, `settlement_sources` and `frequency`, fetched for qualifying series only.

The script that produced this manifest is `scripts/threadc_collection_universe.py`. It is structurally read-only: it never constructs a `KalshiClient` (the class that owns `place_order()` / `cancel_order()`), it issues HTTP GET only, and it refuses any path outside the three-entry allowlist above.

## 5. Ambiguity check: MLB vs other baseball competitions

**Verdict: MLB is cleanly distinguishable from every other named baseball competition; two boundary sets remain unresolved and are quarantined below rather than guessed**

`product_metadata.competition` is a controlled vocabulary, and Kalshi names each baseball competition separately. The values observed across every baseball-affiliated series in this census:

| competition value | series | treated as |
|---|---|---|
| `Pro Baseball` | 138 | **MLB -- included** |
| `World Baseball Classic` | 15 | other baseball competition -- excluded |
| `Home Run Derby` | 13 | MLB-adjacent -- quarantined (Q1), not included |
| `College Baseball` | 11 | other baseball competition -- excluded |
| `All-Star` | 8 | MLB-adjacent -- quarantined (Q1), not included |
| `Japan NPB` | 6 | other baseball competition -- excluded |
| `Korea KBO` | 5 | other baseball competition -- excluded |
| `Pro Baseball Draft` | 2 | MLB-adjacent -- quarantined (Q1), not included |
| `College Softball` | 2 | other baseball competition -- excluded |
| `Mexico LMB` | 1 | other baseball competition -- excluded |
| `Pro Baseball Spring Training` | 1 | MLB-adjacent -- quarantined (Q1), not included |
| `World Baseball Classic Exhibition` | 1 | other baseball competition -- excluded |

### Gate 1 -- disjointness (machine-checked every run)

For every qualifying series, the census collects every *other* competition value that series also exhibits. If a qualifying series were ever to carry a rival baseball competition as well (KBO / NPB / LMB / College Baseball / College Softball / WBC / MiLB), the metadata would not be cleanly separating MLB from other baseball, and **the script exits non-zero and writes nothing** rather than applying a tiebreak.

**Gate 1 passed.** No qualifying series carries any rival baseball competition. `"Pro Baseball"` co-occurs only with `null` and with `"Pro Baseball Spring Training"`. Kalshi's league labels are mutually exclusive per series, so MLB *is* cleanly distinguishable from KBO, NPB, LMB, college baseball, college softball and the World Baseball Classic for every series that has event metadata at all.

### Gate 2 -- unknown vocabulary (machine-checked every run)

Any competition value on a baseball-affiliated series that this script has not classified aborts the run, so new Kalshi vocabulary is reviewed by a human before it lands silently inside or outside the universe.

**Gate 2 passed.** Every competition value observed on a baseball-affiliated series is classified.

### Gate 3 -- complete evidence (machine-checked every run)

A series the census could not read is **not** a series with no MLB events, but downstream the two are indistinguishable -- both simply fail to appear. So if any series in the catalog cannot be read, the run exits non-zero and writes nothing rather than publishing a quietly smaller universe. This is not hypothetical: an earlier census of this same catalog had 46 series exhaust their retry budget on transient HTTP 429s, two of them baseball (`KXMLBF5SPREAD`, `KXMLBHRR`), which would have dropped two real members behind a single warning line. Cached probe errors are never treated as evidence; they are re-probed on every run.

**Gate 3 passed.** All 13788 series in the catalog were read successfully; no series is in an errored or unprobed state.

### Where ambiguity does remain (quarantined, not resolved)

The clean separation above holds *between named leagues*. Two boundaries are genuinely not settled by the metadata, so this manifest reports them and **does not decide them**. Neither is auto-included; resolving either is a human decision that would produce a v2, not a heuristic applied here.

**Q1 -- MLB-adjacent competition values (22 series).** Kalshi labels some MLB-affiliated products with their own competition value rather than `"Pro Baseball"`. These do not satisfy the predicate. They are MLB in ordinary usage, but the metadata does not say so -- there is no `"KBO All-Star"` or `"NPB Home Run Derby"` value in the vocabulary to contrast against, so "All-Star" alone cannot be read as league-specific from the field itself.

| series_ticker | title | competition value(s) | events |
|---|---|---|---|
| `KXMLBALLSTAR` | Pro Baseball All-Stars | `All-Star` | 2 |
| `KXMLBALLSTARHR` | Pro Baseball All-Star Game Total Home Runs | `All-Star` | 1 |
| `KXMLBASGHIT` | All Star Game Hits | `All-Star` | 1 |
| `KXMLBASGHR` | All Star Game Home Runs | `All-Star` | 1 |
| `KXMLBASGKS` | All Star Game Strikesouts | `All-Star` | 1 |
| `KXMLBASGMVP` | Pro Baseball All-Star Game MVP | `All-Star` | 1 |
| `KXMLBDRAFTPICK` | Pro Baseball Draft Pick | `Pro Baseball Draft` | 5 |
| `KXMLBDRAFTTOP` | Pro Baseball Top Pick | `Pro Baseball Draft` | 3 |
| `KXMLBFTGAME` | Pro Baseball Futures Game | `All-Star` | 1 |
| `KXMLBHRDERBY` | Pro Baseball Homerun Derby | `Home Run Derby` | 2 |
| `KXMLBHRDERBY500` | 500+ Foot HRs at the Home Run Derby | `Home Run Derby` | 1 |
| `KXMLBHRDERBYDISTANCE` | Pro Baseball Home Run Derby Longest Distance | `Home Run Derby` | 1 |
| `KXMLBHRDERBYFIN` | Pro Baseball Home Run Derby Finals Qualifiers | `Home Run Derby` | 1 |
| `KXMLBHRDERBYFORECAST` | Home Run Derby Finals Forecast | `Home Run Derby` | 1 |
| `KXMLBHRDERBYLONGEST` | Pro Baseball Home Run Derby Player to Hit Longest | `Home Run Derby` | 1 |
| `KXMLBHRDERBYMATCHUP` | Pro Baseball Home Run Derby Final Matchup | `Home Run Derby` | 1 |
| `KXMLBHRDERBYOU` | Pro Baseball Home Run Derby Over/Under Home Runs | `Home Run Derby` | 2 |
| `KXMLBHRDERBYQUAL` | Pro Baseball Home Run Derby Selections | `Home Run Derby` | 1 |
| `KXMLBHRDERBYR1LEAD` | Round 1 Leader | `Home Run Derby` | 1 |
| `KXMLBHRDERBYSEMI` | Pro Baseball Home Run Derby Semifinals Qualifiers | `Home Run Derby` | 1 |
| `KXMLBHRDERBYTOT` | Home Run Derby Total Home Runs | `Home Run Derby` | 1 |
| `KXMLBHRDERBYVELO` | Highest Exit Velocity Home run | `Home Run Derby` | 1 |

One consequence is worth stating plainly rather than smoothing over: `KXMLBSTGAME` ("Pro Baseball Spring Training game") carries **both** `"Pro Baseball"` and `"Pro Baseball Spring Training"` on its events, so it **does** satisfy the predicate and **is** in v1, while the other adjacent values are out. That asymmetry is an artifact of Kalshi's own tagging, not a judgement made here.

**Q2 -- no event metadata to evaluate (18 series).** A series with no events, or whose every event carries `competition: null`, gives the predicate nothing to test. This is not a marginal set for Thread C: it currently contains postseason series that matter directly to this collection.

| series_ticker | title | tags | events |
|---|---|---|---|
| `KXALALLSTAR` | Player Selected to AL All-Star Team | Baseball | 0 |
| `KXCONGRESSBASEBALL` | Congressional Baseball Game | Baseball | 0 |
| `KXLEADERMLBSAVES` | MLB Saves Leader | Baseball | 0 |
| `KXLEADERMLBSTRIKEOUTS` | MLB Strikeouts Leader | Baseball | 0 |
| `KXMBLNLEAST` | National League East Winner | Baseball | 0 |
| `KXMILBGAME` | MILB Game | Baseball | 0 |
| `KXMLBALCSMVP` | Pro Baseball American League Championship Series MVP | Baseball | 0 |
| `KXMLBFOD` | Field of Dreams Teams | Baseball | 0 |
| `KXMLBNLCSMVP` | Pro Baseball National League Championship Series MVP | Baseball | 0 |
| `KXMLBWALK` | Pro Baseball Walks | Baseball | 0 |
| `KXMLBWS` | MLB World Series | Baseball | 0 |
| `KXNCAAMBACHAMP` | College Baseball Championship | Baseball | 1 |
| `KXNEXTCONTRACTSKUBAL` | Tarik Skubal Next Contract Value | Baseball | 0 |
| `KXNLMOTY` | DO NOT USE | Baseball | 0 |
| `KXOHTANIMVPCY` | Shohei Ohtani MVP and Cy Young | Baseball | 0 |
| `KXWBCHIT` | World Baseball Classic Hits | Baseball | 0 |
| `KXWSAL` | MLB American League champion | Baseball | 0 |
| `KXWSNL` | MLB National League champion | Baseball | 0 |

**Rule 1 is the designed mitigation for Q2.** These series are not judged, excluded on a guess, or admitted on their titles. They enter Collection Universe v1 automatically at the first census that finds them carrying `"Pro Baseball"` -- which is what will happen when Kalshi creates their postseason events -- and the discovery is logged append-only with the metadata that admitted them.

One limitation is stated rather than hidden: a series with **zero events and no `"Baseball"` tag** is invisible both to the predicate and to this quarantine report, because nothing in its official metadata connects it to baseball. Only Rule 1 can recover such a series, and only once it has events.

## 6. Collection Universe v1 -- the qualifying series

138 series. Every row qualified on the same predicate; the `product_metadata.competition` column is the verbatim evidence, with the count of events carrying each value and their `competition_scope` values, so the predicate can be re-run and the same universe reconstructed.

| # | series_ticker | title | category | tags | series product_metadata | events[].product_metadata.competition (verbatim, with counts and scopes) |
|---|---|---|---|---|---|---|
| 1 | `KXCITYMLBEXPAND` | MLB expansion | Sports | Hockey | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 2 | `KXCOACHOUTMLB` | Baseball Managers Out | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: "Managers"×1] |
| 3 | `KXCOACHOUTMLBDATE` | MLB Coach Out Date | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: "Managers"×1] |
| 4 | `KXLEADERMLBAVG` | MLB Batting Average Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 5 | `KXLEADERMLBDOUBLES` | MLB Doubles Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 6 | `KXLEADERMLBERA` | MLB ERA Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 7 | `KXLEADERMLBHITS` | MLB Hits Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 8 | `KXLEADERMLBHR` | MLB Home Runs Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 9 | `KXLEADERMLBKS` | Pro Baseball Strikeouts Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 10 | `KXLEADERMLBOPS` | MLB OPS Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 11 | `KXLEADERMLBRBI` | MLB RBIs Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 12 | `KXLEADERMLBRUNS` | MLB Runs Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 13 | `KXLEADERMLBSTEALS` | MLB Steals Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 14 | `KXLEADERMLBTRIPLES` | MLB Triples Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 15 | `KXLEADERMLBWAR` | MLB WAR Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 16 | `KXLEADERMLBWINS` | MLB Wins Leader | Sports | Baseball | `{"scope": "League Leader"}` | "Pro Baseball"×1 [scopes: "League Leader"×1] |
| 17 | `KXMLB` | World Series | Sports | Baseball | `{"scope": "Future"}` | "Pro Baseball"×2 [scopes: "Future"×1, "Game"×1] |
| 18 | `KXMLBAL` | MLB American League Championship | Sports | Baseball | `{"scope": "Future"}` | "Pro Baseball"×2 [scopes: "Future"×1, "Game"×1] |
| 19 | `KXMLBALCENT` | American League Central Winner | Sports | Baseball | `{"important_info": null, "scope": "Divisions"}` | "Pro Baseball"×2 [scopes: "Divisions"×2] |
| 20 | `KXMLBALCPOTY` | Pro Baseball American League Comeback Player of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 21 | `KXMLBALCY` | Pro Baseball American League Cy Young | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 22 | `KXMLBALEAST` | American League East Winner | Sports | Baseball | `{"important_info": null, "scope": "Divisions"}` | "Pro Baseball"×2 [scopes: "Divisions"×2] |
| 23 | `KXMLBALHAARON` | Pro Baseball American League Hank Aaron | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×1 [scopes: "Awards"×1] |
| 24 | `KXMLBALMOTY` | Pro Baseball American League Manager of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 25 | `KXMLBALMVP` | Pro Baseball American League MVP | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 26 | `KXMLBALRELOTY` | Pro Baseball American League Reliever of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 27 | `KXMLBALROTY` | Pro Baseball American League Rookie of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 28 | `KXMLBALWEST` | American League West Winner | Sports | Baseball | `{"important_info": null, "scope": "Divisions"}` | "Pro Baseball"×2 [scopes: "Divisions"×2] |
| 29 | `KXMLBASGAME` | Professional Baseball All-Star Game | Sports | Baseball | `null` | null×1 [scopes: null×1]; "Pro Baseball"×1 [scopes: "Game"×1] |
| 30 | `KXMLBAWARDCOMBO` | Pro Baseball Award Combo | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×1 [scopes: "Awards"×1] |
| 31 | `KXMLBAWARDFIN` | Pro Baseball Award Finalists | Sports | Baseball | `{"scope": "Award Finalists"}` | "Pro Baseball"×4 [scopes: "Award Finalists"×4] |
| 32 | `KXMLBBESTRECORD` | Pro Baseball Best Record | Sports | Baseball | `{"important_info": null, "scope": "Future"}` | "Pro Baseball"×1 [scopes: "Future"×1] |
| 33 | `KXMLBCBA` | CBA | Entertainment | Baseball | `{"scope": "2027 Season"}` | "Pro Baseball"×1 [scopes: "2027 Season"×1]; null×1 [scopes: null×1] |
| 34 | `KXMLBDEBUT` | Pro Baseball Debut | Sports | Baseball | `{"scope": "Player Debut"}` | "Pro Baseball"×22 [scopes: "Player Debut"×22] |
| 35 | `KXMLBEOTY` | Pro Baseball Executive of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×1 [scopes: "Awards"×1] |
| 36 | `KXMLBERA` | Pro Baseball Earned Runs | Sports | Baseball | `{"important_info": {"id": "KXMLBERA-2026-08-17", "markdown": "**Important information:** If this pitcher does not start the game or starts the game but does not record at least one batter faced, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Earned Runs"}` | "Pro Baseball"×235 [scopes: "Earned Runs"×235] |
| 37 | `KXMLBEXTRAS` | MLB Extra Innings | Sports | Baseball | `{"scope": "Extra Innings"}` | "Pro Baseball"×1511 [scopes: "Extra Innings"×1511] |
| 38 | `KXMLBF3` | First 3 Innings Winner | Sports | Baseball | `{"scope": "First 3 Innings Winner"}` | "Pro Baseball"×843 [scopes: "First 3 Innings Winner"×843] |
| 39 | `KXMLBF5` | First 5 Innings Winner | Sports | Baseball | `{"scope": "First 5 Innings Winner"}` | "Pro Baseball"×2119 [scopes: "First 5 Innings Winner"×2119] |
| 40 | `KXMLBF5SPREAD` | First 5 Innings Spread | Sports | Baseball | `{"scope": "First 5 Innings Spread"}` | "Pro Baseball"×2129 [scopes: "First 5 Innings Spread"×2129] |
| 41 | `KXMLBF5TOTAL` | First 5 Innings Total | Sports | Baseball | `{"scope": "First 5 Innings Total"}` | "Pro Baseball"×2118 [scopes: "First 5 Innings Total"×2118] |
| 42 | `KXMLBF7` | First 7 Innings Winner | Sports | Baseball | `{"scope": "First 7 Innings Winner"}` | "Pro Baseball"×843 [scopes: "First 7 Innings Winner"×843] |
| 43 | `KXMLBFASTPITCH` | Fastest Pitch of season | Sports | Baseball | `{"scope": "Season Milestones"}` | "Pro Baseball"×1 [scopes: "Season Milestones"×1] |
| 44 | `KXMLBFODTEAMS` | Field of Dreams Teams | Sports | (none) | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 45 | `KXMLBGAME` | Professional Baseball Game | Sports | Baseball | `{"scope": "Game"}` | "Pro Baseball"×3403 [scopes: "Game"×3403]; null×949 [scopes: null×949]; "All-Star"×1 [scopes: "Game"×1] |
| 46 | `KXMLBGG` | Pro Baseball Gold Glove | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×20 [scopes: "Awards"×20] |
| 47 | `KXMLBHA` | Pro Baseball Hits Allowed | Sports | Baseball | `{"important_info": {"id": "KXMLBHA-2026-08-17", "markdown": "**Important information:** If this pitcher does not start the game or starts the game but does not record at least one batter faced, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Hits Allowed"}` | "Pro Baseball"×235 [scopes: "Hits Allowed"×235] |
| 48 | `KXMLBHIT` | Pro Baseball Hits | Sports | Baseball | `{"important_info": {"id": "KXMLBHIT-2026-05-29", "markdown": "**Important information:** If this player does not start the game, or starts the game but does not record at least one plate appearance, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Hits"}` | "Pro Baseball"×2114 [scopes: "Hits"×2114] |
| 49 | `KXMLBHR` | Pro Baseball Home Runs | Sports | Baseball | `{"important_info": {"id": "KXMLBHR-2026-05-29", "markdown": "**Important information:** If this player does not start the game, or starts the game but does not record at least one plate appearance, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Home Runs"}` | "Pro Baseball"×2113 [scopes: "Home Runs"×2113] |
| 50 | `KXMLBHRR` | Pro Baseball Hits Runs RBIs | Sports | Baseball | `{"important_info": {"id": "KXMLBHRR-2026-05-29", "markdown": "**Important information:** If this player does not start the game, or starts the game but does not record at least one plate appearance, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Hits + Runs + RBIs"}` | "Pro Baseball"×2114 [scopes: "Hits + Runs + RBIs"×2114] |
| 51 | `KXMLBINNINGTOTAL` | Pro Baseball Inning Total | Sports | Baseball | `{"important_info": {"id": "KXMLBINNINGTOTAL-2026-08-19", "markdown": "**Important information:** This market resolves based on the runs scored during the specified inning.", "message": "", "title": ""}, "scope": "Inning Total Runs"}` | "Pro Baseball"×1701 [scopes: "9th Inning Total"×189, "8th Inning Total"×189, "7th Inning Total"×189, "6th Inning Total"×189, "5th Inning Total"×189, "4th Inning Total"×189, "3rd Inning Total"×189, "2nd Inning Total"×189, "1st Inning Total"×189] |
| 52 | `KXMLBINNINGWIN` | Pro Baseball Inning Win | Sports | Baseball | `{"important_info": {"id": "KXMLBINNINGWIN-2026-08-18", "markdown": "**Important information:** This market resolves based on which team scores more runs during the specified inning.", "message": "", "title": ""}, "scope": "Inning Winner"}` | "Pro Baseball"×1701 [scopes: "9th Inning Winner"×189, "8th Inning Winner"×189, "7th Inning Winner"×189, "6th Inning Winner"×189, "5th Inning Winner"×189, "4th Inning Winner"×189, "3rd Inning Winner"×189, "2nd Inning Winner"×189, "1st Inning Winner"×189] |
| 53 | `KXMLBKS` | Pro Baseball Strikeouts | Sports | Baseball | `{"important_info": {"id": "KXMLBKS-2026-05-29", "markdown": "**Important information:** If this pitcher does not start the game or starts the game but does not record at least one batter faced, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Strikeouts"}` | "Pro Baseball"×2107 [scopes: "Strikeouts"×2107] |
| 54 | `KXMLBLSTREAK` | Longest Losing Streak | Sports | Baseball | `{"important_info": null}` | "Pro Baseball"×1 [scopes: null×1] |
| 55 | `KXMLBMATCHUP` | Pro Baseball Playoff Matchups | Sports | Baseball | `{"scope": "Playoff Matchups"}` | "Pro Baseball"×6 [scopes: "Playoff Matchups"×6] |
| 56 | `KXMLBMENTION` | MLB Announcers | Mentions | Sports | `{"scope": "Mentions"}` | "Pro Baseball"×184 [scopes: "Mentions"×184] |
| 57 | `KXMLBNEXTHR` | Pro Baseball Next Homerun | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 58 | `KXMLBNEXTTEAM` | Pro Baseball Next Team | Sports | Baseball | `{"scope": "Next Team"}` | "Pro Baseball"×12 [scopes: "Next Team"×12] |
| 59 | `KXMLBNL` | MLB National League Championship | Sports | Baseball | `{"scope": "Future"}` | "Pro Baseball"×2 [scopes: "Future"×2] |
| 60 | `KXMLBNLCENT` | National League Central Winner | Sports | Baseball | `{"important_info": null, "scope": "Divisions"}` | "Pro Baseball"×2 [scopes: "Divisions"×2] |
| 61 | `KXMLBNLCPOTY` | National League Comeback Player of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 62 | `KXMLBNLCY` | Pro Baseball National League Cy Young | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 63 | `KXMLBNLEAST` | National League East Winner | Sports | Baseball | `{"important_info": null, "scope": "Divisions"}` | "Pro Baseball"×2 [scopes: "Divisions"×2] |
| 64 | `KXMLBNLHAARON` | Pro Baseball American League Hank Aaron | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×1 [scopes: "Awards"×1] |
| 65 | `KXMLBNLMOTY` | Pro Baseball National League Manager of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 66 | `KXMLBNLMVP` | Pro Baseball National League MVP | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 67 | `KXMLBNLRELOTY` | Pro Baseball National League Reliever of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 68 | `KXMLBNLROTY` | Pro Baseball National League Rookie of the Year | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×2 [scopes: "Awards"×2] |
| 69 | `KXMLBNLWEST` | National League West Winner | Sports | Baseball | `{"important_info": null, "scope": "Divisions"}` | "Pro Baseball"×2 [scopes: "Divisions"×2] |
| 70 | `KXMLBOPENINGDAY` | Pro Baseball Opening Day | Sports | Baseball | `{"scope": "2027 Season"}` | "Pro Baseball"×1 [scopes: "2027 Season"×1] |
| 71 | `KXMLBOUTS` | Pro Baseball Outs Recorded | Sports | Baseball | `{"important_info": {"id": "KXMLBOUTS-2026-06-22", "markdown": "**Important information:** If this pitcher does not start the game or starts the game but does not record at least one pitch, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Outs Recorded"}` | "Pro Baseball"×912 [scopes: "Outs Recorded"×912] |
| 72 | `KXMLBPITCH` | Pro Baseball Player to Pitch | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 73 | `KXMLBPITCHEROTM` | Pro Baseball Pitcher of the Month | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×8 [scopes: "Awards"×8] |
| 74 | `KXMLBPLAYEROTM` | Pro Baseball Player of the Month | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×8 [scopes: "Awards"×8] |
| 75 | `KXMLBPLAYEROTW` | Pro Baseball Player of the Week | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×4 [scopes: "Awards"×4] |
| 76 | `KXMLBPLAYOFFS` | Pro Baseball Playoff Qualifiers | Sports | Baseball | `{"scope": "Future"}` | "Pro Baseball"×2 [scopes: "Future"×2] |
| 77 | `KXMLBRBI` | Pro Baseball RBIs | Sports | Baseball | `{"important_info": {"id": "KXMLBRBI-2026-06-01", "markdown": "**Important information:** If this player does not start the game, or starts the game but does not record at least one plate appearance, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "RBIs"}` | "Pro Baseball"×943 [scopes: "RBIs"×943] |
| 78 | `KXMLBRETURN` | Pro Baseball Player Return | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: "Player Return"×1] |
| 79 | `KXMLBRFI` | Pro Baseball Run in First Inning | Sports | Baseball | `{"scope": "YRFI/NRFI"}` | "Pro Baseball"×2162 [scopes: "YRFI/NRFI"×2162] |
| 80 | `KXMLBSB` | Pro Baseball Stolen Bases | Sports | Baseball | `{"important_info": {"id": "KXMLBSB-2026-06-22", "markdown": "**Important information:** If this player does not start the game, or starts the game but does not record at least one plate appearance, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Stolen Bases"}` | "Pro Baseball"×940 [scopes: "Stolen Bases"×940] |
| 81 | `KXMLBSEASONGAMES` | Pro Baseball Games Played in a Season | Sports | Baseball | `{"scope": "2027 Season"}` | "Pro Baseball"×1 [scopes: "2027 Season"×1] |
| 82 | `KXMLBSEASONHR` | Pro Baseball Season Home Runs | Sports | Baseball | `{"important_info": null, "scope": "Season Stats"}` | "Pro Baseball"×5 [scopes: "Season Stats"×5] |
| 83 | `KXMLBSERIES` | Professional Baseball Series | Sports | Baseball | `{"scope": "Series Winner"}` | "Pro Baseball"×9 [scopes: "Series"×8, "Series Winner"×1] |
| 84 | `KXMLBSERIESEXACT` | Professional Baseball Series Exact Result | Sports | Baseball | `{"scope": "Series"}` | "Pro Baseball"×7 [scopes: "Series"×7] |
| 85 | `KXMLBSERIESGAMETOTAL` | Professional Baseball Series Total Games | Sports | Baseball | `{"scope": "Series"}` | "Pro Baseball"×3 [scopes: "Series"×3] |
| 86 | `KXMLBSISTREAK` | Pro Baseball Scoreless Innings Streak | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 87 | `KXMLBSPREAD` | Pro Baseball Spread | Sports | Baseball | `{"scope": "Spread"}` | "Pro Baseball"×2159 [scopes: "Spread"×2159] |
| 88 | `KXMLBSS` | Pro Baseball Silver Slugger | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×15 [scopes: "Awards"×15] |
| 89 | `KXMLBSTAT` | Pro Baseball Season Stat | Sports | Baseball | `{"scope": "Season Milestones"}` | "Pro Baseball"×11 [scopes: "Season Milestones"×11] |
| 90 | `KXMLBSTATCOUNT` | Pro Baseball Season Stat | Sports | Baseball | `{"important_info": null, "scope": "Season Milestones"}` | "Pro Baseball"×6 [scopes: "Season Milestones"×6] |
| 91 | `KXMLBSTGAME` | Pro Baseball Spring Training game | Sports | Baseball | `{"scope": "Game"}` | "Pro Baseball"×359 [scopes: "Game"×359]; "Pro Baseball Spring Training"×92 [scopes: "Game"×92] |
| 92 | `KXMLBTB` | Pro Baseball Total Bases | Sports | Baseball | `{"important_info": {"id": "KXMLBTB-2026-05-29", "markdown": "**Important information:** If this player does not start the game, or starts the game but does not record at least one plate appearance, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Total Bases"}` | "Pro Baseball"×2114 [scopes: "Total Bases"×2114] |
| 93 | `KXMLBTEAMSALE` | Pro Baseball Team Sale | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 94 | `KXMLBTEAMSTAT` | Pro Baseball Team Stat | Sports | Baseball | `{"scope": "Season Stats"}` | "Pro Baseball"×4 [scopes: "Season Stats"×4] |
| 95 | `KXMLBTEAMTOTAL` | Pro Baseball Team Total | Sports | Baseball | `{"scope": "Team Total"}` | "Pro Baseball"×2057 [scopes: "Team Total"×2057] |
| 96 | `KXMLBTOTAL` | Pro Baseball Total Points | Sports | Baseball | `{"scope": "Point Total"}` | "Pro Baseball"×2159 [scopes: "Point Total"×2159] |
| 97 | `KXMLBTRADE` | Pro Baseball Trades | Sports | Baseball | `null` | "Pro Baseball"×2 [scopes: "Trades"×2] |
| 98 | `KXMLBTRIPLECROWN` | Pro Baseball Triple Crown | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×1 [scopes: "Awards"×1] |
| 99 | `KXMLBWA` | Pro Baseball Walks | Sports | Baseball | `{"important_info": {"id": "KXMLBWA-2026-08-17", "markdown": "**Important information:** If this pitcher does not start the game or starts the game but does not record at least one batter faced, this market will resolve to Fair Market Price.", "message": "", "title": ""}, "scope": "Walks"}` | "Pro Baseball"×234 [scopes: "Walks"×234] |
| 100 | `KXMLBWINS-ATH` | Pro baseball wins A's | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 101 | `KXMLBWINS-ATL` | Pro baseball wins Atlanta | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 102 | `KXMLBWINS-AZ` | Pro baseball wins Arizona | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 103 | `KXMLBWINS-BAL` | Pro baseball wins Baltimore | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 104 | `KXMLBWINS-BOS` | Pro baseball wins Boston | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 105 | `KXMLBWINS-CHC` | Pro baseball wins Chicago C | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 106 | `KXMLBWINS-CIN` | Pro baseball wins Cincinnati | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 107 | `KXMLBWINS-CLE` | Pro baseball wins Cleveland | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 108 | `KXMLBWINS-COL` | Pro baseball wins Colorado | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 109 | `KXMLBWINS-CWS` | Pro baseball wins Chicago W | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 110 | `KXMLBWINS-DET` | Pro baseball wins Detroit | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 111 | `KXMLBWINS-HOU` | Pro baseball wins Houston | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 112 | `KXMLBWINS-KC` | Pro baseball wins Kansas City | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 113 | `KXMLBWINS-LAA` | Pro baseball wins Los Angeles A | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 114 | `KXMLBWINS-LAD` | Pro baseball wins Los Angeles D | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 115 | `KXMLBWINS-MIA` | Pro baseball wins Miami | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 116 | `KXMLBWINS-MIL` | Pro baseball wins Milwaukee | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 117 | `KXMLBWINS-MIN` | Pro baseball wins Minnesota | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 118 | `KXMLBWINS-NYM` | Pro baseball wins New York M | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 119 | `KXMLBWINS-NYY` | Pro baseball wins New York Y | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 120 | `KXMLBWINS-PHI` | Pro baseball wins Philadelphia | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1] |
| 121 | `KXMLBWINS-PIT` | Pro baseball wins Pittsburgh | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 122 | `KXMLBWINS-SD` | Pro baseball wins San Diego | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 123 | `KXMLBWINS-SEA` | Pro baseball wins Seattle | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 124 | `KXMLBWINS-SF` | Pro baseball wins San Francisco | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 125 | `KXMLBWINS-STL` | Pro baseball wins St. Louis | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 126 | `KXMLBWINS-TB` | Pro baseball wins Tampa Bay | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 127 | `KXMLBWINS-TEX` | Pro baseball wins Texas | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 128 | `KXMLBWINS-TOR` | Pro baseball wins Toronto | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 129 | `KXMLBWINS-WSH` | Pro baseball wins Washington | Sports | Baseball | `{"scope": "Wins"}` | "Pro Baseball"×1 [scopes: "Win totals"×1]; null×1 [scopes: null×1] |
| 130 | `KXMLBWORSTRECORD` | Pro Baseball Best Record | Sports | Baseball | `{"important_info": null, "scope": "Future"}` | "Pro Baseball"×1 [scopes: "Future"×1] |
| 131 | `KXMLBWSMVP` | Pro Baseball Championship MVP | Sports | Baseball | `{"scope": "Awards"}` | "Pro Baseball"×1 [scopes: "Awards"×1] |
| 132 | `KXMLBWSTREAK` | Longest Winning Streak | Sports | Baseball | `{"important_info": null}` | "Pro Baseball"×1 [scopes: null×1] |
| 133 | `KXNEXTMANAGERMLB` | Next Manager | Sports | Baseball | `{"scope": "Managers"}` | "Pro Baseball"×3 [scopes: "Managers"×3] |
| 134 | `KXNEXTTEAMMLB` | MLB Player Next Team | Sports | Baseball | `{"scope": "Next Team"}` | "Pro Baseball"×18 [scopes: "Next Team"×12, null×6] |
| 135 | `KXNEXTTEAMSKUBAL` | Tarik Skubal Next Team | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 136 | `KXSALMONRUN` | Seattle Mariners Salmon Run | Sports | Baseball | `null` | "Pro Baseball"×2 [scopes: null×2] |
| 137 | `KXSELLTWINS` | Minnesota Twins sale | Sports | Baseball | `null` | "Pro Baseball"×1 [scopes: null×1] |
| 138 | `KXTEAMSINWS` | Teams in MLB Finals | Sports | Baseball | `{"scope": "Future"}` | "Pro Baseball"×1 [scopes: "Future"×1] |

All rows were retrieved during the census window beginning `2026-09-03T19:54:27.381413+00:00`; per-series retrieval timestamps and the exact endpoint string used for each series are in `research/threadc_universe_v1/universe_v1.json` (fields `retrieved_at` and `endpoints`).

---

*Generated by `scripts/threadc_collection_universe.py` on 2026-09-03. Read-only census; no orders, no writes to any exchange endpoint.*
