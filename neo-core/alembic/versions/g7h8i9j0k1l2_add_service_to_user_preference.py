"""add_service_to_user_preference

Adds the `service` column (and related unique constraint + index) to the
user_preference table for deployments where the table was created without it.

Revision ID: g7h8i9j0k1l2
Revises: c3d4e5f6g7h8
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6g7h8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return any(c["name"] == column for c in insp.get_columns(table))


def _constraint_exists(table: str, name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return any(
        uc["name"] == name
        for uc in insp.get_unique_constraints(table)
    )


def _index_exists(table: str, name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def upgrade() -> None:
    if not _column_exists("user_preference", "service"):
        op.add_column(
            "user_preference",
            sa.Column(
                "service",
                sa.String(64),
                nullable=False,
                server_default="neo-desktop",
            ),
        )
        # Remove server_default after backfill so future rows are explicit
        op.alter_column("user_preference", "service", server_default=None)

    if not _constraint_exists("user_preference", "uq_user_pref"):
        op.create_unique_constraint(
            "uq_user_pref",
            "user_preference",
            ["user_id", "service", "category", "key"],
        )

    if not _index_exists("user_preference", "ix_user_pref_lookup"):
        op.create_index(
            "ix_user_pref_lookup",
            "user_preference",
            ["user_id", "service"],
        )


def downgrade() -> None:
    if _index_exists("user_preference", "ix_user_pref_lookup"):
        op.drop_index("ix_user_pref_lookup", table_name="user_preference")
    if _constraint_exists("user_preference", "uq_user_pref"):
        op.drop_constraint("uq_user_pref", "user_preference")
    if _column_exists("user_preference", "service"):
        op.drop_column("user_preference", "service")
