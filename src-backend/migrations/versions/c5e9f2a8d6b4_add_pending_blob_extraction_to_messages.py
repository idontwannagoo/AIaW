"""add _pending_blob_extraction to messages

Stage 4.5 / Step 4 — Phase C 写入 messages 文字时，对含 attachment（base64
inline 或 contentsBlob ref envelope，或 row JSON 整体超过 BLOB_INLINE_MAX_BYTES
即 64KB 的）行打标，留给 Phase D 去把大附件转成对象存储 ref，并把行 size
压回到广播队列友好的范围。

为什么单独加列而不是「扫 data JSONB 的某个字段」做条件谓词：

- Phase D 的扫描语义是 set-based 单 SQL `SELECT id, data FROM messages
  WHERE _pending_blob_extraction = TRUE AND user_id = :u`，靠 partial
  index 避免每次都全表扫。把判定条件烧进 JSONB 表达式 (`WHERE
  jsonb_path_exists(data, '$.contentsBlob ? (@.type == "ref")')`) 可以
  省一列但拿不到 partial b-tree index 加速，扫描成本随 messages 总量
  线性增长。Phase D 还要在转 ref 后清这个标记 ──
  显式列让 UPDATE 一次置 FALSE，比 jsonb 路径条件改写干净。

- Phase C 写入时已经知道这条 row 是否「需要 Phase D 处理」（行刚被
  序列化扫过），存进显式列零额外成本；让 Phase D 重新扫每行 JSON 再决定
  反而把 worker 跟「客户端 spill 阈值」耦合得更紧 —— 阈值若改，Phase D
  扫描语义跟着漂；改 Phase C 写入策略只需要改 _should_pending_blob_extraction
  helper，不动迁移。

partial index `(_pending_blob_extraction) WHERE _pending_blob_extraction
= TRUE` 让 Phase D 扫描成本与「待处理行数」线性相关，与 messages 总量
无关；TRUE 行被 Phase D 处理完置 FALSE 后立刻从 index 中消失，不会越积
越大。

Revision ID: c5e9f2a8d6b4
Revises: b3e7d4f8a1c5
Create Date: 2026-05-03 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5e9f2a8d6b4'
down_revision: Union[str, None] = 'b3e7d4f8a1c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'messages',
        sa.Column(
            '_pending_blob_extraction',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('FALSE'),
        ),
    )
    # Partial index: only TRUE rows are indexed. Phase D's main SELECT
    # `WHERE user_id = :u AND _pending_blob_extraction = TRUE` becomes
    # an index-only scan over the (typically small) pending set.
    op.create_index(
        'ix_messages_pending_blob_extraction',
        'messages',
        ['_pending_blob_extraction'],
        unique=False,
        postgresql_where=sa.text('_pending_blob_extraction = TRUE'),
    )


def downgrade() -> None:
    op.drop_index(
        'ix_messages_pending_blob_extraction', table_name='messages'
    )
    op.drop_column('messages', '_pending_blob_extraction')
