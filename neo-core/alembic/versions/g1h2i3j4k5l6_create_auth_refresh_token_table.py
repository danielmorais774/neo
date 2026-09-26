"""create_auth_refresh_token_table

Login de usuário (docs/api-auth-plan.md §5): refresh token opaco, revogável.
Aditiva — nada existente é tocado. Rollback = dropar a tabela.

**Sobre o pai desta revisão.** O grafo do alembic teve três heads por um tempo,
resolvidos pelo merge ``i2j3k4l5m6n7``; hoje o head é único
(``f1a2b3c4d5e6``, a tabela ``release_request``, aplicada em produção em
2026-09-22). Esta é filha dele, então ``alembic upgrade head`` roda.

Revision ID: g1h2i3j4k5l6
Revises: f1a2b3c4d5e6
Create Date: 2026-09-25 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if inspect(op.get_bind()).has_table("auth_refresh_token"):
        return
    op.create_table(
        "auth_refresh_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("origem", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("rotated_to", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # O acesso quente é a apresentação do token: busca pelo hash, única.
    op.create_index(
        "ix_auth_refresh_token_token_hash", "auth_refresh_token", ["token_hash"], unique=True
    )
    op.create_index("ix_auth_refresh_token_user_id", "auth_refresh_token", ["user_id"])


def downgrade() -> None:
    if not inspect(op.get_bind()).has_table("auth_refresh_token"):
        return
    op.drop_index("ix_auth_refresh_token_user_id", table_name="auth_refresh_token")
    op.drop_index("ix_auth_refresh_token_token_hash", table_name="auth_refresh_token")
    op.drop_table("auth_refresh_token")
