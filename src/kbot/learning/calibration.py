"""
Probability calibration — the core self-improvement mechanism.

An isotonic regression maps raw model probabilities → calibrated probabilities
that match observed win rates.  This is safe because it only reshapes
probabilities to match outcomes; it does not chase P&L or change position size.

The calibrator is fit on historical trade_outcome events and persisted in the
model registry so the live strategy always uses the current best calibration.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import structlog
from sklearn.isotonic import IsotonicRegression

from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)


# ── Brier score ───────────────────────────────────────────────────────────────

def brier_score(preds: list[float], outcomes: list[int]) -> float:
    """
    Mean squared error between predicted probabilities and binary outcomes.

    Lower is better.  A naive predictor at p=0.5 everywhere scores 0.25.
    """
    if not preds or len(preds) != len(outcomes):
        raise ValueError("preds and outcomes must be non-empty and same length")
    p = np.array(preds, dtype=float)
    o = np.array(outcomes, dtype=float)
    return float(np.mean((p - o) ** 2))


# ── Reliability table ─────────────────────────────────────────────────────────

def reliability_table(
    preds: list[float],
    outcomes: list[int],
    n_buckets: int = 10,
) -> list[dict]:
    """
    Split predictions into equal-width buckets and compute realized win rate.

    Returns a list of dicts, one per bucket:
      bucket_label, bucket_low, bucket_high, count,
      realized_win_rate, mean_pred, brier_contribution
    """
    if not preds:
        return []

    p = np.array(preds, dtype=float)
    o = np.array(outcomes, dtype=float)
    edges = np.linspace(0.0, 1.0, n_buckets + 1)
    rows = []

    for i in range(n_buckets):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi) if i < n_buckets - 1 else (p >= lo) & (p <= hi)
        n = int(mask.sum())
        if n == 0:
            continue
        b_preds = p[mask]
        b_out   = o[mask]
        realized = float(b_out.mean())
        brier_c  = float(np.mean((b_preds - b_out) ** 2))
        rows.append({
            "bucket_label":    f"{lo:.0%}–{hi:.0%}",
            "bucket_low":      round(float(lo), 4),
            "bucket_high":     round(float(hi), 4),
            "count":           n,
            "realized_win_rate": round(realized, 4),
            "mean_pred":       round(float(b_preds.mean()), 4),
            "brier_contribution": round(brier_c, 6),
        })
    return rows


# ── Calibrator fitting ────────────────────────────────────────────────────────

def fit_calibrator(
    preds: list[float],
    outcomes: list[int],
    cfg: Settings | None = None,
) -> IsotonicRegression | None:
    """
    Fit an isotonic regression calibrator on (preds, outcomes).

    Returns None if there are fewer than cfg.calibration_min_samples samples.
    The fitted calibrator can then be passed to apply_calibrator().
    """
    cfg = cfg or _default_settings
    if len(preds) < cfg.calibration_min_samples:
        log.info(
            "calibration.fit.skipped",
            n=len(preds),
            min_required=cfg.calibration_min_samples,
        )
        return None

    p = np.array(preds, dtype=float)
    o = np.array(outcomes, dtype=float)

    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(p, o)

    calibrated = calibrator.predict(p)
    raw_bs  = float(np.mean((p - o) ** 2))
    cal_bs  = float(np.mean((calibrated - o) ** 2))
    log.info(
        "calibration.fit.done",
        n=len(preds),
        raw_brier=round(raw_bs, 6),
        calibrated_brier=round(cal_bs, 6),
        improvement=round(raw_bs - cal_bs, 6),
    )
    return calibrator


def apply_calibrator(calibrator: Any, raw_prob: float) -> float:
    """
    Apply a fitted calibrator to a single raw probability.

    If the calibrator is None, returns raw_prob unchanged.
    Always returns a value in [0, 1].
    """
    if calibrator is None:
        return raw_prob
    try:
        result = float(calibrator.predict([raw_prob])[0])
        return max(0.0, min(1.0, result))
    except Exception as exc:  # noqa: BLE001
        log.warning("calibration.apply.failed", error=str(exc)[:100])
        return raw_prob
