"""Stage 4 / 批次-4c — items REST CRUD.

id-PK envelope, mirrors dialogs. The StoredItem row JSON lives in `data`;
`dialog_id` is promoted to a top-level column so the FK to dialogs and
dialog-scoped cascade queries stay set-based. PUT validates that the
dialog exists and is owned by the same user, otherwise the FK violation
surfaces as a 409 (rather than letting Postgres raise a raw IntegrityError
that becomes a 500).

The server is intentionally agnostic to the inline-vs-ref form of
`data.contentBuffer` — the client uses `src/data/blob-client.ts` to
serialize attachments under/over the 64KB threshold, and the resulting
envelope (`{type:'inline', data:base64, ...}` or `{type:'ref', url,
sha256, ...}`) just rides along inside JSONB. Bytes for ref-mode
attachments are uploaded separately via `/api/v1/blobs`.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from realtime import broker

from ..auth import current_user
from ..db import get_session
from ..models.dialog import Dialog
from ..models.item import Item
from ..models.user import User

router = APIRouter(prefix='/api/v1/items', tags=['items'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class ItemRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(it: Item) -> ItemRow:
    return ItemRow(
        id=it.id,
        version=it.version,
        updated_at=it.updated_at.isoformat(),
        deleted=it.deleted_at is not None,
        data=None if it.deleted_at is not None else it.data,
    )


def _to_event(it: Item) -> dict[str, Any]:
    deleted = it.deleted_at is not None
    return {
        'type': 'event',
        'table': 'items',
        'op': 'delete' if deleted else 'put',
        'id': it.id,
        'rev': it.version,
        'row': None if deleted else {
            'id': it.id,
            'version': it.version,
            'updated_at': it.updated_at.isoformat(),
            'deleted': False,
            'data': it.data,
        },
    }


@router.get('', response_model=list[ItemRow])
async def list_items(
    since: int = 0,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Item)
        .where(Item.user_id == user_id, Item.version > since)
        .order_by(Item.version)
    )
    result = await session.execute(stmt)
    return [_to_row(it) for it in result.scalars()]


@router.get('/{item_id}', response_model=ItemRow)
async def get_item(
    item_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Item).where(
        Item.id == item_id, Item.user_id == user_id
    )
    it = (await session.execute(stmt)).scalar_one_or_none()
    if it is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(it)


@router.put('/{item_id}', response_model=ItemRow)
async def upsert_item(
    item_id: str,
    data: dict[str, Any],
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    dialog_id = data.get('dialogId')
    if not isinstance(dialog_id, str) or not dialog_id:
        raise HTTPException(
            status_code=422, detail='dialogId required in body',
        )
    # Validate dialog ownership before relying on the FK; a missing /
    # cross-user dialog would otherwise surface as a 500 IntegrityError.
    dlg_stmt = select(Dialog.id).where(
        Dialog.id == dialog_id,
        Dialog.user_id == user_id,
        Dialog.deleted_at.is_(None),
    )
    if (await session.execute(dlg_stmt)).scalar_one_or_none() is None:
        raise HTTPException(
            status_code=409,
            detail=f'dialog {dialog_id!r} not found for user',
        )

    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(Item).values(
        id=item_id,
        user_id=user_id,
        dialog_id=dialog_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[Item.id],
        set_={
            'dialog_id': dialog_id,
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(Item.user_id == user_id),
    ).returning(Item)
    try:
        row = (await session.execute(stmt)).scalar_one_or_none()
    except IntegrityError:
        # Race: dialog was deleted between the ownership check and the
        # upsert. Treat the same as the up-front validation miss.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f'dialog {dialog_id!r} not found for user',
        )
    if row is None:
        raise HTTPException(status_code=409, detail='id owned by another user')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{item_id}', response_model=ItemRow)
async def delete_item(
    item_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Item)
        .where(Item.id == item_id, Item.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Item)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
