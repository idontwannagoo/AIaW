from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    token: str
    email: EmailStr


class MeResponse(BaseModel):
    email: EmailStr


class EntityIn(BaseModel):
    id: str
    data: dict[str, Any]
    deleted: bool = False


class EntityOut(BaseModel):
    id: str
    data: dict[str, Any]
    updated_at: datetime
    deleted_at: Optional[datetime] = None


class BulkIn(BaseModel):
    entity: str
    items: list[EntityIn]


class SignPutRequest(BaseModel):
    key: str = Field(min_length=8, max_length=128)
    size: int = Field(ge=0)
    mime_type: str


class SignPutResponse(BaseModel):
    url: str
    method: str = "PUT"
    headers: dict[str, str] = {}


class SignGetResponse(BaseModel):
    url: str


class WsNotification(BaseModel):
    entity: str
    id: str
    updated_at: datetime
    deleted_at: Optional[datetime] = None
