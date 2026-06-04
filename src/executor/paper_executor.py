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

        # Live orderbook state keyed by symbol — use OrderbookSnapshot for best bid/ask
        self._orderbooks: dict[str, OrderbookSnapshot] = {}

        # In-memory state
        self._cash: float = self._initial_balance
        self._realized_pnl: float = 0.0
        self._positions: dict[str, Position] = {}  # key = symbol
        self._orders: dict[str, SimulatedOrder] = {}  # key = order_id
        self._pending_limit_orders: dict[str, asyncio.Task] = {}

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
        if not self._ws:
            return
        for symbol in symbols:
            try:
                await self._ws.subscribe_orderbook(symbol)
            except Exception as exc:
                logger.debug("orderbook subscription skipped", symbol=symbol, error=str(exc))

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
    ) -> OrderResult:
        """Place a paper order (market or limit).

        Args:
            symbol: Trading pair, e.g. "BTC".
            side: LONG or SHORT.
            size: Order size in quote currency (USD).
            order_type: MARKET or LIMIT.
            limit_price: Price for limit orders.
            strategy_name: Optional strategy label for the journal.
            signal_reason: Optional signal metadata for the journal.
            regime: Optional market regime label.

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

        # Update position
        self._update_position(symbol, side, filled_size, fill_price)

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
        self, symbol: str, side: OrderSide, size: float, fill_price: float
    ) -> None:
        """Update or create a position after a fill."""
        existing = self._positions.get(symbol)

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
            )

        else:
            # Opposite side — reduce or close position
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
                    # Residual opens new position in opposite direction
                    new_side = OrderSide.SHORT if side == OrderSide.LONG else OrderSide.LONG
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
                    )
                else:
                    # Exact close — position fully removed
                    del self._positions[symbol]
            else:
                # Partial close — reduce size of existing position
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
            exposure_pct=(
                round(total_exposure / self._initial_balance, 4)
                if self._initial_balance > 0
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