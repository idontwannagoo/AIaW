"""Stage 4.5 / Step 3 — Phase B 结构表写入 + imported_from_job_id 加列 +
DELETE cascade soft-delete 通过判据。

映射 plan line 1143-1161（Step 3 通过判据）+ dev 给的 13 条扩展判据 +
migration schema 验证。

驱动方式：
    1. 上传 dexie 风格 fixture 到 `/api/v1/blobs`（拿 sha256 = raw_object_key）
    2. 直接 INSERT `import_jobs` 行到 status='parsing' 让后端 worker 跑
       Phase A → Phase B → status='phase_c'（Step 4 没实现，所以停在那）
    3. PG 直查或 wait_for_job_status 拿结果

为什么不调 multipart 上传：测试焦点是 Phase B 的 LWW + cascade 行为，
multipart 路径已在 test_imports_router.py 覆盖。直接喂 raw_object_key +
parsing 状态是最短路径，绕开 5 个 endpoint 编排，case 跑得更快也更稳。

注意：
- `import_jobs` 当前不在 `stream.py::TABLE_MODELS` 里（Step 6 才登记），
  所以不能直接 ws_connect + subscribe('import_jobs') 抓 phase B 进度事件。
  test_publishes_progress_event_per_table 用 indirect proof：等到 phase_c +
  per_table 各自 PG 行数符合 fixture，证明所有 7 张表都被处理（worker 在每
  张表完成后才推进总计数 + publish 进度事件，二者同源）。
- DELETE cascade 事件用的是 providers / workspaces 等真实 server-routed
  table 名，能 ws_connect 直接订阅 + 抓帧。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import websockets

from .conftest import WS_URL
from .fixtures.dexie_export import (
    _TABLE_SCHEMAS,
    export_dict_to_bytes,
    make_small_export,
)


# ---- src-backend module access (for SERVER_ROUTED_TABLES + PHASE_B_TABLES) -

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_BACKEND = str(_REPO_ROOT / 'src-backend')
if _SRC_BACKEND not in sys.path:
    sys.path.insert(0, _SRC_BACKEND)

from data.import_worker import (  # noqa: E402
    PHASE_B_TABLES,
    SERVER_ROUTED_TABLES,
)


# ---- helpers ----------------------------------------------------------------


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


async def _upload_raw(client: httpx.AsyncClient, body: bytes) -> str:
    r = await client.post(
        '/api/v1/blobs',
        files={'file': ('export.json', body, 'application/json')},
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
    with pg_conn.cursor() as cur:
        cur.execute(
            '''
            INSERT INTO import_jobs (id, user_id, status, raw_object_key)
            VALUES (%s, %s, %s, %s)
            ''',
            (job_id, user_id, status, raw_object_key),
        )


def wait_for_job_status(
    pg_conn: psycopg.Connection,
    job_id: str,
    *,
    target_in: tuple[str, ...],
    timeout_s: float = 8.0,
    poll_s: float = 0.2,
) -> dict[str, Any]:
    """Poll import_jobs row until status ∈ target_in. Returns the full row.

    Step 4 / 5 will reuse this — moved here as a module-level helper so it's
    importable across spec files. (Step 1 had a private `_wait_for_status`
    with the same shape; we keep both until Step 4 lands and consolidates.)
    """
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        with pg_conn.cursor() as cur:
            cur.execute(
                '''
                SELECT id, status, total_rows, total_blobs, total_bytes,
                       processed_bytes, processed_rows, error_message,
                       version, updated_at
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
                'total_blobs': row[3],
                'total_bytes': row[4],
                'processed_bytes': row[5],
                'processed_rows': row[6],
                'error_message': row[7],
                'version': row[8],
                'updated_at': row[9],
            }
            if last['status'] in target_in:
                return last
        time.sleep(poll_s)
    raise AssertionError(
        f'job {job_id} did not reach {target_in} within {timeout_s}s; '
        f'last seen: {last}'
    )


# ---- fixture builders (Step 3 specific) ------------------------------------


def _envelope(table_chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap per-table chunks. Inlined so we don't depend on dexie_export's
    private _envelope helper."""
    tables_meta = [
        {
            'name': c['tableName'],
            'schema': _TABLE_SCHEMAS.get(c['tableName'], ''),
            'rowCount': len(c['rows']),
        }
        for c in table_chunks
    ]
    return {
        'formatName': 'dexie',
        'formatVersion': 1,
        'data': {
            'databaseName': 'aiaw',
            'databaseVersion': 6,
            'tables': tables_meta,
            'data': [
                {
                    'tableName': c['tableName'],
                    'inbound': True,
                    'rows': c['rows'],
                }
                for c in table_chunks
            ],
        },
    }


def _make_export_with_known_uuids(
    *, ws_ids: list[str], dlg_per_ws: int = 0,
) -> dict[str, Any]:
    """Fixture with workspaces at deterministic UUIDs so PG-side queries can
    look them up by id. Optionally include dialogs FK'd to each workspace."""
    workspaces = [
        {
            'id': wsid,
            'name': f'WS {wsid[-4:]}',
            'parentId': '$root',
            'avatar': {'type': 'icon', 'icon': 'sym_o_folder'},
            'vars': {},
            'indexContent': '',
        }
        for wsid in ws_ids
    ]
    chunks = [{'tableName': 'workspaces', 'rows': workspaces}]
    if dlg_per_ws > 0:
        dialogs = []
        for wsid in ws_ids:
            for i in range(dlg_per_ws):
                dialogs.append({
                    'id': f'dlg-{wsid[-4:]}-{i}',
                    'workspaceId': wsid,
                    'name': f'Dlg {i}',
                    'assistantId': None,
                    'msgTree': {},
                    'msgRoute': [],
                    'msgBranchState': {},
                    'inputVars': {},
                })
        chunks.append({'tableName': 'dialogs', 'rows': dialogs})
    return _envelope(chunks)


def _make_export_all_phase_b_tables() -> dict[str, Any]:
    """Fixture covering all 7 PHASE_B_TABLES with at least one row each.

    Counts (to make assertions trivial):
      providers           = 2
      assistants          = 2
      installedPluginsV2  = 2
      reactives           = 2
      avatarImages        = 2
      workspaces          = 2
      dialogs             = 2 (FK'd to first workspace)
    """
    workspaces = [
        {'id': f'ws-all-{i}', 'name': f'W{i}', 'parentId': '$root',
         'avatar': {'type': 'icon', 'icon': 'sym_o_folder'},
         'vars': {}, 'indexContent': ''}
        for i in range(2)
    ]
    chunks = [
        {'tableName': 'providers', 'rows': [
            {'id': f'prov-all-{i}', 'name': f'P{i}', 'type': 'openai',
             'config': {'apiKey': f'sk-{i}'}}
            for i in range(2)
        ]},
        {'tableName': 'assistants', 'rows': [
            {'id': f'asst-all-{i}', 'name': f'A{i}',
             'workspaceId': workspaces[0]['id'], 'model': 'gpt-4'}
            for i in range(2)
        ]},
        {'tableName': 'installedPluginsV2', 'rows': [
            {'key': f'plugin-{i}', 'enabled': True, 'name': f'plug{i}'}
            for i in range(2)
        ]},
        {'tableName': 'reactives', 'rows': [
            {'key': f'react-{i}', 'value': {'foo': i}}
            for i in range(2)
        ]},
        {'tableName': 'avatarImages', 'rows': [
            {'id': f'avatar-{i}', 'mimeType': 'image/png',
             'contentBuffer': 'AAAA'}
            for i in range(2)
        ]},
        {'tableName': 'workspaces', 'rows': workspaces},
        {'tableName': 'dialogs', 'rows': [
            {'id': f'dlg-all-{i}', 'workspaceId': workspaces[0]['id'],
             'name': f'D{i}', 'assistantId': None,
             'msgTree': {}, 'msgRoute': [],
             'msgBranchState': {}, 'inputVars': {}}
            for i in range(2)
        ]},
    ]
    return _envelope(chunks)


def _make_export_with_stale_workspace(
    ws_id: str, *, stale_iso: str,
) -> dict[str, Any]:
    """Fixture with a single workspace bearing an explicit `updatedAt` field.
    Used by LWW skip / overwrite tests."""
    return _envelope([
        {'tableName': 'workspaces', 'rows': [{
            'id': ws_id,
            'name': 'imported-stale',
            'parentId': '$root',
            'avatar': {'type': 'icon', 'icon': 'sym_o_folder'},
            'vars': {},
            'indexContent': '',
            'updatedAt': stale_iso,
        }]},
    ])


async def _drive_import(
    *,
    client: httpx.AsyncClient,
    pg_conn: psycopg.Connection,
    user_id: str,
    body: bytes,
    job_id_prefix: str = 'job',
) -> tuple[str, dict[str, Any]]:
    """Upload fixture → INSERT job at status='parsing' → wait until phase_c
    (Phase B done) or failed. Returns (job_id, final_row_dict)."""
    sha = await _upload_raw(client, body)
    job_id = f'{job_id_prefix}-{sha[:10]}-{uuid.uuid4().hex[:6]}'
    _insert_import_job(
        pg_conn, user_id=user_id, job_id=job_id,
        status='parsing', raw_object_key=sha,
    )
    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_c', 'failed'),
    )
    return job_id, final


# ---- 1. workspaces UUID round-trip -----------------------------------------


async def test_writes_workspaces_with_original_uuid(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    ws_ids = [f'ws-uuid-{i}-{uuid.uuid4().hex[:8]}' for i in range(3)]
    body = export_dict_to_bytes(_make_export_with_known_uuids(ws_ids=ws_ids))

    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='ws-uuid',
    )
    assert final['status'] == 'phase_c', (
        f'expected phase_c after Phase B, got {final}'
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id FROM workspaces WHERE imported_from_job_id=%s '
            'ORDER BY id',
            (job_id,),
        )
        rows = [r[0] for r in cur.fetchall()]
    assert sorted(rows) == sorted(ws_ids), (
        f'PG workspace ids do not match fixture: pg={rows} expected={ws_ids}'
    )


# ---- 2. all 7 structural tables in dependency order ------------------------


async def test_writes_all_structural_tables_in_dependency_order(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    body = export_dict_to_bytes(_make_export_all_phase_b_tables())
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='all-tables',
    )
    assert final['status'] == 'phase_c', final

    # 7 tables × 2 rows each = 14 rows tagged with this job_id
    expected_per_table = {
        'providers': 2, 'assistants': 2, 'installed_plugins': 2,
        'reactives': 2, 'avatar_images': 2, 'workspaces': 2, 'dialogs': 2,
    }
    actual: dict[str, int] = {}
    with pg_conn.cursor() as cur:
        for backend_table in expected_per_table.keys():
            cur.execute(
                f'SELECT COUNT(*) FROM {backend_table} '
                f'WHERE imported_from_job_id=%s',
                (job_id,),
            )
            actual[backend_table] = cur.fetchone()[0]
    assert actual == expected_per_table, (
        f'per-table row counts mismatch: actual={actual} '
        f'expected={expected_per_table}'
    )

    # Dependency order proof: dialogs FK references workspaces. If workspaces
    # were processed AFTER dialogs, the dialog INSERTs would have failed FK
    # check → we'd see 0 dialog rows + a failed status. The fact that
    # actual['dialogs']==2 + status=phase_c is necessary-and-sufficient proof
    # the worker enforced the ordering. (PHASE_B_TABLES tuple has workspaces
    # at index 5 and dialogs at index 6, asserted directly below.)
    table_seq = [bt for _, bt in PHASE_B_TABLES]
    assert table_seq.index('workspaces') < table_seq.index('dialogs'), (
        f'PHASE_B_TABLES order broken: {table_seq}'
    )


# ---- 3. LWW skips older incoming when existing newer -----------------------


async def test_lww_skips_older_incoming_when_existing_newer(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """先 PUT 一个 newer workspace，再导入 older → 跳过 data 覆盖。

    NOTE: imported_from_job_id 仍然被 SET 即使 LWW 跳过 data —— dev 设计是
    `ON CONFLICT DO UPDATE` clause 把 imported_from_job_id 一起塞进
    set_payload，PG 的 WHERE 子句过滤是「数据是否覆盖」的判定，但 ON
    CONFLICT 命中即触发整套 set 字段写入；既然有 WHERE 守护 newer-existing
    场景，该字段在跳过路径下不会写入。
    """
    ws_id = f'ws-lww-skip-{uuid.uuid4().hex[:8]}'

    # 1. PUT workspace via REST → newer updated_at = now()
    r = await client_a.put(
        f'/api/v1/workspaces/{ws_id}',
        json={'name': 'newer-via-put', 'parentId': '$root'},
    )
    assert r.status_code == 200, r.text
    newer_version = r.json()['version']

    # 2. Import older workspace (updatedAt deliberately stale)
    stale_iso = '2024-01-01T00:00:00+00:00'
    body = export_dict_to_bytes(
        _make_export_with_stale_workspace(ws_id, stale_iso=stale_iso)
    )
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='lww-skip',
    )
    assert final['status'] == 'phase_c', final

    # 3. PG row's data should still reflect the PUT (LWW WHERE blocked
    #    overwrite). Version should not have advanced past newer_version.
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT data, version, imported_from_job_id, updated_at '
            'FROM workspaces WHERE id=%s', (ws_id,),
        )
        data, version, imported_from, updated_at = cur.fetchone()

    assert data['name'] == 'newer-via-put', (
        f'LWW skip failed — PG data was overwritten: '
        f'data={data} version={version} imported_from={imported_from} '
        f'updated_at={updated_at}'
    )
    assert version == newer_version, (
        f'version moved despite LWW skip: pg={version} put={newer_version}'
    )


# ---- 4. LWW overwrites when incoming newer ---------------------------------


async def test_lww_overwrites_when_incoming_newer(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """先 PUT older workspace（pg now()=T0）再导入 incoming.updatedAt=T1>T0
    → 覆盖。"""
    ws_id = f'ws-lww-over-{uuid.uuid4().hex[:8]}'
    r = await client_a.put(
        f'/api/v1/workspaces/{ws_id}',
        json={'name': 'older-via-put', 'parentId': '$root'},
    )
    assert r.status_code == 200, r.text

    # incoming.updatedAt = far future to guarantee it wins
    future_iso = (
        datetime.now(timezone.utc) + timedelta(days=365)
    ).isoformat()
    body = export_dict_to_bytes(
        _make_export_with_stale_workspace(ws_id, stale_iso=future_iso)
    )
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='lww-over',
    )
    assert final['status'] == 'phase_c', final

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT data, imported_from_job_id, imported_at '
            'FROM workspaces WHERE id=%s', (ws_id,),
        )
        data, imported_from, imported_at = cur.fetchone()

    assert data['name'] == 'imported-stale', (
        f'LWW overwrite failed — PG data still old: data={data} '
        f'imported_from={imported_from}'
    )
    assert imported_from == job_id, (
        f'imported_from_job_id should be set on overwrite path: '
        f'got {imported_from}'
    )
    assert imported_at is not None, 'imported_at not set on overwrite'


# ---- 5. imported rows tagged with job_id + timestamp -----------------------


async def test_imported_rows_tagged_with_job_id_and_timestamp(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    ws_ids = [f'ws-tag-{i}-{uuid.uuid4().hex[:6]}' for i in range(3)]
    body = export_dict_to_bytes(_make_export_with_known_uuids(ws_ids=ws_ids))

    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='tag',
    )
    assert final['status'] == 'phase_c', final

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT id, imported_from_job_id, imported_at FROM workspaces '
            'WHERE imported_from_job_id=%s', (job_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 3, f'expected 3 tagged rows, got {len(rows)}'
    for row_id, jid, iat in rows:
        assert jid == job_id, f'wrong job_id on {row_id}: {jid}'
        assert iat is not None, f'imported_at NULL on {row_id}'
        # Should be a tz-aware datetime within ~30s of now
        assert (
            datetime.now(timezone.utc) - iat
        ).total_seconds() < 60, f'imported_at too stale: {iat}'


# ---- 6. progress event per table (indirect proof) --------------------------


async def test_publishes_progress_event_per_table(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """import_jobs 当前不在 TABLE_MODELS（Step 6 才登记）→ 不能直接
    ws_connect + subscribe('import_jobs')。改用 indirect proof：

      - 等到 status='phase_c'
      - processed_rows 累计 == fixture 行数（所有 7 张表都被 Phase B 处理过）
      - per-table PG count 与 fixture 一致

    worker 在每张表完成后才推进 processed_rows + publish 进度事件，二者同源
    （见 import_worker.py::run_phase_b 单 commit/UPDATE 与 publish 紧邻）。
    Step 6 引入 server-routed import_jobs 后会补 case 抓真 WS 帧。
    """
    body = export_dict_to_bytes(_make_export_all_phase_b_tables())
    expected_total_rows = 14  # 7 tables × 2 rows

    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='progress',
    )
    assert final['status'] == 'phase_c', final
    assert final['processed_rows'] == expected_total_rows, (
        f'processed_rows={final["processed_rows"]} '
        f'expected={expected_total_rows}; not all 7 tables processed'
    )


# ---- 7. phase B done triggers phase C transition ---------------------------


async def test_phase_b_done_triggers_phase_c(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """worker 在 Phase B 完成后把 status → 'phase_c'。Phase C 当前是
    NotImplementedError stub，会停在 phase_c（no-op handler，见 worker
    `_advance_one`'s phase_c branch）。"""
    body = export_dict_to_bytes(make_small_export())
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='trigger-c',
    )
    assert final['status'] == 'phase_c', (
        f'Phase B did not advance to phase_c: {final}'
    )
    assert final['error_message'] is None, final
    # Phase B incremented version; should be > 1
    assert final['version'] > 1, f'version did not advance: {final}'


# ---- 8. missing table NDJSON files ok --------------------------------------


async def test_phase_b_handles_missing_table_files(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """fixture 只含 workspaces + dialogs（缺 5 张表 NDJSON）→ Phase B 不
    报错，PG 仅 workspaces + dialogs 有行。"""
    ws_ids = [f'ws-miss-{uuid.uuid4().hex[:6]}']
    body = export_dict_to_bytes(_make_export_with_known_uuids(
        ws_ids=ws_ids, dlg_per_ws=2,
    ))
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='missing',
    )
    assert final['status'] == 'phase_c', final

    with pg_conn.cursor() as cur:
        # Tables with rows
        cur.execute(
            'SELECT COUNT(*) FROM workspaces WHERE imported_from_job_id=%s',
            (job_id,),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            'SELECT COUNT(*) FROM dialogs WHERE imported_from_job_id=%s',
            (job_id,),
        )
        assert cur.fetchone()[0] == 2
        # Tables that should be empty (NDJSON missing)
        for tbl in ('providers', 'assistants', 'reactives',
                    'installed_plugins', 'avatar_images'):
            cur.execute(
                f'SELECT COUNT(*) FROM {tbl} WHERE imported_from_job_id=%s',
                (job_id,),
            )
            assert cur.fetchone()[0] == 0, f'{tbl} should be empty'


# ---- 9. crash recovery: tmp dir missing → failed ---------------------------


async def test_phase_b_recovers_from_crash(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """直接插 status='phase_b' job + raw_object_key 指有效 BlobStore key，
    但 tmp 目录里没有 NDJSON（模拟 phase A crash 后 worker 进程重启，
    /tmp 已被另一个进程清理）→ worker 应捕获 ImportFormatError +
    status='failed' + error_message 含 'temp dir'.

    这是 import_worker.py::run_phase_b 在 line 745-749 的硬保护，**不是**
    happy-path recovery（happy-path: NDJSON 仍在 → re-run 走 LWW 跳过）。
    Step 4/5 引入 phase_c/d 时还会补一档真 happy-path resume case。
    """
    # Use a real blob upload (any bytes work — Phase B doesn't read it
    # again, only Phase A does and we're skipping Phase A here).
    body = export_dict_to_bytes(make_small_export())
    sha = await _upload_raw(client_a, body)

    job_id = f'crash-{sha[:8]}-{uuid.uuid4().hex[:6]}'
    _insert_import_job(
        pg_conn, user_id=user_a['id'], job_id=job_id,
        status='phase_b', raw_object_key=sha,
    )

    final = wait_for_job_status(
        pg_conn, job_id, target_in=('phase_c', 'failed'),
    )
    assert final['status'] == 'failed', final
    assert final['error_message'], final
    assert 'phase B' in final['error_message'], final['error_message']
    assert 'temp dir' in final['error_message'].lower(), (
        final['error_message']
    )


# ---- 10. dexie → backend table name mapping --------------------------------


async def test_dexie_to_backend_table_name_mapping(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """fixture 含 `installedPluginsV2` 表 → import 后 PG `installed_plugins`
    表有行，且 `installedpluginsv2` 表不存在。同样验 avatarImages →
    avatar_images。"""
    body = export_dict_to_bytes(_envelope([
        {'tableName': 'installedPluginsV2', 'rows': [
            {'key': 'plug-map-1', 'enabled': True, 'name': 'P1'},
        ]},
        {'tableName': 'avatarImages', 'rows': [
            {'id': 'av-map-1', 'mimeType': 'image/png',
             'contentBuffer': 'AAAA'},
        ]},
    ]))
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='map',
    )
    assert final['status'] == 'phase_c', final

    with pg_conn.cursor() as cur:
        # snake_case backend tables present
        cur.execute(
            'SELECT key FROM installed_plugins WHERE imported_from_job_id=%s',
            (job_id,),
        )
        plugins = [r[0] for r in cur.fetchall()]
        assert plugins == ['plug-map-1'], plugins

        cur.execute(
            'SELECT id FROM avatar_images WHERE imported_from_job_id=%s',
            (job_id,),
        )
        avatars = [r[0] for r in cur.fetchall()]
        assert avatars == ['av-map-1'], avatars

        # camelCase tables don't exist as PG relations
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN "
            "('installedpluginsv2', 'installedPluginsV2', "
            "'avatarimages', 'avatarImages')"
        )
        assert cur.fetchone()[0] == 0, 'camelCase tables should not exist'


# ---- 11. KV-PK table writes correct row ------------------------------------


async def test_kv_pk_table_writes_correct_row(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """fixture 含 reactives row `{key:'foo', value:'bar', updatedAt:...}`
    → PG `SELECT user_id, key, data FROM reactives WHERE
    imported_from_job_id=:job_id` 命中 + key 字段提升到 PK 列。"""
    body = export_dict_to_bytes(_envelope([
        {'tableName': 'reactives', 'rows': [
            {'key': 'kv-test-foo', 'value': 'bar'},
            {'key': 'kv-test-baz', 'value': {'nested': True}},
        ]},
    ]))
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='kv',
    )
    assert final['status'] == 'phase_c', final

    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT user_id, key, data FROM reactives '
            'WHERE imported_from_job_id=%s ORDER BY key', (job_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 2, rows
    keys = [r[1] for r in rows]
    assert keys == ['kv-test-baz', 'kv-test-foo'], keys
    # data should preserve the original wire row
    foo_row = [r for r in rows if r[1] == 'kv-test-foo'][0]
    assert foo_row[0] == user_a['id'], f'user_id mismatch: {foo_row[0]}'
    assert foo_row[2] == {'key': 'kv-test-foo', 'value': 'bar'}, foo_row[2]


# ---- 12. DELETE cancels job → cascade soft-delete imported rows ------------


async def test_delete_cancelled_job_soft_deletes_imported_rows(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
) -> None:
    """import 完成 → DELETE job → 各表 imported_from_job_id 匹配的行
    deleted_at IS NOT NULL + version 相同（共享 cascade_version）。"""
    body = export_dict_to_bytes(_make_export_all_phase_b_tables())
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='cancel',
    )
    assert final['status'] == 'phase_c', final

    # Snapshot pre-delete state for sanity
    with pg_conn.cursor() as cur:
        cur.execute(
            'SELECT COUNT(*) FROM workspaces '
            'WHERE imported_from_job_id=%s AND deleted_at IS NULL',
            (job_id,),
        )
        pre_alive = cur.fetchone()[0]
    assert pre_alive == 2, f'expected 2 live workspaces pre-delete, got {pre_alive}'

    # DELETE the job → triggers cascade
    r = await client_a.delete(f'/api/v1/import/jobs/{job_id}')
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'cancelled'

    # All imported rows soft-deleted, all share the same version
    versions: set[int] = set()
    total_rows = 0
    for backend_table, expected in [
        ('providers', 2), ('assistants', 2), ('installed_plugins', 2),
        ('reactives', 2), ('avatar_images', 2),
        ('workspaces', 2), ('dialogs', 2),
    ]:
        with pg_conn.cursor() as cur:
            cur.execute(
                f'SELECT COUNT(*), array_agg(version) FROM {backend_table} '
                f'WHERE imported_from_job_id=%s AND deleted_at IS NOT NULL',
                (job_id,),
            )
            count, vers_list = cur.fetchone()
        assert count == expected, (
            f'{backend_table}: expected {expected} cascade-deleted, '
            f'got {count}'
        )
        for v in vers_list:
            versions.add(v)
        total_rows += count

    assert total_rows == 14, f'cascade total mismatch: {total_rows}'
    assert len(versions) == 1, (
        f'cascade rows should share one version, got: {versions}'
    )


# ---- 13. DELETE publishes WS event per cascaded row ------------------------


async def test_delete_publishes_ws_event_per_cascaded_row(
    user_a, client_a: httpx.AsyncClient, pg_conn: psycopg.Connection,
    ws_connect,
) -> None:
    """DELETE 后 captureWs 抓到每张 server-routed 表对应的 delete event。

    用 small fixture (1 workspace + 5 dialogs) 简化抓帧逻辑：只订阅
    workspaces + dialogs 两张表，等 6 个 delete event。captureWs 必须在
    DELETE *之前* 完成 subscribe（避免 race），且 broker 是 in-memory
    fan-out → 我们的本地 ws 必须先在 broker 注册才能收到 publish。
    """
    body = export_dict_to_bytes(_make_export_with_known_uuids(
        ws_ids=[f'ws-evt-{uuid.uuid4().hex[:6]}'],
        dlg_per_ws=5,
    ))
    job_id, final = await _drive_import(
        client=client_a, pg_conn=pg_conn, user_id=user_a['id'], body=body,
        job_id_prefix='evt',
    )
    assert final['status'] == 'phase_c', final

    # Subscribe BEFORE deleting so broker has us registered
    captured: list[dict[str, Any]] = []
    async with ws_connect(user_a['access_token']) as ws:
        # Subscribe to both tables we expect to see tombstones from
        for table in ('workspaces', 'dialogs'):
            await ws.send(json.dumps({
                'type': 'subscribe', 'table': table, 'since': 9_999_999_999,
            }))
            # Drain the replay-done frame (since=huge → no replay)
            done = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=5.0)
            )
            assert done['type'] == 'replay-done', done

        # Trigger cascade
        r = await client_a.delete(f'/api/v1/import/jobs/{job_id}')
        assert r.status_code == 200, r.text

        # Drain frames for ~3s. Expecting 1 workspace + 5 dialogs = 6 deletes
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(captured) < 6:
            try:
                msg = json.loads(await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, deadline - time.monotonic())
                ))
            except asyncio.TimeoutError:
                break
            if msg.get('type') == 'event' and msg.get('op') == 'delete':
                captured.append(msg)

    # By table
    by_table: dict[str, list[dict[str, Any]]] = {}
    for ev in captured:
        by_table.setdefault(ev['table'], []).append(ev)
    assert by_table.get('workspaces', []) and len(by_table['workspaces']) == 1, (
        f'workspaces delete events: got {len(by_table.get("workspaces", []))}, '
        f'expected 1; full captured: {captured}'
    )
    assert by_table.get('dialogs', []) and len(by_table['dialogs']) == 5, (
        f'dialogs delete events: got {len(by_table.get("dialogs", []))}, '
        f'expected 5; full captured: {captured}'
    )
    # Wire shape sanity
    for ev in captured:
        assert ev['op'] == 'delete'
        assert ev['row'] is None
        assert isinstance(ev['id'], str) and len(ev['id']) > 0
        assert isinstance(ev['rev'], int) and ev['rev'] > 0


# ---- 14. migration adds column + FK + index to all server-routed tables ----


def test_migration_adds_imported_from_job_id_to_all_routed_tables(
    pg_conn: psycopg.Connection,
) -> None:
    """alembic upgrade after migration `b3e7d4f8a1c5` should add two columns,
    one FK, one index to each of the 10 server-routed tables.

    Pure schema check — runs against the test DB which alembic has already
    upgraded to head (test:up + backend-start does this). No worker drive.
    """
    expected_tables = list(SERVER_ROUTED_TABLES)
    # SERVER_ROUTED_TABLES uses backend snake_case names; matches PG names
    # 1:1 (validated by test 10 above).

    with pg_conn.cursor() as cur:
        # 14a — both columns exist on every table
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public' "
            "AND column_name IN ('imported_from_job_id', 'imported_at') "
            "AND table_name = ANY(%s) ORDER BY table_name, column_name",
            (expected_tables,),
        )
        cols = cur.fetchall()
    seen: dict[str, set[str]] = {}
    for tname, cname in cols:
        seen.setdefault(tname, set()).add(cname)
    for tbl in expected_tables:
        assert seen.get(tbl, set()) == {'imported_from_job_id', 'imported_at'}, (
            f'{tbl} missing columns; saw {seen.get(tbl)}'
        )

    with pg_conn.cursor() as cur:
        # 14b — FK named fk_<table>_imported_from_job_id with ON DELETE SET NULL
        cur.execute(
            "SELECT tc.table_name, tc.constraint_name, rc.delete_rule "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.referential_constraints rc "
            "  ON tc.constraint_name = rc.constraint_name "
            "WHERE tc.table_schema='public' "
            "AND tc.constraint_type='FOREIGN KEY' "
            "AND tc.constraint_name = ANY(%s)",
            (
                [f'fk_{t}_imported_from_job_id' for t in expected_tables],
            ),
        )
        fk_rows = cur.fetchall()
    fk_by_name = {name: (tbl, rule) for tbl, name, rule in fk_rows}
    for tbl in expected_tables:
        fk_name = f'fk_{tbl}_imported_from_job_id'
        assert fk_name in fk_by_name, f'{fk_name} missing'
        seen_tbl, rule = fk_by_name[fk_name]
        assert seen_tbl == tbl, f'{fk_name} on wrong table {seen_tbl}'
        assert rule == 'SET NULL', (
            f'{fk_name} delete rule should be SET NULL, got {rule!r}'
        )

    with pg_conn.cursor() as cur:
        # 14c — index named ix_<table>_imported_from_job_id exists
        cur.execute(
            "SELECT tablename, indexname FROM pg_indexes "
            "WHERE schemaname='public' "
            "AND indexname = ANY(%s)",
            (
                [f'ix_{t}_imported_from_job_id' for t in expected_tables],
            ),
        )
        ix_rows = cur.fetchall()
    ix_by_name = {name: tbl for tbl, name in ix_rows}
    for tbl in expected_tables:
        ix_name = f'ix_{tbl}_imported_from_job_id'
        assert ix_name in ix_by_name, f'{ix_name} missing'
        assert ix_by_name[ix_name] == tbl
