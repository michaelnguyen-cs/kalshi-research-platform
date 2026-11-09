"""
BTC Hourly + BTC 15-Minute reference strategy.

Fair-value model: driftless lognormal terminal distribution.

Real Kalshi KXBTC (hourly) ticker format (verified against live API):
  KXBTC-YYMONDDHH-[T|B]STRIKE
  e.g.  KXBTC-26JUL0317-T48000
        KXBTC-26JUL0317-B59750
        KXBTC-26JUL0317-T71999.99

  • YY     : 2-digit year (26 → 2026)
  • MON    : 3-letter month (JUL)
  • DD     : 2-digit day (03)
  • HH     : 2-digit hour in US Eastern Time (17 → 21:00 UTC during EDT)
  • T[N]   : catch-all LOW bucket  — YES if BTC ≤ N-0.01 at settlement
  • B[N]   : $500-wide range bucket — YES if BTC in [N-250, N+249.99]
  • T[N.99]: catch-all HIGH bucket  — YES if BTC ≥ N+0.01 (N always ends in .99)

Real Kalshi KXBTC15M (15-minute) ticker format (verified 2026-07 against the
live API, get_events(series_ticker="KXBTC15M") + individual market records
-- see scripts/pull_history.py history / MARKET_RESEARCH.md Source 6 for the
discovery notes):
  KXBTC15M-YYMONDDHHMM-MM
  e.g.  KXBTC15M-26JUL060115-15
        KXBTC15M-26JUL070000-00

  • YYMONDDHHMM : the window's CLOSE time (i.e. settlement moment), in US
    Eastern local time, to the minute. MM is always :00/:15/:30/:45.
  • trailing -MM: redundant suffix, repeats the same minute value.
  • Exactly ONE binary market per event/window (no T/B ladder): the
    market record's strike_type is always "greater_or_equal", with
    floor_strike = the BTC reference price observed at the window's OPEN
    (== the settlement price -- expiration_value -- of the PRECEDING 15m
    window; the reference chains window to window). Title: "BTC price up
    in next 15 mins?" -- YES if BTC (CF Benchmarks BRTI average) at close
    >= floor_strike.
  • CRITICAL: floor_strike is NOT encoded in the ticker string (unlike
    hourly's strike-in-ticker convention) -- it's a dynamic value fixed
    only once the window opens, and is only available via the market's
    own metadata (get_events/get_markets), never derivable from the
    ticker alone. _parse_ticker() returns strike=None for KXBTC15M
    tickers; evaluate() resolves it from self._strike_cache, populated
    out-of-band via ingest_market_metadata() (called by kbot.runner.Runner
    during ticker discovery, live, and by kbot.backtest.run_backtest from
    a CSV's floor_strike column, in backtests) -- a market whose strike
    hasn't been ingested yet is skipped, never guessed.
  • BEWARE: the market record's own `expiration_time`/`latest_expiration_time`
    fields are NOT the settlement time -- they're a ~7-day-out "no later
    than" SLA deadline. The real settlement moment is `close_time`
    (matches the ticker exactly, converted ET->UTC) -- verified directly:
    ticker KXBTC15M-26JUL060115-15 has close_time 2026-07-06T05:15:00Z
    (01:15 EDT) but expiration_time 2026-07-13T05:15:00Z (a week later).

Fair probability formulas:
  T (low, direction="below") : P = N(-d)     where d = ln(S/K)/(σ√T)
  T (high, direction="above"): P = N(d)
  B (range, direction="range"): P = N(d_low) - N(d_high)
                                where d = ln(S/K)/(σ√T) for each bound
  KXBTC15M (direction="above"): same as T-high, K = the ingested floor_strike.

SETTLEMENT IS AN AVERAGE, NOT A POINT (2026-08).  Every formula above prices
P(spot at ONE instant [direction] strike).  These contracts settle on CF
Benchmarks BRTI averaged over the final cfg.settlement_avg_seconds (60s) of
the window.  A time-average is strictly less variable than a point value, so
the point-variance sigma^2*T overstates uncertainty and drags fair_p toward
0.5.  Corrected by substituting T -> effective_t_years(T, tau) = T - (2/3)*tau
everywhere -- see effective_t_years for the derivation, the bounded error from
BRTI's real (non-uniform) weighting, and the T < tau regime where the
correction is variance-correct but drift-wrong.

IMPORTANT CAVEATS:
  • No drift term — risk-neutral pricing assumed.
  • Constant vol; realised vol is stochastic.
  • Range width 250 hardcoded; verify against live market subtitles.
  • S is a COINBASE print; K is a BRTI-derived floor_strike.  Any persistent
    Coinbase-vs-index basis enters ln(S/K) undivided and, near the money,
    maps into fair_p at roughly phi(0)/(sigma*sqrt(T_eff)) per dollar -- at
    15 minutes and sigma = 0.20 that is about 0.5 cents of probability per
    dollar of basis.  Measured with scripts/measure_basis_and_vol.py; no
    correction is applied unless that measurement says one is warranted.
  • Calibrate against live paper-trading before risking real money.  A
    calibrator fitted BEFORE the 2026-08 vol-floor / settlement fixes was fit
    against a fair_p that was pinned near 0.5, so it learned to amplify;
    re-applying it to the corrected (already-spread) fair_p double-counts.
    Refit or clear it before trusting post-fix probabilities.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import structlog
from scipy.stats import norm  # type: ignore[import]

from kbot.config import Settings, settings as _default_settings
from kbot.edge import half_spread_dollars
from kbot.learning.calibration import apply_calibrator
from kbot.models.types import OrderBook, Side, SizingResult
from kbot.strategy.base import Strategy

log = structlog.get_logger(__name__)

_ET = ZoneInfo("America/New_York")

# Matches: KXBTC-26JUL0317-T48000   (integer or decimal strike)
#          KXBTC-26JUL0317-B59750
#          KXBTC-26JUL0317-T71999.99
_TICKER_RE = re.compile(
    r"^KXBTC-(\d{2})([A-Z]{3})(\d{2})(\d{2})-([TB])(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
# Matches: KXBTC15M-26JUL060115-15  (close-time-to-the-minute + redundant suffix)
_TICKER_15M_RE = re.compile(
    r"^KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-(\d{2})$",
    re.IGNORECASE,
)
_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_RANGE_HALF_WIDTH = 250.0   # B-market bracket is midpoint ± $250 (verified live)
_SIGMA_CUTOFF: float = 3.0  # reject markets more than N lognormal σ from spot


def _is_far_otm(
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
) -> bool:
    """
    Return True if the strike is more than _SIGMA_CUTOFF standard deviations
    from spot under the lognormal model.

    The log-scale std-dev of spot at expiry is σ√T, so the test is:
        |ln(spot / strike)| > _SIGMA_CUTOFF × σ × √T

    When σ ≤ 0 or T ≤ 0 the model is undefined → always return True so the
    caller skips the market rather than emitting a degenerate fair_p = 0.5 signal.
    """
    if sigma <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return True
    return abs(math.log(spot / strike)) > _SIGMA_CUTOFF * sigma * math.sqrt(t_years)


def _parse_ticker(ticker: str) -> tuple[float | None, float, str] | None:
    """
    Return (strike_usd, expiry_unix_utc, direction) or None if unparseable.

    strike_usd is None for KXBTC15M tickers -- their floor_strike is a
    dynamic per-window value not encoded in the ticker string at all (see
    this module's docstring); callers must resolve it externally (see
    BTCHourlyStrategy._strike_cache / ingest_market_metadata) before
    pricing. KXBTC (hourly) tickers always return a real float strike,
    unchanged from before KXBTC15M support was added.

    direction is one of: "above" | "below" | "range"
      "below" → T low-catch-all  (YES if BTC < strike)
      "above" → T high-catch-all (YES if BTC ≥ strike), or KXBTC15M
                (YES if BTC ≥ floor_strike at close)
      "range" → B bracket        (YES if BTC in [strike-250, strike+250))
    """
    m15 = _TICKER_15M_RE.match(ticker)
    if m15:
        yy, mon_str, day_str, hour_str, minute_str, _suffix = m15.groups()
        expiry_ts = _et_to_utc_ts(yy, mon_str, day_str, hour_str, minute_str)
        if expiry_ts is None:
            return None
        return None, expiry_ts, "above"

    m = _TICKER_RE.match(ticker)
    if not m:
        return None

    yy, mon_str, day_str, hour_str, mkt_type, strike_str = m.groups()
    expiry_ts = _et_to_utc_ts(yy, mon_str, day_str, hour_str, "0")
    if expiry_ts is None:
        return None

    mkt_type = mkt_type.upper()

    if mkt_type == "B":
        # Range bracket: midpoint is the strike; full range = [mid-250, mid+250)
        return float(strike_str), expiry_ts, "range"

    # T markets: low-catch-all has integer strike; high-catch-all has decimal (.99)
    if "." in strike_str:
        # High-catch-all: T71999.99 → YES if BTC ≥ 72000
        strike = math.ceil(float(strike_str))   # 71999.99 → 72000
        return float(strike), expiry_ts, "above"
    else:
        # Low-catch-all: T48000 → YES if BTC < 48000
        return float(strike_str), expiry_ts, "below"


def _et_to_utc_ts(yy: str, mon_str: str, day_str: str, hour_str: str, minute_str: str) -> float | None:
    """US-Eastern-local (year, month, day, hour, minute) -> Unix UTC seconds, or None if invalid."""
    month = _MONTH_ABBR.get(mon_str.upper())
    if month is None:
        return None
    try:
        year = 2000 + int(yy)
        day = int(day_str)
        hour_et = int(hour_str)
        minute_et = int(minute_str)
        dt_et = datetime(year, month, day, hour_et, minute_et, tzinfo=_ET)
        return dt_et.timestamp()
    except (ValueError, OverflowError):
        return None


_SECONDS_PER_YEAR = 365.25 * 24 * 3600


def effective_t_years(t_years: float, avg_seconds: float) -> float:
    """
    Variance-equivalent time to expiry for a contract that settles on a
    TIME AVERAGE of the underlying rather than on its value at one instant.

    2026-08 settlement-object fix. Kalshi's crypto markets settle on CF
    Benchmarks BRTI averaged over the final `avg_seconds` (60s) of the
    window; _lognormal_prob prices P(spot at ONE instant >= strike). An
    average is less variable than a point, so pricing the average with the
    point variance sigma^2*T overstates uncertainty and pushes fair_p toward
    0.5 -- the same direction as the vol floor, which is why the two compound.

    DERIVATION.  Let X_t = ln S_t be driftless arithmetic Brownian motion with
    variance rate sigma^2 per year, T the time to close, tau the averaging
    window (tau <= T).  Settlement in log space is

        Ybar = (1/tau) * integral_{T-tau}^{T} X_u du
             = X_{T-tau} + (1/tau) * integral_0^tau W_s ds

    where W is the increment process started at 0 at time T-tau, independent
    of X_{T-tau}.  Then

        Var(X_{T-tau})                       = sigma^2 * (T - tau)
        Var((1/tau) * integral_0^tau W_s ds) = sigma^2 * tau / 3
        Cov(the two)                         = 0        (independent increments)

    (the tau/3 is the standard result Var(integral_0^tau W_s ds) = tau^3/3,
    divided by tau^2).  Adding them:

        Var(Ybar) = sigma^2 * (T - tau + tau/3) = sigma^2 * (T - (2/3)*tau)

    so the whole correction is a substitution T -> T_eff = T - (2/3)*tau.
    The VARIANCE reduction factor is (T - 2*tau/3)/T and the SIGMA reduction
    factor is sqrt(1 - 2*tau/(3*T)).  With tau = 60s:

        T = 1 hour  ->  T_eff = 3560s/3600s,  sigma factor 0.994  (-0.6%)
        T = 15 min  ->  T_eff =  860s/ 900s,  sigma factor 0.978  (-2.2%)
        T = 10 min  ->  T_eff =  560s/ 600s,  sigma factor 0.966  (-3.4%)
        T =  5 min  ->  T_eff =  260s/ 300s,  sigma factor 0.931  (-6.9%)
        T =  2 min  ->  T_eff =   80s/ 120s,  sigma factor 0.816  (-18%)

    ASSUMPTIONS, and what they cost:
      (i)   Driftless BM in log-price with sigma constant across the window.
            The same assumption the pricer already makes everywhere else; over
            a 60-second averaging window it is if anything safer here.
      (ii)  The settlement average is a UNIFORM continuous-time average over
            the final tau seconds. BRTI's real weighting is a proprietary
            depth- and time-weighted scheme, not a flat mean. The error is
            BOUNDED, not open-ended: any weighting has variance between the
            uniform case (tau/3) and the point case (tau, all weight at the
            close), so the true T_eff lies in [T - (2/3)*tau, T]. A weighting
            concentrated nearer the close moves T_eff toward T -- this
            correction is the far end of that bracket and can overstate the
            reduction by at most (2/3)*tau = 40 seconds of variance.
      (iii) ln(average of S) ~= average of (ln S). The gap is O(sigma^2*tau);
            at sigma = 0.35 and tau = 60s that is ~2e-7 in log terms, about a
            hundredth of a cent on BTC at 60k. Negligible.
      (iv)  tau <= T.

    WHERE IT BREAKS -- very short time-to-expiry.  For T < tau the averaging
    window has ALREADY OPENED: part of the settlement average is realized and
    observed, and only the remaining T seconds are random. The variance of the
    unrealized part is sigma^2 * T^3 / (3*tau^2), which is what this function
    returns on that branch (continuous with the main branch at T = tau, where
    both give tau/3). But the MEAN is wrong there too: once inside the window
    the correct anchor is a blend of the already-realized partial average and
    current spot, and this pricer only has current spot -- so the T < tau
    branch is variance-correct and drift-WRONG, and must not be traded on. The
    naive T - (2/3)*tau formula is worse still: it goes non-positive at
    T = (2/3)*tau = 40s, which would hand the caller sigma*sqrt(T_eff) = 0 and
    therefore fair_p of exactly 0 or 1 -- unbounded fake edge.

    In practice cfg.no_trade_final_minutes (default 5 min) already blocks every
    entry with T < 300s = 5*tau, well clear of the regime; the branch exists so
    that a mis-set config degrades into "slightly wrong" instead of "sigma is
    zero, fair_p is 0 or 1, and the claimed edge is maximal".

    avg_seconds <= 0 disables the correction and returns t_years unchanged.
    """
    if t_years <= 0:
        return 0.0
    if avg_seconds <= 0:
        return t_years
    tau = avg_seconds / _SECONDS_PER_YEAR
    if t_years <= tau:
        # Inside the averaging window -- variance of the unrealized remainder
        # only. Drift/anchor NOT corrected; see "WHERE IT BREAKS" above.
        return (t_years ** 3) / (3.0 * tau * tau)
    return t_years - (2.0 / 3.0) * tau


def _lognormal_prob(
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
    direction: str,
    *,
    avg_seconds: float = 0.0,
) -> float:
    """
    P(BTC [direction] strike) under driftless lognormal at expiry.

    `avg_seconds` is the settlement averaging window (see effective_t_years).
    Default 0.0 = price the value at ONE instant, the pre-2026-08 behaviour;
    live callers pass cfg.settlement_avg_seconds.
    """
    t_years = effective_t_years(t_years, avg_seconds)
    if t_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.5

    d = math.log(spot / strike) / (sigma * math.sqrt(t_years))
    p_above = float(norm.cdf(d))
    return p_above if direction == "above" else 1.0 - p_above


def _lognormal_range_prob(
    spot: float,
    lower: float,
    upper: float,
    sigma: float,
    t_years: float,
    *,
    avg_seconds: float = 0.0,
) -> float:
    """
    P(lower ≤ BTC < upper) under driftless lognormal at expiry.

    `avg_seconds` is the settlement averaging window (see effective_t_years);
    0.0 = point settlement, the pre-2026-08 behaviour.
    """
    t_years = effective_t_years(t_years, avg_seconds)
    if t_years <= 0 or sigma <= 0 or spot <= 0 or lower <= 0 or upper <= lower:
        return 0.0
    # P(S_T > lower) - P(S_T > upper) = P(lower < S_T < upper)
    p_above_lower = float(norm.cdf(math.log(spot / lower) / (sigma * math.sqrt(t_years))))
    p_above_upper = float(norm.cdf(math.log(spot / upper) / (sigma * math.sqrt(t_years))))
    return max(0.0, p_above_lower - p_above_upper)


class BTCHourlyStrategy(Strategy):
    """
    Driftless lognormal fair-value strategy for Kalshi KXBTC hourly markets.

    Evaluates all three market types (T low, B range, T high) and buys
    whichever side has model probability exceeding the ask by >= min_net_edge
    (net of fees and assumed slippage), sized via fractional Kelly.
    """

    name = "btc_hourly"
    series_tickers = ["KXBTC", "KXBTC15M"]
    required_feeds = ["btc_spot"]

    # ── Underlying identity ───────────────────────────────────────────────────
    # Everything below that is specific to BTC-as-an-underlying is named here
    # rather than hardcoded inline, so a sibling 15-minute strategy on another
    # coin (see kbot.strategy.eth_15m.ETH15MStrategy) inherits _size_side,
    # evaluate, the calibrator wiring, and the sizing path UNCHANGED and
    # overrides only these four attributes plus _parse.
    #
    # This matters concretely: _size_side carries the min_side_win_prob floor
    # and the raw_fair_p plumbing (both 2026-07 fixes). Subclassing keeps those
    # single-sourced; a copy-pasted sibling class would fork them on day one and
    # silently miss every future fix.
    spot_feed_name: str | None = "btc_spot"
    series_15m: str = "KXBTC15M"
    series_hourly: str | None = "KXBTC"

    def _parse(self, ticker: str) -> tuple[float | None, float, str] | None:
        """
        Instance-level indirection over the module's _parse_ticker so a
        subclass can supply its own ticker grammar without reimplementing
        any caller. Every method in this class parses through here, never
        through the module function directly.
        """
        return _parse_ticker(ticker)

    def _is_15m(self, ticker: str) -> bool:
        """True if `ticker` belongs to this strategy's 15-minute series."""
        return ticker.startswith(f"{self.series_15m}-")

    def vol_model_functions(self):
        """This underlying's pricing trio for the runner's ladder-implied vol
        fit (see Strategy.vol_model_functions). Routes through self._parse, so
        a subclass with its own ticker grammar is fit with ITS parser."""
        return (self._parse, _lognormal_prob, _lognormal_range_prob)

    def __init__(
        self,
        cfg: Settings | None = None,
        series_tickers: list[str] | None = None,
        name: str | None = None,
    ) -> None:
        self._cfg = cfg or _default_settings
        if series_tickers is not None:
            self.series_tickers = series_tickers
        # Instance-level name override (default: the class attr "btc_hourly").
        # Lets the SAME class run as a distinctly-gated strategy per series --
        # e.g. a KXBTC15M-only instance registered under name "kxbtc15m" so the
        # runner/PaperTrader route its gate, kill-fast, and telemetry to the
        # "kxbtc15m" gate entry, separate from "btc_hourly" (2026-07: hourly
        # retired for having no discriminating signal; 15m runs live on its own
        # bootstrapped calibrator). The base `name` is a plain class attr, not
        # a property, so this instance attr cleanly shadows it.
        if name is not None:
            self.name = name
        # ticker -> floor_strike, populated out-of-band via
        # ingest_market_metadata() for series whose strike isn't ticker-
        # encoded (KXBTC15M -- see this module's docstring). KXBTC never
        # needs this (its strike comes straight from the ticker string).
        self._strike_cache: dict[str, float] = {}
        # series -> fitted isotonic calibrator (see kbot.learning.calibration).
        # Applied per-side to the raw lognormal fair_p in _size_side. Keyed by
        # SERIES, never global: KXBTC15M and KXBTC have materially different
        # miscalibration profiles (2026-07 audit), so a calibrator fit on one
        # series must NEVER be applied to the other -- an empty/missing entry
        # just means "use the raw model prob for this series" (apply_calibrator
        # is a no-op on None). Populated by set_calibrator(), which the live
        # loader (cli._load_btc_hourly) calls from the model registry.
        self._calibrators: dict[str, object] = {}

    def ingest_market_metadata(self, ticker: str, metadata: dict) -> None:
        """Cache floor_strike from a raw Kalshi market dict, if present."""
        floor_strike = metadata.get("floor_strike")
        if floor_strike is not None:
            try:
                self._strike_cache[ticker] = float(floor_strike)
            except (TypeError, ValueError):
                pass

    def _resolve_strike(self, ticker: str, strike: float | None) -> float | None:
        """strike if already known from the ticker, else a cached ingest_market_metadata() value."""
        return strike if strike is not None else self._strike_cache.get(ticker)

    def _series_of(self, ticker: str) -> str:
        """
        The calibrator-scoping series for `ticker`: series_15m for a
        15-minute ticker, else series_hourly. Prefix order matters --
        "KXBTC15M-" also starts with "KXBTC-", so the 15m test comes first.

        A strategy with no hourly counterpart (series_hourly=None, e.g. the
        ETH 15-minute strategy) reports its 15m series for everything it
        owns -- it never sees a non-15m ticker, and returning the 15m series
        keeps the calibrator lookup correctly scoped either way.
        """
        if self._is_15m(ticker):
            return self.series_15m
        return self.series_hourly or self.series_15m

    def set_calibrator(self, calibrator: object, *, series: str) -> None:
        """
        Install (or clear, with None) the fitted isotonic calibrator for one
        SERIES. Applied per-side to the raw lognormal fair_p in _size_side.

        Scoped by series on purpose: KXBTC15M and KXBTC are the same strategy
        class but have distinct miscalibration profiles (2026-07 audit), so
        each carries its own calibrator and one is never applied to the other.
        The live loader (kbot.cli._load_btc_hourly) calls this with the
        champion calibrator from the model registry on startup; new resolved
        live trades refine it via the champion/challenger retrain flow.
        """
        self._calibrators[series] = calibrator

    def _calibrator_for(self, ticker: str):
        """Fitted calibrator for `ticker`'s series, or None (raw model prob)."""
        return self._calibrators.get(self._series_of(ticker))

    def is_near_atm(self, ticker: str, spot_price: float, strikes: int = 2) -> bool:
        """
        Within `strikes` bracket-widths ($500 each, matching the B-market
        spacing) of spot. Unparseable tickers are never filtered out — an
        evaluate() call on them is cheap and evaluate() itself will reject
        them via _parse_ticker() / _is_far_otm() anyway. A KXBTC15M ticker
        whose floor_strike hasn't been ingested yet is treated the same
        way (not yet filterable -- evaluate() will cheaply skip it).
        """
        parsed = self._parse(ticker)
        if parsed is None:
            return True
        raw_strike, _expiry_ts, _direction = parsed
        strike = self._resolve_strike(ticker, raw_strike)
        if strike is None:
            return True
        return abs(spot_price - strike) <= strikes * (_RANGE_HALF_WIDTH * 2)

    def parse_strike(self, ticker: str) -> tuple[float, str, str] | None:
        """
        (strike, direction, group_key) for the consistency scanner's
        monotonicity check (kbot.scanners.consistency). group_key is the
        market's expiry timestamp, grouping same-expiry T contracts into
        one strike ladder. "range" (B) markets have no above/below ladder
        and are excluded. KXBTC15M markets are single-market events (no
        ladder to check monotonicity against) and are excluded too --
        both because direction alone doesn't distinguish them from hourly's
        T-high and because an unresolved (cache-miss) strike can't build a
        valid ladder entry anyway.
        """
        parsed = self._parse(ticker)
        if parsed is None:
            return None
        raw_strike, expiry_ts, direction = parsed
        if direction not in ("above", "below") or raw_strike is None:
            return None
        return raw_strike, direction, str(expiry_ts)

    def expiry_ts(self, ticker: str) -> float | None:
        """Unix timestamp `ticker` settles at (works for both KXBTC and KXBTC15M -- neither needs the strike to be known)."""
        parsed = self._parse(ticker)
        if parsed is None:
            return None
        _strike, expiry_ts, _direction = parsed
        return expiry_ts

    def vol_window_seconds(self, ticker: str) -> float:
        """
        2026-07 series-aware vol fix: KXBTC15M's 15-minute contracts need
        a much shorter trailing vol-estimation window than KXBTC's
        60-minute ones -- using the hourly window for 15m contracts
        overestimated their vol by ~2x (see this module's docstring / the
        2026-07 calibration diagnosis). Matched to each series' own cfg
        field so both are independently tunable.
        """
        if self._is_15m(ticker):
            return self._cfg.vol_window_seconds_15m
        return self._cfg.vol_window_seconds_hourly

    def vol_multiplier(self, ticker: str) -> float | None:
        """The 15-minute series uses its own fallback-vol multiplier (default 1.0 -- the window fix alone is the intended fix); the hourly series keeps cfg.vol_multiplier unmodified (return None)."""
        if self._is_15m(ticker):
            return self._cfg.vol_multiplier_15m
        return None

    def vol_floor(self, ticker: str) -> float | None:
        """
        2026-08: the 15-minute series uses its own fallback-vol FLOOR.

        cfg.vol_floor (0.35) is an under-bound on hourly-horizon implied vol
        and a genuine degeneracy guard for KXBTC's 3600s estimator. On
        KXBTC15M's 900s estimator the same number sits inside the body of the
        distribution and BOUND on the large majority of evaluations -- so
        sigma was the constant 0.35, |d| = |ln(S/K)|/(sigma*sqrt(T)) was
        crushed, and fair_p degenerated to a near-constant ~0.5. See
        cfg.vol_floor_15m for how its value is derived (it is not tuned to any
        outcome) and kbot.vol.fallback_vol for where it is applied.

        Returns None for the hourly series -- cfg.vol_floor unmodified.
        """
        if self._is_15m(ticker):
            return self._cfg.vol_floor_15m
        return None

    def is_single_binary(self, ticker: str) -> bool:
        """
        KXBTC15M is one binary market per event ("BTC up in next 15 min?" --
        a single YES/NO), so holding both sides of the same ticker is a
        pointless self-hedge -> the execution layer blocks opposite-side
        re-entry. KXBTC hourly is a B/T STRIKE LADDER (many contracts per
        expiry): YES on one strike + NO on another are legitimately different
        positions, so it returns False and is never blocked.
        """
        return self._is_15m(ticker)

    def _size_side(
        self,
        ticker: str,
        side: Side,
        ask: float,
        fair_p_side: float,
        bankroll: float,
        half_spread: float = 0.0,
    ) -> SizingResult | None:
        """
        Return a SizingResult for one side, or None if ask is pathological.
        contracts == 0 → skip sentinel.  contracts > 0 → live signal.

        `half_spread` (see kbot.edge.half_spread_dollars) raises the
        required edge above min_net_edge by half the book's bid-ask spread:
        a claimed edge smaller than that is spread noise, not a real
        mispricing (2026-07 calibration audit — see MARKET_RESEARCH.md).
        """
        win = 1.0 - ask
        loss = ask
        if win <= 0 or loss <= 0:
            return None

        # Isotonic calibration (2026-07): reshape the raw lognormal fair_p to
        # the empirically observed win rate for this series. Applied HERE, per
        # side, because that's exactly the quantity the calibrator was fit on
        # (each trade's own side's fair_p -> its 0/1 outcome). No-op when no
        # calibrator is installed for this series (see set_calibrator). Fixes
        # the residual 20-40% miscalibration a single vol multiplier couldn't
        # (the miss is a non-uniform shape error, not a global scale error).
        # raw_p (pre-calibration) is kept on the SizingResult so retrain fits
        # future calibrators on the same quantity they're applied to.
        raw_p = fair_p_side
        fair_p_side = apply_calibrator(self._calibrator_for(ticker), fair_p_side)

        # Low-bucket win-probability floor (2026-07). Never buy a side whose
        # CALIBRATED win probability is below min_side_win_prob. After the WS
        # feed (Fix 4) began pulling far more thin-edge, noise-triggered
        # entries into the 30-50% predicted region, those buckets realized
        # only 14-31% (badly miscalibrated) while the 50%+ buckets stayed
        # sound. Checked here, per side, on fair_p_side -- the exact quantity
        # the calibration buckets are built from -- so it removes only the
        # miscalibrated sub-floor region and leaves the profitable 50%+ one
        # untouched. Applied AFTER apply_calibrator (the buckets are in
        # calibrated space) and BEFORE the edge gate (a below-floor side is
        # never tradeable no matter how large the raw edge). cfg default 0.50;
        # 0.0 disables it. See cfg.min_side_win_prob.
        if fair_p_side < self._cfg.min_side_win_prob:
            log.debug(
                "btc_hourly.skip",
                ticker=ticker,
                side=side.value,
                fair_p=round(fair_p_side, 4),
                min_side_win_prob=self._cfg.min_side_win_prob,
                reason="below_min_side_win_prob",
            )
            return SizingResult(
                ticker=ticker,
                side=side,
                contracts=0,
                price_dollars=ask,
                fair_p=fair_p_side,
                raw_fair_p=raw_p,
                net_edge_dollars=0.0,
                kelly_fraction_used=0.0,
                skip_reason="below_min_side_win_prob",
                event_driver="crypto_price",
            )

        # Two-sided extension of RULE R1 (2026-07): contracts priced above
        # tradeable_band_high are settlement-zone noise just like ones
        # below tradeable_band_low. All 312 honest-backtest trades already
        # fell inside [0.15, 0.48], so this is a no-op on that dataset --
        # it's a defensive bound for live data and any future backtest.
        if not (self._cfg.tradeable_band_low <= ask <= self._cfg.tradeable_band_high):
            log.debug(
                "btc_hourly.skip",
                ticker=ticker,
                side=side.value,
                ask=ask,
                reason="outside_tradeable_band",
            )
            return SizingResult(
                ticker=ticker,
                side=side,
                contracts=0,
                price_dollars=ask,
                fair_p=fair_p_side,
                raw_fair_p=raw_p,
                net_edge_dollars=0.0,
                kelly_fraction_used=0.0,
                skip_reason="outside_tradeable_band",
                event_driver="crypto_price",
            )

        fee = self._cfg.fee_coefficient * ask * (1.0 - ask)
        net_edge = (fair_p_side - ask) - fee - self._cfg.assumed_slippage
        required_edge = self._cfg.min_net_edge + half_spread

        if net_edge < required_edge:
            log.debug(
                "btc_hourly.skip",
                ticker=ticker,
                side=side.value,
                fair_p=round(fair_p_side, 4),
                ask=ask,
                net_edge=round(net_edge, 4),
                half_spread=round(half_spread, 4),
                reason="edge_below_min",
            )
            return SizingResult(
                ticker=ticker,
                side=side,
                contracts=0,
                price_dollars=ask,
                fair_p=fair_p_side,
                raw_fair_p=raw_p,
                net_edge_dollars=net_edge,
                kelly_fraction_used=0.0,
                skip_reason="edge_below_min",
                event_driver="crypto_price",
            )

        kelly_full = (fair_p_side * win - (1 - fair_p_side) * loss) / win
        kelly_full = max(kelly_full, 0.0)
        kelly_frac = self._cfg.kelly_fraction * kelly_full

        dollar_bet     = kelly_frac * bankroll
        raw_contracts  = int(dollar_bet / ask) if ask > 0 else 0
        cap_per_pos    = int(self._cfg.max_position_pct * bankroll / ask) if ask > 0 else 0
        capped         = raw_contracts > min(cap_per_pos, self._cfg.max_contracts_per_order)
        contracts      = min(raw_contracts, cap_per_pos, self._cfg.max_contracts_per_order)
        contracts      = max(contracts, 0)

        log.info(
            "btc_hourly.signal",
            ticker=ticker,
            side=side.value,
            fair_p=round(fair_p_side, 4),
            ask=ask,
            net_edge=round(net_edge, 4),
            half_spread=round(half_spread, 4),
            kelly_frac=round(kelly_frac, 4),
            contracts=contracts,
            capped=capped,
        )

        return SizingResult(
            ticker=ticker,
            side=side,
            contracts=contracts,
            price_dollars=ask,
            fair_p=fair_p_side,
            raw_fair_p=raw_p,
            net_edge_dollars=net_edge,
            kelly_fraction_used=kelly_frac,
            capped=capped,
            event_driver="crypto_price",
        )

    def evaluate(
        self,
        book: OrderBook,
        *,
        btc_price: float | None = None,
        btc_vol: float | None = None,
        bankroll: float,
        current_time: float | None = None,
    ) -> list[SizingResult]:
        if btc_price is None or btc_vol is None:
            log.debug("btc_hourly.skip", reason="no_btc_data")
            return []

        parsed = self._parse(book.ticker)
        if parsed is None:
            log.warning("btc_hourly.skip", ticker=book.ticker, reason="unparseable_ticker")
            return []

        raw_strike, expiry_ts, direction = parsed
        strike = self._resolve_strike(book.ticker, raw_strike)
        if strike is None:
            # KXBTC15M ticker whose floor_strike hasn't been ingested yet
            # (see ingest_market_metadata) -- can't price without it, and
            # guessing would silently corrupt the model. Not a warning:
            # this is an expected, transient state for a brand-new window.
            log.debug("btc_hourly.skip", ticker=book.ticker, reason="floor_strike_unknown")
            return []

        now    = current_time if current_time is not None else datetime.now(tz=timezone.utc).timestamp()
        t_years = (expiry_ts - now) / (365.25 * 24 * 3600)

        t_minutes = t_years * 365.25 * 24 * 60
        if t_minutes <= self._cfg.no_trade_final_minutes:
            return [SizingResult(
                ticker=book.ticker,
                side=Side.YES,
                contracts=0,
                price_dollars=0.0,
                net_edge_dollars=0.0,
                kelly_fraction_used=0.0,
                skip_reason=f"within_{self._cfg.no_trade_final_minutes}min_of_expiry",
                event_driver="crypto_price",
                priced=False,   # declined on the clock; no quote was read
            )]

        # 2026-07 expiry-cutoff finding: bucketing 312 honest-backtest
        # trades by hours-to-expiry showed win rate INCREASING as expiry
        # approaches, not decreasing -- the far-from-expiry regime is where
        # understated vol does the most damage (more time for spot to wander
        # before the market repricess). So this blocks entries FAR from
        # expiry, not near it -- see cfg.max_hours_to_expiry_for_entry's
        # docstring before assuming this is a near-expiry safety cutoff.
        hours_to_expiry = t_minutes / 60.0
        if hours_to_expiry > self._cfg.max_hours_to_expiry_for_entry:
            return [SizingResult(
                ticker=book.ticker,
                side=Side.YES,
                contracts=0,
                price_dollars=0.0,
                net_edge_dollars=0.0,
                kelly_fraction_used=0.0,
                skip_reason=f"beyond_{self._cfg.max_hours_to_expiry_for_entry}h_to_expiry",
                event_driver="crypto_price",
                priced=False,   # declined on the clock; no quote was read
            )]

        # Far-OTM guard: reject strikes that are more than _SIGMA_CUTOFF σ from
        # current spot.  The lognormal model breaks down that far in the tail,
        # and a zero / fresh vol estimate would otherwise produce a degenerate
        # fair_p = 0.5 signal on every such market at startup.
        # For range markets `strike` is the midpoint of the bracket.
        if _is_far_otm(btc_price, strike, btc_vol, t_years):
            z = (
                abs(math.log(btc_price / strike)) / (btc_vol * math.sqrt(t_years))
                if btc_vol > 0 else float("inf")
            )
            log.debug(
                "btc_hourly.skip",
                ticker=book.ticker,
                reason="far_otm",
                sigma_distance=round(z, 2),
            )
            return []

        # Settlement is a cfg.settlement_avg_seconds average of BRTI, not the
        # value at one instant -- price it as such (2026-08). See
        # effective_t_years. cfg.settlement_avg_seconds = 0 restores
        # point-settlement pricing.
        avg_seconds = self._cfg.settlement_avg_seconds
        if direction == "range":
            lower  = strike - _RANGE_HALF_WIDTH
            upper  = strike + _RANGE_HALF_WIDTH
            fair_p = _lognormal_range_prob(
                btc_price, lower, upper, btc_vol, t_years, avg_seconds=avg_seconds,
            )
        else:
            fair_p = _lognormal_prob(
                btc_price, strike, btc_vol, t_years, direction, avg_seconds=avg_seconds,
            )

        # Near-ATM-only guard (2026-07 calibration audit): the model is
        # systematically overconfident away from fair_p ≈ 0.5 -- backtest on
        # real KXBTC data showed 0-15c contracts (deep OTM, low fair_p) won
        # 1.9% of the time against an 8% breakeven requirement, while
        # near-ATM 30-50c contracts won 60%. Restrict trading to markets
        # where the contract is genuinely undecided, not just cheap.
        if self._cfg.near_atm_only and not (
            self._cfg.near_atm_fair_p_min <= fair_p <= self._cfg.near_atm_fair_p_max
        ):
            log.debug(
                "btc_hourly.skip",
                ticker=book.ticker,
                reason="near_atm_only",
                fair_p=round(fair_p, 4),
            )
            return []

        trades: list[SizingResult] = []
        skips:  list[SizingResult] = []

        best_yes = book.best_yes()
        best_no = book.best_no()
        half_spread = (
            half_spread_dollars(best_yes.price, best_no.price)
            if best_yes is not None and best_no is not None
            else 0.0
        )

        if best_yes is not None:
            r = self._size_side(book.ticker, Side.YES, best_yes.price, fair_p, bankroll, half_spread)
            if r is not None:
                (trades if r.contracts > 0 else skips).append(r)

        if best_no is not None:
            r = self._size_side(book.ticker, Side.NO, best_no.price, 1.0 - fair_p, bankroll, half_spread)
            if r is not None:
                (trades if r.contracts > 0 else skips).append(r)

        if trades:
            return trades
        if skips:
            return [max(skips, key=lambda r: r.net_edge_dollars)]
        return []
