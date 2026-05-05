"""Create mission, whatsapp_contact, user_notification tables.

Revision ID: a1b2c3d4e5f6
Revises: f6g7h8i9j0k1
Create Date: 2026-05-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f6g7h8i9j0k1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # mission table
    op.create_table(
        "mission",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("parent_session_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("contact_jid", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False, server_default="whatsapp"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("last_inbound_message_id", sa.String(128), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("timeout_at", sa.DateTime(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("extra_metadata", postgresql.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_mission_id", "mission", ["id"], unique=False)
    op.create_index("ix_mission_user_id", "mission", ["user_id"], unique=False)
    op.create_index("ix_mission_session_id", "mission", ["session_id"], unique=True)
    op.create_index("ix_mission_parent_session_id", "mission", ["parent_session_id"], unique=False)
    op.create_index("ix_mission_contact_jid", "mission", ["contact_jid"], unique=False)
    op.create_index("ix_mission_status", "mission", ["status"], unique=False)

    # whatsapp_contact table
    op.create_table(
        "whatsapp_contact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("jid", sa.String(128), nullable=False),
        sa.Column("label", sa.String(256), nullable=False),
        sa.Column("phone_e164", sa.String(32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_whatsapp_contact_id", "whatsapp_contact", ["id"], unique=False)
    op.create_index("ix_whatsapp_contact_user_id", "whatsapp_contact", ["user_id"], unique=False)
    op.create_index(
        "ix_whatsapp_contact_user_jid", "whatsapp_contact",
        ["user_id", "jid"], unique=True
    )

    # user_notification table
    op.create_table(
        "user_notification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("mission_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSON(), nullable=False, server_default="{}"),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("acked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_notification_id", "user_notification", ["id"], unique=False)
    op.create_index("ix_user_notification_user_id", "user_notification", ["user_id"], unique=False)
    op.create_index("ix_user_notification_mission_id", "user_notification", ["mission_id"], unique=False)
    op.create_index("ix_user_notification_kind", "user_notification", ["kind"], unique=False)


def downgrade() -> None:
    op.drop_table("user_notification")
    op.drop_table("whatsapp_contact")
    op.drop_table("mission")