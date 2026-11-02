"""Executor da ``solicitar_dado`` — cria o pedido com a origem REAL do turno.

O que o modelo passa: campos, destino, finalidade, texto. O que ele **não**
passa, e não poderia: de onde veio a intenção. Isso sai da
:class:`~core.authority.turn.TurnAuthority`, que só código escreve — senão a
própria injeção assinaria o pedido como "pedido do Daniel".
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.release import ReleaseError

logger = logging.getLogger(__name__)

#: Teto de campos por pedido. Pedir seis coisas de uma vez é o oposto do
#: "peça o mínimo" — e um card com seis itens é um card que ninguém lê.
MAX_CAMPOS = 4


class ReleaseToolExecutor:
    """Contrato ``execute(nome, args)`` — o dialeto que o ToolExecutor chama."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._user_id: UUID | None = None
        self._session_id: UUID | None = None

    def set_context(self, user_id: UUID | None, session_id: UUID | None = None) -> None:
        self._user_id = user_id
        self._session_id = session_id

    async def execute_tool_call(self, tool_call: Any) -> Any:
        """O dialeto do agente do SERVER_TOOL (``ToolCall`` → ``ToolResult``).

        Ali não existe o despacho inline do cognitivo, e a tool faltava por isso:
        na sessão 1a605544 a recusa mandava "chame solicitar_dado", o agente não
        a tinha, e respondeu "ficou esperando aprovação" sem pedido nenhum
        criado.
        """
        import time

        from shared import ToolResult

        inicio = time.monotonic()
        try:
            resultado = await self.execute(tool_call.name, tool_call.arguments or {})
        except Exception as exc:  # noqa: BLE001 — erro volta como dado para o agente
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=None,
                error=str(exc),
                execution_time=time.monotonic() - inicio,
            )
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            result=resultado,
            execution_time=time.monotonic() - inicio,
        )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "solicitar_dado":
            raise ValueError(f"Unknown release tool: {tool_name}")
        if not self._user_id:
            raise ValueError("solicitar_dado requer um usuário identificado.")

        campos_brutos = arguments.get("campos") or []
        if isinstance(campos_brutos, str):
            campos_brutos = [campos_brutos]
        campos = [
            str(c).strip().strip("«»").removeprefix("pks:")
            for c in campos_brutos
            if str(c).strip()
        ]
        if not campos:
            raise ValueError("solicitar_dado requer 'campos' (qual dado sai).")
        if len(campos) > MAX_CAMPOS:
            raise ValueError(
                f"peça no máximo {MAX_CAMPOS} campos por pedido — divida, ou peça só o "
                "que a tarefa precisa agora."
            )

        destino = str(arguments.get("destino") or "").strip()
        destino_tipo = str(arguments.get("destino_tipo") or "").strip().lower()
        finalidade = str(arguments.get("finalidade") or "").strip()
        texto = arguments.get("texto")

        # Tabela de necessidade (F4): campo que a finalidade não justifica nem
        # chega a virar card. É o que responde ao caso "a página pediu CPF para
        # calcular o frete" — com tabela, não com julgamento.
        from core.release.necessity import explicar

        recusa = explicar(finalidade, campos)
        if recusa:
            logger.warning("[solicitar_dado] pedido desnecessário recusado: %s", recusa)
            return {"ok": False, "erro": "campo_desnecessario", "mensagem": recusa}

        origem, origem_ref, motivos, evidencia = self._origem()

        try:
            vista = await self._service.create(
                user_id=self._user_id,
                session_id=self._session_id,
                campos=[f"pks:{c}" for c in campos],
                destino=destino,
                destino_tipo=destino_tipo,
                finalidade=finalidade,
                origem=origem,
                origem_ref=origem_ref,
                evidencia=evidencia,
                tool=("mensagem" if texto else None),
                payload=texto,
                preview=str(texto or ""),
                risco_motivos=motivos,
            )
        except ReleaseError as exc:
            # Erro de negócio volta em português para o turno, não como stack.
            return {"ok": False, "erro": exc.code, "mensagem": exc.message}

        logger.info(
            "[solicitar_dado] pedido %s (%s → %s)", vista.id, ",".join(campos), destino
        )
        return {
            "ok": True,
            "status": vista.status,
            "pedido_id": vista.id,
            "campos": vista.campos,
            "destino": vista.destino,
            "expira_em": vista.expira_em,
            "mensagem": (
                "Pedido de liberação registrado. O Daniel recebeu o aviso nos "
                "aparelhos dele e só ele pode aprovar. Encerre o turno dizendo o que "
                "ficou pendente — não espere aqui e não repita este pedido."
            ),
        }

    # ── origem do turno ──────────────────────────────────────────────────

    def _origem(self) -> tuple[str, str | None, list[str], str]:
        """``(origem, ref, motivos, evidência)`` a partir da autoridade do turno."""
        try:
            from core.authority import current_authority

            authority = current_authority()
        except Exception:  # noqa: BLE001
            authority = None
        if authority is None:
            return "admin", None, [], ""

        from core.release import motivos_de_origem

        motivos = motivos_de_origem(
            authority.principal,
            authority.principal_ref,
            list(authority.taint_sources or []),
            tainted=bool(authority.tainted),
        )
        return authority.principal, authority.principal_ref, motivos, ""
