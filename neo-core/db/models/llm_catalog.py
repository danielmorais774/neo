"""LLMProvider / LLMModel — catálogo de LLM editável no painel.

Fonte única dos providers e modelos que o core sabe usar e que os seletores do
chat (desktop e painel) oferecem. Quem lê é ``modules.llm_catalog``, que
fotografa estas tabelas em ``core.llm.catalog`` a cada gravação.

A chave de API mora AQUI, cifrada com a mesma ``NEO_CRED_KEY``/Fernet do cofre,
e não como uma linha de ``credential``: o ``vault_list`` mostra todo item do
cofre ao cérebro, e apagar o item na tela do Cofre derrubaria o provider sem
aviso. Sem chave no banco vale a do ``.env`` (reserva dos providers de fábrica).

Model NÃO importa de ``core`` (o alembic morre em circular import — ver
db/models/credential.py); a lista de adapters válida mora em core/llm/catalog.py.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Column,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


class LLMProvider(SQLModel, table=True):
    __tablename__ = "llm_provider"

    # Slug estável: é o que os slots (aba Modelos), o seletor do chat e o
    # DEFAULT_PROVIDER do env guardam. Não muda depois de criado.
    name: str = Field(sa_column=Column("name", String(40), primary_key=True))
    label: str = Field(sa_column=Column("label", String(80), nullable=False))
    adapter: str = Field(sa_column=Column("adapter", String(30), nullable=False))
    base_url: Optional[str] = Field(
        default=None, sa_column=Column("base_url", String(300), nullable=True)
    )
    # Fernet token (bytes). NULL = sem chave no banco (vale a do .env, se houver).
    api_key_encrypted: Optional[bytes] = Field(
        default=None, sa_column=Column("api_key_encrypted", LargeBinary, nullable=True)
    )
    api_key_hint: Optional[str] = Field(
        default=None, sa_column=Column("api_key_hint", String(8), nullable=True)
    )
    enabled: bool = Field(
        default=True, sa_column=Column("enabled", Boolean, nullable=False, server_default="true")
    )
    default_model: Optional[str] = Field(
        default=None, sa_column=Column("default_model", String(200), nullable=True)
    )
    position: int = Field(
        default=0, sa_column=Column("position", Integer, nullable=False, server_default="0")
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", DateTime, nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("updated_at", DateTime, nullable=False)
    )


class LLMModel(SQLModel, table=True):
    __tablename__ = "llm_model"
    # Único no catálogo INTEIRO, não por provider: `get_provider_for_model(id)`
    # precisa de uma resposta só. O mesmo modelo em dois providers ganha ids
    # diferentes de qualquer jeito (`deepseek-flash` × `deepseek/deepseek-v4.1-flash`).
    __table_args__ = (UniqueConstraint("model_id", name="uq_llm_model_model_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    provider: str = Field(
        sa_column=Column(
            "provider",
            String(40),
            ForeignKey("llm_provider.name", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    # O id que vai para a API, com o sufixo `@slug` quando há pin de endpoint.
    model_id: str = Field(sa_column=Column("model_id", String(200), nullable=False))
    label: str = Field(default="", sa_column=Column("label", String(120), nullable=False, server_default=""))
    description: str = Field(
        default="", sa_column=Column("description", String(300), nullable=False, server_default="")
    )
    vision: bool = Field(
        default=False, sa_column=Column("vision", Boolean, nullable=False, server_default="false")
    )
    in_selector: bool = Field(
        default=False,
        sa_column=Column("in_selector", Boolean, nullable=False, server_default="false"),
    )
    position: int = Field(
        default=0, sa_column=Column("position", Integer, nullable=False, server_default="0")
    )
    notes: str = Field(default="", sa_column=Column("notes", Text, nullable=False, server_default=""))
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", DateTime, nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("updated_at", DateTime, nullable=False)
    )
