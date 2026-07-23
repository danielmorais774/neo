"""JobManager — reivindica e executa jobs em background sem bloquear o chat.

Um único loop (tick) roda no mesmo processo do neo-core (como o ``_offline_checker``
dos devices). A cada tick ele reivindica jobs prontos da tabela ``agent_job`` (claim
atômico no DB, à prova de dupla execução) e dispara um worker por job, limitado por
um semáforo. Ao terminar, marca o resultado e chama o :class:`ProactiveDispatcher`.

Restart-safe: no boot, todo job preso em ``running`` é reconciliado para ``failed``
(uma instância só roda), e jobs terminais ainda não entregues são reenviados. Nada
em memória é fonte da verdade — o DB é. Ver ``docs/workbench-background-jobs-plan.md``.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

from db.models.agent_job import AgentJob
from core.workbench.sandbox import WorkbenchSandbox, WorkbenchUnavailableError

if TYPE_CHECKING:
    from db.async_client import AsyncPostgresClient
    from core.jobs.dispatcher import ProactiveDispatcher

logger = logging.getLogger(__name__)


class JobManager:
    """Fila + pool de workers para jobs em background do Neo."""

    def __init__(
        self,
        *,
        db: "AsyncPostgresClient",
        sandbox: WorkbenchSandbox,
        max_concurrent: int = 3,
        tick_interval: int = 3,
        default_timeout: int = 1800,
        workspace_root: str = "/workspace",
        dispatcher: "ProactiveDispatcher | None" = None,
        research_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
        browser_runner: "Callable[[AgentJob], Awaitable[dict]] | None" = None,
    ) -> None:
        self.db = db
        self.sandbox = sandbox
        self.tick_interval = tick_interval
        self.default_timeout = default_timeout
        self.workspace_root = workspace_root.rstrip("/") or "/workspace"
        self._dispatcher = dispatcher
        # Runner de jobs kind="research", injetado no bootstrap (o JobManager
        # NÃO importa core/research — docs/web-search-deep-research-plan.md §5.4).
        self._research_runner = research_runner
        # Runner de jobs kind="browser" (subagente browser-use no container
        # neo-browser-agent — docs/browser-agent-plan.md), mesmo padrão.
        self._browser_runner = browser_runner
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active: dict[str, asyncio.Task] = {}
        self._tick_task: asyncio.Task | None = None
        self._poke = asyncio.Event()
        self._stopped = False

    def set_dispatcher(self, dispatcher: "ProactiveDispatcher") -> None:
        self._dispatcher = dispatcher

    def job_log_path(self, job_id) -> str:
        """Caminho absoluto do log do job dentro do sandbox."""
        return f"{self.workspace_root}/.jobs/{job_id}.log"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Reconcilia órfãos do boot e sobe o loop de tick."""
        try:
            orphans = await self.db.reconcile_orphan_agent_jobs()
            if orphans:
                logger.info("[Jobs] Reconciled %d orphan job(s) from a restart", len(orphans))
                for job in orphans:
                    await self._dispatch(job)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Jobs] Boot reconcile failed: %s", e)

        # Reentrega jobs terminais que não chegaram a ser notificados.
        try:
            for job in await self.db.list_undelivered_terminal_jobs():
                await self._dispatch(job)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Jobs] Undelivered catch-up failed: %s", e)

        self._tick_task = asyncio.create_task(self._tick_loop(), name="job-manager-tick")
        logger.info(
            "[Jobs] JobManager started (max_concurrent=%d, tick=%ds)",
            self._max_concurrent, self.tick_interval,
        )

    async def stop(self) -> None:
        self._stopped = True
        self._poke.set()
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._active.values()):
            task.cancel()

    def poke(self) -> None:
        """Acorda o loop para reivindicar um job recém-enfileirado sem esperar o tick."""
        self._poke.set()

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while not self._stopped:
            try:
                await self._claim_and_spawn()
            except Exception as e:  # noqa: BLE001
                logger.warning("[Jobs] tick error: %s", e)
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.tick_interval)
            except asyncio.TimeoutError:
                pass
            finally:
                self._poke.clear()

    async def _claim_and_spawn(self) -> None:
        slots = self._max_concurrent - len(self._active)
        if slots <= 0:
            return
        jobs = await self.db.claim_due_agent_jobs(limit=slots)
        for job in jobs:
            self._spawn_worker(job)

    def _spawn_worker(self, job: AgentJob) -> None:
        job_id = str(job.id)
        if job_id in self._active:
            return
        task = asyncio.create_task(self._run_job(job), name=f"job-{job_id}")
        self._active[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._active.pop(jid, None))

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def _run_job(self, job: AgentJob) -> None:
        """Roda um job já reivindicado (status=running) até um estado terminal."""
        async with self._sem:
            terminal: dict = {"status": "failed", "result_summary": "Falha desconhecida."}
            try:
                if job.kind == "sandbox_task":
                    terminal = await self._run_sandbox_task(job)
                elif job.kind == "research" and self._research_runner is not None:
                    terminal = await self._research_runner(job)
                elif job.kind == "browser" and self._browser_runner is not None:
                    terminal = await self._browser_runner(job)
                else:
                    terminal = {
                        "status": "failed",
                        "result_summary": f"Tipo de job não suportado nesta fase: {job.kind}",
                    }
            except WorkbenchUnavailableError as e:
                terminal = {
                    "status": "failed",
                    "result_summary": f"Ambiente de execução indisponível: {e}",
                }
            except asyncio.CancelledError:
                # Shutdown ou cancel explícito: deixa o estado como está (cancel já
                # marcou 'cancelled'); em shutdown, o boot reconcile cuida.
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("[Jobs] job %s crashed", job.id)
                terminal = {"status": "failed", "result_summary": f"Erro no job: {e}"}

            updated = None
            try:
                updated = await self.db.update_agent_job(
                    job.id,
                    finished_at=datetime.utcnow(),
                    **terminal,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("[Jobs] failed to persist terminal state for %s: %s", job.id, e)
            if updated is not None:
                await self._dispatch(updated)

    async def _run_sandbox_task(self, job: AgentJob) -> dict:
        spec = job.spec_json or {}
        command = str(spec.get("command") or "").strip()
        if not command:
            return {"status": "failed", "result_summary": "Job sem comando."}
        cwd = spec.get("cwd")
        timeout = spec.get("timeout")
        try:
            timeout = int(timeout) if timeout is not None else self.default_timeout
        except (TypeError, ValueError):
            timeout = self.default_timeout

        log_abs = self.job_log_path(job.id)
        log_dir = log_abs.rsplit("/", 1)[0]
        # Redireciona a saída para o log dentro do sandbox (persistente, legível a
        # qualquer momento por job_logs). O exit code do pipeline é o do comando.
        wrapped = (
            f"mkdir -p {shlex.quote(log_dir)} && "
            f"( {command} ) > {shlex.quote(log_abs)} 2>&1"
        )
        try:
            await self.db.update_agent_job(
                job.id,
                log_path=log_abs,
                timeout_at=datetime.utcnow() + timedelta(seconds=timeout),
                heartbeat_at=datetime.utcnow(),
            )
        except Exception:  # noqa: BLE001
            pass

        result = await self.sandbox.run(command=wrapped, cwd=cwd, timeout=timeout)

        # Lê o conteúdo do log para virar o result_summary (tail).
        output = ""
        try:
            log_rel = f".jobs/{job.id}.log"
            log_res = await self.sandbox.read_file(log_rel)
            if log_res.ok:
                output = log_res.stdout
        except Exception:  # noqa: BLE001
            output = result.stdout or result.stderr

        if result.timed_out:
            status = "timeout"
        elif result.ok:
            status = "completed"
        else:
            status = "failed"

        summary = _summarize(output, result.exit_code, status)
        return {
            "status": status,
            "exit_code": result.exit_code,
            "result_summary": summary,
        }

    async def _dispatch(self, job: AgentJob) -> None:
        if self._dispatcher is None:
            # Sem dispatcher (F2 puro): resultado fica no DB; marca notificado para
            # não reprocessar em catch-up.
            try:
                await self.db.update_agent_job(job.id, notified_at=datetime.utcnow())
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            await self._dispatcher.dispatch(job)
        except Exception as e:  # noqa: BLE001
            logger.error("[Jobs] dispatch failed for %s: %s", job.id, e)


def _summarize(output: str, exit_code: int | None, status: str, *, max_chars: int = 4000) -> str:
    """Resumo textual do resultado do job para o result_summary/dispatcher."""
    tail = output or ""
    if len(tail) > max_chars:
        tail = "…" + tail[-max_chars:]
    header = f"[{status}] exit={exit_code}"
    if not tail.strip():
        return f"{header}\n(sem saída)"
    return f"{header}\n{tail}"
