"""Stage 1 / Step 1 — backend skeleton + health.

Maps cloud-sync-migration plan Stage 1 / Step 1's curl-only criterion onto a
single pytest case so future runs can assert it without manual curl.
"""
from __future__ import annotations

import httpx


async def test_health_returns_ok(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/health')
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'ok'
    assert body['db'] == 'ok'
    # `error` is null when db check succeeded; the field exists either way.
    assert body.get('error') is None
