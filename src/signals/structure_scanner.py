"""Multi-Timeframe Market Structure Scanner.

Detects market structure across 1m, 5m, 15m, 1h, 4h timeframes:
- Higher highs/lows (uptrend), lower highs/lows (downtrend), ranging (sideways)
- Support/resistance zones using swing highs/lows
- Structure breaks (break of swing high/low = momentum shift signal)
- Assigns a structure_score: +1 to -1 per timeframe

This module feeds the decision engine's market_structure subsystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..data.models import NormalizedCandle, TimeFrame, Side
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class SwingPoint:
    """A swing high or swing low point in price action."""

    index: int            # index in the candle list
    price: float
    timestamp: datetime
    swing_type: str       # "high" or "low"
    strength: float = 1.0  # 0-1, based on how far it protrudes


@dataclass
class StructureZone:
    """A support or resistance zone."""

    level: float
    zone_type: str        # "support" or "resistance"
    swing_points: list[SwingPoint] = field(default_factory=list)
    touches: int = 1      # how many times price respected this zone
    strength: float = 0.5  # 0-1


@dataclass
class TimeframeStructure:
    """Market structure analysis for a single timeframe."""

    timeframe: TimeFrame
    structure_type: str          # "uptrend", "downtrend", "ranging"
    structure_score: float       # -1 to +1 (bearish to bullish)
    trend_strength: float        # 0 to 1
    support_zones: list[StructureZone] = field(default_factory=list)
    resistance_zones: list[StructureZone] = field(default_factory=list)
    last_swing_high: SwingPoint | None = None
    last_swing_low: SwingPoint | None = None
    structure_broken: bool = False  # did price break below last swing low or above swing high?
    break_direction: str | None = None  # "bullish" or "bearish" break
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class StructureScanResult:
    """Aggregated structure analysis across all timeframes."""

    symbol: str
    timeframe_analysis: dict[str, TimeframeStructure] = field(default_factory=dict)
    # Aggregate scores
    aggregate_score: float = 0.0   # -1 to +1
    aggregate_trend_strength: float = 0.0
    dominant_structure: str = "unknown"
    # Higher timeframe bias (for trend confirmation)
    higher_timeframe_bullish: bool = False
    higher_timeframe_bearish: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def higher_tf_confirms(self, direction: str) -> bool:
        """Check if higher timeframes confirm a given direction."""
        if direction == "bullish":
            return self.higher_timeframe_bullish
        elif direction == "bearish":
            return self.higher_timeframe_bearish
        return False


class StructureScanner:
    """Multi-timeframe market structure scanner.

    Usage:
        scanner = StructureScanner()
        result = await scanner.scan("BTC", candles_by_tf)
        print(result.aggregate_score)
    """

    # Timeframes to scan (from low to high)
    TIMEFRAMES: list[TimeFrame] = [
        TimeFrame.M1, TimeFrame.M5, TimeFrame.M15, TimeFrame.H1, TimeFrame.H4
    ]

    # Higher timeframes for trend confirmation (1h and 4h must be trending)
    HIGHER_TFS: list[TimeFrame] = [TimeFrame.H1, TimeFrame.H4]

    def __init__(self, min_swing_candles: int = 5) -> None:
        self._min_swing = min_swing_candles

    async def scan(
        self,
        symbol: str,
        candles_by_tf: dict[str, list[NormalizedCandle]],
    ) -> StructureScanResult:
        """Scan structure across all available timeframes.

        Args:
            symbol: Trading pair, e.g. "BTC"
            candles_by_tf: Dict of timeframe -> list of candles (oldest first)

        Returns:
            StructureScanResult with per-TF analysis and aggregate scores.
        """
        result = StructureScanResult(symbol=symbol)
        scores: list[float] = []
        strengths: list[float] = []

        for tf_str, candles in candles_by_tf.items():
            try:
                tf = TimeFrame(tf_str) if isinstance(tf_str, str) else tf_str
            except ValueError:
                logger.debug("Unknown timeframe, skipping", tf=tf_str)
                continue

            if len(candles) < 20:
                continue

            analysis = self._analyze_timeframe(symbol, tf, candles)
            result.timeframe_analysis[tf_str] = analysis

            scores.append(analysis.structure_score)
            strengths.append(analysis.trend_strength)

        if scores:
            result.aggregate_score = float(np.mean(scores))
            result.aggregate_trend_strength = float(np.mean(strengths))

            # Determine dominant structure
            if result.aggregate_score > 0.3:
                result.dominant_structure = "uptrend"
            elif result.aggregate_score < -0.3:
                result.dominant_structure = "downtrend"
            else:
                result.dominant_structure = "ranging"

        # Higher timeframe bias
        result.higher_timeframe_bullish = all(
            result.timeframe_analysis.get(tf.value, TimeframeStructure(tf, "", 0.0, 0.0)).structure_score > 0.2
            for tf in self.HIGHER_TFS
            if tf.value in result.timeframe_analysis
        )
        result.higher_timeframe_bearish = all(
            result.timeframe_analysis.get(tf.value, TimeframeStructure(tf, "", 0.0, 0.0)).structure_score < -0.2
            for tf in self.HIGHER_TFS
            if tf.value in result.timeframe_analysis
        )

        logger.debug(
            "Structure scan complete",
            symbol=symbol,
            aggregate_score=round(result.aggregate_score, 3),
            dominant=result.dominant_structure,
            timeframes=len(result.timeframe_analysis),
        )
        return result

    def _analyze_timeframe(
        self,
        symbol: str,
        timeframe: TimeFrame,
        candles: list[NormalizedCandle],
    ) -> TimeframeStructure:
        """Analyze market structure for a single timeframe."""
        if len(candles) < 20:
            return TimeframeStructure(timeframe, "unknown", 0.0, 0.0)

        closes = np.array([c.close for c in candles])
        highs = np.array([c.high for c in candles])
        lows = np.array([c.low for c in candles])

        # Find swing highs and lows
        swing_highs = self._find_swing_points(candles, "high")
        swing_lows = self._find_swing_points(candles, "low")

        last_high = swing_highs[-1] if swing_highs else None
        last_low = swing_lows[-1] if swing_lows else None

        # Determine structure
        structure_type, structure_score, trend_strength = self._classify_structure(
            candles, swing_highs, swing_lows
        )

        # Find support/resistance zones
        support_zones = self._find_support_zones(swing_lows, candles)
        resistance_zones = self._find_resistance_zones(swing_highs, candles)

        # Detect structure break
        structure_broken, break_direction = self._detect_structure_break(
            candles, swing_highs, swing_lows
        )

        return TimeframeStructure(
            timeframe=timeframe,
            structure_type=structure_type,
            structure_score=structure_score,
            trend_strength=trend_strength,
            support_zones=support_zones,
            resistance_zones=resistance_zones,
            last_swing_high=last_high,
            last_swing_low=last_low,
            structure_broken=structure_broken,
            break_direction=break_direction,
        )

    def _find_swing_points(
        self,
        candles: list[NormalizedCandle],
        swing_type: str,
    ) -> list[SwingPoint]:
        """Find swing high or low points.

        A swing high is a point where both neighbours have lower highs.
        A swing low is a point where both neighbours have higher lows.
        """
        if len(candles) < 3:
            return []

        n = len(candles)
        points: list[SwingPoint] = []

        for i in range(self._min_swing, n - self._min_swing):
            c = candles[i]
            prev_candles = candles[i - self._min_swing:i]
            next_candles = candles[i + 1:i + self._min_swing + 1]

            if swing_type == "high":
                # Swing high: all neighbours have lower highs
                if all(nc.high < c.high for nc in prev_candles) and \
                   all(nc.high < c.high for nc in next_candles):
                    # Strength based on how much it protrudes
                    strength = self._calculate_swing_strength(c, candles, i, swing_type)
                    points.append(SwingPoint(
                        index=i,
                        price=c.high,
                        timestamp=c.timestamp,
                        swing_type="high",
                        strength=strength,
                    ))
            else:
                # Swing low: all neighbours have higher lows
                if all(nc.low > c.low for nc in prev_candles) and \
                   all(nc.low > c.low for nc in next_candles):
                    strength = self._calculate_swing_strength(c, candles, i, swing_type)
                    points.append(SwingPoint(
                        index=i,
                        price=c.low,
                        timestamp=c.timestamp,
                        swing_type="low",
                        strength=strength,
                    ))

        return points

    def _calculate_swing_strength(
        self,
        candle: NormalizedCandle,
        all_candles: list[NormalizedCandle],
        idx: int,
        swing_type: str,
    ) -> float:
        """Calculate how significant a swing point is (0-1)."""
        lookback = min(10, idx)
        if lookback == 0:
            return 0.5

        if swing_type == "high":
            nearby_highs = [all_candles[i].high for i in range(max(0, idx - lookback), idx + 1)]
            max_high = max(nearby_highs) if nearby_highs else candle.high
            if max_high == 0:
                return 0.5
            strength = (candle.high - max_high) / max_high
        else:
            nearby_lows = [all_candles[i].low for i in range(max(0, idx - lookback), idx + 1)]
            min_low = min(nearby_lows) if nearby_lows else candle.low
            if min_low == 0:
                return 0.5
            strength = (min_low - candle.low) / min_low

        # Normalize to 0-1 range (assuming max swing is 5%)
        normalized = min(1.0, abs(strength) * 20)
        return float(normalized)

    def _classify_structure(
        self,
        candles: list[NormalizedCandle],
        swing_highs: list[SwingPoint],
        swing_lows: list[SwingPoint],
    ) -> tuple[str, float, float]:
        """Classify the market structure and compute scores.

        Returns: (structure_type, structure_score, trend_strength)
        """
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "ranging", 0.0, 0.0

        # Get last 5 swing points to determine structure
        highs = [(sh.index, sh.price) for sh in swing_highs[-5:]]
        lows = [(sl.index, sl.price) for sl in swing_lows[-5:]]

        # Higher highs = uptrend
        higher_highs = all(h1[1] < h2[1] for h1, h2 in zip(highs[:-1], highs[1:]))
        # Higher lows = uptrend
        higher_lows = all(l1[1] < l2[1] for l1, l2 in zip(lows[:-1], lows[1:]))

        # Lower highs = downtrend
        lower_highs = all(h1[1] > h2[1] for h1, h2 in zip(highs[:-1], highs[1:]))
        # Lower lows = downtrend
        lower_lows = all(l1[1] > l2[1] for l1, l2 in zip(lows[:-1], lows[1:]))

        if higher_highs and higher_lows:
            return "uptrend", 0.7, 0.8
        elif lower_highs and lower_lows:
            return "downtrend", -0.7, 0.8
        elif higher_highs or higher_lows:
            # Weaker uptrend
            return "uptrend", 0.4, 0.5
        elif lower_highs or lower_lows:
            # Weaker downtrend
            return "downtrend", -0.4, 0.5
        else:
            return "ranging", 0.0, 0.3

    def _find_support_zones(
        self,
        swing_lows: list[SwingPoint],
        candles: list[NormalizedCandle],
    ) -> list[StructureZone]:
        """Identify horizontal support zones from swing lows."""
        if not swing_lows:
            return []

        zones: list[StructureZone] = []
        # Cluster nearby swing lows into zones (within 1% of each other)
        cluster_tolerance = 0.01

        clustered: list[list[SwingPoint]] = []
        for sl in swing_lows:
            found_cluster = False
            for cluster in clustered:
                if abs(cluster[0].price - sl.price) / cluster[0].price < cluster_tolerance:
                    cluster.append(sl)
                    found_cluster = True
                    break
            if not found_cluster:
                clustered.append([sl])

        for cluster in clustered:
            avg_price = sum(sp.price for sp in cluster) / len(cluster)
            zone = StructureZone(
                level=avg_price,
                zone_type="support",
                swing_points=cluster,
                touches=len(cluster),
                strength=min(1.0, len(cluster) * 0.25),
            )
            zones.append(zone)

        return zones

    def _find_resistance_zones(
        self,
        swing_highs: list[SwingPoint],
        candles: list[NormalizedCandle],
    ) -> list[StructureZone]:
        """Identify horizontal resistance zones from swing highs."""
        if not swing_highs:
            return []

        zones: list[StructureZone] = []
        cluster_tolerance = 0.01

        clustered: list[list[SwingPoint]] = []
        for sh in swing_highs:
            found_cluster = False
            for cluster in clustered:
                if abs(cluster[0].price - sh.price) / cluster[0].price < cluster_tolerance:
                    cluster.append(sh)
                    found_cluster = True
                    break
            if not found_cluster:
                clustered.append([sh])

        for cluster in clustered:
            avg_price = sum(sp.price for sp in cluster) / len(cluster)
            zone = StructureZone(
                level=avg_price,
                zone_type="resistance",
                swing_points=cluster,
                touches=len(cluster),
                strength=min(1.0, len(cluster) * 0.25),
            )
            zones.append(zone)

        return zones

    def _detect_structure_break(
        self,
        candles: list[NormalizedCandle],
        swing_highs: list[SwingPoint],
        swing_lows: list[SwingPoint],
    ) -> tuple[bool, str | None]:
        """Detect if price has broken structure (swing high/low broken).

        A bullish break = price closes above last swing high.
        A bearish break = price closes below last swing low.
        """
        if len(candles) < 2 or len(swing_lows) < 1 or len(swing_highs) < 1:
            return False, None

        last_candle = candles[-1]
        prev_candle = candles[-2]

        last_swing_low = swing_lows[-1]
        last_swing_high = swing_highs[-1]

        # Bearish break: prev was above swing low, now closing below
        if prev_candle.close > last_swing_low.price and last_candle.close < last_swing_low.price:
            return True, "bearish"

        # Bullish break: prev was below swing high, now closing above
        if prev_candle.close < last_swing_high.price and last_candle.close > last_swing_high.price:
            return True, "bullish"

        return False, None

    def structure_signal(self, result: StructureScanResult) -> dict[str, Any] | None:
        """Convert StructureScanResult into a signal dict for the decision engine.

        Returns None if structure is not clear enough.
        """
        if result.aggregate_score > 0.4 and result.higher_timeframe_bullish:
            return {
                "name": "structure",
                "direction": Side.BUY,
                "confidence": min(1.0, abs(result.aggregate_score)),
                "metadata": {
                    "structure_type": result.dominant_structure,
                    "aggregate_score": result.aggregate_score,
                    "trend_strength": result.aggregate_trend_strength,
                    "structure_broken": any(
                        tf.structure_broken for tf in result.timeframe_analysis.values()
                    ),
                },
            }
        elif result.aggregate_score < -0.4 and result.higher_timeframe_bearish:
            return {
                "name": "structure",
                "direction": Side.SELL,
                "confidence": min(1.0, abs(result.aggregate_score)),
                "metadata": {
                    "structure_type": result.dominant_structure,
                    "aggregate_score": result.aggregate_score,
                    "trend_strength": result.aggregate_trend_strength,
                    "structure_broken": any(
                        tf.structure_broken for tf in result.timeframe_analysis.values()
                    ),
                },
            }
        return None