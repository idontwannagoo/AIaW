"""import_jobs table

Stage 4.5 / Step 1：建 import_jobs 表 + partial unique index 限制每用户同一时间
最多 1 个 active job（status 落在 non-terminal 集合：queued/uploading/
assembling/parsing/phase_b/phase_c/phase_d）。终态（done/failed/cancelled）
不参与唯一约束，历史 job 可累积。

`version` 共享 global_change_seq（建在 b9f5b373903c），让 import_jobs 的进度
推进 event 与其他 server-routed 表共用同一个 per-user since 时间线 — 前端订
阅 `import_jobs` 与订阅 workspaces / dialogs 走同一 ?since 游标，无需新通道。

Revision ID: a91f3c5e8d2b
Revises: e7c4a821f693
Create Date: 2026-05-03 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a91f3c5e8d2b'
down_revision: Union[str, None] = 'e7c4a821f693'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 与 models/import_job.py 的 NON_TERMINAL_STATUSES 保持一致；如改了状态机
# 字面量必须同步两处 + 写新 migration 重建 partial index。
_NON_TERMINAL = (
    'queued',
    'uploading',
    'assembling',
    'parsing',
    'phase_b',
    'phase_c',
    'phase_d',
)


def upgrade() -> None:
    op.create_table(
        'import_jobs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column(
            'status',
            sa.String(),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column('multipart_upload_id', sa.String(), nullable=True),
        sa.Column('raw_object_key', sa.String(), nullable=True),
        sa.Column('total_bytes', sa.BigInteger(), nullable=True),
        sa.Column(
            'processed_bytes',
            sa.BigInteger(),
            server_default=sa.text('0'),
            nullable=False,
        ),
        sa.Column('total_rows', sa.BigInteger(), nullable=True),
        sa.Column(
            'processed_rows',
            sa.BigInteger(),
            server_default=sa.text('0'),
            nullable=False,
        ),
        sa.Column('total_blobs', sa.Integer(), nullable=True),
        sa.Column(
            'processed_blobs',
            sa.Integer(),
            server_default=sa.text('0'),
            nullable=False,
        ),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column(
            'dead_letter',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            'version',
            sa.BigInteger(),
            server_default=sa.text("nextval('global_change_seq')"),
            nullable=False,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id', name='import_jobs_pkey'),
    )
    op.create_index(
        'ix_import_jobs_user_version',
        'import_jobs',
        ['user_id', 'version'],
        unique=False,
    )
    # Partial unique index — 每用户同时只允许 1 个 active job。
    # 注意 SQLAlchemy 里用 postgresql_where 表达 PG WHERE clause；不能用普通
    # UniqueConstraint(unique=True) 因为那是无条件的。
    statuses_sql = ', '.join(f"'{s}'" for s in _NON_TERMINAL)
    op.create_index(
        'uq_import_jobs_active_per_user',
        'import_jobs',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text(f'status IN ({statuses_sql})'),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_import_jobs_active_per_user', table_name='import_jobs'
    )
    op.drop_index('ix_import_jobs_user_version', table_name='import_jobs')
    op.drop_table('import_jobs')
