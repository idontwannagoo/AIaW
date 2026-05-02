"""Stage 3 / 批次-3b — installed_plugins REST CRUD.

KV-shaped (composite (user_id, key) PK like reactives). Plugin `key` is a
manifest-derived string ('lobe:foo' / 'mcp:bar') that may legitimately
collide across users. Distinct from reactives in one place: `data` carries
the full InstalledPlugin row (id / type / available / manifest), not just
a value blob — see `_to_event` and `_to_row`.

The route exposes the plugin key as `{key:path}` so namespaced keys like
`mcp:my-tool` survive without explicit URL encoding (path converter eats
`:` happily where the default would treat it as a path segment delimiter).
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
from ..models.installed_plugin import InstalledPlugin
from ..models.user import User

router = APIRouter(prefix='/api/v1/installed-plugins', tags=['installed-plugins'])


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


class InstalledPluginRow(BaseModel):
    key: str
    version: int
    updated_at: str
    deleted: bool
    data: Optional[dict[str, Any]] = None


def _to_row(p: InstalledPlugin) -> InstalledPluginRow:
    return InstalledPluginRow(
        key=p.key,
        version=p.version,
        updated_at=p.updated_at.isoformat(),
        deleted=p.deleted_at is not None,
        data=None if p.deleted_at is not None else p.data,
    )


def _to_event(p: InstalledPlugin) -> dict[str, Any]:
    deleted = p.deleted_at is not None
    return {
        'type': 'event',
        'table': 'installed_plugins',
        'op': 'delete' if deleted else 'put',
        # KV table: `key` takes the `id` slot in the generic envelope (mirrors
        # reactives) so the realtime dispatcher stays generic.
        'id': p.key,
        'rev': p.version,
        'row': None if deleted else {
            'key': p.key,
            'version': p.version,
            'updated_at': p.updated_at.isoformat(),
            'deleted': False,
            'data': p.data,
        },
    }


@router.get('', response_model=list[InstalledPluginRow])
async def list_installed_plugins(
    since: int = 0,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(InstalledPlugin)
        .where(
            InstalledPlugin.user_id == user_id,
            InstalledPlugin.version > since,
        )
        .order_by(InstalledPlugin.version)
    )
    result = await session.execute(stmt)
    return [_to_row(p) for p in result.scalars()]


@router.get('/{key:path}', response_model=InstalledPluginRow)
async def get_installed_plugin(
    key: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(InstalledPlugin).where(
        InstalledPlugin.user_id == user_id, InstalledPlugin.key == key
    )
    p = (await session.execute(stmt)).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail='not found')
    return _to_row(p)


@router.put('/{key:path}', response_model=InstalledPluginRow)
async def upsert_installed_plugin(
    key: str,
    data: dict[str, Any],
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = pg_insert(InstalledPlugin).values(
        user_id=user_id,
        key=key,
        data=data,
        version=next_version,
        deleted_at=None,
    ).on_conflict_do_update(
        # PK is (user_id, key); upsert against the same user only.
        index_elements=[InstalledPlugin.user_id, InstalledPlugin.key],
        set_={
            'data': data,
            'version': next_version,
            'updated_at': text('now()'),
            'deleted_at': None,
        },
    ).returning(InstalledPlugin)
    row = (await session.execute(stmt)).scalar_one()
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)


@router.delete('/{key:path}', response_model=InstalledPluginRow)
async def delete_installed_plugin(
    key: str,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
):
    next_version = (await session.execute(
        text("SELECT nextval('global_change_seq')")
    )).scalar_one()
    stmt = (
        update(InstalledPlugin)
        .where(InstalledPlugin.user_id == user_id, InstalledPlugin.key == key)
        .values(version=next_version, deleted_at=text('now()'))
        .returning(InstalledPlugin)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail='not found')
    await session.commit()
    await broker.publish(user_id, _to_event(row))
    return _to_row(row)
