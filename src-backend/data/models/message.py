from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class Message(Base):
    __tablename__ = 'messages'

    # Stage 4 / 批次-4e — messages, the dialog conversation rows.
    #
    # id-PK envelope (mirrors Item / Artifact) with `dialog_id` promoted to
    # its own FK column so the dialog-scoped pull / dialog-cascade /
    # workspace-cascade queries stay set-based. The frontend `Message`
    # shape lives entirely inside `data` JSONB:
    #     { id, type:'user'|'assistant', assistantId?, dialogId,
    #       contents:MessageContent[], status, generatingSession?, error?,
    #       warnings?, usage?, modelName? }
    # Attachments referenced by message contents (UserMessageContent.items
    # / AssistantToolContent.result) are NOT stored on this row — they live
    # in the `items` table (4c) and message contents only carry
    # StoredItemId[]. So unlike items, messages never carry binary bytes
    # directly. The wire-level inline/ref decision still applies to message
    # bodies that grow large from streaming token accumulation: the client
    # checks JSON.stringify(data) size and may spill `contents` into a
    # `contentsBlob: AttachmentEnvelope` ref to keep the PG row well below
    # the broker queue's per-event budget. The server treats `data` as
    # opaque JSONB and is agnostic to the inline/ref form — same modeling
    # discipline as items / artifacts.
    #
    # FK to dialogs uses ON DELETE CASCADE for defense-in-depth — the live
    # path never hard-deletes dialogs. routers/dialogs.py and
    # routers/workspaces.py perform the application-level cascade for the
    # normal soft-delete flow (messages join the dialogs+items+artifacts
    # batch under the same cascade_version when a workspace is deleted, or
    # the dialog tombstone batch when a single dialog is deleted).
    #
    # Indices: (user_id, dialog_id, version) is the scoped-pull main path
    # (`?dialogId=Y&since=N`). user_version is kept as a secondary index
    # for cross-dialog admin / migration queries; the per-dialog scoped
    # pull is the dominant access pattern.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    dialog_id: Mapped[str] = mapped_column(
        String,
        ForeignKey('dialogs.id', ondelete='CASCADE'),
        nullable=False,
    )
    data: Mapped[Any] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(
        BigInteger,
        global_change_seq,
        server_default=global_change_seq.next_value(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('now()'),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Stage 4.5 / Step 3 — see provider.py for rationale.
    imported_from_job_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    imported_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Stage 4.5 / Step 4 — Phase C 写入时打标，Phase D 扫描 + 置 FALSE。
    # 见 migration c5e9f2a8d6b4 的 docstring 说明为什么单列 + partial index
    # 而不是把判定烧进 JSONB 表达式。注意：`_` 前缀提示 wire envelope 不
    # 暴露此列（messages router `_to_row` / `_to_event` 只取 `data` JSONB）。
    _pending_blob_extraction: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text('FALSE'),
    )

    __table_args__ = (
        # Composite index drives the scoped-pull main query
        # (WHERE user_id = :u AND dialog_id = :d AND version > :since
        #  ORDER BY version). version comes last so PG can use it for both
        # equality on (user, dialog) and range-scan on version.
        Index(
            'ix_messages_user_dialog_version',
            'user_id', 'dialog_id', 'version',
        ),
        Index('ix_messages_user_version', 'user_id', 'version'),
        Index('ix_messages_dialog', 'dialog_id'),
        Index('ix_messages_imported_from_job_id', 'imported_from_job_id'),
        # Partial index: only TRUE rows make it in. Phase D scan stays
        # `WHERE user_id = :u AND _pending_blob_extraction = TRUE`.
        Index(
            'ix_messages_pending_blob_extraction',
            '_pending_blob_extraction',
            postgresql_where=text('_pending_blob_extraction = TRUE'),
        ),
    )
