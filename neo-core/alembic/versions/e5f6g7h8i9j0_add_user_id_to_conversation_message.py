"""add user_id to conversation_message

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-04-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f6g7h8i9j0"
down_revision = "d4e5f6g7h8i9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_message",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=True),
    )
    op.create_index("ix_conv_msg_user_id", "conversation_message", ["user_id"])

    # Backfill: copiar user_id da session para mensagens existentes
    op.execute("""
        UPDATE conversation_message cm
        SET user_id = cs.user_id
        FROM conversation_session cs
        WHERE cm.session_id = cs.id
          AND cs.user_id IS NOT NULL
          AND cm.user_id IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_conv_msg_user_id", table_name="conversation_message")
    op.drop_column("conversation_message", "user_id")
