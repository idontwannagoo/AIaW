"""items table

Stage 4 / 批次-4c：items 表迁到 backend，schema 与 dialogs 同形（id-PK +
user_id check + JSONB data + version BigInteger 走 global change seq +
deleted_at soft-delete tombstone）+ dialog_id 提到顶层列、加 ON DELETE
CASCADE FK 到 dialogs.id。FK 仅在 hard-delete dialogs 时才触发；live 路径
走 routers/workspaces.py 里的 application-level cascade（cascade=true 时同
事务把 dialogs + items 一并 tombstone + 各发一条 WS event，共用 cascade
_version）。

items 是消息附件载体——StoredItem.contentBuffer:ArrayBuffer 在 wire 上是
AttachmentEnvelope（< 64KB inline base64 / >= 64KB ref 上 BlobStore），但
后端把 data 当 opaque JSON 处理，schema 不感知 inline/ref。

Revision ID: c4f9e2a8d6b3
Revises: b5d2c8e7a394
Create Date: 2026-05-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c4f9e2a8d6b3'
down_revision: Union[str, None] = 'b5d2c8e7a394'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'items',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('dialog_id', sa.String(), nullable=False),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'version',
            sa.BigInteger(),
            server_default=sa.text("nextval('global_change_seq')"),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['dialog_id'], ['dialogs.id'],
            name='fk_items_dialog_id',
            ondelete='CASCADE',
        ),
    )
    op.create_index(
        'ix_items_user_version', 'items', ['user_id', 'version'],
        unique=False,
    )
    op.create_index(
        'ix_items_dialog', 'items', ['dialog_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_items_dialog', table_name='items')
    op.drop_index('ix_items_user_version', table_name='items')
    op.drop_table('items')
