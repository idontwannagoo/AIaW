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


# ---- Stage 4 / 硬前置 3 — scope-aware list (`?workspaceId=`) ---------------
#
# The list endpoint accepts an optional `workspaceId=` query (alias for
# `workspace_id`) so the client can pull a single workspace's dialogs in
# one round-trip instead of the user's entire dialogs table. Mirrors the
# items.py `dialogId=` retrofit. Combinable with `since=` and `limit=`.


async def test_list_with_workspace_id_filters_to_scope(
    client_a: httpx.AsyncClient,
) -> None:
    """Build two workspaces with 5 dialogs each. `?workspaceId=ws1` must
    return exactly the 5 dialogs whose workspace_id matches; rows from the
    other workspace must not leak through (the user_id predicate alone
    would still let them through, so the scope filter is the load-bearing
    part)."""
    ws1 = await _seed_workspace(client_a, 'ws1')
    ws2 = await _seed_workspace(client_a, 'ws2')
    for i in range(5):
        await client_a.put(
            f'/api/v1/dialogs/ws1-d{i}', json=_dialog_body(ws1, name=f'd{i}'),
        )
        await client_a.put(
            f'/api/v1/dialogs/ws2-d{i}', json=_dialog_body(ws2, name=f'd{i}'),
        )

    r = await client_a.get('/api/v1/dialogs?workspaceId=ws1')
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = sorted(row['id'] for row in rows)
    assert ids == sorted(f'ws1-d{i}' for i in range(5)), (
        f'expected only ws1 dialogs, got {ids!r}'
    )
    for row in rows:
        assert row['data']['workspaceId'] == 'ws1', (
            f'leaked row from another workspace: {row!r}'
        )


async def test_workspace_id_combined_with_since_and_limit(
    client_a: httpx.AsyncClient,
) -> None:
    """When the client passes `limit=`, the response shape switches to the
    cursor envelope `{rows, next_cursor}`. Combine `?workspaceId=&since=&
    limit=` and verify the page is filtered, ordered by version, and the
    cursor advances correctly across pages."""
    ws1 = await _seed_workspace(client_a, 'ws-paged')
    ws2 = await _seed_workspace(client_a, 'ws-noise')
    # Interleave writes between the two workspaces so the version sequence
    # for ws-paged is non-contiguous (real-world shape) — this catches a
    # bug where the limit clamps before the WHERE filter.
    for i in range(6):
        await client_a.put(
            f'/api/v1/dialogs/p{i}', json=_dialog_body(ws1, name=f'p{i}'),
        )
        await client_a.put(
            f'/api/v1/dialogs/n{i}', json=_dialog_body(ws2, name=f'n{i}'),
        )

    r = await client_a.get(
        '/api/v1/dialogs?workspaceId=ws-paged&since=0&limit=4'
    )
    assert r.status_code == 200, r.text
    page1 = r.json()
    assert isinstance(page1, dict), (
        f'limit= must yield CursorPage envelope, got {type(page1).__name__}: {page1!r}'
    )
    assert {'rows', 'next_cursor'} <= page1.keys(), page1
    rows1 = page1['rows']
    assert len(rows1) == 4
    ids1 = [row['id'] for row in rows1]
    assert ids1 == sorted(ids1, key=lambda i: rows1[ids1.index(i)]['version']), (
        f'rows not ordered by version: {[(r["id"], r["version"]) for r in rows1]}'
    )
    for row in rows1:
        assert row['data']['workspaceId'] == 'ws-paged', (
            f'leaked row from ws-noise into page1: {row!r}'
        )
    cursor = page1['next_cursor']
    assert cursor is not None and cursor > 0, (
        f'page is full ({len(rows1)} == limit), expected next_cursor, got {cursor!r}'
    )

    # Resume with the cursor — should yield the remaining 2 ws-paged rows.
    r2 = await client_a.get(
        f'/api/v1/dialogs?workspaceId=ws-paged&since={cursor}&limit=4'
    )
    assert r2.status_code == 200
    page2 = r2.json()
    assert isinstance(page2, dict), page2
    rows2 = page2['rows']
    ids2 = [row['id'] for row in rows2]
    assert len(rows2) == 2, (
        f'expected last 2 ws-paged rows, got {ids2!r} (page1 was {ids1!r})'
    )
    for row in rows2:
        assert row['data']['workspaceId'] == 'ws-paged'
    # Page wasn't full → cursor cleared (one extra empty page protocol).
    assert page2['next_cursor'] is None, (
        f'partial page must have null cursor, got {page2["next_cursor"]!r}'
    )

    # Together page1 + page2 must equal the full 6 ws-paged dialogs.
    all_ids = set(ids1) | set(ids2)
    assert all_ids == {f'p{i}' for i in range(6)}, all_ids


async def test_workspace_id_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """User B asks for User A's workspaceId. The user_id predicate already
    isolates accounts, so the WHERE workspace_id filter just lands on an
    empty set rather than 403. Status must be 200 with `[]` — we don't
    leak ownership signal via 403/404."""
    ws_a = await _seed_workspace(client_a, 'ws-a-private')
    for i in range(3):
        await client_a.put(
            f'/api/v1/dialogs/a-d{i}', json=_dialog_body(ws_a, name=f'd{i}'),
        )

    # B has no workspaces but tries to query A's workspaceId.
    r = await client_b.get(f'/api/v1/dialogs?workspaceId={ws_a}')
    assert r.status_code == 200, (
        f'cross-user scopeId must filter to [], not error; got {r.status_code}: {r.text}'
    )
    rows = r.json()
    assert rows == [], (
        f'B leaked A rows via workspaceId scope: {rows!r}'
    )

    # Sanity: A still sees their rows.
    r_a = await client_a.get(f'/api/v1/dialogs?workspaceId={ws_a}')
    assert len(r_a.json()) == 3


async def test_no_scope_param_returns_full_user_table(
    client_a: httpx.AsyncClient,
) -> None:
    """Backwards compatibility: no `?workspaceId=` returns the same shape
    as before (bare list, all of the user's dialogs across all
    workspaces). Stage 1-3 list consumers must keep working."""
    ws1 = await _seed_workspace(client_a, 'ws-back-1')
    ws2 = await _seed_workspace(client_a, 'ws-back-2')
    await client_a.put('/api/v1/dialogs/d1', json=_dialog_body(ws1, name='d1'))
    await client_a.put('/api/v1/dialogs/d2', json=_dialog_body(ws2, name='d2'))

    r = await client_a.get('/api/v1/dialogs')
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list), (
        f'no `limit=` → bare list, not CursorPage envelope; got {type(rows).__name__}'
    )
    ids = sorted(row['id'] for row in rows)
    assert ids == ['d1', 'd2'], ids
