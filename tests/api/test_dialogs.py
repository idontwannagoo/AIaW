"""Stage 4 / 批次-4b — dialogs REST CRUD + workspace cascade.

Mirrors test_workspaces.py — same id-PK envelope + soft-delete tombstone.
Adds the schema-decision cases unique to 4b:

- workspaceId is required in body and validated against workspaces (a
  missing or cross-user workspace returns 409, not 500)
- DELETE /api/v1/workspaces/{id}?cascade=true tombstones all dialogs
  for that workspace in the same transaction (server-side cascade —
  4a was no-op here)
- cascade=false leaves dialogs alive
- TRUNCATE in conftest happens with `RESTART IDENTITY CASCADE` so the
  FK to workspaces doesn't break per-test cleanup

Test names map 1:1 onto the plan's 批次-4b "通过判据" so failure
messages stay readable in `pnpm test:api` output.
"""
from __future__ import annotations

import httpx
import pytest


_DEFAULT_WORKSPACE = {
    'name': 'WS-1',
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


def _dialog_body(workspace_id: str, **overrides) -> dict:
    body = {
        'name': 'D-1',
        'workspaceId': workspace_id,
        'msgTree': {'$root': []},
        'msgRoute': [],
        'inputVars': {},
    }
    body.update(overrides)
    return body


async def _seed_workspace(client: httpx.AsyncClient, ws_id: str = 'w1') -> str:
    r = await client.put(f'/api/v1/workspaces/{ws_id}', json=_DEFAULT_WORKSPACE)
    assert r.status_code == 200, r.text
    return ws_id


# ---- CRUD shape (mirrors workspaces 1:1) -----------------------------------


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    ws_id = await _seed_workspace(client_a)

    r = await client_a.put(
        '/api/v1/dialogs/d1', json=_dialog_body(ws_id, name='Dialog 1')
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['id'] == 'd1'
    assert body['deleted'] is False
    assert body['data']['name'] == 'Dialog 1'
    assert body['data']['workspaceId'] == ws_id
    assert body['version'] >= 1

    r = await client_a.get('/api/v1/dialogs')
    assert r.status_code == 200
    rows = r.json()
    assert [row['id'] for row in rows] == ['d1']

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, workspace_id, data, deleted_at FROM dialogs '
            'WHERE id = %s',
            ('d1',),
        )
        owner, ws_col, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert ws_col == ws_id
    assert data['name'] == 'Dialog 1'
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put('/api/v1/dialogs/d1', json=_dialog_body(ws_id))

    r = await client_a.get('/api/v1/dialogs/d1')
    assert r.status_code == 200
    assert r.json()['id'] == 'd1'

    r = await client_a.get('/api/v1/dialogs/missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    r1 = await client_a.put(
        '/api/v1/dialogs/d1', json=_dialog_body(ws_id, name='v1')
    )
    v1 = r1.json()['version']

    r2 = await client_a.put(
        '/api/v1/dialogs/d1', json=_dialog_body(ws_id, name='v2')
    )
    v2 = r2.json()['version']
    assert v2 > v1
    assert r2.json()['data']['name'] == 'v2'


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put('/api/v1/dialogs/d1', json=_dialog_body(ws_id, name='d1'))
    r2 = await client_a.put(
        '/api/v1/dialogs/d2', json=_dialog_body(ws_id, name='d2')
    )
    cutoff = r2.json()['version']
    r3 = await client_a.put(
        '/api/v1/dialogs/d3', json=_dialog_body(ws_id, name='d3')
    )

    rows = (await client_a.get(f'/api/v1/dialogs?since={cutoff}')).json()
    ids = [row['id'] for row in rows]
    assert ids == ['d3']
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put('/api/v1/dialogs/d1', json=_dialog_body(ws_id))

    r_del = await client_a.delete('/api/v1/dialogs/d1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/dialogs')).json()
    assert len(rows) == 1
    assert rows[0]['id'] == 'd1'
    assert rows[0]['deleted'] is True


async def test_delete_then_put_revives(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put(
        '/api/v1/dialogs/d1', json=_dialog_body(ws_id, name='pre-delete')
    )
    await client_a.delete('/api/v1/dialogs/d1')

    r = await client_a.put(
        '/api/v1/dialogs/d1', json=_dialog_body(ws_id, name='revived')
    )
    assert r.status_code == 200
    body = r.json()
    assert body['deleted'] is False
    assert body['data']['name'] == 'revived'


async def test_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    ws_a = await _seed_workspace(client_a, 'ws-a')
    ws_b = await _seed_workspace(client_b, 'ws-b')

    await client_a.put(
        '/api/v1/dialogs/da', json=_dialog_body(ws_a, name='a-only')
    )
    rows_b = (await client_b.get('/api/v1/dialogs')).json()
    assert rows_b == [], f'B leaked A row: {rows_b!r}'
    r = await client_b.get('/api/v1/dialogs/da')
    assert r.status_code == 404

    # B can also create their own dialog under their own workspace; the
    # two namespaces don't collide on workspace_id.
    r = await client_b.put('/api/v1/dialogs/db', json=_dialog_body(ws_b))
    assert r.status_code == 200


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/dialogs')
    assert r.status_code == 401


# ---- workspace FK / cross-user / cascade -----------------------------------


async def test_put_rejects_missing_workspace_id(
    client_a: httpx.AsyncClient,
) -> None:
    r = await client_a.put('/api/v1/dialogs/d1', json={'name': 'no-ws'})
    assert r.status_code == 422
    assert 'workspaceId' in r.text


async def test_put_rejects_unknown_workspace(
    client_a: httpx.AsyncClient,
) -> None:
    """Body references a workspace that doesn't exist for this user. We
    surface a 409 instead of letting the FK constraint raise an opaque
    500 — caller may want to retry after creating the workspace."""
    r = await client_a.put(
        '/api/v1/dialogs/d1',
        json=_dialog_body('ws-nonexistent', name='orphan'),
    )
    assert r.status_code == 409
    assert 'ws-nonexistent' in r.text


async def test_put_rejects_cross_user_workspace(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """A's workspace must not be addressable from B's session — same 409
    path as the unknown-workspace case (we mask existence rather than
    leak ownership signal)."""
    ws_a = await _seed_workspace(client_a, 'ws-shared')
    r = await client_b.put(
        '/api/v1/dialogs/d-cross',
        json=_dialog_body(ws_a, name='leak-attempt'),
    )
    assert r.status_code == 409


async def test_workspace_cascade_true_tombstones_dialogs(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    """DELETE /workspaces/{id}?cascade=true must in the same request
    tombstone every dialog whose `workspace_id` matches. The dialogs
    list still shows the rows but as `deleted=true`/`data=null`."""
    ws_id = await _seed_workspace(client_a, 'ws-cascade')
    await client_a.put('/api/v1/dialogs/d1', json=_dialog_body(ws_id, name='d1'))
    await client_a.put('/api/v1/dialogs/d2', json=_dialog_body(ws_id, name='d2'))

    r = await client_a.delete(f'/api/v1/workspaces/{ws_id}?cascade=true')
    assert r.status_code == 200, r.text
    assert r.json()['deleted'] is True

    rows = (await client_a.get('/api/v1/dialogs')).json()
    by_id = {row['id']: row for row in rows}
    assert by_id['d1']['deleted'] is True, f'd1 still alive: {by_id["d1"]!r}'
    assert by_id['d1']['data'] is None
    assert by_id['d2']['deleted'] is True
    assert by_id['d2']['data'] is None

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id, deleted_at FROM dialogs WHERE workspace_id = %s '
            'ORDER BY id',
            (ws_id,),
        )
        db_rows = cur.fetchall()
    for row in db_rows:
        assert row[1] is not None, f'dialog {row[0]} not tombstoned in DB'


async def test_workspace_cascade_false_leaves_dialogs(
    client_a: httpx.AsyncClient,
) -> None:
    """cascade=false (the default) must keep dialogs alive — the
    workspace tombstone alone shouldn't ripple. Important regression
    guard so the cascade branch can't accidentally fire on every
    delete."""
    ws_id = await _seed_workspace(client_a, 'ws-no-cascade')
    await client_a.put('/api/v1/dialogs/d1', json=_dialog_body(ws_id))

    r = await client_a.delete(f'/api/v1/workspaces/{ws_id}?cascade=false')
    assert r.status_code == 200
    assert r.json()['deleted'] is True

    rows = (await client_a.get('/api/v1/dialogs')).json()
    by_id = {row['id']: row for row in rows}
    assert 'd1' in by_id
    assert by_id['d1']['deleted'] is False, (
        f'cascade=false must leave dialogs alone, got {by_id["d1"]!r}'
    )


async def test_workspace_cascade_skips_other_workspaces(
    client_a: httpx.AsyncClient,
) -> None:
    """Two workspaces, one dialog each. Cascade-deleting one workspace
    must not touch the other workspace's dialog."""
    ws_a = await _seed_workspace(client_a, 'ws-victim')
    ws_b = await _seed_workspace(client_a, 'ws-bystander')
    await client_a.put('/api/v1/dialogs/d-victim', json=_dialog_body(ws_a))
    await client_a.put(
        '/api/v1/dialogs/d-bystander', json=_dialog_body(ws_b)
    )

    r = await client_a.delete(f'/api/v1/workspaces/{ws_a}?cascade=true')
    assert r.status_code == 200

    rows = (await client_a.get('/api/v1/dialogs')).json()
    by_id = {row['id']: row for row in rows}
    assert by_id['d-victim']['deleted'] is True
    assert by_id['d-bystander']['deleted'] is False, (
        f'bystander dialog tombstoned: {by_id["d-bystander"]!r}'
    )


async def test_cross_user_cannot_delete(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    ws_a = await _seed_workspace(client_a, 'ws-shared')
    await client_a.put(
        '/api/v1/dialogs/d-shared', json=_dialog_body(ws_a)
    )

    r = await client_b.delete('/api/v1/dialogs/d-shared')
    assert r.status_code == 404

    r = await client_a.get('/api/v1/dialogs/d-shared')
    assert r.status_code == 200
    assert r.json()['deleted'] is False
