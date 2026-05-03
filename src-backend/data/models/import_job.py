"""ImportJob — Stage 4.5 Step 1.

Tracks a single user-initiated database import (老用户从 my-deploy 导出的
`aiaw_user_db.json` → 上传到 new-deploy 后端 → worker 解析并写入 PG）.

State machine（status 字段，按推进顺序）:

    queued → uploading → assembling → parsing
       ↓
    phase_b → phase_c → phase_d → done
                                ↘ failed / cancelled

Step 1 只实现 `parsing`（Phase A：ijson 流式 parse raw JSON 写本地 NDJSON）+
后续 phase stub。后端把 ImportJob 当作普通 server-routed 表通过 WS 推进度
（`_envelope()` 与其他 server-routed 表 envelope 形态保持一致 —
`{id, version, updated_at, deleted, data}`），前端订阅同一通道更新 banner。

Active job 唯一性：partial unique index `(user_id) WHERE status IN
('queued','uploading','assembling','parsing','phase_b','phase_c','phase_d')`
保证每用户同一时间只 1 个 active job —— 避免老用户多 tab / 反复点导致并行
worker 互踩临时目录 / 重复写入。终态（done / failed / cancelled）不参与唯一
性约束，可历史保留。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


# 与 plan line 1076 列出的状态字面量保持一致。non-terminal 列表用于 partial
# unique index 与 worker resume 扫描两处复用，避免硬编码漂移。
NON_TERMINAL_STATUSES: tuple[str, ...] = (
    'queued',
    'uploading',
    'assembling',
    'parsing',
    'phase_b',
    'phase_c',
    'phase_d',
)
TERMINAL_STATUSES: tuple[str, ...] = ('done', 'failed', 'cancelled')
ALL_STATUSES: tuple[str, ...] = NON_TERMINAL_STATUSES + TERMINAL_STATUSES


class ImportJob(Base):
    __tablename__ = 'import_jobs'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'queued'")
    )

    # Step 2 fills these in when the multipart upload starts.
    multipart_upload_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    raw_object_key: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )

    # Progress counters. NULL until the relevant phase fills them in.
    total_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    processed_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text('0')
    )
    total_rows: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    processed_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text('0')
    )
    total_blobs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processed_blobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text('0')
    )

    error_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Per-row failures Phase C/D collect for inspect-and-retry; default `[]` so
    # `worker.append_dead_letter()` can treat the column as a list unconditionally.
    dead_letter: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # version on the same global_change_seq other server-routed tables share —
    # so the WS broker's per-user since cursor stays a single monotonic timeline
    # across import_jobs + workspaces + dialogs + ... .
    version: Mapped[int] = mapped_column(
        BigInteger,
        global_change_seq,
        server_default=global_change_seq.next_value(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('now()'),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('now()'),
        nullable=False,
    )

    __table_args__ = (
        # Cursor-pull main path mirrors the rest of the data-API tables.
        Index('ix_import_jobs_user_version', 'user_id', 'version'),
        # The partial unique index itself can't be expressed via a simple
        # Index(unique=True) here because SQLAlchemy needs the WHERE clause —
        # we add it in the alembic migration with `postgresql_where`. Keep the
        # index *named* the same here for orientation when grepping.
    )

    def _envelope(self) -> dict[str, Any]:
        """Wire envelope identical in shape to other server-routed tables.

        `data` is a status snapshot the frontend can render directly into the
        progress banner without a second roundtrip. Counters that are still
        NULL stay None on the wire (frontend treats as "not yet known").
        """
        return {
            'id': self.id,
            'version': int(self.version) if self.version is not None else 0,
            'updated_at': (
                self.updated_at.isoformat() if self.updated_at else None
            ),
            'deleted': False,
            'data': {
                'id': self.id,
                'status': self.status,
                'multipart_upload_id': self.multipart_upload_id,
                'raw_object_key': self.raw_object_key,
                'total_bytes': (
                    int(self.total_bytes) if self.total_bytes is not None else None
                ),
                'processed_bytes': int(self.processed_bytes),
                'total_rows': (
                    int(self.total_rows) if self.total_rows is not None else None
                ),
                'processed_rows': int(self.processed_rows),
                'total_blobs': (
                    int(self.total_blobs) if self.total_blobs is not None else None
                ),
                'processed_blobs': int(self.processed_blobs),
                'error_message': self.error_message,
                'dead_letter': self.dead_letter or [],
                'created_at': (
                    self.created_at.isoformat() if self.created_at else None
                ),
                'updated_at': (
                    self.updated_at.isoformat() if self.updated_at else None
                ),
            },
        }
