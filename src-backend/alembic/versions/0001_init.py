"""initial sync schema

Revision ID: 0001_init
Revises:
Create Date: 2026-04-19 00:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_init"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ENTITY_TABLES = [
    ("workspaces", [("parent_id", sa.String(64)), ("type", sa.String(32))]),
    ("dialogs", [("workspace_id", sa.String(64))]),
    ("messages", [("dialog_id", sa.String(64)), ("type", sa.String(32))]),
    ("assistants", [("workspace_id", sa.String(64))]),
    ("artifacts", [("workspace_id", sa.String(64))]),
    ("items", [("dialog_id", sa.String(64)), ("type", sa.String(32))]),
    ("avatar_images", []),
    ("installed_plugins", []),
    ("reactives", []),
    ("providers", []),
]

SCOPE_INDEX = {
    "dialogs": "workspace_id",
    "messages": "dialog_id",
    "assistants": "workspace_id",
    "artifacts": "workspace_id",
    "items": "dialog_id",
}


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True, index=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    for name, extra in ENTITY_TABLES:
        cols = [
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("data", postgresql.JSONB, nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        ]
        for col_name, col_type in extra:
            cols.append(sa.Column(col_name, col_type, nullable=col_name != "type"))
        op.create_table(name, *cols)
        op.create_index(f"ix_{name}_user_updated", name, ["user_id", "updated_at"])
        scope = SCOPE_INDEX.get(name)
        if scope:
            op.create_index(f"ix_{name}_user_{scope}", name, ["user_id", scope])
        if name == "workspaces":
            op.create_index("ix_workspaces_parent_id", name, ["parent_id"])

    op.create_table(
        "files",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("mime_type", sa.String(127), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("files")
    for name, _ in reversed(ENTITY_TABLES):
        op.drop_table(name)
    op.drop_table("users")
