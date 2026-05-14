"""Add next action columns to mission table.

Revision ID: bb34cc56dd78
Revises: aa12bb34cc56
Create Date: 2026-05-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "bb34cc56dd78"
down_revision: Union[str, Sequence[str], None] = "aa12bb34cc56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mission",
        sa.Column("next_action_type", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "mission",
        sa.Column(
            "next_action_params",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("mission", "next_action_params")
    op.drop_column("mission", "next_action_type")

