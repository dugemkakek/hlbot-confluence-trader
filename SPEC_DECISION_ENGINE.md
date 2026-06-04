# Decision Engine — Specification

## 1. Overview

**Project:** Wing Trading AI — Decision Engine  
**Agent:** Aoi | Domain: Intelligence Layer  
**Phase:** Phase 1.5 — Decision Engine Core  
**Philosophy:** "The objective is NOT maximum trades. The objective is: durable edge, adaptive intelligence, disciplined execution, and long-term asymmetric compounding."

---

## 2. Architecture

```
signals/technical.py         signals/registry.py         [External]
         │                            │
         ▼                            ▼
  [SignalRegistry] ◄────────── [SentimentScorer]
         │                            │
         │                     [RegimeDetector]
         │                            │
         └──────────┬─────────────────┘
                    ▼
          [DecisionEngine]
                    │
                    ▼
             [RiskManager]
                    │
                    ▼
           [PaperExecutor]
```

The Decision Engine is the weighted scoring aggregator at the center of the trading stack:

1. **SignalRegistry** collects signals from all signal modules (technical, sentiment, etc.)
2. **RegimeDetector** classifies the current market regime, which modulates signal weights
3. **SentimentScorer** produces a 0–1 sentiment score per symbol via RSS feeds
4. **DecisionEngine** aggregates all subsystem scores into a weighted final decision

---

## 3. Subsystem Scoring

Each subsystem contributes a raw score in `[0.0, 1.0]`. The final score is a weighted sum.

| Subsystem | Weight | Min Threshold | Role |
|---|---|---|---|
| Market Structure | 25% | 0.3 | Trend, SMA/EMA alignment, structure |
| Momentum | 15% | 0.3 | RSI, MACD, rate of change |
| Orderflow | 20% | 0.3 | Volume, trade size imbalance |
| Sentiment | 15% | 0.3 | RSS/news score |
| Macro | 10% | 0.3 | Cross-symbol correlation, market-wide bias |
| Volatility Regime | 10% | 0.3 | ATR, Bollinger width, realized vol |
| Risk Filter | 5% | — | Hard pass on extreme drawdown / exposure |

### Hard Rules

- **NO single-condition trades.** Minimum **3 independent subsystems** must confirm with score ≥ 0.3
- **NO_TRADE** is a valid and preferred output when conditions are not met
- Each subsystem score must exceed **0.3 minimum threshold** to count as a confirmation
- Final weighted score must exceed **0.60** to fire (matches `engine.min_signal_confidence`)

---

## 4. Market Regime Classification

Regime is detected from price data and affects which signals are weighted higher.

| Regime | Detection Logic |
|---|---|
| `TREND_UP` | EMA(20) > EMA(50) on 1H+, ADX > 25 |
| `TREND_DOWN` | EMA(20) < EMA(50) on 1H+, ADX > 25 |
| `RANGE_BOUND` | ADX < 20, price oscillating within 2σ Bollinger |
| `HIGH_VOL` | ATR% > 2× 20-period ATR SMA, or Bollinger bandwidth > 3× 20-period avg |
| `LOW_LIQUIDITY` | Volume < 50% of 20-period volume SMA, spread widening |

**Regime-dependent signal weighting boost:**
- TREND_UP/TREND_DOWN: Momentum signals boosted +10%
- RANGE_BOUND: Mean-reversion signals boosted +15%
- HIGH_VOL: Volatility-aware signals boosted +10%, others de-emphasized
- LOW_LIQUIDITY: All signals de-emphasized, size reduced

---

## 5. Decision Output

```
Decision(
    action:      "BUY" | "SELL" | "NO_TRADE",
    symbol:      str,
    size:        float,          # fraction of portfolio (0.0–1.0)
    entry:       float | None,
    stop_loss:   float | None,
    take_profit: float | None,
    confidence:  float,          # 0.0–1.0
    regime:      Regime,
    signals:     list[Signal],
    reason:      str,            # human-readable summary
    timestamp:   datetime,
)
```

### Decision Logic

1. Aggregate raw subsystem scores from SignalRegistry + SentimentScorer + RegimeDetector
2. Apply regime-dependent weight boosts/penalties
3. Compute weighted sum: `final_score = Σ (adjusted_score_i × weight_i)`
4. Count subsystems where `adjusted_score_i ≥ 0.3`
5. **FIRE condition:** `final_score ≥ 0.60` **AND** `confirming_subsystems ≥ 3`
6. Direction: BUY if net bullish signals > net bearish, else SELL
7. Size: `final_score × risk_factor` (capped at `max_position_pct`)

---

## 6. Sentiment Scorer

**RSS Feeds (public, no auth):**
- CoinDesk: `https://www.coindesk.com/arc/outboundfeeds/rss/`
- Cointelegraph: `https://cointelegraph.com/rss`

**Scoring:**
- Fetch latest 20 items per feed, filter by symbol keyword
- Score = weighted average recency: `score = Σ (weight_i × sentiment_i) / Σ weight_i`
- `weight_i = 1 / (1 + age_in_hours)` — newer items weighted higher
- Keyword matching: "bullish", "upbeat", "buy", "long" → +1; "bearish", "sell", "short", "crash" → -1
- Normalize to `[0.0, 1.0]`

**Cache:** Results cached in Redis with 5-minute TTL.

---

## 7. File Inventory

| File | Purpose |
|---|---|
| `SPEC_DECISION_ENGINE.md` | This specification |
| `src/engine/decision_engine.py` | Weighted scoring aggregator |
| `src/signals/regime_detector.py` | Market regime classifier |
| `src/signals/sentiment_scorer.py` | RSS sentiment scorer |

---

## 8. Configuration

```yaml
engine:
  cycle_interval_seconds: 60
  min_signal_confidence: 0.60   # must exceed this to fire
  min_confirmations: 3          # minimum subsystems confirming
  min_subsystem_score: 0.30      # per-subsystem minimum threshold

sentiment:
  rss_feeds:
    coindesk: "https://www.coindesk.com/arc/outboundfeeds/rss/"
    cointelegraph: "https://cointelegraph.com/rss"
  cache_ttl_seconds: 300
  max_items_per_feed: 20
```

---

## 9. Error Handling

- If a subsystem fails (e.g., RSS unreachable), it is assigned score `0.0` and excluded from confirmation count
- Decision engine never crashes — it logs the error and outputs `NO_TRADE`
- All errors are logged with full context for post-mortem

---

## 10. Status

- [x] SPEC_DECISION_ENGINE.md
- [x] src/engine/decision_engine.py
- [x] src/signals/regime_detector.py
- [x] src/signals/sentiment_scorer.py
