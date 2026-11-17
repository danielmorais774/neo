"""create_datasets_tables

Cadastros generativos (docs/datasets-crud-plan.md §F0):

- schema Postgres ``neo_data`` — onde as tabelas de cadastro nascem em runtime.
  Fica FORA do `public` de propósito: um bug no DDL dinâmico não alcança as
  tabelas do Neo, e o ``include_object`` do env.py filtra o schema inteiro do
  autogenerate (sem isso o alembic proporia DROP para toda tabela de cadastro).
- ``neo_data_dataset`` — definição (schema JSONB) + nome da tabela física;
- ``neo_data_audit`` — trilha de delete e ALTER destrutivo;
- ``skill_install`` — estado de instalação das skills (plan/apply/verify).

Revision ID: v5w6x7y8z9a0
Revises: u4v5w6x7y8z9
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "v5w6x7y8z9a0"
down_revision: Union[str, Sequence[str], None] = "u4v5w6x7y8z9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DATA_SCHEMA = "neo_data"


def _table_exists(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    # gen_random_uuid() para o DEFAULT das tabelas de cadastro. pgcrypto é
    # redundante no PG13+ (a função é nativa), mas o banco de prod é pg15 com
    # pgvector e a extensão não custa nada se já existir.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {DATA_SCHEMA}")

    if not _table_exists("neo_data_dataset"):
        op.create_table(
            "neo_data_dataset",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("slug", sa.String(length=40), nullable=False),
            sa.Column("code", sa.String(length=8), nullable=False),
            sa.Column("table_name", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=80), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("schema_json", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("skill_name", sa.String(length=40), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_neo_data_dataset_user_id", "neo_data_dataset", ["user_id"])
        op.create_index("ix_neo_data_dataset_slug", "neo_data_dataset", ["slug"])
        op.create_index("ix_neo_data_dataset_skill", "neo_data_dataset", ["skill_name"])
        # O slug é como humano, LLM e URL do painel citam o cadastro — precisa
        # ser único por dono, senão data_list("produtos") vira ambíguo.
        op.create_index(
            "ux_neo_data_dataset_user_slug",
            "neo_data_dataset",
            ["user_id", "slug"],
            unique=True,
        )
        op.create_index(
            "ux_neo_data_dataset_table", "neo_data_dataset", ["table_name"], unique=True
        )

    if not _table_exists("neo_data_audit"):
        op.create_table(
            "neo_data_audit",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("dataset_slug", sa.String(length=40), nullable=False),
            sa.Column("action", sa.String(length=20), nullable=False),
            sa.Column("actor", sa.String(length=20), nullable=False, server_default="painel"),
            sa.Column("row_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_neo_data_audit_user_id", "neo_data_audit", ["user_id"])
        op.create_index("ix_neo_data_audit_slug", "neo_data_audit", ["dataset_slug"])
        op.create_index("ix_neo_data_audit_created", "neo_data_audit", ["created_at"])

    if not _table_exists("skill_install"):
        op.create_table(
            "skill_install",
            sa.Column("name", sa.String(length=40), primary_key=True),
            sa.Column("state", sa.String(length=16), nullable=False, server_default="planejada"),
            sa.Column("install_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("installed_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("datasets", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("tools", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("screens", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("installed_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_skill_install_state", "skill_install", ["state"])


def downgrade() -> None:
    for table in ("skill_install", "neo_data_audit", "neo_data_dataset"):
        if _table_exists(table):
            op.drop_table(table)
    # O schema neo_data NÃO é dropado: ele guarda dados do usuário criados em
    # runtime. Derrubar a migração não pode apagar cadastro (§3.5 do plano).
