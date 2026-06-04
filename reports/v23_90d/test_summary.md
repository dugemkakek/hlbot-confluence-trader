# Backtest Summary — v23_90d/test

**Generated:** 2026-06-02T14:22:00.818620Z

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
| Final equity | $12,606.39 |
| Total return | 26.06% |
| Annualized return | nan% |
| Annualized volatility | 370.17% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -24.10% |
| Max DD duration (bars) | 128 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 27 |
| Wins / Losses | 9 / 18 |
| Win rate | 33.33% |
| Avg win | $26.13 |
| Avg loss | $-14.81 |
| Profit factor | 0.88 |
| Avg bars held | 121.1 |
| Avg exposure | 25.41% |

## Interpretation

⏳ **Window too short** — fewer than ~37 hourly bars; Sharpe not annualized (would be misleading).

⚠️ Trade count is low (<30). Sharpe is statistically unreliable. Run on a longer period or relax confluence threshold.
⚠️ Profit factor < 1.0 — gross losses exceed gross wins.
⚠️ Max drawdown > 20% — risk parameters too loose.