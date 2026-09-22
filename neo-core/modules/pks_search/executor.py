"""PKS search executor — active verification reads against the user's PKS.

Standard module-executor contract (``execute(tool_name, arguments)``) with a
per-request ``set_context(user_id)``, mirroring ``modules/devices``. Delegates to
``PKSService.search_entities`` and compacts the rows for the LLM.
"""

import logging
from typing import Any
from uuid import UUID

from core.pks.ledger_guard import LEDGER_REDIRECT, looks_like_money_query

logger = logging.getLogger(__name__)

_MAX_RESULTS = 12


class PKSSearchExecutor:
    """Executes the ``search_pks`` tool against the user's PKS workspace."""

    def __init__(self, pks_service: Any) -> None:
        self.pks = pks_service
        self._user_id: UUID | None = None

    def set_context(self, user_id: UUID | None) -> None:
        """Scope subsequent searches to a single user."""
        self._user_id = user_id

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "search_pks":
            raise ValueError(f"Unknown pks_search tool: {tool_name}")
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("search_pks requer 'query' não-vazia.")
        if not self._user_id:
            raise ValueError("search_pks requer um usuário identificado (user_id ausente).")

        # Pergunta de dinheiro não se responde pela memória: o PKS não guarda extrato
        # (ledger_guard), então aqui ele ou não acha nada — e o modelo chuta — ou acha
        # um resto velho. Devolver o redirecionamento é mais útil que devolver vazio.
        if looks_like_money_query(query):
            logger.info("[pks_search] money query → redirect para a skill financas: %r", query)
            return {
                "query": query,
                "found": False,
                "count": 0,
                "results": [],
                "redirect": LEDGER_REDIRECT,
            }

        rows = await self.pks.search_entities(user_id=self._user_id, query=query)
        results = [
            {
                "name": r.get("name"),
                "entity_type": r.get("entity_type"),
                "properties": r.get("properties"),
                # As ligações (os exercícios de um treino, os membros de um plano).
                # Sem elas a ferramenta negava o que estava no prompt.
                "relations": r.get("relations") or [],
                "score": r.get("score"),
                "matched_by": r.get("matched_by"),
            }
            for r in rows[:_MAX_RESULTS]
        ]
        return {
            "query": query,
            "found": len(results) > 0,
            "count": len(results),
            "results": results,
        }
