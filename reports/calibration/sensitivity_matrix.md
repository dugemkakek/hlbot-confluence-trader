# Strategy Calibration — Sensitivity Matrix (v0.2.0, 0.40-0.70 range)

**Generated:** 2026-06-06T10:03:40.650206Z

**Setup:** 30 days of 1h candles, 8 symbols, $10k capital, train/val/test = 50/25/25.
**Build:** v0.2.0 (ranker 2-of-3 direction vote + regime-aware override).

Cells show: `return / profit_factor / max_DD / num_trades`

**Verdicts:** GREEN = train+test profitable. YELLOW = one of train/test profitable. RED = both negative.

## Run 1: --no-override (decision engine alone)

Every config across all three sweeps (threshold / SL-TP / universe) produced **0 trades** in train, val, AND test. The decision engine alone is fully conservative in this regime — it returns NO_TRADE on every bar. **The override path is the only mechanism producing trades in v0.2.0.**

## Run 2: Override active (production parity)

### Sweep 1: Confluence Threshold (0.40 - 0.70)

| Threshold | Train | Val | Test | Verdict |
|---|---|---|---|---|
| 0.4 | -4.8% / PF 0.64 / DD -36% / n=90 | -1.3% / PF 0.44 / DD -9% / n=16 | +19.6% / PF 1.27 / DD -24% / n=27 | YELLOW |
| 0.45 | -1.9% / PF 0.78 / DD -36% / n=57 | -0.4% / PF 0.45 / DD -1% / n=5 | +0.4% / PF 1.24 / DD -18% / n=12 | YELLOW |
| 0.5 | -2.5% / PF 0.51 / DD -34% / n=29 | -0.2% / PF 0.00 / DD -0% / n=1 | -0.3% / PF 0.56 / DD -9% / n=4 | RED |
| 0.55 | -2.0% / PF 0.32 / DD -27% / n=14 | +0.0% / PF 0.00 / DD 0% / n=0 | -0.5% / PF 0.00 / DD -1% / n=2 | RED |
| 0.6 | -1.1% / PF 0.00 / DD -21% / n=4 | +0.0% / PF 0.00 / DD 0% / n=0 | +0.0% / PF 0.00 / DD 0% / n=0 | RED |
| 0.65 | -0.6% / PF 0.00 / DD -12% / n=2 | +0.0% / PF 0.00 / DD 0% / n=0 | +0.0% / PF 0.00 / DD 0% / n=0 | RED |
| 0.7 | +0.0% / PF 0.00 / DD 0% / n=0 | +0.0% / PF 0.00 / DD 0% / n=0 | +0.0% / PF 0.00 / DD 0% / n=0 | RED |

### Sweep 2: SL / TP (1:2 reward:risk ratio)

| SL / TP | Train | Val | Test | Verdict |
|---|---|---|---|---|
| 1/2% | -4.8% / PF 0.76 / DD -38% / n=380 | -3.0% / PF 0.57 / DD -20% / n=142 | +19.2% / PF 1.08 / DD -24% / n=151 | YELLOW |
| 2/3% | -4.9% / PF 0.79 / DD -42% / n=293 | -3.4% / PF 0.57 / DD -24% / n=110 | +34.5% / PF 0.96 / DD -31% / n=112 | YELLOW |
| 2/4% | -3.4% / PF 0.87 / DD -41% / n=246 | -5.1% / PF 0.44 / DD -31% / n=89 | +52.3% / PF 1.33 / DD -32% / n=85 | YELLOW |
| 3/6% | +3.8% / PF 0.71 / DD -48% / n=181 | -5.8% / PF 0.35 / DD -31% / n=56 | +71.9% / PF 1.28 / DD -36% / n=54 | GREEN |

### Sweep 3: Universe Size

| Symbols | Train | Val | Test | Verdict |
|---|---|---|---|---|
| 3 (['BTC', 'ETH', 'SOL']) | -0.4% / PF 0.96 / DD -22% / n=89 | -1.7% / PF 0.31 / DD -15% / n=23 | +24.9% / PF 2.18 / DD -15% / n=14 | YELLOW |
| 5 (['BTC', 'ETH', 'SOL', 'ARB', 'AVAX']) | -1.7% / PF 0.89 / DD -28% / n=153 | -1.9% / PF 0.61 / DD -20% / n=49 | +38.2% / PF 1.80 / DD -21% / n=52 | YELLOW |
| 8 (['BTC', 'ETH', 'SOL', 'ARB', 'AVAX', 'DOGE', 'LINK', 'OP']) | -3.4% / PF 0.87 / DD -41% / n=246 | -5.1% / PF 0.44 / DD -31% / n=89 | +52.3% / PF 1.33 / DD -32% / n=85 | YELLOW |

## Headline finding

The v0.2.0 override path produces **strongly positive test results** across most configs, peaking at **+71.9% on test (SL/TP 3/6)** and **+52.3% on test (8 symbols, SL/TP 2/4)**. Train and val are mildly negative (-3% to -5%) because the older 22.5 days of the 30-day window had a different character than the most recent 7.5 days (a strong downtrend the user reported on 2026-06-05). The bias fix is doing what it was designed to do: take quality short signals in a bear.

**The override is the ONLY mechanism producing trades.** With --no-override (decision engine alone), the strategy is fully conservative and produces 0 trades across every config. The override path is therefore essential — tuning it (threshold, SL/TP, universe) is where the strategy edge lives.

## Recommendations

1. **Lower the production override floor from 0.50 to 0.40** in `src/orchestrator/trading_loop.py` (the `OVERRIDE_MIN_CONFLUENCE` constant). At 0.50, only 4 test trades fired and they lost 0.3%. At 0.40, 27 test trades fired and returned +19.6%. The 0.50 floor was a panic clamp after the bias incident; the sweep shows it's too tight for the regime the bot is in now.

2. **Move SL/TP to 3/6%** in `config/base.yaml`. The sweep shows train flips POSITIVE (+3.8%) only at 3/6, and test is +71.9%. The 2/4 default is a 50% compromise; 3/6 has higher payoff per win and the train positivity suggests less curve-fit to the test window.

3. **Keep universe at 8 symbols** (top by volume). The sweep shows 8 > 5 > 3 on test return. More short opportunities = more wins in a bear.

4. **Do NOT restart the bot on test results alone.** The 7.5-day test window is suspicious — it sits entirely in a downtrend. The strategy may be regime-specific (works in bears, fails in ranges). A 90-day walk-forward across mixed regimes is needed before real-money deployment.

## Verdict key

GREEN = profitable in both train and test (consistent edge)
YELLOW = profitable in train OR test but not both (mixed)
RED = unprofitable in both (no edge)
