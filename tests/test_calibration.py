"""
Tests for kbot.learning.calibration.

Required by spec:
  1. On synthetic data where raw probs are 0.1 too high, the fitted isotonic
     calibrator produces a lower Brier score than the raw probs.
  2. fit_calibrator() returns None when n < CALIBRATION_MIN_SAMPLES.
"""

from __future__ import annotations

import numpy as np
import pytest

from kbot.config import Settings
from kbot.learning.calibration import (
    apply_calibrator,
    brier_score,
    fit_calibrator,
    reliability_table,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg(**kw) -> Settings:
    return Settings(
        kalshi_key_id="test",
        calibration_min_samples=kw.get("calibration_min_samples", 50),
    )


# ── brier_score ───────────────────────────────────────────────────────────────

class TestBrierScore:
    def test_perfect_predictions(self):
        preds    = [0.0, 1.0, 0.0, 1.0]
        outcomes = [0,   1,   0,   1]
        assert brier_score(preds, outcomes) == 0.0

    def test_naive_half_predictor(self):
        # p=0.5 everywhere → BS = 0.25 regardless of outcomes
        preds    = [0.5] * 100
        outcomes = ([1] * 50) + ([0] * 50)
        assert abs(brier_score(preds, outcomes) - 0.25) < 1e-9

    def test_worst_predictions(self):
        preds    = [1.0, 0.0]
        outcomes = [0,   1]
        assert brier_score(preds, outcomes) == 1.0

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            brier_score([], [])

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            brier_score([0.5, 0.5], [1])


# ── fit_calibrator: minimum-sample gate ──────────────────────────────────────

class TestFitCalibratorMinSamples:
    def test_returns_none_below_threshold(self):
        cfg    = _cfg(calibration_min_samples=50)
        preds  = [0.6] * 49
        won    = [1]   * 49
        result = fit_calibrator(preds, won, cfg=cfg)
        assert result is None

    def test_returns_none_at_threshold_minus_one(self):
        cfg = _cfg(calibration_min_samples=10)
        assert fit_calibrator([0.5] * 9, [1] * 9, cfg=cfg) is None

    def test_fits_at_threshold(self):
        cfg   = _cfg(calibration_min_samples=10)
        probs = [0.6] * 5 + [0.4] * 5
        won   = [1]   * 5 + [0]   * 5
        cal   = fit_calibrator(probs, won, cfg=cfg)
        assert cal is not None

    def test_fits_above_threshold(self):
        cfg   = _cfg(calibration_min_samples=50)
        probs = np.random.default_rng(0).uniform(0.3, 0.8, 100).tolist()
        won   = [1 if p > 0.5 else 0 for p in probs]
        assert fit_calibrator(probs, won, cfg=cfg) is not None


# ── Calibration reduces Brier on miscalibrated data ──────────────────────────

class TestCalibrationReducesBrier:
    """
    Core spec requirement: on data where raw probs are ~0.1 too high,
    the fitted isotonic calibrator produces a lower Brier score.
    """

    def _make_data(self, n: int = 300, bias: float = 0.10, seed: int = 42):
        rng        = np.random.default_rng(seed)
        true_probs = rng.uniform(0.2, 0.75, n)
        outcomes   = rng.binomial(1, true_probs).tolist()
        raw_probs  = np.clip(true_probs + bias, 0.0, 1.0).tolist()
        return raw_probs, outcomes

    def test_calibrator_lowers_brier(self):
        raw_probs, outcomes = self._make_data(n=300, bias=0.10)
        cfg = _cfg(calibration_min_samples=50)

        raw_bs = brier_score(raw_probs, outcomes)
        cal    = fit_calibrator(raw_probs, outcomes, cfg=cfg)

        assert cal is not None, "Expected calibrator to be fitted"

        calibrated = [apply_calibrator(cal, p) for p in raw_probs]
        cal_bs     = brier_score(calibrated, outcomes)

        assert cal_bs < raw_bs, (
            f"Calibrated Brier ({cal_bs:.6f}) should be below "
            f"raw Brier ({raw_bs:.6f})"
        )

    def test_improvement_is_meaningful(self):
        """At least 5% relative improvement on data with systematic bias."""
        raw_probs, outcomes = self._make_data(n=500, bias=0.12, seed=7)
        cfg = _cfg(calibration_min_samples=50)

        raw_bs = brier_score(raw_probs, outcomes)
        cal    = fit_calibrator(raw_probs, outcomes, cfg=cfg)
        calibrated = [apply_calibrator(cal, p) for p in raw_probs]
        cal_bs = brier_score(calibrated, outcomes)

        relative_improvement = (raw_bs - cal_bs) / raw_bs
        assert relative_improvement >= 0.05, (
            f"Expected ≥5% Brier improvement, got {relative_improvement:.1%}"
        )

    def test_negative_bias_also_corrected(self):
        """Calibrator fixes underconfidence (probs too low) too."""
        raw_probs, outcomes = self._make_data(n=300, bias=-0.10)
        cfg = _cfg(calibration_min_samples=50)

        raw_bs = brier_score(raw_probs, outcomes)
        cal    = fit_calibrator(raw_probs, outcomes, cfg=cfg)
        calibrated = [apply_calibrator(cal, p) for p in raw_probs]
        cal_bs = brier_score(calibrated, outcomes)

        assert cal_bs < raw_bs


# ── Bootstrap: calibration generalizes to a held-out (out-of-sample) set ──────

class TestBootstrapCalibrationGeneralizes:
    """
    The 2026-07 KXBTC15M bootstrap methodology: fit the isotonic calibrator
    on a TIME-ORDERED train split and prove it lowers Brier on a HELD-OUT
    validate split it never saw -- the honest generalization claim, not the
    in-sample fit the tests above check. Mirrors scripts' bootstrap: split
    70/30 in time order (never shuffle a time series), fit on train, evaluate
    on validate.
    """

    def _make_biased_series(self, n: int = 1600, bias: float = 0.15, seed: int = 11):
        # A stationary systematically-overconfident series (raw probs run
        # `bias` too high), like the KXBTC15M low-fair_p miscalibration.
        rng        = np.random.default_rng(seed)
        true_probs = rng.uniform(0.2, 0.75, n)
        outcomes   = rng.binomial(1, true_probs).tolist()
        raw_probs  = np.clip(true_probs + bias, 0.0, 1.0).tolist()
        return raw_probs, outcomes

    def test_calibration_lowers_brier_on_held_out_validate_set(self):
        raw_probs, outcomes = self._make_biased_series()
        cfg = _cfg(calibration_min_samples=50)

        split = int(len(raw_probs) * 0.70)  # time-ordered 70/30, no shuffle
        train_p, val_p = raw_probs[:split], raw_probs[split:]
        train_o, val_o = outcomes[:split], outcomes[split:]

        cal = fit_calibrator(train_p, train_o, cfg=cfg)
        assert cal is not None

        # Calibrator is fit ONLY on train; validate is genuinely out-of-sample.
        val_raw_brier = brier_score(val_p, val_o)
        val_cal_brier = brier_score([apply_calibrator(cal, p) for p in val_p], val_o)

        assert val_cal_brier < val_raw_brier, (
            f"Calibrated validate Brier ({val_cal_brier:.6f}) should beat raw "
            f"({val_raw_brier:.6f}) on the held-out set"
        )

    def test_held_out_improvement_is_meaningful(self):
        raw_probs, outcomes = self._make_biased_series(n=1600, bias=0.15, seed=3)
        cfg = _cfg(calibration_min_samples=50)

        split = int(len(raw_probs) * 0.70)
        cal = fit_calibrator(raw_probs[:split], outcomes[:split], cfg=cfg)

        val_p, val_o = raw_probs[split:], outcomes[split:]
        val_raw = brier_score(val_p, val_o)
        val_cal = brier_score([apply_calibrator(cal, p) for p in val_p], val_o)

        rel = (val_raw - val_cal) / val_raw
        assert rel >= 0.05, f"Expected >=5% held-out Brier improvement, got {rel:.1%}"


# ── apply_calibrator ──────────────────────────────────────────────────────────

class TestApplyCalibrator:
    def test_none_calibrator_returns_raw(self):
        assert apply_calibrator(None, 0.65) == 0.65

    def test_output_in_unit_interval(self):
        rng    = np.random.default_rng(1)
        probs  = rng.uniform(0.2, 0.8, 100).tolist()
        won    = [1 if p > 0.5 else 0 for p in probs]
        cfg    = _cfg(calibration_min_samples=50)
        cal    = fit_calibrator(probs, won, cfg=cfg)

        for p in np.linspace(0.0, 1.0, 21).tolist():
            result = apply_calibrator(cal, p)
            assert 0.0 <= result <= 1.0, f"Out-of-range for input {p}: {result}"

    def test_monotone_non_decreasing(self):
        """Isotonic calibrator must be monotone."""
        rng   = np.random.default_rng(2)
        probs = sorted(rng.uniform(0.2, 0.8, 200).tolist())
        won   = [1 if p > 0.5 else 0 for p in probs]
        cfg   = _cfg(calibration_min_samples=50)
        cal   = fit_calibrator(probs, won, cfg=cfg)

        prev = -1.0
        for p in np.linspace(0.01, 0.99, 50).tolist():
            c = apply_calibrator(cal, p)
            assert c >= prev - 1e-9, f"Calibrator not monotone at {p}: {c} < {prev}"
            prev = c


# ── reliability_table ─────────────────────────────────────────────────────────

class TestReliabilityTable:
    def test_returns_correct_bucket_count(self):
        preds    = list(np.linspace(0.05, 0.95, 100))
        outcomes = [1 if p > 0.5 else 0 for p in preds]
        rows     = reliability_table(preds, outcomes, n_buckets=10)
        assert 1 <= len(rows) <= 10

    def test_count_sums_to_total(self):
        preds    = list(np.linspace(0.0, 1.0, 200))
        outcomes = [1] * 100 + [0] * 100
        rows     = reliability_table(preds, outcomes, n_buckets=10)
        assert sum(r["count"] for r in rows) == 200

    def test_realized_win_rate_in_range(self):
        rng      = np.random.default_rng(3)
        preds    = rng.uniform(0.1, 0.9, 100).tolist()
        outcomes = rng.binomial(1, preds).tolist()
        rows     = reliability_table(preds, outcomes)
        for r in rows:
            assert 0.0 <= r["realized_win_rate"] <= 1.0

    def test_empty_returns_empty(self):
        assert reliability_table([], []) == []
