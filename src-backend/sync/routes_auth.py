from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import CurrentUser, hash_password, issue_token, verify_password
from .db import get_session
from .models import User
from .schemas import AuthResponse, LoginRequest, MeResponse, RegisterRequest

router = APIRouter(prefix="/auth")


@router.post("/register", response_model=AuthResponse)
async def register(
    body: RegisterRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    email = body.email.lower()
    existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(email=email, password_hash=hash_password(body.password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return AuthResponse(token=issue_token(user.id), email=user.email)


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    email = body.email.lower()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return AuthResponse(token=issue_token(user.id), email=user.email)


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser):
    return MeResponse(email=user.email)
