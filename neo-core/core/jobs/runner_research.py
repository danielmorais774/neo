"""Runner de jobs ``kind="research"`` — monta os adapters e persiste o resultado.

Fica no lado jobs de propósito: conhece DB, LLMRouter, WebSearchExecutor e
sandbox; o ``core/research`` (puro) recebe tudo por protocolo. O JobManager
recebe :func:`build_research_runner` já fechado sobre as dependências (injeção
no bootstrap) e NÃO importa core/research.

v2 (docs/deep-research-v2-plan.md): dois adapters de LLM — o barato
(``research_model``) para o volume e o forte (``research_model_writer``) para
escrever/criticar/revisar — e persistência de ``requirements_json``/``quality_json``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from db.models.agent_job import AgentJob
from shared import Message, MessageRole

logger = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "pesquisa"


class _LLMAdapter:
    """ResearchLLM fechado sobre LLMRouter + research_model."""

    def __init__(self, llm_router: Any, model: str) -> None:
        self.llm = llm_router
        self.model = model

    async def complete(self, system: str, user: str, *, max_tokens: int = 1500) -> str:
        provider = self.llm.get_provider_for_model(self.model)
        resp = await provider.complete(
            messages=[
                Message(role=MessageRole.SYSTEM, content=system),
                Message(role=MessageRole.USER, content=user),
            ],
            model=self.model,
            temperature=0.3,
            max_tokens=max_tokens,
        )
        return resp.get_content() or ""


class _SearchAdapter:
    """ResearchSearch fechado sobre WebSearchExecutor; conta o backend usado."""

    def __init__(self, web_search: Any) -> None:
        self.web = web_search
        self.backend_counts: Counter[str] = Counter()

    async def search(self, query: str, *, recency: str, category: str, n: int):
        from core.research.types import SearchHit

        payload = await self.web.search(query, recency=recency, category=category, n=n)
        if payload.get("error"):
            raise RuntimeError(payload.get("message") or payload["error"])
        backend = payload.get("backend")
        if backend:
            self.backend_counts[backend] += 1
        return [
            SearchHit(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet", ""),
                published_at=r.get("published_at"),
                source=r.get("source"),
            )
            for r in payload.get("results", [])
            if r.get("url")
        ]

    async def fetch(self, url: str, *, max_chars: int):
        from modules.web_search import FetchedPage
        from core.research.types import PageContent, PageError

        result = await self.web.fetch(url, max_chars=max_chars)
        if isinstance(result, FetchedPage):
            return PageContent(url=result.url, title=result.title, text=result.text)
        return PageError(url=url, error=result.error)

    @property
    def predominant_backend(self) -> str | None:
        if not self.backend_counts:
            return None
        return self.backend_counts.most_common(1)[0][0]


class _ProgressAdapter:
    """ResearchProgress: fase fina na research_task + progress_note/heartbeat no job."""

    _META_COLUMNS = {"requirements": "requirements_json", "quality": "quality_json"}

    def __init__(self, db: Any, task_id: Any, job_id: Any) -> None:
        self.db = db
        self.task_id = task_id
        self.job_id = job_id

    async def phase(self, status: str, note: str) -> None:
        try:
            await self.db.update_research_task(self.task_id, status=status)
            await self.db.update_agent_job(
                self.job_id, progress_note=note, heartbeat_at=datetime.utcnow()
            )
        except Exception as e:  # noqa: BLE001 — progresso é observabilidade, não fundação
            logger.warning("[Research] progress update falhou: %s", e)

    async def log(self, line: str) -> None:
        logger.info("[Research %s] %s", self.task_id, line)
        try:
            await self.db.update_agent_job(
                self.job_id, progress_note=line[:500], heartbeat_at=datetime.utcnow()
            )
        except Exception:  # noqa: BLE001
            pass

    async def meta(self, key: str, value: dict) -> None:
        """Grava metadados do pipeline (requisitos extraídos) já durante a corrida."""
        column = self._META_COLUMNS.get(key)
        if not column:
            return
        try:
            await self.db.update_research_task(self.task_id, **{column: value})
        except Exception as e:  # noqa: BLE001
            logger.warning("[Research] meta '%s' falhou: %s", key, e)


def _summary_from_report(report_md: str, sources_count: int, status_note: str = "") -> str:
    """Aviso proativo no chat: o RESUMO EXECUTIVO do relatório (a v1 mandava as 12
    primeiras linhas do markdown, que eram título + começo da primeira seção)."""
    match = re.search(
        r"^##\s+Resumo executivo\s*$(.*?)(?=^##\s|\Z)",
        report_md,
        re.MULTILINE | re.DOTALL,
    )
    if match and match.group(1).strip():
        head = match.group(1).strip()
    else:
        head = "\n".join([ln for ln in report_md.splitlines() if ln.strip()][:12])
    if len(head) > 1800:
        head = head[:1800] + "…"
    tail = f"\n\n({sources_count} fonte(s) citadas)"
    if status_note:
        tail += f" {status_note}"
    return head + tail


def build_research_runner(
    *,
    db: Any,
    llm_router: Any,
    web_search: Any,
    sandbox: Any = None,
    research_model: str = "deepseek-flash",
    research_model_writer: str | None = None,
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Fecha as dependências e devolve ``run_research_job(job) -> dict`` p/ o JobManager.

    ``research_model`` roda o volume (plan/triage/evidência/reflect/organize);
    ``research_model_writer`` (forte) escreve, critica e revisa — poucas chamadas,
    onde a qualidade do relatório é decidida.
    """
    default_writer_model = research_model_writer or research_model

    async def _resolve_models() -> tuple[str, str]:
        """Volume + writer models for this run: panel selection (DB) → settings.

        Reads roles "research_volume"/"research_writer" from
        :mod:`modules.model_config` (memoized, never raises). Resolved PER JOB — not
        baked into the closure — so a change in the panel lands on the next
        pesquisa without a restart. Falls back to the env-derived defaults.
        """
        volume, writer = research_model, default_writer_model
        try:
            from modules.model_config import get_model_config

            volume = (
                await get_model_config(
                    db, "research_volume", fallback_model=research_model
                )
            ).get("model") or research_model
            writer = (
                await get_model_config(
                    db, "research_writer", fallback_model=default_writer_model
                )
            ).get("model") or default_writer_model
        except Exception as e:  # noqa: BLE001 — nunca quebra a pesquisa por config
            logger.warning("[Research] resolve models falhou, usando defaults: %s", e)
        return volume, writer

    async def run_research_job(job: AgentJob) -> dict:
        from core.research import ResearchAgent, ResearchError, budget_for

        task = await db.get_research_task_by_job(job.id)
        if task is None:
            return {
                "status": "failed",
                "result_summary": "Job de pesquisa sem research_task correspondente.",
            }

        volume_model, writer_model = await _resolve_models()

        budget = budget_for(task.depth)
        await db.update_research_task(
            task.id, started_at=datetime.utcnow(), model=volume_model
        )
        try:
            await db.update_agent_job(
                job.id,
                timeout_at=datetime.utcnow() + timedelta(seconds=budget.timeout_s),
                heartbeat_at=datetime.utcnow(),
            )
        except Exception:  # noqa: BLE001
            pass

        search_adapter = _SearchAdapter(web_search)
        agent = ResearchAgent(
            llm=_LLMAdapter(llm_router, volume_model),
            search=search_adapter,
            progress=_ProgressAdapter(db, task.id, job.id),
            budget=budget,
            writer_llm=_LLMAdapter(llm_router, writer_model),
        )
        agent.stats.writer_model = writer_model

        try:
            report = await asyncio.wait_for(
                agent.run(task.topic, task.instructions), timeout=budget.timeout_s
            )
        except asyncio.TimeoutError:
            await db.update_research_task(
                task.id,
                status="timeout",
                finished_at=datetime.utcnow(),
                error=f"Estourou o budget de {budget.timeout_s}s ({task.depth}).",
                stats_json=agent.stats.to_dict(),
                backend=search_adapter.predominant_backend,
            )
            return {
                "status": "timeout",
                "result_summary": (
                    f"A pesquisa sobre \"{task.topic}\" estourou o tempo limite "
                    f"({budget.timeout_s}s no modo {task.depth})."
                ),
            }
        except asyncio.CancelledError:
            await db.update_research_task(
                task.id,
                status="cancelled",
                finished_at=datetime.utcnow(),
                stats_json=agent.stats.to_dict(),
                backend=search_adapter.predominant_backend,
            )
            raise
        except ResearchError as e:
            await db.update_research_task(
                task.id,
                status="failed",
                finished_at=datetime.utcnow(),
                error=str(e),
                stats_json=agent.stats.to_dict(),
                backend=search_adapter.predominant_backend,
            )
            return {
                "status": "failed",
                "result_summary": f"A pesquisa sobre \"{task.topic}\" falhou: {e}",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("[Research] job %s crashou", job.id)
            await db.update_research_task(
                task.id,
                status="failed",
                finished_at=datetime.utcnow(),
                error=f"Erro inesperado: {e}",
                stats_json=agent.stats.to_dict(),
                backend=search_adapter.predominant_backend,
            )
            return {
                "status": "failed",
                "result_summary": f"A pesquisa sobre \"{task.topic}\" falhou: {e}",
            }

        # Cópia em /workspace/research/ (best-effort: sandbox fora do ar não
        # falha a pesquisa — só loga e deixa report_path=None).
        report_path = None
        if sandbox is not None:
            try:
                rel_path = f"research/{_slugify(task.topic)}-{str(task.id)[:8]}.md"
                res = await sandbox.write_file(rel_path, report.markdown)
                if getattr(res, "ok", False):
                    report_path = f"/workspace/{rel_path}"
            except Exception as e:  # noqa: BLE001
                logger.warning("[Research] cópia no workspace falhou: %s", e)

        await db.update_research_task(
            task.id,
            status="completed",
            finished_at=datetime.utcnow(),
            report_markdown=report.markdown,
            report_path=report_path,
            sources_json=[
                {"n": s.n, "title": s.title, "url": s.url, "published_at": s.published_at}
                for s in report.sources
            ],
            stats_json=report.stats.to_dict(),
            quality_json=report.quality,
            requirements_json=report.requirements,
            backend=search_adapter.predominant_backend,
        )
        note = f"Relatório completo no painel (aba Pesquisas)."
        if report_path:
            note = f"Relatório completo no painel e em {report_path}."
        return {
            "status": "completed",
            "result_summary": _summary_from_report(
                report.markdown, len(report.sources), note
            ),
        }

    return run_research_job
