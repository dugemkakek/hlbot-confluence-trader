"""WebSocket support — real-time push to connected clients.

Broadcasts four message types:
  { "type": "portfolio",  "data": { ... } }
  { "type": "trade",      "data": { ... } }
  { "type": "signal",     "data": { ... } }
  { "type": "decision",   "data": { ... } }

Usage
-----
    from fastapi import APIRouter
    from .ws import router as ws_router, WebSocketManager

    app.include_router(ws_router)

    # In TradingOrchestrator, broadcast with:
    await WebSocketManager.broadcast({"type": "portfolio", "data": {...}})
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Connection manager
# ─────────────────────────────────────────────────────────────────────────────


class WebSocketManager:
    """Thread-safe global registry of active WebSocket connections."""

    _connections: set[WebSocket] = set()
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def connect(cls, ws: WebSocket) -> None:
        await ws.accept()
        async with cls._lock:
            cls._connections.add(ws)
        logger.info("WS client connected", total=len(cls._connections))

    @classmethod
    async def disconnect(cls, ws: WebSocket) -> None:
        async with cls._lock:
            cls._connections.discard(ws)
        logger.info("WS client disconnected", remaining=len(cls._connections))

    @classmethod
    async def broadcast(cls, message: dict[str, Any]) -> None:
        """Send a message to all connected clients.

        Silently drops clients that have disconnected.
        """
        if not cls._connections:
            return

        payload = json.dumps(message, default=_json_serializer)

        async with cls._lock:
            connections = list(cls._connections)

        dead = []
        for ws in connections:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(payload)
                else:
                    dead.append(ws)
            except Exception as exc:
                logger.warning("WS send failed, dropping client", error=str(exc))
                dead.append(ws)

        if dead:
            async with cls._lock:
                for ws in dead:
                    cls._connections.discard(ws)


# ─────────────────────────────────────────────────────────────────────────────
# JSON helpers
# ─────────────────────────────────────────────────────────────────────────────


def _json_serializer(obj: Any) -> str:
    """Default JSON serializer for objects not serializable by default."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint — /ws.

    After connecting, the client receives streamed JSON messages.
    No authentication for now; extend with a token param if needed.
    """
    await WebSocketManager.connect(websocket)

    try:
        # Send a welcome ping
        await websocket.send_json({
            "type": "connected",
            "data": {"message": "HLBot WebSocket connected", "ts": datetime.now(timezone.utc).isoformat()},
        })

        # Keep the connection alive — relay client messages but no protocol needed
        while True:
            try:
                # Wait for client frames (ping/pong handled by Starlette)
                data = await websocket.receive_text()
                # Echo back for heartbeat / protocol extensibility
                try:
                    parsed = json.loads(data)
                    msg_type = parsed.get("type", "unknown")
                    if msg_type == "ping":
                        await websocket.send_json({
                            "type": "pong",
                            "data": {"ts": datetime.now(timezone.utc).isoformat()},
                        })
                except (json.JSONDecodeError, KeyError):
                    # Not a JSON message — ignore silently
                    pass
            except WebSocketDisconnect:
                break
    except Exception as exc:
        logger.warning("WS connection error", error=str(exc))
    finally:
        await WebSocketManager.disconnect(websocket)