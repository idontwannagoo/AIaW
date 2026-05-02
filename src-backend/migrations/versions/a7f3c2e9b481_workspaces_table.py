"""workspaces table

Stage 4 / 批次-4a：workspaces 表迁到 backend，schema 与 assistants 同形
（id-PK + user_id check + JSONB data + version BigInteger 走 global
change seq + deleted_at soft-delete tombstone）。

Revision ID: a7f3c2e9b481
Revises: f1a3b8c5d4e2
Create Date: 2026-05-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7f3c2e9b481'
down_revision: Union[str, None] = 'f1a3b8c5d4e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'workspaces',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
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
    )
    op.create_index(
        'ix_workspaces_user_version', 'workspaces', ['user_id', 'version'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_workspaces_user_version', table_name='workspaces')
    op.drop_table('workspaces')
