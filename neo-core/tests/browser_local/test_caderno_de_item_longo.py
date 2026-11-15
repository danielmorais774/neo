"""Item maior que a nota (job d73fa72a, Portal, 2026-11-05).

"Extrair as questões e alternativas para o .txt": cada questão tem enunciado de
~1000 caracteres e cinco alternativas, e a nota tinha teto de 1000. As dez notas
foram cortadas no teto, em silêncio, no meio das alternativas. O modelo via o
caderno truncado, voltava à questão para completar, a nota nova tinha o mesmo
começo e era tida como repetida, e ele percorreu as dez questões duas vezes e
começava a terceira quando veio o PARE.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.driver import (
    _NOTA_CORTADA,
    _NOTA_MAX,
    BrowserLocalDriver,
    _nota_cortada,
    _nota_no_prompt,
)
from core.jobs.manager import JobManager
from core.workbench.sandbox import SandboxResult
from db.models.agent_job import AgentJob
from tests.test_jobs import _FakeDB, _RecDispatcher, _WritableSandbox

from .test_driver import _FakeLLM, _FakeProvider


def _questao(n: int) -> str:
    # Vocabulário próprio por questão, como nas reais: questões que só diferem no
    # número são, para o filtro de repetição, a mesma nota.
    enunciado = " ".join(f"tema{n}x{i} apoio{n}y{i} fonte{n}z{i}" for i in range(40))
    alternativas = "\n".join(
        f"ALTERNATIVA {k}: opcao{n}w{k} resposta{n}v{k} com o texto inteiro." for k in range(1, 6)
    )
    return f"QUESTAO {n}\nENUNCIADO: {enunciado}\n{alternativas}"


def test_uma_questao_inteira_cabe_numa_nota():
    nota = _questao(1)
    assert 1000 < len(nota) < _NOTA_MAX, len(nota)
    assert _nota_cortada(nota) == nota


def test_o_corte_quando_ha_e_dito_na_propria_nota():
    gigante = "x" * (_NOTA_MAX + 500)
    cortada = _nota_cortada(gigante)
    assert cortada.endswith(_NOTA_CORTADA)
    assert len(cortada) == _NOTA_MAX + len(_NOTA_CORTADA)


def test_nota_longa_aparece_compacta_no_prompt_com_o_fim_e_o_tamanho():
    nota = _questao(3)
    vista = _nota_no_prompt(nota)
    assert len(vista) < 700
    assert vista.startswith("QUESTAO 3")
    assert "ALTERNATIVA 5: opcao3w5" in vista, "o fim mostra que o item terminou"
    assert f"guardada INTEIRA no caderno: {len(nota)} caracteres" in vista


def test_nota_curta_aparece_inteira():
    assert _nota_no_prompt("PRODUTO 1: R$ 99,90") == "PRODUTO 1: R$ 99,90"


@pytest.mark.asyncio
async def test_a_segunda_volta_nao_cria_nem_corta_nada():
    """Anotar a mesma questão de novo não aumenta o caderno nem a encurta."""
    driver = BrowserLocalDriver(act=None, llm=_FakeLLM(_FakeProvider([])), model="fake")
    for n in range(1, 11):
        await driver._anotar_junto({"anotar": [_questao(n)]})
    assert len(driver.notes) == 10
    assert all("ALTERNATIVA 5" in nota for nota in driver.notes)
    for n in range(1, 11):
        await driver._anotar_junto({"anotar": [_questao(n)]})
    assert len(driver.notes) == 10


# ── o arquivo pedido sai dos itens, sem passar por modelo ───────────────────


def _job_do_navegador(spec):
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="browser_local",
        title="Extrair questões Portal para TXT", spec_json=spec, status="running",
    )
    job.id = uuid4()
    return job


def _terminal(itens):
    return {
        "status": "completed",
        "result_summary": "\n".join(itens)[:500],
        "extra_metadata": {"entrega": {"fecho": "Extraí as 10 questões.", "itens": itens}},
    }


@pytest.mark.asyncio
async def test_pedido_de_arquivo_grava_os_itens_inteiros_e_a_continuacao_aponta_para_ele():
    db = _FakeDB()
    sandbox = _WritableSandbox(SandboxResult(0, "", ""))
    jm = JobManager(db=db, sandbox=sandbox, dispatcher=_RecDispatcher(), tick_interval=1)
    jm.set_scheduled_runner(lambda job: None)
    job = _job_do_navegador({
        "task": "extrair as questões", "pedido": "extrair as questoes e alternativas para o .txt",
        "then": "confirmar a conclusão da extração",
    })
    db.jobs[job.id] = job
    itens = [_questao(n) for n in range(1, 11)] + ["(diário do passo a passo: logs/x)"]

    await jm._settle(job, _terminal(itens))

    gravados = {p: c for p, c in sandbox.written.items() if p.startswith("files/")}
    assert len(gravados) == 1
    caminho, conteudo = next(iter(gravados.items()))
    assert caminho.endswith(f"-{str(job.id)[:8]}.txt") and "extrair-questoes-portal" in caminho
    assert len(conteudo) > 10000, "inteiro, e não os 6000 que a continuação lê"
    assert conteudo == "\n\n".join(itens[:-1]) + "\n"
    assert conteudo.count("ALTERNATIVA 5") == 10
    assert "diário do passo a passo" not in conteudo, "rodapé não é coleta"
    texto = next(j for j in db.jobs.values() if j.kind == "scheduled_prompt").spec_json["action"]["texto"]
    assert f"/workspace/{caminho}" in texto and "file_save" in texto
    # E o caderno também fica sabendo (job f7794968: o arquivo existia e a nota
    # do job não dizia onde).
    meta = db.jobs[job.id].extra_metadata
    assert meta["itens_arquivo"] == f"/workspace/{caminho}"
    assert meta["entrega"]["fecho"] == "Extraí as 10 questões.", "o resto da coluna fica"


@pytest.mark.asyncio
async def test_sem_pedido_de_arquivo_nada_e_gravado():
    db = _FakeDB()
    sandbox = _WritableSandbox(SandboxResult(0, "", ""))
    jm = JobManager(db=db, sandbox=sandbox, dispatcher=_RecDispatcher(), tick_interval=1)
    jm.set_scheduled_runner(lambda job: None)
    job = _job_do_navegador({"task": "compare os preços", "then": "diga qual compensa mais"})
    db.jobs[job.id] = job

    await jm._settle(job, _terminal(["Placa R$1.979,61", "Memória R$4.379,80"]))

    assert not any(p.startswith("files/") for p in sandbox.written)
    texto = next(j for j in db.jobs.values() if j.kind == "scheduled_prompt").spec_json["action"]["texto"]
    assert "gravados, íntegros" not in texto
