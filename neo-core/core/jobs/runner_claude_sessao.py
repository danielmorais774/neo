"""Runner de jobs ``kind="claude_sessao"`` — a volta de um pedido entregue numa
sessão do Claude que o DANIEL usa (docs/ambiente-de-trabalho-plan.md §9.4).

O pedido já foi entregue no turno, pelo ``claude_enviar``. Aqui só se espera a
sessão terminar e se traz a resposta dela ao chat. Quem termina é a sessão, não
o Neo: o sinal é o registro do Claude Code no Mac (``claude.situacao``), que passa
de ``busy`` a ``idle`` com um horário depois da entrega.

A resposta vai LITERAL (``entrega["literal"]``), como a do ``claude_task``: o que a
sessão escreveu é para o Daniel ler, e o redator a comprimia (job 8f83b37c).

Nada aqui roda no core além de perguntar ao Mac, e o job sobrevive a reinício
(``REATTACHABLE_JOB_KINDS``): depois de um ``.py`` salvo ele volta para a fila e
retoma a espera com o mesmo ``enviado_em``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from db.models.agent_job import AgentJob

logger = logging.getLogger(__name__)


def _terminal(status: str, fecho: str, resumo: str, spec: dict[str, Any], situacao: dict[str, Any]) -> dict:
    return {
        "status": status,
        "result_summary": resumo[:6000],
        "extra_metadata": {
            "claude_sessao": {
                "sessao": spec.get("sessao"),
                "titulo": spec.get("titulo"),
                "status": situacao.get("status"),
                "viva": situacao.get("viva"),
                "respostas": situacao.get("respostas"),
            },
            "entrega": {"fecho": fecho, "itens": [], "literal": True},
        },
    }


def build_claude_sessao_runner(
    *,
    get_broadcaster: Callable[[], Any] | None = None,
    dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    relogio: Callable[[], float] = time.monotonic,
) -> Callable[[AgentJob], Awaitable[dict]]:
    """``get_broadcaster``, ``dormir`` e ``relogio`` existem para os testes."""

    def _broadcaster() -> Any:
        if get_broadcaster is not None:
            return get_broadcaster()
        from core.notifications.device_control import get_device_control_broadcaster

        return get_device_control_broadcaster()

    async def run_claude_sessao_job(job: AgentJob) -> dict:
        from core.notifications.device_control import DeviceOffline, DeviceTimeout
        from shared.ajustes import ajustes

        spec = dict(job.spec_json or {})
        device_id = str(spec.get("device_id") or "").strip()
        sessao = str(spec.get("sessao") or "").strip()
        titulo = str(spec.get("titulo") or sessao[:8])
        enviado_em = int(spec.get("enviado_em") or 0)
        if not device_id or not sessao:
            return {"status": "failed", "result_summary": "Pedido sem a sessão ou o Mac de destino."}

        inicio = relogio()
        fora_desde: float | None = None
        situacao: dict[str, Any] = {}
        while True:
            t = ajustes().trabalho
            agora = relogio()
            if agora - inicio > t.retorno_timeout_min * 60:
                return _terminal(
                    "completed",
                    f"A sessão do Claude “{titulo}” ainda está trabalhando no seu pedido. Parei de "
                    f"acompanhar depois de {t.retorno_timeout_min} min: a resposta fica lá nela.",
                    f"Sessão “{titulo}” ainda ocupada após {t.retorno_timeout_min} min.",
                    spec,
                    situacao,
                )
            try:
                r = await _broadcaster().request(
                    device_id,
                    "claude.situacao",
                    {"sessionId": sessao, "desde_ms": enviado_em, "resposta_max": t.retorno_resposta_max},
                    timeout=30,
                )
            except (DeviceOffline, DeviceTimeout) as exc:
                fora_desde = fora_desde if fora_desde is not None else agora
                if agora - fora_desde > t.fora_do_ar_min * 60:
                    return _terminal(
                        "failed",
                        f"Perdi o contato com o Mac enquanto a sessão do Claude “{titulo}” trabalhava "
                        "no seu pedido. A resposta, se veio, está lá nela.",
                        f"Mac fora do ar por mais de {t.fora_do_ar_min} min: {exc}",
                        spec,
                        situacao,
                    )
                await dormir(t.retorno_intervalo_s)
                continue
            fora_desde = None
            if not r.get("ok"):
                logger.warning("[claude-sessao] %s: claude.situacao falhou: %s", job.id, r.get("error"))
                await dormir(t.retorno_intervalo_s)
                continue
            situacao = r.get("result") or {}
            resposta = str(situacao.get("resposta") or "").strip()

            if not situacao.get("viva"):
                # Fechou no meio (o Daniel fechou a aba, ou o processo caiu): o que
                # ela escreveu até ali ainda é a melhor resposta que existe.
                fecho = f"A sessão do Claude “{titulo}” foi fechada antes de terminar o seu pedido."
                if resposta:
                    fecho += f" O que ela respondeu até ali:\n\n{resposta}"
                return _terminal("completed", fecho, f"Sessão “{titulo}” fechou. {resposta[:500]}", spec, situacao)

            terminou = situacao.get("status") == "idle" and int(situacao.get("status_desde_ms") or 0) > enviado_em
            if terminou:
                if resposta:
                    fecho = f"A sessão do Claude “{titulo}” terminou:\n\n{resposta}"
                else:
                    fecho = (
                        f"A sessão do Claude “{titulo}” terminou o seu pedido, mas não escreveu "
                        "resposta em texto. Dê uma olhada nela."
                    )
                return _terminal("completed", fecho, f"Sessão “{titulo}” terminou. {resposta[:1500]}", spec, situacao)

            await dormir(t.retorno_intervalo_s)

    return run_claude_sessao_job
