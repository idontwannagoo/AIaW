"""blobs + blob_refs tables — Stage 4 硬前置 2

content-addressed object store:
- blobs       : sha256 PK，全局去重，记 size / content_type / storage_key
- blob_refs   : (user_id, sha256) 复合 PK，per-user ownership；FK 引用 blobs.sha256
                 ON DELETE CASCADE。GC 由 sha256 上的反向索引支撑

Revision ID: f1a3b8c5d4e2
Revises: e4b2c5f9d017
Create Date: 2026-05-02 23:59:30.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a3b8c5d4e2'
down_revision: Union[str, None] = 'e4b2c5f9d017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'blobs',
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('content_type', sa.String(length=255), nullable=False),
        sa.Column('storage_key', sa.String(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('sha256', name='blobs_pkey'),
    )

    op.create_table(
        'blob_refs',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column(
            'last_seen_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['sha256'],
            ['blobs.sha256'],
            ondelete='CASCADE',
            name='blob_refs_sha256_fkey',
        ),
        sa.PrimaryKeyConstraint('user_id', 'sha256', name='blob_refs_pkey'),
    )
    op.create_index('ix_blob_refs_sha256', 'blob_refs', ['sha256'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_blob_refs_sha256', table_name='blob_refs')
    op.drop_table('blob_refs')
    op.drop_table('blobs')
