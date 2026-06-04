"""CLI runner for the backtest harness.

Usage:
    venv/Scripts/python.exe -m src.backtest.runner \\
        --universe BTC,ETH,SOL,ARB,AVAX,DOGE,LINK,OP \\
        --days 60 \\
        --capital 10000 \\
        --train-pct 50 --val-pct 25 --test-pct 25 \\
        --min-confluence 0.35 \\
        --output-dir reports

Saves:
  reports/{label}_metrics.json   - all performance metrics
  reports/{label}_equity.csv     - bar-by-bar equity curve
  reports/{label}_trades.csv     - trade-by-trade log
  reports/{label}_summary.md     - human-readable summary
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..utils.logging import get_logger
from .data_fetcher import fetch_universe, DEFAULT_UNIVERSE
from .engine import BacktestEngine
from .metrics import calculate_metrics
from .strategy import BacktestStrategy

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HLBot backtest harness")
    p.add_argument("--universe", default=",".join(DEFAULT_UNIVERSE),
                   help="Comma-separated symbols. Default: top Hyperliquid perps.")
    p.add_argument("--days", type=int, default=60, help="Lookback in days")
    p.add_argument("--interval", default="1h", help="Candle interval")
    p.add_argument("--capital", type=float, default=10_000.0, help="Initial capital USD")
    p.add_argument("--position-size", type=float, default=0.10,
                   help="Fraction of equity per entry (fallback)")
    p.add_argument("--max-position", type=float, default=0.20)
    p.add_argument("--stop-loss", type=float, default=0.02)
    p.add_argument("--take-profit", type=float, default=0.04)
    p.add_argument("--min-confluence", type=float, default=0.35)
    p.add_argument("--train-pct", type=float, default=50.0)
    p.add_argument("--val-pct", type=float, default=25.0)
    p.add_argument("--test-pct", type=float, default=25.0)
    p.add_argument("--output-dir", default="reports", help="Where to save results")
    p.add_argument("--label", default=None, help="Run label (default: timestamp)")
    p.add_argument("--force-refresh", action="store_true", help="Re-fetch candles")
    return p.parse_args()


def _write_summary_md(
    metrics, split_label: str, output_path: Path, config: dict
) -> None:
    lines: list[str] = []
    lines.append(f"# Backtest Summary — {split_label}")
    lines.append("")
    lines.append(f"**Generated:** {datetime.utcnow().isoformat()}Z")
    lines.append("")
    lines.append("## Configuration")
    for k, v in config.items():
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("## Performance")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Initial capital | ${config['initial_capital']:,.2f} |")
    lines.append(f"| Final equity | ${metrics.final_equity:,.2f} |")
    lines.append(f"| Total return | {metrics.total_return:.2%} |")
    lines.append(f"| Annualized return | {metrics.annual_return:.2%} |")
    lines.append(f"| Annualized volatility | {metrics.annual_volatility:.2%} |")
    lines.append(f"| Sharpe ratio | {metrics.sharpe_ratio:.2f} |")
    lines.append(f"| Sortino ratio | {metrics.sortino_ratio:.2f} |")
    lines.append(f"| Calmar ratio | {metrics.calmar_ratio:.2f} |")
    lines.append(f"| Max drawdown | {metrics.max_drawdown:.2%} |")
    lines.append(f"| Max DD duration (bars) | {metrics.max_drawdown_duration_bars} |")
    lines.append("")
    lines.append("## Trade Stats")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Number of trades | {metrics.num_trades} |")
    lines.append(f"| Wins / Losses | {metrics.num_wins} / {int(metrics.num_losses)} |")
    lines.append(f"| Win rate | {metrics.win_rate:.2%} |")
    lines.append(f"| Avg win | ${metrics.avg_win:,.2f} |")
    lines.append(f"| Avg loss | ${metrics.avg_loss:,.2f} |")
    lines.append(f"| Profit factor | {metrics.profit_factor:.2f} |")
    lines.append(f"| Avg bars held | {metrics.avg_bars_held:.1f} |")
    lines.append(f"| Avg exposure | {metrics.exposure_pct_avg:.2%} |")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    # Auto commentary
    import math
    if math.isnan(metrics.sharpe_ratio):
        verdict = "⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead."
    elif metrics.sharpe_ratio < 0:
        verdict = "❌ **Negative risk-adjusted returns** — strategy loses money on a risk-adjusted basis."
    elif metrics.sharpe_ratio < 0.5:
        verdict = "⚠️ **Marginal** — Sharpe below 0.5, strategy is barely better than noise."
    elif metrics.sharpe_ratio < 1.0:
        verdict = "🟡 **Promising but unproven** — Sharpe 0.5-1.0, worth further investigation."
    elif metrics.sharpe_ratio < 2.0:
        verdict = "🟢 **Solid** — Sharpe 1.0-2.0, real edge."
    else:
        verdict = "🌟 **Exceptional** — Sharpe > 2.0, likely overfit; verify out-of-sample."
    lines.append(verdict)
    lines.append("")
    if metrics.num_trades < 30:
        lines.append("⚠️ Trade count is low (<30). Sharpe is statistically unreliable. "
                     "Run on a longer period or relax confluence threshold.")
    if metrics.profit_factor < 1.0:
        lines.append("⚠️ Profit factor < 1.0 — gross losses exceed gross wins.")
    if metrics.max_drawdown < -0.20:
        lines.append("⚠️ Max drawdown > 20% — risk parameters too loose.")
    output_path.write_text("\n".join(lines))


async def run_backtest(
    universe: dict[str, pd.DataFrame],
    *,
    initial_capital: float = 10_000.0,
    position_size_pct: float = 0.10,
    max_position_pct: float = 0.20,
    stop_loss_pct: float = 0.02,
    take_profit_pct: float = 0.04,
    min_confluence: float = 0.35,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    label: str = "backtest",
) -> tuple:
    """Run a backtest over [start, end]. Returns (result, metrics)."""
    # Slice universe to the requested period.
    sliced = {
        sym: df.loc[start:end] if start is not None or end is not None else df
        for sym, df in universe.items()
    }
    # Drop symbols that lost all data after slicing
    sliced = {s: d for s, d in sliced.items() if len(d) > 50}

    strategy = BacktestStrategy(
        symbols=list(sliced.keys()),
        lookback_bars=100,
        min_confluence=min_confluence,
    )
    engine = BacktestEngine(
        universe=sliced,
        strategy=strategy,
        initial_capital=initial_capital,
        position_size_pct=position_size_pct,
        max_position_pct=max_position_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        run_label=label,
    )
    result = await engine.run()
    metrics = calculate_metrics(
        result.equity_curve,
        result.trades,
        initial_capital=initial_capital,
    )
    return result, metrics


async def main() -> int:
    args = parse_args()
    label = args.label or datetime.utcnow().strftime("backtest_%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    universe_symbols = [s.strip() for s in args.universe.split(",") if s.strip()]

    print(f"=== HLBot Backtest ===")
    print(f"Label:       {label}")
    print(f"Universe:    {universe_symbols}")
    print(f"Days:        {args.days}")
    print(f"Capital:     ${args.capital:,.2f}")
    print(f"Confluence:  {args.min_confluence}")
    print()

    # Fetch
    print("Fetching historical candles...")
    universe = await fetch_universe(
        universe_symbols,
        interval=args.interval,
        lookback_days=args.days,
        force_refresh=args.force_refresh,
    )
    if not universe:
        print("ERROR: no data fetched. Aborting.")
        return 1

    # Find common time range
    all_starts = [df.index.min() for df in universe.values()]
    all_ends = [df.index.max() for df in universe.values()]
    common_start = max(all_starts)
    common_end = min(all_ends)
    print(f"Common range: {common_start} → {common_end}")
    print(f"Symbols with data: {list(universe.keys())}")
    print()

    # Compute split boundaries
    total_span = common_end - common_start
    train_end = common_start + total_span * (args.train_pct / 100.0)
    val_end = train_end + total_span * (args.val_pct / 100.0)
    print(f"Splits:")
    print(f"  Train: {common_start} → {train_end}")
    print(f"  Val:   {train_end} → {val_end}")
    print(f"  Test:  {val_end} → {common_end}")
    print()

    # Run all three splits
    splits = [
        ("train", common_start, train_end),
        ("val", train_end, val_end),
        ("test", val_end, common_end),
    ]

    aggregate_metrics = {}
    for split_name, start, end in splits:
        print(f"--- {split_name.upper()} split ---")
        result, metrics = await run_backtest(
            universe,
            initial_capital=args.capital,
            position_size_pct=args.position_size,
            max_position_pct=args.max_position,
            stop_loss_pct=args.stop_loss,
            take_profit_pct=args.take_profit,
            min_confluence=args.min_confluence,
            start=start,
            end=end,
            label=f"{label}_{split_name}",
        )
        print(f"  Trades:      {metrics.num_trades}")
        print(f"  Win rate:    {metrics.win_rate:.2%}")
        print(f"  Total ret:   {metrics.total_return:.2%}")
        print(f"  Sharpe:      {metrics.sharpe_ratio:.2f}")
        print(f"  Max DD:      {metrics.max_drawdown:.2%}")
        print(f"  Profit fact: {metrics.profit_factor:.2f}")
        print(f"  Final eq:    ${metrics.final_equity:,.2f}")
        print()

        # Save outputs
        split_dir = output_dir / label
        split_dir.mkdir(parents=True, exist_ok=True)
        result.equity_curve.to_csv(split_dir / f"{split_name}_equity.csv")
        result.trades_df().to_csv(split_dir / f"{split_name}_trades.csv", index=False)
        (split_dir / f"{split_name}_metrics.json").write_text(
            json.dumps(metrics.to_dict(), indent=2, default=str)
        )
        _write_summary_md(
            metrics, f"{label}/{split_name}",
            split_dir / f"{split_name}_summary.md",
            config={
                "initial_capital": args.capital,
                "position_size_pct": args.position_size,
                "max_position_pct": args.max_position,
                "stop_loss_pct": args.stop_loss,
                "take_profit_pct": args.take_profit,
                "min_confluence": args.min_confluence,
                "split": split_name,
                "period": f"{start} → {end}",
            },
        )
        aggregate_metrics[split_name] = metrics.to_dict()

    # Save aggregate
    (output_dir / label / "all_splits.json").write_text(
        json.dumps(aggregate_metrics, indent=2, default=str)
    )
    print(f"=== DONE ===")
    print(f"Results saved to {output_dir / label}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
