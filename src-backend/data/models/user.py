from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class User(Base):
    __tablename__ = 'users'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    # 'active' | 'disabled' | 'pending' — coarse lifecycle state.
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'active'")
    )
    # First-write-wins mapping to a Dexie Cloud account email; used by Stage 3+
    # data migration. Backend cannot verify the client really holds that Dexie
    # session, so the UNIQUE + first-write-wins limits the blast radius.
    linked_dexie_email: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('now()'),
        nullable=False,
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index('ix_users_email', 'email', unique=True),
    )
