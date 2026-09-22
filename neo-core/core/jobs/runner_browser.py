"""Runner de jobs ``kind="browser"`` — delega ao container neo-browser-agent.

O neo-core orquestra; quem dirige o Chromium é o subagente browser-use no
container (``docker/neo-browser-agent``), com LLM de visão próprio. Este runner
só faz o POST na sessão certa, espera o resultado e traduz para o estado
terminal do job (docs/browser-agent-plan.md §3.6).

O MODELO do subagente vai no corpo da task (role ``browser_agent`` do painel).
Antes ele era o ``NEO_BROWSER_LLM`` do container — cravado no compose, fora do
alcance de qualquer tela de config: trocar o LLM de todos os outros subsistemas
não mudava este, e o único jeito de vê-lo era o log do outro container.

Verdade do resultado vem do HISTÓRICO da lib (``success``/``is_done``), nunca
da narrativa do LLM — lição da skill HA (falso "desliguei").

Prints: o wrapper devolve as capturas em base64 (modo ``auto`` = só quando dá
erro); aqui elas viram arquivos do File Store e o ``file_id`` vai no
``extra_metadata["images"]`` do job. Quem transforma isso em imagem na bolha é o
ProactiveDispatcher — depois de redigir o texto, para o LLM não ter chance de
comer o marcador.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx

from core.credentials.browser import build_browser_credentials
from core.jobs.screenshots import ingest_shots
from db.models.agent_job import AgentJob

from core.jobs.browser_blocked import anotar_se_bloqueio

logger = logging.getLogger(__name__)


def _resolve_store(getter: Callable[[], Any] | None) -> Any:
    """Getter tolerante: cofre ausente vira problema explicado, não exceção."""
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


def _summary(payload: dict, task: str) -> str:
    """Aviso proativo no chat: resultado final + onde a sessão parou."""
    final = (payload.get("final_result") or "").strip()
    if not final:
        final = f"A tarefa \"{task}\" terminou sem resultado textual."
    if len(final) > 1500:
        final = final[:1500] + "…"
    url = payload.get("current_url")
    if url:
        final += f"\n\n(sessão parada em {url} — dá para continuar de lá)"
    return final


def build_browser_runner(
    *,
    db: Any,
    base_url: str,
    timeout_s: int = 600,
    get_file_store: Callable[[], Any] | None = None,
    get_credential_store: Callable[[], Any] | None = None,
    default_screenshots: str = "auto",
    max_screenshots: int = 3,
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Fecha as dependências e devolve ``run_browser_job(job) -> dict`` p/ o JobManager.

    ``get_file_store`` é um GETTER, não o store: no bootstrap o JobManager nasce
    antes do File Store (api/main.py), então quem resolve na hora do uso é o
    runner. Sem store, a task continua valendo — só não tem print.
    """
    base = base_url.rstrip("/")

    async def _resolve_model() -> str | None:
        """Modelo do subagente para ESTA task: escolha do painel (DB) → default.

        Lê o role ``browser_agent`` (memoizado, nunca levanta) e resolve POR JOB
        — não no fecho — para que trocar o modelo no painel valha na próxima
        task, sem restart e sem rebuild do container.

        ``None`` = "não opino": o wrapper cai no ``NEO_BROWSER_LLM`` dele. É o
        que mantém o comportamento de antes quando o banco está vazio.
        """
        try:
            from modules.model_config import get_model_config

            model = (await get_model_config(db, "browser_agent")).get("model")
        except Exception as e:  # noqa: BLE001 — nunca quebra a task por config
            logger.warning("[Browser] resolve model falhou, usando o default do wrapper: %s", e)
            return None
        if not model:
            return None
        # Pin de provedor (`vendor/modelo@slug`) é convenção do LLMRouter daqui;
        # quem chama lá é o browser-use, que manda o ID cru para o gateway e
        # tomaria 404. Nenhum modelo com visão tem pin hoje — isto é a rede.
        if "@" in model:
            bare = model.rsplit("@", 1)[0]
            logger.warning(
                "[Browser] o subagente não honra pin de provedor — %r vai como %r",
                model, bare,
            )
            return bare
        return model

    async def _run_unlocked(job: AgentJob) -> dict:
        spec = job.spec_json or {}
        task = str(spec.get("task") or "").strip()
        if not task:
            return {"status": "failed", "result_summary": "Job de browser sem task no spec."}
        session = str(spec.get("session") or "user-default")

        try:
            await db.update_agent_job(
                job.id,
                timeout_at=datetime.utcnow() + timedelta(seconds=timeout_s + 30),
                heartbeat_at=datetime.utcnow(),
                progress_note=f"Browser trabalhando na sessão '{session}'…",
            )
        except Exception:  # noqa: BLE001
            pass

        shots_mode = str(spec.get("screenshots") or default_screenshots)

        # Credenciais (browser-agent-plan §3.5): o spec guarda RÓTULOS; o valor é
        # decifrado aqui, na borda, e vai direto para o wrapper como
        # `sensitive_data` escopado por domínio. Não passa por prompt, por log de
        # job nem pelo LLM do subagente — que só vê o placeholder.
        creds = None
        labels = [str(x) for x in (spec.get("credentials") or []) if str(x).strip()]
        if labels:
            store = _resolve_store(get_credential_store)
            creds = await build_browser_credentials(store, labels)
            if creds.problems:
                logger.warning("[Browser] job %s: %s", job.id, "; ".join(creds.problems))

        context = str(spec.get("context") or "")
        if creds and creds.notes:
            context = "\n".join([context, *creds.notes]).strip()

        body = {
            "task": task,
            "model": await _resolve_model(),
            "context": context or None,
            "max_steps": spec.get("max_steps"),
            "screenshots": shots_mode,
            "max_screenshots": spec.get("max_screenshots") or max_screenshots,
            "sensitive_data": (creds.sensitive_data or None) if creds else None,
        }

        def _with_credential_notes(terminal: dict) -> dict:
            """Cola no resumo o que NÃO deu com as credenciais.

            Sem isto o usuário vê "não consegui logar" e não descobre que o
            motivo foi um domínio em branco no cadastro do cofre.
            """
            if not creds or not creds.problems:
                return terminal
            aviso = "⚠️ Credenciais: " + " ".join(creds.problems)
            terminal["result_summary"] = f"{terminal.get('result_summary') or ''}\n\n{aviso}".strip()
            return terminal

        async def _with_shots(terminal: dict, payload: dict) -> dict:
            """Ingere os prints do payload e pendura os file_ids no job."""
            store = _resolve_store(get_file_store)
            terminal = _with_credential_notes(terminal)
            images = await ingest_shots(
                store,
                payload.get("screenshots"),
                user_id=job.user_id,
                session_id=job.session_id,
                label="browser",
                source={"job_id": str(job.id), "session": session, "url": payload.get("current_url")},
            )
            if not images:
                return terminal
            logger.info("[Browser] job %s: %s print(s) no File Store", job.id, len(images))
            terminal["extra_metadata"] = {
                **(job.extra_metadata or {}),
                "images": images,
            }
            # O texto AVISA que há print; o marcador quem põe é o dispatcher.
            summary = terminal.get("result_summary") or ""
            plural = "prints" if len(images) > 1 else "print"
            terminal["result_summary"] = f"{summary}\n\n📸 {len(images)} {plural} da tela em anexo.".strip()
            return terminal

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_s + 15, connect=10)
            ) as client:
                resp = await client.post(f"{base}/sessions/{session}/tasks", json=body)
        except httpx.ConnectError:
            return {
                "status": "failed",
                "result_summary": "O serviço de browser (neo-browser-agent) está fora do ar.",
            }
        except httpx.TimeoutException:
            return {
                "status": "timeout",
                "result_summary": (
                    f"A tarefa de browser \"{task[:120]}\" estourou o tempo limite "
                    f"({timeout_s}s). A sessão pode ter ficado no meio do caminho."
                ),
            }

        if resp.status_code == 429:
            return {
                "status": "failed",
                "result_summary": (
                    f"A sessão de browser '{session}' está ocupada com outras tarefas "
                    "(fila cheia). Tente de novo em instantes."
                ),
            }
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json().get("detail") or "")[:300]
            except Exception:  # noqa: BLE001
                detail = resp.text[:300]
            return {
                "status": "failed",
                "result_summary": f"O browser recusou a tarefa (HTTP {resp.status_code}): {detail}",
            }

        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return {
                "status": "failed",
                "result_summary": "Resposta ilegível do serviço de browser.",
            }

        # `success` vem de history.is_successful() da lib: True/False/None (None =
        # terminou sem veredito). ok=False = a corrida em si falhou/crashou.
        # Os três finais abaixo passam pelo `_with_shots`: o print interessa mais
        # no fracasso (é a tela do erro) do que no sucesso.
        if not payload.get("ok"):
            err = (payload.get("error") or "erro desconhecido")[:300]
            # O agente que estourou o prazo não é um agente que não descobriu
            # nada: ele carrega a memória do que já tentou. Até 2026-09-20 isso
            # era descartado e o usuário recebia só "timeout depois de 570s"
            # (job 36a81dd6), quando o agente sabia exatamente o problema.
            diag = (payload.get("diagnosis") or "").strip()
            resumo = f"A tarefa de browser falhou: {err}"
            if diag:
                resumo = f"{resumo}\n\n{diag}"
            return await _with_shots(
                {
                    "status": "failed",
                    "result_summary": anotar_se_bloqueio(resumo),
                },
                payload,
            )
        # Sem veredito E sem nada para mostrar não é sucesso — é uma corrida que
        # não produziu nada. Em 2026-08-28 um job assim (success=None,
        # final_result vazio, sessão parada em about:blank) foi marcado
        # `completed` e o dispatcher redigiu "Deu tudo certo" para o usuário,
        # que não tinha recebido arquivo nenhum. Mesma família do
        # neo-ha-false-success-invented-entity: estado terminal se decide pela
        # EVIDÊNCIA, não pela ausência de erro.
        if payload.get("success") is None and not (payload.get("final_result") or "").strip():
            logger.info(
                "[Browser] job %s sem veredito e sem resultado — tratando como falha",
                job.id,
            )
            return await _with_shots(
                {
                    "status": "failed",
                    "result_summary": anotar_se_bloqueio(
                        "O subagente do browser terminou sem veredito e sem nenhum "
                        "resultado — não dá para dizer que a tarefa foi feita. "
                        + _summary(payload, task)
                    ),
                },
                payload,
            )
        if payload.get("success") is False:
            return await _with_shots(
                {
                    "status": "failed",
                    "result_summary": anotar_se_bloqueio(
                        "O subagente do browser não conseguiu completar a tarefa: "
                        + _summary(payload, task)
                    ),
                },
                payload,
            )
        logger.info(
            "[Browser] job %s ok em %s passos (sessão %s)",
            job.id, payload.get("steps"), session,
        )
        return await _with_shots(
            {"status": "completed", "result_summary": _summary(payload, task)},
            payload,
        )

    # UMA tarefa por sessão de browser de cada vez, esperando AQUI e não no container.
    # O container atende cada sessão em série e o JobManager roda até 3 jobs juntos:
    # antes, o 2º, 3º e 4º job da mesma sessão iam direto para o POST, esperavam na
    # fila de lá, e o timeout do httpx corria durante a espera. session-30b1e484:
    # dois jobs estouraram os 600s sem nunca terem começado e o quinto tomou 429
    # "fila cheia". Aqui o relógio só começa quando chega a vez — o `timeout_at` é
    # gravado dentro de `_run_unlocked`, depois do lock. Um processo só (o core roda
    # um worker), então um lock em memória basta.
    locks: dict[str, asyncio.Lock] = {}

    async def run_browser_job(job: AgentJob) -> dict:
        session = str((job.spec_json or {}).get("session") or "user-default")
        lock = locks.setdefault(session, asyncio.Lock())
        if lock.locked():
            try:
                await db.update_agent_job(
                    job.id,
                    heartbeat_at=datetime.utcnow(),
                    progress_note=(
                        f"Na fila: a sessão de browser '{session}' está terminando "
                        "outra tarefa."
                    ),
                )
            except Exception:  # noqa: BLE001
                pass
        async with lock:
            return await _run_unlocked(job)

    return run_browser_job
