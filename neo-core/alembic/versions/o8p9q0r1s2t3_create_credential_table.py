"""create_credential_table

Cria ``credential`` — cofre Fernet compartilhado pelo Service Gateway (F2,
docs/service-gateway-dsl-plan.md §3.2) e pelo browser-agent (F3 do
docs/browser-agent-plan.md §3.5). Guarda só o ciphertext; a chave
(``NEO_CRED_KEY``) mora na env da VPS.

Revision ID: o8p9q0r1s2t3
Revises: n7o8p9q0r1s2
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "o8p9q0r1s2t3"
down_revision: Union[str, Sequence[str], None] = "n7o8p9q0r1s2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if _table_exists("credential"):
        return
    op.create_table(
        "credential",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="token"),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("value_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("hint", sa.String(length=8), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_unique_constraint("uq_credential_label", "credential", ["label"])


def downgrade() -> None:
    if _table_exists("credential"):
        op.drop_table("credential")
