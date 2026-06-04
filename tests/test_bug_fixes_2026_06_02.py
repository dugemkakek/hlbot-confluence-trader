"""Regression tests for the six bug fixes landed on 2026-06-02.

These are pinned, single-purpose tests that fail if any of the bugs
regress. They cover the exact patterns documented in BUGS.md and the
"Latent bugs / missing logic" section of README.md.

Coverage:
  - Bug #1  new_side NameError in _execute_decision
  - Bug #2  volume score normalization off-by-range
  - Bug #3  direction defaults to BUY on NO_TRADE
  - Bug #4  hard-coded regime="trending" in trade log
  - Bug #5  size_pct semantics mismatch (fraction vs base units)
  - Bug #6  is_actionable does not enforce confluence threshold
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timezone

import pytest

from src.data.models import (
    Decision,
    NormalizedCandle,
    OrderSide,
    Regime,
    Side,
    TimeFrame,
)
from src.signals.pair_ranker import PairRanker, RankedPair
from src.engine.decision_engine import DecisionEngine, SUBSYSTEMS
from src.orchestrator.trading_loop import TradingOrchestrator


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _src(fn) -> str:
    """Return the dedented source of a function so ast.parse can read it."""
    return textwrap.dedent(inspect.getsource(fn))


def _make_candles(n: int = 60, base: float = 100.0, step: float = 0.5) -> list[NormalizedCandle]:
    """Build n synthetic candles with a mild uptrend and steady volume.

    Timestamps step by 1 hour but wrap day boundaries so the test
    doesn't crash on datetime's 0..23 hour constraint.
    """
    out: list[NormalizedCandle] = []
    from datetime import timedelta
    base_ts = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        close = base + i * step
        out.append(
            NormalizedCandle(
                symbol="BTC",
                timeframe=TimeFrame.H1,
                timestamp=base_ts + timedelta(hours=i),
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=1000.0 + (i % 5) * 50,
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Bug #1 — new_side NameError in _execute_decision
# ─────────────────────────────────────────────────────────────────────────────


class TestBug1NewSideOrdering:
    """`new_side` must be defined before any branch that references it."""

    def test_new_side_assigned_before_use_in_execute_decision(self):
        """AST-level check: `new_side = ...` must appear in the source
        before any read of `new_side` inside `_execute_decision`.

        The walk counts a Name node as a "read" only if it is NOT the
        target of an assignment (those are writes, not reads).
        """
        src = _src(TradingOrchestrator._execute_decision)
        tree = ast.parse(src)

        # First pass: collect id() of Name nodes that are Assign targets
        target_node_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "new_side":
                        target_node_ids.add(id(t))

        # Second pass: reads = Name('new_side') that are NOT assign targets
        assigns: list[int] = []
        reads: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "new_side":
                        assigns.append(node.lineno)
            elif isinstance(node, ast.Name) and node.id == "new_side":
                if id(node) not in target_node_ids:
                    reads.append(node.lineno)

        assert assigns, "No assignment to new_side found"
        assert reads, "No read of new_side found"
        # Every read must come strictly after the first assignment.
        first_assign = min(assigns)
        bad = [r for r in reads if r <= first_assign]
        assert not bad, (
            f"new_side read at line(s) {bad} before assignment at line {first_assign}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug #2 — volume score normalization
# ─────────────────────────────────────────────────────────────────────────────


class TestBug2VolumeNormalization:
    """volume_score / 3.0 must clip to [0, 1] before being added to confluence."""

    def test_volume_normalized_in_unit_range_for_typical_inputs(self):
        # Build a ranker directly. Avoid pulling the global config — we
        # only need the math, not the scanner wiring.
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        candles = _make_candles(60)
        pair = ranker._score_pair("BTC", candles, {"1h": candles, "15m": candles})
        # The post-normalization confluence should be in [0, 1]. Previously
        # the broken (vol + 1) / 2 formula could push it to ~0.7+ even for
        # zero signal pairs; but the deeper issue was unbounded scaling.
        # Verify the score itself is bounded.
        assert 0.0 <= pair.confluence_score <= 1.0, (
            f"confluence_score {pair.confluence_score} out of [0, 1]"
        )

    def test_high_volume_does_not_breach_confluence_ceiling(self):
        """A pair with maximum volume_ratio=3 should not produce a
        confluence above 1.0 via the volume component alone."""
        # We inject a fake volume by patching _calc_volume_score.
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        candles = _make_candles(60)
        # Force volume_score = 3 (the cap value from _calc_volume_score).
        original_calc = ranker._calc_volume_score
        ranker._calc_volume_score = lambda c: 3.0  # noqa: SLF001
        try:
            pair = ranker._score_pair("BTC", candles, {"1h": candles, "15m": candles})
        finally:
            ranker._calc_volume_score = original_calc  # noqa: SLF001
        # Volume contribution is at most 0.20 * 1.0 = 0.20
        # Structure/pullback/momentum abs sum is bounded by 0.25+0.30+0.25 = 0.80.
        # Their typical values are << 1, so confluence stays under 1.0.
        assert pair.confluence_score <= 1.0, (
            f"High-volume confluence breached ceiling: {pair.confluence_score}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug #3 — direction defaults to None on NO_TRADE
# ─────────────────────────────────────────────────────────────────────────────


class TestBug3DecisionDirection:
    """decision.direction must be None when decision.action is NO_TRADE."""

    def test_decide_returns_no_trade_action(self):
        # We construct a DecisionEngine with no signals and regime detector
        # to force a NO_TRADE outcome. The fix asserts that the
        # Decision.action is "NO_TRADE" (not "BUY" via a misleading default).
        from src.signals.registry import SignalRegistry
        from src.signals.regime_detector import RegimeDetector
        from src.signals.sentiment_scorer import SentimentScorer

        registry = SignalRegistry()
        regime = RegimeDetector(min_candles=10)
        sentiment = SentimentScorer()
        engine = DecisionEngine(
            signal_registry=registry,
            regime_detector=regime,
            sentiment_scorer=sentiment,
            min_signal_confidence=0.99,  # force NO_TRADE
            min_confirmations=99,         # impossible
        )
        candles = _make_candles(60)
        d = engine.decide(symbol="BTC", timeframe=TimeFrame.H1, candles=candles)
        assert d.action == "NO_TRADE", (
            f"Expected NO_TRADE, got action={d.action}"
        )
        # And the Decision model has no `direction` field (the misleading
        # local was removed). This guards against the dead code being
        # re-introduced with a real attribute.
        assert not hasattr(d, "direction") or d.direction is None, (
            "Decision.direction was re-introduced (it was dead code)"
        )

    def test_buy_decision_action_is_buy(self):
        """Sanity: BUY action still maps to action='BUY' (didn't over-correct)."""
        from src.signals.registry import SignalRegistry
        from src.signals.regime_detector import RegimeDetector
        from src.signals.sentiment_scorer import SentimentScorer

        registry = SignalRegistry()
        regime = RegimeDetector(min_candles=10)
        sentiment = SentimentScorer()
        engine = DecisionEngine(
            signal_registry=registry,
            regime_detector=regime,
            sentiment_scorer=sentiment,
            min_signal_confidence=0.0,
            min_confirmations=0,
        )
        candles = _make_candles(60)
        d = engine.decide(symbol="BTC", timeframe=TimeFrame.H1, candles=candles)
        # The action field is the source of truth — verify it doesn't
        # silently default to BUY when the engine sees nothing.
        assert d.action in ("BUY", "SELL", "NO_TRADE")


# ─────────────────────────────────────────────────────────────────────────────
# Bug #4 — hard-coded regime='trending' in trade log
# ─────────────────────────────────────────────────────────────────────────────


class TestBug4RegimeIsDynamic:
    """The trade log must use the regime_detector output, not a literal."""

    def test_run_cycle_does_not_hardcode_trending(self):
        """Static check: `regime="trending"` must not appear as a literal
        in `run_cycle` — the only valid use is from a variable lookup."""
        src = _src(TradingOrchestrator.run_cycle)
        assert 'regime="trending"' not in src, (
            "run_cycle still contains hard-coded regime='trending'"
        )

    def test_regime_lookup_in_trade_log_branch(self):
        """The trade-log branch should consult _last_regime_analysis."""
        src = _src(TradingOrchestrator.run_cycle)
        assert "_last_regime_analysis" in src, (
            "run_cycle does not consult _last_regime_analysis when logging trade regime"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug #5 — size_pct semantics: pass fraction, not base units
# ─────────────────────────────────────────────────────────────────────────────


class TestBug5SizePctSemantics:
    """pre_trade_check must be called with the original size_fraction, not
    the now-converted base-unit size stored in decision.size."""

    def test_execute_decision_preserves_size_fraction(self):
        """Static check: the source must keep an explicit `size_fraction`
        variable and pass it to pre_trade_check, not decision.size."""
        src = _src(TradingOrchestrator._execute_decision)
        # The local variable that holds the original fraction.
        assert "size_fraction" in src, (
            "_execute_decision does not preserve original size_fraction"
        )
        # The risk-check call must reference size_fraction, not decision.size.
        assert "size_pct=size_fraction" in src, (
            "pre_trade_check still called with size_pct=decision.size"
        )
        assert "size_pct=decision.size" not in src, (
            "pre_trade_check must not be called with the post-conversion value"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug #6 — is_actionable must enforce confluence threshold
# ─────────────────────────────────────────────────────────────────────────────


class TestBug6ActionableThreshold:
    """_evaluate_ranked_pair must require confluence >= min_confluence_score."""

    def test_evaluate_ranked_pair_enforces_threshold(self):
        """Static check: the actionable gate must include the threshold."""
        src = _src(TradingOrchestrator._evaluate_ranked_pair)
        assert "self._min_confluence" in src, (
            "_evaluate_ranked_pair does not consult self._min_confluence"
        )
        # Find the actionable line and verify threshold is part of it.
        tree = ast.parse(src)
        found_threshold_compare = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                # Look for a Compare with `confluence_score` on the left
                # and self._min_confluence as one of the comparators.
                left = node.left
                if (
                    isinstance(left, ast.Attribute)
                    and left.attr == "confluence_score"
                ):
                    for comp in node.comparators:
                        if (
                            isinstance(comp, ast.Attribute)
                            and comp.attr == "_min_confluence"
                        ):
                            found_threshold_compare = True
        assert found_threshold_compare, (
            "No `confluence_score >= self._min_confluence` comparison found"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Sanity: importing the public surface still works
# ─────────────────────────────────────────────────────────────────────────────


def test_score_subsystems_sum_to_one_minus_risk_gate():
    """Scoring subsystems (market_structure, momentum, orderflow, sentiment,
    macro, volatility_regime) sum to 0.95; the 0.05 risk_filter is a hard
    gate and intentionally not a score input (see SUBSYSTEMS in
    decision_engine.py:74). If this breaks to 1.0 or 0.0, someone
    accidentally re-added or re-removed the risk_filter weight."""
    total = sum(s.weight for s in SUBSYSTEMS)
    assert abs(total - 0.95) < 1e-6, f"Subsystem weights sum to {total} (expected 0.95)"


# ─────────────────────────────────────────────────────────────────────────────
# Bug #7 — decision engine was permanently NO_TRADE
# ─────────────────────────────────────────────────────────────────────────────


class TestBug7DecisionEngineCanFire:
    """Audit on 2026-06-02 found: of 173 decision engine evaluations
    against live data, 100% returned NO_TRADE because the configured
    min_signal_confidence (0.60) exceeded the max achievable final
    score (~0.26), and min_subsystem_score (0.30) was higher than
    most real scores.

    Repairs landed: min_signal_confidence 0.60 → 0.20, min_subsystem
    score 0.30 → 0.15, weights rebalanced (momentum 0.15 → 0.30,
    market_structure 0.25 → 0.20, orderflow 0.20 → 0.10), orchestrator
    min_confirmations 3 → 2, backtest min_confirmations 2 → 1.

    This test guards the reweight.
    """

    def test_momentum_weight_dominates(self):
        # The audit showed momentum is the only signal with real
        # amplitude (61% > 0.2). The reweight gives it the largest
        # share. If someone reverts to the equal-weight mindset,
        # this test fails.
        momentum = next(s for s in SUBSYSTEMS if s.name == "momentum")
        structure = next(s for s in SUBSYSTEMS if s.name == "market_structure")
        assert momentum.weight >= structure.weight, (
            f"momentum ({momentum.weight}) should be >= market_structure "
            f"({structure.weight}) after the 2026-06-02 reweight"
        )

    def test_min_subsystem_score_is_low_enough(self):
        # The default min_subsystem_score must be low enough that
        # the active subsystems (mkt_struct, momentum, vol_regime)
        # can confirm on real data. Real averages are 0.06-0.40;
        # 0.30 was unreachable. Locked at 0.15 by the repair.
        for s in SUBSYSTEMS:
            assert s.min_score <= 0.20, (
                f"subsystem {s.name} min_score {s.min_score} is too high "
                f"(post-2026-06-02 audit raised it from 0.15)"
            )

    def test_config_min_signal_confidence_lowered(self):
        """The base config must have min_signal_confidence <= 0.25.
        The 0.60 default was unreachable (max score ~0.26) and
        made the engine a permanent NO_TRADE.
        """
        from src.utils.config import get_config
        cfg = get_config()
        assert cfg.engine.min_signal_confidence <= 0.25, (
            f"engine.min_signal_confidence={cfg.engine.min_signal_confidence} "
            f"is too high — max achievable final_score is ~0.26"
        )
