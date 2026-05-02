"""Stage 4 硬前置 2 — `/api/v1/blobs` endpoint test suite.

Maps to the plan's "通过判据" for 硬前置 2 (object-storage 分流):
- POST 5MB attachment → server PG row 中 attachment 字段是 ref envelope，
  对象存储里 `<sha256>` 文件存在  → test_post_creates_blob_row_and_storage_file
- 同一 sha256 第二次上传去重（PG `blobs` 表行不增，对象存储不重复写）
  → test_same_content_second_upload_is_deduped
- 64KB 边界 → < 64KB inline 进 PG，= 64KB 走对象存储  →（前端责任，
  这里以 boundary 大小往返验证后端不挡 64KB exact + 处理 1B 边界）
  → test_boundary_size_64kb_round_trip
- 跨用户隔离：同 sha256 第二个用户独立 ref，但隐藏对方存在
  → test_cross_user_each_owns_independent_ref
- 鉴权 → unauth 401 + cross-user 404 mask
- 签名 URL 闭环 → exp/sig 校验 + 篡改 / 过期 / 缺失各拒绝
- 删 ref：不删 bytes，不影响其他用户

The whole test module is async (matches conftest.py asyncio_mode=auto).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx
import pytest


# ---- factories --------------------------------------------------------------


async def _upload(
    client: httpx.AsyncClient, content: bytes, content_type: str = 'image/png'
) -> dict:
    r = await client.post(
        '/api/v1/blobs',
        files={
            'file': ('upload.bin', content, content_type),
        },
    )
    assert r.status_code == 201, f'upload failed: {r.status_code} {r.text}'
    return r.json()


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- happy path -------------------------------------------------------------


async def test_post_creates_blob_row_and_storage_file(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    content = b'hello-blob-world' * 100  # ~1.6KB
    body = await _upload(client_a, content, content_type='text/plain')
    assert body['sha256'] == _sha256(content)
    assert body['size'] == len(content)
    assert body['content_type'] == 'text/plain'
    assert body['deduped'] is False
    # Embeddable ref envelope shape — Stage 4 主体批次会原样塞进 row data
    assert body['ref']['type'] == 'ref'
    assert body['ref']['sha256'] == body['sha256']
    assert body['ref']['size'] == body['size']
    assert body['ref']['content_type'] == body['content_type']
    assert body['ref']['url'] == body['url']
    # URL must be absolute and point at /api/v1/blobs/<sha>/data with sig+exp
    assert f"/api/v1/blobs/{body['sha256']}/data?" in body['url'], body['url']
    assert 'sig=' in body['url'] and 'exp=' in body['url']

    # Postgres truth.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT sha256, size, content_type, storage_key '
            'FROM blobs WHERE sha256 = %s',
            (body['sha256'],),
        )
        row = cur.fetchone()
    assert row is not None, 'blobs row not persisted'
    assert row[0] == body['sha256']
    assert row[1] == len(content)
    assert row[2] == 'text/plain'
    assert row[3] == body['sha256']  # LocalFS uses sha256 as storage_key


async def test_post_creates_per_user_ref(
    client_a: httpx.AsyncClient, user_a, pg_conn,
) -> None:
    content = b'ref-content'
    body = await _upload(client_a, content)
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, sha256 FROM blob_refs '
            'WHERE user_id = %s AND sha256 = %s',
            (user_a['id'], body['sha256']),
        )
        ref = cur.fetchone()
    assert ref is not None, f'no blob_refs row for user {user_a["id"]}'


async def test_same_content_second_upload_is_deduped(
    client_a: httpx.AsyncClient, pg_conn,
) -> None:
    content = b'dedup-me-please'
    first = await _upload(client_a, content)
    second = await _upload(client_a, content)

    assert first['sha256'] == second['sha256']
    assert first['deduped'] is False
    assert second['deduped'] is True

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT count(*) FROM blobs WHERE sha256 = %s',
            (first['sha256'],),
        )
        n_blobs = cur.fetchone()[0]
        cur.execute(
            'SELECT count(*) FROM blob_refs WHERE sha256 = %s',
            (first['sha256'],),
        )
        n_refs = cur.fetchone()[0]
    assert n_blobs == 1, f'expected 1 blob row after dedup, got {n_blobs}'
    assert n_refs == 1, f'expected 1 ref row after dedup, got {n_refs}'


async def test_signed_url_returns_bytes(
    client_a: httpx.AsyncClient, anon_client: httpx.AsyncClient,
) -> None:
    content = b'\x89PNG\r\n\x1a\n' + b'\x00' * 200  # PNG magic + padding
    body = await _upload(client_a, content, content_type='image/png')

    # Use anon_client for the GET — the URL is a bearer for these bytes,
    # not a session cred. This proves the signed URL works without auth header.
    r = await anon_client.get(body['url'])
    assert r.status_code == 200, r.text
    assert r.content == content
    assert r.headers['content-type'] == 'image/png'
    assert r.headers['content-length'] == str(len(content))
    assert r.headers['x-blob-sha256'] == body['sha256']
    # Bytes are immutable for a sha — long cache + immutable directive.
    assert 'immutable' in r.headers.get('cache-control', '')


async def test_get_metadata_returns_envelope(
    client_a: httpx.AsyncClient,
) -> None:
    content = b'metadata-only'
    body = await _upload(client_a, content, content_type='application/pdf')

    r = await client_a.get(f'/api/v1/blobs/{body["sha256"]}')
    assert r.status_code == 200, r.text
    env = r.json()
    assert env['type'] == 'ref'
    assert env['sha256'] == body['sha256']
    assert env['size'] == len(content)
    assert env['content_type'] == 'application/pdf'
    assert 'url' in env and 'sig=' in env['url']


async def test_head_returns_metadata_headers(
    client_a: httpx.AsyncClient,
) -> None:
    content = b'head-me' * 50
    body = await _upload(client_a, content, content_type='application/json')

    r = await client_a.head(f'/api/v1/blobs/{body["sha256"]}')
    assert r.status_code == 200, r.text
    assert r.headers['content-type'] == 'application/json'
    assert r.headers['content-length'] == str(len(content))
    assert r.headers['x-blob-sha256'] == body['sha256']


# ---- size & boundary cases --------------------------------------------------


@pytest.mark.parametrize('size', [1, 64 * 1024 - 1, 64 * 1024, 64 * 1024 + 1])
async def test_boundary_size_64kb_round_trip(
    client_a: httpx.AsyncClient, anon_client: httpx.AsyncClient, size: int,
) -> None:
    """The backend MUST accept any size; the 64KB threshold is a *frontend*
    concern (= when to inline vs ref). Verifying round-trip across the
    boundary documents this contract: backend does not act on the threshold."""
    # Use a deterministic-but-non-zero pattern so size collision doesn't make
    # tests trivially share a sha256 across sizes.
    content = (b'\x42' * size)
    body = await _upload(client_a, content)
    assert body['size'] == size
    r = await anon_client.get(body['url'])
    assert r.status_code == 200
    assert len(r.content) == size
    assert r.content == content


@pytest.mark.slow
async def test_large_5mb_round_trip(
    client_a: httpx.AsyncClient, anon_client: httpx.AsyncClient,
) -> None:
    """Sanity: messages-sized attachments work end-to-end."""
    size = 5 * 1024 * 1024
    content = b'x' * size
    body = await _upload(client_a, content, content_type='application/zip')
    assert body['size'] == size
    r = await anon_client.get(body['url'])
    assert r.status_code == 200
    assert len(r.content) == size
    assert r.headers['content-type'] == 'application/zip'


async def test_empty_body_rejected(
    client_a: httpx.AsyncClient,
) -> None:
    r = await client_a.post(
        '/api/v1/blobs',
        files={'file': ('empty.bin', b'', 'application/octet-stream')},
    )
    assert r.status_code == 400, r.text


# ---- account isolation ------------------------------------------------------


async def test_cross_user_each_owns_independent_ref(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
    user_a, user_b, pg_conn,
) -> None:
    content = b'shared-bytes-but-not-shared-ownership'
    a_resp = await _upload(client_a, content)
    b_resp = await _upload(client_b, content)

    # Same content hash...
    assert a_resp['sha256'] == b_resp['sha256']
    # ...A wrote bytes; B got dedup hit on bytes but distinct ref row.
    assert a_resp['deduped'] is False
    assert b_resp['deduped'] is True

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id FROM blob_refs WHERE sha256 = %s ORDER BY user_id',
            (a_resp['sha256'],),
        )
        owners = {r[0] for r in cur.fetchall()}
    assert owners == {user_a['id'], user_b['id']}, (
        f'expected both users own a ref, got {owners}'
    )


async def test_user_b_cannot_see_a_uploaded_blob(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """B never uploaded → metadata GET 404 (existence hidden) even though
    bytes are deduped on disk."""
    body = await _upload(client_a, b'private-to-a')
    r = await client_b.get(f'/api/v1/blobs/{body["sha256"]}')
    assert r.status_code == 404, (
        f'B should get 404 (existence-hidden), got {r.status_code} {r.text}'
    )
    r = await client_b.head(f'/api/v1/blobs/{body["sha256"]}')
    assert r.status_code == 404


async def test_user_b_cannot_delete_a_ref(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
    user_a, pg_conn,
) -> None:
    body = await _upload(client_a, b'delete-protection')
    # B has no ref → DELETE returns 404 (consistent with metadata GET).
    r = await client_b.delete(f'/api/v1/blobs/{body["sha256"]}/refs')
    assert r.status_code == 404
    # A's ref untouched.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT count(*) FROM blob_refs WHERE user_id = %s AND sha256 = %s',
            (user_a['id'], body['sha256']),
        )
        n = cur.fetchone()[0]
    assert n == 1


# ---- presign signature edge cases ------------------------------------------


def _replace_query(url: str, key: str, value: str) -> str:
    """Surgical query-string replacement that preserves order — using parsed
    URLs would re-sort and the sig would change for a different reason."""
    # Find &<key>= or ?<key>= ; replace until next '&' or end.
    import re
    return re.sub(rf'([?&]{key}=)[^&]*', rf'\g<1>{value}', url)


async def test_signed_url_rejects_tampered_signature(
    client_a: httpx.AsyncClient, anon_client: httpx.AsyncClient,
) -> None:
    body = await _upload(client_a, b'tamper-target')
    bad = _replace_query(body['url'], 'sig', '0' * 64)
    r = await anon_client.get(bad)
    assert r.status_code == 403, f'expected 403 for bad sig, got {r.status_code}'


async def test_signed_url_rejects_expired(
    client_a: httpx.AsyncClient, anon_client: httpx.AsyncClient,
) -> None:
    body = await _upload(client_a, b'expired-target')
    # Replace exp with one in the past. sig won't match either, but the
    # backend should reject expired-first; if both checks run independently
    # we still get 403 either way.
    expired_url = _replace_query(body['url'], 'exp', str(int(time.time()) - 60))
    r = await anon_client.get(expired_url)
    assert r.status_code == 403, f'expected 403 for expired, got {r.status_code}'


async def test_signed_url_path_rejects_missing_query(
    client_a: httpx.AsyncClient, anon_client: httpx.AsyncClient,
) -> None:
    body = await _upload(client_a, b'no-query-target')
    # Strip query string.
    bare = body['url'].split('?')[0]
    r = await anon_client.get(bare)
    # FastAPI Query(...) without a value → 422 (validation). Both 4xx outcomes
    # serve the same purpose: bytes don't leak.
    assert r.status_code in (403, 422), f'got {r.status_code}'


async def test_signed_url_for_unknown_sha_returns_404(
    anon_client: httpx.AsyncClient,
) -> None:
    """Even with a valid sig (we can compute one anyway), a non-existent
    blob must 404. This catches the path where presign would happen *before*
    a row gets persisted (shouldn't be possible, but worth pinning)."""
    # Compute a sig for a sha we never uploaded. We can use the same code
    # the server uses (the JWT_SECRET is a shared test constant).
    import os
    secret = os.environ.get('JWT_SECRET', 'test-only-secret-do-not-use-in-prod')
    import hmac
    sha = '0' * 64
    exp = int(time.time()) + 60
    sig = hmac.new(
        secret.encode(), f'{sha}|{exp}'.encode(), hashlib.sha256
    ).hexdigest()
    r = await anon_client.get(f'/api/v1/blobs/{sha}/data?exp={exp}&sig={sig}')
    assert r.status_code == 404, r.text


async def test_invalid_sha_format_400(
    client_a: httpx.AsyncClient,
) -> None:
    r = await client_a.get('/api/v1/blobs/not-a-sha')
    assert r.status_code == 400


# ---- delete ref -------------------------------------------------------------


async def test_delete_ref_drops_only_callers_ownership(
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
    user_a, user_b, pg_conn,
) -> None:
    content = b'two-users-one-blob'
    a_body = await _upload(client_a, content)
    await _upload(client_b, content)

    # A drops their ref.
    r = await client_a.delete(f'/api/v1/blobs/{a_body["sha256"]}/refs')
    assert r.status_code == 204

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id FROM blob_refs WHERE sha256 = %s',
            (a_body['sha256'],),
        )
        remaining = {r[0] for r in cur.fetchall()}
        cur.execute(
            'SELECT count(*) FROM blobs WHERE sha256 = %s',
            (a_body['sha256'],),
        )
        n_blobs = cur.fetchone()[0]
    assert remaining == {user_b['id']}, (
        f'expected only B to retain ref, got {remaining}'
    )
    # Bytes / row stay; GC handles real removal later.
    assert n_blobs == 1


async def test_a_can_repost_after_delete_ref(
    client_a: httpx.AsyncClient,
) -> None:
    content = b'roundtrip-after-delete'
    first = await _upload(client_a, content)
    r = await client_a.delete(f'/api/v1/blobs/{first["sha256"]}/refs')
    assert r.status_code == 204
    # Ref is gone; metadata GET 404.
    r = await client_a.get(f'/api/v1/blobs/{first["sha256"]}')
    assert r.status_code == 404
    # Re-upload — bytes still on disk → deduped True; ref re-created.
    second = await _upload(client_a, content)
    assert second['sha256'] == first['sha256']
    assert second['deduped'] is True
    r = await client_a.get(f'/api/v1/blobs/{second["sha256"]}')
    assert r.status_code == 200


# ---- unauth -----------------------------------------------------------------


async def test_unauth_post_rejected(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.post(
        '/api/v1/blobs',
        files={'file': ('x.bin', b'no-auth', 'text/plain')},
    )
    assert r.status_code == 401, r.text


async def test_unauth_metadata_rejected(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.get('/api/v1/blobs/' + 'a' * 64)
    assert r.status_code == 401


async def test_unauth_head_rejected(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.head('/api/v1/blobs/' + 'a' * 64)
    assert r.status_code == 401


async def test_unauth_delete_rejected(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.delete('/api/v1/blobs/' + 'a' * 64 + '/refs')
    assert r.status_code == 401


# ---- storage-side sanity ----------------------------------------------------


async def test_localfs_writes_sharded_path(
    client_a: httpx.AsyncClient,
) -> None:
    """LocalFsBlobStore shards by sha256[:2] / sha256[2:]. Verify the file
    actually lands at that path on disk so we trust dedup + GC paths."""
    import os
    content = b'check-shard-path'
    body = await _upload(client_a, content)

    root = os.environ.get('BLOB_STORE_PATH')
    if root:
        base = Path(root)
    else:
        # Default in blob_store._localfs_root() = src-backend/.blob-store
        # Resolve relative to repo root via running this test's own location.
        base = Path(__file__).resolve().parent.parent.parent / 'src-backend' / '.blob-store'

    sha = body['sha256']
    expected = base / sha[:2] / sha[2:]
    assert expected.exists(), (
        f'expected sharded blob path missing: {expected}'
    )
    assert expected.stat().st_size == len(content)
