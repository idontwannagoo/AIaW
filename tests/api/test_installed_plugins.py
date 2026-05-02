"""Stage 3 / 批次-3b — installed_plugins KV REST CRUD.

Mirrors test_reactives.py — both use composite (user_id, key) PK so the
same key on two users is two distinct rows. The unique-to-this-table case:
plugin keys are namespaced with `:` (`lobe:foo`, `mcp:bar`); the route
uses `{key:path}` so we do NOT need to URL-encode the colon. We also
exercise account isolation under same key (composite PK semantics).

`data` in this envelope carries the FULL InstalledPlugin row (id / type /
manifest), distinct from reactives where `data` is the value blob only —
front-end re-wraps differently for each.
"""
from __future__ import annotations

import httpx
import pytest


_LOBE_DATA = {
    'id': 'plugin-id-1',
    'key': 'lobe:foo',
    'type': 'lobe',
    'available': True,
    'manifest': {'identifier': 'foo', 'meta': {}, 'api': []},
}

_MCP_DATA = {
    'id': 'plugin-id-2',
    'key': 'mcp:bar',
    'type': 'mcp',
    'available': True,
    'manifest': {
        'id': 'bar',
        'title': 'Bar Plugin',
        'transport': {'type': 'http', 'url': 'https://example.invalid'},
        'tools': [],
        'resources': [],
        'prompts': [],
    },
}


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    r = await client_a.put('/api/v1/installed-plugins/lobe:foo', json=_LOBE_DATA)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['key'] == 'lobe:foo'
    assert body['deleted'] is False
    assert body['data']['type'] == 'lobe'
    assert body['data']['manifest']['identifier'] == 'foo'
    assert body['version'] >= 1

    r = await client_a.get('/api/v1/installed-plugins')
    assert r.status_code == 200
    rows = r.json()
    assert [row['key'] for row in rows] == ['lobe:foo']

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, data, deleted_at FROM installed_plugins '
            'WHERE key = %s',
            ('lobe:foo',),
        )
        owner, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert data['type'] == 'lobe'
    assert deleted_at is None


async def test_get_returns_single_row_with_namespaced_key(
    client_a: httpx.AsyncClient,
) -> None:
    await client_a.put('/api/v1/installed-plugins/mcp:bar', json=_MCP_DATA)
    r = await client_a.get('/api/v1/installed-plugins/mcp:bar')
    assert r.status_code == 200
    assert r.json()['key'] == 'mcp:bar'
    assert r.json()['data']['type'] == 'mcp'

    r = await client_a.get('/api/v1/installed-plugins/lobe:missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    r1 = await client_a.put(
        '/api/v1/installed-plugins/lobe:foo',
        json={**_LOBE_DATA, 'available': True},
    )
    v1 = r1.json()['version']

    r2 = await client_a.put(
        '/api/v1/installed-plugins/lobe:foo',
        json={**_LOBE_DATA, 'available': False},
    )
    v2 = r2.json()['version']

    assert v2 > v1
    assert r2.json()['data']['available'] is False

    r3 = await client_a.get('/api/v1/installed-plugins/lobe:foo')
    assert r3.json()['version'] == v2
    assert r3.json()['data']['available'] is False


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    await client_a.put('/api/v1/installed-plugins/lobe:k1', json={**_LOBE_DATA, 'key': 'lobe:k1'})
    r2 = await client_a.put('/api/v1/installed-plugins/lobe:k2', json={**_LOBE_DATA, 'key': 'lobe:k2'})
    cutoff = r2.json()['version']
    r3 = await client_a.put('/api/v1/installed-plugins/lobe:k3', json={**_LOBE_DATA, 'key': 'lobe:k3'})

    rows = (await client_a.get(f'/api/v1/installed-plugins?since={cutoff}')).json()
    keys = [row['key'] for row in rows]
    assert keys == ['lobe:k3']
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    await client_a.put('/api/v1/installed-plugins/lobe:foo', json=_LOBE_DATA)

    r_del = await client_a.delete('/api/v1/installed-plugins/lobe:foo')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/installed-plugins')).json()
    assert len(rows) == 1
    assert rows[0]['key'] == 'lobe:foo'
    assert rows[0]['deleted'] is True

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT deleted_at FROM installed_plugins WHERE key = %s',
            ('lobe:foo',),
        )
        (deleted_at,) = cur.fetchone()
    assert deleted_at is not None


async def test_delete_then_put_revives(client_a: httpx.AsyncClient) -> None:
    await client_a.put('/api/v1/installed-plugins/lobe:foo', json=_LOBE_DATA)
    await client_a.delete('/api/v1/installed-plugins/lobe:foo')

    r = await client_a.put(
        '/api/v1/installed-plugins/lobe:foo',
        json={**_LOBE_DATA, 'available': False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body['deleted'] is False
    assert body['data']['available'] is False

    r2 = await client_a.get('/api/v1/installed-plugins/lobe:foo')
    assert r2.status_code == 200
    assert r2.json()['deleted'] is False


async def test_account_isolation_same_key_two_users(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """Same plugin key 'lobe:foo' independently held by A and B — composite
    PK means there is no cross-user collision."""
    await client_a.put(
        '/api/v1/installed-plugins/lobe:foo',
        json={**_LOBE_DATA, 'manifest': {'identifier': 'A-foo', 'meta': {}, 'api': []}},
    )
    await client_b.put(
        '/api/v1/installed-plugins/lobe:foo',
        json={**_LOBE_DATA, 'manifest': {'identifier': 'B-foo', 'meta': {}, 'api': []}},
    )

    rows_a = (await client_a.get('/api/v1/installed-plugins')).json()
    rows_b = (await client_b.get('/api/v1/installed-plugins')).json()
    assert [r['key'] for r in rows_a] == ['lobe:foo']
    assert [r['key'] for r in rows_b] == ['lobe:foo']
    assert rows_a[0]['data']['manifest']['identifier'] == 'A-foo'
    assert rows_b[0]['data']['manifest']['identifier'] == 'B-foo'


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/installed-plugins')
    assert r.status_code == 401
