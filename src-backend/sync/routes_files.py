import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import CurrentUser
from .config import MAX_UPLOAD_SIZE
from .db import get_session
from .models import FileObject
from .s3 import head_object, presign_get, presign_put
from .schemas import SignGetResponse, SignPutRequest, SignPutResponse

router = APIRouter(prefix="/files")

_KEY_RE = re.compile(r"^[a-f0-9]{8,128}$")


def _validate_key(key: str) -> None:
    if not _KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Invalid key format")


@router.post("/sign-put", response_model=SignPutResponse)
async def sign_put(
    body: SignPutRequest,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    _validate_key(body.key)
    if body.size > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large")

    existing = (
        await session.execute(select(FileObject).where(FileObject.key == body.key))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            FileObject(
                key=body.key,
                user_id=user.id,
                mime_type=body.mime_type,
                size=body.size,
            )
        )
        await session.commit()
    url = presign_put(body.key, body.mime_type)
    return SignPutResponse(url=url, headers={"Content-Type": body.mime_type})


@router.get("/sign-get/{key}", response_model=SignGetResponse)
async def sign_get(
    key: str,
    user: CurrentUser,
):
    _validate_key(key)
    return SignGetResponse(url=presign_get(key))


@router.head("/{key}")
async def head_key(key: str, user: CurrentUser):
    _validate_key(key)
    if not head_object(key):
        raise HTTPException(status_code=404)
    return {}
