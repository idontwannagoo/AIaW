from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from realtime import broker

from ..auth import current_user
from ..db import get_session
from ..models.provider import Provider
from ..models.user import User
from ..pagination import CursorPage, build_page, normalize_limit

router = APIRouter(prefix='/api/v1/providers', tags=['providers'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class ProviderRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(p: Provider) -> ProviderRow:
    return ProviderRow(
        id=p.id,
        version=p.version,
        updated_at=p.updated_at.isoformat(),
        deleted=p.deleted_at is not None,
        data=None if p.deleted_at is not None else p.data,
    )


def _to_event(p: Provider) -> dict[str, Any]:
    deleted = p.deleted_at is not None
    return {
        'type': 'event',
        'table': 'providers',
        'op': 'delete' if deleted else 'put',
        'id': p.id,
        'rev': p.version,
        'row': None if deleted else {
            'id': p.id,
            'version': p.version,
            'updated_at': p.updated_at.isoformat(),
            'deleted': False,
            'data': p.data,
        },
    }


@router.get('', response_model=Union[list[ProviderRow], CursorPage[ProviderRow]])
async def list_providers(
    since: int = 0,
    limit: Optional[int] = None,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    fetch_limit = normalize_limit(limit)
    stmt = (
        select(Provider)
        .where(Provider.user_id == user_id, Provider.version > since)
        .order_by(Provider.version)
    )
    if fetch_limit is not None:
        stmt = stmt.limit(fetch_limit)
    result = await session.execute(stmt)
    rows = [_to_row(p) for p in result.scalars()]
    if fetch_limit is None:
        return rows
    return build_page(rows, fetch_limit)


@router.get('/{provider_id}', response_model=ProviderRow)
async def get_provider(
    provider_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Provider).where(
        Provider.id == provider_id, Provider.user_id == user_id
    )
    p = (await session.execute(stmt)).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(p)


@router.put('/{provider_id}', response_model=ProviderRow)
async def upsert_provider(
    provider_id: str,
    data: dict[str, Any],
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    # Upsert: bump version + clear any tombstone on revival.
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(Provider).values(
        id=provider_id,
        user_id=user_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[Provider.id],
        set_={
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(Provider.user_id == user_id),
    ).returning(Provider)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        # Conflict on id but owned by another user.
        raise HTTPException(status_code=409, detail='id owned by another user')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{provider_id}', response_model=ProviderRow)
async def delete_provider(
    provider_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(Provider)
        .where(Provider.id == provider_id, Provider.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(Provider)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
