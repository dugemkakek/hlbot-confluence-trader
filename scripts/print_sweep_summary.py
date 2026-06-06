"""Print a quick summary of the v0.2.0 calibration sweep results."""
import csv
from pathlib import Path

p = Path("reports/calibration/sweep_results.csv")
with p.open(encoding="utf-8") as f:
    reader = csv.DictReader(f)
    print(f"{'param':<16} {'value':<8} {'train_n':>8} {'train_ret':>10} {'val_n':>6} {'val_ret':>8} {'test_n':>7} {'test_ret':>9}  {'train_pf':>8} {'test_pf':>7}")
    for r in reader:
        print(
            f"{r['param']:<16} {r['value']:<8} "
            f"{r['train_num_trades']:>8} {float(r['train_total_return'])*100:>9.1f}% "
            f"{r['val_num_trades']:>6} {float(r['val_total_return'])*100:>7.1f}% "
            f"{r['test_num_trades']:>7} {float(r['test_total_return'])*100:>8.1f}%  "
            f"{float(r['train_profit_factor']):>8.2f} {float(r['test_profit_factor']):>7.2f}"
        )
