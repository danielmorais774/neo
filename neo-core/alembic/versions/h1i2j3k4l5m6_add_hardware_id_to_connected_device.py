"""add_hardware_id_to_connected_device

Add the `hardware_id` column to connected_device so desktop clients can persist
machine identifiers alongside their logical device IDs.

Revision ID: h1i2j3k4l5m6
Revises: g7h8i9j0k1l2
Create Date: 2026-06-03 23:50:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "h1i2j3k4l5m6"
down_revision: Union[str, Sequence[str], None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _column_exists("connected_device", "hardware_id"):
        op.add_column(
            "connected_device",
            sa.Column(
                "hardware_id",
                sa.String(length=256),
                nullable=False,
                server_default="",
            ),
        )
        op.alter_column("connected_device", "hardware_id", server_default=None)


def downgrade() -> None:
    if _column_exists("connected_device", "hardware_id"):
        op.drop_column("connected_device", "hardware_id")
