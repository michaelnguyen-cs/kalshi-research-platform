"""
Typed configuration loaded from environment / .env file.
All sensitive fields are optional at import time so `kbot markets`
(no keys needed) still works without a .env present.
"""

from functools import cached_property
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_strategy_allocations(raw: str) -> dict[str, float]:
    """
    Parse "name:fraction,name:fraction,..." into {name: fraction}.

    Raises ValueError (with a specific reason) on: a malformed entry, a
    non-positive or non-numeric fraction, a duplicate name, or fractions
    summing to more than 1.0 (a small epsilon tolerates float rounding).
    Used both by Settings' own validator (fail fast on bad config) and by
    kbot.runner to build the actual strategy/allocation map.
    """
    allocations: dict[str, float] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(f"strategy_allocations entry {entry!r} must be 'name:fraction'")
        name, _, fraction_str = entry.partition(":")
        name = name.strip()
        if not name:
            raise ValueError(f"strategy_allocations entry {entry!r} has an empty name")
        try:
            fraction = float(fraction_str.strip())
        except ValueError as exc:
            raise ValueError(f"strategy_allocations entry {entry!r} has a non-numeric fraction") from exc
        if fraction <= 0:
            raise ValueError(f"strategy_allocations entry {entry!r} must have fraction > 0")
        if name in allocations:
            raise ValueError(f"strategy_allocations lists {name!r} more than once")
        allocations[name] = fraction

    if not allocations:
        raise ValueError("strategy_allocations must list at least one strategy")
    total = sum(allocations.values())
    if total > 1.0 + 1e-9:
        raise ValueError(f"strategy_allocations fractions sum to {total:.4f}, must be <= 1.0")
    return allocations


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Credentials ──────────────────────────────────────────────────────────
    kalshi_key_id: str = Field(default="")
    kalshi_private_key_path: str = Field(default="./kalshi_private_key.pem")
    # "demo" or "prod"
    kalshi_env: str = Field(default="demo")

    # ── Safety ───────────────────────────────────────────────────────────────
    # DRY_RUN defaults to True; real orders require explicit --live + confirmation gauntlet.
    dry_run: bool = Field(default=True)
    # 2026-07: a strategy demoted to gate level "backtest" (paused, pending
    # human review after a kill-fast trigger or a mixed validation result)
    # would otherwise also be blocked from running in PaperTrader -- but
    # paper trading is always DRY_RUN (enforced in PaperTrader.__init__) and
    # is exactly how forward evidence for re-promotion gets gathered. The
    # gate pause should still block LIVE promotion; it should not block
    # dry-run paper evaluation. Default True; set False to fully halt a
    # paused strategy everywhere, including paper.
    paused_allows_dry_run: bool = Field(default=True)

    # ── Strategy thresholds ──────────────────────────────────────────────────
    # Minimum net edge per contract in dollars (after fees + assumed slippage).
    min_net_edge: float = Field(default=0.04)
    # Fraction of full Kelly to size with. 0.25 = quarter-Kelly.
    kelly_fraction: float = Field(default=0.25)
    # Do not open new positions in the final N minutes before expiry.
    no_trade_final_minutes: int = Field(default=5)

    # 2026-07: bankroll used as the Kelly-sizing reference in paper/dry-run
    # mode ONLY. When None (the default), sizing uses the real Kalshi account
    # balance. Set this to size paper trades against a realistic notional
    # bankroll instead -- a tiny demo balance (e.g. $12) rounds nearly every
    # Kelly size down to 0 contracts, starving paper trading of the fills it
    # needs to build a track record. NEVER applies in live mode (dry_run
    # False): real capital is always sized against the real balance.
    paper_bankroll_override: float | None = Field(default=None)

    # ── Position / exposure caps ─────────────────────────────────────────────
    # Max fraction of bankroll allocated to any single position.
    max_position_pct: float = Field(default=0.05)
    # Max fraction of bankroll across all open positions combined.
    max_total_exposure_pct: float = Field(default=0.40)
    # Hard ceiling on contracts per order regardless of sizing model.
    max_contracts_per_order: int = Field(default=100)

    # ── Risk management ──────────────────────────────────────────────────────
    # Kill-switch threshold: halt and exit when daily realized loss reaches this
    # fraction of start-of-day bankroll. No override flag.
    daily_loss_limit_pct: float = Field(default=0.10)

    # ── Rate limits ──────────────────────────────────────────────────────────
    # Requests per second; tune to your Kalshi API tier (Basic = 20 read / 10 write).
    rate_limit_reads: int = Field(default=20)
    rate_limit_writes: int = Field(default=10)

    # ── Fee model ────────────────────────────────────────────────────────────
    # Fee coefficient in Kalshi's formula: fee = ceil(coeff * C * P * (1-P)).
    # The 0.07 default applies to most taker orders; maker/special markets differ.
    # ALWAYS verify against the current published fee schedule before trusting.
    fee_coefficient: float = Field(default=0.07)
    # Maker (resting-order) fee coefficient, same formula. Kalshi does not charge
    # fees on most resting orders — ALWAYS verify against the current fee schedule.
    fee_coefficient_maker: float = Field(default=0.0)
    # Conservative assumed slippage in dollars per contract.
    assumed_slippage: float = Field(default=0.005)

    # ── Maker execution ──────────────────────────────────────────────────────
    # "taker" = always cross the spread. "maker" = always rest at bid+0.01 when
    # the spread allows it. "auto" = rest when spread >= 2 cents, else cross.
    execution_mode: str = Field(default="auto")
    # Cancel any resting maker order older than this many seconds.
    maker_timeout_seconds: int = Field(default=120)
    # Max number of cancel+reprice cycles per signal before giving up.
    max_reprices: int = Field(default=3)

    # ── Adverse selection monitor ────────────────────────────────────────────
    # Number of most-recent fills (per strategy) the rolling markout mean is
    # computed over.  Also the minimum sample size before the flag can trip.
    adverse_selection_window: int = Field(default=30)
    # When a strategy's adverse_selection_detected flag is set, its
    # min_net_edge is multiplied by this before the edge gate is checked.
    adverse_selection_edge_multiplier: float = Field(default=1.5)

    # ── Portfolio / event-driver correlation caps ────────────────────────────
    # Max fraction of bankroll any single event_driver bucket (crypto_price,
    # us_macro, weather_{city}, sports_game:{id}, ...) may hold in open
    # positions at once. Halved for drivers listed in DRIVER_IMPACT_MAP while
    # a matching research advisory flag is set.
    driver_cap_pct: float = Field(default=0.25)

    # ── Phase A4 — research-driven rules ─────────────────────────────────────
    # RULE R1 (MARKET_RESEARCH.md Source 1/4/7): never allow a TAKER entry
    # priced below this. Sub-15c contracts are a structural fee-death zone —
    # a flat/near-flat fee is a huge percentage tax on a tiny stake, and the
    # favorite-longshot bias means longshot buyers lose money on average.
    taker_price_floor: float = Field(default=0.15)
    # Default per-contract maker fee coefficient (fee = coeff * P * (1-P)),
    # used for markets with no per-series override (see edge.py
    # MAKER_FEE_SPECIAL_SERIES). Most standard markets charge 0% maker fee.
    maker_fee_default: float = Field(default=0.0)

    # 2026-07 calibration audit (real KXBTC candlestick backtest): the
    # btc_hourly lognormal model is systematically overconfident far from
    # the money -- 0-15c contracts won 1.9% of the time against an 8%
    # breakeven requirement, while near-ATM 30-50c contracts won 60%. When
    # True, btc_hourly only evaluates markets whose model fair_p (YES side)
    # falls in [near_atm_fair_p_min, near_atm_fair_p_max].
    near_atm_only: bool = Field(default=True)
    near_atm_fair_p_min: float = Field(default=0.25)
    near_atm_fair_p_max: float = Field(default=0.75)

    # 2026-07 low-bucket miscalibration fix (see btc_hourly._size_side). After
    # the WebSocket BTC feed (Fix 4, deployed 2026-07-22) began firing far more
    # off-cycle spot-trigger evaluations on transient tick-level moves, the
    # low-confidence calibration buckets rotted: trades the model predicted to
    # win 30-50% realized only 14-31%, dragging kxbtc15m's win rate 57% ->
    # 49.7%, while the 50%+ buckets stayed well-calibrated. This floors the
    # TRADED SIDE's *calibrated* win probability -- checked per-side in
    # _size_side, on exactly the quantity the calibration buckets are built
    # from -- so raising it excises the miscalibrated sub-floor region without
    # touching the profitable 50%+ one. The near_atm band can't do this: it
    # gates the whole market on the YES-side model prob, not the traded side.
    # 0.0 disables the floor (pre-2026-07 behavior).
    min_side_win_prob: float = Field(default=0.50)

    # 2026-07 vol-calibration fix. See kbot.vol for the full story: our
    # realized-vol estimator understated market-implied vol by a median of
    # 2.2x across 312 honest backtest trades, inflating RANGE contracts'
    # claimed stay-in-bucket probability. VOL_MULTIPLIER/VOL_FLOOR set the
    # realized-vol fallback (used when the live strike ladder can't be
    # fit); values are anchored to CME BTC 25-delta IV (46-95% in 2026),
    # not tuned against this dataset.
    vol_multiplier: float = Field(default=1.5)
    vol_floor: float = Field(default=0.35)

    # 2026-08 vol-floor horizon fix. VOL_FLOOR above (0.35) was chosen as a
    # conservative *under-bound* on CME BTC implied vol quoted at an HOURLY
    # (and longer) horizon, and it sits comfortably below KXBTC's own 3600s
    # realized-vol distribution -- it is a degeneracy guard there, not a
    # forecast. Applied unchanged to KXBTC15M's 900s window it stopped being a
    # guard and became THE ESTIMATE: measured on kbot_events.db, max(EWMA,
    # 0.35) returned the FLOOR on the large majority of KXBTC15M evaluations
    # (see scripts/replay_pricing_fixes.py, section 1). A binding floor pins
    # sigma high, which shrinks |d| = |ln(S/K)| / (sigma*sqrt(T)) and drives
    # fair_p to ~0.5 on every contract -- the mechanism behind the "fair_p is
    # a constant, so net_edge is just (const - price)" finding.
    #
    # vol_floor is therefore the third member of the series-scoped vol trio
    # (vol_window_seconds_*, vol_multiplier_*) and was the one left global.
    # This is its 15-minute counterpart, plumbed identically via
    # Strategy.vol_floor(ticker) -> kbot.vol.fallback_vol(floor=...).
    #
    # NOT tuned to any P&L or win-rate outcome. The value is the smaller of:
    #   (a) 0.10 annualised -- a first-principles degeneracy bound of the same
    #       character as kbot.vol._SIGMA_LO (0.05): at 15 minutes it implies a
    #       terminal sd of 0.10 * sqrt(900/31_557_600) = 0.053% of spot, ~$32
    #       at BTC 60k. BTC does not spend 15 minutes inside a $32 one-sigma
    #       band outside a stalled feed, so this can only fire on a degenerate
    #       (cold / flat / gapped) EWMA -- exactly what a floor is for; and
    #   (b) the 5th percentile of MEASURED 900s realized vol, rounded down to
    #       0.01 (scripts/measure_basis_and_vol.py, section 2) -- so the floor
    #       provably sits under the low tail of the estimator's own empirical
    #       distribution rather than inside its body.
    # Re-run that script and lower this value if (b) ever comes in below (a).
    vol_floor_15m: float = Field(default=0.10)

    # 2026-08 settlement-object fix. Kalshi's crypto contracts do NOT settle
    # on the spot price at one instant: they settle on a SETTLEMENT_AVG_SECONDS
    # average of CF Benchmarks BRTI over the final seconds of the window. A
    # time-averaged terminal value has strictly less variance than a point
    # value, so pricing it with the point-value variance sigma^2*T overstates
    # uncertainty and (again) pushes fair_p toward 0.5.
    #
    # For driftless BM in log-price the average over the final tau seconds has
    # variance sigma^2 * (T - 2*tau/3), i.e. an EFFECTIVE time to expiry of
    # T - (2/3)*tau -- see kbot.strategy.btc_hourly.effective_t_years for the
    # derivation, the assumptions, and the T < tau regime where it breaks.
    # 0.0 disables the correction (point-settlement pricing, pre-2026-08).
    settlement_avg_seconds: float = Field(default=60.0)

    # 2026-07 series-aware vol window: VOL_MULTIPLIER above was tuned for
    # KXBTC's 60-minute contracts. Applying it to KXBTC15M's 15-minute
    # contracts (with a matched, shorter EWMA/Parkinson window -- see
    # kbot.strategy.btc_hourly.vol_window_seconds) overestimated their vol
    # by a further ~2x on top of that window mismatch; kept as a SEPARATE
    # knob (default 1.0 -- no multiplier) rather than repurposing
    # VOL_MULTIPLIER, so hourly and 15m can be tuned independently if the
    # window fix alone isn't sufficient. Not tuned against the KXBTC15M
    # backtest that motivated it -- one honest value, tested, reported.
    vol_multiplier_15m: float = Field(default=1.0)

    # The EWMA/Parkinson vol estimator's trailing window must match the
    # contract's own horizon -- root cause of the KXBTC15M calibration
    # miss (a mismatched 3600s window overestimated 15-minute vol by
    # ~2x). KXBTC (60-minute contracts): 3600s. KXBTC15M (15-minute
    # contracts): 900s. See kbot.strategy.btc_hourly.vol_window_seconds.
    vol_window_seconds_hourly: float = Field(default=3600.0)
    vol_window_seconds_15m: float = Field(default=900.0)

    # 2026-07: a degenerate early-session estimate (thin history, EWMA not
    # yet warmed up) can produce sigma far above any realistic BTC vol --
    # observed vol=1.84 (184% annualised) still producing signals. Cap at
    # 150% annualised (well above CME's observed 46-95% 25-delta IV) and log
    # a warning rather than silently trusting an implausible number into the
    # pricing model.
    vol_sanity_cap: float = Field(default=1.5)

    # MARKET_RESEARCH.md item 3 / Source 5: after a major market resolves,
    # adjacent contracts in the same series often lag in repricing for
    # 15-30 min -- a structural lag edge, not a forecast. See
    # kbot.scanners.post_resolution. WINDOW_MINUTES is how long the faster
    # 1s eval cadence + discounted edge gate stay active per settlement;
    # EDGE_DISCOUNT is the fraction shaved off min_net_edge during that
    # window (0.30 = 30% lower bar -- edges appear and disappear faster
    # while the market is actively repricing).
    post_resolution_window_minutes: float = Field(default=30.0)
    post_resolution_edge_discount: float = Field(default=0.30)

    # Two-sided extension of RULE R1's taker price floor: contracts priced
    # above tradeable_band_high are settlement-zone noise just like ones
    # below tradeable_band_low (the existing taker_price_floor covers the
    # low side already; this adds the high side and is checked as one band
    # in btc_hourly._size_side).
    tradeable_band_low: float = Field(default=0.15)
    tradeable_band_high: float = Field(default=0.85)

    # 2026-07 expiry-cutoff finding: bucketing the 312 honest-backtest
    # trades by hours-to-expiry showed win rate INCREASING as expiry
    # approaches (>12h: 15.5%, 6-12h: 13.6%, 2-6h: 24.2%, 0-2h: 36.0%) --
    # the opposite of the "settlement noise near expiry" assumption that
    # motivated this cutoff. The data says the far-from-expiry regime is
    # where understated vol does the most damage (more time for spot to
    # wander before the market's own pricing catches up), not the close-to-
    # expiry one. So this blocks NEW entries more than this many hours from
    # expiry, not within it -- see MARKET_RESEARCH.md for the full writeup
    # before assuming the name means what RULE-R1-style cutoffs usually mean.
    max_hours_to_expiry_for_entry: float = Field(default=6.0)

    # ── C5 base-rate divergence (MARKET_RESEARCH.md Source 4, MAXIMUS pivot) ──
    # Slow, calendar-driven entries days before a scheduled FOMC/CPI/jobs
    # release -- the opposite of btc_hourly's speed-sensitive repricing-lag
    # edge. No millisecond race; the edge is decades of empirical base rate
    # vs. retail-anchored market price.
    base_rate_entry_days: int = Field(default=7)
    # Stand down this close to the actual release -- once the print is
    # imminent the market price stops reflecting base rate at all and starts
    # reflecting last-minute positioning/leaks; there's no edge left to trade.
    base_rate_stand_down_minutes: int = Field(default=30)

    # ── Phase B2 multi-strategy runner ────────────────────────────────────────
    # "name:fraction,name:fraction,..." -- fraction of total bankroll each
    # strategy's Kelly sizing is computed against (portfolio-level driver
    # caps in risk_portfolio.py still apply on top, against the FULL
    # bankroll). Parsed/validated by parse_strategy_allocations(); kept as a
    # plain string field (not a dict) so it round-trips cleanly through
    # .env / environment variables like every other setting here.
    # 2026-07: default is kxbtc15m (BTC 15-minute), the only series with a
    # backtest-validated edge (via its bootstrapped isotonic calibrator).
    # btc_hourly was retired -- its lognormal model has no discriminating
    # signal on 60-minute contracts (see its gate note); it stays loadable
    # for research but is no longer the live default.
    strategy_allocations: str = Field(default="kxbtc15m:1.0")

    # ── Per-deployment database paths ─────────────────────────────────────────
    # Every persistent store this process writes, as an overridable path. The
    # defaults reproduce the historical hardcoded kbot_*.db-in-CWD names, so an
    # existing BTC deployment is byte-for-byte unaffected.
    #
    # These exist so a SECOND deployment (the KXETH15M service) can run against
    # a completely separate set of files by setting four env vars, with no
    # shared state whatsoever. That separation is not cosmetic:
    # kbot.learning.retrain.run_champion_challenger fits ONE global champion
    # calibrator over load_outcomes(telemetry) -- ALL outcomes in the events DB,
    # unfiltered by strategy. Two crypto strategies sharing an events DB would
    # therefore fit the "KXBTC15M" champion on BTC+ETH trades mixed, silently
    # corrupting the calibrator that gates BTC's live sizing. The gates DB is
    # likewise per-deployment so each strategy earns its own backtest -> paper
    # promotion clock against its own trade history.
    #
    # kbot.runner.Runner additionally REFUSES to load two strategies that each
    # own a calibrator domain into one process (see _assert_isolated_domains) --
    # the env vars separate the files, that check separates the processes.
    events_db_path: str = Field(default="kbot_events.db")
    gates_db_path: str = Field(default="kbot_gates.db")
    registry_db_path: str = Field(default="kbot_registry.db")
    positions_db_path: str = Field(default="kbot_positions.db")

    # The other two files a running service writes. Both were plain relative
    # paths resolved against the process cwd, which made co-tenancy on one host
    # depend entirely on the two services never sharing a working directory:
    #
    #   heartbeat_path  Written every _HEARTBEAT_CYCLES by the runner and read
    #                   back by its OWN dead-man's switch. Two services sharing
    #                   this file is the worst of the collisions available: each
    #                   process keeps the other's switch permanently satisfied
    #                   (so a genuine freeze is never caught), while `kbot
    #                   status` reports whichever wrote last. Silent in both
    #                   directions -- no error, no log line.
    #   log_path        Rotating file log. Sharing it interleaves two services'
    #                   lines and makes them share one 100MB x 8 rotation
    #                   budget, so the busier service evicts the other's history.
    #
    # Defaults are byte-identical to the previously hardcoded values, so a
    # deployment with no override behaves exactly as before.
    heartbeat_path: str = Field(default="kbot_heartbeat.json")
    log_path: str = Field(default="logs/paper.log")

    # RULE (Source 6): Kalshi crypto contracts reprice with a 3-7s lag after
    # spot moves. A spot move >= this many basis points within a 3s window
    # fires an immediate off-cycle evaluation of near-ATM markets instead of
    # waiting for the next fixed-interval tick (see paper.py / feeds/btc.py).
    spot_trigger_bps: float = Field(default=10.0)
    # BTC feed poll interval in seconds. Target 1-2s data freshness so the
    # spot trigger above can actually catch the 3-7s repricing window;
    # BTCFeed enforces a >= 1.0s floor to stay well under Coinbase's public
    # rate limit.
    btc_poll_interval_seconds: float = Field(default=2.0)
    # ETH feed poll interval. Separate field from btc_poll_interval_seconds
    # (rather than one shared "spot poll interval") so the two deployments can
    # be tuned independently -- ETH-USD prints at a different rate than
    # BTC-USD, and the ETH service is a separate process with its own .env.
    # ETHFeed enforces the same >= 1.0s floor.
    eth_poll_interval_seconds: float = Field(default=2.0)
    # 2026-07: stream BTC ticks over the Coinbase Exchange WS feed alongside
    # the REST poll (which stays on as heartbeat/fallback). The poll alone
    # burned up to poll_interval + request latency (~2.5s) of the 3-7s Kalshi
    # repricing window before the strategy saw a move; ticks cut that to
    # socket latency. Set False to fall back to poll-only -- e.g. if the
    # socket is unreachable from a given host.
    btc_ws_enabled: bool = Field(default=True)

    # 2026-07: how often the runner re-discovers open markets for each
    # strategy's series and subscribes to any newly-opened windows (and
    # prunes expired ones) on the live WS connection WITHOUT reconnecting.
    # Essential for fast-rolling series like KXBTC15M, whose 15-minute
    # windows open continuously -- a once-at-startup subscription would
    # trade only the first window then idle. Must be well under the window
    # length so a new window is caught within its first few minutes (600s
    # vs KXBTC15M's 900s window). 0 disables periodic re-discovery (the
    # startup subscription is then the whole session, pre-2026-07 behavior).
    ticker_rediscovery_interval_seconds: float = Field(default=600.0)

    # ── Google Sheets ─────────────────────────────────────────────────────────
    google_service_account_json: str = Field(default="./service_account.json")
    kalshi_sheet_id: str = Field(default="<GOOGLE_SHEET_ID>")

    # ── Research agent ────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="")
    research_interval_hours: int = Field(default=6)
    research_model: str = Field(default="claude-sonnet-4-6")

    # ── Self-learning loop ────────────────────────────────────────────────────
    learn_eval_interval_hours: int = Field(default=24)
    # Challenger must beat champion net-of-fees on validation by at least this.
    promotion_min_improvement: float = Field(default=0.0)
    # Minimum resolved trades before isotonic calibration is attempted.
    calibration_min_samples: int = Field(default=50)
    # Allowed range for kelly_fraction tuning — never widened by a win streak.
    kelly_fraction_min: float = Field(default=0.10)
    kelly_fraction_max: float = Field(default=0.35)

    # ── Alerts (optional) ────────────────────────────────────────────────────
    discord_webhook_url: str = Field(default="")
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    alert_email: str = Field(default="")

    # ── Validators ───────────────────────────────────────────────────────────

    @field_validator("kalshi_env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("demo", "prod"):
            raise ValueError("KALSHI_ENV must be 'demo' or 'prod'")
        return v

    @field_validator(
        "kelly_fraction", "max_position_pct", "max_total_exposure_pct",
        "driver_cap_pct", "taker_price_floor",
    )
    @classmethod
    def validate_fractions(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError("Fraction must be in (0, 1]")
        return v

    @field_validator("near_atm_fair_p_min", "near_atm_fair_p_max")
    @classmethod
    def validate_near_atm_bounds(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("near_atm_fair_p_min/max must be in [0, 1]")
        return v

    @field_validator("min_side_win_prob")
    @classmethod
    def validate_min_side_win_prob(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("min_side_win_prob must be in [0, 1] (0.0 disables the floor)")
        return v

    @model_validator(mode="after")
    def validate_near_atm_band(self) -> "Settings":
        if self.near_atm_fair_p_min >= self.near_atm_fair_p_max:
            raise ValueError("near_atm_fair_p_min must be < near_atm_fair_p_max")
        return self

    @field_validator("tradeable_band_low", "tradeable_band_high")
    @classmethod
    def validate_tradeable_band_bounds(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("tradeable_band_low/high must be in [0, 1]")
        return v

    @model_validator(mode="after")
    def validate_tradeable_band(self) -> "Settings":
        if self.tradeable_band_low >= self.tradeable_band_high:
            raise ValueError("tradeable_band_low must be < tradeable_band_high")
        return self

    @field_validator(
        "vol_multiplier", "vol_multiplier_15m", "vol_floor", "vol_floor_15m",
        "settlement_avg_seconds", "max_hours_to_expiry_for_entry",
        "base_rate_stand_down_minutes",
    )
    @classmethod
    def validate_non_negative(cls, v: float) -> float:
        if v < 0.0:
            raise ValueError(
                "vol_multiplier, vol_multiplier_15m, vol_floor, vol_floor_15m, "
                "settlement_avg_seconds, max_hours_to_expiry_for_entry, and "
                "base_rate_stand_down_minutes must be >= 0"
            )
        return v

    @field_validator("vol_sanity_cap")
    @classmethod
    def validate_vol_sanity_cap(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError("vol_sanity_cap must be > 0")
        return v

    @field_validator("vol_window_seconds_hourly", "vol_window_seconds_15m")
    @classmethod
    def validate_vol_window_seconds(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError("vol_window_seconds_hourly and vol_window_seconds_15m must be > 0")
        return v

    @field_validator("post_resolution_window_minutes")
    @classmethod
    def validate_post_resolution_window_minutes(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError("post_resolution_window_minutes must be > 0")
        return v

    @field_validator("post_resolution_edge_discount")
    @classmethod
    def validate_post_resolution_edge_discount(cls, v: float) -> float:
        if not (0.0 <= v < 1.0):
            raise ValueError("post_resolution_edge_discount must be in [0.0, 1.0)")
        return v

    @field_validator("base_rate_entry_days")
    @classmethod
    def validate_base_rate_entry_days(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("base_rate_entry_days must be > 0")
        return v

    @field_validator("strategy_allocations")
    @classmethod
    def validate_strategy_allocations(cls, v: str) -> str:
        parse_strategy_allocations(v)  # raises ValueError on anything malformed
        return v

    @field_validator("execution_mode")
    @classmethod
    def validate_execution_mode(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("taker", "maker", "auto"):
            raise ValueError("EXECUTION_MODE must be 'taker', 'maker', or 'auto'")
        return v

    @field_validator("btc_poll_interval_seconds")
    @classmethod
    def validate_btc_poll_interval(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("BTC_POLL_INTERVAL_SECONDS must be >= 1.0 (BTCFeed's own floor)")
        return v

    @field_validator("eth_poll_interval_seconds")
    @classmethod
    def validate_eth_poll_interval(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("ETH_POLL_INTERVAL_SECONDS must be >= 1.0 (ETHFeed's own floor)")
        return v

    @field_validator("ticker_rediscovery_interval_seconds")
    @classmethod
    def validate_ticker_rediscovery_interval(cls, v: float) -> float:
        if v < 0.0:
            raise ValueError("TICKER_REDISCOVERY_INTERVAL_SECONDS must be >= 0 (0 disables re-discovery)")
        return v

    @field_validator("paper_bankroll_override")
    @classmethod
    def validate_paper_bankroll_override(cls, v: float | None) -> float | None:
        if v is not None and v <= 0.0:
            raise ValueError("PAPER_BANKROLL_OVERRIDE, when set, must be > 0")
        return v

    @model_validator(mode="after")
    def warn_if_live_setup_incomplete(self) -> "Settings":
        if self.kalshi_env == "prod" and not self.dry_run:
            # Not an error at config load — execution.py enforces the full gauntlet.
            pass
        return self

    # ── Computed properties ───────────────────────────────────────────────────

    @cached_property
    def base_url(self) -> str:
        if self.kalshi_env == "prod":
            return "https://external-api.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"

    @cached_property
    def ws_url(self) -> str:
        if self.kalshi_env == "prod":
            return "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
        return "wss://demo-api.kalshi.co/trade-api/ws/v2"

    @cached_property
    def private_key_path(self) -> Path:
        return Path(self.kalshi_private_key_path).expanduser().resolve()

    def require_credentials(self) -> None:
        """Raise if credentials are not configured (call before any authed request)."""
        if not self.kalshi_key_id:
            raise RuntimeError(
                "KALSHI_KEY_ID is not set. Add it to .env or the environment."
            )
        if not self.private_key_path.exists():
            raise RuntimeError(
                f"Private key not found at {self.private_key_path}. "
                "Set KALSHI_PRIVATE_KEY_PATH in .env."
            )


# Module-level singleton — import this everywhere.
settings = Settings()
