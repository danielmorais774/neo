"""Autoridade do turno e portão de liberação (docs/release-request-plan.md).

Deliberadamente LEVE, pelo mesmo motivo de ``core/credentials/__init__.py``:
este pacote é importado pelo executor de tools, que é carregado sempre. Nada de
banco, nada de LLM, nada que possa derrubar o boot no reload.
"""

from core.authority.gate import (
    GateDecision,
    covering_release,
    set_release_service,
    NEGAR,
    PERMITIR,
    SOLICITAR,
    evaluate,
    gate_tool_call,
)
from core.authority.images import check_inline_images, enforce_inline_images
from core.authority.sinks import (
    Destination,
    covers,
    destination_of,
    has_destination,
    payload_of,
)
from core.authority.risk import (
    ATUACAO,
    FONTE_EXTERNA,
    LEITURA_PRIVADA,
    REVELACAO,
    classes_of,
    is_actuation,
    is_external_source,
    is_private_read,
    is_sink,
    known,
)
from core.authority.turn import (
    PRINCIPAL_ADMIN,
    PRINCIPAL_AGENDA,
    PRINCIPAL_EXTERNO,
    TurnAuthority,
    bind_authority,
    canonical_url,
    current_authority,
    lote_de_chamadas,
    note_tool_result,
    principal_from_metadata,
    reset_authority,
    turno_de_fundo,
)
# Importado DEPOIS de gate/turn de propósito: o invólucro usa os dois.
from core.authority.wrap import ExecutorComPortao, com_portao

__all__ = [
    "ATUACAO",
    "Destination",
    "ExecutorComPortao",
    "com_portao",
    "reset_authority",
    "turno_de_fundo",
    "covering_release",
    "covers",
    "destination_of",
    "has_destination",
    "payload_of",
    "set_release_service",
    "FONTE_EXTERNA",
    "GateDecision",
    "LEITURA_PRIVADA",
    "NEGAR",
    "PERMITIR",
    "PRINCIPAL_ADMIN",
    "PRINCIPAL_AGENDA",
    "PRINCIPAL_EXTERNO",
    "REVELACAO",
    "SOLICITAR",
    "TurnAuthority",
    "bind_authority",
    "canonical_url",
    "check_inline_images",
    "classes_of",
    "enforce_inline_images",
    "current_authority",
    "evaluate",
    "gate_tool_call",
    "is_actuation",
    "is_external_source",
    "is_private_read",
    "is_sink",
    "known",
    "lote_de_chamadas",
    "note_tool_result",
    "principal_from_metadata",
]
