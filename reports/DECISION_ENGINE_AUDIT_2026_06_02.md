# Decision Engine Audit — 2026-06-02

## Method

Sampled 28 bars across 90 days of cached 1h data (BTC, ETH, SOL,
ARB, AVAX, DOGE, LINK, OP) = 173 (symbol × bar) evaluations. For
each, ran the live `PairRanker` + `DecisionEngine` with the same
signal stack the backtest uses (sma/ema/rsi/macd/bollinger) and
recorded the full distribution of every score.

## Finding 1 — Signal distribution is brutally weak

| Score | Mean | Median | Max | % > 0.2 |
|---|---|---|---|---|
| abs(structure) | 0.061 | 0.046 | 0.250 | 2.3% |
| abs(pullback) | 0.081 | 0.048 | 0.505 | 10.4% |
| abs(momentum) | 0.295 | 0.261 | 0.799 | 61.3% |
| volume (norm) | 0.397 | 0.277 | 1.000 | n/a |

→ **Structure and pullback are binding constraints.** They make
up 55% of confluence weight but produce tiny values on most bars.
Momentum is the only signal with real amplitude.

→ **Confluence is bounded.** Mean 0.185, max 0.447, 75th pct
0.230. At production threshold 0.35, only 6.4% of ranked pairs
fire. At 0.20, 38% fire. The threshold *is* the strategy.

## Finding 2 — Decision engine is dead code

Of 173 decision engine evaluations, **all 173 returned NO_TRADE**.
0 BUY, 0 SELL ever produced. Reason-code distribution:

- 37% `insufficient_confirmations` (only 1-2 of 6 subsystems confirming)
- 63% `final_score_low` (final score 0.20 vs threshold 0.60)

**The bot's live trades (4 LONG positions in production) all
come from the ranker override path, not the decision engine.**

The override path (in `trading_loop._evaluate_ranked_pair` and
`BacktestStrategy.on_bar`) is:
- ranker.is_actionable (confluence > 0 + direction set)
- confluence >= min_confluence
- → force a trade, ignoring the decision engine entirely

So the architecture has two paths — ranker and decision engine —
and only the ranker is alive. The decision engine is dead code
that the bot carries for audit-log reasons.

## Finding 3 — Three subsystems are non-functional in backtest

- **orderflow** (20% weight) — requires `volume_spike`,
  `orderbook_imbalance`, `trade_size`, `obv` signals. None of
  these are registered in the backtest strategy. Returns 0.
- **sentiment** (15% weight) — `SentimentScorer` tries to fetch
  RSS feeds (`coindesk`, `cointelegraph`). On any failure,
  returns 0.5. In backtest, it usually returns 0.5 (default).
- **macro** (10% weight) — counts other symbols' signals in
  the same direction. The backtest resets the registry per
  evaluation, so it always sees an empty registry → returns 0.

So 45% of the decision engine's weight is on dead subsystems.
The remaining 55% (mkt_structure, momentum, vol_regime) is
active but capped low by the data distribution.

## Finding 4 — Even with low thresholds, decision engine rarely fires

| Config (min_conf, min_confirm) | Actionable decisions (of 173) |
|---|---|
| 0.60, 2 (production) | 0 |
| 0.30, 2 | TBD (still running) |
| 0.20, 1 | TBD |
| 0.10, 1 | TBD |

**Likely outcome:** the decision engine never has enough
confirming subsystems (3-4 needed) to hit even a relaxed
threshold, because 3 subsystems are always 0 or 0.5.

## Repairs (proposed, in priority order)

### R1 — Lower min_signal_confidence from 0.60 to 0.30

- **Effect:** Allows the decision engine to fire when the
  active subsystems (mkt_struct, momentum, vol_regime) together
  produce a meaningful score, even with dead subsystems.
- **Risk:** The engine was tuned for the old threshold. Could
  produce too many trades. Mitigation: the live position cap
  (max_position_pct=0.20) and max_daily_trades=20 still apply.
- **Effort:** One line in `config/base.yaml` + a config override.

### R2 — Lower min_subsystem_score from 0.30 to 0.15

- **Effect:** More subsystems count as "confirming", raising
  the confirming count above the 2-3 threshold. With sentiment
  at 0.5 (its default fallback) and momentum at 0.3, both
  would confirm.
- **Risk:** Lower-quality confirmations. Mitigation: the
  final_score still has to clear min_signal_confidence.
- **Effort:** One line in `config/base.yaml` + the orchestrator's
  DecisionEngine constructor.

### R3 — Disable the dead subsystems (orderflow, macro)

- **Effect:** Redistributes their 30% weight to the active
  subsystems. Confluence becomes more sensitive to real
  signals.
- **Risk:** In live trading, the orchestrator DOES register
  orderflow signals (volume_spike, etc.). Disabling the
  subsystem would lose that. Mitigation: only disable in
  backtest mode (env-gated), or replace with a more honest
  "no data" return that doesn't fake a 0.5.
- **Effort:** Medium — need to add an env check, or replace
  with proper no-data signal.

### R4 — Reweight: give momentum more, structure less

- **Effect:** Since structure is essentially dead and momentum
  is the only signal with amplitude, redistributing weight
  toward momentum captures more of the real signal.
- **Risk:** Departure from "balanced" multi-signal design.
  Mitigation: this is what the data is telling us.
- **Effort:** Update SUBSYSTEMS table in `decision_engine.py`.

### R5 — Fix macro subsystem for backtest

- **Macro counts other symbols' signals in the same direction.**
  In the backtest, the registry is per-call. A real fix would
  be to pass a cross-symbol aggregator. Lower priority.

## Recommended pair: R1 + R2

These are the lowest-risk, highest-leverage changes. R3-R5
require more thinking and may break the live bot's audit-log
behavior. R1+R2 should immediately make the decision engine
start contributing to the trade pipeline, which means:
- Audit log will show BUY/SELL decisions with reasons
- The override path will still fire (it doesn't need the engine)
- Real-money confidence is improved because we'd have two
  independent signal sources

Implementation order: R1 → re-run backtest → R2 → re-run → compare.
