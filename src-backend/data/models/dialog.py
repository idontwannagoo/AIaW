from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class Dialog(Base):
    __tablename__ = 'dialogs'

    # id-PK pattern (mirrors Workspace / Assistant): client-generated UUIDs,
    # ownership enforced by user_id column. `data` carries the full Dialog row
    # JSON (incl. `name`, `assistantId`, `msgTree`, `msgRoute`,
    # `msgBranchState`, `inputVars`, `modelOverride`).
    #
    # Schema decisions (Stage 4 / 批次-4b):
    # - workspace_id is promoted to its own column (not buried inside `data`)
    #   so the FK to workspaces and the cascade-delete query
    #   (DELETE FROM dialogs WHERE workspace_id = :ws + soft-delete bump) can
    #   stay set-based. The frontend-visible `workspaceId` field inside
    #   `data` is kept for byte-identical round-trip — both representations
    #   carry the same value, populated client-side via `genId()`.
    # - The FK declares ON DELETE CASCADE for defense-in-depth (only fires on
    #   *hard* delete of a workspaces row, which we don't do today; the live
    #   path is soft-delete). The application-level cascade in
    #   routers/workspaces.py is what actually clears dialogs on a tombstone
    #   workspace.
    # - Cascade behavior to *child* tables (messages / items / artifacts)
    #   stays no-op at 4b — those tables are still in Dexie. 4c/4d/4e fold
    #   them in.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey('workspaces.id', ondelete='CASCADE'),
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
        Index('ix_dialogs_user_version', 'user_id', 'version'),
        Index('ix_dialogs_workspace', 'workspace_id'),
    )
