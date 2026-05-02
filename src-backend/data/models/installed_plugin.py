from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Index, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .provider import global_change_seq


class InstalledPlugin(Base):
    __tablename__ = 'installed_plugins'

    # KV-shaped (mirrors Reactive): plugin `key` is a manifest-derived string
    # like 'lobe:foo' / 'mcp:bar' — the same key may belong to multiple users
    # so the natural identity is composite (user_id, key). `data` carries the
    # full InstalledPlugin row (distinct from reactives where `data` is just
    # the value blob — the row is the data here).
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
        PrimaryKeyConstraint('user_id', 'key', name='installed_plugins_pkey'),
        Index('ix_installed_plugins_user_version', 'user_id', 'version'),
    )
