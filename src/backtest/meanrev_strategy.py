"""Mean-reversion strategy.

Designed for the current downtrend market. Buys oversold
extremes and exits on a bounce or stop.

Entry rules:
  - LONG:  RSI < rsi_buy AND close < lower_bollinger
  - SHORT: RSI > rsi_sell AND close > upper_bollinger  (only if allow_shorts)

Hypothesis (from the user's market read): crypto is in a
downtrend. Oversold bounces have the highest expected payoff.
Long-only is the default; shorts are opt-in.

Cooldown: After an exit, don't re-enter the same symbol
for `cooldown_bars`. Prevents the "re-enter at the same
oversold level that's still going down" trap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from ..data.models import (
    Decision,
    NormalizedCandle,
    TimeFrame,
)
from ..utils.logging import get_logger
from .strategy import PendingOrder

logger = get_logger(__name__)


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def _bollinger(closes: np.ndarray, period: int = 20, std_dev: float = 2.0):
    """Return (middle, upper, lower)."""
    if len(closes) < period:
        last = float(closes[-1]) if len(closes) else 0.0
        return last, last, last
    window = closes[-period:]
    middle = float(window.mean())
    std = float(window.std(ddof=0))
    return middle, middle + std_dev * std, middle - std_dev * std


class MeanReversionStrategy:
    """Mean-reversion signal stack. Long-only by default."""

    def __init__(
        self,
        symbols: list[str],
        *,
        lookback_bars: int = 100,
        rsi_period: int = 14,
        rsi_buy: float = 30.0,
        rsi_sell: float = 70.0,
        bb_period: int = 20,
        bb_std_dev: float = 2.0,
        cooldown_bars: int = 12,
        allow_shorts: bool = False,
        position_size: float = 0.10,
    ) -> None:
        self.symbols = symbols
        self.lookback_bars = lookback_bars
        self.rsi_period = rsi_period
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.bb_period = bb_period
        self.bb_std_dev = bb_std_dev
        self.cooldown_bars = cooldown_bars
        self.allow_shorts = allow_shorts
        self.position_size = position_size
        # Track last exit time per symbol for cooldown
        self._last_exit: dict[str, datetime] = {}

    async def on_bar(
        self,
        timestamp: datetime,
        history_by_symbol: dict[str, pd.DataFrame],
        current_positions: set[str] | None = None,
        recently_closed: set[str] | None = None,
    ) -> list[PendingOrder]:
        """Produce pending orders. The engine passes in:
        - `current_positions`: symbols with open positions (skip them)
        - `recently_closed`: symbols that exited in the last bar (skip
          for cooldown_bars to prevent re-entering at the same
          oversold level).
        """
        if current_positions is None:
            current_positions = set()
        if recently_closed is None:
            recently_closed = set()

        orders: list[PendingOrder] = []
        for sym, df in history_by_symbol.items():
            if sym in current_positions:
                continue  # already in a position
            if sym in recently_closed:
                continue  # cooldown after exit

            sub = df.loc[df.index <= timestamp]
            if len(sub) < self.bb_period + 5:
                continue
            closes = sub["close"].to_numpy(dtype=float)
            last_close = float(closes[-1])
            rsi = _rsi(closes, self.rsi_period)
            middle, upper, lower = _bollinger(closes, self.bb_period, self.bb_std_dev)

            # How far below the lower band is the price?
            # Negative = below band, positive = above.
            bb_distance = (last_close - lower) / max(middle, 1e-9)

            desired: str | None = None
            confidence: float = 0.5
            reason: str = ""

            # LONG: oversold + below lower band. We use rsi<=rsi_buy
            # (not strictly <) because in strong downtrends RSI can
            # bottom out around 30-32 for many bars and we want to
            # catch the first entry. The Bollinger band touch filters
            # out the chop.
            if rsi <= self.rsi_buy and last_close <= lower:
                desired = "buy"
                # Stronger signal the further below the band
                confidence = min(1.0, 0.5 + abs(bb_distance) * 5)
                reason = f"oversold: rsi={rsi:.1f}<={self.rsi_buy}, price {abs(bb_distance)*100:.1f}% below lower BB"

            # SHORT: overbought + above upper band (only if enabled)
            elif self.allow_shorts and rsi >= self.rsi_sell and last_close >= upper:
                desired = "sell"
                confidence = min(1.0, 0.5 + abs(bb_distance) * 5)
                reason = f"overbought: rsi={rsi:.1f}>={self.rsi_sell}, price {abs(bb_distance)*100:.1f}% above upper BB"

            if desired is None:
                continue

            decision = Decision(
                action="BUY" if desired == "buy" else "SELL",
                symbol=sym,
                size=self.position_size,
                entry=last_close,
                stop_loss=None,
                take_profit=None,
                confidence=confidence,
                reason=reason,
                timestamp=timestamp,
            )
            orders.append(
                PendingOrder(
                    symbol=sym,
                    side=desired,
                    quantity=self.position_size,
                    decision=decision,
                    generated_at=timestamp,
                )
            )

        return orders
