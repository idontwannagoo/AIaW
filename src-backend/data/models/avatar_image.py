from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class AvatarImage(Base):
    __tablename__ = 'avatar_images'

    # id-PK pattern (mirrors Provider). The `contentBuffer` field on the
    # AvatarImage row is an ArrayBuffer at the type level; on the wire it is
    # base64-encoded inside `data` so the JSONB column stays string-safe.
    # < 64KB rows go inline here (no object-store split until Stage 4 hard-pre-2).
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
    # Stage 4.5 / Step 3 — see provider.py for rationale.
    imported_from_job_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    imported_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index('ix_avatar_images_user_version', 'user_id', 'version'),
        Index(
            'ix_avatar_images_imported_from_job_id',
            'imported_from_job_id',
        ),
    )
