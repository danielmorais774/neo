"""Orquestrador do Service Gateway (docs/service-gateway-dsl-plan.md §2.1, §4).

Costura o pipeline na ordem imutável:

    validate → resolve connection → policy → (confirm?) → broker → execute
             → redact → audit → respond

O broker só é consultado DEPOIS do allow — segredo nunca é materializado para
uma request que seria negada. Os métodos ``handle_*`` devolvem dicts prontos
para virar ToolResult (verdade bruta, sem interpretação).
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_mod
from typing import Any
from uuid import UUID

from core.gateway.audit import AuditWriter
from core.gateway.broker import CredentialBroker
from core.gateway.confirm import ConfirmStore, PendingRequest
from core.gateway.executor import HTTPExecutor
from core.gateway.ir import (
    ConnectionConfig,
    GatewayError,
    PolicyEffect,
    ServiceRequestIR,
)
from core.gateway.policy import evaluate
from core.gateway.registry import ConnectionRegistry
from core.gateway.validator import validate_request

logger = logging.getLogger(__name__)


def _as_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _body_bytes(body: Any) -> int:
    if body is None:
        return 0
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    try:
        return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


class GatewayService:
    """Ponto único de entrada da lógica do gateway (usado pelo module executor)."""

    def __init__(
        self,
        *,
        registry: ConnectionRegistry,
        broker: CredentialBroker,
        http_executor: HTTPExecutor,
        confirm_store: ConfirmStore,
        audit: AuditWriter,
    ) -> None:
        self._registry = registry
        self._broker = broker
        self._http = http_executor
        self._confirm = confirm_store
        self._audit = audit
        # Cache do índice OpenAPI (F3): parsear uma spec grande a cada
        # service_docs seria desperdício — ela muda quando o painel salva.
        self._spec_cache_key: tuple[str, int] | None = None
        self._spec_cache: Any = None

    @property
    def registry(self) -> ConnectionRegistry:
        """O registry por trás do serviço — o painel (F2) faz CRUD por ele."""
        return self._registry

    # ------------------------------------------------------------------
    # service_list / service_docs
    # ------------------------------------------------------------------

    async def list_services(self) -> dict[str, Any]:
        conns = await self._registry.list_enabled()
        return {
            "connections": [
                {
                    "service": c.id,
                    "name": c.name,
                    "provider": c.provider,
                    "policy": self._policy_summary(c),
                    "docs_url": c.docs_url,
                    "capabilities": c.capabilities,
                }
                for c in conns
            ],
            "note": (
                "Use service_request para chamar um endpoint sem tool dedicada. O host e "
                "as credenciais são resolvidos pelo gateway — você só informa service, "
                "method, path (relativo) e body."
            ),
        }

    async def describe_service(self, service: str, query: str | None = None) -> dict[str, Any]:
        conn = await self._registry.get(service)
        if conn is None or not conn.enabled:
            return GatewayError("unknown_service", f"conexão '{service}' não existe ou está desabilitada.").to_result()
        payload: dict[str, Any] = {
            "service": conn.id,
            "name": conn.name,
            "base_path_note": "informe apenas o path relativo (ex.: /api/states).",
            "docs_url": conn.docs_url,
            "policy": self._policy_summary(conn),
            "capabilities": conn.capabilities,
        }
        # F3: com spec cadastrada, devolvemos as operações RELEVANTES à intenção —
        # nunca a spec inteira (centenas de KB estouram o contexto).
        index = self._openapi_index(conn)
        if index is not None:
            ops = index.search(query or "", limit=8)
            payload["operations"] = [op.to_dict() for op in ops]
            payload["spec"] = {
                "title": index.title,
                "version": index.version,
                "total_operations": len(index),
                "showing": len(ops),
            }
            payload["note"] = (
                "Estas são as operações mais relevantes da OpenAPI deste serviço"
                + (f" para '{query}'" if query else "")
                + ". Monte o service_request com o method e o path exatos acima "
                "(troque {placeholders} pelos valores). Se nada casar, chame "
                "service_docs de novo com outros termos."
            )
        else:
            payload["note"] = (
                "Sem OpenAPI cadastrada para este serviço: consulte docs_url para montar "
                "o path/body, ou peça ao usuário para cadastrar a spec no painel (aba Serviços)."
            )
        return payload

    def _openapi_index(self, conn: ConnectionConfig) -> Any | None:
        """Índice da spec da conexão, cacheado por conteúdo (a spec muda pouco)."""
        if not conn.openapi_spec:
            return None
        cache_key = (conn.id, hash(conn.openapi_spec))
        if self._spec_cache_key == cache_key:
            return self._spec_cache
        try:
            from core.gateway.openapi import parse_spec

            index = parse_spec(conn.openapi_spec)
        except Exception as exc:  # noqa: BLE001 — spec ruim não derruba o service_docs
            logger.warning("[gateway] openapi da conexão '%s' não parseou: %s", conn.id, exc)
            return None
        self._spec_cache_key = cache_key
        self._spec_cache = index
        return index

    @staticmethod
    def _policy_summary(conn: ConnectionConfig) -> str:
        read = {PolicyEffect.ALLOW: "leitura livre", PolicyEffect.CONFIRM: "leitura com confirmação",
                PolicyEffect.DENY: "leitura bloqueada"}[conn.default_read]
        write = {PolicyEffect.ALLOW: "escrita livre", PolicyEffect.CONFIRM: "escrita com confirmação",
                 PolicyEffect.DENY: "escrita bloqueada"}[conn.default_write]
        denies = [r.path for r in conn.policies if r.effect is PolicyEffect.DENY]
        extra = f"; bloqueios: {', '.join(denies[:4])}" if denies else ""
        return f"{read}; {write} (por padrão){extra}"

    # ------------------------------------------------------------------
    # service_request
    # ------------------------------------------------------------------

    async def handle_request(
        self,
        raw: dict[str, Any],
        *,
        source: str = "chat",
        user_id: Any = None,
        session_id: Any = None,
    ) -> dict[str, Any]:
        try:
            ir = ServiceRequestIR(**raw)
        except Exception as exc:  # noqa: BLE001 — erro de schema vira erro amigável
            return GatewayError("invalid_request", f"requisição malformada: {exc}").to_result()

        conn = await self._registry.get(ir.service)
        if conn is None or not conn.enabled:
            return GatewayError(
                "unknown_service",
                f"conexão '{ir.service}' não existe ou está desabilitada. Use service_list para ver as disponíveis.",
            ).to_result()

        try:
            ir = validate_request(ir, conn)
        except GatewayError as exc:
            await self._audit.record(
                connection_id=conn.id, method=ir.method, path=ir.path,
                policy_decision="error", policy_reason=exc.code, source=source,
                user_id=_as_uuid(user_id), session_id=_as_uuid(session_id), error=exc.message,
            )
            return exc.to_result()

        decision = evaluate(ir, conn)

        if decision.effect is PolicyEffect.DENY:
            await self._audit.record(
                connection_id=conn.id, method=ir.method, path=ir.path,
                policy_decision="deny", policy_reason=decision.reason, source=source,
                user_id=_as_uuid(user_id), session_id=_as_uuid(session_id),
            )
            return {
                "denied": True,
                "reason": decision.reason,
                "message": (
                    f"A política da conexão '{conn.id}' bloqueia {ir.method} {ir.path}. "
                    "Explique ao usuário ou proponha uma alternativa permitida."
                ),
            }

        if decision.effect is PolicyEffect.CONFIRM:
            confirmation_id = self._confirm.put(
                ir, decision.reason, user_id=str(user_id) if user_id else None,
                session_id=str(session_id) if session_id else None,
            )
            await self._audit.record(
                connection_id=conn.id, method=ir.method, path=ir.path,
                policy_decision="confirm_pending", policy_reason=decision.reason, source=source,
                user_id=_as_uuid(user_id), session_id=_as_uuid(session_id),
                request_id=confirmation_id,
            )
            return {
                "pending_confirmation": True,
                "confirmation_id": confirmation_id,
                "summary": {
                    "service": conn.id,
                    "method": ir.method,
                    "path": ir.path,
                    "body_preview": self._body_preview(ir.body),
                },
                "message": (
                    f"Esta é uma escrita ({ir.method} {ir.path} em {conn.name}). Confirme com o "
                    "usuário em linguagem clara e, se ele aprovar, chame service_confirm com este "
                    "confirmation_id. NÃO reenvie service_request."
                ),
            }

        # allow → executa
        return await self._execute(ir, conn, source=source, user_id=user_id, session_id=session_id)

    # ------------------------------------------------------------------
    # service_confirm
    # ------------------------------------------------------------------

    async def handle_confirm(
        self,
        confirmation_id: str,
        *,
        source: str = "chat",
        user_id: Any = None,
        session_id: Any = None,
    ) -> dict[str, Any]:
        pending: PendingRequest | None = self._confirm.take(confirmation_id)
        if pending is None:
            return GatewayError(
                "confirmation_not_found",
                "confirmação inexistente, já usada ou expirada (validade de 5 min). Refaça o pedido.",
            ).to_result()
        conn = await self._registry.get(pending.ir.service)
        if conn is None or not conn.enabled:
            return GatewayError("unknown_service", f"conexão '{pending.ir.service}' indisponível.").to_result()
        # Reavalia a política no momento da confirmação (defesa se a política mudou).
        decision = evaluate(pending.ir, conn)
        if decision.effect is PolicyEffect.DENY:
            await self._audit.record(
                connection_id=conn.id, method=pending.ir.method, path=pending.ir.path,
                policy_decision="deny", policy_reason=decision.reason, source=source,
                user_id=_as_uuid(user_id), session_id=_as_uuid(session_id),
            )
            return {"denied": True, "reason": decision.reason, "message": "A política passou a bloquear esta operação."}
        return await self._execute(
            pending.ir, conn, source=source, user_id=user_id, session_id=session_id, confirmed=True,
        )

    # ------------------------------------------------------------------
    # Execução comum
    # ------------------------------------------------------------------

    async def _execute(
        self,
        ir: ServiceRequestIR,
        conn: ConnectionConfig,
        *,
        source: str,
        user_id: Any,
        session_id: Any,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        try:
            auth = await self._broker.build_auth(conn)  # segredo só a partir daqui
        except GatewayError as exc:
            await self._audit.record(
                connection_id=conn.id, method=ir.method, path=ir.path,
                policy_decision="error", policy_reason=exc.code, source=source,
                user_id=_as_uuid(user_id), session_id=_as_uuid(session_id), error=exc.message,
            )
            return exc.to_result()

        request_id = uuid_mod.uuid4().hex
        try:
            response = await self._http.execute(ir, conn, auth, request_id=request_id)
        except GatewayError as exc:
            await self._audit.record(
                connection_id=conn.id, method=ir.method, path=ir.path,
                policy_decision="error", policy_reason=exc.code, source=source,
                user_id=_as_uuid(user_id), session_id=_as_uuid(session_id),
                error=exc.message, request_id=request_id, req_bytes=_body_bytes(ir.body),
            )
            return exc.to_result()

        await self._audit.record(
            connection_id=conn.id, method=ir.method, path=ir.path,
            policy_decision="confirmed" if confirmed else "allow",
            source=source, user_id=_as_uuid(user_id), session_id=_as_uuid(session_id),
            status=response.status, duration_ms=response.duration_ms,
            req_bytes=_body_bytes(ir.body), resp_bytes=len(response.body.encode("utf-8")),
            request_id=request_id,
        )
        result = response.model_dump()
        result["ok"] = 200 <= response.status < 300
        return result

    @staticmethod
    def _body_preview(body: Any) -> str:
        if body is None:
            return ""
        try:
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(body)
        return text[:300]
