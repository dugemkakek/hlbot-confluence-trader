"""FastAPI Dashboard — HTTP API for the trading AI.

Provides:
  - Health: GET /health, GET /ready
  - Portfolio: GET /api/v1/portfolio, GET /api/v1/positions, GET /api/v1/positions/{symbol}
  - Trading: GET /api/v1/trades, POST /api/v1/execute, POST /api/v1/orders/{order_id}/cancel
  - Signals: GET /api/v1/signals, GET /api/v1/regime/{symbol}
  - Decision: POST /api/v1/decide/{symbol}
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..data.models import (
    Decision,
    OrderSide,
    OrderType,
    PortfolioSummary,
    Position,
    Regime,
    Signal,
    SimulatedOrder,
)
from ..executor.paper_executor import PaperExecutor
from ..orchestrator.trading_loop import TradingOrchestrator
from ..risk.risk_manager import RiskManager
from ..signals.registry import SignalRegistry
from ..signals.regime_detector import RegimeDetector
from ..signals.sentiment_scorer import SentimentScorer
from ..utils.config import get_config, AppConfig
from ..utils.logging import get_logger, setup_logging
from ..audit import get_audit_logger
from .ws import router as ws_router, WebSocketManager

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic request / response models
# ─────────────────────────────────────────────────────────────────────────────


class ExecuteRequest(BaseModel):
    """POST /api/v1/execute body."""

    symbol: str = Field(..., description="Trading pair, e.g. BTC")
    side: OrderSide = Field(..., description="LONG or SHORT")
    size: float = Field(..., gt=0, description="Order size in units")


class HealthResponse(BaseModel):
    status: str
    pid: int


class ReadyResponse(BaseModel):
    status: str
    db_connected: bool
    redis_connected: bool


class DecisionResponse(BaseModel):
    """POST /api/v1/decide/{symbol} response."""

    decision: dict[str, Any]


class ErrorResponse(BaseModel):
    detail: str


# ─────────────────────────────────────────────────────────────────────────────
# Application state (lifecycle-managed)
# ─────────────────────────────────────────────────────────────────────────────


class AppState:
    """Holds shared resources attached to the FastAPI app."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.executor: PaperExecutor | None = None
        self.risk_manager: RiskManager | None = None
        self.signal_registry: SignalRegistry | None = None
        self.regime_detector: RegimeDetector | None = None
        self.sentiment_scorer: SentimentScorer | None = None
        self.orchestrator: TradingOrchestrator | None = None
        self._ready = False

    def set_ready(self) -> None:
        self._ready = True


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    setup_logging()
    cfg = get_config()

    state = AppState(cfg)
    app = FastAPI(
        title="HLBot Trading API",
        description="Hyperliquid Trading AI — portfolio, signals, execution, and decision endpoints",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── WebSocket routes ─────────────────────────────────────────────────────────
    app.include_router(ws_router)

    # ── Lifecycle events ──────────────────────────────────────────────────────
    @app.on_event("startup")
    async def startup() -> None:
        logger.info("API server starting", host=cfg.api.host, port=cfg.api.port, pid=os.getpid())

        # Initialise components (orchestrator manages its own connect/disconnect)
        try:
            state.regime_detector = RegimeDetector(min_candles=50)
            state.sentiment_scorer = SentimentScorer()

            state.orchestrator = TradingOrchestrator(config=cfg)
            await state.orchestrator.start()
            state.set_ready()

            logger.info("API server ready")
        except Exception as exc:
            logger.error("Startup failed", error=str(exc), exc_info=True)
            raise

    @app.on_event("shutdown")
    async def shutdown() -> None:
        logger.info("API server shutting down")
        if state.orchestrator:
            await state.orchestrator.stop()

    # ── Health ────────────────────────────────────────────────────────────────
    @app.get("/health", response_model=HealthResponse, tags=["Health"])
    async def health() -> HealthResponse:
        """Liveness probe — returns OK and process ID."""
        return HealthResponse(status="ok", pid=os.getpid())

    @app.get("/ready", response_model=ReadyResponse, tags=["Health"])
    async def ready() -> ReadyResponse:
        """Readiness probe — checks DB and Redis connectivity."""
        db_ok = False
        redis_ok = False
        if state.orchestrator and state.orchestrator.executor:
            ex = state.orchestrator.executor
            db_ok = ex._db is not None
            redis_ok = ex._redis is not None
        return ReadyResponse(status="ready" if state._ready else "not_ready",
                             db_connected=db_ok, redis_connected=redis_ok)

    # ── Portfolio ────────────────────────────────────────────────────────────
    @app.get("/api/v1/portfolio", response_model=PortfolioSummary, tags=["Portfolio"])
    async def get_portfolio() -> PortfolioSummary:
        """Current portfolio summary (cash, equity, exposure, positions)."""
        ex = _get_executor(state)
        return ex.get_portfolio()

    @app.get("/api/v1/positions", response_model=list[Position], tags=["Portfolio"])
    async def get_positions() -> list[Position]:
        """All open positions."""
        ex = _get_executor(state)
        return ex.get_positions()

    @app.get("/api/v1/positions/{symbol}", response_model=Position, tags=["Portfolio"])
    async def get_position(symbol: str) -> Position:
        """Single position by symbol, or 404."""
        ex = _get_executor(state)
        positions = ex.get_positions(symbol.upper())
        if not positions:
            raise HTTPException(status_code=404, detail=f"No position for {symbol}")
        return positions[0]

    # ── Trading ───────────────────────────────────────────────────────────────
    @app.get("/api/v1/trades", response_model=list[dict[str, Any]], tags=["Trading"])
    async def get_trades(
        symbol: str | None = Query(None, description="Filter by symbol"),
        limit: int = Query(50, ge=1, le=500, description="Max rows"),
    ) -> list[dict[str, Any]]:
        """Trade history from the journal."""
        ex = _get_executor(state)
        return await ex.get_trade_history(symbol=symbol.upper() if symbol else None, limit=limit)

    @app.post("/api/v1/execute", response_model=dict[str, Any], status_code=status.HTTP_201_CREATED,
              tags=["Trading"])
    async def execute_trade(req: ExecuteRequest) -> dict[str, Any]:
        """Manually place a paper trade (LONG or SHORT)."""
        ex = _get_executor(state)

        # Dry-run guard
        if state.orchestrator and not state.orchestrator.dry_run:
            # Real trading — reject from API
            raise HTTPException(
                status_code=403,
                detail="Live trading not enabled — set orchestrator.dry_run=false to allow execution",
            )

        result = await ex.place_order(
            symbol=req.symbol.upper(),
            side=req.side,
            size=req.size,
            order_type=OrderType.MARKET,
            strategy_name="manual_api",
        )

        if not result.success:
            raise HTTPException(status_code=400, detail=result.error or "Execution failed")

        order = result.order
        return {
            "success": True,
            "order_id": order.order_id if order else None,
            "symbol": order.symbol if order else req.symbol,
            "side": order.side.value if order else req.side.value,
            "size": order.size if order else req.size,
            "fill_price": result.fill_price,
            "status": order.status.value if order else "FILLED",
        }

    @app.post("/api/v1/orders/{order_id}/cancel", response_model=dict[str, Any], tags=["Trading"])
    async def cancel_order(order_id: str) -> dict[str, Any]:
        """Cancel a pending or open order."""
        ex = _get_executor(state)
        result = await ex.cancel_order(order_id)
        if not result.success:
            raise HTTPException(status_code=404, detail=result.error or "Cancel failed")
        return {"success": True, "order_id": result.order_id}

    # ── Signals ──────────────────────────────────────────────────────────────
    @app.get("/api/v1/signals", response_model=dict[str, Any], tags=["Signals"])
    async def get_signals(
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Persistent signals summary backed by SQLite.

        Counts and symbols survive bot restarts (the in-memory
        `SignalRegistry` clears per cycle). Optional `start`/`end`
        ISO-8601 timestamps filter the window. Without them, returns
        all-time totals.
        """
        from datetime import datetime
        from ..data.capture import get_data_capture
        cap = get_data_capture()
        # Fall back to in-memory if the capture writer never initialised
        # (e.g. SQLite unavailable). That preserves the old behaviour.
        if cap._init_failed:
            reg = _get_registry(state)
            return reg.summary()
        s = datetime.fromisoformat(start) if start else None
        e = datetime.fromisoformat(end) if end else None
        summary = cap.get_signals_summary(start=s, end=e)
        # Also surface the in-memory current-cycle count under
        # "current_cycle" so callers can see what's live vs historical.
        try:
            reg = _get_registry(state)
            mem = reg.summary()
            summary["current_cycle"] = mem.get("total_signals", 0)
            summary["current_symbols"] = mem.get("symbols", [])
        except Exception:
            summary["current_cycle"] = 0
            summary["current_symbols"] = []
        summary["source"] = "sqlite"
        return summary

    @app.get("/api/v1/regime/{symbol}", response_model=dict[str, Any], tags=["Signals"])
    async def get_regime(symbol: str) -> dict[str, Any]:
        """Current regime classification for a symbol (requires candles)."""
        # In the real loop, candles are fetched internally. For the API we
        # delegate to the orchestrator's latest analysis if available.
        orch = state.orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="Orchestrator not running")

        key = symbol.upper()
        analysis = orch._last_regime_analysis.get(key)
        if analysis is None:
            raise HTTPException(
                status_code=404,
                detail=f"No regime analysis available for {symbol}. Run a decision cycle first.",
            )
        return {
            "symbol": analysis.symbol,
            "regime": analysis.regime.value,
            "confidence": analysis.confidence,
            "indicators": analysis.indicators,
            "trend_strength": analysis.trend_strength,
            "volatility_ratio": analysis.volatility_ratio,
            "volume_ratio": analysis.volume_ratio,
            "timestamp": analysis.timestamp.isoformat(),
        }

    # ── Scanner ────────────────────────────────────────────────────────────────
    @app.get("/api/v1/scanner/pairs", response_model=dict[str, Any], tags=["Scanner"])
    async def get_scanner_pairs() -> dict[str, Any]:
        """All discovered pairs with their confluence scores.

        Returns the full ranking from the last cycle: all pairs sorted by
        confluence_score descending.
        """
        orch = state.orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="Orchestrator not running")

        ranked = orch.get_ranked_pairs()
        return {
            "total": len(ranked),
            "threshold": orch._min_confluence,
            "max_pairs": orch._max_pairs,
            "pairs": [
                {
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "confluence_score": round(p.confluence_score, 4),
                    "structure_score": round(p.structure_score, 4),
                    "pullback_score": round(p.pullback_score, 4),
                    "momentum_score": round(p.momentum_score, 4),
                    "volume_score": round(p.volume_score, 4),
                    "confidence": round(p.confidence, 4),
                    "is_actionable": p.is_actionable,
                    "metadata": p.metadata,
                    "timestamp": p.timestamp.isoformat(),
                }
                for p in ranked
            ],
        }

    @app.get("/api/v1/scanner/top", response_model=dict[str, Any], tags=["Scanner"])
    async def get_scanner_top() -> dict[str, Any]:
        """Top N ranked pairs that exceeded the confluence threshold.

        These are the pairs that were evaluated in the last cycle.
        """
        orch = state.orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="Orchestrator not running")

        top = orch.get_top_pairs()
        return {
            "count": len(top),
            "threshold": orch._min_confluence,
            "max_pairs": orch._max_pairs,
            "pairs": [
                {
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "confluence_score": round(p.confluence_score, 4),
                    "structure_score": round(p.structure_score, 4),
                    "pullback_score": round(p.pullback_score, 4),
                    "momentum_score": round(p.momentum_score, 4),
                    "volume_score": round(p.volume_score, 4),
                    "confidence": round(p.confidence, 4),
                    "metadata": p.metadata,
                    "timestamp": p.timestamp.isoformat(),
                }
                for p in top
            ],
        }

    # ── Decision ─────────────────────────────────────────────────────────────
    @app.post("/api/v1/decide/{symbol}", response_model=DecisionResponse, tags=["Decision"])
    async def decide_symbol(symbol: str) -> DecisionResponse:
        """Trigger the decision engine for a symbol (runs one evaluation cycle)."""
        orch = state.orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="Orchestrator not running")

        try:
            decision = await orch.run_decision_for_symbol(symbol.upper())
            return DecisionResponse(decision=decision.model_dump(mode="json"))
        except Exception as exc:
            logger.error("decide failed", symbol=symbol, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Audit ───────────────────────────────────────────────────────────────
    @app.get("/api/v1/audit/{symbol}", tags=["Audit"])
    async def get_audit(
        symbol: str,
        limit: int = Query(50, ge=1, le=500, description="Max rows to return"),
        decision: str | None = Query(
            None,
            description="Filter by decision: BUY, SELL, or NO_TRADE",
        ),
        reason_code: str | None = Query(
            None,
            description="Filter by NO_TRADE reason code (e.g. confluence_low)",
        ),
    ) -> dict[str, Any]:
        """Return recent audit-log rows for a symbol.

        Every trading decision (BUY, SELL, NO_TRADE) is logged with:
          - timestamp, symbol, regime, confluence / subsystem scores
          - decision, reason, and a normalized reason_code
          - for executed trades: entry_price, size, stop_loss, take_profit

        Use this endpoint to analyse WHY the bot does or doesn't trade
        rather than just observing the trade history.
        """
        try:
            entries = get_audit_logger().get_entries(
                symbol=symbol,
                decision=decision,
                reason_code=reason_code,
                limit=limit,
            )
        except Exception as exc:
            logger.error("audit fetch failed", symbol=symbol, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

        return {
            "symbol": symbol.upper(),
            "count": len(entries),
            "limit": limit,
            "decision_filter": decision,
            "reason_code_filter": reason_code,
            "entries": [e.model_dump(mode="json") for e in entries],
        }

    @app.get("/api/v1/audit/{symbol}/reasons", tags=["Audit"])
    async def get_audit_reason_counts(
        symbol: str,
        since: str | None = Query(
            None,
            description="ISO 8601 timestamp; only count rows >= this",
        ),
    ) -> dict[str, Any]:
        """Group NO_TRADE counts by reason_code for a symbol.

        Useful for a quick dashboard: "in the last 24h, why didn't we trade?"
        """
        from datetime import datetime as _dt
        since_dt: _dt | None = None
        if since:
            try:
                since_dt = _dt.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid since timestamp: {since}",
                )

        try:
            counts = get_audit_logger().count_by_reason_code(
                since=since_dt, symbol=symbol
            )
        except Exception as exc:
            logger.error("audit reason counts failed", symbol=symbol, error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc))

        return {
            "symbol": symbol.upper(),
            "since": since,
            "counts": counts,
            "total": sum(counts.values()),
        }

    # ── Global exception handler ──────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Any, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception", path=getattr(request, "url", "?"),
                      error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error — see logs"},
        )

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_executor(state: AppState) -> PaperExecutor:
    if state.orchestrator is None or state.orchestrator.executor is None:
        raise HTTPException(status_code=503, detail="Trading system not initialised")
    return state.orchestrator.executor


def _get_registry(state: AppState) -> SignalRegistry:
    """Return the orchestrator's shared SignalRegistry (not a standalone instance)."""
    if state.orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not running")
    return state.orchestrator.signal_registry