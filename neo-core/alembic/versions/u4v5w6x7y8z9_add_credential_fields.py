"""add_credential_fields

Adiciona ``credential.fields_json``: os campos NÃO secretos de uma credencial
(usuário de um login, endereço/host de uma caixa de email, domínio onde vale).
Até aqui o cofre guardava um segredo solto e o ``kind`` era decorativo — não
havia onde pôr o usuário de um "usuário e senha". Ver core/credentials/kinds.py.

Em claro de propósito: é isto que o painel exibe e que o cérebro lê para
escolher a credencial certa. O segredo continua só em ``value_encrypted``.

Revision ID: u4v5w6x7y8z9
Revises: t3u4v5w6x7y8
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "u4v5w6x7y8z9"
down_revision: Union[str, Sequence[str], None] = "t3u4v5w6x7y8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("credential", "fields_json"):
        op.add_column(
            "credential",
            sa.Column(
                "fields_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )


def downgrade() -> None:
    if _has_column("credential", "fields_json"):
        op.drop_column("credential", "fields_json")
