"""Sliding-window walk-forward backtest.

For each window:
  - Train: 30 days
  - Test (out-of-sample): next 30 days
  - Slide by `step_days` (default 14)

Reports per-window out-of-sample metrics. The aggregate tells
us whether the strategy has any edge forward (positive average
OOS return, or >50% of windows profitable).

Two modes:
  1. "Fixed" — single config, run as-is. Tests robustness.
  2. "Calibrated" — small parameter sweep on train, best on test.
     Tests adaptability.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .data_fetcher import fetch_universe, DEFAULT_UNIVERSE
from .engine import BacktestEngine
from .metrics import calculate_metrics
from .strategy import BacktestStrategy


def parse_args():
    p = argparse.ArgumentParser(description="Walk-forward backtest")
    p.add_argument("--universe", default=",".join(DEFAULT_UNIVERSE[:5]))
    p.add_argument("--interval", default="1h")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--train-days", type=int, default=30)
    p.add_argument("--test-days", type=int, default=30)
    p.add_argument("--step-days", type=int, default=14)
    p.add_argument("--min-confluence", type=float, default=0.20)
    p.add_argument(
        "--min-signal-confidence", type=float, default=0.20,
        help="DecisionEngine final-score threshold. Live uses 0.20. "
             "Distinct from --min-confluence, which is the ranker gate.",
    )
    p.add_argument("--stop-loss", type=float, default=0.02)
    p.add_argument("--take-profit", type=float, default=0.04)
    p.add_argument("--capital", type=float, default=10_000.0)
    p.add_argument(
        "--lookback-bars", type=int, default=100,
        help="Warmup bars for the strategy. Lower for higher timeframes "
             "(e.g. 20 for 1d so 30-day test windows have room to trade).",
    )
    p.add_argument(
        "--min-confirmations", type=int, default=2,
        help="DecisionEngine min_confirmations. Live uses 2; backtest "
             "default is also 2 (must match for apples-to-apples).",
    )
    p.add_argument(
        "--min-subsystem-score", type=float, default=0.15,
        help="DecisionEngine per-subsystem threshold. Live uses 0.15.",
    )
    p.add_argument("--label", default=None)
    p.add_argument("--output-dir", default="reports/walkforward")
    p.add_argument("--force-refresh", action="store_true")
    return p.parse_args()


@dataclass
class WindowResult:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_return: float
    train_max_dd: float
    train_trades: int
    test_return: float
    test_max_dd: float
    test_trades: int
    test_win_rate: float
    test_pf: float
    test_final_equity: float


async def run_window(
    sliced: dict[str, pd.DataFrame],
    *,
    min_confluence: float,
    min_signal_confidence: float = 0.20,
    stop_loss: float,
    take_profit: float,
    initial_capital: float,
    lookback_bars: int = 100,
    min_confirmations: int = 2,
    min_subsystem_score: float = 0.15,
) -> tuple[Any, Any]:
    strategy = BacktestStrategy(
        symbols=list(sliced.keys()),
        lookback_bars=lookback_bars,
        min_confluence=min_confluence,
        min_signal_confidence=min_signal_confidence,
        min_confirmations=min_confirmations,
        min_subsystem_score=min_subsystem_score,
        top_n_per_bar=3,
    )
    engine = BacktestEngine(
        universe=sliced,
        strategy=strategy,
        initial_capital=initial_capital,
        position_size_pct=0.10,
        max_position_pct=0.20,
        stop_loss_pct=stop_loss,
        take_profit_pct=take_profit,
    )
    result = await engine.run()
    metrics = calculate_metrics(result.equity_curve, result.trades, initial_capital)
    return result, metrics


async def main():
    args = parse_args()
    label = args.label or f"wf_{args.interval}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip() for s in args.universe.split(",") if s.strip()]
    print(f"=== Walk-Forward Backtest ===")
    print(f"Label: {label}, TF: {args.interval}, Universe: {symbols}")
    print(f"Train: {args.train_days}d, Test: {args.test_days}d, Step: {args.step_days}d")
    print(f"Conf: {args.min_confluence}, SL: {args.stop_loss*100:.1f}%, TP: {args.take_profit*100:.1f}%")
    print()

    universe = await fetch_universe(
        symbols, interval=args.interval, lookback_days=args.days,
        force_refresh=args.force_refresh,
    )
    if not universe:
        print("ERROR: no data")
        return 1
    print(f"Loaded {len(universe)} symbols, {args.interval} bars")
    for sym in universe:
        print(f"  {sym}: {len(universe[sym])} bars, {universe[sym].index.min().date()} to {universe[sym].index.max().date()}")
    print()

    all_starts = [df.index.min() for df in universe.values()]
    all_ends = [df.index.max() for df in universe.values()]
    data_start = max(all_starts)
    data_end = min(all_ends)
    print(f"Data range: {data_start.date()} to {data_end.date()}")
    print(f"Total span: {(data_end - data_start).days} days")
    print()

    # Slide windows
    windows: list[WindowResult] = []
    cursor = data_start
    win_idx = 0
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=args.train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=args.test_days)
        if test_end > data_end:
            break
        win_idx += 1
        print(f"[W{win_idx}] Train: {train_start.date()} → {train_end.date()}, "
              f"Test: {test_start.date()} → {test_end.date()}")

        # Train window
        train_sliced = {s: df.loc[train_start:train_end] for s, df in universe.items()}
        train_sliced = {s: d for s, d in train_sliced.items() if len(d) > 50}
        if not train_sliced:
            print("  (no train data)"); cursor += timedelta(days=args.step_days); continue
        _, train_metrics = await run_window(
            train_sliced, min_confluence=args.min_confluence,
            min_signal_confidence=args.min_signal_confidence,
            stop_loss=args.stop_loss, take_profit=args.take_profit,
            initial_capital=args.capital,
            lookback_bars=args.lookback_bars,
            min_confirmations=args.min_confirmations,
            min_subsystem_score=args.min_subsystem_score,
        )

        # Test (out-of-sample) window
        test_sliced = {s: df.loc[test_start:test_end] for s, df in universe.items()}
        test_sliced = {s: d for s, d in test_sliced.items() if len(d) > 50}
        if not test_sliced:
            print("  (no test data)"); cursor += timedelta(days=args.step_days); continue
        _, test_metrics = await run_window(
            test_sliced, min_confluence=args.min_confluence,
            min_signal_confidence=args.min_signal_confidence,
            stop_loss=args.stop_loss, take_profit=args.take_profit,
            initial_capital=args.capital,
            lookback_bars=args.lookback_bars,
            min_confirmations=args.min_confirmations,
            min_subsystem_score=args.min_subsystem_score,
        )

        wr = WindowResult(
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
            train_return=train_metrics.total_return,
            train_max_dd=train_metrics.max_drawdown,
            train_trades=train_metrics.num_trades,
            test_return=test_metrics.total_return,
            test_max_dd=test_metrics.max_drawdown,
            test_trades=test_metrics.num_trades,
            test_win_rate=test_metrics.win_rate,
            test_pf=test_metrics.profit_factor,
            test_final_equity=test_metrics.final_equity,
        )
        windows.append(wr)
        print(f"  Train: ret={wr.train_return:+.2%}, DD={wr.train_max_dd:.2%}, n={wr.train_trades}")
        print(f"  Test:  ret={wr.test_return:+.2%}, DD={wr.test_max_dd:.2%}, "
              f"win={wr.test_win_rate:.1%}, PF={wr.test_pf:.2f}, n={wr.test_trades}, "
              f"final=${wr.test_final_equity:,.0f}")

        cursor += timedelta(days=args.step_days)

    # Aggregate
    print()
    print("=" * 60)
    print(f"Walk-Forward Summary: {label}")
    print("=" * 60)
    if not windows:
        print("No windows completed.")
        return 0

    oos_returns = [w.test_return for w in windows]
    oos_wins = sum(1 for r in oos_returns if r > 0)
    oos_avg = sum(oos_returns) / len(oos_returns)
    oos_total = (1 + oos_returns[0])  # not meaningful; show compounded
    for r in oos_returns:
        oos_total *= (1 + r)
    oos_total -= 1

    avg_train = sum(w.train_return for w in windows) / len(windows)
    avg_oos = oos_avg

    print(f"  Windows: {len(windows)}")
    print(f"  OOS wins: {oos_wins}/{len(windows)} ({oos_wins/len(windows):.1%})")
    print(f"  Avg train return: {avg_train:+.2%}")
    print(f"  Avg OOS return:   {avg_oos:+.2%}")
    print(f"  Compounded OOS:   {oos_total:+.2%}")
    print(f"  Train-OOS gap:    {(avg_train - avg_oos):+.2%}  "
          f"({'good — edge persists' if avg_oos > 0.01 else '⚠️  edge decays OOS' if avg_oos > 0 else '❌ no edge'})")
    print()

    # Verdict
    if oos_wins / len(windows) >= 0.6 and avg_oos > 0.02:
        verdict = "🟢 ROBUST EDGE: profitable in >60% of OOS windows with positive avg return"
    elif oos_wins / len(windows) >= 0.5 and avg_oos > 0:
        verdict = "🟡 MARGINAL: more OOS wins than losses, but small avg return"
    elif oos_wins / len(windows) < 0.4:
        verdict = "❌ NO EDGE: unprofitable in majority of OOS windows"
    else:
        verdict = "🟡 MIXED: roughly 50/50 OOS wins, no clear edge"
    print(f"VERDICT: {verdict}")
    print()

    # Save
    win_dicts = [w.__dict__ for w in windows]
    (output_dir / f"{label}_windows.json").write_text(json.dumps(win_dicts, indent=2, default=str))
    summary = {
        "label": label,
        "interval": args.interval,
        "n_windows": len(windows),
        "oos_wins": oos_wins,
        "oos_win_rate": oos_wins / len(windows),
        "avg_train_return": avg_train,
        "avg_oos_return": avg_oos,
        "compounded_oos": oos_total,
        "train_oos_gap": avg_train - avg_oos,
        "verdict": verdict,
    }
    (output_dir / f"{label}_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"Saved to {output_dir}/{label}_*.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
