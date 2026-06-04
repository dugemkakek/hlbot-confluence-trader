# Backtest Summary — v20_strict/test

**Generated:** 2026-06-02T14:00:13.989796Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.35
- `split`: test
- `period`: 2026-05-23 03:30:00+00:00 → 2026-06-02 13:00:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $5,162.53 |
| Total return | -48.37% |
| Annualized return | -100.00% |
| Annualized volatility | 185.61% |
| Sharpe ratio | -0.54 |
| Sortino ratio | -0.38 |
| Calmar ratio | -2.06 |
| Max drawdown | -48.60% |
| Max DD duration (bars) | 128 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 4 |
| Wins / Losses | 1 / 3 |
| Win rate | 25.00% |
| Avg win | $25.22 |
| Avg loss | $-12.21 |
| Profit factor | 0.69 |
| Avg bars held | 138.5 |
| Avg exposure | 12.88% |

## Interpretation

❌ **Negative risk-adjusted returns** — strategy loses money on a risk-adjusted basis.

⚠️ Trade count is low (<30). Sharpe is statistically unreliable. Run on a longer period or relax confluence threshold.
⚠️ Profit factor < 1.0 — gross losses exceed gross wins.
⚠️ Max drawdown > 20% — risk parameters too loose.