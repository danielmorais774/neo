"""Add mission_id to conversation_message for mission-level tracing.

Revision ID: c1d2e3f4a5b6
Revises: bb34cc56dd78
Create Date: 2026-05-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "bb34cc56dd78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversation_message",
        sa.Column("mission_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_conversation_message_mission_id",
        "conversation_message",
        ["mission_id"],
        unique=False,
    )
    op.create_index(
        "ix_conv_msg_mission_created",
        "conversation_message",
        ["mission_id", "created_at"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_conversation_message_mission_id_mission",
        "conversation_message",
        "mission",
        ["mission_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_conversation_message_mission_id_mission",
        "conversation_message",
        type_="foreignkey",
    )
    op.drop_index("ix_conv_msg_mission_created", table_name="conversation_message")
    op.drop_index("ix_conversation_message_mission_id", table_name="conversation_message")
    op.drop_column("conversation_message", "mission_id")

