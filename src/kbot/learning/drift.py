"""
Model drift and regime-change detection.

On drift: stand down (pause new positions) and alert, rather than
"learning" through chaos.  Standing down is a valid, safe response.

Two signals:
  1. Rolling Brier score degradation — model's probability estimates are
     getting worse relative to outcomes.
  2. Realized volatility regime change — the asset is behaving very
     differently from when the strategy was calibrated.
"""

from __future__ import annotations

import numpy as np
import structlog

log = structlog.get_logger(__name__)


def rolling_brier(
    preds: list[float],
    outcomes: list[int],
    window: int = 50,
) -> list[float]:
    """
    Compute the rolling Brier score over a sliding window.

    Returns one score per position from index `window` onward, so the
    output list has len(preds) - window + 1 elements (or empty if too few).
    """
    if len(preds) < window:
        return []
    p = np.array(preds, dtype=float)
    o = np.array(outcomes, dtype=float)
    scores = []
    for i in range(window, len(p) + 1):
        bs = float(np.mean((p[i - window:i] - o[i - window:i]) ** 2))
        scores.append(bs)
    return scores


def detect_drift(
    preds: list[float],
    outcomes: list[int],
    window: int = 50,
    degradation_threshold: float = 0.05,
) -> bool:
    """
    Return True if the model shows signs of calibration drift.

    Drift is defined as: the most recent rolling Brier score exceeds the
    first rolling Brier score (the historical baseline window) by more than
    `degradation_threshold`.

    On True: caller should pause new positions and alert.
    """
    scores = rolling_brier(preds, outcomes, window=window)
    if len(scores) < 2:
        return False

    baseline_bs = scores[0]
    recent_bs   = scores[-1]
    drifted     = (recent_bs - baseline_bs) > degradation_threshold

    if drifted:
        log.warning(
            "drift.detected",
            baseline_brier=round(baseline_bs, 6),
            recent_brier=round(recent_bs, 6),
            degradation=round(recent_bs - baseline_bs, 6),
            threshold=degradation_threshold,
        )
    return drifted


def check_vol_regime(
    realized_vol: float,
    baseline_vol: float,
    multiplier: float = 2.0,
) -> bool:
    """
    Return True if realized volatility is more than `multiplier` × baseline.

    A vol regime change means the market is behaving very differently from
    when the model was calibrated — another reason to stand down.
    """
    if baseline_vol <= 0:
        return False
    outside = realized_vol > baseline_vol * multiplier
    if outside:
        log.warning(
            "drift.vol_regime_change",
            realized_vol=round(realized_vol, 4),
            baseline_vol=round(baseline_vol, 4),
            ratio=round(realized_vol / baseline_vol, 2),
            threshold_multiplier=multiplier,
        )
    return outside
