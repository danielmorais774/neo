"""O reteste do Google Forms (sessão 9b2e29e2, 2026-11-01): a pausa funcionou, o card não.

O job parou no CEP como devia (``needs_release``, job 2559cd46), e daí em diante
tudo o que podia descartar campo descartou:

1. o pedido levava os SETE marcadores que o cérebro escreveu na tarefa ("use só
   os que o formulário pedir"), para um formulário de quatro perguntas;
2. a tabela de necessidade leu a tarefa inteira, achou "contato do respondente"
   e barrou CPF, CEP e endereço como "não necessário";
3. a memória disse que não tinha o nome (``full_name`` é chave genérica, a busca
   não a localiza) nem o e-mail (empatava com o "E-mail do Neo");
4. sem nada para pedir, nenhum card — e o job terminou ``failed``, que o redator
   leu como "A tarefa falhou".

Os fakes daqui respondem como o PKS de produção respondeu, não como o dublê
antigo (que devolvia qualquer chave para qualquer busca e por isso deixou o
sinônimo ``nome → full_name`` passar nos testes sem nunca funcionar).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.release import ReleaseFiller
from tests.browser_local.test_pausa_por_liberacao import (
    URL,
    _Criador,
    _JobsDB,
    _Row,
    _Service,
    _aprovada,
    _driver,
    _fills,
    _parado,
)

#: A tarefa como o cérebro escreveu (encurtada, mesmas armadilhas).
TAREFA = (
    f"Abrir a URL {URL} e preencher TODOS os campos do formulário com os dados do "
    "Daniel. (1) nos campos de dado pessoal use exatamente o marcador: «pks:nome», "
    "«pks:cpf», «pks:cep», «pks:endereco», «pks:telefone», «pks:email», "
    "«pks:data_nascimento» — use só os que o formulário realmente pedir. (3) Se o "
    "formulário tiver campo de e-mail, use o e-mail do Neo quando pedir contato do "
    "respondente: «pks:email». (5) Depois de preencher tudo, clique em Enviar."
)
PEDIDO = f"preenche esse formulario e envia {URL}?usp=publish-editor, vamos usar o meu navegador"
TITULO = "Preencher e enviar formulário Google Forms"


class _PKSDeProducao:
    """Como o PKS de produção respondeu em 01/11 (chaves conferidas no banco)."""

    def __init__(self):
        self.dono = {
            "full_name": "Daniel Morais", "email": "daniel@exemplo.com",
            "cep": "72630-123", "cpf": "52998224725",
        }

    async def primary_user_field(self, *, user_id, key):
        return self.dono.get(key)

    async def search_entities(self, *, user_id, query, redact=True):
        # `full_name`/`nome` são GENERIC_FIELD_KEYS: a busca não acha entidade.
        if query == "email":
            return [
                {"name": "E-mail do Neo", "properties": {"email": "neo@exemplo.com"}},
                {"name": "Daniel Morais", "properties": {"email": self.dono["email"]}},
            ]
        if query in ("cep", "cpf", "endereco"):
            props = {k: self.dono[k] for k in ("cep", "cpf")}
            return [{"name": "Daniel Morais", "properties": props}]
        return []


class _PKSSemDono(_PKSDeProducao):
    """O mesmo PKS visto pelo caminho antigo: só a busca."""

    primary_user_field = None


def _forms_real() -> dict:
    """A tela do passo 1 do job 2559cd46, com o texto solto que confunde."""
    return {
        "url": URL,
        "title": "Formulário sem título",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 3, "tag": "a", "text": "Mudar de conta"},
            {"ref": 4, "tag": "div", "type": "img",
             "text": "Seu e-mail e sua Conta do Google não fazem parte da resposta."},
            {"ref": 5, "tag": "div", "text": ""},
            {"ref": 6, "tag": "div", "text": "Qual o seu cep? Sua resposta"},
            {"ref": 7, "tag": "div", "text": "Sua resposta"},
            {"ref": 8, "tag": "div", "text": "Sua resposta"},
            {"ref": 9, "tag": "input", "type": "text", "text": "Qual o seu cep?"},
            {"ref": 10, "tag": "div", "text": "Qual o seu cpf? Sua resposta"},
            {"ref": 13, "tag": "input", "type": "text", "text": "Qual o seu cpf?"},
            {"ref": 14, "tag": "div", "text": "qual o seu endereço? Sua resposta"},
            {"ref": 17, "tag": "input", "type": "text", "text": "qual o seu endereço?"},
            {"ref": 18, "tag": "div", "text": "Qual o seu nome Sua resposta"},
            {"ref": 21, "tag": "input", "type": "text", "text": "Qual o seu nome"},
            {"ref": 23, "tag": "div", "type": "button", "text": "Enviar"},
        ],
        "text": (
            "Formulário sem título danielmorais774@gmail.com Mudar de conta Qual o seu "
            "cep? Qual o seu cpf? qual o seu endereço? Qual o seu nome Enviar"
        ),
    }


def _job(**spec):
    base = {"task": TAREFA, "pedido": PEDIDO}
    base.update(spec)
    return SimpleNamespace(
        id=uuid4(), user_id=uuid4(), session_id=uuid4(),
        title=TITULO, spec_json=base, extra_metadata={},
    )


def _outcome(campos):
    return SimpleNamespace(
        release={"campos": campos, "host": "docs.google.com", "rotulos": ["Qual o seu cep?"]}
    )


# ── 1. o card pede o que o FORMULÁRIO pergunta ────────────────────────────


def test_so_o_rotulo_de_campo_digitavel_conta():
    from core.release.sensitive import campos_pedidos_na_pagina

    pedidos = campos_pedidos_na_pagina(
        _forms_real(),
        ["nome", "cpf", "cep", "endereco", "telefone", "email", "data_nascimento"],
    )
    # "Seu e-mail e sua Conta do Google…" é aviso, não pergunta.
    assert pedidos == ["nome", "cpf", "cep", "endereco"]


def test_campo_sem_vocabulario_fica_e_pagina_sem_campo_nao_filtra():
    from core.release.sensitive import campos_pedidos_na_pagina

    assert campos_pedidos_na_pagina(_forms_real(), ["profissao"]) == ["profissao"]
    assert campos_pedidos_na_pagina({"elements": []}, ["telefone"]) == ["telefone"]


@pytest.mark.asyncio
async def test_o_driver_para_pedindo_os_quatro_campos_do_formulario(monkeypatch):
    import tests.browser_local.test_pausa_por_liberacao as base

    monkeypatch.setattr(base, "_forms", _forms_real)
    acted: list = []
    driver = _driver(
        [{"action": "fill", "args": {"ref": 9, "value": "«pks:cep»"}}],
        filler=ReleaseFiller(_Service(), _PKSDeProducao()),
        acted=acted,
    )
    out = await driver.run(TAREFA, start_url=URL)
    assert out.status == "needs_release"
    assert out.release["campos"] == ["nome", "cpf", "cep", "endereco"]
    assert _fills(acted) == []


# ── 2. a finalidade é o pedido do Daniel, não a instrução do cérebro ──────


@pytest.mark.asyncio
async def test_contato_na_instrucao_do_cerebro_nao_barra_o_formulario():
    from core.jobs.runner_browser_local import _pedir_liberacao

    criador = _Criador()
    pedido = await _pedir_liberacao(
        job=_job(), outcome=_outcome(["nome", "cpf", "cep", "endereco"]),
        filler=ReleaseFiller(_Service(), _PKSDeProducao()), service=criador,
    )
    assert pedido["barrados"] == []
    assert pedido["pedir"] == ["nome", "cpf", "cep"]
    assert pedido["faltando"] == ["endereco"]
    assert pedido["id"]
    card = criador.criados[0]
    assert card["campos"] == ["pks:nome", "pks:cpf", "pks:cep"]
    assert card["finalidade"] == TITULO


@pytest.mark.asyncio
async def test_job_antigo_sem_pedido_ainda_e_julgado_pela_tarefa():
    """Sem `pedido` no spec (job de antes, ou da agenda) a tarefa é o que há."""
    from core.jobs.runner_browser_local import _pedir_liberacao

    criador = _Criador()
    job = _job(task="Calcular o frete em https://loja.com.br com «pks:cep» e «pks:cpf»")
    job.spec_json.pop("pedido")
    pedido = await _pedir_liberacao(
        job=job, outcome=_outcome(["cep", "cpf"]),
        filler=ReleaseFiller(_Service(), _PKSDeProducao()), service=criador,
    )
    assert pedido["barrados"] == ["cpf"]
    # A explicação fala da finalidade ("frete"), não cita a tarefa inteira.
    assert "frete" in pedido["explicacao"] and "loja.com.br" not in pedido["explicacao"]


@pytest.mark.asyncio
async def test_pedido_de_frete_do_daniel_ainda_barra_o_cpf():
    from core.jobs.runner_browser_local import _pedir_liberacao

    criador = _Criador()
    job = _job(pedido="calcula o frete da cortina nesse site", task=TAREFA)
    pedido = await _pedir_liberacao(
        job=job, outcome=_outcome(["cep", "cpf"]),
        filler=ReleaseFiller(_Service(), _PKSDeProducao()), service=criador,
    )
    assert pedido["barrados"] == ["cpf"]
    assert criador.criados[0]["campos"] == ["pks:cep"]


def test_url_no_pedido_nao_escolhe_finalidade():
    from core.release.necessity import finalidade_conhecida

    assert finalidade_conhecida("preenche https://loja.com.br/pix/nota-fiscal aqui") is None
    assert finalidade_conhecida("emite a nota em https://x.com") == "nota_fiscal"


@pytest.mark.asyncio
async def test_literal_no_formulario_nao_vira_nao_necessario_por_contato():
    """A mesma tabela, dentro do driver: CPF escrito à mão neste formulário vira
    pedido, não "não é necessário para contato"."""
    from core.browser_local.driver import BrowserLocalDriver, NeedsRelease

    driver = BrowserLocalDriver(
        act=None, llm=None, model="x",
        filler=ReleaseFiller(_Service(), _PKSDeProducao()), user_id=uuid4(),
        finalidade=f"{PEDIDO}\n{TITULO}",
    )
    driver._task_text = TAREFA
    with pytest.raises(NeedsRelease) as pedido:
        await driver._resolver_placeholder({"ref": 13, "value": "52998224725"}, _forms_real())
    assert "cpf" in pedido.value.campos


# ── 3. o dado é do DONO ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nome_e_email_vem_da_entidade_do_daniel():
    filler = ReleaseFiller(_Service(), _PKSDeProducao())
    uid = uuid4()
    assert await filler.disponivel(user_id=uid, campo="nome") is None
    assert await filler.disponivel(user_id=uid, campo="email") is None
    assert "não está na memória" in await filler.disponivel(user_id=uid, campo="endereco")


@pytest.mark.asyncio
async def test_pela_busca_so_o_nome_some_e_o_email_empata():
    """O que produção fazia — e o motivo de o dono vir primeiro."""
    filler = ReleaseFiller(_Service(), _PKSSemDono())
    uid = uuid4()
    assert "não está na memória" in await filler.disponivel(user_id=uid, campo="nome")
    assert "mais de um lugar" in await filler.disponivel(user_id=uid, campo="email")


@pytest.mark.asyncio
async def test_aprovado_o_que_falta_na_memoria_fica_em_branco_e_o_resto_segue(monkeypatch):
    """Antes, «pks:endereco» parava a continuação pedindo um card que nunca ia
    nascer, e o nome depois dele nunca era digitado."""
    import tests.browser_local.test_pausa_por_liberacao as base

    monkeypatch.setattr(base, "_forms", _forms_real)
    acted: list = []
    servico = _Service([_Row(["pks:nome", "pks:cpf", "pks:cep"], "docs.google.com")])
    driver = _driver(
        [
            {"action": "fill", "args": {"ref": 9, "value": "«pks:cep»"}},
            {"action": "fill", "args": {"ref": 13, "value": "«pks:cpf»"}},
            {"action": "fill", "args": {"ref": 17, "value": "«pks:endereco»"}},
            {"action": "fill", "args": {"ref": 21, "value": "«pks:nome»"}},
            {"action": "done", "args": {"answer": "preenchido, sem o endereço"}},
        ],
        filler=ReleaseFiller(servico, _PKSDeProducao()),
        acted=acted,
        abas=[{"tabId": 7, "url": URL}],
        tab_ref=7,
    )
    out = await driver.run(TAREFA, reabrir_url=URL)

    assert out.status != "needs_release", out.summary
    valores = {f["ref"]: f["value"] for f in _fills(acted)}
    assert valores == {9: "72630-123", 13: "52998224725", 21: "Daniel Morais"}
    erro = next(s for s in out.steps if s.action == "fill" and not s.ok)
    assert "não está na memória" in erro.detail


# ── 4. parada esperando o Daniel não é falha ──────────────────────────────


def test_parada_com_card_e_waiting_sem_card_e_failed():
    from core.jobs.runner_browser_local import _estado_final

    parou = SimpleNamespace(status="needs_release", pending="")
    assert _estado_final(parou, {"id": "abc"}) == "waiting"
    assert _estado_final(parou, {"id": "", "faltando": ["cpf"]}) == "failed"
    clique = SimpleNamespace(status="needs_confirmation", pending="Enviar")
    assert _estado_final(clique, None) == "waiting"
    assert _estado_final(SimpleNamespace(status="completed", pending=""), None) == "completed"
    assert _estado_final(SimpleNamespace(status="failed", pending=""), None) == "failed"


def test_sem_redator_a_parada_nao_abre_com_falhou():
    from core.jobs.dispatcher import ProactiveDispatcher
    from db.models.agent_job import AgentJob

    disp = ProactiveDispatcher(db=None, broker=None, llm_router=None)
    texto = disp._fallback_text(
        AgentJob(kind="browser_local", title="Preencher", spec_json={}, status="waiting"), ""
    )
    assert texto.startswith("⏸") and "falhou" not in texto


@pytest.mark.asyncio
async def test_o_redator_e_avisado_de_que_e_pausa_e_nao_falha():
    from core.jobs.dispatcher import ProactiveDispatcher
    from tests.test_jobs import _ComposeLLM, _ComposeProvider, _FakeBroker, _FakeDB, _job_com_entrega

    db, broker = _FakeDB(), _FakeBroker()
    prov = _ComposeProvider()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=_ComposeLLM(prov))
    job = _job_com_entrega(status="waiting")
    db.jobs[job.id] = job
    await disp.dispatch(job)
    prompt = prov.prompts[0]
    assert "PAUSADA" in prompt
    assert "Diga o que falhou" not in prompt


# ── 5. a decisão fecha a parada ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_aprovar_marca_a_parada_como_continuada():
    from core.jobs.release_resume import build_job_resumer

    row = _aprovada()
    parado = _parado(row.id)
    parado.status = "waiting"
    db = _JobsDB([parado])
    resume = build_job_resumer(db=db, job_manager=None)
    assert await resume(row) is True
    assert parado.status == "continued"
    assert len(db.criados) == 1


@pytest.mark.asyncio
async def test_recusar_cancela_a_parada_sem_abrir_tarefa():
    from core.jobs.release_resume import build_job_resumer

    row = _aprovada(status="recusada")
    parado = _parado(row.id)
    parado.status = "waiting"
    db = _JobsDB([parado])
    resume = build_job_resumer(db=db, job_manager=None)
    assert await resume(row) is True
    assert parado.status == "cancelled"
    assert db.criados == []


@pytest.mark.asyncio
async def test_tarefa_cancelada_nao_volta_por_aprovacao_tardia():
    from core.jobs.release_resume import build_job_resumer

    row = _aprovada()
    parado = _parado(row.id)
    parado.status = "cancelled"
    db = _JobsDB([parado])
    resume = build_job_resumer(db=db, job_manager=None)
    assert await resume(row) is False
    assert db.criados == []


@pytest.mark.asyncio
async def test_build_resumer_leva_a_recusa_ate_a_tarefa_e_nao_abre_turno():
    from core.release.resume import build_resumer

    vistos: list = []

    async def resume_job(row):
        vistos.append(row.status)
        return True

    turnos: list = []
    resume = build_resumer(get_ai_service=lambda: turnos.append(1), resume_job=resume_job)
    assert await resume(_aprovada(status="recusada")) is False
    assert vistos == ["recusada"] and turnos == []


@pytest.mark.asyncio
async def test_continuacao_herda_o_pedido():
    from core.jobs.release_resume import spec_de_continuacao

    row = _aprovada()
    parado = _parado(row.id)
    parado.spec_json["pedido"] = PEDIDO
    assert spec_de_continuacao(parado, row)["pedido"] == PEDIDO


# ── 6. o executor grava o pedido no spec ──────────────────────────────────


@pytest.mark.asyncio
async def test_browser_local_task_grava_o_pedido_do_daniel(monkeypatch):
    from shared.config import get_settings
    from tests.browser_local.test_gate_dispatch import _executor

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", True, raising=False)
    monkeypatch.setattr("shared.config.get_settings", lambda: settings)
    ex = _executor(PEDIDO)
    result = await ex._browser_local_task({"task": TAREFA, "title": TITULO, "start_url": URL})
    assert result["ok"] is True, result
    assert ex.db.criados[-1].spec_json["pedido"] == PEDIDO
