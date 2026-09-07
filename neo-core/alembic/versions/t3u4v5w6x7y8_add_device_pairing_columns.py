"""add_device_pairing_columns

Adiciona a ``connected_device`` as colunas de pareamento/controle
(docs/device-control-plan.md §1):

- ``display_name``       nome humano do device ("PC da sala") — é por ele que o
                         cérebro endereça, nunca pelo device_id opaco.
- ``pairing_token_hash`` sha256 do token do device. É o que torna o ``user_id``
                         um dono PROVADO em vez de declarado pelo cliente.
- ``paired_at``          quando o pareamento aconteceu.
- ``capabilities``       o que o device sabe fazer, informado por ele.

Todas nullable: os devices já registrados continuam funcionando sem token
(fase permissiva, §2.4) — o que exige token é só o canal de controle, que é novo.

Revision ID: t3u4v5w6x7y8
Revises: s2t3u4v5w6x7
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "t3u4v5w6x7y8"
down_revision: Union[str, Sequence[str], None] = "s2t3u4v5w6x7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table):
        return False
    return any(col["name"] == column for col in insp.get_columns(table))


def upgrade() -> None:
    if not _column_exists("connected_device", "display_name"):
        op.add_column(
            "connected_device",
            sa.Column("display_name", sa.String(length=64), nullable=True),
        )
    if not _column_exists("connected_device", "pairing_token_hash"):
        op.add_column(
            "connected_device",
            sa.Column("pairing_token_hash", sa.String(length=128), nullable=True),
        )
        # O lookup do canal de controle é por hash do token — sem índice ele vira
        # seq scan a cada reconexão de device.
        op.create_index(
            "ix_connected_device_pairing_token_hash",
            "connected_device",
            ["pairing_token_hash"],
            unique=False,
        )
    if not _column_exists("connected_device", "paired_at"):
        op.add_column(
            "connected_device",
            sa.Column("paired_at", sa.DateTime(), nullable=True),
        )
    if not _column_exists("connected_device", "capabilities"):
        op.add_column(
            "connected_device",
            sa.Column("capabilities", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("connected_device", "capabilities"):
        op.drop_column("connected_device", "capabilities")
    if _column_exists("connected_device", "paired_at"):
        op.drop_column("connected_device", "paired_at")
    if _column_exists("connected_device", "pairing_token_hash"):
        op.drop_index(
            "ix_connected_device_pairing_token_hash",
            table_name="connected_device",
        )
        op.drop_column("connected_device", "pairing_token_hash")
    if _column_exists("connected_device", "display_name"):
        op.drop_column("connected_device", "display_name")
