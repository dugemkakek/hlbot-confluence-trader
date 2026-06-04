# Backtest Summary — v25_recalib/test

**Generated:** 2026-06-02T16:08:14.212210Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.2
- `split`: test
- `period`: 2026-05-23 03:30:00+00:00 → 2026-06-02 13:00:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $13,320.38 |
| Total return | 33.20% |
| Annualized return | nan% |
| Annualized volatility | 281.64% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -16.31% |
| Max DD duration (bars) | 107 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 17 |
| Wins / Losses | 7 / 10 |
| Win rate | 41.18% |
| Avg win | $24.11 |
| Avg loss | $-13.78 |
| Profit factor | 1.22 |
| Avg bars held | 121.9 |
| Avg exposure | 18.05% |

## Interpretation

⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead.

⚠️ Trade count is low (<30). Sharpe is statistically unreliable. Run on a longer period or relax confluence threshold.