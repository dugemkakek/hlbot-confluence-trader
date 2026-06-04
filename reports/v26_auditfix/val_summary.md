# Backtest Summary — v26_auditfix/val

**Generated:** 2026-06-02T16:14:56.598608Z

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
| Final equity | $14,171.20 |
| Total return | 41.71% |
| Annualized return | nan% |
| Annualized volatility | 269.41% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -12.92% |
| Max DD duration (bars) | 105 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 9 |
| Wins / Losses | 3 / 6 |
| Win rate | 33.33% |
| Avg win | $34.91 |
| Avg loss | $-14.51 |
| Profit factor | 1.20 |
| Avg bars held | 134.1 |
| Avg exposure | 22.18% |

## Interpretation

⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead.

⚠️ Trade count is low (<30). Sharpe is statistically unreliable. Run on a longer period or relax confluence threshold.