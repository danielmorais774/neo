"""Relax mission.session_id uniqueness and ensure lookup index.

Revision ID: aa12bb34cc56
Revises: a7b8c9d0e1f2
Create Date: 2026-05-13
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa12bb34cc56"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop possible unique index from older schemas.
    op.execute("DROP INDEX IF EXISTS ix_mission_session_id")
    op.execute("DROP INDEX IF EXISTS uq_mission_session_id")

    # Ensure non-unique index for mission → session lookups.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mission_session_id ON mission (session_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_mission_session_id")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_mission_session_id ON mission (session_id)"
    )
