# Wing Trading AI — Phase 1 Infrastructure Specification

## Overview

**Project:** Wing Trading AI  
**Phase:** 1 — Infrastructure Foundation  
**Scope:** Hyperliquid-only, paper trading, async-first modular stack  
**Stack:** Python 3.11+, asyncio, FastAPI, PostgreSQL, Redis, Polars, Pydantic, structlog

---

## Architecture

```
trading_ai/
├── src/
│   ├── data/          # Market data ingestion (Hyperliquid WebSocket + REST)
│   ├── signals/       # TA/PA signal generation modules
│   ├── engine/        # Decision engine (multi-agent orchestration)
│   ├── executor/      # Paper trading executor
│   ├── risk/          # Risk management and position sizing
│   └── api/           # FastAPI endpoints + health checks
├── config/            # YAML configuration (env-specific)
├── tests/             # pytest + pytest-asyncio
├── scripts/           # Utility scripts
└── notebooks/         # Analysis notebooks
```

### Data Flow

```
Hyperliquid WebSocket/REST
        ↓
   [data/] Market Data Ingestion
        ↓
   [signals/] Signal Generation
        ↓
   [engine/] Decision Engine
        ↓
   [risk/] Risk Management
        ↓
   [executor/] Paper Trading Executor
        ↓
PostgreSQL (OHLCV, Trades, Orderbook, Journal)
        ↑
Redis (Signal Cache, Pub/Sub, Live Scores)
```

---

## Components

### 1. Data Layer (`src/data/`)

**HyperliquidWebSocket** — `hyperliquid_ws.py`
- Connects to `wss://api.hyperliquid.xyz/ws` via `websockets`
- Subscribes to: trades, candles, l2_book (orderbook), fills
- Normalizes all data to Pydantic models
- Auto-reconnect with exponential backoff
- Emits to async event bus

**HyperliquidREST** — `hyperliquid_rest.py`
- `aiohttp` client for REST fallback
- Endpoints: `/info`, `/candles`, `/trades`, `/orderbook`
- Historical OHLCV for backfill
- Rate limiting (10 req/s)

**DataNormalizer** — `normalizer.py`
- Converts Hyperliquid format → standard internal format
- Standardized OHLCV: `{symbol, timeframe, timestamp, open, high, low, close, volume}`
- Standardized Trade: `{symbol, timestamp, price, size, side, trade_id}`
- Standardized Orderbook: `{symbol, timestamp, bids[], asks[]}`

**Storage** — `storage.py`
- `asyncpg` connection pool to PostgreSQL
- TimescaleDB-style hypertables for OHLCV (1m, 5m, 15m, 1H, 4H, 1D)
- `INSERT ... ON CONFLICT DO UPDATE` for idempotency
- Trade tape and orderbook snapshot tables

### 2. Signals Layer (`src/signals/`)

**SignalRegistry** — `registry.py`
- Discovers and registers signal modules by convention
- Each signal: `{name, symbol, timeframe, direction, confidence, metadata}`
- Signals cached in Redis with TTL

**TechnicalSignals** — `technical.py`
- Trend: SMA cross, EMA cross, MACD
- Momentum: RSI, Stochastic, CCI
- Volatility: ATR, Bollinger Bands
- Volume: OBV, Volume profile

**PriceActionSignals** — `price_action.py`
- Breakout/breakdown detection
- Support/resistance detection
- Fair value gaps (order block detection)

### 3. Engine Layer (`src/engine/`)

**DecisionEngine** — `decision_engine.py`
- Consumes signals from registry
- Aggregates signal scores per symbol/timeframe
- Regime detection (trend, range, high_vol, low_liquidity)
- Outputs: `{action: BUY/SELL/HOLD, size, entry, stop, tp, confidence}`

**AgentOrchestrator** — `orchestrator.py`
- Async task scheduler
- Coordinates data → signals → decision → execution cycle
- Configurable cycle interval (default: 1 min)

### 4. Risk Layer (`src/risk/`)

**RiskManager** — `risk_manager.py`
- Pre-trade risk checks: position limits, exposure limits, drawdown
- Position sizing: fixed fractional, Kelly criterion (simplified)
- Stop-loss / take-profit validation
- Max drawdown circuit breaker

**PortfolioTracker** — `portfolio.py`
- Tracks open positions: `{symbol, side, size, entry, current, pnl, exposure}`
- Portfolio-level metrics: total equity, margin used, unrealized PnL
- Exposure limits per symbol and aggregate

### 5. Executor Layer (`src/executor/`)

**PaperExecutor** — `paper_executor.py`
- Simulates order execution against live orderbook data
- Realistic slippage: `slippage = size_bucket * base_slippage_bps`
- Fee calculation: Hyperliquid taker 0.035%, maker 0.02%
- Order types: market, limit (simulated fill at best bid/ask)
- Position tracking: open, closed, PnL realized/unrealized

**OrderRouter** — `order_router.py`
- Routes simulated orders to paper executor
- Maintains order book: `{order_id, symbol, side, size, price, status, fills[]}`

### 6. API Layer (`src/api/`)

**FastAPI App** — `main.py`
- `GET /health` — liveness probe
- `GET /ready` — readiness probe (DB + Redis connectivity)
- `GET /api/v1/positions` — open positions
- `GET /api/v1/portfolio` — portfolio summary
- `GET /api/v1/trades` — trade history
- `GET /api/v1/signals` — current signals
- `POST /api/v1/execute` — trigger manual execution (paper)

**WebSocket** — `ws.py`
- Real-time trade stream
- Real-time signal updates
- Real-time portfolio updates

### 7. Infrastructure

**Logging** — `src/utils/logging.py`
- `structlog` with JSON output
- Per-component loggers with context: `{component, correlation_id, symbol}`
- Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

**Config** — `src/utils/config.py`
- Pydantic settings from YAML
- Environment override: `config/dev.yaml`, `config/test.yaml`, `config/prod.yaml`

---

## Database Schema

### OHLCV Table
```sql
CREATE TABLE ohlcv (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC,
    high NUMERIC,
    low NUMERIC,
    close NUMERIC,
    volume NUMERIC,
    PRIMARY KEY (symbol, timeframe, timestamp)
);
SELECT create_hypertable('ohlcv', 'timestamp');
```

### Trades Table
```sql
CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    price NUMERIC NOT NULL,
    size NUMERIC NOT NULL,
    side TEXT NOT NULL,
    trade_id TEXT UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Orders Table
```sql
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size NUMERIC NOT NULL,
    price NUMERIC,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_size NUMERIC DEFAULT 0,
    avg_fill_price NUMERIC,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Trade Journal Table
```sql
CREATE TABLE trade_journal (
    id SERIAL PRIMARY KEY,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size NUMERIC NOT NULL,
    entry_price NUMERIC NOT NULL,
    exit_price NUMERIC,
    pnl NUMERIC,
    pnl_pct NUMERIC,
    fees NUMERIC,
    strategy_name TEXT,
    signal_reason JSONB,
    regime TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
```

---

## Configuration Schema

```yaml
# config/base.yaml
hyperliquid:
  ws_url: "wss://api.hyperliquid.xyz/ws"
  rest_url: "https://api.hyperliquid.xyz"
  testnet: false
  symbols:
    - "BTC"
    - "ETH"
    - "SOL"

database:
  host: "localhost"
  port: 5432
  name: "trading_ai"
  user: "postgres"
  password: "postgres"
  pool_size: 10

redis:
  host: "localhost"
  port: 6379
  db: 0

executor:
  slippage_base_bps: 1.5
  maker_fee_bps: 2.0
  taker_fee_bps: 3.5
  initial_balance: 100000.0

risk:
  max_position_pct: 0.10
  max_portfolio_exposure: 0.50
  max_drawdown_pct: 0.15
  stop_loss_pct: 0.02
  take_profit_pct: 0.04

engine:
  cycle_interval_seconds: 60
  min_signal_confidence: 0.60

logging:
  level: "INFO"
  format: "json"

# config/dev.yaml overrides
database:
  host: "localhost"
  name: "trading_ai_dev"

# config/prod.yaml overrides
logging:
  level: "WARNING"
```

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `HL_ENV` | Environment: `dev`, `test`, `prod` | `dev` |
| `HL_CONFIG_PATH` | Path to config YAML | `config/base.yaml` |
| `HL_DB_URL` | Database connection URL | Overrides YAML |
| `HL_REDIS_URL` | Redis connection URL | Overrides YAML |
| `HL_LOG_LEVEL` | Log level override | From config |

---

## Testing Strategy

- **Unit tests:** pytest + pytest-asyncio, isolated with mocks
- **Integration tests:** Real DB/Redis via Docker Compose
- **Signal tests:** Historical data replay against known market conditions
- **Executor tests:** Deterministic simulation with fixed orderbook states

---

## Deployment

### Docker Compose (Local Dev)
```yaml
services:
  postgres:
    image: timescale/timescaledb:latest-pg15
    environment:
      POSTGRES_DB: trading_ai
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  trading_ai:
    build: .
    depends_on:
      - postgres
      - redis
    environment:
      HL_ENV: dev
    volumes:
      - ./config:/app/config
```

---

## Status: Phase 1 Complete

This specification defines the complete Phase 1 infrastructure. All components are modular and replaceable. The stack is designed to support Phase 2 (multi-agent intelligence) and Phase 3 (live trading, with exchange adapter changes only).
