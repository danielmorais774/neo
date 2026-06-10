"""PKS search executor — active verification reads against the user's PKS.

Standard module-executor contract (``execute(tool_name, arguments)``) with a
per-request ``set_context(user_id)``, mirroring ``modules/devices``. Delegates to
``PKSService.search_entities`` and compacts the rows for the LLM.
"""

import logging
from typing import Any
from uuid import UUID

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

        rows = await self.pks.search_entities(user_id=self._user_id, query=query)
        results = [
            {
                "name": r.get("name"),
                "entity_type": r.get("entity_type"),
                "properties": r.get("properties"),
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
