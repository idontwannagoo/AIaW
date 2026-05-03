"""Stage 4.5 / Step 4 — Phase C messages 文字 LWW UPSERT + 500 行/批 +
`_pending_blob_extraction` 双重判定 + dead_letter array-concat 通过判据。

映射 plan line 1180-1196（Step 4 通过判据 6 条）+ dev 给的 11 条扩展 case +
migration partial-index 校验。

驱动方式（与 Step 3 对照）：

    1. 直接 INSERT workspaces + dialogs 行（绕过 Phase B，留专注 Phase C）
    2. 写 messages.ndjson 到 worker 默认 /tmp/import-<job_id>/messages.ndjson
       （worker 进程的 IMPORT_JOB_TMP_ROOT env 默认 /tmp，与 test 进程相同）
    3. INSERT import_jobs 行 status='phase_c' + raw_object_key 占位
       （worker 1s 内 dispatch → _do_phase_c → 跑完转 phase_d）
    4. wait_for_job_status 拿 phase_d 或 failed → PG 直查校验

为什么不调真 multipart + Phase A + Phase B：测试焦点是 Phase C 的
LWW UPSERT + dead_letter + _pending_blob_extraction 判定 + 批次切分；前面阶段
已在 test_imports_router.py / test_import_phase_b.py 覆盖。直接喂 messages.ndjson
是最短路径，case 跑得快也更稳。

注意：
- import_jobs 当前不在 stream.py::TABLE_MODELS（Step 6 才登记）→ 不能直接
  ws_connect + subscribe('import_jobs') 抓 phase C progress 帧。test_progress
  与 test_writes 用 indirect proof：processed_rows 单调递增 + 最终 == 行数。
- `_pending_blob_extraction` 不进 wire envelope（messages router `_to_row`
  / `_to_event` 只取 `data` JSONB）→ 验证必须 PG 直查。
- Phase D 当前是 stub：worker `_advance_one('phase_d')` log warn no-op，
  job 安静停在 phase_d；test_phase_c_done_triggers_phase_d 不要再 wait done。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest


# ---- src-backend module access ---------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_BACKEND = str(_REPO_ROOT / 'src-backend')
if _SRC_BACKEND not in sys.path:
    sys.path.insert(0, _SRC_BACKEND)

from data.import_worker import (  # noqa: E402
    PHASE_C_BATCH_SIZE,
    _PHASE_C_PENDING_SIZE_THRESHOLD,
    _row_has_attachment_envelope,
    _should_pending_blob_extraction,
    temp_dir_for_job,
)


# ---- helpers ----------------------------------------------------------------


def _insert_import_job(
    pg_conn: psycopg.Connection,
    *,
    user_id: str,
    job_id: str,
    status: str = 'phase_c',
    raw_object_key: str | None = None,
) -> None:
    """Insert an import_jobs row directly. Phase C tests drive the worker by
    pre-staging status='phase_c' + an existing messages.ndjson on disk."""
    with pg_conn.cursor() as cur:
        cur.execute(
            '''
            INSERT INTO import_jobs (id, user_id, status, raw_object_key)
            VALUES (%s, %s, %s, %s)
            ''',
            (job_id, user_id, status, raw_object_key),
        )


def _insert_workspace(
    pg_conn: psycopg.Connection,
    *,
    user_id: str,
    ws_id: str,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            '''
            INSERT INTO workspaces (id, user_id, data, updated_at)
            VALUES (%s, %s, %s, now())
            ''',
            (ws_id, user_id, json.dumps(
                {'id': ws_id, 'name': 'test-ws', 'parentId': '$root'}
            )),
        )


def _insert_dialog(
    pg_conn: psycopg.Connection,
    *,
    user_id: str,
    dlg_id: str,
    ws_id: str,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            '''
            INSERT INTO dialogs (id, user_id, workspace_id, data, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ''',
            (dlg_id, user_id, ws_id, json.dumps(
                {'id': dlg_id, 'workspaceId': ws_id, 'name': 'test-dlg'}
            )),
        )


def _stage_messages_ndjson(
    job_id: str, rows: list[dict[str, Any]],
) -> Path:
    """Write rows to <tmp_root>/import-<job_id>/messages.ndjson, mimicking what
    Phase A would have produced. Returns the temp dir path so caller can
    inspect / cleanup."""
    tmp = temp_dir_for_job(job_id)
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / 'messages.ndjson'
    with open(path, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, separators=(',', ':')) + '\n')
    return tmp


def _stage_empty_messages_ndjson(job_id: str) -> Path:
    """Write a 0-byte messages.ndjson; tests this as a 0-batch boundary."""
    tmp = temp_dir_for_job(job_id)
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / 'messages.ndjson'
    path.write_text('', encoding='utf-8')
    return tmp


def _ensure_no_messages_ndjson(job_id: str) -> Path:
    """Ensure tmp dir exists but no messages.ndjson — covers the «messages.ndjson
    file does not exist at all» boundary (run_phase_c logs + skips cleanly)."""
    tmp = temp_dir_for_job(job_id)
    tmp.mkdir(parents=True, exist_ok=True)
    msg_path = tmp / 'messages.ndjson'
    if msg_path.exists():
        msg_path.unlink()
    return tmp


def wait_for_job_status(
    pg_conn: psycopg.Connection,
    job_id: str,
    *,
    target_in: tuple[str, ...],
    timeout_s: float = 10.0,
    poll_s: float = 0.2,
) -> dict[str, Any]:
    """Poll import_jobs row until status ∈ target_in. Returns full row dict.
    Mirrors test_import_phase_b's helper, kept local so adding new columns
    doesn't require coordinating with that file."""
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        with pg_conn.cursor() as cur:
            cur.execute(
                '''
                SELECT id, status, total_rows, processed_rows, version,
                       updated_at, error_message, dead_letter
                FROM import_jobs WHERE id = %s
                ''',
                (job_id,),
            )
            row = cur.fetchone()
        if row is not None:
            last = {
                'id': row[0],
                'status': row[1],
                'total_rows': row[2],
                'processed_rows': row[3],
                'version': row[4],
                'updated_at': row[5],
                'error_message': row[6],
                'dead_letter': row[7],
            }
            if last['status'] in target_in:
                return last
        time.sleep(poll_s)
    raise AssertionError(
        f'job {job_id} did not reach {target_in} within {timeout_s}s; '
        f'last seen: {last}'
    )


def _msg_row(
    msg_id: str,
    dialog_id: str,
    *,
    text: str = 'hello',
    updated_at: str | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a dexie-shape message row dict (what Phase A would write to
    NDJSON, what Phase C reads)."""
    row: dict[str, Any] = {
        'id': msg_id,
        'dialogId': dialog_id,
        'type': 'user',
        'contents': [{'type': 'user-message', 'text': text}],
        'status': 'default',
    }
    if updated_at is not None:
        row['updatedAt'] = updated_at
    if extras:
        row.update(extras)
    return row


def _make_job_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


# ---- 1. writes message text in batches of 500 ------------------------------


@pytest.mark.slow
async def test_writes_message_text_in_batches_of_500(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """fixture 1500 messages（同已存在 dialog）→ PG count(*) = 1500 +
    SELECT processed_rows = 1500 + per-batch progress events ≥ 3 (indirect:
    processed_rows monotonic 500/1000/1500 by virtue of single-source-of-truth
    publish-after-commit pattern)."""
    ws_id = f'ws-batch-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-batch-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    job_id = _make_job_id('batch1500')
    rows = [
        _msg_row(f'msg-{i:04d}-{uuid.uuid4().hex[:6]}', dlg_id, text=f't{i}')
        for i in range(1500)
    ]
    _stage_messages_ndjson(job_id, rows)

    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_d', 'failed'), timeout_s=15.0,
    )
    assert final['status'] == 'phase_d', final
    assert final['processed_rows'] == 1500, (
        f'processed_rows={final["processed_rows"]}, expected 1500; final={final}'
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT COUNT(*) FROM messages WHERE imported_from_job_id=%s',
            (job_id,),
        )
        count = cur.fetchone()[0]
    assert count == 1500, f'PG message count = {count}, expected 1500'

    # Indirect proof of «≥ 3 batches»: processed_rows is updated *only* inside
    # _flush_batch() and bumps by exactly len(batch); 1500 / 500 = 3 batches.
    # We also assert PHASE_C_BATCH_SIZE == 500 so a future tweak breaks here.
    assert PHASE_C_BATCH_SIZE == 500, (
        f'PHASE_C_BATCH_SIZE constant changed to {PHASE_C_BATCH_SIZE}; '
        'spec assumes 500-row batches. Update the spec or revert the change.'
    )


# ---- 2. _pending_blob_extraction marking -----------------------------------


async def test_marks_pending_blob_extraction_for_messages_with_attachment(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """3 message rows: (a) plain text → FALSE; (b) inline blob envelope →
    TRUE (envelope-shape match); (c) row JSON > 64KB → TRUE (size threshold).
    Verified PG-side because _pending_blob_extraction is NOT in wire envelope."""
    ws_id = f'ws-pending-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-pending-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    plain_id = f'msg-plain-{uuid.uuid4().hex[:6]}'
    inline_id = f'msg-inline-{uuid.uuid4().hex[:6]}'
    big_id = f'msg-big-{uuid.uuid4().hex[:6]}'

    plain = _msg_row(plain_id, dlg_id, text='small message')
    # (b) attachment envelope nested inside contents — recursive scan must catch.
    inline = _msg_row(inline_id, dlg_id, text='with inline blob', extras={
        'contentsBlob': {
            'type': 'inline',
            'data': 'AAAA',  # 4-byte b64 placeholder
            'content_type': 'application/octet-stream',
            'size': 128,
        },
    })
    # (c) row JSON > 64KB. Use top-level `bigtext` field with 80KB string.
    big = _msg_row(big_id, dlg_id, text='small head', extras={
        'bigtext': 'X' * (80 * 1024),
    })

    job_id = _make_job_id('pending')
    _stage_messages_ndjson(job_id, [plain, inline, big])
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_d', 'failed'),
    )
    assert final['status'] == 'phase_d', final

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id, _pending_blob_extraction FROM messages '
            'WHERE imported_from_job_id=%s ORDER BY id',
            (job_id,),
        )
        rows = {r[0]: r[1] for r in cur.fetchall()}

    assert rows.get(plain_id) is False, (
        f'plain message should NOT be marked pending; got {rows.get(plain_id)} '
        f'(full row map: {rows})'
    )
    assert rows.get(inline_id) is True, (
        f'inline-blob envelope should mark TRUE; got {rows.get(inline_id)} '
        f'(full row map: {rows})'
    )
    assert rows.get(big_id) is True, (
        f'80KB row JSON should mark TRUE (size threshold = '
        f'{_PHASE_C_PENDING_SIZE_THRESHOLD}B); got {rows.get(big_id)} '
        f'(full row map: {rows})'
    )


# ---- 3. orphan FK → dead_letter --------------------------------------------


async def test_orphan_message_without_dialog_lands_in_dead_letter(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """fixture 含 message 引用 dialogId='never-existed-uuid' → PG messages 表
    无该 row + dead_letter JSONB 数组含 {table:'messages', row_id:..., error
    含 'orphan: dialog'}."""
    ws_id = f'ws-orphan-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-orphan-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    good_id = f'msg-good-{uuid.uuid4().hex[:6]}'
    orphan_id = f'msg-orphan-{uuid.uuid4().hex[:6]}'
    fake_dlg = 'never-existed-uuid'

    rows = [
        _msg_row(good_id, dlg_id, text='legit'),
        _msg_row(orphan_id, fake_dlg, text='orphan'),
    ]
    job_id = _make_job_id('orphan')
    _stage_messages_ndjson(job_id, rows)
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_d', 'failed'),
    )
    assert final['status'] == 'phase_d', final

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id FROM messages WHERE imported_from_job_id=%s',
            (job_id,),
        )
        msg_ids = {r[0] for r in cur.fetchall()}
    assert good_id in msg_ids, f'good message missing: {msg_ids}'
    assert orphan_id not in msg_ids, (
        f'orphan should not have landed in messages: {msg_ids}'
    )

    dl = final['dead_letter']
    assert isinstance(dl, list) and len(dl) >= 1, (
        f'dead_letter should be non-empty list; got {dl!r}'
    )
    matches = [
        e for e in dl
        if e.get('row_id') == orphan_id and e.get('table') == 'messages'
    ]
    assert matches, (
        f'no dead_letter entry for orphan {orphan_id}; full dl={dl}'
    )
    entry = matches[0]
    assert 'orphan' in entry.get('error', '').lower(), (
        f'expected "orphan" in error; got: {entry}'
    )
    assert fake_dlg in entry.get('error', ''), (
        f'expected fake dialog id "{fake_dlg}" in error; got: {entry}'
    )
    assert 'batch_index' in entry, entry
    assert 'ts' in entry, entry


# ---- 4. progress event per batch (indirect proof) --------------------------


@pytest.mark.slow
async def test_progress_event_emitted_per_batch(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """Indirect proof, same template as Step 3's test_publishes_progress_event
    _per_table: import_jobs not yet in TABLE_MODELS → can't WS-subscribe.

    1500 rows / 500 batch = 3 batches; processed_rows is bumped *only* inside
    _flush_batch() (after the per-batch commit + progress publish), so a 3-batch
    run produces 3 distinct processed_rows snapshots {500, 1000, 1500}. We poll
    the row repeatedly during the run + collect distinct (processed_rows,
    version) tuples to prove monotonic per-batch advance."""
    ws_id = f'ws-progress-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-progress-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    job_id = _make_job_id('progress')
    rows = [
        _msg_row(f'msg-prg-{i:04d}', dlg_id, text=f't{i}')
        for i in range(1500)
    ]
    _stage_messages_ndjson(job_id, rows)
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    snapshots: list[tuple[int, int]] = []
    final_status: str | None = None
    deadline = time.monotonic() + 30.0
    # Aggressive poll: each batch commit + processed_rows UPDATE takes a few
    # ms, but is very narrow. 25ms poll keeps stdout small but catches all 3.
    while time.monotonic() < deadline:
        with pg_conn.cursor() as cur:
            cur.execute(
                'SELECT processed_rows, version, status FROM import_jobs '
                'WHERE id=%s', (job_id,),
            )
            r = cur.fetchone()
        if r is not None:
            tup = (int(r[0]), int(r[1]))
            if not snapshots or snapshots[-1] != tup:
                snapshots.append(tup)
            final_status = r[2]
            if final_status in ('phase_d', 'failed'):
                # Drain a bit more to capture any straggler
                time.sleep(0.2)
                with pg_conn.cursor() as cur:
                    cur.execute(
                        'SELECT processed_rows, version FROM import_jobs '
                        'WHERE id=%s', (job_id,),
                    )
                    r2 = cur.fetchone()
                tup2 = (int(r2[0]), int(r2[1]))
                if snapshots[-1] != tup2:
                    snapshots.append(tup2)
                break
        time.sleep(0.025)

    assert final_status == 'phase_d', (
        f'final status {final_status}; snapshots={snapshots}'
    )
    # Filter out (0, *) initial-snapshot noise; keep monotonic processed_rows
    progress_rows = sorted({s[0] for s in snapshots if s[0] > 0})
    # Indirect proof: ≥ 3 distinct cumulative checkpoints + final = 1500.
    # Polling can race with ultra-fast batch commits (each ~ms-scale), so we
    # don't pin the exact intermediate values {500, 1000} — only assert ≥ 3
    # distinct snapshots and that they're each a multiple of PHASE_C_BATCH_SIZE
    # (proves batch-aligned commits, not row-by-row writes).
    assert progress_rows[-1] == 1500, (
        f'final 1500 missing from progress snapshots: {progress_rows} '
        f'(full snaps {snapshots})'
    )
    assert len(progress_rows) >= 3, (
        f'expected ≥ 3 batch checkpoints, got {len(progress_rows)}: '
        f'{progress_rows} (full snaps {snapshots})'
    )
    for v in progress_rows:
        assert v % PHASE_C_BATCH_SIZE == 0 or v == 1500, (
            f'snapshot {v} not aligned with batch size '
            f'{PHASE_C_BATCH_SIZE}: {progress_rows}'
        )
    # Monotonic
    assert progress_rows == sorted(progress_rows), (
        f'progress not monotonic: {progress_rows}'
    )


# ---- 5. phase_c done triggers phase_d --------------------------------------


@pytest.mark.slow
async def test_phase_c_done_triggers_phase_d(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """worker after run_phase_c success advances status='phase_d'.

    Stage 4.5 / Step 5 落地后 Phase D 不再是 stub —— job will continue to
    progress through phase_d → done (with 0 attachments to extract since this
    fixture has no inline blob envelopes ≥ 64KB). We assert the chain
    `phase_c → phase_d → done` is reached without intermediate failure."""
    ws_id = f'ws-trig-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-trig-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    job_id = _make_job_id('trig')
    rows = [_msg_row(f'msg-trig-{i}', dlg_id, text=f't{i}') for i in range(5)]
    _stage_messages_ndjson(job_id, rows)
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    # Step 5 implemented: messages have no inline attachment envelopes →
    # phase D scan returns 0 rows (no _pending_blob_extraction TRUE rows) →
    # status flips done immediately. Verify we reach done not failed.
    final = wait_for_job_status(
        pg_conn, job_id, target_in=('done', 'failed'),
    )
    assert final['status'] == 'done', final
    assert final['error_message'] is None, final


# ---- 6. LWW message text skip when existing newer --------------------------


async def test_lww_message_text_skip_when_existing_newer(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """先 PUT 一条 message（pg now()=newer），再导入老版本（updatedAt=
    far-past）→ PG row data.text 仍是 PUT 版（LWW 跳过）。"""
    ws_id = f'ws-lww-msg-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-lww-msg-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    msg_id = f'msg-lww-{uuid.uuid4().hex[:6]}'

    # Step A — PUT a newer message via REST. messages router has no PUT shortcut
    # by id; create via the standard POST contract. Inspect routers/messages.py
    # to confirm — falls through to PUT-by-id pattern in line with other tables.
    r = await client_a.put(
        f'/api/v1/messages/{msg_id}',
        json={
            'id': msg_id,
            'dialogId': dlg_id,
            'type': 'user',
            'contents': [{'type': 'user-message', 'text': 'NEWER-VIA-PUT'}],
            'status': 'default',
        },
    )
    assert r.status_code == 200, f'PUT failed: {r.status_code} {r.text}'
    put_version = r.json()['version']

    # Step B — import an older version of the same id (updatedAt deliberately
    # stale) and assert PG row still reflects PUT.
    stale_iso = '2024-01-01T00:00:00+00:00'
    older = _msg_row(msg_id, dlg_id, text='OLDER-VIA-IMPORT',
                     updated_at=stale_iso)
    job_id = _make_job_id('lww')
    _stage_messages_ndjson(job_id, [older])
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_d', 'failed'),
    )
    assert final['status'] == 'phase_d', final

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT data, version, imported_from_job_id FROM messages '
            'WHERE id=%s', (msg_id,),
        )
        data, version, imported_from = cur.fetchone()

    contents = data.get('contents') or []
    text = contents[0].get('text') if contents else None
    assert text == 'NEWER-VIA-PUT', (
        f'LWW skip failed — PG message text overwritten: '
        f'data={data} version={version} imported_from={imported_from}'
    )
    assert version == put_version, (
        f'version moved despite LWW skip: pg={version} put={put_version}'
    )


# ---- 7. batch boundary -----------------------------------------------------


@pytest.mark.slow
async def test_batch_boundary(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """Verify exact batch boundaries: 0/500/501/1499 inputs.

    Bundle sub-cases into one test to amortize the workspace+dialog setup
    (4 separate jobs per case else; this stays one slow test)."""
    ws_id = f'ws-bnd-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)

    # We use 4 distinct dialog ids so message ids per case stay isolated; a
    # single dialog with 1499 + 500 + 501 + 0 messages = 2500 rows would
    # collide on iteration order assertions if we ever add them.
    cases = [
        ('zero', 0),
        ('one-batch-500', 500),
        ('two-batch-501', 501),
        ('three-batch-1499', 1499),
    ]
    for label, n in cases:
        dlg_id = f'dlg-{label}-{uuid.uuid4().hex[:6]}'
        _insert_dialog(
            pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
        )
        job_id = _make_job_id(f'bnd-{label}')
        if n > 0:
            rows = [
                _msg_row(f'm-{label}-{i:04d}', dlg_id, text=f't{i}')
                for i in range(n)
            ]
            _stage_messages_ndjson(job_id, rows)
        else:
            # 0 case: messages.ndjson does NOT exist (Phase A would not create
            # the file when no messages in fixture). Worker must skip cleanly.
            _ensure_no_messages_ndjson(job_id)
        _insert_import_job(
            pg_conn, user_id=user_a['id'], job_id=job_id,
            status='phase_c', raw_object_key='dummy-raw-key',
        )

        final = wait_for_job_status(
            pg_conn, job_id, target_in=('phase_d', 'failed'), timeout_s=15.0,
        )
        assert final['status'] == 'phase_d', (
            f'case {label} (n={n}) failed: {final}'
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                'SELECT COUNT(*) FROM messages WHERE imported_from_job_id=%s',
                (job_id,),
            )
            count = cur.fetchone()[0]
        assert count == n, (
            f'case {label}: PG count={count}, expected {n}, final={final}'
        )
        assert final['processed_rows'] == n, (
            f'case {label}: processed_rows={final["processed_rows"]}, '
            f'expected {n}'
        )
        # Free the partial-unique-index slot so the next sub-case's job can
        # land for the same user (terminal status doesn't participate in the
        # uq_import_jobs_active_per_user index).
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE import_jobs SET status='done' WHERE id=%s", (job_id,),
            )


# ---- 8. crash recovery -----------------------------------------------------


async def test_phase_c_recovers_from_crash(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """直接插 status='phase_c' job + 写 messages.ndjson → worker 启动 dispatch
    → 重跑 → 完成推到 phase_d，已写行 LWW 跳过。

    我们模拟「worker 进程重启」的方式：插 phase_c job + ndjson, 等 worker poll
    周期捡起来。第二次再插同样 ndjson 的另一个 job_id，行 id 复用 → LWW 路径
    必须命中（updatedAt 一致 → 不 overwrite）。
    """
    ws_id = f'ws-crash-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-crash-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    msg_id = f'msg-crash-{uuid.uuid4().hex[:6]}'
    fixed_ts = '2025-01-01T00:00:00+00:00'
    row = _msg_row(msg_id, dlg_id, text='first-pass', updated_at=fixed_ts)

    job_id_a = _make_job_id('crash-a')
    _stage_messages_ndjson(job_id_a, [row])
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id_a, status='phase_c',
        raw_object_key='dummy-raw-key',
    )
    final_a = wait_for_job_status(
        pg_conn, job_id_a, target_in=('phase_d', 'failed'),
    )
    assert final_a['status'] == 'phase_d', final_a

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT data, version FROM messages WHERE id=%s', (msg_id,),
        )
        data_a, version_a = cur.fetchone()
    assert data_a['contents'][0]['text'] == 'first-pass', data_a

    # Mark job_a terminal (cancelled) so the partial unique index slot frees up
    # for job_b on the same user.
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET status='done' WHERE id=%s", (job_id_a,),
        )

    # Second pass: distinct job, same NDJSON shape (same updatedAt). LWW WHERE
    # `existing.updated_at < incoming.updated_at` is FALSE (equal, not less)
    # → DO NOTHING. PG row should remain v=version_a, text='first-pass'.
    job_id_b = _make_job_id('crash-b')
    _stage_messages_ndjson(job_id_b, [row])
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id_b, status='phase_c',
        raw_object_key='dummy-raw-key',
    )
    final_b = wait_for_job_status(
        pg_conn, job_id_b, target_in=('phase_d', 'failed'),
    )
    assert final_b['status'] == 'phase_d', final_b

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT data, version, imported_from_job_id FROM messages '
            'WHERE id=%s', (msg_id,),
        )
        data_b, version_b, imp_from_b = cur.fetchone()

    assert data_b['contents'][0]['text'] == 'first-pass', (
        f'LWW skip failed on resume — text overwritten: data_b={data_b}, '
        f'version_a={version_a} version_b={version_b} imp_from_b={imp_from_b}'
    )
    assert version_b == version_a, (
        f'version advanced despite LWW skip: a={version_a} b={version_b}'
    )


# ---- 9. partial index used --------------------------------------------------


def test_pending_blob_extraction_partial_index_used(
    pg_conn: psycopg.Connection,
) -> None:
    """alembic upgrade after migration c5e9f2a8d6b4 → pg_indexes 应有
    ix_messages_pending_blob_extraction + indexdef 含 partial WHERE clause。
    Pure schema check — no worker drive."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public' "
            "AND indexname='ix_messages_pending_blob_extraction'"
        )
        row = cur.fetchone()
    assert row is not None, 'ix_messages_pending_blob_extraction missing'
    indexname, indexdef = row
    # PG normalizes WHERE clause; just check the boolean column + true literal
    # appear and that it's marked as a partial index.
    assert '_pending_blob_extraction' in indexdef.lower(), indexdef
    assert 'where' in indexdef.lower(), (
        f'index is not partial (no WHERE clause): {indexdef}'
    )
    assert 'true' in indexdef.lower(), (
        f'partial WHERE should reference TRUE: {indexdef}'
    )


# ---- 10. imported_from_job_id set on Phase C messages ----------------------


async def test_imported_from_job_id_set_on_phase_c_messages(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """All Phase C-written rows must carry `imported_from_job_id == job_id` +
    `imported_at` non-NULL — same template as Phase B test 5."""
    ws_id = f'ws-tag-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-tag-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    msg_ids = [f'msg-tag-{i}-{uuid.uuid4().hex[:6]}' for i in range(3)]
    rows = [_msg_row(mid, dlg_id, text=f't{i}') for i, mid in enumerate(msg_ids)]
    job_id = _make_job_id('tag')
    _stage_messages_ndjson(job_id, rows)
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_d', 'failed'),
    )
    assert final['status'] == 'phase_d', final

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id, imported_from_job_id, imported_at FROM messages '
            'WHERE id = ANY(%s)', (msg_ids,),
        )
        rows = cur.fetchall()
    assert len(rows) == 3, f'expected 3 tagged rows, got {len(rows)}: {rows}'
    for row_id, jid, iat in rows:
        assert jid == job_id, f'wrong job_id on {row_id}: {jid}'
        assert iat is not None, f'imported_at NULL on {row_id}'
        assert (
            datetime.now(timezone.utc) - iat
        ).total_seconds() < 60, f'imported_at too stale: {iat}'


# ---- 11. dead_letter array-concat ------------------------------------------


async def test_dead_letter_array_concat_appends_no_overwrite(
    user_a, pg_conn: psycopg.Connection,
) -> None:
    """fixture 含两个 orphan messages（不同 fake dialogIds）→ 两次 dead_letter
    append → JSONB 数组长度 = 2 + 顺序保留（按 NDJSON 原始行序）。

    若 worker 用 read-modify-write 而不是 SQL `||` array-concat，并发 worker
    重启时会丢历史；用 array-concat 既不会丢、也保留先后顺序。"""
    ws_id = f'ws-dl-{uuid.uuid4().hex[:6]}'
    dlg_id = f'dlg-dl-{uuid.uuid4().hex[:6]}'
    _insert_workspace(pg_conn, user_id=user_a['id'], ws_id=ws_id)
    _insert_dialog(
        pg_conn, user_id=user_a['id'], dlg_id=dlg_id, ws_id=ws_id,
    )

    fake_a = 'orphan-dialog-aaa'
    fake_b = 'orphan-dialog-bbb'
    orphan_a_id = f'msg-orphA-{uuid.uuid4().hex[:6]}'
    orphan_b_id = f'msg-orphB-{uuid.uuid4().hex[:6]}'
    rows = [
        _msg_row(orphan_a_id, fake_a, text='orphA'),
        _msg_row(orphan_b_id, fake_b, text='orphB'),
    ]
    job_id = _make_job_id('dl')
    _stage_messages_ndjson(job_id, rows)
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id, status='phase_c',
        raw_object_key='dummy-raw-key',
    )

    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_d', 'failed'),
    )
    assert final['status'] == 'phase_d', final

    dl = final['dead_letter']
    assert isinstance(dl, list), f'dead_letter not a list: {dl!r}'
    # Filter to entries from this run (the test's job_id won't appear inside dl
    # entries, but row_id will).
    relevant = [
        e for e in dl
        if e.get('table') == 'messages'
        and e.get('row_id') in (orphan_a_id, orphan_b_id)
    ]
    assert len(relevant) == 2, (
        f'expected 2 dead_letter entries from this job, got {len(relevant)}; '
        f'full dl={dl}'
    )
    # Order preserved (insertion-order from NDJSON)
    row_ids = [e['row_id'] for e in relevant]
    assert row_ids == [orphan_a_id, orphan_b_id], (
        f'dead_letter order not preserved: got {row_ids}, '
        f'expected [{orphan_a_id}, {orphan_b_id}]'
    )
    errors = [e['error'] for e in relevant]
    assert all('orphan' in err.lower() for err in errors), errors
    assert fake_a in errors[0], errors
    assert fake_b in errors[1], errors


# ---- 12. helper unit-tests (defense-in-depth on the predicate) -------------


def test_helper_row_has_attachment_envelope_recurses() -> None:
    """`_row_has_attachment_envelope` must catch envelopes nested deep inside
    contents[].items[]... — Phase A's heuristic only top-level + 1 level list,
    but Phase C must be sure (a missed envelope leaves base64 in PG forever)."""
    # Top level
    assert _row_has_attachment_envelope({
        'type': 'inline', 'data': 'AAAA',
    }) is True
    # Nested in a list
    assert _row_has_attachment_envelope({
        'contents': [
            {'type': 'user-message', 'text': 'hi'},
            {'type': 'ref', 'sha256': 'abc', 'url': 'x', 'size': 1},
        ],
    }) is True
    # Deeply nested
    assert _row_has_attachment_envelope({
        'a': {'b': {'c': [{'type': 'inline', 'base64': 'AAAA'}]}},
    }) is True
    # Plain row with no envelope
    assert _row_has_attachment_envelope({
        'id': 'x', 'contents': [{'type': 'user-message', 'text': 'hi'}],
    }) is False


def test_helper_should_pending_size_threshold() -> None:
    """Size-prong: row JSON ≥ 64KB → TRUE even without envelope."""
    big_row = {'id': 'x', 'bigtext': 'X' * (80 * 1024)}
    big_json = json.dumps(big_row, separators=(',', ':'))
    assert len(big_json.encode('utf-8')) >= _PHASE_C_PENDING_SIZE_THRESHOLD
    assert _should_pending_blob_extraction(big_row, big_json) is True

    small_row = {'id': 'y', 'text': 'tiny'}
    small_json = json.dumps(small_row, separators=(',', ':'))
    assert len(small_json.encode('utf-8')) < _PHASE_C_PENDING_SIZE_THRESHOLD
    assert _should_pending_blob_extraction(small_row, small_json) is False
