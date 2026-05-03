"""Bug 1/2/3/4 — server-side contracts behind the auth-hydration fix.

The frontend Bug 1/3/4 fix relies on `GET /api/v1/bootstrap` returning the
right user's data right after a fresh login + on the response being cached
in a cross-account-safe way (see `Vary: Authorization` header). These pytest
cases pin both behaviours so a future backend refactor that drops the
header (or returns the wrong user's rows) gets caught at the API layer
before any e2e spec gets a chance.

Cases:
- test_bootstrap_returns_only_callers_workspaces — A and B each seed one
  workspace via PUT /api/v1/workspaces; A's bootstrap response must contain
  only A's row, B's only B's. Cross-isolation contract.
- test_bootstrap_response_is_cached_per_authorization — both users hit
  /bootstrap; the response carries `Vary: Authorization` so a browser cache
  cannot leak A's response to B in the same UA tab. We assert the header
  shape; the e2e bug2 case2 spec verifies the user-visible effect.
- test_bootstrap_unauth_rejected — missing bearer → 401, not 200 with
  empty payload (which would have masked Bug 2's "401 死链" symptom).
"""
from __future__ import annotations

import httpx
import pytest


pytestmark = pytest.mark.asyncio


async def _put_workspace(client: httpx.AsyncClient, ws_id: str, name: str) -> None:
    r = await client.put(
        f'/api/v1/workspaces/{ws_id}',
        json={
            'id': ws_id,
            'name': name,
            'avatar': {'type': 'icon', 'icon': 'sym_o_deployed_code'},
            'type': 'workspace',
            'parentId': '$root',
            'prompt': '',
            'indexContent': '# index',
            'vars': {},
            'listOpen': {'assistants': True, 'artifacts': False, 'dialogs': True},
        },
    )
    assert r.status_code == 200, f'put workspace {ws_id} failed: {r.status_code} {r.text}'


async def test_bootstrap_returns_only_callers_workspaces(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient
) -> None:
    """Bug 2 cross-account isolation contract — bootstrap must filter by the
    bearer's user_id, never return rows belonging to a different account.
    Without this, the e2e Bug 2 case 2 'cross-account no A residue' check
    can't possibly pass because the server would itself leak rows.
    """
    ws_a = 'wsA-isol-test-1'
    ws_b = 'wsB-isol-test-1'
    await _put_workspace(client_a, ws_a, 'A WS')
    await _put_workspace(client_b, ws_b, 'B WS')

    ra = await client_a.get('/api/v1/bootstrap')
    rb = await client_b.get('/api/v1/bootstrap')
    assert ra.status_code == 200, f'A bootstrap failed: {ra.status_code} {ra.text}'
    assert rb.status_code == 200, f'B bootstrap failed: {rb.status_code} {rb.text}'

    a_ws_ids = {env['id'] for env in ra.json()['workspaces']}
    b_ws_ids = {env['id'] for env in rb.json()['workspaces']}
    assert ws_a in a_ws_ids, f'A bootstrap missing A workspace; got {a_ws_ids}'
    assert ws_b in b_ws_ids, f'B bootstrap missing B workspace; got {b_ws_ids}'
    assert ws_b not in a_ws_ids, (
        f'CROSS-ACCOUNT LEAK: A bootstrap contains B workspace {ws_b}; got {a_ws_ids}'
    )
    assert ws_a not in b_ws_ids, (
        f'CROSS-ACCOUNT LEAK: B bootstrap contains A workspace {ws_a}; got {b_ws_ids}'
    )


async def test_bootstrap_response_carries_vary_authorization(
    client_a: httpx.AsyncClient,
) -> None:
    """Bug 2 cache-control contract — Cache-Control: private + max-age=10
    is set for performance (a layout remount within 10s reuses the
    response). Without `Vary: Authorization`, the browser's cache is keyed
    on URL alone and a logout-then-relogin-as-different-user inside that
    10s window would serve user A's response to user B.

    This case asserts the header shape; the e2e bug2 case 2 spec asserts
    the resulting user-visible effect (B's workspace lands, not A's).
    """
    r = await client_a.get('/api/v1/bootstrap')
    assert r.status_code == 200, f'bootstrap failed: {r.status_code} {r.text}'

    cache_control = r.headers.get('Cache-Control', '')
    assert 'private' in cache_control, f'expected private in Cache-Control; got {cache_control!r}'
    assert 'max-age=10' in cache_control, f'expected max-age=10 in Cache-Control; got {cache_control!r}'

    vary = r.headers.get('Vary', '')
    # Case-insensitive header value check; the spec allows comma-separated
    # tokens so we tolerate `Authorization, X-Foo` etc.
    vary_tokens = {t.strip().lower() for t in vary.split(',') if t.strip()}
    assert 'authorization' in vary_tokens, (
        f"expected 'Authorization' in Vary header to prevent cross-account "
        f'cache leak (Bug 2 fix); got Vary={vary!r}'
    )


async def test_bootstrap_unauth_rejected(anon_client: httpx.AsyncClient) -> None:
    """Bug 2 'no 401 死链' contract — the bootstrap endpoint must 401 on
    missing/invalid bearer rather than returning an empty payload. An
    empty 200 would mask cross-account leaks (we couldn't tell whether B
    saw nothing because they have nothing or because the request was
    silently anon-ified).
    """
    r = await anon_client.get('/api/v1/bootstrap')
    assert r.status_code == 401, f'expected 401 for unauth bootstrap; got {r.status_code} {r.text}'


async def test_bootstrap_returns_user_perfs_reactive_for_bug4(
    client_a: httpx.AsyncClient,
) -> None:
    """Bug 4 hydration contract — the `#user-perfs` reactive (which the
    Bug 4 fix relies on for default model/provider hydration) must
    appear in the bootstrap response's `reactives` array so
    persistentReactive's useLiveQuery picks it up on cache-hit instead
    of fetching individually.
    """
    perfs_value = {
        'provider': {
            'type': 'openai-response',
            'settings': {'apiKey': 'sk-bug4-bootstrap-test'},
        },
        'model': {'name': 'bug4-bootstrap-sentinel'},
        'themeHue': 200,
    }
    r = await client_a.put(
        '/api/v1/reactives/%23user-perfs',
        json=perfs_value,
    )
    assert r.status_code == 200, f'put reactive failed: {r.status_code} {r.text}'

    r = await client_a.get('/api/v1/bootstrap')
    assert r.status_code == 200, f'bootstrap failed: {r.status_code} {r.text}'
    body = r.json()
    keys_present = {env['key'] for env in body['reactives']}
    assert '#user-perfs' in keys_present, (
        f"#user-perfs reactive missing from bootstrap.reactives; "
        f'got keys {keys_present}'
    )
    perfs_env = next(
        (e for e in body['reactives'] if e['key'] == '#user-perfs'), None
    )
    assert perfs_env is not None
    assert perfs_env['data']['provider']['settings']['apiKey'] == 'sk-bug4-bootstrap-test'
    assert perfs_env['data']['model']['name'] == 'bug4-bootstrap-sentinel'
