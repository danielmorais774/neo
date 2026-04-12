"""create knowledge_entity and knowledge_relation tables

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2025-01-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4e5f6g7h8i9"
down_revision = "c3d4e5f6g7h8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # knowledge_entity
    op.create_table(
        "knowledge_entity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ke_user_type", "knowledge_entity", ["user_id", "type"])
    op.create_index("ix_ke_user_name", "knowledge_entity", ["user_id", "name"])

    # knowledge_relation
    op.create_table(
        "knowledge_relation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("knowledge_entity.id"), nullable=False),
        sa.Column("target_id", sa.Uuid(), sa.ForeignKey("knowledge_entity.id"), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kr_user_type", "knowledge_relation", ["user_id", "type"])
    op.create_index("ix_kr_source", "knowledge_relation", ["source_id"])
    op.create_index("ix_kr_target", "knowledge_relation", ["target_id"])


def downgrade() -> None:
    op.drop_index("ix_kr_target", table_name="knowledge_relation")
    op.drop_index("ix_kr_source", table_name="knowledge_relation")
    op.drop_index("ix_kr_user_type", table_name="knowledge_relation")
    op.drop_table("knowledge_relation")

    op.drop_index("ix_ke_user_name", table_name="knowledge_entity")
    op.drop_index("ix_ke_user_type", table_name="knowledge_entity")
    op.drop_table("knowledge_entity")
