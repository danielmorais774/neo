"""Executor da tool `conversation_recall`.

Contrato padrão de módulo (``execute(tool_name, arguments)``) com
``set_context(user_id, session_id)`` por request, no molde de
``modules/pks_search``.

Além de buscar, guarda o CURSOR da busca na sessão
(``conversation_session.extra_metadata["recall"]``, mesmo mecanismo do
``active_skill``): é ele que faz o "não foi isso, foi antes ainda" continuar de
onde parou em vez de recomeçar. Ver docs/conversation-recall-plan.md §4/F4.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from core.memory.conversation_recall import ConversationRecallSearch, RecallResult
from modules.conversation_recall.render import render_recall_block, render_recall_summary

logger = logging.getLogger(__name__)

# Por quantos turnos sem uso o cursor de recall continua vivo na sessão.
CURSOR_TTL_TURNS = 3


def parse_before(raw: Any) -> datetime | None:
    """Lê o ``before`` que o modelo mandou (ISO-8601), tolerante a lixo.

    O modelo às vezes devolve "2026-08-01T19:00:40Z" ou com offset; o banco
    guarda naive-UTC, então normaliza para naive.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("[Recall] `before` inválido: %r — ignorado", raw)
        return None
    if parsed.tzinfo is not None:
        from datetime import timezone

        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class ConversationRecallExecutor:
    """Executa `conversation_recall` contra o arquivo de conversas do usuário."""

    def __init__(self, db: Any, embeddings: Any = None) -> None:
        self._db = db
        self._search = ConversationRecallSearch(db=db, embeddings=embeddings)
        self._user_id: UUID | None = None
        self._session_id: UUID | None = None

    def set_context(self, user_id: UUID | None, session_id: UUID | None = None) -> None:
        self._user_id = user_id
        self._session_id = session_id

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "conversation_recall":
            raise ValueError(f"Unknown conversation_recall tool: {tool_name}")
        if not self._user_id:
            raise ValueError("conversation_recall requer um usuário identificado.")

        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("conversation_recall requer 'query' não-vazia.")
        before = parse_before(arguments.get("before"))

        from shared.config import get_settings

        settings = get_settings()
        result = await self._search.search(
            user_id=self._user_id,
            query=query,
            before=before,
            exclude_session_id=self._session_id,
            limit_sessions=getattr(settings, "conversation_recall_sessions_per_page", 3),
            per_session=getattr(settings, "conversation_recall_excerpts_per_session", 4),
        )
        await self._save_cursor(result, before)
        return self.to_payload(result)

    # ── payload ───────────────────────────────────────────────────────

    @staticmethod
    def to_payload(result: RecallResult) -> dict[str, Any]:
        """Resultado da tool.

        ``bloco`` é o corpo que o loop cognitivo injeta como SYSTEM (fora do
        histórico); ``resumo`` é o que fica no slot de resultado de tool. O
        resto é metadado para o log e para o cursor.
        """
        return {
            "resumo": render_recall_summary(result),
            "bloco": render_recall_block(result),
            "encontrou": result.found,
            "total_conversas": result.total_sessions,
            "total_mensagens": result.total_messages,
            "termos": result.terms,
            "trilho": result.track,
            "tem_mais": result.has_more,
            "proxima_busca_before": (
                result.next_before.isoformat() if result.next_before else None
            ),
            "erro": result.error,
        }

    # ── cursor ────────────────────────────────────────────────────────

    async def _save_cursor(self, result: RecallResult, before: datetime | None) -> None:
        """Persiste onde esta busca parou, para o "foi antes ainda" continuar.

        Best-effort: falhar aqui não pode derrubar o turno — no pior caso a
        próxima página recomeça do topo.
        """
        if self._session_id is None or result.error == "sem_termo":
            return
        try:
            sess = await self._db.get_conversation_session(self._session_id)
            if sess is None:
                return
            meta = dict(sess.extra_metadata or {})
            meta["recall"] = {
                "query": result.query,
                "terms": result.terms,
                "before": before.isoformat() if before else None,
                "next_before": (
                    result.next_before.isoformat() if result.next_before else None
                ),
                "has_more": result.has_more,
                "seen_sessions": [str(s.session_id) for s in result.window],
                "turns_left": CURSOR_TTL_TURNS,
            }
            sess.extra_metadata = meta
            await self._db.update_session(sess)
        except Exception as e:  # noqa: BLE001 — cursor nunca derruba o turno
            logger.warning("[Recall] persistência do cursor falhou: %s", e)
