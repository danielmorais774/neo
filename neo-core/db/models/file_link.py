"""FileLink — onde está cada cópia de um documento (docs/file-links-plan.md).

Uma linha por MOVIMENTO de arquivo entre os três lugares que não se enxergam: o
disco do usuário (host), o catálogo (File Store) e o sandbox (/workspace). As
linhas com o mesmo ``doc_key`` são o mesmo documento lógico — o PDF que veio da
pasta dele, a conclusão que o Neo gerou e entregou em duas pastas, a cópia que
ele editou direto no Mac.

Nasceu da sessão f33632b4: o Neo pôs o PDF do Charlie numa pasta do usuário com o
nome da conclusão ``.md``, disse "Pronto" e, cobrado, não sabia mais qual cópia
estava onde — 5 turnos sem conseguir consertar. O registro é append-only e em
Postgres porque a memória em RAM da sessão (``_session_seen_files``) some a cada
reload do core, que é a cada arquivo salvo.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, String, Text, Uuid as SAUuid
from sqlmodel import Field, SQLModel

#: pulled: host→catálogo · staged: catálogo→sandbox · saved: sandbox→catálogo
#: edited_sandbox: sandbox mudou sem publicar · delivered: catálogo→host
#: written_host / edited_host / read_host / deleted_host: direto no disco dele
FILE_LINK_ACTIONS = (
    "pulled", "staged", "saved", "edited_sandbox", "delivered",
    "written_host", "edited_host", "read_host", "deleted_host",
)


class FileLink(SQLModel, table=True):
    __tablename__ = "file_link"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("session_id", SAUuid, nullable=True, index=True)
    )
    doc_key: str = Field(sa_column=Column("doc_key", String(64), nullable=False, index=True))
    action: str = Field(sa_column=Column("action", String(24), nullable=False))
    # Nome que o usuário reconhece (do catálogo ou do caminho).
    name: str = Field(default="", sa_column=Column("name", String(255), nullable=False, server_default=""))
    # host: caminho absoluto no disco dele · sandbox: /workspace/...
    host_path: Optional[str] = Field(default=None, sa_column=Column("host_path", String(1024), nullable=True))
    sandbox_path: Optional[str] = Field(default=None, sa_column=Column("sandbox_path", String(1024), nullable=True))
    file_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("file_id", SAUuid, nullable=True, index=True)
    )
    sha256: Optional[str] = Field(default=None, sa_column=Column("sha256", String(64), nullable=True))
    size_bytes: Optional[int] = Field(default=None, sa_column=Column("size_bytes", BigInteger, nullable=True))
    # ok · wrong_content (o que foi gravado não é o documento que o nome diz)
    status: str = Field(default="ok", sa_column=Column("status", String(24), nullable=False, server_default="ok"))
    note: str = Field(default="", sa_column=Column("note", Text, nullable=False, server_default=""))
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False, index=True)
    )
