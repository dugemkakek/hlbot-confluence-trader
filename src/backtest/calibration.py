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
  reports/calibration/run_<timestamp>.log   (full stdout transcript)

Time:  ~6-10 minutes total on 30 days of 8-symbol data.

# v0.2.0 (2026-06-06): added a 30s heartbeat + tee-to-file logging
# because the previous sweep run on 2026-06-05 hung silently for
# 24+ hours (PID 33404) and we had no way to tell which config
# was stuck. Heartbeat prints total elapsed + current sweep/config
# every 30s; tee writes everything to reports/calibration/run_<ts>.log
# so a future hang leaves a paper trail.
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


# ─────────────────────────────────────────────────────────────────────────────
# v0.2.0: tee-to-file + heartbeat state
# ─────────────────────────────────────────────────────────────────────────────

# Mutable state read by the heartbeat task. Updated by each sweep/config
# entry point. Module-level so the asyncio heartbeat task spawned in main()
# can read it without threading it through every function signature.
_RUN_STATE: dict[str, Any] = {
    "t_start": 0.0,
    "sweep_idx": 0,
    "sweep_total": 3,
    "sweep_name": "(init)",
    "config_idx": 0,
    "config_total": 0,
    "config_label": "(init)",
    "split_name": "(init)",
    "log_path": None,
}


def _tee(line: str) -> None:
    """Print to stdout AND append to the run log file (if open).

    Flushes both so a hang leaves a complete transcript on disk.
    """
    print(line, flush=True)
    log_path = _RUN_STATE.get("log_path")
    if log_path is not None:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # Never let the logger kill the sweep.
            pass


def _format_elapsed(seconds: float) -> str:
    """Format seconds as Hh Mm Ss."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


async def _heartbeat_loop(interval_s: float = 30.0) -> None:
    """Print a one-line heartbeat every `interval_s` while the sweep runs.

    Shows total elapsed + current sweep/config. If a single config hangs,
    the heartbeat keeps ticking — so a stuck run is visible from outside
    (the user can see "stuck on config 4/13 for 18m").

    The task is created in main() and cancelled in the `finally` of
    main()'s try block.
    """
    t0 = _RUN_STATE["t_start"]
    try:
        while True:
            await asyncio.sleep(interval_s)
            elapsed = time.time() - t0
            _tee(
                f"  [heartbeat] elapsed={_format_elapsed(elapsed)} "
                f"sweep={_RUN_STATE['sweep_idx']}/{_RUN_STATE['sweep_total']} "
                f"({_RUN_STATE['sweep_name']}) "
                f"config={_RUN_STATE['config_idx']}/{_RUN_STATE['config_total']} "
                f"[{_RUN_STATE['config_label']}] "
                f"split={_RUN_STATE['split_name']}"
            )
    except asyncio.CancelledError:
        # Expected on sweep completion. Final heartbeat was the
        # last per-config line; no need to print a goodbye.
        raise


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HLBot strategy calibration")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--capital", type=float, default=10_000.0)
    p.add_argument("--output-dir", default="reports/calibration")
    p.add_argument("--force-refresh", action="store_true")
    # v0.2.0 (2026-06-06): when True, the ranker override path is
    # bypassed and we see what the decision engine alone produces.
    # Useful to isolate whether the override is rescuing the
    # strategy or making things worse. Default False (override
    # active, mirrors production).
    p.add_argument("--no-override", action="store_true", help="Bypass the ranker override path; see decision engine alone")
    return p.parse_args()


async def run_single(
    universe: dict[str, pd.DataFrame],
    *,
    min_confluence: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    initial_capital: float,
    label: str = "single",
    no_override: bool = False,
) -> dict[str, dict[str, Any]]:
    """Run train/val/test for a single parameter set. Returns per-split
    metrics dict.

    v0.2.0: tracks per-split progress in the heartbeat state so a
    stuck train/val/test is visible. Previously a single hanging
    split would freeze the whole sweep silently.
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
        _RUN_STATE["split_name"] = split_name
        split_t0 = time.time()
        _tee(f"    +-- split={split_name} start ({(end-start).total_seconds()/3600:.1f}h span)")
        sliced = {
            sym: df.loc[start:end]
            for sym, df in universe.items()
        }
        sliced = {s: d for s, d in sliced.items() if len(d) > 50}
        if not sliced:
            out[split_name] = {"error": "no data after slicing"}
            _tee(f"    +-- split={split_name} SKIP (no data) {time.time()-split_t0:.1f}s")
            continue
        strategy = BacktestStrategy(
            symbols=list(sliced.keys()),
            lookback_bars=100,
            min_confluence=min_confluence,
            top_n_per_bar=3,
            no_override=no_override,
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
        _tee(
            f"    +-- split={split_name} done "
            f"trades={metrics.num_trades} ret={metrics.total_return*100:+.1f}% "
            f"({time.time()-split_t0:.1f}s)"
        )
    _RUN_STATE["split_name"] = "(between configs)"
    return out


def _format_cell(metrics: dict[str, Any] | None) -> str:
    """Format a metrics dict as a single table cell."""
    if not metrics or "error" in metrics:
        return "-"
    ret = metrics.get("total_return", 0.0) * 100
    pf = metrics.get("profit_factor", 0.0)
    dd = metrics.get("max_drawdown", 0.0) * 100
    n = metrics.get("num_trades", 0)
    return f"{ret:+.1f}% / PF {pf:.2f} / DD {dd:.0f}% / n={n}"


async def threshold_sweep(
    universe: dict[str, pd.DataFrame],
    *,
    initial_capital: float,
    no_override: bool = False,
) -> list[dict[str, Any]]:
    """Sweep confluence threshold.

    v0.2.0 (2026-06-06): range changed from [0.10, 0.35] to
    [0.40, 0.70]. The v0.1.0 range sat entirely below the
    OVERRIDE_MIN_CONFLUENCE floor of 0.50 (production safety,
    added in the bias fix), so every config clamped to 0.50 and
    produced identical results. The new range actually moves
    the threshold so we can find the right floor for this regime.
    """
    thresholds = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    results: list[dict[str, Any]] = []
    _RUN_STATE["sweep_idx"] = 1
    _RUN_STATE["sweep_name"] = "Confluence Threshold"
    _RUN_STATE["config_total"] = len(thresholds)
    _tee(f"\n=== Sweep 1/3: Confluence Threshold ===")
    for i, t in enumerate(thresholds, 1):
        t0 = time.time()
        _RUN_STATE["config_idx"] = i
        _RUN_STATE["config_label"] = f"threshold={t:.2f}"
        _tee(f"  [{i}/{len(thresholds)}] threshold={t:.2f} ...")
        per_split = await run_single(
            universe,
            min_confluence=t,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            initial_capital=initial_capital,
            no_override=no_override,
            label=f"thr_{t:.2f}",
        )
        elapsed = time.time() - t0
        _tee(f"  [{i}/{len(thresholds)}] threshold={t:.2f} done ({elapsed:.1f}s)")
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
    no_override: bool = False,
) -> list[dict[str, Any]]:
    """Sweep SL/TP combinations. All 1:2 reward:risk."""
    combos = [
        (0.010, 0.020),
        (0.015, 0.030),
        (0.020, 0.040),
        (0.030, 0.060),
    ]
    results: list[dict[str, Any]] = []
    _RUN_STATE["sweep_idx"] = 2
    _RUN_STATE["sweep_name"] = "SL/TP"
    _RUN_STATE["config_total"] = len(combos)
    _tee(f"\n=== Sweep 2/3: SL/TP Combinations ===")
    for i, (sl, tp) in enumerate(combos, 1):
        t0 = time.time()
        _RUN_STATE["config_idx"] = i
        _RUN_STATE["config_label"] = f"SL={sl*100:.1f}%/TP={tp*100:.1f}%"
        _tee(f"  [{i}/{len(combos)}] SL={sl*100:.1f}% / TP={tp*100:.1f}% ...")
        per_split = await run_single(
            universe,
            min_confluence=0.20,
            stop_loss_pct=sl,
            take_profit_pct=tp,
            initial_capital=initial_capital,
            no_override=no_override,
            label=f"sltp_{sl*100:.0f}_{tp*100:.0f}",
        )
        elapsed = time.time() - t0
        _tee(f"  [{i}/{len(combos)}] SL={sl*100:.1f}% / TP={tp*100:.1f}% done ({elapsed:.1f}s)")
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
    no_override: bool = False,
) -> list[dict[str, Any]]:
    """Sweep universe size (top-N by stored order)."""
    sizes = [3, 5, 8]
    all_symbols = list(universe.keys())
    results: list[dict[str, Any]] = []
    _RUN_STATE["sweep_idx"] = 3
    _RUN_STATE["sweep_name"] = "Universe Size"
    _RUN_STATE["config_total"] = len(sizes)
    _tee(f"\n=== Sweep 3/3: Universe Size ===")
    for i, n in enumerate(sizes, 1):
        t0 = time.time()
        symbols = all_symbols[:n]
        _RUN_STATE["config_idx"] = i
        _RUN_STATE["config_label"] = f"top {n} symbols ({symbols})"
        _tee(f"  [{i}/{len(sizes)}] top {n} symbols ({symbols}) ...")
        sliced = {s: universe[s] for s in symbols}
        per_split = await run_single(
            sliced,
            min_confluence=0.20,
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            initial_capital=initial_capital,
            no_override=no_override,
            label=f"univ_{n}",
        )
        elapsed = time.time() - t0
        _tee(f"  [{i}/{len(sizes)}] top {n} symbols done ({elapsed:.1f}s)")
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
        verdict = "GREEN" if (train_ret > 0 and test_ret > 0) else ("YELLOW" if max(train_ret, test_ret) > 0 else "RED")
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
        verdict = "GREEN" if (train_ret > 0 and test_ret > 0) else ("YELLOW" if max(train_ret, test_ret) > 0 else "RED")
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
        verdict = "GREEN" if (train_ret > 0 and test_ret > 0) else ("YELLOW" if max(train_ret, test_ret) > 0 else "RED")
        sym_str = ", ".join(symbols[:3]) + ("..." if len(symbols) > 3 else "")
        lines.append(f"| {n} ({sym_str}) | {train} | {val} | {test} | {verdict} |")
    lines.append("")

    # Verdict
    lines.append("## Verdict")
    lines.append("")
    lines.append("GREEN = profitable in both train and test (consistent edge)")
    lines.append("YELLOW = profitable in train OR test but not both (mixed)")
    lines.append("RED = unprofitable in both (no edge)")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # v0.2.0: Windows defaults stdout to cp1252 which rejects
    # anything outside Latin-1. Force utf-8 so progress strings
    # can use box-drawing or emoji in the future. No-op on
    # platforms whose stdout is already utf-8.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    # v0.2.0: open the run log file. All subsequent _tee() calls write
    # here in addition to stdout, so a hang leaves a paper trail.
    run_ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    log_path = output_dir / f"run_{run_ts}.log"
    _RUN_STATE["log_path"] = log_path
    # Truncate the file (start fresh)
    log_path.write_text("", encoding="utf-8")

    _tee(f"=== HLBot Calibration Sweep ===")
    _tee(f"Days: {args.days}, Capital: ${args.capital:,.2f}")
    _tee(f"Output: {output_dir}")
    _tee(f"Run log: {log_path}")
    _tee(f"PID: {__import__('os').getpid()}")
    _tee(f"Override path: {'DISABLED (--no-override)' if args.no_override else 'ENABLED (production parity)'}")
    _tee("")

    heartbeat_task: asyncio.Task | None = None
    try:
        # Load data
        _tee("Loading historical data...")
        universe = await fetch_universe(
            DEFAULT_UNIVERSE,
            interval="1h",
            lookback_days=args.days,
            force_refresh=args.force_refresh,
        )
        if not universe:
            _tee("ERROR: no data fetched")
            return 1
        _tee(f"Loaded {len(universe)} symbols")
        for sym in universe:
            _tee(f"  {sym}: {len(universe[sym])} bars")
        _tee("")

        # Start heartbeat task. Ticks every 30s while the sweeps run.
        _RUN_STATE["t_start"] = time.time()
        heartbeat_task = asyncio.create_task(_heartbeat_loop(interval_s=30.0))

        # Sweep 1: threshold
        threshold_results = await threshold_sweep(
            universe, initial_capital=args.capital, no_override=args.no_override,
        )
        # Sweep 2: SL/TP
        sl_tp_results = await sl_tp_sweep(
            universe, initial_capital=args.capital, no_override=args.no_override,
        )
        # Sweep 3: universe
        universe_results = await universe_sweep(
            universe, initial_capital=args.capital, no_override=args.no_override,
        )

        elapsed = time.time() - _RUN_STATE["t_start"]
        _tee(f"\n=== Sweep complete in {_format_elapsed(elapsed)} ===")

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
            _tee(f"CSV: {csv_path}")

        # Save markdown
        md_path = output_dir / "sensitivity_matrix.md"
        write_sensitivity_matrix(
            threshold_results, sl_tp_results, universe_results, md_path
        )
        _tee(f"Matrix: {md_path}")

        return 0
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
