"""Tipos centrais do Service Gateway (docs/service-gateway-dsl-plan.md §3.1).

A ``ServiceRequestIR`` é a interface entre o LLM e o executor — os campos da
tool ``service_request`` são exatamente estes. A DSL textual ``.http`` (F3) é
só um front que converte para esta mesma IR; o caminho quente não tem parser.

Nada aqui carrega segredo: a conexão referencia a credencial por
``credential_ref`` (``env://VAR`` | ``fernet://<id>``); o material real só
existe em memória, dentro do broker, no momento da execução.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# Métodos aceitos. Mantido fechado de propósito: TRACE/CONNECT/OPTIONS não têm
# uso legítimo aqui e só ampliam superfície.
HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})
# Idempotentes segundo o RFC 7231 — só estes podem ser re-tentados no retry.
IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "PUT", "DELETE"})


class PolicyEffect(str, Enum):
    """Efeito de uma regra / de uma decisão de política."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


class AuthType(str, Enum):
    """Como o broker aplica a credencial na request (ver broker.py)."""

    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    HEADER = "header"
    QUERY_PARAM = "query_param"
    # Reservados para F4 (mTLS / OAuth) — declarados para a tabela não migrar depois.
    CLIENT_CERT = "client_cert"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    OAUTH2_DEVICE_FLOW = "oauth2_device_flow"


class ServiceRequestIR(BaseModel):
    """Requisição normalizada. O ``service`` é um slug de conexão — NUNCA um host.

    O ``path`` é relativo, começa com ``/`` e é normalizado pelo validator antes
    de qualquer match de política. Headers aqui são só complementares (Accept,
    Content-Type…); reservados de autenticação/transporte são recusados.
    """

    service: str
    method: HttpMethod
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    timeout_s: float | None = None


class ServiceResponse(BaseModel):
    """Resposta bruta devolvida ao cognitivo — status real, sem interpretação.

    O contrato é "verdade bruta": o modelo relata com base no ``status``, nunca
    presume sucesso (precedente do falso-sucesso da skill HA). ``body`` já vem
    redactado e truncado ao teto da conexão.
    """

    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    truncated: bool = False
    duration_ms: int = 0
    request_id: str = ""


class PolicyRule(BaseModel):
    """Regra de política de uma conexão. Avaliação: deny > confirm > allow.

    ``methods`` aceita ``["*"]`` para qualquer método. ``path`` é um glob
    (fnmatch) casado contra o path normalizado.
    """

    effect: PolicyEffect
    methods: list[str] = Field(default_factory=lambda: ["*"])
    path: str = "/*"
    description: str | None = None


class ConnectionConfig(BaseModel):
    """Representação em memória de uma conexão (linha de ``service_connection``
    ou seed de env). Consumida por policy/broker/executor. Sem segredo dentro.
    """

    id: str
    name: str
    provider: str = "generic"
    base_url: str
    auth_type: AuthType = AuthType.NONE
    auth_config: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = None
    default_headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 15.0
    retry_max: int = 2
    verify_tls: bool = True
    allow_http: bool = False
    follow_redirects: bool = False
    max_body_bytes: int = 262_144
    max_response_bytes: int = 65_536
    default_read: PolicyEffect = PolicyEffect.ALLOW
    default_write: PolicyEffect = PolicyEffect.CONFIRM
    policies: list[PolicyRule] = Field(default_factory=list)
    docs_url: str | None = None
    # [F3] Spec OpenAPI/Swagger crua + de onde veio. Indexada em memória pelo
    # GatewayService; o modelo só vê as operações que o service_docs seleciona.
    openapi_spec: str | None = None
    openapi_ref: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class PolicyDecision(BaseModel):
    """Resultado da avaliação de política para uma request."""

    effect: PolicyEffect
    matched_rule: PolicyRule | None = None
    reason: str = ""


class GatewayError(Exception):
    """Erro estrutural do gateway (validação, conexão inexistente, política).

    ``code`` é estável e serve ao modelo/painel; a mensagem é humana e já
    redactada. NUNCA carrega segredo.
    """

    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_result(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        return payload
