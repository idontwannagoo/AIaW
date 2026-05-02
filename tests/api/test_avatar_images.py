"""Stage 3 / 批次-3b — avatar_images REST CRUD.

id-PK pattern same as providers / assistants. The unique-to-this-table cases:
- contentBuffer is sent as a base64 string in `data` (front-end encodes
  ArrayBuffer → base64 before PUT) — server is opaque, just round-trips it
- a moderately large blob (~50 KB base64) round-trips correctly while still
  staying inline — Stage 4 hard-pre-2 is when ≥ 64KB starts going to object
  storage; until then everything is JSONB.
"""
from __future__ import annotations

import base64
import os

import httpx
import pytest


def _b64(n_bytes: int) -> str:
    return base64.b64encode(os.urandom(n_bytes)).decode('ascii')


def _row(content: str, mime: str = 'image/png') -> dict:
    return {'id': 'placeholder', 'contentBuffer': content, 'mimeType': mime}


async def test_put_creates_and_list_returns_it(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    body_in = _row(_b64(64))
    r = await client_a.put('/api/v1/avatar-images/img1', json=body_in)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['id'] == 'img1'
    assert body['deleted'] is False
    assert body['data']['contentBuffer'] == body_in['contentBuffer']
    assert body['data']['mimeType'] == 'image/png'
    assert body['version'] >= 1

    r = await client_a.get('/api/v1/avatar-images')
    rows = r.json()
    assert [row['id'] for row in rows] == ['img1']

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, data, deleted_at FROM avatar_images WHERE id = %s',
            ('img1',),
        )
        owner, data, deleted_at = cur.fetchone()
    assert owner == user_a['id']
    assert data['contentBuffer'] == body_in['contentBuffer']
    assert deleted_at is None


async def test_get_returns_single_row(client_a: httpx.AsyncClient) -> None:
    await client_a.put('/api/v1/avatar-images/img1', json=_row(_b64(32)))
    r = await client_a.get('/api/v1/avatar-images/img1')
    assert r.status_code == 200
    assert r.json()['id'] == 'img1'

    r = await client_a.get('/api/v1/avatar-images/missing')
    assert r.status_code == 404


async def test_put_update_bumps_version(client_a: httpx.AsyncClient) -> None:
    blob1 = _b64(32)
    blob2 = _b64(32)
    r1 = await client_a.put('/api/v1/avatar-images/img1', json=_row(blob1))
    v1 = r1.json()['version']
    r2 = await client_a.put('/api/v1/avatar-images/img1', json=_row(blob2))
    v2 = r2.json()['version']

    assert v2 > v1
    assert r2.json()['data']['contentBuffer'] == blob2

    r3 = await client_a.get('/api/v1/avatar-images/img1')
    assert r3.json()['version'] == v2
    assert r3.json()['data']['contentBuffer'] == blob2


async def test_since_filter_drops_older_revisions(
    client_a: httpx.AsyncClient,
) -> None:
    await client_a.put('/api/v1/avatar-images/i1', json=_row(_b64(32)))
    r2 = await client_a.put('/api/v1/avatar-images/i2', json=_row(_b64(32)))
    cutoff = r2.json()['version']
    r3 = await client_a.put('/api/v1/avatar-images/i3', json=_row(_b64(32)))

    rows = (await client_a.get(f'/api/v1/avatar-images?since={cutoff}')).json()
    ids = [row['id'] for row in rows]
    assert ids == ['i3']
    assert rows[0]['version'] == r3.json()['version']


async def test_soft_delete_yields_tombstone_in_list(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    await client_a.put('/api/v1/avatar-images/img1', json=_row(_b64(32)))

    r_del = await client_a.delete('/api/v1/avatar-images/img1')
    assert r_del.status_code == 200
    assert r_del.json()['deleted'] is True
    assert r_del.json()['data'] is None

    rows = (await client_a.get('/api/v1/avatar-images')).json()
    assert len(rows) == 1
    assert rows[0]['deleted'] is True
    assert rows[0]['data'] is None


async def test_account_isolation(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    await client_a.put('/api/v1/avatar-images/img1', json=_row(_b64(32)))
    rows_b = (await client_b.get('/api/v1/avatar-images')).json()
    assert rows_b == [], f'B leaked A row: {rows_b!r}'
    r = await client_b.get('/api/v1/avatar-images/img1')
    assert r.status_code == 404


async def test_large_inline_blob_roundtrip(client_a: httpx.AsyncClient) -> None:
    """50 KB base64 (≈ 37 KB binary) should still ride inline through JSONB
    without being mangled. Stage 4 hard-pre-2 will rewrite this to require
    ≥ 64 KB → ref split; for now it's the canary that JSONB handles
    realistic-sized avatars."""
    blob = _b64(50_000)  # ~67 KB ascii — purposely > 64 KB to prove inline
    r = await client_a.put('/api/v1/avatar-images/big', json=_row(blob))
    assert r.status_code == 200, r.text
    assert r.json()['data']['contentBuffer'] == blob

    r2 = await client_a.get('/api/v1/avatar-images/big')
    assert r2.json()['data']['contentBuffer'] == blob


async def test_unauth_request_rejected(anon_client: httpx.AsyncClient) -> None:
    r = await anon_client.get('/api/v1/avatar-images')
    assert r.status_code == 401
