"""Self-hosted JWT auth (Stage 1.5).

Replaces the DEV_USER_ID stub. Issues short-lived access tokens (HS256) plus
refresh tokens whose hashes live in the `refresh_tokens` table so logout can
revoke them server-side.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models.refresh_token import RefreshToken
from .models.user import User

JWT_SECRET = os.environ.get('JWT_SECRET')
if not JWT_SECRET:
    raise RuntimeError(
        'JWT_SECRET env var is required. Generate one with: '
        "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )

JWT_ALGORITHM = 'HS256'
ACCESS_TOKEN_TTL = timedelta(minutes=30)
REFRESH_TOKEN_TTL = timedelta(days=30)

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')

# tokenUrl is informational (used by /docs); login is JSON-body, not form.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='/api/v1/auth/login', auto_error=False)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_access_token(user_id: str) -> str:
    payload = {
        'sub': user_id,
        'iat': int(_now().timestamp()),
        'exp': int((_now() + ACCESS_TOKEN_TTL).timestamp()),
        'type': 'access',
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _hash_refresh(token: str) -> str:
    # Refresh tokens are random opaque strings — SHA-256 is sufficient and
    # cheap; only the hash lives in the DB.
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


async def issue_refresh_token(session: AsyncSession, user_id: str) -> str:
    raw = secrets.token_urlsafe(48)
    record = RefreshToken(
        id=str(uuid.uuid4()),
        user_id=user_id,
        token_hash=_hash_refresh(raw),
        expires_at=_now() + REFRESH_TOKEN_TTL,
    )
    session.add(record)
    return raw


async def consume_refresh_token(
    session: AsyncSession, raw: str
) -> Optional[RefreshToken]:
    """Return the record if valid, else None. Caller decides what to do."""
    stmt = select(RefreshToken).where(RefreshToken.token_hash == _hash_refresh(raw))
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is None:
        return None
    if record.revoked_at is not None:
        return None
    if record.expires_at <= _now():
        return None
    return record


async def revoke_refresh_token(session: AsyncSession, raw: str) -> bool:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == _hash_refresh(raw))
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = _now()
    return True


def _decode_access(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='token expired',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='invalid token',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    if payload.get('type') != 'access':
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='wrong token type'
        )
    return payload


async def current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='not authenticated',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    payload = _decode_access(token)
    user_id = payload.get('sub')
    if not user_id:
        raise HTTPException(status_code=401, detail='invalid token payload')
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail='user not found')
    if user.status != 'active':
        raise HTTPException(status_code=403, detail=f'user {user.status}')
    return user
