"""Auth endpoints (Stage 1.5).

Endpoints:
- POST /api/v1/auth/register
- POST /api/v1/auth/login
- POST /api/v1/auth/refresh
- POST /api/v1/auth/logout
- GET  /api/v1/auth/me
- POST /api/v1/auth/link-dexie
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    consume_refresh_token,
    current_user,
    hash_password,
    issue_access_token,
    issue_refresh_token,
    revoke_refresh_token,
    verify_password,
)
from ..db import get_session
from ..models.user import User

router = APIRouter(prefix='/api/v1/auth', tags=['auth'])

# 'true' (open), 'false' (locked), 'invite' (requires INVITE_CODE).
ALLOW_REGISTRATION = os.environ.get('ALLOW_REGISTRATION', 'invite').lower()
INVITE_CODE = os.environ.get('INVITE_CODE')


class UserOut(BaseModel):
    id: str
    email: EmailStr
    status: str
    linked_dexie_email: Optional[EmailStr] = None
    created_at: datetime
    last_login_at: Optional[datetime] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = 'Bearer'
    user: UserOut


class RegisterIn(BaseModel):
    email: EmailStr
    # 8 char floor is a sanity check, not a strength meter.
    password: str = Field(min_length=8, max_length=200)
    invite_code: Optional[str] = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str


class LinkDexieIn(BaseModel):
    dexie_email: EmailStr


def _to_user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        status=u.status,
        linked_dexie_email=u.linked_dexie_email,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


def _check_registration_allowed(invite_code: Optional[str]) -> None:
    if ALLOW_REGISTRATION == 'true':
        return
    if ALLOW_REGISTRATION == 'invite':
        if not INVITE_CODE:
            raise HTTPException(
                status_code=503,
                detail='registration in invite mode but INVITE_CODE not set',
            )
        if invite_code != INVITE_CODE:
            raise HTTPException(status_code=403, detail='invalid invite code')
        return
    raise HTTPException(status_code=403, detail='registration disabled')


@router.post(
    '/register', response_model=TokenPair, status_code=status.HTTP_201_CREATED
)
async def register(
    body: RegisterIn, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    _check_registration_allowed(body.invite_code)
    email = body.email.lower()
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=hash_password(body.password),
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail='email already registered')
    refresh = await issue_refresh_token(session, user.id)
    access = issue_access_token(user.id)
    await session.commit()
    return TokenPair(
        access_token=access, refresh_token=refresh, user=_to_user_out(user)
    )


@router.post('/login', response_model=TokenPair)
async def login(
    body: LoginIn, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    email = body.email.lower()
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        # Same error for missing user vs. wrong password — don't leak existence.
        raise HTTPException(status_code=401, detail='invalid credentials')
    if user.status != 'active':
        raise HTTPException(status_code=403, detail=f'user {user.status}')
    user.last_login_at = datetime.now(tz=user.created_at.tzinfo)
    refresh = await issue_refresh_token(session, user.id)
    access = issue_access_token(user.id)
    await session.commit()
    return TokenPair(
        access_token=access, refresh_token=refresh, user=_to_user_out(user)
    )


@router.post('/refresh', response_model=TokenPair)
async def refresh(
    body: RefreshIn, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    record = await consume_refresh_token(session, body.refresh_token)
    if record is None:
        raise HTTPException(status_code=401, detail='invalid refresh token')
    # Rotate: revoke the presented refresh, issue a new one. Limits replay.
    record.revoked_at = datetime.now(tz=record.expires_at.tzinfo)
    user = (
        await session.execute(select(User).where(User.id == record.user_id))
    ).scalar_one_or_none()
    if user is None or user.status != 'active':
        await session.commit()
        raise HTTPException(status_code=401, detail='user inactive')
    new_refresh = await issue_refresh_token(session, user.id)
    new_access = issue_access_token(user.id)
    await session.commit()
    return TokenPair(
        access_token=new_access, refresh_token=new_refresh, user=_to_user_out(user)
    )


@router.post('/logout', status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutIn, session: AsyncSession = Depends(get_session)
):
    await revoke_refresh_token(session, body.refresh_token)
    await session.commit()
    return None


@router.get('/me', response_model=UserOut)
async def me(user: User = Depends(current_user)) -> UserOut:
    return _to_user_out(user)


@router.post('/link-dexie', response_model=UserOut)
async def link_dexie(
    body: LinkDexieIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    dexie_email = body.dexie_email.lower()
    if user.linked_dexie_email is not None:
        if user.linked_dexie_email == dexie_email:
            return _to_user_out(user)
        raise HTTPException(
            status_code=409,
            detail='user already linked to a different dexie email',
        )
    user.linked_dexie_email = dexie_email
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail='dexie email already linked to another user',
        )
    await session.refresh(user)
    return _to_user_out(user)
