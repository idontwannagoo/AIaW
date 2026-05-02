"""assistants + avatar_images + installed_plugins tables

Stage 3 / 批次-3b：3 张叶子表合一批落地。
- assistants: id-PK，与 providers 同形 (单列 id + user_id check)
- avatar_images: id-PK，data JSONB 中的 contentBuffer 由前端 base64 编码后写入
- installed_plugins: 复合 PK (user_id, key)，与 reactives 同形（plugin key
  跨用户可碰撞）；data 是 InstalledPlugin 整行 (区别于 reactives 的 value 单值)

Revision ID: e4b2c5f9d017
Revises: d6a3f8c91e22
Create Date: 2026-05-02 23:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e4b2c5f9d017'
down_revision: Union[str, None] = 'd6a3f8c91e22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_id_pk_table(name: str, version_index_name: str) -> None:
    op.create_table(
        name,
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
        version_index_name, name, ['user_id', 'version'], unique=False,
    )


def upgrade() -> None:
    _create_id_pk_table('assistants', 'ix_assistants_user_version')
    _create_id_pk_table('avatar_images', 'ix_avatar_images_user_version')

    op.create_table(
        'installed_plugins',
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
        sa.PrimaryKeyConstraint('user_id', 'key', name='installed_plugins_pkey'),
    )
    op.create_index(
        'ix_installed_plugins_user_version',
        'installed_plugins',
        ['user_id', 'version'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_installed_plugins_user_version', table_name='installed_plugins'
    )
    op.drop_table('installed_plugins')
    op.drop_index('ix_avatar_images_user_version', table_name='avatar_images')
    op.drop_table('avatar_images')
    op.drop_index('ix_assistants_user_version', table_name='assistants')
    op.drop_table('assistants')
