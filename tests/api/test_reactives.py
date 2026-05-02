"""Stage 3 / 批次-3a — reactives KV REST CRUD.

KV-shaped twin of test_providers.py with additional cases for the composite
(user_id, key) PK that providers' single-id PK can't exercise:
- two users keep separate rows under the same key (PK uniqueness applies
  per-user, not globally)
- soft-delete tombstone applies per-user-per-key

Envelope shape: {key, version, updated_at, deleted, data}. `data` carries
the raw value blob (StoredReactive's `value` field) — the front-end
re-wraps to {key, value: row.data} for IndexedDB.
"""
from __future__ import annotations

import httpx
import pytest


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    r = await client_a.put(
        '/api/v1/reactives/%23user-data', json={'lastWorkspaceId': 'w1'}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['key'] == '#user-data'
    assert body['deleted'] is False
    assert body['data'] == {'lastWorkspaceId': 'w1'}
    assert body['version'] >= 1

    r = await client_a.get('/api/v1/reactives')
    assert r.status_code == 200
    rows = r.json()
    assert [row['key'] for row in rows] == ['#user-data']

    # Postgres truth.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, data, deleted_at FROM reactives WHERE key = %s',
            ('#user-data',),
        )
        owner, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert data == {'lastWorkspaceId': 'w1'}
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    await client_a.put('/api/v1/reactives/%23user-data', json={'lwid': 'w1'})
    r = await client_a.get('/api/v1/reactives/%23user-data')
    assert r.status_code == 200
    assert r.json()['key'] == '#user-data'
    assert r.json()['data'] == {'lwid': 'w1'}

    r = await client_a.get('/api/v1/reactives/missing-key')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    r1 = await client_a.put('/api/v1/reactives/%23user-data', json={'lwid': 'w1'})
    v1 = r1.json()['version']

    r2 = await client_a.put('/api/v1/reactives/%23user-data', json={'lwid': 'w2'})
    v2 = r2.json()['version']

    assert v2 > v1
    assert r2.json()['data']['lwid'] == 'w2'

    # GET reflects latest write.
    r3 = await client_a.get('/api/v1/reactives/%23user-data')
    assert r3.json()['version'] == v2
    assert r3.json()['data']['lwid'] == 'w2'


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    await client_a.put('/api/v1/reactives/k1', json={'v': 1})
    r2 = await client_a.put('/api/v1/reactives/k2', json={'v': 2})
    cutoff = r2.json()['version']
    r3 = await client_a.put('/api/v1/reactives/k3', json={'v': 3})

    rows = (await client_a.get(f'/api/v1/reactives?since={cutoff}')).json()
    keys = [row['key'] for row in rows]
    # Strictly greater than cutoff — k2 itself should be excluded.
    assert keys == ['k3']
    assert all(row['version'] > cutoff for row in rows)
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    await client_a.put('/api/v1/reactives/k1', json={'v': 1})

    r_del = await client_a.delete('/api/v1/reactives/k1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/reactives')).json()
    assert len(rows) == 1
    assert rows[0]['key'] == 'k1'
    assert rows[0]['deleted'] is True
    assert rows[0]['data'] is None

    # Postgres: the row is still there, just with deleted_at set.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT deleted_at FROM reactives WHERE key = %s', ('k1',)
        )
        (deleted_at,) = cur.fetchone()
    assert deleted_at is not None


async def test_delete_then_put_revives(client_a: httpx.AsyncClient) -> None:
    """A KV row deleted then re-put should clear the tombstone."""
    await client_a.put('/api/v1/reactives/k1', json={'v': 1})
    await client_a.delete('/api/v1/reactives/k1')

    r = await client_a.put('/api/v1/reactives/k1', json={'v': 2})
    assert r.status_code == 200
    body = r.json()
    assert body['deleted'] is False
    assert body['data'] == {'v': 2}

    # GET reads the live row.
    r2 = await client_a.get('/api/v1/reactives/k1')
    assert r2.status_code == 200
    assert r2.json()['deleted'] is False


async def test_account_isolation_same_key_two_users(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """Same key '#user-data' independently held by two users — composite PK
    means there is no cross-user collision."""
    await client_a.put('/api/v1/reactives/%23user-data', json={'lwid': 'A-ws'})
    await client_b.put('/api/v1/reactives/%23user-data', json={'lwid': 'B-ws'})

    rows_a = (await client_a.get('/api/v1/reactives')).json()
    rows_b = (await client_b.get('/api/v1/reactives')).json()
    assert [r['key'] for r in rows_a] == ['#user-data']
    assert [r['key'] for r in rows_b] == ['#user-data']
    assert rows_a[0]['data'] == {'lwid': 'A-ws'}
    assert rows_b[0]['data'] == {'lwid': 'B-ws'}

    # Cross-account read returns the asker's row, not the other user's data.
    a_get = await client_a.get('/api/v1/reactives/%23user-data')
    b_get = await client_b.get('/api/v1/reactives/%23user-data')
    assert a_get.json()['data'] == {'lwid': 'A-ws'}
    assert b_get.json()['data'] == {'lwid': 'B-ws'}


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/reactives')
    assert r.status_code == 401
