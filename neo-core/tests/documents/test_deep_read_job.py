"""deep_read como job: enfileiração, recusas imediatas, manager e runner.

O runner roda com o extrator de verdade (sandbox em disco, ver
``test_deep_reader``) e um provider de LLM falso — é o caminho inteiro que o
JobManager executa em produção, menos a rede.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from core.jobs.executor import JobToolExecutor
from core.jobs.manager import JobManager
from core.jobs.runner_deep_read import build_deep_read_runner
from db.models.agent_job import AgentJob
from shared import ToolCall
from tests.documents.test_deep_reader import _DiskSandbox, _paginas, _ReaderLLM, _txt_as_pages, fake_pdftotext  # noqa: F401
from tests.test_jobs import _FakeDB


class _JM:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.poked = 0

    def poke(self):
        self.poked += 1


class _PathSandbox(_DiskSandbox):
    def resolve_path(self, path):
        from core.workbench.sandbox import WorkbenchSandbox

        return WorkbenchSandbox().resolve_path(path)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "Relatorio_Charlie.pdf").write_text("x")
    (tmp_path / "files" / "Echo.pdf").write_text("x")
    return tmp_path


def _executor(db, sandbox, session_id=None, user_id=None):
    return JobToolExecutor(
        db=db, job_manager=_JM(sandbox), user_id=user_id or uuid4(), session_id=session_id or uuid4()
    )


async def _call(ex, **args):
    return (await ex.execute_tool_call(ToolCall(id="1", name="deep_read", arguments=args))).result


@pytest.mark.asyncio
async def test_enfileira_com_pasta_propria_e_then(ws):
    db = _FakeDB()
    ex = _executor(db, _PathSandbox(ws))
    out = await _call(
        ex,
        files=["/workspace/files/Relatorio_Charlie.pdf", "Echo.pdf"],
        questions=["aumento de carga tributária", "regulação"],
        goal="comparar planos",
        title="Relatórios anuais 2026",
        then="leia a sintese.md e escreva a conclusão",
    )
    assert out["ok"] is True, out
    job = next(iter(db.jobs.values()))
    assert job.kind == "deep_read" and job.status == "queued"
    assert out["pasta"] == f"/workspace/analises/relatorios-anuais-2026-{str(job.id)[:8]}"
    assert job.spec_json["out_dir"] == out["pasta"]
    assert job.spec_json["files"] == [
        {"path": "/workspace/files/Relatorio_Charlie.pdf"}, {"path": "/workspace/files/Echo.pdf"},
    ]
    assert job.spec_json["then"].startswith("leia a sintese")
    assert ex.jm.poked == 1
    assert "NÃO leia estes documentos você mesmo" in out["note"]


@pytest.mark.asyncio
async def test_caminho_do_disco_do_usuario_e_recusado_na_hora(ws):
    db = _FakeDB()
    out = await _call(
        _executor(db, _PathSandbox(ws)),
        files=["/Users/daniel/Documents/analises/relatorios-anuais-2026/Relatorio_Foxtrot.pdf"],
        questions=["regulação"], goal="x", title="x",
    )
    assert out["ok"] is False and not db.jobs
    assert "request_user_file" in out["problemas"][0]


@pytest.mark.asyncio
async def test_arquivo_que_nao_esta_no_sandbox_e_recusado_e_nada_enfileira(ws):
    """Sessão f33632b4: o Foxtrot nunca subiu. Aqui isso aparece ANTES de ler os outros."""
    db = _FakeDB()
    out = await _call(
        _executor(db, _PathSandbox(ws)),
        files="/workspace/files/Relatorio_Charlie.pdf\n/workspace/files/Relatorio_Foxtrot.pdf",
        questions=["regulação"], goal="x", title="x",
    )
    assert out["ok"] is False and not db.jobs
    assert any("Relatorio_Foxtrot.pdf" in p for p in out["problemas"])


@pytest.mark.asyncio
async def test_sem_perguntas_nao_enfileira(ws):
    out = await _call(_executor(_FakeDB(), _PathSandbox(ws)), files=["Echo.pdf"], goal="x", title="x")
    assert out["ok"] is False and "questions" in out["error"]


@pytest.mark.asyncio
async def test_mesma_leitura_repetida_na_sessao_reusa_o_job(ws):
    db = _FakeDB()
    sessao, usuario = uuid4(), uuid4()
    args = dict(files=["Echo.pdf"], questions=["regulação"], goal="x", title="x")
    primeiro = await _call(_executor(db, _PathSandbox(ws), sessao, usuario), **args)
    segundo = await _call(_executor(db, _PathSandbox(ws), sessao, usuario), **args)
    assert segundo["reused"] is True and segundo["job_id"] == primeiro["job_id"]
    assert len(db.jobs) == 1


@pytest.mark.asyncio
async def test_manager_entrega_o_kind_ao_runner():
    visto = []

    async def runner(job):
        visto.append(job.kind)
        return {"status": "completed", "result_summary": "ok"}

    db = _FakeDB()
    jm = JobManager(db=db, sandbox=None, deep_read_runner=runner, tick_interval=1)
    job = await db.create_agent_job(AgentJob(kind="deep_read", title="t", spec_json={}, status="running"))
    await jm._run_job(job)
    assert visto == ["deep_read"] and db.jobs[job.id].status == "completed"


# ── runner ponta a ponta ────────────────────────────────────────────────────


class _Resp:
    def __init__(self, text):
        self.text = text

    def get_content(self):
        return self.text


class _Provider:
    def __init__(self):
        self.reader = _ReaderLLM()
        self.kwargs = []

    async def complete(self, *, messages, model, temperature, max_tokens, **kwargs):
        self.kwargs.append(kwargs)
        return _Resp(await self.reader.complete(messages[0].content, messages[1].content, max_tokens=max_tokens))


class _Router:
    def __init__(self):
        self.provider = _Provider()

    def get_provider(self, name=None):
        return self.provider


class _WM:
    def __init__(self):
        self.writes = []

    async def write(self, **kw):
        self.writes.append(kw)


@pytest.mark.asyncio
async def test_runner_le_escreve_e_devolve_so_ponteiro(ws, fake_pdftotext):
    a = _txt_as_pages(ws, "Plano_A.pdf", _paginas(5, "herança"))
    db, router, wm = _FakeDB(), _Router(), _WM()
    job = await db.create_agent_job(AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="deep_read", title="Leitura: Planos",
        spec_json={
            "goal": "comparar", "questions": ["carga tributária", "regulação"],
            "files": [{"path": a}], "out_dir": "/workspace/analises/planos-12345678",
        },
        status="running",
    ))
    runner = build_deep_read_runner(
        db=db, llm_router=router, sandbox=_DiskSandbox(ws),
        get_working_memory=lambda: wm, chunk_chars=4000,
    )
    terminal = await runner(job)

    assert terminal["status"] == "completed"
    resumo = terminal["result_summary"]
    assert "/workspace/analises/planos-12345678/" in resumo and "sintese.md" in resumo
    assert "imposto sobre herança" not in resumo
    assert (ws / "analises" / "planos-12345678" / "sintese.md").is_file()
    # Leitura de JSON sem raciocínio: é o que impede o content vazio.
    assert all(k.get("disable_thinking") is True and k.get("response_format") for k in router.provider.kwargs)

    # Caderno: uma nota SUBSTITUÍDA a cada marco, só com endereço e status.
    assert wm.writes and all(w["mode"] == "replace" for w in wm.writes)
    assert {w["slug"] for w in wm.writes} == {"leitura-planos-12345678"}
    ultima = wm.writes[-1]
    assert "1/1 feitos" in ultima["content"] and "imposto" not in ultima["content"]
    assert json.loads((ws / "analises" / "planos-12345678" / "estado.json").read_text())["status"] == "completed"


# ── toolset do cérebro ──────────────────────────────────────────────────────


def _svc_para_toolset(*, flag: bool, runner: bool):
    from types import SimpleNamespace

    from modules.desktop.ai_service import AIService

    svc = AIService.__new__(AIService)
    svc.settings = SimpleNamespace(
        deep_read_enabled=flag, research_enabled=True, web_search_enabled=True,
        browser_agent_enabled=True, browser_local_enabled=True,
    )
    svc.job_manager = SimpleNamespace(_deep_read_runner=(lambda job: None) if runner else None)
    svc.agent = SimpleNamespace(tool_registry=SimpleNamespace(exists=lambda name: True))
    return svc


@pytest.mark.parametrize("flag,runner,esperado", [(True, True, True), (False, True, False), (True, False, False)])
def test_deep_read_so_entra_no_toolset_quando_roda_de_verdade(flag, runner, esperado):
    svc = _svc_para_toolset(flag=flag, runner=runner)
    nomes = {t.name for t in svc._agent_action_tools()}
    assert ("deep_read" in nomes) is esperado
    assert svc._deep_read_available() is esperado
