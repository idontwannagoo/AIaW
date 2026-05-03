"""Stage 4 / 批次-4d — artifacts REST CRUD + workspace FK + scope filter +
workspace cascade.

Mirrors test_dialogs.py / test_items.py — same id-PK envelope + soft-delete
tombstone. Plan 2026-05-03 修订 (4d artifacts 单 scope) makes artifacts a
single-scope table keyed on `workspaceId` only — there is NO `dialogId`
column, no `?dialogId=` filter, no dialog cascade hop. The Artifact wire
shape carries `versions: ArtifactVersion[]` inline when JSON.stringify
fits under 64KB; otherwise the client spills bytes into a
`versionsBlob: AttachmentEnvelope` ref. Like items, the server is opaque to
which mode is used — it just round-trips whatever the client serialized.

Test names map 1:1 onto the plan's 批次-4d "通过判据" so failure messages
stay readable in `pnpm test:api` output.
"""
from __future__ import annotations

import httpx


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


def _artifact_body(workspace_id: str, **overrides) -> dict:
    """Build a minimal Artifact wire body. The wire shape mirrors
    `WireArtifact` from `src/data/repositories/artifacts.server.ts`:
    `versions` is an array of `{date: ISO-string, text: string}` entries
    (date is JSON-stringified in inline mode); the server never inspects
    the field shape, only forwards JSONB."""
    body = {
        'id': 'a1',  # echoed back inside data; some specs assert it
        'name': 'Artifact 1',
        'workspaceId': workspace_id,
        'versions': [
            {'date': '2026-05-03T00:00:00.000Z', 'text': 'hello'},
        ],
        'currIndex': 0,
        'readable': True,
        'writable': True,
        'open': False,
        'tmp': '',
    }
    body.update(overrides)
    return body


async def _seed_workspace(client: httpx.AsyncClient, ws_id: str = 'w1') -> str:
    r = await client.put(f'/api/v1/workspaces/{ws_id}', json=_DEFAULT_WORKSPACE)
    assert r.status_code == 200, r.text
    return ws_id


# ---- CRUD shape (mirrors dialogs/items 1:1) --------------------------------


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    ws_id = await _seed_workspace(client_a)

    r = await client_a.put(
        '/api/v1/artifacts/a1', json=_artifact_body(ws_id, name='Artifact 1'),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['id'] == 'a1'
    assert body['deleted'] is False
    assert body['data']['name'] == 'Artifact 1'
    assert body['data']['workspaceId'] == ws_id
    assert body['version'] >= 1

    rows = (await client_a.get('/api/v1/artifacts')).json()
    assert [row['id'] for row in rows] == ['a1']

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, workspace_id, data, deleted_at FROM artifacts '
            'WHERE id = %s',
            ('a1',),
        )
        owner, ws_col, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert ws_col == ws_id
    assert data['name'] == 'Artifact 1'
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put('/api/v1/artifacts/a1', json=_artifact_body(ws_id))

    r = await client_a.get('/api/v1/artifacts/a1')
    assert r.status_code == 200
    assert r.json()['id'] == 'a1'

    r = await client_a.get('/api/v1/artifacts/missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    r1 = await client_a.put(
        '/api/v1/artifacts/a1', json=_artifact_body(ws_id, name='v1'),
    )
    v1 = r1.json()['version']

    r2 = await client_a.put(
        '/api/v1/artifacts/a1', json=_artifact_body(ws_id, name='v2'),
    )
    v2 = r2.json()['version']
    assert v2 > v1
    assert r2.json()['data']['name'] == 'v2'


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put('/api/v1/artifacts/a1', json=_artifact_body(ws_id, id='a1'))
    r2 = await client_a.put(
        '/api/v1/artifacts/a2', json=_artifact_body(ws_id, id='a2'),
    )
    cutoff = r2.json()['version']
    r3 = await client_a.put(
        '/api/v1/artifacts/a3', json=_artifact_body(ws_id, id='a3'),
    )

    rows = (await client_a.get(f'/api/v1/artifacts?since={cutoff}')).json()
    ids = [row['id'] for row in rows]
    assert ids == ['a3']
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient,
) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put('/api/v1/artifacts/a1', json=_artifact_body(ws_id))

    r_del = await client_a.delete('/api/v1/artifacts/a1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/artifacts')).json()
    assert len(rows) == 1
    assert rows[0]['id'] == 'a1'
    assert rows[0]['deleted'] is True


async def test_delete_then_put_revives(client_a: httpx.AsyncClient) -> None:
    ws_id = await _seed_workspace(client_a)
    await client_a.put(
        '/api/v1/artifacts/a1', json=_artifact_body(ws_id, name='pre-delete'),
    )
    await client_a.delete('/api/v1/artifacts/a1')

    r = await client_a.put(
        '/api/v1/artifacts/a1', json=_artifact_body(ws_id, name='revived'),
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
        '/api/v1/artifacts/aa', json=_artifact_body(ws_a, id='aa', name='a-only'),
    )
    rows_b = (await client_b.get('/api/v1/artifacts')).json()
    assert rows_b == [], f'B leaked A row: {rows_b!r}'
    r = await client_b.get('/api/v1/artifacts/aa')
    assert r.status_code == 404

    r = await client_b.put(
        '/api/v1/artifacts/ab', json=_artifact_body(ws_b, id='ab'),
    )
    assert r.status_code == 200


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/artifacts')
    assert r.status_code == 401


async def test_cross_user_cannot_delete(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    ws_a = await _seed_workspace(client_a, 'ws-shared')
    await client_a.put(
        '/api/v1/artifacts/a-shared', json=_artifact_body(ws_a, id='a-shared'),
    )

    r = await client_b.delete('/api/v1/artifacts/a-shared')
    assert r.status_code == 404

    r = await client_a.get('/api/v1/artifacts/a-shared')
    assert r.status_code == 200
    assert r.json()['deleted'] is False


# ---- workspace FK / cross-user / 422 / 409 ---------------------------------


async def test_put_rejects_missing_workspace_id(
    client_a: httpx.AsyncClient,
) -> None:
    r = await client_a.put('/api/v1/artifacts/a1', json={'name': 'x'})
    assert r.status_code == 422
    assert 'workspaceId' in r.text


async def test_put_rejects_unknown_workspace(
    client_a: httpx.AsyncClient,
) -> None:
    """Body references a workspace that doesn't exist for this user. We
    surface a 409 instead of letting the FK constraint raise an opaque
    500 — caller may want to retry after creating the workspace."""
    r = await client_a.put(
        '/api/v1/artifacts/a1',
        json=_artifact_body('ws-nonexistent'),
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
        '/api/v1/artifacts/a-cross', json=_artifact_body(ws_a, id='a-cross'),
    )
    assert r.status_code == 409


# ---- attachment envelope round-trip (server is opaque) ---------------------


async def test_put_artifact_with_inline_versions(
    client_a: httpx.AsyncClient,
) -> None:
    """Small `versions` ride inline on the wire. Server stores the array
    verbatim and reflects it back."""
    ws_id = await _seed_workspace(client_a)
    versions = [
        {'date': '2026-05-03T00:00:00.000Z', 'text': 'small body 1'},
        {'date': '2026-05-03T00:00:01.000Z', 'text': 'small body 2'},
    ]
    body = _artifact_body(ws_id, versions=versions, currIndex=1)
    r = await client_a.put('/api/v1/artifacts/a1', json=body)
    assert r.status_code == 200, r.text
    out = r.json()['data']
    assert out['versions'] == versions, (
        f'inline versions mutated round-trip: sent={versions!r} '
        f'got={out["versions"]!r}'
    )
    assert out['currIndex'] == 1
    # versionsBlob must NOT be present in inline mode
    assert 'versionsBlob' not in out

    r2 = await client_a.get('/api/v1/artifacts/a1')
    assert r2.json()['data']['versions'] == versions


async def test_put_artifact_with_ref_versions_round_trips(
    client_a: httpx.AsyncClient,
) -> None:
    """≥ 64KB serialized `versions` live in object storage; the row only
    carries the `versionsBlob: AttachmentEnvelope` ref envelope. The API
    must store the {type:'ref', url, sha256, size, content_type} envelope
    verbatim."""
    ws_id = await _seed_workspace(client_a)
    ref_env = {
        'type': 'ref',
        'url': 'https://example.invalid/blobs/abc123/data?exp=1&sig=x',
        'sha256': 'a' * 64,
        'size': 70_000,
        'content_type': 'application/json',
    }
    body = _artifact_body(
        ws_id, versions=[], versionsBlob=ref_env,
    )
    r = await client_a.put('/api/v1/artifacts/a1', json=body)
    assert r.status_code == 200, r.text
    out = r.json()['data']
    assert out['versionsBlob'] == ref_env, (
        f'ref envelope mutated round-trip: sent={ref_env!r} '
        f'got={out["versionsBlob"]!r}'
    )
    assert out['versions'] == []

    r2 = await client_a.get('/api/v1/artifacts/a1')
    assert r2.json()['data']['versionsBlob'] == ref_env


async def test_inline_below_64kb(client_a: httpx.AsyncClient) -> None:
    """Boundary: 65535 bytes of serialized versions is just under the
    threshold — client-side decision is `bytes < BLOB_INLINE_MAX_BYTES`.
    Server is opaque so we just verify a body of that approximate size
    round-trips inline."""
    ws_id = await _seed_workspace(client_a)
    # Build a single version whose text just fits under 64KB.
    big_text = 'x' * 65000  # well under 64KB serialized
    versions = [{'date': '2026-05-03T00:00:00.000Z', 'text': big_text}]
    body = _artifact_body(ws_id, versions=versions)
    r = await client_a.put('/api/v1/artifacts/a1', json=body)
    assert r.status_code == 200, r.text
    out = r.json()['data']
    assert len(out['versions']) == 1
    assert out['versions'][0]['text'] == big_text
    assert 'versionsBlob' not in out


async def test_ref_at_or_above_64kb(client_a: httpx.AsyncClient) -> None:
    """Boundary: ≥ 64KB triggers the client to switch to ref mode. Server
    stores the ref envelope as-is; `versions` is the empty array (the
    actual bytes live in the blob store)."""
    ws_id = await _seed_workspace(client_a)
    ref_env = {
        'type': 'ref',
        'url': 'https://example.invalid/blobs/deadbeef/data?exp=1&sig=y',
        'sha256': 'b' * 64,
        'size': 65536,
        'content_type': 'application/json',
    }
    body = _artifact_body(ws_id, versions=[], versionsBlob=ref_env)
    r = await client_a.put('/api/v1/artifacts/a1', json=body)
    assert r.status_code == 200, r.text
    out = r.json()['data']
    assert out['versionsBlob'] == ref_env
    assert out['versions'] == []


# ---- workspace cascade -----------------------------------------------------
#
# Plan 2026-05-03 修订: artifacts hang directly off workspace_id (NO dialog
# hop — frontend `Artifact` type carries only `workspaceId`). The cascade
# branch in routers/workspaces.py tombstones every artifact whose
# workspace_id matches alongside dialogs + items, all sharing one
# cascade_version.


async def test_workspace_cascade_true_tombstones_artifacts(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    """DELETE /workspaces/{id}?cascade=true must in the same request
    tombstone every artifact whose `workspace_id` matches. The artifacts
    list still shows the rows but as `deleted=true`/`data=null`."""
    ws_id = await _seed_workspace(client_a, 'ws-cascade')
    await client_a.put(
        '/api/v1/artifacts/a1', json=_artifact_body(ws_id, id='a1'),
    )
    await client_a.put(
        '/api/v1/artifacts/a2', json=_artifact_body(ws_id, id='a2'),
    )

    r = await client_a.delete(f'/api/v1/workspaces/{ws_id}?cascade=true')
    assert r.status_code == 200, r.text
    assert r.json()['deleted'] is True

    rows = (await client_a.get('/api/v1/artifacts')).json()
    by_id = {row['id']: row for row in rows}
    for aid in ('a1', 'a2'):
        assert by_id[aid]['deleted'] is True, (
            f'artifact {aid} still alive: {by_id[aid]!r}'
        )
        assert by_id[aid]['data'] is None

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id, deleted_at FROM artifacts WHERE workspace_id = %s '
            'ORDER BY id',
            (ws_id,),
        )
        db_rows = cur.fetchall()
    for aid, deleted_at in db_rows:
        assert deleted_at is not None, f'artifact {aid} not tombstoned in DB'


async def test_workspace_cascade_skips_other_workspaces_artifacts(
    client_a: httpx.AsyncClient,
) -> None:
    """Two workspaces, one artifact each. Cascade-deleting one workspace
    must not touch the other workspace's artifact — protects against a
    bystander workspace_id scoping bug in the cascade branch."""
    ws_a = await _seed_workspace(client_a, 'ws-victim')
    ws_b = await _seed_workspace(client_a, 'ws-bystander')
    await client_a.put(
        '/api/v1/artifacts/a-victim',
        json=_artifact_body(ws_a, id='a-victim'),
    )
    await client_a.put(
        '/api/v1/artifacts/a-bystander',
        json=_artifact_body(ws_b, id='a-bystander'),
    )

    r = await client_a.delete(f'/api/v1/workspaces/{ws_a}?cascade=true')
    assert r.status_code == 200

    rows = (await client_a.get('/api/v1/artifacts')).json()
    by_id = {row['id']: row for row in rows}
    assert by_id['a-victim']['deleted'] is True
    assert by_id['a-bystander']['deleted'] is False, (
        f'bystander artifact tombstoned: {by_id["a-bystander"]!r}'
    )


# ---- Stage 4 / 硬前置 3 — scope-aware list (`?workspaceId=`) ---------------
#
# Single-scope filter: `?workspaceId=`. There is no `?dialogId=` filter for
# artifacts — the frontend `Artifact` type carries only `workspaceId`
# (plan 2026-05-03 修订, see models/artifact.py header). Mirrors the
# dialogs `?workspaceId=` retrofit shape.


async def test_list_with_workspace_id_filters_to_scope(
    client_a: httpx.AsyncClient,
) -> None:
    """Two workspaces with 5 artifacts each. `?workspaceId=ws1` must
    return exactly the 5 artifacts whose workspace_id matches. The other
    workspace's artifacts must not leak."""
    ws1 = await _seed_workspace(client_a, 'ws1')
    ws2 = await _seed_workspace(client_a, 'ws2')
    for i in range(5):
        await client_a.put(
            f'/api/v1/artifacts/ws1-a{i}',
            json=_artifact_body(ws1, id=f'ws1-a{i}', name=f't{i}'),
        )
        await client_a.put(
            f'/api/v1/artifacts/ws2-a{i}',
            json=_artifact_body(ws2, id=f'ws2-a{i}', name=f't{i}'),
        )

    r = await client_a.get('/api/v1/artifacts?workspaceId=ws1')
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = sorted(row['id'] for row in rows)
    assert ids == sorted(f'ws1-a{i}' for i in range(5)), (
        f'expected only ws1 artifacts, got {ids!r}'
    )
    for row in rows:
        assert row['data']['workspaceId'] == 'ws1', (
            f'leaked row from another workspace: {row!r}'
        )


async def test_workspace_id_combined_with_since_and_limit(
    client_a: httpx.AsyncClient,
) -> None:
    """Same cursor envelope semantics as dialogs `?workspaceId=` —
    `limit=` switches the response to `{rows, next_cursor}` and the
    scope filter applies before LIMIT."""
    ws_paged = await _seed_workspace(client_a, 'ws-paged')
    ws_noise = await _seed_workspace(client_a, 'ws-noise')
    for i in range(6):
        await client_a.put(
            f'/api/v1/artifacts/p{i}',
            json=_artifact_body(ws_paged, id=f'p{i}'),
        )
        await client_a.put(
            f'/api/v1/artifacts/n{i}',
            json=_artifact_body(ws_noise, id=f'n{i}'),
        )

    r = await client_a.get(
        '/api/v1/artifacts?workspaceId=ws-paged&since=0&limit=4'
    )
    assert r.status_code == 200, r.text
    page1 = r.json()
    assert isinstance(page1, dict), (
        f'limit= must yield CursorPage envelope, got {type(page1).__name__}: {page1!r}'
    )
    rows1 = page1['rows']
    assert len(rows1) == 4
    for row in rows1:
        assert row['data']['workspaceId'] == 'ws-paged', (
            f'leaked row from ws-noise: {row!r}'
        )
    cursor = page1['next_cursor']
    assert cursor is not None and cursor > 0, (
        f'page is full, expected next_cursor, got {cursor!r}'
    )

    r2 = await client_a.get(
        f'/api/v1/artifacts?workspaceId=ws-paged&since={cursor}&limit=4'
    )
    page2 = r2.json()
    rows2 = page2['rows']
    assert len(rows2) == 2, (
        f'expected last 2 ws-paged artifacts, got {[r["id"] for r in rows2]!r}'
    )
    assert page2['next_cursor'] is None
    all_ids = {r['id'] for r in rows1} | {r['id'] for r in rows2}
    assert all_ids == {f'p{i}' for i in range(6)}, all_ids


async def test_workspace_id_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """User B queries User A's workspaceId. user_id predicate already
    isolates accounts → empty set, status 200, no 403/404 ownership
    leak."""
    ws_a = await _seed_workspace(client_a, 'ws-a-private')
    for i in range(3):
        await client_a.put(
            f'/api/v1/artifacts/a-i{i}',
            json=_artifact_body(ws_a, id=f'a-i{i}'),
        )

    r = await client_b.get(f'/api/v1/artifacts?workspaceId={ws_a}')
    assert r.status_code == 200, (
        f'cross-user scopeId must filter to [], not error; got {r.status_code}: {r.text}'
    )
    rows = r.json()
    assert rows == [], (
        f'B leaked A rows via workspaceId scope: {rows!r}'
    )

    r_a = await client_a.get(f'/api/v1/artifacts?workspaceId={ws_a}')
    assert len(r_a.json()) == 3


async def test_no_scope_param_returns_full_user_table(
    client_a: httpx.AsyncClient,
) -> None:
    """Backwards compatibility — no `?workspaceId=` → bare list of all
    the user's artifacts across all workspaces."""
    ws1 = await _seed_workspace(client_a, 'ws1')
    ws2 = await _seed_workspace(client_a, 'ws2')
    await client_a.put('/api/v1/artifacts/a1', json=_artifact_body(ws1, id='a1'))
    await client_a.put('/api/v1/artifacts/a2', json=_artifact_body(ws2, id='a2'))

    r = await client_a.get('/api/v1/artifacts')
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list), (
        f'no `limit=` → bare list; got {type(rows).__name__}'
    )
    ids = sorted(row['id'] for row in rows)
    assert ids == ['a1', 'a2'], ids
