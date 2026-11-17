"""Create Personal Knowledge Store tables.

Revision ID: e1f2a3b4c5d6
Revises: c1d2e3f4a5b6
Create Date: 2026-05-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "pks_workspace",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("external_refs", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pks_workspace_owner", "pks_workspace", ["owner_user_id"], postgresql_where=sa.text("deleted_at IS NULL"))

    op.create_table(
        "pks_space",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["pks_workspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pks_space_ws", "pks_space", ["workspace_id"], postgresql_where=sa.text("deleted_at IS NULL"))
    op.execute(
        "CREATE UNIQUE INDEX uniq_pks_space_name "
        "ON pks_space (workspace_id, lower(name)) WHERE deleted_at IS NULL"
    )

    op.create_table(
        "pks_collection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("schema", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["pks_workspace.id"]),
        sa.ForeignKeyConstraint(["space_id"], ["pks_space.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pks_collection_space", "pks_collection", ["space_id"], postgresql_where=sa.text("deleted_at IS NULL"))
    op.execute(
        "CREATE UNIQUE INDEX uniq_pks_collection_name "
        "ON pks_collection (space_id, lower(name)) WHERE deleted_at IS NULL"
    )

    op.create_table(
        "pks_entity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=True),
        sa.Column("collection_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("external_refs", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(), server_default=sa.text("1"), nullable=False),
        sa.Column("sensitivity", sa.Text(), server_default=sa.text("'private'"), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["pks_workspace.id"]),
        sa.ForeignKeyConstraint(["space_id"], ["pks_space.id"]),
        sa.ForeignKeyConstraint(["collection_id"], ["pks_collection.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("ALTER TABLE pks_entity ALTER COLUMN embedding TYPE vector(384) USING embedding::vector")
    op.execute(
        "CREATE UNIQUE INDEX uniq_pks_entity_name "
        "ON pks_entity (workspace_id, type, lower(name)) WHERE deleted_at IS NULL"
    )
    op.create_index("ix_pks_entity_ws_type", "pks_entity", ["workspace_id", "type"], postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("ix_pks_entity_search_trgm", "pks_entity", ["search_text"], postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"}, postgresql_where=sa.text("deleted_at IS NULL"))

    op.create_table(
        "pks_entity_field",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_number", sa.Numeric(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(), server_default=sa.text("1"), nullable=False),
        sa.Column("sensitivity", sa.Text(), server_default=sa.text("'private'"), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["entity_id"], ["pks_entity.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uniq_pks_field_key "
        "ON pks_entity_field (entity_id, lower(key)) WHERE deleted_at IS NULL"
    )

    op.create_table(
        "pks_entity_relation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("from_entity_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.Text(), nullable=False),
        sa.Column("to_entity_id", sa.Uuid(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Numeric(), server_default=sa.text("1"), nullable=False),
        sa.Column("sensitivity", sa.Text(), server_default=sa.text("'private'"), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["pks_workspace.id"]),
        sa.ForeignKeyConstraint(["from_entity_id"], ["pks_entity.id"]),
        sa.ForeignKeyConstraint(["to_entity_id"], ["pks_entity.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uniq_pks_relation", "pks_entity_relation", ["from_entity_id", "relation_type", "to_entity_id"], unique=True, postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("ix_pks_relation_from", "pks_entity_relation", ["from_entity_id", "relation_type"], postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("ix_pks_relation_to", "pks_entity_relation", ["to_entity_id", "relation_type"], postgresql_where=sa.text("deleted_at IS NULL"))

    op.create_table(
        "pks_workspace_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("from_workspace_id", sa.Uuid(), nullable=False),
        sa.Column("to_workspace_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["from_workspace_id"], ["pks_workspace.id"]),
        sa.ForeignKeyConstraint(["to_workspace_id"], ["pks_workspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "pks_audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["pks_workspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pks_audit_ws", "pks_audit_log", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_pks_audit_ws", table_name="pks_audit_log")
    op.drop_table("pks_audit_log")
    op.drop_table("pks_workspace_link")
    op.drop_index("ix_pks_relation_to", table_name="pks_entity_relation")
    op.drop_index("ix_pks_relation_from", table_name="pks_entity_relation")
    op.drop_index("uniq_pks_relation", table_name="pks_entity_relation")
    op.drop_table("pks_entity_relation")
    op.drop_index("uniq_pks_field_key", table_name="pks_entity_field")
    op.drop_table("pks_entity_field")
    op.drop_index("ix_pks_entity_search_trgm", table_name="pks_entity")
    op.drop_index("ix_pks_entity_ws_type", table_name="pks_entity")
    op.drop_index("uniq_pks_entity_name", table_name="pks_entity")
    op.drop_table("pks_entity")
    op.drop_index("uniq_pks_collection_name", table_name="pks_collection")
    op.drop_index("ix_pks_collection_space", table_name="pks_collection")
    op.drop_table("pks_collection")
    op.drop_index("uniq_pks_space_name", table_name="pks_space")
    op.drop_index("ix_pks_space_ws", table_name="pks_space")
    op.drop_table("pks_space")
    op.drop_index("ix_pks_workspace_owner", table_name="pks_workspace")
    op.drop_table("pks_workspace")
