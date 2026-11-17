"""Uma tarefa por sessão de browser de cada vez — e o prazo só corre na vez dela.

session-30b1e484: cinco jobs do mesmo site na sessão ``user-default``. O
JobManager rodou três juntos, os três foram direto para o POST e esperaram na
fila do container com o timeout do httpx correndo: dois estouraram os 600s sem
nunca terem começado e o quinto tomou 429 "fila cheia".
"""

import asyncio
from uuid import uuid4

import httpx
import pytest

from core.jobs.runner_browser import build_browser_runner
from db.models.agent_job import AgentJob


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


def _job(db, session="user-default"):
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="browser", title="T",
        spec_json={"task": "abrir o site", "session": session}, status="running",
    )
    db.jobs[job.id] = job
    return job


def _container_em_serie(estado):
    """Container falso que mede quantas tarefas estão DENTRO dele ao mesmo tempo."""

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "success": True, "final_result": "feito"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            estado["dentro"] += 1
            estado["pico"] = max(estado["pico"], estado["dentro"])
            estado["urls"].append(url)
            await asyncio.sleep(0.05)
            estado["dentro"] -= 1
            return _Resp()

    return _Client


@pytest.mark.asyncio
async def test_mesma_sessao_espera_a_vez_no_core(monkeypatch):
    estado = {"dentro": 0, "pico": 0, "urls": []}
    monkeypatch.setattr(httpx, "AsyncClient", _container_em_serie(estado))
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100")
    jobs = [_job(db) for _ in range(3)]

    terminais = await asyncio.gather(*(runner(j) for j in jobs))

    assert [t["status"] for t in terminais] == ["completed"] * 3
    assert estado["pico"] == 1, "duas tarefas da mesma sessão entraram juntas no container"
    notas = [f.get("progress_note") or "" for _, f in db.updates]
    assert any(n.startswith("Na fila") for n in notas), "quem espera tem que dizer que espera"


@pytest.mark.asyncio
async def test_prazo_so_e_gravado_quando_chega_a_vez(monkeypatch):
    """O `timeout_at` de quem esperou nasce DEPOIS do fim do anterior."""
    estado = {"dentro": 0, "pico": 0, "urls": []}
    monkeypatch.setattr(httpx, "AsyncClient", _container_em_serie(estado))
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100", timeout_s=600)
    primeiro, segundo = _job(db), _job(db)

    await asyncio.gather(runner(primeiro), runner(segundo))

    prazos = {jid: f["timeout_at"] for jid, f in db.updates if "timeout_at" in f}
    assert set(prazos) == {primeiro.id, segundo.id}
    assert prazos[segundo.id] > prazos[primeiro.id]


@pytest.mark.asyncio
async def test_sessoes_diferentes_nao_se_esperam(monkeypatch):
    estado = {"dentro": 0, "pico": 0, "urls": []}
    monkeypatch.setattr(httpx, "AsyncClient", _container_em_serie(estado))
    db = _FakeDB()
    runner = build_browser_runner(db=db, base_url="http://browser:5100")

    await asyncio.gather(runner(_job(db, "user-default")), runner(_job(db, "compras")))

    assert estado["pico"] == 2
