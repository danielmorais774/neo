"""O aviso por cima da página e o navegador certo para continuar (sessão cb41834a).

Portal, 10 questões. O agente respondeu as dez; o site mostrou por cima da
página "Você respondeu todas as questões, lembre-se de Finalizar o
questionário", e o modelo nunca leu isso: era o último texto do DOM e a leitura
estourava o teto antes. Ele repetiu a questão 10 até o PARE, e a visão ainda
disse que o Responder desabilitado estava habilitado. Depois, "monta um arquivo
com as questões" foi para o navegador sem janela, que não tem o login dele.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.driver import BrowserLocalDriver
from core.jobs.runner_browser_local import _resolver_aba

from .test_driver import _snapshot


# ── o aviso tem seção própria ───────────────────────────────────────────────


def test_aviso_novo_vem_antes_da_tela_e_diz_que_acabou_de_aparecer():
    texto = BrowserLocalDriver._render(
        _snapshot(
            tela="Questão 10 de 10\nAlternativas",
            avisos=[{
                "texto": "Finalizar questionário · Você respondeu todas as questões, "
                "lembre-se de Finalizar o questionário.",
                "nova": True,
            }],
        )
    )
    assert ">>> AVISO NA TELA — APARECEU AGORA" in texto
    assert "Você respondeu todas as questões" in texto
    assert texto.index("AVISO NA TELA") < texto.index("NA TELA AGORA")


def test_aviso_antigo_continua_aparecendo_sem_a_marca_de_novo():
    texto = BrowserLocalDriver._render(
        _snapshot(tela="x", avisos=[{"texto": "Novos avisos! Fale com o mediador", "nova": False}])
    )
    assert ">>> AVISO NA TELA — por cima da página" in texto
    assert "APARECEU AGORA" not in texto


def test_snapshot_sem_avisos_nao_muda_nada():
    assert "AVISO NA TELA" not in BrowserLocalDriver._render(_snapshot(tela="x"))


# ── a visão contra o DOM ────────────────────────────────────────────────────

#: A resposta real do `look` no passo 36 do job 21c563c1.
_VISAO_ERRADA = (
    "NÃO. Nenhuma alternativa está marcada na questão 10 (elementos 51 a 59). O "
    "botão Responder (60) está habilitado, com cor azul clara e texto visível — não "
    "está cinza ou desabilitado."
)


def _tela_da_q10():
    return _snapshot(elements=[
        {"ref": 10, "tag": "li", "text": "Questão 10"},
        {"ref": 54, "tag": "label", "text": "Alternativa 3: A exposição contínua", "checked": True},
        {"ref": 55, "tag": "label", "text": "", "checked": True},
        {"ref": 56, "tag": "label", "text": "Alternativa 4: As plataformas", "checked": False},
        {"ref": 60, "tag": "button", "text": "Responder", "disabled": True},
        {"ref": 61, "tag": "button", "text": "Finalizar"},
    ])


def test_visao_que_contradiz_o_dom_recebe_o_estado_do_dom():
    texto = BrowserLocalDriver._com_estado_do_dom(_VISAO_ERRADA, _tela_da_q10())
    assert texto.startswith(_VISAO_ERRADA), "a resposta da visão não é apagada"
    assert "[60] «Responder» DESABILITADO" in texto
    assert "[54] «Alternativa 3: A exposição contínua» MARCADO" in texto
    assert "[55] MARCADO" in texto
    assert "[10]" not in texto, "item sem estado não entra"


def test_resposta_que_nao_fala_de_estado_fica_como_veio():
    resposta = "SIM. O título da página é 'Questionário' e há 10 questões (1 a 10)."
    assert BrowserLocalDriver._com_estado_do_dom(resposta, _tela_da_q10()) == resposta


def test_preco_e_porcentagem_nao_viram_numero_de_elemento():
    resposta = "O botão está desabilitado; o preço é R$ 60,00 e a barra mostra 60% concluído."
    assert BrowserLocalDriver._com_estado_do_dom(resposta, _tela_da_q10()) == resposta


# ── continuar no navegador em que o trabalho está ───────────────────────────


class _DB:
    def __init__(self, jobs):
        self.jobs = jobs
        self.criados = []

    async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
        return self.jobs

    async def create_agent_job(self, job):
        if getattr(job, "id", None) is None:
            job.id = uuid4()
        self.criados.append(job)
        return job


def _anterior(session_id, *, tab, url, minutos, resultado=""):
    fim = datetime.utcnow() - timedelta(minutes=minutos)
    return SimpleNamespace(
        id=uuid4(), kind="browser_local", session_id=session_id, status="completed",
        created_at=fim - timedelta(minutes=1), finished_at=fim,
        spec_json={}, result_summary=resultado,
        extra_metadata={"browser_session": {"tab_ref": tab, "url": url, "device_id": "device-1"}},
    )


_PORTAL = "https://portal.exemplo.edu.br/#!/app/portal/aluno/questionario/384523"


async def _spec_da_tarefa(mensagem, anteriores, *, task=f"extrair as questões de {_PORTAL}"):
    from core.jobs.executor import JobToolExecutor

    session_id = anteriores[0].session_id if anteriores else uuid4()
    ex = JobToolExecutor(
        db=_DB(anteriores), job_manager=SimpleNamespace(poke=lambda: None),
        user_id=uuid4(), session_id=session_id, user_message=mensagem, device_id="device-1",
    )
    ex._enabled = True
    resultado = await ex._browser_local_task({"task": task, "title": "t"})
    assert resultado["ok"] is True, resultado
    return ex.db.criados[-1].spec_json


@pytest.fixture(autouse=True)
def _liga_o_local(monkeypatch):
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", True, raising=False)
    monkeypatch.setattr("shared.config.get_settings", lambda: settings)


@pytest.mark.asyncio
async def test_continuacao_no_mesmo_site_fica_no_navegador_dele():
    sessao = uuid4()
    spec = await _spec_da_tarefa(
        "consegue montar um arquivo com todas as questoes?",
        [_anterior(sessao, tab=1976622394, url=_PORTAL, minutos=1)],
    )
    assert spec["headless"] is False


@pytest.mark.asyncio
async def test_headless_que_parou_no_login_manda_a_proxima_para_o_navegador_dele():
    sessao = uuid4()
    spec = await _spec_da_tarefa(
        "vamos responder essa entao",
        [_anterior(sessao, tab=-1, url="https://portal.exemplo.edu.br/#!/access/login",
                   minutos=180, resultado="A aba está na tela de LOGIN do Portal")],
    )
    assert spec["headless"] is False


@pytest.mark.asyncio
async def test_janela_herdada_caduca():
    """Horas depois, sem ele pedir, não se toma a tela dele de novo."""
    sessao = uuid4()
    spec = await _spec_da_tarefa(
        "extrai as questões", [_anterior(sessao, tab=1976622394, url=_PORTAL, minutos=45)]
    )
    assert spec["headless"] is True


@pytest.mark.asyncio
async def test_outro_site_nao_herda():
    sessao = uuid4()
    spec = await _spec_da_tarefa(
        "procura o preço do teclado",
        [_anterior(sessao, tab=1976622394, url=_PORTAL, minutos=1)],
        task="preço do teclado em https://www.kabum.com.br",
    )
    assert spec["headless"] is True


@pytest.mark.asyncio
async def test_headless_que_funcionou_continua_headless():
    sessao = uuid4()
    spec = await _spec_da_tarefa(
        "agora ordena por preço",
        [_anterior(sessao, tab=-3, url="https://www.kabum.com.br/busca/teclado", minutos=2)],
        task="ordenar por preço em https://www.kabum.com.br/busca/teclado",
    )
    assert spec["headless"] is True


# ── a aba reaproveitada é do mesmo navegador ────────────────────────────────


def _job_atual():
    return SimpleNamespace(id=uuid4(), user_id=uuid4())


class _DBAbas:
    def __init__(self, jobs):
        self.jobs = jobs

    async def list_agent_jobs(self, user_id, limit=20):
        return self.jobs


@pytest.mark.asyncio
async def test_tarefa_headless_nao_reaproveita_a_aba_do_navegador_dele():
    anterior = _anterior(uuid4(), tab=1976622394, url=_PORTAL, minutos=1)
    assert await _resolver_aba(_DBAbas([anterior]), _job_atual(), "device-1", {"headless": True}) is None


@pytest.mark.asyncio
async def test_tarefa_com_janela_nao_reaproveita_a_aba_do_headless():
    anterior = _anterior(uuid4(), tab=-2, url=_PORTAL, minutos=1)
    assert await _resolver_aba(_DBAbas([anterior]), _job_atual(), "device-1", {"headless": False}) is None


@pytest.mark.asyncio
async def test_mesmo_navegador_continua_reaproveitando():
    anterior = _anterior(uuid4(), tab=1976622394, url=_PORTAL, minutos=1)
    assert await _resolver_aba(_DBAbas([anterior]), _job_atual(), "device-1", {}) == 1976622394
