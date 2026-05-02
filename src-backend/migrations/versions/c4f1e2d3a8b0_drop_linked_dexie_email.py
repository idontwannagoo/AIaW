"""drop linked_dexie_email column from users

Stage 2.5：导入导出 only 迁移路径下不再需要把 backend 账号 ↔ Dexie email
关联起来。drop 列 + UNIQUE 约束。

Revision ID: c4f1e2d3a8b0
Revises: 72ae82a9bb6d
Create Date: 2026-05-02 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4f1e2d3a8b0'
down_revision: Union[str, None] = '72ae82a9bb6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('users_linked_dexie_email_key', type_='unique')
        batch.drop_column('linked_dexie_email')


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('linked_dexie_email', sa.String(), nullable=True))
        batch.create_unique_constraint(
            'users_linked_dexie_email_key', ['linked_dexie_email']
        )
