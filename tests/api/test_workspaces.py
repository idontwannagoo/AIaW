"""Stage 4 / 批次-4a — workspaces REST CRUD.

Mirrors test_assistants.py — workspaces is the same id-PK envelope shape
plus a few schema-decision cases unique to 4a:

- type ∈ {'workspace', 'folder'} round-trips inside `data` (envelope is
  uniform across both kinds; the discriminator stays in data, see
  models/workspace.py module docstring)
- folder/workspace parentId chain ('$root' sentinel + nested folder ids)
  round-trips verbatim — there is no self-FK
- DELETE accepts ?cascade=true|false but is a no-op at 4a (child tables
  still in Dexie); the parameter is forward-compat only

Test names map 1:1 onto the plan's 批次-4a "通过判据" so failure messages
stay readable in `pnpm test:api` output.
"""
from __future__ import annotations

import httpx
import pytest


_DEFAULT_WORKSPACE = {
    'name': 'Workspace 1',
    'avatar': {'type': 'icon', 'icon': 'sym_o_deployed_code'},
    'type': 'workspace',
    'parentId': '$root',
    'prompt': '',
    'indexContent': '# index',
    'vars': {},
    'listOpen': {
        'assistants': True,
        'artifacts': False,
        'dialogs': True,
    },
}


_DEFAULT_FOLDER = {
    'name': 'Folder 1',
    'avatar': {'type': 'icon', 'icon': 'sym_o_folder'},
    'type': 'folder',
    'parentId': '$root',
}


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    r = await client_a.put('/api/v1/workspaces/w1', json=_DEFAULT_WORKSPACE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['id'] == 'w1'
    assert body['deleted'] is False
    assert body['data']['name'] == 'Workspace 1'
    assert body['data']['type'] == 'workspace'
    assert body['data']['parentId'] == '$root'
    assert body['version'] >= 1

    r = await client_a.get('/api/v1/workspaces')
    assert r.status_code == 200
    rows = r.json()
    assert [row['id'] for row in rows] == ['w1']

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, data, deleted_at FROM workspaces WHERE id = %s',
            ('w1',),
        )
        owner, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert data['name'] == 'Workspace 1'
    assert data['type'] == 'workspace'
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    await client_a.put('/api/v1/workspaces/w1', json=_DEFAULT_WORKSPACE)
    r = await client_a.get('/api/v1/workspaces/w1')
    assert r.status_code == 200
    assert r.json()['id'] == 'w1'

    r = await client_a.get('/api/v1/workspaces/missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    r1 = await client_a.put(
        '/api/v1/workspaces/w1', json={**_DEFAULT_WORKSPACE, 'name': 'v1'}
    )
    v1 = r1.json()['version']

    r2 = await client_a.put(
        '/api/v1/workspaces/w1', json={**_DEFAULT_WORKSPACE, 'name': 'v2'}
    )
    v2 = r2.json()['version']

    assert v2 > v1
    assert r2.json()['data']['name'] == 'v2'

    r3 = await client_a.get('/api/v1/workspaces/w1')
    assert r3.json()['version'] == v2
    assert r3.json()['data']['name'] == 'v2'


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    await client_a.put(
        '/api/v1/workspaces/w1', json={**_DEFAULT_WORKSPACE, 'name': 'w1'}
    )
    r2 = await client_a.put(
        '/api/v1/workspaces/w2', json={**_DEFAULT_WORKSPACE, 'name': 'w2'}
    )
    cutoff = r2.json()['version']
    r3 = await client_a.put(
        '/api/v1/workspaces/w3', json={**_DEFAULT_WORKSPACE, 'name': 'w3'}
    )

    rows = (await client_a.get(f'/api/v1/workspaces?since={cutoff}')).json()
    ids = [row['id'] for row in rows]
    assert ids == ['w3']
    assert all(row['version'] > cutoff for row in rows)
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    await client_a.put('/api/v1/workspaces/w1', json=_DEFAULT_WORKSPACE)

    r_del = await client_a.delete('/api/v1/workspaces/w1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/workspaces')).json()
    assert len(rows) == 1
    assert rows[0]['id'] == 'w1'
    assert rows[0]['deleted'] is True
    assert rows[0]['data'] is None

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT deleted_at FROM workspaces WHERE id = %s', ('w1',)
        )
        (deleted_at,) = cur.fetchone()
    assert deleted_at is not None


async def test_delete_then_put_revives(client_a: httpx.AsyncClient) -> None:
    await client_a.put(
        '/api/v1/workspaces/w1',
        json={**_DEFAULT_WORKSPACE, 'name': 'pre-delete'},
    )
    await client_a.delete('/api/v1/workspaces/w1')

    r = await client_a.put(
        '/api/v1/workspaces/w1', json={**_DEFAULT_WORKSPACE, 'name': 'revived'}
    )
    assert r.status_code == 200
    body = r.json()
    assert body['deleted'] is False
    assert body['data']['name'] == 'revived'


async def test_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    await client_a.put(
        '/api/v1/workspaces/w1', json={**_DEFAULT_WORKSPACE, 'name': 'a-only'}
    )
    rows_b = (await client_b.get('/api/v1/workspaces')).json()
    assert rows_b == [], f'B leaked A row: {rows_b!r}'
    r = await client_b.get('/api/v1/workspaces/w1')
    assert r.status_code == 404


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/workspaces')
    assert r.status_code == 401


async def test_folder_type_round_trips(client_a: httpx.AsyncClient) -> None:
    """folder rows carry just {id,name,avatar,type,parentId} — no workspace
    fields. Envelope must round-trip the smaller shape verbatim, including
    the absence of the workspace-only fields."""
    r = await client_a.put('/api/v1/workspaces/f1', json=_DEFAULT_FOLDER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['data']['type'] == 'folder'
    assert body['data']['parentId'] == '$root'
    assert 'vars' not in body['data']
    assert 'listOpen' not in body['data']

    r = await client_a.get('/api/v1/workspaces/f1')
    assert r.json()['data'] == _DEFAULT_FOLDER


async def test_folder_tree_with_parent_chain(
    client_a: httpx.AsyncClient,
) -> None:
    """parentId is a sentinel string ('$root') or another row id; there is
    no self-FK, so deeply nested chains round-trip without referential
    integrity rejecting them. Two folders + a workspace under the deeper
    folder is enough to verify the chain isn't being normalized away."""
    await client_a.put(
        '/api/v1/workspaces/f1',
        json={**_DEFAULT_FOLDER, 'name': 'top', 'parentId': '$root'},
    )
    await client_a.put(
        '/api/v1/workspaces/f2',
        json={**_DEFAULT_FOLDER, 'name': 'mid', 'parentId': 'f1'},
    )
    await client_a.put(
        '/api/v1/workspaces/w-leaf',
        json={**_DEFAULT_WORKSPACE, 'name': 'leaf', 'parentId': 'f2'},
    )

    rows = (await client_a.get('/api/v1/workspaces')).json()
    by_id = {row['id']: row['data'] for row in rows}
    assert by_id['f1']['parentId'] == '$root'
    assert by_id['f2']['parentId'] == 'f1'
    assert by_id['w-leaf']['parentId'] == 'f2'
    assert by_id['w-leaf']['type'] == 'workspace'
    assert by_id['f2']['type'] == 'folder'


async def test_cascade_param_accepted_no_op_at_4a(
    client_a: httpx.AsyncClient,
) -> None:
    """`?cascade=true|false` is forward-compat at 4a — no server-routed
    child tables exist yet for cascade to act on. Both values must
    succeed with a tombstone (not 422), so future stages can grow the
    impl without breaking the wire contract or the frontend's
    stores/workspaces.ts call site."""
    await client_a.put('/api/v1/workspaces/w1', json=_DEFAULT_WORKSPACE)
    r = await client_a.delete('/api/v1/workspaces/w1?cascade=true')
    assert r.status_code == 200, r.text
    assert r.json()['deleted'] is True

    await client_a.put('/api/v1/workspaces/w2', json=_DEFAULT_WORKSPACE)
    r = await client_a.delete('/api/v1/workspaces/w2?cascade=false')
    assert r.status_code == 200, r.text
    assert r.json()['deleted'] is True


async def test_cross_user_cannot_delete(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """B cannot delete A's workspace — the row stays alive on A's side
    (not a tombstone). PUT collision is a separate test path (id taken
    by another user → 409); DELETE just 404s because we filter by
    user_id in the WHERE clause."""
    await client_a.put('/api/v1/workspaces/w-shared', json=_DEFAULT_WORKSPACE)

    r = await client_b.delete('/api/v1/workspaces/w-shared')
    assert r.status_code == 404

    r = await client_a.get('/api/v1/workspaces/w-shared')
    assert r.status_code == 200
    assert r.json()['deleted'] is False
