"""Stage 4 / 批次-4d — artifacts REST CRUD.

id-PK envelope, mirrors dialogs / items. The Artifact row JSON lives in
`data`; `workspace_id` is promoted to a top-level column so the FK to
workspaces and workspace-scoped cascade queries stay set-based. PUT
validates that the workspace exists and is owned by the same user,
otherwise the FK violation surfaces as a 409 (rather than letting Postgres
raise a raw IntegrityError that becomes a 500).

The server is intentionally agnostic to whether `data.versions` is shipped
inline or spilled into `data.versionsBlob: AttachmentEnvelope` — the client
uses `src/data/blob-client.ts` to make that decision under/over the 64KB
threshold and the resulting envelope rides along inside JSONB. Bytes for
ref-mode payloads are uploaded separately via `/api/v1/blobs`.

Note on scope: the frontend `Artifact` type carries ONLY `workspaceId`
(no `dialogId`). Plan 2026-05-03 修订 originally listed double-scope
filters but that doesn't match the source-of-truth shape — `list_artifacts`
exposes `?workspaceId=` only. See models/artifact.py header for the full
rationale.
"""
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from realtime import broker

from ..auth import current_user
from ..db import get_session
from ..models.artifact import Artifact
from ..models.user import User
from ..models.workspace import Workspace
from ..pagination import CursorPage, build_page, normalize_limit

router = APIRouter(prefix='/api/v1/artifacts', tags=['artifacts'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class ArtifactRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(a: Artifact) -> ArtifactRow:
    return ArtifactRow(
        id=a.id,
        version=a.version,
        updated_at=a.updated_at.isoformat(),
        deleted=a.deleted_at is not None,
        data=None if a.deleted_at is not None else a.data,
    )


def _to_event(a: Artifact) -> dict[str, Any]:
    deleted = a.deleted_at is not None
    return {
        'type': 'event',
        'table': 'artifacts',
        'op': 'delete' if deleted else 'put',
        'id': a.id,
        'rev': a.version,
        'row': None if deleted else {
            'id': a.id,
            'version': a.version,
            'updated_at': a.updated_at.isoformat(),
            'deleted': False,
            'data': a.data,
        },
    }


@router.get(
    '',
    response_model=Union[list[ArtifactRow], CursorPage[ArtifactRow]],
)
async def list_artifacts(
    since: int = 0,
    limit: Optional[int] = None,
    workspace_id: Optional[str] = Query(None, alias='workspaceId'),
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    """List artifacts for the authed user.

    Stage 4 / 硬前置 3: when `workspaceId=` is provided, the query is scoped
    to that workspace via `WHERE workspace_id = :ws`. Cross-user scopeIds
    silently filter to the empty set (the user_id predicate already isolates
    accounts; we don't 403 because the scope id is just a `WHERE` value, not
    an explicit ownership claim). Combinable with `since=` and `limit=`.
    """
    fetch_limit = normalize_limit(limit)
    stmt = (
        select(Artifact)
        .where(Artifact.user_id == user_id, Artifact.version > since)
        .order_by(Artifact.version)
    )
    if workspace_id is not None:
        stmt = stmt.where(Artifact.workspace_id == workspace_id)
    if fetch_limit is not None:
        stmt = stmt.limit(fetch_limit)
    result = await session.execute(stmt)
    rows = [_to_row(a) for a in result.scalars()]
    if fetch_limit is None:
        return rows
    return build_page(rows, fetch_limit)


@router.get('/{artifact_id}', response_model=ArtifactRow)
async def get_artifact(
    artifact_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Artifact).where(
        Artifact.id == artifact_id, Artifact.user_id == user_id
    )
    a = (await session.execute(stmt)).scalar_one_or_none()
    if a is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(a)


@router.put('/{artifact_id}', response_model=ArtifactRow)
async def upsert_artifact(
    artifact_id: str,
    data: dict[str, Any],
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    workspace_id = data.get('workspaceId')
    if not isinstance(workspace_id, str) or not workspace_id:
        raise HTTPException(
            status_code=422, detail='workspaceId required in body',
        )
    # Validate workspace ownership before relying on the FK; a missing /
    # cross-user workspace would otherwise surface as a 500 IntegrityError.
    ws_stmt = select(Workspace.id).where(
        Workspace.id == workspace_id,
        Workspace.user_id == user_id,
        Workspace.deleted_at.is_(None),
    )
    if (await session.execute(ws_stmt)).scalar_one_or_none() is None:
        raise HTTPException(
            status_code=409,
            detail=f'workspace {workspace_id!r} not found for user',
        )

    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(Artifact).values(
        id=artifact_id,
        user_id=user_id,
        workspace_id=workspace_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[Artifact.id],
        set_={
            'workspace_id': workspace_id,
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(Artifact.user_id == user_id),
    ).returning(Artifact)
    try:
        row = (await session.execute(stmt)).scalar_one_or_none()
    except IntegrityError:
        # Race: workspace was deleted between the ownership check and the
        # upsert. Treat the same as the up-front validation miss.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f'workspace {workspace_id!r} not found for user',
        )
    if row is None:
        raise HTTPException(status_code=409, detail='id owned by another user')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{artifact_id}', response_model=ArtifactRow)
async def delete_artifact(
    artifact_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Artifact)
        .where(Artifact.id == artifact_id, Artifact.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Artifact)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
