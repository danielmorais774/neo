"""add_subscription_current_code

Código do despacho que a inscrição está vigiando (docs/playlists-skill-plan.md §7).

O aparelho devolve este código em cada evento (`faixa_iniciou`,
`faixa_terminando`, `faixa_terminou`) e é ele que faz o status acertar a LINHA
certa da fila — em vez de "marcar quem estiver como tocando", que erra assim que
a fila é mexida no meio do caminho.

Revision ID: x7y8z9a0b1c2
Revises: w6x7y8z9a0b1
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "x7y8z9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "w6x7y8z9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("event_subscription", "current_code"):
        op.add_column(
            "event_subscription",
            sa.Column("current_code", sa.String(length=16), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _has_column("event_subscription", "current_code"):
        op.drop_column("event_subscription", "current_code")
