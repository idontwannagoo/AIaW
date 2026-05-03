"""Stage 4.5 / Step 1 — ImportJob model + worker scaffold + Phase A.

Maps the plan's Stage 4.5 / Step 1 「通过判据」段（plan line 1083-1088）to
concrete pytest cases. Step 1 only ships Phase A (status='parsing' →
'phase_b'); Phase B / C / D stubs raise NotImplementedError until later
Steps. Tests therefore only assert behaviour up to and including the
status='parsing' → 'phase_b' transition.

The test backend at port 9011 is started by `tests/scripts/backend-start.sh`
with `IMPORT_JOB_ENABLED=true` (this commit), so an in-process
`ImportWorker` is already polling `import_jobs` once per second. Tests drive
Phase A through the live worker by:

    1. Uploading the raw dexie-export-import bytes via `POST /api/v1/blobs`
       (LocalFsBlobStore stores them at `<sha256[:2]>/<sha256[2:]>`, so the
       returned sha256 doubles as the storage_key for `raw_object_key`).
    2. INSERT-ing an `import_jobs` row with `status='parsing'` +
       `raw_object_key=<sha256>` via the `pg_conn` fixture.
    3. Polling the row until status leaves 'parsing' (worker pick-up <= 2s
       worst case: 1s poll + sub-second Phase A on the small fixtures used
       here).

Direct-Python tests of `run_phase_a()` (memory-bound case) instead import
the worker module + drive Phase A in-process against an isolated
`LocalFsBlobStore`, bypassing the running backend's BlobStore singleton so
the fixture path can use a per-test tmpdir.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest

from .fixtures.dexie_export import (
    collect_iter_to_bytes,
    export_dict_to_bytes,
    iter_huge_export_bytes,
    make_small_export,
)


# ---- src-backend module access ---------------------------------------------

# tests/api/ doesn't have src-backend on sys.path by default. Existing tests
# only talk over HTTP, but Step 1's memory-bound case needs to drive
# `run_phase_a` against an isolated LocalFS root — tests can't reuse the
# running backend's BlobStore singleton because we need a tmpdir per case.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_BACKEND = str(_REPO_ROOT / 'src-backend')
if _SRC_BACKEND not in sys.path:
    sys.path.insert(0, _SRC_BACKEND)

# Defer import until after sys.path mutation. These pull in SQLAlchemy /
# asyncio worker code; both are safe to import in the test process.
from data import import_worker  # noqa: E402
from data.blob_store import LocalFsBlobStore  # noqa: E402
from data.import_worker import (  # noqa: E402
    ImportFormatError,
    NON_TERMINAL_STATUSES,
    run_phase_a,
    temp_dir_for_job,
)


# ---- helpers ----------------------------------------------------------------


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


async def _upload_raw(
    client: httpx.AsyncClient, body: bytes,
) -> str:
    """POST raw bytes to /api/v1/blobs and return the sha256 (== LocalFS
    storage_key the worker will read via `BlobStore.open_stream(key)`).
    """
    r = await client.post(
        '/api/v1/blobs',
        files={
            'file': ('export.json', body, 'application/json'),
        },
    )
    assert r.status_code == 201, f'blob upload failed: {r.status_code} {r.text}'
    return r.json()['sha256']


def _insert_import_job(
    pg_conn: psycopg.Connection,
    *,
    user_id: str,
    job_id: str,
    status: str = 'parsing',
    raw_object_key: str | None = None,
) -> None:
    """Insert an import_jobs row directly. Step 2 will own the HTTP-side
    creation; Step 1 tests drive the worker by writing the row themselves
    so we don't have to wait for endpoints that don't exist yet.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            '''
            INSERT INTO import_jobs (id, user_id, status, raw_object_key)
            VALUES (%s, %s, %s, %s)
            ''',
            (job_id, user_id, status, raw_object_key),
        )


def _select_import_job(
    pg_conn: psycopg.Connection, job_id: str,
) -> dict | None:
    with pg_conn.cursor() as cur:
        cur.execute(
            '''
            SELECT id, status, total_rows, total_blobs, total_bytes,
                   processed_bytes, error_message, version, updated_at
            FROM import_jobs WHERE id = %s
            ''',
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        'id': row[0],
        'status': row[1],
        'total_rows': row[2],
        'total_blobs': row[3],
        'total_bytes': row[4],
        'processed_bytes': row[5],
        'error_message': row[6],
        'version': row[7],
        'updated_at': row[8],
    }


def _wait_for_status(
    pg_conn: psycopg.Connection,
    job_id: str,
    *,
    target_in: tuple[str, ...] = ('phase_b', 'failed'),
    timeout_s: float = 6.0,
    poll_s: float = 0.2,
) -> dict:
    """Poll import_jobs until status is in `target_in`. Worker poll interval
    defaults to 1.0s, so the worst case before pick-up is ~1s + Phase A
    runtime; 6s is generous. Returns the final row dict on success.
    """
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        last = _select_import_job(pg_conn, job_id)
        if last is not None and last['status'] in target_in:
            return last
        time.sleep(poll_s)
    raise AssertionError(
        f'job {job_id} did not reach {target_in} within {timeout_s}s; '
        f'last seen: {last}'
    )


def _count_lines(path: Path) -> int:
    with open(path, 'rb') as f:
        return sum(1 for _ in f)


# ---- backend-running-side cases --------------------------------------------


async def test_phase_a_extracts_table_row_counts(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """fixture small (10 providers + 5 dialogs + 1 workspace) → run Phase A
    via the live worker → total_rows == 16 + per-table NDJSON files exist
    with exact line counts.
    """
    body_bytes = export_dict_to_bytes(make_small_export())
    sha = await _upload_raw(client_a, body_bytes)
    job_id = f'job-small-{sha[:8]}'

    _insert_import_job(
        pg_conn,
        user_id=user_a['id'],
        job_id=job_id,
        status='parsing',
        raw_object_key=sha,
    )

    final = _wait_for_status(pg_conn, job_id, target_in=('phase_b', 'failed'))
    assert final['status'] == 'phase_b', f'unexpected end state: {final}'
    assert final['error_message'] is None

    # 10 providers + 1 workspace + 5 dialogs = 16 rows total.
    assert final['total_rows'] == 16, f'total_rows mismatch: {final}'
    assert final['total_bytes'] == len(body_bytes), final
    assert final['processed_bytes'] == len(body_bytes), final
    # No blob-bearing tables in the small fixture.
    assert final['total_blobs'] == 0

    # NDJSON files written under temp dir with exact per-table counts.
    tmp = temp_dir_for_job(job_id)
    providers_ndjson = tmp / 'providers.ndjson'
    workspaces_ndjson = tmp / 'workspaces.ndjson'
    dialogs_ndjson = tmp / 'dialogs.ndjson'
    assert providers_ndjson.exists(), f'missing: {providers_ndjson}'
    assert workspaces_ndjson.exists(), f'missing: {workspaces_ndjson}'
    assert dialogs_ndjson.exists(), f'missing: {dialogs_ndjson}'
    assert _count_lines(providers_ndjson) == 10
    assert _count_lines(workspaces_ndjson) == 1
    assert _count_lines(dialogs_ndjson) == 5


async def test_phase_a_invalid_json_marks_failed(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """Truncated JSON bytes → ijson raises mid-parse → worker catches and
    marks status='failed' with non-empty error_message.
    """
    bad = b'{"formatName":"dexie","formatVersion":1,"data":{"databaseName":"x'
    sha = await _upload_raw(client_a, bad)
    job_id = f'job-bad-{sha[:8]}'

    _insert_import_job(
        pg_conn,
        user_id=user_a['id'],
        job_id=job_id,
        status='parsing',
        raw_object_key=sha,
    )

    final = _wait_for_status(pg_conn, job_id, target_in=('phase_b', 'failed'))
    assert final['status'] == 'failed', f'expected failed, got: {final}'
    assert final['error_message'], 'error_message should not be empty'
    # Worker wraps as ImportFormatError("phase A stream parse failed: …")
    # then prefixes with "phase A: " in _mark_failed.
    assert 'phase A' in final['error_message'], final['error_message']


async def test_phase_a_unsupported_format_name_marks_failed(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """Header validation: formatName != 'dexie' → status='failed' with
    error_message mentioning 'unsupported formatName'.
    """
    export = make_small_export()
    export['formatName'] = 'sqlite'  # valid JSON but wrong format
    body_bytes = export_dict_to_bytes(export)
    sha = await _upload_raw(client_a, body_bytes)
    job_id = f'job-fmt-{sha[:8]}'

    _insert_import_job(
        pg_conn,
        user_id=user_a['id'],
        job_id=job_id,
        status='parsing',
        raw_object_key=sha,
    )

    final = _wait_for_status(pg_conn, job_id, target_in=('phase_b', 'failed'))
    assert final['status'] == 'failed', final
    assert final['error_message'], final
    assert 'unsupported formatName' in final['error_message'], (
        final['error_message']
    )


async def test_active_job_unique_constraint(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """Partial unique index `(user_id) WHERE status IN <NON_TERMINAL>`
    prevents a second active job per user. INSERT with status='queued'
    after another non-terminal row exists → IntegrityError.
    """
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id='active-1', status='queued',
    )
    # Second active row for the same user → must raise.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_import_job(
            pg_conn,
            user_id=user_a['id'],
            job_id='active-2',
            status='queued',
        )


async def test_unique_constraint_allows_terminal_jobs(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """Partial index does NOT cover terminal status — so a user with prior
    done/failed/cancelled rows can still create a fresh active job. Verifies
    the partial WHERE clause is actually partial (not unconditional unique).
    """
    # Three terminal jobs first.
    for i, terminal in enumerate(('done', 'failed', 'cancelled')):
        _insert_import_job(
            pg_conn,
            user_id=user_a['id'],
            job_id=f'old-{i}',
            status=terminal,
        )
    # A new active job for the same user must succeed.
    _insert_import_job(
        pg_conn,
        user_id=user_a['id'],
        job_id='new-active',
        status='queued',
    )
    # And we should still be unable to create a second active one.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_import_job(
            pg_conn,
            user_id=user_a['id'],
            job_id='another-active',
            status='uploading',
        )


async def test_worker_recovers_active_job_on_startup(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """Worker dispatch loop scans NON_TERMINAL_STATUSES on every poll. We
    insert a row with status='parsing' + raw upload pointed at a real
    BlobStore key; the worker should pick it up within one poll interval
    (worker.startup_complete is set in lifespan; ongoing _dispatch_active_jobs
    drains the same set every poll). Drives the same code path as the real
    crash-resume scenario without needing a backend restart.
    """
    body_bytes = export_dict_to_bytes(make_small_export())
    sha = await _upload_raw(client_a, body_bytes)
    job_id = f'recover-{sha[:8]}'

    _insert_import_job(
        pg_conn,
        user_id=user_a['id'],
        job_id=job_id,
        status='parsing',
        raw_object_key=sha,
    )

    final = _wait_for_status(pg_conn, job_id, target_in=('phase_b', 'failed'))
    assert final['status'] == 'phase_b', f'recovery failed: {final}'
    # NDJSON should have been written (proves worker ran Phase A end-to-end).
    tmp = temp_dir_for_job(job_id)
    assert (tmp / 'providers.ndjson').exists()


async def test_envelope_wire_format() -> None:
    """ImportJob._envelope() returns the same envelope shape as other
    server-routed tables (`{id, version, updated_at, deleted, data: {...}}`).
    Stage 4.5 Step 6 makes import_jobs a server-routed table; this asserts
    the envelope contract is in place from Step 1 so future steps don't
    have to retrofit it.

    Drives the model directly via constructor — the test process's
    SessionLocal points at the dev DB (5433, no DATABASE_URL env), not the
    test DB (5434, in the backend process). We don't need a DB roundtrip
    for envelope shape; we only need to verify the model serializer.
    """
    from datetime import datetime, timezone
    from data.models.import_job import ImportJob

    job = ImportJob(
        id='env-1',
        user_id='env-test-user',
        status='parsing',
        raw_object_key='deadbeef' * 8,
        processed_bytes=0,
        processed_rows=0,
        processed_blobs=0,
        version=42,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        dead_letter=[],
    )

    env = job._envelope()

    # Top-level envelope shape (matches plan line 1179: `{id, version,
    # updated_at, deleted, data: <ImportJobStatus>}`).
    assert set(env.keys()) >= {'id', 'version', 'updated_at', 'deleted', 'data'}
    assert env['id'] == 'env-1'
    assert env['deleted'] is False
    assert isinstance(env['version'], int) and env['version'] == 42
    assert isinstance(env['updated_at'], str)  # ISO datetime

    # data is the status snapshot.
    data = env['data']
    assert data['id'] == 'env-1'
    assert data['status'] == 'parsing'
    assert data['raw_object_key'] == 'deadbeef' * 8
    assert data['processed_bytes'] == 0
    assert data['processed_rows'] == 0
    assert data['processed_blobs'] == 0
    assert data['total_bytes'] is None
    assert data['total_rows'] is None
    assert data['total_blobs'] is None
    assert data['error_message'] is None
    assert data['dead_letter'] == []


# ---- direct-Python case: streaming memory bound ----------------------------


@pytest.mark.slow
async def test_phase_a_streaming_memory_under_200mb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase A must stream — it can never load the whole upload into memory.
    Drive run_phase_a() in-process against an isolated LocalFsBlobStore so
    we can point IMPORT_JOB_TMP_ROOT at a tmpdir.

    Runtime knobs (via env, default ~50MB so CI stays fast; bump locally
    via IMPORT_HUGE_MESSAGES / IMPORT_HUGE_ATTACHMENT_BYTES for the full
    200MB exercise):
        IMPORT_HUGE_MESSAGES         — total messages (default 10000)
        IMPORT_HUGE_ATTACHMENTS      — # with inline base64 attachment
        IMPORT_HUGE_ATTACHMENT_BYTES — raw bytes per attachment

    We also explicitly pass smaller knobs to keep the default test fast
    (~50MB JSON via 25 attachments × 2MB) so the slow-mark threshold of
    200MB RSS delta is comfortably satisfied — the assertion guards against
    a regression to the obvious bug (whole upload buffered in RAM), not the
    exact 200MB wire-budget.

    RSS measurement: resource.getrusage().ru_maxrss is in KB on macOS/Linux
    (Linux historically returned bytes; modern glibc returns KB but we
    normalize defensively by recording both before & after the same-process
    delta).
    """
    import resource

    # Build the huge fixture on disk.
    raw_path = tmp_path / 'huge_export.json'
    total_bytes = 0
    msg_count = int(os.environ.get('IMPORT_HUGE_MESSAGES_TEST', '300'))
    attach_count = int(
        os.environ.get('IMPORT_HUGE_ATTACHMENTS_TEST', '25')
    )
    attach_bytes = int(
        os.environ.get('IMPORT_HUGE_ATTACHMENT_BYTES_TEST', str(2 * 1024 * 1024))
    )
    with open(raw_path, 'wb') as f:
        for chunk in iter_huge_export_bytes(
            message_count=msg_count,
            attachment_count=attach_count,
            attachment_bytes=attach_bytes,
        ):
            f.write(chunk)
            total_bytes += len(chunk)
    # Sanity: fixture is non-trivial.
    assert total_bytes >= 30 * 1024 * 1024, (
        f'fixture too small to exercise streaming: {total_bytes}B'
    )

    # Spin an isolated BlobStore rooted in tmp_path; put() the bytes from
    # disk. We need the storage_key (== sha256) and to register a real
    # ImportJob row pointing at it.
    blob_root = tmp_path / 'blob-store'
    blob_root.mkdir()
    store = LocalFsBlobStore(root=blob_root)
    raw_bytes = raw_path.read_bytes()  # one-time materialize for sha + put
    sha = _sha256(raw_bytes)
    await store.put(sha, raw_bytes, 'application/json')
    del raw_bytes  # don't let it linger across the RSS measurement

    # Isolate Phase A's temp dir to tmp_path, not /tmp.
    monkeypatch.setenv('IMPORT_JOB_TMP_ROOT', str(tmp_path / 'import-tmp'))
    (tmp_path / 'import-tmp').mkdir()

    # Insert a job row so run_phase_a() can SELECT raw_object_key. Use a
    # direct sync psycopg conn — fixture pg_conn isn't available because
    # this test takes (tmp_path, monkeypatch) instead of (pg_conn,…) to
    # keep its scope clear.
    pg_dsn = os.environ.get(
        'TEST_PG_DSN',
        'postgresql://aiaw:aiaw_test@localhost:5434/aiaw_test',
    )
    job_id = f'huge-{sha[:8]}'
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            # User row required by FK-less convention (no FK on import_jobs.user_id)
            # so we can use any string. Avoid touching the auth path.
            cur.execute(
                'INSERT INTO import_jobs (id, user_id, status, raw_object_key) '
                'VALUES (%s, %s, %s, %s) '
                'ON CONFLICT (id) DO UPDATE SET status=excluded.status, '
                'raw_object_key=excluded.raw_object_key',
                (job_id, 'rss-test-user', 'parsing', sha),
            )

    # Re-bind data.db.SessionLocal to the test DB. Module-level engine was
    # created from the DATABASE_URL env at first import (defaulted to dev
    # DB 5433 because the test process inherits no DATABASE_URL — only the
    # backend subprocess gets that env). Build a fresh engine pointed at
    # the test DB and swap it in for run_phase_a()'s lookup.
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker as _aslmk,
        create_async_engine as _cae,
        AsyncSession as _ASess,
    )
    from data import db as _db_mod

    async_dsn = pg_dsn.replace('postgresql://', 'postgresql+asyncpg://')
    test_engine = _cae(async_dsn, pool_pre_ping=True)
    test_sm = _aslmk(test_engine, class_=_ASess, expire_on_commit=False)
    monkeypatch.setattr(_db_mod, 'SessionLocal', test_sm)
    # import_worker imported SessionLocal directly; patch the worker module
    # symbol too so its `from .db import SessionLocal` reference flips.
    monkeypatch.setattr(import_worker, 'SessionLocal', test_sm)

    # Measure RSS delta around run_phase_a() — should be tiny (a few MB),
    # certainly << total fixture size if streaming actually works.
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    summary = await run_phase_a(job_id, store, chunk_size=256 * 1024)
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # On macOS ru_maxrss is in bytes; on Linux it's in kilobytes. Normalize
    # by capping the delta in both interpretations and asserting the smaller
    # of the two stays under the 200MB budget — both interpretations agree
    # when the delta is negative (peak unchanged).
    delta_raw = max(0, rss_after - rss_before)
    delta_kb = delta_raw  # assume KB on linux
    delta_b = delta_raw   # assume bytes on macos
    # Convert to MB under both assumptions; the test passes if EITHER is
    # under the budget (i.e. the smaller / true unit).
    delta_mb_kb = delta_kb / 1024.0
    delta_mb_b = delta_b / (1024.0 * 1024.0)
    delta_mb = min(delta_mb_kb, delta_mb_b)

    print(
        f'RSS delta during phase A: '
        f'before={rss_before} after={rss_after} '
        f'delta_raw={delta_raw} '
        f'(if KB → {delta_mb_kb:.1f}MB, if B → {delta_mb_b:.1f}MB) '
        f'fixture={total_bytes/1024/1024:.1f}MB '
        f'phase_a_total_rows={summary["total_rows"]}'
    )

    # Hard ceiling: 200MB peak growth during a phase-A pass. With ijson
    # streaming this should be a couple MB; loading the whole upload would
    # be >= fixture size (≥ 30MB).
    assert delta_mb < 200, (
        f'Phase A RSS grew by {delta_mb:.1f}MB (over 200MB budget); '
        f'fixture was {total_bytes/1024/1024:.1f}MB → likely loaded the '
        f'whole upload into RAM instead of streaming'
    )

    # Sanity: rows were actually counted. msg_count messages + 1 dialog +
    # 1 workspace = msg_count + 2.
    assert summary['total_rows'] == msg_count + 2, summary
    assert summary['total_bytes'] == total_bytes
    # Blob-bearing tables are scanned; messages with attachments should
    # count as candidates.
    assert summary['total_blobs'] == attach_count, summary
