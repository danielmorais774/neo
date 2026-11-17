"""O vigia de download — o laço que espera SEM gastar passo do agente.

A ideia veio do usuário: *"eu só pedi para ele baixar; podia já dizer concluída
com o download em andamento… ai poderia ser um cron em código mesmo, se o status
muda ele chama o llm para dizer qual o status"*. É exatamente isto: o navegador
termina quando o download COMEÇA, e daqui em diante quem olha é código puro. O
LLM só volta uma vez, no fim, para redigir o aviso (quem faz isso é o
``ProactiveDispatcher`` de sempre, em cima do ``result_summary``).

O que está sob teste é o veredito: cada desfecho tem que sair da evidência do
disco, não de suposição.
"""

from __future__ import annotations

import uuid

import pytest

from core.jobs.runner_download_watch import build_download_watch_runner
from db.models.agent_job import AgentJob


class _FakeDB:
    def __init__(self) -> None:
        self.notas: list[str] = []

    async def update_agent_job(self, job_id, **campos):
        if campos.get("progress_note"):
            self.notas.append(campos["progress_note"])


def _job(**over):
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        kind="download_watch",
        title="Acompanhar o download",
        spec_json={"device_id": "dev-1", "name": "Print.zip.crdownload"},
        status="running",
    )
    base.update(over)
    return AgentJob(**base)


def _canal(monkeypatch, respostas):
    """Substitui o broadcaster do device por um que devolve a fila."""
    fila = list(respostas)

    class _B:
        async def request(self, device_id, action, args, timeout=None):
            return fila.pop(0) if len(fila) > 1 else fila[0]

    import core.notifications.device_control as dc

    monkeypatch.setattr(dc, "get_device_control_broadcaster", lambda: _B())


def _arquivos(*files):
    return {"ok": True, "result": {"files": list(files)}}


_VINDO = {"name": "Print.zip.crdownload", "bytes": 1_000_000, "seconds_ago": 2, "incomplete": True}
_MAIOR = {"name": "Print.zip.crdownload", "bytes": 5_000_000, "seconds_ago": 1, "incomplete": True}
_PRONTO = {"name": "Print.zip", "bytes": 8_000_000, "seconds_ago": 1, "incomplete": False}


@pytest.mark.asyncio
async def test_arquivo_que_termina_vira_aviso_de_conclusao(monkeypatch):
    """O nome muda quando o navegador tira o `.crdownload` — a busca é por prefixo."""
    _canal(monkeypatch, [_arquivos(_VINDO), _arquivos(_PRONTO)])
    runner = build_download_watch_runner(db=_FakeDB(), poll_s=0)
    terminal = await runner(_job())
    assert terminal["status"] == "completed"
    assert "Print.zip" in terminal["result_summary"]
    assert "7.6 MB" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_arquivo_que_some_e_download_cancelado(monkeypatch):
    _canal(monkeypatch, [_arquivos(_VINDO), _arquivos()])
    runner = build_download_watch_runner(db=_FakeDB(), poll_s=0)
    terminal = await runner(_job())
    assert terminal["status"] == "failed"
    assert "cancelado" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_tamanho_que_nao_anda_e_declarado_travado(monkeypatch):
    """Parado é diferente de lento: o veredito olha o TAMANHO, não o relógio."""
    _canal(monkeypatch, [_arquivos(_VINDO)])
    runner = build_download_watch_runner(db=_FakeDB(), poll_s=0, stall_s=0)
    terminal = await runner(_job())
    assert terminal["status"] == "failed"
    assert "travou" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_download_que_avanca_nao_e_confundido_com_travado(monkeypatch):
    _canal(monkeypatch, [_arquivos(_VINDO), _arquivos(_MAIOR), _arquivos(_PRONTO)])
    runner = build_download_watch_runner(db=_FakeDB(), poll_s=0, stall_s=1)
    terminal = await runner(_job())
    assert terminal["status"] == "completed"


@pytest.mark.asyncio
async def test_tempo_esgotado_nao_afirma_fracasso_nem_sucesso(monkeypatch):
    _canal(monkeypatch, [_arquivos(_MAIOR), _arquivos(_VINDO), _arquivos(_MAIOR)])
    runner = build_download_watch_runner(db=_FakeDB(), poll_s=0, default_timeout_s=0)
    terminal = await runner(_job(spec_json={"device_id": "d", "name": "Print.zip.crdownload"}))
    assert terminal["status"] == "failed"
    assert "não cancelei nada" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_progresso_vai_para_o_job_enquanto_baixa(monkeypatch):
    db = _FakeDB()
    _canal(monkeypatch, [_arquivos(_VINDO), _arquivos(_MAIOR), _arquivos(_PRONTO)])
    runner = build_download_watch_runner(db=db, poll_s=0, stall_s=99)
    await runner(_job())
    assert any("MB baixados" in nota for nota in db.notas)


@pytest.mark.asyncio
async def test_maquina_offline_admite_que_nao_sabe(monkeypatch):
    from core.notifications.device_control import DeviceOffline

    class _B:
        async def request(self, *a, **kw):
            raise DeviceOffline("desktop fechado")

    import core.notifications.device_control as dc

    monkeypatch.setattr(dc, "get_device_control_broadcaster", lambda: _B())
    runner = build_download_watch_runner(db=_FakeDB(), poll_s=0)
    terminal = await runner(_job())
    assert terminal["status"] == "failed"
    assert "Não sei dizer" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_spec_sem_device_ou_nome_nao_roda(monkeypatch):
    runner = build_download_watch_runner(db=_FakeDB(), poll_s=0)
    terminal = await runner(_job(spec_json={"device_id": "d"}))
    assert terminal["status"] == "failed"
    assert "sem device ou arquivo" in terminal["result_summary"]
