"""Performance metrics for backtest results.

Follows the backtesting-frameworks skill's calculate_metrics() pattern:
- Annualization factor 8760 for hourly bars (24*365)
- Sharpe, Sortino, Calmar
- Win rate, profit factor
- Max drawdown + duration
- Exposure stats
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class PerformanceMetrics:
    total_return: float
    annual_return: float
    annual_volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    max_drawdown_duration_bars: int
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    num_trades: int
    num_wins: int
    num_losses: float
    avg_bars_held: float
    exposure_pct_avg: float
    final_equity: float
    initial_capital: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_metrics(
    equity_curve: pd.DataFrame,
    trades: list,
    initial_capital: float,
    annualization_factor: float = 8760.0,  # hourly bars: 24*365
) -> PerformanceMetrics:
    """Compute all performance metrics.

    equity_curve: DataFrame with DatetimeIndex, columns [equity, cash, exposure]
    trades: list of ClosedTrade dataclasses
    """
    if equity_curve.empty:
        return PerformanceMetrics(
            total_return=0.0, annual_return=0.0, annual_volatility=0.0,
            sharpe_ratio=0.0, sortino_ratio=0.0, calmar_ratio=0.0,
            max_drawdown=0.0, max_drawdown_duration_bars=0,
            win_rate=0.0, profit_factor=0.0, avg_win=0.0, avg_loss=0.0,
            num_trades=0, num_wins=0, num_losses=0.0, avg_bars_held=0.0,
            exposure_pct_avg=0.0, final_equity=initial_capital,
            initial_capital=initial_capital,
        )

    equity = equity_curve["equity"]
    returns = equity.pct_change().dropna()

    # ── Returns ──
    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_capital - 1.0
    n_bars = len(equity)
    # Annualization is meaningless on short windows. With 1h bars
    # and a 30-day test, ratio = 8760/720 = 12.2x — already a stretch.
    # We refuse to annualize unless the test is at least 90 days
    # (annualization factor / n_bars <= 1.21). Below that, annual_return
    # is set to NaN and the report flags the window as too short.
    if n_bars > 1 and (1 + total_return) > 0:
        ratio = annualization_factor / n_bars
        if ratio > 1.21:  # less than 90 days of 1h bars
            annual_return = float("nan")
        else:
            annual_return = (1 + total_return) ** ratio - 1
    else:
        annual_return = 0.0
    annual_vol = float(returns.std() * np.sqrt(annualization_factor)) if len(returns) > 1 else 0.0

    # ── Sharpe / Sortino ──
    # If annualization was capped (short window), Sharpe is meaningless
    # — return NaN so the report can flag it instead of printing a
    # misleading number like 2.9 billion.
    if annual_return == float("inf") or annual_vol == 0.0:
        sharpe = float("nan")
    else:
        sharpe = annual_return / annual_vol

    downside = returns[returns < 0]
    downside_vol = float(downside.std() * np.sqrt(annualization_factor)) if len(downside) > 1 else 0.0
    if annual_return == float("inf") or downside_vol == 0.0:
        sortino = float("nan")
    else:
        sortino = annual_return / downside_vol

    # ── Drawdown ──
    rolling_max = equity.cummax()
    drawdown = (equity - rolling_max) / rolling_max
    max_dd = float(drawdown.min()) if len(drawdown) > 0 else 0.0

    # Longest underwater streak
    underwater = drawdown < 0
    if underwater.any():
        streaks = (underwater != underwater.shift()).cumsum()
        streak_groups = streaks[underwater]
        if len(streak_groups) > 0:
            max_dd_dur = int(streak_groups.value_counts().max())
        else:
            max_dd_dur = 0
    else:
        max_dd_dur = 0

    calmar = (annual_return / abs(max_dd)) if (max_dd < 0 and annual_return not in (0.0, float("inf"))) else (float("nan") if annual_return == float("inf") else 0.0)

    # ── Trade stats ──
    pnls = [t.pnl_net for t in trades]
    n_trades = len(pnls)
    if n_trades > 0:
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        n_wins = len(wins)
        n_losses = len(losses)
        win_rate = n_wins / n_trades
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        avg_bars = float(np.mean([t.bars_held for t in trades]))
    else:
        n_wins = 0
        n_losses = 0
        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        profit_factor = 0.0
        avg_bars = 0.0

    # ── Exposure stats ──
    if "exposure" in equity_curve.columns and "equity" in equity_curve.columns:
        exposure_ratio = equity_curve["exposure"] / equity_curve["equity"].replace(0, np.nan)
        exposure_pct_avg = float(exposure_ratio.fillna(0).mean())
    else:
        exposure_pct_avg = 0.0

    return PerformanceMetrics(
        total_return=float(total_return),
        annual_return=float(annual_return),
        annual_volatility=float(annual_vol),
        sharpe_ratio=float(sharpe),
        sortino_ratio=float(sortino),
        calmar_ratio=float(calmar),
        max_drawdown=float(max_dd),
        max_drawdown_duration_bars=int(max_dd_dur),
        win_rate=float(win_rate),
        profit_factor=float(profit_factor),
        avg_win=float(avg_win),
        avg_loss=float(avg_loss),
        num_trades=int(n_trades),
        num_wins=int(n_wins),
        num_losses=float(n_losses),
        avg_bars_held=float(avg_bars),
        exposure_pct_avg=float(exposure_pct_avg),
        final_equity=final_equity,
        initial_capital=initial_capital,
    )
