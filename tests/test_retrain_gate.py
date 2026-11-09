"""
Tests for the champion/challenger gate in kbot.learning.retrain.

Required by spec:
  1. A challenger that does NOT beat the champion on validation is rejected.
  2. A challenger that does beat the champion (by >= threshold) is promoted.
  3. Promotion is logged (checked via registry state).
"""

from __future__ import annotations

import pytest

from kbot.config import Settings
from kbot.learning.registry import ModelRegistry
from kbot.learning.retrain import (
    apply_kelly_bounds,
    build_candidate_params,
    run_champion_challenger,
    walk_forward_evaluate,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg(**kw) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kelly_fraction=kw.get("kelly_fraction", 0.25),
        kelly_fraction_min=kw.get("kelly_fraction_min", 0.10),
        kelly_fraction_max=kw.get("kelly_fraction_max", 0.35),
        min_net_edge=kw.get("min_net_edge", 0.04),
        fee_coefficient=0.07,
        assumed_slippage=0.005,
        promotion_min_improvement=kw.get("promotion_min_improvement", 0.0),
        calibration_min_samples=kw.get("calibration_min_samples", 9999),
    )


def _make_outcome(
    pnl: float,
    edge: float = 0.06,
    kelly: float = 0.25,
    model_prob: float = 0.60,
    entry_price: float = 0.55,
) -> dict:
    return {
        "pnl_dollars":         pnl,
        "net_edge_dollars":    edge,
        "kelly_fraction_used": kelly,
        "model_prob":          model_prob,
        "entry_price":         entry_price,
    }


def _registry() -> ModelRegistry:
    return ModelRegistry(db_path=":memory:")


# ── Challenger rejected when not better ──────────────────────────────────────

class TestChallengerRejected:
    """
    Challenger is rejected when its walk-forward score does not exceed
    the champion score (or does not exceed it by the required margin).
    """

    def test_rejected_when_improvement_below_threshold(self):
        """
        Champion kelly=0.25, all outcomes profitable at that fraction.
        Set promotion_min_improvement so the typical ~20% improvement
        from kelly=0.30 doesn't clear the bar.

        With pnl=0.05/trade and champion score ≈ 0.05, the kelly=0.30
        challenger scores ≈ 0.06, improvement ≈ 0.01.
        Setting threshold=0.10 forces "keep".
        """
        cfg = _cfg(promotion_min_improvement=0.10)
        outcomes = [_make_outcome(pnl=0.05, kelly=0.25)] * 50

        reg = _registry()
        reg.save_version("champ", {"kelly_fraction": 0.25, "min_net_edge": 0.04},
                         None, {}, is_champion=True)

        result = run_champion_challenger(outcomes, reg, cfg=cfg)
        assert result["decision"] == "keep"
        assert result["promoted_params"] is None

    def test_rejected_when_challenger_strictly_worse(self):
        """
        Challenger with lower edge threshold takes extra losing trades,
        making its per-trade score lower than the champion's.

        Champion (edge>=0.04): only sees the 0.06-edge profitable trades.
        Challenger (edge>=0.03): also sees the 0.03-edge losing trades.

        kelly_fraction is capped at 0.25 so no higher-kelly candidate can
        mask the effect — only edge variations are evaluated.
        """
        cfg = _cfg(
            kelly_fraction_max=0.25,   # cap kelly so candidates only vary edge
            promotion_min_improvement=0.0,
        )
        # Good trades (edge=0.06, profitable): taken by champion and strict challengers
        good = [_make_outcome(pnl=+0.40, edge=0.06, kelly=0.25)] * 30
        # Bad trades (edge=0.03, losing): taken by challenger with edge>=0.03
        bad  = [_make_outcome(pnl=-0.80, edge=0.03, kelly=0.25)] * 30
        outcomes = good + bad

        reg = _registry()
        reg.save_version("champ", {"kelly_fraction": 0.25, "min_net_edge": 0.04},
                         None, {}, is_champion=True)

        result = run_champion_challenger(outcomes, reg, cfg=cfg)
        assert result["decision"] == "keep"

    def test_insufficient_data_returns_keep(self):
        """Too few outcomes for walk_forward → -inf → keep."""
        cfg      = _cfg()
        outcomes = [_make_outcome(0.10)] * 5   # below n_splits * 2 = 10
        reg      = _registry()
        result   = run_champion_challenger(outcomes, reg, cfg=cfg)
        assert result["decision"] == "keep"


# ── Challenger promoted when better ──────────────────────────────────────────

class TestChallengerPromoted:
    """
    Challenger is promoted when its walk-forward score beats the champion
    by at least promotion_min_improvement.
    """

    def test_promoted_when_improvement_exceeds_threshold(self):
        """
        Higher kelly_fraction produces proportionally higher P&L per trade.

        kelly=0.25 → score ≈ 0.40/trade
        kelly=0.30 → score ≈ 0.40 * (0.30/0.25) = 0.48/trade
        Improvement ≈ 0.08 >> threshold=0.0 → PROMOTE.
        """
        cfg      = _cfg(promotion_min_improvement=0.0)
        outcomes = [_make_outcome(pnl=0.40, edge=0.06, kelly=0.25)] * 50

        reg = _registry()
        reg.save_version("champ", {"kelly_fraction": 0.25, "min_net_edge": 0.04},
                         None, {}, is_champion=True)

        result = run_champion_challenger(outcomes, reg, cfg=cfg)
        assert result["decision"] == "promote"
        assert result["promoted_params"] is not None

    def test_promoted_params_have_higher_score(self):
        cfg      = _cfg(promotion_min_improvement=0.0)
        outcomes = [_make_outcome(pnl=0.40, edge=0.06, kelly=0.25)] * 50

        reg = _registry()
        reg.save_version("champ", {"kelly_fraction": 0.25, "min_net_edge": 0.04},
                         None, {}, is_champion=True)

        result = run_champion_challenger(outcomes, reg, cfg=cfg)
        assert result["challenger_score"] > result["champion_score"]

    def test_promoted_kelly_within_bounds(self):
        """The promoted params must respect the kelly bounds."""
        cfg      = _cfg(
            kelly_fraction_min=0.10,
            kelly_fraction_max=0.35,
            promotion_min_improvement=0.0,
        )
        outcomes = [_make_outcome(pnl=0.40, edge=0.06, kelly=0.25)] * 50

        reg = _registry()
        reg.save_version("champ", {"kelly_fraction": 0.25, "min_net_edge": 0.04},
                         None, {}, is_champion=True)

        result = run_champion_challenger(outcomes, reg, cfg=cfg)
        if result["decision"] == "promote":
            kf = result["promoted_params"]["kelly_fraction"]
            assert cfg.kelly_fraction_min <= kf <= cfg.kelly_fraction_max


# ── Promotion is logged (registry state) ─────────────────────────────────────

class TestPromotionLogged:
    def test_promotion_creates_new_champion_in_registry(self):
        cfg      = _cfg(promotion_min_improvement=0.0)
        outcomes = [_make_outcome(pnl=0.40, edge=0.06, kelly=0.25)] * 50

        reg = _registry()
        reg.save_version("baseline", {"kelly_fraction": 0.25, "min_net_edge": 0.04},
                         None, {}, is_champion=True)

        result = run_champion_challenger(outcomes, reg, cfg=cfg)

        if result["decision"] == "promote":
            champ = reg.get_champion()
            assert champ is not None
            assert champ.params == result["promoted_params"]
            assert champ.is_champion
            # Metrics must be persisted
            assert "champion_score" in champ.metrics
            assert "challenger_score" in champ.metrics

    def test_rejection_leaves_champion_unchanged(self):
        cfg      = _cfg(promotion_min_improvement=100.0)  # unreachable threshold
        outcomes = [_make_outcome(pnl=0.40, edge=0.06, kelly=0.25)] * 50
        champ_params = {"kelly_fraction": 0.25, "min_net_edge": 0.04}

        reg = _registry()
        reg.save_version("baseline", champ_params, None, {}, is_champion=True)

        run_champion_challenger(outcomes, reg, cfg=cfg)

        champ = reg.get_champion()
        assert champ is not None
        assert champ.params == champ_params

    def test_baseline_preserved_after_promotion(self):
        """The immutable baseline must survive a promotion."""
        cfg      = _cfg(promotion_min_improvement=0.0)
        outcomes = [_make_outcome(pnl=0.40, edge=0.06, kelly=0.25)] * 50

        reg = _registry()
        reg.save_version("v1", {"kelly_fraction": 0.25, "min_net_edge": 0.04},
                         None, {}, is_champion=True)
        baseline_before = reg.get_baseline()
        assert baseline_before is not None

        run_champion_challenger(outcomes, reg, cfg=cfg)

        baseline_after = reg.get_baseline()
        assert baseline_after is not None
        assert baseline_after.id == baseline_before.id


# ── build_candidate_params ────────────────────────────────────────────────────

class TestBuildCandidateParams:
    def test_all_kelly_within_bounds(self):
        cfg        = _cfg(kelly_fraction_min=0.10, kelly_fraction_max=0.35)
        candidates = build_candidate_params({"kelly_fraction": 0.25, "min_net_edge": 0.04}, cfg=cfg)
        for c in candidates:
            assert cfg.kelly_fraction_min <= c["kelly_fraction"] <= cfg.kelly_fraction_max

    def test_champion_params_excluded(self):
        champ      = {"kelly_fraction": 0.25, "min_net_edge": 0.04}
        candidates = build_candidate_params(champ)
        for c in candidates:
            assert c != champ, "Champion's exact params should not appear in candidates"

    def test_at_max_bound_does_not_exceed(self):
        cfg        = _cfg(kelly_fraction_max=0.30)
        candidates = build_candidate_params({"kelly_fraction": 0.30, "min_net_edge": 0.04}, cfg=cfg)
        for c in candidates:
            assert c["kelly_fraction"] <= 0.30

    def test_at_min_bound_does_not_go_below(self):
        cfg        = _cfg(kelly_fraction_min=0.10)
        candidates = build_candidate_params({"kelly_fraction": 0.10, "min_net_edge": 0.04}, cfg=cfg)
        for c in candidates:
            assert c["kelly_fraction"] >= 0.10

    def test_no_duplicate_candidates(self):
        candidates = build_candidate_params({"kelly_fraction": 0.25, "min_net_edge": 0.04})
        keys = [(c["kelly_fraction"], c["min_net_edge"]) for c in candidates]
        assert len(keys) == len(set(keys))


# ── walk_forward_evaluate ─────────────────────────────────────────────────────

class TestWalkForwardEvaluate:
    def test_returns_neg_inf_on_too_few_samples(self):
        params   = {"kelly_fraction": 0.25, "min_net_edge": 0.04}
        outcomes = [_make_outcome(0.10)] * 5
        score    = walk_forward_evaluate(params, outcomes, n_splits=5)
        assert score == float("-inf")

    def test_higher_kelly_scores_higher_on_profitable_trades(self):
        cfg      = _cfg(calibration_min_samples=9999)  # suppress calibration
        outcomes = [_make_outcome(pnl=0.40, edge=0.06, kelly=0.25)] * 50
        low_k    = {"kelly_fraction": 0.20, "min_net_edge": 0.04}
        high_k   = {"kelly_fraction": 0.30, "min_net_edge": 0.04}
        score_lo = walk_forward_evaluate(low_k,  outcomes, cfg=cfg)
        score_hi = walk_forward_evaluate(high_k, outcomes, cfg=cfg)
        assert score_hi > score_lo

    def test_stricter_edge_threshold_removes_losers(self):
        """Higher min_net_edge filters out marginal losers → better per-trade score."""
        cfg      = _cfg(calibration_min_samples=9999)
        # Mix: good trades (edge=0.06) and marginal losers (edge=0.03)
        good     = [_make_outcome(pnl=+0.40, edge=0.06, kelly=0.25)] * 30
        bad      = [_make_outcome(pnl=-0.30, edge=0.03, kelly=0.25)] * 20
        outcomes = good + bad
        strict   = {"kelly_fraction": 0.25, "min_net_edge": 0.04}  # skips bad
        loose    = {"kelly_fraction": 0.25, "min_net_edge": 0.02}  # takes bad too
        score_s  = walk_forward_evaluate(strict, outcomes, cfg=cfg)
        score_l  = walk_forward_evaluate(loose,  outcomes, cfg=cfg)
        assert score_s > score_l
