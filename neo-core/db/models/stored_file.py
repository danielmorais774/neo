"""StoredFile — catálogo de arquivos do Neo (docs/file-store-plan.md §3.2).

Uma linha por INGESTÃO; o conteúdo mora num blob deduplicado por ``sha256``
(``/data/files/blobs/<sha[:2]>/<sha>``). Enviar o mesmo arquivo 2x = 2 linhas,
1 blob — o unlink do blob só acontece quando nenhuma linha ``available`` aponta
para o sha (refcount).

``retention`` distingue rascunho de acervo: ``temp`` expira em ``expires_at``
(GC marca ``expired`` e apaga o blob se ninguém mais usa); ``perm`` fica.
Deleção é soft (``status=deleted``) — a linha sobrevive para auditoria no panel.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Origem da ingestão — de onde o arquivo entrou no store.
FILE_ORIGINS = (
    "upload-desktop", "upload-panel", "whatsapp", "url", "workbench", "research", "job",
)
RETENTIONS = ("temp", "perm")
# available: usável · expired: TTL venceu (blob já foi) · deleted: soft-delete do usuário
FILE_STATUSES = ("available", "expired", "deleted")
LIVE_STATUS = "available"


class StoredFile(SQLModel, table=True):
    __tablename__ = "stored_file"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Nullable como em agent_job: nem toda ingestão tem usuário resolvido
    # (upload por script/gateway). Single-user hoje; indexado para o dia que não for.
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("session_id", SAUuid, nullable=True, index=True)
    )
    origin: str = Field(sa_column=Column("origin", String(20), nullable=False, index=True))
    # Nome original do arquivo (o que o usuário reconhece) — nunca usado como path.
    name: str = Field(sa_column=Column("name", String(255), nullable=False))
    mime: str = Field(
        default="application/octet-stream",
        sa_column=Column("mime", String(120), nullable=False),
    )
    size_bytes: int = Field(sa_column=Column("size_bytes", BigInteger, nullable=False))
    sha256: str = Field(sa_column=Column("sha256", String(64), nullable=False, index=True))
    retention: str = Field(
        default="temp", sa_column=Column("retention", String(10), nullable=False, index=True)
    )
    # NULL quando perm; o GC varre por esta coluna.
    expires_at: Optional[datetime] = Field(
        default=None, sa_column=Column("expires_at", nullable=True, index=True)
    )
    status: str = Field(
        default="available", sa_column=Column("status", String(12), nullable=False, index=True)
    )
    # {line_count, pages, extraction, preview_head, source:{...}, mime_mismatch?}
    meta_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("meta_json", JSONB, nullable=False, server_default="{}"),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False)
    )
    last_accessed_at: Optional[datetime] = Field(
        default=None, sa_column=Column("last_accessed_at", nullable=True)
    )
    access_count: int = Field(
        default=0, sa_column=Column("access_count", Integer, nullable=False, server_default="0")
    )
