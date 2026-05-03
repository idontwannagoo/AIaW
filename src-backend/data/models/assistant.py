from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class Assistant(Base):
    __tablename__ = 'assistants'

    # id-PK pattern (mirrors Provider): client-generated UUIDs, ownership
    # enforced by the user_id column not by composite PK — `data` carries the
    # full Assistant row as JSON.
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
        Index('ix_assistants_user_version', 'user_id', 'version'),
        Index('ix_assistants_imported_from_job_id', 'imported_from_job_id'),
    )
