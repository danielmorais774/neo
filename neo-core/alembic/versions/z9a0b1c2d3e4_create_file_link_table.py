"""create_file_link_table

Registro de vínculos de arquivo (docs/file-links-plan.md): uma linha por
movimento de arquivo entre o disco do usuário, o catálogo e o sandbox.

Aditiva: nada existente é tocado. Rollback = dropar a tabela.

Revision ID: z9a0b1c2d3e4
Revises: y8z9a0b1c2d3
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "z9a0b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = "y8z9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if inspect(op.get_bind()).has_table("file_link"):
        return
    op.create_table(
        "file_link",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("doc_key", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("host_path", sa.String(length=1024), nullable=True),
        sa.Column("sandbox_path", sa.String(length=1024), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="ok"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_file_link_user_id", "file_link", ["user_id"])
    op.create_index("ix_file_link_session_id", "file_link", ["session_id"])
    op.create_index("ix_file_link_doc_key", "file_link", ["doc_key"])
    op.create_index("ix_file_link_file_id", "file_link", ["file_id"])
    op.create_index("ix_file_link_created_at", "file_link", ["created_at"])


def downgrade() -> None:
    op.drop_table("file_link")
