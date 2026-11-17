"""Drop legacy knowledge graph tables.

Revision ID: e2f3a4b5c6d7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_kr_target")
    op.execute("DROP INDEX IF EXISTS ix_kr_source")
    op.execute("DROP INDEX IF EXISTS ix_kr_user_type")
    op.execute("DROP TABLE IF EXISTS knowledge_relation")
    op.execute("DROP INDEX IF EXISTS ix_ke_user_name")
    op.execute("DROP INDEX IF EXISTS ix_ke_user_type")
    op.execute("DROP TABLE IF EXISTS knowledge_entity")


def downgrade() -> None:
    op.create_table(
        "knowledge_entity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ke_user_type", "knowledge_entity", ["user_id", "type"])
    op.create_index("ix_ke_user_name", "knowledge_entity", ["user_id", "name"])

    op.create_table(
        "knowledge_relation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_entity.id"]),
        sa.ForeignKeyConstraint(["target_id"], ["knowledge_entity.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kr_user_type", "knowledge_relation", ["user_id", "type"])
    op.create_index("ix_kr_source", "knowledge_relation", ["source_id"])
    op.create_index("ix_kr_target", "knowledge_relation", ["target_id"])
