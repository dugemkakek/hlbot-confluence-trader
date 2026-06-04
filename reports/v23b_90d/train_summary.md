# Backtest Summary — v23b_90d/train

**Generated:** 2026-06-02T14:33:08.729172Z

## Configuration
- `initial_capital`: 10000.0
- `position_size_pct`: 0.1
- `max_position_pct`: 0.2
- `stop_loss_pct`: 0.02
- `take_profit_pct`: 0.04
- `min_confluence`: 0.2
- `split`: train
- `period`: 2026-02-18 15:00:00+00:00 → 2026-04-11 14:30:00+00:00

## Performance

| Metric | Value |
|---|---|
| Initial capital | $10,000.00 |
| Final equity | $9,345.04 |
| Total return | -6.55% |
| Annualized return | nan% |
| Annualized volatility | 408.48% |
| Sharpe ratio | nan |
| Sortino ratio | nan |
| Calmar ratio | nan |
| Max drawdown | -43.59% |
| Max DD duration (bars) | 574 |

## Trade Stats

| Metric | Value |
|---|---|
| Number of trades | 235 |
| Wins / Losses | 65 / 170 |
| Win rate | 27.66% |
| Avg win | $29.03 |
| Avg loss | $-14.86 |
| Profit factor | 0.75 |
| Avg bars held | 133.3 |
| Avg exposure | 19.86% |

## Interpretation

⏳ **Window too short** — fewer than ~37 hourly bars; Sharpe not annualized (would be misleading).

⚠️ Profit factor < 1.0 — gross losses exceed gross wins.
⚠️ Max drawdown > 20% — risk parameters too loose.