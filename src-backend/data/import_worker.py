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


# ---- phase stubs (Steps 4 / 5 fill these in) -------------------------------


async def run_phase_c(job_id: str) -> None:
    raise NotImplementedError(
        f'phase C not implemented (Step 4); job {job_id} cannot advance'
    )


async def run_phase_d(job_id: str) -> None:
    raise NotImplementedError(
        f'phase D not implemented (Step 5); job {job_id} cannot advance'
    )


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

        if status in ('phase_c', 'phase_d'):
            # Steps 4 / 5 plug stub handlers. We leave the row alone —
            # don't crash the worker loop just because later phases aren't
            # implemented yet.
            logger.debug(
                'import job %s in %s: handler not implemented yet (Step 4+)',
                job_id, status,
            )
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
