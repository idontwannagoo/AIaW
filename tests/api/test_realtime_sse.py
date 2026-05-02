"""Stage 2 / Step 5 — SSE downgrade endpoint.

The SSE endpoint mirrors the WS endpoint's wire model:

    GET /api/v1/stream/sse?since=<rev>&tables=providers
    Authorization: Bearer <jwt>

Server emits ``text/event-stream`` frames. Each event is one of:

    event: event
    id: <rev>
    data: {"type":"event","table":"...","op":"put|delete","id":"...","rev":N,"row":{...}|null}

    event: replay-done
    data: {"type":"replay-done","table":"...","rev":N}

    event: error
    data: {"type":"error","code":"...","message":"..."}

EventSource clients reconnect with ``Last-Event-ID: <rev>`` to resume; the
server's ``since`` defaults to ``Last-Event-ID`` if the query string omits it.

These tests use ``httpx`` to consume the stream so we don't pull in an
EventSource client lib for tests.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx
import pytest

from .conftest import BACKEND_URL


def _parse_sse_block(block: str) -> dict[str, Any] | None:
    """Parse a single SSE event block (text between two blank lines).

    Returns a dict with keys ``event`` / ``id`` / ``data`` (only those that
    appear). ``data`` is JSON-decoded if non-empty.
    """
    if not block.strip():
        return None
    out: dict[str, Any] = {}
    data_lines: list[str] = []
    for line in block.splitlines():
        if not line or line.startswith(':'):
            continue
        if ':' in line:
            field, _, value = line.partition(':')
            value = value.lstrip(' ')
        else:
            field, value = line, ''
        if field == 'data':
            data_lines.append(value)
        elif field in ('event', 'id', 'retry'):
            out[field] = value
    if data_lines:
        joined = '\n'.join(data_lines)
        try:
            out['data'] = json.loads(joined)
        except json.JSONDecodeError:
            out['data'] = joined
    return out or None


class SseReader:
    """Drain SSE event blocks across multiple calls on one streaming response.

    httpx ``aiter_text`` / ``aiter_bytes`` can only be iterated once per
    response, so we hold a single iterator + a buffer in instance state and
    let the test pull events incrementally.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._aiter = response.aiter_text()
        self._buf = ''

    async def read_events(
        self, *, expect: int, timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        # Drain whatever's already in the buffer first.
        while '\n\n' in self._buf and len(events) < expect:
            block, self._buf = self._buf.split('\n\n', 1)
            parsed = _parse_sse_block(block)
            if parsed is not None:
                events.append(parsed)

        async def drain() -> None:
            async for chunk in self._aiter:
                self._buf += chunk
                while '\n\n' in self._buf and len(events) < expect:
                    block, self._buf = self._buf.split('\n\n', 1)
                    parsed = _parse_sse_block(block)
                    if parsed is not None:
                        events.append(parsed)
                if len(events) >= expect:
                    return

        if len(events) < expect:
            await asyncio.wait_for(drain(), timeout=timeout)
        return events


async def _open_sse(
    client: httpx.AsyncClient,
    token: str | None,
    *,
    since: int | None = None,
    tables: str = 'providers',
    last_event_id: str | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {'Accept': 'text/event-stream'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if last_event_id is not None:
        headers['Last-Event-ID'] = last_event_id
    params: dict[str, Any] = {'tables': tables}
    if since is not None:
        params['since'] = since
    req = client.build_request(
        'GET', '/api/v1/stream/sse', params=params, headers=headers
    )
    return await client.send(req, stream=True)


# ---- 1. auth boundary ------------------------------------------------------


async def test_sse_without_token_rejected() -> None:
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=5.0) as client:
        resp = await _open_sse(client, token=None)
        try:
            assert resp.status_code == 401
        finally:
            await resp.aclose()


async def test_sse_with_invalid_token_rejected() -> None:
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=5.0) as client:
        resp = await _open_sse(client, token='not-a-jwt')
        try:
            assert resp.status_code == 401
        finally:
            await resp.aclose()


# ---- 2. replay then live ---------------------------------------------------


async def test_sse_replay_then_live(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    """A subscribes after seeding 1 row → sees replay event + replay-done,
    then a fresh PUT lands as a live event on the same stream."""
    r1 = await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})
    assert r1.status_code == 200
    v1 = r1.json()['version']

    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=10.0) as sse_client:
        resp = await _open_sse(sse_client, user_a['access_token'], since=0)
        try:
            assert resp.status_code == 200
            assert 'text/event-stream' in resp.headers.get('content-type', '')
            reader = SseReader(resp)

            # 1 replay event + 1 replay-done.
            initial = await reader.read_events(expect=2, timeout=5.0)
            assert initial[0].get('event') == 'event'
            assert initial[0]['data']['id'] == 'p1'
            assert initial[0]['data']['rev'] == v1
            assert initial[0].get('id') == str(v1)
            assert initial[1].get('event') == 'replay-done'
            assert initial[1]['data']['rev'] == v1

            # Live PUT → event on same stream.
            r2 = await client_a.put('/api/v1/providers/p2', json={'name': 'p2'})
            assert r2.status_code == 200
            v2 = r2.json()['version']

            live = await reader.read_events(expect=1, timeout=5.0)
            assert live[0].get('event') == 'event'
            assert live[0]['data']['op'] == 'put'
            assert live[0]['data']['id'] == 'p2'
            assert live[0]['data']['rev'] == v2
            assert live[0].get('id') == str(v2)
        finally:
            await resp.aclose()


# ---- 3. account isolation --------------------------------------------------


async def test_sse_account_isolation(
    user_a, client_b: httpx.AsyncClient,
) -> None:
    """A's SSE stream must not see B's writes."""
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=10.0) as sse_client:
        resp = await _open_sse(sse_client, user_a['access_token'], since=0)
        try:
            reader = SseReader(resp)
            # Initial replay-done on empty table.
            initial = await reader.read_events(expect=1, timeout=5.0)
            assert initial[0].get('event') == 'replay-done'

            # B writes — A's stream must NOT carry the event.
            r = await client_b.put('/api/v1/providers/p1', json={'name': 'p1'})
            assert r.status_code == 200

            with pytest.raises(asyncio.TimeoutError):
                await reader.read_events(expect=1, timeout=1.5)
        finally:
            await resp.aclose()


# ---- 4. Last-Event-ID resumes from rev -------------------------------------


async def test_sse_last_event_id_resumes_from_rev(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    """Reconnect with Last-Event-ID=<rev>; only events with rev > that
    Last-Event-ID are replayed (server uses the header when ``since`` is
    omitted from the query string)."""
    r1 = await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})
    r2 = await client_a.put('/api/v1/providers/p2', json={'name': 'p2'})
    v1 = r1.json()['version']
    v2 = r2.json()['version']
    assert v2 > v1

    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=10.0) as sse_client:
        resp = await _open_sse(
            sse_client,
            user_a['access_token'],
            last_event_id=str(v1),
        )
        try:
            reader = SseReader(resp)
            # Should replay only p2 (rev > v1), then replay-done.
            events = await reader.read_events(expect=2, timeout=5.0)
            assert events[0].get('event') == 'event'
            assert events[0]['data']['id'] == 'p2'
            assert events[0]['data']['rev'] == v2
            assert events[1].get('event') == 'replay-done'
            assert events[1]['data']['rev'] == v2
        finally:
            await resp.aclose()
