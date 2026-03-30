"""create_user_preference_table

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-03-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6g7h8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6g7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_preference",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("user.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("category", sa.String(64), nullable=False, index=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source_message", sa.Text(), nullable=True),
        sa.Column("pref_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # Unique constraint: one preference per user+service+category+key
    op.create_unique_constraint(
        "uq_user_pref",
        "user_preference",
        ["user_id", "service", "category", "key"],
    )

    # Lookup index for fetching all prefs for a user+service
    op.create_index(
        "ix_user_pref_lookup",
        "user_preference",
        ["user_id", "service"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_pref_lookup", table_name="user_preference")
    op.drop_constraint("uq_user_pref", "user_preference")
    op.drop_table("user_preference")
