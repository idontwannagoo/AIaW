from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, DateTime, Index, String, Sequence, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

# Single global sequence: every write across every table draws from it,
# so a client's ?since=<rev> can scan a single monotonic timeline.
global_change_seq = Sequence('global_change_seq')


class Provider(Base):
    __tablename__ = 'providers'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
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
    # Stage 4.5 / Step 3 — backref to the ImportJob that wrote this row, used
    # by `DELETE /api/v1/import/jobs/{id}` to cascade-soft-delete partial
    # writes when a user cancels an import. Both columns are NULL on rows
    # written via the regular live API (PUT /api/v1/providers/...). Not part
    # of the wire envelope — pure PG bookkeeping.
    imported_from_job_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    imported_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index('ix_providers_user_version', 'user_id', 'version'),
        Index('ix_providers_imported_from_job_id', 'imported_from_job_id'),
    )
