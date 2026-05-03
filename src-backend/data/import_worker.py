"""ImportJob worker — Stage 4.5 Step 1.

Single asyncio task per backend process. Polls `import_jobs` for non-terminal
rows and drives them through the phase state machine:

    queued → uploading → assembling → parsing → phase_b → phase_c → phase_d
                                                                         ↓
                                                                       done
                                                       failed / cancelled

Step 1 only implements **Phase A** (status='parsing'): stream the raw upload
out of BlobStore via ijson, validate dexie-export-import schema, split rows
into per-table NDJSON files under `/tmp/import-<job_id>/<table>.ndjson`,
populate `total_rows` / `total_blobs` / `total_bytes`, then advance status to
`phase_b`. Phase B / C / D are stub handlers that raise NotImplementedError —
they're filled in by Steps 3 / 4 / 5 of the plan.

Crash recovery: on startup we scan all rows with status in NON_TERMINAL_STATUSES
and resume them. Phase A is idempotent — overwriting `/tmp/import-<job_id>/`
NDJSON files is safe because they're recreated atomically per phase entry.

Lifecycle (process-side):
    worker = ImportWorker()
    await worker.start()        # spawns background task; resolves once initial
                                # active-job scan is done (worker.startup_complete).
    await worker.stop()         # cancels task; waits for clean exit.

The module is intentionally lazy-import-friendly: nothing at import time
touches DATABASE_URL / JWT_SECRET. The worker is started by app.py inside
`_enable_backend_data_api()` only when both BACKEND_DATA_API_ENABLED and
IMPORT_JOB_ENABLED are true (see app.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import ijson  # type: ignore[import-untyped]
from sqlalchemy import select, text, update

from .db import SessionLocal
from .models.import_job import (
    ImportJob,
    NON_TERMINAL_STATUSES,
)

if TYPE_CHECKING:
    from .blob_store import BlobStore

logger = logging.getLogger('aiaw.backend.import_worker')


# ---- module-level singleton state -------------------------------------------


_worker_singleton: Optional['ImportWorker'] = None


def get_worker() -> 'ImportWorker':
    """Process-wide singleton used by app.py startup hook + tests.
    Tests call `reset_worker_for_tests()` between cases to swap state.
    """
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = ImportWorker()
    return _worker_singleton


def reset_worker_for_tests() -> None:
    global _worker_singleton
    _worker_singleton = None


def get_active_jobs_count() -> int:
    """Synchronous best-effort count for tests.

    Reads via a fresh sync DB connection so callers don't need to be async.
    Returns 0 if the worker singleton hasn't been created (test fixture
    convenience). Never raises — wraps DB errors as 0 so a failed introspect
    doesn't poison the test loop.
    """
    try:
        import psycopg
        from .db import DATABASE_URL

        # asyncpg DSN → psycopg DSN
        sync_dsn = DATABASE_URL.replace('postgresql+asyncpg://', 'postgresql://')
        statuses_sql = ', '.join(f"'{s}'" for s in NON_TERMINAL_STATUSES)
        with psycopg.connect(sync_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT COUNT(*) FROM import_jobs '
                    f'WHERE status IN ({statuses_sql})'
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception as e:  # pragma: no cover — telemetry helper, swallow
        logger.debug('get_active_jobs_count failed: %s', e)
        return 0


# ---- configuration ----------------------------------------------------------


def _temp_root() -> Path:
    """Where Phase A drops per-table NDJSON files. Override via
    IMPORT_JOB_TMP_ROOT (tests use a tmpdir to keep /tmp clean)."""
    return Path(os.environ.get('IMPORT_JOB_TMP_ROOT', '/tmp')).resolve()


def temp_dir_for_job(job_id: str) -> Path:
    """`<tmp_root>/import-<job_id>/` — single dir for all phase-A NDJSON +
    intermediate files. Phase D cleanup (Step 5) wipes this directory.
    """
    return _temp_root() / f'import-{job_id}'


# ---- phase A implementation -------------------------------------------------


# dexie-export-import wire shape (formatVersion=1):
#   {
#     "formatName": "dexie",
#     "formatVersion": 1,
#     "data": {
#       "databaseName": "...",
#       "databaseVersion": <int>,
#       "tables": [{"name": "<table>", "schema": "...", "rowCount": <int>}, ...],
#       "data":   [{"tableName": "<table>", "inbound": <bool>, "rows": [...]}]
#     }
#   }
#
# Phase A streams the outer object with ijson, validating formatName +
# formatVersion before walking `data.data` (the per-table row arrays). Each
# row goes to its own NDJSON file. We DON'T trust the `tables[].rowCount`
# pre-counts — frontend serializers have historically been wrong about these
# (e.g. v3 dexie-export-import omitted soft-deleted rows from rowCount); we
# count rows ourselves by iterating.

# Heuristic: which dexie tables we expect blob-bearing rows in. Phase A only
# *counts* candidate blobs (Phase D actually extracts them). A row is a
# candidate if a top-level field looks like an attachment envelope (has
# `type:'inline'|'ref'` shape) or the row itself is a base64 string longer
# than the inline threshold. Conservative — false positives only inflate
# total_blobs, which is a UX hint not a hard contract.
_BLOB_BEARING_TABLES = {'messages', 'items', 'avatarImages'}


def _looks_like_blob_envelope(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    t = value.get('type')
    return t in ('inline', 'ref')


def _count_candidate_blobs(table: str, row: Any) -> int:
    """Best-effort scan for attachment envelopes in a row. Returns 0 for
    tables outside the blob-bearing set. Phase D is the source of truth for
    actual blob extraction; this is just a UX progress hint.
    """
    if table not in _BLOB_BEARING_TABLES:
        return 0
    if not isinstance(row, dict):
        return 0
    n = 0
    for v in row.values():
        if _looks_like_blob_envelope(v):
            n += 1
        elif isinstance(v, list):
            for item in v:
                if _looks_like_blob_envelope(item):
                    n += 1
    return n


class ImportFormatError(Exception):
    """Raised when Phase A finds the upload isn't a dexie-export-import
    formatted JSON. Worker catches → status='failed' + error_message set."""


async def run_phase_a(
    job_id: str,
    blob_store: 'BlobStore',
    *,
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    """Stream raw upload → split into per-table NDJSON. Returns a summary
    dict the worker writes back into the ImportJob row.

    Memory model: we never hold the full upload in RAM. ijson consumes the
    chunked stream; per-row JSON allocations are short-lived. The only
    long-lived state is the open NDJSON file handles (one per table — a
    handful for the dexie-export-import shape).

    Note on idempotency: this always wipes & recreates the temp dir, so a
    crash-resumed Phase A starts clean. Counters are returned at end (not
    incrementally written) so a half-finished run doesn't leave stale
    `total_rows`. Worker writes them in a single UPDATE.
    """
    # Resolve raw_object_key first so a missing key fails fast before we touch
    # the filesystem.
    async with SessionLocal() as session:
        result = await session.execute(
            select(ImportJob).where(ImportJob.id == job_id)
        )
        job = result.scalar_one()
        raw_key = job.raw_object_key

    if not raw_key:
        raise ImportFormatError(
            f'job {job_id}: raw_object_key is NULL; did Step 2 multipart '
            'complete run?'
        )

    tmp = temp_dir_for_job(job_id)
    if tmp.exists():
        # Resume / retry: drop stale NDJSON before re-parsing. Phase B reads
        # whichever files exist post-A; partial leftovers would corrupt
        # downstream counts.
        await asyncio.to_thread(shutil.rmtree, tmp)
    await asyncio.to_thread(tmp.mkdir, parents=True, exist_ok=True)

    # Per-table file handles — opened lazily on first row of that table so
    # we don't create empty files for tables not present in this export.
    files: dict[str, Any] = {}
    counts: dict[str, int] = {}
    blob_count = 0
    byte_count = 0
    header = {'formatName': None, 'formatVersion': None}

    # Wrap the BlobStore async iterator into an ijson-compatible async
    # file-like object: ijson.parse_async() calls `await f.read(n)`, NOT
    # `async for chunk in f`. We keep an in-flight chunk buffer and serve
    # `read(n)` slices out of it, drawing the next chunk from the blob
    # store iterator when the buffer drains. byte_count accumulates raw
    # bytes consumed (== bytes yielded by the BlobStore stream).
    src_iter = blob_store.open_stream(raw_key, chunk_size=chunk_size).__aiter__()

    class _AsyncReader:
        def __init__(self) -> None:
            self._buf = b''
            self._eof = False

        async def read(self, n: int = -1) -> bytes:
            nonlocal byte_count
            # n=-1 → drain the rest of the stream (rarely called by ijson but
            # honour the file-like contract so debug callers don't surprise).
            if n == -1:
                parts = [self._buf]
                self._buf = b''
                while not self._eof:
                    try:
                        chunk = await src_iter.__anext__()
                    except StopAsyncIteration:
                        self._eof = True
                        break
                    byte_count += len(chunk)
                    parts.append(chunk)
                return b''.join(parts)
            # Refill until we have n bytes or hit EOF.
            while len(self._buf) < n and not self._eof:
                try:
                    chunk = await src_iter.__anext__()
                except StopAsyncIteration:
                    self._eof = True
                    break
                byte_count += len(chunk)
                self._buf += chunk
            out = self._buf[:n]
            self._buf = self._buf[n:]
            return out

    reader = _AsyncReader()

    # Single events-driven pass over the upload:
    #   - Header keys (formatName / formatVersion) at top level are validated
    #     as they fly by.
    #   - `data.data.item` is built as a per-table chunk by accumulating
    #     events into a small ObjectBuilder. We materialize one *row at a
    #     time* via `data.data.item.rows.item` so the largest in-memory
    #     allocation is one row dict — keeps RSS bounded even for the huge
    #     fixture (10k+ messages with attachments).
    current_table: Optional[str] = None

    try:
        # Use ijson's events stream + a hand-rolled row builder so we can
        # multiplex header validation and per-row materialization over a
        # single pass through the byte iterator. ijson.kvitems / items would
        # each need their own parser, but the underlying byte iterator is
        # consume-once (BlobStore.open_stream).
        # ObjectBuilder lives at ijson.common.ObjectBuilder across all
        # ijson versions ≥ 3.0; ijson.ObjectBuilder is a re-export added
        # later. Import the canonical path for portability.
        from ijson.common import ObjectBuilder

        row_builder: Optional[ObjectBuilder] = None
        in_rows_array = False

        async for prefix, event, value in ijson.parse_async(reader):
            # Header validation.
            if prefix == 'formatName' and event == 'string':
                header['formatName'] = value
                if value != 'dexie':
                    raise ImportFormatError(
                        f'unsupported formatName: {value!r}'
                    )
            elif prefix == 'formatVersion' and event == 'number':
                header['formatVersion'] = value

            # Per-table chunk header.
            elif (
                prefix == 'data.data.item.tableName' and event == 'string'
            ):
                current_table = value
                if current_table not in files:
                    files[current_table] = await asyncio.to_thread(
                        open,
                        tmp / f'{current_table}.ndjson',
                        'w',
                        encoding='utf-8',
                    )
                    counts[current_table] = 0

            # Enter / exit the per-table rows array.
            elif (
                prefix == 'data.data.item.rows' and event == 'start_array'
            ):
                in_rows_array = True
            elif (
                prefix == 'data.data.item.rows' and event == 'end_array'
            ):
                in_rows_array = False
                current_table = None

            # Row construction: start_map / end_map / kv events under the
            # row prefix get fed into a fresh ObjectBuilder. On end_map we
            # have a complete row dict to flush.
            elif in_rows_array and current_table is not None:
                if prefix == 'data.data.item.rows.item' and event == 'start_map':
                    row_builder = ObjectBuilder()
                    row_builder.event(event, value)
                elif prefix == 'data.data.item.rows.item' and event == 'end_map':
                    assert row_builder is not None
                    row_builder.event(event, value)
                    row = row_builder.value
                    row_builder = None
                    line = json.dumps(
                        row, ensure_ascii=False, separators=(',', ':')
                    )
                    await asyncio.to_thread(
                        files[current_table].write, line + '\n'
                    )
                    counts[current_table] += 1
                    blob_count += _count_candidate_blobs(current_table, row)
                elif row_builder is not None:
                    row_builder.event(event, value)
                # Non-map row values (rare in dexie-export-import; rows are
                # objects) — silently skip; could log if needed.
    except ImportFormatError:
        raise
    except Exception as e:
        # Wrap any json / io error so caller can mark failed cleanly.
        raise ImportFormatError(f'phase A stream parse failed: {e}') from e
    finally:
        for f in files.values():
            await asyncio.to_thread(f.close)

    # Header validation — formatName must have been seen at least once;
    # missing header means the upload isn't a dexie export at all.
    if header['formatName'] != 'dexie':
        raise ImportFormatError(
            f'missing or invalid formatName (got {header["formatName"]!r})'
        )

    total_rows = sum(counts.values())

    return {
        'total_rows': total_rows,
        'total_blobs': blob_count,
        'total_bytes': byte_count,
        'per_table_counts': counts,
    }


# ---- phase B (structural tables) -------------------------------------------


# Wire-name → backend-table-name map. The 7 small tables Phase B writes:
# providers / reactives / assistants / installedPluginsV2 / avatarImages /
# workspaces / dialogs. Larger tables (messages / items / artifacts) belong
# to Phase C / D.
#
# Dexie names (left side) come from `src/utils/db.ts` schema declaration —
# `installedPluginsV2` and `avatarImages` are camelCase per dexie convention,
# while backend uses snake_case `installed_plugins` / `avatar_images`. The
# map keeps the worker dexie-name-aware (NDJSON files Phase A wrote use the
# dexie name as filename) but PG-name-correct (sqlalchemy tables use the
# snake_case `__tablename__`).
PHASE_B_TABLES: tuple[tuple[str, str], ...] = (
    # (dexie_name, backend_table) — order is dependency order:
    # workspaces have no FK to anything else; dialogs reference workspaces
    # so MUST come after; the others are independent leaf tables but we
    # still write workspaces before dialogs as the load-bearing ordering.
    # The leaf tables (providers / assistants / reactives /
    # installed_plugins / avatar_images) can go in any relative order — we
    # keep them in the human-friendly order plan Step 3 lists.
    ('providers', 'providers'),
    ('assistants', 'assistants'),
    ('installedPluginsV2', 'installed_plugins'),
    ('reactives', 'reactives'),
    ('avatarImages', 'avatar_images'),
    ('workspaces', 'workspaces'),
    ('dialogs', 'dialogs'),
)


# All server-routed tables that grew an `imported_from_job_id` column in
# Stage 4.5 / Step 3. Used by `imports.py::cancel_import_job` for the
# DELETE → soft-delete-imported-rows cascade. Order matters only for cascade
# WS publish ordering (children before parents → consistent with how
# workspaces.py cascade publishes child events first); for the actual SQL
# UPDATE order is immaterial since each row is self-contained.
#
# When a new server-routed table lands, append both here AND in the
# alembic migration `b3e7d4f8a1c5_add_imported_from_job_id_to_routed_tables`
# (or write a follow-up migration adding the column to the new table).
SERVER_ROUTED_TABLES: tuple[str, ...] = (
    'providers',
    'reactives',
    'assistants',
    'installed_plugins',
    'avatar_images',
    'messages',
    'items',
    'artifacts',
    'dialogs',
    'workspaces',
)


# Number of NDJSON rows assembled per VALUES batch. Keep small enough to
# leave headroom for a multi-row pg INSERT (PG ~limit 32k bind params per
# query → at 4 columns per row, 500 rows = 2000 params, safe). Bigger
# batches reduce per-table commit cost; smaller batches surface partial
# progress sooner. 500 matches the messages-batch knob in Step 4.
PHASE_B_BATCH_SIZE = 500


def _extract_lww_timestamp(row: dict[str, Any]) -> datetime:
    """Pull a deterministic LWW timestamp out of an incoming dexie row.

    Dexie schema (Stage 0+) does NOT declare an `updated_at` field on most
    tables, so legacy exports won't have one. We accept either `updatedAt`
    (camelCase, what a future dexie hook would write) or `updated_at`
    (snake_case, what backend rows would carry if the export came from a
    new-deploy database). Both ISO 8601 strings.

    Falls back to `datetime.now(UTC)` (the import time) if neither is
    present. Behaviorally that means a re-import of an old dexie export
    over a freshly PUT row will overwrite the PUT row, because import time
    is later than PUT time. Tests that need to assert LWW-skip semantics
    must inject an explicit `updatedAt` on the imported row (older than
    the existing row's PG `updated_at`) — see plan Step 3 通过判据
    `test_lww_skips_older_incoming_when_existing_newer`.
    """
    raw = row.get('updatedAt') or row.get('updated_at')
    if raw is not None:
        try:
            # `fromisoformat` handles trailing 'Z' from Python 3.11+.
            s = raw if not isinstance(raw, str) else raw.replace('Z', '+00:00')
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _publish_table_progress(
    job: ImportJob, table: str, *, processed_rows: int
) -> dict[str, Any]:
    """Build the WS event body for a per-table progress publish.

    Contract: same envelope shape as other server-routed tables (and
    matches `import_jobs._envelope()` so frontend's realtime handler can
    decode it via the same code path used for status changes). The `data`
    payload reflects the latest counters — frontend treats successive
    events as full snapshots, not deltas.
    """
    snap = job._envelope()
    snap['data']['processed_rows'] = processed_rows
    snap['data']['phase_b_table'] = table
    return {
        'type': 'event',
        'table': 'import_jobs',
        'op': 'put',
        'id': job.id,
        'rev': int(job.version) if job.version is not None else 0,
        'row': snap,
    }


def _normalize_kv_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Map a dexie wire row to the PG row shape for this table.

    For id-PK tables the dexie row has its own `id` field that becomes the
    PG primary key; everything else (incl. workspaceId / dialogId nested in
    the row) lives inside `data`.

    For KV-PK tables (reactives / installed_plugins) the natural identity is
    the `key` field, NOT `id`. We pluck it out to populate the composite PK
    and keep the original payload as `data`.

    Returns a dict with keys: pk_extras (dict of PK columns to bind),
    `data` (JSONB blob), and possibly `workspace_id` / `dialog_id` if the
    table promotes a parent FK.
    """
    if table == 'reactives':
        # reactives row is `{key, value}` per src/utils/types.ts. PK is
        # (user_id, key); `data` carries the whole row dict so we don't
        # lose `value` shape.
        return {
            'pk': {'key': row.get('key', '')},
            'data': row,
        }
    if table == 'installed_plugins':
        # InstalledPlugin row is full plugin envelope; `key` field is the
        # plugin manifest key. PK is (user_id, key).
        return {
            'pk': {'key': row.get('key', '')},
            'data': row,
        }
    if table == 'avatar_images':
        # avatarImages row is `{id, contentBuffer:ArrayBuffer, mimeType}`.
        # ArrayBuffer arrives base64-encoded inside JSONB — Phase B keeps
        # the wire form opaque (consistent with existing avatar_images
        # router). id is the PK.
        return {
            'pk': {'id': row.get('id', '')},
            'data': row,
        }
    if table == 'dialogs':
        # dialogs has workspace_id promoted to its own column (see
        # models/dialog.py). Dexie row carries `workspaceId` (camelCase);
        # we extract it but keep the camelCase in `data` for byte-identical
        # round-trip.
        return {
            'pk': {'id': row.get('id', '')},
            'data': row,
            'workspace_id': row.get('workspaceId', ''),
        }
    # providers / assistants / workspaces — id-PK, no FK promotion.
    return {
        'pk': {'id': row.get('id', '')},
        'data': row,
    }


def _build_insert_stmt(table: str):
    """Return a SQLAlchemy `pg_insert` statement bound to the model class
    for the given backend table name. Worker uses one statement per table
    with bound params per batch row; the `ON CONFLICT (...) DO UPDATE
    WHERE ...` clause provides LWW.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from .models.artifact import Artifact
    from .models.assistant import Assistant
    from .models.avatar_image import AvatarImage
    from .models.dialog import Dialog
    from .models.installed_plugin import InstalledPlugin
    from .models.item import Item
    from .models.message import Message
    from .models.provider import Provider
    from .models.reactive import Reactive
    from .models.workspace import Workspace

    # Map table name → (Model, conflict_index_elements)
    # KV-PK tables conflict on the composite PK; id-PK tables conflict on `id`.
    table_map: dict[str, tuple[Any, list[str]]] = {
        'providers': (Provider, ['id']),
        'assistants': (Assistant, ['id']),
        'workspaces': (Workspace, ['id']),
        'dialogs': (Dialog, ['id']),
        'items': (Item, ['id']),
        'artifacts': (Artifact, ['id']),
        'messages': (Message, ['id']),
        'avatar_images': (AvatarImage, ['id']),
        'reactives': (Reactive, ['user_id', 'key']),
        'installed_plugins': (InstalledPlugin, ['user_id', 'key']),
    }
    Model, idx = table_map[table]
    return pg_insert(Model), Model, idx


async def _phase_b_load_table(
    *,
    session: Any,
    job: ImportJob,
    user_id: str,
    dexie_table: str,
    backend_table: str,
    ndjson_path: Path,
) -> int:
    """Stream NDJSON for one table → batched UPSERT with LWW. Returns the
    number of rows processed (incl. those skipped by LWW WHERE clause —
    which still consume an INSERT attempt at the SQL layer)."""
    if not ndjson_path.exists():
        # Fixture / export simply didn't include this table. Common case:
        # users without any installed plugins / avatar images. Not an
        # error — just no work to do.
        logger.debug(
            'phase B / job %s / table %s: NDJSON missing, skipping',
            job.id, dexie_table,
        )
        return 0

    insert_stmt, Model, conflict_cols = _build_insert_stmt(backend_table)
    now = datetime.now(timezone.utc)

    # ON CONFLICT DO UPDATE clause — LWW: only overwrite the existing row
    # when its `updated_at` is older than the incoming row's. The `EXCLUDED`
    # pseudo-table refers to the values we tried to INSERT; PG eats the
    # write silently if the WHERE clause is false (DO NOTHING semantics for
    # this row), which is exactly what we want for skip-older.
    set_payload: dict[str, Any] = {
        'data': insert_stmt.excluded.data,
        'version': insert_stmt.excluded.version,
        'updated_at': insert_stmt.excluded.updated_at,
        'deleted_at': None,  # revival on re-import
        'imported_from_job_id': insert_stmt.excluded.imported_from_job_id,
        'imported_at': insert_stmt.excluded.imported_at,
    }
    if backend_table == 'dialogs':
        set_payload['workspace_id'] = insert_stmt.excluded.workspace_id

    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=conflict_cols,
        set_=set_payload,
        # LWW guard. `Model.__table__.c.updated_at` references the existing
        # row's column; `insert_stmt.excluded.updated_at` is the incoming
        # row's value. For id-PK tables we additionally guard on user_id
        # ownership (defense-in-depth — id collisions across users would
        # otherwise be silently overwritten).
        where=(Model.__table__.c.updated_at < insert_stmt.excluded.updated_at),
    )

    processed = 0
    batch: list[dict[str, Any]] = []

    # Stream NDJSON line-by-line. Phase A writes one row per line, JSON-
    # encoded with separators=(',', ':'); empty lines are unexpected but
    # tolerated (skip silently).
    def _read_lines() -> list[str]:
        # Read the whole NDJSON in one go — Phase B's small-table set is
        # bounded (typical user: < 100 dialogs, < 100 workspaces, < 50
        # plugins). Even pathological cases stay under a few MB. For
        # messages (Phase C) the streaming model differs.
        with open(ndjson_path, 'r', encoding='utf-8') as f:
            return [line for line in f if line.strip()]

    lines = await asyncio.to_thread(_read_lines)

    next_versions: list[int] = []
    if lines:
        # Pre-fetch one nextval per row for monotonic version assignment.
        # Doing it inline (one round-trip per row) would blow up our query
        # count; doing it as a single UPDATE-from-VALUES is uglier than
        # batching nextvals. PG-native approach: SELECT array of nextvals.
        nv_result = await session.execute(
            text(
                "SELECT nextval('global_change_seq') "
                'FROM generate_series(1, :n)'
            ),
            {'n': len(lines)},
        )
        next_versions = [row[0] for row in nv_result.all()]

    for line, next_version in zip(lines, next_versions):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(
                'phase B / job %s / table %s: skipping malformed line: %s',
                job.id, dexie_table, e,
            )
            continue
        if not isinstance(row, dict):
            continue

        normalized = _normalize_kv_row(backend_table, row)
        lww_ts = _extract_lww_timestamp(row)
        values: dict[str, Any] = {
            'user_id': user_id,
            'data': normalized['data'],
            'version': next_version,
            'updated_at': lww_ts,
            'deleted_at': None,
            'imported_from_job_id': job.id,
            'imported_at': now,
        }
        values.update(normalized['pk'])
        if 'workspace_id' in normalized:
            values['workspace_id'] = normalized['workspace_id']
        batch.append(values)
        processed += 1

        if len(batch) >= PHASE_B_BATCH_SIZE:
            await session.execute(upsert_stmt, batch)
            batch = []

    if batch:
        await session.execute(upsert_stmt, batch)

    return processed


async def run_phase_b(job_id: str) -> dict[str, Any]:
    """Phase B — load 7 small structural tables from Phase A's NDJSON
    output into PG with LWW conflict resolution.

    Tables are processed in dependency order (workspaces before dialogs).
    Each batch commits independently so a mid-table crash leaves an
    idempotent partial state — re-running phase B will re-attempt every
    row, and ON CONFLICT will dedupe.

    Returns a summary the caller (`ImportWorker._do_phase_b`) writes back
    into the job row alongside the status flip to `phase_c`.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(ImportJob).where(ImportJob.id == job_id)
        )
        job = result.scalar_one()
        user_id = job.user_id
        # Pull the row out of the session — we'll reuse it as a snapshot
        # for progress publishes.
        session.expunge(job)

    tmp = temp_dir_for_job(job_id)
    if not tmp.exists():
        raise ImportFormatError(
            f'phase B job {job_id}: temp dir {tmp} missing; phase A did not '
            'leave NDJSON output to consume'
        )

    per_table_processed: dict[str, int] = {}
    total_processed = 0

    from .blob_store import BlobStore  # noqa: F401 (circular guard)

    # Load + commit one table at a time — keeps each commit small and lets
    # progress events fire between tables. If we did one giant transaction
    # the WS subscriber would see no progress until the very end.
    for dexie_table, backend_table in PHASE_B_TABLES:
        ndjson_path = tmp / f'{dexie_table}.ndjson'
        async with SessionLocal() as session:
            try:
                processed = await _phase_b_load_table(
                    session=session,
                    job=job,
                    user_id=user_id,
                    dexie_table=dexie_table,
                    backend_table=backend_table,
                    ndjson_path=ndjson_path,
                )
                # Bump the job row's processed_rows counter as we go so a
                # WS subscriber sees actual progress, not just per-table
                # phase indicators. Single UPDATE per table keeps the
                # row + counter atomic with the just-completed batch.
                total_processed += processed
                next_version = (await session.execute(
                    text("SELECT nextval('global_change_seq')")
                )).scalar_one()
                await session.execute(
                    update(ImportJob)
                    .where(ImportJob.id == job_id)
                    .values(
                        processed_rows=total_processed,
                        version=next_version,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                per_table_processed[backend_table] = processed
            except Exception as e:
                logger.exception(
                    'phase B / job %s / table %s failed: %s',
                    job_id, backend_table, e,
                )
                # Re-raise so the worker's _do_phase_b wrapper can
                # mark the job failed cleanly. Tables already written in
                # earlier iterations stay in PG with their imported_from_
                # job_id tag — a subsequent DELETE / cancel cleans them
                # via the soft-delete cascade.
                raise

        # Publish a progress event after each table commits.
        async with SessionLocal() as session:
            refreshed = (await session.execute(
                select(ImportJob).where(ImportJob.id == job_id)
            )).scalar_one()
            session.expunge(refreshed)
        try:
            from realtime import broker as realtime_broker
            await realtime_broker.publish(
                user_id,
                _publish_table_progress(
                    refreshed,
                    backend_table,
                    processed_rows=total_processed,
                ),
            )
        except Exception as e:  # pragma: no cover — broker failure shouldn't kill phase
            logger.warning(
                'phase B / job %s: failed to publish progress for %s: %s',
                job_id, backend_table, e,
            )

    return {
        'per_table_processed': per_table_processed,
        'total_processed': total_processed,
    }


# ---- phase C (messages text) -----------------------------------------------


# Plan line 1184 / 1191 / 1192: messages 流式读 + 500 行一批 LWW UPSERT，
# 进度按批次报。`PHASE_C_BATCH_SIZE` 与 `PHASE_B_BATCH_SIZE` 同值非偶然 —
# 共用一份「每批 500 行」的 PG bind-param 上限直觉（messages 行的 PG
# UPSERT 列数比 phase B 表略多 1 列 _pending_blob_extraction，但仍远低于
# 32k bind-param 上限 / 4 ≈ 8000 行）。Phase B 的 batch size 改了 Phase C
# 不一定要跟着改 —— 两个常量独立。
PHASE_C_BATCH_SIZE = 500


# 与 frontend `BLOB_INLINE_MAX_BYTES` 对齐的 64KB 阈值。Phase C 用它判定
# 「这条 message 是否需要 Phase D 扫」—— 行 JSON 整体超过此阈值，或行内
# 任意层级出现 attachment envelope（type:'inline'/'ref'）的，都进 pending
# 集合。值故意硬编码而不是从 blob_store 读 —— 避免 import_worker 在 Phase C
# 阶段就触发 blob_store 模块 import（blob_store 读 JWT_SECRET / S3 client，
# Phase A/B 都不需要）。Phase D 自己 import blob_store 时再用 module
# constant 保证一致。
_PHASE_C_PENDING_SIZE_THRESHOLD = 64 * 1024


def _row_has_attachment_envelope(value: Any, _depth: int = 0) -> bool:
    """Recursively scan a row dict / list for an `{type:'inline'|'ref',...}`
    envelope. Mirrors `_looks_like_blob_envelope` from Phase A but recurses
    arbitrarily deep — Phase A only counted top-level + one-level-list (a
    UX hint, not a hard contract), Phase C needs to be sure: a missed
    envelope here means Phase D won't see the row, leaving the inline
    base64 to bloat PG forever.

    Depth cap is defense against pathological self-referential JSON; real
    dexie rows nest at most a handful of levels (contents[].items[].…).
    """
    if _depth > 16:
        return False
    if isinstance(value, dict):
        t = value.get('type')
        if t == 'inline' or t == 'ref':
            # Loose check: must also look envelope-shaped (have one of the
            # well-known sibling keys). Avoid false-positive on user-typed
            # JSON like {"type":"inline","value":"..."} that happens to
            # share the discriminator. Sibling keys: `data`/`base64`/`url`
            # /`sha256` are all envelope hallmarks.
            for sibling in ('data', 'base64', 'url', 'sha256', 'content_type', 'size'):
                if sibling in value:
                    return True
            # Discriminator alone is enough if we found nothing else but the
            # loop also didn't find a positive sibling — be conservative and
            # mark TRUE so Phase D inspects it. False positive: Phase D
            # finds nothing to extract, clears the flag, costs one extra
            # row scan. Cheaper than missing a real attachment.
            return True
        for v in value.values():
            if _row_has_attachment_envelope(v, _depth + 1):
                return True
        return False
    if isinstance(value, list):
        for v in value:
            if _row_has_attachment_envelope(v, _depth + 1):
                return True
    return False


def _should_pending_blob_extraction(row: dict[str, Any], row_json: str) -> bool:
    """Two-pronged decision Phase C uses to set `_pending_blob_extraction`:

    1. Row JSON utf-8 byte size ≥ 64KB → mark TRUE. Catches «huge inline
       base64 attachment» / «runaway streaming-token accumulation» without
       inspecting structure. Most safety-net case.
    2. Row contains an attachment envelope anywhere in its tree (typical
       carrier: `contentsBlob: {type:'ref',...}` after client-side spill,
       or per-content `{type:'inline', data:base64, ...}` in legacy exports).

    Both prongs catch the cases plan line 1192/1213 expects: «Phase C 标记
    含 attachment 的 row → Phase D 把 ≥ 64KB 的 inline 转 ref，把 ref 拍平
    到 wire-format-stable 状态」.
    """
    # Prong 1 — size guard. utf-8 encoding length of the json string is
    # what the broker queue will eventually carry; if it's already huge
    # before Phase D runs, Phase D MUST process it.
    if len(row_json.encode('utf-8')) >= _PHASE_C_PENDING_SIZE_THRESHOLD:
        return True
    # Prong 2 — structural envelope scan.
    return _row_has_attachment_envelope(row)


def _publish_phase_c_progress(
    job: ImportJob, *, processed_rows: int, batch_index: int
) -> dict[str, Any]:
    """WS event body for a per-batch progress publish. Same envelope shape
    as `_publish_table_progress` (Phase B) — frontend code path is unified.

    `data.phase_c_batch_index` is a hint for the UI to render «batch N
    written» without re-counting; `data.processed_rows` is the cumulative
    count across all batches so far. Frontend treats successive events as
    full snapshots not deltas (consistent with Phase B contract).
    """
    snap = job._envelope()
    snap['data']['processed_rows'] = processed_rows
    snap['data']['phase_c_batch_index'] = batch_index
    return {
        'type': 'event',
        'table': 'import_jobs',
        'op': 'put',
        'id': job.id,
        'rev': int(job.version) if job.version is not None else 0,
        'row': snap,
    }


async def _append_dead_letter(
    session: Any, job_id: str, entry: dict[str, Any]
) -> None:
    """Append `entry` to `import_jobs.dead_letter` (JSONB list).

    Server-side concat via `||` so concurrent worker restarts can't lose
    earlier entries — read-modify-write would race. The column server_default
    is `'[]'::jsonb` so `dead_letter || (entry-as-array)::jsonb` is always
    well-formed even on first append.

    We pass the entry as a single-element JSON array string so jsonb's `||`
    operator (array-concat-array semantics) gets two arrays — `dead_letter
    || '[entry]'::jsonb` produces `dead_letter ++ [entry]`. This avoids
    the `dead_letter || object` ambiguity (PG's `||` between array and
    object is asymmetric).

    Caller is responsible for `await session.commit()` — we keep this a
    pure statement so the worker can batch dead_letter appends with the
    surrounding UPSERT in one commit when convenient.
    """
    entry_array = json.dumps([entry], ensure_ascii=False, default=str)
    # Raw SQL via `text()` keeps the JSONB `||` operator + parameter
    # binding readable; using `update(ImportJob).values(...)` would
    # require a server-side jsonb expression construct and the `||`
    # operator overload across asyncpg / psycopg quirks. A plain
    # parameterized UPDATE is unambiguous.
    await session.execute(
        text(
            "UPDATE import_jobs "
            "SET dead_letter = dead_letter || CAST(:entry AS jsonb), "
            "    updated_at = now() "
            "WHERE id = :jid"
        ),
        {'entry': entry_array, 'jid': job_id},
    )


async def _phase_c_lookup_known_dialogs(
    session: Any, user_id: str
) -> set[str]:
    """Pre-fetch the set of dialog ids owned by this user, so Phase C can
    pre-flight FK validation per row before issuing the batched UPSERT.

    Why pre-flight rather than relying on PG's FK violation:

    - A FK violation aborts the entire batch transaction (PG's all-or-
      nothing semantic for `INSERT ... VALUES (...), (...)` plus FK).
      Splitting an orphan out post-fact would mean re-trying the rest of
      the batch row-by-row — quadratic in worst case (a fixture with one
      bad row in every batch would slow Phase C 500x).
    - Filtering out orphans BEFORE the batch UPSERT keeps each batch a
      single multi-row INSERT and lands the orphan in dead_letter once.

    Cost: one `SELECT id FROM dialogs WHERE user_id = :u` per Phase C
    invocation. For typical users (<200 dialogs) that's a few hundred
    bytes of memory; for pathological users (10k dialogs) still well
    under the 200MB Phase A budget.
    """
    from .models.dialog import Dialog

    result = await session.execute(
        select(Dialog.id).where(
            Dialog.user_id == user_id,
            Dialog.deleted_at.is_(None),
        )
    )
    return {row[0] for row in result.all()}


async def run_phase_c(job_id: str) -> dict[str, Any]:
    """Phase C — stream messages.ndjson into PG with LWW UPSERT.

    Streaming model (memory-bounded):

    - Open `<tmp>/messages.ndjson` once, iterate line-by-line. The largest
      in-memory allocation at any moment is a 500-row batch list of dict
      values + matching nextval int list.
    - Each batch: pre-fetch 500 nextvals in one SQL round-trip (mirrors
      Phase B's pattern), build the UPSERT statement once, execute with
      the batch values, commit per-batch. Per-batch commit means a
      mid-phase crash leaves a clean idempotent state — re-run picks up
      where it left off because LWW skips already-written rows.
    - Orphan handling: pre-flight `dialog_id ∈ known_dialogs` per row;
      orphans go to `dead_letter` (with `error = 'orphan: dialog X not
      found'`) and are EXCLUDED from the batch UPSERT — keeps each batch
      one clean INSERT.

    `_pending_blob_extraction` decision (per row): see
    `_should_pending_blob_extraction`. The flag is written into the PG
    row but NOT into the wire envelope (`_to_row` / `_to_event` in
    routers/messages.py only reads `data` JSONB; the underscore prefix
    on the column name is the local convention reminding maintainers).

    Returns a summary dict the caller (`ImportWorker._do_phase_c`) writes
    into the job row alongside the status flip to `phase_d`.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(ImportJob).where(ImportJob.id == job_id)
        )
        job = result.scalar_one()
        user_id = job.user_id
        # Snapshot for progress publishes — released from the session so
        # subsequent commits don't trip the «row was modified» guard.
        session.expunge(job)

    tmp = temp_dir_for_job(job_id)
    if not tmp.exists():
        raise ImportFormatError(
            f'phase C job {job_id}: temp dir {tmp} missing; phase A did not '
            'leave NDJSON output to consume'
        )

    ndjson_path = tmp / 'messages.ndjson'
    if not ndjson_path.exists():
        # Fixture / export simply didn't include messages (a brand new
        # user, or an export from a workspace with no conversation yet).
        # Plan rule (Step 4 user prompt 「小 fixture 行为」): skip cleanly,
        # status flip handled by caller.
        logger.info(
            'phase C / job %s: no messages.ndjson, skipping',
            job_id,
        )
        return {
            'total_processed': 0,
            'orphan_count': 0,
            'pending_blob_count': 0,
            'batches': 0,
        }

    # Pre-flight: known dialog ids for FK validation. We refresh this
    # *once* per Phase C invocation — Phase B already wrote dialogs
    # before transitioning to phase_c, so the set is stable for the
    # duration of this run. If a concurrent dialog-deletion happens
    # mid-Phase-C, an FK violation in the batch UPSERT would still
    # surface (PG enforces it server-side); we'd mark the batch failed
    # and re-run on next dispatch.
    async with SessionLocal() as session:
        known_dialogs = await _phase_c_lookup_known_dialogs(session, user_id)

    insert_stmt, Model, conflict_cols = _build_insert_stmt('messages')
    set_payload: dict[str, Any] = {
        'data': insert_stmt.excluded.data,
        'version': insert_stmt.excluded.version,
        'updated_at': insert_stmt.excluded.updated_at,
        'deleted_at': None,
        'dialog_id': insert_stmt.excluded.dialog_id,
        'imported_from_job_id': insert_stmt.excluded.imported_from_job_id,
        'imported_at': insert_stmt.excluded.imported_at,
        '_pending_blob_extraction': insert_stmt.excluded._pending_blob_extraction,
    }
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=conflict_cols,
        set_=set_payload,
        # LWW guard — same shape as Phase B. existing.updated_at < incoming
        # → overwrite; otherwise PG silently skips this row (DO NOTHING for
        # this conflict).
        where=(Model.__table__.c.updated_at < insert_stmt.excluded.updated_at),
    )

    total_processed = 0
    orphan_count = 0
    pending_blob_count = 0
    batch_index = 0
    batch: list[dict[str, Any]] = []

    now = datetime.now(timezone.utc)

    async def _flush_batch() -> None:
        """Commit the current batch + publish progress + clear list."""
        nonlocal batch, batch_index, total_processed
        if not batch:
            return
        batch_index += 1
        async with SessionLocal() as session:
            try:
                # Pre-fetch nextvals for this batch in one round-trip.
                nv_result = await session.execute(
                    text(
                        "SELECT nextval('global_change_seq') "
                        'FROM generate_series(1, :n)'
                    ),
                    {'n': len(batch)},
                )
                next_versions = [row[0] for row in nv_result.all()]
                # Stitch versions into the batch values right before the
                # INSERT — version is the only field we couldn't precompute
                # outside the session (depends on the seq state at flush
                # time, not row-build time).
                for values, ver in zip(batch, next_versions):
                    values['version'] = ver
                await session.execute(upsert_stmt, batch)
                # Bump processed_rows + version on the import_jobs row so
                # WS subscribers see real cumulative progress (mirrors
                # Phase B per-table commit pattern).
                total_processed += len(batch)
                job_next_version = (await session.execute(
                    text("SELECT nextval('global_change_seq')")
                )).scalar_one()
                await session.execute(
                    update(ImportJob)
                    .where(ImportJob.id == job_id)
                    .values(
                        processed_rows=total_processed,
                        version=job_next_version,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
            except Exception as e:
                # Single-batch failure: log, dead-letter the whole batch
                # (with row ids so a future inspect-and-retry tool can
                # find them), continue. Don't let one bad batch tank the
                # whole phase.
                await session.rollback()
                logger.exception(
                    'phase C / job %s / batch %d: UPSERT failed (%d rows): %s',
                    job_id, batch_index, len(batch), e,
                )
                async with SessionLocal() as inner:
                    for values in batch:
                        await _append_dead_letter(inner, job_id, {
                            'table': 'messages',
                            'row_id': values.get('id'),
                            'error': f'batch upsert failed: {e}',
                            'batch_index': batch_index,
                            'ts': datetime.now(timezone.utc).isoformat(),
                        })
                    await inner.commit()

        # Publish progress per batch (throttled by definition: one event
        # per 500 rows). Failure to publish must not abort the phase —
        # broker is best-effort for progress UX.
        async with SessionLocal() as session:
            refreshed = (await session.execute(
                select(ImportJob).where(ImportJob.id == job_id)
            )).scalar_one()
            session.expunge(refreshed)
        try:
            from realtime import broker as realtime_broker
            await realtime_broker.publish(
                user_id,
                _publish_phase_c_progress(
                    refreshed,
                    processed_rows=total_processed,
                    batch_index=batch_index,
                ),
            )
        except Exception as e:  # pragma: no cover — broker failure shouldn't kill phase
            logger.warning(
                'phase C / job %s: failed to publish progress for batch %d: %s',
                job_id, batch_index, e,
            )

        batch = []

    # Stream NDJSON. Use asyncio.to_thread for the file I/O so we don't
    # block the event loop on disk reads — Phase D blob uploads will be
    # competing for the same loop time and we want the worker responsive.
    def _open_messages() -> Any:
        return open(ndjson_path, 'r', encoding='utf-8')

    f = await asyncio.to_thread(_open_messages)
    try:
        while True:
            line = await asyncio.to_thread(f.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(
                    'phase C / job %s: skipping malformed line: %s',
                    job_id, e,
                )
                async with SessionLocal() as session:
                    await _append_dead_letter(session, job_id, {
                        'table': 'messages',
                        'row_id': None,
                        'error': f'malformed json: {e}',
                        'batch_index': batch_index + 1,
                        'ts': datetime.now(timezone.utc).isoformat(),
                    })
                    await session.commit()
                continue
            if not isinstance(row, dict):
                continue

            row_id = row.get('id')
            dialog_id = row.get('dialogId')
            if not isinstance(row_id, str) or not row_id:
                async with SessionLocal() as session:
                    await _append_dead_letter(session, job_id, {
                        'table': 'messages',
                        'row_id': row_id,
                        'error': 'missing or non-string id',
                        'batch_index': batch_index + 1,
                        'ts': datetime.now(timezone.utc).isoformat(),
                    })
                    await session.commit()
                continue
            if not isinstance(dialog_id, str) or not dialog_id:
                async with SessionLocal() as session:
                    await _append_dead_letter(session, job_id, {
                        'table': 'messages',
                        'row_id': row_id,
                        'error': 'missing or non-string dialogId',
                        'batch_index': batch_index + 1,
                        'ts': datetime.now(timezone.utc).isoformat(),
                    })
                    await session.commit()
                orphan_count += 1
                continue

            # FK pre-flight: dialog must be known (Phase B wrote dialogs).
            if dialog_id not in known_dialogs:
                async with SessionLocal() as session:
                    await _append_dead_letter(session, job_id, {
                        'table': 'messages',
                        'row_id': row_id,
                        'error': f'orphan: dialog {dialog_id} not found',
                        'batch_index': batch_index + 1,
                        'ts': datetime.now(timezone.utc).isoformat(),
                    })
                    await session.commit()
                orphan_count += 1
                continue

            lww_ts = _extract_lww_timestamp(row)
            # Use the canonical separators=(',', ':') JSON dump for both
            # «row JSON byte size» measurement AND Phase A NDJSON serial
            # form so the size threshold behaves consistently regardless
            # of whitespace coming from the dexie source. The actual
            # JSONB column receives the dict (psycopg/asyncpg encodes it),
            # not this string — but using the string for size measurement
            # keeps the threshold comparable across phases.
            row_json = json.dumps(
                row, ensure_ascii=False, separators=(',', ':')
            )
            pending = _should_pending_blob_extraction(row, row_json)
            if pending:
                pending_blob_count += 1

            batch.append({
                'id': row_id,
                'user_id': user_id,
                'dialog_id': dialog_id,
                'data': row,
                # version filled by `_flush_batch` (one nextval call per
                # batch covers all of them).
                'updated_at': lww_ts,
                'deleted_at': None,
                'imported_from_job_id': job_id,
                'imported_at': now,
                '_pending_blob_extraction': pending,
            })

            if len(batch) >= PHASE_C_BATCH_SIZE:
                await _flush_batch()

        # Flush remainder.
        await _flush_batch()
    finally:
        await asyncio.to_thread(f.close)

    return {
        'total_processed': total_processed,
        'orphan_count': orphan_count,
        'pending_blob_count': pending_blob_count,
        'batches': batch_index,
    }


# ---- phase D (attachments → object store) ---------------------------------


# Plan line 1219-1224: 64KB inline 阈值（与 frontend `BLOB_INLINE_MAX_BYTES` /
# backend blob_store.BLOB_INLINE_MAX_BYTES 对齐）。同 Phase C 注释，硬编码而不
# 是从 blob_store 读 module constant —— 避免 worker 启动期 import blob_store
# 触发 JWT_SECRET 检查。Phase D 真要 put blob 时再 lazy-import blob_store，
# 那时拿到的 module-level 常量必然与本地这个一致（CI 跑全套测试会捕捉漂移）。
_PHASE_D_INLINE_MAX_BYTES = 64 * 1024

# Plan line 1222: 4 路并发上限。把 attachment 处理并发到 4 个 task —— 与
# blob_store 的 thread pool / asyncpg pool 大小匹配，再高 PG 连接 pool 容易
# 抢空。
_PHASE_D_CONCURRENCY = 4

# Plan line 1222: 重试 3 次指数退避。1s / 2s / 4s 总计 7s + put 自身耗时；
# attachment 上传慢节点也能撑过短暂 5xx。第 4 次失败 → dead_letter，phase
# 继续。
_PHASE_D_MAX_RETRIES = 3
_PHASE_D_RETRY_BACKOFFS_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Plan line 1224: 进度节流，每 10 个 attachment 完成 publish 一次。比 Phase C
# 的 500 行更细粒度（attachment 处理是 IO-bound 慢，单 message 可能分钟级），
# 用户体感是「跑动了」最重要。
_PHASE_D_PROGRESS_INTERVAL = 10


def _maybe_decode_inline_envelope(value: Any) -> Optional[tuple[bytes, str, int]]:
    """Recognize an `{type:'inline', data:<base64>, ...}` envelope and return
    `(decoded_bytes, content_type, declared_size)`. Returns None if `value`
    isn't a recognizable inline attachment.

    Matches the loose envelope shape `serializeAttachment` writes (see
    `src/data/blob-client.ts::InlineBlob`):

        { type:'inline', data:<base64>, content_type:<str>, size:<int> }

    Tolerant of legacy variants:
      - `data` key sometimes carried as `base64` in older client builds.
      - Missing `size` → fall back to len(decoded_bytes).
      - Missing `content_type` → fall back to 'application/octet-stream'.

    Returns None (envelope unrecognized) on:
      - non-dict
      - `type` != 'inline'
      - missing data/base64
      - base64 decode error (logged + skipped — the value stays as-is and
        Phase D leaves the row alone, will retry on next worker pass; if
        consistently malformed it'll burn through retries → dead_letter).
    """
    if not isinstance(value, dict):
        return None
    if value.get('type') != 'inline':
        return None
    raw_b64 = value.get('data') or value.get('base64')
    if not isinstance(raw_b64, str) or not raw_b64:
        return None
    try:
        import base64
        decoded = base64.b64decode(raw_b64, validate=False)
    except Exception:
        return None
    ct = value.get('content_type') or 'application/octet-stream'
    if not isinstance(ct, str) or not ct:
        ct = 'application/octet-stream'
    declared_size = value.get('size')
    if not isinstance(declared_size, int) or declared_size < 0:
        declared_size = len(decoded)
    return decoded, ct, declared_size


def _walk_attachments(
    container: Any, *, _path: tuple = (), _depth: int = 0
) -> list[tuple[tuple, dict[str, Any]]]:
    """Walk a row tree and yield every inline-attachment envelope along with
    a *path* the caller can use to mutate the original container in place.

    Each yielded entry is `(path, envelope_dict)`:
      - `path` is a tuple of dict-keys / list-indices to reach the envelope
        from the root container. Caller uses `_set_at_path(root, path, ref)`
        to swap the inline envelope for a ref envelope.
      - `envelope_dict` is the dict reference itself (for content / size
        re-extraction without re-walking).

    Why a list of paths rather than mutate-during-walk:
      - Mutating a dict you're iterating over is a footgun; some envelopes
        live in a list and reassigning the slot mid-walk skips the next
        item. Two-pass (collect → process → write) is simpler.
      - Paths are also useful for dead_letter entries: we record the
        attachment location on failure so a future inspect-and-retry tool
        knows exactly which field needs human attention.

    Depth cap matches `_row_has_attachment_envelope` in Phase C — defense
    against pathological self-referential JSON.
    """
    if _depth > 16:
        return []
    found: list[tuple[tuple, dict[str, Any]]] = []
    if isinstance(container, dict):
        # Check if THIS dict is itself an inline envelope.
        if container.get('type') == 'inline' and (
            'data' in container or 'base64' in container
        ):
            found.append((_path, container))
            # Don't recurse into the envelope's own fields — `data` is a
            # base64 string, not a nested envelope.
            return found
        for k, v in container.items():
            found.extend(
                _walk_attachments(v, _path=_path + (k,), _depth=_depth + 1)
            )
    elif isinstance(container, list):
        for i, v in enumerate(container):
            found.extend(
                _walk_attachments(v, _path=_path + (i,), _depth=_depth + 1)
            )
    return found


def _set_at_path(root: Any, path: tuple, new_value: Any) -> None:
    """Replace the value at `path` in `root` with `new_value`. Path is a
    tuple of dict-keys / list-indices as produced by `_walk_attachments`.

    No-op if path is empty (would replace the root, which the caller never
    wants — we always rewrite *into* the row, not the row itself).
    """
    if not path:
        return
    cursor: Any = root
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = new_value


async def _phase_d_upload_one(
    *,
    semaphore: asyncio.Semaphore,
    blob_store: 'BlobStore',
    user_id: str,
    decoded: bytes,
    content_type: str,
) -> dict[str, Any]:
    """Upload `decoded` to BlobStore (sha256-keyed dedup) + ensure a per-user
    blob_ref row exists. Returns the ref envelope dict ready to slot back
    into the row.

    Wraps put/ref work in the semaphore so we cap to 4 concurrent uploads.
    Retries (3 attempts, exp backoff 1/2/4s) live in the caller — keeping
    this function single-attempt makes the retry loop trivial to reason
    about.

    The blob_refs INSERT is `ON CONFLICT DO NOTHING` so re-runs after a
    crash + same (user, sha256) don't error. Same idempotency model as
    `routers/blobs.py::upload_blob`.
    """
    sha256 = hashlib.sha256(decoded).hexdigest()
    size = len(decoded)
    async with semaphore:
        # 1. Put bytes (idempotent on sha256).
        await blob_store.put(sha256, decoded, content_type)

        # 2. Ensure blob row exists. Mirror routers/blobs.py — INSERT with
        # ON CONFLICT DO NOTHING so a concurrent uploader doesn't 500 us.
        async with SessionLocal() as session:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from .models.blob import Blob, BlobRef

            blob_stmt = pg_insert(Blob).values(
                sha256=sha256,
                size=size,
                content_type=content_type,
                storage_key=sha256,  # LocalFs uses sha256 as storage_key
            ).on_conflict_do_nothing(index_elements=[Blob.sha256])
            await session.execute(blob_stmt)

            # 3. Per-user ref. ON CONFLICT bumps last_seen_at — same shape
            # as the live PUT path so a re-import of the same attachment
            # behaves identically to a fresh upload.
            ref_stmt = pg_insert(BlobRef).values(
                user_id=user_id,
                sha256=sha256,
            ).on_conflict_do_update(
                index_elements=[BlobRef.user_id, BlobRef.sha256],
                set_={'last_seen_at': text('now()')},
            )
            await session.execute(ref_stmt)
            await session.commit()

        # 4. Build the ref envelope. URL is presigned per-process; the row's
        # signed URL will expire after BLOB_PRESIGN_TTL_SECONDS. The
        # frontend treats sha256 as the canonical id and re-presigns via
        # GET /api/v1/blobs/<sha>/presign on demand (or just refetches the
        # row to get a fresh envelope). For Phase D we put a *valid for the
        # next hour* URL into the row so an immediate read after import
        # works without an extra round-trip.
        from .blob_store import BLOB_PRESIGN_TTL_SECONDS
        # No request context inside the worker → can't compute X-Forwarded
        # absolute base. Use the configured backend external URL so the
        # URL points at the same host the frontend will hit. Fallback to
        # relative if no env is set (frontend fetch will resolve relative
        # to its origin, which works in same-origin deployments).
        backend_base = os.environ.get('BACKEND_DATA_API_URL', '').rstrip('/')
        if not backend_base:
            backend_base = ''  # presign_get_url tolerates empty → relative URL
        url = blob_store.presign_get_url(
            sha256, ttl_seconds=BLOB_PRESIGN_TTL_SECONDS, base_url=backend_base
        )

    return {
        'type': 'ref',
        'url': url,
        'sha256': sha256,
        'size': size,
        'content_type': content_type,
    }


async def _phase_d_process_row(
    *,
    semaphore: asyncio.Semaphore,
    blob_store: 'BlobStore',
    job_id: str,
    user_id: str,
    row_id: str,
    row_data: dict[str, Any],
) -> dict[str, Any]:
    """Process every inline attachment in one message row.

    Walk → for each inline envelope:
      - If decoded size < 64KB → leave as-is (inline stays in PG row).
      - If decoded size ≥ 64KB → upload + replace with ref envelope. Retry
        up to 3 times with exp backoff on transient errors. Final failure
        → dead_letter entry, but envelope stays inline (incorrect-but-
        tolerable: row remains queryable, future re-run can retry).

    Then UPDATE the messages row in one transaction:
      - data := mutated row_data (with refs swapped in)
      - _pending_blob_extraction := FALSE
      - version := nextval('global_change_seq')
      - updated_at := now()

    Same-tx invariant: row data rewrite + flag clear together so a worker
    crash between the two can never leave a row marked TRUE but already
    rewritten (would re-process refs → no-op since envelope is now ref →
    flag eventually clears, but burns IO). Doing both in one UPDATE makes
    the crash recovery story trivial.

    Returns a per-row summary dict for the caller to aggregate: number of
    attachments processed, failures, etc. Failures don't raise; the caller
    decides not to abort the phase.
    """
    attachments = _walk_attachments(row_data)
    n_processed = 0
    n_uploaded = 0
    n_inline_kept = 0
    n_failed = 0
    failures: list[dict[str, Any]] = []

    for path, envelope in attachments:
        decoded_meta = _maybe_decode_inline_envelope(envelope)
        if decoded_meta is None:
            # Already a ref / unrecognizable / decode failed — leave alone.
            # If the value is a `{type:'ref',...}` envelope this is the
            # crash-recovery happy path: we already rewrote it, no work to
            # do.
            continue

        decoded, content_type, _declared_size = decoded_meta
        actual_size = len(decoded)

        if actual_size < _PHASE_D_INLINE_MAX_BYTES:
            # Plan line 1220 — small attachments stay inline. Don't upload,
            # don't rewrite. Counts as "processed" for the throttle so
            # progress events still fire on small-attachment rows.
            n_inline_kept += 1
            n_processed += 1
            continue

        # Large attachment: try uploading with retries.
        last_error: Optional[Exception] = None
        ref_envelope: Optional[dict[str, Any]] = None
        for attempt in range(_PHASE_D_MAX_RETRIES + 1):
            try:
                ref_envelope = await _phase_d_upload_one(
                    semaphore=semaphore,
                    blob_store=blob_store,
                    user_id=user_id,
                    decoded=decoded,
                    content_type=content_type,
                )
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < _PHASE_D_MAX_RETRIES:
                    backoff = _PHASE_D_RETRY_BACKOFFS_SECONDS[attempt]
                    logger.warning(
                        'phase D / job %s / row %s / attachment %s: '
                        'attempt %d/%d failed (%s), retrying in %.1fs',
                        job_id, row_id, path, attempt + 1,
                        _PHASE_D_MAX_RETRIES + 1, e, backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.warning(
                        'phase D / job %s / row %s / attachment %s: '
                        'all %d attempts failed (%s), dead-lettering',
                        job_id, row_id, path,
                        _PHASE_D_MAX_RETRIES + 1, e,
                    )

        if ref_envelope is not None:
            _set_at_path(row_data, path, ref_envelope)
            n_uploaded += 1
            n_processed += 1
        else:
            # All retries failed — dead_letter and leave the inline envelope
            # in place. The row stays semantically correct (frontend can
            # decode inline base64) but bloats PG. A future inspect-and-
            # retry tool reads dead_letter, picks rows where the attachment
            # is still inline, and re-runs Phase D on demand.
            n_failed += 1
            failures.append({
                'table': 'messages',
                'row_id': row_id,
                'attachment_path': list(path),
                'error': (
                    f'attachment upload failed after '
                    f'{_PHASE_D_MAX_RETRIES + 1} attempts: {last_error}'
                ),
                'attempt': _PHASE_D_MAX_RETRIES + 1,
                'ts': datetime.now(timezone.utc).isoformat(),
            })

    # UPDATE the row. Even if no attachments were rewritten (all stayed
    # inline / all failed), we still clear `_pending_blob_extraction` so the
    # row drops out of the partial-index scan on next pass — failures are
    # captured in dead_letter, not by re-queueing the row indefinitely.
    async with SessionLocal() as session:
        from .models.message import Message

        next_version = (await session.execute(
            text("SELECT nextval('global_change_seq')")
        )).scalar_one()
        await session.execute(
            update(Message)
            .where(Message.id == row_id, Message.user_id == user_id)
            .values(
                data=row_data,
                _pending_blob_extraction=False,
                version=next_version,
                updated_at=datetime.now(timezone.utc),
            )
        )
        # Append all failures for this row as one JSONB array-concat (single
        # UPDATE) — same `||` template as Phase C `_append_dead_letter` but
        # batched per-row to keep commit cost low when a single row has many
        # failed attachments.
        if failures:
            entries_array = json.dumps(failures, ensure_ascii=False, default=str)
            await session.execute(
                text(
                    'UPDATE import_jobs '
                    'SET dead_letter = dead_letter || CAST(:entries AS jsonb), '
                    '    updated_at = now() '
                    'WHERE id = :jid'
                ),
                {'entries': entries_array, 'jid': job_id},
            )
        await session.commit()

    return {
        'row_id': row_id,
        'n_processed': n_processed,
        'n_uploaded': n_uploaded,
        'n_inline_kept': n_inline_kept,
        'n_failed': n_failed,
    }


def _publish_phase_d_progress(
    job: ImportJob, *, processed_blobs: int, attachment_index: int
) -> dict[str, Any]:
    """WS event body for Phase D progress publishes. Same envelope shape as
    Phase B / C — frontend treats successive events as full snapshots, not
    deltas. `phase_d_attachment_index` lets UI render a per-attachment
    «processed N/M» without a separate count round-trip.
    """
    snap = job._envelope()
    snap['data']['processed_blobs'] = processed_blobs
    snap['data']['phase_d_attachment_index'] = attachment_index
    return {
        'type': 'event',
        'table': 'import_jobs',
        'op': 'put',
        'id': job.id,
        'rev': int(job.version) if job.version is not None else 0,
        'row': snap,
    }


async def _phase_d_publish_progress(
    job_id: str, user_id: str, *, processed_blobs: int, attachment_index: int
) -> None:
    """Refresh the job snapshot + publish one progress event. Best-effort —
    a broker failure logs and continues so progress UX glitches never tank
    the phase.
    """
    async with SessionLocal() as session:
        # Bump processed_blobs on the row so the snapshot we publish carries
        # the same counter. Single UPDATE per progress tick is cheap (every
        # 10 attachments).
        next_version = (await session.execute(
            text("SELECT nextval('global_change_seq')")
        )).scalar_one()
        await session.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id)
            .values(
                processed_blobs=processed_blobs,
                version=next_version,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        refreshed = (await session.execute(
            select(ImportJob).where(ImportJob.id == job_id)
        )).scalar_one()
        session.expunge(refreshed)
    try:
        from realtime import broker as realtime_broker
        await realtime_broker.publish(
            user_id,
            _publish_phase_d_progress(
                refreshed,
                processed_blobs=processed_blobs,
                attachment_index=attachment_index,
            ),
        )
    except Exception as e:  # pragma: no cover — broker failure shouldn't kill phase
        logger.warning(
            'phase D / job %s: failed to publish progress at index %d: %s',
            job_id, attachment_index, e,
        )


async def _phase_d_cleanup(
    job_id: str, raw_object_key: Optional[str], blob_store: 'BlobStore'
) -> None:
    """Plan line 1225: 全部完成 → 清 `/tmp/import-<job_id>/` + 对象存储里
    的 raw upload (TTL 7 天 lifecycle rule 兜底).

    tmp dir：unconditional rmtree, ignore_errors —— 残留 NDJSON 不影响后续
    job（每个 job 一个独立目录）。

    raw upload：trickier。LocalFs 把 multipart-completed bytes 存到 sha256
    路径 (`<root>/<sha[:2]>/<sha[2:]>`)，与所有其他 blob 共享文件命名空间。
    如果 raw upload 的 sha256 恰好匹配某个 user 的 attachment blob（极小
    概率：用户上传了 backup file 然后 backup 内某个 attachment 的字节正好
    与 backup file 自身相同——不会发生），盲删会把那个 blob 的字节也删了。
    防御：先查 `blobs` 表，如果有 row 引用同 sha256 → skip 删，让 GC 处理；
    否则安全删除。

    本函数对所有失败 best-effort warn —— job 已 done，cleanup 失败不应回滚
    状态。
    """
    tmp = temp_dir_for_job(job_id)
    try:
        if await asyncio.to_thread(tmp.exists):
            await asyncio.to_thread(shutil.rmtree, tmp, ignore_errors=True)
            logger.info('phase D / job %s: cleaned up tmp dir %s', job_id, tmp)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(
            'phase D / job %s: tmp cleanup failed (non-fatal): %s', job_id, e,
        )

    if not raw_object_key:
        return

    try:
        # Check if any blobs row references the same key — for LocalFs
        # `storage_key == sha256 == raw_object_key` (after multipart complete
        # rewrites raw_object_key to the canonical sha256). If a row exists,
        # it means the bytes are also serving as a user attachment blob and
        # we MUST NOT delete them. Lifecycle GC will reap the orphan if the
        # blob really is import-only.
        async with SessionLocal() as session:
            from .models.blob import Blob

            result = await session.execute(
                select(Blob).where(Blob.sha256 == raw_object_key)
            )
            shared_blob = result.scalar_one_or_none()

        if shared_blob is not None:
            logger.info(
                'phase D / job %s: raw upload %s is shared with user blob, '
                'skipping delete (lifecycle GC will handle)',
                job_id, raw_object_key,
            )
            return

        await blob_store.delete(raw_object_key)
        logger.info(
            'phase D / job %s: deleted raw upload %s', job_id, raw_object_key,
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(
            'phase D / job %s: raw upload cleanup failed (non-fatal, '
            'lifecycle GC will catch): %s', job_id, e,
        )


async def run_phase_d(job_id: str) -> dict[str, Any]:
    """Phase D — extract attachments from messages tagged
    `_pending_blob_extraction=TRUE` for this job's user, decide inline vs
    ref by 64KB threshold, upload large ones to BlobStore + rewrite the
    row's attachment field to a `{type:'ref',...}` envelope.

    Concurrency model:
    - One `asyncio.Semaphore(4)` shared across all rows of this phase. The
      semaphore wraps the BlobStore put + blob_refs INSERT for one
      attachment, NOT the row processing as a whole — that means a row
      with 5 attachments gets to start uploading attachment 1 even while
      attachment 0 of the previous row is still in flight (saturating the
      4-slot pool whenever there's work).
    - Per-row processing is sequential within each row to keep the row
      UPDATE atomic (one transaction per row).
    - Rows are processed in batches of `concurrency*4` so the asyncio
      gather() set never balloons with thousands of pending tasks for a
      large fixture; we yield to the event loop between batches.

    Crash recovery:
    - The partial index `ix_messages_pending_blob_extraction` (Phase C
      created) only covers TRUE rows. On restart we re-scan and re-process
      whichever rows didn't get their flag cleared. Already-rewritten rows
      have `{type:'ref',...}` envelopes which `_walk_attachments` skips
      naturally (no inline `data` key → `_maybe_decode_inline_envelope`
      returns None).
    - `BlobStore.put(sha256, ...)` is idempotent (same sha → same bytes,
      no-op write) so a half-uploaded attachment from the previous run
      doesn't double-bill or corrupt.

    Returns a summary dict the caller writes into the job row alongside
    the status flip to `done`.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(ImportJob).where(ImportJob.id == job_id)
        )
        job = result.scalar_one()
        user_id = job.user_id
        raw_object_key = job.raw_object_key
        session.expunge(job)

    from .blob_store import get_blob_store
    blob_store = get_blob_store()

    # Find all rows still flagged TRUE for this user. The partial index
    # makes this scan cheap even with millions of cleared rows in the
    # table.
    async with SessionLocal() as session:
        from .models.message import Message

        # NB: use `== True` (compiles to `= TRUE`) rather than `.is_(True)`
        # so the predicate matches the partial index's WHERE clause exactly
        # (`postgresql_where=sa.text('_pending_blob_extraction = TRUE')` in
        # migration c5e9f2a8d6b4). PG's planner won't always pick a partial
        # index when the query uses `IS TRUE` even though it's logically
        # equivalent.
        result = await session.execute(
            select(Message.id, Message.data).where(
                Message.user_id == user_id,
                Message._pending_blob_extraction == True,  # noqa: E712
            )
        )
        rows: list[tuple[str, dict[str, Any]]] = [
            (row_id, row_data) for row_id, row_data in result.all()
        ]

    if not rows:
        logger.info(
            'phase D / job %s: no pending rows, advancing to done', job_id,
        )
        # Even on empty work set we still run cleanup — tmp dir + raw upload
        # could exist from earlier phases.
        await _phase_d_cleanup(job_id, raw_object_key, blob_store)
        return {
            'rows_processed': 0,
            'attachments_processed': 0,
            'attachments_uploaded': 0,
            'attachments_inline_kept': 0,
            'attachments_failed': 0,
        }

    semaphore = asyncio.Semaphore(_PHASE_D_CONCURRENCY)

    rows_processed = 0
    attachments_processed = 0
    attachments_uploaded = 0
    attachments_inline_kept = 0
    attachments_failed = 0

    # Process rows in batches so the gather() set stays bounded. Batch size
    # `_PHASE_D_CONCURRENCY * 4` strikes a balance: enough rows in flight
    # to keep the 4-slot semaphore saturated, not so many that we stall
    # garbage collection on the per-row dict copies. Each batch is one
    # `asyncio.gather` await.
    batch_size = _PHASE_D_CONCURRENCY * 4
    last_published_processed = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        results = await asyncio.gather(
            *[
                _phase_d_process_row(
                    semaphore=semaphore,
                    blob_store=blob_store,
                    job_id=job_id,
                    user_id=user_id,
                    row_id=row_id,
                    row_data=row_data,
                )
                for row_id, row_data in batch
            ],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                # Per-row processing exceptions are rare — _phase_d_process_row
                # swallows attachment-level failures into dead_letter. A raise
                # here means the UPDATE itself failed, e.g. PG conn drop.
                # Log + count as failed; don't abort the whole phase.
                logger.exception(
                    'phase D / job %s: row processing raised: %s', job_id, r,
                )
                attachments_failed += 1
                continue
            rows_processed += 1
            attachments_processed += r['n_processed']
            attachments_uploaded += r['n_uploaded']
            attachments_inline_kept += r['n_inline_kept']
            attachments_failed += r['n_failed']

        # Throttled progress: publish only when we've crossed a multiple of
        # the interval since the last publish. Prevents one batch with 100
        # attachments from spamming 100 events.
        if (
            attachments_processed - last_published_processed
            >= _PHASE_D_PROGRESS_INTERVAL
        ):
            await _phase_d_publish_progress(
                job_id, user_id,
                processed_blobs=attachments_processed,
                attachment_index=attachments_processed,
            )
            last_published_processed = attachments_processed

    # Final progress event so a watcher always sees the last value (even if
    # the last batch didn't cross the throttle threshold).
    if attachments_processed != last_published_processed or rows_processed > 0:
        await _phase_d_publish_progress(
            job_id, user_id,
            processed_blobs=attachments_processed,
            attachment_index=attachments_processed,
        )

    # Cleanup tmp + raw upload. Best-effort — failure here doesn't unwind
    # the phase (rows are already correctly rewritten in PG).
    await _phase_d_cleanup(job_id, raw_object_key, blob_store)

    return {
        'rows_processed': rows_processed,
        'attachments_processed': attachments_processed,
        'attachments_uploaded': attachments_uploaded,
        'attachments_inline_kept': attachments_inline_kept,
        'attachments_failed': attachments_failed,
    }


# ---- worker --------------------------------------------------------------


# Poll interval — Step 2 will wire a notify_pending() shortcut so the worker
# wakes immediately on multipart-complete; for Step 1 we just poll.
POLL_INTERVAL_SECONDS = float(
    os.environ.get('IMPORT_JOB_POLL_INTERVAL_SECONDS', '1.0')
)


class ImportWorker:
    """Single background task that drives all import jobs in this process.

    State machine dispatch is keyed on `ImportJob.status`. The worker mutates
    status as it advances — concurrent worker instances would race on this
    column; partial unique index alone doesn't prevent two workers from both
    picking up the same row. Step 6 (multi-process deploy) will introduce a
    `pg_try_advisory_xact_lock(hashtext(job_id))` per-job lease; for Step 1
    we assume single-process backend (Northflank's current `app.py` runs one
    uvicorn worker), and a single in-process worker singleton.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        # Resolved once the initial active-jobs scan completes — tests await
        # this to know the worker has had a chance to pick up pre-existing
        # rows before they assert on state.
        self.startup_complete = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return  # already running
        self._stop.clear()
        self._wake.clear()
        self.startup_complete.clear()
        self._task = asyncio.create_task(self._run(), name='import-worker')
        # Don't return until startup_complete fires so callers (FastAPI
        # lifespan + tests) can rely on "if start() returned, recovery scan
        # already happened".
        await self.startup_complete.wait()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover — defensive
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._task = None

    def notify_pending(self) -> None:
        """Step 2 endpoints call this after multipart-complete to wake the
        worker immediately instead of waiting for the next poll tick."""
        self._wake.set()

    async def _run(self) -> None:
        # Recovery scan first.
        try:
            await self._dispatch_active_jobs()
        finally:
            self.startup_complete.set()

        while not self._stop.is_set():
            try:
                # Wait for either a wake signal or the poll interval.
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=POLL_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                if self._stop.is_set():
                    break
                await self._dispatch_active_jobs()
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover — defensive
                logger.exception('import worker loop error: %s', e)
                # Back off briefly so a persistent error doesn't tight-loop.
                await asyncio.sleep(1.0)

    async def _dispatch_active_jobs(self) -> None:
        """Scan for non-terminal jobs and advance each one phase by phase."""
        async with SessionLocal() as session:
            result = await session.execute(
                select(ImportJob.id, ImportJob.status)
                .where(ImportJob.status.in_(NON_TERMINAL_STATUSES))
                .order_by(ImportJob.created_at)
            )
            jobs = result.all()

        for job_id, status in jobs:
            try:
                await self._advance_one(job_id, status)
            except Exception as e:
                logger.exception(
                    'import job %s: unhandled error during dispatch: %s',
                    job_id, e,
                )
                await self._mark_failed(job_id, f'dispatch error: {e}')

    async def _advance_one(self, job_id: str, status: str) -> None:
        """Drive one job through its current phase. Returns when the job is
        either advanced to the next phase, or terminal (done/failed)."""
        # Step 1 only acts on parsing → phase_b. Other states either belong
        # to Step 2 (uploading / assembling / queued) or downstream phases
        # (Step 3+); we leave those as no-ops so jobs in those states sit
        # quietly until the responsible code lands. For 'queued' (a job that
        # finished assembling but Phase A hasn't started) we promote to
        # 'parsing' first.
        if status == 'queued':
            # Step 2's POST /complete will set status='queued' after the
            # multipart upload assembles; we promote it to 'parsing' so
            # Phase A picks it up on the next iteration.
            await self._update_status(job_id, 'parsing')
            return

        if status == 'parsing':
            await self._do_phase_a(job_id)
            return

        if status == 'phase_b':
            await self._do_phase_b(job_id)
            return

        if status == 'phase_c':
            await self._do_phase_c(job_id)
            return

        if status == 'phase_d':
            await self._do_phase_d(job_id)
            return

        # uploading / assembling are owned by HTTP endpoints (Step 2). The
        # worker doesn't drive these transitions.
        return

    async def _do_phase_a(self, job_id: str) -> None:
        # Lazy import to avoid blob_store importing at module load (it reads
        # JWT_SECRET at first put-presign — fine here but symmetric with the
        # rest of the data layer).
        from .blob_store import get_blob_store
        store = get_blob_store()
        try:
            summary = await run_phase_a(job_id, store)
        except ImportFormatError as e:
            logger.warning('import job %s: phase A failed: %s', job_id, e)
            await self._mark_failed(job_id, f'phase A: {e}')
            return
        except Exception as e:
            logger.exception(
                'import job %s: unexpected phase A error: %s', job_id, e
            )
            await self._mark_failed(job_id, f'phase A unexpected: {e}')
            return

        # Persist counters + advance status in one UPDATE.
        async with SessionLocal() as session:
            next_version = (await session.execute(
                text("SELECT nextval('global_change_seq')")
            )).scalar_one()
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id)
                .values(
                    status='phase_b',
                    total_rows=summary['total_rows'],
                    total_blobs=summary['total_blobs'],
                    total_bytes=summary['total_bytes'],
                    processed_bytes=summary['total_bytes'],
                    updated_at=datetime.now(timezone.utc),
                    # Bump version so subscribers see a new event.
                    version=next_version,
                )
            )
            await session.commit()
        logger.info(
            'import job %s: phase A done (rows=%d blobs=%d bytes=%d)',
            job_id, summary['total_rows'], summary['total_blobs'],
            summary['total_bytes'],
        )

    async def _do_phase_b(self, job_id: str) -> None:
        """Run Phase B (structural tables → PG with LWW), then advance to
        phase_c. Errors mark the job failed with `phase B: <msg>` so the
        UI can render a meaningful banner.

        Idempotency: ON CONFLICT DO UPDATE WHERE LWW means re-running the
        whole phase after a crash is safe — already-written rows stay,
        re-attempted rows match the same LWW guard.
        """
        try:
            summary = await run_phase_b(job_id)
        except ImportFormatError as e:
            logger.warning('import job %s: phase B failed: %s', job_id, e)
            await self._mark_failed(job_id, f'phase B: {e}')
            return
        except Exception as e:
            logger.exception(
                'import job %s: unexpected phase B error: %s', job_id, e
            )
            await self._mark_failed(job_id, f'phase B unexpected: {e}')
            return

        # Advance status → phase_c with a fresh version so the WS event
        # caused by the transition is distinct from the per-table progress
        # events that fired during run_phase_b.
        async with SessionLocal() as session:
            next_version = (await session.execute(
                text("SELECT nextval('global_change_seq')")
            )).scalar_one()
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id)
                .values(
                    status='phase_c',
                    updated_at=datetime.now(timezone.utc),
                    version=next_version,
                )
            )
            await session.commit()
        logger.info(
            'import job %s: phase B done (per_table=%s total_processed=%d)',
            job_id,
            summary.get('per_table_processed'),
            summary.get('total_processed', 0),
        )

    async def _do_phase_c(self, job_id: str) -> None:
        """Run Phase C (messages.ndjson → PG with LWW + dead_letter for
        FK orphans), then advance to phase_d.

        Idempotency: per-batch commit + LWW UPSERT means re-running after a
        crash is safe — already-written rows skip via LWW WHERE clause,
        already-dead-lettered orphans get appended again on retry (the
        dead_letter list grows; that's fine, dedup is a future concern,
        retries are rare enough for the count to stay small).
        """
        try:
            summary = await run_phase_c(job_id)
        except ImportFormatError as e:
            logger.warning('import job %s: phase C failed: %s', job_id, e)
            await self._mark_failed(job_id, f'phase C: {e}')
            return
        except Exception as e:
            logger.exception(
                'import job %s: unexpected phase C error: %s', job_id, e
            )
            await self._mark_failed(job_id, f'phase C unexpected: {e}')
            return

        # Advance status → phase_d with a fresh version so the WS event
        # for the transition is distinct from the per-batch progress
        # events that fired during run_phase_c.
        async with SessionLocal() as session:
            next_version = (await session.execute(
                text("SELECT nextval('global_change_seq')")
            )).scalar_one()
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id)
                .values(
                    status='phase_d',
                    updated_at=datetime.now(timezone.utc),
                    version=next_version,
                )
            )
            await session.commit()
        logger.info(
            'import job %s: phase C done '
            '(processed=%d orphans=%d pending_blob=%d batches=%d)',
            job_id,
            summary.get('total_processed', 0),
            summary.get('orphan_count', 0),
            summary.get('pending_blob_count', 0),
            summary.get('batches', 0),
        )

    async def _do_phase_d(self, job_id: str) -> None:
        """Run Phase D (extract attachments → BlobStore for ≥ 64KB / leave
        inline for < 64KB), then advance to done.

        Idempotency: partial-index scan only finds rows still flagged TRUE;
        already-rewritten rows (from a previous crashed run) have ref
        envelopes that `_walk_attachments` skips naturally. `BlobStore.put`
        is idempotent on sha256 so a half-uploaded attachment from the
        previous run doesn't double-bill.
        """
        try:
            summary = await run_phase_d(job_id)
        except ImportFormatError as e:
            logger.warning('import job %s: phase D failed: %s', job_id, e)
            await self._mark_failed(job_id, f'phase D: {e}')
            return
        except Exception as e:
            logger.exception(
                'import job %s: unexpected phase D error: %s', job_id, e
            )
            await self._mark_failed(job_id, f'phase D unexpected: {e}')
            return

        # Advance status → done with a fresh version so the WS event for
        # the transition is distinct from the per-N-attachment progress
        # events that fired during run_phase_d.
        async with SessionLocal() as session:
            next_version = (await session.execute(
                text("SELECT nextval('global_change_seq')")
            )).scalar_one()
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id)
                .values(
                    status='done',
                    updated_at=datetime.now(timezone.utc),
                    version=next_version,
                )
            )
            await session.commit()

        # Best-effort final WS publish so the frontend immediately sees the
        # 'done' status without waiting for the next poll. Reuses the
        # generic envelope shape — frontend's realtime handler decodes it
        # via the same code path used for status changes.
        try:
            from realtime import broker as realtime_broker
            async with SessionLocal() as session:
                refreshed = (await session.execute(
                    select(ImportJob).where(ImportJob.id == job_id)
                )).scalar_one()
                session.expunge(refreshed)
            await realtime_broker.publish(
                refreshed.user_id,
                {
                    'type': 'event',
                    'table': 'import_jobs',
                    'op': 'put',
                    'id': refreshed.id,
                    'rev': (
                        int(refreshed.version)
                        if refreshed.version is not None else 0
                    ),
                    'row': refreshed._envelope(),
                },
            )
        except Exception as e:  # pragma: no cover — broker failure shouldn't undo done
            logger.warning(
                'phase D / job %s: failed to publish done event: %s',
                job_id, e,
            )

        logger.info(
            'import job %s: phase D done '
            '(rows=%d attachments=%d uploaded=%d inline_kept=%d failed=%d)',
            job_id,
            summary.get('rows_processed', 0),
            summary.get('attachments_processed', 0),
            summary.get('attachments_uploaded', 0),
            summary.get('attachments_inline_kept', 0),
            summary.get('attachments_failed', 0),
        )

    async def _update_status(self, job_id: str, new_status: str) -> None:
        async with SessionLocal() as session:
            next_version = (await session.execute(
                text("SELECT nextval('global_change_seq')")
            )).scalar_one()
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id)
                .values(
                    status=new_status,
                    updated_at=datetime.now(timezone.utc),
                    version=next_version,
                )
            )
            await session.commit()

    async def _mark_failed(self, job_id: str, message: str) -> None:
        async with SessionLocal() as session:
            next_version = (await session.execute(
                text("SELECT nextval('global_change_seq')")
            )).scalar_one()
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id)
                .values(
                    status='failed',
                    error_message=message,
                    updated_at=datetime.now(timezone.utc),
                    version=next_version,
                )
            )
            await session.commit()
