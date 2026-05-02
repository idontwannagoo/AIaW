"""Stage 4 / 批次-4a — workspaces REST CRUD.

id-PK envelope, byte-for-byte equivalent to assistants. `data` carries the
full Workspace | Folder row (the discriminator `type` field lives inside
`data`; the envelope itself is uniform across both kinds).

`DELETE /api/v1/workspaces/{id}` accepts an optional `?cascade=true|false`
query parameter. At 4a only the workspaces table itself is server-routed —
its child tables (dialogs / messages / items / artifacts) are still in
Dexie, and assistants is a sibling server table without a workspaces FK
yet. The cascade parameter is therefore a no-op here; it's accepted (not
422'd) so 4b/4c/4d/4e can grow the implementation without breaking the
client contract. The frontend continues to perform per-table cleanup at
the Repository layer in stores/workspaces.ts.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from realtime import broker

from ..auth import current_user
from ..db import get_session
from ..models.user import User
from ..models.workspace import Workspace

router = APIRouter(prefix='/api/v1/workspaces', tags=['workspaces'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class WorkspaceRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(w: Workspace) -> WorkspaceRow:
    return WorkspaceRow(
        id=w.id,
        version=w.version,
        updated_at=w.updated_at.isoformat(),
        deleted=w.deleted_at is not None,
        data=None if w.deleted_at is not None else w.data,
    )


def _to_event(w: Workspace) -> dict[str, Any]:
    deleted = w.deleted_at is not None
    return {
        'type': 'event',
        'table': 'workspaces',
        'op': 'delete' if deleted else 'put',
        'id': w.id,
        'rev': w.version,
        'row': None if deleted else {
            'id': w.id,
            'version': w.version,
            'updated_at': w.updated_at.isoformat(),
            'deleted': False,
            'data': w.data,
        },
    }


@router.get('', response_model=list[WorkspaceRow])
async def list_workspaces(
    since: int = 0,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Workspace)
        .where(Workspace.user_id == user_id, Workspace.version > since)
        .order_by(Workspace.version)
    )
    result = await session.execute(stmt)
    return [_to_row(w) for w in result.scalars()]


@router.get('/{workspace_id}', response_model=WorkspaceRow)
async def get_workspace(
    workspace_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Workspace).where(
        Workspace.id == workspace_id, Workspace.user_id == user_id
    )
    w = (await session.execute(stmt)).scalar_one_or_none()
    if w is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(w)


@router.put('/{workspace_id}', response_model=WorkspaceRow)
async def upsert_workspace(
    workspace_id: str,
    data: dict[str, Any],
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(Workspace).values(
        id=workspace_id,
        user_id=user_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[Workspace.id],
        set_={
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(Workspace.user_id == user_id),
    ).returning(Workspace)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail='id owned by another user')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{workspace_id}', response_model=WorkspaceRow)
async def delete_workspace(
    workspace_id: str,
    cascade: bool = False,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    # `cascade` is accepted for client-side forward compatibility; at 4a
    # there are no server-routed child tables that cascade can act on, so
    # it's intentionally a no-op (see module docstring). Leaving the flag
    # in the signature means stores/workspaces.ts can already pass it
    # unconditionally and 4b/4c/4d/4e get to grow the impl behind it
    # without revving the wire contract.
    _ = cascade
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Workspace)
        .where(Workspace.id == workspace_id, Workspace.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Workspace)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
