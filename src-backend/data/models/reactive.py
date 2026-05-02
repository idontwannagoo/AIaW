from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Index, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class Reactive(Base):
    __tablename__ = 'reactives'

    # KV table: a key like '#user-data' / '#user-perfs' is reused across
    # users, so the natural identity is composite (user_id, key) — distinct
    # from providers which has a single-column id PK with a user_id check.
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
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
        PrimaryKeyConstraint('user_id', 'key', name='reactives_pkey'),
        Index('ix_reactives_user_version', 'user_id', 'version'),
    )
