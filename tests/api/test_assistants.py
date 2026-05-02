"""Stage 3 / 批次-3b — assistants REST CRUD.

Mirrors test_providers.py — assistants is the same id-PK envelope shape.
Test names map 1:1 onto the plan's Step "通过判据" so failure messages stay
readable in `pnpm test:api` output.
"""
from __future__ import annotations

import httpx
import pytest


_DEFAULT_DATA = {
    'name': 'My Assistant',
    'avatar': {'type': 'icon', 'icon': 'sym_o_robot'},
    'workspaceId': 'ws-1',
    'prompt': 'You are a helpful assistant',
    'promptTemplate': '',
    'promptVars': [],
    'provider': None,
    'model': None,
    'modelSettings': {'temperature': 0.6, 'topP': 1, 'presencePenalty': 0, 'frequencyPenalty': 0, 'maxSteps': 4, 'maxRetries': 1},
    'plugins': {},
    'promptRole': 'system',
    'stream': True,
}


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    r = await client_a.put('/api/v1/assistants/a1', json=_DEFAULT_DATA)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['id'] == 'a1'
    assert body['deleted'] is False
    assert body['data']['name'] == 'My Assistant'
    assert body['version'] >= 1

    r = await client_a.get('/api/v1/assistants')
    assert r.status_code == 200
    rows = r.json()
    assert [row['id'] for row in rows] == ['a1']

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, data, deleted_at FROM assistants WHERE id = %s',
            ('a1',),
        )
        owner, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert data['name'] == 'My Assistant'
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    await client_a.put('/api/v1/assistants/a1', json=_DEFAULT_DATA)
    r = await client_a.get('/api/v1/assistants/a1')
    assert r.status_code == 200
    assert r.json()['id'] == 'a1'

    r = await client_a.get('/api/v1/assistants/missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    r1 = await client_a.put(
        '/api/v1/assistants/a1', json={**_DEFAULT_DATA, 'name': 'v1'}
    )
    v1 = r1.json()['version']

    r2 = await client_a.put(
        '/api/v1/assistants/a1', json={**_DEFAULT_DATA, 'name': 'v2'}
    )
    v2 = r2.json()['version']

    assert v2 > v1
    assert r2.json()['data']['name'] == 'v2'

    r3 = await client_a.get('/api/v1/assistants/a1')
    assert r3.json()['version'] == v2
    assert r3.json()['data']['name'] == 'v2'


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    await client_a.put(
        '/api/v1/assistants/a1', json={**_DEFAULT_DATA, 'name': 'a1'}
    )
    r2 = await client_a.put(
        '/api/v1/assistants/a2', json={**_DEFAULT_DATA, 'name': 'a2'}
    )
    cutoff = r2.json()['version']
    r3 = await client_a.put(
        '/api/v1/assistants/a3', json={**_DEFAULT_DATA, 'name': 'a3'}
    )

    rows = (await client_a.get(f'/api/v1/assistants?since={cutoff}')).json()
    ids = [row['id'] for row in rows]
    assert ids == ['a3']
    assert all(row['version'] > cutoff for row in rows)
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    await client_a.put('/api/v1/assistants/a1', json=_DEFAULT_DATA)

    r_del = await client_a.delete('/api/v1/assistants/a1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/assistants')).json()
    assert len(rows) == 1
    assert rows[0]['id'] == 'a1'
    assert rows[0]['deleted'] is True
    assert rows[0]['data'] is None

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT deleted_at FROM assistants WHERE id = %s', ('a1',)
        )
        (deleted_at,) = cur.fetchone()
    assert deleted_at is not None


async def test_delete_then_put_revives(client_a: httpx.AsyncClient) -> None:
    await client_a.put(
        '/api/v1/assistants/a1', json={**_DEFAULT_DATA, 'name': 'pre-delete'}
    )
    await client_a.delete('/api/v1/assistants/a1')

    r = await client_a.put(
        '/api/v1/assistants/a1', json={**_DEFAULT_DATA, 'name': 'revived'}
    )
    assert r.status_code == 200
    body = r.json()
    assert body['deleted'] is False
    assert body['data']['name'] == 'revived'


async def test_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    await client_a.put(
        '/api/v1/assistants/a1', json={**_DEFAULT_DATA, 'name': 'a-only'}
    )
    rows_b = (await client_b.get('/api/v1/assistants')).json()
    assert rows_b == [], f'B leaked A row: {rows_b!r}'
    r = await client_b.get('/api/v1/assistants/a1')
    assert r.status_code == 404


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/assistants')
    assert r.status_code == 401
