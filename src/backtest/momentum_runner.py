"""Run momentum-only backtest. Uses the engine's standard run() loop."""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from .data_fetcher import fetch_universe, DEFAULT_UNIVERSE
from .engine import BacktestEngine
from .metrics import calculate_metrics
from .momentum_strategy import MomentumStrategy


def parse_args():
    p = argparse.ArgumentParser(description="Momentum-only backtest")
    p.add_argument("--universe", default=",".join(DEFAULT_UNIVERSE[:5]))
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--capital", type=float, default=10_000.0)
    p.add_argument("--position-size", type=float, default=0.10)
    p.add_argument("--max-position", type=float, default=0.20)
    p.add_argument("--stop-loss", type=float, default=0.02)
    p.add_argument("--take-profit", type=float, default=0.04)
    p.add_argument("--no-shorts", action="store_true")
    p.add_argument("--label", default=None)
    p.add_argument("--output-dir", default="reports/momentum")
    p.add_argument("--force-refresh", action="store_true")
    return p.parse_args()


async def run_split(
    sliced: dict[str, pd.DataFrame],
    *,
    initial_capital: float,
    position_size: float,
    max_position: float,
    stop_loss: float,
    take_profit: float,
    allow_shorts: bool,
    label: str,
) -> tuple:
    """Run a single backtest split using the engine's run() loop."""
    strategy = MomentumStrategy(
        symbols=list(sliced.keys()),
        lookback_bars=100,
        position_size=position_size,
        allow_shorts=allow_shorts,
    )
    engine = BacktestEngine(
        universe=sliced,
        strategy=strategy,
        initial_capital=initial_capital,
        position_size_pct=position_size,
        max_position_pct=max_position,
        stop_loss_pct=stop_loss,
        take_profit_pct=take_profit,
        run_label=label,
    )
    result = await engine.run()
    metrics = calculate_metrics(result.equity_curve, result.trades, initial_capital)
    return result, metrics


async def main():
    args = parse_args()
    label = args.label or f"momentum_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip() for s in args.universe.split(",") if s.strip()]
    print(f"=== Momentum-Only Backtest ===")
    print(f"Label: {label}, Universe: {symbols}, Days: {args.days}")
    print(f"SL={args.stop_loss*100:.1f}% TP={args.take_profit*100:.1f}%, "
          f"pos_size={args.position_size*100:.0f}%, "
          f"shorts={'NO' if args.no_shorts else 'YES'}")
    print()

    universe = await fetch_universe(
        symbols, interval="1h", lookback_days=args.days,
        force_refresh=args.force_refresh,
    )
    if not universe:
        print("ERROR: no data")
        return 1
    print(f"Loaded {len(universe)} symbols")
    for sym in universe:
        print(f"  {sym}: {len(universe[sym])} bars")
    print()

    all_starts = [df.index.min() for df in universe.values()]
    all_ends = [df.index.max() for df in universe.values()]
    common_start = max(all_starts)
    common_end = min(all_ends)
    total_span = common_end - common_start
    train_end = common_start + total_span * 0.50
    val_end = train_end + total_span * 0.25
    splits = [
        ("train", common_start, train_end),
        ("val", train_end, val_end),
        ("test", val_end, common_end),
    ]

    results: dict = {}
    for split_name, start, end in splits:
        print(f"--- {split_name.upper()} ({start.date()} → {end.date()}) ---")
        sliced = {s: df.loc[start:end] for s, df in universe.items()}
        sliced = {s: d for s, d in sliced.items() if len(d) > 50}
        if not sliced:
            print("  no data after slicing")
            continue
        result, metrics = await run_split(
            sliced,
            initial_capital=args.capital,
            position_size=args.position_size,
            max_position=args.max_position,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            allow_shorts=not args.no_shorts,
            label=f"{label}_{split_name}",
        )
        results[split_name] = metrics.to_dict()
        print(f"  Trades: {metrics.num_trades}")
        print(f"  Win rate: {metrics.win_rate:.1%}")
        print(f"  Return: {metrics.total_return:+.2%}")
        print(f"  Max DD: {metrics.max_drawdown:.2%}")
        print(f"  PF: {metrics.profit_factor:.2f}")
        print(f"  Final: ${metrics.final_equity:,.2f}")

        result.equity_curve.to_csv(output_dir / f"{label}_{split_name}_equity.csv")
        result.trades_df().to_csv(output_dir / f"{label}_{split_name}_trades.csv", index=False)

    (output_dir / f"{label}_metrics.json").write_text(
        json.dumps(results, indent=2, default=str)
    )
    print(f"\nResults saved to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
