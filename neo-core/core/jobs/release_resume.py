"""Aprovar a liberação continua a tarefa do navegador que parou esperando por ela.

O fluxo de formulário NÃO é linear, e é isso que este arquivo assume:

1. o job do navegador chega num campo com dado pessoal sem liberação, PARA, e
   o runner cria o pedido (o card nos aparelhos) — ``runner_browser_local``;
2. o job termina e a aba fica aberta onde parou. O turno que o disparou já
   acabou há muito tempo;
3. minutos ou horas depois, o Daniel aprova no aparelho;
4. a aprovação acha o job que estava esperando AQUELA liberação e enfileira a
   continuação — mesma tarefa, mesma aba, mesmo navegador — sem LLM no meio e
   sem depender de alguém repetir o pedido no chat.

Antes disto, a única retomada possível era um turno cognitivo genérico
(``core/release/resume.py``), que precisava REDESCOBRIR a tarefa a partir do
histórico e reescrevê-la — a mesma reescrita que matou três jobs em 2026-09-01.
Aquele turno continua existindo para os pedidos que não nasceram de um job
(``solicitar_dado`` de um e-mail, por exemplo); para os que nasceram, é este.

A continuação herda (e por quê):

* a **tarefa** original, palavra por palavra — reescrever é o que estraga;
* a **aba** e, se ela sumiu, o **endereço** onde a tarefa parou;
* o ``confirm_sensitive_ok`` original e nada além: liberar o DADO não é liberar
  o clique em "Enviar". Se a tarefa não tinha permissão para o clique final,
  ela para nele e pergunta, como sempre;
* a **origem** do turno que criou a tarefa, para um segundo pedido (outro campo)
  sair com o mesmo aviso de "nasceu de terceiro" que o primeiro.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from db.models.agent_job import AgentJob
from db.models.release_request import STATUS_APROVADA

logger = logging.getLogger(__name__)

#: Quantos jobs recentes olhar para achar o que espera a liberação. A
#: aprovação vale até 15 min depois do pedido (TTL do card); 40 jobs de folga
#: cobre um dia movimentado.
_JANELA_DE_JOBS = 40


def _sessao(job: AgentJob) -> dict:
    return dict((job.extra_metadata or {}).get("browser_session") or {})


async def job_esperando(db: Any, row: Any) -> AgentJob | None:
    """O job do navegador que parou esperando ESTA liberação, se houver.

    Já retomado uma vez → ``None``: a mesma aprovação não abre duas tarefas no
    navegador dele, nem depois de o core reiniciar (a marca mora no banco, não
    na memória do processo).
    """
    try:
        recentes = await db.list_agent_jobs(row.user_id, limit=_JANELA_DE_JOBS)
    except Exception as exc:  # noqa: BLE001 — sem banco, cai no turno genérico
        logger.warning("[release][job] não consegui listar jobs: %s", exc)
        return None
    for job in recentes or []:
        if job.kind != "browser_local":
            continue
        sessao = _sessao(job)
        if str(sessao.get("release_pending") or "") != str(row.id):
            continue
        if job.status == "cancelled":
            # Ele cancelou a tarefa na janela de tarefas: aprovar depois não a
            # ressuscita.
            logger.info("[release][job] %s: a tarefa %s foi cancelada", row.id, job.id)
            return None
        if sessao.get("release_resumed"):
            logger.info(
                "[release][job] %s já continuou no job %s", row.id, sessao["release_resumed"]
            )
            return None
        return job
    return None


def spec_de_continuacao(parado: AgentJob, row: Any) -> dict[str, Any]:
    """O ``spec_json`` da tarefa que continua de onde ``parado`` parou."""
    antigo = dict(parado.spec_json or {})
    sessao = _sessao(parado)
    campos = [str(c).split(":", 1)[-1] for c in (row.campos or [])]
    marcas = ", ".join(f"«pks:{c}»" for c in campos)
    liberado = (
        f"CONTINUAÇÃO: esta tarefa já tinha começado e parou pedindo liberação. O "
        f"Daniel APROVOU {', '.join(campos)} para {row.destino}. A aba já está onde a "
        f"tarefa parou — não recomece do zero. Preencha esses campos escrevendo "
        f"{marcas} como valor: o sistema troca pelo dado real, e só neste site. "
        "Nunca escreva o número."
    )
    contexto = "\n\n".join(p for p in (str(antigo.get("context") or "").strip(), liberado) if p)
    spec: dict[str, Any] = {
        "task": antigo.get("task") or "",
        "context": contexto[-2000:],
        "device_id": sessao.get("device_id") or antigo.get("device_id"),
        "headless": antigo.get("headless"),
        "surface_provider": antigo.get("surface_provider"),
        "max_steps": antigo.get("max_steps"),
        "tab_ref": sessao.get("tab_ref"),
        "reabrir_url": sessao.get("url") or antigo.get("start_url"),
        "confirm_sensitive_ok": antigo.get("confirm_sensitive_ok"),
        "autoridade": antigo.get("autoridade"),
        "pedido": antigo.get("pedido"),
        # O que a memória respondeu antes da parada: a continuação preenche com
        # isto em vez de consultar de novo (ver `BrowserLocalDriver._consultar`).
        "respostas_memoria": sessao.get("respostas_memoria"),
        "retomada_de": str(parado.id),
        "release_id": str(row.id),
    }
    return {k: v for k, v in spec.items() if v not in (None, "", [])}


async def navegador_ocupado(db: Any, user_id: Any, device_id: str) -> AgentJob | None:
    """Uma tarefa já rodando NESTE navegador? Só existe uma aba por máquina."""
    try:
        vivos = await db.list_agent_jobs(user_id, statuses=["queued", "running"], limit=20)
    except Exception:  # noqa: BLE001 — na dúvida, deixa criar (comportamento antigo)
        return None
    for job in vivos or []:
        if job.kind == "browser_local" and str(
            (job.spec_json or {}).get("device_id") or ""
        ) == str(device_id or ""):
            return job
    return None


async def continuar(db: Any, job_manager: Any, parado: AgentJob, row: Any) -> AgentJob:
    """Enfileira a continuação e marca o job parado como retomado."""
    spec = spec_de_continuacao(parado, row)
    novo = AgentJob(
        user_id=parado.user_id,
        session_id=parado.session_id,
        kind="browser_local",
        title=f"{parado.title} (liberado)"[:256],
        spec_json=spec,
        status="queued",
        notify_policy="chat",
    )
    novo = await db.create_agent_job(novo)
    meta = dict(parado.extra_metadata or {})
    meta["browser_session"] = {**_sessao(parado), "release_resumed": str(novo.id)}
    try:
        # `continued`: a parada deixou de esperar — na janela de tarefas ela sai
        # de "esperando você" e o trabalho segue na tarefa nova.
        await db.update_agent_job(parado.id, extra_metadata=meta, status="continued")
    except Exception as exc:  # noqa: BLE001 — a continuação já existe; a marca é guarda
        logger.warning("[release][job] não marquei %s como retomado: %s", parado.id, exc)
    if job_manager is not None:
        try:
            job_manager.poke()
        except Exception:  # noqa: BLE001
            pass
    logger.warning(
        "[release][job] liberação %s → continuando %s como %s na aba %s",
        row.id, parado.id, novo.id, spec.get("tab_ref"),
    )
    return novo


def build_job_resumer(
    *,
    db: Any,
    job_manager: Any,
    announce: Callable[[Any, str], Awaitable[None]] | None = None,
) -> Callable[[Any], Awaitable[bool]]:
    """``async (row) -> bool``: ``True`` quando a decisão foi tratada aqui.

    ``False`` = não havia job esperando esta liberação, e quem chama segue para
    o turno cognitivo de sempre. Recusa também passa por aqui: a tarefa que
    esperava por ela é encerrada.
    """

    async def _avisar(row: Any, texto: str) -> None:
        if announce is None:
            return
        try:
            await announce(row, texto)
        except Exception:  # noqa: BLE001 — o aviso é conveniência
            logger.exception("[release][job] falha ao avisar sobre %s", row.id)

    async def _encerrar(parado: AgentJob, row: Any) -> None:
        """A liberação que a tarefa esperava não veio: ela termina cancelada.

        Sem aviso no chat: o anúncio da decisão já sai pelo announcer da rota.
        Cancelada, ela também não volta por aprovação tardia (`job_esperando`).
        """
        try:
            await db.update_agent_job(parado.id, status="cancelled")
        except Exception as exc:  # noqa: BLE001 — a decisão já está gravada
            logger.warning("[release][job] não encerrei %s: %s", parado.id, exc)
            return
        logger.warning(
            "[release][job] liberação %s %s → tarefa %s cancelada", row.id, row.status, parado.id
        )

    async def resume(row: Any) -> bool:
        parado = await job_esperando(db, row)
        if parado is None:
            return False
        if row.status != STATUS_APROVADA:
            # Recusou (ou venceu): a tarefa não espera mais nada. Fica cancelada,
            # e não "esperando você" para sempre na janela de tarefas.
            await _encerrar(parado, row)
            return True
        device_id = str(_sessao(parado).get("device_id") or (parado.spec_json or {}).get("device_id") or "")
        ocupado = await navegador_ocupado(db, parado.user_id, device_id)
        if ocupado is not None:
            # Disparar outra tarefa agora derrubaria a que está rodando (uma aba
            # por navegador). A liberação continua valendo; quem retoma depois é
            # o `continue_job` — a instrução para isso já foi entregue no chat.
            await _avisar(
                row,
                f"🔐 Liberado. Seu navegador está ocupado com “{ocupado.title}”; quando "
                f"terminar, me peça para continuar “{parado.title}” e eu sigo de onde parou.",
            )
            return True
        novo = await continuar(db, job_manager, parado, row)
        await _avisar(
            row,
            f"▶ Continuando “{parado.title}” no seu navegador com o que você liberou. "
            "Te aviso quando terminar.",
        )
        return bool(novo)

    return resume
