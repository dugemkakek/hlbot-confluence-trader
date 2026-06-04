"""Strategy calibration sweep.

Runs three parameter sweeps over the historical data and outputs a
sensitivity matrix. Designed to find whether ANY parameter region
produces consistent positive returns across train/val/test.

Sweeps:
  1. Confluence threshold:  [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
  2. SL/TP combinations:   [(0.01,0.02), (0.015,0.03), (0.02,0.04), (0.03,0.06)]
  3. Universe size:        [3, 5, 8] (top-N by volume)

Output:
  reports/calibration/sensitivity_matrix.md
  reports/calibration/sweep_results.csv

Time:  ~6-10 minutes total on 30 days of 8-symbol data.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .data_fetcher import fetch_universe
from .engine import BacktestEngine
from .metrics import calculate_metrics
from .strategy import BacktestStrategy

# Default universe (top 8 by volume on Hyperliquid)
DEFAULT_UNIVERSE = ["BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "LINK", "OP"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HLBot strategy calibration")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--capital", type=float, default=10_000.0)
    p.add_argument("--output-dir", default="reports/calibration")
    p.add_argument("--force-refresh", action="store_true")
    return p.parse_args()


async def run_single(
    universe: dict[str, pd.DataFrame],
    *,
    min_confluence: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    initial_capital: float,
    label: str = "single",
) -> dict[str, dict[str, Any]]:
    """Run train/val/test for a single parameter set. Returns per-split
    metrics dict.
    """
    # Common date range
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
    out: dict[str, dict[str, Any]] = {}
    for split_name, start, end in splits:
        sliced = {
            sym: df.loc[start:end]
            for sym, df in universe.items()
        }
        sliced = {s: d for s, d in sliced.items() if len(d) > 50}
        if not sliced:
            out[split_name] = {"error": "no data after slicing"}
            continue
        strategy = BacktestStrategy(
            symbols=list(sliced.keys()),
            lookback_bars=100,
            min_confluence=min_confluence,
            top_n_per_bar=3,
        )
        engine = BacktestEngine(
            universe=sliced,
            strategy=strategy,
            initial_capital=initial_capital,
            position_size_pct=0.10,
            max_position_pct=0.20,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            run_label=label,
        )
        result = await engine.run()
        metrics = calculate_metrics(
            result.equity_curve, result.trades, initial_capital
        )
        out[split_name] = metrics.to_dict()
    return out


def _format_cell(metrics: dict[str, Any] | None) -> str:
    """Format a metrics dict as a single table cell."""
    if not metrics or "error" in metrics:
        return "—"
    ret = metrics.get("total_return", 0.0) * 100
    pf = metrics.get("profit_factor", 0.0)
    dd = metrics.get("max_drawdown", 0.0) * 100
    n = metrics.get("num_trades", 0)
    return f"{ret:+.1f}% / PF {pf:.2f} / DD {dd:.0f}% / n={n}"


async def threshold_sweep(
    universe: dict[str, pd.DataFrame],
    *,
    initial_capital: float,
) -> list[dict[str, Any]]:
    """Sweep confluence threshold."""
    thresholds = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
    results: list[dict[str, Any]] = []
    print(f"\n=== Sweep 1/3: Confluence Threshold ===")
    for i, t in enumerate(thresholds, 1):
        t0 = time.time()
        print(f"  [{i}/{len(thresholds)}] threshold={t:.2f} ...", end=" ", flush=True)
        per_split = await run_single(
            universe,
            min_confluence=t,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            initial_capital=initial_capital,
            label=f"thr_{t:.2f}",
        )
        elapsed = time.time() - t0
        print(f"({elapsed:.1f}s)")
        results.append({
            "param": "threshold",
            "value": t,
            **{f"{k}_{m}": v for k, metrics in per_split.items() for m, v in metrics.items() if m in ("total_return","win_rate","profit_factor","max_drawdown","num_trades","final_equity")},
        })
    return results


async def sl_tp_sweep(
    universe: dict[str, pd.DataFrame],
    *,
    initial_capital: float,
) -> list[dict[str, Any]]:
    """Sweep SL/TP combinations. All 1:2 reward:risk."""
    combos = [
        (0.010, 0.020),
        (0.015, 0.030),
        (0.020, 0.040),
        (0.030, 0.060),
    ]
    results: list[dict[str, Any]] = []
    print(f"\n=== Sweep 2/3: SL/TP Combinations ===")
    for i, (sl, tp) in enumerate(combos, 1):
        t0 = time.time()
        print(f"  [{i}/{len(combos)}] SL={sl*100:.1f}% / TP={tp*100:.1f}% ...", end=" ", flush=True)
        per_split = await run_single(
            universe,
            min_confluence=0.20,
            stop_loss_pct=sl,
            take_profit_pct=tp,
            initial_capital=initial_capital,
            label=f"sltp_{sl*100:.0f}_{tp*100:.0f}",
        )
        elapsed = time.time() - t0
        print(f"({elapsed:.1f}s)")
        results.append({
            "param": "sl_tp",
            "value": f"{sl*100:.0f}/{tp*100:.0f}",
            **{f"{k}_{m}": v for k, metrics in per_split.items() for m, v in metrics.items() if m in ("total_return","win_rate","profit_factor","max_drawdown","num_trades","final_equity")},
        })
    return results


async def universe_sweep(
    universe: dict[str, pd.DataFrame],
    *,
    initial_capital: float,
) -> list[dict[str, Any]]:
    """Sweep universe size (top-N by stored order)."""
    sizes = [3, 5, 8]
    all_symbols = list(universe.keys())
    results: list[dict[str, Any]] = []
    print(f"\n=== Sweep 3/3: Universe Size ===")
    for i, n in enumerate(sizes, 1):
        t0 = time.time()
        symbols = all_symbols[:n]
        print(f"  [{i}/{len(sizes)}] top {n} symbols ({symbols}) ...", end=" ", flush=True)
        sliced = {s: universe[s] for s in symbols}
        per_split = await run_single(
            sliced,
            min_confluence=0.20,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            initial_capital=initial_capital,
            label=f"univ_{n}",
        )
        elapsed = time.time() - t0
        print(f"({elapsed:.1f}s)")
        results.append({
            "param": "universe_size",
            "value": n,
            "symbols": symbols,
            **{f"{k}_{m}": v for k, metrics in per_split.items() for m, v in metrics.items() if m in ("total_return","win_rate","profit_factor","max_drawdown","num_trades","final_equity")},
        })
    return results


def write_sensitivity_matrix(
    threshold_results: list[dict[str, Any]],
    sl_tp_results: list[dict[str, Any]],
    universe_results: list[dict[str, Any]],
    output_path: Path,
) -> None:
    """Render the sensitivity matrix as a markdown table."""
    lines: list[str] = []
    lines.append("# Strategy Calibration — Sensitivity Matrix")
    lines.append("")
    lines.append(f"**Generated:** {datetime.utcnow().isoformat()}Z")
    lines.append("")
    lines.append("**Setup:** 30 days of 1h candles, 8 symbols, $10k capital, train/val/test = 50/25/25.")
    lines.append("")
    lines.append("Cells show: `return / profit_factor / max_DD / num_trades`")
    lines.append("")

    # Threshold sweep table
    lines.append("## Sweep 1: Confluence Threshold")
    lines.append("")
    lines.append("| Threshold | Train | Val | Test | Verdict |")
    lines.append("|---|---|---|---|---|")
    for r in threshold_results:
        t = r["value"]
        train = _format_cell({
            "total_return": r.get("train_total_return", 0),
            "profit_factor": r.get("train_profit_factor", 0),
            "max_drawdown": r.get("train_max_drawdown", 0),
            "num_trades": r.get("train_num_trades", 0),
        })
        val = _format_cell({
            "total_return": r.get("val_total_return", 0),
            "profit_factor": r.get("val_profit_factor", 0),
            "max_drawdown": r.get("val_max_drawdown", 0),
            "num_trades": r.get("val_num_trades", 0),
        })
        test = _format_cell({
            "total_return": r.get("test_total_return", 0),
            "profit_factor": r.get("test_profit_factor", 0),
            "max_drawdown": r.get("test_max_drawdown", 0),
            "num_trades": r.get("test_num_trades", 0),
        })
        train_ret = r.get("train_total_return", 0)
        test_ret = r.get("test_total_return", 0)
        verdict = "🟢" if (train_ret > 0 and test_ret > 0) else ("🟡" if max(train_ret, test_ret) > 0 else "❌")
        lines.append(f"| {t:.2f} | {train} | {val} | {test} | {verdict} |")
    lines.append("")

    # SL/TP sweep table
    lines.append("## Sweep 2: SL / TP (1:2 reward:risk ratio)")
    lines.append("")
    lines.append("| SL / TP | Train | Val | Test | Verdict |")
    lines.append("|---|---|---|---|---|")
    for r in sl_tp_results:
        sl_tp = r["value"]
        train = _format_cell({
            "total_return": r.get("train_total_return", 0),
            "profit_factor": r.get("train_profit_factor", 0),
            "max_drawdown": r.get("train_max_drawdown", 0),
            "num_trades": r.get("train_num_trades", 0),
        })
        val = _format_cell({
            "total_return": r.get("val_total_return", 0),
            "profit_factor": r.get("val_profit_factor", 0),
            "max_drawdown": r.get("val_max_drawdown", 0),
            "num_trades": r.get("val_num_trades", 0),
        })
        test = _format_cell({
            "total_return": r.get("test_total_return", 0),
            "profit_factor": r.get("test_profit_factor", 0),
            "max_drawdown": r.get("test_max_drawdown", 0),
            "num_trades": r.get("test_num_trades", 0),
        })
        train_ret = r.get("train_total_return", 0)
        test_ret = r.get("test_total_return", 0)
        verdict = "🟢" if (train_ret > 0 and test_ret > 0) else ("🟡" if max(train_ret, test_ret) > 0 else "❌")
        lines.append(f"| {sl_tp}% | {train} | {val} | {test} | {verdict} |")
    lines.append("")

    # Universe size table
    lines.append("## Sweep 3: Universe Size")
    lines.append("")
    lines.append("| Symbols | Train | Val | Test | Verdict |")
    lines.append("|---|---|---|---|---|")
    for r in universe_results:
        n = r["value"]
        symbols = r.get("symbols", [])
        train = _format_cell({
            "total_return": r.get("train_total_return", 0),
            "profit_factor": r.get("train_profit_factor", 0),
            "max_drawdown": r.get("train_max_drawdown", 0),
            "num_trades": r.get("train_num_trades", 0),
        })
        val = _format_cell({
            "total_return": r.get("val_total_return", 0),
            "profit_factor": r.get("val_profit_factor", 0),
            "max_drawdown": r.get("val_max_drawdown", 0),
            "num_trades": r.get("val_num_trades", 0),
        })
        test = _format_cell({
            "total_return": r.get("test_total_return", 0),
            "profit_factor": r.get("test_profit_factor", 0),
            "max_drawdown": r.get("test_max_drawdown", 0),
            "num_trades": r.get("test_num_trades", 0),
        })
        train_ret = r.get("train_total_return", 0)
        test_ret = r.get("test_total_return", 0)
        verdict = "🟢" if (train_ret > 0 and test_ret > 0) else ("🟡" if max(train_ret, test_ret) > 0 else "❌")
        sym_str = ", ".join(symbols[:3]) + ("..." if len(symbols) > 3 else "")
        lines.append(f"| {n} ({sym_str}) | {train} | {val} | {test} | {verdict} |")
    lines.append("")

    # Verdict
    lines.append("## Verdict")
    lines.append("")
    lines.append("🟢 = profitable in both train and test (consistent edge)")
    lines.append("🟡 = profitable in train OR test but not both (mixed)")
    lines.append("❌ = unprofitable in both (no edge)")
    lines.append("")

    output_path.write_text("\n".join(lines))


async def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== HLBot Calibration Sweep ===")
    print(f"Days: {args.days}, Capital: ${args.capital:,.2f}")
    print(f"Output: {output_dir}")
    print()

    # Load data
    print("Loading historical data...")
    universe = await fetch_universe(
        DEFAULT_UNIVERSE,
        interval="1h",
        lookback_days=args.days,
        force_refresh=args.force_refresh,
    )
    if not universe:
        print("ERROR: no data fetched")
        return 1
    print(f"Loaded {len(universe)} symbols")
    for sym in universe:
        print(f"  {sym}: {len(universe[sym])} bars")
    print()

    t_start = time.time()

    # Sweep 1: threshold
    threshold_results = await threshold_sweep(universe, initial_capital=args.capital)
    # Sweep 2: SL/TP
    sl_tp_results = await sl_tp_sweep(universe, initial_capital=args.capital)
    # Sweep 3: universe
    universe_results = await universe_sweep(universe, initial_capital=args.capital)

    elapsed = time.time() - t_start
    print(f"\n=== Sweep complete in {elapsed:.0f}s ===")

    # Save CSV
    all_results = threshold_results + sl_tp_results + universe_results
    csv_path = output_dir / "sweep_results.csv"
    if all_results:
        keys = sorted({k for r in all_results for k in r.keys()})
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in all_results:
                w.writerow(r)
        print(f"CSV: {csv_path}")

    # Save markdown
    md_path = output_dir / "sensitivity_matrix.md"
    write_sensitivity_matrix(
        threshold_results, sl_tp_results, universe_results, md_path
    )
    print(f"Matrix: {md_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
