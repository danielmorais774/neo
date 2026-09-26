"""Solicitação de Liberação (docs/release-request-plan.md, F3).

Leve como ``core/credentials/__init__.py``: só o serviço e os tipos. Nada de
notificação, WebSocket ou LLM aqui — quem cria o card é injetado.
"""

from core.release.fill import (
    FillOutcome,
    PLACEHOLDER_RE,
    ReleaseFiller,
    domain_matches,
    host_of,
)
from core.release.necessity import (
    campos_admitidos,
    campos_desnecessarios,
    explicar,
    finalidade_conhecida,
)
from core.release.service import (
    DEFAULT_TTL_MIN,
    MAX_PENDENTES,
    ReleaseError,
    ReleaseService,
    ReleaseView,
    payload_fingerprint,
)

__all__ = [
    "DEFAULT_TTL_MIN",
    "campos_admitidos",
    "campos_desnecessarios",
    "explicar",
    "finalidade_conhecida",
    "FillOutcome",
    "PLACEHOLDER_RE",
    "ReleaseFiller",
    "domain_matches",
    "host_of",
    "MAX_PENDENTES",
    "ReleaseError",
    "ReleaseService",
    "ReleaseView",
    "payload_fingerprint",
]
