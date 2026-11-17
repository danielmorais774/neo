"""create_stored_file_table

Cria ``stored_file`` — catálogo de arquivos do Neo (docs/file-store-plan.md §3.2).
Uma linha por ingestão; conteúdo em blob deduplicado por sha256 no volume
``neo-files``. Ver db/models/stored_file.py para as decisões de modelagem.

Revision ID: n7o8p9q0r1s2
Revises: m6n7o8p9q0r1
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "n7o8p9q0r1s2"
down_revision: Union[str, Sequence[str], None] = "m6n7o8p9q0r1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table)


def upgrade() -> None:
    if _table_exists("stored_file"):
        return
    op.create_table(
        "stored_file",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=120), nullable=False, server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("retention", sa.String(length=10), nullable=False, server_default="temp"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="available"),
        sa.Column("meta_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_stored_file_user_id", "stored_file", ["user_id"])
    op.create_index("ix_stored_file_session_id", "stored_file", ["session_id"])
    op.create_index("ix_stored_file_origin", "stored_file", ["origin"])
    op.create_index("ix_stored_file_sha256", "stored_file", ["sha256"])
    op.create_index("ix_stored_file_retention", "stored_file", ["retention"])
    op.create_index("ix_stored_file_status", "stored_file", ["status"])
    # Varredura do GC: temp vencido e ainda available.
    op.create_index("ix_stored_file_expires_at", "stored_file", ["expires_at"])


def downgrade() -> None:
    if _table_exists("stored_file"):
        op.drop_table("stored_file")
