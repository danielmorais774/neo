"""Portão para o executor que não o chama por dentro (release-request-plan §5.2).

O portão nasceu DENTRO de dois executores (``ToolExecutor`` e
``CognitiveReadExecutor``), e o desenho supõe que todo tool call passa por um
dos dois. Medido em 2026-10-03: não passa. As tools de job, as da agenda e os
pipelines de skill (WhatsApp, Home Assistant) têm executor PRÓPRIO, por request,
e chegam ao modelo pelos mesmos turnos. O que isso custava, em concreto:

- ``deep_read``, ``deep_research``, ``browser_task``, ``browser_local_task`` e
  ``browser_screenshot`` são ``fonte_externa`` e **não contaminavam o turno** —
  quem registra contaminação é o ``note_tool_result`` dos dois executores
  gated. Ler um PDF de terceiro e depois mandar e-mail passava como turno limpo,
  que é exatamente o ataque que o plano existe para barrar.
- a única regra ``negar`` do portão (agendar *prompt* a partir de texto de
  terceiro, ``gate.py``) era **código morto**: ``schedule_create`` sai pelo
  ``ScheduleToolExecutor``.

Repetir a chamada do portão em cada executor novo é como a divergência nasceu —
são 5 executores hoje e o 6º nasceria sem. Aqui ele é um INVÓLUCRO: envolve o
executor e devolve a recusa como ``error`` da tool, no formato que o modelo já
recebe (a recusa ENSINA o próximo passo; ver ``GateDecision.mensagem``).

Envolver um executor que já gateia por dentro é inofensivo: o portão deduplica
por ``tool_call.id`` (sempre presente — ``ToolCall.id`` tem default), então a
mesma chamada não conta duas vezes no relatório da sombra nem consome a
liberação duas vezes. ``tests/authority/test_portao_em_todo_executor.py`` varre
o fonte e falha quando aparece executor de tool fora do portão.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.authority.gate import gate_tool_call
from core.authority.turn import note_tool_result
from shared import ToolResult

logger = logging.getLogger(__name__)


class ExecutorComPortao:
    """Executor envolvido pelo portão de autoridade do turno."""

    def __init__(self, inner: Any, *, rotulo: str = "") -> None:
        self._inner = inner
        self._rotulo = rotulo or type(inner).__name__

    def __getattr__(self, nome: str) -> Any:
        """``set_context`` & cia. seguem alcançáveis — só a execução é interceptada."""
        return getattr(self._inner, nome)

    async def execute_tool_call(self, tool_call: Any) -> Any:
        inicio = asyncio.get_event_loop().time()
        nome = getattr(tool_call, "name", "") or ""
        call_id = getattr(tool_call, "id", None)
        decisao = await gate_tool_call(nome, getattr(tool_call, "arguments", None), call_id)
        if decisao.blocked:
            logger.warning(
                "[authority] %s barrada antes do executor %s — %s",
                nome, self._rotulo, decisao.motivo,
            )
            return ToolResult(
                tool_call_id=str(call_id or ""),
                name=nome,
                result=None,
                error=decisao.mensagem(),
                execution_time=asyncio.get_event_loop().time() - inicio,
            )
        resultado = await self._inner.execute_tool_call(tool_call)
        # Procedência + contaminação. É esta linha que faltava nos executores de
        # job: sem ela o `deep_read` de um PDF de terceiro deixava o turno limpo.
        note_tool_result(nome, getattr(resultado, "result", None))
        return resultado


def com_portao(executor: Any, *, rotulo: str = "") -> Any:
    """Envolve ``executor`` no portão. Idempotente e tolerante a ``None``."""
    if executor is None or isinstance(executor, ExecutorComPortao):
        return executor
    return ExecutorComPortao(executor, rotulo=rotulo)
