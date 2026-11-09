"""
Firing classification by IMPORTING the pinned classifier. Reuse, never
re-derive.

The pilot's classifier is candle-based: price_source is 99.4% "mid", computed as
(yes_bid.close + yes_ask.close)/2 at scripts/market_calibration_study.py:169. So
REST 1-minute yes_bid/yes_ask candlesticks are the intended source, and the
chain below is exactly the pilot's own, entry point for entry point:

    sort_candles / candle_at_or_before / yes_price_from_candle
        -> market_calibration_study.sample_market(..., slices=[7])
    write_csv                      -> the study's own CSV serialisation
    load_observations              -> test_longshot_rule's own CSV parser
    qualifying(obs, 0.70, 7)       -> the firing set
    favorite_side(yes_price)       -> the side the rule buys

The CSV round-trip is not decoration. The pilot's firing decision was made on
observations LOADED FROM data/calib_full.csv, and write_csv serialises the price
as f"{yes_price:.4f}". A mid of 0.699951 is 0.6999 in the pilot's world and a
mid of 0.699951 is >= 0.70 in a naive float world -- those disagree at the
threshold. load_observations also drops rows at exactly 0.00/1.00. Both of those
are classifier semantics, and both belong to the pinned code rather than to this
module, so this module routes through them instead of reimplementing them.

WHAT THIS MODULE IS ALLOWED TO DO
    convert REST candle JSON fields and types into the structures the pinned
    code expects, and shuttle results between the two pinned scripts.

WHAT IT MUST NEVER DO
    recreate (bid+ask)/2, select T-7, choose the favorite, apply >= 0.70, or
    otherwise duplicate classifier semantics outside the pinned code.

tests/test_depthrec_classifier.py holds the regression fixture that proves the
adapter has not changed what the imported classifier sees.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from depthrec import frozen

log = structlog.get_logger(__name__)

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# The pinned classifier. depthrec.integrity has already fail-closed on these two
# files' contents before a recorder gets here; importing them by name is what
# makes "reuse, never re-derive" checkable rather than aspirational.
import market_calibration_study as mcs  # noqa: E402
import test_longshot_rule as tls  # noqa: E402

FEE_COEFFICIENT = 0.07
"""market_calibration_study.write_csv's `coefficient` argument. It feeds only
the ev_cheaper_side column, which the firing decision never reads; it is passed
so the pinned serialiser runs unmodified. tls._FEE_COEFFICIENT is the pinned
constant with the same value and is pinned by the integrity manifest."""

_PLACEHOLDER_SETTLED_YES = False
"""Settlement is UNKNOWN when a live market is classified -- the market has not
closed yet. `qualifying` never reads settled_yes, and this module never reads
ev_cheaper_side, so the placeholder cannot reach the firing decision. That claim
is not left to inspection: assert_settlement_is_irrelevant() below re-runs the
whole classification under both settlement values and requires an identical
firing set, and the regression test calls it."""


class ClassifierUnavailable(RuntimeError):
    """
    The pinned classifier could not be imported and applied literally.

    Raised, never worked around. This -- not depth and not EV -- is what halts
    the pipeline: a recorder that quietly substitutes its own threshold logic
    would produce a denominator nobody can check.
    """


@dataclass
class Classification:
    """One REST-enumerated market's firing verdict."""

    ticker: str
    series: str
    close_ts: int
    target_ts: int
    is_firing: bool
    not_firing_reason: str
    yes_price: float | None
    price_source: str
    side: str
    entry_price: float | None
    candle_end_ts: int | None
    candles: list[dict[str, Any]]

    @property
    def candles_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.candles, sort_keys=True, default=str).encode()
        ).hexdigest()


def classifier_pin() -> dict[str, str]:
    """Which pinned entry points this adapter is calling, for the record."""
    return {
        "study_module": mcs.__file__,
        "longshot_module": tls.__file__,
        "entry_points": (
            "market_calibration_study.sample_market,"
            "market_calibration_study.write_csv,"
            "test_longshot_rule.load_observations,"
            "test_longshot_rule.qualifying,"
            "test_longshot_rule.favorite_side"
        ),
        "threshold": repr(frozen.RULE_THRESHOLD),
        "slice_minutes": repr(frozen.RULE_SLICE_MINUTES),
    }


def target_ts_for(close_ts: int) -> int:
    """Section 3's target time. One subtraction, from a frozen constant."""
    return close_ts - frozen.TARGET_OFFSET_SECONDS


def adapt_candles(raw: Any) -> list[dict[str, Any]]:
    """
    REST candlestick JSON -> the list-of-dicts shape the pinned code expects.

    Kalshi returns exactly that shape already, so this is a type and container
    check rather than a transformation: reject anything that is not a list of
    dicts rather than let a surprise payload reach the classifier as something
    it will silently mis-read. `end_period_ts` is coerced to int because the
    pinned bisect key compares it numerically.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ClassifierUnavailable(f"candles payload is {type(raw).__name__}, expected list")
    out: list[dict[str, Any]] = []
    for candle in raw:
        if not isinstance(candle, dict):
            raise ClassifierUnavailable(f"candle entry is {type(candle).__name__}, expected dict")
        adapted = dict(candle)
        end = adapted.get("end_period_ts")
        if end is not None:
            try:
                adapted["end_period_ts"] = int(end)
            except (TypeError, ValueError) as exc:
                raise ClassifierUnavailable(f"end_period_ts {end!r} is not an int") from exc
        out.append(adapted)
    return out


def _firing_via_pinned_chain(
    *,
    series: str,
    ticker: str,
    close_ts: int,
    candles: list[dict[str, Any]],
    settled_yes: bool,
    threshold: float,
    slice_minutes: int,
) -> tuple[list[Any], list[Any]]:
    """
    The pilot's chain, run end to end on one market.

    Returns (loaded, fired): the observations as the pinned CSV loader produced
    them, and the subset the pinned `qualifying()` fires on. Both are returned
    because the price the CLASSIFIER saw is the loaded one -- write_csv rounds to
    four decimals -- and a Classification that reported the pre-round float would
    be reporting a number no decision was made on.
    """
    study_obs = mcs.sample_market(series, ticker, close_ts, candles, settled_yes, [slice_minutes])
    if not study_obs:
        return [], []

    # write_csv/load_observations is the pilot's own serialisation boundary and
    # carries its 4-decimal price quantisation and its 0.00/1.00 drop rule. A
    # temp file rather than an in-memory buffer because write_csv takes a Path
    # and opens it itself -- adapting the I/O is plumbing; reimplementing the
    # format would be duplicating classifier semantics.
    with tempfile.TemporaryDirectory(prefix="depthrec-classify-") as tmp:
        csv_path = Path(tmp) / "slice.csv"
        mcs.write_csv(csv_path, study_obs, FEE_COEFFICIENT, tls._CONTRACTS_PER_ORDER)
        # The pinned loader narrates dropped rows on stdout; it was written to
        # run as a script. Capture that rather than let one line per market
        # bury the recorder's own log, and hand it to the caller as data.
        chatter = io.StringIO()
        with contextlib.redirect_stdout(chatter):
            loaded = tls.load_observations(csv_path)
    if chatter.getvalue().strip():
        log.debug("depthrec.classify.pinned_stdout", ticker=ticker,
                  text=chatter.getvalue().strip())
    return loaded, tls.qualifying(loaded, threshold, slice_minutes)


def classify_market(
    *,
    series: str,
    ticker: str,
    close_ts: int,
    candles: Any,
    threshold: float = frozen.RULE_THRESHOLD,
    slice_minutes: int = frozen.RULE_SLICE_MINUTES,
) -> Classification:
    """
    Is this REST-enumerated market an otherwise-firing signal?

    `threshold` and `slice_minutes` are arguments rather than constants read
    inside because they are command-line arguments to the pinned scripts and no
    source hash covers them; the caller passes the frozen values it asserted at
    startup, so the value actually used is the value that was checked.
    """
    adapted = adapt_candles(candles)
    target_ts = target_ts_for(close_ts)

    try:
        loaded, fired = _firing_via_pinned_chain(
            series=series, ticker=ticker, close_ts=close_ts, candles=adapted,
            settled_yes=_PLACEHOLDER_SETTLED_YES,
            threshold=threshold, slice_minutes=slice_minutes,
        )
    except SystemExit as exc:
        # load_observations exits rather than raising when a CSV yields nothing
        # usable -- the pilot ran as a script. That is "no usable price at
        # T-7", not a classifier failure.
        if "no usable observations" in str(exc):
            loaded, fired = [], []
        else:
            raise ClassifierUnavailable(
                f"pinned classifier exited while classifying {ticker}: {exc}"
            ) from exc
    except ClassifierUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ClassifierUnavailable(
            f"pinned classifier could not be applied literally to {ticker}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # The price the classifier actually saw, taken from the pinned chain's own
    # output rather than recomputed. `loaded` is post-CSV, so it carries the
    # pilot's 4-decimal quantisation -- the same number the threshold was
    # applied to.
    sampled = loaded[0] if loaded else None
    candle = mcs.candle_at_or_before(mcs.sort_candles(adapted), target_ts)
    candle_end = candle.get("end_period_ts") if candle else None

    if fired:
        obs = fired[0]
        side, entry = tls.favorite_side(obs.yes_price)
        return Classification(
            ticker=ticker, series=series, close_ts=close_ts, target_ts=target_ts,
            is_firing=True, not_firing_reason="",
            yes_price=obs.yes_price, price_source=obs.price_source,
            side=side, entry_price=entry, candle_end_ts=candle_end, candles=adapted,
        )

    if not adapted:
        reason = "no candles returned by REST"
    elif candle is None:
        reason = "no causal candle at or before T-7"
    elif sampled is None:
        reason = (
            "no usable yes price at T-7 -- the pinned chain produced no loaded "
            "observation (yes_price_from_candle found no quote, or the pinned "
            "loader dropped a degenerate 0.00/1.00 price)"
        )
    else:
        reason = "favorite price below the frozen threshold (pinned qualifying)"

    return Classification(
        ticker=ticker, series=series, close_ts=close_ts, target_ts=target_ts,
        is_firing=False, not_firing_reason=reason,
        yes_price=sampled.yes_price if sampled else None,
        price_source=sampled.price_source if sampled else "",
        side="", entry_price=None, candle_end_ts=candle_end, candles=adapted,
    )


def assert_settlement_is_irrelevant(
    *,
    series: str,
    ticker: str,
    close_ts: int,
    candles: Any,
    threshold: float = frozen.RULE_THRESHOLD,
    slice_minutes: int = frozen.RULE_SLICE_MINUTES,
) -> None:
    """
    Prove that the settled_yes placeholder cannot reach the firing decision.

    Runs the whole pinned chain twice, once with settled_yes True and once with
    False, and requires an identical firing set. If the placeholder ever started
    mattering -- because the pinned code changed, or because this adapter
    started reading a settlement-derived column -- this raises instead of
    quietly biasing the denominator.
    """
    adapted = adapt_candles(candles)
    sets = []
    for settled in (False, True):
        _, fired = _firing_via_pinned_chain(
            series=series, ticker=ticker, close_ts=close_ts, candles=adapted,
            settled_yes=settled, threshold=threshold, slice_minutes=slice_minutes,
        )
        sets.append(sorted((o.ticker, o.slice_minutes, o.yes_price) for o in fired))
    if sets[0] != sets[1]:
        raise ClassifierUnavailable(
            f"{ticker}: the settled_yes placeholder changes the firing set "
            f"({sets[0]} vs {sets[1]}) -- classification is no longer independent "
            f"of an outcome the recorder cannot know at T-7"
        )
