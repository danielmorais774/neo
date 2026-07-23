"""Module executor do Service Gateway (docs/service-gateway-dsl-plan.md §2.1).

Camada fina: recebe a tool call do ToolExecutor global e delega ao
``GatewayService`` (que faz validate → policy → confirm/broker → execute →
audit). Mantém o ``GatewayService`` como singleton para o store de confirmações
sobreviver entre turnos.

Contexto (user_id/session_id/source) é best-effort na F1: o executor global
registrado no boot não o thread-a por request, então a auditoria grava o que
tiver (default source='chat'). ``set_context`` existe para o dia em que o
cognitivo criar um executor fresco por request (como devices/finance).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.gateway.service import GatewayService

logger = logging.getLogger(__name__)


class ServicesExecutor:
    """Executor server-side das tools service_*."""

    def __init__(self, gateway: GatewayService) -> None:
        self._gateway = gateway
        self._user_id: UUID | None = None
        self._session_id: UUID | None = None
        self._source: str = "chat"

    def set_context(
        self, user_id: UUID | None = None, session_id: UUID | None = None, source: str = "chat"
    ) -> None:
        self._user_id = user_id
        self._session_id = session_id
        self._source = source

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "service_list":
            return await self._gateway.list_services()
        if tool_name == "service_docs":
            return await self._gateway.describe_service(
                str(arguments.get("service") or ""), arguments.get("query")
            )
        if tool_name == "service_request":
            payload = {
                "service": arguments.get("service"),
                "method": arguments.get("method"),
                "path": arguments.get("path"),
                "query": arguments.get("query") or {},
                "headers": arguments.get("headers") or {},
                "body": arguments.get("body"),
                "timeout_s": arguments.get("timeout_s"),
            }
            return await self._gateway.handle_request(
                payload, source=self._source, user_id=self._user_id, session_id=self._session_id
            )
        if tool_name == "service_confirm":
            return await self._gateway.handle_confirm(
                str(arguments.get("confirmation_id") or ""),
                source=self._source,
                user_id=self._user_id,
                session_id=self._session_id,
            )
        raise ValueError(f"Unknown services tool: {tool_name}")
