from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class _EntityMixin:
    """Per-user primary-key-id row with jsonb payload and updated/deleted tracking."""

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False, index=True
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Workspace(Base, _EntityMixin):
    __tablename__ = "workspaces"
    parent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_workspaces_user_updated", "user_id", "updated_at"),)


class Dialog(Base, _EntityMixin):
    __tablename__ = "dialogs"
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    __table_args__ = (
        Index("ix_dialogs_user_updated", "user_id", "updated_at"),
        Index("ix_dialogs_user_workspace", "user_id", "workspace_id"),
    )


class Message(Base, _EntityMixin):
    __tablename__ = "messages"
    dialog_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_messages_user_updated", "user_id", "updated_at"),
        Index("ix_messages_user_dialog", "user_id", "dialog_id"),
    )


class Assistant(Base, _EntityMixin):
    __tablename__ = "assistants"
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    __table_args__ = (Index("ix_assistants_user_updated", "user_id", "updated_at"),)


class Artifact(Base, _EntityMixin):
    __tablename__ = "artifacts"
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    __table_args__ = (Index("ix_artifacts_user_updated", "user_id", "updated_at"),)


class Item(Base, _EntityMixin):
    __tablename__ = "items"
    dialog_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_items_user_updated", "user_id", "updated_at"),
        Index("ix_items_user_dialog", "user_id", "dialog_id"),
    )


class AvatarImage(Base, _EntityMixin):
    __tablename__ = "avatar_images"
    __table_args__ = (Index("ix_avatar_images_user_updated", "user_id", "updated_at"),)


class InstalledPlugin(Base, _EntityMixin):
    """id = client-provided key (unique per user)."""

    __tablename__ = "installed_plugins"
    __table_args__ = (Index("ix_installed_plugins_user_updated", "user_id", "updated_at"),)


class Reactive(Base, _EntityMixin):
    """id = reactive key (e.g. #user-data)."""

    __tablename__ = "reactives"
    __table_args__ = (Index("ix_reactives_user_updated", "user_id", "updated_at"),)


class Provider(Base, _EntityMixin):
    __tablename__ = "providers"
    __table_args__ = (Index("ix_providers_user_updated", "user_id", "updated_at"),)


class FileObject(Base):
    __tablename__ = "files"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


ENTITY_MODEL_MAP = {
    "workspaces": Workspace,
    "dialogs": Dialog,
    "messages": Message,
    "assistants": Assistant,
    "artifacts": Artifact,
    "items": Item,
    "avatarImages": AvatarImage,
    "installedPluginsV2": InstalledPlugin,
    "reactives": Reactive,
    "providers": Provider,
}

SCOPE_COLUMN_MAP: dict[str, dict[str, str]] = {
    "workspaces": {"parentId": "parent_id"},
    "dialogs": {"workspaceId": "workspace_id"},
    "messages": {"dialogId": "dialog_id"},
    "assistants": {"workspaceId": "workspace_id"},
    "artifacts": {"workspaceId": "workspace_id"},
    "items": {"dialogId": "dialog_id"},
    "avatarImages": {},
    "installedPluginsV2": {},
    "reactives": {},
    "providers": {},
}
