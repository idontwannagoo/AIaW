"""Stage 4 / 批次-4e — messages REST CRUD.

id-PK envelope, mirrors items. The Message row JSON lives in `data`;
`dialog_id` is promoted to a top-level column so the FK to dialogs and
dialog-scoped pull / cascade queries stay set-based. PUT validates that
the dialog exists and is owned by the same user, otherwise the FK
violation surfaces as a 409 (rather than letting Postgres raise a raw
IntegrityError that becomes a 500).

The server is intentionally agnostic to the inline-vs-ref form of message
body bytes — the client uses `src/data/blob-client.ts` to serialize a
`contents`-spilled blob under/over the 64KB threshold, and the resulting
envelope (`{type:'inline', data:base64, ...}` or `{type:'ref', url,
sha256, ...}`) just rides along inside JSONB. Bytes for ref-mode
attachments are uploaded separately via `/api/v1/blobs`.

Mandatory `dialogId` filter (Stage 4 / 硬前置 3 + 2026-05-03 修订):
unlike the items / artifacts list endpoints which accept the scope filter
as optional, the messages list endpoint REQUIRES `?dialogId=Y` because a
"give me all my messages" call is meaningless (single user messages count
is easily 10k+ and would blow the broker queue / response budget). The
endpoint returns 422 when the parameter is absent.
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
from ..pagination import CursorPage, build_page, normalize_limit

router = APIRouter(prefix='/api/v1/messages', tags=['messages'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class MessageRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(m: Message) -> MessageRow:
    return MessageRow(
        id=m.id,
        version=m.version,
        updated_at=m.updated_at.isoformat(),
        deleted=m.deleted_at is not None,
        data=None if m.deleted_at is not None else m.data,
    )


def _to_event(m: Message) -> dict[str, Any]:
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


@router.get(
    '',
    response_model=Union[list[MessageRow], CursorPage[MessageRow]],
)
async def list_messages(
    since: int = 0,
    limit: Optional[int] = None,
    dialog_id: Optional[str] = Query(None, alias='dialogId'),
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    """List messages for the authed user, scoped to a dialog.

    Stage 4 / 硬前置 3 + 2026-05-03 修订: `dialogId` is **mandatory** here.
    Unlike items / artifacts where the scope filter is optional and a bare
    `?since=N` falls back to a full-table pull, messages are too large to
    serve un-scoped (single user 10k+ rows easily). Missing `dialogId`
    returns 422. Cross-user scopeIds silently filter to the empty set
    (the user_id predicate already isolates accounts; we don't 403 because
    the scope id is just a `WHERE` value, not an explicit ownership claim).
    Combinable with `since=` and `limit=` for cursor pagination.
    """
    if not dialog_id:
        raise HTTPException(
            status_code=422,
            detail='dialogId query parameter required for messages list',
        )
    fetch_limit = normalize_limit(limit)
    stmt = (
        select(Message)
        .where(
            Message.user_id == user_id,
            Message.dialog_id == dialog_id,
            Message.version > since,
        )
        .order_by(Message.version)
    )
    if fetch_limit is not None:
        stmt = stmt.limit(fetch_limit)
    result = await session.execute(stmt)
    rows = [_to_row(m) for m in result.scalars()]
    if fetch_limit is None:
        return rows
    return build_page(rows, fetch_limit)


@router.get('/{message_id}', response_model=MessageRow)
async def get_message(
    message_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Message).where(
        Message.id == message_id, Message.user_id == user_id
    )
    m = (await session.execute(stmt)).scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(m)


@router.put('/{message_id}', response_model=MessageRow)
async def upsert_message(
    message_id: str,
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
    stmt = pg_insert(Message).values(
        id=message_id,
        user_id=user_id,
        dialog_id=dialog_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[Message.id],
        set_={
            'dialog_id': dialog_id,
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(Message.user_id == user_id),
    ).returning(Message)
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


@router.delete('/{message_id}', response_model=MessageRow)
async def delete_message(
    message_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Message)
        .where(Message.id == message_id, Message.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Message)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
