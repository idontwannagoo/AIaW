"""Stage 4.5 / Step 8 — `GET /api/v1/bootstrap` first-screen hydration.

Single round-trip endpoint that returns every small server-routed table
in full plus a per-dialog "recent N messages" tail capped at 1MB. Plan
强约束 (line 1234-1241):

  - Response top-level keys MUST be exactly:
      schema_version, workspaces, dialogs, providers, assistants,
      installed_plugins, reactives, avatar_images, messages_recent
    (8 keys total — `items` and `artifacts` are scoped-pull only).
  - Each list element is the same envelope shape as the per-table list
    endpoint / realtime event uses (`{id|key, version, updated_at,
    deleted, data}`).
  - Per-dialog tail capped at 50 newest messages.
  - Total response size capped at 1MB; truncation is at the *dialog*
    boundary (we never send a partial tail).
  - `Cache-Control: private, max-age=10` so a within-session layout
    remount doesn't re-hit the endpoint.
  - Bootstrap queries `WHERE deleted_at IS NULL` and is import-status
    agnostic — Phase B partial writes show up as soon as they hit the
    structural tables.

Mapping back to the plan's "通过判据" (one test per bullet, plus two
defense-in-depth assertions on the items/artifacts exclusion and on
empty-tail dialogs being skipped):

  - test_returns_all_small_tables_in_one_response
  - test_messages_recent_excludes_items_artifacts (二次断言)
  - test_messages_recent_limited_to_50_per_dialog
  - test_response_size_under_1mb_for_typical_user
  - test_response_truncates_to_under_1mb_for_heavy_user
  - test_account_isolation
  - test_partial_data_during_active_import
  - test_cache_control_header_set
  - test_envelope_shape_matches_list_endpoint
  - test_unauth_request_rejected
  - test_dialogs_with_no_messages_dont_appear_in_messages_recent
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest


# ---- seed helpers (kept local to this file — same pattern as test_messages.py)


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


def _message_body(dialog_id: str, msg_id: str = 'm1', text: str = 'hello') -> dict:
    return {
        'id': msg_id,
        'type': 'assistant',
        'dialogId': dialog_id,
        'contents': [
            {'type': 'assistant-message', 'text': text},
        ],
        'status': 'default',
    }


async def _seed_workspace(client: httpx.AsyncClient, ws_id: str = 'w1') -> str:
    r = await client.put(
        f'/api/v1/workspaces/{ws_id}',
        json={**_DEFAULT_WORKSPACE, 'name': f'WS-{ws_id}'},
    )
    assert r.status_code == 200, r.text
    return ws_id


async def _seed_dialog(
    client: httpx.AsyncClient, ws_id: str, dlg_id: str = 'd1',
) -> str:
    r = await client.put(
        f'/api/v1/dialogs/{dlg_id}',
        json={**_dialog_body(ws_id), 'name': f'D-{dlg_id}'},
    )
    assert r.status_code == 200, r.text
    return dlg_id


# ---- 1. response shape: 8 keys, no items / artifacts ------------------------


async def test_returns_all_small_tables_in_one_response(
    client_a: httpx.AsyncClient,
) -> None:
    """Response top-level key set is exactly the 8 documented keys."""
    r = await client_a.get('/api/v1/bootstrap')
    assert r.status_code == 200, r.text
    body = r.json()
    expected = {
        'schema_version',
        'workspaces',
        'dialogs',
        'providers',
        'assistants',
        'installed_plugins',
        'reactives',
        'avatar_images',
        'messages_recent',
    }
    actual = set(body.keys())
    assert actual == expected, f'unexpected keys: {actual ^ expected}'
    # Defense-in-depth: scoped-pull tables must never sneak in.
    assert 'items' not in body, 'items leaked into bootstrap response'
    assert 'artifacts' not in body, 'artifacts leaked into bootstrap response'
    assert isinstance(body['schema_version'], int)


# ---- 2. messages_recent capped at 50 per dialog ------------------------------


async def test_messages_recent_limited_to_50_per_dialog(
    client_a: httpx.AsyncClient,
    user_a,
    pg_conn: psycopg.Connection,
) -> None:
    """Seed 100 messages in 1 dialog; bootstrap returns only the 50 newest.

    PG-direct seed: deterministic updated_at ordering matters here (we
    must verify the algorithm picks the m050..m099 tail, not a random
    50). httpx PUTs in a tight loop don't give enough timestamp
    resolution to guarantee the order.
    """
    base_time = datetime.now(timezone.utc)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workspaces (id, user_id, data)
            VALUES (%s, %s, %s::jsonb)
            """,
            ('w1', user_a['id'], '{"id":"w1","name":"WS","type":"workspace"}'),
        )
        cur.execute(
            """
            INSERT INTO dialogs (id, user_id, workspace_id, data)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (
                'd1',
                user_a['id'],
                'w1',
                '{"id":"d1","name":"D","workspaceId":"w1"}',
            ),
        )
        for i in range(100):
            msg_id = f'm{i:03d}'
            ts = base_time + timedelta(seconds=i)
            cur.execute(
                """
                INSERT INTO messages
                    (id, user_id, dialog_id, data, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (
                    msg_id,
                    user_a['id'],
                    'd1',
                    '{"id":"' + msg_id + '","type":"assistant","dialogId":"d1",'
                    '"contents":[{"type":"assistant-message","text":"x"}],'
                    '"status":"default"}',
                    ts,
                ),
            )

    r = await client_a.get('/api/v1/bootstrap')
    body = r.json()
    assert len(body['messages_recent']) == 50, (
        f"expected 50 (cap per dialog), got {len(body['messages_recent'])}"
    )
    # All envelopes belong to that one dialog.
    for env in body['messages_recent']:
        assert env['data']['dialogId'] == 'd1'

    # The 50 returned must be the last 50 inserted (m050..m099). They come
    # back ordered by updated_at DESC — i.e. m099 first, m050 last.
    returned_ids = {env['id'] for env in body['messages_recent']}
    expected_ids = {f'm{i:03d}' for i in range(50, 100)}
    assert returned_ids == expected_ids, (
        f'expected newest 50, got: {sorted(returned_ids)[:5]}...'
    )


# ---- 3. typical-user response < 1MB ------------------------------------------


def _bulk_seed_via_pg(
    pg_conn: psycopg.Connection,
    user_id: str,
    workspace_id: str,
    dialogs: int,
    messages_per_dialog: int,
    msg_text: str = 'x',
) -> None:
    """Skip the HTTP layer for big seeds — direct PG INSERT.

    Two reasons we don't use the API for >100 row seeds:
      1. ~5000 sequential httpx PUTs takes 30s+ and the JWT bearer ages
         out of the cached `client_a` connection pool in batch runs (we
         saw 401 'user not found' after long loops in the first draft).
      2. The bootstrap endpoint reads from PG with `WHERE deleted_at IS
         NULL` — same predicate as the API path — so the only thing the
         API path adds is overhead. Bypassing it makes the case test
         exactly what bootstrap promises (a SQL-level snapshot) without
         the seed dominating runtime.

    We populate `data` with the minimum JSONB the bootstrap envelope
    serializes verbatim. `version` and `updated_at` are auto-assigned
    by the same `global_change_seq` and `now()` defaults the API uses.
    """
    with pg_conn.cursor() as cur:
        # Workspace.
        cur.execute(
            """
            INSERT INTO workspaces (id, user_id, data)
            VALUES (%s, %s, %s::jsonb)
            """,
            (
                workspace_id,
                user_id,
                '{"id":"' + workspace_id + '","name":"WS","type":"workspace",'
                '"avatar":{"type":"icon","icon":"sym_o_deployed_code"},'
                '"parentId":"$root","prompt":"","indexContent":"# index",'
                '"vars":{},"listOpen":{"assistants":true,"artifacts":false,'
                '"dialogs":true}}',
            ),
        )
        # Dialogs + messages. We assign updated_at deterministically so the
        # MAX(updated_at) DESC ordering inside `_gather_messages_recent`
        # picks the highest-index dialog first (matches "most recently
        # active dialogs come first").
        base_time = datetime.now(timezone.utc)
        for d in range(dialogs):
            dlg_id = f'd{d:04d}'
            cur.execute(
                """
                INSERT INTO dialogs (id, user_id, workspace_id, data)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    dlg_id,
                    user_id,
                    workspace_id,
                    '{"id":"' + dlg_id + '","name":"D","workspaceId":"'
                    + workspace_id + '","msgTree":{"$root":[]},"msgRoute":[],'
                    '"inputVars":{}}',
                ),
            )
            for m in range(messages_per_dialog):
                msg_id = f'{dlg_id}m{m:02d}'
                # Higher dialog index = newer updated_at (so it ranks higher
                # in `MAX(updated_at) DESC`). Within a dialog, higher m
                # index = newer.
                ts = base_time + timedelta(seconds=d * 1000 + m)
                cur.execute(
                    """
                    INSERT INTO messages
                        (id, user_id, dialog_id, data, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        msg_id,
                        user_id,
                        dlg_id,
                        '{"id":"' + msg_id + '","type":"assistant","dialogId":"'
                        + dlg_id + '","contents":[{"type":"assistant-message",'
                        '"text":"' + msg_text + '"}],"status":"default"}',
                        ts,
                    ),
                )


async def test_response_size_under_1mb_for_typical_user(
    client_a: httpx.AsyncClient,
    user_a,
    pg_conn: psycopg.Connection,
) -> None:
    """100 dialogs × 50 messages = 5000 messages — typical heavy user.

    The 1MB budget is per the plan; in this test we only assert the
    actual byte size of the returned payload comes in under it. We don't
    construct giant text per message because that would be testing the
    truncation case (test 4) — typical-user means "lots of small rows".

    Direct PG seed (see `_bulk_seed_via_pg` docstring for rationale).
    """
    _bulk_seed_via_pg(
        pg_conn,
        user_id=user_a['id'],
        workspace_id='w1',
        dialogs=100,
        messages_per_dialog=50,
    )

    r = await client_a.get('/api/v1/bootstrap')
    assert r.status_code == 200, r.text
    raw = r.content  # bytes, what hits the wire
    size = len(raw)
    body = r.json()
    assert size < 1_000_000, (
        f'typical-user response size {size}B exceeds 1MB budget; '
        f'top-level counts: '
        f'workspaces={len(body["workspaces"])} '
        f'dialogs={len(body["dialogs"])} '
        f'messages_recent={len(body["messages_recent"])}'
    )
    # Sanity: with 5000 small messages we should still ship the per-dialog
    # tails (each dialog ≤50, so ≤5000 total — we expect ~all of them
    # since payload is tiny).
    assert len(body['messages_recent']) > 0


# ---- 4. heavy-user truncation (BIG fixture) ----------------------------------


@pytest.mark.slow
async def test_response_truncates_to_under_1mb_for_heavy_user(
    client_a: httpx.AsyncClient,
    user_a,
    pg_conn: psycopg.Connection,
) -> None:
    """1000 dialogs × 50 messages with padded message text — far exceeds budget.

    Validates that:
      - response stays under 1MB + 5% framing headroom (truncation works),
      - len(messages_recent) << 50,000 (we did stop early),
      - truncation is at the *dialog* boundary (each dialog that DID
        ship has a complete 50-message tail, never a partial slice).

    Marked slow: even with PG-direct seed, 50k INSERTs takes ~10s.
    Excluded from `pnpm test:api -m "not slow"`.
    """
    # Pad message text so each row is ~500 bytes — guarantees 1000×50 rows
    # blow the 1MB budget by ~25x.
    big_text = 'X' * 500
    _bulk_seed_via_pg(
        pg_conn,
        user_id=user_a['id'],
        workspace_id='w1',
        dialogs=1000,
        messages_per_dialog=50,
        msg_text=big_text,
    )

    r = await client_a.get('/api/v1/bootstrap')
    assert r.status_code == 200, r.text
    raw = r.content
    size = len(raw)
    body = r.json()

    # Hard size check: 1MB + 5% framing headroom.
    assert size <= 1_050_000, (
        f'heavy-user response size {size}B exceeds 1MB+5% framing budget'
    )
    # Truncation actually happened — we did NOT ship all 50k messages.
    assert len(body['messages_recent']) < 50_000, (
        f"messages_recent={len(body['messages_recent'])} "
        f'(should be far less than 50k)'
    )
    # Truncation is at the dialog boundary: the messages we shipped are
    # complete tails per dialog. Group by dialogId and assert each dialog
    # got exactly 50 (its full cap), not a partial slice.
    by_dialog: dict[str, int] = {}
    for env in body['messages_recent']:
        by_dialog[env['data']['dialogId']] = by_dialog.get(
            env['data']['dialogId'], 0
        ) + 1
    assert by_dialog, 'no dialogs at all in messages_recent'
    for dlg, count in by_dialog.items():
        assert count == 50, (
            f'dialog {dlg} has partial tail of {count} messages — '
            'truncation not at dialog boundary'
        )


# ---- 5. account isolation ----------------------------------------------------


async def test_account_isolation(
    client_a: httpx.AsyncClient,
    client_b: httpx.AsyncClient,
) -> None:
    """A's bootstrap must not contain any of B's rows and vice versa."""
    ws_a = await _seed_workspace(client_a, 'wA')
    dlg_a = await _seed_dialog(client_a, ws_a, 'dA')
    body_a = _message_body(dlg_a, msg_id='mA', text='a-only')
    await client_a.put('/api/v1/messages/mA', json=body_a)

    ws_b = await _seed_workspace(client_b, 'wB')
    dlg_b = await _seed_dialog(client_b, ws_b, 'dB')
    body_b = _message_body(dlg_b, msg_id='mB', text='b-only')
    await client_b.put('/api/v1/messages/mB', json=body_b)

    boot_a = (await client_a.get('/api/v1/bootstrap')).json()
    boot_b = (await client_b.get('/api/v1/bootstrap')).json()

    a_ws_ids = {w['id'] for w in boot_a['workspaces']}
    a_dlg_ids = {d['id'] for d in boot_a['dialogs']}
    a_msg_ids = {m['id'] for m in boot_a['messages_recent']}
    assert 'wB' not in a_ws_ids, f'B workspace leaked into A: {a_ws_ids}'
    assert 'dB' not in a_dlg_ids, f'B dialog leaked into A: {a_dlg_ids}'
    assert 'mB' not in a_msg_ids, f'B message leaked into A: {a_msg_ids}'

    b_ws_ids = {w['id'] for w in boot_b['workspaces']}
    b_dlg_ids = {d['id'] for d in boot_b['dialogs']}
    b_msg_ids = {m['id'] for m in boot_b['messages_recent']}
    assert 'wA' not in b_ws_ids
    assert 'dA' not in b_dlg_ids
    assert 'mA' not in b_msg_ids


# ---- 6. active import doesn't block bootstrap --------------------------------


async def test_partial_data_during_active_import(
    client_a: httpx.AsyncClient,
    user_a,
    pg_conn: psycopg.Connection,
) -> None:
    """Bootstrap returns whatever Phase B has finished writing — it does
    NOT wait on or care about an in-flight import. We seed N workspaces
    via the API + manually INSERT an import_jobs row in status='phase_c'
    and assert bootstrap still returns the workspaces (i.e. no implicit
    "block while job active" filter)."""
    # Seed 3 workspaces via the API (mirrors what Phase B would have
    # written).
    for i in range(3):
        await _seed_workspace(client_a, f'w{i}')

    # Manually INSERT an active import_jobs row to simulate Phase C in
    # progress. The bootstrap endpoint must not look at this table.
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO import_jobs
                (id, user_id, status, processed_rows, processed_blobs,
                 dead_letter, created_at, updated_at)
            VALUES (%s, %s, %s, 0, 0, '[]'::jsonb, now(), now())
            """,
            ('job-active', user_a['id'], 'phase_c'),
        )

    r = await client_a.get('/api/v1/bootstrap')
    assert r.status_code == 200, r.text
    body = r.json()
    ws_ids = {w['id'] for w in body['workspaces']}
    assert ws_ids == {'w0', 'w1', 'w2'}, (
        f'bootstrap blocked by active import: got {ws_ids}'
    )


# ---- 7. cache header ---------------------------------------------------------


async def test_cache_control_header_set(client_a: httpx.AsyncClient) -> None:
    """Cache-Control must be exactly `private, max-age=10`."""
    r = await client_a.get('/api/v1/bootstrap')
    assert r.status_code == 200
    cc = r.headers.get('Cache-Control')
    assert cc == 'private, max-age=10', f'unexpected Cache-Control: {cc!r}'


# ---- 8. envelope shape matches list endpoint ---------------------------------


async def test_envelope_shape_matches_list_endpoint(
    client_a: httpx.AsyncClient,
) -> None:
    """A workspace envelope from bootstrap must match the same row's
    envelope from `GET /api/v1/workspaces` byte-for-byte (same field set,
    same `data` content). This is the contract that lets the per-table
    apply paths consume bootstrap rows without a special decoder."""
    await _seed_workspace(client_a, 'w1')

    list_rows = (await client_a.get('/api/v1/workspaces')).json()
    boot = (await client_a.get('/api/v1/bootstrap')).json()

    list_w1 = next(r for r in list_rows if r['id'] == 'w1')
    boot_w1 = next(r for r in boot['workspaces'] if r['id'] == 'w1')

    assert set(list_w1.keys()) == {'id', 'version', 'updated_at', 'deleted', 'data'}
    assert set(boot_w1.keys()) == {'id', 'version', 'updated_at', 'deleted', 'data'}
    # Byte-level equality on the `data` field — bootstrap must not strip
    # or rewrite anything.
    assert boot_w1['data'] == list_w1['data']
    assert boot_w1['version'] == list_w1['version']
    assert boot_w1['deleted'] == list_w1['deleted']


# ---- 9. unauth ---------------------------------------------------------------


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    """No auth → 401; the endpoint participates in the same auth gate as
    the rest of the data API."""
    r = await anon_client.get('/api/v1/bootstrap')
    assert r.status_code == 401, r.text


# ---- 10. items / artifacts excluded (defense-in-depth) -----------------------


async def test_messages_recent_excludes_items_artifacts(
    client_a: httpx.AsyncClient,
) -> None:
    """Even after seeding items / artifacts via their REST endpoints,
    bootstrap must not include them. (The endpoint code never reads
    those tables — this case is the contract belt-and-suspenders so a
    future "convenience" addition fails CI.)"""
    ws_id = await _seed_workspace(client_a)
    dlg_id = await _seed_dialog(client_a, ws_id)

    # Seed an item — content shape mirrors test_items.py minimal input.
    await client_a.put(
        '/api/v1/items/i1',
        json={
            'id': 'i1',
            'type': 'text',
            'name': 'note',
            'dialogId': dlg_id,
            'contentText': 'note body',
        },
    )
    # Seed an artifact.
    await client_a.put(
        '/api/v1/artifacts/a1',
        json={
            'id': 'a1',
            'name': 'art',
            'workspaceId': ws_id,
            'versions': [],
            'currIndex': -1,
            'open': False,
        },
    )

    r = await client_a.get('/api/v1/bootstrap')
    body = r.json()
    assert 'items' not in body
    assert 'artifacts' not in body
    # And the keys we DO expect haven't grown an "items"-flavoured item by
    # accident (e.g. some buggy code mapping items into messages_recent).
    for env in body['messages_recent']:
        # message envelope's `data.dialogId` is fine; we just verify there's
        # no `name`/`contentText` field hinting at items contamination.
        assert 'contentText' not in (env.get('data') or {}), (
            'items field leaked into messages_recent envelope'
        )


# ---- 11. dialogs with zero messages don't appear in messages_recent ----------


async def test_dialogs_with_no_messages_dont_appear_in_messages_recent(
    client_a: httpx.AsyncClient,
) -> None:
    """A dialog with no non-deleted messages contributes 0 entries; only
    the dialog with messages shows up under messages_recent. (The dialog
    itself still shows up under `dialogs`.)"""
    ws_id = await _seed_workspace(client_a)
    dlg_empty = await _seed_dialog(client_a, ws_id, 'd-empty')
    dlg_full = await _seed_dialog(client_a, ws_id, 'd-full')

    for i in range(5):
        body = _message_body(dlg_full, msg_id=f'mf{i}', text=f'msg-{i}')
        await client_a.put(f'/api/v1/messages/{body["id"]}', json=body)

    r = await client_a.get('/api/v1/bootstrap')
    body = r.json()
    dlg_ids_in_dialogs = {d['id'] for d in body['dialogs']}
    assert dlg_empty in dlg_ids_in_dialogs
    assert dlg_full in dlg_ids_in_dialogs

    msg_dialogs = {env['data']['dialogId'] for env in body['messages_recent']}
    assert dlg_full in msg_dialogs
    assert dlg_empty not in msg_dialogs, (
        f'empty dialog leaked into messages_recent: {msg_dialogs}'
    )
    assert len(body['messages_recent']) == 5
