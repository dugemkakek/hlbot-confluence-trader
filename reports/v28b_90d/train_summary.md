# Backtest Summary — v28b_90d/train

**Generated:** 2026-06-03T00:04:34.987838Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.2
- `split`: train
- `period`: 2026-02-19 00:00:00+00:00 → 2026-04-11 23:30:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $9,530.53 |
| Total return | -4.69% |
| Annualized return | nan% |
| Annualized volatility | 352.73% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -37.75% |
| Max DD duration (bars) | 573 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 207 |
| Wins / Losses | 60 / 147 |
| Win rate | 28.99% |
| Avg win | $27.18 |
| Avg loss | $-14.30 |
| Profit factor | 0.78 |
| Avg bars held | 135.0 |
| Avg exposure | 18.37% |

## Interpretation

⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead.

⚠️ Profit factor < 1.0 — gross losses exceed gross wins.
⚠️ Max drawdown > 20% — risk parameters too loose.