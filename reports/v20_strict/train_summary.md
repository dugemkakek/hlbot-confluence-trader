# Backtest Summary — v20_strict/train

**Generated:** 2026-06-02T13:59:37.365956Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.35
- `split`: train
- `period`: 2026-04-21 23:00:00+00:00 → 2026-05-12 18:00:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $1,976.07 |
| Total return | -80.24% |
| Annualized return | -100.00% |
| Annualized volatility | 231.27% |
| Sharpe ratio | -0.43 |
| Sortino ratio | -0.30 |
| Calmar ratio | -1.25 |
| Max drawdown | -80.26% |
| Max DD duration (bars) | 377 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 24 |
| Wins / Losses | 7 / 17 |
| Win rate | 29.17% |
| Avg win | $7.52 |
| Avg loss | $-8.71 |
| Profit factor | 0.36 |
| Avg bars held | 155.6 |
| Avg exposure | 21.74% |

## Interpretation

❌ **Negative risk-adjusted returns** — strategy loses money on a risk-adjusted basis.

⚠️ Trade count is low (<30). Sharpe is statistically unreliable. Run on a longer period or relax confluence threshold.
⚠️ Profit factor < 1.0 — gross losses exceed gross wins.
⚠️ Max drawdown > 20% — risk parameters too loose.