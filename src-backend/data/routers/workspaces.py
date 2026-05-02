"""Stage 4 / 批次-4a/4b — workspaces REST CRUD.

id-PK envelope, byte-for-byte equivalent to assistants. `data` carries the
full Workspace | Folder row (the discriminator `type` field lives inside
`data`; the envelope itself is uniform across both kinds).

`DELETE /api/v1/workspaces/{id}` accepts `?cascade=true|false`. As
server-routed child tables come online (dialogs at 4b, items / artifacts
/ messages at 4c–4e), each one gets folded into the cascade branch below
so the entire subtree tombstones in a single request. The frontend's
stores/workspaces.ts still performs the same per-table sweep over Dexie
tables that haven't migrated yet — the two are complementary, not
duplicate, since each table lives in exactly one persistence layer.
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
from ..models.dialog import Dialog
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


def _dialog_event(d: Dialog) -> dict[str, Any]:
    deleted = d.deleted_at is not None
    return {
        'type': 'event',
        'table': 'dialogs',
        'op': 'delete' if deleted else 'put',
        'id': d.id,
        'rev': d.version,
        'row': None if deleted else {
            'id': d.id,
            'version': d.version,
            'updated_at': d.updated_at.isoformat(),
            'deleted': False,
            'data': d.data,
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
    # When cascade=true, soft-delete every server-routed child row that
    # belongs to this workspace in the same transaction, then publish a
    # `delete` event for each so live tabs reflect the cascade without a
    # round-trip. Tables that haven't migrated yet (items / artifacts /
    # messages at 4b) are still cleaned up in stores/workspaces.ts on the
    # frontend; that path stays for as long as those tables live in Dexie.
    cascaded_dialogs: list[Dialog] = []
    if cascade:
        cascade_version = (await session.execute(
            text("SELECT nextval('global_change_seq')")
        )).scalar_one()
        dlg_stmt = (
            update(Dialog)
            .where(
                Dialog.workspace_id == workspace_id,
                Dialog.user_id == user_id,
                Dialog.deleted_at.is_(None),
            )
            .values(version=cascade_version, deleted_at=text('now()'))
            .returning(Dialog)
        )
        cascaded_dialogs = list(
            (await session.execute(dlg_stmt)).scalars().all()
        )
        # All cascaded child rows share the same `version` — clients treat
        # them as one tombstone batch, and per-row LWW idempotency makes
        # ordering inside the batch immaterial.

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
    for d in cascaded_dialogs:
        await broker.publish(user_id, _dialog_event(d))
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
