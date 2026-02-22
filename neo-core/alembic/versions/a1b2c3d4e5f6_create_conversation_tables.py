"""create_conversation_tables

Revision ID: a1b2c3d4e5f6
Revises: 04cf18aa2e51
Create Date: 2026-02-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "04cf18aa2e51"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pgvector extension (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── conversation_session ──────────────────────────────────────
    op.create_table(
        "conversation_session",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("user.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("topic_summary", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("session_metadata", sa.JSON(), nullable=True),
    )

    # Add vector column separately (raw SQL — pgvector type)
    op.execute(
        "ALTER TABLE conversation_session ADD COLUMN topic_embedding vector(1536)"
    )

    # ── conversation_message ──────────────────────────────────────
    op.create_table(
        "conversation_message",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("conversation_session.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=True),
        sa.Column("tool_call_id", sa.String(64), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("msg_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # Vector column for message embedding
    op.execute(
        "ALTER TABLE conversation_message ADD COLUMN embedding vector(1536)"
    )

    # Composite index for efficient session history queries
    op.create_index(
        "ix_conv_msg_session_created",
        "conversation_message",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conv_msg_session_created", table_name="conversation_message")
    op.drop_table("conversation_message")
    op.drop_table("conversation_session")
