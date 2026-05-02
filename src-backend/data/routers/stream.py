"""WebSocket realtime endpoint (Stage 2 / Step 1).

Single multiplexed connection per tab. Auth via the
``Sec-WebSocket-Protocol: bearer.<jwt>`` subprotocol — keeps the token out of
URL query strings (which leak into proxy / nginx access logs).

Wire protocol (JSON text frames):

    client -> server
        {"type": "subscribe",   "table": "providers", "since": 0}
        {"type": "unsubscribe", "table": "providers"}
        {"type": "pong"}

    server -> client
        {"type": "event", "table": "...", "op": "put|delete", "id": "...",
         "row": {...} | null, "rev": <int>}
        {"type": "ping"}
        {"type": "replay-done", "table": "...", "rev": <int>}
        {"type": "error", "code": "...", "message": "..."}

Subscribing with ``since`` first SELECTs rows ``WHERE version > since`` from the
table (per-user) and replays them, then sends ``replay-done`` so the client
knows live mode has begun. Live events that arrive *during* the replay are
queued by the broker and drained after — duplicates are tolerated client-side
via per-row ``rev`` LWW.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, status
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect, WebSocketState

from realtime import Subscription, broker

from ..auth import _decode_access
from ..db import SessionLocal
from ..models.assistant import Assistant
from ..models.avatar_image import AvatarImage
from ..models.installed_plugin import InstalledPlugin
from ..models.provider import Provider
from ..models.reactive import Reactive
from ..models.user import User

logger = logging.getLogger('aiaw.backend.realtime.ws')

router = APIRouter(tags=['stream'])

# Tables clients are allowed to subscribe to + how to replay them.
TABLE_MODELS = {
    'providers': Provider,
    'reactives': Reactive,
    'assistants': Assistant,
    'avatar_images': AvatarImage,
    'installed_plugins': InstalledPlugin,
}

HEARTBEAT_INTERVAL = 25.0  # server pings this often
HEARTBEAT_GRACE = 10.0     # max wait for a pong after each ping
CLOSE_TOKEN_EXPIRED = 4001
CLOSE_QUEUE_OVERFLOW = 4002


def _parse_subprotocol(header: Optional[str]) -> Optional[str]:
    """Extract the JWT from a ``bearer.<jwt>`` entry in Sec-WebSocket-Protocol.

    Browsers send subprotocols as a comma-separated list. We accept any entry
    that starts with ``bearer.`` so clients are free to advertise additional
    protocols alongside auth.
    """
    if not header:
        return None
    for raw in header.split(','):
        item = raw.strip()
        if item.startswith('bearer.'):
            return item[len('bearer.'):]
    return None


async def _validate_token(token: str) -> Optional[tuple[str, float]]:
    """Decode + validate JWT, return ``(user_id, exp_ts)`` or None.

    We can't use the ``current_user`` FastAPI dependency here — WS handshake
    runs outside the normal request scope — so we replicate the user lookup.
    """
    try:
        payload = _decode_access(token)
    except Exception:  # _decode_access raises HTTPException on invalid/expired
        return None
    user_id = payload.get('sub')
    exp = payload.get('exp')
    if not user_id or not exp:
        return None
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
    if user is None or user.status != 'active':
        return None
    return user_id, float(exp)


def _serialize_provider(p: Provider) -> dict[str, Any]:
    deleted = p.deleted_at is not None
    return {
        'type': 'event',
        'table': 'providers',
        'op': 'delete' if deleted else 'put',
        'id': p.id,
        'rev': p.version,
        'row': None if deleted else {
            'id': p.id,
            'version': p.version,
            'updated_at': p.updated_at.isoformat(),
            'deleted': False,
            'data': p.data,
        },
    }


def _serialize_reactive(r: Reactive) -> dict[str, Any]:
    deleted = r.deleted_at is not None
    return {
        'type': 'event',
        'table': 'reactives',
        'op': 'delete' if deleted else 'put',
        # KV table: `key` takes the `id` slot in the generic envelope.
        'id': r.key,
        'rev': r.version,
        'row': None if deleted else {
            'key': r.key,
            'version': r.version,
            'updated_at': r.updated_at.isoformat(),
            'deleted': False,
            'data': r.data,
        },
    }


def _serialize_assistant(a: Assistant) -> dict[str, Any]:
    deleted = a.deleted_at is not None
    return {
        'type': 'event',
        'table': 'assistants',
        'op': 'delete' if deleted else 'put',
        'id': a.id,
        'rev': a.version,
        'row': None if deleted else {
            'id': a.id,
            'version': a.version,
            'updated_at': a.updated_at.isoformat(),
            'deleted': False,
            'data': a.data,
        },
    }


def _serialize_avatar_image(a: AvatarImage) -> dict[str, Any]:
    deleted = a.deleted_at is not None
    return {
        'type': 'event',
        'table': 'avatar_images',
        'op': 'delete' if deleted else 'put',
        'id': a.id,
        'rev': a.version,
        'row': None if deleted else {
            'id': a.id,
            'version': a.version,
            'updated_at': a.updated_at.isoformat(),
            'deleted': False,
            'data': a.data,
        },
    }


def _serialize_installed_plugin(p: InstalledPlugin) -> dict[str, Any]:
    deleted = p.deleted_at is not None
    return {
        'type': 'event',
        'table': 'installed_plugins',
        'op': 'delete' if deleted else 'put',
        # KV table: `key` takes the `id` slot in the generic envelope.
        'id': p.key,
        'rev': p.version,
        'row': None if deleted else {
            'key': p.key,
            'version': p.version,
            'updated_at': p.updated_at.isoformat(),
            'deleted': False,
            'data': p.data,
        },
    }


SERIALIZERS = {
    'providers': _serialize_provider,
    'reactives': _serialize_reactive,
    'assistants': _serialize_assistant,
    'avatar_images': _serialize_avatar_image,
    'installed_plugins': _serialize_installed_plugin,
}


async def _replay_table(
    user_id: str, table: str, since: int
) -> list[dict[str, Any]]:
    model = TABLE_MODELS.get(table)
    serializer = SERIALIZERS.get(table)
    if model is None or serializer is None:
        return []
    async with SessionLocal() as session:
        stmt = (
            select(model)
            .where(model.user_id == user_id, model.version > since)
            .order_by(model.version)
        )
        rows = (await session.execute(stmt)).scalars().all()
    return [serializer(r) for r in rows]


@router.websocket('/api/v1/stream')
async def stream(ws: WebSocket) -> None:
    token = _parse_subprotocol(ws.headers.get('sec-websocket-protocol'))
    if not token:
        # No subprotocol means no auth — reject before accepting so the client
        # gets a clean 1006 / handshake failure rather than a post-accept close.
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    validated = await _validate_token(token)
    if validated is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    user_id, exp_ts = validated

    # Echo back the auth subprotocol so the WS handshake completes; some
    # browsers / proxies require the server to pick one.
    await ws.accept(subprotocol=f'bearer.{token}')

    sub = Subscription()
    await broker.add(user_id, sub)
    last_pong = time.monotonic()

    async def recv_loop() -> None:
        nonlocal last_pong
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json(
                    {'type': 'error', 'code': 'bad-json', 'message': 'invalid json'}
                )
                continue
            mtype = msg.get('type')
            if mtype == 'pong':
                last_pong = time.monotonic()
            elif mtype == 'subscribe':
                table = msg.get('table')
                since = int(msg.get('since') or 0)
                if table not in TABLE_MODELS:
                    await ws.send_json({
                        'type': 'error', 'code': 'unknown-table',
                        'message': f'table {table!r} not subscribable',
                    })
                    continue
                # Add to filter *before* replay so any live event committed
                # mid-replay is queued and gets delivered after replay-done.
                sub.tables.add(table)
                events = await _replay_table(user_id, table, since)
                for evt in events:
                    await ws.send_json(evt)
                last_rev = events[-1]['rev'] if events else since
                await ws.send_json({
                    'type': 'replay-done', 'table': table, 'rev': last_rev,
                })
            elif mtype == 'unsubscribe':
                table = msg.get('table')
                if table:
                    sub.tables.discard(table)
            else:
                await ws.send_json({
                    'type': 'error', 'code': 'unknown-type',
                    'message': f'unknown message type {mtype!r}',
                })

    async def send_loop() -> None:
        while True:
            event = await sub.queue.get()
            await ws.send_json(event)

    async def heartbeat_loop() -> None:
        # RFC 6455-style ping/pong. Send ping every HEARTBEAT_INTERVAL; if the
        # client doesn't pong within HEARTBEAT_GRACE of *that* ping, declare
        # dead and bail. Worst-case detection time = INTERVAL + GRACE.
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await ws.send_json({'type': 'ping'})
            ping_at = time.monotonic()
            await asyncio.sleep(HEARTBEAT_GRACE)
            if last_pong < ping_at:
                logger.info('ws heartbeat timeout for user %s', user_id)
                return

    async def exp_watchdog() -> None:
        # Sleep until exp; close so client does a refresh + reconnect. We add a
        # tiny grace so the client's clock-skew doesn't beat us to the close.
        delay = max(1.0, exp_ts - time.time())
        await asyncio.sleep(delay)
        logger.info('ws token expired for user %s', user_id)

    async def closed_watcher() -> None:
        await sub.closed.wait()
        logger.info('ws subscription marked closed for user %s', user_id)

    tasks = [
        asyncio.create_task(recv_loop(), name='recv'),
        asyncio.create_task(send_loop(), name='send'),
        asyncio.create_task(heartbeat_loop(), name='hb'),
        asyncio.create_task(exp_watchdog(), name='exp'),
        asyncio.create_task(closed_watcher(), name='closed'),
    ]
    try:
        done, _pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        first = next(iter(done))
        # If exp_watchdog or closed_watcher finished first, pick the matching
        # close code. recv_loop ending normally means client disconnected.
        close_code = status.WS_1000_NORMAL_CLOSURE
        if first.get_name() == 'exp':
            close_code = CLOSE_TOKEN_EXPIRED
        elif first.get_name() == 'closed':
            close_code = CLOSE_QUEUE_OVERFLOW
        for t in tasks:
            if not t.done():
                t.cancel()
        # Surface unexpected exceptions in logs (cancelled / disconnect are
        # normal and silenced).
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (asyncio.CancelledError, WebSocketDisconnect)):
                logger.exception('ws task %s crashed', t.get_name(), exc_info=exc)
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close(code=close_code)
            except RuntimeError:
                pass
    finally:
        await broker.remove(user_id, sub)
        for t in tasks:
            if not t.done():
                t.cancel()
        # Drain cancellations so we don't leak warnings.
        await asyncio.gather(*tasks, return_exceptions=True)
