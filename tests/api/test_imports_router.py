"""Stage 4.5 / Step 2 — multipart upload 5 endpoints + LocalFs _internal PUT shim.

Maps the plan's Stage 4.5 / Step 2「通过判据」段 (plan line 1105-1113) to
concrete pytest cases.

Backend at port 9011 is started by `tests/scripts/backend-start.sh` with
IMPORT_JOB_ENABLED=true + JWT_SECRET=test-only-secret-do-not-use-in-prod
(the same secret signs the multipart part HMAC).

Endpoints under test:

  POST   /api/v1/import/jobs                              create job + multipart
  POST   /api/v1/import/jobs/{id}/parts/{n}               mint presigned PUT URL
  POST   /api/v1/import/jobs/{id}/complete                finalize → queued
  GET    /api/v1/import/jobs/{id}                         status snapshot
  GET    /api/v1/import/jobs?status=active                user's active job
  DELETE /api/v1/import/jobs/{id}                         abort + cancelled

  PUT    /api/v1/_internal/multipart/{up_id}/part/{n}     LocalFs receive bytes

Cross-user policy: every {job_id} endpoint 404-masks on non-owner access
(never reveal another tenant's job existence).

Active-job uniqueness: partial unique index `uq_import_jobs_active_per_user`
permits at most one non-terminal job per user. Second create → 409 + current
active job's snapshot.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg
import pytest


# ---- helpers ----------------------------------------------------------------


async def _create_job(
    client: httpx.AsyncClient, *, file_size: int = 1024,
) -> dict:
    r = await client.post(
        '/api/v1/import/jobs', json={'file_size': file_size},
    )
    assert r.status_code == 201, f'create failed: {r.status_code} {r.text}'
    return r.json()


async def _get_part_url(
    client: httpx.AsyncClient, job_id: str, part_number: int,
) -> dict:
    r = await client.post(
        f'/api/v1/import/jobs/{job_id}/parts/{part_number}',
    )
    assert r.status_code == 200, f'part url failed: {r.status_code} {r.text}'
    return r.json()


async def _put_part(
    upload_url: str, body: bytes,
) -> dict:
    """PUT raw bytes to the presigned URL. Returns server response JSON."""
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.put(upload_url, content=body)
        assert r.status_code == 200, (
            f'part PUT failed: {r.status_code} {r.text}'
        )
        return r.json()


# ---- 1. create_job_returns_job_id_and_multipart_upload_id ------------------


async def test_create_job_returns_job_id_and_multipart_upload_id(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    body = await _create_job(client_a, file_size=1024)

    # job_id is a UUID (string form)
    import uuid as _uuid
    parsed = _uuid.UUID(body['job_id'])
    assert str(parsed) == body['job_id']

    assert isinstance(body['multipart_upload_id'], str)
    assert len(body['multipart_upload_id']) > 0

    expected_key = f'imports/{user_a["id"]}/{body["job_id"]}.json'
    assert body['raw_object_key'] == expected_key, (
        f'raw_object_key mismatch: {body}'
    )

    # Snapshot via GET should report status='uploading'
    r = await client_a.get(f'/api/v1/import/jobs/{body["job_id"]}')
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap['status'] == 'uploading', snap


# ---- 2. part_url_is_presigned_with_short_ttl -------------------------------


async def test_part_url_is_presigned_with_short_ttl(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    job = await _create_job(client_a, file_size=4096)
    before = int(time.time())
    part = await _get_part_url(client_a, job['job_id'], 1)
    after = int(time.time())

    assert part['part_number'] == 1, part

    parsed = urlparse(part['upload_url'])
    qs = parse_qs(parsed.query)
    assert 'exp' in qs, f'missing exp in upload_url: {part["upload_url"]}'
    assert 'sig' in qs, f'missing sig in upload_url: {part["upload_url"]}'

    expected_path = (
        f'/api/v1/_internal/multipart/{job["multipart_upload_id"]}/part/1'
    )
    assert parsed.path == expected_path, (
        f'unexpected path: {parsed.path!r} != {expected_path!r}'
    )

    sig = qs['sig'][0]
    assert len(sig) == 64, f'sig should be 64 hex chars, got {len(sig)}: {sig}'
    assert all(c in '0123456789abcdef' for c in sig), (
        f'sig should be lowercase hex: {sig}'
    )

    # expires_at should be ~3600s from now (BLOB_PRESIGN_TTL_SECONDS=3600
    # default) within the request window plus generous slack.
    expires_at = part['expires_at']
    assert before + 3600 - 10 <= expires_at <= after + 3600 + 10, (
        f'expires_at out of expected window: '
        f'before+3600={before+3600} expires_at={expires_at} after+3600={after+3600}'
    )

    # exp in URL should equal expires_at in body (consistent contract).
    assert int(qs['exp'][0]) == expires_at


# ---- 3. complete_with_all_parts_triggers_worker ----------------------------


async def test_complete_with_all_parts_triggers_worker(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """End-to-end: create → 2 part PUTs → complete → worker drains queue."""
    # Build a tiny but valid dexie export so Phase A succeeds quickly. Split
    # into 2 contiguous parts.
    import json
    payload = {
        'formatName': 'dexie',
        'formatVersion': 1,
        'data': {
            'databaseName': 'aiaw',
            'databaseVersion': 1,
            'tables': [
                {'name': 'workspaces', 'schema': '++id', 'rowCount': 1},
            ],
            'data': [
                {
                    'tableName': 'workspaces',
                    'inbound': True,
                    'rows': [{'id': 'ws-1', 'name': 'x'}],
                },
            ],
        },
    }
    raw = json.dumps(payload).encode('utf-8')
    # Force at least 2 parts: split in half.
    half = len(raw) // 2
    chunks = [raw[:half], raw[half:]]
    assert len(chunks[0]) > 0 and len(chunks[1]) > 0

    job = await _create_job(client_a, file_size=len(raw))

    etags: list[dict] = []
    for n, chunk in enumerate(chunks, start=1):
        part = await _get_part_url(client_a, job['job_id'], n)
        resp = await _put_part(part['upload_url'], chunk)
        assert resp['part_number'] == n, resp
        assert resp['size'] == len(chunk), resp
        assert isinstance(resp['etag'], str) and len(resp['etag']) == 64
        etags.append({'part_number': n, 'etag': resp['etag']})

    # Complete
    r = await client_a.post(
        f'/api/v1/import/jobs/{job["job_id"]}/complete',
        json={'parts': etags},
    )
    assert r.status_code == 200, f'complete failed: {r.status_code} {r.text}'
    snap = r.json()
    assert snap['status'] == 'queued', f'expected queued just after complete: {snap}'

    # Worker should pick this up within ~2s (poll=1s + sub-second Phase A on
    # this micro fixture). After that it'll either be in `parsing` /
    # `phase_b` (Step 1's terminal-for-Phase-A-only behavior) / `failed`.
    await asyncio.sleep(2.5)
    r2 = await client_a.get(f'/api/v1/import/jobs/{job["job_id"]}')
    assert r2.status_code == 200, r2.text
    later = r2.json()
    # Phase A succeeds → status='phase_b' (Step 1 leaves it there awaiting Step 3).
    assert later['status'] in ('phase_b', 'parsing'), (
        f'worker did not advance from queued; snap={later}'
    )


# ---- 4. complete_with_missing_parts_returns_400 ----------------------------


async def test_complete_with_missing_parts_returns_400(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    job = await _create_job(client_a, file_size=1024)

    # 4-1: missing part 2 (gap in the middle)
    r1 = await client_a.post(
        f'/api/v1/import/jobs/{job["job_id"]}/complete',
        json={'parts': [
            {'part_number': 1, 'etag': 'a' * 64},
            {'part_number': 3, 'etag': 'b' * 64},
        ]},
    )
    assert r1.status_code == 400, f'expected 400, got: {r1.status_code} {r1.text}'
    assert 'contiguous' in r1.text.lower(), r1.text

    # 4-2: starts at 2 (missing 1)
    r2 = await client_a.post(
        f'/api/v1/import/jobs/{job["job_id"]}/complete',
        json={'parts': [
            {'part_number': 2, 'etag': 'a' * 64},
            {'part_number': 3, 'etag': 'b' * 64},
        ]},
    )
    assert r2.status_code == 400, f'expected 400, got: {r2.status_code} {r2.text}'
    assert 'start at 1' in r2.text.lower(), r2.text

    # 4-3: duplicate part_number
    r3 = await client_a.post(
        f'/api/v1/import/jobs/{job["job_id"]}/complete',
        json={'parts': [
            {'part_number': 1, 'etag': 'a' * 64},
            {'part_number': 1, 'etag': 'b' * 64},
        ]},
    )
    assert r3.status_code == 400, f'expected 400, got: {r3.status_code} {r3.text}'
    assert 'duplicate' in r3.text.lower(), r3.text


# ---- 5. get_job_returns_status_and_progress --------------------------------


async def test_get_job_returns_status_and_progress(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    job = await _create_job(client_a, file_size=8192)
    r = await client_a.get(f'/api/v1/import/jobs/{job["job_id"]}')
    assert r.status_code == 200, r.text
    snap = r.json()

    expected_keys = {
        'job_id', 'status',
        'processed_rows', 'total_rows',
        'processed_blobs', 'total_blobs',
        'error_message', 'dead_letter',
        'created_at', 'updated_at',
    }
    missing = expected_keys - set(snap.keys())
    assert not missing, f'missing keys in snapshot: {missing}; full: {snap}'

    assert snap['status'] == 'uploading'
    assert snap['processed_rows'] == 0
    assert snap['processed_blobs'] == 0
    assert snap['error_message'] is None
    assert snap['dead_letter'] == []
    assert isinstance(snap['created_at'], str)
    assert isinstance(snap['updated_at'], str)


# ---- 6. get_active_jobs_returns_in_progress_only ---------------------------


async def test_get_active_jobs_returns_in_progress_only(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    job = await _create_job(client_a, file_size=1024)

    r1 = await client_a.get('/api/v1/import/jobs?status=active')
    assert r1.status_code == 200, r1.text
    arr1 = r1.json()
    assert isinstance(arr1, list), arr1
    assert len(arr1) == 1, f'expected 1 active job, got: {arr1}'
    assert arr1[0]['job_id'] == job['job_id']
    assert arr1[0]['status'] == 'uploading'

    # Cancel → list should be empty
    r2 = await client_a.delete(f'/api/v1/import/jobs/{job["job_id"]}')
    assert r2.status_code == 200, r2.text

    r3 = await client_a.get('/api/v1/import/jobs?status=active')
    assert r3.status_code == 200, r3.text
    assert r3.json() == [], r3.json()

    # Bad status filter → 400
    r4 = await client_a.get('/api/v1/import/jobs?status=foo')
    assert r4.status_code == 400, f'expected 400, got: {r4.status_code} {r4.text}'


# ---- 7. delete_active_job_aborts_multipart_and_releases_slot ---------------


async def test_delete_active_job_aborts_multipart_and_releases_slot(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    job1 = await _create_job(client_a, file_size=1024)

    # DELETE → status='cancelled'
    r1 = await client_a.delete(f'/api/v1/import/jobs/{job1["job_id"]}')
    assert r1.status_code == 200, r1.text
    snap = r1.json()
    assert snap['status'] == 'cancelled', snap

    # Slot must be released → second create immediately succeeds (no 409)
    job2 = await _create_job(client_a, file_size=2048)
    assert job2['job_id'] != job1['job_id']

    # And the first one is still visible & terminal via GET (soft cancel)
    r2 = await client_a.get(f'/api/v1/import/jobs/{job1["job_id"]}')
    assert r2.status_code == 200, r2.text
    assert r2.json()['status'] == 'cancelled'


# ---- 8. user_b_cannot_access_user_a_job_returns_404 ------------------------


async def test_user_b_cannot_access_user_a_job_returns_404(
    user_a, user_b,
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    job = await _create_job(client_a, file_size=1024)
    jid = job['job_id']

    # GET
    r1 = await client_b.get(f'/api/v1/import/jobs/{jid}')
    assert r1.status_code == 404, f'GET should 404, got: {r1.status_code} {r1.text}'
    assert 'not found' in r1.text.lower(), r1.text

    # POST parts (mint URL)
    r2 = await client_b.post(f'/api/v1/import/jobs/{jid}/parts/1')
    assert r2.status_code == 404, f'parts should 404, got: {r2.status_code} {r2.text}'

    # POST complete
    r3 = await client_b.post(
        f'/api/v1/import/jobs/{jid}/complete',
        json={'parts': [{'part_number': 1, 'etag': 'a' * 64}]},
    )
    assert r3.status_code == 404, f'complete should 404, got: {r3.status_code} {r3.text}'

    # DELETE
    r4 = await client_b.delete(f'/api/v1/import/jobs/{jid}')
    assert r4.status_code == 404, f'DELETE should 404, got: {r4.status_code} {r4.text}'


# ---- 9. part_endpoint_rejects_when_job_not_in_uploading_status -------------


async def test_part_endpoint_rejects_when_job_not_in_uploading_status(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    job = await _create_job(client_a, file_size=1024)

    # Force status to 'parsing' via direct SQL update (worker-driven path
    # would require a real upload + complete; this is a faster targeted test)
    with pg_conn.cursor() as cur:
        cur.execute(
            'UPDATE import_jobs SET status=%s WHERE id=%s',
            ('parsing', job['job_id']),
        )

    r = await client_a.post(f'/api/v1/import/jobs/{job["job_id"]}/parts/1')
    assert r.status_code == 409, f'expected 409, got: {r.status_code} {r.text}'
    assert 'not uploading' in r.text.lower(), r.text


# ---- 10. create_active_job_conflict_returns_existing_snapshot --------------


async def test_create_active_job_conflict_returns_existing_snapshot(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    first = await _create_job(client_a, file_size=1024)

    # Second create should 409 + return existing snapshot
    r = await client_a.post(
        '/api/v1/import/jobs', json={'file_size': 2048},
    )
    assert r.status_code == 409, f'expected 409, got: {r.status_code} {r.text}'
    body = r.json()
    detail = body.get('detail')
    assert isinstance(detail, dict), f'detail should be dict: {body}'
    assert 'job' in detail, f'detail.job missing: {detail}'
    assert detail['job']['job_id'] == first['job_id'], (
        f'snapshot mismatch: detail.job.job_id={detail["job"]["job_id"]} '
        f'!= first.job_id={first["job_id"]}'
    )
    assert detail['job']['status'] == 'uploading'

    # Cancel the first → next create should succeed (slot released)
    r2 = await client_a.delete(f'/api/v1/import/jobs/{first["job_id"]}')
    assert r2.status_code == 200, r2.text
    r3 = await client_a.post(
        '/api/v1/import/jobs', json={'file_size': 2048},
    )
    assert r3.status_code == 201, f'create-after-cancel failed: {r3.status_code} {r3.text}'


# ---- 11. internal_part_endpoint_rejects_bad_sig ----------------------------


async def test_internal_part_endpoint_rejects_bad_sig(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    job = await _create_job(client_a, file_size=1024)
    part = await _get_part_url(client_a, job['job_id'], 1)

    parsed = urlparse(part['upload_url'])
    qs = parse_qs(parsed.query)
    good_sig = qs['sig'][0]
    good_exp = int(qs['exp'][0])

    # Tamper the sig → 403 'bad signature'
    # Flip first hex char predictably (a→b, otherwise 0→1 etc.)
    tampered = ('b' if good_sig[0] == 'a' else 'a') + good_sig[1:]
    bad_url_sig = (
        f'http://127.0.0.1:9011{parsed.path}?exp={good_exp}&sig={tampered}'
    )
    async with httpx.AsyncClient(timeout=10.0) as c:
        r1 = await c.put(bad_url_sig, content=b'hello')
        assert r1.status_code == 403, (
            f'tampered sig: expected 403, got {r1.status_code}: {r1.text}'
        )
        assert 'bad signature' in r1.text.lower(), r1.text

    # Expired URL: exp in the past, but with a sig matching that past exp →
    # still 403 because we check exp before sig (and exp is in the past).
    # Use a sig that matches the past exp (we'd have to compute it; instead
    # just verify the expired-exp branch by setting exp=1 and any sig).
    expired_url = f'http://127.0.0.1:9011{parsed.path}?exp=1&sig={good_sig}'
    async with httpx.AsyncClient(timeout=10.0) as c:
        r2 = await c.put(expired_url, content=b'hello')
        assert r2.status_code == 403, (
            f'expired url: expected 403, got {r2.status_code}: {r2.text}'
        )
        assert 'expired' in r2.text.lower(), r2.text


# ---- 12. complete_dedupes_same_sha256_across_jobs --------------------------


async def test_complete_dedupes_same_sha256_across_jobs(
    user_a, user_b,
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
    pg_conn: psycopg.Connection,
) -> None:
    """Two jobs (different users so we can keep both active) upload the same
    bytes → second job's complete should land on the same canonical sha256
    storage path (LocalFs trust-the-hash dedup). The blob_store directory
    layout makes this easy to verify: there should be exactly one file at
    <root>/<sha[:2]>/<sha[2:]>.
    """
    import hashlib
    import json
    from pathlib import Path

    payload = {
        'formatName': 'dexie',
        'formatVersion': 1,
        'data': {
            'databaseName': 'aiaw',
            'databaseVersion': 1,
            'tables': [],
            'data': [],
        },
    }
    raw = json.dumps(payload).encode('utf-8')
    sha = hashlib.sha256(raw).hexdigest()

    async def _full_upload(client: httpx.AsyncClient) -> str:
        job = await _create_job(client, file_size=len(raw))
        part = await _get_part_url(client, job['job_id'], 1)
        resp = await _put_part(part['upload_url'], raw)
        r = await client.post(
            f'/api/v1/import/jobs/{job["job_id"]}/complete',
            json={'parts': [{'part_number': 1, 'etag': resp['etag']}]},
        )
        assert r.status_code == 200, f'complete failed: {r.status_code} {r.text}'
        return job['job_id']

    job1 = await _full_upload(client_a)
    job2 = await _full_upload(client_b)

    # The canonical blob path should hold the bytes exactly once.
    # _localfs_root() defaults to src-backend/.blob-store relative to module.
    import os as _os
    blob_root = (
        _os.environ.get('BLOB_STORE_PATH')
        or str(
            (Path(__file__).resolve().parent.parent.parent
             / 'src-backend' / '.blob-store').resolve()
        )
    )
    canonical = Path(blob_root) / sha[:2] / sha[2:]
    assert canonical.exists(), (
        f'canonical blob path missing after dedup test: {canonical}'
    )
    assert canonical.stat().st_size == len(raw), (
        f'canonical blob size mismatch: '
        f'{canonical.stat().st_size} != {len(raw)}'
    )

    # Both jobs should report the same raw_object_key (== sha256) on snapshot.
    r1 = await client_a.get(f'/api/v1/import/jobs/{job1}')
    r2 = await client_b.get(f'/api/v1/import/jobs/{job2}')
    snap1 = r1.json()
    snap2 = r2.json()
    assert snap1['raw_object_key'] == sha, snap1
    assert snap2['raw_object_key'] == sha, snap2
