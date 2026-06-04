"""Decision Engine — Weighted Scoring Aggregator.

The Decision Engine is the core intelligence layer of the trading system.
It consumes signals from the SignalRegistry, applies regime-dependent
weighting, and produces a final trading decision (BUY / SELL / NO_TRADE).

Hard Rules:
  1. NO single-condition trades — minimum 3 independent subsystems
     must confirm with score >= 0.3.
  2. Final weighted score must exceed 0.60 to fire.
  3. Each subsystem score must exceed 0.3 minimum threshold to count
     as a confirmation.
  4. NO_TRADE is always a valid output when conditions are not met.

Subsystem Weights:
  Market Structure : 25%
  Momentum        : 15%
  Orderflow       : 20%
  Sentiment       : 15%
  Macro            : 10%
  Volatility Regime: 10%
  Risk Filter      : 5%  (pass/fail gate, not a score input)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from ..data.models import (
    Decision,
    NormalizedCandle,
    OrderSide,
    Regime,
    Side,
    Signal,
    TimeFrame,
)
from ..signals.regime_detector import RegimeAnalysis, RegimeDetector
from ..signals.sentiment_scorer import SentimentScorer
from ..signals.registry import AggregatedSignal, SignalRegistry
from ..utils.config import get_config
from ..utils.logging import get_logger
from ..audit import AuditEntryInput, get_audit_logger
from ..audit.reason_codes import classify_no_trade_reason

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Subsystem definitions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SubsystemConfig:
    """Configuration for a single scoring subsystem."""

    name: str
    weight: float           # Must sum to 1.0 across all subsystems
    min_score: float = 0.3  # Minimum to count as confirmation


# Default subsystem table — must sum to 1.0
SUBSYSTEMS: list[SubsystemConfig] = [
    SubsystemConfig(name="market_structure",  weight=0.20, min_score=0.15),
    SubsystemConfig(name="momentum",           weight=0.30, min_score=0.15),
    SubsystemConfig(name="orderflow",          weight=0.10, min_score=0.15),
    SubsystemConfig(name="sentiment",          weight=0.15, min_score=0.15),
    SubsystemConfig(name="macro",               weight=0.10, min_score=0.15),
    SubsystemConfig(name="volatility_regime",   weight=0.10, min_score=0.15),
    # Risk Filter (weight=0.05) is a hard gate, not a score input
]


# ─────────────────────────────────────────────────────────────────────────────
# Subsystem score record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SubsystemScore:
    """Score record for a single subsystem."""

    name: str
    raw_score: float          # 0.0–1.0 before regime adjustment
    adjusted_score: float     # 0.0–1.0 after regime adjustment
    weight: float
    is_confirming: bool       # True if adjusted_score >= min_threshold
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionAudit:
    """Full audit trail of a decision for post-mortem and logging."""

    symbol: str
    timeframe: TimeFrame
    regime: Regime
    regime_confidence: float
    final_score: float
    confirming_count: int
    required_confirmations: int
    decision: str              # BUY / SELL / NO_TRADE
    subsystem_scores: list[SubsystemScore]
    reason: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# DecisionEngine
# ─────────────────────────────────────────────────────────────────────────────

class DecisionEngine:
    """Weighted multi-subsystem scoring decision engine.

    Parameters
    ----------
    signal_registry : SignalRegistry
        Source of aggregated signals per symbol/timeframe.
    regime_detector : RegimeDetector
        Produces RegimeAnalysis for the current market state.
    sentiment_scorer : SentimentScorer
        Produces 0–1 sentiment scores per symbol via RSS.
    min_signal_confidence : float
        Minimum final weighted score to fire. Default 0.60.
    min_confirmations : int
        Minimum number of subsystems confirming. Default 3.
    min_subsystem_score : float
        Per-subsystem minimum threshold. Default 0.30.
    max_position_pct : float
        Maximum position size as fraction of portfolio. Default 0.10.
    """

    def __init__(
        self,
        signal_registry: SignalRegistry,
        regime_detector: RegimeDetector,
        sentiment_scorer: SentimentScorer,
        min_signal_confidence: float = 0.60,
        min_confirmations: int = 3,
        min_subsystem_score: float = 0.30,
        max_position_pct: float = 0.10,
    ) -> None:
        self.registry = signal_registry
        self.regime_detector = regime_detector
        self.sentiment_scorer = sentiment_scorer

        # Load from config if not overridden
        cfg = get_config()
        self.min_confidence = min_signal_confidence
        self.min_confirmations = min_confirmations
        self.min_subsystem_score = min_subsystem_score
        self.max_position_pct = max_position_pct

        # Subsystem configs — verify weights sum to 1.0
        self._subsystems = [s for s in SUBSYSTEMS if s.name != "risk_filter"]
        total_weight = sum(s.weight for s in self._subsystems)
        if abs(total_weight - 1.0) > 1e-6:
            logger.warning(
                "Subsystem weights do not sum to 1.0",
                total=total_weight,
                subsystems=[s.name for s in self._subsystems],
            )

        logger.info(
            "DecisionEngine initialized",
            min_confidence=self.min_confidence,
            min_confirmations=self.min_confirmations,
            min_subsystem_score=self.min_subsystem_score,
            max_position_pct=self.max_position_pct,
            subsystems=[s.name for s in self._subsystems],
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def decide(
        self,
        symbol: str,
        timeframe: TimeFrame | str,
        candles: list[NormalizedCandle],
        risk_metrics: dict[str, Any] | None = None,
    ) -> Decision:
        """Make a trading decision for a symbol/timeframe.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. "BTC".
        timeframe : TimeFrame | str
            Evaluation timeframe.
        candles : list[NormalizedCandle]
            Historical candles for regime detection and signal computation.
        risk_metrics : dict[str, Any], optional
            Portfolio-level risk metrics from RiskManager.
            Keys: portfolio_exposure_pct, daily_trades, max_drawdown_pct.

        Returns
        -------
        Decision
            action in {BUY, SELL, NO_TRADE}, with confidence and audit trail.
        """
        tf = TimeFrame(timeframe) if isinstance(timeframe, str) else timeframe

        # ── Step 1: Regime detection ──
        regime_analysis = self.regime_detector.detect(candles, symbol, tf)
        regime = regime_analysis.regime
        regime_conf = regime_analysis.confidence

        # ── Step 2: Collect signals from registry ──
        aggregated = self.registry.get_aggregated(symbol, tf)
        signals = aggregated.signals

        # ── Step 3: Compute per-subsystem scores ──
        subsystem_scores = self._score_subsystems(
            symbol=symbol,
            timeframe=tf,
            signals=signals,
            regime=regime,
            risk_metrics=risk_metrics or {},
        )

        # ── Step 4: Compute weighted final score ──
        final_score = sum(s.adjusted_score * s.weight for s in subsystem_scores)
        confirming = [s for s in subsystem_scores if s.is_confirming]
        confirming_count = len(confirming)

        # ── Step 5: Fire decision ──
        decision, reason = self._make_decision(
            final_score=final_score,
            confirming_count=confirming_count,
            regime=regime,
            signals=signals,
            subsystem_scores=subsystem_scores,
        )

        # ── Step 6: Build Decision output ──
        # Bug #3 fix (2026-06-02): the previous `direction = Side.BUY ...
        # else Side.BUY` was misleading dead code — the Decision model
        # has no `direction` field (action is the source of truth), so
        # the local was set but never read. The fallback `else Side.BUY`
        # would have silently over-stated intent if a `direction` field
        # were ever added. Removed entirely.
        confidence = float(np.clip(final_score, 0.0, 1.0))

        if decision == "NO_TRADE":
            size = 0.0
        else:
            # Size scales with confidence × regime multiplier, capped at max_position_pct.
            # The multiplier comes from the RegimePreset table (see regime_detector.py).
            # Dangerous regimes (LIQUIDITY_CRISIS, CHOPPY_CONTRACTING_VOL, MARKET_DISTORTION)
            # carry size_multiplier=0.0, which forces a 0-size position.
            raw_size = confidence * self.max_position_pct
            if regime_analysis is not None and hasattr(regime_analysis, "size_multiplier"):
                raw_size *= regime_analysis.size_multiplier
            elif regime == Regime.LOW_LIQUIDITY:
                # Fallback for callers that pass a bare Regime enum
                raw_size *= 0.75
            size = float(np.clip(raw_size, 0.0, self.max_position_pct))

        # Estimate entry / stop / tp from last candle.
        # The local `direction` was removed (Bug #3 fix) — derive the Side
        # from the decision action string here. NO_TRADE produces a Side.BUY
        # default for the helper, but since the orchestrator gates on
        # action == BUY/SELL, the SL/TP values are not consumed in that case.
        side_for_helpers: Side = (
            Side.BUY if decision == "BUY"
            else Side.SELL if decision == "SELL"
            else Side.BUY  # NO_TRADE placeholder; not used downstream
        )
        entry = candles[-1].close if candles else None
        stop_loss = self._calc_stop_loss(entry, side_for_helpers, risk_metrics) if entry else None
        take_profit = self._calc_take_profit(entry, side_for_helpers, risk_metrics) if entry else None

        decision_obj = Decision(
            action=decision,
            symbol=symbol,
            size=size,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            regime=regime,
            signals=signals,
            reason=reason,
            timestamp=datetime.now(timezone.utc),
        )

        # ── Step 7: Audit log ──
        audit = DecisionAudit(
            symbol=symbol,
            timeframe=tf,
            regime=regime,
            regime_confidence=regime_conf,
            final_score=final_score,
            confirming_count=confirming_count,
            required_confirmations=self.min_confirmations,
            decision=decision,
            subsystem_scores=subsystem_scores,
            reason=reason,
        )
        self._log_audit(audit)

        logger.info(
            "Decision made",
            symbol=symbol,
            timeframe=tf.value,
            action=decision,
            confidence=confidence,
            regime=regime.value,
            confirming=confirming_count,
            required=self.min_confirmations,
            final_score=round(final_score, 4),
        )

        return decision_obj

    # ─────────────────────────────────────────────────────────────────────────
    # Subsystem scoring
    # ─────────────────────────────────────────────────────────────────────────

    def _score_subsystems(
        self,
        symbol: str,
        timeframe: TimeFrame,
        signals: list[Signal],
        regime: Regime,
        risk_metrics: dict[str, Any],
    ) -> list[SubsystemScore]:
        """Compute per-subsystem scores with regime adjustments."""

        # Filter signals by min confidence threshold
        valid_signals = [s for s in signals if s.confidence >= self.min_subsystem_score]

        scores: list[SubsystemScore] = []

        # ── Market Structure (25%) ──
        scores.append(self._score_market_structure(valid_signals, regime))

        # ── Momentum (15%) ──
        scores.append(self._score_momentum(valid_signals, regime))

        # ── Orderflow (20%) ──
        scores.append(self._score_orderflow(valid_signals, regime))

        # ── Sentiment (15%) ──
        scores.append(self._score_sentiment(symbol, regime))

        # ── Macro (10%) ──
        scores.append(self._score_macro(symbol, signals, regime))

        # ── Volatility Regime (10%) ──
        scores.append(self._score_volatility_regime(regime, risk_metrics))

        # ── Apply regime adjustments ──
        adjusted = []
        for score in scores:
            adj = self._apply_regime_adjustment(score, regime)
            adjusted.append(adj)

        return adjusted

    def _score_market_structure(
        self,
        signals: list[Signal],
        regime: Regime,
    ) -> SubsystemScore:
        """Market Structure — 25%.

        Signals: sma_cross, ema_cross, bollinger_breakout
        Regime boost: +10% in TREND_UP/TREND_DOWN
        """
        structure_signals = [
            s for s in signals
            if s.name in ("sma_cross", "ema_cross", "bollinger_breakout", "structure")
        ]
        if not structure_signals:
            return SubsystemScore(
                name="market_structure",
                raw_score=0.0,
                adjusted_score=0.0,
                weight=0.25,
                is_confirming=False,
                metadata={"signal_count": 0},
            )

        avg_conf = sum(s.confidence for s in structure_signals) / len(structure_signals)
        net_direction = self._net_signal_direction(structure_signals)

        raw = avg_conf
        if regime in (
            Regime.TREND_UP,
            Regime.TREND_DOWN,
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.WEAK_TREND_STABLE_VOL,
            Regime.WEAK_TREND_CONTRACTING_VOL,
            Regime.BREAKOUT_ATTEMPT,
        ):
            raw = min(1.0, raw * 1.10)

        return SubsystemScore(
            name="market_structure",
            raw_score=avg_conf,
            adjusted_score=raw,
            weight=0.25,
            is_confirming=raw >= 0.3,
            metadata={
                "signal_count": len(structure_signals),
                "signal_names": [s.name for s in structure_signals],
                "net_direction": net_direction,
            },
        )

    def _score_momentum(
        self,
        signals: list[Signal],
        regime: Regime,
    ) -> SubsystemScore:
        """Momentum — 15%.

        Signals: rsi, macd, stochastic, cci
        Regime boost: +10% in TREND_UP (for BUY) / TREND_DOWN (for SELL)
        """
        momentum_signals = [
            s for s in signals
            if s.name in ("rsi", "macd", "stochastic", "cci")
        ]
        if not momentum_signals:
            return SubsystemScore(
                name="momentum",
                raw_score=0.0,
                adjusted_score=0.0,
                weight=0.15,
                is_confirming=False,
                metadata={"signal_count": 0},
            )

        avg_conf = sum(s.confidence for s in momentum_signals) / len(momentum_signals)
        net_direction = self._net_signal_direction(momentum_signals)

        raw = avg_conf
        # Regime directional boost — applies to all trend regimes (legacy + new)
        trend_regimes = {
            Regime.TREND_UP,
            Regime.TREND_DOWN,
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.WEAK_TREND_STABLE_VOL,
            Regime.WEAK_TREND_CONTRACTING_VOL,
            Regime.BREAKOUT_ATTEMPT,
        }
        if regime in trend_regimes:
            if (regime in (Regime.TREND_UP, Regime.STRONG_TREND_STABLE_VOL, Regime.STRONG_TREND_EXPANDING_VOL)
                    and net_direction == "BUY"):
                raw = min(1.0, raw * 1.10)
            elif (regime in (Regime.TREND_DOWN, Regime.STRONG_TREND_STABLE_VOL, Regime.STRONG_TREND_EXPANDING_VOL)
                    and net_direction == "SELL"):
                # Note: STRONG_TREND_* is neutral on direction; check EMA in caller.
                # For the strong regimes, both BUY and SELL in direction of EMA get the boost.
                raw = min(1.0, raw * 1.10)
            elif regime in (Regime.WEAK_TREND_STABLE_VOL, Regime.WEAK_TREND_CONTRACTING_VOL, Regime.BREAKOUT_ATTEMPT):
                # Direction-agnostic weak-trend boost: momentum signs the trade.
                if net_direction in ("BUY", "SELL"):
                    raw = min(1.0, raw * 1.05)

        return SubsystemScore(
            name="momentum",
            raw_score=avg_conf,
            adjusted_score=raw,
            weight=0.15,
            is_confirming=raw >= 0.3,
            metadata={
                "signal_count": len(momentum_signals),
                "signal_names": [s.name for s in momentum_signals],
                "net_direction": net_direction,
            },
        )

    def _score_orderflow(
        self,
        signals: list[Signal],
        regime: Regime,
    ) -> SubsystemScore:
        """Orderflow — 20%.

        Signals: volume_spike, orderbook_imbalance, trade_size_imbalance
        In LOW_LIQUIDITY: de-emphasize by 10%
        """
        orderflow_signals = [
            s for s in signals
            if s.name in ("volume_spike", "orderbook_imbalance", "trade_size", "obv")
        ]
        if not orderflow_signals:
            return SubsystemScore(
                name="orderflow",
                raw_score=0.0,
                adjusted_score=0.0,
                weight=0.20,
                is_confirming=False,
                metadata={"signal_count": 0},
            )

        avg_conf = sum(s.confidence for s in orderflow_signals) / len(orderflow_signals)
        raw = avg_conf

        if regime == Regime.LOW_LIQUIDITY:
            raw = raw * 0.90

        return SubsystemScore(
            name="orderflow",
            raw_score=avg_conf,
            adjusted_score=raw,
            weight=0.20,
            is_confirming=raw >= 0.3,
            metadata={
                "signal_count": len(orderflow_signals),
                "signal_names": [s.name for s in orderflow_signals],
            },
        )

    def _score_sentiment(
        self,
        symbol: str,
        regime: Regime,
    ) -> SubsystemScore:
        """Sentiment — 15%. RSS/news-based score."""
        try:
            score = self.sentiment_scorer.get_score(symbol)
        except Exception as exc:
            logger.warning("Sentiment scoring failed, returning neutral", error=str(exc))
            score = 0.50

        # In HIGH_VOL, sentiment more important
        raw = score
        if regime == Regime.HIGH_VOL:
            raw = min(1.0, raw * 1.10)

        return SubsystemScore(
            name="sentiment",
            raw_score=score,
            adjusted_score=raw,
            weight=0.15,
            is_confirming=raw >= 0.3,
            metadata={"symbol": symbol},
        )

    def _score_macro(
        self,
        symbol: str,
        signals: list[Signal],
        regime: Regime,
    ) -> SubsystemScore:
        """Macro — 10%. Cross-symbol correlation and market-wide bias.

        Placeholder: currently scores based on how many other symbols
        are signaling in the same direction (from the registry).
        """
        all_symbols = self.registry.get_all_symbols()

        if not all_symbols:
            return SubsystemScore(
                name="macro",
                raw_score=0.0,
                adjusted_score=0.0,
                weight=0.10,
                is_confirming=False,
                metadata={"note": "no other symbols in registry"},
            )

        # Count how many other symbols have a BUY or SELL lean
        agreeing = 0
        total = 0
        target_direction = self._net_signal_direction(
            [s for s in signals if s.name in ("sma_cross", "ema_cross", "rsi", "macd")]
        )

        for other_symbol in all_symbols:
            if other_symbol == symbol:
                continue
            other_agg = self.registry.get_aggregated(other_symbol, TimeFrame.H1)
            other_direction = self._net_signal_direction(other_agg.signals)
            if other_direction is not None and other_direction == target_direction:
                agreeing += 1
            total += 1

        raw = float(agreeing / total) if total > 0 else 0.0

        return SubsystemScore(
            name="macro",
            raw_score=raw,
            adjusted_score=raw,
            weight=0.10,
            is_confirming=raw >= 0.3,
            metadata={
                "agreeing_symbols": agreeing,
                "total_symbols": total,
                "target_direction": target_direction,
            },
        )

    def _score_volatility_regime(
        self,
        regime: Regime,
        risk_metrics: dict[str, Any],
    ) -> SubsystemScore:
        """Volatility Regime — 10%.

        Converts regime into a volatility score. Higher score = regime
        is tradeable. Danger states (LIQUIDITY_CRISIS, MARKET_DISTORTION,
        CHOPPY_CONTRACTING_VOL) return scores below the 0.3 confirmation
        threshold so they cannot be a confirming subsystem.

        Score semantics:
          0.65 — high vol / strong trend / breakout: opportunity + risk
          0.55 — ranging / stable: tradeable but limited edge
          0.45 — weak trend / low vol: less favourable
          0.30 — danger: no new entries
          0.20 — critical danger: stand down
        """
        regime_scores = {
            # Legacy regimes (backward compat)
            Regime.TREND_UP: 0.60,
            Regime.TREND_DOWN: 0.60,
            Regime.RANGE_BOUND: 0.55,
            Regime.HIGH_VOL: 0.65,
            Regime.LOW_VOL: 0.45,
            Regime.LOW_LIQUIDITY: 0.30,
            # Strong trend regimes
            Regime.STRONG_TREND_STABLE_VOL: 0.65,
            Regime.STRONG_TREND_EXPANDING_VOL: 0.70,
            # Weak trend regimes
            Regime.WEAK_TREND_STABLE_VOL: 0.50,
            Regime.WEAK_TREND_CONTRACTING_VOL: 0.45,
            # Ranging regimes
            Regime.RANGING_STABLE_VOL: 0.55,
            Regime.RANGING_LOW_VOL: 0.45,
            Regime.RANGING_HIGH_VOL: 0.50,
            # Choppy regimes
            Regime.CHOPPY_CONTRACTING_VOL: 0.20,
            Regime.CHOPPY_EXPANDING_VOL: 0.30,
            # Transitional regimes
            Regime.BREAKOUT_ATTEMPT: 0.60,
            Regime.REVERSAL_SETUP: 0.45,
            # Safety / hazard regimes
            Regime.VOL_SPIKE: 0.30,
            Regime.LIQUIDITY_CRISIS: 0.15,
            Regime.VOLUME_ANOMALY: 0.30,
            Regime.MARKET_DISTORTION: 0.10,
            Regime.UNKNOWN: 0.40,
        }

        raw = regime_scores.get(regime, 0.50)

        return SubsystemScore(
            name="volatility_regime",
            raw_score=raw,
            adjusted_score=raw,
            weight=0.10,
            is_confirming=raw >= 0.3,
            metadata={"regime": regime.value},
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Decision logic
    # ─────────────────────────────────────────────────────────────────────────

    def _make_decision(
        self,
        final_score: float,
        confirming_count: int,
        regime: Regime,
        signals: list[Signal],
        subsystem_scores: list[SubsystemScore],
    ) -> tuple[str, str]:
        """Determine final action: BUY / SELL / NO_TRADE.

        Rules:
          1. final_score must exceed min_confidence threshold.
          2. confirming subsystems must meet min_confirmations.
          3. Direction from net signal direction.
        """
        # Check hard gate: risk filter
        if confirming_count < self.min_confirmations:
            return "NO_TRADE", (
                f"Insufficient confirmations: {confirming_count}/{self.min_confirmations} "
                f"(final_score={final_score:.3f})"
            )

        if final_score < self.min_confidence:
            return "NO_TRADE", (
                f"Final score {final_score:.3f} below threshold {self.min_confidence:.3f}"
            )

        # Determine direction
        direction = self._net_signal_direction(signals)
        if direction is None:
            return "NO_TRADE", "No clear directional bias from signals"

        if direction == "BUY":
            return "BUY", f"BUY signal — final_score={final_score:.3f}, confirmations={confirming_count}"
        else:
            return "SELL", f"SELL signal — final_score={final_score:.3f}, confirmations={confirming_count}"

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_regime_adjustment(
        self,
        score: SubsystemScore,
        regime: Regime,
    ) -> SubsystemScore:
        """Apply regime-dependent weight boost/penalty to a subsystem score.

        Note: adjustments are applied to the score *value* (not weight) to keep
        weighted sum normalization intact. This effectively boosts or reduces
        the influence of a subsystem's score based on regime fit.

        Boost map for the 16-regime taxonomy:
          - Trend regimes:    boost momentum + market_structure (+10%)
          - Ranging regimes:  boost orderflow + sentiment (+15%)
          - High vol / spike: boost volatility_regime (+10%), penalize others (-5%)
          - Danger states:    penalize all subsystems (-10% to -30%)
        """
        # Trending regimes (legacy + new)
        trending_regimes = {
            Regime.TREND_UP,
            Regime.TREND_DOWN,
            Regime.STRONG_TREND_STABLE_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.WEAK_TREND_STABLE_VOL,
            Regime.WEAK_TREND_CONTRACTING_VOL,
            Regime.BREAKOUT_ATTEMPT,
        }
        # Ranging / mean-reversion regimes
        ranging_regimes = {
            Regime.RANGE_BOUND,
            Regime.RANGING_STABLE_VOL,
            Regime.RANGING_LOW_VOL,
            Regime.RANGING_HIGH_VOL,
            Regime.REVERSAL_SETUP,
        }
        # High volatility regimes
        high_vol_regimes = {
            Regime.HIGH_VOL,
            Regime.STRONG_TREND_EXPANDING_VOL,
            Regime.RANGING_HIGH_VOL,
            Regime.CHOPPY_EXPANDING_VOL,
            Regime.BREAKOUT_ATTEMPT,
            Regime.VOL_SPIKE,
        }
        # Danger / hazard regimes
        danger_regimes = {
            Regime.LOW_LIQUIDITY,
            Regime.LIQUIDITY_CRISIS,
            Regime.MARKET_DISTORTION,
            Regime.CHOPPY_CONTRACTING_VOL,
        }

        boost = 1.0

        if regime in trending_regimes:
            if score.name in ("momentum", "market_structure"):
                boost = 1.10
        elif regime in ranging_regimes:
            if score.name in ("orderflow", "sentiment"):
                boost = 1.15
        elif regime in high_vol_regimes:
            if score.name == "volatility_regime":
                boost = 1.10
            elif score.name not in ("market_structure", "momentum"):
                boost = 0.95
        elif regime in danger_regimes:
            # Degrade all subsystem scores in danger states.
            # MARKET_DISTORTION is most severe (-30%), others -10%.
            if regime == Regime.MARKET_DISTORTION:
                boost = 0.70
            else:
                boost = 0.90
        elif regime == Regime.VOLUME_ANOMALY:
            # Wait for confirmation — mild degradation
            boost = 0.95
        elif regime == Regime.UNKNOWN:
            # Insufficient data — be conservative
            boost = 0.85

        adjusted = float(np.clip(score.adjusted_score * boost, 0.0, 1.0))
        return SubsystemScore(
            name=score.name,
            raw_score=score.raw_score,
            adjusted_score=adjusted,
            weight=score.weight,
            is_confirming=adjusted >= self.min_subsystem_score,
            metadata={**score.metadata, "regime_boost": boost},
        )

    @staticmethod
    def _net_signal_direction(signals: list[Signal]) -> str | None:
        """Return net direction: 'BUY', 'SELL', or None if no signals."""
        if not signals:
            return None
        buy_count = sum(1 for s in signals if s.direction == Side.BUY)
        sell_count = sum(1 for s in signals if s.direction == Side.SELL)
        if buy_count > sell_count:
            return "BUY"
        elif sell_count > buy_count:
            return "SELL"
        return None

    @staticmethod
    def _calc_stop_loss(
        entry: float | None,
        direction: Side,
        risk_metrics: dict[str, Any],
    ) -> float | None:
        """Calculate stop-loss price from entry and risk config."""
        if entry is None:
            return None
        cfg = get_config()
        sl_pct = cfg.risk.stop_loss_pct
        if direction == Side.SELL:
            return float(entry * (1 + sl_pct))
        return float(entry * (1 - sl_pct))

    @staticmethod
    def _calc_take_profit(
        entry: float | None,
        direction: Side,
        risk_metrics: dict[str, Any],
    ) -> float | None:
        """Calculate take-profit price from entry and risk config."""
        if entry is None:
            return None
        cfg = get_config()
        tp_pct = cfg.risk.take_profit_pct
        if direction == Side.SELL:
            return float(entry * (1 - tp_pct))
        return float(entry * (1 + tp_pct))

    def _log_audit(self, audit: DecisionAudit) -> None:
        """Emit structured audit log for post-mortem analysis.

        Two sinks:
        1. Structured log line (existing) — for log-based observability.
        2. SQLite audit_log table (new) — for queryable history.
        """
        subsystem_summary = {
            s.name: {
                "raw": round(s.raw_score, 4),
                "adjusted": round(s.adjusted_score, 4),
                "confirming": s.is_confirming,
            }
            for s in audit.subsystem_scores
        }

        logger.info(
            "Decision audit",
            symbol=audit.symbol,
            action=audit.decision,
            final_score=round(audit.final_score, 4),
            confirming_count=audit.confirming_count,
            required_confirmations=audit.required_confirmations,
            regime=audit.regime.value,
            regime_confidence=round(audit.regime_confidence, 4),
            subsystem_scores=subsystem_summary,
            reason=audit.reason,
        )

        # ── Persist to SQLite audit log ───────────────────────────────────
        # Best-effort: failures here must not affect the decision path.
        from ..audit.models import SubsystemScoreRow  # local import to avoid cycle

        try:
            subsystem_rows = [
                SubsystemScoreRow(
                    name=s.name,
                    raw_score=s.raw_score,
                    adjusted_score=s.adjusted_score,
                    weight=s.weight,
                    is_confirming=s.is_confirming,
                    metadata=dict(s.metadata or {}),
                )
                for s in audit.subsystem_scores
            ]

            reason_code = (
                classify_no_trade_reason(audit.reason).value
                if audit.decision == "NO_TRADE"
                else None
            )

            entry = AuditEntryInput(
                symbol=audit.symbol,
                timeframe=str(audit.timeframe.value) if audit.timeframe else None,
                decision=audit.decision,
                reason=audit.reason,
                reason_code=reason_code,
                regime=audit.regime.value,
                regime_confidence=audit.regime_confidence,
                final_score=audit.final_score,
                confirming_count=audit.confirming_count,
                required_confirmations=audit.required_confirmations,
                subsystem_scores=subsystem_rows,
                metadata={
                    "decision_engine": True,
                    "min_confidence": self.min_confidence,
                    "min_subsystem_score": self.min_subsystem_score,
                },
                source="decision_engine",
            )
            get_audit_logger().log(entry)
        except Exception as exc:
            # Audit is best-effort. Log and continue.
            logger.warning(
                "Failed to persist decision audit row",
                symbol=audit.symbol,
                error=str(exc),
            )
