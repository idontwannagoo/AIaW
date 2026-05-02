from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class Workspace(Base):
    __tablename__ = 'workspaces'

    # id-PK pattern (mirrors Provider / Assistant): client-generated UUIDs,
    # ownership enforced by the user_id column not by composite PK. `data`
    # carries the full Workspace | Folder row JSON (incl. `type`, `parentId`,
    # `name`, `avatar`, plus the workspace-only fields like `vars`,
    # `indexContent`, `defaultAssistantId`, `lastDialogId`, `listOpen`).
    #
    # Schema decisions (Stage 4 / 批次-4a):
    # - parentId is kept inside `data`, not promoted to its own column. The
    #   '$root' sentinel string from the existing Dexie schema is preserved
    #   verbatim — no self-FK, since Dexie callers already rely on that
    #   sentinel and a real FK would force every existing parent reference
    #   to point at a real row.
    # - `type` ({'workspace', 'folder'}) likewise stays inside `data`. The
    #   wire envelope is uniform across leaf and folder rows; downstream
    #   filters happen client-side via the existing observeFind queries.
    # - Cascade behavior for child tables (dialogs / messages / items /
    #   artifacts / assistants) is intentionally not implemented at 4a — only
    #   `assistants` is server-routed today, and the rest live in Dexie.
    #   Cascade will fold in naturally via Postgres ON DELETE CASCADE as 4b
    #   onward server-route those tables.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
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
        Index('ix_workspaces_user_version', 'user_id', 'version'),
    )
