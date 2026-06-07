"""Paper Trading Executor — simulates market orders with realistic slippage.

Tracks positions and portfolio, logs all trades to PostgreSQL trade_journal,
reads live orderbook from Hyperliquid WebSocket.

Architecture:
    Decision Engine → Signal → PaperExecutor.place_order() → PostgreSQL + Redis
                                             ↓
                               HyperliquidWS (live orderbook prices)
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    import redis.asyncio as redis
except ImportError:  # Redis is optional — executor runs without it
    redis = None  # type: ignore[assignment]

from ..utils.logging import get_logger
from ..utils.config import get_config, AppConfig
from ..audit import AuditEntryInput, get_audit_logger
from ..audit.reason_codes import NoTradeReason, classify_no_trade_reason
from ..data.models import (
    OrderSide,
    OrderType,
    OrderStatus,
    Position,
    PortfolioSummary,
    SimulatedOrder,
    OrderbookLevel,
    OrderbookSnapshot,
)
from ..data.hyperliquid_ws import HyperliquidWebSocket, WSMessage

logger = get_logger(__name__)


@dataclass
class OrderResult:
    """Result of an order operation."""

    success: bool
    order: SimulatedOrder | None = None
    error: str | None = None
    fill_price: float | None = None


@dataclass
class CancelResult:
    """Result of a cancel operation."""

    success: bool
    order_id: str
    error: str | None = None


class PaperExecutor:
    """Paper trading executor.

    Simulates market and limit orders against live Hyperliquid orderbook data
    with realistic slippage, tracks open positions and portfolio equity,
    logs all trades to PostgreSQL, and emits events to Redis pub/sub.

    Slippage model
    --------------
    slippage_bps = base_slippage_bps × sqrt(order_size_usd / 10_000)
    Capped at 5× base for very large orders.

    Order execution
    ---------------
    MARKET BUY  → fill at best_ask  × (1 + slippage_bps / 10_000)
    MARKET SELL → fill at best_bid  × (1 - slippage_bps / 10_000)
    LIMIT BUY   → fill at min(limit_price, best_ask)
    LIMIT SELL  → fill at max(limit_price, best_bid)

    Fees (applied on fill)
    ----------------------
    Maker: executor.maker_fee_bps  (default 2.0 bps = 0.02%)
    Taker: executor.taker_fee_bps  (default 3.5 bps = 0.035%)
    """

    def __init__(
        self,
        initial_balance: float | None = None,
        config: AppConfig | None = None,
    ) -> None:
        """Initialize the executor.

        Args:
            initial_balance: Starting cash balance. Defaults to config value.
            config: Application config. Defaults to global config.
        """
        self.cfg = config or get_config()
        self._initial_balance = (
            initial_balance
            if initial_balance is not None
            else self.cfg.executor.initial_balance
        )

        # Slippage & fee params
        self._slippage_base_bps: float = self.cfg.executor.slippage_base_bps
        self._maker_fee_bps: float = self.cfg.executor.maker_fee_bps
        self._taker_fee_bps: float = self.cfg.executor.taker_fee_bps

        # Risk params
        self._max_portfolio_exposure: float = self.cfg.risk.max_portfolio_exposure

        # Connections (set in connect())
        self._ws: HyperliquidWebSocket | None = None
        self._db: Database | None = None
        self._redis: redis.Redis | None = None

        # 2026-06-04: Binance paper-trading support. When the
        # configured exchange venue is binance, we poll the public
        # ccxt REST endpoint for orderbook data instead of the
        # Hyperliquid WebSocket. Symbol mapping: Hyperliquid's
        # "BTC" maps to Binance's "BTC/USDT" (etc.). We keep
        # `self._orderbooks` keyed by the *Hyperliquid* symbol so
        # the rest of the executor (and the orchestrator's symbol
        # flow) is unchanged.
        self._venue: str = "hyperliquid"
        try:
            exch = getattr(self.cfg, "exchange", None)
            if exch is not None:
                self._venue = getattr(exch, "venue", "hyperliquid")
        except Exception:
            self._venue = "hyperliquid"
        self._binance_client: Any = None
        self._binance_subscribed: set[str] = set()
        self._binance_poll_task: asyncio.Task | None = None
        self._binance_poll_interval: float = 2.0  # seconds

        # 2026-06-04: shared CEX poller state. Same shape as
        # the Binance one above, used for any non-Hyperliquid
        # venue. Symbol mapping is venue-specific and resolved
        # per venue.
        self._cex_client: Any = None
        self._cex_subscribed: set[str] = set()
        self._cex_poll_task: asyncio.Task | None = None
        self._cex_venue_kind: str = ""  # e.g. "okx", "gate", "binance"

        # Live orderbook state keyed by symbol — use OrderbookSnapshot for best bid/ask
        self._orderbooks: dict[str, OrderbookSnapshot] = {}

        # In-memory state
        self._cash: float = self._initial_balance
        self._realized_pnl: float = 0.0
        self._positions: dict[str, Position] = {}  # key = symbol
        self._orders: dict[str, SimulatedOrder] = {}  # key = order_id
        self._pending_limit_orders: dict[str, asyncio.Task] = {}

        # 2026-06-07 (v0.2.7): equity curve for Sharpe/MDD continuity.
        # Capped at 10,000 points (~3.5 days at 30s cycles). On overflow,
        # drop the oldest 10% in one batch to amortize the resize cost.
        self._equity_curve: list[dict[str, Any]] = []
        self._equity_curve_max: int = 10_000

        # Lock to prevent concurrent order modifications
        self._lock = asyncio.Lock()

        # Track daily trade count for risk limit
        self._daily_trade_count: int = 0
        self._daily_reset_at: datetime | None = None

        logger.info(
            "PaperExecutor initialised",
            initial_balance=self._initial_balance,
            slippage_base_bps=self._slippage_base_bps,
            maker_fee_bps=self._maker_fee_bps,
            taker_fee_bps=self._taker_fee_bps,
        )

        # 2026-06-04: install DoH resolver for blocked crypto
        # hostnames. Some networks (e.g. captive-portal ISPs)
        # intercept DNS for exchanges like api.binance.com.
        # The DoH patch makes socket.getaddrinfo go through
        # Cloudflare/Google for the blocked hosts, so ccxt +
        # aiohttp can reach them even when the system DNS is
        # compromised.
        try:
            from ..utils.doh import install_doh_resolver
            exch = getattr(self.cfg, "exchange", None)
            doh = getattr(exch, "doh", "system") if exch else "system"
            if doh in ("cloudflare", "google"):
                install_doh_resolver(doh)
        except Exception as exc:
            logger.debug("DoH install skipped", error=str(exc))

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Hyperliquid WebSocket, PostgreSQL, and Redis.

        PostgreSQL and Redis are optional — executor runs in memory-only mode
        if they are unavailable (useful for local dev / API-only usage).
        """
        logger.info("Connecting PaperExecutor services")

        # ── PostgreSQL (optional) ───────────────────────────────────────────────
        try:
            self._db = Database()
            await self._db.connect()
        except Exception as exc:
            logger.warning("PostgreSQL unavailable — running without DB", error=str(exc))
            self._db = None

        # ── Redis (optional) ───────────────────────────────────────────────────
        if redis is None:
            logger.warning("redis package not installed — running without Redis")
            self._redis = None
        else:
            try:
                self._redis = redis.Redis(
                    host=self.cfg.redis.host,
                    port=self.cfg.redis.port,
                    db=self.cfg.redis.db,
                    password=self.cfg.redis.password,
                    decode_responses=True,
                    max_connections=self.cfg.redis.max_connections,
                )
                await asyncio.wait_for(self._redis.ping(), timeout=5.0)
                logger.info("Redis connected")
            except Exception as exc:
                logger.warning("Redis unavailable — running without Redis", error=str(exc))
                self._redis = None

        # ── WebSocket (required for live prices) ───────────────────────────────
        if self._venue in ("binance", "okx", "gate", "bybit"):
            # CEX paper mode: public REST polling, no auth needed.
            # No API keys = paper mode (synthetic fills from last
            # ticker). We poll the venue's L2 orderbook endpoint
            # via ccxt. No orders are placed.
            self._cex_venue_kind = self._venue
            doh = "system"
            try:
                exch = getattr(self.cfg, "exchange", None)
                if exch is not None:
                    doh = getattr(exch, "doh", "system")
            except Exception:
                pass
            try:
                import ccxt.async_support as ccxt_async
                ccxt_class = getattr(ccxt_async, self._venue)
                opts: dict[str, Any] = {
                    "enableRateLimit": True,
                    "options": {},
                }
                # Per-venue default type for ccxt
                if self._venue == "binance":
                    opts["options"]["defaultType"] = "future"
                elif self._venue == "okx":
                    opts["defaultType"] = "swap"
                elif self._venue == "gate":
                    opts["defaultType"] = "spot"
                # elif bybit: defaultType=linear
                if doh in ("cloudflare", "google"):
                    try:
                        from aiohttp.resolver import AsyncResolver
                        nameservers = {
                            "cloudflare": ["1.1.1.1", "1.0.0.1"],
                            "google": ["8.8.8.8", "8.8.4.4"],
                        }[doh]
                        def _factory(loop=None):
                            return AsyncResolver(nameservers=nameservers, loop=loop)
                        opts["aiohttp_trust_env"] = False
                        opts["connector_args"] = {"resolver_factory": _factory}
                        logger.info(f"{self._venue} using DoH", provider=doh)
                    except Exception as exc:
                        logger.warning("DoH setup failed, falling back to system DNS", error=str(exc))
                self._cex_client = ccxt_class(opts)
                self._binance_client = self._cex_client  # legacy alias
                # Smoke test the connection
                smoke_symbol = self._cex_smoke_symbol()
                if smoke_symbol:
                    await self._cex_client.fetch_ticker(smoke_symbol)
                self._cex_poll_task = asyncio.create_task(self._cex_poll_loop())
                self._binance_poll_task = self._cex_poll_task  # legacy alias
                logger.info(
                    f"{self._venue} paper mode connected",
                    note="polling public orderbook data; no orders placed",
                )
            except Exception as exc:
                logger.warning(
                    f"{self._venue} connect failed — running without orderbook data",
                    error=str(exc),
                )
                self._cex_client = None
                self._binance_client = None
            return

        try:
            self._ws = HyperliquidWebSocket()
            await self._ws.connect()

            # Subscribe to orderbook for the optional startup preload list.
            # This is just to have data ready before the first discovery cycle
            # completes — the orchestrator's Phase 4b call to
            # subscribe_orderbooks() handles all dynamic universe coverage.
            for symbol in self.cfg.hyperliquid.preload_symbols:
                await self._ws.subscribe_orderbook(symbol)

            # Register a single handler that dispatches to per-symbol caching.
            # Symbol extraction must handle the actual Hyperliquid shape:
            # data messages arrive as {"channel":"l2Book","data":{"coin":"EIGEN","levels":[[],[]]}}
            # so we read raw.data.coin. The earlier version only checked raw.data.symbol
            # (which doesn't exist on l2Book data messages) and fell through to
            # raw.subscription.coin (only present on subscription confirmations).
            # Both checks returned "" → dispatcher bailed → no orderbook data cached.
            # 2026-06-02.
            async def orderbook_dispatcher(msg: WSMessage) -> None:
                raw = msg.raw or {}
                inner = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
                symbol = (
                    inner.get("coin")
                    or inner.get("symbol")
                    or raw.get("subscription", {}).get("coin", "")
                )
                if not symbol:
                    return
                await self._handle_orderbook_update(symbol, msg.data)

            self._ws._handlers.setdefault("l2Book", []).append(orderbook_dispatcher)
            logger.info(
                "WebSocket connected",
                preload_symbols=self.cfg.hyperliquid.preload_symbols,
                note="Dynamic discovery will add more symbols on the first cycle",
            )
        except Exception as exc:
            logger.warning("WebSocket unavailable — running in offline mode", error=str(exc))
            self._ws = None

        logger.info("PaperExecutor initialised (partial mode if services missing)")

    async def _handle_orderbook_update(self, symbol: str, data: dict[str, Any]) -> None:
        """Normalize and cache an incoming orderbook update."""
        if self._ws is None:
            return
        normalized = self._ws.normalize_orderbook(data, symbol)
        if normalized is None:
            return

        # Convert NormalizedOrderbook (list-of-dicts style) to OrderbookSnapshot.
        # Skip empty updates (Hyperliquid sends a heartbeat-shaped first message
        # with levels=[[],[]] after each subscribe; overwriting the real snapshot
        # with an empty one starves place_order). 2026-06-02.
        bid_levels = data.get("levels", [[]])[0]
        ask_levels = data.get("levels", [[]])[1]
        if not bid_levels and not ask_levels:
            return
        bids = [
            OrderbookLevel(price=float(l["px"]), size=float(l["n"]))
            for l in bid_levels
        ]
        asks = [
            OrderbookLevel(price=float(l["px"]), size=float(l["n"]))
            for l in ask_levels
        ]
        self._orderbooks[symbol] = OrderbookSnapshot(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(timezone.utc),
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect all services."""
        logger.info("Disconnecting PaperExecutor")

        # Cancel all pending limit order tasks
        for task in self._pending_limit_orders.values():
            task.cancel()
        self._pending_limit_orders.clear()

        # 2026-06-04: stop the CEX poller if running (covers
        # Binance, OKX, Gate, Bybit — all share the same poll
        # loop and client)
        if self._binance_poll_task is not None:
            self._binance_poll_task.cancel()
            try:
                await self._binance_poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._binance_poll_task = None
        if self._binance_client is not None:
            try:
                await self._binance_client.close()
            except Exception:
                pass
            self._binance_client = None
        # Clear the new CEX state aliases too
        self._cex_poll_task = None
        self._cex_client = None
        self._cex_subscribed = set()
        self._cex_venue_kind = ""

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._db:
            await self._db.close()
            self._db = None

        if self._redis:
            await self._redis.close()
            self._redis = None

        logger.info("PaperExecutor disconnected")

    # ------------------------------------------------------------------
    # Orderbook subscription (dynamic — called each cycle)
    # ------------------------------------------------------------------

    async def subscribe_orderbooks(self, symbols: list[str]) -> None:
        """Subscribe to L2 orderbook for a list of symbols.

        Safe to call every cycle — WS layer skips already-subscribed symbols.
        Without this, order execution fails for any symbol not in the hardcoded
        config list with "No orderbook data available".
        """
        if self._venue in ("binance", "okx", "gate", "bybit"):
            # The polling loop picks up new symbols on its next tick.
            new = [s for s in symbols if s not in self._cex_subscribed]
            if new:
                self._cex_subscribed.update(new)
                # Legacy alias for code that still references it
                self._binance_subscribed.update(new)
                logger.debug(
                    f"{self._venue} orderbook subscription updated",
                    new=new, total=len(self._cex_subscribed),
                )
            return
        if not self._ws:
            return
        for symbol in symbols:
            try:
                await self._ws.subscribe_orderbook(symbol)
            except Exception as exc:
                logger.debug("orderbook subscription skipped", symbol=symbol, error=str(exc))

    @staticmethod
    def _binance_symbol(hl_symbol: str) -> str:
        """Map Hyperliquid symbol (e.g. 'BTC') to Binance ccxt symbol ('BTC/USDT').

        Stablecoins and quote-only symbols pass through unchanged.
        """
        if "/" in hl_symbol:
            return hl_symbol
        # Skip if already looks like a USDT pair or quote is something else
        if hl_symbol.endswith("USDT") or hl_symbol.endswith("USDC"):
            return hl_symbol
        # Most perp symbols on Binance USDT-M are "BASE/USDT"
        return f"{hl_symbol}/USDT"

    @staticmethod
    def _okx_symbol(hl_symbol: str) -> str:
        """Map Hyperliquid symbol to OKX ccxt symbol (e.g. 'BTC-USDT').

        OKX uses hyphenated symbols. We default to SWAP (linear perp)
        since that's what our strategy trades. Stablecoins pass through.
        """
        if "-" in hl_symbol:
            return hl_symbol
        if hl_symbol.endswith("USDT") or hl_symbol.endswith("USDC"):
            return hl_symbol
        return f"{hl_symbol}-USDT"

    @staticmethod
    def _gate_symbol(hl_symbol: str) -> str:
        """Map Hyperliquid symbol to Gate.io ccxt symbol (e.g. 'BTC_USDT').

        Gate.io uses underscored symbols. Default to USDT pairs.
        """
        if "_" in hl_symbol:
            return hl_symbol
        if hl_symbol.endswith("USDT") or hl_symbol.endswith("USDC"):
            return hl_symbol
        return f"{hl_symbol}_USDT"

    def _cex_symbol(self, hl_symbol: str) -> str:
        """Dispatch to the venue-specific symbol mapper."""
        if self._venue == "okx":
            return self._okx_symbol(hl_symbol)
        if self._venue == "gate":
            return self._gate_symbol(hl_symbol)
        return self._binance_symbol(hl_symbol)  # binance + bybit fallback

    def _cex_smoke_symbol(self) -> str:
        """Return a stable symbol to use for the post-connect smoke test."""
        if self._venue == "okx":
            return "BTC-USDT"
        if self._venue == "gate":
            return "BTC_USDT"
        if self._venue == "bybit":
            return "BTC/USDT"
        return "BTC/USDT"  # binance default

    async def _binance_poll_loop(self) -> None:
        """Background task: poll CEX L2 orderbook for each subscribed symbol.

        Runs every `self._binance_poll_interval` seconds. Updates
        `self._orderbooks` keyed by the Hyperliquid symbol so the
        rest of the executor (and the orchestrator) is unchanged.

        The loop body is venue-agnostic: it reads from
        `self._cex_client` and dispatches symbol mapping through
        `self._cex_symbol`. Older code (and references to
        `_binance_poll_loop` / `_binance_poll_once` / `_binance_subscribed`)
        remain valid via the `self._binance_*` aliases set in
        `connect()`.
        """
        while True:
            try:
                await self._cex_poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"{self._venue} poll loop error", error=str(exc))
            await asyncio.sleep(self._binance_poll_interval)

    async def _cex_poll_once(self) -> None:
        if not self._cex_client or not self._cex_subscribed:
            return
        # Snapshot subscribed set so concurrent mutations don't blow up
        symbols = list(self._cex_subscribed)
        for hl_symbol in symbols:
            try:
                venue_sym = self._cex_symbol(hl_symbol)
                ob = await self._cex_client.fetch_order_book(venue_sym, limit=20)
            except Exception as exc:
                logger.debug(f"{self._venue} orderbook fetch failed", symbol=hl_symbol, error=str(exc))
                continue
            bids_raw = ob.get("bids") or []
            asks_raw = ob.get("asks") or []
            if not bids_raw or not asks_raw:
                continue
            bids = [
                OrderbookLevel(price=float(p), size=float(s))
                for p, s in bids_raw[:20]
            ]
            asks = [
                OrderbookLevel(price=float(p), size=float(s))
                for p, s in asks_raw[:20]
            ]
            self._orderbooks[hl_symbol] = OrderbookSnapshot(
                symbol=hl_symbol,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(timezone.utc),
            )

            # 2026-06-05: refresh the position's current_price from
            # the freshest orderbook so uPnL tracks reality. Without
            # this, current_price freezes at the last fill price
            # until the next fill on the same symbol, which means
            # SL/TP checks against stale data.
            if hl_symbol in self._positions and bids and asks:
                mid = (bids[0].price + asks[0].price) / 2
                self._refresh_unrealized_pnl(mid, hl_symbol)

    # legacy aliases used by old call sites / external tests
    _binance_poll_once = _cex_poll_once
    _cex_poll_loop = _binance_poll_loop

    async def _binance_poll_loop(self) -> None:
        """Background task: poll Binance L2 orderbook for each subscribed symbol.

        Runs every `self._binance_poll_interval` seconds. Updates
        `self._orderbooks` keyed by the Hyperliquid symbol so the
        rest of the executor (and the orchestrator) is unchanged.
        """
        while True:
            try:
                await self._binance_poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Binance poll loop error", error=str(exc))
            await asyncio.sleep(self._binance_poll_interval)

    async def _binance_poll_once(self) -> None:
        if not self._binance_client or not self._binance_subscribed:
            return
        # Snapshot subscribed set so concurrent mutations don't blow up
        symbols = list(self._binance_subscribed)
        for hl_symbol in symbols:
            try:
                binance_sym = self._binance_symbol(hl_symbol)
                ob = await self._binance_client.fetch_order_book(binance_sym, limit=20)
            except Exception as exc:
                logger.debug("Binance orderbook fetch failed", symbol=hl_symbol, error=str(exc))
                continue
            bids_raw = ob.get("bids") or []
            asks_raw = ob.get("asks") or []
            if not bids_raw or not asks_raw:
                continue
            bids = [
                OrderbookLevel(price=float(p), size=float(s))
                for p, s in bids_raw[:20]
            ]
            asks = [
                OrderbookLevel(price=float(p), size=float(s))
                for p, s in asks_raw[:20]
            ]
            self._orderbooks[hl_symbol] = OrderbookSnapshot(
                symbol=hl_symbol,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(timezone.utc),
            )

            # 2026-06-05: refresh the position's current_price from
            # the freshest orderbook so uPnL tracks reality. Without
            # this, current_price freezes at the last fill price
            # until the next fill on the same symbol, which means
            # SL/TP checks against stale data.
            if hl_symbol in self._positions and bids and asks:
                mid = (bids[0].price + asks[0].price) / 2
                self._refresh_unrealized_pnl(mid, hl_symbol)

    def get_orderbook(self, symbol: str) -> OrderbookSnapshot | None:
        """Return cached orderbook snapshot for a symbol, or None."""
        return self._orderbooks.get(symbol)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        strategy_name: str | None = None,
        signal_reason: dict[str, Any] | None = None,
        regime: str | None = None,
        position_metadata: dict[str, Any] | None = None,
    ) -> OrderResult:
        """Place a paper order (market or limit).

        Args:
            symbol: Trading pair, e.g. "BTC".
            side: LONG or SHORT.
            size: 2026-06-06 (v0.2.5) — Order size in **base currency
                units** (e.g. BTC for BTC/USDT), NOT quote currency
                (USD). The orchestrator computes this as
                `capped_notional / decision.entry` (see
                `trading_loop._execute_decision`), where
                `capped_notional` is USD and `decision.entry` is
                USD-per-base. The resulting size is base units. The
                pre-v0.2.5 docstring said "quote currency (USD)"
                which was misleading and led to a v0.2.4 CHANGELOG
                note claiming the PnL math was 100x off — the math
                is in fact correct given base-unit size semantics.
                Verified against the live 2026-06-06 production run
                (0.675 AR @ $1.85 = $1.25 notional).
            order_type: MARKET or LIMIT.
            limit_price: Price for limit orders.
            strategy_name: Optional strategy label for the journal.
            signal_reason: Optional signal metadata for the journal.
            regime: Optional market regime label.
            position_metadata: 2026-06-06 (v0.2.3) — metadata written
                into the new/updated Position (e.g. `entry_confluence`).
                Default None leaves Position.metadata at its default {}.

        Returns:
            OrderResult with the filled SimulatedOrder or error details.
        """
        async with self._lock:
            try:
                return await self._execute_order(
                    symbol,
                    side,
                    size,
                    order_type,
                    limit_price,
                    strategy_name,
                    signal_reason,
                    regime,
                    position_metadata,
                )
            except Exception as e:
                logger.error("place_order unexpected error", symbol=symbol, error=str(e))
                return OrderResult(success=False, error=str(e))

    async def _execute_order(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        order_type: OrderType,
        limit_price: float | None,
        strategy_name: str | None,
        signal_reason: dict[str, Any] | None,
        regime: str | None,
        position_metadata: dict[str, Any] | None = None,
    ) -> OrderResult:
        """Internal order execution with full error handling."""
        # --- Risk checks ---
        portfolio = self.get_portfolio()
        if portfolio.exposure_pct >= self._max_portfolio_exposure:
            self._audit_no_trade(
                symbol=symbol,
                reason=(
                    f"Max portfolio exposure reached ({portfolio.exposure_pct:.1%})"
                ),
                regime=regime,
                metadata={"strategy_name": strategy_name},
            )
            return OrderResult(
                success=False,
                error=f"Max portfolio exposure reached ({portfolio.exposure_pct:.1%})",
            )

        if self._daily_trade_count >= self.cfg.risk.max_daily_trades:
            self._audit_no_trade(
                symbol=symbol,
                reason=f"Daily trade limit reached ({self._daily_trade_count})",
                regime=regime,
                metadata={
                    "strategy_name": strategy_name,
                    "daily_trade_count": self._daily_trade_count,
                },
            )
            return OrderResult(
                success=False,
                error=f"Daily trade limit reached ({self._daily_trade_count})",
            )

        # --- Get current orderbook ---
        ob = self._orderbooks.get(symbol)
        if not ob or ob.best_bid is None or ob.best_ask is None:
            self._audit_no_trade(
                symbol=symbol,
                reason=f"No orderbook data available for {symbol}",
                regime=regime,
                metadata={"strategy_name": strategy_name},
            )
            return OrderResult(
                success=False,
                error=f"No orderbook data available for {symbol}",
            )

        # --- Determine fill price ---
        order_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        if order_type == OrderType.MARKET:
            fill_price, slippage_bps = self._market_fill_price(side, ob, size)
            fee_bps = self._taker_fee_bps
        elif order_type == OrderType.LIMIT:
            if limit_price is None:
                return OrderResult(success=False, error="limit_price required for LIMIT order")
            fill_price, slippage_bps = self._limit_fill_price(side, ob, limit_price)
            fee_bps = self._maker_fee_bps
        else:
            return OrderResult(success=False, error=f"Unsupported order type: {order_type}")

        filled_size = size  # paper executor fills fully immediately
        notional = fill_price * filled_size
        fee_cost = notional * (fee_bps / 10_000)

        # Update cash
        if side == OrderSide.LONG:
            self._cash -= notional + fee_cost
        else:  # SHORT
            self._cash += notional - fee_cost

        # Create order record
        order = SimulatedOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            size=filled_size,
            price=fill_price,
            order_type=order_type,
            status=OrderStatus.FILLED,
            filled_size=filled_size,
            avg_fill_price=fill_price,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
            created_at=now,
            updated_at=now,
            metadata={
                "strategy_name": strategy_name,
                "signal_reason": signal_reason,
                "regime": regime,
            },
        )
        self._orders[order_id] = order

        # Update position. position_metadata is plumbed through so the
        # orchestrator can attach entry-time signals (e.g. entry_confluence)
        # that survive the per-tick `_refresh_unrealized_pnl` reconstructions.
        self._update_position(symbol, side, filled_size, fill_price, position_metadata)

        # --- Log to PostgreSQL ---
        await self._log_trade_journal(order, filled_size, fill_price, fee_cost)

        # --- Publish to Redis ---
        await self._publish_order_event(order)

        self._daily_trade_count += 1
        self._reset_daily_counter_if_needed()

        # --- Audit log: filled order row ---
        # Compute SL/TP from fill price so the audit row shows the executed plan.
        try:
            sl_pct = self.cfg.risk.stop_loss_pct
            tp_pct = self.cfg.risk.take_profit_pct
            if side == OrderSide.LONG:
                stop_loss = fill_price * (1 - sl_pct)
                take_profit = fill_price * (1 + tp_pct)
            else:
                stop_loss = fill_price * (1 + sl_pct)
                take_profit = fill_price * (1 - tp_pct)

            self._audit_trade_filled(
                symbol=symbol,
                side=side,
                order_id=order_id,
                entry_price=fill_price,
                size=filled_size,
                stop_loss=stop_loss,
                take_profit=take_profit,
                regime=regime,
                strategy_name=strategy_name,
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
                fee_cost=fee_cost,
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist trade audit row",
                order_id=order_id,
                error=str(exc),
            )

        logger.info(
            "Order filled",
            order_id=order_id,
            symbol=symbol,
            side=side.value,
            size=filled_size,
            fill_price=fill_price,
            slippage_bps=slippage_bps,
            fee_cost=fee_cost,
        )
        return OrderResult(success=True, order=order, fill_price=fill_price)

    def _market_fill_price(
        self, side: OrderSide, ob: OrderbookSnapshot, size: float
    ) -> tuple[float, float]:
        """Calculate market order fill price with slippage."""
        slippage_bps = self._calculate_slippage(size)
        if side == OrderSide.LONG:
            # Aggressive buy — cross the spread to best ask
            fill_price = ob.best_ask * (1 + slippage_bps / 10_000)
        else:
            # Aggressive sell — cross the spread to best bid
            fill_price = ob.best_bid * (1 - slippage_bps / 10_000)
        return fill_price, slippage_bps

    def _limit_fill_price(
        self, side: OrderSide, ob: OrderbookSnapshot, limit_price: float
    ) -> tuple[float, float]:
        """Calculate limit order fill price (optimistic fill against book)."""
        if side == OrderSide.LONG:
            # Buy: fill at the lower of limit_price or best_ask
            fill_price = min(limit_price, ob.best_ask or limit_price)
        else:
            # Sell: fill at the higher of limit_price or best_bid
            fill_price = max(limit_price, ob.best_bid or limit_price)

        # Slippage is distance from mid to fill price in bps
        mid = (ob.best_bid + ob.best_ask) / 2 if ob.best_bid and ob.best_ask else limit_price
        slippage_bps = abs(fill_price - mid) / mid * 10_000 if mid > 0 else 0.0
        return fill_price, slippage_bps

    def _calculate_slippage(self, size_usd: float) -> float:
        """Calculate slippage in basis points based on order size.

        slippage = base_slippage × sqrt(size_usd / 10_000)
        Capped at 5× base for very large orders.
        """
        raw = self._slippage_base_bps * math.sqrt(size_usd / 10_000)
        return min(raw, self._slippage_base_bps * 5)

    def _update_position(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        fill_price: float,
        position_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update or create a position after a fill.

        position_metadata (2026-06-06 v0.2.3): free-form dict stored on the
        Position. The orchestrator passes `entry_confluence` here so
        `_rescore_open_positions` can compute the confluence-drop alert
        without re-querying the decision log. For a NEW position the
        metadata is used verbatim. For a SAME-DIRECTION average-in, the
        existing metadata is preserved (entry signal is the same trade).
        For an OPPOSITE-SIDE flip/close, the residual position's
        metadata is reset to position_metadata (the new entry).
        """
        existing = self._positions.get(symbol)
        new_meta = position_metadata or {}

        if existing is None:
            # New position
            self._positions[symbol] = Position(
                symbol=symbol,
                side=side,
                size=size,
                entry_price=fill_price,
                current_price=fill_price,
                unrealized_pnl=0.0,
                unrealized_pnl_pct=0.0,
                exposure=size * fill_price,
                created_at=datetime.now(timezone.utc),
                metadata=dict(new_meta),
            )

        elif existing.side == side:
            # Averaging into same direction — weighted average entry price
            total_size = existing.size + size
            new_entry = (
                existing.entry_price * existing.size + fill_price * size
            ) / total_size
            exposure = total_size * fill_price
            self._positions[symbol] = Position(
                symbol=symbol,
                side=side,
                size=total_size,
                entry_price=new_entry,
                current_price=fill_price,
                unrealized_pnl=0.0,
                unrealized_pnl_pct=0.0,
                exposure=exposure,
                created_at=existing.created_at,
                # Same-direction average-in: preserve original entry
                # metadata. If new keys were passed, merge them on top.
                metadata={**existing.metadata, **new_meta},
            )

        else:
            # Opposite side — reduce or close position.
            # 2026-06-06 (v0.2.5): size is in BASE units (e.g. BTC),
            # not USD notional. The PnL math
            # `size * (fill - entry)` is correct given base-unit
            # size — it yields USD PnL directly. The v0.2.4
            # CHANGELOG noted a 100x PnL discrepancy that turned
            # out to be a docstring confusion in `place_order` (the
            # docstring said "quote currency (USD)" but the
            # orchestrator passes base units). The math was right
            # all along.
            if size >= existing.size:
                # Fully or partially close; residual becomes new position in opposite direction
                residual = size - existing.size

                # Realized PnL from closing existing position
                if existing.side == OrderSide.LONG:
                    pnl = existing.size * (fill_price - existing.entry_price)
                else:
                    pnl = existing.size * (existing.entry_price - fill_price)
                self._realized_pnl += pnl

                if residual > 0:
                    # Residual opens new position in opposite direction.
                    # 2026-06-06 (v0.2.4): the residual side inherits the
                    # NEW order's direction. The previous logic
                    # (`new_side = OrderSide.SHORT if side == LONG else
                    # LONG`) inverted it, so a SHORT that flipped a LONG
                    # ended up as a LONG residual. Pre-existing bug,
                    # surfaced while writing the v0.2.3 flip-path tests.
                    new_side = side
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        side=new_side,
                        size=residual,
                        entry_price=fill_price,
                        current_price=fill_price,
                        unrealized_pnl=0.0,
                        unrealized_pnl_pct=0.0,
                        exposure=residual * fill_price,
                        created_at=datetime.now(timezone.utc),
                        # Flipped into new direction — entry signal is the
                        # new trade. Drop the old entry's metadata; the
                        # new metadata is the entry confluence for the flip.
                        metadata=dict(new_meta),
                    )
                else:
                    # Exact close — position fully removed
                    del self._positions[symbol]
            else:
                # Partial close — reduce size of existing position.
                # PnL math: `size * (fill - entry)` where `size` is
                # the partial-close size in BASE units. See v0.2.5
                # comment in the opposite-side branch above.
                if existing.side == OrderSide.LONG:
                    pnl = size * (fill_price - existing.entry_price)
                else:
                    pnl = size * (existing.entry_price - fill_price)
                self._realized_pnl += pnl

                remaining_size = existing.size - size
                self._positions[symbol] = Position(
                    symbol=symbol,
                    side=existing.side,
                    size=remaining_size,
                    entry_price=existing.entry_price,
                    current_price=fill_price,
                    unrealized_pnl=0.0,
                    unrealized_pnl_pct=0.0,
                    exposure=remaining_size * fill_price,
                    created_at=existing.created_at,
                    # Partial close of same-direction: entry signal is
                    # the same trade — preserve + merge any new keys.
                    metadata={**existing.metadata, **new_meta},
                )

        # Recalculate unrealized PnL for this symbol using last fill price
        self._refresh_unrealized_pnl(fill_price, symbol)

    def _refresh_unrealized_pnl(self, last_price: float, symbol: str | None = None) -> None:
        """Recalculate unrealized PnL for all positions or just one symbol."""
        symbols = [symbol] if symbol else list(self._positions.keys())
        for sym in symbols:
            pos = self._positions.get(sym)
            if not pos:
                continue
            price = last_price if sym == symbol else pos.current_price
            if pos.side == OrderSide.LONG:
                unrealized = pos.size * (price - pos.entry_price)
            else:
                unrealized = pos.size * (pos.entry_price - price)
            unrealized_pct = (
                (unrealized / (pos.size * pos.entry_price)) * 100
                if pos.entry_price > 0 and pos.size > 0
                else 0.0
            )
            self._positions[sym] = Position(
                symbol=pos.symbol,
                side=pos.side,
                size=pos.size,
                entry_price=pos.entry_price,
                current_price=price,
                unrealized_pnl=unrealized,
                unrealized_pnl_pct=unrealized_pct,
                exposure=pos.size * price,
                created_at=pos.created_at,
                # Preserve metadata across price-tick reconstructions.
                # Without this, entry_confluence and any other entry-time
                # signals would be wiped on every tick (v0.2.3 fix).
                metadata=dict(pos.metadata),
            )

    async def _log_trade_journal(
        self,
        order: SimulatedOrder,
        filled_size: float,
        fill_price: float,
        fee_cost: float,
    ) -> None:
        """Write a filled order to the PostgreSQL trade_journal."""
        if not self._db:
            logger.warning("Database not connected, skipping trade journal log")
            return

        pos = self._positions.get(order.symbol)
        pnl = 0.0
        pnl_pct = 0.0
        if pos and pos.size > 0:
            if pos.side == OrderSide.LONG:
                pnl = pos.size * (pos.current_price - pos.entry_price)
            else:
                pnl = pos.size * (pos.entry_price - pos.current_price)
            if pos.size * pos.entry_price > 0:
                pnl_pct = (pnl / (pos.size * pos.entry_price)) * 100

        sql = """
        INSERT INTO trade_journal
            (order_id, symbol, side, size, entry_price, exit_price, pnl, pnl_pct,
             fees, strategy_name, signal_reason, regime, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        """
        try:
            if self._db:
                async with self._db.acquire() as conn:
                    await conn.execute(
                        sql,
                        order.order_id,
                        order.symbol,
                        order.side.value,
                        filled_size,
                        fill_price,
                        None,  # exit_price set on close
                        pnl,
                        pnl_pct,
                        fee_cost,
                        order.metadata.get("strategy_name"),
                        json.dumps(order.metadata.get("signal_reason")),
                        order.metadata.get("regime"),
                        order.created_at,
                    )
            else:
                logger.debug("DB unavailable, skipping trade journal write", order_id=order.order_id)
        except Exception as e:
            logger.error(
                "Failed to log trade journal",
                order_id=order.order_id,
                error=str(e),
            )
            # Don't fail the order for DB write errors

    async def _publish_order_event(self, order: SimulatedOrder) -> None:
        """Publish order event to Redis pub/sub."""
        if not self._redis:
            return
        try:
            payload = json.dumps({
                "event": "order_filled",
                "order_id": order.order_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "size": order.size,
                "fill_price": order.avg_fill_price,
                "slippage_bps": order.slippage_bps,
                "fee_bps": order.fee_bps,
                "timestamp": order.created_at.isoformat(),
            })
            await self._redis.publish(f"paper_executor:orders:{order.symbol}", payload)
        except Exception as e:
            logger.warning(
                "Failed to publish order event",
                order_id=order.order_id,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Cancel order
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> CancelResult:
        """Cancel a pending order by ID."""
        async with self._lock:
            order = self._orders.get(order_id)
            if not order:
                return CancelResult(success=False, order_id=order_id, error="Order not found")

            if order.status not in (OrderStatus.PENDING, OrderStatus.OPEN):
                return CancelResult(
                    success=False,
                    order_id=order_id,
                    error=f"Cannot cancel order in status {order.status.value}",
                )

            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.now(timezone.utc)
            self._orders[order_id] = order

            logger.info("Order cancelled", order_id=order_id, symbol=order.symbol)
            return CancelResult(success=True, order_id=order_id)

    # ------------------------------------------------------------------
    # Close position
    # ------------------------------------------------------------------

    async def close_position(self, symbol: str) -> OrderResult:
        """Close an open position at current market price."""
        pos = self._positions.get(symbol)
        if not pos:
            return OrderResult(success=False, error=f"No open position for {symbol}")

        ob = self._orderbooks.get(symbol)
        if not ob or ob.best_bid is None or ob.best_ask is None:
            return OrderResult(success=False, error=f"No orderbook data for {symbol}")

        close_side = OrderSide.SHORT if pos.side == OrderSide.LONG else OrderSide.LONG
        notional = pos.size * pos.current_price

        # Pass pos.size (base units), not notional (USD), so the slippage
        # estimate reflects the actual size being filled. Earlier version
        # passed notional which is a USD value (e.g. $17 for a small ETH
        # position) but slippage is computed as bps of base size. Mismatch
        # caused inflated slippage on small closes. 2026-06-02.
        fill_price, slippage_bps = self._market_fill_price(close_side, ob, pos.size)
        fee_bps = self._taker_fee_bps
        fee_cost = notional * (fee_bps / 10_000)

        # Realized PnL
        if pos.side == OrderSide.LONG:
            pnl = pos.size * (fill_price - pos.entry_price)
        else:
            pnl = pos.size * (pos.entry_price - fill_price)
        self._realized_pnl += pnl

        # Update cash
        if close_side == OrderSide.SHORT:
            self._cash += notional - fee_cost
        else:
            self._cash -= notional + fee_cost

        # Remove position
        del self._positions[symbol]

        # Record synthetic close order
        order_id = str(uuid.uuid4())
        order = SimulatedOrder(
            order_id=order_id,
            symbol=symbol,
            side=close_side,
            size=pos.size,
            price=fill_price,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            filled_size=pos.size,
            avg_fill_price=fill_price,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata={"reason": "position_close"},
        )
        self._orders[order_id] = order
        await self._log_trade_journal(order, pos.size, fill_price, fee_cost)
        await self._publish_order_event(order)

        logger.info("Position closed", symbol=symbol, fill_price=fill_price, pnl=pnl)
        return OrderResult(success=True, order=order, fill_price=fill_price)

    # ------------------------------------------------------------------
    # State persistence (v0.2.7)
    # ------------------------------------------------------------------

    def export_state(self) -> dict[str, Any]:
        """Snapshot the executor's in-memory state for persistence.

        Used by the orchestrator to write data/bot_equity.json so
        paper-mode restarts can restore the full state (cash,
        positions, realized PnL, equity curve). Live mode does not
        use this — it queries the exchange on every start instead.
        """
        return {
            "cash_balance": round(self._cash, 8),
            "initial_balance": round(self._initial_balance, 8),
            "realized_pnl": round(self._realized_pnl, 8),
            "positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side.value if hasattr(p.side, "value") else p.side,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "unrealized_pnl_pct": p.unrealized_pnl_pct,
                    "exposure": p.exposure,
                    "created_at": p.created_at.isoformat() if hasattr(p.created_at, "isoformat") else str(p.created_at),
                    "metadata": dict(p.metadata),
                }
                for p in self._positions.values()
            ],
            "equity_curve": list(self._equity_curve),
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore the executor's in-memory state from a saved snapshot.

        Used on paper-mode startup when data/bot_equity.json exists.
        Validates the schema, then overwrites _cash, _realized_pnl,
        _positions, _equity_curve. Does NOT change _initial_balance
        (that's the YAML/env value — what you started with, not what
        you have now).

        Failures (bad schema, missing keys) are non-fatal: the
        helper logs and leaves the executor in its current state.
        """
        try:
            cash = float(state.get("cash_balance", self._initial_balance))
            realized = float(state.get("realized_pnl", 0.0))
            positions_data = state.get("positions", [])
            equity_curve_data = state.get("equity_curve", [])

            if cash < 0:
                raise ValueError(f"cash_balance must be >= 0, got {cash}")

            self._cash = cash
            self._realized_pnl = realized
            self._positions = {}
            for pd in positions_data:
                p = Position(
                    symbol=pd["symbol"],
                    side=OrderSide(pd["side"]) if isinstance(pd["side"], str) else pd["side"],
                    size=float(pd["size"]),
                    entry_price=float(pd["entry_price"]),
                    current_price=float(pd["current_price"]),
                    unrealized_pnl=float(pd.get("unrealized_pnl", 0.0)),
                    unrealized_pnl_pct=float(pd.get("unrealized_pnl_pct", 0.0)),
                    exposure=float(pd.get("exposure", 0.0)),
                    created_at=__import__("datetime").datetime.fromisoformat(pd["created_at"])
                        if isinstance(pd.get("created_at"), str) else pd.get("created_at",
                            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)),
                    metadata=dict(pd.get("metadata", {})),
                )
                self._positions[p.symbol] = p
            self._equity_curve = list(equity_curve_data)

            logger.info(
                "Executor state restored from disk",
                cash=round(self._cash, 2),
                realized_pnl=round(self._realized_pnl, 2),
                positions=len(self._positions),
                equity_curve_points=len(self._equity_curve),
            )
        except Exception as exc:
            logger.warning(
                "Failed to restore executor state — using defaults",
                error=str(exc),
            )

    def record_equity_point(self, equity: float, ts: datetime | None = None) -> None:
        """Append a (timestamp, equity) point to the equity curve.

        Capped at 10,000 points (~3.5 days at 30s cycles). On overflow,
        drops the oldest 10% in one batch to amortize the resize. Used
        by Sharpe/MDD calculations so the metrics survive restarts.
        """
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._equity_curve.append({
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "equity": round(equity, 8),
        })
        if len(self._equity_curve) > self._equity_curve_max:
            drop = max(1, self._equity_curve_max // 10)
            self._equity_curve = self._equity_curve[drop:]

    def get_equity_curve(self) -> list[dict[str, Any]]:
        """Return a copy of the equity curve for Sharpe/MDD calc."""
        return list(self._equity_curve)

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        """Get open positions, optionally filtered by symbol."""
        if symbol:
            pos = self._positions.get(symbol)
            return [pos] if pos else []
        return list(self._positions.values())

    def get_portfolio(self) -> PortfolioSummary:
        """Compute and return the current portfolio summary."""
        total_exposure = sum(p.exposure for p in self._positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        total_equity = self._cash + total_exposure + total_unrealized

        return PortfolioSummary(
            total_equity=round(total_equity, 2),
            cash_balance=round(self._cash, 2),
            unrealized_pnl=round(total_unrealized, 2),
            realized_pnl=round(self._realized_pnl, 2),
            total_pnl=round(self._realized_pnl + total_unrealized, 2),
            margin_used=0.0,  # Paper trading — no actual margin
            exposure=round(total_exposure, 2),
            # 2026-06-04: changed denominator from initial_balance to
            # total_equity. Using initial_balance made the cap relative
            # to a stale $50 baseline, so as cash/equity drifted, the
            # bot kept adding positions until exposure_pct crossed 50%
            # only after the fact. The risk check is called per-entry
            # with the pre-trade portfolio, so the denominator must
            # reflect current equity for the cap to be meaningful.
            exposure_pct=(
                round(total_exposure / total_equity, 4)
                if total_equity > 0
                else 0.0
            ),
            positions=list(self._positions.values()),
        )

    def get_order(self, order_id: str) -> SimulatedOrder | None:
        """Get a single order by its ID."""
        return self._orders.get(order_id)

    async def get_trade_history(
        self,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get filled trades from the trade journal.

        Args:
            symbol: Optional symbol filter.
            limit: Maximum number of rows to return.

        Returns:
            List of trade journal rows as dicts.
        """
        if not self._db:
            return []

        sql = """
        SELECT order_id, symbol, side, size, entry_price, exit_price,
               pnl, pnl_pct, fees, strategy_name, created_at, closed_at
        FROM trade_journal
        WHERE 1=1
        """
        params: list[Any] = []
        if symbol:
            sql += f" AND symbol = ${len(params) + 1}"
            params.append(symbol)
        sql += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
        params.append(limit)

        try:
            if not self._db:
                logger.warning("DB unavailable, cannot fetch trade history")
                return []
            async with self._db.acquire() as conn:
                rows = await conn.fetch(sql, *params)
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Failed to fetch trade history", error=str(e))
            return []

    # ------------------------------------------------------------------
    # Daily counter reset
    # ------------------------------------------------------------------

    def _reset_daily_counter_if_needed(self) -> None:
        """Reset daily trade counter at midnight UTC."""
        now = datetime.now(timezone.utc)
        if self._daily_reset_at is None or now.date() > self._daily_reset_at.date():
            self._daily_trade_count = 0
            self._daily_reset_at = now

    # ------------------------------------------------------------------
    # Audit log helpers
    # ------------------------------------------------------------------

    def _audit_no_trade(
        self,
        symbol: str,
        reason: str,
        regime: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Write a NO_TRADE audit row from the executor layer.

        Used when the executor blocks a trade due to risk gates or missing
        market data. Failures are best-effort and never raised.
        """
        try:
            entry = AuditEntryInput(
                symbol=symbol,
                decision="NO_TRADE",
                reason=reason,
                reason_code=classify_no_trade_reason(reason).value,
                regime=regime,
                metadata=metadata or {},
                source="executor",
            )
            get_audit_logger().log(entry)
        except Exception as exc:
            logger.debug("executor audit_no_trade failed", error=str(exc))

    def _audit_trade_filled(
        self,
        symbol: str,
        side: OrderSide,
        order_id: str,
        entry_price: float,
        size: float,
        stop_loss: float,
        take_profit: float,
        regime: str | None = None,
        strategy_name: str | None = None,
        slippage_bps: float = 0.0,
        fee_bps: float = 0.0,
        fee_cost: float = 0.0,
    ) -> None:
        """Write a BUY/SELL audit row for a successfully filled order."""
        try:
            decision = "BUY" if side == OrderSide.LONG else "SELL"
            entry = AuditEntryInput(
                symbol=symbol,
                decision=decision,
                reason=f"Trade filled: {decision} {size} @ {entry_price}",
                reason_code=None,
                regime=regime,
                order_id=order_id,
                entry_price=entry_price,
                size=size,
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata={
                    "strategy_name": strategy_name,
                    "slippage_bps": slippage_bps,
                    "fee_bps": fee_bps,
                    "fee_cost": fee_cost,
                    "side": side.value,
                },
                source="executor",
            )
            get_audit_logger().log(entry)
        except Exception as exc:
            logger.debug("executor audit_trade_filled failed", error=str(exc))

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PaperExecutor":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()


# Avoid circular import at module level
from ..data.storage import Database  # noqa: E402