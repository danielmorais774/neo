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
        if tool_name == "pks_reveal":
            return await self._reveal(arguments)
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

    async def _reveal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """``pks_reveal`` — UM campo, UMA entidade, valor em claro.

        A redação (F2, docs/release-request-plan.md §7) tira identificação do
        prompt; esta é a única porta de volta. Ela é estreita de propósito:

        - devolve **um** campo, nunca a entidade inteira — pedir o CEP não pode
          entregar o endereço junto;
        - **não** revela credencial (nível ``secret``): o lugar dela é o cofre,
          e nenhuma tarefa legítima precisa da senha no contexto do modelo;
        - o portão (``core/authority``) é que decide se a chamada acontece num
          turno contaminado — aqui não se repete essa regra, senão viram duas.
        """
        from core.pks.sensitivity import SECRET, effective_level

        entidade = str(arguments.get("entidade") or "").strip()
        campo = str(arguments.get("campo") or "").strip().strip("«»").removeprefix("pks:")
        if not entidade or not campo:
            raise ValueError("pks_reveal requer 'entidade' e 'campo'.")
        if not self._user_id:
            raise ValueError("pks_reveal requer um usuário identificado (user_id ausente).")

        rows = await self.pks.search_entities(
            user_id=self._user_id, query=entidade, redact=False
        )
        alvo = (entidade or "").casefold()
        candidatas = [r for r in rows if (r.get("name") or "").casefold() == alvo] or rows
        for row in candidatas:
            props = row.get("properties") or {}
            for chave, valor in props.items():
                if chave.casefold() != campo.casefold():
                    continue
                if effective_level(chave) == SECRET:
                    logger.warning("[pks_reveal] recusado: %s é credencial", chave)
                    return {
                        "found": False,
                        "campo": chave,
                        "motivo": (
                            "Credencial não sai da memória para o contexto. Se for um "
                            "login, o lugar dele é o cofre (vault_list)."
                        ),
                    }
                logger.info(
                    "[pks_reveal] %s.%s revelado para o turno", row.get("name"), chave
                )
                return {
                    "found": True,
                    "entidade": row.get("name"),
                    "campo": chave,
                    "valor": valor,
                }
        return {
            "found": False,
            "entidade": entidade,
            "campo": campo,
            "motivo": "Não achei esse campo nessa entidade. Confira o nome com search_pks.",
        }
