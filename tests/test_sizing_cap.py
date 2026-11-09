"""
Tests for KellyAdapter in kbot.learning.retrain.

Required by spec:
  - A winning streak does NOT raise kelly_fraction.
  - A drawdown lowers kelly_fraction.
  - Fraction always stays within [kelly_fraction_min, kelly_fraction_max].
"""

from __future__ import annotations

import pytest

from kbot.config import Settings
from kbot.learning.retrain import KellyAdapter, apply_kelly_bounds


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg(**kw) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kelly_fraction=kw.get("kelly_fraction", 0.25),
        kelly_fraction_min=kw.get("kelly_fraction_min", 0.10),
        kelly_fraction_max=kw.get("kelly_fraction_max", 0.35),
    )


def _adapter(initial: float = 0.25, **kw) -> KellyAdapter:
    return KellyAdapter(initial_fraction=initial, cfg=_cfg(**kw))


# ── Winning streak must NOT raise kelly_fraction ──────────────────────────────

class TestWinningStreakDoesNotRaise:
    """
    This is the core anti-footgun invariant.  A winning streak provides no
    automatic reward — that would be curve-fitting to luck.  The only path
    to a higher fraction is a validated promotion.
    """

    def test_single_win_does_not_raise(self):
        adapter  = _adapter(0.25)
        before   = adapter.current_fraction
        adapter.on_win()
        assert adapter.current_fraction == before

    def test_ten_consecutive_wins_do_not_raise(self):
        adapter  = _adapter(0.25)
        before   = adapter.current_fraction
        for _ in range(10):
            adapter.on_win()
        assert adapter.current_fraction == before

    def test_fifty_consecutive_wins_do_not_raise(self):
        adapter  = _adapter(0.25)
        before   = adapter.current_fraction
        for _ in range(50):
            adapter.on_win()
        assert adapter.current_fraction == before

    def test_win_after_loss_does_not_overshoot(self):
        """Win resets loss counter but does not restore fraction above original."""
        adapter  = _adapter(0.25)
        original = adapter.current_fraction
        adapter.on_loss()   # fraction decreases
        lowered  = adapter.current_fraction
        adapter.on_win()    # resets counter, but fraction stays at lowered value
        assert adapter.current_fraction == lowered
        assert adapter.current_fraction <= original


# ── Drawdown lowers kelly_fraction ───────────────────────────────────────────

class TestDrawdownLowersFraction:
    def test_drawdown_reduces_fraction(self):
        adapter = _adapter(0.25)
        before  = adapter.current_fraction
        adapter.on_drawdown(0.10)
        assert adapter.current_fraction < before

    def test_larger_drawdown_reduces_more(self):
        a1 = _adapter(0.25)
        a2 = _adapter(0.25)
        a1.on_drawdown(0.05)
        a2.on_drawdown(0.20)
        assert a2.current_fraction < a1.current_fraction

    def test_zero_drawdown_is_noop(self):
        adapter = _adapter(0.25)
        before  = adapter.current_fraction
        adapter.on_drawdown(0.0)
        assert adapter.current_fraction == before

    def test_negative_drawdown_is_noop(self):
        adapter = _adapter(0.25)
        before  = adapter.current_fraction
        adapter.on_drawdown(-0.05)
        assert adapter.current_fraction == before

    def test_consecutive_losses_reduce_fraction(self):
        adapter = _adapter(0.25)
        before  = adapter.current_fraction
        for _ in range(5):
            adapter.on_loss()
        assert adapter.current_fraction < before


# ── Fraction stays within [min, max] ─────────────────────────────────────────

class TestFractionBounds:
    def test_fraction_never_below_min(self):
        adapter = _adapter(0.25, kelly_fraction_min=0.10)
        for _ in range(200):
            adapter.on_loss()
        assert adapter.current_fraction >= 0.10

    def test_fraction_never_below_min_on_large_drawdown(self):
        adapter = _adapter(0.25, kelly_fraction_min=0.10)
        adapter.on_drawdown(0.99)   # almost total loss
        assert adapter.current_fraction >= 0.10

    def test_initial_fraction_clamped_to_min(self):
        adapter = _adapter(initial=0.01, kelly_fraction_min=0.10)
        assert adapter.current_fraction >= 0.10

    def test_initial_fraction_clamped_to_max(self):
        adapter = _adapter(initial=0.99, kelly_fraction_max=0.35)
        assert adapter.current_fraction <= 0.35

    def test_set_from_promotion_clamped_to_max(self):
        adapter = _adapter(0.25, kelly_fraction_max=0.35)
        adapter.set_from_promotion(0.99)   # unreasonably high
        assert adapter.current_fraction <= 0.35

    def test_set_from_promotion_clamped_to_min(self):
        adapter = _adapter(0.25, kelly_fraction_min=0.10)
        adapter.set_from_promotion(0.01)   # unreasonably low
        assert adapter.current_fraction >= 0.10

    def test_fraction_bounds_maintained_throughout_mixed_history(self):
        adapter = _adapter(0.25, kelly_fraction_min=0.10, kelly_fraction_max=0.35)
        for i in range(100):
            if i % 3 == 0:
                adapter.on_win()
            elif i % 3 == 1:
                adapter.on_loss()
            else:
                adapter.on_drawdown(0.05)
            f = adapter.current_fraction
            assert 0.10 <= f <= 0.35, f"Fraction {f} out of bounds at step {i}"


# ── set_from_promotion: only path to increasing fraction ─────────────────────

class TestSetFromPromotion:
    def test_promotion_can_increase_fraction(self):
        """After losses, a promotion should restore or increase the fraction."""
        adapter = _adapter(0.25)
        for _ in range(10):
            adapter.on_loss()
        lowered = adapter.current_fraction
        adapter.set_from_promotion(0.30)
        assert adapter.current_fraction > lowered

    def test_promotion_resets_loss_counter(self):
        """Loss counter is reset so promotion doesn't carry past-loss penalty."""
        adapter = _adapter(0.25)
        for _ in range(5):
            adapter.on_loss()
        adapter.set_from_promotion(0.25)
        # Next loss should only apply a single-loss reduction, not 6
        fraction_after_one_loss = adapter.current_fraction * (0.95 ** 1)
        adapter.on_loss()
        assert adapter.current_fraction >= fraction_after_one_loss * 0.99  # small tolerance


# ── apply_kelly_bounds ────────────────────────────────────────────────────────

class TestApplyKellyBounds:
    def test_clamps_below_min(self):
        cfg = _cfg(kelly_fraction_min=0.10)
        assert apply_kelly_bounds(0.05, cfg) == 0.10

    def test_clamps_above_max(self):
        cfg = _cfg(kelly_fraction_max=0.35)
        assert apply_kelly_bounds(0.50, cfg) == 0.35

    def test_in_range_unchanged(self):
        cfg = _cfg(kelly_fraction_min=0.10, kelly_fraction_max=0.35)
        assert apply_kelly_bounds(0.25, cfg) == 0.25
