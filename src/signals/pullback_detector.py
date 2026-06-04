"""Pullback Detection Module.

Generates pullback entry signals:
- In uptrend (higher highs/lows): detect price pulling back to EMA/SMA/zones → BUY signal
- In downtrend (lower highs/lows): detect bounces to EMA/SMA/zones → SELL signal
- Pullback valid only if price has pulled back AT LEAST 50% toward the opposite extreme
- Requires trend confirmation from higher timeframe (1h or 4h must be trending)
- Uses RSI + Bollinger Band position to confirm pullback depth
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..data.models import NormalizedCandle, TimeFrame, Side, Signal
from .structure_scanner import StructureScanResult, StructureScanner
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PullbackSignal:
    """A detected pullback opportunity."""

    symbol: str
    direction: Side                          # BUY = buy dip, SELL = sell bounce
    pullback_pct: float                       # how far price pulled back (0-1)
    rsi_value: float                          # RSI at pullback detection
    bb_position: float                        # Bollinger band position (0-1)
    ema_distance_pct: float                   # price distance from EMA in %
    structure_confirmed: bool                 # did structure confirm this direction?
    higher_tf_confirmed: bool                # did 1h or 4h confirm trend?
    entry_zone: float                         # suggested entry price zone
    stop_loss: float                          # suggested stop loss
    confidence: float = 0.0                   # overall confidence 0-1
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PullbackDetector:
    """Detects pullback entries in trending markets.

    Usage:
        detector = PullbackDetector()
        signal = detector.detect_pullback("BTC", candles, structure_result)
        if signal:
            print(f"Pullback BUY detected at {signal.entry_zone}")
    """

    # Minimum pullback depth as a fraction (50% = 0.50)
    MIN_PULLBACK_PCT: float = 0.50

    # RSI thresholds for pullback confirmation
    RSI_OVERSOLD: float = 35.0   # for BUY pullbacks
    RSI_OVERBOUGHT: float = 65.0  # for SELL pullbacks

    # Bollinger Band position thresholds (0 = lower band, 1 = upper band)
    BB_LOWER_THRESHOLD: float = 0.20  # price near lower band = oversold
    BB_UPPER_THRESHOLD: float = 0.80  # price near upper band = overbought

    def __init__(self, ema_fast: int = 20, ema_slow: int = 50) -> None:
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow

    def detect_pullback(
        self,
        symbol: str,
        candles: list[NormalizedCandle],
        structure: StructureScanResult,
    ) -> PullbackSignal | None:
        """Detect if price is in a valid pullback within the given structure.

        Args:
            symbol: Trading pair
            candles: Recent candles (must be >= 50 for valid EMA/RSI)
            structure: Pre-computed structure scan result

        Returns:
            PullbackSignal if valid pullback detected, None otherwise.
        """
        if len(candles) < 50:
            return None

        closes = np.array([c.close for c in candles])

        # Determine trend direction from structure
        if structure.aggregate_score > 0.3:
            direction = Side.BUY   # trending up → look for buy pullbacks
            trend = "uptrend"
        elif structure.aggregate_score < -0.3:
            direction = Side.SELL  # trending down → look for sell pullbacks
            trend = "downtrend"
        else:
            # No clear trend → no pullback trades
            return None

        # Higher timeframe confirmation check
        higher_tf_ok = False
        if direction == Side.BUY:
            higher_tf_ok = structure.higher_timeframe_bullish
        else:
            higher_tf_ok = structure.higher_timeframe_bearish

        if not higher_tf_ok:
            # Need 1h or 4h to be confirming the trend
            logger.debug("Pullback requires higher TF confirmation", symbol=symbol)
            return None

        # Calculate indicators
        ema_20 = self._calc_ema(closes, self._ema_fast)
        ema_50 = self._calc_ema(closes, self._ema_slow)
        rsi_val = self._calc_rsi(closes, period=14)
        bb_position = self._calc_bb_position(closes)

        # Determine pullback depth
        pullback_pct = self._calc_pullback_depth(candles, direction, trend)

        if pullback_pct < self.MIN_PULLBACK_PCT:
            logger.debug(
                "Pullback not deep enough",
                symbol=symbol,
                pullback_pct=round(pullback_pct, 3),
                min=self.MIN_PULLBACK_PCT,
            )
            return None

        # Validate pullback with RSI and BB
        rsi_ok = self._validate_rsi(rsi_val, direction)
        bb_ok = self._validate_bb_position(bb_position, direction)

        if not (rsi_ok or bb_ok):
            # At least one of RSI or BB should confirm
            logger.debug("Pullback RSI/BB not confirmed", symbol=symbol, rsi=rsi_val, bb=bb_position)
            return None

        # Calculate entry zone (EMA cluster area)
        entry_zone = (ema_20 + ema_50) / 2 if ema_50 > 0 else closes[-1]

        # Calculate stop loss (below swing low for buys, above swing high for sells)
        stop_loss = self._calc_stop_loss(candles, direction)

        # Calculate confidence
        confidence = self._calc_confidence(
            pullback_pct=pullback_pct,
            rsi_value=rsi_val,
            bb_position=bb_position,
            structure_score=structure.aggregate_score,
            trend_strength=structure.aggregate_trend_strength,
        )

        return PullbackSignal(
            symbol=symbol,
            direction=direction,
            pullback_pct=pullback_pct,
            rsi_value=rsi_val,
            bb_position=bb_position,
            ema_distance_pct=abs((closes[-1] - entry_zone) / entry_zone * 100) if entry_zone > 0 else 0.0,
            structure_confirmed=True,
            higher_tf_confirmed=True,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            confidence=confidence,
            metadata={
                "trend": trend,
                "ema_20": float(ema_20),
                "ema_50": float(ema_50),
                "structure_score": structure.aggregate_score,
                "aggregate_trend_strength": structure.aggregate_trend_strength,
                "dominant_structure": structure.dominant_structure,
            },
        )

    def _calc_ema(self, closes: np.ndarray, period: int) -> float:
        """Calculate EMA for a period."""
        if len(closes) < period:
            return float(closes[-1]) if len(closes) > 0 else 0.0

        mult = 2.0 / (period + 1)
        ema = float(closes[0])
        for price in closes[1:]:
            ema = (float(price) - ema) * mult + ema
        return ema

    def _calc_rsi(self, closes: np.ndarray, period: int = 14) -> float:
        """Calculate RSI."""
        if len(closes) < period + 2:
            return 50.0  # neutral

        deltas = np.diff(closes[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = float(gains.mean())
        avg_loss = float(losses.mean())

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1 + rs))

    def _calc_bb_position(self, closes: np.ndarray, period: int = 20, std_dev: float = 2.0) -> float:
        """Calculate Bollinger Band position (0-1 scale).

        0 = price at lower band, 0.5 = at middle SMA, 1 = at upper band.
        """
        if len(closes) < period:
            return 0.5

        recent = closes[-period:]
        sma = float(recent.mean())
        std = float(recent.std())

        if std == 0:
            return 0.5

        lower = sma - std_dev * std
        upper = sma + std_dev * std

        # Position formula: (price - lower) / (upper - lower)
        current = closes[-1]
        position = (current - lower) / (upper - lower)
        return float(np.clip(position, 0.0, 1.0))

    def _calc_pullback_depth(
        self,
        candles: list[NormalizedCandle],
        direction: Side,
        trend: str,
    ) -> float:
        """Calculate how deep the pullback is (0 to 1).

        Pullback depth = how much price has retraced toward the opposite extreme.
        In an uptrend, we measure from the last high toward the last low.
        In a downtrend, we measure from the last low toward the last high.

        A pullback of 50% (0.50) means price has given back half the move.
        """
        if len(candles) < 20:
            return 0.0

        # Get last 20 candles to find recent high/low
        recent = candles[-20:]
        high_prices = [c.high for c in recent]
        low_prices = [c.low for c in recent]

        last_high_idx = int(np.argmax(high_prices))
        last_low_idx = int(np.argmin(low_prices))

        # Make sure high/low are not the same candle
        if last_high_idx == last_low_idx:
            return 0.0

        last_high = high_prices[last_high_idx] if last_high_idx < len(recent) else recent[-1].high
        last_low = low_prices[last_low_idx] if last_low_idx < len(recent) else recent[-1].low

        current_price = candles[-1].close

        if direction == Side.BUY:
            # Measure pullback from high to low (how far has price fallen from high?)
            total_move = last_high - last_low
            if total_move == 0:
                return 0.0
            pullback_distance = last_high - current_price
            pullback_pct = pullback_distance / total_move
        else:
            # Measure pullback from low to high (how far has price risen from low?)
            total_move = last_high - last_low
            if total_move == 0:
                return 0.0
            pullback_distance = current_price - last_low
            pullback_pct = pullback_distance / total_move

        return float(np.clip(pullback_pct, 0.0, 1.0))

    def _validate_rsi(self, rsi: float, direction: Side) -> bool:
        """Check if RSI confirms the pullback direction.

        BUY pullback: RSI should be near oversold (< 35) or rising from lows
        SELL pullback: RSI should be near overbought (> 65) or falling from highs
        """
        if direction == Side.BUY:
            return rsi < self.RSI_OVERSOLD or (30 < rsi < 45)
        else:
            return rsi > self.RSI_OVERBOUGHT or (55 < rsi < 70)

    def _validate_bb_position(self, bb_pos: float, direction: Side) -> bool:
        """Check if Bollinger Band position confirms pullback.

        BUY pullback: bb_position should be < 0.3 (near lower band)
        SELL pullback: bb_position should be > 0.7 (near upper band)
        """
        if direction == Side.BUY:
            return bb_pos < self.BB_LOWER_THRESHOLD or (0.3 < bb_pos < 0.5)
        else:
            return bb_pos > self.BB_UPPER_THRESHOLD or (0.5 < bb_pos < 0.7)

    def _calc_stop_loss(
        self,
        candles: list[NormalizedCandle],
        direction: Side,
    ) -> float:
        """Calculate stop loss based on recent swing low/high."""
        if len(candles) < 20:
            return candles[-1].close * 0.98 if direction == Side.BUY else candles[-1].close * 1.02

        recent = candles[-20:]
        lows = [c.low for c in recent]
        highs = [c.high for c in recent]

        if direction == Side.BUY:
            # Stop below recent swing low
            swing_low = min(lows)
            return float(swing_low * 0.998)  # Just below swing low
        else:
            # Stop above recent swing high
            swing_high = max(highs)
            return float(swing_high * 1.002)  # Just above swing high

    def _calc_confidence(
        self,
        pullback_pct: float,
        rsi_value: float,
        bb_position: float,
        structure_score: float,
        trend_strength: float,
    ) -> float:
        """Calculate overall pullback confidence 0-1.

        Components:
        - Pullback depth: deeper pullbacks are more reliable (weight: 0.25)
        - RSI confirmation: extreme RSI is stronger signal (weight: 0.20)
        - BB confirmation: near band is stronger (weight: 0.15)
        - Structure score: stronger trend = more reliable pullback (weight: 0.25)
        - Trend strength: stronger trend = more reliable pullback (weight: 0.15)
        """
        # Pullback depth score (50% = base, higher = better)
        depth_score = min(1.0, pullback_pct / 0.5) if pullback_pct >= 0.5 else pullback_pct

        # RSI score (centered at 50, more extreme = higher)
        if rsi_value < 50:
            rsi_score = (50 - rsi_value) / 50
        else:
            rsi_score = (rsi_value - 50) / 50

        # BB position score (0.5 is neutral, extremes are stronger)
        bb_score = abs(bb_position - 0.5) * 2

        # Structure score
        structure_conf = abs(structure_score)

        # Trend strength score
        strength_score = trend_strength

        confidence = (
            depth_score * 0.25 +
            rsi_score * 0.20 +
            bb_score * 0.15 +
            structure_conf * 0.25 +
            strength_score * 0.15
        )

        return float(np.clip(confidence, 0.0, 1.0))

    def to_signal(self, pb: PullbackSignal) -> Signal:
        """Convert a PullbackSignal into a Signal for the registry."""
        return Signal(
            name="pullback",
            symbol=pb.symbol,
            timeframe=TimeFrame.M15,  # Default to 15m for pullback detection
            direction=pb.direction,
            confidence=pb.confidence,
            metadata={
                "pullback_pct": pb.pullback_pct,
                "rsi_value": pb.rsi_value,
                "bb_position": pb.bb_position,
                "ema_distance_pct": pb.ema_distance_pct,
                "structure_confirmed": pb.structure_confirmed,
                "higher_tf_confirmed": pb.higher_tf_confirmed,
                "entry_zone": pb.entry_zone,
                "stop_loss": pb.stop_loss,
                **pb.metadata,
            },
        )