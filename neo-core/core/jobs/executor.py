"""JobToolExecutor — executa ``neo_bash_background`` + ``job_*`` com escopo do turno.

Precisa saber o ``user_id`` e o ``session_id`` da conversa para amarrar o job à
sessão de origem (é para lá que o resultado volta). Por isso é construído por
request (como os executores user-scoped dos read tools) e roteado explicitamente
no loop cognitivo, não pelo executor global. Ver
``docs/workbench-background-jobs-plan.md``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from db.models.agent_job import AgentJob, TERMINAL_STATUSES
from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)

_MAX_JOB_TIMEOUT = 21_600  # 6h


class JobToolExecutor:
    """Executor por-request das tools de jobs em background."""

    def __init__(
        self,
        *,
        db: Any,
        job_manager: Any,
        user_id: UUID | None,
        session_id: UUID | None,
    ) -> None:
        self.db = db
        self.jm = job_manager
        self.user_id = user_id
        self.session_id = session_id

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        name = tool_call.name
        args = tool_call.arguments or {}
        try:
            if name == "neo_bash_background":
                result = await self._enqueue(args)
            elif name == "job_status":
                result = await self._status(args)
            elif name == "job_list":
                result = await self._list(args)
            elif name == "job_logs":
                result = await self._logs(args)
            elif name == "job_cancel":
                result = await self._cancel(args)
            else:
                raise ValueError(f"Tool de job desconhecida: {name}")
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=result,
                execution_time=asyncio.get_event_loop().time() - start,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Job tool '%s' failed: %s", name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )

    # ------------------------------------------------------------------

    async def _enqueue(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "command vazio."}
        title = str(args.get("title") or "Tarefa em background").strip()[:256]
        cwd = args.get("cwd")
        timeout = args.get("timeout")
        spec: dict[str, Any] = {"command": command}
        if cwd:
            spec["cwd"] = str(cwd)
        if timeout is not None:
            try:
                spec["timeout"] = max(1, min(int(timeout), _MAX_JOB_TIMEOUT))
            except (TypeError, ValueError):
                pass
        job = AgentJob(
            user_id=self.user_id,
            session_id=self.session_id,
            kind="sandbox_task",
            title=title,
            spec_json=spec,
            status="queued",
            notify_policy="chat",
        )
        job = await self.db.create_agent_job(job)
        # Acorda o loop para pegar já, sem esperar o tick.
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": title,
            "status": "queued",
            "note": "Job enfileirado. Avise o usuário que você avisará quando terminar.",
        }

    async def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        job = await self._get_owned(args.get("job_id"))
        if job is None:
            return {"ok": False, "error": "job não encontrado."}
        return {
            "ok": True,
            "job_id": str(job.id),
            "title": job.title,
            "status": job.status,
            "exit_code": job.exit_code,
            "result_summary": (job.result_summary or "")[:2000],
            "created_at": _iso(job.created_at),
            "finished_at": _iso(job.finished_at),
        }

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        only_active = bool(args.get("only_active"))
        statuses = ["queued", "running"] if only_active else None
        jobs = await self.db.list_agent_jobs(
            user_id=self.user_id, statuses=statuses, limit=20
        )
        return {
            "ok": True,
            "jobs": [
                {
                    "job_id": str(j.id),
                    "title": j.title,
                    "status": j.status,
                    "created_at": _iso(j.created_at),
                }
                for j in jobs
            ],
        }

    async def _logs(self, args: dict[str, Any]) -> dict[str, Any]:
        job = await self._get_owned(args.get("job_id"))
        if job is None:
            return {"ok": False, "error": "job não encontrado."}
        if not job.log_path:
            # Ainda não começou a escrever log (na fila) → devolve o que tiver.
            return {
                "ok": True,
                "job_id": str(job.id),
                "status": job.status,
                "logs": job.result_summary or "(sem logs ainda)",
            }
        tail = args.get("tail")
        max_bytes = None
        if tail is not None:
            try:
                max_bytes = max(100, int(tail))
            except (TypeError, ValueError):
                max_bytes = None
        log_rel = f".jobs/{job.id}.log"
        res = await self.jm.sandbox.read_file(log_rel, max_bytes=max_bytes)
        return {
            "ok": True,
            "job_id": str(job.id),
            "status": job.status,
            "logs": res.stdout if res.ok else (job.result_summary or "(sem logs)"),
            "truncated": res.truncated,
        }

    async def _cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        job = await self._get_owned(args.get("job_id"))
        if job is None:
            return {"ok": False, "error": "job não encontrado."}
        if job.status in TERMINAL_STATUSES:
            return {"ok": False, "error": f"job já está {job.status}.", "status": job.status}
        updated = await self.db.update_agent_job(
            job.id,
            status="cancelled",
            finished_at=datetime.utcnow(),
            result_summary=(job.result_summary or "Cancelado pelo usuário."),
        )
        # Aborta o worker em execução, se houver.
        try:
            task = self.jm._active.get(str(job.id))
            if task is not None:
                task.cancel()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "job_id": str(job.id), "status": "cancelled"}

    async def _get_owned(self, job_id_raw: Any) -> AgentJob | None:
        if not job_id_raw:
            return None
        try:
            job_id = UUID(str(job_id_raw))
        except ValueError:
            return None
        job = await self.db.get_agent_job(job_id)
        if job is None:
            return None
        # Escopo: um usuário só enxerga os próprios jobs (jobs sem dono são globais).
        if job.user_id is not None and self.user_id is not None and job.user_id != self.user_id:
            return None
        return job


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
