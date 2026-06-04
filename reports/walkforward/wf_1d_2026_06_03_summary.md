# 1d walk-forward — 2026-06-03

## Setup
- Universe: BTC, ETH, SOL, ARB, LINK, OP (6/8 — AVAX/DOGE rate-limited)
- Data: 500 daily bars, 2025-01-20 to 2026-06-03 (Hyperliquid REST, cached CSV)
- Engine: `min_signal_confidence=0.20, min_confirmations=1, min_subsystem_score=0.30` (backtest defaults)
- 12 sliding windows: 90d train / 60d test, step 30d
- lookback_bars=20 (1d equivalent — needed to leave room in 60d test window)

## Per-window OOS results
| Window | Test Period       | Return  | MaxDD  | Trades | WinRate | PF   | Final Eq   |
|--------|-------------------|---------|--------|--------|---------|------|------------|
| W1     | 2025-04 → 2025-06 |  -2.24% | -18.4% | 67     | 28.4%   | 0.74 | $9,776     |
| W2     | 2025-05 → 2025-07 |  -1.21% | -14.3% | 67     | 31.3%   | 0.86 | $9,879     |
| W3 ✓   | 2025-06 → 2025-08 |  +1.53% |  -6.1% | 81     | 38.3%   | 1.13 | $10,153    |
| W4 ✓   | 2025-07 → 2025-09 | +10.30% | -10.1% | 51     | 35.3%   | 0.85 | $11,030    |
| W5     | 2025-08 → 2025-10 |  -0.67% | -21.1% | 71     | 33.8%   | 0.93 | $9,933     |
| W6 ✓   | 2025-09 → 2025-11 |  +4.16% | -16.9% | 73     | 26.0%   | 0.65 | $10,416    |
| W7     | 2025-10 → 2025-12 |  -1.28% | -30.8% | 59     | 30.5%   | 0.85 | $9,872     |
| W8     | 2025-11 → 2026-01 |  -3.29% | -19.8% | 63     | 28.6%   | 0.67 | $9,671     |
| W9     | 2025-12 → 2026-02 |  -6.67% |  -8.1% | 81     | 22.2%   | 0.50 | $9,333     |
| W10 ✓  | 2026-01 → 2026-03 |  +1.50% | -28.8% | 68     | 35.3%   | 1.12 | $10,150    |
| W11    | 2026-02 → 2026-04 |  -1.53% | -22.1% | 61     | 31.1%   | 0.80 | $9,847     |
| W12    | 2026-03 → 2026-05 |  -0.58% |  -9.2% | 58     | 34.5%   | 0.92 | $9,942     |

## Aggregate
- 4/12 OOS wins (33%)
- Avg OOS return: 0.00%
- Compounded OOS: -3.13%
- Win-rate never above 38.3% (need >50% to break even on 1:2 RR)
- Profit factor mostly <1

## Verdict
**No edge at 1d under the backtest's default gating.** 
Strategy works on 1h and 4h (4/4 and 5/8 OOS wins respectively) but does not extend to 1d.
The 1d test is more conservative than live (min_confirmations=1, min_subsystem_score=0.30) so a second pass with live gating is needed to make sure we're not throwing out a viable edge.

## Next step
- Re-run with `--min-confirmations 2 --min-subsystem-score 0.15` to match live config
- If still no edge, recommend keeping 4h as the primary timeframe for real-money
