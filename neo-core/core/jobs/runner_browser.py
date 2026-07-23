"""Runner de jobs ``kind="browser"`` — delega ao container neo-browser-agent.

O neo-core orquestra; quem dirige o Chromium é o subagente browser-use no
container (``docker/neo-browser-agent``), com LLM próprio de visão. Este runner
só faz o POST na sessão certa, espera o resultado e traduz para o estado
terminal do job (docs/browser-agent-plan.md §3.6).

Verdade do resultado vem do HISTÓRICO da lib (``success``/``is_done``), nunca
da narrativa do LLM — lição da skill HA (falso "desliguei").
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx

from db.models.agent_job import AgentJob

logger = logging.getLogger(__name__)


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
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Fecha as dependências e devolve ``run_browser_job(job) -> dict`` p/ o JobManager."""
    base = base_url.rstrip("/")

    async def run_browser_job(job: AgentJob) -> dict:
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

        body = {
            "task": task,
            "context": spec.get("context"),
            "max_steps": spec.get("max_steps"),
        }
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
        if not payload.get("ok"):
            err = (payload.get("error") or "erro desconhecido")[:300]
            return {
                "status": "failed",
                "result_summary": f"A tarefa de browser falhou: {err}",
            }
        if payload.get("success") is False:
            return {
                "status": "failed",
                "result_summary": (
                    "O subagente do browser não conseguiu completar a tarefa: "
                    + _summary(payload, task)
                ),
            }
        logger.info(
            "[Browser] job %s ok em %s passos (sessão %s)",
            job.id, payload.get("steps"), session,
        )
        return {"status": "completed", "result_summary": _summary(payload, task)}

    return run_browser_job
