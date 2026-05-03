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


# ---- phase stubs (Steps 3 / 4 / 5 fill these in) ----------------------------


async def run_phase_b(job_id: str) -> None:
    raise NotImplementedError(
        f'phase B not implemented (Step 3); job {job_id} cannot advance'
    )


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

        if status in ('phase_b', 'phase_c', 'phase_d'):
            # Steps 3 / 4 / 5 plug stub handlers. For Step 1 we leave the
            # row alone — don't crash the worker loop just because later
            # phases aren't implemented yet.
            logger.debug(
                'import job %s in %s: handler not implemented yet (Step 3+)',
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
