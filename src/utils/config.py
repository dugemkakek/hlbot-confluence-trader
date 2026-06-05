"""Configuration management via YAML + environment overrides.

Loads config from YAML files with environment-specific overrides.
All config values are typed via Pydantic for validation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class HyperliquidConfig(BaseModel):
    """Hyperliquid exchange configuration.

    The pair universe is discovered dynamically each cycle via PairDiscoverer
    (see src.signals.pair_discovery). `preload_symbols` is OPTIONAL — it lists
    pairs to subscribe to at startup so the executor has orderbook data
    immediately, before the first discovery cycle completes. Leave empty to
    rely entirely on discovery.
    """

    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    rest_url: str = "https://api.hyperliquid.xyz"
    testnet: bool = False
    # Optional startup preload (WS orderbook pre-subscription). Discovery is
    # the source of truth — these are NOT a hardcoded trading universe.
    preload_symbols: list[str] = ["BTC", "ETH", "SOL"]
    timeframes: list[str] = ["1m", "5m", "15m", "1H", "4H", "1D"]


class DatabaseConfig(BaseModel):
    """PostgreSQL database configuration."""

    host: str = "localhost"
    port: int = 5432
    name: str = "trading_ai"
    user: str = "postgres"
    password: str = "postgres"
    pool_size: int = 10
    pool_timeout: int = 30

    @property
    def dsn(self) -> str:
        """Build asyncpg DSN string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def asyncpg_dsn(self) -> str:
        """Build asyncpg-compatible DSN."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class ExchangeConfig(BaseModel):
    """Exchange adapter selection (Phase 3).

    Set `venue` to one of: hyperliquid (default), binance,
    bybit, gate, okx, paper. The factory in
    `src/exchange/factory.py` builds the corresponding adapter.

    Only `hyperliquid` and `paper` are fully implemented today.
    CEX venues raise a clear "not yet implemented" error.

    2026-06-04: added market_type / doh / leverage / margin_mode
    for CEX venues. The Binance paper-trading path reads these
    via the loaded AppConfig (see PaperExecutor).
    """

    venue: str = "hyperliquid"
    market_type: str = "usdt-m-future"
    doh: str = "system"
    leverage: int = 1
    margin_mode: str = "isolated"
    testnet: bool = False
    api_key: str | None = None
    api_secret: str | None = None


class RedisConfig(BaseModel):
    """Redis configuration."""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    max_connections: int = 20

    @property
    def url(self) -> str:
        """Build Redis URL."""
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class ExecutorConfig(BaseModel):
    """Paper trading executor configuration."""

    slippage_base_bps: float = 1.5
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 3.5
    initial_balance: float = 50.0
    order_timeout_seconds: int = 30


class RiskConfig(BaseModel):
    """Risk management configuration."""

    max_position_pct: float = 0.20
    max_portfolio_exposure: float = 0.50
    max_drawdown_pct: float = 0.15
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    max_leverage: int = 1
    max_daily_trades: int = 20
    # 2026-06-05: hard cap on the number of simultaneously-open
    # positions. The confluence engine can produce many actionable
    # signals in a single cycle, and each individual trade passed
    # the per-position + per-portfolio exposure checks — but the
    # book kept growing until exposure crossed 50%. With max_positions,
    # the book is bounded to 4 by default, which is enough room for
    # the strategy to express a view but not enough to blow up.
    max_positions: int = 4


class EngineConfig(BaseModel):
    """Decision engine configuration."""

    cycle_interval_seconds: int = 60
    min_signal_confidence: float = 0.60
    warmup_candles: int = 100


class APIConfig(BaseModel):
    """FastAPI server configuration."""

    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:3000"]
    rate_limit_per_minute: int = 60


class OrchestratorConfig(BaseModel):
    """Trading orchestrator configuration.

    Note: the orchestrator no longer accepts a hardcoded pair list. The trading
    universe is discovered dynamically each cycle from Hyperliquid's /info meta
    endpoint (see PairDiscoverer + PairRanker). The only per-cycle tunables here
    are timing and the paper-trading mode flag.
    """

    cycle_interval_seconds: int = 60
    timeframes: list[str] = ["1H"]
    dry_run: bool = True  # paper trading — no real orders


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    format: str = "json"
    output: str = "stdout"


class NarrativeConfig(BaseModel):
    """Human-readable stdout logging for live operation.

    2026-06-05: adds plain-English cycle summaries, decision
    rationale, fill/close events, regime shifts, and drawdown
    warnings. Designed for human operators watching the bot in
    a terminal — not for production log aggregation.
    """

    enabled: bool = True


class ScannerConfig(BaseModel):
    """Dynamic pair scanner configuration."""

    max_pairs_per_cycle: int = 5
    rough_filter_max: int = 10          # candles fetched for only top 10 (no candles needed for rough ranking)
    min_sz_decimals: int = 4             # szDecimals from universe as liquidity proxy
    min_confluence_score: float = 0.55
    pair_filter: list[str] = ["*"]
    exclude_coins: list[str] = ["USDC", "USDT", "FEE", "GFC", "JTO"]
    min_volume_24h_usd: float = 10000.0


class AuditConfig(BaseModel):
    """Decision audit log configuration.

    Stores the SQLite DB for the paper trading audit trail (every
    BUY/SELL/NO_TRADE decision). Separate from the main PostgreSQL
    trade_journal so audit survives DB outages and is cheap to read.
    """

    db_path: str = "data/audit.db"        # relative to HLBot/ project root
    retention_days: int = 90              # prune rows older than this


class AppConfig(BaseModel):
    """Root application configuration."""

    hyperliquid: HyperliquidConfig = Field(default_factory=HyperliquidConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    narrative: NarrativeConfig = Field(default_factory=NarrativeConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge two dicts. Override wins on conflict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(env: str | None = None) -> AppConfig:
    """Load configuration from YAML files with environment override.

    Priority (lowest to highest):
        1. base.yaml
        2. {env}.yaml (if env is set)
        3. Environment variables prefixed HL_

    Args:
        env: Environment name (dev, test, prod). Defaults to HL_ENV env var.

    Returns:
        Validated AppConfig instance.
    """
    if env is None:
        env = os.getenv("HL_ENV", "dev")

    config_root = Path(__file__).parent.parent.parent / "config"

    # Load base config
    base_path = config_root / "base.yaml"
    config_data: dict[str, Any] = {}
    if base_path.exists():
        with open(base_path) as f:
            config_data = yaml.safe_load(f) or {}

    # Load environment config
    env_path = config_root / f"{env}.yaml"
    if env_path.exists():
        with open(env_path) as f:
            env_data = yaml.safe_load(f) or {}
        config_data = _deep_merge(config_data, env_data)

    # Apply environment variable overrides (HL_SECTION_KEY=value)
    for key, value in os.environ.items():
        if not key.startswith("HL_"):
            continue
        parts = key[3:].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, subkey = parts
        if section in config_data and isinstance(config_data[section], dict):
            # Attempt type coercion
            current = config_data[section].get(subkey)
            if current is not None:
                if isinstance(current, bool):
                    value = value.lower() in ("true", "1", "yes")
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
            config_data[section][subkey] = value

    return AppConfig.model_validate(config_data)


# Global config instance (lazy-loaded)
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Get the global config instance (loads once, caches)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
