"""
Regression fixture for the pinned firing classifier and its adapter.

Three things must agree on identical source data:

  1. the PILOT's pipeline
         market_calibration_study.sample_market
      -> market_calibration_study.write_csv        (one batched CSV, as the
      -> test_longshot_rule.load_observations       pilot wrote calib_full.csv)
      -> test_longshot_rule.qualifying(0.70, 7)
  2. depthrec.classify.classify_market, market by market
  3. the firing set committed in tests/fixtures/depthrec_classifier_pilot.json

(1) vs (2) is the requirement: the imported classifier selects the same signals
as the pilot for identical source data, and the adapter has not changed what the
classifier sees. (3) stops both from drifting together -- a committed
expectation cannot be re-derived by the code it checks.

The fixture is real Kalshi 1-minute candlestick JSON for 80 settled Thread A
markets spanning T-7 prices from 0.05 to 0.95, with both YES-side and NO-side
favorites firing. Captured by scripts/depthrec_capture_fixture.py.

The pilot path gets each market's REAL settlement; the adapter path gets the
placeholder it uses live, because at T-7 the outcome does not exist yet. Their
agreeing on real data is also a demonstration that the placeholder cannot reach
the firing decision.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from depthrec import classify, frozen  # noqa: E402

import market_calibration_study as mcs  # noqa: E402
import test_longshot_rule as tls  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "depthrec_classifier_pilot.json"


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _pilot_firing(markets: list[dict], tmp_path: Path) -> list[dict]:
    """The pilot's own pipeline, batched exactly as the pilot ran it."""
    study_obs = []
    for m in markets:
        study_obs.extend(mcs.sample_market(
            m["series"], m["ticker"], m["close_ts"], m["candles"],
            m["settled_yes"], [frozen.RULE_SLICE_MINUTES],
        ))
    csv_path = tmp_path / "calib_slice.csv"
    mcs.write_csv(csv_path, study_obs, classify.FEE_COEFFICIENT, tls._CONTRACTS_PER_ORDER)
    fired = tls.qualifying(
        tls.load_observations(csv_path), frozen.RULE_THRESHOLD, frozen.RULE_SLICE_MINUTES
    )
    rows = []
    for obs in fired:
        side, entry = tls.favorite_side(obs.yes_price)
        rows.append({
            "ticker": obs.ticker, "series": obs.series, "yes_price": obs.yes_price,
            "price_source": obs.price_source, "side": side, "entry_price": entry,
        })
    return sorted(rows, key=lambda r: r["ticker"])


def _adapter_firing(markets: list[dict]) -> list[dict]:
    rows = []
    for m in markets:
        result = classify.classify_market(
            series=m["series"], ticker=m["ticker"], close_ts=m["close_ts"],
            candles=m["candles"],
            threshold=frozen.RULE_THRESHOLD, slice_minutes=frozen.RULE_SLICE_MINUTES,
        )
        if result.is_firing:
            rows.append({
                "ticker": result.ticker, "series": result.series,
                "yes_price": result.yes_price, "price_source": result.price_source,
                "side": result.side, "entry_price": result.entry_price,
            })
    return sorted(rows, key=lambda r: r["ticker"])


def test_fixture_is_a_real_and_useful_sample(fixture):
    """A fixture that never straddles the threshold proves nothing."""
    markets = fixture["markets"]
    assert len(markets) >= 50
    assert fixture["threshold"] == frozen.RULE_THRESHOLD
    assert fixture["slice_minutes"] == frozen.RULE_SLICE_MINUTES

    expected = fixture["expected_firing"]
    assert 0 < len(expected) < len(markets), "fixture must contain both firing and non-firing"
    sides = {row["side"] for row in expected}
    assert sides == {"YES", "NO"}, f"fixture must exercise both favorite sides, got {sides}"


def test_adapter_selects_the_same_signals_as_the_pilot(fixture, tmp_path):
    pilot = _pilot_firing(fixture["markets"], tmp_path)
    adapter = _adapter_firing(fixture["markets"])
    assert [r["ticker"] for r in adapter] == [r["ticker"] for r in pilot]
    assert adapter == pilot


def test_both_paths_match_the_committed_expectation(fixture, tmp_path):
    """
    Guards against the pilot path and the adapter drifting together. The
    expectation was computed once, at capture time, and committed.
    """
    expected = sorted(fixture["expected_firing"], key=lambda r: r["ticker"])
    assert _pilot_firing(fixture["markets"], tmp_path) == expected
    assert _adapter_firing(fixture["markets"]) == expected


def test_non_firing_markets_are_classified_not_dropped(fixture):
    """
    Every fixture market gets a verdict. A market the rule does not fire on is
    recorded as not firing with a reason -- never silently absent, which would
    be indistinguishable from a market the recorder never saw.
    """
    firing = {row["ticker"] for row in fixture["expected_firing"]}
    for m in fixture["markets"]:
        result = classify.classify_market(
            series=m["series"], ticker=m["ticker"], close_ts=m["close_ts"],
            candles=m["candles"],
        )
        assert result.ticker == m["ticker"]
        assert result.target_ts == m["close_ts"] - frozen.TARGET_OFFSET_SECONDS
        assert result.is_firing == (m["ticker"] in firing)
        if not result.is_firing:
            assert result.not_firing_reason


def test_settlement_placeholder_cannot_reach_the_firing_decision(fixture):
    """
    On real data: running the whole pinned chain under both settlement values
    must give an identical firing set. If it ever does not, the recorder is
    conditioning on an outcome it cannot know at T-7.
    """
    for m in fixture["markets"]:
        classify.assert_settlement_is_irrelevant(
            series=m["series"], ticker=m["ticker"], close_ts=m["close_ts"],
            candles=m["candles"],
        )


def test_adapter_uses_the_pinned_price_and_slice_selection(fixture):
    """
    The price the adapter reports is the one the CLASSIFIER decided on.

    That is the pinned causal candle's price after the pilot's own CSV
    round-trip, which serialises as f"{yes_price:.4f}" -- so a mid of
    0.10500000000000001 is 0.105 by the time the threshold sees it. Reporting
    the pre-round float would name a number no decision was made on, and at the
    threshold the two can disagree about whether the rule fires at all.

    The reference values here come from mcs's own helpers rather than a local
    (bid+ask)/2, because a local reimplementation in the test would be exactly
    the duplication the adapter is forbidden.
    """
    for m in fixture["markets"][:20]:
        target = m["close_ts"] - frozen.TARGET_OFFSET_SECONDS
        candle = mcs.candle_at_or_before(mcs.sort_candles(m["candles"]), target)
        price, source = mcs.yes_price_from_candle(candle) if candle else (None, "")
        result = classify.classify_market(
            series=m["series"], ticker=m["ticker"], close_ts=m["close_ts"],
            candles=m["candles"],
        )
        if result.yes_price is not None:
            assert result.yes_price == float(f"{price:.4f}")
            assert result.price_source == source
        assert candle is None or result.candle_end_ts == candle["end_period_ts"]
        # Strictly causal: the candle used never ends after T-7.
        assert candle is None or candle["end_period_ts"] <= target


def test_reported_price_is_the_post_csv_price_the_threshold_saw(fixture):
    """
    Pin the quantisation itself: at least one fixture market must have a raw mid
    that differs from its 4-decimal serialisation, and the adapter must report
    the serialised one. If this ever finds no such market the fixture has lost
    its teeth and the guard is silently gone.
    """
    found = 0
    for m in fixture["markets"]:
        target = m["close_ts"] - frozen.TARGET_OFFSET_SECONDS
        candle = mcs.candle_at_or_before(mcs.sort_candles(m["candles"]), target)
        if candle is None:
            continue
        raw_price, _ = mcs.yes_price_from_candle(candle)
        if raw_price is None or raw_price == float(f"{raw_price:.4f}"):
            continue
        found += 1
        result = classify.classify_market(
            series=m["series"], ticker=m["ticker"], close_ts=m["close_ts"],
            candles=m["candles"],
        )
        assert result.yes_price == float(f"{raw_price:.4f}") != raw_price
    assert found, "no fixture market exercises the CSV price quantisation"


def test_adapter_rejects_payloads_it_cannot_adapt():
    with pytest.raises(classify.ClassifierUnavailable):
        classify.classify_market(series="S", ticker="T", close_ts=1_787_000_000,
                                 candles={"not": "a list"})
    with pytest.raises(classify.ClassifierUnavailable):
        classify.classify_market(series="S", ticker="T", close_ts=1_787_000_000,
                                 candles=[{"end_period_ts": "not-an-int"}])


def test_no_candles_is_not_a_firing_verdict():
    result = classify.classify_market(
        series="S", ticker="T", close_ts=1_787_000_000, candles=[]
    )
    assert result.is_firing is False
    assert "no candles" in result.not_firing_reason
