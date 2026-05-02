"""Stage 1 / Step 2 — providers REST CRUD.

Covers all 6 of the cloud-sync-migration plan's "通过判据" for Step 2 plus
direct Postgres assertion for the row, mapping each onto a named test so
the plan's criteria can be re-verified by `pnpm test:api`.

Note we intentionally do NOT exercise the global_change_seq's exact integer
values — the seq is shared across all providers tables and is reset per-
test by db_reset, so the *first* version is always 1, *next* is 2, etc.
Tests assert monotonicity (newer > older) rather than absolute values, so
they survive any future tables that draw from the same sequence.
"""
from __future__ import annotations

import httpx
import pytest


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    r = await client_a.put(
        '/api/v1/providers/p1', json={'kind': 'openai', 'name': 'p1'}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['id'] == 'p1'
    assert body['deleted'] is False
    assert body['data'] == {'kind': 'openai', 'name': 'p1'}
    assert body['version'] >= 1

    r = await client_a.get('/api/v1/providers')
    assert r.status_code == 200
    rows = r.json()
    assert [row['id'] for row in rows] == ['p1']

    # Postgres truth.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, data, deleted_at FROM providers WHERE id = %s',
            ('p1',),
        )
        owner, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert data == {'kind': 'openai', 'name': 'p1'}
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    await client_a.put(
        '/api/v1/providers/p1', json={'kind': 'openai', 'name': 'p1'}
    )
    r = await client_a.get('/api/v1/providers/p1')
    assert r.status_code == 200
    assert r.json()['id'] == 'p1'

    r = await client_a.get('/api/v1/providers/missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    r1 = await client_a.put(
        '/api/v1/providers/p1', json={'kind': 'openai', 'name': 'v1'}
    )
    v1 = r1.json()['version']

    r2 = await client_a.put(
        '/api/v1/providers/p1', json={'kind': 'openai', 'name': 'v2'}
    )
    v2 = r2.json()['version']

    assert v2 > v1
    assert r2.json()['data']['name'] == 'v2'

    # GET reflects the latest write.
    r3 = await client_a.get('/api/v1/providers/p1')
    assert r3.json()['version'] == v2
    assert r3.json()['data']['name'] == 'v2'


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})
    r2 = await client_a.put('/api/v1/providers/p2', json={'name': 'p2'})
    cutoff = r2.json()['version']
    r3 = await client_a.put('/api/v1/providers/p3', json={'name': 'p3'})

    rows = (await client_a.get(f'/api/v1/providers?since={cutoff}')).json()
    ids = [row['id'] for row in rows]
    # Strictly greater than cutoff — p2 itself should be excluded.
    assert ids == ['p3']
    assert all(row['version'] > cutoff for row in rows)
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    await client_a.put('/api/v1/providers/p1', json={'name': 'p1'})

    r_del = await client_a.delete('/api/v1/providers/p1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/providers')).json()
    assert len(rows) == 1
    assert rows[0]['id'] == 'p1'
    assert rows[0]['deleted'] is True
    assert rows[0]['data'] is None

    # Postgres: the row is still there, just with deleted_at set.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT deleted_at FROM providers WHERE id = %s', ('p1',)
        )
        (deleted_at,) = cur.fetchone()
    assert deleted_at is not None


async def test_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    # A writes p1.
    await client_a.put('/api/v1/providers/p1', json={'name': 'a-p1'})
    # B's list must be empty.
    rows_b = (await client_b.get('/api/v1/providers')).json()
    assert rows_b == [], f'B leaked A row: {rows_b!r}'
    # B's GET on A's id is a 404, not somebody else's data.
    r = await client_b.get('/api/v1/providers/p1')
    assert r.status_code == 404


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/providers')
    assert r.status_code == 401
