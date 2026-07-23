"""Modelos do Service Gateway (docs/service-gateway-dsl-plan.md §3.2).

``service_connection`` é o registro central de conexões (host, auth por
REFERÊNCIA, política) — a fonte de verdade que o ConnectionRegistry carrega e
cacheia. NUNCA guarda o segredo em si: só ``credential_ref``
(``env://VAR`` | ``fernet://<id>``).

``service_request_log`` é a auditoria — grava SEMPRE (allow, deny, confirm,
erro), com path/erro já redactados e sem header de auth. É o sensor de abuso
induzido por prompt injection e a "verdade consultável" (lição do gap de
observabilidade da skill HA).
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    Numeric,
    String,
    Text,
    Uuid as SAUuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ServiceConnection(SQLModel, table=True):
    """Uma conexão HTTP cadastrada. ``id`` é o slug usado na DSL/tool."""

    __tablename__ = "service_connection"

    # Slug: ^[a-z0-9][a-z0-9-]{1,39}$ (validado na camada de registry/painel).
    id: str = Field(sa_column=Column("id", String(40), primary_key=True))
    name: str = Field(sa_column=Column("name", String(120), nullable=False))
    provider: str = Field(
        default="generic", sa_column=Column("provider", String(40), nullable=False)
    )
    base_url: str = Field(sa_column=Column("base_url", String(500), nullable=False))
    auth_type: str = Field(
        default="none", sa_column=Column("auth_type", String(40), nullable=False)
    )
    auth_config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("auth_config", JSONB, nullable=False, server_default="{}")
    )
    credential_ref: Optional[str] = Field(
        default=None, sa_column=Column("credential_ref", String(200), nullable=True)
    )
    default_headers: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("default_headers", JSONB, nullable=False, server_default="{}")
    )
    timeout_s: float = Field(
        default=15.0, sa_column=Column("timeout_s", Numeric(6, 2), nullable=False, server_default="15")
    )
    retry_max: int = Field(
        default=2, sa_column=Column("retry_max", Integer, nullable=False, server_default="2")
    )
    verify_tls: bool = Field(
        default=True, sa_column=Column("verify_tls", Boolean, nullable=False, server_default="true")
    )
    allow_http: bool = Field(
        default=False, sa_column=Column("allow_http", Boolean, nullable=False, server_default="false")
    )
    follow_redirects: bool = Field(
        default=False, sa_column=Column("follow_redirects", Boolean, nullable=False, server_default="false")
    )
    max_body_bytes: int = Field(
        default=262_144, sa_column=Column("max_body_bytes", Integer, nullable=False, server_default="262144")
    )
    max_response_bytes: int = Field(
        default=65_536, sa_column=Column("max_response_bytes", Integer, nullable=False, server_default="65536")
    )
    default_read: str = Field(
        default="allow", sa_column=Column("default_read", String(10), nullable=False, server_default="allow")
    )
    default_write: str = Field(
        default="confirm", sa_column=Column("default_write", String(10), nullable=False, server_default="confirm")
    )
    # Lista ordenada de regras (ver core/gateway/ir.py::PolicyRule).
    policies: list[Any] = Field(
        default_factory=list, sa_column=Column("policies", JSONB, nullable=False, server_default="[]")
    )
    docs_url: Optional[str] = Field(
        default=None, sa_column=Column("docs_url", String(500), nullable=True)
    )
    # [F3] De onde veio a spec (URL ou "upload") — rastreabilidade para o painel.
    openapi_ref: Optional[str] = Field(
        default=None, sa_column=Column("openapi_ref", String(500), nullable=True)
    )
    # [F3] A spec crua (JSON/YAML). Fica no banco porque é config da conexão, não
    # arquivo do usuário; é indexada em memória no load e nunca vai inteira ao LLM.
    openapi_spec: Optional[str] = Field(
        default=None, sa_column=Column("openapi_spec", Text, nullable=True)
    )
    capabilities: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("capabilities", JSONB, nullable=False, server_default="{}")
    )
    enabled: bool = Field(
        default=True, sa_column=Column("enabled", Boolean, nullable=False, server_default="true")
    )
    # Conexões vindas de env (seed) são read-only no painel — marcadas aqui.
    managed_by: str = Field(
        default="panel", sa_column=Column("managed_by", String(20), nullable=False, server_default="panel")
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("updated_at", nullable=False)
    )


class ServiceRequestLog(SQLModel, table=True):
    """Auditoria de cada requisição do gateway. Nunca guarda segredo."""

    __tablename__ = "service_request_log"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    ts: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("ts", nullable=False, index=True)
    )
    user_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("user_id", SAUuid, nullable=True, index=True)
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("session_id", SAUuid, nullable=True)
    )
    source: str = Field(
        default="chat", sa_column=Column("source", String(20), nullable=False)  # chat|job|skill|panel
    )
    connection_id: str = Field(
        sa_column=Column("connection_id", String(40), nullable=False, index=True)
    )
    method: str = Field(sa_column=Column("method", String(10), nullable=False))
    path: str = Field(sa_column=Column("path", String(1000), nullable=False))  # redactado
    policy_decision: str = Field(
        sa_column=Column("policy_decision", String(20), nullable=False)  # allow|deny|confirm_pending|confirmed|error
    )
    policy_reason: Optional[str] = Field(
        default=None, sa_column=Column("policy_reason", String(300), nullable=True)
    )
    status: Optional[int] = Field(default=None, sa_column=Column("status", Integer, nullable=True))
    duration_ms: Optional[int] = Field(
        default=None, sa_column=Column("duration_ms", Integer, nullable=True)
    )
    req_bytes: Optional[int] = Field(default=None, sa_column=Column("req_bytes", Integer, nullable=True))
    resp_bytes: Optional[int] = Field(default=None, sa_column=Column("resp_bytes", Integer, nullable=True))
    error: Optional[str] = Field(default=None, sa_column=Column("error", Text, nullable=True))  # redactado
    request_id: Optional[str] = Field(
        default=None, sa_column=Column("request_id", String(64), nullable=True)
    )
