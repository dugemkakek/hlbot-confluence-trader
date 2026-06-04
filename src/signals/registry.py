"""Signal registry for managing and aggregating trading signals.

The registry:
- Discovers signal modules by convention
- Aggregates signals per symbol/timeframe
- Caches signals in Redis with TTL
- Provides unified signal interface to decision engine
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..utils.logging import get_logger
from ..utils.config import get_config
from ..data.models import Signal, Side, TimeFrame

logger = get_logger(__name__)


@dataclass
class AggregatedSignal:
    """Aggregated signals for a symbol/timeframe."""
    symbol: str
    timeframe: TimeFrame
    signals: list[Signal] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def net_direction(self) -> Side | None:
        """Net signal direction based on signal weights."""
        if not self.signals:
            return None
        buy_count = sum(1 for s in self.signals if s.direction == Side.BUY)
        sell_count = sum(1 for s in self.signals if s.direction == Side.SELL)
        if buy_count > sell_count:
            return Side.BUY
        elif sell_count > buy_count:
            return Side.SELL
        return None

    @property
    def avg_confidence(self) -> float:
        """Average confidence across all signals."""
        if not self.signals:
            return 0.0
        return sum(s.confidence for s in self.signals) / len(self.signals)

    @property
    def signal_count(self) -> int:
        """Number of active signals."""
        return len(self.signals)


class SignalRegistry:
    """Central registry for trading signals.

    Collects signals from all registered signal modules and provides
    aggregated views per symbol and timeframe.
    """

    def __init__(self) -> None:
        """Initialize the signal registry."""
        self._signals: dict[str, list[Signal]] = {}  # key = f"{symbol}:{timeframe}"
        self._modules: dict[str, Any] = {}
        logger.info("SignalRegistry initialized")

    def register_signal(self, signal: Signal) -> None:
        """Register a single signal.

        Args:
            signal: Signal to register.
        """
        key = f"{signal.symbol}:{signal.timeframe.value}"
        self._signals.setdefault(key, []).append(signal)
        logger.debug(
            "Signal registered",
            signal_name=signal.name,
            symbol=signal.symbol,
            direction=signal.direction.value,
            confidence=signal.confidence,
        )

    def register_module(self, name: str, module: Any) -> None:
        """Register a signal module.

        Args:
            name: Module name.
            module: Module instance with compute_* methods.
        """
        self._modules[name] = module
        logger.info("Signal module registered", module=name)

    def get_signals(self, symbol: str, timeframe: TimeFrame | str) -> list[Signal]:
        """Get all signals for a symbol/timeframe.

        Args:
            symbol: Trading pair (e.g. "BTC").
            timeframe: TimeFrame or string.

        Returns:
            List of signals.
        """
        tf = timeframe.value if isinstance(timeframe, TimeFrame) else timeframe
        key = f"{symbol}:{tf}"
        return self._signals.get(key, [])

    def get_aggregated(self, symbol: str, timeframe: TimeFrame | str) -> AggregatedSignal:
        """Get aggregated signals for a symbol/timeframe.

        Args:
            symbol: Trading pair.
            timeframe: TimeFrame or string.

        Returns:
            AggregatedSignal with all signals rolled up.
        """
        signals = self.get_signals(symbol, timeframe)
        tf = timeframe.value if isinstance(timeframe, TimeFrame) else timeframe
        return AggregatedSignal(symbol=symbol, timeframe=TimeFrame(tf), signals=signals)

    def clear_stale(self, max_age_seconds: float = 300) -> int:
        """Remove signals older than max_age_seconds.

        Args:
            max_age_seconds: Maximum age in seconds.

        Returns:
            Number of signals removed.
        """
        now = datetime.now(timezone.utc)
        removed = 0
        for key, signals in list(self._signals.items()):
            original_count = len(signals)
            self._signals[key] = [
                s for s in signals
                if (now - s.timestamp).total_seconds() < max_age_seconds
            ]
            removed += original_count - len(self._signals[key])
        if removed:
            logger.info("Stale signals cleared", count=removed)
        return removed

    def get_all_symbols(self) -> list[str]:
        """Get all symbols with registered signals."""
        symbols = set()
        for key in self._signals:
            symbol = key.split(":")[0]
            symbols.add(symbol)
        return sorted(symbols)

    def summary(self) -> dict[str, Any]:
        """Get a summary of all registered signals."""
        return {
            "total_signals": sum(len(v) for v in self._signals.values()),
            "symbols": self.get_all_symbols(),
            "modules": list(self._modules.keys()),
            "by_key": {
                k: len(v) for k, v in self._signals.items()
            },
        }
