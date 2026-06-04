"""Technical analysis signal generation.

Implements common TA indicators as signal generators.
"""

from __future__ import annotations

import numpy as np
from typing import Optional

from ..data.models import NormalizedCandle, Side, Signal, TimeFrame
from ..utils.logging import get_logger

logger = get_logger(__name__)


class TechnicalSignals:
    """Collection of technical indicator signal generators."""

    @staticmethod
    def sma_cross(candles: list[NormalizedCandle], fast: int = 10, slow: int = 25) -> Optional[Signal]:
        if len(candles) < slow + 1:
            return None
        closes = np.array([c.close for c in candles])
        fast_sma = closes[-fast:].mean() if len(closes) >= fast else np.nan
        slow_sma_prev = closes[-(slow + 1):-1].mean() if len(closes) >= slow + 1 else np.nan
        slow_sma = closes[-slow:].mean()
        fast_sma_prev = closes[-(fast + 1):-1].mean() if len(closes) >= fast + 1 else np.nan
        if np.isnan(fast_sma_prev) or np.isnan(slow_sma_prev):
            return None
        if fast_sma_prev <= slow_sma_prev and fast_sma > slow_sma:
            spread_pct = abs(fast_sma - slow_sma) / slow_sma
            return Signal(
                name="sma_cross", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.BUY, confidence=float(min(1.0, spread_pct * 10)),
                metadata={"fast": fast, "slow": slow, "fast_sma": float(fast_sma), "slow_sma": float(slow_sma)},
            )
        elif fast_sma_prev >= slow_sma_prev and fast_sma < slow_sma:
            spread_pct = abs(slow_sma - fast_sma) / slow_sma
            return Signal(
                name="sma_cross", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.SELL, confidence=float(min(1.0, spread_pct * 10)),
                metadata={"fast": fast, "slow": slow},
            )
        return None

    @staticmethod
    def ema_cross(candles: list[NormalizedCandle], fast: int = 12, slow: int = 26) -> Optional[Signal]:
        if len(candles) < slow + 1:
            return None
        closes = np.array([c.close for c in candles])
        def ema(arr, period):
            mult = 2.0 / (period + 1)
            val = float(arr[0])
            for p in arr[1:]:
                val = (float(p) - val) * mult + val
            return val
        fast_ema = ema(closes[-fast:], fast) if len(closes) >= fast else np.nan
        slow_ema_prev = ema(closes[-(slow + 1):-1], slow) if len(closes) >= slow + 1 else np.nan
        slow_ema = ema(closes[-slow:], slow) if len(closes) >= slow else np.nan
        fast_ema_prev = ema(closes[-(fast + 1):-1], fast) if len(closes) >= fast + 1 else np.nan
        if np.isnan(fast_ema_prev) or np.isnan(slow_ema_prev):
            return None
        if fast_ema_prev <= slow_ema_prev and fast_ema > slow_ema:
            spread_pct = abs(fast_ema - slow_ema) / slow_ema
            return Signal(name="ema_cross", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.BUY, confidence=float(min(1.0, spread_pct * 20)), metadata={"fast": fast, "slow": slow})
        elif fast_ema_prev >= slow_ema_prev and fast_ema < slow_ema:
            spread_pct = abs(slow_ema - fast_ema) / slow_ema
            return Signal(name="ema_cross", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.SELL, confidence=float(min(1.0, spread_pct * 20)), metadata={"fast": fast, "slow": slow})
        return None

    @staticmethod
    def rsi(candles: list[NormalizedCandle], period: int = 14) -> Optional[Signal]:
        if len(candles) < period + 2:
            return None
        closes = np.array([c.close for c in candles])
        deltas = np.diff(closes[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = float(gains.mean())
        avg_loss = float(losses.mean())
        rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
        rsi_val = 100.0 if avg_loss == 0 else (100.0 - (100.0 / (1 + rs)))
        if rsi_val < 30:
            confidence = (30 - rsi_val) / 30
            return Signal(name="rsi", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.BUY, confidence=float(min(1.0, confidence)), metadata={"rsi": float(rsi_val)})
        elif rsi_val > 70:
            confidence = (rsi_val - 70) / 30
            return Signal(name="rsi", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.SELL, confidence=float(min(1.0, confidence)), metadata={"rsi": float(rsi_val)})
        return None

    @staticmethod
    def macd(candles: list[NormalizedCandle], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Optional[Signal]:
        if len(candles) < slow + signal_period:
            return None
        closes = np.array([c.close for c in candles])
        def ema(arr, period):
            mult = 2.0 / (period + 1)
            val = float(arr[0])
            for p in arr[1:]:
                val = (float(p) - val) * mult + val
            return val
        fast_ema = ema(closes[-fast:], fast) if len(closes) >= fast else np.nan
        slow_ema = ema(closes[-slow:], slow) if len(closes) >= slow else np.nan
        if np.isnan(fast_ema) or np.isnan(slow_ema):
            return None
        macd_line = fast_ema - slow_ema
        macd_history = []
        for i in range(slow, min(len(closes) + 1, slow + signal_period + 2)):
            f = ema(closes[max(0, i - fast):i], fast) if i >= fast else np.nan
            s = ema(closes[max(0, i - slow):i], slow) if i >= slow else np.nan
            if not np.isnan(f) and not np.isnan(s):
                macd_history.append(f - s)
        if len(macd_history) < 2:
            return None
        signal_line = ema(np.array(macd_history[-signal_period:]), signal_period)
        if macd_history[-2] <= signal_line and macd_line > signal_line:
            return Signal(name="macd", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.BUY, confidence=0.65, metadata={"macd": float(macd_line), "signal": float(signal_line)})
        elif macd_history[-2] >= signal_line and macd_line < signal_line:
            return Signal(name="macd", symbol=candles[-1].symbol, timeframe=candles[-1].timeframe,
                direction=Side.SELL, confidence=0.65, metadata={"macd": float(macd_line), "signal": float(signal_line)})
        return None

    @staticmethod
    def atr(candles: list[NormalizedCandle], period: int = 14) -> float:
        if len(candles) < period + 1:
            return 0.0
        trs = []
        for i in range(1, min(period + 1, len(candles))):
            c = candles[-i]
            p = candles[-i - 1]
            tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            trs.append(tr)
        return float(np.mean(trs))

    @staticmethod
    def bollinger_bands(candles: list[NormalizedCandle], period: int = 20, std_dev: float = 2.0) -> Optional[Signal]:
        if len(candles) < period:
            return None
        closes = np.array([c.close for c in candles[-period:]])
        sma = float(closes.mean())
        std = float(closes.std())
        upper = sma + std_dev * std
        lower = sma - std_dev * std
        current = candles[-1]
        prev = candles[-2] if len(candles) >= 2 else None
        if prev is None:
            return None
        if prev.close <= upper and current.close > upper:
            return Signal(name="bb_breakout", symbol=current.symbol, timeframe=current.timeframe,
                direction=Side.BUY, confidence=0.70, metadata={"upper": float(upper)})
        elif prev.close >= lower and current.close < lower:
            return Signal(name="bb_breakout", symbol=current.symbol, timeframe=current.timeframe,
                direction=Side.SELL, confidence=0.70, metadata={"lower": float(lower)})
        return None
