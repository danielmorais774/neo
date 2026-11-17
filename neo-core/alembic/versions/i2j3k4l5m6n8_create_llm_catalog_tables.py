"""create_llm_catalog_tables

Catálogo de LLM editável no painel (Configurações → Providers): ``llm_provider``
e ``llm_model``. Substitui o mapa cravado no ``core/llm/router.py`` e os enums
copiados no desktop e no chat do painel.

Aditiva: nada existente é tocado. As tabelas nascem VAZIAS; a semente
(``core/llm/catalog_seed.py``) é gravada pelo próprio core na primeira subida —
a migração não importa ``core`` (circular import, ver db/models/credential.py).
Até lá, e enquanto esta migração não rodar, o core usa a semente em memória.
Rollback = dropar as duas tabelas.

Revision ID: i2j3k4l5m6n8
Revises: h1i2j3k4l5m7
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "i2j3k4l5m6n8"
down_revision: Union[str, Sequence[str], None] = "h1i2j3k4l5m7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = inspect(op.get_bind())
    if not insp.has_table("llm_provider"):
        op.create_table(
            "llm_provider",
            sa.Column("name", sa.String(length=40), primary_key=True),
            sa.Column("label", sa.String(length=80), nullable=False),
            sa.Column("adapter", sa.String(length=30), nullable=False),
            sa.Column("base_url", sa.String(length=300), nullable=True),
            sa.Column("api_key_encrypted", sa.LargeBinary(), nullable=True),
            sa.Column("api_key_hint", sa.String(length=8), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("default_model", sa.String(length=200), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if not insp.has_table("llm_model"):
        op.create_table(
            "llm_model",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "provider",
                sa.String(length=40),
                sa.ForeignKey("llm_provider.name", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("model_id", sa.String(length=200), nullable=False),
            sa.Column("label", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("description", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("vision", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("in_selector", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("model_id", name="uq_llm_model_model_id"),
        )
        op.create_index("ix_llm_model_provider", "llm_model", ["provider"])


def downgrade() -> None:
    op.drop_table("llm_model")
    op.drop_table("llm_provider")
