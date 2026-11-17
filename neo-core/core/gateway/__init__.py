"""Service Gateway — HTTP arbitrário para serviços cadastrados sem o LLM ver
credencial (docs/service-gateway-dsl-plan.md).

Ponto de passagem único (validate → policy → confirm/broker → execute → audit).
Use ``build_gateway_service`` para montar tudo com as dependências reais.
"""

from __future__ import annotations

from typing import Any

from core.gateway.audit import AuditWriter
from core.gateway.broker import CredentialBroker, CredentialResolver
from core.gateway.confirm import ConfirmStore
from core.gateway.executor import HTTPExecutor
from core.gateway.ir import (
    ConnectionConfig,
    GatewayError,
    PolicyEffect,
    PolicyRule,
    ServiceRequestIR,
    ServiceResponse,
)
from core.gateway.registry import ConnectionRegistry
from core.gateway.service import GatewayService

__all__ = [
    "build_gateway_service",
    "GatewayService",
    "ConnectionRegistry",
    "ConnectionConfig",
    "ServiceRequestIR",
    "ServiceResponse",
    "PolicyRule",
    "PolicyEffect",
    "GatewayError",
]


def build_gateway_service(
    *, db: Any = None, settings: Any = None, fernet_store: Any = None
) -> GatewayService:
    """Monta o GatewayService com registry (db+seeds), broker (env://), executor,
    store de confirmações e auditoria."""
    registry = ConnectionRegistry(db=db, settings=settings)
    resolver = CredentialResolver(settings=settings, fernet_store=fernet_store)
    broker = CredentialBroker(resolver)
    return GatewayService(
        registry=registry,
        broker=broker,
        http_executor=HTTPExecutor(),
        confirm_store=ConfirmStore(),
        audit=AuditWriter(db),
    )
