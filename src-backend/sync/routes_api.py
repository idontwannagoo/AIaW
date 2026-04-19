from datetime import datetime
from typing import Annotated, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import crud
from .auth import CurrentUser
from .broadcaster import broadcaster
from .db import get_session
from .models import ENTITY_MODEL_MAP
from .schemas import BulkIn, EntityIn, EntityOut

router = APIRouter(prefix="/api")


def _entity_or_404(entity: str) -> None:
    if entity not in ENTITY_MODEL_MAP:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown entity: {entity}")


def _row_to_out(row) -> EntityOut:
    return EntityOut(
        id=row.id,
        data=row.data or {},
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


@router.get("/{entity}", response_model=list[EntityOut])
async def list_entity(
    entity: str,
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    since: Optional[datetime] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
):
    _entity_or_404(entity)
    scope = {
        k: unquote(v)
        for k, v in request.query_params.items()
        if k not in {"since", "limit"}
    }
    rows = await crud.list_since(session, entity, user.id, since=since, scope=scope, limit=limit)
    return [_row_to_out(r) for r in rows]


@router.put("/{entity}/{id}", response_model=EntityOut)
async def upsert_entity(
    entity: str,
    id: str,
    body: EntityIn,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    _entity_or_404(entity)
    if body.id != id:
        raise HTTPException(status_code=400, detail="id mismatch between path and body")
    obj, updated_at = await crud.upsert(session, entity, user.id, id, body.data)
    await session.commit()
    await broadcaster.publish(
        user.id,
        {"entity": entity, "id": id, "updated_at": updated_at.isoformat()},
    )
    return _row_to_out(obj)


@router.delete("/{entity}/{id}")
async def delete_entity(
    entity: str,
    id: str,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    _entity_or_404(entity)
    deleted_at = await crud.soft_delete(session, entity, user.id, id)
    await session.commit()
    await broadcaster.publish(
        user.id,
        {
            "entity": entity,
            "id": id,
            "updated_at": deleted_at.isoformat(),
            "deleted_at": deleted_at.isoformat(),
        },
    )
    return {"ok": True, "deleted_at": deleted_at.isoformat()}


@router.post("/bulk")
async def bulk_upsert(
    body: BulkIn,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    _entity_or_404(body.entity)
    notifications = []
    for item in body.items:
        if item.deleted:
            updated_at = await crud.soft_delete(session, body.entity, user.id, item.id)
            notifications.append(
                {
                    "entity": body.entity,
                    "id": item.id,
                    "updated_at": updated_at.isoformat(),
                    "deleted_at": updated_at.isoformat(),
                }
            )
        else:
            _, updated_at = await crud.upsert(session, body.entity, user.id, item.id, item.data)
            notifications.append(
                {"entity": body.entity, "id": item.id, "updated_at": updated_at.isoformat()}
            )
    await session.commit()
    for n in notifications:
        await broadcaster.publish(user.id, n)
    return {"ok": True, "count": len(body.items)}
