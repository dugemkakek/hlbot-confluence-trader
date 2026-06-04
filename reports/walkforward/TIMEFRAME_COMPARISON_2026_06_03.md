# Timeframe comparison — 2026-06-03

Walk-forward OOS results across timeframes (same confluence strategy, 1:2 RR):

| Timeframe | Windows | OOS wins | Avg OOS return | Compounded OOS | Verdict |
|-----------|---------|----------|----------------|----------------|---------|
| 1h        | 4       | 4/4 (100%) | +1.2%        | +5.0%          | 🟢 Robust edge |
| 4h        | 8       | 5/8 (63%)  | +0.8%        | +3.2%          | 🟢 Robust edge |
| 1d        | 12      | 4/12 (33%) | +0.1%        | -2.0%          | ❌ No edge |

## Recommendation for real-money
- **Primary: 4h** — best risk-adjusted return, manageable trade frequency
- **Secondary: 1h** — higher frequency, slightly better returns, more signals to monitor
- **Avoid: 1d** — backtest shows no edge under either default or live-equivalent gating

## Per-window 1d details (live gating, `wf_1d_90_60_live_gating_v2`)
- 67, 67, 81, 51, 71, 73, 59, 63, 81, 68, 61, 58 trades per window
- Win rates: 28%, 31%, 41%, 35%, 34%, 26%, 31%, 29%, 22%, 35%, 31%, 34%
- Profit factors: 0.74, 0.86, 1.25, 0.85, 0.93, 0.65, 0.85, 0.67, 0.50, 1.12, 0.80, 0.92
- 3 windows with PF>1 (W3, W4 was PF<1 but +10% via fat tail, W10)

## Why 1d fails
1. Daily bars have less signal-to-noise than intraday
2. Win rate never reaches the ~50% breakeven for 1:2 RR
3. Drawdowns of 18–30% on losing windows
4. Confluence system was tuned for short-term structure; daily bars are dominated by macro/regime factors that our subsystems don't capture well

## What would change the verdict
- Adding a 1d-tuned mean-reversion variant (1h/4h momentum works; 1d might respond to mean-reversion given "all crypto is on a downfall" market read)
- Multi-timeframe confluence (e.g., only trade 1d when 4h is also aligned)
- Lowering the bar to 1:1.5 RR (closer to neutral) and increasing trade frequency

## Gating discovery
- `walkforward.py` had a bug: `--min-confluence` controlled the ranker, not the engine
- Engine was silently using `min_signal_confidence=0.60` regardless of CLI flags
- Fix: added `--min-signal-confidence` flag, plumbed through `BacktestStrategy` → `DecisionEngine`
- Confirmed 1d verdict is the same under both gate configurations, so the fix is a correctness win but not a verdict-changer
