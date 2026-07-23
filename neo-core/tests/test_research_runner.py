"""Runner de research + tool deep_research — fakes de DB/LLM/busca (sem rede).

Cobre: job kind=research → completed com research_task consistente (relatório,
fontes, stats, qualidade e requisitos persistidos); os DOIS modelos (volume e
writer) chegam ao LLMRouter; o aviso do chat é o RESUMO EXECUTIVO; timeout do
budget → timeout nos dois; deep_research cria agent_job + research_task e volta
job_id na hora. Ver docs/deep-research-v2-plan.md §9.
"""

import json
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from core.jobs.executor import JobToolExecutor
from core.jobs.runner_research import build_research_runner
from db.models.agent_job import AgentJob
from db.models.research_task import ResearchTask
from shared import ToolCall


# ── fakes ───────────────────────────────────────────────────────────────────
class _FakeDB:
    def __init__(self):
        self.jobs: dict[UUID, AgentJob] = {}
        self.tasks: dict[UUID, ResearchTask] = {}

    async def create_agent_job(self, job):
        self.jobs[job.id] = job
        return job

    async def get_agent_job(self, job_id):
        return self.jobs.get(job_id)

    async def update_agent_job(self, job_id, **fields):
        job = self.jobs.get(job_id)
        if job is None:
            return None
        for k, v in fields.items():
            setattr(job, k, v)
        return job

    async def create_research_task(self, task):
        self.tasks[task.id] = task
        return task

    async def get_research_task(self, task_id):
        return self.tasks.get(task_id)

    async def get_research_task_by_job(self, job_id):
        for t in self.tasks.values():
            if t.job_id == job_id:
                return t
        return None

    async def update_research_task(self, task_id, **fields):
        task = self.tasks.get(task_id)
        if task is None:
            return None
        for k, v in fields.items():
            setattr(task, k, v)
        return task


class _FakeProvider:
    """Responde por MARCADOR do system prompt (o pipeline v2 não é uma fila linear)."""

    def __init__(self, script: dict[str, str], models: list[str]):
        self.script = script
        self.models = models  # registra qual modelo o runner pediu em cada chamada

    async def complete(self, **kwargs):
        system = ""
        for msg in kwargs.get("messages") or []:
            if getattr(msg, "role", None) and "system" in str(msg.role).lower():
                system = msg.content
        self.models.append(kwargs.get("model"))
        text = next((v for k, v in self.script.items() if k in system), "(vazio)")

        class _R:
            def __init__(self, t):
                self._t = t

            def get_content(self):
                return self._t

        return _R(text)


class _FakeLLMRouter:
    def __init__(self, script: dict[str, str]):
        self.models_used: list[str] = []
        self.provider = _FakeProvider(script, self.models_used)

    def get_provider_for_model(self, model):
        return self.provider


class _FakeWebSearch:
    """Espelha a interface do WebSearchExecutor (search → payload, fetch → page)."""

    def __init__(self, slow=False):
        self.slow = slow

    async def search(self, query, *, recency, category, n):
        if self.slow:
            import asyncio

            await asyncio.sleep(5)
        return {
            "results": [
                {"title": "Fonte A", "url": "https://a.com/x", "snippet": "s"},
                {"title": "Fonte B", "url": "https://b.com/y", "snippet": "s"},
            ],
            "backend": "searxng",
        }

    async def fetch(self, url, *, max_chars):
        from modules.web_search import FetchedPage

        return FetchedPage(title=f"T {url}", text="Fato em 2026-07-10. " * 40, truncated=False, url=url)


def _research_job_pair(db_less=False):
    db = _FakeDB()
    job = AgentJob(kind="research", title="Pesquisa: t", spec_json={}, status="running")
    task = ResearchTask(job_id=job.id, topic="tópico t", depth="quick", status="queued")
    db.jobs[job.id] = job
    db.tasks[task.id] = task
    return db, job, task


_SCRIPT = {
    "Você PLANEJA uma pesquisa": json.dumps(
        {
            "title": "Relatório de teste",
            "sub_questions": [{"id": "q1", "question": "o que aconteceu?"}],
            "outline": [{"section": "Contexto", "covers": ["q1"]}],
            "queries": [
                {"query": "q1", "category": "general", "recency": "any", "sub_question": "q1"}
            ],
        }
    ),
    "Você tria resultados": json.dumps({"selected": [{"n": 1, "reason": "boa"}, {"n": 2, "reason": "boa"}]}),
    "Você extrai EVIDÊNCIA": json.dumps(
        {
            "evidence": [
                {"claim": "fato em 2026", "quote": "trecho", "url": "https://a.com/x", "sub_question": "q1"},
                {"claim": "outro fato", "quote": "trecho", "url": "https://b.com/y", "sub_question": "q1"},
            ]
        }
    ),
    "Você organiza a evidência": json.dumps(
        {"sections": [{"section": "Contexto", "evidence_ids": ["e1", "e2"], "convergences": [], "divergences": [], "links": []}]}
    ),
    "escrevendo UMA seção": "Análise cruzando [1] e [2].",
    "escreve o RESUMO EXECUTIVO": "<<<RESUMO>>>\nAchado central [1].\n<<<CONCLUSAO>>>\nFecho [2].",
    "professor universitário": json.dumps({"scores": {"depth": 4}, "issues": []}),
    "Você audita a COBERTURA": json.dumps({"queries": [], "new_sub_questions": []}),
}


# ── testes ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_research_job_completes_with_consistent_task():
    db, job, task = _research_job_pair()
    router = _FakeLLMRouter(_SCRIPT)
    runner = build_research_runner(
        db=db,
        llm_router=router,
        web_search=_FakeWebSearch(),
        sandbox=None,
        research_model="fake-flash",
        research_model_writer="fake-pro",
    )
    terminal = await runner(job)

    assert terminal["status"] == "completed"
    # O aviso no chat é o RESUMO EXECUTIVO (a v1 mandava as 12 primeiras linhas).
    assert "Achado central" in terminal["result_summary"]

    refreshed = db.tasks[task.id]
    assert refreshed.status == "completed"
    assert refreshed.report_markdown.startswith("# Relatório de teste")
    assert "## Contexto" in refreshed.report_markdown
    assert refreshed.backend == "searxng"
    assert len(refreshed.sources_json) == 2
    assert refreshed.stats_json["sources_used"] == 2
    assert refreshed.stats_json["writer_model"] == "fake-pro"
    assert refreshed.model == "fake-flash"
    assert refreshed.quality_json["validation"]["passed"] is True
    assert refreshed.report_path is None  # sem sandbox → best-effort vira None

    # Volume no modelo barato, escrita/crítica no forte.
    assert "fake-flash" in router.models_used and "fake-pro" in router.models_used


@pytest.mark.asyncio
async def test_research_timeout_marks_both_terminal(monkeypatch):
    from core.research import budgets as budgets_mod
    from core.research.types import Budget

    db, job, task = _research_job_pair()
    # Budget minúsculo: a busca fake (5s) estoura o timeout de 1s.
    monkeypatch.setitem(
        budgets_mod.BUDGETS, "quick", Budget(max_queries=1, max_fetches=2, max_rounds=1, timeout_s=1)
    )
    runner = build_research_runner(
        db=db,
        llm_router=_FakeLLMRouter(_SCRIPT),
        web_search=_FakeWebSearch(slow=True),
        sandbox=None,
    )
    terminal = await runner(job)
    assert terminal["status"] == "timeout"
    assert db.tasks[task.id].status == "timeout"


@pytest.mark.asyncio
async def test_research_job_without_task_fails_clean():
    db = _FakeDB()
    job = AgentJob(kind="research", title="Pesquisa: x", spec_json={}, status="running")
    db.jobs[job.id] = job
    runner = build_research_runner(
        db=db, llm_router=_FakeLLMRouter({}), web_search=_FakeWebSearch(), sandbox=None
    )
    terminal = await runner(job)
    assert terminal["status"] == "failed"


class _FakeJM:
    def __init__(self):
        self.poked = False
        self._active = {}

    def poke(self):
        self.poked = True


@pytest.mark.asyncio
async def test_deep_research_tool_creates_job_and_task():
    db = _FakeDB()
    jm = _FakeJM()
    user_id, session_id = uuid4(), uuid4()
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=user_id, session_id=session_id)

    result = await ex.execute_tool_call(
        ToolCall(name="deep_research", arguments={"topic": "história do SearXNG", "depth": "quick"})
    )
    assert result.error is None
    payload = result.result
    assert payload["ok"] is True
    assert payload["depth"] == "quick"
    job = db.jobs[UUID(payload["job_id"])]
    task = db.tasks[UUID(payload["research_id"])]
    assert job.kind == "research"
    assert task.job_id == job.id
    assert task.status == "queued"
    assert task.user_id == user_id
    assert jm.poked


@pytest.mark.asyncio
async def test_deep_research_requires_topic():
    ex = JobToolExecutor(db=_FakeDB(), job_manager=_FakeJM(), user_id=None, session_id=None)
    result = await ex.execute_tool_call(ToolCall(name="deep_research", arguments={}))
    assert result.result["ok"] is False


@pytest.mark.asyncio
async def test_cancel_queued_research_closes_task_too():
    db = _FakeDB()
    jm = _FakeJM()
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=None, session_id=None)
    created = await ex.execute_tool_call(
        ToolCall(name="deep_research", arguments={"topic": "x"})
    )
    job_id = created.result["job_id"]
    cancelled = await ex.execute_tool_call(
        ToolCall(name="job_cancel", arguments={"job_id": job_id})
    )
    assert cancelled.result["ok"] is True
    task = db.tasks[UUID(created.result["research_id"])]
    assert task.status == "cancelled"
