"""Tests for the v0.2.0 strategy direction-bias fix.

Two layers are fixed in v0.2.0:
  1. PairRanker direction is now a 2-of-3 component vote (structure,
     pullback, momentum). The previous logic defaulted to a
     momentum-only fallback at ±0.2, which biased direction toward
     whichever sign momentum happened to favor — a known sell bias
     in the 30d calibration window.
  2. The override path in trading_loop (and mirrored in the backtest
     strategy) now consults the regime. The previous override
     forced a trade whenever the ranker was actionable + direction
     was set + confluence >= 0.35, with no regime veto. This is the
     layer that let the 2026-06-05 01:33 incident open 14 SHORTs
     in 1.5h in a bearish regime.

These tests pin the new behavior so regressions surface immediately.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.data.models import (
    NormalizedCandle,
    Regime,
    TimeFrame,
)
from src.orchestrator.trading_loop import (
    OVERRIDE_MIN_CONFLUENCE,
    TradingOrchestrator,
    direction_matches_regime,
)
from src.signals.pair_ranker import PairRanker, RankedPair
from src.signals.regime_detector import RegimeAnalysis


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_candles(n: int = 60, base: float = 100.0, step: float = 0.5) -> list[NormalizedCandle]:
    """Build n synthetic candles with a mild uptrend and steady volume.

    Timestamps step by 1 hour but wrap day boundaries so the test
    doesn't crash on datetime's 0..23 hour constraint.
    """
    out: list[NormalizedCandle] = []
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


def _df_from_candles(candles: list[NormalizedCandle]) -> "pd.DataFrame":
    """Build a pandas DataFrame from a list of NormalizedCandle.

    The backtest strategy's on_bar expects history_by_symbol as
    DataFrames indexed by timestamp. Mirrors what run_backtest.py
    would feed it.
    """
    import pandas as pd
    return pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        },
        index=pd.DatetimeIndex([c.timestamp for c in candles]),
    )


def _make_regime(regime: Regime, ema_fast: float = 100.0, ema_slow: float = 100.0) -> RegimeAnalysis:
    """Build a RegimeAnalysis with the given regime + ema geometry.

    `is_bullish()` and `is_bearish()` depend on both the regime AND
    ema_fast vs ema_slow, so the ema fields are required to
    exercise the direction/regime compatibility check correctly.
    """
    return RegimeAnalysis(
        regime=regime,
        symbol="BTC",
        timeframe=TimeFrame.H1,
        confidence=0.85,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fix #1: 2-of-3 component vote in PairRanker
# ─────────────────────────────────────────────────────────────────────────────


class TestRanker2Of3DirectionVote:
    """The ranker should set direction only when at least 2 of 3
    components (structure, pullback, momentum) agree.
    """

    def test_all_three_agree_bullish_sets_buy(self):
        """structure > gate, pullback > gate, momentum > gate → buy."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(0.20, 0.30, 0.40)
        assert result == "buy"

    def test_all_three_agree_bearish_sets_sell(self):
        """All three < -gate → sell."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(-0.20, -0.30, -0.40)
        assert result == "sell"

    def test_only_momentum_negative_no_direction(self):
        """v0.1.0 BUG REPRODUCTION: structure/pullback near zero,
        momentum only slightly negative. v0.1.0 would have set
        direction='sell' from the momentum fallback. v0.2.0 must
        keep direction=None because only 1 of 3 votes."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(
            0.05,   # below STRUCTURE_GATE=0.10
            0.02,   # below PULLBACK_GATE=0.15
            -0.25,  # above MOMENTUM_GATE=0.20 (negative)
        )
        assert result is None, (
            "v0.2.0 regression: only 1 of 3 components voted, "
            "direction should be None (was sell in v0.1.0)"
        )

    def test_only_momentum_positive_no_direction(self):
        """Mirror of the v0.1.0 bug: 1/3 votes is not enough."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(-0.05, -0.10, 0.30)
        assert result is None

    def test_two_of_three_bullish_sets_buy(self):
        """Structure + momentum agree bullish, pullback disagrees → buy."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(0.20, -0.20, 0.30)
        assert result == "buy"

    def test_two_of_three_bearish_sets_sell(self):
        """Pullback + momentum agree bearish, structure neutral → sell."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(0.05, -0.20, -0.30)
        assert result == "sell"

    def test_split_three_ways_no_direction(self):
        """structure=buy, pullback=sell, momentum=buy → 2 buy / 1 sell → buy."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(0.20, -0.30, 0.40)
        assert result == "buy"

    def test_all_three_below_gate_no_direction(self):
        """All three components below their gates (ranging market) → None."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        result = ranker._direction_from_votes(0.05, 0.10, 0.10)
        assert result is None

    def test_volume_does_not_vote(self):
        """Volume is not a parameter of _direction_from_votes —
        it cannot vote by construction. Pin that."""
        import inspect
        sig = inspect.signature(PairRanker._direction_from_votes)
        params = list(sig.parameters.keys())
        assert "volume" not in params, (
            f"volume should not be a parameter of _direction_from_votes, got {params}"
        )

    def test_score_pair_integration_uses_new_vote(self):
        """End-to-end: _score_pair should produce direction=None
        for a pair where only momentum crosses its gate (the
        v0.1.0 sell-bias scenario)."""
        ranker = PairRanker(max_pairs=5, min_confluence_score=0.0)
        # Patch each component scorer to return values that would
        # produce the v0.1.0 sell bias.
        ranker._calc_structure_score = lambda c: 0.05  # below gate
        ranker._calc_pullback_score = lambda c: 0.02   # below gate
        ranker._calc_momentum_score = lambda c: -0.30  # above gate (negative)
        ranker._calc_volume_score = lambda c: 0.5
        candles = _make_candles(60)
        pair = ranker._score_pair("BTC", candles, {"1h": candles, "15m": candles})
        assert pair.direction is None, (
            f"v0.2.0 regression: only momentum voted, expected None, got {pair.direction}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fix #2: regime-aware override in trading_loop + backtest
# ─────────────────────────────────────────────────────────────────────────────


class TestDirectionMatchesRegime:
    """The override guard should veto direction/regime mismatches."""

    def test_no_direction_is_allowed(self):
        """direction=None is not a contradiction; just no signal."""
        allowed, reason = direction_matches_regime(None, None)
        assert allowed is True
        assert reason == "no_direction_or_no_regime"

    def test_no_regime_is_allowed(self):
        """Cold start — no regime to disagree with."""
        allowed, reason = direction_matches_regime("buy", None)
        assert allowed is True
        assert reason == "no_direction_or_no_regime"

    def test_bullish_regime_rejects_sell(self):
        """STRONG_TREND + ema_fast > ema_slow + direction=sell → reject."""
        regime = _make_regime(
            Regime.STRONG_TREND_STABLE_VOL,
            ema_fast=105.0,
            ema_slow=100.0,
        )
        allowed, reason = direction_matches_regime("sell", regime)
        assert allowed is False
        assert "bullish_regime_sell_ranker" in reason
        assert Regime.STRONG_TREND_STABLE_VOL.value in reason

    def test_bearish_regime_rejects_buy(self):
        """STRONG_TREND + ema_fast < ema_slow + direction=buy → reject."""
        regime = _make_regime(
            Regime.STRONG_TREND_STABLE_VOL,
            ema_fast=100.0,
            ema_slow=105.0,
        )
        allowed, reason = direction_matches_regime("buy", regime)
        assert allowed is False
        assert "bearish_regime_buy_ranker" in reason

    def test_bearish_regime_allows_sell(self):
        """The 2026-06-05 incident: bearish regime + ranker says
        sell. With the regime guard this is still allowed — the
        guard is for MISMATCHES, not for any short position. The
        real protection is in confluence floor + max_positions +
        ranking pool size."""
        regime = _make_regime(
            Regime.STRONG_TREND_STABLE_VOL,
            ema_fast=100.0,
            ema_slow=105.0,
        )
        allowed, reason = direction_matches_regime("sell", regime)
        assert allowed is True
        assert reason == "compatible"

    def test_bullish_regime_allows_buy(self):
        regime = _make_regime(
            Regime.STRONG_TREND_STABLE_VOL,
            ema_fast=105.0,
            ema_slow=100.0,
        )
        allowed, reason = direction_matches_regime("buy", regime)
        assert allowed is True
        assert reason == "compatible"

    def test_dangerous_regime_rejects_everything(self):
        """LIQUIDITY_CRISIS, MARKET_DISTORTION, CHOPPY_CONTRACTING_VOL
        reject both directions."""
        for dangerous in (
            Regime.LIQUIDITY_CRISIS,
            Regime.MARKET_DISTORTION,
            Regime.CHOPPY_CONTRACTING_VOL,
        ):
            regime = _make_regime(dangerous, ema_fast=100.0, ema_slow=100.0)
            for direction in ("buy", "sell"):
                allowed, reason = direction_matches_regime(direction, regime)
                assert allowed is False, (
                    f"dangerous={dangerous} direction={direction} should be rejected"
                )
                assert "dangerous_regime" in reason

    def test_ranging_regime_does_not_constrain_direction(self):
        """RANGING markets should not veto the override — the
        ranker can still pick a direction in chop, and the user
        explicitly asked for the bot to keep trading in sideways
        markets. is_bullish/is_bearish return False for RANGING,
        so neither mismatch check fires."""
        for ranging in (
            Regime.RANGING_STABLE_VOL,
            Regime.RANGING_LOW_VOL,
            Regime.RANGING_HIGH_VOL,
        ):
            regime = _make_regime(ranging, ema_fast=100.0, ema_slow=100.0)
            for direction in ("buy", "sell"):
                allowed, reason = direction_matches_regime(direction, regime)
                assert allowed is True, (
                    f"ranging={ranging} direction={direction} should be allowed "
                    f"(reason={reason})"
                )

    def test_weak_trend_does_not_count_as_bullish_or_bearish(self):
        """is_bullish/is_bearish require STRONG_TREND. WEAK_TREND
        with ema_fast > ema_slow should NOT reject a sell — the
        trend is too weak to use as a veto."""
        regime = _make_regime(
            Regime.WEAK_TREND_STABLE_VOL,
            ema_fast=105.0,
            ema_slow=100.0,
        )
        allowed, _ = direction_matches_regime("sell", regime)
        assert allowed is True, (
            "WEAK_TREND should not veto a sell — is_bullish() is False for WEAK_TREND"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Constants & wiring
# ─────────────────────────────────────────────────────────────────────────────


class TestOverrideConfig:
    """Pin the override confluence floor and its usage."""

    def test_override_floor_is_stricter_than_scanner_floor(self):
        """OVERRIDE_MIN_CONFLUENCE must be > the dev scanner
        min_confluence_score (0.35) so the override is a
        higher-quality bar than the soft gate."""
        # v0.2.2 (2026-06-06): lowered 0.50 -> 0.40 after the 90-day
        # walk-forward validated it. 0.40 is still > the dev scanner
        # min_confluence_score (0.35), so the override remains a
        # higher-quality bar than the soft gate.
        assert OVERRIDE_MIN_CONFLUENCE > 0.35
        assert OVERRIDE_MIN_CONFLUENCE == 0.40

    def test_trading_loop_uses_override_floor_not_scanner_floor(self):
        """AST-level check: the override path in _evaluate_ranked_pair
        must reference OVERRIDE_MIN_CONFLUENCE, not self._min_confluence."""
        from src.orchestrator.trading_loop import TradingOrchestrator
        src = inspect.getsource(TradingOrchestrator._evaluate_ranked_pair)
        assert "OVERRIDE_MIN_CONFLUENCE" in src, (
            "trading_loop override path does not reference OVERRIDE_MIN_CONFLUENCE"
        )

    def test_backtest_uses_sweep_threshold_as_override_floor(self):
        """The backtest strategy's override threshold follows the
        sweep's `min_confluence` parameter (not the production
        hardcoded `OVERRIDE_MIN_CONFLUENCE`). The sweep is the test
        of that threshold — clamping it to a production constant
        makes the sweep degenerate (every config collapses to the
        same floor). Production uses the hardcoded floor in
        trading_loop.py as a safety clamp; the backtest is free
        to test the full range.
        """
        import ast
        import textwrap
        from src.backtest.strategy import BacktestStrategy
        src = inspect.getsource(TradingOrchestrator._evaluate_ranked_pair)
        assert "OVERRIDE_MIN_CONFLUENCE" in src, (
            "trading_loop production override must use OVERRIDE_MIN_CONFLUENCE"
        )

        # Now check the backtest: the threshold comparison in the
        # actionable condition must reference self.min_confluence,
        # not OVERRIDE_MIN_CONFLUENCE. We do this with an AST walk
        # so the comment-level references (which are fine) don't
        # trip the test.
        # inspect.getsource returns class-indented text; dedent
        # before ast.parse or the first statement is "unexpected indent".
        backtest_src = textwrap.dedent(inspect.getsource(BacktestStrategy._on_bar_async))
        tree = ast.parse(backtest_src)
        # Collect id() of Name nodes that are LHS of a Compare
        # where the right side is `self.min_confluence` — those
        # are threshold checks that follow the sweep parameter.
        threshold_self_refs = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for comp in node.comparators:
                    if (
                        isinstance(comp, ast.Attribute)
                        and comp.attr == "min_confluence"
                        and isinstance(comp.value, ast.Name)
                        and comp.value.id == "self"
                    ):
                        threshold_self_refs += 1
        assert threshold_self_refs >= 1, (
            "backtest override must compare confluence_score against "
            "self.min_confluence (sweep parameter), not a hardcoded constant"
        )
        # Regime guard is still applied.
        assert "direction_matches_regime" in backtest_src

    def test_backtest_no_override_flag_skips_regime_and_override(self):
        """The no_override flag bypasses the override path entirely
        — the decision engine's NO_TRADE stands. Useful for the
        calibration sweep's 'decision engine only' baseline."""
        from src.backtest.strategy import BacktestStrategy
        # The flag exists on the constructor and is stored.
        import inspect
        sig = inspect.signature(BacktestStrategy.__init__)
        assert "no_override" in sig.parameters
        assert sig.parameters["no_override"].default is False
        # The flag is read in _on_bar_async (skips the override block).
        src = inspect.getsource(BacktestStrategy._on_bar_async)
        assert "self.no_override" in src, (
            "_on_bar_async must consult self.no_override to skip the override"
        )

    def test_backtest_on_bar_runs_in_both_modes_without_nameerror(self):
        """Regression: a leftover logger.info call outside the
        no_override/override branches referenced OVERRIDE_MIN_CONFLUENCE
        and crashed with NameError in BOTH modes. This test exercises
        the actual on_bar path in both modes against synthetic data
        to catch the regression class.

        Pins:
        - no_override=True: regime detection is skipped, decision
          engine's NO_TRADE stands, no NameError.
        - no_override=False: regime detection runs, override applies
          (or is suppressed by direction_matches_regime), no NameError.
        """
        import asyncio
        from src.backtest.strategy import BacktestStrategy

        candles = _make_candles(60)
        # 3 symbols × 60 bars is enough to drive a few _on_bar_async calls
        history = {
            "BTC": _df_from_candles(candles),
            "ETH": _df_from_candles(candles),
            "SOL": _df_from_candles(candles),
        }

        for no_override in (True, False):
            strat = BacktestStrategy(
                symbols=["BTC", "ETH", "SOL"],
                lookback_bars=50,
                min_confluence=0.40,
                top_n_per_bar=3,
                no_override=no_override,
            )
            ts = candles[-1].timestamp
            # _on_bar_async must not raise NameError. The result
            # can be empty (no trades) — that's fine. We just need
            # the path to complete.
            try:
                result = asyncio.run(strat.on_bar(ts, history))
            except NameError as exc:
                pytest.fail(
                    f"_on_bar_async crashed with NameError when no_override={no_override}: {exc}"
                )
            assert isinstance(result, list), (
                f"expected list of PendingOrder, got {type(result)} for no_override={no_override}"
            )
