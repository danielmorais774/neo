"""ScheduleToolExecutor — executa as ``schedule_*`` com o usuário do turno.

Por request, como o :class:`JobToolExecutor`: sem ``user_id`` uma agenda não tem
dono, e uma tarefa sem dono é uma tarefa que dispara para ninguém (ou, pior,
para todo mundo). É o mesmo motivo pelo qual estas tools não são roteadas
direto pelo classificador.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from core.scheduler.service import ScheduleValidationError
from core.scheduler.timing import ScheduleError, to_local
from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class ScheduleToolExecutor:
    """Executor por-request das tools de agenda."""

    def __init__(self, *, service: Any, user_id: UUID | None, session_id: UUID | None) -> None:
        self.service = service
        self.user_id = user_id
        self.session_id = session_id

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        inicio = time.monotonic()
        args = tool_call.arguments or {}
        try:
            resultado = await self._dispatch(tool_call.name, args)
            erro = None
        except (ScheduleValidationError, ScheduleError) as exc:
            # Erro de forma é resposta, não exceção: o cérebro consegue corrigir
            # a frase e tentar de novo no mesmo turno.
            resultado, erro = {"ok": False, "error": str(exc)}, None
        except Exception as exc:  # noqa: BLE001
            logger.exception("[agenda] tool %s falhou", tool_call.name)
            resultado, erro = None, str(exc)
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            result=resultado,
            error=erro,
            execution_time=round(time.monotonic() - inicio, 3),
        )

    # ──────────────────────────────────────────────────────────────────

    async def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "schedule_create":
            return await self._create(args)
        if name == "schedule_list":
            return await self._list(args)
        if name == "schedule_update":
            return await self._update(args)
        if name in ("schedule_pause", "schedule_resume"):
            return await self._toggle(args, enabled=(name == "schedule_resume"))
        if name == "schedule_cancel":
            return await self._cancel(args)
        if name == "schedule_run_now":
            return await self._run_now(args)
        return {"ok": False, "error": f"tool de agenda desconhecida: {name}"}

    async def _create(self, args: dict[str, Any]) -> dict:
        if self.user_id is None:
            return {"ok": False, "error": "não dá para agendar sem um usuário identificado."}
        acao_tipo = str(args.get("acao_tipo") or "").strip()
        acao: dict[str, Any] = {}
        if acao_tipo == "prompt":
            acao = {"texto": args.get("texto") or ""}
        elif acao_tipo == "tool":
            acao = {"tool": args.get("tool") or "", "arguments": args.get("arguments") or {}}
        elif acao_tipo == "notify":
            acao = {"titulo": args.get("titulo") or "", "corpo": args.get("corpo") or ""}
        elif acao_tipo == "job":
            acao = {"kind": args.get("job_kind") or "", "spec": args.get("job_spec") or {}}

        tarefa = await self.service.create(
            {
                "name": args.get("nome"),
                "quando": args.get("quando"),
                "action_kind": acao_tipo,
                "action_json": acao,
                "notify_policy": args.get("canal") or "chat",
                "session_mode": args.get("sessao") or "nova",
                "only_if_changed": bool(args.get("so_se_mudar")),
            },
            user_id=self.user_id,
        )
        return {
            "ok": True,
            "id": str(tarefa.id),
            "nome": tarefa.name,
            "quando": self.service.to_dict(tarefa)["descricao_horario"],
            "proximo_local": _proximo(tarefa),
            "nota": (
                "Agendado. CONFIRME o horário para o usuário usando 'proximo_local' — "
                "é assim que ele corrige um mal-entendido antes de a hora chegar."
            ),
        }

    async def _list(self, args: dict[str, Any]) -> dict:
        tarefas = await self.service.list(self.user_id, enabled_only=bool(args.get("ativas_apenas")))
        return {
            "ok": True,
            "total": len(tarefas),
            "tarefas": [
                {
                    "id": str(t.id),
                    "nome": t.name,
                    "ligada": t.enabled,
                    "quando": self.service.to_dict(t)["descricao_horario"],
                    "proximo": _proximo(t),
                    "acao": t.action_kind,
                    "ultimo_status": t.last_status or "—",
                    "pausada_porque": t.paused_reason or "",
                }
                for t in tarefas
            ],
        }

    async def _update(self, args: dict[str, Any]) -> dict:
        alvo = await self._resolver(args.get("id"))
        if alvo is None:
            return {"ok": False, "error": "tarefa não encontrada."}
        payload: dict[str, Any] = {}
        if args.get("quando"):
            payload["quando"] = args["quando"]
        if args.get("nome"):
            payload["name"] = args["nome"]
        if args.get("canal"):
            payload["notify_policy"] = args["canal"]
        if args.get("texto"):
            acao = dict(alvo.action_json or {})
            if alvo.action_kind == "prompt":
                acao["texto"] = args["texto"]
            elif alvo.action_kind == "notify":
                acao["corpo"] = args["texto"]
            payload["action_json"] = acao
        if not payload:
            return {"ok": False, "error": "nada para mudar."}
        tarefa = await self.service.update(alvo.id, payload)
        return {
            "ok": True,
            "id": str(tarefa.id),
            "quando": self.service.to_dict(tarefa)["descricao_horario"],
            "proximo_local": _proximo(tarefa),
        }

    async def _toggle(self, args: dict[str, Any], *, enabled: bool) -> dict:
        alvo = await self._resolver(args.get("id"))
        if alvo is None:
            return {"ok": False, "error": "tarefa não encontrada."}
        tarefa = await self.service.set_enabled(alvo.id, enabled)
        return {
            "ok": True,
            "id": str(tarefa.id),
            "ligada": tarefa.enabled,
            "proximo_local": _proximo(tarefa),
        }

    async def _cancel(self, args: dict[str, Any]) -> dict:
        alvo = await self._resolver(args.get("id"))
        if alvo is None:
            return {"ok": False, "error": "tarefa não encontrada."}
        apagou = await self.service.delete(alvo.id)
        return {"ok": apagou, "id": str(alvo.id), "nome": alvo.name}

    async def _run_now(self, args: dict[str, Any]) -> dict:
        alvo = await self._resolver(args.get("id"))
        if alvo is None:
            return {"ok": False, "error": "tarefa não encontrada."}
        job = await self.service.materialize(alvo, reason="run_now")
        return {
            "ok": True,
            "job_id": str(job.id),
            "nota": (
                "Disparei agora, fora de hora. O horário normal continua o mesmo. "
                "O resultado chega pelo canal da tarefa quando terminar."
            ),
        }

    async def _resolver(self, valor: Any) -> Any:
        """Aceita id — e também o NOME, que é o que o modelo tende a mandar.

        Mesmo defeito já visto no ``file_id``: o classificador põe o nome no
        campo do id. Resolver pelo nome aqui custa uma query e evita um "tarefa
        não encontrada" que o usuário leria como bug.
        """
        bruto = str(valor or "").strip()
        if not bruto:
            return None
        try:
            return await self.service.get(UUID(bruto))
        except (ValueError, AttributeError, TypeError):
            pass
        tarefas = await self.service.list(self.user_id)
        alvo = bruto.lower()
        exatos = [t for t in tarefas if t.name.lower() == alvo]
        if exatos:
            return exatos[0]
        parciais = [t for t in tarefas if alvo in t.name.lower()]
        return parciais[0] if len(parciais) == 1 else None


def _proximo(tarefa: Any) -> str | None:
    if not tarefa or not tarefa.next_run_at:
        return None
    return to_local(tarefa.next_run_at, tarefa.timezone).strftime("%d/%m/%Y às %H:%M")
