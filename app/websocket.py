import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.utils import json_default


LOGGER = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.active_connections.discard(websocket)

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"event": event, "payload": payload}, default=json_default)
        async with self._lock:
            connections = list(self.active_connections)
        for websocket in connections:
            try:
                await websocket.send_text(message)
            except (WebSocketDisconnect, RuntimeError):
                await self.disconnect(websocket)
            except Exception:
                LOGGER.exception("WebSocket broadcast failed")
                await self.disconnect(websocket)

