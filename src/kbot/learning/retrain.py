"""
Champion/challenger retraining with walk-forward validation.

"Self-learning" design constraints enforced here:
  - kelly_fraction may only be tuned within [kelly_fraction_min, kelly_fraction_max].
  - kelly_fraction is NEVER raised as a reaction to a winning streak.
    The only path to a higher kelly is a validated champion/challenger promotion.
  - A challenger is only promoted if its walk-forward validation score beats
    the champion's by >= PROMOTION_MIN_IMPROVEMENT.
  - Retrain is triggered manually (or on a deliberate weekly schedule),
    not automatically in response to recent P&L.

Walk-forward evaluation logic:
  Split historical outcomes chronologically into n_splits windows.
  For each window i: fit calibrator on data before window i, then score
  the candidate params on window i using calibrated probabilities.
  Return the mean score across windows.  Never train and evaluate on the
  same data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.learning.calibration import apply_calibrator, fit_calibrator

log = structlog.get_logger(__name__)


# ── Kelly bounds ──────────────────────────────────────────────────────────────

def apply_kelly_bounds(fraction: float, cfg: Settings | None = None) -> float:
    """Clamp kelly_fraction to the safe [min, max] range from config."""
    cfg = cfg or _default_settings
    return max(cfg.kelly_fraction_min, min(cfg.kelly_fraction_max, fraction))


# ── Candidate generation ──────────────────────────────────────────────────────

def build_candidate_params(
    champion_params: dict,
    cfg: Settings | None = None,
) -> list[dict]:
    """
    Build a grid of candidate parameter sets near the champion.

    Varies kelly_fraction (±0.05) and min_net_edge (±0.01).
    All kelly values are clamped to [kelly_fraction_min, kelly_fraction_max].
    The champion's exact params are excluded — they are evaluated separately
    as the baseline score.
    """
    cfg = cfg or _default_settings
    base_kelly = champion_params.get("kelly_fraction", cfg.kelly_fraction)
    base_edge  = champion_params.get("min_net_edge",   cfg.min_net_edge)

    candidates: list[dict] = []
    seen: set[tuple] = set()

    for dk in (-0.05, 0.0, 0.05):
        kelly = apply_kelly_bounds(base_kelly + dk, cfg)
        for de in (-0.01, 0.0, 0.01):
            edge  = round(max(0.01, base_edge + de), 4)
            key   = (kelly, edge)
            champ_key = (
                apply_kelly_bounds(base_kelly, cfg),
                round(max(0.01, base_edge), 4),
            )
            if key in seen or key == champ_key:
                continue
            seen.add(key)
            candidates.append({"kelly_fraction": kelly, "min_net_edge": edge})

    return candidates


# ── Walk-forward evaluation ───────────────────────────────────────────────────

def _score_window(
    params: dict,
    outcomes: list[dict],
    calibrator: Any,
    cfg: Settings,
) -> float:
    """Mean scaled P&L per accepted trade in one validation window."""
    total_pnl = 0.0
    n_taken   = 0

    for trade in outcomes:
        # raw_fair_p is the PRE-calibration model probability -- the domain
        # the calibrator is applied to at trade time. model_prob (the
        # calibrated output) is only a fallback for legacy rows recorded
        # before raw_fair_p existed.
        raw_prob   = trade.get("raw_fair_p", trade.get("model_prob", 0.5))
        calibrated = apply_calibrator(calibrator, raw_prob)
        cal_delta  = calibrated - raw_prob   # 0.0 when calibrator is None

        if "net_edge_dollars" in trade:
            # Use stored edge (computed at trade time with actual fees)
            # and shift it by any calibration correction.
            net_edge = trade["net_edge_dollars"] + cal_delta
        else:
            # Fallback: recompute from stored probabilities and price.
            market_price = trade.get("entry_price", raw_prob)
            fee          = cfg.fee_coefficient * market_price * (1.0 - market_price)
            net_edge     = calibrated - market_price - fee - cfg.assumed_slippage

        if net_edge < params.get("min_net_edge", cfg.min_net_edge):
            continue

        # Scale historical P&L to reflect the new kelly_fraction
        orig_pnl   = trade.get("pnl_dollars", 0.0)
        orig_kelly = trade.get("kelly_fraction_used", cfg.kelly_fraction)
        new_kelly  = params.get("kelly_fraction", cfg.kelly_fraction)
        scaled     = orig_pnl * (new_kelly / orig_kelly) if orig_kelly > 0 else orig_pnl

        total_pnl += scaled
        n_taken   += 1

    return total_pnl / n_taken if n_taken > 0 else 0.0


def walk_forward_evaluate(
    params: dict,
    outcomes: list[dict],
    cfg: Settings | None = None,
    n_splits: int = 5,
) -> float:
    """
    Walk-forward evaluation of candidate params.

    For each window i:
      - Train: fit calibrator on outcomes before window i.
      - Validate: score params on window i using the fitted calibrator.
    Returns the mean validation score.  Returns -inf if too few samples.

    Never evaluates on the training data — each window is held out.
    """
    cfg = cfg or _default_settings
    if len(outcomes) < n_splits * 2:
        return float("-inf")

    window_size = len(outcomes) // n_splits
    scores: list[float] = []

    for i in range(1, n_splits + 1):
        val_start = (i - 1) * window_size
        val_end   = val_start + window_size if i < n_splits else len(outcomes)

        train_outcomes = outcomes[:val_start]
        val_outcomes   = outcomes[val_start:val_end]

        calibrator = None
        if len(train_outcomes) >= cfg.calibration_min_samples:
            # Fit on the raw (pre-calibration) probability -- the quantity
            # the calibrator is applied to live. Fitting on model_prob (the
            # calibrated output) while applying to raw inputs compounds a
            # domain mismatch every retrain generation.
            train_probs = [o.get("raw_fair_p", o.get("model_prob", 0.5)) for o in train_outcomes]
            train_won   = [1 if o.get("pnl_dollars", 0) > 0 else 0 for o in train_outcomes]
            calibrator  = fit_calibrator(train_probs, train_won, cfg=cfg)

        scores.append(_score_window(params, val_outcomes, calibrator, cfg))

    return float(np.mean(scores))


# ── Champion / challenger ─────────────────────────────────────────────────────

def run_champion_challenger(
    outcomes: list[dict],
    registry: Any,
    cfg: Settings | None = None,
) -> dict:
    """
    Evaluate challenger parameter sets against the current champion.

    Promotion rules (all must hold):
      - Best challenger walk-forward score > champion score.
      - Improvement >= cfg.promotion_min_improvement.
      - Walk-forward returned a valid score (not -inf).
      - (Optional) Human ack in live mode — TODO: wire to CLI flag.

    Every decision (promote OR keep) is logged and the champion state in
    the registry is updated on promotion.

    Returns:
      {"decision": "promote"|"keep",
       "champion_score": float,
       "challenger_score": float,
       "promoted_params": dict|None}
    """
    cfg = cfg or _default_settings

    champion = registry.get_champion()
    champion_params: dict = (
        champion.params if champion is not None
        else {"kelly_fraction": cfg.kelly_fraction, "min_net_edge": cfg.min_net_edge}
    )

    champion_score = walk_forward_evaluate(champion_params, outcomes, cfg=cfg)
    candidates     = build_candidate_params(champion_params, cfg=cfg)

    best_score     = float("-inf")
    best_params    = None

    for cand in candidates:
        score = walk_forward_evaluate(cand, outcomes, cfg=cfg)
        if score > best_score:
            best_score  = score
            best_params = cand

    improvement = best_score - champion_score
    valid_scores = (
        best_score  > float("-inf")
        and champion_score > float("-inf")
    )
    should_promote = (
        valid_scores
        and best_params is not None
        and best_score > champion_score          # challenger must strictly beat champion
        and improvement >= cfg.promotion_min_improvement
    )

    if should_promote:
        # Fit calibrator on all available outcomes for the promoted version.
        # Raw (pre-calibration) probs, same reasoning as walk_forward_evaluate.
        calibrator = None
        if len(outcomes) >= cfg.calibration_min_samples:
            probs = [o.get("raw_fair_p", o.get("model_prob", 0.5)) for o in outcomes]
            won   = [1 if o.get("pnl_dollars", 0) > 0 else 0 for o in outcomes]
            calibrator = fit_calibrator(probs, won, cfg=cfg)

        tag = f"champ_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        registry.save_version(
            tag=tag,
            params=best_params,
            calibrator=calibrator,
            metrics={
                "champion_score":  round(champion_score, 6),
                "challenger_score": round(best_score, 6),
                "improvement":     round(improvement, 6),
                "n_outcomes":      len(outcomes),
            },
            is_champion=True,
        )
        log.info(
            "retrain.promoted",
            tag=tag,
            params=best_params,
            champion_score=round(champion_score, 6),
            challenger_score=round(best_score, 6),
            improvement=round(improvement, 6),
        )
    else:
        log.info(
            "retrain.kept_champion",
            champion_params=champion_params,
            champion_score=round(champion_score, 6) if valid_scores else "insufficient_data",
            best_challenger_score=round(best_score, 6) if valid_scores else "insufficient_data",
            improvement=round(improvement, 6) if valid_scores else "N/A",
            reason=(
                "insufficient_data" if not valid_scores
                else "improvement_below_threshold"
            ),
        )

    return {
        "decision":        "promote" if should_promote else "keep",
        "champion_score":  champion_score,
        "challenger_score": best_score,
        "promoted_params": best_params if should_promote else None,
    }


# ── Kelly adapter ─────────────────────────────────────────────────────────────

class KellyAdapter:
    """
    Runtime kelly_fraction manager.

    INVARIANT: a winning streak NEVER raises the fraction automatically.
    The only path to a higher kelly_fraction is a validated promotion via
    run_champion_challenger → registry → set_from_promotion().
    Drawdown and consecutive losses always tighten; wins reset the loss
    counter but do not widen.
    """

    def __init__(self, initial_fraction: float, cfg: Settings | None = None) -> None:
        self._cfg      = cfg or _default_settings
        self._fraction = apply_kelly_bounds(initial_fraction, self._cfg)
        self._consec_losses = 0

    @property
    def current_fraction(self) -> float:
        return self._fraction

    def on_win(self) -> float:
        """Win: reset loss counter.  Fraction is NOT increased."""
        self._consec_losses = 0
        return self._fraction

    def on_loss(self) -> float:
        """Consecutive loss: tighten fraction by 5% per loss."""
        self._consec_losses += 1
        reduction = 0.95 ** self._consec_losses
        self._fraction = max(
            self._cfg.kelly_fraction_min,
            self._fraction * reduction,
        )
        return self._fraction

    def on_drawdown(self, drawdown_pct: float) -> float:
        """Proportional tightening on drawdown; ignores zero or negative."""
        if drawdown_pct <= 0.0:
            return self._fraction
        factor = 1.0 - min(drawdown_pct, 0.9)
        self._fraction = max(
            self._cfg.kelly_fraction_min,
            self._fraction * factor,
        )
        return self._fraction

    def set_from_promotion(self, new_fraction: float) -> float:
        """
        Apply a new fraction that has passed the walk-forward validation gate.

        This is the ONLY way the fraction can increase.
        """
        self._fraction      = apply_kelly_bounds(new_fraction, self._cfg)
        self._consec_losses = 0
        return self._fraction
