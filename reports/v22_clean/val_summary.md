# Backtest Summary — v22_clean/val

**Generated:** 2026-06-02T14:16:01.260869Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.2
- `split`: val
- `period`: 2026-05-12 18:00:00+00:00 → 2026-05-23 03:30:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $14,881.30 |
| Total return | 48.81% |
| Annualized return | 1036222787616.56% |
| Annualized volatility | 353.13% |
| Sharpe ratio | 2934387961.01 |
| Sortino ratio | 3544780477.53 |
| Calmar ratio | 47705023708.87 |
| Max drawdown | -21.72% |
| Max DD duration (bars) | 66 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 20 |
| Wins / Losses | 8 / 12 |
| Win rate | 40.00% |
| Avg win | $30.98 |
| Avg loss | $-15.56 |
| Profit factor | 1.33 |
| Avg bars held | 124.9 |
| Avg exposure | 27.60% |

## Interpretation

🌟 **Exceptional** — Sharpe > 2.0, likely overfit; verify out-of-sample.

⚠️ Trade count is low (<30). Sharpe is statistically unreliable. Run on a longer period or relax confluence threshold.
⚠️ Max drawdown > 20% — risk parameters too loose.