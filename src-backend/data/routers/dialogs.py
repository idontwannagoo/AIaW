"""Stage 4 / 批次-4b — dialogs REST CRUD.

id-PK envelope, mirrors workspaces. The Dialog row JSON lives in `data`;
`workspace_id` is promoted to a top-level column so the FK to workspaces
and workspace-scoped cascade queries stay set-based. PUT validates that
the workspace exists and is owned by the same user, otherwise the FK
violation surfaces as a 409 (rather than letting Postgres raise a raw
IntegrityError that becomes a 500).
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
from ..models.dialog import Dialog
from ..models.message import Message
from ..models.user import User
from ..models.workspace import Workspace
from ..pagination import CursorPage, build_page, normalize_limit

router = APIRouter(prefix='/api/v1/dialogs', tags=['dialogs'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class DialogRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(d: Dialog) -> DialogRow:
    return DialogRow(
        id=d.id,
        version=d.version,
        updated_at=d.updated_at.isoformat(),
        deleted=d.deleted_at is not None,
        data=None if d.deleted_at is not None else d.data,
    )


def _to_event(d: Dialog) -> dict[str, Any]:
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


@router.get(
    '',
    response_model=Union[list[DialogRow], CursorPage[DialogRow]],
)
async def list_dialogs(
    since: int = 0,
    limit: Optional[int] = None,
    workspace_id: Optional[str] = Query(None, alias='workspaceId'),
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    """List dialogs for the authed user.

    Stage 4 / 硬前置 3: when `workspaceId=` is provided, the query is scoped
    to that workspace via `WHERE workspace_id = :ws`. Cross-user scopeIds
    silently filter to the empty set (the user_id predicate already isolates
    accounts; we don't 403 because the scope id is just a `WHERE` value, not
    an explicit ownership claim). Combinable with `since=` and `limit=`.
    """
    fetch_limit = normalize_limit(limit)
    stmt = (
        select(Dialog)
        .where(Dialog.user_id == user_id, Dialog.version > since)
        .order_by(Dialog.version)
    )
    if workspace_id is not None:
        stmt = stmt.where(Dialog.workspace_id == workspace_id)
    if fetch_limit is not None:
        stmt = stmt.limit(fetch_limit)
    result = await session.execute(stmt)
    rows = [_to_row(d) for d in result.scalars()]
    if fetch_limit is None:
        return rows
    return build_page(rows, fetch_limit)


@router.get('/{dialog_id}', response_model=DialogRow)
async def get_dialog(
    dialog_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Dialog).where(
        Dialog.id == dialog_id, Dialog.user_id == user_id
    )
    d = (await session.execute(stmt)).scalar_one_or_none()
    if d is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(d)


@router.put('/{dialog_id}', response_model=DialogRow)
async def upsert_dialog(
    dialog_id: str,
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
    stmt = pg_insert(Dialog).values(
        id=dialog_id,
        user_id=user_id,
        workspace_id=workspace_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[Dialog.id],
        set_={
            'workspace_id': workspace_id,
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(Dialog.user_id == user_id),
    ).returning(Dialog)
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


def _message_event(m: Message) -> dict[str, Any]:
    deleted = m.deleted_at is not None
    return {
        'type': 'event',
        'table': 'messages',
        'op': 'delete' if deleted else 'put',
        'id': m.id,
        'rev': m.version,
        'row': None if deleted else {
            'id': m.id,
            'version': m.version,
            'updated_at': m.updated_at.isoformat(),
            'deleted': False,
            'data': m.data,
        },
    }


@router.delete('/{dialog_id}', response_model=DialogRow)
async def delete_dialog(
    dialog_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    # Stage 4 / 批次-4e — application-level cascade: when a dialog is
    # tombstoned, its messages must also be tombstoned in the same
    # transaction so live tabs see a consistent batch (per-row LWW
    # idempotency makes ordering inside the batch immaterial). items also
    # FK to dialog_id but per 2026-05-03 plan modeling we tombstone items
    # only via the workspace cascade path (the live UI never deletes a
    # standalone dialog while keeping items around — items belong to a
    # dialog, and the user-facing flow that calls dialog delete also wants
    # those items gone). Symmetric with workspaces.delete_workspace's
    # cascade chain.
    cascade_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    msg_stmt = (
        update(Message)
        .where(
            Message.dialog_id == dialog_id,
            Message.user_id == user_id,
            Message.deleted_at.is_(None),
        )
        .values(version=cascade_version, deleted_at=text('now()'))
        .returning(Message)
    )
    cascaded_messages = list(
        (await session.execute(msg_stmt)).scalars().all()
    )

    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Dialog)
        .where(Dialog.id == dialog_id, Dialog.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Dialog)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    for m in cascaded_messages:
        await broker.publish(user_id, _message_event(m))
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
