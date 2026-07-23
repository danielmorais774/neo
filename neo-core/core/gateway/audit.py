"""Writer da auditoria do gateway (docs/service-gateway-dsl-plan.md §2.2).

Grava um ``service_request_log`` para TODA decisão — allow, deny, confirm e
erro. Path e erro entram já redactados; header de auth e body completo nunca
entram. É o sensor de abuso induzido e a verdade consultável (o painel e uma
tool read-only na F2 leem daqui).

A gravação é best-effort: uma falha de auditoria nunca derruba a requisição do
usuário (mas é logada).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.gateway.redact import redact_text
from db.models.gateway import ServiceRequestLog

logger = logging.getLogger(__name__)


class AuditWriter:
    """Persiste registros de auditoria. Recebe o db client (get_session)."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def record(
        self,
        *,
        connection_id: str,
        method: str,
        path: str,
        policy_decision: str,
        policy_reason: str | None = None,
        source: str = "chat",
        user_id: UUID | None = None,
        session_id: UUID | None = None,
        status: int | None = None,
        duration_ms: int | None = None,
        req_bytes: int | None = None,
        resp_bytes: int | None = None,
        error: str | None = None,
        request_id: str | None = None,
    ) -> None:
        if self._db is None:
            return
        entry = ServiceRequestLog(
            connection_id=connection_id,
            method=method,
            path=redact_text(path)[:1000],
            policy_decision=policy_decision,
            policy_reason=(policy_reason or "")[:300] or None,
            source=source,
            user_id=user_id,
            session_id=session_id,
            status=status,
            duration_ms=duration_ms,
            req_bytes=req_bytes,
            resp_bytes=resp_bytes,
            error=redact_text(error)[:4000] if error else None,
            request_id=request_id,
        )
        try:
            async with self._db.get_session() as session:
                session.add(entry)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — auditoria nunca derruba o request
            logger.warning("[gateway] falha ao gravar auditoria: %s", exc)
