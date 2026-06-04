# HLBot — Hyperliquid Paper Trading Bot

A self-contained paper trading bot that runs multi-signal
confluence-based trading strategies against the Hyperliquid
perp DEX. Built for development and validation — no real money
is ever at risk. The bot's first 5 days of operation produced
zero trades due to a cascade of latent bugs (now all fixed and
documented); it has since been running cleanly with trades
flowing.

This README is the handoff document. If you're an agent or
developer picking this up, read this first. It covers the
project's purpose, architecture, how to run it, how to debug
it, what the known issues are, and where the deeper docs live.

---

## What this project is

**HLBot** is Luke's paper-trading bot for the Hyperliquid
exchange. Its purpose is to validate a confluence-based
trading strategy in real market conditions without risking
capital. The bot:

- Scans the Hyperliquid universe dynamically (17+ pairs)
- Computes a confluence score from 4-6 independent signal
  modules (structure, pullback, momentum, volume, regime,
  sentiment)
- Runs a decision engine that requires a minimum number of
  confirmations before trading
- Routes decisions through a risk manager (position caps,
  exposure caps, drawdown breaker)
- Executes paper trades via a simulated fill model (no real
  orders ever leave this process)
- Logs every decision and fill to an audit log for
  post-hoc analysis

The bot is **NOT** a strategy optimizer. It's an execution
sandbox. Strategy changes happen in `src/signals/` and
`src/engine/`; the orchestration, risk, and execution layers
are infrastructure.

---

## Quick start (TL;DR)

```bash
# 1. Clone / navigate
cd D:/Programs/TradingBot/HLBot

# 2. Start the bot (Windows)
start.bat
# or directly:
C:/Users/luc18/AppData/Local/Programs/Python/Python314/python.exe \
  -m uvicorn src.api.main:create_app --factory \
  --host 0.0.0.0 --port 8000

# 3. Health check
curl http://localhost:8000/health
# -> {"status":"ok","pid":<pid>}

# 4. Portfolio state
curl http://localhost:8000/api/v1/portfolio

# 5. Live trades
curl http://localhost:8000/api/v1/trades
```

The bot starts in **paper mode** (`orchestrator.dry_run: true`).
No real orders are ever placed. To stop, kill the process
(`taskkill /F /PID <pid>` on Windows) or send SIGTERM.

---

## Project layout

```
D:/Programs/TradingBot/HLBot/
├── README.md                  ← this file (handoff doc)
├── CHANGELOG.md               ← keep-a-changelog version history
├── BUGS.md                    ← detailed bug investigation log
├── SPEC.md                    ← original Phase 1 architecture spec
├── SPEC_DECISION_ENGINE.md    ← decision engine rationale
├── start.bat                  ← Windows launcher
├── config/
│   ├── base.yaml              ← primary config (read first)
│   └── dev.yaml               ← dev overrides (active when DEV env set)
├── src/
│   ├── api/main.py            ← FastAPI app, registers all endpoints
│   ├── api/ws.py              ← WebSocket fanout
│   ├── audit/                 ← Audit logger
│   ├── data/
│   │   ├── hyperliquid_rest.py  ← REST client (no auth needed for public data)
│   │   ├── hyperliquid_ws.py   ← WebSocket client
│   │   ├── models.py            ← Pydantic models
│   │   ├── storage.py           ← Postgres pool wrapper (currently unused, SQLite fallback active)
│   │   └── trade_db.py          ← Trade audit log
│   ├── engine/decision_engine.py  ← Multi-signal confluence scoring
│   ├── executor/paper_executor.py ← Simulated fills, no real orders
│   ├── orchestrator/trading_loop.py ← The 60s cycle: scan → decide → execute
│   ├── risk/risk_manager.py        ← Position sizing, drawdown caps
│   ├── signals/
│   │   ├── pair_discovery.py    ← Dynamic HLP universe scanner
│   │   ├── pair_ranker.py       ← Confluence + direction logic
│   │   ├── structure_scanner.py  ← 3-phase: rough → top 10 → full analysis
│   │   ├── technical.py         ← TA indicators
│   │   ├── regime_detector.py   ← Market regime classification
│   │   ├── pullback_detector.py ← Entry timing
│   │   ├── sentiment_scorer.py  ← News sentiment
│   │   └── registry.py          ← Signal registration
│   └── utils/{config,logging,datetime_utils}.py
├── tests/                     ← pytest (mostly empty; add tests as you fix bugs)
├── data/                      ← Runtime: audit.db, trades.db
└── notebooks/                 ← Analysis scratchpads
```

---

## Architecture: how a trade happens

The bot runs a 60-second cycle (configurable, dev mode uses
30s). Each cycle is a pipeline of 6 phases:

```
[1. Discover]    PairDiscoverer
                 ↓
                 List of 17+ pairs from Hyperliquid
                 ↓
[2. Rough rank]  Filter by liquidity (sz_decimals)
                 ↓
                 Top 20 candidates
                 ↓
[3. Fetch candles] REST API → 100 candles per timeframe
                 ↓
                 candles_by_symbol dict
                 ↓
[4. Full rank]   Structure + pullback + momentum + volume
                 ↓
                 confluence_score (0-1), direction (buy/sell/None)
                 ↓
[5. Evaluate]    For each top pair:
                 - structure_scanner (swing points, support/resistance)
                 - pullback_detector (entry timing)
                 - regime_detector (market state)
                 - decision_engine.decide() → BUY/SELL/NO_TRADE
                 - If NO_TRADE but pair ranker is actionable: OVERRIDE
                 ↓
                 Decision (action, size, entry, stop, tp, confidence)
                 ↓
[6. Execute]     _execute_decision(decision):
                 - Size conversion (fraction → base units)
                 - Cash-cap by available balance
                 - Per-position-cap (max_position_pct aggregate)
                 - Position-replace (close opposite if needed)
                 - Risk manager: pre_trade_check (size, exposure, daily trades)
                 - Executor: place_order
                 - Audit log
```

The full cycle runs in `trading_loop.py:run_cycle()`.

---

## How to run it

### Prerequisites

- Python 3.11+ (this project uses 3.14 from a specific install)
- No GPU/ML deps — just standard scientific Python
- Internet access (REST + WebSocket to api.hyperliquid.xyz)
- No Hyperliquid account needed for paper mode (uses public
  market data only)

### Setup

The repo is at `D:/Programs/TradingBot/HLBot/`. No
`requirements.txt` is checked in; the Python environment is
managed externally. The launcher (`start.bat`) assumes
`C:/Users/luc18/AppData/Local/Programs/Python/Python314/python.exe`.

If you need to recreate the environment:
```bash
python -m venv venv
venv\Scripts\pip install fastapi uvicorn pydantic structlog \
    httpx websockets aiohttp numpy pandas
```

(There's no full pinned dependency list — `pip freeze` on
the working install will give you one.)

### Start the bot

```bash
# Option A: use the launcher
start.bat

# Option B: manual (preferred for development)
python -m uvicorn src.api.main:create_app --factory \
  --host 0.0.0.0 --port 8000

# Both: bot runs on localhost:8000, log to stdout
# The launcher opens a new console window; the manual command
# uses the current terminal.
```

### Verify it's running

```bash
curl http://localhost:8000/health
# {"status":"ok","pid":12345}

curl http://localhost:8000/api/v1/portfolio | python -m json.tool
# Shows cash, equity, positions, exposure
```

If `/health` returns 200 but `/api/v1/portfolio` is empty, the
bot is still in its first-cycle warmup. Wait 60-90s.

### Stop the bot

```bash
# On Windows:
taskkill /F /PID <pid>
# where <pid> is the number from /health response

# Or Ctrl+C in the terminal where it's running
```

---

## API endpoints (the handoff surface)

All endpoints are under `/api/v1/`. Bare paths like `/scan`
return 404. Source: `src/api/main.py`. Swagger UI:
`http://localhost:8000/docs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (DB/Redis state) |
| GET | `/api/v1/portfolio` | Cash, equity, positions, exposure |
| GET | `/api/v1/positions` | All open positions |
| GET | `/api/v1/positions/{symbol}` | One position |
| GET | `/api/v1/trades` | Trade history |
| POST | `/api/v1/execute` | Submit paper trade (manual override) |
| POST | `/api/v1/orders/{order_id}/cancel` | Cancel open order |
| GET | `/api/v1/signals` | All current signals |
| GET | `/api/v1/regime/{symbol}` | Regime for a pair |
| GET | `/api/v1/scanner/pairs` | Universe + ranks (full list) |
| GET | `/api/v1/scanner/top` | Top actionable pairs |
| POST | `/api/v1/decide/{symbol}` | Run decision engine for a symbol |
| GET | `/api/v1/audit/{symbol}` | Per-symbol audit log |

### Common queries

```bash
# What's the bot doing right now?
curl localhost:8000/api/v1/portfolio

# Why isn't X trading?
curl "localhost:8000/api/v1/audit/ETH?limit=5" | python -m json.tool

# What's the scanner seeing?
curl "localhost:8000/api/v1/scanner/top?limit=10" | python -m json.tool

# Is the orderbook for X alive?
curl "localhost:8000/api/v1/regime/ETH" | python -m json.tool
```

---

## Configuration

Two files: `config/base.yaml` (primary) and `config/dev.yaml`
(overrides). The dev overrides win when the `DEV` env var
is set; otherwise base is used.

### Keys you'll touch most

```yaml
# Risk parameters
risk:
  max_position_pct: 0.20         # 20% of equity per symbol (aggregate)
  max_portfolio_exposure: 0.50   # 50% deployed at any time
  max_drawdown_pct: 0.15         # Halt at 15% drawdown
  stop_loss_pct: 0.02            # 2% — current scalp config
  take_profit_pct: 0.04          # 4% — 2:1 reward:risk
  max_daily_trades: 20

# Scanner
scanner:
  min_confluence_score: 0.55     # base, dev.yaml overrides to 0.35
  max_pairs_per_cycle: 5
  rough_filter_max: 10
  min_confirmations: 3           # min independent signals to trade

# Engine
engine:
  cycle_interval_seconds: 60
  min_signal_confidence: 0.60
  warmup_candles: 100

# Orchestrator
orchestrator:
  dry_run: true                 # ALWAYS true in this project; no live trading
  cycle_interval_seconds: 60
```

### How a config value reaches the code

`src/utils/config.py` defines the Pydantic models
(`AppConfig`, `RiskConfig`, `EngineConfig`, etc.). At import
time, `get_config()` reads both YAML files, merges them, and
returns an `AppConfig` instance. Code uses it as
`self.cfg.risk.max_position_pct` (for example).

To add a new config key:
1. Add the field to the appropriate `*Config` class in
   `config.py` (Pydantic BaseModel)
2. Add a default value in `config/base.yaml`
3. Override in `config/dev.yaml` if needed
4. Access via `self.cfg.<section>.<key>`

---

## How to debug it

### The bot is running but no trades are firing

This was the original "5-day silence" bug cascade. All
those bugs are fixed and documented in `BUGS.md` (Bugs
#1-#11). The diagnostic recipe is in the `hlbot-kanban-debug`
skill (Hermes-side, see References below).

Quick health check:
```bash
# 1. Is the bot alive?
curl localhost:8000/health

# 2. Are signals being generated?
curl "localhost:8000/api/v1/scanner/pairs" | python -m json.tool | head -50
# Look for: is_actionable=true, direction != null

# 3. Is the decision engine blocking?
curl "localhost:8000/api/v1/audit/ETH?limit=5"
# Look for: reason codes that say why trades were rejected
```

Common reason codes you'll see in the audit log:

| Code | Meaning |
|---|---|
| `below_scanner_threshold` | confluence < min_confluence_score |
| `insufficient_confirmations` | fewer than min_confirmations signals |
| `final_score_low` | decision engine's final score too low |
| `final_score_below_min_confidence` | similar, post-final-score check |
| `no_orderbook` | executor had no live orderbook for symbol |
| `max_daily_trades_reached` | risk manager daily limit hit |
| `max_portfolio_exposure` | risk manager portfolio cap hit |
| `max_position_pct_exceeded` | new cap, per-symbol aggregate |

If you see `below_scanner_threshold` but the
`confluence_score` in the same row is ABOVE the threshold,
that's the audit-log-lies pattern from bug #4. Check
`BUGS.md`.

### The bot crashed

The bot writes its log to stdout. If you started it via
`start.bat`, the log is in the console window. If you started
it manually, it's in your terminal. If you started it in
the background via subprocess.Popen, redirect to a file:

```python
import subprocess
log = open("C:/temp/hlbot.log", "wb")
proc = subprocess.Popen(
    [python, "-m", "uvicorn", "src.api.main:create_app", "--factory",
     "--host", "0.0.0.0", "--port", "8000"],
    cwd="D:/Programs/TradingBot/HLBot",
    stdout=log, stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
```

### Latent bugs / "missing logic" entries

When you see a TODO comment in the code, treat it as a
latent bug. The position-replace logic (TODO in the original
code) was the bug that surfaced after the cascade fix
allowed trades to flow. The per-position cap was the bug
after position-replace. The pattern: **fixing one bug
exposes the next one in the same code path.** Expect 3-5
follow-on bugs after any major pipeline fix.

### Where to add tests

`tests/` is mostly empty. Add tests as you fix bugs — the
most valuable tests are the regression tests for the bugs
in `BUGS.md`. The orchestrator's size conversion and
position-cap logic are particularly test-worthy because
they're doing arithmetic on fractions and notionals.

---

## Project conventions

### Commit hygiene

This is a personal project (no PRs, no reviewers). Commit
when a logical unit of work is done. Format:

```
<area>: <one-line summary>

<paragraph explaining the change, including the bug number
from BUGS.md if this fixes one>
```

Example: `risk: enforce max_position_pct as aggregate per-symbol cap (fixes bug #11)`

### Code style

- Type hints on public function signatures
- Inline comments only where the *why* isn't obvious
- Doc strings on public functions
- Use `logger.info` / `logger.warning` / `logger.error` for
  observability — these show up in the audit log and in the
  run logs
- Pydantic models for any data crossing module boundaries
  (REST, WS, audit log, API responses)

### What NOT to do

- Don't commit `data/audit.db` or `data/trades.db` — they're
  runtime state
- Don't change `orchestrator.dry_run` to `false` unless you
  understand that this will place real orders. There is no
  other safety net.
- Don't remove the risk manager from `_execute_decision`
  "to simplify" — every check in there is there because a
  real bug exploited its absence
- Don't change the override path in `pair_ranker.py:55-58`
  (`is_actionable` requires both confluence > 0 AND direction
  not None) without also fixing all 4 sites that read
  `is_actionable`

---

## Known issues (as of 2026-06-02)

These are tracked in `CHANGELOG.md` and `BUGS.md`. Listed
here in priority order:

1. **`/api/v1/portfolio` exposure_pct field is wrong.** It
   reports `0.6%` when exposure is $28.28 / equity $50.00
   (should be 56.6%). Display bug; not affecting any decision
   logic. Fix: the field is probably computing against
   `initial_balance` or some other wrong denominator in
   `paper_executor.py:get_portfolio`. Deferred.

2. **Position averaging still allowed below the cap.** The
   cap is a soft clamp, not a stop. The bot can still add to
   a position up to the cap; we just don't let it exceed. If
   the strategy is wrong direction, the position can still
   grow to 20% before reversing. Mitigation: monitor the
   audit log and consider adding a hard stop on consecutive
   losing adds.

3. **Daily trade count is not yet enforced against the
   per-day limit.** `risk.max_daily_trades: 20` is configured
   but the daily counter doesn't reset at midnight UTC — it
   resets on first trade of a new day, which means a 24-hour
   window starting at 3am could be missed. Fix: explicit
   midnight-UTC reset in `paper_executor.py`.

4. **WebSocket reconnects drop orderbook state.** When the
   Hyperliquid WS reconnects, the executor's `_orderbooks`
   dict is preserved (we don't clear it on reconnect), but
   the data may be stale by a few seconds. Mitigation: force
   a re-subscribe and treat the next ~500ms as no-data.

5. **No unit tests for `_execute_decision`.** The risk
   manager, size conversion, position-replace, and cap block
   all live in one function. A regression test would
   dramatically reduce the chance of breaking any of them
   in future edits.

6. **Postgres/Redis config is stale.** `/ready` reports
   `db_connected: false, redis_connected: false` even though
   the bot is functioning (SQLite fallback). Non-blocking
   but cosmetically misleading. Fix: either connect to
   real Postgres/Redis or remove the readiness checks.

---

## Where the deeper docs live

This README is the entry point. The deeper docs:

- **`SPEC.md`** — Original Phase 1 architecture spec. Read
  this to understand the original design intent.
- **`SPEC_DECISION_ENGINE.md`** — Decision engine rationale.
  Read this before changing `src/engine/decision_engine.py`.
- **`CHANGELOG.md`** — keep-a-changelog version history.
  Lists all fixes, added features, and known issues.
- **`BUGS.md`** — Detailed bug investigation log. 11 bugs
  documented with full root-cause analysis. The bugs are
  numbered; refer to them by number when adding new ones.

### Related Hermes skills (if you're working through Hermes)

- **`hlbot`** — Full project context, file layout, API
  endpoints, config keys, current state. Load before any
  work on the trading bot.
- **`hlbot-kanban-debug`** — Worker crash + silent-trading
  diagnostic recipe. Includes a 5-bug cascade case study
  with the actual data distribution and code patterns.
- **`trading-bot`** — Alias for `hlbot`, used in some
  kanban task bodies.

---

## Coordination notes (for the multi-agent setup)

This project has an owner (Luke) and three agents that have
worked on it:

- **Emi** — Infrastructure owner. Trading loop, executor,
  risk modules, backtests, audit log, data layer.
- **Aoi** — Trading intelligence. Regime detection,
  decision engine tuning, market reads, calling `/execute`
  with judgment.
- **Sana** — Reliability. Monitor script, alert system,
  devops.

If you're an agent picking this up, your domain is whichever
of the above matches your role. Don't cross boundaries:
strategy changes go through the trading agent,
infrastructure through the infra agent, monitoring through
the reliability agent.

### Handoff protocol

When you finish work on this project, write a short handoff
note in the team's coordination file (see Aya for the
current path). Include:
- What you changed
- What you verified (and how)
- What's still open
- Any new bugs you found but didn't fix

The owner reviews the note, the code, and the live bot
state before approving the handoff.

---

## License and ownership

Personal project. Not licensed for redistribution. All
modifications go through the owner (Luke) before commit.

For questions, ask the team coordinator (Aya) or the owner
directly. The 2026-06-02 working session with Aoi as the
primary agent produced the cascade fix + follow-on fixes
documented in `BUGS.md` and `CHANGELOG.md`. The next session
should start by reading those two files and verifying the
bot is in the "trades flowing" state described in
`CHANGELOG.md` "Verified" section.
