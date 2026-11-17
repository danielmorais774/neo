"""A promessa que depende do job vira o `then` dele, e roda quando ele termina.

O caso (job b8648f0e, 2026-11-02): "compara os 3 anúncios… no fim me diz qual
compensa mais". O cérebro abriu o job do navegador e respondeu "te aviso com o
veredito de qual compensa assim que terminar". O verificador de promessa leu
promessa pendente, rodou a continuação NA HORA — ela só pôde consultar o status
e mandar "Ainda rodando" — e, quando o job acabou, ninguém ficou encarregado do
veredito: a mensagem final listou os anúncios e não disse qual compensava.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.jobs.manager import JobManager
from core.workbench.sandbox import SandboxResult
from db.models.agent_job import AgentJob
from modules.desktop.ai_service import AIService, _ultimo_job_sem_continuacao

from tests.test_jobs import _FakeDB, _RecDispatcher, _WritableSandbox


def _job(kind="browser_local", status="running", spec=None):
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind=kind, title="Comparar AMD BC-250",
        spec_json=dict(spec or {"task": "compare"}), status=status,
    )
    job.id = uuid4()
    return job


def test_o_job_aberto_sem_continuacao_e_o_mais_recente():
    trace = [
        {"tool": "neo_bash_background", "async_job": True, "job_id": "j1", "then": True},
        {"tool": "browser_local_task", "async_job": True, "job_id": "j2", "then": False},
        {"tool": "search_pks", "ok": True},
    ]
    assert _ultimo_job_sem_continuacao(trace) == "j2"
    assert _ultimo_job_sem_continuacao([trace[0]]) is None
    assert _ultimo_job_sem_continuacao(None) is None


@pytest.mark.asyncio
async def test_pendura_no_job_que_ainda_roda_e_nao_no_que_ja_acabou():
    db = _FakeDB()
    rodando, acabou, com_then = _job(), _job(status="completed"), _job(spec={"then": "outra coisa"})
    for j in (rodando, acabou, com_then):
        db.jobs[j.id] = j
    servico = SimpleNamespace(db=db)

    ordem = "comparar os 3 anúncios e dizer qual compensa mais"
    assert await AIService._pendurar_no_job(servico, str(rodando.id), ordem) is True
    assert db.jobs[rodando.id].spec_json == {"task": "compare", "then": ordem}
    assert await AIService._pendurar_no_job(servico, str(acabou.id), ordem) is False
    assert await AIService._pendurar_no_job(servico, str(com_then.id), ordem) is False
    assert await AIService._pendurar_no_job(servico, "não-é-uuid", ordem) is False


@pytest.mark.asyncio
async def test_then_pendurado_depois_do_inicio_vira_continuacao_e_os_itens_continuam_saindo():
    db = _FakeDB()
    dispatcher = _RecDispatcher()
    jm = JobManager(db=db, sandbox=_WritableSandbox(SandboxResult(0, "", "")), dispatcher=dispatcher, tick_interval=1)
    jm.set_scheduled_runner(lambda job: None)
    # O objeto em memória é de quando o job foi reivindicado: sem `then`.
    em_memoria = _job()
    db.jobs[em_memoria.id] = _job()
    db.jobs[em_memoria.id].id = em_memoria.id
    db.jobs[em_memoria.id].spec_json = {"task": "compare", "then": "diga qual compensa mais"}

    await jm._settle(em_memoria, {"status": "completed", "result_summary": "- Placa R$1.979,61\n- Memória R$4.379,80"})

    cont = [j for j in db.jobs.values() if j.kind == "scheduled_prompt"]
    assert len(cont) == 1
    texto = cont[0].spec_json["action"]["texto"]
    assert "diga qual compensa mais" in texto and "R$1.979,61" in texto
    assert "JÁ foram entregues ao usuário" in texto
    # No navegador os itens são a entrega: a mensagem do job NÃO é silenciada.
    assert db.jobs[em_memoria.id].notify_policy != "silent"


@pytest.mark.asyncio
async def test_fora_do_navegador_o_job_continua_silenciado_pela_continuacao():
    db = _FakeDB()
    jm = JobManager(db=db, sandbox=_WritableSandbox(SandboxResult(0, "", "")), dispatcher=_RecDispatcher(), tick_interval=1)
    jm.set_scheduled_runner(lambda job: None)
    job = _job(kind="sandbox_task", spec={"command": "python3 ocr.py", "then": "entregue o .txt"})
    db.jobs[job.id] = job
    await jm._settle(job, {"status": "completed", "result_summary": "RESULT {}"})
    assert job.notify_policy == "silent"
    texto = next(j for j in db.jobs.values() if j.kind == "scheduled_prompt").spec_json["action"]["texto"]
    assert "JÁ foram entregues" not in texto


@pytest.mark.asyncio
async def test_browser_local_task_guarda_o_then(monkeypatch):
    from tests.browser_local.test_gate_dispatch import _executor

    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", True, raising=False)
    monkeypatch.setattr("shared.config.get_settings", lambda: settings)
    ex = _executor("no meu navegador, compara os 3 anúncios e diz qual compensa")
    r = await ex._browser_local_task({
        "task": "comparar os 3 anúncios", "title": "t",
        "then": "compare os 3 anúncios e diga qual compensa mais",
    })
    assert r["ok"] is True
    assert ex.db.criados[-1].spec_json["then"] == "compare os 3 anúncios e diga qual compensa mais"


def test_a_ferramenta_oferece_o_then():
    from core.jobs.tools import JOB_TOOLS

    tool = next(t for t in JOB_TOOLS if t.name == "browser_local_task")
    assert "then" in tool.parameters
    assert "qual compensa mais" in tool.parameters["then"].description


# ── o bloco [[HANDOFF:continuar]] escrito pelo cérebro (session-bd44cc03) ──────


def _servico(db):
    servico = SimpleNamespace(db=db)
    servico._pendurar_no_job = lambda *a, **k: AIService._pendurar_no_job(servico, *a, **k)
    return servico


def _continuar(texto):
    from core.pipeline.types import Handoff
    from shared.types import IntentType

    return Handoff(target_intent=IntentType.COGNITIVE, input_text=texto, is_self=True)


@pytest.mark.asyncio
async def test_continuar_escrito_com_job_aberto_espera_o_job_em_vez_de_rodar_agora():
    """O turno passou `then` ao job E emitiu o bloco; a continuação rodou na hora e
    respondeu "Ainda rodando" 4 s depois de "Rodando de novo"."""
    from core.pipeline.types import Handoff
    from shared.types import IntentType

    db = _FakeDB()
    job = _job(spec={"task": "compare", "then": "Resuma os preços e diga o menor preço por GB."})
    db.jobs[job.id] = job
    trace = [{"tool": "browser_local_task", "async_job": True, "job_id": str(job.id), "then": True}]
    ordem = "quando o job terminar, calcular o preço por GB de cada variação e dizer qual anúncio vence"
    musica = Handoff(target_intent=IntentType.MUSIC, input_text="tocar Duality", target_skill="music")

    restantes = await AIService._continuar_depois_do_job(_servico(db), [_continuar(ordem), musica], trace)

    assert restantes == [musica]  # só a autocontinuação espera; o resto do turno segue
    assert db.jobs[job.id].spec_json["then"] == f"Resuma os preços e diga o menor preço por GB.\n{ordem}"


@pytest.mark.asyncio
async def test_sem_then_combinado_a_ordem_vira_o_then_e_o_verificador_fica_sabendo():
    from modules.desktop.ai_service import _open_jobs

    db = _FakeDB()
    job = _job()
    db.jobs[job.id] = job
    trace = [{"tool": "browser_local_task", "async_job": True, "job_id": str(job.id), "then": False}]

    restantes = await AIService._continuar_depois_do_job(_servico(db), [_continuar("compare os 3")], trace)

    assert restantes == []
    assert db.jobs[job.id].spec_json["then"] == "compare os 3"
    # É por esta marca que o verificador não sintetiza outra continuação para já.
    assert _open_jobs(trace)[1] is True


@pytest.mark.asyncio
async def test_job_que_ja_acabou_ou_turno_sem_job_nao_seguram_a_continuacao():
    db = _FakeDB()
    acabou = _job(status="completed")
    db.jobs[acabou.id] = acabou
    h = _continuar("baixar a próxima página")

    trace = [{"tool": "neo_bash_background", "async_job": True, "job_id": str(acabou.id)}]
    assert await AIService._continuar_depois_do_job(_servico(db), [h], trace) == [h]
    assert "then" not in db.jobs[acabou.id].spec_json
    assert await AIService._continuar_depois_do_job(_servico(db), [h], [{"tool": "search_pks"}]) == [h]


@pytest.mark.asyncio
async def test_juntar_nao_repete_a_ordem_que_ja_esta_combinada():
    db = _FakeDB()
    job = _job(spec={"task": "compare", "then": "Compare os 3 anúncios e diga qual compensa mais."})
    db.jobs[job.id] = job
    assert await AIService._pendurar_no_job(
        _servico(db), str(job.id), "compare os 3 anúncios e diga qual compensa mais", juntar=True
    ) is True
    assert db.jobs[job.id].spec_json["then"] == "Compare os 3 anúncios e diga qual compensa mais."


# ── turno RETOMADO (sessão be8d195e) ───────────────────────────────────────
#
# `ls` da foto aprovado → o turno parou no cliente e foi retomado com
# `enable_handoff=False`. O job "só mapear" abriu na retomada, a resposta prometeu
# "te aviso com a lista do que pretendo mudar", e o bloco do verificador nunca
# rodou: o job acabou em 26 s e ninguém voltou.

_RESPOSTA_BE8D = (
    "Abrindo o GeekHunter na sua tela agora — a aba vai aparecer e você acompanha cada "
    "passo.\n\nPrimeiro eu mapeio o perfil atual e só depois mexo. Quando terminar o "
    "mapeamento eu te aviso aqui com a lista, e em seguida eu vou gravar as alterações "
    "no perfil."
)
# (A resposta REAL de be8d195e — "Primeiro eu mapeio… e só depois mexo" — nem passa
# no pré-filtro de promessa. Esse caso quem fecha é a recusa da tarefa só-leitura,
# core/browser_local/pedido.py; aqui o que se testa é o buraco da retomada.)


def _servico_da_retomada(db, synth_texto):
    chamadas = []

    async def _verificar(user_message, reply, note, dispatched):
        chamadas.append(dispatched)
        return SimpleNamespace(input_text=synth_texto) if synth_texto else None

    servico = SimpleNamespace(db=db, _verify_promise_continuation=_verificar)
    servico._pendurar_no_job = lambda job_id, ordem, **kw: AIService._pendurar_no_job(
        servico, job_id, ordem, **kw
    )
    return servico, chamadas


@pytest.mark.asyncio
async def test_retomada_pendura_a_promessa_no_job_que_ela_abriu():
    db = _FakeDB()
    job = _job()
    db.jobs[job.id] = job
    servico, chamadas = _servico_da_retomada(db, "aplicar as mudanças do currículo no perfil")
    trace = [{"tool": "browser_local_task", "async_job": True, "job_id": str(job.id), "then": False}]

    ok = await AIService._pendurar_promessa_da_retomada(
        servico, "atualize meu perfil", _RESPOSTA_BE8D, trace, {}, None
    )
    assert ok is True
    assert db.jobs[job.id].spec_json["then"] == "aplicar as mudanças do currículo no perfil"
    assert chamadas and "job em segundo plano" in chamadas[0][0]


@pytest.mark.asyncio
async def test_retomada_sem_job_so_com_aviso_ou_com_then_nao_pendura():
    db = _FakeDB()
    job = _job()
    db.jobs[job.id] = job
    servico, chamadas = _servico_da_retomada(db, "qualquer coisa")
    aberto = [{"tool": "browser_local_task", "async_job": True, "job_id": str(job.id), "then": False}]

    # sem job aberto
    assert not await AIService._pendurar_promessa_da_retomada(servico, "x", _RESPOSTA_BE8D, [], {}, None)
    # só o "te aviso quando terminar": quem avisa é o JobManager
    so_aviso = "Abri a tarefa no seu navegador. Te aviso quando terminar."
    assert not await AIService._pendurar_promessa_da_retomada(servico, "x", so_aviso, aberto, {}, None)
    # job já com continuação combinada
    com_then = [{**aberto[0], "then": True}]
    assert not await AIService._pendurar_promessa_da_retomada(servico, "x", _RESPOSTA_BE8D, com_then, {}, None)
    assert chamadas == []
    assert "then" not in db.jobs[job.id].spec_json
