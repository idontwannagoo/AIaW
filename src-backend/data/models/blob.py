"""Blob (object-storage) tables — Stage 4 硬前置 2.

Two-table design:
    - `blobs`     : content-addressed by sha256, deduped globally. The bytes
                    live in BlobStore (LocalFS / S3). This row carries the
                    metadata (size, content_type, storage_key).
    - `blob_refs` : per-user ownership. Granting access to a sha256 means a
                    `(user_id, sha256)` row exists. GET endpoints check this.
                    A future Stage 4 GC job will scan blobs that have zero
                    refs for > 7d and delete the bytes.

The bytes are deduped *globally* by sha256 (same content, single copy on disk
or in S3). Per-user isolation is enforced by `blob_refs` membership: user B
cannot read user A's blob unless B independently uploaded the same content.
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class Blob(Base):
    __tablename__ = 'blobs'

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    # Logical key into the BlobStore. For LocalFS this is the sha256; for S3
    # implementations it's typically `<bucket-prefix>/<sha256>`. Decoupling the
    # BlobStore key from the sha256 lets us swap stores without rewriting refs.
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('now()'),
        nullable=False,
    )


class BlobRef(Base):
    __tablename__ = 'blob_refs'

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey('blobs.sha256', ondelete='CASCADE'),
        primary_key=True,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text('now()'),
        nullable=False,
    )

    __table_args__ = (
        # Reverse lookup for the GC job: scan blobs with no refs.
        Index('ix_blob_refs_sha256', 'sha256'),
    )
