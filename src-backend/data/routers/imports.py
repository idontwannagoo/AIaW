"""Stage 4.5 / Step 2 — ImportJob multipart upload endpoints.

Five public endpoints + one internal LocalFs-only PUT shim:

  POST   /api/v1/import/jobs                         create job + multipart
  POST   /api/v1/import/jobs/{id}/parts/{n}          mint presigned PUT URL
  POST   /api/v1/import/jobs/{id}/complete           finalize multipart → queued
  GET    /api/v1/import/jobs/{id}                    status snapshot
  GET    /api/v1/import/jobs?status=active           current user's active job
  DELETE /api/v1/import/jobs/{id}                    abort + cancelled

  PUT    /api/v1/_internal/multipart/{up_id}/part/{n}   LocalFs only — receives
                                                          part bytes after HMAC
                                                          sig verify

Cross-user policy: every endpoint that takes `{job_id}` 404-masks on a
non-owner access (we never reveal job existence in another tenant). The
multipart_upload_id is *never* returned to a non-owner — endpoints look it up
by job ownership before invoking BlobStore.

Active-job uniqueness is enforced at the DB layer by the partial unique index
`uq_import_jobs_active_per_user` (see migration `a91f3c5e8d2b`). When that
constraint fires we translate to a 409 with the current active job's snapshot
in the body so the client can offer "resume / cancel" UX.

Step 3 will add `imported_from_job_id` columns to all server-routed tables and
DELETE will gain a soft-delete pass for partially-imported rows. For Step 2
the DELETE path only aborts the multipart and flips status; the TODO is
called out inline.

Note on parts contract: `parts` is `[{part_number, etag}]`, sorted +
contiguous 1..N. We validate before calling BlobStore.complete because a
half-finished assembly leaves stray staging files; better to return 400 fast.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from realtime import broker

from ..auth import current_user
from ..blob_store import (
    BLOB_PRESIGN_TTL_SECONDS,
    LocalFsBlobStore,
    get_blob_store,
    verify_multipart_part_signature,
)
from ..db import get_session
from ..import_worker import SERVER_ROUTED_TABLES, get_worker
from ..models.artifact import Artifact
from ..models.assistant import Assistant
from ..models.avatar_image import AvatarImage
from ..models.dialog import Dialog
from ..models.import_job import ImportJob, NON_TERMINAL_STATUSES
from ..models.installed_plugin import InstalledPlugin
from ..models.item import Item
from ..models.message import Message
from ..models.provider import Provider
from ..models.reactive import Reactive
from ..models.user import User
from ..models.workspace import Workspace

logger = logging.getLogger('aiaw.backend.imports')

router = APIRouter(tags=['imports'])


# ---- helpers ---------------------------------------------------------------


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


def _absolute_base(request: Request) -> str:
    """Same X-Forwarded-* honoring scheme as blobs router. Northflank's TLS
    proxy injects these; without them the URL we mint comes back http:// and
    the browser blocks it as mixed content.
    """
    fwd_proto = request.headers.get('x-forwarded-proto')
    fwd_host = request.headers.get('x-forwarded-host')
    scheme = (fwd_proto or request.url.scheme).split(',')[0].strip()
    host = (fwd_host or request.url.netloc).split(',')[0].strip()
    return f'{scheme}://{host}'


def _job_data_snapshot(job: ImportJob) -> dict[str, Any]:
    """The `data` payload the frontend renders into the progress banner.
    Mirrors `ImportJob._envelope().data` so REST GETs and WS events have
    identical shapes — frontend code can treat both as the same row.
    """
    env = job._envelope()
    return env['data']


async def _load_job_for_user(
    session: AsyncSession, job_id: str, user_id: str
) -> ImportJob:
    """Fetch a job by id, enforcing ownership. 404-masks cross-user access
    (we hide existence rather than 403'ing — same template as blobs router)."""
    stmt = select(ImportJob).where(ImportJob.id == job_id)
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None or job.user_id != user_id:
        raise HTTPException(status_code=404, detail='not found')
    return job


async def _find_active_job(
    session: AsyncSession, user_id: str
) -> Optional[ImportJob]:
    stmt = (
        select(ImportJob)
        .where(
            ImportJob.user_id == user_id,
            ImportJob.status.in_(NON_TERMINAL_STATUSES),
        )
        .order_by(ImportJob.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ---- request / response shapes --------------------------------------------


class CreateJobRequest(BaseModel):
    file_size: int = Field(..., gt=0, description='total upload size in bytes')
    content_type: Optional[str] = None


class CreateJobResponse(BaseModel):
    job_id: str
    multipart_upload_id: str
    raw_object_key: str


class PartUrlResponse(BaseModel):
    upload_url: str
    part_number: int
    expires_at: int


class CompletePart(BaseModel):
    part_number: int = Field(..., ge=1)
    etag: str


class CompleteJobRequest(BaseModel):
    parts: list[CompletePart]


class JobStatusResponse(BaseModel):
    """REST-shape mirror of the server-routed envelope `data` payload."""
    job_id: str
    status: str
    multipart_upload_id: Optional[str] = None
    raw_object_key: Optional[str] = None
    total_bytes: Optional[int] = None
    processed_bytes: int
    total_rows: Optional[int] = None
    processed_rows: int
    total_blobs: Optional[int] = None
    processed_blobs: int
    error_message: Optional[str] = None
    dead_letter: list[Any] = []
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _to_status(job: ImportJob) -> JobStatusResponse:
    snap = _job_data_snapshot(job)
    return JobStatusResponse(
        job_id=job.id,
        status=snap['status'],
        multipart_upload_id=snap['multipart_upload_id'],
        raw_object_key=snap['raw_object_key'],
        total_bytes=snap['total_bytes'],
        processed_bytes=snap['processed_bytes'],
        total_rows=snap['total_rows'],
        processed_rows=snap['processed_rows'],
        total_blobs=snap['total_blobs'],
        processed_blobs=snap['processed_blobs'],
        error_message=snap['error_message'],
        dead_letter=snap['dead_letter'],
        created_at=snap['created_at'],
        updated_at=snap['updated_at'],
    )


# ---- POST /api/v1/import/jobs ----------------------------------------------


@router.post(
    '/api/v1/import/jobs',
    response_model=CreateJobResponse,
    status_code=201,
)
async def create_import_job(
    body: CreateJobRequest,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
) -> CreateJobResponse:
    """Create a new ImportJob in status='uploading' + open a multipart upload
    against BlobStore. The (job_id, multipart_upload_id, raw_object_key) tuple
    is returned so the client can immediately request part URLs.

    Uniqueness: the partial unique index permits at most one non-terminal job
    per user. If the client races a second create, we return 409 with the
    *existing* active job's status snapshot so the UI can offer "resume" /
    "cancel and restart" instead of a generic error.
    """
    # Pre-flight: short-circuit obvious "already have one" cases without going
    # through the multipart create (which would leave a dangling upload that
    # the caller can't tie back to anything). The partial unique index is
    # still authoritative — this just saves a BlobStore round-trip.
    existing = await _find_active_job(session, user_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                'message': 'an active import job already exists',
                'job': _to_status(existing).model_dump(),
            },
        )

    job_id = str(uuid.uuid4())
    raw_object_key = f'imports/{user_id}/{job_id}.json'
    blob_store = get_blob_store()
    multipart_upload_id = await blob_store.create_multipart_upload(raw_object_key)

    job = ImportJob(
        id=job_id,
        user_id=user_id,
        status='uploading',
        multipart_upload_id=multipart_upload_id,
        raw_object_key=raw_object_key,
        total_bytes=body.file_size,
    )
    session.add(job)
    try:
        await session.commit()
    except IntegrityError:
        # Race with another request that won the partial-unique-index lottery
        # in the gap between our pre-flight and our INSERT. Roll back, abort
        # the orphan multipart, and return the winner's snapshot.
        await session.rollback()
        try:
            await blob_store.abort_multipart_upload(multipart_upload_id)
        except Exception as e:  # pragma: no cover — best-effort cleanup
            logger.warning(
                'failed to abort orphan multipart %s after race: %s',
                multipart_upload_id, e,
            )
        winner = await _find_active_job(session, user_id)
        if winner is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    'message': 'an active import job already exists',
                    'job': _to_status(winner).model_dump(),
                },
            )
        raise HTTPException(
            status_code=409,
            detail='active import job conflict',
        )

    return CreateJobResponse(
        job_id=job_id,
        multipart_upload_id=multipart_upload_id,
        raw_object_key=raw_object_key,
    )


# ---- POST /api/v1/import/jobs/{id}/parts/{n} -------------------------------


@router.post(
    '/api/v1/import/jobs/{job_id}/parts/{part_number}',
    response_model=PartUrlResponse,
)
async def get_part_url(
    job_id: str,
    part_number: int,
    request: Request,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
) -> PartUrlResponse:
    if part_number < 1:
        raise HTTPException(
            status_code=400, detail='part_number must be >= 1',
        )
    job = await _load_job_for_user(session, job_id, user_id)
    if job.status != 'uploading':
        # Once we leave the uploading phase the multipart is either being
        # assembled (no more parts accepted) or already done. 409 conveys
        # "the resource is in the wrong state for this op" per HTTP semantics.
        raise HTTPException(
            status_code=409,
            detail=f'job is in status {job.status!r}, not uploading',
        )
    if not job.multipart_upload_id:
        # Should never happen — uploading status implies create succeeded.
        raise HTTPException(
            status_code=500,
            detail='job has no multipart_upload_id',
        )

    import time
    base = _absolute_base(request)
    blob_store = get_blob_store()
    upload_url = await blob_store.generate_part_url(
        job.multipart_upload_id,
        part_number,
        ttl_seconds=BLOB_PRESIGN_TTL_SECONDS,
        base_url=base,
    )
    expires_at = int(time.time()) + BLOB_PRESIGN_TTL_SECONDS
    return PartUrlResponse(
        upload_url=upload_url,
        part_number=part_number,
        expires_at=expires_at,
    )


# ---- POST /api/v1/import/jobs/{id}/complete --------------------------------


def _validate_parts_contiguous(parts: list[CompletePart]) -> None:
    """Parts must be 1-indexed, contiguous, no duplicates. We sort by
    part_number first so the client can pass them in any order.
    """
    if not parts:
        raise HTTPException(status_code=400, detail='parts list is empty')
    nums = sorted(p.part_number for p in parts)
    if len(set(nums)) != len(nums):
        raise HTTPException(
            status_code=400,
            detail=f'duplicate part_number in parts list: {nums}',
        )
    if nums[0] != 1:
        raise HTTPException(
            status_code=400,
            detail=f'parts must start at 1, got {nums[0]}',
        )
    expected = list(range(1, len(nums) + 1))
    if nums != expected:
        raise HTTPException(
            status_code=400,
            detail=(
                f'parts must be contiguous 1..N, got {nums} '
                f'(expected {expected})'
            ),
        )


@router.post(
    '/api/v1/import/jobs/{job_id}/complete',
    response_model=JobStatusResponse,
)
async def complete_import_job(
    job_id: str,
    body: CompleteJobRequest,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
) -> JobStatusResponse:
    """Assemble the multipart upload + advance state to `queued` so the worker
    picks it up on next tick. We bounce status uploading → assembling →
    queued so a slow assemble shows the right banner phase.
    """
    _validate_parts_contiguous(body.parts)

    job = await _load_job_for_user(session, job_id, user_id)
    if job.status != 'uploading':
        raise HTTPException(
            status_code=409,
            detail=f'job is in status {job.status!r}, not uploading',
        )
    if not job.multipart_upload_id or not job.raw_object_key:
        raise HTTPException(
            status_code=500,
            detail='job missing multipart_upload_id or raw_object_key',
        )

    # 1. Flip status → assembling so a watcher sees we're past uploading even
    # if the assembly step takes a few seconds for a 200MB upload.
    await session.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id)
        .values(status='assembling')
    )
    await session.commit()

    blob_store = get_blob_store()
    parts_payload = [
        {'part_number': p.part_number, 'etag': p.etag}
        for p in body.parts
    ]
    try:
        final_key = await blob_store.complete_multipart_upload(
            job.multipart_upload_id,
            parts_payload,
            job.raw_object_key,
        )
    except FileNotFoundError as e:
        # Missing part bytes / unknown upload_id — flip back to uploading so
        # the client can retry the missing parts, *or* it can DELETE and
        # restart. We don't auto-cancel; either choice has UX trade-offs and
        # the client knows which.
        await session.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id)
            .values(status='uploading')
        )
        await session.commit()
        raise HTTPException(status_code=400, detail=f'complete failed: {e}')
    except Exception as e:  # pragma: no cover — defensive
        logger.exception('complete_multipart_upload error: %s', e)
        await session.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id)
            .values(status='failed', error_message=f'complete: {e}')
        )
        await session.commit()
        raise HTTPException(
            status_code=500, detail=f'multipart complete failed: {e}',
        )

    # 2. Advance status → queued and write final raw_object_key (LocalFs uses
    # the canonical sha256 path; S3 stays on the original key the multipart
    # was created against).
    await session.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id)
        .values(status='queued', raw_object_key=final_key)
    )
    await session.commit()

    # 3. Re-read for snapshot + WS publish.
    refreshed = await _load_job_for_user(session, job_id, user_id)
    await broker.publish(user_id, {
        'type': 'event',
        'table': 'import_jobs',
        'op': 'put',
        'id': refreshed.id,
        'rev': int(refreshed.version) if refreshed.version is not None else 0,
        'row': refreshed._envelope(),
    })

    # 4. Wake the worker so the queued → parsing transition fires immediately
    # instead of waiting up to POLL_INTERVAL_SECONDS.
    try:
        get_worker().notify_pending()
    except Exception as e:  # pragma: no cover — worker not running shouldn't 500
        logger.warning('worker.notify_pending failed: %s', e)

    return _to_status(refreshed)


# ---- GET /api/v1/import/jobs/{id} ------------------------------------------


@router.get(
    '/api/v1/import/jobs/{job_id}',
    response_model=JobStatusResponse,
)
async def get_import_job(
    job_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
) -> JobStatusResponse:
    job = await _load_job_for_user(session, job_id, user_id)
    return _to_status(job)


# ---- GET /api/v1/import/jobs?status=active ---------------------------------


@router.get(
    '/api/v1/import/jobs',
    response_model=list[JobStatusResponse],
)
async def list_import_jobs(
    status: str = Query('active', description='only "active" supported'),
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[JobStatusResponse]:
    """Return the user's active job (≤ 1, by partial unique index).

    Stage 4.5 only needs the active filter — the multi-job history view is a
    Step 7 frontend concern. Reject other status filters explicitly so a
    typo'd `status=all` doesn't silently return [] and confuse the caller.
    """
    if status != 'active':
        raise HTTPException(
            status_code=400,
            detail=f"only status='active' supported, got {status!r}",
        )
    job = await _find_active_job(session, user_id)
    return [_to_status(job)] if job is not None else []


# ---- DELETE /api/v1/import/jobs/{id} ---------------------------------------


# Map backend table name → (Model, event-emit-table-name on wire). The
# wire name matches what the existing per-table router publishes — frontend
# realtime handlers decode by table name. KV-PK tables (reactives /
# installed_plugins) do not have an `id` column on the row but they DO
# have a stable composite key (user_id, key); we publish their tombstone
# events with `id` = the `key` field so the wire shape stays uniform with
# id-PK tables. Frontend realtime decoder for those tables already
# accepts `id` as the cache key.
_CASCADE_TABLE_MAP: dict[str, tuple[Any, str]] = {
    'providers': (Provider, 'providers'),
    'reactives': (Reactive, 'reactives'),
    'assistants': (Assistant, 'assistants'),
    # NOTE: wire table names match what each per-table router's _to_event
    # publishes (see routers/installed_plugins.py / routers/avatar_images.py
    # — both use snake_case). Frontend realtime decoder dispatches on this
    # exact string; mismatch would silently drop the cascade tombstone for
    # those subscribers.
    'installed_plugins': (InstalledPlugin, 'installed_plugins'),
    'avatar_images': (AvatarImage, 'avatar_images'),
    'messages': (Message, 'messages'),
    'items': (Item, 'items'),
    'artifacts': (Artifact, 'artifacts'),
    'dialogs': (Dialog, 'dialogs'),
    'workspaces': (Workspace, 'workspaces'),
}


def _row_id_for_event(row: Any, backend_table: str) -> str:
    """Pull the wire-side `id` for a cascade tombstone event.

    For KV-PK tables (reactives / installed_plugins) the natural identity
    is `key`; the frontend cache also keys on `key` for those, so we put
    that into the event's `id` field. For id-PK tables it's just `row.id`.
    """
    if backend_table in ('reactives', 'installed_plugins'):
        return getattr(row, 'key', '')
    return row.id


def _cascade_tombstone_event(row: Any, backend_table: str) -> dict[str, Any]:
    """Build a `delete` WS event for a cascade-soft-deleted row, mirroring
    the per-table router `_to_event(deleted=True)` shape so the frontend
    realtime handler can dispatch by `e.table` without knowing the row
    came from an import-cancel cascade specifically.
    """
    _, wire_table = _CASCADE_TABLE_MAP[backend_table]
    return {
        'type': 'event',
        'table': wire_table,
        'op': 'delete',
        'id': _row_id_for_event(row, backend_table),
        'rev': int(row.version) if row.version is not None else 0,
        'row': None,
    }


@router.delete(
    '/api/v1/import/jobs/{job_id}',
    response_model=JobStatusResponse,
)
async def cancel_import_job(
    job_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
) -> JobStatusResponse:
    """Abort the multipart upload + flip status to 'cancelled' + cascade-
    soft-delete every row tagged with this job_id.

    Idempotency: aborting an already-cancelled job is a no-op (returns the
    snapshot). The job row itself is NOT deleted — soft transition keeps
    history visible to the user and frees the partial unique index slot
    so a fresh create_import_job() succeeds immediately.

    Cascade-soft-delete (Stage 4.5 / Step 3): for each server-routed table,
    UPDATE rows WHERE imported_from_job_id = :job_id AND user_id = :user
    AND deleted_at IS NULL → set deleted_at=now() and bump version to a
    shared `cascade_version`. All tombstoned rows share that version so
    clients treat them as a single batch (consistent with how
    workspaces.py cascade publishes events). Per-row WS `delete` events
    fire after commit so live tabs roll back the partial import without a
    refresh.
    """
    job = await _load_job_for_user(session, job_id, user_id)
    if job.status == 'cancelled':
        return _to_status(job)

    blob_store = get_blob_store()
    if job.multipart_upload_id:
        try:
            await blob_store.abort_multipart_upload(job.multipart_upload_id)
        except Exception as e:  # pragma: no cover — best-effort cleanup
            logger.warning(
                'abort_multipart_upload(%s) failed during cancel: %s',
                job.multipart_upload_id, e,
            )

    # ---- cascade soft-delete of imported rows --------------------------
    # Collect tombstoned rows per table so we can publish per-row events
    # after commit. We use a single shared cascade_version (same template
    # as workspaces.py): all rows tombstoned in this transaction land on
    # the same monotonic point in the per-user since timeline.
    cascade_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()

    cascaded: dict[str, list[Any]] = {}
    for backend_table in SERVER_ROUTED_TABLES:
        Model, _ = _CASCADE_TABLE_MAP[backend_table]
        stmt = (
            update(Model)
            .where(
                Model.imported_from_job_id == job_id,
                Model.user_id == user_id,
                Model.deleted_at.is_(None),
            )
            .values(version=cascade_version, deleted_at=text('now()'))
            .returning(Model)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if rows:
            cascaded[backend_table] = rows

    # Flip the job status itself.
    await session.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id)
        .values(status='cancelled')
    )
    await session.commit()

    # Publish per-table cascade tombstone events (children before parents
    # mirror workspaces.py ordering). The dict iteration order matches
    # SERVER_ROUTED_TABLES so subscribers see leaf tables flush first
    # — frontend cleanup is already idempotent per row, but ordering
    # keeps debug log output consistent across runs.
    for backend_table, rows in cascaded.items():
        for row in rows:
            await broker.publish(
                user_id, _cascade_tombstone_event(row, backend_table),
            )

    refreshed = await _load_job_for_user(session, job_id, user_id)
    await broker.publish(user_id, {
        'type': 'event',
        'table': 'import_jobs',
        'op': 'put',
        'id': refreshed.id,
        'rev': int(refreshed.version) if refreshed.version is not None else 0,
        'row': refreshed._envelope(),
    })
    return _to_status(refreshed)


# ---- PUT /api/v1/_internal/multipart/{up_id}/part/{n}  (LocalFs only) -----
#
# This endpoint receives the actual part bytes for the LocalFs simulated
# multipart backend. It is *not* part of the public API contract — it exists
# because LocalFs has nowhere else to put presigned-routed bytes. The HMAC
# signature in the URL (sig + exp) is the only credential; no bearer required
# (mirrors how /api/v1/blobs/{sha256}/data works for downloads).
#
# When BLOB_STORE_KIND=s3 the client uploads parts directly to S3 and this
# endpoint is unused; we still mount it to keep the LocalFs path drop-in.


@router.put('/api/v1/_internal/multipart/{upload_id}/part/{part_number}')
async def _put_multipart_part(
    upload_id: str,
    part_number: int,
    request: Request,
    exp: int = Query(...),
    sig: str = Query(...),
) -> dict[str, Any]:
    import time
    if exp < int(time.time()):
        raise HTTPException(status_code=403, detail='url expired')
    if not verify_multipart_part_signature(upload_id, part_number, exp, sig):
        raise HTTPException(status_code=403, detail='bad signature')
    if part_number < 1:
        raise HTTPException(
            status_code=400, detail='part_number must be >= 1',
        )

    blob_store = get_blob_store()
    if not isinstance(blob_store, LocalFsBlobStore):
        # Non-LocalFs backends use real S3 presigned URLs that hit the bucket
        # directly; this endpoint should never be invoked.
        raise HTTPException(
            status_code=404,
            detail='internal multipart shim disabled for non-LocalFs backend',
        )

    # Read the entire part body. Per S3 / our protocol, parts are ≤ 8MB on the
    # client side — we don't enforce a hard cap here because the client
    # already sized them, but a malicious client could send a huge part. The
    # 100MB single-blob cap (BLOB_MAX_UPLOAD_BYTES) is a fine ceiling.
    from ..blob_store import BLOB_MAX_UPLOAD_BYTES
    body = await request.body()
    if len(body) == 0:
        raise HTTPException(status_code=400, detail='empty part body')
    if len(body) > BLOB_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f'part exceeds max size {BLOB_MAX_UPLOAD_BYTES} bytes',
        )

    try:
        etag = await blob_store.write_part(upload_id, part_number, body)
    except FileNotFoundError as e:
        # upload_id never created or already aborted.
        raise HTTPException(status_code=404, detail=str(e))
    return {'etag': etag, 'part_number': part_number, 'size': len(body)}
