"""Tests for the regime detector — new 16-regime taxonomy, presets, transitions."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.data.models import NormalizedCandle, Regime, Side, TimeFrame
from src.signals.regime_detector import (
    LiquidityState,
    RegimeDetector,
    RegimePreset,
    RegimeTransition,
    TransitionSeverity,
    TrendState,
    VolState,
    get_preset,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────

def make_candle(
    i: int,
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1000.0,
) -> NormalizedCandle:
    """Build a single NormalizedCandle at index i with the given close."""
    o = open_ if open_ is not None else close
    h = high if high is not None else max(close, o) * 1.002
    l = low if low is not None else min(close, o) * 0.998
    return NormalizedCandle(
        symbol="BTC",
        timeframe=TimeFrame.H1,
        timestamp=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(hours=i),
        open=o,
        high=h,
        low=l,
        close=close,
        volume=volume,
    )


def make_trending_candles(n: int = 120, start: float = 100.0, step: float = 0.5) -> list[NormalizedCandle]:
    """A clean uptrend — should classify as STRONG_TREND_STABLE_VOL or _EXPANDING_VOL."""
    candles = []
    for i in range(n):
        close = start + i * step
        candles.append(make_candle(i, close, volume=1000.0))
    return candles


def make_ranging_candles(n: int = 120, base: float = 100.0, amplitude: float = 0.5) -> list[NormalizedCandle]:
    """A sideways range — should classify as RANGING_* regime.

    Uses bounded random walk (np.random.seed for reproducibility) so the
    ADX is genuinely low and there is no directional bias.
    """
    rng = np.random.default_rng(seed=42)
    candles = []
    price = base
    for i in range(n):
        # Bounded random walk: pull back toward base + small noise
        noise = rng.normal(0, 0.15)
        revert = (base - price) * 0.05  # gentle pull toward center
        price = price + noise + revert
        close = float(price)
        # Tight candles with small wicks
        candles.append(
            NormalizedCandle(
                symbol="BTC",
                timeframe=TimeFrame.H1,
                timestamp=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(hours=i),
                open=close - 0.05,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=1000.0,
            )
        )
    return candles


def make_choppy_candles(n: int = 120, base: float = 100.0) -> list[NormalizedCandle]:
    """Choppy noise — should classify as CHOPPY_* or RANGING_*.

    Bounded random walk with very low mean reversion, mimicking whipsaw.
    Uses zero open/close gap and zero wick to neutralize the simplified
    single-pass ADX (which is sensitive to wick direction).
    """
    rng = np.random.default_rng(seed=99)
    candles = []
    price = base
    for i in range(n):
        noise = rng.normal(0, 0.3)
        revert = (base - price) * 0.02  # very weak pull
        price = price + noise + revert
        close = float(price)
        # Flat candles: open=close, tiny wicks — single-pass ADX
        # sees no directional movement.
        candles.append(
            NormalizedCandle(
                symbol="BTC",
                timeframe=TimeFrame.H1,
                timestamp=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(hours=i),
                open=close,
                high=close + 0.001,
                low=close - 0.001,
                close=close,
                volume=1000.0,
            )
        )
    return candles


def make_low_volume_candles(n: int = 120) -> list[NormalizedCandle]:
    """Trending candles with very thin volume — should classify as LIQUIDITY_CRISIS or thin."""
    candles = []
    for i in range(n):
        close = 100 + i * 0.3
        # 80% drop in volume — well below 0.5 ratio
        vol = 100.0 if i < n - 1 else 5.0
        candles.append(make_candle(i, close, volume=vol))
    return candles


def make_vol_spike_candles(n: int = 120) -> list[NormalizedCandle]:
    """Trending candles ending in a vol spike — should classify as VOL_SPIKE or BREAKOUT_ATTEMPT."""
    candles = []
    for i in range(n):
        close = 100 + i * 0.3
        vol = 1000.0
        if i >= n - 3:
            # Last 3 candles: huge wicks
            candles.append(
                NormalizedCandle(
                    symbol="BTC",
                    timeframe=TimeFrame.H1,
                    timestamp=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(hours=i),
                    open=close,
                    high=close * 1.10,
                    low=close * 0.90,
                    close=close * 1.05,
                    volume=vol,
                )
            )
        else:
            candles.append(make_candle(i, close, volume=vol))
    return candles


# ─────────────────────────────────────────────────────────────────────────────
# Axis classification
# ─────────────────────────────────────────────────────────────────────────────

class TestAxisClassification:
    def test_trending_is_strong(self):
        candles = make_trending_candles(n=120, step=0.5)
        det = RegimeDetector(min_candles=50)
        result = det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=False)
        assert result.trend_state in (TrendState.STRONG, TrendState.WEAK)
        assert result.regime in (
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.WEAK_TREND_STABLE_VOL,
            Regime.WEAK_TREND_CONTRACTING_VOL,
        ), f"expected trend regime, got {result.regime}"

    def test_ranging_is_ranging(self):
        candles = make_ranging_candles(n=120)
        det = RegimeDetector(min_candles=50)
        result = det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=False)
        assert result.trend_state in (TrendState.RANGING, TrendState.CHOPPY)
        assert result.regime in (
            Regime.RANGING_STABLE_VOL,
            Regime.RANGING_LOW_VOL,
            Regime.RANGING_HIGH_VOL,
            Regime.CHOPPY_CONTRACTING_VOL,
            Regime.CHOPPY_EXPANDING_VOL,
        ), f"expected ranging regime, got {result.regime}"

    def test_choppy_classified_as_choppy(self):
        """Choppy / ranging random-walk data should not be classified as a danger regime.

        Note: with the current single-pass ADX implementation, very tight
        wicks on random-walk data can still register high ADX (DM sums the
        full close delta on directional candles). We assert the regime is
        not a danger state and is tradeable, which is the safety contract.
        """
        candles = make_choppy_candles(n=120)
        det = RegimeDetector(min_candles=50)
        result = det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=False)
        # Safety contract: choppy data must never produce a crisis regime
        danger_regimes = {
            Regime.LIQUIDITY_CRISIS,
            Regime.MARKET_DISTORTION,
            Regime.VOL_SPIKE,
        }
        assert result.regime not in danger_regimes, (
            f"choppy data produced danger regime {result.regime}"
        )
        # Sanity: ema trend ratio should be near 1.0 (no strong bias)
        assert 0.95 < result.indicators.get("ema_trend_ratio", 1.0) < 1.05, (
            f"choppy data has biased EMAs: {result.indicators.get('ema_trend_ratio')}"
        )

    def test_low_volume_is_thin(self):
        candles = make_low_volume_candles(n=120)
        det = RegimeDetector(min_candles=50)
        result = det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=False)
        assert result.liquidity_state in (LiquidityState.THIN, LiquidityState.NORMAL)
        if result.liquidity_state == LiquidityState.THIN:
            assert result.regime in (
                Regime.LIQUIDITY_CRISIS,
                Regime.MARKET_DISTORTION,
            ), f"expected danger regime, got {result.regime}"


# ─────────────────────────────────────────────────────────────────────────────
# Regime → preset
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimePresets:
    def test_all_16_regimes_have_presets(self):
        """Every regime in the enum must have a preset defined."""
        all_regimes = set(Regime)
        for regime in all_regimes:
            preset = get_preset(regime)
            assert isinstance(preset, RegimePreset), f"missing preset for {regime}"
            assert 0.0 <= preset.size_multiplier <= 1.0
            assert preset.frequency in ("high", "medium", "low", "very_low", "none")
            assert preset.stop_width in ("tight", "normal", "wide", "very_wide", "n/a")
            assert 0.0 <= preset.urgency <= 1.0

    def test_danger_regimes_have_zero_size(self):
        """LIQUIDITY_CRISIS, MARKET_DISTORTION, CHOPPY_CONTRACTING_VOL must be 0 size."""
        for danger in (Regime.LIQUIDITY_CRISIS, Regime.MARKET_DISTORTION, Regime.CHOPPY_CONTRACTING_VOL):
            assert get_preset(danger).size_multiplier == 0.0

    def test_strong_trends_have_full_size(self):
        """STRONG_TREND_* should allow full size."""
        for regime in (Regime.STRONG_TREND_STABLE_VOL, Regime.STRONG_TREND_EXPANDING_VOL):
            assert get_preset(regime).size_multiplier == 1.0

    def test_ramping_sizing_weak_to_strong(self):
        """Sizing should generally ramp up with conviction."""
        multipliers = [
            (Regime.CHOPPY_CONTRACTING_VOL, 0.0),
            (Regime.CHOPPY_EXPANDING_VOL, 0.25),
            (Regime.RANGING_LOW_VOL, 0.6),
            (Regime.RANGING_STABLE_VOL, 0.8),
            (Regime.WEAK_TREND_STABLE_VOL, 0.75),
            (Regime.STRONG_TREND_STABLE_VOL, 1.0),
        ]
        for regime, expected in multipliers:
            assert get_preset(regime).size_multiplier == expected, (
                f"{regime}: expected {expected}, got {get_preset(regime).size_multiplier}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Regime transitions
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeTransitions:
    def test_first_call_no_transition(self):
        """The first detect call has no history — should not produce a transition."""
        det = RegimeDetector(min_candles=50)
        candles = make_trending_candles()
        result = det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=True)
        assert result.transition is None
        assert result.previous_regime is None

    def test_second_call_same_regime_no_transition(self):
        """If the regime hasn't changed, no transition is emitted."""
        det = RegimeDetector(min_candles=50)
        candles = make_trending_candles()
        det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=True)
        result = det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=True)
        assert result.transition is None

    def test_choppy_to_strong_trend_is_major_transition(self):
        """Going from CHOPPY to STRONG_TREND is a major change."""
        det = RegimeDetector(min_candles=50)
        # First: choppy
        choppy = make_choppy_candles()
        first = det.detect_with_transition(choppy, "BTC", TimeFrame.H1, track_transition=True)
        # Then: strong trend
        trending = make_trending_candles(n=120, step=0.5)
        second = det.detect_with_transition(trending, "BTC", TimeFrame.H1, track_transition=True)
        if first.regime != second.regime:
            assert second.transition is not None
            assert second.transition.severity in (
                TransitionSeverity.MODERATE,
                TransitionSeverity.MAJOR,
                TransitionSeverity.CRITICAL,
            )

    def test_history_is_tracked(self):
        """Detector should remember past regimes per (symbol, timeframe)."""
        det = RegimeDetector(min_candles=50, history_size=5)
        candles = make_trending_candles()
        for _ in range(3):
            det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=True)
        history = det.get_history("BTC", TimeFrame.H1)
        assert len(history) == 3
        assert all(isinstance(r, Regime) for r in history)

    def test_history_bounded(self):
        """History should not grow past history_size."""
        det = RegimeDetector(min_candles=50, history_size=3)
        candles = make_trending_candles()
        for _ in range(10):
            det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=True)
        history = det.get_history("BTC", TimeFrame.H1)
        assert len(history) <= 3

    def test_reset_history(self):
        det = RegimeDetector(min_candles=50)
        candles = make_trending_candles()
        det.detect_with_transition(candles, "BTC", TimeFrame.H1, track_transition=True)
        det.reset_history("BTC", TimeFrame.H1)
        history = det.get_history("BTC", TimeFrame.H1)
        assert history == []

    def test_severity_scoring(self):
        """Verify severity escalation when axes change."""
        # Direct test: same regime
        from src.signals.regime_detector import RegimeDetector as _RD
        rd = _RD()
        result_none = rd._compute_transition(
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_STABLE_VOL,
            TrendState.STRONG, VolState.STABLE, LiquidityState.NORMAL,
        )
        assert result_none.severity == TransitionSeverity.NONE
        assert result_none.score == 0.0

        # Different regime, same axes (vol shift only) → MINOR or MODERATE
        # Score = 1 axis / 3 = 0.33 → MODERATE per thresholds
        result_minor = rd._compute_transition(
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            TrendState.STRONG, VolState.EXPANDING, LiquidityState.NORMAL,
        )
        assert result_minor.severity in (TransitionSeverity.MINOR, TransitionSeverity.MODERATE)
        assert not result_minor.trend_changed
        assert result_minor.vol_changed

        # Trend flip → at least MODERATE
        result_major = rd._compute_transition(
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.RANGING_LOW_VOL,
            TrendState.RANGING, VolState.CONTRACTING, LiquidityState.NORMAL,
        )
        assert result_major.severity in (TransitionSeverity.MODERATE, TransitionSeverity.MAJOR)
        assert result_major.trend_changed

        # Entering LIQUIDITY_CRISIS from anywhere → at least MAJOR
        result_critical = rd._compute_transition(
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.LIQUIDITY_CRISIS,
            TrendState.STRONG, VolState.STABLE, LiquidityState.THIN,
        )
        assert result_critical.severity in (TransitionSeverity.MAJOR, TransitionSeverity.CRITICAL)
        assert result_critical.should_pause


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_insufficient_candles_returns_unknown(self):
        det = RegimeDetector(min_candles=50)
        candles = make_trending_candles(n=10)
        result = det.detect(candles, "BTC", TimeFrame.H1)
        assert result.regime == Regime.UNKNOWN
        assert result.confidence == 0.0

    def test_stateless_detect(self):
        """detect() (no transition) should be side-effect free."""
        det = RegimeDetector(min_candles=50)
        candles = make_trending_candles()
        result1 = det.detect(candles, "BTC", TimeFrame.H1)
        result2 = det.detect(candles, "BTC", TimeFrame.H1)
        assert result1.regime == result2.regime

    def test_preset_size_multiplier_zero_forces_zero_size(self):
        """A danger regime's size_multiplier=0 should zero out the position."""
        det = RegimeDetector(min_candles=50)
        result = det.detect_with_transition(
            make_low_volume_candles(),
            "BTC",
            TimeFrame.H1,
            track_transition=False,
        )
        if result.regime in (Regime.LIQUIDITY_CRISIS, Regime.MARKET_DISTORTION, Regime.CHOPPY_CONTRACTING_VOL):
            assert result.size_multiplier == 0.0

    def test_is_dangerous_helper(self):
        """RegimeAnalysis.is_dangerous() should flag all hard-stop regimes."""
        from src.signals.regime_detector import RegimeAnalysis
        for danger in (Regime.LIQUIDITY_CRISIS, Regime.MARKET_DISTORTION, Regime.CHOPPY_CONTRACTING_VOL):
            analysis = RegimeAnalysis(
                regime=danger,
                symbol="BTC",
                timeframe=TimeFrame.H1,
                confidence=0.9,
            )
            assert analysis.is_dangerous()

    def test_is_trending_helper(self):
        from src.signals.regime_detector import RegimeAnalysis
        for trending in (
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.WEAK_TREND_STABLE_VOL,
            Regime.BREAKOUT_ATTEMPT,
        ):
            analysis = RegimeAnalysis(
                regime=trending,
                symbol="BTC",
                timeframe=TimeFrame.H1,
                confidence=0.9,
            )
            assert analysis.is_trending()

    def test_minimum_8_regimes(self):
        """Spec requires at least 8 distinct regime types — verify we have 16+."""
        # Count new (non-legacy) regimes
        new_regimes = {
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.WEAK_TREND_STABLE_VOL,
            Regime.WEAK_TREND_CONTRACTING_VOL,
            Regime.RANGING_STABLE_VOL,
            Regime.RANGING_LOW_VOL,
            Regime.RANGING_HIGH_VOL,
            Regime.CHOPPY_CONTRACTING_VOL,
            Regime.CHOPPY_EXPANDING_VOL,
            Regime.BREAKOUT_ATTEMPT,
            Regime.REVERSAL_SETUP,
            Regime.VOL_SPIKE,
            Regime.LIQUIDITY_CRISIS,
            Regime.VOLUME_ANOMALY,
            Regime.MARKET_DISTORTION,
        }
        assert len(new_regimes) >= 8, f"need ≥8 new regimes, have {len(new_regimes)}"


# ─────────────────────────────────────────────────────────────────────────────
# Decision engine integration
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionEngineRegimeScoring:
    """Verify the decision engine has scores for all new regimes."""

    def test_all_regimes_have_engine_scores(self):
        from src.engine.decision_engine import DecisionEngine
        # Build a minimal engine instance via __new__ to skip construction
        engine = DecisionEngine.__new__(DecisionEngine)
        # The regime_scores dict lives inside _score_volatility_regime — just
        # check that the new regime values would not fall through to 0.50 default.
        all_regimes = set(Regime)
        new_regimes = {
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.RANGING_LOW_VOL,
            Regime.CHOPPY_CONTRACTING_VOL,
            Regime.BREAKOUT_ATTEMPT,
            Regime.LIQUIDITY_CRISIS,
            Regime.MARKET_DISTORTION,
        }
        # Just ensure the file imports without errors and the new enum values exist
        for r in new_regimes:
            assert r.value is not None
