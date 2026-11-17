"""O que o agente descobriu antes de morrer chega até o usuário.

Job 36a81dd6 (2026-09-20). O `neo-browser-agent`, ao estourar o prazo, devolvia
só ``{"ok": false, "error": "timeout depois de 570s"}`` — com o objeto do agente
na mão e ``shots.history_of(agent)`` sendo usado CINCO LINHAS ACIMA para tirar
os prints. Toda a memória acumulada do agente ("tentei cdn.exemplo.gov.br → 403,
dadosabertos → Access Denied, DuckDuckGo → CAPTCHA…") ia para o lixo, e o
usuário recebia um fracasso mudo.

Agora o serviço manda `diagnosis` junto, o runner costura no resumo, e — quando
o padrão é bloqueio por ORIGEM — pendura a instrução de perguntar sobre o
navegador local em vez de insistir no servidor.
"""

import asyncio
from uuid import uuid4

import httpx
import pytest

from core.jobs.runner_browser import build_browser_runner
from db.models.agent_job import AgentJob

DIAGNOSTICO = (
    "Chegou ao passo 22 antes de ser interrompido.\n"
    "O que o agente já tinha tentado: Tentativas falhas: 1) cdn.exemplo.gov.br → 403. "
    "2) dados.exemplo.gov.br → Access Denied (EdgeSuite). 3) DuckDuckGo → CAPTCHA. "
    "4) Google → CAPTCHA. 5) www.exemplo.gov.br → Access Denied."
)


class _FakeDB:
    def __init__(self):
        self.jobs = {}
        self.updates: list[tuple] = []

    async def update_agent_job(self, job_id, **fields):
        self.updates.append((job_id, fields))
        job = self.jobs.get(job_id)
        for k, v in fields.items():
            setattr(job, k, v)
        return job


def _job(db):
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="browser",
        title="Baixar relatórios anuais 2026 (portal de dados)",
        spec_json={"task": "baixar o zip do portal de dados", "session": "user-default"},
        status="running",
    )
    db.jobs[job.id] = job
    return job


def _client_devolvendo(payload):
    class _Resp:
        status_code = 200

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            await asyncio.sleep(0)
            return _Resp()

    return _Client


@pytest.mark.asyncio
async def test_diagnostico_do_agente_entra_no_resumo(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client_devolvendo({
            "ok": False,
            "error": "timeout depois de 570s",
            "diagnosis": DIAGNOSTICO,
        }),
    )
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100")

    terminal = await runner(_job(db))

    assert terminal["status"] == "failed"
    resumo = terminal["result_summary"]
    assert "timeout depois de 570s" in resumo, "o erro curto continua lá"
    assert "passo 22" in resumo, "quantos passos rodou"
    assert "cdn.exemplo.gov.br" in resumo, "o que o agente já tinha tentado"


@pytest.mark.asyncio
async def test_bloqueio_por_origem_vira_pergunta_sobre_o_navegador_local(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client_devolvendo({
            "ok": False,
            "error": "timeout depois de 570s",
            "diagnosis": DIAGNOSTICO,
        }),
    )
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100")

    resumo = (await runner(_job(db)))["result_summary"]

    assert "datacenter" in resumo, "tem que dizer POR QUE falhou"
    assert "local" in resumo.lower(), "tem que apontar a saída"
    assert "PERGUNTE" in resumo, "e nunca tomar a tela dele por conta própria"


@pytest.mark.asyncio
async def test_sem_diagnostico_o_comportamento_e_o_de_antes(monkeypatch):
    """Serviço antigo (sem o campo) não pode quebrar o runner."""
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client_devolvendo({"ok": False, "error": "Chromium morreu"}),
    )
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100")

    terminal = await runner(_job(db))

    assert terminal["status"] == "failed"
    assert terminal["result_summary"] == "A tarefa de browser falhou: Chromium morreu"


@pytest.mark.asyncio
async def test_falha_comum_nao_ganha_sugestao_de_navegador(monkeypatch):
    """Timeout sem assinatura de bloqueio continua sendo só um timeout."""
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client_devolvendo({
            "ok": False,
            "error": "timeout depois de 570s",
            "diagnosis": "Chegou ao passo 12 antes de ser interrompido.\n"
                         "O que o agente já tinha tentado: o botão de download "
                         "não aparece na página.",
        }),
    )
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100")

    resumo = (await runner(_job(db)))["result_summary"]

    assert "botão de download" in resumo
    assert "PERGUNTE" not in resumo
    assert "datacenter" not in resumo


@pytest.mark.asyncio
async def test_sucesso_continua_sucesso(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client_devolvendo({"ok": True, "success": True, "final_result": "baixado"}),
    )
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100")

    terminal = await runner(_job(db))

    assert terminal["status"] == "completed"
    assert "PERGUNTE" not in terminal["result_summary"]
