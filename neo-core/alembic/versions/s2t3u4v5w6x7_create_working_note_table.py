"""create_working_note_table

Cria ``working_note`` — o caderno de trabalho do Neo
(docs/working-memory-plan.md §4). Uma linha por nota, ``body`` em markdown,
``slug`` único por usuário (é o alvo de ``[[slug]]`` e o argumento de
``wm_read``). Ver db/models/working_note.py para as decisões de modelagem.

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "s2t3u4v5w6x7"
down_revision: Union[str, Sequence[str], None] = "r1s2t3u4v5w6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if _table_exists("working_note"):
        return
    op.create_table(
        "working_note",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="active"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("links", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("origin", sa.String(length=12), nullable=False, server_default="model"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_working_note_user_id", "working_note", ["user_id"])
    op.create_index("ix_working_note_session_id", "working_note", ["session_id"])
    op.create_index("ix_working_note_slug", "working_note", ["slug"])
    op.create_index("ix_working_note_status", "working_note", ["status"])
    op.create_index("ix_working_note_updated_at", "working_note", ["updated_at"])
    # Varredura do GC: nota vencida e não-pinned.
    op.create_index("ix_working_note_expires_at", "working_note", ["expires_at"])
    # wm_write resolve a nota por (usuário, slug) — upsert, nunca duplica.
    op.create_index(
        "ux_working_note_user_slug", "working_note", ["user_id", "slug"], unique=True
    )
    # Montagem do índice do prompt: ativas do usuário, mais recentes primeiro.
    op.create_index(
        "ix_working_note_active", "working_note", ["user_id", "status", "updated_at"]
    )


def downgrade() -> None:
    if _table_exists("working_note"):
        op.drop_table("working_note")
