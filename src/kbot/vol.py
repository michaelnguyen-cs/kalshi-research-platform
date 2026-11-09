"""
Volatility estimation: ladder-implied (primary, live-only) and a
realized-vol fallback (EWMA + Parkinson).

2026-07 calibration audit: our close-to-close realized-vol estimator badly
understated true BTC vol (logged 15-36% annualised vs. CME 25-delta IV of
46-95%), which for RANGE contracts inflates the model's stay-in-bucket
probability. Confirmed by back-solving the market-implied vol for 312
honest 30-day backtest trades: 100% of solvable cases had
implied_vol > our_vol, median ratio 2.2x (range 1.37x-5.39x).

Fix: prefer the market's OWN current vol (ladder-implied -- back-solved
from the live near-ATM strike ladder, which reprices every tick) over our
own forecast. Our edge becomes the documented 3-7s repricing lag, not a
claim to out-forecast volatility. Fall back to a conservative realized-vol
estimate (EWMA + Parkinson, floored) only when the ladder can't be fit
(thin book, or backtest data with no simultaneous ladder to fit).

2026-07 series-aware vol window: the fallback estimator above was tuned
(VOL_MULTIPLIER/VOL_FLOOR) against KXBTC's 60-minute contracts using a
fixed 3600s trailing window -- appropriate there, but applying the SAME
window to KXBTC15M's 15-minute contracts systematically OVERESTIMATED
their vol (confirmed: median implied-vol/estimated-vol ratio 0.488 at the
3600s window, i.e. ~2x too high), compressing fair_p toward 0.5 in exactly
the direction the KXBTC15M calibration audit found. ewma_vol/fallback_vol
now take an optional window/multiplier so each series can use its own
horizon -- see Strategy.vol_window_seconds / Strategy.vol_multiplier.

2026-08 vol FLOOR horizon fix: the floor was the one member of that trio left
series-global. cfg.vol_floor = 0.35 is a sane degeneracy guard under KXBTC's
3600s realized-vol distribution but sits INSIDE the body of KXBTC15M's 900s
one, where it bound on the large majority of evaluations -- so sigma was the
constant 0.35 rather than an estimate, and fair_p = Phi(ln(S/K) / (sigma*sqrt(T)))
collapsed to a near-constant ~0.5. fallback_vol now takes a `floor` override
alongside `multiplier`, and reports whether the floor bound. See
cfg.vol_floor_15m / Strategy.vol_floor.

2026-08 settlement object: these contracts settle on a
cfg.settlement_avg_seconds average of BRTI, not on a point value. The ladder
fit therefore prices its candidate strikes on the same EFFECTIVE time to
expiry the strategy uses (see kbot.strategy.btc_hourly.effective_t_years) --
fitting sigma against point-settlement prices and then pricing with
average-settlement ones would put a systematic bias straight into the fitted
sigma.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import structlog
from scipy.optimize import minimize_scalar

from kbot.config import Settings, settings as _default_settings
from kbot.models.types import OrderBook

log = structlog.get_logger(__name__)

# Bounds for any solved/fitted sigma (5% to 300% annualised).
_SIGMA_LO = 0.05
_SIGMA_HI = 3.0


@dataclass
class VolEstimate:
    """A volatility estimate plus which source produced it (for logging)."""
    sigma: float
    source: str  # "ladder" | "fallback"
    floored: bool = False
    """True if the VOL FLOOR (not the realized estimate) produced `sigma`.

    2026-08: a floor that binds is not a guard, it IS the estimate -- and a
    pinned sigma pins fair_p at ~0.5 on every contract. Surfaced so the
    binding rate is observable in telemetry instead of being discovered
    months later by replaying kbot_events.db.
    """


# ── Realized-vol fallback (EWMA + Parkinson) ─────────────────────────────────

def ewma_vol(
    price_history: list[tuple[float, float]],
    *,
    lam: float = 0.94,
    window_seconds: float | None = None,
) -> float | None:
    """
    RiskMetrics EWMA (lambda=0.94 by default) annualised log-return
    volatility from a (timestamp, price) series, oldest first.

    Unlike a flat-window close-to-close estimator, EWMA weights recent
    returns more heavily, so it reacts faster to a genuine vol regime
    change instead of smearing it evenly across the whole window.
    Returns None if fewer than 2 samples (after windowing, if given).

    `window_seconds`, if given, restricts the computation to samples
    within that trailing window of the LAST sample's own timestamp (this
    function has no notion of "now" beyond what price_history itself
    contains). 2026-07 series-aware vol fix: a mismatched, too-long window
    for a short-horizon contract was confirmed to overestimate its vol
    (KXBTC15M's 15-minute contracts against the previous fixed 3600s
    window: median implied/estimated vol ratio 0.488, i.e. our vol ran
    ~2x too high) -- callers must pass a window matching the contract's
    own horizon (see Strategy.vol_window_seconds).
    """
    if window_seconds is not None and price_history:
        cutoff = price_history[-1][0] - window_seconds
        price_history = [(t, p) for t, p in price_history if t >= cutoff]

    if len(price_history) < 2:
        return None

    ts = [t for t, _ in price_history]
    px = [p for _, p in price_history]
    log_returns = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i] > 0 and px[i - 1] > 0]
    if not log_returns:
        return None

    var = log_returns[0] ** 2
    for r in log_returns[1:]:
        var = lam * var + (1.0 - lam) * r * r
    std_per_sample = math.sqrt(var)

    avg_interval = (ts[-1] - ts[0]) / max(len(log_returns), 1)
    if avg_interval <= 0:
        return None
    samples_per_year = (365.25 * 24 * 3600) / avg_interval
    return std_per_sample * math.sqrt(samples_per_year)


def parkinson_vol(
    candles: list[tuple[float, float]],
    *,
    bar_seconds: float,
) -> float | None:
    """
    Parkinson high-low range volatility estimator, annualised.

    `candles` is a list of (high, low) pairs per bar. Captures intra-bar
    jump/range volatility that a close-to-close (or EWMA-on-closes)
    estimator misses entirely -- a bar that spikes and reverts looks flat
    to a close-based estimator but wide to Parkinson.

    sigma_bar^2 = (1 / (4 ln 2)) * mean(ln(H/L)^2)
    """
    if not candles or bar_seconds <= 0:
        return None
    factor = 1.0 / (4.0 * math.log(2.0))
    sq_log_ranges = [math.log(h / l) ** 2 for h, l in candles if h > 0 and l > 0 and h >= l]
    if not sq_log_ranges:
        return None
    var_per_bar = factor * (sum(sq_log_ranges) / len(sq_log_ranges))
    std_per_bar = math.sqrt(var_per_bar)
    bars_per_year = (365.25 * 24 * 3600) / bar_seconds
    return std_per_bar * math.sqrt(bars_per_year)


def fallback_vol(
    ewma: float | None,
    parkinson: float | None,
    cfg: Settings | None = None,
    *,
    multiplier: float | None = None,
    floor: float | None = None,
) -> tuple[float, bool]:
    """
    Realized-vol fallback used when the ladder can't be fit:
    max(realized, VOL_FLOOR) after scaling realized by a multiplier,
    where realized = max(ewma, parkinson) (whichever samples are available).

    Returns (sigma, floored) -- `floored` is True when the FLOOR, not the
    realized estimate, produced sigma. Callers that only want the number can
    unpack and discard it; the flag exists because a floor that binds most of
    the time has silently replaced the model (see VolEstimate.floored).

    `multiplier`, if given, overrides cfg.vol_multiplier -- lets callers
    apply a series-specific scaling (e.g. cfg.vol_multiplier_15m for
    KXBTC15M, which needs its own value now that the window fix means
    hourly and 15m no longer share one vol-estimation path -- see
    Strategy.vol_multiplier) without constructing a whole separate
    Settings object. None (the default) uses cfg.vol_multiplier unchanged.

    `floor`, if given, overrides cfg.vol_floor the same way, for the same
    reason (see cfg.vol_floor_15m / Strategy.vol_floor). 2026-08: cfg.vol_floor
    = 0.35 is an under-bound on HOURLY-horizon implied vol; on KXBTC15M's 900s
    window it bound on the large majority of evaluations, pinning sigma and so
    pinning fair_p at ~0.5. A floor must sit below the low tail of the
    estimator it guards, and that tail is horizon-specific -- one global value
    cannot serve both horizons, exactly as with the window and the multiplier.

    VOL_MULTIPLIER and VOL_FLOOR default to values justified by CME BTC
    25-delta IV levels (46-95% observed in 2026) rather than tuned against
    this backtest -- the whole point is to stop under-forecasting vol, not
    to curve-fit a new constant that happens to work on 30 days of data.
    """
    cfg = cfg or _default_settings
    effective_multiplier = cfg.vol_multiplier if multiplier is None else multiplier
    effective_floor = cfg.vol_floor if floor is None else floor
    candidates = [v for v in (ewma, parkinson) if v is not None]
    realized = max(candidates) if candidates else 0.0
    scaled = realized * effective_multiplier
    if scaled >= effective_floor:
        return scaled, False
    return effective_floor, True


# ── Ladder-implied vol (live-only: needs multiple simultaneous strikes) ──────

def implied_vol_from_ladder(
    books: dict[str, OrderBook],
    spot: float,
    now_ts: float,
    *,
    parse_ticker: Callable[[str], tuple[float, float, str] | None],
    lognormal_prob: Callable[[float, float, float, float, str], float],
    lognormal_range_prob: Callable[[float, float, float, float, float], float],
    range_half_width: float = 250.0,
    max_strikes: int = 5,
    lo: float = _SIGMA_LO,
    hi: float = _SIGMA_HI,
    settlement_avg_seconds: float = 0.0,
) -> float | None:
    """
    Back-solve a single sigma that best reproduces the CURRENT prices of
    the most liquid near-ATM range buckets, by minimizing squared pricing
    error across them jointly (one shared sigma explaining several
    simultaneous strikes is a much more robust estimate than fitting any
    one contract alone).

    `books` is a ticker -> OrderBook map (e.g. PaperTrader._books) spanning
    one expiry's strike ladder. Only "range" (B-type) markets are used --
    their narrow $500 buckets are far more sensitive to sigma than the T
    catch-alls, which is exactly what makes them informative for this fit.
    "Most liquid" is approximated by proximity to spot (closest `max_strikes`
    buckets), since that's what's actually observable pre-trade.

    `parse_ticker`/`lognormal_prob`/`lognormal_range_prob` are passed in
    (rather than imported) to avoid a circular import with
    kbot.strategy.btc_hourly, which is this function's only caller.

    `settlement_avg_seconds` (2026-08) is the settlement averaging window --
    the fit prices its candidates on the same variance-equivalent effective
    time the strategy prices with, so the sigma it hands back is on the same
    footing as the sigma the pricer consumes. 0.0 = point settlement.

    Returns None if fewer than 2 usable near-ATM range quotes are found, or
    the fit can't bracket a sane sigma (thin/degenerate book) -- callers
    should fall back to fallback_vol() in that case.
    """
    candidates: list[tuple[float, float, float]] = []  # (strike, expiry_ts, market_price)
    for ticker, book in books.items():
        parsed = parse_ticker(ticker)
        if parsed is None:
            continue
        strike, expiry_ts, direction = parsed
        if direction != "range":
            continue
        best_yes = book.best_yes()
        if best_yes is None:
            continue
        candidates.append((strike, expiry_ts, best_yes.price))

    if len(candidates) < 2:
        return None

    candidates.sort(key=lambda c: abs(c[0] - spot))
    candidates = candidates[:max_strikes]

    def objective(sigma: float) -> float:
        error = 0.0
        for strike, expiry_ts, market_price in candidates:
            t_years = (expiry_ts - now_ts) / (365.25 * 24 * 3600)
            if t_years <= 0:
                continue
            lower, upper = strike - range_half_width, strike + range_half_width
            model_price = lognormal_range_prob(
                spot, lower, upper, sigma, t_years,
                avg_seconds=settlement_avg_seconds,
            )
            error += (model_price - market_price) ** 2
        return error

    result = minimize_scalar(objective, bounds=(lo, hi), method="bounded")
    if not result.success:
        return None
    # A fit that doesn't actually explain the data (near-flat objective, or
    # pinned at a bound) isn't a real signal -- treat as "couldn't fit".
    if result.x <= lo * 1.01 or result.x >= hi * 0.99:
        return None
    return float(result.x)


def _apply_sanity_cap(sigma: float, source: str, cfg: Settings) -> float:
    """
    Clamp a degenerate sigma to cfg.vol_sanity_cap (default 150% annualised
    -- well above any realistic BTC vol, including CME's observed 46-95%
    25-delta IV). Guards against thin/early-session estimates (either
    fallback_vol on a barely-warmed-up EWMA, or a ladder fit that lands
    high without tripping implied_vol_from_ladder's own near-bound
    rejection) silently producing fake edge downstream.
    """
    if sigma <= cfg.vol_sanity_cap:
        return sigma
    log.warning(
        "vol.sanity_cap_applied",
        sigma=round(sigma, 4),
        cap=cfg.vol_sanity_cap,
        source=source,
    )
    return cfg.vol_sanity_cap


def estimate_vol(
    books: dict[str, OrderBook] | None,
    spot: float | None,
    now_ts: float,
    *,
    parse_ticker: Callable[[str], tuple[float, float, str] | None],
    lognormal_prob: Callable[[float, float, float, float, str], float],
    lognormal_range_prob: Callable[[float, float, float, float, float], float],
    ewma: float | None,
    parkinson: float | None,
    cfg: Settings | None = None,
    vol_multiplier_override: float | None = None,
    vol_floor_override: float | None = None,
) -> VolEstimate:
    """
    Ladder-implied vol if it can be fit, else the realized-vol fallback.
    Always returns a usable sigma (never None) -- see fallback_vol().
    Clamped to cfg.vol_sanity_cap in either case -- see _apply_sanity_cap().

    `vol_multiplier_override` / `vol_floor_override`, if given, are passed
    through to fallback_vol (see its docstring) -- they let a per-ticker,
    series-specific multiplier and floor apply to the fallback path only (the
    ladder-implied path needs neither: it's fit directly from live prices).

    NOTE which path each series actually takes. implied_vol_from_ladder needs
    >= 2 simultaneous RANGE (B-type) quotes; KXBTC15M / KXETH15M are
    single-binary series with no range ladder at all, so they ALWAYS land on
    the fallback -- which is why the fallback's floor, not the ladder, is what
    sets their sigma in practice.
    """
    cfg = cfg or _default_settings
    if books and spot is not None:
        sigma = implied_vol_from_ladder(
            books, spot, now_ts,
            parse_ticker=parse_ticker,
            lognormal_prob=lognormal_prob,
            lognormal_range_prob=lognormal_range_prob,
            settlement_avg_seconds=cfg.settlement_avg_seconds,
        )
        if sigma is not None:
            return VolEstimate(sigma=_apply_sanity_cap(sigma, "ladder", cfg), source="ladder")
    sigma, floored = fallback_vol(
        ewma, parkinson, cfg,
        multiplier=vol_multiplier_override,
        floor=vol_floor_override,
    )
    if floored:
        log.debug("vol.floor_bound", sigma=round(sigma, 4), ewma=ewma, parkinson=parkinson)
    return VolEstimate(
        sigma=_apply_sanity_cap(sigma, "fallback", cfg), source="fallback", floored=floored,
    )
