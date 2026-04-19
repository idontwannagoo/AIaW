import asyncio
from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import WebSocket


class Broadcaster:
    """In-process pub/sub for WebSocket fanout, keyed by user id."""

    def __init__(self) -> None:
        self._subs: dict[UUID, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def register(self, user_id: UUID, ws: WebSocket) -> None:
        async with self._lock:
            self._subs[user_id].add(ws)

    async def unregister(self, user_id: UUID, ws: WebSocket) -> None:
        async with self._lock:
            subs = self._subs.get(user_id)
            if subs and ws in subs:
                subs.remove(ws)
                if not subs:
                    self._subs.pop(user_id, None)

    async def publish(self, user_id: UUID, message: dict[str, Any]) -> None:
        sockets = list(self._subs.get(user_id, ()))
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                await self.unregister(user_id, ws)


broadcaster = Broadcaster()
