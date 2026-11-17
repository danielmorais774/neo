"""add_openapi_spec_to_connection

Adiciona ``service_connection.openapi_spec`` (F3 do Service Gateway): a spec crua
(JSON/YAML) da conexão, indexada em memória para o ``service_docs`` buscar
operações. Ver docs/service-gateway-dsl-plan.md §9 e core/gateway/openapi.py.

Revision ID: p9q0r1s2t3u4
Revises: o8p9q0r1s2t3
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "p9q0r1s2t3u4"
down_revision: Union[str, Sequence[str], None] = "o8p9q0r1s2t3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("service_connection", "openapi_spec"):
        op.add_column("service_connection", sa.Column("openapi_spec", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("service_connection", "openapi_spec"):
        op.drop_column("service_connection", "openapi_spec")
