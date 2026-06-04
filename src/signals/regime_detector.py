"""Market regime classifier.

Classifies the current market regime for a symbol based on price data
and technical indicators. The detected regime modulates which signals
are weighted higher in the decision engine, and how large / how often
we are willing to trade.

# Regime taxonomy (16 states)

The regime is a product of three orthogonal axes:
  - **Trend**     : STRONG_TREND / WEAK_TREND / RANGING / CHOPPY
  - **Volatility** : EXPANDING / CONTRACTING / STABLE / SPIKE
  - **Liquidity**  : DEEP / NORMAL / THIN / ANOMALOUS

A small number of *named* regimes are emitted (instead of a Cartesian
product) so the decision engine and human operators can reason about
each one explicitly. Each regime carries a `RegimePreset` (sizing,
frequency, stop width) and an `urgency` flag (1.0 = act now, 0.0 = stand
down). The detector also tracks the previous regime and surfaces
`RegimeTransition` events when the market changes character — those are
the moments where Aoi's judgment is most valuable.

## Regime catalogue

| Regime                      | Trend        | Vol          | Liquidity  | Sizing | Frequency | Stop width | Urgency |
|-----------------------------|--------------|--------------|------------|--------|-----------|------------|---------|
| STRONG_TREND_STABLE_VOL     | strong       | stable       | normal+    | 1.00x  | high      | normal     | 0.85    |
| STRONG_TREND_EXPANDING_VOL  | strong       | expanding    | normal+    | 1.00x  | high      | wide       | 0.90    |
| WEAK_TREND_STABLE_VOL       | weak         | stable       | normal+    | 0.75x  | medium    | normal     | 0.55    |
| WEAK_TREND_CONTRACTING_VOL  | weak         | contracting  | normal+    | 0.50x  | low       | tight      | 0.50    |
| RANGING_STABLE_VOL          | ranging      | stable       | normal+    | 0.80x  | medium    | normal     | 0.55    |
| RANGING_LOW_VOL             | ranging      | contracting  | normal+    | 0.60x  | low       | tight      | 0.40    |
| RANGING_HIGH_VOL            | ranging      | expanding    | normal+    | 0.50x  | low       | wide       | 0.45    |
| CHOPPY_CONTRACTING_VOL      | choppy       | contracting  | normal+    | 0.00x  | none      | n/a        | 0.20    |
| CHOPPY_EXPANDING_VOL        | choppy       | expanding    | normal+    | 0.25x  | very low  | wide       | 0.35    |
| BREAKOUT_ATTEMPT            | weak→strong  | expanding    | normal+    | 0.75x  | medium    | wide       | 0.70    |
| REVERSAL_SETUP              | ranging      | contracting  | normal+    | 0.40x  | low       | tight      | 0.45    |
| VOL_SPIKE                   | any          | spike        | normal+    | 0.25x  | very low  | very wide  | 0.30    |
| LIQUIDITY_CRISIS            | any          | any          | thin       | 0.00x  | none      | n/a        | 0.10    |
| VOLUME_ANOMALY              | any          | any          | anomalous  | 0.25x  | very low  | wide       | 0.25    |
| MARKET_DISTORTION           | any          | spike        | thin       | 0.00x  | none      | n/a        | 0.10    |
| UNKNOWN                     | insufficient data                                                                |

`x` in sizing column is `risk.max_position_pct` (default 0.20). These
multipliers are exposed as `RegimePreset.size_multiplier` and applied
in the decision engine.

## Regime transitions

`RegimeTransition` captures the *delta* between the previous regime
and the new one. Severity is computed from how different the two
regimes are in trend / volatility / liquidity axes. Severity >= 0.5
should pause the bot and require Aoi's review before resuming — a
sudden regime change means prior context is no longer valid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np

from ..data.models import NormalizedCandle, Regime, TimeFrame
from ..utils.logging import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Trend / Volatility / Liquidity axis states
# ─────────────────────────────────────────────────────────────────────────────

class TrendState(str, Enum):
    """Trend axis — direction + strength."""

    STRONG = "STRONG"   # ADX > 30, EMA(20) clearly above/below EMA(50)
    WEAK = "WEAK"       # 20 < ADX < 30, mild EMA separation
    RANGING = "RANGING" # ADX < 20, EMAs near-equal
    CHOPPY = "CHOPPY"   # ADX < 20, EMAs crossing frequently (whipsaw)


class VolState(str, Enum):
    """Volatility axis — ATR behaviour."""

    EXPANDING = "EXPANDING"   # ATR% rising vs 20-period SMA
    CONTRACTING = "CONTRACTING"  # ATR% falling vs 20-period SMA
    STABLE = "STABLE"         # ATR% within ±15% of its SMA
    SPIKE = "SPIKE"           # ATR% > 3x its SMA (single event)


class LiquidityState(str, Enum):
    """Liquidity / volume axis."""

    DEEP = "DEEP"             # volume_ratio > 1.5
    NORMAL = "NORMAL"         # 0.7 <= volume_ratio <= 1.5
    THIN = "THIN"             # volume_ratio < 0.5
    ANOMALOUS = "ANOMALOUS"   # volume_ratio > 3.0 (single-candle news/wash)


# ─────────────────────────────────────────────────────────────────────────────
# Regime → Trading Preset
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegimePreset:
    """How a regime should influence position sizing and trade frequency.

    The decision engine reads these to scale position size, widen or
    tighten stops, and gate entry frequency.
    """

    size_multiplier: float        # 0.0–1.0; multiplies max_position_pct
    frequency: str                # "high" | "medium" | "low" | "very_low" | "none"
    stop_width: str               # "tight" | "normal" | "wide" | "very_wide" | "n/a"
    urgency: float                # 0.0–1.0; how time-sensitive the setup is
    notes: str                    # human-readable guidance for Aoi


# Preset table — single source of truth for sizing implications.
# Documented at module level above; this is the lookup the engine consumes.
REGIME_PRESETS: dict[Regime, RegimePreset] = {
    Regime.STRONG_TREND_STABLE_VOL: RegimePreset(
        size_multiplier=1.00,
        frequency="high",
        stop_width="normal",
        urgency=0.85,
        notes="Clean trend, ride it. Full size, normal stops, trend-following bias.",
    ),
    Regime.STRONG_TREND_EXPANDING_VOL: RegimePreset(
        size_multiplier=1.00,
        frequency="high",
        stop_width="wide",
        urgency=0.90,
        notes="Trend + breakout. Full size, wider stops to absorb swings, continuation bias.",
    ),
    Regime.WEAK_TREND_STABLE_VOL: RegimePreset(
        size_multiplier=0.75,
        frequency="medium",
        stop_width="normal",
        urgency=0.55,
        notes="Trend losing steam. Reduce size, require stronger signal confluence.",
    ),
    Regime.WEAK_TREND_CONTRACTING_VOL: RegimePreset(
        size_multiplier=0.50,
        frequency="low",
        stop_width="tight",
        urgency=0.50,
        notes="Coiling. Small probes only; expect breakout or reversal.",
    ),
    Regime.RANGING_STABLE_VOL: RegimePreset(
        size_multiplier=0.80,
        frequency="medium",
        stop_width="normal",
        urgency=0.55,
        notes="Mean-reversion friendly. Fade extremes, expect continuation of range.",
    ),
    Regime.RANGING_LOW_VOL: RegimePreset(
        size_multiplier=0.60,
        frequency="low",
        stop_width="tight",
        urgency=0.40,
        notes="Quiet chop. Tight stops, mean reversion only at band extremes.",
    ),
    Regime.RANGING_HIGH_VOL: RegimePreset(
        size_multiplier=0.50,
        frequency="low",
        stop_width="wide",
        urgency=0.45,
        notes="Wide range, no trend. Fade extremes with wide stops, small size.",
    ),
    Regime.CHOPPY_CONTRACTING_VOL: RegimePreset(
        size_multiplier=0.00,
        frequency="none",
        stop_width="n/a",
        urgency=0.20,
        notes="Whipsaw + low vol. Stand down. No new entries — losing regime.",
    ),
    Regime.CHOPPY_EXPANDING_VOL: RegimePreset(
        size_multiplier=0.25,
        frequency="very_low",
        stop_width="wide",
        urgency=0.35,
        notes="Vol expansion without direction. Tiny size, very wide stops, or skip.",
    ),
    Regime.BREAKOUT_ATTEMPT: RegimePreset(
        size_multiplier=0.75,
        frequency="medium",
        stop_width="wide",
        urgency=0.70,
        notes="Vol expansion + low ADX + volume surge. Emerging trend. Enter on confirmation.",
    ),
    Regime.REVERSAL_SETUP: RegimePreset(
        size_multiplier=0.40,
        frequency="low",
        stop_width="tight",
        urgency=0.45,
        notes="After trend, now ranging with vol contraction. Coiled spring — small probe.",
    ),
    Regime.VOL_SPIKE: RegimePreset(
        size_multiplier=0.25,
        frequency="very_low",
        stop_width="very_wide",
        urgency=0.30,
        notes="Single ATR spike. Wait for confirmation. If in position, hold with wide stops.",
    ),
    Regime.LIQUIDITY_CRISIS: RegimePreset(
        size_multiplier=0.00,
        frequency="none",
        stop_width="n/a",
        urgency=0.10,
        notes="Orderbook withdrawal or volume collapse. No new entries. Close risk positions.",
    ),
    Regime.VOLUME_ANOMALY: RegimePreset(
        size_multiplier=0.25,
        frequency="very_low",
        stop_width="wide",
        urgency=0.25,
        notes="Single-candle volume event (news/wash). Wait for confirmation candle.",
    ),
    Regime.MARKET_DISTORTION: RegimePreset(
        size_multiplier=0.00,
        frequency="none",
        stop_width="n/a",
        urgency=0.10,
        notes="Extreme vol + thin liquidity. Multiple signals degraded. Stand down.",
    ),
    Regime.UNKNOWN: RegimePreset(
        size_multiplier=0.50,
        frequency="low",
        stop_width="normal",
        urgency=0.30,
        notes="Insufficient data. Reduce size, require extra confirmation.",
    ),
}


def get_preset(regime: Regime) -> RegimePreset:
    """Return the trading preset for a regime (always defined; UNKNOWN is the fallback)."""
    return REGIME_PRESETS.get(regime, REGIME_PRESETS[Regime.UNKNOWN])


# ─────────────────────────────────────────────────────────────────────────────
# Regime transitions
# ─────────────────────────────────────────────────────────────────────────────

class TransitionSeverity(str, Enum):
    """How serious a regime change is."""

    NONE = "NONE"             # Same regime as before
    MINOR = "MINOR"           # Same trend, vol/liquidity shift
    MODERATE = "MODERATE"     # One axis changed (e.g. trend flipped)
    MAJOR = "MAJOR"           # Two or more axes changed
    CRITICAL = "CRITICAL"     # Entering crisis state (LIQUIDITY_CRISIS / MARKET_DISTORTION)


@dataclass
class RegimeTransition:
    """A regime change between two analysis runs.

    `severity >= MODERATE` should pause the bot and require Aoi's review.
    """

    previous: Regime
    current: Regime
    severity: TransitionSeverity
    score: float                # 0.0–1.0 continuous severity
    trend_changed: bool
    vol_changed: bool
    liquidity_changed: bool
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def requires_review(self) -> bool:
        return self.severity in (TransitionSeverity.MODERATE, TransitionSeverity.MAJOR, TransitionSeverity.CRITICAL)

    @property
    def should_pause(self) -> bool:
        """Critical transitions should halt the bot until reviewed."""
        return self.severity in (TransitionSeverity.MAJOR, TransitionSeverity.CRITICAL)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeAnalysis:
    """Result of a regime classification run."""

    regime: Regime
    symbol: str
    timeframe: TimeFrame
    confidence: float = 0.0          # 0.0–1.0 how confident we are
    indicators: dict[str, float] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Axis decomposition — useful for explainability in Aoi's decisions
    trend_state: TrendState = TrendState.RANGING
    vol_state: VolState = VolState.STABLE
    liquidity_state: LiquidityState = LiquidityState.NORMAL

    # Regime-specific metadata
    trend_strength: float = 0.0     # ADX
    volatility_ratio: float = 0.0   # Current ATR% / avg ATR%
    volume_ratio: float = 0.0       # Current volume / avg volume
    ema_fast: float = 0.0          # EMA(20)
    ema_slow: float = 0.0           # EMA(50)
    ema_separation_pct: float = 0.0 # |fast - slow| / slow * 100

    # Transition info (only populated when previous regime is known)
    transition: RegimeTransition | None = None
    previous_regime: Regime | None = None

    # Convenience: regime-specific helper
    @property
    def preset(self) -> RegimePreset:
        return get_preset(self.regime)

    @property
    def size_multiplier(self) -> float:
        return self.preset.size_multiplier

    def is_trending(self) -> bool:
        return self.regime in (
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.WEAK_TREND_STABLE_VOL,
            Regime.WEAK_TREND_CONTRACTING_VOL,
            Regime.BREAKOUT_ATTEMPT,
        )

    def is_bullish(self) -> bool:
        """Bullish if trend state is up and trend is meaningful."""
        return (
            self.regime in (Regime.STRONG_TREND_STABLE_VOL, Regime.STRONG_TREND_EXPANDING_VOL)
            and self.ema_fast > self.ema_slow
        )

    def is_bearish(self) -> bool:
        return (
            self.regime in (Regime.STRONG_TREND_STABLE_VOL, Regime.STRONG_TREND_EXPANDING_VOL)
            and self.ema_fast < self.ema_slow
        )

    def is_dangerous(self) -> bool:
        """Regimes where the bot should not enter new positions."""
        return self.regime in (
            Regime.LIQUIDITY_CRISIS,
            Regime.MARKET_DISTORTION,
            Regime.CHOPPY_CONTRACTING_VOL,
        )


# ─────────────────────────────────────────────────────────────────────────────
# RegimeDetector
# ─────────────────────────────────────────────────────────────────────────────

class RegimeDetector:
    """Market regime classifier with multi-axis decomposition and transition tracking.

    Operates on a list of NormalizedCandle objects. Classifies the market
    into one of 16 named regimes. Maintains a per-(symbol, timeframe) history
    of past regimes so callers can detect regime *transitions*.

    Parameters
    ----------
    min_candles : int
        Minimum candles required for a reliable classification. Default 50.
    history_size : int
        How many past regimes to remember per symbol. Default 20.
    """

    def __init__(self, min_candles: int = 50, history_size: int = 20) -> None:
        self.min_candles = min_candles
        self.history_size = history_size
        # (symbol, timeframe) -> deque[Regime]
        self._history: dict[tuple[str, TimeFrame], list[Regime]] = {}
        # (symbol, timeframe) -> Regime (last seen)
        self._last_regime: dict[tuple[str, TimeFrame], Regime] = {}
        logger.info("RegimeDetector initialized", min_candles=min_candles, history_size=history_size)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def detect(
        self,
        candles: list[NormalizedCandle],
        symbol: str,
        timeframe: TimeFrame | str,
    ) -> RegimeAnalysis:
        """Detect market regime for a symbol/timeframe (stateless — no transition).

        See `detect_with_transition` if you also need regime-change detection.
        """
        return self.detect_with_transition(candles, symbol, timeframe, track_transition=False)

    def detect_with_transition(
        self,
        candles: list[NormalizedCandle],
        symbol: str,
        timeframe: TimeFrame | str,
        track_transition: bool = True,
    ) -> RegimeAnalysis:
        """Detect market regime with optional transition tracking.

        When `track_transition=True` (default), the detector remembers the
        last regime per (symbol, timeframe) and emits a `RegimeTransition`
        on the result when the regime changes. Transitions of MAJOR or
        CRITICAL severity should pause the bot for human review.
        """
        tf = TimeFrame(timeframe) if isinstance(timeframe, str) else timeframe
        key = (symbol, tf)

        if len(candles) < self.min_candles:
            logger.warning(
                "Insufficient candles for regime detection",
                symbol=symbol,
                have=len(candles),
                need=self.min_candles,
            )
            return RegimeAnalysis(
                regime=Regime.UNKNOWN,
                symbol=symbol,
                timeframe=tf,
                confidence=0.0,
                indicators={"error": "insufficient_candles"},
            )

        try:
            # Compute raw indicators
            adx = self._adx(candles)
            ema_fast_val = self._ema(candles, 20)
            ema_slow_val = self._ema(candles, 50)
            atr_ratio = self._atr_ratio(candles)
            volume_ratio = self._volume_ratio(candles)
            bb_width_ratio = self._bollinger_width_ratio(candles)
            ema_trend = self._ema_trend_ratio(candles, 20, 50)
            ema_separation_pct = abs(ema_fast_val - ema_slow_val) / ema_slow_val * 100 if ema_slow_val else 0.0

            indicators = {
                "adx": round(adx, 4),
                "ema_fast": round(ema_fast_val, 4),
                "ema_slow": round(ema_slow_val, 4),
                "atr_ratio": round(atr_ratio, 4),
                "volume_ratio": round(volume_ratio, 4),
                "bb_width_ratio": round(bb_width_ratio, 4),
                "ema_trend_ratio": round(ema_trend, 4),
                "ema_separation_pct": round(ema_separation_pct, 4),
            }

            # Decompose into axis states, then map to named regime
            trend_state = self._classify_trend(adx, ema_fast_val, ema_slow_val, ema_trend)
            vol_state = self._classify_volatility(atr_ratio, bb_width_ratio)
            liquidity_state = self._classify_liquidity(volume_ratio)

            regime, confidence = self._map_regime(
                trend=trend_state,
                vol=vol_state,
                liquidity=liquidity_state,
                atr_ratio=atr_ratio,
                volume_ratio=volume_ratio,
            )

            result = RegimeAnalysis(
                regime=regime,
                symbol=symbol,
                timeframe=tf,
                confidence=confidence,
                indicators=indicators,
                trend_state=trend_state,
                vol_state=vol_state,
                liquidity_state=liquidity_state,
                trend_strength=adx,
                volatility_ratio=atr_ratio,
                volume_ratio=volume_ratio,
                ema_fast=ema_fast_val,
                ema_slow=ema_slow_val,
                ema_separation_pct=ema_separation_pct,
            )

            # Transition tracking
            if track_transition:
                prev = self._last_regime.get(key)
                if prev is not None and prev != regime:
                    result.previous_regime = prev
                    result.transition = self._compute_transition(prev, regime, trend_state, vol_state, liquidity_state)
                    if result.transition.requires_review:
                        logger.warning(
                            "Regime transition requires review",
                            symbol=symbol,
                            previous=prev.value,
                            current=regime.value,
                            severity=result.transition.severity.value,
                            score=result.transition.score,
                        )
                # Update history
                history = self._history.setdefault(key, [])
                history.append(regime)
                if len(history) > self.history_size:
                    history.pop(0)
                self._last_regime[key] = regime

            logger.debug(
                "Regime detected",
                symbol=symbol,
                regime=regime.value,
                confidence=confidence,
                trend=trend_state.value,
                vol=vol_state.value,
                liquidity=liquidity_state.value,
                **indicators,
            )
            return result

        except Exception as exc:
            logger.error(
                "Regime detection failed",
                symbol=symbol,
                error=str(exc),
                exc_info=True,
            )
            return RegimeAnalysis(
                regime=Regime.UNKNOWN,
                symbol=symbol,
                timeframe=tf,
                confidence=0.0,
                indicators={"error": str(exc)},
            )

    def get_history(self, symbol: str, timeframe: TimeFrame | str) -> list[Regime]:
        """Return the recent regime history for a symbol/timeframe (oldest first)."""
        tf = TimeFrame(timeframe) if isinstance(timeframe, str) else timeframe
        return list(self._history.get((symbol, tf), []))

    def reset_history(self, symbol: str | None = None, timeframe: TimeFrame | str | None = None) -> None:
        """Clear regime history. Pass both args to clear one key, or neither to clear all."""
        if symbol is None and timeframe is None:
            self._history.clear()
            self._last_regime.clear()
        else:
            if timeframe is not None:
                tf = TimeFrame(timeframe) if isinstance(timeframe, str) else timeframe
            else:
                tf = None
            keys_to_clear = [k for k in self._history if (symbol is None or k[0] == symbol) and (tf is None or k[1] == tf)]
            for k in keys_to_clear:
                self._history.pop(k, None)
                self._last_regime.pop(k, None)

    # ─────────────────────────────────────────────────────────────────────────
    # Axis classification
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_trend(adx: float, ema_fast: float, ema_slow: float, ema_trend: float) -> TrendState:
        """Classify trend axis from ADX and EMA geometry."""
        # Choppy: low ADX with EMAs close together (whipsaw)
        if adx < 20 and abs(ema_trend - 1.0) < 0.005:
            return TrendState.CHOPPY
        # Ranging: low ADX but EMAs not crossing
        if adx < 20:
            return TrendState.RANGING
        # Strong trend
        if adx >= 30:
            return TrendState.STRONG
        # 20 <= ADX < 30 — weak trend
        return TrendState.WEAK

    @staticmethod
    def _classify_volatility(atr_ratio: float, bb_width_ratio: float) -> VolState:
        """Classify volatility axis from ATR ratio and Bollinger width ratio."""
        # Spike: extreme single-bar expansion
        if atr_ratio > 3.0 or bb_width_ratio > 4.0:
            return VolState.SPIKE
        # Expanding: ATR well above its average
        if atr_ratio > 1.3 or bb_width_ratio > 1.3:
            return VolState.EXPANDING
        # Contracting: ATR well below average
        if atr_ratio < 0.7 or bb_width_ratio < 0.7:
            return VolState.CONTRACTING
        # Within ±30% of average
        return VolState.STABLE

    @staticmethod
    def _classify_liquidity(volume_ratio: float) -> LiquidityState:
        """Classify liquidity axis from current volume vs 20-period average."""
        if volume_ratio > 3.0:
            return LiquidityState.ANOMALOUS
        if volume_ratio > 1.5:
            return LiquidityState.DEEP
        if volume_ratio < 0.5:
            return LiquidityState.THIN
        return LiquidityState.NORMAL

    # ─────────────────────────────────────────────────────────────────────────
    # Regime mapping (axis states → named regime)
    # ─────────────────────────────────────────────────────────────────────────────

    def _map_regime(
        self,
        trend: TrendState,
        vol: VolState,
        liquidity: LiquidityState,
        atr_ratio: float,
        volume_ratio: float,
    ) -> tuple[Regime, float]:
        """Map (trend, vol, liquidity) axes to a named regime + confidence.

        Confidence rises with how extreme the inputs are on each axis.
        """

        # ── Safety states first (always win) ─────────────────────────────────
        if liquidity == LiquidityState.THIN and vol == VolState.SPIKE:
            return Regime.MARKET_DISTORTION, 0.95
        if liquidity == LiquidityState.THIN:
            return Regime.LIQUIDITY_CRISIS, 0.90
        if liquidity == LiquidityState.ANOMALOUS:
            return Regime.VOLUME_ANOMALY, 0.80
        if vol == VolState.SPIKE:
            return Regime.VOL_SPIKE, 0.85

        # ── Trend × Vol matrix (liquidity already filtered to NORMAL/DEEP) ────
        if trend == TrendState.STRONG:
            if vol == VolState.EXPANDING:
                return Regime.STRONG_TREND_EXPANDING_VOL, 0.85
            if vol in (VolState.STABLE, VolState.CONTRACTING):
                # Contracting is unusual for strong trend; lower confidence
                conf = 0.85 if vol == VolState.STABLE else 0.65
                return Regime.STRONG_TREND_STABLE_VOL, conf
            return Regime.STRONG_TREND_STABLE_VOL, 0.70

        if trend == TrendState.WEAK:
            if vol == VolState.CONTRACTING:
                return Regime.WEAK_TREND_CONTRACTING_VOL, 0.70
            if vol in (VolState.STABLE, VolState.EXPANDING):
                conf = 0.70 if vol == VolState.STABLE else 0.65
                return Regime.WEAK_TREND_STABLE_VOL, conf
            return Regime.WEAK_TREND_STABLE_VOL, 0.60

        if trend == TrendState.RANGING:
            if vol == VolState.CONTRACTING:
                return Regime.RANGING_LOW_VOL, 0.75
            if vol == VolState.EXPANDING:
                # Heuristic: if vol expansion is large but ADX low, possibly BREAKOUT_ATTEMPT
                if atr_ratio > 1.7 and volume_ratio > 1.5:
                    return Regime.BREAKOUT_ATTEMPT, 0.65
                return Regime.RANGING_HIGH_VOL, 0.70
            if vol == VolState.STABLE:
                return Regime.RANGING_STABLE_VOL, 0.75
            return Regime.RANGING_STABLE_VOL, 0.60

        if trend == TrendState.CHOPPY:
            if vol == VolState.CONTRACTING:
                return Regime.CHOPPY_CONTRACTING_VOL, 0.85
            if vol == VolState.EXPANDING:
                return Regime.CHOPPY_EXPANDING_VOL, 0.75
            return Regime.CHOPPY_CONTRACTING_VOL, 0.65

        # Should not reach here, but be safe
        return Regime.UNKNOWN, 0.30

    # ─────────────────────────────────────────────────────────────────────────
    # Transition detection
    # ─────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_transition(
        previous: Regime,
        current: Regime,
        trend: TrendState,
        vol: VolState,
        liquidity: LiquidityState,
    ) -> RegimeTransition:
        """Compute severity of a regime change.

        Severity is the count of changed axes (trend / vol / liquidity) plus
        a 0.5 bonus if the current regime is a *dangerous* state
        (LIQUIDITY_CRISIS, MARKET_DISTORTION).
        """
        prev_axes = _regime_to_axes(previous)
        curr_axes = _regime_to_axes(current)

        trend_changed = prev_axes[0] != curr_axes[0] and curr_axes[0] is not None and prev_axes[0] is not None
        vol_changed = prev_axes[1] != curr_axes[1] and curr_axes[1] is not None and prev_axes[1] is not None
        liquidity_changed = prev_axes[2] != curr_axes[2] and curr_axes[2] is not None and prev_axes[2] is not None

        changed_count = sum([trend_changed, vol_changed, liquidity_changed])
        score = changed_count / 3.0

        # Bonus for entering a danger state
        if current in (Regime.LIQUIDITY_CRISIS, Regime.MARKET_DISTORTION):
            score = min(1.0, score + 0.4)
        # Penalty for going from a danger state to safety (less severe)
        if previous in (Regime.LIQUIDITY_CRISIS, Regime.MARKET_DISTORTION) and current not in (
            Regime.LIQUIDITY_CRISIS,
            Regime.MARKET_DISTORTION,
        ):
            score = max(0.0, score - 0.2)

        if score >= 0.9:
            severity = TransitionSeverity.CRITICAL
        elif score >= 0.6:
            severity = TransitionSeverity.MAJOR
        elif score >= 0.3:
            severity = TransitionSeverity.MODERATE
        elif score > 0.0:
            severity = TransitionSeverity.MINOR
        else:
            severity = TransitionSeverity.NONE

        return RegimeTransition(
            previous=previous,
            current=current,
            severity=severity,
            score=score,
            trend_changed=trend_changed,
            vol_changed=vol_changed,
            liquidity_changed=liquidity_changed,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Indicator computations
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _ema(candles: list[NormalizedCandle], period: int) -> float:
        """Compute EMA for close prices."""
        if len(candles) < period:
            return candles[-1].close if candles else 0.0

        closes = np.array([c.close for c in candles[-period:]])
        mult = 2.0 / (period + 1)
        val = float(closes[0])
        for price in closes[1:]:
            val = (float(price) - val) * mult + val
        return val

    @staticmethod
    def _sma(values: np.ndarray, period: int) -> float:
        """Simple moving average."""
        if len(values) < period:
            return float(values.mean()) if len(values) > 0 else 0.0
        return float(values[-period:].mean())

    def _adx(self, candles: list[NormalizedCandle], period: int = 14) -> float:
        """Average Directional Index — measures trend strength (0–100)."""
        if len(candles) < period + 2:
            return 0.0

        highs = np.array([c.high for c in candles])
        lows = np.array([c.low for c in candles])
        closes = np.array([c.close for c in candles])

        # True range
        trs = []
        for i in range(1, min(period + 2, len(candles))):
            c = candles[-i]
            p = candles[-i - 1]
            tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            trs.append(tr)

        trs = np.array(trs[-period:])
        atr = float(trs.mean()) if len(trs) > 0 else 0.0
        if atr == 0:
            return 0.0

        # Directional movement
        plus_dm = []
        minus_dm = []
        for i in range(1, min(period + 2, len(candles))):
            c = candles[-i]
            p = candles[-i - 1]
            up_move = c.high - p.high
            down_move = p.low - c.low
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        plus_dm = np.array(plus_dm[-period:])
        minus_dm = np.array(minus_dm[-period:])

        plus_di = 100 * float(plus_dm.mean()) / atr if atr > 0 else 0.0
        minus_di = 100 * float(minus_dm.mean()) / atr if atr > 0 else 0.0

        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0.0

        dx = 100 * abs(plus_di - minus_di) / di_sum

        # Smooth DX into ADX (simplified — single-pass EMA)
        adx = dx  # for short lookback, use raw DX
        return float(adx)

    def _atr_ratio(self, candles: list[NormalizedCandle], period: int = 14) -> float:
        """ATR as a percentage of close price, vs its 20-period SMA."""
        if len(candles) < period + 2:
            return 1.0

        atr = self._atr(candles, period)
        closes = np.array([c.close for c in candles])
        atr_pct = (atr / closes[-1]) * 100 if closes[-1] != 0 else 0.0

        # Compare to 20-period average ATR%
        atr_pcts = []
        for i in range(period, min(len(candles), period + 20)):
            window = candles[i - period:i + 1]
            window_atr = self._atr(window, period)
            window_atr_pct = (window_atr / window[-1].close) * 100 if window[-1].close != 0 else 0.0
            atr_pcts.append(window_atr_pct)

        if not atr_pcts:
            return 1.0
        avg_atr_pct = np.mean(atr_pcts)
        return float(atr_pct / avg_atr_pct) if avg_atr_pct > 0 else 1.0

    @staticmethod
    def _atr(candles: list[NormalizedCandle], period: int = 14) -> float:
        """Average True Range."""
        if len(candles) < period + 1:
            return 0.0
        trs = []
        for i in range(1, min(period + 1, len(candles))):
            c = candles[-i]
            p = candles[-i - 1]
            tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            trs.append(tr)
        return float(np.mean(trs)) if trs else 0.0

    def _volume_ratio(self, candles: list[NormalizedCandle], period: int = 20) -> float:
        """Current volume vs 20-period volume SMA."""
        if len(candles) < period:
            return 1.0

        volumes = np.array([c.volume for c in candles[-period:]])
        current_vol = volumes[-1]
        avg_vol = float(volumes.mean())
        return float(current_vol / avg_vol) if avg_vol > 0 else 1.0

    def _bollinger_width_ratio(
        self, candles: list[NormalizedCandle], period: int = 20, std_dev: float = 2.0
    ) -> float:
        """Current Bollinger Bandwidth vs its 20-period SMA."""
        if len(candles) < period:
            return 1.0

        closes = np.array([c.close for c in candles[-period:]])
        sma = float(closes.mean())
        std = float(closes.std())
        current_width = (sma + std_dev * std) - (sma - std_dev * std)  # = 2 * std_dev * std

        widths = []
        for i in range(period, min(len(candles), period + 20)):
            window = np.array([c.close for c in candles[i - period:i]])
            w_sma = float(window.mean())
            w_std = float(window.std())
            widths.append(2 * std_dev * w_std)

        if not widths:
            return 1.0
        avg_width = np.mean(widths)
        return float(current_width / avg_width) if avg_width > 0 else 1.0

    def _ema_trend_ratio(self, candles: list[NormalizedCandle], fast: int, slow: int) -> float:
        """Ratio of EMA(fast) / EMA(slow) — >1 means bullish, <1 means bearish."""
        if len(candles) < slow:
            return 1.0
        ema_f = self._ema(candles, fast)
        ema_s = self._ema(candles, slow)
        return float(ema_f / ema_s) if ema_s > 0 else 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Map each named regime to its decomposed (trend, vol, liquidity) axes.
# Used by the transition detector to know what changed.
_REGIME_AXIS_MAP: dict[Regime, tuple[TrendState | None, VolState | None, LiquidityState | None]] = {
    Regime.STRONG_TREND_STABLE_VOL: (TrendState.STRONG, VolState.STABLE, LiquidityState.NORMAL),
    Regime.STRONG_TREND_EXPANDING_VOL: (TrendState.STRONG, VolState.EXPANDING, LiquidityState.NORMAL),
    Regime.WEAK_TREND_STABLE_VOL: (TrendState.WEAK, VolState.STABLE, LiquidityState.NORMAL),
    Regime.WEAK_TREND_CONTRACTING_VOL: (TrendState.WEAK, VolState.CONTRACTING, LiquidityState.NORMAL),
    Regime.RANGING_STABLE_VOL: (TrendState.RANGING, VolState.STABLE, LiquidityState.NORMAL),
    Regime.RANGING_LOW_VOL: (TrendState.RANGING, VolState.CONTRACTING, LiquidityState.NORMAL),
    Regime.RANGING_HIGH_VOL: (TrendState.RANGING, VolState.EXPANDING, LiquidityState.NORMAL),
    Regime.CHOPPY_CONTRACTING_VOL: (TrendState.CHOPPY, VolState.CONTRACTING, LiquidityState.NORMAL),
    Regime.CHOPPY_EXPANDING_VOL: (TrendState.CHOPPY, VolState.EXPANDING, LiquidityState.NORMAL),
    Regime.BREAKOUT_ATTEMPT: (TrendState.RANGING, VolState.EXPANDING, LiquidityState.NORMAL),
    Regime.REVERSAL_SETUP: (TrendState.RANGING, VolState.CONTRACTING, LiquidityState.NORMAL),
    Regime.VOL_SPIKE: (None, VolState.SPIKE, LiquidityState.NORMAL),
    Regime.LIQUIDITY_CRISIS: (None, None, LiquidityState.THIN),
    Regime.VOLUME_ANOMALY: (None, None, LiquidityState.ANOMALOUS),
    Regime.MARKET_DISTORTION: (None, VolState.SPIKE, LiquidityState.THIN),
    Regime.UNKNOWN: (None, None, None),
}


def _regime_to_axes(regime: Regime) -> tuple[TrendState | None, VolState | None, LiquidityState | None]:
    """Return (trend, vol, liquidity) axes for a regime. None means 'unspecified'."""
    return _REGIME_AXIS_MAP.get(regime, (None, None, None))
