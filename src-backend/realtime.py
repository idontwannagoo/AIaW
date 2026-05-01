"""In-memory pub/sub broker for realtime events (Stage 2 / Step 1).

Single-process; events are lost on broker restart. Clients reconnect with
`since=<lastRev>` and the WS endpoint replays missed rows directly from the
table via SQL — no separate event log needed while we only ship one table.

Multi-instance scaling will swap this for Redis pub/sub; the broker API
(``add`` / ``remove`` / ``publish``) is the only surface callers depend on.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger('aiaw.backend.realtime')

# Bounded so a stuck client cannot accumulate unbounded backlog. When full we
# close the subscription and let the client recover via reconnect + ``since``.
QUEUE_MAXSIZE = 200


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    )
    tables: set[str] = field(default_factory=set)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class Broker:
    """Per-user fan-out, no persistence."""

    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = {}
        self._lock = asyncio.Lock()

    async def add(self, user_id: str, sub: Subscription) -> None:
        async with self._lock:
            self._subs.setdefault(user_id, set()).add(sub)

    async def remove(self, user_id: str, sub: Subscription) -> None:
        async with self._lock:
            bucket = self._subs.get(user_id)
            if bucket is None:
                return
            bucket.discard(sub)
            if not bucket:
                self._subs.pop(user_id, None)

    async def publish(self, user_id: str, event: dict[str, Any]) -> None:
        """Fan an event to every matching subscription for one user.

        Subs whose queue is full are flagged closed so the connection task can
        tear them down — we never block the publisher on a slow consumer.
        """
        async with self._lock:
            subs = list(self._subs.get(user_id, ()))
        table = event.get('table')
        for sub in subs:
            if table not in sub.tables:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    'realtime queue full for user %s, marking sub closed', user_id
                )
                sub.closed.set()


broker = Broker()
