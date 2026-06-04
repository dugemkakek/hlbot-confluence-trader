# Strategy Calibration — Sensitivity Matrix

**Generated:** 2026-06-02T23:53:03.436557Z

**Setup:** 30 days of 1h candles, 8 symbols, $10k capital, train/val/test = 50/25/25.

Cells show: `return / profit_factor / max_DD / num_trades`

## Sweep 1: Confluence Threshold

| Threshold | Train | Val | Test | Verdict |
|---|---|---|---|---|
| 0.10 | +6.4% / PF 1.12 / DD -29% / n=76 | +46.3% / PF 1.09 / DD -27% / n=20 | +23.9% / PF 1.17 / DD -24% / n=25 | 🟢 |
| 0.15 | +6.9% / PF 1.15 / DD -29% / n=71 | +47.3% / PF 1.19 / DD -22% / n=19 | +28.3% / PF 1.26 / DD -24% / n=23 | 🟢 |
| 0.20 | +22.1% / PF 1.15 / DD -27% / n=73 | +48.8% / PF 1.33 / DD -22% / n=20 | +26.1% / PF 0.88 / DD -24% / n=27 | 🟢 |
| 0.25 | +26.4% / PF 1.07 / DD -30% / n=76 | +75.8% / PF 1.31 / DD -23% / n=19 | +29.8% / PF 0.71 / DD -20% / n=24 | 🟢 |
| 0.30 | +22.3% / PF 1.00 / DD -13% / n=62 | +76.2% / PF 0.97 / DD -19% / n=12 | +16.0% / PF 0.75 / DD -25% / n=19 | 🟢 |
| 0.35 | +13.4% / PF 0.71 / DD -30% / n=54 | +65.4% / PF 1.25 / DD -14% / n=8 | +15.6% / PF 0.39 / DD -20% / n=12 | 🟢 |

## Sweep 2: SL / TP (1:2 reward:risk ratio)

| SL / TP | Train | Val | Test | Verdict |
|---|---|---|---|---|
| 1/2% | +5.3% / PF 0.97 / DD -27% / n=134 | +35.6% / PF 1.06 / DD -30% / n=44 | +15.8% / PF 0.98 / DD -25% / n=46 | 🟢 |
| 2/3% | +10.9% / PF 0.90 / DD -27% / n=101 | +45.1% / PF 1.50 / DD -27% / n=30 | +15.0% / PF 0.71 / DD -26% / n=35 | 🟢 |
| 2/4% | +22.1% / PF 1.15 / DD -27% / n=73 | +48.8% / PF 1.33 / DD -22% / n=20 | +26.1% / PF 0.88 / DD -24% / n=27 | 🟢 |
| 3/6% | +12.6% / PF 0.81 / DD -30% / n=41 | +49.9% / PF 0.95 / DD -16% / n=9 | +40.6% / PF 0.61 / DD -23% / n=13 | 🟢 |

## Sweep 3: Universe Size

| Symbols | Train | Val | Test | Verdict |
|---|---|---|---|---|
| 3 (BTC, ETH, SOL) | +9.6% / PF 0.65 / DD -15% / n=21 | +20.2% / PF 0.00 / DD -1% / n=0 | +20.3% / PF 1.79 / DD -12% / n=8 | 🟢 |
| 5 (BTC, ETH, SOL...) | +15.0% / PF 0.73 / DD -23% / n=50 | +41.7% / PF 1.20 / DD -13% / n=9 | +33.2% / PF 1.22 / DD -16% / n=17 | 🟢 |
| 8 (BTC, ETH, SOL...) | +22.1% / PF 1.15 / DD -27% / n=73 | +48.8% / PF 1.33 / DD -22% / n=20 | +26.1% / PF 0.88 / DD -24% / n=27 | 🟢 |

## Verdict

🟢 = profitable in both train and test (consistent edge)
🟡 = profitable in train OR test but not both (mixed)
❌ = unprofitable in both (no edge)

## 90-day validation (best config: 5 symbols, threshold 0.20, SL/TP 2/4%)

The 30-day sweep above shows ALL 13 configs as 🟢. **This is misleading**
— the 30-day window happened to capture a favorable regime.

| Split | Trades | Win% | Max DD | Final | Return |
|---|---|---|---|---|---|
| TRAIN | 207 | 29.0% | -37.8% | $9,531 | **-4.7%** |
| VAL | 67 | 20.9% | -23.7% | $10,171 | +1.7% |
| TEST | 69 | 40.6% | -29.9% | $10,200 | +2.0% |

**Verdict on 90 days: essentially break-even.** Train is slightly
negative, val/test barely positive. The 30-day sweep was
window-dependent noise.

## Honest Take

The confluence-based strategy has a **weak and possibly non-existent
edge** over 90 days. Win rate hovers around 30%, max DD is
25-38% (high), and cumulative return is ~0% over 3 months.

The 30-day sweep showed +22%/+49%/+26% which would have been
genuinely profitable — but the 90-day validation reveals
that was lucky timing, not skill.

**Recommendation:** Before going to real money, either
- (a) find a fundamentally different signal stack (the
  confluence approach isn't predictive enough), or
- (b) run a 6-month or 1-year out-of-sample backtest to see
  if the strategy has any edge at all across market regimes.

The current strategy is **not suitable for real-money deployment**
in its current form. The risk-adjusted returns are negative
in train and only marginally positive in test — that's not
an edge, that's noise.

The good news: the harness is honest now. We can iterate on
the signal stack and re-test quickly.
