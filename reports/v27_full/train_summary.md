# Backtest Summary — v27_full/train

**Generated:** 2026-06-02T16:19:41.690446Z

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
| Final equity | $11,502.55 |
| Total return | 15.03% |
| Annualized return | nan% |
| Annualized volatility | 172.00% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -23.29% |
| Max DD duration (bars) | 332 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 50 |
| Wins / Losses | 14 / 36 |
| Win rate | 28.00% |
| Avg win | $23.83 |
| Avg loss | $-12.69 |
| Profit factor | 0.73 |
| Avg bars held | 122.5 |
| Avg exposure | 20.36% |

## Interpretation

⏳ **Window too short to annualize** — less than 90 days of hourly bars; Sharpe would be misleading. Use period_return as the headline metric instead.

⚠️ Profit factor < 1.0 — gross losses exceed gross wins.
⚠️ Max drawdown > 20% — risk parameters too loose.