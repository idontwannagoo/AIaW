from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class Item(Base):
    __tablename__ = 'items'

    # Stage 4 / 批次-4c — items, the message attachment carrier.
    #
    # id-PK envelope (mirrors Dialog) with `dialog_id` promoted to its own
    # FK column so the workspace-cascade query stays set-based and the FK to
    # dialogs is enforceable. The frontend `StoredItem` shape lives entirely
    # inside `data` JSONB:
    #     { id, dialogId, type:'text'|'file'|'quote', references:number,
    #       contentText?:string, name?:string, mimeType?:string,
    #       contentBuffer?: AttachmentEnvelope }
    # where AttachmentEnvelope is one of:
    #     { type:'inline', data:base64, content_type, size }      (< 64KB)
    #     { type:'ref',    url, sha256, size, content_type }       (>= 64KB)
    # The decision is made client-side by `src/data/blob-client.ts` so the
    # server treats `data` as opaque JSON — same modeling discipline as
    # workspaces / dialogs. Bytes for ref-mode attachments live in the
    # `blobs` table (see Stage 4 hard-pre-2); items rows hold only refs.
    #
    # FK to dialogs: ON DELETE CASCADE is for defense-in-depth — the live
    # path never hard-deletes dialogs. routers/workspaces.py performs the
    # application-level cascade for the normal soft-delete flow.
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

    __table_args__ = (
        Index('ix_items_user_version', 'user_id', 'version'),
        Index('ix_items_dialog', 'dialog_id'),
    )
