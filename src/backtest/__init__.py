"""Backtest harness for HLBot.

Replays historical OHLCV through the production decision engine and
pair ranker, simulating fills at next bar's open with realistic fees
and slippage. Produces an equity curve, trade log, and performance
metrics report.
"""

from .metrics import calculate_metrics
from .engine import BacktestEngine, BacktestResult
from .execution import SimulatedExecution
from .strategy import BacktestStrategy

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "SimulatedExecution",
    "BacktestStrategy",
    "calculate_metrics",
]
