"""Executor das tools ``wm_*`` (docs/working-memory-plan.md §5).

Contrato padrão de módulo (``execute(tool_name, arguments)``) com
``set_context(user_id, session_id)`` por request, no molde de
``modules/conversation_recall``. O caderno é por usuário, então a instância é
fresca a cada turno — nunca a global (que fica sem usuário).

O retorno é escrito para o MODELO ler: confirma o que foi gravado e diz o nome
pelo qual reabrir depois. Erro de negócio vira mensagem acionável, não stack.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.working_memory.store import WorkingMemoryError, WorkingMemoryStore
from modules.working_memory.tools import WORKING_MEMORY_TOOL_NAMES

logger = logging.getLogger(__name__)


def _as_bool(value: Any) -> bool:
    """O modelo manda ``true``, ``"true"`` ou ``"sim"`` — todos valem."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "sim")


def _as_links(value: Any) -> list[str]:
    """``links`` chega como CSV (o schema é string — ver tools.py)."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


class WorkingMemoryExecutor:
    """Executa ``wm_write``/``wm_read`` contra o caderno do usuário."""

    def __init__(self, db: Any, store: WorkingMemoryStore | None = None) -> None:
        from shared.config import get_settings

        settings = get_settings()
        self._store = store or WorkingMemoryStore(
            db=db,
            max_note_bytes=getattr(settings, "working_memory_note_max_bytes", 32768),
            ttl_days=getattr(settings, "working_memory_ttl_days", 30),
            max_index_notes=getattr(settings, "working_memory_max_notes_in_index", 8),
            index_auto_quota=getattr(settings, "working_memory_index_auto_quota", 2),
            read_tail_entries=getattr(settings, "working_memory_read_tail_entries", 4),
        )
        self._user_id: UUID | None = None
        self._session_id: UUID | None = None

    def set_context(self, user_id: UUID | None, session_id: UUID | None = None) -> None:
        self._user_id = user_id
        self._session_id = session_id

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in WORKING_MEMORY_TOOL_NAMES:
            raise ValueError(f"Unknown working_memory tool: {tool_name}")
        try:
            if tool_name == "wm_write":
                return await self._write(arguments)
            return await self._store.read(
                user_id=self._user_id,
                slug=str(arguments.get("note") or ""),
                full=_as_bool(arguments.get("full")),
            )
        except WorkingMemoryError as exc:
            # Esperado (slug ruim, conteúdo vazio, sem usuário): o modelo precisa
            # conseguir corrigir o rumo na mesma resposta.
            return {"error": exc.code, "message": exc.message}

    async def _write(self, arguments: dict[str, Any]) -> dict[str, Any]:
        note = await self._store.write(
            user_id=self._user_id,
            session_id=self._session_id,
            slug=str(arguments.get("note") or ""),
            content=str(arguments.get("content") or ""),
            title=arguments.get("title"),
            summary=arguments.get("summary"),
            mode=str(arguments.get("mode") or "append"),
            status=str(arguments.get("status") or "active"),
            links=_as_links(arguments.get("links")),
        )
        return {
            "note": note.slug,
            "title": note.title,
            "entries": note.entry_count,
            "status": note.status,
            "message": (
                f"Anotado em [[{note.slug}]] (entrada {note.entry_count}). "
                "Não repita o conteúdo da anotação ao usuário — só siga a conversa. "
                f"Nos próximos turnos, reabra com wm_read('{note.slug}') em vez de "
                "refazer este trabalho."
            ),
        }
