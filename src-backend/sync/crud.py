from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ENTITY_MODEL_MAP, SCOPE_COLUMN_MAP


class UnknownEntity(Exception):
    pass


def model_for(entity: str):
    model = ENTITY_MODEL_MAP.get(entity)
    if not model:
        raise UnknownEntity(entity)
    return model


_DROP_FIELDS = {"owner", "realmId", "$ts"}


def _clean_payload(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k not in _DROP_FIELDS}


def _extract_scope_values(entity: str, data: dict) -> dict[str, Optional[str]]:
    mapping = SCOPE_COLUMN_MAP.get(entity, {})
    return {col: data.get(client_key) for client_key, col in mapping.items()}


async def list_since(
    session: AsyncSession,
    entity: str,
    user_id: UUID,
    since: Optional[datetime] = None,
    scope: Optional[dict[str, str]] = None,
    limit: int = 500,
) -> list:
    Model = model_for(entity)
    conditions = [Model.user_id == user_id]
    if since is not None:
        conditions.append(Model.updated_at > since)

    scope_mapping = SCOPE_COLUMN_MAP.get(entity, {})
    if scope:
        for client_key, value in scope.items():
            col_name = scope_mapping.get(client_key)
            if col_name:
                conditions.append(getattr(Model, col_name) == value)

    stmt = select(Model).where(and_(*conditions)).order_by(Model.updated_at.asc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_one(session: AsyncSession, entity: str, user_id: UUID, id_: str):
    Model = model_for(entity)
    stmt = select(Model).where(Model.user_id == user_id, Model.id == id_)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert(
    session: AsyncSession,
    entity: str,
    user_id: UUID,
    id_: str,
    payload: dict,
) -> tuple[object, datetime]:
    Model = model_for(entity)
    clean = _clean_payload(payload)
    scope_cols = _extract_scope_values(entity, clean)

    existing = await get_one(session, entity, user_id, id_)
    now = datetime.now(timezone.utc)

    if existing:
        existing.data = clean
        existing.updated_at = now
        existing.deleted_at = None
        for col_name, val in scope_cols.items():
            setattr(existing, col_name, val)
        if entity == "workspaces":
            existing.type = clean.get("type", existing.type)
        if entity == "messages" or entity == "items":
            existing.type = clean.get("type", existing.type)
        obj = existing
    else:
        extra: dict = {}
        if entity == "workspaces":
            extra["type"] = clean.get("type", "workspace")
        if entity in ("messages", "items"):
            extra["type"] = clean.get("type", "")
        obj = Model(
            id=id_,
            user_id=user_id,
            data=clean,
            updated_at=now,
            **scope_cols,
            **extra,
        )
        session.add(obj)

    await session.flush()
    return obj, now


async def soft_delete(
    session: AsyncSession,
    entity: str,
    user_id: UUID,
    id_: str,
) -> Optional[datetime]:
    Model = model_for(entity)
    existing = await get_one(session, entity, user_id, id_)
    now = datetime.now(timezone.utc)
    if not existing:
        obj = Model(
            id=id_,
            user_id=user_id,
            data={},
            updated_at=now,
            deleted_at=now,
            **{col: None for col in SCOPE_COLUMN_MAP.get(entity, {}).values()},
            **({"type": "deleted"} if entity in ("workspaces", "messages", "items") else {}),
        )
        session.add(obj)
    else:
        existing.updated_at = now
        existing.deleted_at = now
    await session.flush()
    return now
