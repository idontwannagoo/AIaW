"""add imported_from_job_id + imported_at to all server-routed tables

Stage 4.5 / Step 3 — Phase B 结构表写入需要给所有 server-routed 表加两列：

  - imported_from_job_id: NULLable String FK → import_jobs.id ON DELETE SET NULL
    （job 行被硬删时 backref 列置 NULL；live 路径不会 hard-delete jobs，但
    保留做防御）
  - imported_at: NULLable TIMESTAMP，worker 写入时填 now()

两列在 wire envelope 之外（不进 _to_row / _to_event 的 data 字段），仅作为
PG bookkeeping，用于：
  1. cancel/cleanup：DELETE /api/v1/import/jobs/{id} 调用时 worker 已写入的行
     按 (user_id, imported_from_job_id) 套索定位，整批 soft-delete；
  2. 未来观测：查询「这个 job 写了哪些行」「某行是哪次导入留下的」。

每张受影响的表独立索引 (imported_from_job_id)，因为 cancel cascade 是
per-job-id 的 set-based UPDATE，没有索引会全表扫；非 NULL 的 partial index
更省空间但 SET NULL on delete + 大多数行 imported_from_job_id IS NULL 时
partial 收益小，先用普通 b-tree 索引。

受影响的表（10 张）按 alembic 模型注册顺序枚举：providers / reactives /
assistants / installed_plugins / avatar_images / workspaces / dialogs /
items / artifacts / messages。新增 server-routed 表必须同步加这两列 —
plan 4.5 Step 3 的「未来 server-routed 表新增模板」会被 Step 6 加进
SERVER_ROUTED_TABLES 常量。

Revision ID: b3e7d4f8a1c5
Revises: a91f3c5e8d2b
Create Date: 2026-05-03 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3e7d4f8a1c5'
down_revision: Union[str, None] = 'a91f3c5e8d2b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that grow the two new columns. Order is migration-only — runtime
# code reads from import_worker.SERVER_ROUTED_TABLES (Step 3 introduces).
_TABLES: tuple[str, ...] = (
    'providers',
    'reactives',
    'assistants',
    'installed_plugins',
    'avatar_images',
    'workspaces',
    'dialogs',
    'items',
    'artifacts',
    'messages',
)


def upgrade() -> None:
    for tbl in _TABLES:
        op.add_column(
            tbl,
            sa.Column(
                'imported_from_job_id', sa.String(), nullable=True,
            ),
        )
        op.add_column(
            tbl,
            sa.Column(
                'imported_at',
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.create_foreign_key(
            f'fk_{tbl}_imported_from_job_id',
            tbl,
            'import_jobs',
            ['imported_from_job_id'],
            ['id'],
            ondelete='SET NULL',
        )
        op.create_index(
            f'ix_{tbl}_imported_from_job_id',
            tbl,
            ['imported_from_job_id'],
            unique=False,
        )


def downgrade() -> None:
    for tbl in _TABLES:
        op.drop_index(
            f'ix_{tbl}_imported_from_job_id', table_name=tbl,
        )
        op.drop_constraint(
            f'fk_{tbl}_imported_from_job_id', tbl, type_='foreignkey',
        )
        op.drop_column(tbl, 'imported_at')
        op.drop_column(tbl, 'imported_from_job_id')
