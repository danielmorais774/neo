"""Executor da tool pks_remember — escrita sob demanda no PKS.

Interface FINA: delega a decisão de COMO/ONDE persistir ao subagente de memória
(PKSMemoryPlanner, via PKSService.learn em modo confirmado). O cognitivo já
decidiu O QUÊ e passou o conteúdo formulado; aqui só orquestramos a chamada
síncrona e devolvemos um echo do que foi realmente salvo, pro modelo confirmar
citando o conteúdo real (nunca um "salvei" cego).

Todas as escritas caem no `user` scope — NUNCA no `system` scope (personality/
perfil), que continua exigindo o fluxo de treino explícito.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)

# Teto de tamanho do conteúdo — a tool é para um fato, não para colar um
# documento inteiro na memória semântica.
_MAX_CONTENT_CHARS = 2_000


class PKSWriteExecutor:
    """Executa pks_remember (contrato igual aos demais module executors)."""

    def __init__(self, *, pks_service: Any) -> None:
        self._pks = pks_service
        self._user_id: UUID | None = None

    def set_context(self, user_id: UUID | None) -> None:
        self._user_id = user_id

    # -- contrato AgentExecutor ------------------------------------------------
    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        try:
            result = await self.execute(tool_call.name, tool_call.arguments)
            return ToolResult(
                tool_call_id=tool_call.id, name=tool_call.name, result=result,
                execution_time=asyncio.get_event_loop().time() - start,
            )
        except Exception as e:
            logger.warning("pks_write tool '%s' failed: %s", tool_call.name, e)
            return ToolResult(
                tool_call_id=tool_call.id, name=tool_call.name, result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self._user_id is None:
            raise ValueError(f"{tool_name} exige um usuário identificado nesta sessão.")
        if tool_name == "pks_remember":
            return await self._remember(arguments)
        if tool_name == "pks_forget":
            return await self._forget(arguments)
        raise ValueError(f"Tool desconhecida no módulo pks_write: {tool_name}")

    async def _remember(self, arguments: dict[str, Any]) -> str:
        content = str(arguments.get("content") or "").strip()
        if not content:
            raise ValueError("content é obrigatório (o fato a lembrar, por extenso).")
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS]
        note = str(arguments.get("note") or "").strip()

        # Contexto PKS atual: ajuda o planner a resolver entidade canônica e não
        # duplicar. Best-effort — se falhar, segue sem contexto (o planner cria).
        pks_context = await self._retrieve_context(content)

        # Mensagem sintética só para deixar claro ao planner que é confirmação;
        # o que manda de verdade é confirmed_save=True (modo confirmado), que faz
        # o planner ESTRUTURAR sem re-julgar se salva.
        user_message = f"salva isto na memória: {note}" if note else "salva isto na memória"

        applied = await self._pks.learn(
            user_id=self._user_id,
            user_message=user_message,
            assistant_response=content,
            memory_target="user",
            pks_context=pks_context,
            confirmed_save=True,
        )
        return _format_echo(content, applied)

    async def _forget(self, arguments: dict[str, Any]) -> str:
        target = str(arguments.get("target") or "").strip()
        if not target:
            raise ValueError("target é obrigatório (o que esquecer, por extenso).")
        if len(target) > _MAX_CONTENT_CHARS:
            target = target[:_MAX_CONTENT_CHARS]

        # O contexto PKS é ESSENCIAL no forget: o planner só apaga entidade que
        # aparece aqui (soft_delete resolve por nome/type contra o que existe).
        pks_context = await self._retrieve_context(target)

        applied = await self._pks.learn(
            user_id=self._user_id,
            user_message=f"esquece isto da memória: {target}",
            assistant_response=target,
            memory_target="user",
            pks_context=pks_context,
            forget=True,
        )
        return _format_forget_echo(target, applied)

    async def _retrieve_context(self, query: str) -> str:
        try:
            _result, pks_context = await self._pks.retrieve_context(
                user_id=self._user_id, keywords=None, user_message=query,
            )
            return pks_context
        except Exception as exc:  # noqa: BLE001 — contexto é opcional no save
            logger.info("[pks_write] retrieve_context indisponível: %s", exc)
            return ""


def _format_echo(content: str, applied: list[dict[str, Any]] | None) -> str:
    """Resumo pt-BR do que foi salvo, para o modelo confirmar citando o real.

    Se o planner não produziu operação nenhuma, dizemos isso claramente — a tool
    NUNCA finge sucesso (o modelo então não deve afirmar que salvou).
    """
    ops = applied or []
    if not ops:
        return (
            "NÃO foi possível estruturar isso na memória (nenhuma operação gerada). "
            "Não afirme que salvou; peça ao usuário para reformular o que quer guardar."
        )

    entidades: list[str] = []
    campos = 0
    relacoes = 0
    for op in ops:
        kind = op.get("op")
        if kind == "upsert_entity":
            nome = op.get("name")
            tipo = op.get("type")
            if nome:
                entidades.append(f"{nome} ({tipo})" if tipo else str(nome))
        elif kind == "upsert_field":
            campos += 1
        elif kind == "upsert_relation":
            relacoes += 1

    partes: list[str] = [f"Salvei na sua memória ({len(ops)} operação(ões))."]
    if entidades:
        partes.append("Entidades: " + ", ".join(entidades[:6]) + ".")
    if campos:
        partes.append(f"{campos} campo(s).")
    if relacoes:
        partes.append(f"{relacoes} relação(ões).")
    partes.append(f"Conteúdo: {content[:280]}")
    return " ".join(partes)


def _format_forget_echo(target: str, applied: list[dict[str, Any]] | None) -> str:
    """Resumo pt-BR do que foi apagado. Se nada casou, diz claramente — a tool
    NUNCA finge ter apagado (o modelo então não deve afirmar que esqueceu)."""
    removed = [
        op.get("name")
        for op in (applied or [])
        if op.get("op") == "soft_delete_entity" and op.get("name")
    ]
    if not removed:
        return (
            f"NÃO encontrei nada na memória que corresponda a \"{target[:120]}\" — "
            "nada foi apagado. Não afirme que esqueceu; confirme com o usuário o "
            "que exatamente ele quer remover."
        )
    return "Removi da sua memória: " + ", ".join(str(r) for r in removed[:6]) + "."
