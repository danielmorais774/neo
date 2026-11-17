"""Store de confirmações pendentes (docs/service-gateway-dsl-plan.md §2.2).

Quando a política resolve ``confirm``, a request validada (SEM segredo — só a
IR) é guardada aqui com um id opaco, TTL curto e uso único. O Neo mostra o
resumo ao usuário; um ``service_confirm(id)`` afirmativo reexecuta.

In-memory de propósito para a F1: processo único, usuário único. O segredo NÃO
mora aqui — só a IR; o broker resolve de novo na hora de confirmar. Se o
processo reiniciar, a confirmação expira junto (fail-safe: escrita não sobrevive
a um restart sem nova aprovação).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from core.gateway.ir import ServiceRequestIR

_DEFAULT_TTL = 300.0  # 5 min
_MAX_PENDING = 64


@dataclass
class PendingRequest:
    request_id: str
    ir: ServiceRequestIR
    reason: str
    created_at: float = field(default_factory=time.monotonic)
    user_id: str | None = None
    session_id: str | None = None


class ConfirmStore:
    """Guarda requests aguardando aprovação humana. TTL + uso único."""

    def __init__(self, ttl: float = _DEFAULT_TTL, max_pending: int = _MAX_PENDING) -> None:
        self._ttl = ttl
        self._max = max_pending
        self._pending: dict[str, PendingRequest] = {}

    def _sweep(self) -> None:
        now = time.monotonic()
        expired = [k for k, p in self._pending.items() if now - p.created_at > self._ttl]
        for key in expired:
            self._pending.pop(key, None)

    def put(
        self, ir: ServiceRequestIR, reason: str, *, user_id: str | None = None, session_id: str | None = None
    ) -> str:
        self._sweep()
        if len(self._pending) >= self._max:
            oldest = min(self._pending, key=lambda k: self._pending[k].created_at)
            self._pending.pop(oldest, None)
        confirmation_id = uuid.uuid4().hex
        self._pending[confirmation_id] = PendingRequest(
            request_id=confirmation_id, ir=ir, reason=reason, user_id=user_id, session_id=session_id
        )
        return confirmation_id

    def take(self, confirmation_id: str) -> PendingRequest | None:
        """Consome (uso único). Devolve None se inexistente/expirado."""
        self._sweep()
        return self._pending.pop(confirmation_id, None)
