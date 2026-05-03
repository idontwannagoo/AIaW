"""messages table

Stage 4 / 批次-4e：messages 表迁到 backend，schema 与 items 同形（id-PK +
user_id check + JSONB data + version BigInteger 走 global change seq +
deleted_at soft-delete tombstone）+ dialog_id 提到顶层列、加 ON DELETE
CASCADE FK 到 dialogs.id。FK 仅在 hard-delete dialogs 时才触发；live 路径
走 routers/dialogs.py + routers/workspaces.py 里的 application-level
cascade（dialog 删除时同事务把 messages 一并 tombstone + 各发一条 WS
event；workspace cascade=true 时与 dialogs+items+artifacts 共用同一
cascade_version 一并发出）。

message body 走 inline / ref 阈值由客户端 blob-client.ts 决定：
JSON.stringify(data) 整体 < 64KB 直接内联，>= 64KB spill 到 data.contentsBlob
为 AttachmentEnvelope ref，bytes 上 BlobStore。后端把 data 当 opaque JSON
处理，schema 不感知 inline / ref。

索引设计：(user_id, dialog_id, version) 复合索引承担 scoped-pull 主路径
(`?dialogId=Y&since=N`，messages 全表无索引情况下扫表无意义，因为单 user
messages 行数轻易上万；user_version + dialog 单列索引保留作为兜底)。

Revision ID: e7c4a821f693
Revises: d8b2f7c4a195
Create Date: 2026-05-03 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e7c4a821f693'
down_revision: Union[str, None] = 'd8b2f7c4a195'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'messages',
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
            name='fk_messages_dialog_id',
            ondelete='CASCADE',
        ),
    )
    op.create_index(
        'ix_messages_user_dialog_version', 'messages',
        ['user_id', 'dialog_id', 'version'], unique=False,
    )
    op.create_index(
        'ix_messages_user_version', 'messages',
        ['user_id', 'version'], unique=False,
    )
    op.create_index(
        'ix_messages_dialog', 'messages', ['dialog_id'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_messages_dialog', table_name='messages')
    op.drop_index('ix_messages_user_version', table_name='messages')
    op.drop_index('ix_messages_user_dialog_version', table_name='messages')
    op.drop_table('messages')
