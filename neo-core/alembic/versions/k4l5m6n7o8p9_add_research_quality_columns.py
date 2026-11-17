"""add_research_quality_columns

Deep Research v2 (docs/deep-research-v2-plan.md §4.1): duas colunas novas na
``research_task``.

- ``requirements_json``: requisitos explícitos extraídos do pedido (INTAKE) —
  é contra eles que o VALIDATE cobra o relatório ("pediu 30 referências").
- ``quality_json``: resultado do VALIDATE + rubrica do CRITIC + matriz de
  cobertura. Coluna própria (não cabe em stats_json, que o runner sobrescreve
  com as stats do agente nos estados terminais).

Pesquisas antigas ficam com {} nos dois campos e continuam legíveis no painel.

Revision ID: k4l5m6n7o8p9
Revises: j3k4l5m6n7o8
Create Date: 2026-07-11 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "k4l5m6n7o8p9"
down_revision: Union[str, Sequence[str], None] = "j3k4l5m6n7o8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    existing = _columns("research_task")
    if not existing:
        return  # tabela ainda não existe (migração anterior cuida disso)
    if "requirements_json" not in existing:
        op.add_column(
            "research_task",
            sa.Column("requirements_json", sa.JSON(), nullable=False, server_default="{}"),
        )
    if "quality_json" not in existing:
        op.add_column(
            "research_task",
            sa.Column("quality_json", sa.JSON(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    existing = _columns("research_task")
    if "quality_json" in existing:
        op.drop_column("research_task", "quality_json")
    if "requirements_json" in existing:
        op.drop_column("research_task", "requirements_json")
