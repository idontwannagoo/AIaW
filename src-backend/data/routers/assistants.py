"""Stage 3 / 批次-3b — assistants REST CRUD.

id-PK pattern: byte-for-byte equivalent to providers (Stage 1 / Step 2),
just renamed. `data` carries the full Assistant row JSON.
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
from ..models.assistant import Assistant
from ..models.user import User

router = APIRouter(prefix='/api/v1/assistants', tags=['assistants'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class AssistantRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(a: Assistant) -> AssistantRow:
    return AssistantRow(
        id=a.id,
        version=a.version,
        updated_at=a.updated_at.isoformat(),
        deleted=a.deleted_at is not None,
        data=None if a.deleted_at is not None else a.data,
    )


def _to_event(a: Assistant) -> dict[str, Any]:
    deleted = a.deleted_at is not None
    return {
        'type': 'event',
        'table': 'assistants',
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


@router.get('', response_model=list[AssistantRow])
async def list_assistants(
    since: int = 0,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Assistant)
        .where(Assistant.user_id == user_id, Assistant.version > since)
        .order_by(Assistant.version)
    )
    result = await session.execute(stmt)
    return [_to_row(a) for a in result.scalars()]


@router.get('/{assistant_id}', response_model=AssistantRow)
async def get_assistant(
    assistant_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Assistant).where(
        Assistant.id == assistant_id, Assistant.user_id == user_id
    )
    a = (await session.execute(stmt)).scalar_one_or_none()
    if a is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(a)


@router.put('/{assistant_id}', response_model=AssistantRow)
async def upsert_assistant(
    assistant_id: str,
    data: dict[str, Any],
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(Assistant).values(
        id=assistant_id,
        user_id=user_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[Assistant.id],
        set_={
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(Assistant.user_id == user_id),
    ).returning(Assistant)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail='id owned by another user')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{assistant_id}', response_model=AssistantRow)
async def delete_assistant(
    assistant_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Assistant)
        .where(Assistant.id == assistant_id, Assistant.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Assistant)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
