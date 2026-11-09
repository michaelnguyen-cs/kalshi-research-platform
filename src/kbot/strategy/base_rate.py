"""
C5 — Base-rate divergence strategy (MARKET_RESEARCH.md Source 4, the
MAXIMUS/North Lake Labs pivot).

Retail-dominated macro markets (FOMC rate decisions, CPI, jobs reports)
systematically misprice against decades of historical base rates because
retail anchors on narrative/news/recency. This strategy computes the
empirical historical frequency of each outcome (Laplace-smoothed, optionally
conditioned on one objective, data-backed trailing condition) and trades the
divergence against the market price when it clears fees -- slow, calendar-
driven entries days before a scheduled release, never a speed race.

Confirmed live ticker formats (2026-07, verified against the real Kalshi API
-- see kbot.feeds.econ_history's module docstring for the historical data
side):
  KXFEDDECISION-{YY}{MON}-{OUTCOME}
      Discrete mutually-exclusive Fed decision, 5 markets per meeting event:
      OUTCOME in {C26 (cut >25bp), C25 (cut 25bp), H0 (hold),
                  H25 (hike 25bp), H26 (hike >25bp)}.
      e.g. KXFEDDECISION-28JAN-H0. {YY}{MON} is the MEETING month.
  KXCPI-{YY}{MON}-T{strike}
      Threshold ladder, strike_type="greater": YES if MoM CPI % change is
      greater than {strike} (a single-decimal percent, may be negative).
      e.g. KXCPI-26NOV-T0.2. {YY}{MON} is the DATA month (the CPI print
      covering that month, released the following month).
  KXPAYROLLS-{YY}{MON}-T{strike}
      Threshold ladder, strike_type="greater": YES if the nonfarm payrolls
      MoM change is greater than {strike} RAW JOBS (not thousands).
      e.g. KXPAYROLLS-26NOV-T10000. {YY}{MON} is the DATA month.

(KXFED, KXCPICORE, and KXUSNFP are real but not used here -- KXFED prices
the absolute rate level rather than the next decision, which needs a full
rate-path model rather than a single-meeting base rate; KXCPICORE is
redundant with headline CPI for a v1; KXUSNFP looked like a legacy/duplicate
series with several dead sibling tickers during discovery.)

Entry window: only evaluates/trades within cfg.base_rate_entry_days of the
scheduled release (config, default 7) and stands down within
cfg.base_rate_stand_down_minutes of it (default 30) -- once the print is
imminent the market price stops reflecting base rate and starts reflecting
last-minute positioning, so there's no base-rate edge left to trade.

Sizing/edge flow through the same pipeline as btc_hourly: fee-aware net
edge, spread-awareness (half_spread), fractional Kelly, RULE R1's tradeable
band, event_driver="us_macro" for the portfolio-level driver cap.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from kbot.config import Settings, settings as _default_settings
from kbot.edge import half_spread_dollars
from kbot.feeds.econ_history import (
    fomc_base_rates,
    load_cpi_history,
    load_fomc_history,
    load_payrolls_history,
    load_release_calendar,
    mom_threshold_base_rate,
    release_for_period,
    trend_bucket,
)
from kbot.models.types import OrderBook, Side, SizingResult
from kbot.strategy.base import Strategy

_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_FEDDECISION_RE = re.compile(r"^KXFEDDECISION-(\d{2})([A-Za-z]{3})-(C26|C25|H0|H25|H26)$")
_CPI_RE = re.compile(r"^KXCPI-(\d{2})([A-Za-z]{3})-T(-?\d+(?:\.\d+)?)$")
_PAYROLLS_RE = re.compile(r"^KXPAYROLLS-(\d{2})([A-Za-z]{3})-T(-?\d+(?:\.\d+)?)$")

_TREND_WINDOW = 3  # trailing months used for the CPI/payrolls trend condition


def _data_period(yy: str, mon: str) -> str | None:
    month = _MONTH_ABBR.get(mon.upper())
    if month is None:
        return None
    return f"20{yy}-{month:02d}"


def parse_ticker(ticker: str) -> tuple[str, str, str | float] | None:
    """
    (event_type, data_period, outcome_or_strike) or None if unparseable.

    event_type: "fomc" | "cpi" | "jobs"
    data_period: "YYYY-MM"
    outcome_or_strike: for fomc, one of the 5 KXFEDDECISION outcome codes
        (str); for cpi/jobs, the strike value (float).
    """
    m = _FEDDECISION_RE.match(ticker)
    if m:
        yy, mon, outcome = m.groups()
        period = _data_period(yy, mon)
        return ("fomc", period, outcome.upper()) if period else None

    m = _CPI_RE.match(ticker)
    if m:
        yy, mon, strike = m.groups()
        period = _data_period(yy, mon)
        return ("cpi", period, float(strike)) if period else None

    m = _PAYROLLS_RE.match(ticker)
    if m:
        yy, mon, strike = m.groups()
        period = _data_period(yy, mon)
        return ("jobs", period, float(strike)) if period else None

    return None


class BaseRateStrategy(Strategy):
    """
    Empirical base-rate divergence for FOMC/CPI/jobs markets. See module
    docstring for the confirmed ticker formats and entry-window logic.
    """

    name = "base_rate"
    series_tickers = ["KXFEDDECISION", "KXCPI", "KXPAYROLLS"]
    required_feeds: list[str] = []  # no shared external feed -- release calendar + history are loaded, not polled

    def __init__(self, cfg: Settings | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._fomc_history = load_fomc_history()
        self._cpi_history = load_cpi_history()
        self._payrolls_history = load_payrolls_history()
        self._calendar = load_release_calendar()

    # ── Base rate computation (look-ahead-free: only uses data strictly
    # before the period being predicted) ────────────────────────────────────

    def _fomc_fair_p_yes(self, data_period: str, outcome: str) -> float | None:
        cutoff = data_period + "-01"
        history_before = [m for m in self._fomc_history if m.meeting_date < cutoff]
        if len(history_before) < 3:
            return None  # not enough precedent for even an unconditional rate
        trailing_pattern = tuple(m.decision for m in history_before[-3:])
        rates = fomc_base_rates(history_before, trailing_pattern=trailing_pattern)
        return rates.get(outcome)

    def _cpi_fair_p_yes(self, data_period: str, strike: float) -> float | None:
        history_before = [p for p in self._cpi_history if p.month < data_period + "-01"]
        if len(history_before) < 12:
            return None
        trend = trend_bucket(history_before, len(history_before), window=_TREND_WINDOW)
        return mom_threshold_base_rate(
            history_before, strike, value_field="mom_pct_change",
            direction="greater", condition_trend=trend,
        )

    def _payrolls_fair_p_yes(self, data_period: str, strike_raw_jobs: float) -> float | None:
        history_before = [p for p in self._payrolls_history if p.month < data_period + "-01"]
        if len(history_before) < 12:
            return None
        trend = trend_bucket(history_before, len(history_before), window=_TREND_WINDOW)
        threshold_thousands = strike_raw_jobs / 1000.0
        return mom_threshold_base_rate(
            history_before, threshold_thousands, value_field="mom_change",
            direction="greater", condition_trend=trend,
        )

    def _fair_p_yes(self, event_type: str, data_period: str, outcome_or_strike: str | float) -> float | None:
        if event_type == "fomc":
            return self._fomc_fair_p_yes(data_period, outcome_or_strike)
        if event_type == "cpi":
            return self._cpi_fair_p_yes(data_period, outcome_or_strike)
        if event_type == "jobs":
            return self._payrolls_fair_p_yes(data_period, outcome_or_strike)
        return None

    # ── Sizing (same edge/Kelly pattern as btc_hourly) ──────────────────────

    def _size_side(
        self,
        ticker: str,
        side: Side,
        ask: float,
        fair_p_side: float,
        bankroll: float,
        half_spread: float = 0.0,
    ) -> SizingResult | None:
        win = 1.0 - ask
        loss = ask
        if win <= 0 or loss <= 0:
            return None

        if not (self._cfg.tradeable_band_low <= ask <= self._cfg.tradeable_band_high):
            return SizingResult(
                ticker=ticker, side=side, contracts=0, price_dollars=ask,
                fair_p=fair_p_side, net_edge_dollars=0.0, kelly_fraction_used=0.0,
                skip_reason="outside_tradeable_band", event_driver="us_macro",
            )

        fee = self._cfg.fee_coefficient * ask * (1.0 - ask)
        net_edge = (fair_p_side - ask) - fee - self._cfg.assumed_slippage
        required_edge = self._cfg.min_net_edge + half_spread

        if net_edge < required_edge:
            return SizingResult(
                ticker=ticker, side=side, contracts=0, price_dollars=ask,
                fair_p=fair_p_side, net_edge_dollars=net_edge, kelly_fraction_used=0.0,
                skip_reason="edge_below_min", event_driver="us_macro",
            )

        kelly_full = (fair_p_side * win - (1 - fair_p_side) * loss) / win
        kelly_full = max(kelly_full, 0.0)
        kelly_frac = self._cfg.kelly_fraction * kelly_full

        dollar_bet = kelly_frac * bankroll
        raw_contracts = int(dollar_bet / ask) if ask > 0 else 0
        cap_per_pos = int(self._cfg.max_position_pct * bankroll / ask) if ask > 0 else 0
        capped = raw_contracts > min(cap_per_pos, self._cfg.max_contracts_per_order)
        contracts = min(raw_contracts, cap_per_pos, self._cfg.max_contracts_per_order)
        contracts = max(contracts, 0)

        return SizingResult(
            ticker=ticker, side=side, contracts=contracts, price_dollars=ask,
            fair_p=fair_p_side, net_edge_dollars=net_edge, kelly_fraction_used=kelly_frac,
            capped=capped, event_driver="us_macro",
        )

    # ── Strategy interface ──────────────────────────────────────────────────

    def evaluate(
        self,
        book: OrderBook,
        *,
        btc_price: float | None = None,   # unused -- base_rate is not a BTC strategy
        btc_vol: float | None = None,     # unused
        bankroll: float,
        current_time: float | None = None,
    ) -> list[SizingResult]:
        parsed = parse_ticker(book.ticker)
        if parsed is None:
            return []
        event_type, data_period, outcome_or_strike = parsed

        release = release_for_period(self._calendar, event_type, data_period)
        if release is None:
            # No known release time -- can't gate the entry window honestly,
            # so don't trade rather than guess (matches this codebase's
            # "unsure = don't trade" convention, e.g. near_atm_only).
            return []

        now = current_time if current_time is not None else datetime.now(tz=timezone.utc).timestamp()
        seconds_to_release = release.release_ts - now

        if seconds_to_release < 0:
            return []  # release has passed; market should be settling, not entering

        if seconds_to_release <= self._cfg.base_rate_stand_down_minutes * 60:
            return [SizingResult(
                ticker=book.ticker, side=Side.YES, contracts=0, price_dollars=0.0,
                net_edge_dollars=0.0, kelly_fraction_used=0.0,
                skip_reason=f"within_{self._cfg.base_rate_stand_down_minutes}min_of_release",
                event_driver="us_macro", priced=False,
            )]

        days_to_release = seconds_to_release / 86400.0
        if days_to_release > self._cfg.base_rate_entry_days:
            return [SizingResult(
                ticker=book.ticker, side=Side.YES, contracts=0, price_dollars=0.0,
                net_edge_dollars=0.0, kelly_fraction_used=0.0,
                skip_reason=f"beyond_{self._cfg.base_rate_entry_days}d_entry_window",
                event_driver="us_macro", priced=False,
            )]

        fair_p = self._fair_p_yes(event_type, data_period, outcome_or_strike)
        if fair_p is None:
            return []  # not enough historical precedent to make a claim

        trades: list[SizingResult] = []
        skips: list[SizingResult] = []

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
