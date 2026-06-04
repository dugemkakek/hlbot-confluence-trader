# Backtest Summary — v28b_90d/test

**Generated:** 2026-06-03T00:08:21.354502Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.2
- `split`: test
- `period`: 2026-05-07 23:15:00+00:00 → 2026-06-02 23:00:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $10,199.64 |
| Total return | 2.00% |
| Annualized return | nan% |
| Annualized volatility | 290.48% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -29.92% |
| Max DD duration (bars) | 333 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 69 |
| Wins / Losses | 28 / 41 |
| Win rate | 40.58% |
| Avg win | $27.54 |
| Avg loss | $-13.94 |
| Profit factor | 1.35 |
| Avg bars held | 145.4 |
| Avg exposure | 20.65% |

## Interpretation

⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead.

⚠️ Max drawdown > 20% — risk parameters too loose.