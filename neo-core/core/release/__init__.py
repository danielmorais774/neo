"""Solicitação de Liberação (docs/release-request-plan.md, F3).

Leve como ``core/credentials/__init__.py``: só o serviço e os tipos. Nada de
notificação, WebSocket ou LLM aqui — quem cria o card é injetado.
"""

from core.release.fill import (
    FillOutcome,
    PLACEHOLDER_RE,
    ReleaseFiller,
    campos_do_texto,
    domain_matches,
    host_of,
)
from core.release.necessity import (
    campos_admitidos,
    campos_desnecessarios,
    explicar,
    finalidade_conhecida,
)
from core.release.sensitive import (
    CAMPOS_SENSIVEIS,
    campo_do_fill,
    classificar_campo,
    classificar_valor,
    humano,
)
from core.release.service import (
    DEFAULT_TTL_MIN,
    MAX_PENDENTES,
    ReleaseError,
    ReleaseService,
    ReleaseView,
    motivos_de_origem,
    payload_fingerprint,
)

__all__ = [
    "CAMPOS_SENSIVEIS",
    "DEFAULT_TTL_MIN",
    "campo_do_fill",
    "campos_admitidos",
    "campos_desnecessarios",
    "classificar_campo",
    "classificar_valor",
    "explicar",
    "finalidade_conhecida",
    "humano",
    "FillOutcome",
    "PLACEHOLDER_RE",
    "ReleaseFiller",
    "campos_do_texto",
    "domain_matches",
    "host_of",
    "MAX_PENDENTES",
    "ReleaseError",
    "ReleaseService",
    "ReleaseView",
    "motivos_de_origem",
    "payload_fingerprint",
]
