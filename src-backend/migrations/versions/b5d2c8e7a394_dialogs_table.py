"""dialogs table

Stage 4 / 批次-4b：dialogs 表迁到 backend，schema 与 workspaces 同形
（id-PK + user_id check + JSONB data + version BigInteger 走 global
change seq + deleted_at soft-delete tombstone）+ workspace_id 提到顶层
列、加 ON DELETE CASCADE FK 到 workspaces.id。FK 仅在 hard-delete
workspaces 时才触发；live 路径走 routers/workspaces.py 里的
application-level cascade（cascade=true 时同事务把 dialogs 也 tombstone
+ 各发一条 WS event）。

Revision ID: b5d2c8e7a394
Revises: a7f3c2e9b481
Create Date: 2026-05-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b5d2c8e7a394'
down_revision: Union[str, None] = 'a7f3c2e9b481'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dialogs',
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
            name='fk_dialogs_workspace_id',
            ondelete='CASCADE',
        ),
    )
    op.create_index(
        'ix_dialogs_user_version', 'dialogs', ['user_id', 'version'],
        unique=False,
    )
    op.create_index(
        'ix_dialogs_workspace', 'dialogs', ['workspace_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_dialogs_workspace', table_name='dialogs')
    op.drop_index('ix_dialogs_user_version', table_name='dialogs')
    op.drop_table('dialogs')
