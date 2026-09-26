"""create_release_request_table

Solicitação de Liberação (docs/release-request-plan.md §6.2, F3): uma linha por
pedido de "posso entregar este dado a este destino?".

Aditiva: nada existente é tocado. Rollback = dropar a tabela.

**Sobre o pai desta revisão:** o repositório tem TRÊS heads
(``b1c2d3e4f5a6``, ``e2f3a4b5c6d7``, ``h1i2j3k4l5m6``) e a produção está em
``b1c2d3e4f5a6`` — ou seja, ``alembic upgrade head`` não roda. Medido em
2026-09-22: as outras duas são no-op na prática (o banco já tem
``connected_device.hardware_id`` e as tabelas legadas do grafo já não existem),
então elas foram aplicadas fora do alembic. Esta revisão é filha do que está
DE FATO aplicado, e se aplica com

    docker compose exec neo-core alembic upgrade f1a2b3c4d5e6

Um merge revision juntando as três seria o certo para o grafo, mas faria o
alembic tentar rodar as duas pendentes — o drop de tabela que não existe e o
add de coluna que já existe quebrariam no meio. Consertar o grafo é tarefa
própria, deliberada, não efeito colateral desta.

Revision ID: f1a2b3c4d5e6
Revises: b1c2d3e4f5a6
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if inspect(op.get_bind()).has_table("release_request"):
        return
    op.create_table(
        "release_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origem", sa.String(length=16), nullable=False, server_default="admin"),
        sa.Column("origem_ref", sa.String(length=320), nullable=True),
        sa.Column("origem_evidencia", sa.Text(), nullable=False, server_default=""),
        sa.Column("campos", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("destino_tipo", sa.String(length=16), nullable=False),
        sa.Column("destino", sa.String(length=320), nullable=False),
        sa.Column("finalidade", sa.String(length=280), nullable=False, server_default=""),
        sa.Column("tool", sa.String(length=64), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("risco", sa.String(length=24), nullable=False, server_default="desconhecido"),
        sa.Column("risco_motivos", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pendente"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_device", sa.String(length=128), nullable=True),
        sa.Column("decision_source", sa.String(length=24), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_release_request_user_id", "release_request", ["user_id"])
    op.create_index("ix_release_request_session_id", "release_request", ["session_id"])
    # O acesso quente é "o que está pendente deste usuário" (card, painel, e a
    # checagem de duplicata antes de criar outro pedido igual).
    op.create_index(
        "ix_release_request_user_status", "release_request", ["user_id", "status"]
    )


def downgrade() -> None:
    if not inspect(op.get_bind()).has_table("release_request"):
        return
    op.drop_index("ix_release_request_user_status", table_name="release_request")
    op.drop_index("ix_release_request_session_id", table_name="release_request")
    op.drop_index("ix_release_request_user_id", table_name="release_request")
    op.drop_table("release_request")
