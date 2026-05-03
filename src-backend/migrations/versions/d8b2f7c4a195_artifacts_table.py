"""artifacts table

Stage 4 / 批次-4d：artifacts 表迁到 backend，schema 与 dialogs 同形（id-PK +
user_id check + JSONB data + version BigInteger 走 global change seq +
deleted_at soft-delete tombstone）+ workspace_id 提到顶层列、加 ON DELETE
CASCADE FK 到 workspaces.id。FK 仅在 hard-delete workspaces 时才触发；live
路径走 routers/workspaces.py 里的 application-level cascade（cascade=true
时同事务把 dialogs + items + artifacts 一并 tombstone + 各发一条 WS event，
共用 cascade_version）。

artifact 大版本文本走对象存储——data.versions[].text 在 wire 上由
blob-client.ts 决定 inline / ref：JSON 序列化整体 < 64KB 直接内联，>= 64KB
spill 到 data.versionsBlob 为 AttachmentEnvelope ref，bytes 上 BlobStore。
后端把 data 当 opaque JSON 处理，schema 不感知 inline / ref。

注：plan 2026-05-03 修订原写「workspaceId / dialogId 双 FK」，但
src/utils/types.ts::Artifact 实际只有 workspaceId（无 dialogId）。按 4c
items 修订记录先例，schema 按代码事实做单 FK；plan 后续刷新对齐。

Revision ID: d8b2f7c4a195
Revises: c4f9e2a8d6b3
Create Date: 2026-05-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd8b2f7c4a195'
down_revision: Union[str, None] = 'c4f9e2a8d6b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'artifacts',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('workspace_id', sa.String(), nullable=False),
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
            ['workspace_id'], ['workspaces.id'],
            name='fk_artifacts_workspace_id',
            ondelete='CASCADE',
        ),
    )
    op.create_index(
        'ix_artifacts_user_version', 'artifacts', ['user_id', 'version'],
        unique=False,
    )
    op.create_index(
        'ix_artifacts_workspace', 'artifacts', ['workspace_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_artifacts_workspace', table_name='artifacts')
    op.drop_index('ix_artifacts_user_version', table_name='artifacts')
    op.drop_table('artifacts')
