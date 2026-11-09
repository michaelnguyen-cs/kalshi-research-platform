# KALSHI MARKET RESEARCH — KNOWLEDGE BASE & PLAN REVISIONS
Compiled July 2026. Drop this in the repo root as MARKET_RESEARCH.md so every future
Claude Code session builds with this context.

═══════════════════════════════════════════════════════════════
SOURCE 1 — Bürgi, Deng & Whelan (CEPR/UCD, 300,000+ contracts)
"Makers and Takers: The Economics of the Kalshi Prediction Market"
═══════════════════════════════════════════════════════════════
FINDINGS
• Kalshi prices are informative and get MORE accurate approaching close.
• Strong favorite–longshot bias: contracts under 10¢ lose buyers ~60%+ of their
  money; contracts above 50¢ earn small positive returns.
• Takers lose ~32% on average; Makers lose ~10% on average. Maker >> Taker.
• The authors explicitly note these anomalies may DECAY now that they are published.
• IMPORTANT SCOPE NOTE: the study EXCLUDED hourly markets (crypto/index hourlies)
  and only analyzed markets open 24h+. Our BTC hourly niche is comparatively
  UNSTUDIED — plausibly less efficient, plausibly more bot-infested. Unknown.

BOT IMPLICATIONS
1. Hard rule: never TAKER-buy contracts under $0.15 (see also Source 4). Our
   3-sigma far-OTM filter partially covers this; add an explicit price floor.
2. Structural tilt: prefer the favorite side (higher-priced contract) when model
   edge is symmetric — the bias pays the favorite buyer.
3. Maker-first execution (Phase A1) is validated by the strongest dataset that
   exists. Keep it.
4. Track edge decay over time — published anomalies shrink. The learning loop's
   evaluate reports should trend maker-vs-taker realized returns.

═══════════════════════════════════════════════════════════════
SOURCE 2 — Diercks, Katz & Wright (Federal Reserve / NBER w34702)
"Kalshi and the Rise of Macro Markets"
═══════════════════════════════════════════════════════════════
FINDINGS
• Kalshi macro markets (CPI, GDP, unemployment, Fed funds) are as accurate as or
  better than Bloomberg consensus, Fed surveys, and fed funds futures — and update
  in real time.
• The MODE of Kalshi's Fed-funds distribution has perfectly matched the realized
  rate at every meeting since 2022 — beating surveys AND futures.
• Caveat the Fed itself flagged: low-probability (tail) contracts have thin volume,
  i.e., the tails are where calibration is weakest.

BOT IMPLICATIONS
1. Sobering: the CENTER of macro markets is efficient — "beat the Kalshi CPI
   consensus with a nowcast" is mostly a losing thesis, because Kalshi IS the
   best nowcast. Revise C2's framing.
2. The exploitable part of macro markets is the TAILS (thin, poorly calibrated)
   and the POST-RESOLUTION repricing window (adjacent contracts lag 15–30 min
   after a related market resolves — e.g., after an FOMC decision, the
   next-meeting and inflation-tracker contracts reprice slowly).
3. But per Source 1, tails = sub-10¢ contracts = fee death zone as taker. The
   ONLY sane way to trade tails is as a MAKER selling overpriced longshots
   (equivalently: providing NO liquidity at 90¢+), never buying them.

═══════════════════════════════════════════════════════════════
SOURCE 3 — Palumbo (March 2026), NFL moneyline microstructure
[uploaded paper: trade-level data, full 18-week NFL season]
═══════════════════════════════════════════════════════════════
FINDINGS
• Passive liquidity providers (LPs) on Kalshi sports markets are NOT classical
  spread-capturing market makers. They accumulate net directional exposure that
  SURVIVES TO SETTLEMENT — structurally closer to UNDERWRITING (like a sportsbook
  or insurer) than to intermediation.
• Aggregate LP profit across the NFL season ≈ $29M, but weekly P&L was volatile
  with significant drawdown weeks. LPs win in expectation, not every week.
• Regression (R²≈0.59): LP profitability correlates with (a) winning-side trade
  volume, (b) flow imbalance ratio (highly significant), (c) average price
  discipline. Profit comes from PRICING AND MANAGING IMBALANCE, not neutrality.
• No underlying spot asset exists → no mechanical hedge. Whatever exposure you
  accumulate, you carry to resolution unless you trade out.
• Sustained two-sided passive sell liquidity implies some participants operate
  under special collateral arrangements — i.e., the pros on the other side are
  NOT retail-constrained.

BOT IMPLICATIONS
1. If we rest maker orders (we now do), we are a small underwriter. Expect to
   carry directional exposure to resolution. This is fine ONLY with pricing
   discipline (our fair-value model) and imbalance management.
2. Our adverse-selection markout monitor (Phase A2) is exactly the imbalance-
   management instrument the paper says separates winning LPs from losing ones.
   Weight it heavily.
3. Sports maker-side competition includes institutional players (Susquehanna,
   DL Trading) with special arrangements. Retail sports MAKING at scale is
   their game. Our sports strategy (C3) should stay TAKER-style momentum on
   live-game mispricings, small size, not standing liquidity provision.

═══════════════════════════════════════════════════════════════
SOURCE 4 — MAXIMUS / North Lake Labs postmortem (Feb 2026)
"What I Learned Losing Money on Kalshi Weather Markets" — 0 WINS, 32 LOSSES
Running nearly the EXACT strategy we specced as C1.
═══════════════════════════════════════════════════════════════
THE THREE STRUCTURAL FAILURES (verbatim lessons)
1. GAUSSIAN BLINDNESS. He assumed NWS forecast errors are normally distributed.
   They are FAT-TAILED: real "2-sigma" temperature misses occur ~10–12% of the
   time, not 5%. His "90% certainty" was really 75–80%. Fix: build EMPIRICAL
   error distributions per city per lead-time from years of NWS forecast-vs-
   actual data. Never assume a distribution shape you haven't verified.
2. FEE DEATH ZONE. Flat per-contract fees are a ~20% tax on a 5¢ contract. A 5¢
   buy needs >83% true win rate just to break even before slippage. Rule:
   never trade contracts below ~$0.15.
3. EXIT LIQUIDITY. Weather arb bots execute within SECONDS of each NWS model
   cycle release. His 15–60 min polling meant every "edge" he saw was already
   priced — he was buying the leftovers from faster bots. If professional arb
   infrastructure already trades your signal, you are confirming their edge,
   not finding yours.

HIS VALIDATION CHECKLIST (adopt wholesale)
a. Validate distributional assumptions against historical outcomes FIRST.
b. Simulate fees per price tier before any trade: what win rate breaks even?
c. Paper-test calibration: say "90%" 30 times → did ~27 happen?
d. Ask who else trades this signal. Existing pro infrastructure = no edge.

HIS PIVOT — BASE-RATE DIVERGENCE (adopt as a strategy)
Retail-dominated markets (FOMC, CPI, jobs, political) systematically misprice
against HISTORICAL BASE RATES because retail anchors on narrative/news/recency.
Example: Fed hold/cut base rates over the last N meetings vs. a market pricing
in narrative-driven uncertainty. Decades of clean data, no millisecond race,
and these contracts trade at fee-friendly prices (20–80¢).

ALSO FROM SAME SERIES: his bot later lost $35 on tennis via an unattended
autonomous sports trade → his conclusion: allowlists + kill switches for any
autonomous execution. We have these; keep them strict.

BOT IMPLICATIONS
1. WEATHER (C1) IS DEMOTED, NOT DELETED. It only proceeds if we do what he
   didn't: (a) empirical per-city error distributions from historical NWS
   verification data, (b) ≥$0.15 price floor, (c) event-driven trading keyed to
   NWS model-cycle release times (approx. 4x daily) instead of interval polling
   — trade AT the release when we can be early, stand down otherwise, and
   (d) maker-only entries. If we can't satisfy all four, weather stays parked.
2. NEW STRATEGY C5 — BASE-RATE DIVERGENCE: for FOMC/CPI/jobs markets, compute
   empirical base rates from decades of history, compare to market price,
   trade the divergence at fee-friendly price points. Slow (per-release), data-
   tractable, no speed race. This becomes the SECOND strategy to gate, ahead
   of weather.
3. KILL-FAST RULE (new, portfolio-wide): any strategy that goes 0-for-10 on
   resolved trades, or whose realized P&L is below the 5th percentile of its
   own backtest distribution over the first 20 resolved trades, is AUTO-PAUSED
   (gate demoted, alert fired, human review required). Codify "kill at 10, not
   32."

═══════════════════════════════════════════════════════════════
SOURCE 5 — Practitioner/industry corroboration (secondary sources)
═══════════════════════════════════════════════════════════════
• Post-resolution repricing: first 15–30 min after a major related resolution
  (e.g., FOMC) is edge-rich as adjacent contracts recalibrate slowly.
• Evening windows (7–10 PM ET): thinner volume, wider spreads, slower price
  adjustment — better maker entries for modeled markets.
• Volume Incentive Program (through Sep 1, 2026): ~$0.005/contract cashback on
  3–97¢ trades + daily liquidity payouts ($10–$1,000) for resting limit orders.
  Our maker executor should be capturing this — VERIFY enrollment in account.
• Liquidity filter: prefer markets with meaningful open interest; avoid books
  where our own order is a large fraction of displayed size.
• Weather calibration data (Lychee): Kalshi weather extremes are actually well
  calibrated at 0–10% and 90–100% — consistent with arb bots keeping the tails
  honest — reinforcing that naive weather edge-hunting is crowded.

═══════════════════════════════════════════════════════════════
REVISED STRATEGY PRIORITY (supersedes Part 3 ordering)
═══════════════════════════════════════════════════════════════
1. BTC hourly (running) — keep proving. Bonus: academically unstudied niche.
2. C5 Base-rate divergence (FOMC/CPI/jobs) — NEW, build next. Slow, data-rich,
   fee-friendly prices, no speed race, matches our infra perfectly.
3. Post-resolution repricing module — event-driven: when a tracked market
   resolves, scan adjacent contracts in the same series/event for 30 min with
   loosened polling and normal edge gates.
4. Sports live momentum (C3) — taker-style, small, strict exits. NOT standing
   liquidity provision (that's Susquehanna's/DL Trading's game per Source 3).
5. Weather (C1) — parked until the four preconditions above are met.
6. Cross-platform arb — scanner/alert only (unchanged).

═══════════════════════════════════════════════════════════════
NEW PORTFOLIO-WIDE RULES (add to code, not just docs)
═══════════════════════════════════════════════════════════════
R1. Taker price floor: never taker-buy below $0.15. (edge.py hard gate)
R2. Favorite tilt: on symmetric edge, prefer the higher-priced side. (strategy)
R3. Kill-fast: auto-pause any strategy 0-for-10 resolved, alert + human review.
    (gates.py)
R4. Fee-tier simulator: backtests must report break-even win rate per price
    bucket actually traded. (backtest.py report section)
R5. Calibration honesty check: evaluate must compare stated confidence buckets
    to realized frequencies and FLAG any bucket off by >10pp. (already close —
    make the flag explicit.)
R6. Crowding check (qualitative, per new strategy): before gating any strategy
    to paper, document who else trades the signal and why we're not their exit
    liquidity. One paragraph in the gate registry notes field, mandatory.
R7. Distribution verification: any strategy using a parametric distribution
    (lognormal, normal) must ship a validation script comparing it to empirical
    outcomes, run during backtest gate. BTC included — check our lognormal tails
    against realized hourly BTC moves.

═══════════════════════════════════════════════════════════════
SOURCE 6 — Crypto hourly/15-min microstructure (practitioner repos + industry)
═══════════════════════════════════════════════════════════════
FINDINGS
• THE documented structural edge in our exact niche: Binance spot BTC updates
  ~every 200ms via WebSocket; Kalshi crypto contracts reprice with a 3–7 SECOND
  lag relative to spot moves. During that window contracts are mispriced vs.
  true implied probability. (brandononchain/kalshibot documents this explicitly
  on the KXBTC15M 15-minute series.)
• KXBTC15M exists: rolling 15-minute BTC direction contracts — even faster
  feedback loop than hourlies, continuously created.
• Competition is real and growing: ~$40M extracted from prediction-market
  arbitrage Apr 2024–Apr 2025 ("Unravelling the Probabilistic Forest", Aug 2025);
  14 of the top 20 Polymarket leaderboard wallets are bots. Most automated
  profit comes from STRUCTURAL edges (latency, YES+NO<$1, cross-platform,
  logical mismatches between related contracts) rather than better prediction.

BOT IMPLICATIONS
1. Our BTC strategy's real edge source is now named: the 3–7s repricing lag.
   Our loop should react to Binance/Coinbase spot moves EVENT-DRIVEN (on tick),
   not on a fixed 5s evaluation cycle — a 5s cycle can miss most of a 3–7s
   window. Add: trigger immediate re-evaluation of near-ATM markets whenever
   spot moves more than X bps within N seconds.
2. Consider KXBTC15M as a second crypto series (same model, faster cycle,
   more resolutions/day = faster calibration data accumulation). Higher
   fee-drag sensitivity — model it first.
3. Add an intra-Kalshi consistency scanner (cheap, pure math): YES+NO < $1
   on the same market after fees, and logical mismatches between related
   contracts (e.g., "above 60k" priced below "above 61k"). These are the
   structural freebies bots harvest.

═══════════════════════════════════════════════════════════════
SOURCE 7 — Official fee schedule (verified June 29, 2026 PDF + secondary)
═══════════════════════════════════════════════════════════════
CONFIRMED NUMBERS
• Taker fee = 0.07 × C × P × (1−P), rounded UP to the cent (max 1.75¢/contract
  at 50¢). Round-trip taker at 50¢ = 3.5¢/contract.
• Maker fee ≈ 25% of taker (coefficient ~0.0175) WHERE maker fees apply —
  but MOST standard markets currently charge 0% maker fee. Special events
  (NFL championship, elections, etc.) charge a flat 0.25% maker fee.
• Worked example from Market Math: a 4¢ edge at 50¢ loses 44% of its edge to
  taker fees vs. only ~11% as maker. Maker-vs-taker IS the profitability line.
• Kalshi pays 3.75–4% APY on idle cash balances (partially offsets fee drag;
  free carry on undeployed bankroll).
• Fee overpayments from rounding are reimbursed monthly only if > $10.

BOT IMPLICATIONS
1. edge.py verified correct: taker coefficient 0.07 with ceil ✓; maker
   coefficient should be 0.0175 on fee-charging markets and 0.0 on standard
   markets — make it PER-MARKET (read from the market/fee metadata when
   available, config fallback), not a single global constant.
2. Special-event maker fee (0.25% flat) can be 12–50% of price on sub-5¢
   contracts — one more reason the sub-15¢ taker floor should extend to
   maker orders during special events.
3. Idle-cash APY means an un-deployed bankroll isn't dead capital — do not
   force trades to "put money to work." The math already says this; the APY
   pays you to be patient.

═══════════════════════════════════════════════════════════════
SOURCE 8 — Historical data may exist after all (VERIFIED IN CODE, 2026-07)
═══════════════════════════════════════════════════════════════
• A June 2026 industry survey states Kalshi now provides "candlestick and
  historical-data endpoints" plus public "Market Data & Insights" pages with
  historical exchange data (tickers, open interest, daily volume).
• Our earlier probe of /markets/{ticker}/candlesticks returned 404 — but that
  was the wrong path/params: the endpoint is SERIES-scoped, not market-scoped.
• CONFIRMED: the installed kalshi_python SDK (>=2.0, already a project
  dependency) exposes MarketsApi.get_market_candlesticks(), which serializes
  to:
      GET /series/{series_ticker}/markets/{market_ticker}/candlesticks
      query params: start_ts, end_ts, period_interval (e.g. "1m"/"5m"/"1h"/"1d")
      auth: bearerAuth (credentials required)
  Response: GetMarketCandlesticksResponse.candlesticks, each a Candlestick
  with start_ts, end_ts, open, high, low, close, volume. Price fields are a
  Union[float, int] in the SDK's pydantic model — not dollar strings — so
  they're very likely the historical integer-cents convention Kalshi read
  endpoints have long used. UNVERIFIED against a live authenticated pull as
  of this writing (no credentials available in the dev sandbox this was
  checked from) — the first live run of scripts/pull_history.py must sanity
  check the actual scale before trusting the /100 conversion it applies.
• DONE: added KalshiClient.get_candlesticks(series_ticker, market_ticker,
  start_ts=, end_ts=, period_interval=) in kbot/kalshi_client.py, and
  scripts/pull_history.py, which downloads KXBTC market candlesticks +
  backfills BTC/USD spot history from Coinbase's public candles endpoint to
  produce a kbot.backtest-format CSV. This unlocks BACKTESTING ON ACTUAL
  KALSHI PRICES — eliminating the synthetic-ask problem that was the biggest
  honesty gap in the whole backtest pipeline. Run it and re-run backtests
  before trusting any P&L number that previously came from synthetic data.

═══════════════════════════════════════════════════════════════
FINAL GAP LIST — VERIFY EMPIRICALLY, NOT VIA MORE READING
═══════════════════════════════════════════════════════════════
V1. Candlestick/historical endpoints — check docs + probe API (Source 8).
V2. Maker fee status per series we trade (KXBTC*, KXBTC15M, weather, econ) —
    read from fee schedule page/market metadata; set per-market coefficient.
V3. Volume Incentive Program enrollment + eligibility of our order flow.
V4. KXBTC15M liquidity/spread profile vs hourly — observe before modeling.
V5. Our own markout data (Phase A2) — the only way to know if the 3–7s lag
    edge is still alive for OUR fills, or already harvested by faster bots.
