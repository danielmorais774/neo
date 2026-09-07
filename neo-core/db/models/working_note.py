"""WorkingNote — o caderno de trabalho do Neo (docs/working-memory-plan.md §4).

Uma linha por NOTA (``slug`` único por usuário). O ``body`` É markdown e cresce
por append timestampado — é o ``.md`` do plano, guardado no Postgres para que o
ÍNDICE possa entrar no prompt de todo turno sem depender do sandbox estar de pé
(§3.1: o modelo não pode precisar lembrar de abrir o caderno).

Diferença para o PKS (§3.4, não negociável): aqui é rascunho de TAREFA — número
já apurado, formato de um arquivo, decisão tomada, caminho de artefato. Vive
dias e expira. Fato permanente sobre o usuário continua indo para o PKS via
``pks_remember``, que valida contra a ontologia e audita.

``status='active'`` é o que ocupa o índice; ``done`` sai do índice e continua
legível por ``wm_read``. ``expires_at`` é TTL de TOQUE (renovado a cada escrita)
e fica NULL quando ``pinned`` — o GC varre por ela.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, Integer, String, Text, Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# active: entra no índice injetado no prompt · done: arquivada, só por wm_read
NOTE_STATUSES = ("active", "done")
LIVE_STATUS = "active"
# model: o Neo chamou wm_write · auto: backstop determinístico do turno (§7)
NOTE_ORIGINS = ("model", "auto")


class WorkingNote(SQLModel, table=True):
    __tablename__ = "working_note"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Nullable como em stored_file, mas na prática sempre preenchido: as tools
    # wm_* são user-scoped e o índice só é montado com usuário resolvido.
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    # Sessão de ORIGEM, não dono: a nota sobrevive à rotação de sessão
    # (should_rotate_session pode rotacionar no meio da tarefa). Serve só para
    # ordenar o índice — nota desta sessão aparece primeiro.
    session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("session_id", SAUuid, nullable=True, index=True)
    )
    # O alvo de [[slug]] no índice e o argumento de wm_read.
    slug: str = Field(sa_column=Column("slug", String(64), nullable=False, index=True))
    title: str = Field(sa_column=Column("title", String(120), nullable=False))
    # A ÚNICA parte que entra no prompt todo turno — o corpo só sai por wm_read.
    summary: str = Field(default="", sa_column=Column("summary", String(240), nullable=False))
    body: str = Field(default="", sa_column=Column("body", Text, nullable=False))
    status: str = Field(
        default="active", sa_column=Column("status", String(10), nullable=False, index=True)
    )
    pinned: bool = Field(
        default=False, sa_column=Column("pinned", Boolean, nullable=False, server_default="false")
    )
    # ["outra-nota", "file:<uuid>", "path:/workspace/…"] — extraídos dos [[…]]
    # do conteúdo e do argumento links da tool.
    links: list = Field(
        default_factory=list,
        sa_column=Column("links", JSONB, nullable=False, server_default="[]"),
    )
    entry_count: int = Field(
        default=0, sa_column=Column("entry_count", Integer, nullable=False, server_default="0")
    )
    origin: str = Field(
        default="model", sa_column=Column("origin", String(12), nullable=False)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("updated_at", nullable=False, index=True)
    )
    # NULL quando pinned; o GC varre por esta coluna.
    expires_at: Optional[datetime] = Field(
        default=None, sa_column=Column("expires_at", nullable=True, index=True)
    )
