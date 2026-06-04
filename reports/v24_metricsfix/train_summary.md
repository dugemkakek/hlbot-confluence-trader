# Backtest Summary — v24_metricsfix/train

**Generated:** 2026-06-02T15:32:07.651413Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.2
- `split`: train
- `period`: 2026-04-21 23:00:00+00:00 → 2026-05-12 18:00:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $12,210.05 |
| Total return | 22.10% |
| Annualized return | nan% |
| Annualized volatility | 202.86% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -27.30% |
| Max DD duration (bars) | 329 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 73 |
| Wins / Losses | 29 / 44 |
| Win rate | 39.73% |
| Avg win | $23.66 |
| Avg loss | $-13.54 |
| Profit factor | 1.15 |
| Avg bars held | 144.0 |
| Avg exposure | 28.39% |

## Interpretation

⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead.

⚠️ Max drawdown > 20% — risk parameters too loose.