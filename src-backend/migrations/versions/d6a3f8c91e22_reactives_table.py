"""reactives KV table

Stage 3 / 批次-3a：KV 形主键 (user_id, key) — 同一个 key 在不同用户名下不冲突，
所以不能复用 providers 那种 single-column id PK + user_id check。version 列共享
全局 global_change_seq（建在 b9f5b373903c 里），用户的 ?since=N 仍是单调时间线。

Revision ID: d6a3f8c91e22
Revises: c4f1e2d3a8b0
Create Date: 2026-05-02 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd6a3f8c91e22'
down_revision: Union[str, None] = 'c4f1e2d3a8b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reactives',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
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
        sa.PrimaryKeyConstraint('user_id', 'key', name='reactives_pkey'),
    )
    op.create_index(
        'ix_reactives_user_version',
        'reactives',
        ['user_id', 'version'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_reactives_user_version', table_name='reactives')
    op.drop_table('reactives')
