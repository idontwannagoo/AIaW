"""Stage 2 / Step 1 — realtime WS broker.

Maps the cloud-sync-migration plan's Stage 2 / Step 1 verification scenarios:
  1. WS auth via `Sec-WebSocket-Protocol: bearer.<token>` (no token → reject)
  2. Two WS subscriptions for the same user both receive the same event
  3. Cross-account isolation — A's WS gets nothing when B writes
  4. Subscribe with `since=0` replays existing rows then transitions to live
  5. Heartbeat timeout — server closes after ~35s of no pong (slow case)

These cover the "after-the-fact end-to-end checks" the plan listed under
Stage 2 / Step 1's progress snapshot, and previously only existed as ad-hoc
Console scripts.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets
import websockets.exceptions

from .conftest import WS_URL


# ---- 1. handshake auth ------------------------------------------------------


async def test_ws_without_subprotocol_is_rejected() -> None:
    # Server calls ws.close() before ws.accept() when the bearer subprotocol
    # is missing; the client sees an HTTP rejection rather than a clean close.
    with pytest.raises(
        (
            websockets.exceptions.InvalidStatus,
            websockets.exceptions.InvalidHandshake,
            websockets.exceptions.ConnectionClosed,
        )
    ):
        async with websockets.connect(WS_URL):
            pass


async def test_ws_with_invalid_token_is_rejected() -> None:
    with pytest.raises(
        (
            websockets.exceptions.InvalidStatus,
            websockets.exceptions.InvalidHandshake,
            websockets.exceptions.ConnectionClosed,
        )
    ):
        async with websockets.connect(
            WS_URL, subprotocols=['bearer.not-a-jwt']
        ):
            pass


async def test_ws_accepts_valid_token(ws_connect, user_a) -> None:
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'providers', 'since': 0})
        )
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        # Empty replay → rev=0 marker frame.
        assert msg == {'type': 'replay-done', 'table': 'providers', 'rev': 0}


# ---- 2. unknown table is a soft error, not a disconnect --------------------


async def test_ws_unknown_table_returns_error_frame(ws_connect, user_a) -> None:
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'no_such_table'})
        )
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert msg['type'] == 'error'
        assert msg['code'] == 'unknown-table'


# ---- 3. replay then live transition ----------------------------------------


async def test_ws_replay_emits_existing_rows_then_replay_done(
    ws_connect, user_a, client_a: httpx.AsyncClient,
) -> None:
    r1 = await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})
    r2 = await client_a.put('/api/v1/providers/p2', json={'name': 'p2'})
    v1 = r1.json()['version']
    v2 = r2.json()['version']

    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'providers', 'since': 0})
        )

        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert first['type'] == 'event'
        assert first['id'] == 'p1'
        assert first['rev'] == v1

        second = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert second['type'] == 'event'
        assert second['id'] == 'p2'
        assert second['rev'] == v2

        done = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert done == {
            'type': 'replay-done', 'table': 'providers', 'rev': v2,
        }


async def test_ws_replay_done_then_live_event(
    ws_connect, user_a, client_a: httpx.AsyncClient,
) -> None:
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'providers', 'since': 0})
        )
        # Empty table → immediately replay-done.
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert msg['type'] == 'replay-done'

        r = await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})
        assert r.status_code == 200

        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert evt['type'] == 'event'
        assert evt['op'] == 'put'
        assert evt['id'] == 'p1'
        assert evt['table'] == 'providers'
        assert evt['rev'] == r.json()['version']
        assert evt['row']['data'] == {'name': 'p1'}


async def test_ws_live_delete_event_is_tombstone(
    ws_connect, user_a, client_a: httpx.AsyncClient,
) -> None:
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'providers', 'since': 0})
        )
        await asyncio.wait_for(ws.recv(), timeout=5.0)  # replay-done

        r = await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})
        await asyncio.wait_for(ws.recv(), timeout=5.0)  # put event

        await client_a.delete('/api/v1/providers/p1')
        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert evt['type'] == 'event'
        assert evt['op'] == 'delete'
        assert evt['id'] == 'p1'
        assert evt['row'] is None


# ---- 4. fan-out to multiple subscribers + isolation ------------------------


async def test_ws_two_subscribers_same_user_both_receive(
    ws_connect, user_a, client_a: httpx.AsyncClient,
) -> None:
    async with ws_connect(user_a['access_token']) as ws1, ws_connect(
        user_a['access_token']
    ) as ws2:
        for ws in (ws1, ws2):
            await ws.send(
                json.dumps(
                    {'type': 'subscribe', 'table': 'providers', 'since': 0}
                )
            )
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert msg['type'] == 'replay-done'

        r = await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})
        assert r.status_code == 200

        for ws in (ws1, ws2):
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert evt['type'] == 'event'
            assert evt['id'] == 'p1'


async def test_ws_account_isolation(
    ws_connect, user_a, client_b: httpx.AsyncClient,
) -> None:
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'providers', 'since': 0})
        )
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert msg['type'] == 'replay-done'

        # B writes — A's WS must NOT see anything.
        r = await client_b.put('/api/v1/providers/p1', json={'name': 'p1'})
        assert r.status_code == 200

        with pytest.raises(asyncio.TimeoutError):
            # 1.5s budget: enough that a fan-out would have arrived; short
            # enough that the test stays snappy.
            await asyncio.wait_for(ws.recv(), timeout=1.5)


# ---- 5. unsubscribe stops events --------------------------------------------


async def test_ws_unsubscribe_stops_events(
    ws_connect, user_a, client_a: httpx.AsyncClient,
) -> None:
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'providers', 'since': 0})
        )
        await asyncio.wait_for(ws.recv(), timeout=5.0)  # replay-done

        await ws.send(json.dumps({'type': 'unsubscribe', 'table': 'providers'}))

        # Give the server a moment to apply the unsubscribe before publishing.
        await asyncio.sleep(0.2)
        await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=1.5)


# ---- 6. heartbeat timeout (slow) -------------------------------------------


@pytest.mark.slow
async def test_ws_heartbeat_timeout_closes_connection(
    ws_connect, user_a,
) -> None:
    """Server sends ping at T=25s, expects pong by T=35s. We deliberately
    swallow the ping and assert the connection is closed within ~36s."""
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(
            json.dumps({'type': 'subscribe', 'table': 'providers', 'since': 0})
        )
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert msg['type'] == 'replay-done'

        # First ping should land at ~T=25s. Be generous in case the loop
        # is busy.
        ping = json.loads(await asyncio.wait_for(ws.recv(), timeout=30.0))
        assert ping['type'] == 'ping'

        # Don't pong. Server should close within HEARTBEAT_GRACE (10s) of
        # the ping.
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=15.0)
