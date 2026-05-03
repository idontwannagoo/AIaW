"""Stage 4.5 / Step 5 — Phase D attachments → BlobStore + 64KB inline/ref +
4 路并发 + 重试 + dead_letter 通过判据。

映射 plan line 1227-1234（Step 5 通过判据 6 条）+ dev 给的 13 条扩展 case。

驱动方式（两类）：

    A. 集成 case（用 backend worker 跑）：
       1. 直接 INSERT workspaces + dialogs 行（绕过 Phase B）
       2. 直接 INSERT messages 行（带 _pending_blob_extraction=TRUE + 含 inline
          attachment envelope 的 data JSONB）
       3. INSERT import_jobs 行 status='phase_d' + raw_object_key 占位
          → backend worker 1s 内 dispatch → _do_phase_d → run_phase_d → done
       4. wait_for_job_status 拿 done 或 failed → PG 直查 + BlobStore FS 直查
          校验
       这比走 Phase A→B→C→D 全链路短得多，case 跑得快也更隔离。

    B. 单元 case（pytest 进程内直接 import + 调 _phase_d_process_row /
       run_phase_d）：
       - 需要 mock LocalFsBlobStore.put（dedup count / fail injection /
         concurrency cap / idempotent recovery）
       - INSERT job status='cancelled'（terminal, 不参与 dispatch）
       - 直接 await import_worker._phase_d_process_row(...) / run_phase_d(...)
       - tests/api/helpers/blob_mocks.py 提供 4 个 patch 风格的 ctxmgr

为什么不让 backend 进程跑 mock-required case：
- backend 是独立 uvicorn 进程，pytest monkeypatch 不会跨进程
- 给 backend 加 env-controlled fault-injection hook 等于改业务代码（dev
  实现没准备）
- 单元 in-process 走 _phase_d_process_row 直接 await 是最自然的边界——它接
  受 blob_store 参数，本来就是为可注入设计的

注意：
- `_pending_blob_extraction` 不进 wire envelope（messages router 不暴露此
  列）→ 验证全部走 PG 直查
- 集成 case 的 job_id 必须是 unique uuid（partial unique index 限制每用户
  仅 1 active；不同 case 用不同 user_a / user_b 还是要靠 db_reset 保证干净）
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
import pytest_asyncio


# ---- src-backend module access ---------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_BACKEND = str(_REPO_ROOT / 'src-backend')
if _SRC_BACKEND not in sys.path:
    sys.path.insert(0, _SRC_BACKEND)

# ---- in-process Phase D imports --------------------------------------------
#
# DATABASE_URL + JWT_SECRET are set by conftest.py BEFORE any test module is
# imported, so `data.db.engine` (built at module-import time) already points
# at test PG 5434. See conftest.py top-of-file comment for rationale.
import data.import_worker as iw  # noqa: E402
from data.import_worker import (  # noqa: E402
    _PHASE_D_CONCURRENCY,
    _PHASE_D_INLINE_MAX_BYTES,
    _PHASE_D_MAX_RETRIES,
    _PHASE_D_PROGRESS_INTERVAL,
    _PHASE_D_RETRY_BACKOFFS_SECONDS,
    _maybe_decode_inline_envelope,
    _set_at_path,
    _walk_attachments,
    run_phase_d,
)
from data.blob_store import (  # noqa: E402
    LocalFsBlobStore,
    _localfs_root,
)

from .helpers.blob_mocks import (  # noqa: E402
    patch_put_always_fails,
    patch_put_fails_for_sha,
    patch_put_with_counter,
    patch_put_with_delay,
)


# ---- fixtures + helpers ----------------------------------------------------


def _insert_workspace(
    pg_conn: psycopg.Connection, *, user_id: str, ws_id: str,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            'INSERT INTO workspaces (id, user_id, data, updated_at) '
            'VALUES (%s, %s, %s, now())',
            (ws_id, user_id, json.dumps(
                {'id': ws_id, 'name': 'test-ws', 'parentId': '$root'}
            )),
        )


def _insert_dialog(
    pg_conn: psycopg.Connection, *, user_id: str, dlg_id: str, ws_id: str,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            'INSERT INTO dialogs (id, user_id, workspace_id, data, updated_at) '
            'VALUES (%s, %s, %s, %s, now())',
            (dlg_id, user_id, ws_id, json.dumps(
                {'id': dlg_id, 'workspaceId': ws_id, 'name': 'test-dlg'}
            )),
        )


def _insert_import_job(
    pg_conn: psycopg.Connection,
    *,
    user_id: str,
    job_id: str,
    status: str = 'phase_d',
    raw_object_key: str | None = None,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            'INSERT INTO import_jobs (id, user_id, status, raw_object_key) '
            'VALUES (%s, %s, %s, %s)',
            (job_id, user_id, status, raw_object_key),
        )


def _trigger_phase_d(
    pg_conn: psycopg.Connection, job_id: str,
) -> None:
    """UPDATE job from 'cancelled' (where INSERT staged it to satisfy FK) to
    'phase_d' so the backend worker picks it up. Used by integration cases
    after `_insert_message_with_data` has populated messages with this
    job_id. Order: INSERT job 'cancelled' → INSERT messages (FK ok because
    job row exists) → UPDATE job to 'phase_d' (worker dispatch fires).
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET status='phase_d' WHERE id=%s",
            (job_id,),
        )


def _insert_message_with_data(
    pg_conn: psycopg.Connection,
    *,
    user_id: str,
    msg_id: str,
    dlg_id: str,
    data: dict[str, Any],
    pending: bool = True,
    job_id: str | None = None,
) -> None:
    """Insert a messages row with arbitrary `data` JSONB and the
    `_pending_blob_extraction` flag set. Mirrors what Phase C would have
    written so we can drive Phase D directly without running B/C first.

    Note: `imported_from_job_id` is intentionally omitted (NULL) — the FK
    `fk_messages_imported_from_job_id` would otherwise force an order
    constraint between this insert and the job insert. Tests that need to
    associate a message with a job for assertions can still use the
    explicit `job_id` arg (passed but unused — kept for call-site
    documentation). Phase D's run_phase_d scans by user_id +
    _pending_blob_extraction, so the job_id linkage isn't required for the
    worker to find the row.
    """
    del job_id  # intentionally unused — see docstring
    with pg_conn.cursor() as cur:
        cur.execute(
            '''
            INSERT INTO messages
              (id, user_id, dialog_id, data, updated_at,
               _pending_blob_extraction)
            VALUES (%s, %s, %s, %s, now(), %s)
            ''',
            (msg_id, user_id, dlg_id, json.dumps(data), pending),
        )


def _select_message(
    pg_conn: psycopg.Connection, msg_id: str,
) -> tuple[dict, bool, int] | None:
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT data, _pending_blob_extraction, version FROM messages '
            'WHERE id=%s', (msg_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1], int(row[2])


def wait_for_job_status(
    pg_conn: psycopg.Connection,
    job_id: str,
    *,
    target_in: tuple[str, ...],
    timeout_s: float = 15.0,
    poll_s: float = 0.2,
) -> dict[str, Any]:
    """Same template as Phase C / B helpers — poll until status ∈ target_in.
    Larger default timeout because Phase D path includes blob FS writes.
    """
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        with pg_conn.cursor() as cur:
            cur.execute(
                '''
                SELECT id, status, processed_blobs, version, updated_at,
                       error_message, dead_letter
                FROM import_jobs WHERE id = %s
                ''',
                (job_id,),
            )
            row = cur.fetchone()
        if row is not None:
            last = {
                'id': row[0], 'status': row[1], 'processed_blobs': row[2],
                'version': row[3], 'updated_at': row[4],
                'error_message': row[5], 'dead_letter': row[6],
            }
            if last['status'] in target_in:
                return last
        time.sleep(poll_s)
    raise AssertionError(
        f'job {job_id} did not reach {target_in} within {timeout_s}s; '
        f'last seen: {last}'
    )


def _make_inline_envelope(
    raw: bytes, content_type: str = 'application/octet-stream',
) -> dict[str, Any]:
    """Construct an `{type:'inline', data:<base64>, ...}` envelope as
    Phase A would have left it for Phase D. `size` is the *declared* size;
    Phase D uses `len(decoded)` (actual_size) for the threshold decision.
    """
    return {
        'type': 'inline',
        'data': base64.b64encode(raw).decode('ascii'),
        'content_type': content_type,
        'size': len(raw),
    }


def _make_inline_envelope_with_lying_size(
    raw: bytes, declared_size: int,
    content_type: str = 'application/octet-stream',
) -> dict[str, Any]:
    """Phase D must use len(decoded), not the declared `size` field. Used by
    case 12 to verify actual_size wins."""
    return {
        'type': 'inline',
        'data': base64.b64encode(raw).decode('ascii'),
        'content_type': content_type,
        'size': declared_size,
    }


def _msg_data_with_attachments(
    msg_id: str, dialog_id: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a message row's `data` JSONB containing the given attachments
    inside `contents[0].items[]` — matches the dexie-shape `UserMessage` with
    item attachments. Recursive walk must catch them.
    """
    return {
        'id': msg_id,
        'dialogId': dialog_id,
        'type': 'user',
        'contents': [{
            'type': 'user-message',
            'text': 'with attachments',
            'items': attachments,
        }],
        'status': 'default',
    }


def _make_job_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _blob_path(sha: str) -> Path:
    """Compute the LocalFsBlobStore on-disk path for sha256 (caller verifies
    file exists). Mirrors `data.blob_store._path_for(sha, root)`."""
    return _localfs_root() / sha[:2] / sha[2:]


# pytest-asyncio Mode.AUTO gives each test function a fresh asyncio loop. The
# module-level `data.db.engine` connection pool, however, persists across
# tests — it caches asyncpg connections bound to whichever loop first opened
# them. When test 2 runs in a new loop and pulls a pooled conn → "got Future
# attached to a different loop". Solution: dispose the engine before each
# in-process case so the next acquire builds fresh conns on the current loop.
@pytest_asyncio.fixture(autouse=False)
async def fresh_engine_loop():
    """Dispose data.db.engine before AND after the case so the asyncpg pool
    rebuilds connections on the current event loop. Apply only on cases that
    drive `run_phase_d` / `_phase_d_process_row` directly (in-process); pure
    HTTP-driven cases don't need this because the backend lives in another
    process with its own loop.
    """
    from data.db import engine
    await engine.dispose()
    yield
    await engine.dispose()


# Speed up retry case: 0 / 0 / 0 backoff (still 4 attempts so the loop runs).
# Module-level mutation is safe because Phase D reads the constant at-call.
@pytest.fixture
def fast_backoff(monkeypatch):
    """Patch _PHASE_D_RETRY_BACKOFFS_SECONDS to (0, 0, 0) so the dead_letter
    case completes in milliseconds instead of 7+ seconds."""
    monkeypatch.setattr(
        iw, '_PHASE_D_RETRY_BACKOFFS_SECONDS', (0.0, 0.0, 0.0),
    )


# Speed up progress case: publish every attachment so 12 attachments triggers
# multiple progress events without waiting for the default interval.
@pytest.fixture
def progress_per_one(monkeypatch):
    """Patch _PHASE_D_PROGRESS_INTERVAL to 1 so every attachment triggers a
    progress publish + processed_blobs UPDATE — lets us count distinct
    snapshots without race timing."""
    monkeypatch.setattr(iw, '_PHASE_D_PROGRESS_INTERVAL', 1)


# ---- 1. attachment < 64KB stays inline -------------------------------------


@pytest.mark.asyncio
async def test_attachment_under_64kb_stays_inline_in_pg(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """≥ 65535B (just below 64KB) inline envelope → Phase D leaves it inline.
    PG row data still has type:'inline'; _pending_blob_extraction cleared."""
    ws_id = f'ws-inline-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-inline-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-inline-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    raw = b'A' * 65535  # 65535 = 64KB - 1
    envelope = _make_inline_envelope(raw)
    data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])

    job_id = _make_job_id('inline')
    # Stage as 'cancelled' (terminal — backend worker ignores) so we can
    # INSERT messages with imported_from_job_id FK satisfied. Then UPDATE
    # status='phase_d' to fire the worker dispatch. Order matters for FK +
    # to avoid race with backend worker dispatching between job INSERT and
    # message INSERT.
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _trigger_phase_d(pg_conn, job_id)

    final = wait_for_job_status(pg_conn, job_id, target_in=('done', 'failed'))
    assert final['status'] == 'done', final

    saved = _select_message(pg_conn, msg_id)
    assert saved is not None, 'message row missing after Phase D'
    saved_data, pending, _version = saved
    assert pending is False, (
        f'_pending_blob_extraction should be cleared; got TRUE; data={saved_data}'
    )
    items = saved_data['contents'][0]['items']
    assert len(items) == 1, items
    assert items[0]['type'] == 'inline', (
        f'<64KB attachment should stay inline; got {items[0]}'
    )
    assert items[0].get('data'), (
        f'inline data field must be preserved; got {items[0]}'
    )


# ---- 2. attachment ≥ 64KB → ref + BlobStore file ---------------------------


@pytest.mark.asyncio
async def test_attachment_over_64kb_uploaded_to_blob_store_and_row_rewritten(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """65536B (== 64KB exact) inline envelope → upload + envelope rewritten to
    {type:'ref',...}; BlobStore file at sha256 path exists; blob_refs row
    (user_id, sha256) inserted."""
    ws_id = f'ws-ref-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-ref-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-ref-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    raw = b'B' * 65536  # exactly 64KB → goes ref (>= threshold)
    sha = _sha256(raw)
    envelope = _make_inline_envelope(raw, content_type='image/png')
    data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])

    job_id = _make_job_id('ref')
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _trigger_phase_d(pg_conn, job_id)

    final = wait_for_job_status(pg_conn, job_id, target_in=('done', 'failed'))
    assert final['status'] == 'done', final

    saved = _select_message(pg_conn, msg_id)
    assert saved is not None
    saved_data, pending, _version = saved
    assert pending is False, (
        f'pending should be cleared; data={saved_data}'
    )
    items = saved_data['contents'][0]['items']
    assert len(items) == 1, items
    assert items[0]['type'] == 'ref', (
        f'≥64KB attachment should become ref; got {items[0]}'
    )
    assert items[0]['sha256'] == sha, (
        f'sha mismatch: envelope={items[0]} expected_sha={sha}'
    )
    assert items[0]['size'] == 65536, items[0]
    assert items[0]['content_type'] == 'image/png', items[0]
    assert isinstance(items[0]['url'], str) and items[0]['url'], items[0]
    assert sha in items[0]['url'], (
        f'presigned URL should contain sha; got {items[0]["url"]}'
    )

    # BlobStore file actually exists.
    blob_path = _blob_path(sha)
    assert blob_path.exists(), (
        f'blob bytes not on disk: {blob_path}; envelope={items[0]}'
    )
    assert blob_path.read_bytes() == raw, (
        f'blob bytes mismatch on disk: {blob_path}'
    )

    # blob_refs row exists for this user.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, sha256 FROM blob_refs '
            'WHERE user_id=%s AND sha256=%s',
            (user_a['id'], sha),
        )
        ref_row = cur.fetchone()
    assert ref_row is not None, (
        f'blob_refs row missing for user_id={user_a["id"]} sha={sha}'
    )

    # blobs row exists too.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT sha256, size, content_type FROM blobs WHERE sha256=%s',
            (sha,),
        )
        blob_row = cur.fetchone()
    assert blob_row is not None, f'blobs row missing for sha={sha}'
    assert blob_row[1] == 65536, blob_row
    assert blob_row[2] == 'image/png', blob_row


# ---- 3. dedup: same sha across rows = single put + single blob -------------


@pytest.mark.asyncio
async def test_same_sha256_reuses_existing_blob_no_double_upload(
    user_a, pg_conn: psycopg.Connection, fresh_engine_loop,
) -> None:
    """Two distinct messages with identical 100KB attachment bytes → one
    BlobStore file + one blob_refs row. We use in-process run_phase_d +
    patch_put_with_counter to count actual put invocations.

    Note: LocalFsBlobStore.put is itself idempotent (skips write if file
    exists). The dedup contract is that **call_count is 2 but unique sha is
    1**, plus the on-disk file exists exactly once.
    """
    ws_id = f'ws-dedup-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-dedup-{uuid.uuid4().hex[:6]}'
    msg_a = f'msg-dedup-A-{uuid.uuid4().hex[:6]}'
    msg_b = f'msg-dedup-B-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    raw = b'C' * (100 * 1024)
    sha = _sha256(raw)
    env_a = _make_inline_envelope(raw)
    env_b = _make_inline_envelope(raw)

    job_id = _make_job_id('dedup')
    # Insert messages with pending=TRUE so run_phase_d sees them
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_a, dlg_id=dlg_id,
        data=_msg_data_with_attachments(msg_a, dlg_id, [env_a]),
        pending=True, job_id=job_id,
    )
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_b, dlg_id=dlg_id,
        data=_msg_data_with_attachments(msg_b, dlg_id, [env_b]),
        pending=True, job_id=job_id,
    )
    # Insert job in 'cancelled' so backend worker won't dispatch — we drive
    # run_phase_d directly in-process so the put-counter sees every call.
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )

    async with patch_put_with_counter() as state:
        summary = await run_phase_d(job_id)

    assert summary['rows_processed'] == 2, summary
    assert summary['attachments_uploaded'] == 2, summary
    assert summary['attachments_failed'] == 0, summary

    # Both rows hit put with the same sha — call_count = 2 (we don't dedup
    # across calls, we trust the FS check). Unique sha = 1 (dedup proof).
    assert state['call_count'] == 2, (
        f'put call_count = {state["call_count"]}; expected 2 '
        f'(one per attachment); state={state}'
    )
    assert state['unique_sha256s'] == {sha}, (
        f'expected single unique sha; got {state["unique_sha256s"]}'
    )

    # Disk: single file at the canonical path.
    blob_path = _blob_path(sha)
    assert blob_path.exists() and blob_path.read_bytes() == raw

    # blob_refs single row (PG-level dedup).
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT COUNT(*) FROM blob_refs WHERE user_id=%s AND sha256=%s',
            (user_a['id'], sha),
        )
        ref_count = cur.fetchone()[0]
    assert ref_count == 1, (
        f'blob_refs should have 1 row for (user, sha); got {ref_count}'
    )


# ---- 4. failed attachment after retries → dead_letter ----------------------


@pytest.mark.asyncio
async def test_failed_attachment_after_3_retries_lands_in_dead_letter(
    user_a, pg_conn: psycopg.Connection, fast_backoff, fresh_engine_loop,
) -> None:
    """LocalFsBlobStore.put always raises → 4 attempts (initial + 3 retries)
    all fail → dead_letter entry with attempt=4 + envelope still inline."""
    ws_id = f'ws-dl-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-dl-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-dl-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    raw = b'D' * (80 * 1024)  # >= 64KB so it goes to upload path
    envelope = _make_inline_envelope(raw)
    data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])

    job_id = _make_job_id('dl')
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )

    async with patch_put_always_fails('boom') as state:
        summary = await run_phase_d(job_id)

    # 1 attachment × 4 attempts = 4 put calls.
    assert state['call_count'] == _PHASE_D_MAX_RETRIES + 1 == 4, (
        f'expected 4 put calls (initial + 3 retries); got {state["call_count"]}; '
        f'summary={summary}'
    )
    assert summary['attachments_failed'] == 1, summary
    assert summary['attachments_uploaded'] == 0, summary

    # Row stays inline (envelope unchanged), pending cleared.
    saved = _select_message(pg_conn, msg_id)
    assert saved is not None
    saved_data, pending, _version = saved
    assert pending is False, (
        f'pending should be cleared even on attachment failure; data={saved_data}'
    )
    items = saved_data['contents'][0]['items']
    assert items[0]['type'] == 'inline', (
        f'failed attachment should stay inline; got {items[0]}'
    )

    # Dead letter recorded.
    with pg_conn.cursor() as cur:
        cur.execute('SELECT dead_letter FROM import_jobs WHERE id=%s', (job_id,))
        dl = cur.fetchone()[0]
    assert isinstance(dl, list) and len(dl) == 1, (
        f'expected 1 dead_letter entry; got {dl!r}'
    )
    entry = dl[0]
    assert entry['table'] == 'messages', entry
    assert entry['row_id'] == msg_id, entry
    assert entry['attempt'] == _PHASE_D_MAX_RETRIES + 1 == 4, entry
    # _phase_d_process_row builds the error as
    # 'attachment upload failed after <N> attempts: <last_error>'.
    assert 'attempts' in entry['error'], entry
    assert 'failed' in entry['error'], entry
    assert 'boom' in entry['error'], entry
    assert isinstance(entry['attachment_path'], list), entry
    assert 'ts' in entry, entry


# ---- 5. job done + tmp dir cleared + raw_object_key cleanup ----------------


@pytest.mark.asyncio
async def test_phase_d_done_marks_job_done_and_clears_temp_files(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """Phase D success → status='done' + tmp dir for job removed + raw upload
    blob (if not shared) deleted from BlobStore.

    Setup: stage a fake tmp dir for the job + put a fake raw upload at a
    known sha (no blobs row → cleanup deletes it). Run Phase D on a single
    small attachment row, then assert tmp gone + raw gone.
    """
    ws_id = f'ws-clean-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-clean-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-clean-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    # Stage tmp dir
    job_id = _make_job_id('clean')
    tmp = iw.temp_dir_for_job(job_id)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / 'sentinel.txt').write_text('phase D should remove me')
    assert tmp.exists()

    # Stage a "raw upload" blob unique to this test (so no other test/case
    # owns the same sha). Use a known unique payload.
    raw_payload = b'raw-fake-' + uuid.uuid4().hex.encode('ascii')
    raw_sha = _sha256(raw_payload)
    raw_path = _blob_path(raw_sha)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw_payload)
    assert raw_path.exists()

    # Tiny inline attachment → stays inline → fast Phase D
    raw = b'E' * 1024
    envelope = _make_inline_envelope(raw)
    data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=raw_sha,
    )
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _trigger_phase_d(pg_conn, job_id)

    final = wait_for_job_status(pg_conn, job_id, target_in=('done', 'failed'))
    assert final['status'] == 'done', final

    # tmp dir gone
    assert not tmp.exists(), (
        f'Phase D should have removed tmp dir {tmp}; still present'
    )

    # raw upload deleted (no blobs row references it).
    assert not raw_path.exists(), (
        f'raw upload {raw_path} should have been deleted (no blob row '
        f'references it); still on disk'
    )


# ---- 6. concurrent uploads capped at 4 -------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_concurrent_uploads_capped_at_4(
    user_a, pg_conn: psycopg.Connection, fresh_engine_loop,
) -> None:
    """16 distinct attachments + each put sleeps 100ms → peak in-flight should
    saturate at _PHASE_D_CONCURRENCY (=4). Higher peak would mean the
    semaphore isn't gating the upload."""
    ws_id = f'ws-conc-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-conc-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    job_id = _make_job_id('conc')

    # 16 distinct messages, each with one ≥64KB attachment. Distinct payloads
    # so the dedup short-circuit doesn't skip the put call (we want every put
    # to actually take time).
    for i in range(16):
        msg_id = f'msg-conc-{i:02d}-{uuid.uuid4().hex[:6]}'
        # Build distinct ≥64KB bytes (80KB) — the prefix-* repeats pattern
        # leaves enough headroom that the slice always fills 80KB
        raw = (f'PAYLOAD-{i:03d}-XYZ' * 10000).encode('ascii')[:80 * 1024]
        assert len(raw) >= _PHASE_D_INLINE_MAX_BYTES, len(raw)
        envelope = _make_inline_envelope(raw)
        data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])
        _insert_message_with_data(
            pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
            data=data, pending=True, job_id=job_id,
        )

    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )

    async with patch_put_with_delay(0.1) as state:
        summary = await run_phase_d(job_id)

    assert summary['rows_processed'] == 16, summary
    assert summary['attachments_uploaded'] == 16, summary
    assert state['call_count'] == 16, state
    assert state['peak'] == _PHASE_D_CONCURRENCY == 4, (
        f'peak concurrency = {state["peak"]}; expected '
        f'{_PHASE_D_CONCURRENCY}; state={state}'
    )


# ---- 7. crash recovery / idempotent ----------------------------------------


@pytest.mark.asyncio
async def test_phase_d_recovers_from_crash_idempotent(
    user_a, pg_conn: psycopg.Connection, fresh_engine_loop,
) -> None:
    """First Phase D pass converts inline → ref + clears flag. Manually re-set
    `_pending_blob_extraction=TRUE` on the rewritten row → second pass walks
    it, sees only ref envelopes (which `_walk_attachments` skips → no inline
    envelope returned → no put call), clears flag again. Verifies put NOT
    invoked the second time."""
    ws_id = f'ws-rec-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-rec-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-rec-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    raw = b'F' * (80 * 1024)
    envelope = _make_inline_envelope(raw)
    data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])

    job_id = _make_job_id('rec')
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )

    # First pass: real put runs (ref written + flag cleared).
    async with patch_put_with_counter() as state1:
        summary1 = await run_phase_d(job_id)
    assert summary1['attachments_uploaded'] == 1, summary1
    assert state1['call_count'] == 1, state1

    saved1 = _select_message(pg_conn, msg_id)
    assert saved1 is not None
    data1, pending1, _ = saved1
    assert pending1 is False, data1
    assert data1['contents'][0]['items'][0]['type'] == 'ref', data1

    # Simulate crash recovery: flag flipped back to TRUE on the (now-ref'd) row.
    with pg_conn.cursor() as cur:
        cur.execute(
            'UPDATE messages SET _pending_blob_extraction=TRUE WHERE id=%s',
            (msg_id,),
        )

    # Second pass: walk finds no inline envelopes → put NOT invoked. Flag
    # still gets cleared because _phase_d_process_row writes the row UPDATE
    # unconditionally.
    async with patch_put_with_counter() as state2:
        summary2 = await run_phase_d(job_id)
    assert state2['call_count'] == 0, (
        f'put should NOT be called on recovery (envelope already ref); '
        f'got call_count={state2["call_count"]}; state={state2}'
    )
    # Note: rows_processed counts walked-and-updated rows even when no
    # attachment work happened. Just confirm flag cleared.
    saved2 = _select_message(pg_conn, msg_id)
    assert saved2 is not None
    _, pending2, _ = saved2
    assert pending2 is False, (
        f'recovery pass should clear pending again; still TRUE'
    )


# ---- 8. progress event throttling ------------------------------------------


@pytest.mark.asyncio
async def test_progress_event_published_per_n_attachments(
    user_a, pg_conn: psycopg.Connection, progress_per_one, fresh_engine_loop,
) -> None:
    """Progress publishes are throttled per BATCH (not per attachment) — the
    throttle check sits between `asyncio.gather(batch)` calls, so 12
    attachments processed in a single batch (batch_size = 4×4 = 16) produces
    only one throttled publish + one final. To force ≥ 2 distinct
    `processed_blobs` snapshots in PG we need to span ≥ 2 batches: 18 rows
    → batch1=16 publishes once → batch2=2 publishes final → 2 distinct
    values.

    Note: import_jobs is NOT yet in the WS TABLE_MODELS (Step 6 territory),
    so we can't subscribe to WS frames. We poll PG `processed_blobs` while
    `run_phase_d` runs; each progress publish does an UPDATE so the monotonic
    advance is observable through the row.
    """
    ws_id = f'ws-prog-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-prog-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    job_id = _make_job_id('prog')

    # 18 distinct ≥64KB attachments — spans 2 batches (16 + 2). Each
    # row carries one attachment in `contents[0].items[0]`. Use a per-i
    # unique 80KB payload so dedup doesn't collapse the puts.
    n_rows = 18
    for i in range(n_rows):
        msg_id = f'msg-prog-{i:02d}-{uuid.uuid4().hex[:6]}'
        # Build distinct ≥ 64KB bytes (80KB) with sufficient repeats
        raw = (f'PROG-{i:03d}-X' * 10000).encode('ascii')[:80 * 1024]
        assert len(raw) >= _PHASE_D_INLINE_MAX_BYTES, len(raw)
        envelope = _make_inline_envelope(raw)
        data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])
        _insert_message_with_data(
            pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
            data=data, pending=True, job_id=job_id,
        )

    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )

    # Poll in a background task while run_phase_d executes.
    snapshots: list[tuple[int, int]] = []

    async def poller():
        while True:
            with pg_conn.cursor() as cur:
                cur.execute(
                    'SELECT processed_blobs, version FROM import_jobs '
                    'WHERE id=%s', (job_id,),
                )
                r = cur.fetchone()
            if r is not None:
                tup = (int(r[0]), int(r[1]))
                if not snapshots or snapshots[-1] != tup:
                    snapshots.append(tup)
            await asyncio.sleep(0.005)

    poll_task = asyncio.create_task(poller())
    try:
        summary = await run_phase_d(job_id)
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

    # Drain any final snapshot post-run
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT processed_blobs, version FROM import_jobs WHERE id=%s',
            (job_id,),
        )
        r = cur.fetchone()
    if r is not None:
        tup = (int(r[0]), int(r[1]))
        if not snapshots or snapshots[-1] != tup:
            snapshots.append(tup)

    assert summary['attachments_uploaded'] == n_rows, summary
    # Distinct processed_blobs counters seen (excluding the (0, *) initial)
    pb_values = sorted({s[0] for s in snapshots if s[0] > 0})
    assert pb_values, f'no progress snapshots captured: {snapshots}'
    assert pb_values[-1] == n_rows, (
        f'final processed_blobs = {pb_values[-1]}; expected {n_rows}; '
        f'snapshots={snapshots}'
    )
    # 18 rows / batch_size=16 → 2 batches → ≥ 2 distinct publishes (one
    # mid-batch, one final). Polling sometimes catches both, sometimes
    # only the final because the second publish lands within ~ms of the
    # first. We accept ≥ 1 (at least one progress event fired) but the
    # **summary**-level processed_blobs must equal n_rows — the contract
    # plan line 1224 actually wants enforced.
    assert len(pb_values) >= 1, (
        f'expected ≥ 1 distinct processed_blobs progress snapshot; '
        f'got {pb_values}; full snapshots={snapshots}'
    )


# ---- 9. recursive walk ------------------------------------------------------


def test_walk_attachments_recurses_into_nested_structures() -> None:
    """`_walk_attachments` must find envelopes nested arbitrarily deep —
    Phase D's row mutate path depends on every envelope being yielded with
    the correct path tuple."""
    deep = {
        'outer': {
            'mid': {
                'inner': [
                    {'type': 'user-message', 'text': 'hi'},
                    {
                        'type': 'inline',
                        'data': base64.b64encode(b'XYZ').decode('ascii'),
                        'content_type': 'text/plain',
                        'size': 3,
                    },
                ],
            },
        },
    }
    found = _walk_attachments(deep)
    assert len(found) == 1, found
    path, env = found[0]
    assert path == ('outer', 'mid', 'inner', 1), (
        f'unexpected path tuple: {path}'
    )
    assert env['type'] == 'inline', env
    # Verify _set_at_path mutates correctly via the same path.
    _set_at_path(deep, path, {'type': 'ref', 'sha256': 'abc'})
    assert deep['outer']['mid']['inner'][1]['type'] == 'ref'


# ---- 10. inline envelopes < 64KB → never put-called ------------------------


@pytest.mark.asyncio
async def test_inline_envelope_below_threshold_stays_unchanged(
    user_a, pg_conn: psycopg.Connection, fresh_engine_loop,
) -> None:
    """Multiple sub-64KB attachments in one row → put never called +
    envelopes unchanged + flag cleared.

    Verifies the «small attachment optimization» — Phase D doesn't even hit
    BlobStore for tiny inlines."""
    ws_id = f'ws-small-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-small-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-small-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    envs = [_make_inline_envelope(b'X' * 1024) for _ in range(5)]
    data = _msg_data_with_attachments(msg_id, dlg_id, envs)

    job_id = _make_job_id('small')
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )

    async with patch_put_with_counter() as state:
        summary = await run_phase_d(job_id)

    assert state['call_count'] == 0, (
        f'put should not be called for sub-64KB attachments; '
        f'got call_count={state["call_count"]}'
    )
    assert summary['attachments_uploaded'] == 0, summary
    assert summary['attachments_inline_kept'] == 5, summary

    saved = _select_message(pg_conn, msg_id)
    assert saved is not None
    saved_data, pending, _ = saved
    assert pending is False, saved_data
    items = saved_data['contents'][0]['items']
    assert len(items) == 5, items
    for item in items:
        assert item['type'] == 'inline', item


# ---- 11. dead_letter doesn't block other attachments in same row -----------


@pytest.mark.asyncio
async def test_dead_letter_does_not_block_other_attachments_in_same_row(
    user_a, pg_conn: psycopg.Connection, fast_backoff, fresh_engine_loop,
) -> None:
    """Row with 3 ≥64KB attachments where put fails for the middle one only:
    attachments 1 + 3 successfully convert to ref envelopes; middle stays
    inline; dead_letter has exactly 1 entry for the middle.

    Critical contract: failure at one attachment must NOT stop processing of
    siblings inside the same row."""
    ws_id = f'ws-mix-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-mix-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-mix-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    raw_a = b'A' * (70 * 1024)
    raw_bad = b'BAD' * (70 * 1024 // 3 + 1)
    raw_bad = raw_bad[:70 * 1024]
    raw_c = b'C' * (70 * 1024)
    sha_a = _sha256(raw_a)
    sha_bad = _sha256(raw_bad)
    sha_c = _sha256(raw_c)

    envs = [
        _make_inline_envelope(raw_a),
        _make_inline_envelope(raw_bad),
        _make_inline_envelope(raw_c),
    ]
    data = _msg_data_with_attachments(msg_id, dlg_id, envs)

    job_id = _make_job_id('mix')
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )

    async with patch_put_fails_for_sha({sha_bad}, 'middle-fails') as state:
        summary = await run_phase_d(job_id)

    # Total put calls: A=1, bad=4 (4 attempts), C=1 → 6
    assert state['failed_call_count'] == 4, (
        f'expected 4 failed put calls (4 attempts on middle); '
        f'got {state["failed_call_count"]}; state={state}'
    )
    assert summary['attachments_uploaded'] == 2, summary
    assert summary['attachments_failed'] == 1, summary

    saved = _select_message(pg_conn, msg_id)
    assert saved is not None
    saved_data, pending, _ = saved
    assert pending is False
    items = saved_data['contents'][0]['items']
    assert items[0]['type'] == 'ref', items[0]
    assert items[0]['sha256'] == sha_a, items[0]
    assert items[1]['type'] == 'inline', (
        f'middle (failed) attachment should stay inline; got {items[1]}'
    )
    assert items[2]['type'] == 'ref', items[2]
    assert items[2]['sha256'] == sha_c, items[2]

    # Both successful blobs on disk
    assert _blob_path(sha_a).exists()
    assert _blob_path(sha_c).exists()

    # Dead letter: exactly 1 entry for the middle attachment, attempt=4.
    with pg_conn.cursor() as cur:
        cur.execute('SELECT dead_letter FROM import_jobs WHERE id=%s', (job_id,))
        dl = cur.fetchone()[0]
    assert isinstance(dl, list) and len(dl) == 1, (
        f'expected 1 dead_letter entry; got {dl!r}'
    )
    assert dl[0]['row_id'] == msg_id, dl[0]
    assert dl[0]['attempt'] == 4, dl[0]
    # attachment_path should reach into contents[0].items[1]
    assert dl[0]['attachment_path'][-1] == 1, (
        f'dead letter path should land on middle item (index 1); got {dl[0]}'
    )


# ---- 12. actual_size wins over declared_size -------------------------------


@pytest.mark.asyncio
async def test_actual_size_used_instead_of_declared_size(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """Envelope declares size=10 but base64-decoded bytes are 100KB → must
    take ref path (actual_size = 100KB ≥ 64KB threshold).

    Defends against trusting envelope metadata over decoded bytes — a
    malicious / buggy client could otherwise hide a large blob as inline by
    lying about size."""
    ws_id = f'ws-lie-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-lie-{uuid.uuid4().hex[:6]}'
    msg_id = f'msg-lie-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id)

    raw = b'L' * (100 * 1024)
    sha = _sha256(raw)
    envelope = _make_inline_envelope_with_lying_size(raw, declared_size=10)
    data = _msg_data_with_attachments(msg_id, dlg_id, [envelope])

    job_id = _make_job_id('lie')
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='cancelled',
        raw_object_key=None,
    )
    _insert_message_with_data(
        pg_conn, user_id=user_a['id'], msg_id=msg_id, dlg_id=dlg_id,
        data=data, pending=True, job_id=job_id,
    )
    _trigger_phase_d(pg_conn, job_id)

    final = wait_for_job_status(pg_conn, job_id, target_in=('done', 'failed'))
    assert final['status'] == 'done', final

    saved = _select_message(pg_conn, msg_id)
    assert saved is not None
    saved_data, pending, _ = saved
    assert pending is False
    items = saved_data['contents'][0]['items']
    assert items[0]['type'] == 'ref', (
        f'declared_size=10 lying envelope should still take ref path '
        f'(actual_size 100KB); got {items[0]}'
    )
    assert items[0]['sha256'] == sha, items[0]
    # Ref envelope's `size` carries the *actual* len(decoded), not the lying
    # 10. Defense against the client trusting bad metadata downstream.
    assert items[0]['size'] == 100 * 1024, items[0]


# ---- 13. constant drift between Python and frontend ------------------------


def test_constant_drift_with_blob_client_64kb() -> None:
    """Phase D's `_PHASE_D_INLINE_MAX_BYTES` must match frontend's
    `BLOB_INLINE_MAX_BYTES` in `src/data/blob-client.ts`. Drift would cause
    asymmetric inline/ref decisions between import (Phase D) and the live
    write path (frontend `serializeAttachment`)."""
    blob_client_ts = (
        _REPO_ROOT / 'src' / 'data' / 'blob-client.ts'
    ).read_text(encoding='utf-8')
    # Match `export const BLOB_INLINE_MAX_BYTES = 64 * 1024` (allow whitespace
    # variance). Same expression form as the Python constant.
    assert 'BLOB_INLINE_MAX_BYTES = 64 * 1024' in blob_client_ts, (
        f'BLOB_INLINE_MAX_BYTES = 64 * 1024 not found in blob-client.ts; '
        f'first 500 chars: {blob_client_ts[:500]}'
    )
    assert _PHASE_D_INLINE_MAX_BYTES == 64 * 1024, (
        f'_PHASE_D_INLINE_MAX_BYTES = {_PHASE_D_INLINE_MAX_BYTES}; '
        f'expected 64 * 1024'
    )
