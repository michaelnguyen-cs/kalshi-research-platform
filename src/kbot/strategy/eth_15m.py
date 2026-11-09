"""
ETH 15-Minute strategy (Kalshi series KXETH15M).

A thin subclass of BTCHourlyStrategy -- the driftless-lognormal fair-value
model, the Kelly sizing path, the isotonic calibrator wiring, and the whole
of _size_side are INHERITED, not copied. Only what is genuinely specific to
ETH-as-an-underlying is overridden below: the ticker grammar, the series
identity, and the spot feed name.

WHY INHERITANCE: _size_side carries two 2026-07 fixes -- the
min_side_win_prob floor and the raw_fair_p (pre-calibration probability)
plumbing that retrain fits on. A copy-pasted sibling class would fork both on
day one and silently miss every future fix to either. Subclassing means a fix
to the shared sizing path lands in BTC and ETH simultaneously. The same
reasoning does NOT apply to the price feed (kbot.feeds.eth), which is a pure
I/O adapter with no shared-fix surface and is a deliberate copy.

Real Kalshi KXETH15M ticker format (confirmed against the live API, 2026-07):
  KXETH15M-YYMONDDHHMM-MM
  e.g.  KXETH15M-26JUL311800-00
        (floor_strike 1860.71, close_time 2026-07-31T22:00:00Z == 18:00 EDT)

Structurally identical to KXBTC15M:
  • YYMONDDHHMM : the window's CLOSE time (settlement moment), in US Eastern
    local time, to the minute. MM is always :00/:15/:30/:45.
  • trailing -MM: redundant suffix, repeats the same minute value.
  • Exactly ONE binary market per event (no T/B ladder): strike_type is
    "greater_or_equal", floor_strike = the ETH reference price at the
    window's OPEN. Title: "ETH price up in next 15 mins?" -- YES if ETH at
    close >= floor_strike.
  • CRITICAL: floor_strike is NOT in the ticker string. _parse() returns
    strike=None; evaluate() resolves it from the inherited _strike_cache,
    populated out-of-band via ingest_market_metadata() (called by
    kbot.runner.Runner during ticker discovery). A market whose strike
    hasn't been ingested yet is skipped, never guessed.
  • BEWARE: as with KXBTC15M, the market record's expiration_time is a
    ~7-day-out SLA deadline, NOT the settlement time. close_time is the real
    settlement moment.

CALIBRATION STATUS: ETH has NO backtest history and NO fitted calibrator.
_calibrator_for() returns None for KXETH15M, so apply_calibrator is a no-op
and the strategy trades on RAW lognormal probabilities. That is the correct
starting state -- it must earn its way up from the "backtest" gate on its own
forward evidence (kbot.gates.check_paper_promotion), exactly as kxbtc15m did.
It must also run against its OWN databases: see Settings.events_db_path and
Runner._assert_isolated_domains for why sharing BTC's would corrupt BTC's
champion calibrator.
"""

from __future__ import annotations

import re

import structlog

from kbot.strategy.btc_hourly import BTCHourlyStrategy, _et_to_utc_ts

log = structlog.get_logger(__name__)

# Matches: KXETH15M-26JUL311800-00  (close-time-to-the-minute + redundant suffix)
# Mirrors btc_hourly._TICKER_15M_RE exactly, with the series prefix swapped.
_TICKER_15M_RE = re.compile(
    r"^KXETH15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-(\d{2})$",
    re.IGNORECASE,
)


class ETH15MStrategy(BTCHourlyStrategy):
    """
    Driftless lognormal fair-value strategy for Kalshi KXETH15M markets.

    Single binary per 15-minute window ("ETH up in next 15 mins?"), priced
    against the ingested floor_strike. Inherits evaluate(), _size_side(),
    sizing, and calibrator handling from BTCHourlyStrategy unchanged.
    """

    name = "kxeth15m"
    series_tickers = ["KXETH15M"]
    required_feeds = ["eth_spot"]

    # Underlying identity (see BTCHourlyStrategy's "Underlying identity" block).
    # series_hourly is None: there is no KXETH hourly counterpart, so every
    # ticker this strategy owns is a 15-minute one.
    spot_feed_name = "eth_spot"
    series_15m = "KXETH15M"
    series_hourly = None

    def _parse(self, ticker: str) -> tuple[float | None, float, str] | None:
        """
        (strike, expiry_unix_utc, direction) or None if unparseable.

        strike is ALWAYS None for KXETH15M -- floor_strike is a dynamic
        per-window value not encoded in the ticker at all (see this module's
        docstring). evaluate() resolves it from _strike_cache and skips the
        market when it hasn't been ingested yet. direction is always "above"
        (YES if ETH >= floor_strike at close).
        """
        m = _TICKER_15M_RE.match(ticker)
        if not m:
            return None
        yy, mon_str, day_str, hour_str, minute_str, _suffix = m.groups()
        expiry_ts = _et_to_utc_ts(yy, mon_str, day_str, hour_str, minute_str)
        if expiry_ts is None:
            return None
        return None, expiry_ts, "above"
