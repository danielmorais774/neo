"""Acompanha, do servidor, um comando que virou TAREFA no computador do usuário.

Até a session-1bbdd7c5 todo ``run_command`` no Mac tinha 120 s de vida: o
``zip -r`` de 5,6 GB morreu no meio, com 2,6 GB escritos, e o turno caiu junto. O
desktop (``services/local_jobs.rs``) agora adota o comando que passa do orçamento de
primeiro plano ainda trabalhando: o processo continua lá, com a saída num log local,
e o ToolResult volta na hora com ``{"neo_tarefa_local": {...}}``.

Daqui para a frente é um ``agent_job`` comum (``kind="comando_local"``): aparece na
janela Tarefas, cancela pelo mesmo botão, sobrevive a reload do core
(``REATTACHABLE_JOB_KINDS``) e, ao terminar, o dispatcher avisa no chat.

**O que este runner pode fazer no computador: consultar e parar.** Ele fala com o
desktop pelo canal ``/ws/devices/control`` (``tarefa.status``/``tarefa.stop``), e o
desktop não tem ação que INICIE tarefa: só vira tarefa o comando que já passou pela
política ou pelo card dentro de um turno. Um core comprometido não ganha execução
remota nova por aqui.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from db.models.agent_job import AgentJob

logger = logging.getLogger(__name__)

KIND = "comando_local"
#: Rabo da saída pedido a cada conferência e no fim.
_TAIL_BYTES = 16 * 1024
#: Quanto do rabo vai para o `progress_note` (a linha que a janela Tarefas mostra).
_PROGRESSO_MAX = 400
#: Espera da resposta do computador a uma conferência.
_PEDIDO_TIMEOUT_S = 20.0


def tarefa_do_resultado(result: str | None) -> dict[str, Any] | None:
    """O ``{"neo_tarefa_local": {...}}`` que o desktop devolve quando o comando vira tarefa."""
    if not result or "neo_tarefa_local" not in result:
        return None
    try:
        dados = json.loads(result)
    except (TypeError, ValueError):
        return None
    tarefa = dados.get("neo_tarefa_local") if isinstance(dados, dict) else None
    if not isinstance(tarefa, dict) or not str(tarefa.get("id") or "").strip():
        return None
    return tarefa


def duracao(segundos: float | int | None) -> str:
    """``95`` → ``"1 min 35 s"``."""
    s = int(segundos or 0)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m} min {r} s" if r else f"{m} min"
    h, r = divmod(s, 3600)
    m = r // 60
    return f"{h} h {m} min" if m else f"{h} h"


def titulo(command: str) -> str:
    comando = " ".join(str(command or "").split())
    if len(comando) > 90:
        comando = comando[:87] + "…"
    return f"Comando no computador: {comando}"


def _ultimas_linhas(texto: str, n: int = 3) -> str:
    linhas = [l.rstrip() for l in str(texto or "").splitlines() if l.strip()]
    fim = " · ".join(linhas[-n:])
    return fim[-_PROGRESSO_MAX:]


def progresso(estado: dict[str, Any]) -> str:
    """A linha de progresso da janela Tarefas: há quanto tempo, e a saída mais recente."""
    partes = [f"rodando há {duracao(estado.get('elapsed_s'))}"]
    fim = _ultimas_linhas(estado.get("tail") or "")
    if fim:
        partes.append(f"último: {fim}")
    return " — ".join(partes)


def terminal(estado: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Estado terminal do job a partir do último ``tarefa.status``."""
    from core.jobs.manager import _summarize

    motivo = str(estado.get("reason") or "").strip()
    codigo = estado.get("exit_code")
    if "teto" in motivo:
        status = "timeout"
    elif motivo.startswith("parada"):
        status = "cancelled"
    elif motivo:
        status = "failed"
    elif codigo == 0:
        status = "completed"
    else:
        status = "failed"

    saida = str(estado.get("tail") or "").rstrip()
    erros = str(estado.get("stderr_tail") or "").strip()
    if erros:
        saida = f"{saida}\n[stderr]\n{erros}" if saida else f"[stderr]\n{erros}"

    fim = f"exit {codigo}" if codigo is not None else (
        f"sinal {estado.get('signal')}" if estado.get("signal") is not None else "sem código de saída"
    )
    cabeca = [
        f"Comando: {spec.get('command') or estado.get('command') or '?'}",
        f"Pasta: {spec.get('cwd') or estado.get('cwd') or '(home)'}",
        f"Duração: {duracao(estado.get('elapsed_s'))} ({fim})",
    ]
    if motivo:
        cabeca.append(f"Motivo do fim: {motivo}")
    return {
        "status": status,
        "exit_code": codigo,
        # O fim da saída basta (o erro, o total): o dispatcher corta o resumo em
        # 1200 caracteres no fallback e a lista do `zip -r` não diz nada a ninguém.
        "result_summary": "\n".join(cabeca) + "\n" + _summarize(saida, codigo, status, max_chars=800),
        "progress_note": None,
    }


def _falha(texto: str) -> dict[str, Any]:
    return {"status": "failed", "result_summary": texto, "progress_note": None}


async def acompanhar(
    job: AgentJob,
    *,
    db: Any,
    broadcaster: Any = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Consulta a tarefa no computador até ela terminar e devolve o estado terminal.

    Cancelado (botão da janela Tarefas, ``job_cancel``), PARA a tarefa no computador
    antes de propagar o cancelamento.
    """
    from core.notifications.device_control import (
        DeviceOffline,
        DeviceTimeout,
        get_device_control_broadcaster,
    )
    from shared.ajustes import ajustes

    spec = job.spec_json or {}
    device_id = str(spec.get("device_id") or "").strip()
    tarefa_id = str(spec.get("tarefa_id") or "").strip()
    if not device_id or not tarefa_id:
        return _falha("Tarefa sem computador ou sem id: não há o que acompanhar.")
    canal = broadcaster or get_device_control_broadcaster()
    cfg = ajustes().comando_no_computador
    intervalo = float(cfg.conferir_min_s)
    fora_desde: float | None = None

    try:
        while True:
            try:
                resposta = await canal.request(
                    device_id, "tarefa.status",
                    {"id": tarefa_id, "tail_bytes": _TAIL_BYTES},
                    timeout=_PEDIDO_TIMEOUT_S,
                )
            except (DeviceOffline, DeviceTimeout) as exc:
                agora = clock()
                fora_desde = fora_desde if fora_desde is not None else agora
                if agora - fora_desde >= cfg.fora_do_ar_min * 60:
                    return _falha(
                        f"O computador ficou {cfg.fora_do_ar_min} min sem responder no meio da "
                        f"tarefa, e não sei como ela terminou ({exc}). Comando: "
                        f"{spec.get('command') or '?'}"
                    )
                await sleep(intervalo)
                continue
            fora_desde = None
            if not resposta.get("ok"):
                erro = str(resposta.get("error") or "o computador recusou a consulta")
                if "desconhecida" in erro:
                    # Fechado normalmente, o app encerra as tarefas (`kill_all`). Morto
                    # de forma brusca, um comando que não escreve nada (`zip -rq`) pode
                    # seguir sozinho: não dá para afirmar que parou.
                    return _falha(
                        "O app do Neo no computador foi fechado ou reiniciado no meio da tarefa, "
                        "e o acompanhamento dela se perdeu. Fechado normalmente, o app encerra "
                        "as tarefas; se ele caiu, o comando pode ter continuado sozinho, então "
                        f"vale conferir o resultado. Comando: {spec.get('command') or '?'}"
                    )
                return _falha(f"Não consegui consultar a tarefa no computador: {erro}")
            estado = resposta.get("result") or {}
            if estado.get("status") == "exited":
                return terminal(estado, spec)
            try:
                await db.update_agent_job(
                    job.id, heartbeat_at=datetime.utcnow(), progress_note=progresso(estado)
                )
            except Exception:  # noqa: BLE001 — progresso é enfeite, a conferência segue
                pass
            await sleep(intervalo)
            intervalo = min(intervalo * 2, float(cfg.conferir_max_s))
    except asyncio.CancelledError:
        try:
            await asyncio.shield(
                canal.request(device_id, "tarefa.stop", {"id": tarefa_id}, timeout=10.0)
            )
            logger.info("[comando-local] tarefa %s parada no computador (job %s)", tarefa_id, job.id)
        except Exception as exc:  # noqa: BLE001 — offline: o app mata as tarefas ao fechar
            logger.warning("[comando-local] não consegui parar a tarefa %s: %s", tarefa_id, exc)
        raise


async def logs(job: AgentJob, max_bytes: int | None = None, broadcaster: Any = None) -> dict[str, Any]:
    """O fim da saída para a janela Tarefas: ao vivo do computador, ou o resumo."""
    from core.notifications.device_control import get_device_control_broadcaster

    spec = job.spec_json or {}
    resumo = job.result_summary or job.progress_note or "(sem saída ainda)"
    if job.status != "running":
        return {"logs": resumo, "truncated": False, "source": "resumo"}
    canal = broadcaster or get_device_control_broadcaster()
    try:
        resposta = await canal.request(
            str(spec.get("device_id") or ""), "tarefa.status",
            {"id": str(spec.get("tarefa_id") or ""), "tail_bytes": min(int(max_bytes or _TAIL_BYTES), 64 * 1024)},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {"logs": resumo, "truncated": False, "source": "resumo", "error": str(exc)}
    estado = (resposta or {}).get("result") or {}
    if not (resposta or {}).get("ok") or not estado:
        return {"logs": resumo, "truncated": False, "source": "resumo"}
    corpo = str(estado.get("tail") or "")
    if estado.get("stderr_tail"):
        corpo += "\n[stderr]\n" + str(estado["stderr_tail"])
    return {
        "logs": corpo or "(sem saída ainda)",
        "truncated": int(estado.get("bytes") or 0) > len(corpo.encode()),
        "source": "computador",
    }
