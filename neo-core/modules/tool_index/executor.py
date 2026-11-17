"""Executor do agregador — ``tool_find`` (busca) e ``tool_run`` (dispatcher).

``tool_run`` recebe um ``dispatcher`` injetado em vez de falar direto com o
:class:`~core.tools.executor.ToolExecutor` global. É isso que faz a chamada
respeitar o mesmo caminho de sempre: no cognitivo o dispatcher é o
``CognitiveReadExecutor`` (executores frescos e user-scoped), no executor global
é o próprio ToolExecutor. Sem isso, ``tool_run("fin_balances")`` cairia no
executor global — que roda SEM usuário — e o dado voltaria vazio.

Duas travas, ambas por construção:

- **só ``server-side``**: tool client-side (``run_command``, ``save_generated_file``)
  não executa in-process — ela levanta ``ClientActuationRequested`` no loop
  cognitivo. Passá-la por aqui quebraria esse fluxo silenciosamente.
- **sem recursão**: ``tool_run`` não chama ``tool_run`` nem ``tool_find``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from core.tools.aggregator import ToolAggregator, get_aggregator
from core.tools.validator import ToolValidator
from shared import ToolCall

logger = logging.getLogger(__name__)

_SELF_TOOLS = frozenset({"tool_run", "tool_find"})


class ToolIndexExecutor:
    """Serve ``tool_find``/``tool_run`` contra o agregador do processo."""

    def __init__(
        self,
        *,
        dispatcher: Callable[[ToolCall], Awaitable[Any]] | None = None,
        aggregator: ToolAggregator | None = None,
    ) -> None:
        self._aggregator = aggregator or get_aggregator()
        self._dispatcher = dispatcher

    def set_dispatcher(self, dispatcher: Callable[[ToolCall], Awaitable[Any]]) -> None:
        self._dispatcher = dispatcher

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        args = arguments or {}
        if tool_name == "tool_find":
            return self._find(str(args.get("busca") or ""))
        if tool_name == "tool_run":
            return await self._run(args)
        raise ValueError(f"Tool desconhecida no módulo tool_index: {tool_name}")

    # ------------------------------------------------------------------

    def _find(self, query: str) -> dict[str, Any]:
        hits = [b for b in self._aggregator.search(query, limit=12) if b.name not in _SELF_TOOLS]
        return {
            "busca": query,
            "encontradas": len(hits),
            "ferramentas": [
                {
                    "nome": b.name,
                    "assinatura": b.signature(),
                    "resumo": b.summary,
                    "origem": b.origin,
                    "skill": b.owner_skill,
                    "executavel_por_tool_run": b.schema.execution == "server-side",
                }
                for b in hits
            ],
            "nota": (
                "Se a ferramenta já aparece na sua lista de funções, chame-a direto — "
                "tool_run é só para o que não está disponível nesta conversa."
            ) if hits else "Nada encontrado com esse termo. Não invente nome de ferramenta.",
        }

    async def _run(self, args: dict[str, Any]) -> Any:
        name = str(args.get("nome") or "").strip()
        if not name:
            raise ValueError("tool_run precisa do 'nome' da ferramenta.")
        if name in _SELF_TOOLS:
            raise ValueError("tool_run não chama a si mesma nem tool_find.")

        binding = self._aggregator.get(name)
        if binding is None:
            similares = [b.name for b in self._aggregator.search(name, limit=5)]
            dica = f" Parecidas: {', '.join(similares)}." if similares else ""
            raise ValueError(f"Ferramenta '{name}' não existe no índice.{dica}")

        if binding.schema.execution != "server-side":
            raise ValueError(
                f"'{name}' roda no aparelho do usuário e não pode ser executada por "
                "tool_run. Chame-a diretamente."
            )
        if self._dispatcher is None:
            raise ValueError("tool_run indisponível: nenhum despachante configurado.")

        payload = args.get("args") or {}
        if not isinstance(payload, dict):
            raise ValueError(f"'args' precisa ser um objeto. Assinatura: {binding.signature()}")

        # Valida ANTES de despachar: a mensagem de erro carrega a assinatura
        # inteira, que é o que falta ao modelo quando ele chuta argumento.
        try:
            ToolValidator.validate_arguments(binding.schema, payload)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Argumentos inválidos para '{name}': {exc}. "
                f"Assinatura: {binding.signature()}."
            ) from exc

        logger.info("[tool_run] %s(%s)", name, ", ".join(sorted(payload)))
        result = await self._dispatcher(ToolCall(name=name, arguments=payload))
        # O dispatcher pode devolver ToolResult (contrato do AgentExecutor) ou o
        # dict cru — os dois contratos que já convivem no ToolExecutor._dispatch.
        error = getattr(result, "error", None)
        if error:
            raise ValueError(str(error))
        return {"ferramenta": name, "resultado": getattr(result, "result", result)}
