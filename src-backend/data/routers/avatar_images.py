"""Stage 3 / 批次-3b — avatar_images REST CRUD.

id-PK pattern (mirrors providers / assistants). The client serializes the
ArrayBuffer field to base64 before PUT and decodes it back on read; the
server treats `data` as opaque JSON, so binary handling stays at the edges.

< 64KB rows are inline here. Stage 4 hard-pre-2 will introduce object-store
ref-splitting for ≥ 64KB blobs; until then everything sits in JSONB.
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
from ..models.avatar_image import AvatarImage
from ..models.user import User

router = APIRouter(prefix='/api/v1/avatar-images', tags=['avatar-images'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class AvatarImageRow(BaseModel):
    id: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(a: AvatarImage) -> AvatarImageRow:
    return AvatarImageRow(
        id=a.id,
        version=a.version,
        updated_at=a.updated_at.isoformat(),
        deleted=a.deleted_at is not None,
        data=None if a.deleted_at is not None else a.data,
    )


def _to_event(a: AvatarImage) -> dict[str, Any]:
    deleted = a.deleted_at is not None
    return {
        'type': 'event',
        'table': 'avatar_images',
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


@router.get('', response_model=list[AvatarImageRow])
async def list_avatar_images(
    since: int = 0,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(AvatarImage)
        .where(AvatarImage.user_id == user_id, AvatarImage.version > since)
        .order_by(AvatarImage.version)
    )
    result = await session.execute(stmt)
    return [_to_row(a) for a in result.scalars()]


@router.get('/{image_id}', response_model=AvatarImageRow)
async def get_avatar_image(
    image_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(AvatarImage).where(
        AvatarImage.id == image_id, AvatarImage.user_id == user_id
    )
    a = (await session.execute(stmt)).scalar_one_or_none()
    if a is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(a)


@router.put('/{image_id}', response_model=AvatarImageRow)
async def upsert_avatar_image(
    image_id: str,
    data: dict[str, Any],
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(AvatarImage).values(
        id=image_id,
        user_id=user_id,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        index_elements=[AvatarImage.id],
        set_={
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
        where=(AvatarImage.user_id == user_id),
    ).returning(AvatarImage)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail='id owned by another user')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{image_id}', response_model=AvatarImageRow)
async def delete_avatar_image(
    image_id: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(AvatarImage)
        .where(AvatarImage.id == image_id, AvatarImage.user_id == user_id)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(AvatarImage)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
