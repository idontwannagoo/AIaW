"""Stage 3 / 批次-3a — reactives KV REST CRUD.

Mirrors providers (Stage 1 / Step 2) but with a KV-shaped envelope:
- Composite PK `(user_id, key)` instead of a single-column id PK with
  user_id check, since keys like '#user-data' collide across users.
- Envelope shape `{key, version, updated_at, deleted, data}` — `key`
  takes the place of `id`. `data` is the raw value blob (StoredReactive's
  `value`); the front-end re-wraps it as `{key, value: row.data}` for
  IndexedDB. Distinct unwrap from providers, which is intentional per
  plan's "per-table envelope" allowance.
- WS event keeps the unified `RealtimeEvent.id` slot — we set `id = key`
  so the existing dispatcher (which keys subscriptions by `id`) doesn't
  need a special-case for KV tables.
"""
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from realtime import broker

from ..auth import current_user
from ..db import get_session
from ..models.reactive import Reactive
from ..models.user import User

router = APIRouter(prefix='/api/v1/reactives', tags=['reactives'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class ReactiveRow(BaseModel):
    key: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[Any] = None


def _to_row(r: Reactive) -> ReactiveRow:
    return ReactiveRow(
        key=r.key,
        version=r.version,
        updated_at=r.updated_at.isoformat(),
        deleted=r.deleted_at is not None,
        data=None if r.deleted_at is not None else r.data,
    )


def _to_event(r: Reactive) -> dict[str, Any]:
    deleted = r.deleted_at is not None
    return {
        'type': 'event',
        'table': 'reactives',
        'op': 'delete' if deleted else 'put',
        # KV table: the `key` is the natural identity, mapped onto the
        # generic envelope's `id` slot so realtime dispatcher stays generic.
        'id': r.key,
        'rev': r.version,
        'row': None if deleted else {
            'key': r.key,
            'version': r.version,
            'updated_at': r.updated_at.isoformat(),
            'deleted': False,
            'data': r.data,
        },
    }


@router.get('', response_model=list[ReactiveRow])
async def list_reactives(
    since: int = 0,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Reactive)
        .where(Reactive.user_id == user_id, Reactive.version > since)
        .order_by(Reactive.version)
    )
    result = await session.execute(stmt)
    return [_to_row(r) for r in result.scalars()]


@router.get('/{key:path}', response_model=ReactiveRow)
async def get_reactive(
    key: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Reactive).where(
        Reactive.user_id == user_id, Reactive.key == key
    )
    r = (await session.execute(stmt)).scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(r)


@router.put('/{key:path}', response_model=ReactiveRow)
async def upsert_reactive(
    key: str,
    # Accept any JSON value as the payload body. persistent-reactive only
    # ever stores plain objects, but Body(...) keeps the door open for
    # primitives if a caller needs them.
    data: Any = Body(...),
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(Reactive).values(
        user_id=user_id,
        key=key,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        # PK is (user_id, key); upsert against the same user only.
        index_elements=[Reactive.user_id, Reactive.key],
        set_={
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
    ).returning(Reactive)
    row = (await session.execute(stmt)).scalar_one()
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{key:path}', response_model=ReactiveRow)
async def delete_reactive(
    key: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Reactive)
        .where(Reactive.user_id == user_id, Reactive.key == key)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Reactive)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
