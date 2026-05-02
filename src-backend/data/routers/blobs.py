"""Blob upload / download endpoints — Stage 4 硬前置 2.

Three real surfaces in this batch:

- POST /api/v1/blobs                  multipart upload
- GET  /api/v1/blobs/{sha256}/data    presign-validated bytes (no auth header)
- GET  /api/v1/blobs/{sha256}         auth + ref check, returns metadata
- HEAD /api/v1/blobs/{sha256}         auth + ref check, headers-only metadata
- DELETE /api/v1/blobs/{sha256}/refs  drop *this user's* ref (bytes stay until GC)

Why a separate `/data` path for presigned reads?
- Browsers cache by URL. If the auth GET and the presigned GET share the same
  path, varying-by-cookie / Authorization caching gets ugly. Splitting `/data`
  off keeps the auth path small and the presign path purely query-string keyed.
- It also signals intent: `GET /api/v1/blobs/<sha>` returns *metadata JSON*
  (size, content_type, ref status); `/data` returns the actual bytes.

The presigned URL doesn't bind a user_id. Once a user has put a blob, they
can hand its presigned URL to their browser; the URL is a bearer token for
those bytes, expires in 1h, can't be tampered with (HMAC-SHA256 over
`sha256|exp` using JWT_SECRET as key).

Phase D of Stage 4.5 ImportJob worker will call POST /api/v1/blobs internally
with the user's id — same code path, same dedup behavior, no special endpoint.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..blob_store import (
    BLOB_MAX_UPLOAD_BYTES,
    BLOB_PRESIGN_TTL_SECONDS,
    get_blob_store,
    verify_blob_signature,
)
from ..db import get_session
from ..models.blob import Blob, BlobRef
from ..models.user import User

router = APIRouter(prefix='/api/v1/blobs', tags=['blobs'])


_DEFAULT_CT = 'application/octet-stream'


def _hex_sha256(s: str) -> bool:
    return len(s) == 64 and all(c in '0123456789abcdef' for c in s)


def _absolute_base(request: Request) -> str:
    """Build `<scheme>://<host>` for the current request, honoring X-Forwarded-*
    headers. Northflank fronts us with a TLS-terminating proxy that injects
    these; without honoring them the URL we mint comes back as plain http://
    and the browser blocks it as mixed content from the HTTPS frontend.
    """
    fwd_proto = request.headers.get('x-forwarded-proto')
    fwd_host = request.headers.get('x-forwarded-host')
    scheme = (fwd_proto or request.url.scheme).split(',')[0].strip()
    host = (fwd_host or request.url.netloc).split(',')[0].strip()
    return f'{scheme}://{host}'


# ---- response shapes --------------------------------------------------------


class BlobRefEnvelope(BaseModel):
    """Embeddable ref envelope. This is what Stage 4 主体批次的 row data
    fields will store in lieu of the actual bytes once a blob crosses the
    BLOB_INLINE_MAX_BYTES threshold."""
    type: str = 'ref'
    sha256: str
    size: int
    content_type: str
    url: str


class BlobMetadata(BaseModel):
    sha256: str
    size: int
    content_type: str


class UploadBlobResponse(BaseModel):
    sha256: str
    size: int
    content_type: str
    url: str
    ref: BlobRefEnvelope
    deduped: bool  # True iff bytes were already on disk for this sha256


# ---- helpers ----------------------------------------------------------------


async def _user_owns_blob(
    session: AsyncSession, user_id: str, sha256: str
) -> bool:
    stmt = select(BlobRef).where(
        BlobRef.user_id == user_id, BlobRef.sha256 == sha256
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _get_blob(session: AsyncSession, sha256: str) -> Optional[Blob]:
    stmt = select(Blob).where(Blob.sha256 == sha256)
    return (await session.execute(stmt)).scalar_one_or_none()


def _build_envelope(blob: Blob, url: str) -> BlobRefEnvelope:
    return BlobRefEnvelope(
        sha256=blob.sha256,
        size=blob.size,
        content_type=blob.content_type,
        url=url,
    )


# ---- POST upload ------------------------------------------------------------


@router.post('', response_model=UploadBlobResponse, status_code=201)
async def upload_blob(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UploadBlobResponse:
    # Read the upload in chunks so a 100MB POST doesn't load entirely into
    # memory before we even compute the hash. python-multipart already buffers
    # to a SpooledTemporaryFile, so this is a streamed read either way.
    h = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
        if total > BLOB_MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f'blob exceeds max size {BLOB_MAX_UPLOAD_BYTES} bytes',
            )
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail='empty body')
    data = b''.join(chunks)
    sha256 = h.hexdigest()

    content_type = (
        file.content_type or _DEFAULT_CT
    ).split(';')[0].strip() or _DEFAULT_CT
    if len(content_type) > 255:
        content_type = content_type[:255]

    blob_store = get_blob_store()

    # If we already have a row, the bytes are already on disk (we never delete
    # bytes without dropping the row in the same tx). Treat as dedup hit.
    existing = await _get_blob(session, sha256)
    if existing is None:
        storage_key = await blob_store.put(sha256, data, content_type)
        # ON CONFLICT DO NOTHING: if a concurrent upload from another worker
        # raced us to INSERT this sha256, we lose harmlessly — bytes are
        # identical (same hash) and the row will exist either way.
        stmt = pg_insert(Blob).values(
            sha256=sha256,
            size=total,
            content_type=content_type,
            storage_key=storage_key,
        ).on_conflict_do_nothing(index_elements=[Blob.sha256])
        await session.execute(stmt)
        # Re-read so we get the row that actually persisted (ours or the
        # racer's; either one is correct, but we need it for the envelope).
        existing = await _get_blob(session, sha256)
        if existing is None:  # pragma: no cover — unreachable barring DB issue
            raise HTTPException(status_code=500, detail='blob row not persisted')
        deduped = False
    else:
        deduped = True

    # Upsert the per-user ref. last_seen_at bumps on every upload, which lets
    # GC tell "really untouched" blobs apart from "actively re-uploaded".
    ref_stmt = pg_insert(BlobRef).values(
        user_id=user.id,
        sha256=sha256,
    ).on_conflict_do_update(
        index_elements=[BlobRef.user_id, BlobRef.sha256],
        set_={'last_seen_at': text('now()')},
    )
    await session.execute(ref_stmt)
    await session.commit()

    base = _absolute_base(request)
    url = blob_store.presign_get_url(
        sha256, ttl_seconds=BLOB_PRESIGN_TTL_SECONDS, base_url=base
    )
    envelope = _build_envelope(existing, url)
    return UploadBlobResponse(
        sha256=existing.sha256,
        size=existing.size,
        content_type=existing.content_type,
        url=url,
        ref=envelope,
        deduped=deduped,
    )


# ---- GET metadata + signed URL ----------------------------------------------


@router.get('/{sha256}', response_model=BlobRefEnvelope)
async def get_blob_metadata(
    sha256: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BlobRefEnvelope:
    if not _hex_sha256(sha256):
        raise HTTPException(status_code=400, detail='invalid sha256')
    blob = await _get_blob(session, sha256)
    if blob is None:
        raise HTTPException(status_code=404, detail='not found')
    if not await _user_owns_blob(session, user.id, sha256):
        # Hide existence from non-owners. 404 (not 403) so probing a sha256
        # space doesn't reveal "this hash exists in the system".
        raise HTTPException(status_code=404, detail='not found')

    base = _absolute_base(request)
    url = get_blob_store().presign_get_url(
        sha256, ttl_seconds=BLOB_PRESIGN_TTL_SECONDS, base_url=base
    )
    return _build_envelope(blob, url)


@router.head('/{sha256}')
async def head_blob(
    sha256: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not _hex_sha256(sha256):
        raise HTTPException(status_code=400, detail='invalid sha256')
    blob = await _get_blob(session, sha256)
    if blob is None:
        raise HTTPException(status_code=404, detail='not found')
    if not await _user_owns_blob(session, user.id, sha256):
        raise HTTPException(status_code=404, detail='not found')
    return Response(
        status_code=200,
        headers={
            'Content-Type': blob.content_type,
            'Content-Length': str(blob.size),
            'X-Blob-Sha256': blob.sha256,
        },
    )


# ---- presigned bytes --------------------------------------------------------


@router.get('/{sha256}/data')
async def get_blob_data(
    sha256: str,
    exp: int = Query(...),
    sig: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return raw bytes for a sha256, validated by HMAC sig. No bearer token
    required: the URL itself is the credential, generated by upload_blob /
    get_blob_metadata for the requesting user.
    """
    if not _hex_sha256(sha256):
        raise HTTPException(status_code=400, detail='invalid sha256')
    if exp < int(time.time()):
        raise HTTPException(status_code=403, detail='url expired')
    if not verify_blob_signature(sha256, exp, sig):
        raise HTTPException(status_code=403, detail='bad signature')

    blob = await _get_blob(session, sha256)
    if blob is None:
        raise HTTPException(status_code=404, detail='not found')

    try:
        data = await get_blob_store().get(blob.storage_key)
    except FileNotFoundError:
        # Row says we have it but bytes are gone — DB / storage drifted.
        # 410 Gone is the spec-correct status; treat as a 404 to the client
        # but log loudly server-side so this gets noticed.
        raise HTTPException(status_code=410, detail='blob bytes missing')

    headers: dict[str, Any] = {
        'Content-Type': blob.content_type,
        'Content-Length': str(blob.size),
        # 1y immutable cache: the URL has an exp, but the bytes themselves
        # never change for a given sha256. The client only needs to refetch
        # when exp passes; the bytes are content-addressed.
        'Cache-Control': 'private, max-age=31536000, immutable',
        'X-Blob-Sha256': blob.sha256,
    }
    return Response(content=data, headers=headers)


# ---- DELETE per-user ref ----------------------------------------------------


@router.delete('/{sha256}/refs', status_code=204)
async def delete_blob_ref(
    sha256: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Drop *this user's* blob_ref. Bytes stay until the GC pass picks up an
    orphan blob (no refs, > 7d). Other users' refs are untouched.
    """
    if not _hex_sha256(sha256):
        raise HTTPException(status_code=400, detail='invalid sha256')
    stmt = select(BlobRef).where(
        BlobRef.user_id == user.id, BlobRef.sha256 == sha256
    )
    ref = (await session.execute(stmt)).scalar_one_or_none()
    if ref is None:
        # 404 stays consistent with metadata GETs above.
        raise HTTPException(status_code=404, detail='not found')
    await session.delete(ref)
    await session.commit()
    return Response(status_code=204)
