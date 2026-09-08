"""create_event_subscription_tables

Inscrições de evento (docs/playlists-skill-plan.md §7):

- ``event_subscription`` — o contrato concreto (aparelho + alvo + faixa esperada
  + handler). É a peça que deixa o Neo continuar uma fila sozinho **sem** que um
  pedido avulso ("toca um AC/DC") acione coisa nenhuma;
- ``event_delivery`` — trilha de cada evento recebido e do que ele causou.

Revision ID: w6x7y8z9a0b1
Revises: v5w6x7y8z9a0
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "w6x7y8z9a0b1"
down_revision: Union[str, Sequence[str], None] = "v5w6x7y8z9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _table_exists("event_subscription"):
        op.create_table(
            "event_subscription",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("device_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("channel", sa.String(length=32), nullable=False),
            sa.Column("events", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("target", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("expected_ref", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("handler_ref", sa.String(length=96), nullable=False),
            sa.Column("handler_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("state", sa.String(length=16), nullable=False, server_default="armada"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("fires", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_fires", sa.Integer(), nullable=False, server_default="200"),
            sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_event_at", sa.DateTime(), nullable=True),
            sa.Column("last_event_key", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_event_subscription_user", "event_subscription", ["user_id"])
        op.create_index("ix_event_subscription_state", "event_subscription", ["state"])
        # O casamento do evento é sempre (aparelho, canal, estado): é este índice
        # que faz o POST do fim de faixa custar uma linha, não uma varredura.
        op.create_index(
            "ix_event_subscription_lookup",
            "event_subscription",
            ["device_id", "channel", "state"],
        )

    if not _table_exists("event_delivery"):
        op.create_table(
            "event_delivery",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("channel", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("event", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("event_key", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("outcome", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("action", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_event_delivery_sub", "event_delivery", ["subscription_id"])
        op.create_index("ix_event_delivery_user", "event_delivery", ["user_id"])
        op.create_index("ix_event_delivery_key", "event_delivery", ["event_key"])
        op.create_index("ix_event_delivery_created", "event_delivery", ["created_at"])


def downgrade() -> None:
    for table in ("event_delivery", "event_subscription"):
        if _table_exists(table):
            op.drop_table(table)
