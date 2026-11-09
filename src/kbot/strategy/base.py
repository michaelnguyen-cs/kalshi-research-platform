"""
Abstract base class for kbot trading strategies.

A Strategy receives a market snapshot and returns a list of SizingResults.
The execution layer decides whether to act on them (DRY_RUN, edge gate, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kbot.config import Settings
from kbot.models.types import OrderBook, SizingResult


class Strategy(ABC):
    """
    All strategies implement exactly one method: evaluate().

    Parameters passed to evaluate() are read-only snapshots; the strategy
    must not make any API calls directly — it computes signals and returns
    intents, the execution layer acts on them.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique slug used for logging, telemetry, and CLI --strategy flag."""
        ...

    @property
    def cfg(self) -> Settings:
        """
        This strategy's own Settings instance. Exposed (not just an
        internal self._cfg) so kbot.scanners.post_resolution can
        temporarily swap in a discounted-edge cfg for its own evaluate()
        calls without needing a second strategy instance (which would lose
        per-instance state, e.g. BTCHourlyStrategy's floor_strike cache).
        Every subclass in this codebase stores its cfg as self._cfg;
        override this property (and its setter) if that's ever not true.
        """
        return self._cfg  # type: ignore[attr-defined]

    @cfg.setter
    def cfg(self, value: Settings) -> None:
        self._cfg = value  # type: ignore[attr-defined]

    # ── Multi-strategy runner wiring (Phase B2) ─────────────────────────────
    # Both are plain class attributes (not abstract) so existing single-
    # strategy subclasses don't need to change unless they want feed sharing
    # or WS multiplexing -- default is "no series, no shared feeds", which
    # just means the runner won't auto-subscribe or route anything to this
    # strategy until it declares what it needs.

    series_tickers: list[str] = []
    """Kalshi series prefixes (e.g. "KXBTC") this strategy trades. The
    runner unions these across all loaded strategies to build its WS
    subscription list, and uses them (via owns_ticker) to route each
    orderbook update to only the strategies that claim it."""

    required_feeds: list[str] = []
    """Shared external feed names this strategy needs (e.g. "btc_spot").
    The runner instantiates one singleton per named feed and hands the same
    instance to every strategy that declares it, so N strategies sharing a
    feed only poll its upstream API once."""

    spot_feed_name: str | None = None
    """Which of `required_feeds` is this strategy's SPOT price feed -- the one
    whose price/vol the runner passes into evaluate(), and whose ticks fire the
    off-cycle spot trigger. None (the default) means "this strategy has no spot
    underlying"; the runner then passes (None, None) and never re-evaluates it
    on a price tick.

    Named explicitly rather than inferred from required_feeds[0] because the two
    are different questions: a strategy may need several feeds and only one of
    them is the underlying it prices against. Before this existed the runner
    hardcoded "btc_spot" in three places, so any second spot-driven strategy
    would silently receive (None, None) from _feed_inputs_for and skip every
    evaluation with no error -- see kbot.runner.Runner._feed_inputs_for."""

    def vol_model_functions(self):
        """
        (parse_ticker, lognormal_prob, lognormal_range_prob) for this
        strategy's underlying, or None if it has no lognormal pricing model.

        The runner uses these to back-solve ladder-implied vol from THIS
        strategy's own books (kbot.vol.estimate_vol). Supplied by the strategy
        rather than imported by the runner so that each underlying is priced
        with its own ticker grammar -- the runner previously imported
        btc_hourly's parser unconditionally, which would have mis-parsed (and
        so silently mis-priced) any non-BTC series.
        """
        return None

    def owns_ticker(self, ticker: str) -> bool:
        """
        True if `ticker` belongs to one of this strategy's series_tickers
        (matched as a "{series}-" prefix, the standard Kalshi ticker
        format). Override if a strategy's routing logic needs to be more
        precise than a series-prefix match.
        """
        return any(ticker.startswith(f"{series}-") for series in self.series_tickers)

    def is_single_binary(self, ticker: str) -> bool:
        """
        True if `ticker` is a single YES/NO binary market per event (exactly
        one contract, no strike ladder). For such markets, holding YES and NO
        on the SAME ticker is a pointless self-hedge -- the execution layer
        blocks opposite-side re-entry when this is True (see
        kbot.execution.Executor.check_gates).

        Default False: a strategy trading a strike LADDER (e.g. KXBTC hourly's
        B/T markets) has many contracts per expiry, so YES on one strike and
        NO on another are legitimately different positions and must NOT be
        blocked. Override per-ticker in strategies that trade single-binary
        series (e.g. KXBTC15M -- one "up in next 15 min?" market per window).
        """
        return False

    @abstractmethod
    def evaluate(
        self,
        book: OrderBook,
        *,
        btc_price: float | None = None,
        btc_vol: float | None = None,
        bankroll: float,
        current_time: float | None = None,   # Unix ts; defaults to now (live) or row ts (backtest)
    ) -> list[SizingResult]:
        """
        Evaluate the current market state and return sizing intents.

        Parameters
        ----------
        book:
            Current order-book snapshot for the market being evaluated.
        btc_price:
            Most recent BTC/USD spot price (None if unavailable).
        btc_vol:
            Annualised BTC log-return volatility estimate (None if unavailable).
        bankroll:
            Current total balance in dollars (used for position sizing).

        Returns
        -------
        List of SizingResult.  Empty list = no trade this tick.
        contracts == 0 in a result = strategy explicitly skipping with a reason.
        """
        ...

    def is_near_atm(self, ticker: str, spot_price: float, strikes: int = 2) -> bool:
        """
        True if `ticker` is within `strikes` strike-widths of `spot_price`.

        Used by the event-driven spot trigger (paper.py / Source 6) to
        cheaply filter which markets get an immediate off-cycle evaluation
        when spot moves fast, instead of re-evaluating every tracked ticker
        on every trigger. Default: no filtering (every ticker counts as
        "near ATM") — override in strategies that can parse their own
        ticker format into a strike price.
        """
        return True

    def parse_strike(self, ticker: str) -> tuple[float, str, str] | None:
        """
        (strike, direction, group_key) for `ticker`, or None to opt out.

        Used by kbot.scanners.consistency's monotonicity check to group
        same-expiry "above"/"below" contracts into a strike ladder.
        direction must be "above" or "below" ("range" contracts have no
        strict strike ordering and should return None). group_key
        identifies the ladder a contract belongs to (e.g. its expiry).
        Default: no monotonicity checking — override in strategies whose
        ticker format encodes a strike + above/below direction.
        """
        return None

    def ingest_market_metadata(self, ticker: str, metadata: dict) -> None:
        """
        Receive out-of-band market metadata for `ticker` (a raw Kalshi
        market dict -- ticker/floor_strike/cap_strike/strike_type/etc.).

        Some series don't encode everything evaluate() needs in the ticker
        string itself (e.g. KXBTC15M's floor_strike is a dynamic reference
        price set at window-open, not part of the ticker -- see
        kbot.strategy.btc_hourly's module docstring). For those, the
        caller (kbot.runner.Runner during ticker discovery, or
        kbot.backtest.run_backtest replaying a CSV's floor_strike column)
        calls this once metadata becomes available so evaluate() has
        somewhere to look it up. Default: no-op -- override in strategies
        that need external metadata; evaluate() must never assume this was
        called (metadata may arrive late or not at all; skip rather than
        guess).
        """
        return None

    def expiry_ts(self, ticker: str) -> float | None:
        """
        Unix timestamp `ticker` expires/settles at, or None if unknown or
        this strategy doesn't track expiries generically.

        Used by kbot.scanners.post_resolution to detect a market
        transitioning from open to settled (wall-clock time crossing its
        own known expiry) without depending on an unverified WS lifecycle
        message format -- every strategy already computes an exact expiry
        while parsing its own ticker format, so exposing it here is enough.
        Default: no-op (opt-in) -- override in strategies with a
        deterministic, ticker-derivable expiry (e.g. kbot.strategy.btc_hourly).
        """
        return None

    def vol_window_seconds(self, ticker: str) -> float:
        """
        Trailing window (seconds) the EWMA/Parkinson vol estimator should
        use for `ticker`.

        2026-07: this MUST match the contract's own horizon. A strategy
        spanning series with materially different contract durations
        (e.g. KXBTC's 60-minute markets and KXBTC15M's 15-minute ones,
        both traded by kbot.strategy.btc_hourly.BTCHourlyStrategy) needs a
        DIFFERENT window per ticker, not one fixed value -- confirmed:
        using the 60-minute-appropriate 3600s window for a 15-minute
        contract overestimated its vol by ~2x (median implied/estimated
        ratio 0.488), compressing fair_p toward 0.5 and producing a
        severe, systematic calibration miss. Default: 3600.0 (1 hour) --
        override per-ticker in strategies spanning multiple horizons.
        """
        return 3600.0

    def vol_multiplier(self, ticker: str) -> float | None:
        """
        Series-specific override for cfg.vol_multiplier (the realized-vol
        fallback's scaling factor -- see kbot.vol.fallback_vol), or None
        to use cfg.vol_multiplier unmodified.

        Default: None. Override in strategies that need a per-series
        value once vol_window_seconds also varies by ticker (e.g.
        KXBTC15M's cfg.vol_multiplier_15m, kept as an independent knob
        from KXBTC's cfg.vol_multiplier since the two series' fallback
        vol no longer shares one estimation window).
        """
        return None

    def vol_floor(self, ticker: str) -> float | None:
        """
        Series-specific override for cfg.vol_floor (the realized-vol
        fallback's lower bound -- see kbot.vol.fallback_vol), or None to
        use cfg.vol_floor unmodified.

        2026-08: this is the third member of the series-scoped vol trio and
        the one that was left global. A vol FLOOR is only a degeneracy guard
        while it sits BELOW the low tail of the estimator it guards, and that
        tail is horizon-specific: cfg.vol_floor = 0.35 is under KXBTC's 3600s
        distribution but inside the body of KXBTC15M's 900s one, where it
        bound on the large majority of evaluations. A binding floor is not a
        guard -- it IS the estimate, and a pinned sigma pins
        fair_p = Phi(ln(S/K)/(sigma*sqrt(T))) at ~0.5 on every contract.

        Default: None. Override wherever vol_window_seconds already varies by
        ticker -- the two must move together (e.g. cfg.vol_floor_15m).
        """
        return None
