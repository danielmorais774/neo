"""Runners dos disparos agendados (docs/scheduler-plan.md §5 e §6).

Um runner por ação. Todos devolvem o dicionário de estado terminal que o
JobManager persiste — então concorrência, timeout, reconcile de boot e entrega
proativa continuam sendo dele. A agenda não executa nada por fora desse trilho.

Duas coisas aqui merecem atenção porque parecem detalhe e não são:

* **``texto_literal``.** O ProactiveDispatcher redige o aviso com um LLM. Isso é
  ótimo para "o script terminou com exit 1" e péssimo para um alerta de texto
  fixo (que vira paráfrase) ou para a resposta de um turno cognitivo (que já
  saiu com a persona certa e só perderia qualidade sendo reescrita por um
  modelo menor). Quem sabe o texto final põe em ``texto_literal`` e o dispatcher
  entrega como está.
* **O contador da tarefa mora aqui.** O tick dispara e esquece; é o runner que
  vê o desfecho — e portanto é ele quem zera ou incrementa
  ``consecutive_errors`` e arma o disjuntor.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable
from uuid import UUID

from core.scheduler.conditions import fingerprint
from core.scheduler.timing import in_quiet_hours, quiet_hours_end, to_local, to_utc_naive, utcnow
from db.models.agent_job import AgentJob
from db.models.scheduled_task import MAX_CONSECUTIVE_ERRORS, ScheduledTask
from shared import ToolCall

logger = logging.getLogger(__name__)

#: Teto absoluto de um turno agendado, mesmo que a tarefa peça mais.
_MAX_TURN_SECONDS = 1800


def build_scheduled_runner(
    *,
    db: Any,
    service: Any,
    ai_service: Any = None,
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Devolve o runner que o JobManager chama para todo ``scheduled_*``."""

    async def run(job: AgentJob) -> dict:
        spec = job.spec_json or {}
        task = await _load_task(db, spec.get("scheduled_task_id"))
        action_kind = str(spec.get("action_kind") or "").strip()
        action = dict(spec.get("action") or {})

        try:
            if action_kind == "notify":
                terminal = _run_notify(action)
            elif action_kind == "tool":
                terminal = await _run_tool(job, action, spec, ai_service)
            elif action_kind == "job":
                terminal = await _run_job_action(job, action, ai_service)
            elif action_kind == "prompt":
                terminal = await _run_prompt(job, action, spec, ai_service, db, task)
            else:
                terminal = {
                    "status": "failed",
                    "result_summary": f"Ação agendada desconhecida: {action_kind!r}",
                }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[agenda] disparo %s quebrou", job.id)
            terminal = {"status": "failed", "result_summary": f"Erro no disparo: {exc}"}

        terminal = _apply_only_if_changed(terminal, task, spec)
        terminal = _apply_quiet_hours(terminal, spec)
        await _settle_task(db, service, task, terminal, job)
        return terminal

    return run


# ──────────────────────────────────────────────────────────────────────
# Ações
# ──────────────────────────────────────────────────────────────────────

def _run_notify(action: dict) -> dict:
    """Alerta de texto fixo: zero LLM, zero rede, zero surpresa."""
    titulo = str(action.get("titulo") or "").strip()
    corpo = str(action.get("corpo") or "").strip()
    texto = "\n\n".join(p for p in (titulo, corpo) if p) or titulo or corpo
    return {
        "status": "completed",
        "result_summary": texto,
        "extra_metadata_patch": {"texto_literal": texto},
        "_fingerprint_source": texto,
    }


async def _run_tool(job: AgentJob, action: dict, spec: dict, ai_service: Any) -> dict:
    """Uma tool server-side, direto, sem turno cognitivo."""
    if ai_service is None:
        return {"status": "failed", "result_summary": "AIService indisponível para executar a tool."}
    nome = str(action.get("tool") or "").strip()
    argumentos = dict(action.get("arguments") or {})
    if not nome:
        return {"status": "failed", "result_summary": "Disparo de tool sem nome de tool."}

    permitido, motivo = _tool_allowed(nome, spec.get("tool_policy") or {})
    if not permitido:
        return {"status": "failed", "result_summary": motivo}

    executor = ai_service.build_request_executor(
        user_id=job.user_id,
        session_id=job.session_id,
        metadata={"source": "schedule", "unattended": True},
        user_message=job.title or "",
    )
    timeout = _timeout_of(spec)
    try:
        resultado = await asyncio.wait_for(
            executor.execute_tool_call(ToolCall(name=nome, arguments=argumentos)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"status": "timeout", "result_summary": f"A tool {nome} passou de {timeout}s."}

    erro = getattr(resultado, "error", None)
    payload = getattr(resultado, "result", resultado)
    if erro:
        return {"status": "failed", "result_summary": f"{nome} falhou: {erro}"}
    return {
        "status": "completed",
        "result_summary": f"{nome} → {_short(payload)}",
        "_fingerprint_source": payload,
    }


async def _run_job_action(job: AgentJob, action: dict, ai_service: Any) -> dict:
    """Enfileira um job pesado (pesquisa, navegador, sandbox) pela tool dele.

    Reusar a tool em vez de montar o ``agent_job`` na mão não é preguiça: é o
    que garante que uma pesquisa agendada nasça com a ``research_task`` junto —
    do contrário o job roda e morre sem ter o que executar.
    """
    from core.scheduler.service import JOB_ACTION_TOOLS

    kind = str(action.get("kind") or "").strip()
    tool = JOB_ACTION_TOOLS.get(kind)
    if not tool:
        return {"status": "failed", "result_summary": f"Tipo de job agendado inválido: {kind!r}"}

    resultado = await _run_tool(
        job,
        {"tool": tool, "arguments": dict(action.get("spec") or {})},
        {"tool_policy": {}},
        ai_service,
    )
    if resultado.get("status") != "completed":
        return resultado
    # O job FILHO é quem avisa quando terminar. Falar duas vezes ("comecei" +
    # "terminei") por uma tarefa que o usuário agendou e já sabe que existe é
    # ruído — e ruído é o que faz agenda ser desligada.
    resultado["notify_policy"] = "silent"
    return resultado


async def _run_prompt(
    job: AgentJob, action: dict, spec: dict, ai_service: Any, db: Any, task: ScheduledTask | None
) -> dict:
    """Um turno cognitivo completo, como se o usuário tivesse falado — quase.

    O "quase" é o ``metadata`` (§6): ``source=schedule`` tira o turno do afeto e
    da birra, ``unattended`` tira as tools client-side da mesa e devolve um
    CLIENT_TOOL ao cognitivo.
    """
    if ai_service is None:
        return {"status": "failed", "result_summary": "AIService indisponível para o turno."}
    texto = str(action.get("texto") or "").strip()
    if not texto:
        return {"status": "failed", "result_summary": "Disparo de prompt sem texto."}

    metadata = {
        "source": "schedule",
        "unattended": True,
        "client": "neo-scheduler",
        "scheduled_task_id": spec.get("scheduled_task_id"),
    }
    if action.get("modelo"):
        metadata["model"] = action["modelo"]

    final: str | None = None
    sessao_usada: Any = None
    pendente_cliente: str | None = None
    arquivos: list | None = None

    async def _consumir() -> None:
        nonlocal final, sessao_usada, pendente_cliente, arquivos
        async for resp in ai_service.process_user_input(
            user_message=texto,
            session_id=job.session_id,
            user_id=job.user_id,
            service="neo-scheduler",
            metadata=metadata,
        ):
            sessao_usada = getattr(resp, "session_id", None) or sessao_usada
            pedido = getattr(resp, "tool_request", None) or getattr(resp, "command_request", None)
            if pedido and not getattr(resp, "is_final", False):
                pendente_cliente = str((pedido or {}).get("name") or pedido)[:120]
            if getattr(resp, "is_final", False):
                final = resp.message
                # Arquivo entregue por file_save no turno: sem isto o aviso chega ao
                # chat sem o botão de download (o turno não tinha ninguém na frente).
                arquivos = (getattr(resp, "metadata", None) or {}).get("files") or None

    timeout = _timeout_of(spec)
    try:
        await asyncio.wait_for(_consumir(), timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "status": "timeout",
            "result_summary": f"O turno agendado passou de {timeout}s e foi interrompido.",
        }

    # Sessão "própria": a primeira execução define a conversa da tarefa, e daí em
    # diante ela compara com o próprio passado sem precisar de tool nenhuma.
    if task is not None and task.session_mode == "propria" and task.session_id is None and sessao_usada:
        try:
            await db.update_scheduled_task(task.id, session_id=_as_uuid(sessao_usada))
        except Exception:  # noqa: BLE001
            logger.warning("[agenda] não consegui fixar a sessão própria de %s", task.id)

    if not final:
        motivo = (
            f"O turno pediu atuação na máquina do usuário ({pendente_cliente}) e não havia "
            "ninguém na frente."
            if pendente_cliente
            else "O turno agendado terminou sem resposta final."
        )
        return {"status": "failed", "result_summary": motivo}

    return {
        "status": "completed",
        "result_summary": final,
        # A resposta já saiu com a persona do Neo. Mandar o redator do dispatcher
        # reescrevê-la só a encurtaria e a pioraria.
        "extra_metadata_patch": {"texto_literal": final, **({"files": arquivos} if arquivos else {})},
        "_fingerprint_source": final,
    }


# ──────────────────────────────────────────────────────────────────────
# Pós-processamento comum
# ──────────────────────────────────────────────────────────────────────

def _tool_allowed(nome: str, policy: dict) -> tuple[bool, str]:
    deny = {str(x) for x in (policy.get("deny") or [])}
    allow = {str(x) for x in (policy.get("allow") or [])}
    if nome in deny:
        return False, f"A tool {nome} está na lista de bloqueio desta tarefa."
    if allow and nome not in allow:
        return False, f"A tool {nome} não está na lista permitida desta tarefa."
    return True, ""


def _timeout_of(spec: dict) -> int:
    policy = spec.get("tool_policy") or {}
    try:
        segundos = int(policy.get("max_seconds") or 600)
    except (TypeError, ValueError):
        segundos = 600
    return max(30, min(segundos, _MAX_TURN_SECONDS))


def _apply_only_if_changed(terminal: dict, task: ScheduledTask | None, spec: dict) -> dict:
    """Resultado igual ao da vez passada → cala a boca (mas registra)."""
    fonte = terminal.pop("_fingerprint_source", None)
    if not spec.get("only_if_changed") or terminal.get("status") != "completed":
        if fonte is not None:
            terminal["_novo_fingerprint"] = fingerprint(fonte)
        return terminal
    novo = fingerprint(fonte)
    terminal["_novo_fingerprint"] = novo
    anterior = spec.get("last_fingerprint")
    if anterior and anterior == novo:
        terminal["notify_policy"] = "silent"
        terminal["result_summary"] = (
            f"(sem mudança desde o disparo anterior)\n{terminal.get('result_summary', '')}"
        )
        terminal.pop("extra_metadata_patch", None)
    return terminal


def _apply_quiet_hours(terminal: dict, spec: dict) -> dict:
    """Silêncio noturno: silenciar, adiar ou forçar (§8.3).

    ``adiar`` não some com o aviso — ele vira um disparo novo marcado para o fim
    do silêncio. É a diferença entre "te aviso às 7h" e "esqueci".
    """
    quiet = spec.get("quiet_hours") or {}
    acao = str(quiet.get("acao") or "").strip()
    if not quiet or not acao or terminal.get("notify_policy") == "silent":
        return terminal
    tz = spec.get("timezone")
    agora_local = to_local(utcnow(), tz)
    if not in_quiet_hours(agora_local, quiet):
        return terminal
    if acao == "forcar":
        return terminal
    if acao == "silenciar":
        terminal["notify_policy"] = "silent"
        terminal["result_summary"] = f"(silenciado: horário de silêncio)\n{terminal.get('result_summary','')}"
        return terminal
    if acao == "adiar":
        fim = quiet_hours_end(agora_local, quiet)
        if fim is not None:
            terminal["notify_policy"] = "silent"
            terminal["_adiar_para"] = to_utc_naive(fim)
    return terminal


async def _settle_task(
    db: Any, service: Any, task: ScheduledTask | None, terminal: dict, job: AgentJob
) -> None:
    """Fecha a conta da tarefa: contadores, disjuntor e o adiamento pendente."""
    adiar_para = terminal.pop("_adiar_para", None)
    novo_fp = terminal.pop("_novo_fingerprint", None)
    patch = terminal.pop("extra_metadata_patch", None)
    if patch:
        terminal["extra_metadata"] = {**(job.extra_metadata or {}), **patch}

    if adiar_para is not None:
        await _agendar_adiamento(db, job, terminal, adiar_para)

    if task is None:
        return

    status = terminal.get("status")
    fields: dict[str, Any] = {
        "last_status": str(status or ""),
        "last_run_at": datetime.utcnow(),
        "last_job_id": job.id,
        "run_count": (task.run_count or 0) + 1,
    }
    if novo_fp:
        fields["last_fingerprint"] = novo_fp

    if status == "completed":
        fields["consecutive_errors"] = 0
    else:
        erros = (task.consecutive_errors or 0) + 1
        fields["consecutive_errors"] = erros
        if erros >= MAX_CONSECUTIVE_ERRORS:
            # Disjuntor: pausa E avisa. Uma agenda que falha em silêncio é pior
            # que não ter agenda — então o disparo que abriu o disjuntor entrega
            # a notícia, mesmo que a política fosse silenciosa.
            fields["enabled"] = False
            fields["paused_reason"] = (
                f"pausada após {erros} falhas seguidas — a última: "
                f"{str(terminal.get('result_summary') or '')[:200]}"
            )
            terminal["notify_policy"] = "chat"
            terminal["result_summary"] = (
                f"⚠️ Desliguei a tarefa \"{task.name}\": {erros} falhas seguidas.\n\n"
                f"{terminal.get('result_summary', '')}"
            )
            terminal.pop("extra_metadata", None)

    # Teto de execuções: chegou no fim, desliga (sem isso, `max_runs` seria só
    # um número bonito na tela).
    if task.max_runs and fields["run_count"] >= task.max_runs:
        fields["enabled"] = False
        fields["paused_reason"] = f"concluída: atingiu o teto de {task.max_runs} execuções"

    try:
        await db.update_scheduled_task(task.id, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agenda] não consegui atualizar a tarefa %s: %s", task.id, exc)


async def _agendar_adiamento(db: Any, job: AgentJob, terminal: dict, quando: datetime) -> None:
    """Cria o disparo que entrega o aviso quando o silêncio acabar."""
    texto = str(terminal.get("result_summary") or "").strip()
    if not texto:
        return
    try:
        await db.create_agent_job(
            AgentJob(
                user_id=job.user_id,
                session_id=job.session_id,
                kind="scheduled_notify",
                title=job.title,
                spec_json={
                    **(job.spec_json or {}),
                    "action_kind": "notify",
                    "action": {"titulo": "", "corpo": texto},
                    "quiet_hours": {},  # já foi adiado uma vez; não adia de novo
                    "only_if_changed": False,
                },
                status="queued",
                next_run_at=quando,
                notify_policy="chat",
                extra_metadata={**(job.extra_metadata or {}), "adiado_de": str(job.id)},
            )
        )
        logger.info("[agenda] aviso de %s adiado para %s (horário de silêncio)", job.id, quando)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agenda] falha ao adiar o aviso de %s: %s", job.id, exc)


# ──────────────────────────────────────────────────────────────────────

async def _load_task(db: Any, task_id: Any) -> ScheduledTask | None:
    if not task_id:
        return None
    try:
        return await db.get_scheduled_task(_as_uuid(task_id))
    except Exception:  # noqa: BLE001
        return None


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _short(value: Any, limit: int = 1500) -> str:
    import json

    if isinstance(value, str):
        texto = value
    else:
        try:
            texto = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            texto = str(value)
    return texto if len(texto) <= limit else texto[:limit] + "…"
