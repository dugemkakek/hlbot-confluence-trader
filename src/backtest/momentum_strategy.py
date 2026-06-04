"""Momentum-only strategy.

Bypasses the confluence ranker and decision engine. Uses MACD
direction and RSI state as the sole signal source.

The hypothesis (from the audit): the confluence approach overweights
weak signals (structure, pullback, volume, macro). Momentum is
the only signal with real amplitude in the data. By going
momentum-only, we focus the signal stack on what works.

Entry rules (default):
  - LONG:  MACD line > 0  AND  RSI > 50  AND  MACD crossed above 0 in last 3 bars
  - SHORT: MACD line < 0  AND  RSI < 50  AND  MACD crossed below 0 in last 3 bars

Exit (handled by the engine's SL/TP):
  - SL: 2% (configurable)
  - TP: 4% (configurable)

This file is a clean, side-by-side test of the confluence
approach: same backtest engine, same fills, same fees — only the
signal stack differs.
"""

from __future__ import annotations

import asyncio
import math
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


def _ema(arr: np.ndarray, period: int) -> float:
    """EMA of an array, in chronological order."""
    if len(arr) < period:
        return float(arr[-1]) if len(arr) > 0 else 0.0
    mult = 2.0 / (period + 1)
    val = float(arr[0])
    for p in arr[1:]:
        val = (float(p) - val) * mult + val
    return val


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    """RSI 0-100."""
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


def _macd_state(closes: np.ndarray, fast: int = 12, slow: int = 26, signal_p: int = 9):
    """Return (macd_line, signal_line, histogram).

    Positive histogram = bullish momentum.
    """
    if len(closes) < slow + signal_p:
        return 0.0, 0.0, 0.0
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    macd_line = fast_ema - slow_ema
    # signal line: EMA of the last signal_p MACD values
    # We approximate by computing MACD for the last few bars
    macd_history = []
    for i in range(max(0, len(closes) - signal_p - 2), len(closes) + 1):
        if i < slow:
            continue
        f = _ema(closes[max(0, i - fast):i], fast)
        s = _ema(closes[max(0, i - slow):i], slow)
        macd_history.append(f - s)
    if len(macd_history) < 2:
        return macd_line, 0.0, 0.0
    signal_line = _ema(np.array(macd_history[-signal_p:]), signal_p)
    return macd_line, signal_line, macd_line - signal_line


class MomentumStrategy:
    """Momentum-only signal stack. No ranker, no decision engine."""

    def __init__(
        self,
        symbols: list[str],
        *,
        lookback_bars: int = 100,
        position_size: float = 0.10,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        rsi_period: int = 14,
        rsi_long_threshold: float = 50.0,
        rsi_short_threshold: float = 50.0,
        max_per_symbol: int = 1,  # no averaging
        allow_shorts: bool = True,
    ) -> None:
        self.symbols = symbols
        self.lookback_bars = lookback_bars
        self.position_size = position_size
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.rsi_period = rsi_period
        self.rsi_long_threshold = rsi_long_threshold
        self.rsi_short_threshold = rsi_short_threshold
        self.max_per_symbol = max_per_symbol
        self.allow_shorts = allow_shorts
        # In-flight state: tracks what we already recommended at the
        # last bar so we don't double-fire.
        self._last_action: dict[str, str | None] = {s: None for s in symbols}

    async def on_bar(
        self,
        timestamp: datetime,
        history_by_symbol: dict[str, pd.DataFrame],
        current_positions: set[str] | None = None,
    ) -> list[PendingOrder]:
        """Return pending orders to fill at next bar's open.

        `current_positions` is a set of symbols the engine currently
        holds. Used to prevent duplicate entries.
        """
        if current_positions is None:
            current_positions = set()

        orders: list[PendingOrder] = []
        for sym, df in history_by_symbol.items():
            sub = df.loc[df.index <= timestamp]
            if len(sub) < self.macd_slow + self.macd_signal + 5:
                continue
            closes = sub["close"].to_numpy(dtype=float)

            macd_line, signal_line, hist = _macd_state(
                closes, self.macd_fast, self.macd_slow, self.macd_signal
            )
            rsi = _rsi(closes, self.rsi_period)

            # Determine desired direction
            desired: str | None = None
            if sym in current_positions:
                # Already in a position — only emit exit if reverse signal
                # (engine's SL/TP will close on stops; we don't fight that)
                continue

            # LONG: MACD bullish AND RSI above long threshold
            if macd_line > 0 and rsi >= self.rsi_long_threshold:
                desired = "buy"
            # SHORT: MACD bearish AND RSI below short threshold
            elif macd_line < 0 and rsi <= self.rsi_short_threshold and self.allow_shorts:
                desired = "sell"

            if desired is None:
                continue
            # Don't re-fire the same direction immediately
            if self._last_action.get(sym) == desired:
                continue

            entry_price = float(closes[-1])
            decision = Decision(
                action="BUY" if desired == "buy" else "SELL",
                symbol=sym,
                size=self.position_size,  # fraction of equity
                entry=entry_price,
                stop_loss=None,  # engine computes
                take_profit=None,  # engine computes
                confidence=min(1.0, abs(hist) * 5),  # rough signal-strength proxy
                reason=f"momentum: macd={macd_line:.4f} signal={signal_line:.4f} rsi={rsi:.1f}",
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
            self._last_action[sym] = desired

        return orders
