"""Modelos dos cadastros generativos (docs/datasets-crud-plan.md §3.3 e §4.2).

Três tabelas de CONTROLE, todas no `public` e gerenciadas pelo alembic:

- ``neo_data_dataset`` — a definição de um cadastro (schema em JSONB) e o nome
  da tabela física que o materializa;
- ``neo_data_audit`` — quem apagou/alterou o quê (delete e ALTER destrutivo);
- ``skill_install`` — o estado de instalação de uma skill (§3.3 do plano).

As tabelas de DADOS não estão aqui: elas nascem em runtime, no schema
``neo_data``, e são descritas por ``neo_data_dataset.schema_json``. É por isso
que o ``alembic/env.py`` precisa do ``include_object`` — sem ele o autogenerate
proporia DROP para toda tabela de cadastro (ela não está no SQLModel.metadata).
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, Column, Integer, String, Text, Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

#: Estados de :class:`SkillInstall`. `desatualizada` = o SKILL.md mudou depois
#: da instalação e o novo plano ainda não foi aplicado (§3.4 do plano).
INSTALL_STATES = ("planejada", "instalada", "desatualizada", "falhou", "desinstalada")


class DatasetDefinition(SQLModel, table=True):
    """Um cadastro: o schema + o endereço da tabela física que o guarda."""

    __tablename__ = "neo_data_dataset"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    # Nome curto usado por humano, LLM e URL do painel. Único por usuário.
    slug: str = Field(sa_column=Column("slug", String(40), nullable=False, index=True))
    # Sufixo que evita colisão entre duas skills que querem o mesmo slug — e
    # entre um cadastro novo e o resíduo de um homônimo excluído.
    code: str = Field(sa_column=Column("code", String(8), nullable=False))
    # `<slug>_<code>`. É O ÚNICO nome de tabela aceito pelo executor: um
    # table_name que não sai daqui é recusado antes de chegar ao banco.
    table_name: str = Field(
        sa_column=Column("table_name", String(64), nullable=False, unique=True)
    )
    title: str = Field(sa_column=Column("title", String(80), nullable=False))
    description: str = Field(
        default="", sa_column=Column("description", Text, nullable=False, server_default="")
    )
    # DatasetSchema serializado (core/datasets/schema.py). O atributo se chama
    # `schema_data` porque `schema_json` sombreia um método do BaseModel do
    # pydantic (warning ruidoso no import); a COLUNA continua `schema_json`.
    schema_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("schema_json", JSONB, nullable=False, server_default="{}"),
    )
    # Skill que criou/possui este cadastro. NULL = criado direto no painel; a
    # desinstalação da skill não mexe nos dados de qualquer forma (§3.5).
    skill_name: Optional[str] = Field(
        default=None, sa_column=Column("skill_name", String(40), nullable=True, index=True)
    )
    enabled: bool = Field(
        default=True, sa_column=Column("enabled", Boolean, nullable=False, server_default="true")
    )
    # Versão do FORMATO do schema_json (não do conteúdo) — permite migrar o
    # contrato sem reescrever os cadastros existentes.
    schema_version: int = Field(
        default=1, sa_column=Column("schema_version", Integer, nullable=False, server_default="1")
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class DatasetAudit(SQLModel, table=True):
    """Trilha do que é irreversível na prática: delete de linha e ALTER destrutivo."""

    __tablename__ = "neo_data_audit"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    dataset_slug: str = Field(
        sa_column=Column("dataset_slug", String(40), nullable=False, index=True)
    )
    # delete | restore | add_field | drop_field | alter_field | create | drop_table
    action: str = Field(sa_column=Column("action", String(20), nullable=False))
    # Quem mandou: "painel", "tool" (o cérebro), "instalador".
    actor: str = Field(
        default="painel", sa_column=Column("actor", String(20), nullable=False, server_default="painel")
    )
    row_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("row_id", SAUuid, nullable=True)
    )
    # Snapshot do que existia antes — é o que permite desfazer de verdade.
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("payload", JSONB, nullable=False, server_default="{}"),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SkillInstall(SQLModel, table=True):
    """Estado de instalação de uma skill (§3.3 do plano).

    ``install_hash`` é o sha256 do frontmatter normalizado: é ele que detecta
    drift quando o SKILL.md muda — inclusive quando quem muda é o próprio Neo.
    """

    __tablename__ = "skill_install"

    name: str = Field(sa_column=Column("name", String(40), primary_key=True))
    state: str = Field(
        default="planejada",
        sa_column=Column("state", String(16), nullable=False, server_default="planejada", index=True),
    )
    install_hash: str = Field(
        default="", sa_column=Column("install_hash", String(64), nullable=False, server_default="")
    )
    installed_version: int = Field(
        default=0, sa_column=Column("installed_version", Integer, nullable=False, server_default="0")
    )
    # Slugs de cadastro criados por esta skill (para a desinstalação saber o que
    # desligar — sem NUNCA dropar a tabela).
    datasets: list[Any] = Field(
        default_factory=list,
        sa_column=Column("datasets", JSONB, nullable=False, server_default="[]"),
    )
    tools: list[Any] = Field(
        default_factory=list,
        sa_column=Column("tools", JSONB, nullable=False, server_default="[]"),
    )
    screens: list[Any] = Field(
        default_factory=list,
        sa_column=Column("screens", JSONB, nullable=False, server_default="[]"),
    )
    last_error: str = Field(
        default="", sa_column=Column("last_error", Text, nullable=False, server_default="")
    )
    installed_at: Optional[datetime] = Field(default=None, nullable=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
