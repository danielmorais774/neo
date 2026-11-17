"""create_birra_act_table

Cria ``birra_act`` — ledger de desfazer do Modo Birra
(docs/modo-birra-plan.md §6.2). Uma linha por ato; ato que alterou o mundo grava
``undo`` + ``revert_at`` e é revertido pelo sweeper (ou antes, na desculpa).
Ver db/models/birra_act.py para as decisões de modelagem.

Revision ID: q0r1s2t3u4v5
Revises: p9q0r1s2t3u4
Create Date: 2026-08-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "q0r1s2t3u4v5"
down_revision: Union[str, Sequence[str], None] = "p9q0r1s2t3u4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if _table_exists("birra_act"):
        return
    op.create_table(
        "birra_act",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("episode_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("mecanica", sa.String(length=12), nullable=False),
        sa.Column("fala", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=True),
        sa.Column("requested", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("performed", postgresql.JSONB(), nullable=True),
        sa.Column("undo", postgresql.JSONB(), nullable=True),
        sa.Column("undo_expected_state", sa.String(length=64), nullable=True),
        sa.Column("revert_at", sa.DateTime(), nullable=True),
        sa.Column("reverted_at", sa.DateTime(), nullable=True),
        sa.Column("revert_reason", sa.String(length=16), nullable=True),
        sa.Column("revert_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_birra_act_user_id", "birra_act", ["user_id"])
    op.create_index("ix_birra_act_episode_id", "birra_act", ["episode_id"])
    # Varredura do sweeper: ato com prazo vencido e ainda não revertido.
    op.create_index(
        "ix_birra_act_pending",
        "birra_act",
        ["revert_at"],
        postgresql_where=sa.text("reverted_at IS NULL"),
    )


def downgrade() -> None:
    if _table_exists("birra_act"):
        op.drop_table("birra_act")
