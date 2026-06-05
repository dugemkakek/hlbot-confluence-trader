"""Dynamic Pair Ranker — scores and ranks all discovered pairs by confluence.

This module:
- Runs across ALL discovered pairs
- Each pair gets a confluence_score (0-1) per cycle
- Ranks all pairs by confluence_score descending
- Selects top N pairs to evaluate for entry (N = 5 by default, configurable)
- Only the top-ranked pairs with score > threshold get full decision evaluation

The confluence score is a weighted combination of:
- Structure score (weight: 0.25): higher highs/lows quality
- Pullback validity (weight: 0.30): valid pullback signal
- Momentum confirmation (weight: 0.25): RSI, MACD confirming direction
- Volume confirmation (weight: 0.20): volume supporting the move
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..data.models import NormalizedCandle, TimeFrame, Side, Signal
from ..utils.config import get_config
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RankedPair:
    """A pair with its computed confluence scores."""

    symbol: str
    # Component scores (0-1)
    structure_score: float = 0.0
    pullback_score: float = 0.0
    momentum_score: float = 0.0
    volume_score: float = 0.0
    # Weighted composite
    confluence_score: float = 0.0
    # Individual signal details
    direction: str | None = None  # "buy" or "sell"
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_actionable(self) -> bool:
        # Trade if: confluence > 0 AND direction is set (momentum confirmed)
        return self.confluence_score > 0 and self.direction is not None


@dataclass
class PairRankingResult:
    """Results of a full ranking scan across all pairs."""

    ranked_pairs: list[RankedPair] = field(default_factory=list)
    top_pairs: list[RankedPair] = field(default_factory=list)
    top_pair: RankedPair | None = None  # single best pair for execution
    total_discovered: int = 0
    min_confluence_threshold: float = 0.0  # is_actionable now uses confluence > 0
    max_pairs: int = 5
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Filter reasoning for debug logging
    filter_reasons: dict[str, str] = field(default_factory=dict)


class PairRanker:
    """Ranks all discovered pairs by multi-factor confluence score.

    Usage:
        ranker = PairRanker()
        result = await ranker.rank_pairs(pairs, candles_by_symbol)
        top = result.top_pairs  # top 5 actionable pairs
    """

    # Weights for each factor
    WEIGHT_STRUCTURE: float = 0.25
    WEIGHT_PULLBACK: float = 0.30
    WEIGHT_MOMENTUM: float = 0.25
    WEIGHT_VOLUME: float = 0.20

    # Direction-vote gates (added 2026-06-06 v0.2.0). The previous
    # structure/pullback gates (0.1/0.15) plus momentum-only fallback
    # at ±0.2 produced a systematically biased direction in bearish
    # regimes: structure/pullback rarely agreed, so the momentum
    # fallback was the only path that ever set direction, and
    # momentum_score was observably biased negative
    # ([-0.93, +0.33], mean -0.22 in the 30d calibration window).
    # See CHANGELOG.md "v0.2.0 — strategy direction bias".
    STRUCTURE_GATE: float = 0.10
    PULLBACK_GATE: float = 0.15
    MOMENTUM_GATE: float = 0.20

    def __init__(
        self,
        max_pairs: int = 5,
        min_confluence_score: float = 0.55,
    ) -> None:
        cfg = get_config()
        self._max_pairs = max_pairs if max_pairs else cfg.scanner.max_pairs_per_cycle
        self._min_confluence = min_confluence_score if min_confluence_score else cfg.scanner.min_confluence_score

    async def rank_pairs(
        self,
        symbols: list[str],
        candles_by_symbol: dict[str, dict[str, list[NormalizedCandle]]],
        structure_results: dict[str, Any] | None = None,
        pullback_results: dict[str, Any] | None = None,
    ) -> PairRankingResult:
        """Rank all pairs by confluence score.

        Args:
            symbols: List of symbols to rank
            candles_by_symbol: Dict of symbol -> {timeframe -> candles}
            structure_results: Optional pre-computed structure scan results
            pullback_results: Optional pre-computed pullback results

        Returns:
            PairRankingResult with ranked list and top N actionable pairs.
        """
        result = PairRankingResult(
            total_discovered=len(symbols),
            min_confluence_threshold=self._min_confluence,
            max_pairs=self._max_pairs,
            filter_reasons={},
        )

        ranked: list[RankedPair] = []
        skipped_no_candles = 0
        skipped_low_score = 0
        skipped_no_direction = 0

        for symbol in symbols:
            candle_data = candles_by_symbol.get(symbol, {})
            if not candle_data:
                result.filter_reasons[symbol] = "no_candle_data"
                skipped_no_candles += 1
                continue

            # Use 15m candles as the primary evaluation timeframe
            primary_candles = candle_data.get("15m", candle_data.get("1h", []))

            if len(primary_candles) < 30:
                result.filter_reasons[symbol] = f"insufficient_candles_{len(primary_candles)}"
                skipped_no_candles += 1
                continue

            ranked_pair = self._score_pair(symbol, primary_candles, candle_data)
            ranked.append(ranked_pair)

            # Track why a pair might not make the cut
            if ranked_pair.confluence_score < self._min_confluence:
                result.filter_reasons[symbol] = f"low_confluence_{ranked_pair.confluence_score:.3f}"
                skipped_low_score += 1
            elif ranked_pair.direction is None:
                result.filter_reasons[symbol] = f"no_direction_structure={ranked_pair.structure_score:.3f}_pullback={ranked_pair.pullback_score:.3f}"
                skipped_no_direction += 1

        # Sort by confluence_score descending
        ranked.sort(key=lambda p: p.confluence_score, reverse=True)
        result.ranked_pairs = ranked

        # Normalize volume scores across all candidates for proper relative ranking
        if ranked:
            self.normalize_volume_scores(ranked)

        # Re-sort after volume normalization (confluence may have changed)
        ranked.sort(key=lambda p: p.confluence_score, reverse=True)
        result.ranked_pairs = ranked

        # Filter to top N above threshold
        actionable = [p for p in ranked if p.is_actionable]
        result.top_pairs = actionable[: self._max_pairs]
        result.top_pair = actionable[0] if actionable else None

        logger.info(
            "Pair ranking complete",
            total=len(ranked),
            actionable=len(actionable),
            top_n=len(result.top_pairs),
            threshold=self._min_confluence,
            skipped_no_candles=skipped_no_candles,
            skipped_low_score=skipped_low_score,
            skipped_no_direction=skipped_no_direction,
        )

        # Log filter reasons for debugging
        if result.filter_reasons:
            logger.debug(
                "Filter reasons",
                reasons={
                    k: v for k, v in result.filter_reasons.items()
                    if "low_confluence" in v or "no_direction" in v
                },
            )

        return result

    def _score_pair(
        self,
        symbol: str,
        primary_candles: list[NormalizedCandle],
        all_candles: dict[str, list[NormalizedCandle]],
    ) -> RankedPair:
        """Compute all component scores for a single pair."""
        pair = RankedPair(symbol=symbol)

        # Structure score (from swing high/low analysis)
        pair.structure_score = self._calc_structure_score(primary_candles)

        # Pullback score (detect pullback within trend)
        pair.pullback_score = self._calc_pullback_score(primary_candles)

        # Momentum score (RSI + MACD)
        pair.momentum_score = self._calc_momentum_score(primary_candles)

        # Volume score (raw ratio, normalized later)
        pair.volume_score = self._calc_volume_score(primary_candles)

        # Weighted composite (structure, pullback, momentum on -1 to 1 scale; volume on 0-3)
        # Bug #2 fix (2026-06-02): the previous formula `(volume + 1) / 2`
        # assumed volume was in [-1, +1] but _calc_volume_score returns a ratio
        # clipped to [0, 3]. The result was vol_normalized in [0.5, 2.0], which
        # over-weighted high-volume pairs and let confluence exceed 1.0. Now
        # we linearly map [0, 3] -> [0, 1] by dividing by 3 and clipping.
        vol_normalized = float(np.clip(pair.volume_score / 3.0, 0.0, 1.0))
        pair.confluence_score = (
            abs(pair.structure_score) * self.WEIGHT_STRUCTURE +
            abs(pair.pullback_score) * self.WEIGHT_PULLBACK +
            abs(pair.momentum_score) * self.WEIGHT_MOMENTUM +
            vol_normalized * self.WEIGHT_VOLUME
        )

        # Determine direction from scores (v0.2.0 — 2-of-3 component vote).
        # See _direction_from_votes for the rules; extracted so the
        # vote logic is unit-testable in isolation.
        pair.direction = self._direction_from_votes(
            pair.structure_score, pair.pullback_score, pair.momentum_score
        )

        pair.confidence = pair.confluence_score

        return pair

    def _calc_structure_score(self, candles: list[NormalizedCandle]) -> float:
        """Score market structure from price action.

        Returns -1 to +1:
        - Positive = bullish structure (higher highs/lows)
        - Negative = bearish structure (lower highs/lows)
        - Near 0 = ranging
        """
        if len(candles) < 20:
            return 0.0

        # Find swing points using proper swing detection
        swing_highs = self._find_swing_points(candles, "high")
        swing_lows = self._find_swing_points(candles, "low")

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return 0.0

        # Count higher highs, lower highs, higher lows, lower lows
        # by comparing each swing to the PREVIOUS swing of the same type
        hh_count = 0
        lh_count = 0
        hl_count = 0
        ll_count = 0

        for i in range(1, len(swing_highs)):
            if swing_highs[i].price > swing_highs[i-1].price:
                hh_count += 1
            else:
                lh_count += 1

        for i in range(1, len(swing_lows)):
            if swing_lows[i].price > swing_lows[i-1].price:
                hl_count += 1
            else:
                ll_count += 1

        # Score: net swing direction
        bullish_swings = hh_count + hl_count
        bearish_swings = lh_count + ll_count
        total = bullish_swings + bearish_swings

        if total == 0:
            return 0.0

        # Structure score: fraction of bullish swings, scaled to -1 to +1
        score = (bullish_swings - bearish_swings) / total
        return float(np.clip(score, -1.0, 1.0))

    def _find_swing_points(
        self,
        candles: list[NormalizedCandle],
        swing_type: str,
    ) -> list:
        """Find swing high or low points.

        A swing high is a point where both neighbours have lower highs.
        A swing low is a point where both neighbours have higher lows.
        """
        if len(candles) < 5:
            return []

        n = len(candles)
        points = []

        for i in range(2, n - 2):
            c = candles[i]
            if swing_type == "high":
                # Swing high: all neighbours have lower highs
                if (c.high > candles[i-1].high and
                    c.high > candles[i-2].high and
                    c.high > candles[i+1].high and
                    c.high > candles[i+2].high):
                    from dataclasses import dataclass
                    @dataclass
                    class SwingPoint:
                        index: int
                        price: float
                    points.append(SwingPoint(index=i, price=c.high))
            else:
                # Swing low: all neighbours have higher lows
                if (c.low < candles[i-1].low and
                    c.low < candles[i-2].low and
                    c.low < candles[i+1].low and
                    c.low < candles[i+2].low):
                    from dataclasses import dataclass
                    @dataclass
                    class SwingPoint:
                        index: int
                        price: float
                    points.append(SwingPoint(index=i, price=c.low))

        return points

    def _calc_pullback_score(self, candles: list[NormalizedCandle]) -> float:
        """Score pullback validity.

        Returns -1 to +1:
        - Positive = valid buy pullback in uptrend
        - Negative = valid sell pullback in downtrend
        - Near 0 = no valid pullback

        Uses a continuous scoring approach based on:
        - EMA alignment (20 above 50 = bullish trend)
        - Price distance from EMA (how far pulled back)
        - RSI position (oversold/overbought confirmation)
        """
        if len(candles) < 30:
            return 0.0

        closes = np.array([c.close for c in candles])

        # Calculate EMAs
        ema_20 = self._ema(closes, 20)
        ema_50 = self._ema(closes, 50)

        if ema_20 == 0 or ema_50 == 0:
            return 0.0

        # Price relative to EMAs (continuous, not binary)
        price_vs_ema20 = (closes[-1] - ema_20) / ema_20  # e.g. -0.03 = 3% below EMA

        # EMA alignment indicates trend direction
        ema_bullish = ema_20 > ema_50
        ema_bearish = ema_20 < ema_50

        # RSI for confirmation
        rsi = self._calc_rsi(closes, 14)
        rsi_oversold = rsi < 40
        rsi_overbought = rsi > 60

        # Continuous pullback score based on depth and trend alignment
        if ema_bullish:
            # Uptrend: pullback is price getting close to/below EMA20
            if price_vs_ema20 < 0:
                pullback_depth = min(1.0, abs(price_vs_ema20) * 20)  # 5% pullback = 1.0
                # Bonus if RSI confirms oversold
                if rsi_oversold:
                    pullback_depth = min(1.0, pullback_depth * 1.2)
                return float(pullback_depth)
            else:
                # Price above EMA - not a pullback, slight positive
                return float(np.clip(price_vs_ema20 * 5, 0.0, 0.3))
        elif ema_bearish:
            # Downtrend: pullback is price getting close to/above EMA20
            if price_vs_ema20 > 0:
                pullback_depth = -min(1.0, abs(price_vs_ema20) * 20)
                if rsi_overbought:
                    pullback_depth = max(-1.0, pullback_depth * 1.2)
                return float(pullback_depth)
            else:
                return float(np.clip(price_vs_ema20 * 5, -0.3, 0.0))

        return 0.0

    def _calc_momentum_score(self, candles: list[NormalizedCandle]) -> float:
        """Score momentum indicators (RSI + MACD).

        Returns -1 to +1:
        - Positive = bullish momentum
        - Negative = bearish momentum

        Uses continuous scoring based on RSI distance from 50 and MACD histogram strength.
        """
        if len(candles) < 30:
            return 0.0

        closes = np.array([c.close for c in candles])

        # RSI - continuous score based on distance from 50
        rsi = self._calc_rsi(closes, 14)
        rsi_distance = abs(rsi - 50) / 50  # 0 at 50, 1 at 0 or 100

        # MACD score
        macd_score = self._calc_macd_score(closes)

        # Combined: magnitude from RSI and direction from MACD
        # Higher RSI distance = more momentum conviction
        momentum_magnitude = rsi_distance
        # MACD confirms direction
        if macd_score > 0:
            raw = momentum_magnitude
        else:
            raw = -momentum_magnitude

        return float(np.clip(raw, -1.0, 1.0))

    def _calc_macd_score(self, closes: np.ndarray) -> float:
        """Calculate MACD score -1 to +1."""
        if len(closes) < 26:
            return 0.0

        ema_12 = self._ema(closes, 12)
        ema_26 = self._ema(closes, 26)

        if ema_12 == 0 or ema_26 == 0:
            return 0.0

        macd_line = ema_12 - ema_26

        # Signal line approximation: EMA of MACD over 9 periods
        # We simplify by checking MACD histogram direction
        macd_hist = macd_line / ema_26 if ema_26 != 0 else 0

        # Normalize to roughly -1 to +1 range
        return float(np.clip(macd_hist * 5, -1.0, 1.0))

    def _calc_rsi(self, closes: np.ndarray, period: int = 14) -> float:
        """Calculate RSI value 0-100."""
        if len(closes) < period + 2:
            return 50.0

        deltas = np.diff(closes[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = float(gains.mean())
        avg_loss = float(losses.mean())

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1 + rs))

    def _calc_volume_score(self, candles: list[NormalizedCandle]) -> float:
        """Score volume confirmation — returns raw ratio for later normalization.

        Returns raw volume ratio (recent/prior) - will be normalized across
        all candidates in a post-processing step for proper relative ranking.
        """
        if len(candles) < 20:
            return 0.0

        volumes = np.array([c.volume for c in candles])

        # Compare recent volume (last 5 candles) to average of prior 15
        recent_vol = volumes[-5:].mean()
        prior_vol = volumes[-20:-5].mean() if len(volumes) >= 20 else volumes[:-5].mean()

        if prior_vol == 0:
            return 0.5

        vol_ratio = recent_vol / prior_vol
        return float(np.clip(vol_ratio, 0.0, 3.0))  # Cap at 3x to avoid extreme outliers

    @staticmethod
    def _direction_from_votes(
        structure_score: float,
        pullback_score: float,
        momentum_score: float,
    ) -> str | None:
        """Return the direction implied by a 2-of-3 component vote (v0.2.0).

        Each component casts a vote if its signed score crosses the
        gate in the same direction. The overall direction is set only
        when at least 2 of 3 components agree. This prevents the
        v0.1.0 failure mode where structure/pullback rarely agreed
        (their gates were effectively unreachable in production data)
        and the momentum-only fallback at ±0.2 silently biased
        direction toward whichever sign momentum happened to favor
        (mean -0.22 in the recent calibration window — i.e. sell).

        Volume does NOT vote on direction — it is confirmation only.
        Volume's job is to upweight the confluence score; whether
        the move is up or down is for structure/pullback/momentum
        to decide.

        Returns:
            "buy" if at least 2 components vote buy,
            "sell" if at least 2 components vote sell,
            None otherwise (0 votes, 1 vote, or split 1/1/1).
        """
        votes: list[str] = []
        if structure_score > PairRanker.STRUCTURE_GATE:
            votes.append("buy")
        elif structure_score < -PairRanker.STRUCTURE_GATE:
            votes.append("sell")

        if pullback_score > PairRanker.PULLBACK_GATE:
            votes.append("buy")
        elif pullback_score < -PairRanker.PULLBACK_GATE:
            votes.append("sell")

        if momentum_score > PairRanker.MOMENTUM_GATE:
            votes.append("buy")
        elif momentum_score < -PairRanker.MOMENTUM_GATE:
            votes.append("sell")

        if votes.count("buy") >= 2:
            return "buy"
        if votes.count("sell") >= 2:
            return "sell"
        return None

    @staticmethod
    def normalize_volume_scores(pairs: list[RankedPair]) -> None:
        """Normalize volume scores across all pairs to 0-1 range.

        Uses min-max scaling of the raw volume ratios to produce a
        continuous ranking. Pairs with no volume data get 0.5.
        """
        if not pairs:
            return

        raw_scores = [p.volume_score for p in pairs]
        min_s = min(raw_scores)
        max_s = max(raw_scores)
        rng = max_s - min_s if max_s > min_s else 1.0

        for p in pairs:
            p.volume_score = float((p.volume_score - min_s) / rng)

    @staticmethod
    def _ema(closes: np.ndarray, period: int) -> float:
        """Calculate EMA."""
        if len(closes) < period:
            return float(closes[-1]) if len(closes) > 0 else 0.0

        mult = 2.0 / (period + 1)
        ema = float(closes[0])
        for price in closes[1:]:
            ema = (float(price) - ema) * mult + ema
        return ema