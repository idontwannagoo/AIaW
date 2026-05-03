"""Stage 4 / 批次-4c — items REST CRUD + dialog FK + workspace cascade.

Mirrors test_dialogs.py — same id-PK envelope + soft-delete tombstone.
Adds the schema-decision cases unique to 4c:

- dialogId is required in body and validated against dialogs (a missing
  or cross-user dialog returns 409, not 500)
- DELETE /api/v1/workspaces/{id}?cascade=true second-hops through
  dialogs and tombstones every item under each cascaded dialog in the
  same transaction (server-side cascade)
- Hard-DELETE on a dialogs row triggers PG ON DELETE CASCADE on items
  (defense-in-depth — the live path never hard-deletes, so this is a
  guardrail that the FK is wired)
- Server is opaque to inline-vs-ref attachment envelopes — items rows
  carry whatever the client serialized via blob-client.ts, the API
  byte-faithfully round-trips both shapes

Test names map 1:1 onto the plan's 批次-4c "通过判据" so failure messages
stay readable in `pnpm test:api` output.
"""
from __future__ import annotations

import base64

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


def _item_body(dialog_id: str, **overrides) -> dict:
    body = {
        'id': 'i1',  # echoed back inside data; some specs assert it
        'dialogId': dialog_id,
        'type': 'text',
        'references': 1,
        'contentText': 'hello',
    }
    body.update(overrides)
    return body


async def _seed_workspace(client: httpx.AsyncClient, ws_id: str = 'w1') -> str:
    r = await client.put(f'/api/v1/workspaces/{ws_id}', json=_DEFAULT_WORKSPACE)
    assert r.status_code == 200, r.text
    return ws_id


async def _seed_dialog(
    client: httpx.AsyncClient, ws_id: str, dlg_id: str = 'd1'
) -> str:
    r = await client.put(f'/api/v1/dialogs/{dlg_id}', json=_dialog_body(ws_id))
    assert r.status_code == 200, r.text
    return dlg_id


# ---- CRUD shape (mirrors dialogs 1:1) --------------------------------------


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)

    r = await client_a.put(
        '/api/v1/items/i1', json=_item_body(dlg_id, contentText='Item 1'),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['id'] == 'i1'
    assert body['deleted'] is False
    assert body['data']['contentText'] == 'Item 1'
    assert body['data']['dialogId'] == dlg_id
    assert body['version'] >= 1

    rows = (await client_a.get('/api/v1/items')).json()
    assert [row['id'] for row in rows] == ['i1']

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, dialog_id, data, deleted_at FROM items '
            'WHERE id = %s',
            ('i1',),
        )
        owner, dlg_col, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert dlg_col == dlg_id
    assert data['contentText'] == 'Item 1'
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    await client_a.put('/api/v1/items/i1', json=_item_body(dlg_id))

    r = await client_a.get('/api/v1/items/i1')
    assert r.status_code == 200
    assert r.json()['id'] == 'i1'

    r = await client_a.get('/api/v1/items/missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    r1 = await client_a.put(
        '/api/v1/items/i1', json=_item_body(dlg_id, contentText='v1'),
    )
    v1 = r1.json()['version']

    r2 = await client_a.put(
        '/api/v1/items/i1', json=_item_body(dlg_id, contentText='v2'),
    )
    v2 = r2.json()['version']
    assert v2 > v1
    assert r2.json()['data']['contentText'] == 'v2'


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    await client_a.put('/api/v1/items/i1', json=_item_body(dlg_id, id='i1'))
    r2 = await client_a.put(
        '/api/v1/items/i2', json=_item_body(dlg_id, id='i2'),
    )
    cutoff = r2.json()['version']
    r3 = await client_a.put(
        '/api/v1/items/i3', json=_item_body(dlg_id, id='i3'),
    )

    rows = (await client_a.get(f'/api/v1/items?since={cutoff}')).json()
    ids = [row['id'] for row in rows]
    assert ids == ['i3']
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient,
) -> None:
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    await client_a.put('/api/v1/items/i1', json=_item_body(dlg_id))

    r_del = await client_a.delete('/api/v1/items/i1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/items')).json()
    assert len(rows) == 1
    assert rows[0]['id'] == 'i1'
    assert rows[0]['deleted'] is True


async def test_delete_then_put_revives(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    await client_a.put(
        '/api/v1/items/i1', json=_item_body(dlg_id, contentText='pre-delete'),
    )
    await client_a.delete('/api/v1/items/i1')

    r = await client_a.put(
        '/api/v1/items/i1', json=_item_body(dlg_id, contentText='revived'),
    )
    assert r.status_code == 200
    body = r.json()
    assert body['deleted'] is False
    assert body['data']['contentText'] == 'revived'


async def test_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    ws_a = await _seed_workspace(client_a, 'ws-a')
    dlg_a = await _seed_dialog(client_a, ws_a, 'd-a')
    ws_b = await _seed_workspace(client_b, 'ws-b')
    dlg_b = await _seed_dialog(client_b, ws_b, 'd-b')

    await client_a.put(
        '/api/v1/items/ia', json=_item_body(dlg_a, id='ia', contentText='a-only'),
    )
    rows_b = (await client_b.get('/api/v1/items')).json()
    assert rows_b == [], f'B leaked A row: {rows_b!r}'
    r = await client_b.get('/api/v1/items/ia')
    assert r.status_code == 404

    # B can also create their own item under their own dialog; the two
    # namespaces don't collide on dialog_id.
    r = await client_b.put('/api/v1/items/ib', json=_item_body(dlg_b, id='ib'))
    assert r.status_code == 200


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/items')
    assert r.status_code == 401


# ---- dialog FK / cross-user / 422 / 409 ------------------------------------


async def test_put_rejects_missing_dialog_id(
    client_a: httpx.AsyncClient,
) -> None:
    r = await client_a.put('/api/v1/items/i1', json={'type': 'text'})
    assert r.status_code == 422
    assert 'dialogId' in r.text


async def test_put_rejects_unknown_dialog(
    client_a: httpx.AsyncClient,
) -> None:
    """Body references a dialog that doesn't exist for this user. We
    surface a 409 instead of letting the FK constraint raise an opaque
    500 — caller may want to retry after creating the dialog."""
    r = await client_a.put(
        '/api/v1/items/i1', json=_item_body('dlg-nonexistent'),
    )
    assert r.status_code == 409
    assert 'dlg-nonexistent' in r.text


async def test_put_rejects_cross_user_dialog(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """A's dialog must not be addressable from B's session — same 409
    path as the unknown-dialog case (we mask existence rather than
    leak ownership signal)."""
    ws_a = await _seed_workspace(client_a, 'ws-shared')
    dlg_a = await _seed_dialog(client_a, ws_a, 'd-shared')
    r = await client_b.put(
        '/api/v1/items/i-cross', json=_item_body(dlg_a, id='i-cross'),
    )
    assert r.status_code == 409


# ---- attachment envelope round-trip (server is opaque) ---------------------


async def test_put_item_with_inline_attachment(
    client_a: httpx.AsyncClient,
) -> None:
    """< 64KB attachments ride inline in the row — the API just stores
    whatever the client serialized. The server must not mutate the
    envelope shape on the way back out."""
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    inline_env = {
        'type': 'inline',
        'data': base64.b64encode(b'hello bytes').decode('ascii'),
        'content_type': 'application/octet-stream',
        'size': 11,
    }
    body = _item_body(
        dlg_id, type='file', name='hello.bin',
        mimeType='application/octet-stream',
        contentBuffer=inline_env,
    )
    r = await client_a.put('/api/v1/items/i1', json=body)
    assert r.status_code == 200, r.text
    out = r.json()['data']
    assert out['contentBuffer'] == inline_env, (
        f'inline envelope mutated round-trip: sent={inline_env!r} '
        f'got={out["contentBuffer"]!r}'
    )

    # GET must return the same envelope.
    r2 = await client_a.get('/api/v1/items/i1')
    assert r2.json()['data']['contentBuffer'] == inline_env


async def test_put_item_with_ref_attachment_round_trips(
    client_a: httpx.AsyncClient,
) -> None:
    """≥ 64KB attachments live in object storage; the row only carries
    the ref envelope. The API must store the {type:'ref', url, sha256,
    size, content_type} envelope verbatim."""
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    ref_env = {
        'type': 'ref',
        'url': 'https://example.invalid/blobs/abc123/data?exp=1&sig=x',
        'sha256': 'a' * 64,
        'size': 70_000,
        'content_type': 'image/png',
    }
    body = _item_body(
        dlg_id, type='file', name='big.png', mimeType='image/png',
        contentBuffer=ref_env,
    )
    r = await client_a.put('/api/v1/items/i1', json=body)
    assert r.status_code == 200, r.text
    out = r.json()['data']
    assert out['contentBuffer'] == ref_env, (
        f'ref envelope mutated round-trip: sent={ref_env!r} '
        f'got={out["contentBuffer"]!r}'
    )

    r2 = await client_a.get('/api/v1/items/i1')
    assert r2.json()['data']['contentBuffer'] == ref_env


async def test_inline_envelope_at_64kb_minus_one(
    client_a: httpx.AsyncClient,
) -> None:
    """Boundary: 65535 bytes still fits inline (client-side decision is
    `size < BLOB_INLINE_MAX_BYTES`). Server is opaque so we just verify
    that sending an inline envelope of size 65535 round-trips."""
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    payload = b'x' * 65535
    inline_env = {
        'type': 'inline',
        'data': base64.b64encode(payload).decode('ascii'),
        'content_type': 'application/octet-stream',
        'size': 65535,
    }
    body = _item_body(dlg_id, type='file', contentBuffer=inline_env)
    r = await client_a.put('/api/v1/items/i1', json=body)
    assert r.status_code == 200, r.text
    out = r.json()['data']['contentBuffer']
    assert out['size'] == 65535
    assert out['type'] == 'inline'
    # sha-by-base64 cheap equivalence check
    assert out['data'] == inline_env['data']


async def test_ref_envelope_at_64kb_threshold(
    client_a: httpx.AsyncClient,
) -> None:
    """Boundary: 65536 bytes triggers the client to switch to ref mode.
    Server stores the ref envelope as-is and reflects it back."""
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    ref_env = {
        'type': 'ref',
        'url': 'https://example.invalid/blobs/deadbeef/data?exp=1&sig=y',
        'sha256': 'b' * 64,
        'size': 65536,
        'content_type': 'application/octet-stream',
    }
    body = _item_body(dlg_id, type='file', contentBuffer=ref_env)
    r = await client_a.put('/api/v1/items/i1', json=body)
    assert r.status_code == 200, r.text
    assert r.json()['data']['contentBuffer'] == ref_env


# ---- cascade chains --------------------------------------------------------


async def test_workspace_cascade_true_tombstones_items_via_dialog(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    """DELETE /workspaces/{id}?cascade=true must in the same request
    tombstone every dialog under the workspace AND every item under
    each cascaded dialog. The items list still shows the rows but as
    `deleted=true`/`data=null`, and the broker publishes one event per
    cascaded item (verified separately in the e2e cascade spec)."""
    ws_id = await _seed_workspace(client_a, 'ws-cascade')
    await _seed_dialog(client_a, ws_id, 'd1')
    await _seed_dialog(client_a, ws_id, 'd2')
    await client_a.put(
        '/api/v1/items/i-d1-1', json=_item_body('d1', id='i-d1-1'),
    )
    await client_a.put(
        '/api/v1/items/i-d2-1', json=_item_body('d2', id='i-d2-1'),
    )

    r = await client_a.delete(f'/api/v1/workspaces/{ws_id}?cascade=true')
    assert r.status_code == 200, r.text
    assert r.json()['deleted'] is True

    rows = (await client_a.get('/api/v1/items')).json()
    by_id = {row['id']: row for row in rows}
    for iid in ('i-d1-1', 'i-d2-1'):
        assert by_id[iid]['deleted'] is True, (
            f'item {iid} still alive: {by_id[iid]!r}'
        )
        assert by_id[iid]['data'] is None

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id, deleted_at FROM items WHERE id IN '
            "('i-d1-1', 'i-d2-1') ORDER BY id",
        )
        db_rows = cur.fetchall()
    for iid, deleted_at in db_rows:
        assert deleted_at is not None, f'item {iid} not tombstoned in DB'


async def test_workspace_cascade_skips_other_workspaces_items(
    client_a: httpx.AsyncClient,
) -> None:
    """Two workspaces, one item each (under their respective dialogs).
    Cascade-deleting one workspace must not touch the other workspace's
    item — protects against a bystander dialog_id scoping bug."""
    ws_a = await _seed_workspace(client_a, 'ws-victim')
    ws_b = await _seed_workspace(client_a, 'ws-bystander')
    await _seed_dialog(client_a, ws_a, 'd-victim')
    await _seed_dialog(client_a, ws_b, 'd-bystander')
    await client_a.put(
        '/api/v1/items/i-victim', json=_item_body('d-victim', id='i-victim'),
    )
    await client_a.put(
        '/api/v1/items/i-bystander',
        json=_item_body('d-bystander', id='i-bystander'),
    )

    r = await client_a.delete(f'/api/v1/workspaces/{ws_a}?cascade=true')
    assert r.status_code == 200

    rows = (await client_a.get('/api/v1/items')).json()
    by_id = {row['id']: row for row in rows}
    assert by_id['i-victim']['deleted'] is True
    assert by_id['i-bystander']['deleted'] is False, (
        f'bystander item tombstoned: {by_id["i-bystander"]!r}'
    )


async def test_dialog_hard_delete_cascades_items_via_pg_fk(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    """Defense-in-depth: the live path soft-deletes dialogs (deleted_at),
    but the items.dialog_id FK declares ON DELETE CASCADE so a hard
    DELETE on dialogs (e.g. ops cleanup, future GC) drops items too.
    No app code does this today; the test guards the FK contract."""
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)
    await client_a.put(
        '/api/v1/items/i-fk', json=_item_body(dlg_id, id='i-fk'),
    )
    # Sanity: row exists in PG before the hard delete
    with pg_conn.cursor() as cur:
        cur.execute('SELECT id FROM items WHERE id = %s', ('i-fk',))
        assert cur.fetchone() is not None

    # Hard-delete the dialog row out from under the FK. We bypass the
    # API on purpose — the API only soft-deletes.
    with pg_conn.cursor() as cur:
        cur.execute('DELETE FROM dialogs WHERE id = %s', (dlg_id,))

    with pg_conn.cursor() as cur:
        cur.execute('SELECT id FROM items WHERE id = %s', ('i-fk',))
        assert cur.fetchone() is None, 'PG ON DELETE CASCADE did not fire'


async def test_cross_user_cannot_delete(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    ws_a = await _seed_workspace(client_a, 'ws-shared')
    dlg_a = await _seed_dialog(client_a, ws_a, 'd-shared')
    await client_a.put(
        '/api/v1/items/i-shared', json=_item_body(dlg_a, id='i-shared'),
    )

    r = await client_b.delete('/api/v1/items/i-shared')
    assert r.status_code == 404

    r = await client_a.get('/api/v1/items/i-shared')
    assert r.status_code == 200
    assert r.json()['deleted'] is False


# ---- Stage 4 / 硬前置 3 — scope-aware list (`?dialogId=`) ------------------
#
# Mirrors the dialogs `?workspaceId=` retrofit. The scope filter is the
# load-bearing path for the lazy frontend pull (items can be tens of
# thousands per active user; opening a single dialog must not pull the
# whole user's items table).


async def test_list_with_dialog_id_filters_to_scope(
    client_a: httpx.AsyncClient,
) -> None:
    """Two dialogs (under one workspace) with 5 items each. `?dialogId=d1`
    must return exactly the 5 items whose dialog_id matches. The other
    dialog's items must not leak."""
    ws_id = await _seed_workspace(client_a)
    d1 = await _seed_dialog(client_a, ws_id, 'd1')
    d2 = await _seed_dialog(client_a, ws_id, 'd2')
    for i in range(5):
        await client_a.put(
            f'/api/v1/items/d1-i{i}',
            json=_item_body(d1, id=f'd1-i{i}', contentText=f't{i}'),
        )
        await client_a.put(
            f'/api/v1/items/d2-i{i}',
            json=_item_body(d2, id=f'd2-i{i}', contentText=f't{i}'),
        )

    r = await client_a.get('/api/v1/items?dialogId=d1')
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = sorted(row['id'] for row in rows)
    assert ids == sorted(f'd1-i{i}' for i in range(5)), (
        f'expected only d1 items, got {ids!r}'
    )
    for row in rows:
        assert row['data']['dialogId'] == 'd1', (
            f'leaked row from another dialog: {row!r}'
        )


async def test_dialog_id_combined_with_since_and_limit(
    client_a: httpx.AsyncClient,
) -> None:
    """Same cursor envelope semantics as dialogs `?workspaceId=` —
    `limit=` switches the response to `{rows, next_cursor}` and the
    scope filter applies before LIMIT."""
    ws_id = await _seed_workspace(client_a)
    d_paged = await _seed_dialog(client_a, ws_id, 'd-paged')
    d_noise = await _seed_dialog(client_a, ws_id, 'd-noise')
    for i in range(6):
        await client_a.put(
            f'/api/v1/items/p{i}', json=_item_body(d_paged, id=f'p{i}'),
        )
        await client_a.put(
            f'/api/v1/items/n{i}', json=_item_body(d_noise, id=f'n{i}'),
        )

    r = await client_a.get(
        '/api/v1/items?dialogId=d-paged&since=0&limit=4'
    )
    assert r.status_code == 200, r.text
    page1 = r.json()
    assert isinstance(page1, dict), (
        f'limit= must yield CursorPage envelope, got {type(page1).__name__}: {page1!r}'
    )
    rows1 = page1['rows']
    assert len(rows1) == 4
    for row in rows1:
        assert row['data']['dialogId'] == 'd-paged', (
            f'leaked row from d-noise: {row!r}'
        )
    cursor = page1['next_cursor']
    assert cursor is not None and cursor > 0, (
        f'page is full, expected next_cursor, got {cursor!r}'
    )

    r2 = await client_a.get(
        f'/api/v1/items?dialogId=d-paged&since={cursor}&limit=4'
    )
    page2 = r2.json()
    rows2 = page2['rows']
    assert len(rows2) == 2, (
        f'expected last 2 d-paged items, got {[r["id"] for r in rows2]!r}'
    )
    assert page2['next_cursor'] is None
    all_ids = {r['id'] for r in rows1} | {r['id'] for r in rows2}
    assert all_ids == {f'p{i}' for i in range(6)}, all_ids


async def test_dialog_id_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """User B queries User A's dialogId. user_id predicate already
    isolates accounts → empty set, status 200, no 403/404 ownership
    leak."""
    ws_a = await _seed_workspace(client_a, 'ws-a')
    dlg_a = await _seed_dialog(client_a, ws_a, 'd-a-private')
    for i in range(3):
        await client_a.put(
            f'/api/v1/items/a-i{i}',
            json=_item_body(dlg_a, id=f'a-i{i}'),
        )

    r = await client_b.get(f'/api/v1/items?dialogId={dlg_a}')
    assert r.status_code == 200, (
        f'cross-user scopeId must filter to [], not error; got {r.status_code}: {r.text}'
    )
    rows = r.json()
    assert rows == [], (
        f'B leaked A rows via dialogId scope: {rows!r}'
    )

    r_a = await client_a.get(f'/api/v1/items?dialogId={dlg_a}')
    assert len(r_a.json()) == 3


async def test_no_dialog_id_param_returns_full_user_table(
    client_a: httpx.AsyncClient,
) -> None:
    """Backwards compatibility — no `?dialogId=` → bare list of all the
    user's items across all dialogs."""
    ws_id = await _seed_workspace(client_a)
    d1 = await _seed_dialog(client_a, ws_id, 'd1')
    d2 = await _seed_dialog(client_a, ws_id, 'd2')
    await client_a.put('/api/v1/items/i1', json=_item_body(d1, id='i1'))
    await client_a.put('/api/v1/items/i2', json=_item_body(d2, id='i2'))

    r = await client_a.get('/api/v1/items')
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list), (
        f'no `limit=` → bare list; got {type(rows).__name__}'
    )
    ids = sorted(row['id'] for row in rows)
    assert ids == ['i1', 'i2'], ids
