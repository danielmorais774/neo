"""Solicitação de Liberação (docs/release-request-plan.md §6.2).

Uma linha por pedido de "posso entregar este dado a este destino?". É o único
caminho pelo qual dado rotulado atravessa para fora quando o turno não é um
pedido limpo do dono — se existir um caminho paralelo, ele é o que a injeção vai
usar.

Três campos carregam o peso da segurança:

``origem``/``origem_ref``/``origem_evidencia``
    De quem partiu a intenção. A evidência é o texto original do terceiro, e é o
    que deixa o card dizer "isto veio de um e-mail, não de você" — a linha que
    desarma o ataque.
``campos``
    Os RÓTULOS pedidos (``["pks:endereco.cep"]``), nunca os valores. O valor é
    resolvido na hora de usar, por quem tem autorização.
``payload_hash``
    Amarra a aprovação ao conteúdo exato que foi mostrado. Muda o texto, morre a
    aprovação.

``db/models/`` não importa de ``core`` (o alembic importa os models direto e
``core/__init__`` puxa a engine inteira — circular import que quebra a migração,
não a app). Por isso os estados vivem aqui como constantes locais.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column, String, Text, Uuid as SAUuid
from sqlmodel import Field, SQLModel

#: Estados. ``usada`` é terminal e separado de ``aprovada``: aprovação de uso
#: único que já foi consumida não pode valer de novo.
STATUS_PENDENTE = "pendente"
STATUS_APROVADA = "aprovada"
STATUS_RECUSADA = "recusada"
STATUS_EXPIRADA = "expirada"
STATUS_USADA = "usada"

STATUSES = (
    STATUS_PENDENTE,
    STATUS_APROVADA,
    STATUS_RECUSADA,
    STATUS_EXPIRADA,
    STATUS_USADA,
)

#: Origem da intenção — espelha os principals de ``core/authority/turn.py``.
#: Duplicado de propósito (ver docstring do módulo); um teste trava a divergência.
ORIGENS = ("admin", "externo", "agenda")

#: Tipos de destino que o executor sabe honrar.
DESTINO_TIPOS = ("email", "whatsapp", "dominio", "chat")


class ReleaseRequest(SQLModel, table=True):
    __tablename__ = "release_request"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("session_id", SAUuid, nullable=True, index=True),
    )

    # ── de onde veio o pedido ────────────────────────────────────────────
    origem: str = Field(
        sa_column=Column("origem", String(16), nullable=False, server_default="admin"),
    )
    origem_ref: Optional[str] = Field(
        default=None,
        sa_column=Column("origem_ref", String(320), nullable=True),
    )
    origem_evidencia: str = Field(
        default="",
        sa_column=Column("origem_evidencia", Text, nullable=False, server_default=""),
    )

    # ── o que sai, para onde ─────────────────────────────────────────────
    campos: list = Field(
        default_factory=list,
        sa_column=Column("campos", JSON, nullable=False),
    )
    destino_tipo: str = Field(
        sa_column=Column("destino_tipo", String(16), nullable=False),
    )
    destino: str = Field(
        sa_column=Column("destino", String(320), nullable=False),
    )
    finalidade: str = Field(
        default="",
        sa_column=Column("finalidade", String(280), nullable=False, server_default=""),
    )
    #: Tool que ficou pendurada esperando a liberação (para o card explicar o
    #: que vai acontecer quando você aprovar).
    tool: Optional[str] = Field(
        default=None,
        sa_column=Column("tool", String(64), nullable=True),
    )
    payload_hash: Optional[str] = Field(
        default=None,
        sa_column=Column("payload_hash", String(64), nullable=True),
    )

    # ── avaliação (§10) ──────────────────────────────────────────────────
    risco: str = Field(
        default="desconhecido",
        sa_column=Column("risco", String(24), nullable=False, server_default="desconhecido"),
    )
    risco_motivos: list = Field(
        default_factory=list,
        sa_column=Column("risco_motivos", JSON, nullable=False),
    )

    # ── ciclo de vida ────────────────────────────────────────────────────
    status: str = Field(
        default=STATUS_PENDENTE,
        sa_column=Column("status", String(16), nullable=False, index=True, server_default=STATUS_PENDENTE),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("expires_at", nullable=True),
    )
    decided_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("decided_at", nullable=True),
    )
    #: QUEM decidiu, resolvido pelo token do aparelho ou pelo login do painel —
    #: nunca o que o cliente declarou (§11).
    decided_by: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("decided_by", SAUuid, nullable=True),
    )
    decided_device: Optional[str] = Field(
        default=None,
        sa_column=Column("decided_device", String(128), nullable=True),
    )
    decision_source: Optional[str] = Field(
        default=None,
        sa_column=Column("decision_source", String(24), nullable=True),
    )
    used_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("used_at", nullable=True),
    )
    notification_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("notification_id", SAUuid, nullable=True),
    )
