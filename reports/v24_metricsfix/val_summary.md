# Backtest Summary — v24_metricsfix/val

**Generated:** 2026-06-02T15:32:43.702812Z

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
| Annualized return | nan% |
| Annualized volatility | 353.13% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
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

⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead.

⚠️ Trade count is low (<30). Sharpe is statistically unreliable. Run on a longer period or relax confluence threshold.
⚠️ Max drawdown > 20% — risk parameters too loose.