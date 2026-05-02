"""Server-Sent Events realtime endpoint (Stage 2 / Step 5).

Companion downgrade transport for clients that can't open a WebSocket. Same
broker / replay / per-row ``rev`` semantics as ``stream.py``; the only thing
that changes is the wire format. EventSource clients reconnect with
``Last-Event-ID: <rev>`` so the server seeds ``since`` from that header when
the query string omits it.

Wire (text/event-stream):

    event: event
    id: <rev>
    data: {"type":"event","table":"...","op":"put|delete","id":"...","rev":N,"row":{...}|null}

    event: replay-done
    data: {"type":"replay-done","table":"...","rev":N}

    event: error
    data: {"type":"error","code":"...","message":"..."}

Auth: standard ``Authorization: Bearer <jwt>`` header. EventSource doesn't
support custom headers natively, but ``event-source-polyfill`` does — that's
how the frontend ships it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from starlette.responses import StreamingResponse

from realtime import Subscription, broker

from ..auth import _decode_access
from ..db import SessionLocal
from ..models.assistant import Assistant
from ..models.avatar_image import AvatarImage
from ..models.installed_plugin import InstalledPlugin
from ..models.provider import Provider
from ..models.reactive import Reactive
from ..models.user import User

logger = logging.getLogger('aiaw.backend.realtime.sse')

router = APIRouter(tags=['stream'])

# Mirrors stream.py — kept separate so the WS module is the source of truth
# for the WS subprotocol path and SSE only depends on the public broker API.
TABLE_MODELS = {
    'providers': Provider,
    'reactives': Reactive,
    'assistants': Assistant,
    'avatar_images': AvatarImage,
    'installed_plugins': InstalledPlugin,
}

# SSE keepalives are comments; clients (including event-source-polyfill)
# treat them as heartbeats. Send slightly under typical proxy idle timeouts.
HEARTBEAT_INTERVAL = 25.0


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


def _format_sse(*, event: str, data: dict[str, Any], event_id: Optional[int] = None) -> bytes:
    lines = [f'event: {event}']
    if event_id is not None:
        lines.append(f'id: {event_id}')
    payload = json.dumps(data, separators=(',', ':'))
    lines.append(f'data: {payload}')
    lines.append('')
    lines.append('')
    return '\n'.join(lines).encode('utf-8')


async def _validate_token(token: str) -> Optional[str]:
    """Decode + validate JWT, return user_id or None.

    Mirrors stream._validate_token but tailored to the SSE GET path: we don't
    need the exp timestamp here because the StreamingResponse generator
    detects client disconnect via Request.is_disconnected; clients that hold
    a stream open past their token's exp will hit a 401 on their next REST
    call and refresh + reconnect.
    """
    try:
        payload = _decode_access(token)
    except HTTPException:
        return None
    user_id = payload.get('sub')
    if not user_id:
        return None
    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
    if user is None or user.status != 'active':
        return None
    return user_id


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


def _parse_authorization(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.strip().split(' ', 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    token = parts[1].strip()
    return token or None


@router.get('/api/v1/stream/sse')
async def stream_sse(
    request: Request,
    tables: str = Query('providers'),
    since: Optional[int] = Query(None),
    authorization: Optional[str] = Header(None),
    last_event_id: Optional[str] = Header(None, alias='Last-Event-ID'),
) -> StreamingResponse:
    token = _parse_authorization(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='missing bearer')
    user_id = await _validate_token(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid token')

    # Last-Event-ID is the EventSource resume hook: when the query string
    # omits ``since``, fall back to the header so reconnects don't replay
    # from zero.
    effective_since = since
    if effective_since is None and last_event_id:
        try:
            effective_since = int(last_event_id)
        except ValueError:
            effective_since = 0
    if effective_since is None:
        effective_since = 0

    requested = [t.strip() for t in tables.split(',') if t.strip()]
    valid = [t for t in requested if t in TABLE_MODELS]
    invalid = [t for t in requested if t not in TABLE_MODELS]

    sub = Subscription()
    sub.tables.update(valid)
    await broker.add(user_id, sub)

    async def gen() -> AsyncIterator[bytes]:
        try:
            for bad in invalid:
                yield _format_sse(
                    event='error',
                    data={
                        'type': 'error',
                        'code': 'unknown-table',
                        'message': f'table {bad!r} not subscribable',
                    },
                )

            # Replay each requested table, then send a per-table replay-done.
            # Ordering inside one table matches version ASC; cross-table order
            # is whatever Python iteration produces — the client treats events
            # per-table anyway.
            for table in valid:
                events = await _replay_table(user_id, table, effective_since)
                last_rev = effective_since
                for evt in events:
                    last_rev = evt['rev']
                    yield _format_sse(event='event', data=evt, event_id=last_rev)
                yield _format_sse(
                    event='replay-done',
                    data={
                        'type': 'replay-done',
                        'table': table,
                        'rev': last_rev,
                    },
                )

            # Live mode: pull from the per-user broker queue. Keepalive comments
            # go out periodically so the connection survives idle proxies.
            last_keepalive = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                if sub.closed.is_set():
                    yield _format_sse(
                        event='error',
                        data={
                            'type': 'error',
                            'code': 'queue-overflow',
                            'message': 'subscription queue overflow',
                        },
                    )
                    return
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_keepalive >= HEARTBEAT_INTERVAL:
                        last_keepalive = time.monotonic()
                        yield b': keepalive\n\n'
                    continue
                table = event.get('table')
                if table not in sub.tables:
                    continue
                yield _format_sse(
                    event='event', data=event, event_id=event.get('rev'),
                )
        finally:
            await broker.remove(user_id, sub)

    headers = {
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    }
    return StreamingResponse(
        gen(), media_type='text/event-stream', headers=headers,
    )
