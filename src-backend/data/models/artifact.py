from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class Artifact(Base):
    __tablename__ = 'artifacts'

    # Stage 4 / 批次-4d — artifacts (markdown / code snippets edited
    # alongside a dialog).
    #
    # id-PK envelope (mirrors Dialog / Item) with `workspace_id` promoted to
    # its own FK column so the workspace-cascade query stays set-based.
    #
    # Note on scope: the frontend `Artifact` type carries ONLY `workspaceId`
    # (no `dialogId`). Plan 2026-05-03 修订 originally listed double-FK
    # (workspaceId + dialogId) as the schema decision but that doesn't match
    # the source-of-truth shape in `src/utils/types.ts::Artifact`. We follow
    # the same precedent the 4c items refresh set: code wins over stale plan
    # text, and the test agent / next plan revision will reflect the
    # single-scope reality. If a future product change adds dialogId to
    # artifacts, that's a separate alembic migration to add the column +
    # FK + index, plus an update to scoped-pull.ts (which today is single
    # scope by design).
    #
    # `data` JSONB carries the full Artifact row:
    #   { id, name, workspaceId, versions: ArtifactVersion[],
    #     currIndex, readable, writable, open, language?, tmp }
    # where `versions[].text` can be large. Client-side blob-client.ts
    # decides whether to ship `versions` inline or to spill into a
    # `versionsBlob: AttachmentEnvelope` ref (>= 64KB JSON). The server
    # treats `data` as opaque JSONB — same modeling discipline as items /
    # dialogs / workspaces; PG does not split attachment fields.
    #
    # FK to workspaces uses ON DELETE CASCADE for defense-in-depth — the
    # live path never hard-deletes workspaces. routers/workspaces.py
    # performs the application-level cascade for the normal soft-delete
    # flow (artifacts join the dialogs + items batch under the same
    # cascade_version).
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
        Index('ix_artifacts_user_version', 'user_id', 'version'),
        Index('ix_artifacts_workspace', 'workspace_id'),
    )
