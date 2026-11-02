"""A tarefa para no campo, pede a liberação e CONTINUA depois da aprovação.

O caso (sessão 1a605544, 2026-11-01): o Daniel montou um Google Forms com CEP,
CPF, endereço e nome e pediu "preenche e envia". Nada vazou — e nada funcionou:

1. ``_last_seen_url`` nunca era atribuído, e os quatro `fill` com `«pks:…»`
   morreram em "não sei em que página estou" (job 232d0b30);
2. a recusa era erro de passo comum: o freio de repetição fechou a tarefa com
   "ação repetida", e o motivo real nunca chegou a quem podia resolver;
3. ninguém criou card: quem pedia era o cérebro, e o agente que leu a recusa não
   tinha a tool `solicitar_dado`;
4. mesmo aprovado, `consume` gastava a liberação inteira no primeiro campo.

O fluxo não é linear: o pedido e a aprovação podem estar a horas de distância, e
o turno que disparou a tarefa já acabou. Aqui está ponta a ponta, sem rede.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.driver import BrowserLocalDriver, NeedsRelease
from core.release import ReleaseFiller

from tests.browser_local.test_driver_vision import _FakeLLM, _FakeProvider

URL = "https://docs.google.com/forms/d/e/1FAIpQLSen9wfl/viewform"

TAREFA = (
    f"Abrir o formulário {URL}, preencher os 4 campos de resposta curta com "
    "«pks:nome», «pks:endereco», «pks:cep» e «pks:cpf» e depois clicar em Enviar."
)


def _forms() -> dict:
    """A tela que o modelo viu no job 232d0b30, enxuta."""
    return {
        "url": URL,
        "title": "Formulário sem título",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 6, "tag": "div", "text": "Qual o seu cep? Sua resposta"},
            {"ref": 9, "tag": "input", "type": "text", "text": "Qual o seu cep?"},
            {"ref": 10, "tag": "div", "text": "Qual o seu cpf? Sua resposta"},
            {"ref": 13, "tag": "input", "type": "text", "text": "Qual o seu cpf?"},
            {"ref": 17, "tag": "input", "type": "text", "text": "qual o seu endereço?"},
            {"ref": 21, "tag": "input", "type": "text", "text": "Qual o seu nome"},
            {"ref": 23, "tag": "div", "type": "button", "text": "Enviar"},
        ],
        "text": "Qual o seu cep? Qual o seu cpf? qual o seu endereço? Qual o seu nome Enviar",
    }


class _Row:
    def __init__(self, campos, destino):
        self.id = uuid4()
        self.campos = campos
        self.destino = destino


class _Service:
    def __init__(self, aprovadas=()):
        self._aprovadas = list(aprovadas)
        self.consumidas: list = []

    async def list_approved(self, user_id):
        return [r for r in self._aprovadas if r.id not in self.consumidas]

    async def consume(self, rid, payload=None):
        if rid in self.consumidas:
            raise RuntimeError("liberação já usada")
        self.consumidas.append(rid)


class _PKS:
    def __init__(self, props):
        self.props = props

    async def search_entities(self, *, user_id, query, redact=True):
        return [{"name": "Daniel Morais", "properties": dict(self.props)}]


MEMORIA = {"cep": "72630-123", "cpf": "52998224725", "full_name": "Daniel Morais"}


def _driver(steps, *, filler, acted, abas=None, tab_ref=None):
    digitado: dict = {}

    def _tela() -> dict:
        # Como o navegador de verdade: o campo preenchido volta com `value` no
        # snapshot seguinte. Sem isso o freio de giro lê "a tela não mudou".
        tela = _forms()
        for item in tela["elements"]:
            if item["ref"] in digitado:
                item["value"] = digitado[item["ref"]]
        return tela

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": args.get("tab_id", 42)}
        if action == "browser.tabs":
            return {"tabs": abas or []}
        if action == "browser.fill":
            digitado[args.get("ref")] = args.get("value")
            return {"ok": True}
        if action == "browser.snapshot":
            return _tela()
        if action == "browser.screenshot":
            return {"ok": True, "format": "jpeg", "data": "UFJJTlQ=", "marks": True}
        if action == "browser.downloads":
            return {"ok": True, "dir": "/Users/daniel/Downloads", "count": 0, "files": []}
        return {"ok": True}

    return BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider(steps)),
        model="texto",
        filler=filler,
        user_id=uuid4(),
        tab_ref=tab_ref,
    )


def _fills(acted):
    return [args for action, args in acted if action == "browser.fill"]


# ── 1. sem liberação: para AGORA, com o pedido do formulário inteiro ────────


@pytest.mark.asyncio
async def test_sem_liberacao_a_tarefa_para_no_primeiro_campo_com_o_pedido_todo():
    acted: list = []
    filler = ReleaseFiller(_Service(), _PKS(MEMORIA))
    driver = _driver(
        [{"action": "fill", "args": {"ref": 9, "value": "«pks:cep»"}}],
        filler=filler,
        acted=acted,
    )
    out = await driver.run(TAREFA, start_url=URL)

    assert out.status == "needs_release"
    # O domínio veio da ABA — sem isto era "não sei em que página estou".
    assert out.release["host"] == "docs.google.com"
    # Um card por formulário: tudo o que a tarefa vai digitar, de uma vez.
    assert out.release["campos"] == ["nome", "endereco", "cep", "cpf"]
    assert "Qual o seu cep?" in out.release["rotulos"]
    # Nada foi digitado — nem o marcador, nem valor.
    assert _fills(acted) == []
    # Parou na primeira tentativa: o freio de repetição não chegou a rodar.
    assert len([s for s in out.steps if s.action == "fill"]) == 0


@pytest.mark.asyncio
async def test_o_que_ja_esta_liberado_nao_entra_no_pedido():
    acted: list = []
    filler = ReleaseFiller(_Service([_Row(["pks:nome"], "docs.google.com")]), _PKS(MEMORIA))
    driver = _driver(
        [{"action": "fill", "args": {"ref": 9, "value": "«pks:cep»"}}],
        filler=filler,
        acted=acted,
    )
    out = await driver.run(TAREFA, start_url=URL)
    assert out.status == "needs_release"
    assert "nome" not in out.release["campos"]
    assert out.release["campos"][0] == "endereco"


# ── 2. aprovado: a continuação preenche TODOS os campos com uma aprovação ──


@pytest.mark.asyncio
async def test_aprovado_a_continuacao_preenche_todos_os_campos_e_gasta_uma_vez():
    acted: list = []
    servico = _Service([_Row(["pks:nome", "pks:cep", "pks:cpf"], "docs.google.com")])
    filler = ReleaseFiller(servico, _PKS(MEMORIA))
    driver = _driver(
        [
            {"action": "fill", "args": {"ref": 9, "value": "«pks:cep»"}},
            {"action": "fill", "args": {"ref": 13, "value": "«pks:cpf»"}},
            {"action": "fill", "args": {"ref": 21, "value": "«pks:nome»"}},
            {"action": "done", "args": {"answer": "preenchido"}},
        ],
        filler=filler,
        acted=acted,
        abas=[{"tabId": 7, "url": URL}],
        tab_ref=7,
    )
    out = await driver.run(TAREFA, reabrir_url=URL)

    assert out.status == "completed", out.summary
    valores = {f["ref"]: f["value"] for f in _fills(acted)}
    assert valores == {9: "72630-123", 13: "52998224725", 21: "Daniel Morais"}
    assert len(servico.consumidas) == 1


# ── 3. número escrito à mão num campo justificado também vira pedido ───────


@pytest.mark.asyncio
async def test_literal_em_campo_justificado_vira_pedido_e_nao_erro_de_passo():
    driver = BrowserLocalDriver(
        act=None, llm=None, model="x",
        filler=ReleaseFiller(_Service(), _PKS(MEMORIA)), user_id=uuid4(),
    )
    driver._task_text = TAREFA
    with pytest.raises(NeedsRelease) as pedido:
        await driver._resolver_placeholder({"ref": 9, "value": "72630-123"}, _forms())
    assert "cep" in pedido.value.campos
    assert pedido.value.host == "docs.google.com"


@pytest.mark.asyncio
async def test_literal_com_liberacao_ja_dada_manda_usar_o_marcador():
    """Sem esta guarda, cada número digitado à mão abriria um card novo."""
    driver = BrowserLocalDriver(
        act=None, llm=None, model="x",
        filler=ReleaseFiller(_Service([_Row(["pks:cep"], "docs.google.com")]), _PKS(MEMORIA)),
        user_id=uuid4(),
    )
    driver._task_text = TAREFA
    with pytest.raises(RuntimeError) as erro:
        await driver._resolver_placeholder({"ref": 9, "value": "72630-123"}, _forms())
    assert "já está liberado" in str(erro.value) and "«pks:cep»" in str(erro.value)


# ── 4. a aba sumiu entre o pedido e a aprovação ──────────────────────────


@pytest.mark.asyncio
async def test_aba_fechada_reabre_onde_a_tarefa_parou_e_nao_em_about_blank():
    acted: list = []
    driver = _driver(
        [{"action": "done", "args": {"answer": "ok"}}],
        filler=None,
        acted=acted,
        abas=[],  # a aba 7 não existe mais
        tab_ref=7,
    )
    await driver.run(TAREFA, reabrir_url=URL)
    claim = next(args for action, args in acted if action == "browser.claim")
    assert claim.get("url") == URL and "tab_id" not in claim


@pytest.mark.asyncio
async def test_aba_viva_nao_e_recarregada():
    """Navegar de novo apagaria o que a tarefa já tinha digitado."""
    acted: list = []
    driver = _driver(
        [{"action": "done", "args": {"answer": "ok"}}],
        filler=None,
        acted=acted,
        abas=[{"tabId": 7, "url": URL}],
        tab_ref=7,
    )
    await driver.run(TAREFA, reabrir_url=URL)
    claim = next(args for action, args in acted if action == "browser.claim")
    assert claim.get("tab_id") == 7 and "url" not in claim


# ── 5. o runner cria o card sozinho ──────────────────────────────────────


class _Criador:
    def __init__(self):
        self.criados: list[dict] = []

    async def create(self, **kwargs):
        self.criados.append(kwargs)
        return SimpleNamespace(id=uuid4())


def _job(**spec):
    base = {"task": TAREFA}
    base.update(spec)
    return SimpleNamespace(
        id=uuid4(), user_id=uuid4(), session_id=uuid4(),
        title="Preencher e enviar formulário", spec_json=base, extra_metadata={},
    )


def _outcome(campos):
    return SimpleNamespace(
        release={"campos": campos, "host": "docs.google.com", "rotulos": ["Qual o seu cep?"]}
    )


@pytest.mark.asyncio
async def test_runner_pede_so_o_que_a_memoria_tem_e_avisa_o_que_falta():
    from core.jobs.runner_browser_local import _pedir_liberacao

    criador = _Criador()
    filler = ReleaseFiller(_Service(), _PKS(MEMORIA))
    pedido = await _pedir_liberacao(
        job=_job(), outcome=_outcome(["nome", "endereco", "cep", "cpf"]),
        filler=filler, service=criador,
    )
    assert pedido["id"]
    assert pedido["pedir"] == ["nome", "cep", "cpf"]
    assert pedido["faltando"] == ["endereco"]
    card = criador.criados[0]
    assert card["campos"] == ["pks:nome", "pks:cep", "pks:cpf"]
    assert card["destino"] == "docs.google.com" and card["destino_tipo"] == "dominio"
    assert card["origem"] == "admin" and card["risco_motivos"] == []
    assert "Qual o seu cep?" in card["evidencia"]


@pytest.mark.asyncio
async def test_card_de_turno_contaminado_diz_que_nasceu_de_terceiro():
    from core.jobs.runner_browser_local import _pedir_liberacao
    from core.release.service import nasceu_de_terceiro

    criador = _Criador()
    job = _job(autoridade={"principal": "admin", "tainted": True, "taint_sources": ["web_fetch"]})
    await _pedir_liberacao(
        job=job, outcome=_outcome(["cep"]),
        filler=ReleaseFiller(_Service(), _PKS(MEMORIA)), service=criador,
    )
    motivos = criador.criados[0]["risco_motivos"]
    assert nasceu_de_terceiro(SimpleNamespace(risco_motivos=motivos))


@pytest.mark.asyncio
async def test_finalidade_que_nao_justifica_o_campo_nem_vira_card():
    from core.jobs.runner_browser_local import _pedir_liberacao

    criador = _Criador()
    job = _job(task="Calcular o frete da cortina em https://loja.com.br/frete com «pks:cep» e «pks:cpf»")
    pedido = await _pedir_liberacao(
        job=job, outcome=_outcome(["cep", "cpf"]),
        filler=ReleaseFiller(_Service(), _PKS(MEMORIA)), service=criador,
    )
    assert pedido["barrados"] == ["cpf"]
    assert criador.criados[0]["campos"] == ["pks:cep"]


def test_a_mensagem_da_pausa_diz_onde_aprovar_e_que_continua_sozinha():
    from core.jobs.runner_browser_local import _texto_da_pausa

    job = _job()
    fecho, itens, _ = _texto_da_pausa(
        job, {"id": "abc", "pedir": ["cep", "cpf"], "faltando": ["endereco"],
              "barrados": [], "host": "docs.google.com"},
    )
    texto = "\n".join(itens)
    assert "⏸" in fecho
    assert "não vale como aprovação" in texto
    assert "continua sozinha" in texto
    assert f'continue_job="{job.id}"' in texto
    assert "NÃO chame solicitar_dado" in texto
    assert "Não tenho na memória" in texto


# ── 6. a aprovação continua a tarefa — sem turno, sem reescrever ────────


class _JobsDB:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.criados: list = []
        self.atualizados: dict = {}

    async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
        if statuses:
            return [j for j in self.jobs + self.criados if j.status in statuses]
        return self.jobs

    async def create_agent_job(self, job):
        if getattr(job, "id", None) is None:
            job.id = uuid4()
        self.criados.append(job)
        return job

    async def update_agent_job(self, job_id, **fields):
        self.atualizados[job_id] = fields
        for j in self.jobs:
            if j.id != job_id:
                continue
            if "extra_metadata" in fields:
                j.extra_metadata = fields["extra_metadata"]
            if "status" in fields:
                j.status = fields["status"]


def _parado(release_id, **sessao_extra):
    sessao = {
        "device_id": "dev-1", "tab_ref": 7, "url": URL,
        "release_pending": str(release_id), **sessao_extra,
    }
    return SimpleNamespace(
        id=uuid4(), user_id=uuid4(), session_id=uuid4(), kind="browser_local",
        status="failed", title="Preencher e enviar formulário",
        spec_json={"task": TAREFA, "device_id": "dev-1", "headless": False,
                   "confirm_sensitive_ok": True, "context": "o Daniel pediu"},
        extra_metadata={"browser_session": sessao},
    )


def _aprovada(**kw):
    base = dict(id=uuid4(), user_id=uuid4(), session_id=uuid4(), status="aprovada",
                campos=["pks:cep", "pks:cpf"], destino="docs.google.com")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_aprovar_enfileira_a_mesma_tarefa_na_mesma_aba():
    from core.jobs.release_resume import build_job_resumer

    row = _aprovada()
    parado = _parado(row.id)
    db = _JobsDB([parado])
    avisos: list[str] = []

    async def announce(r, texto):
        avisos.append(texto)

    resume = build_job_resumer(db=db, job_manager=SimpleNamespace(poke=lambda: None), announce=announce)
    assert await resume(row) is True

    novo = db.criados[0]
    spec = novo.spec_json
    assert spec["task"] == TAREFA  # palavra por palavra — reescrever é o que estraga
    assert spec["tab_ref"] == 7 and spec["reabrir_url"] == URL
    assert spec["device_id"] == "dev-1"
    assert spec["release_id"] == str(row.id)
    assert "«pks:cep»" in spec["context"] and "o Daniel pediu" in spec["context"]
    # Liberar o DADO não é liberar o clique: herda o que a tarefa já tinha, nada além.
    assert spec["confirm_sensitive_ok"] is True
    assert parado.extra_metadata["browser_session"]["release_resumed"] == str(novo.id)
    assert avisos and "Continuando" in avisos[0]


@pytest.mark.asyncio
async def test_a_mesma_aprovacao_nao_abre_duas_tarefas():
    from core.jobs.release_resume import build_job_resumer

    row = _aprovada()
    db = _JobsDB([_parado(row.id)])
    resume = build_job_resumer(db=db, job_manager=None)
    assert await resume(row) is True
    assert await resume(row) is False  # a marca mora no banco, não no processo
    assert len(db.criados) == 1


@pytest.mark.asyncio
async def test_sem_job_esperando_devolve_false_para_o_turno_generico():
    from core.jobs.release_resume import build_job_resumer

    resume = build_job_resumer(db=_JobsDB([_parado(uuid4())]), job_manager=None)
    assert await resume(_aprovada()) is False


@pytest.mark.asyncio
async def test_navegador_ocupado_nao_atropela_a_tarefa_que_esta_rodando():
    from core.jobs.release_resume import build_job_resumer

    row = _aprovada()
    rodando = SimpleNamespace(
        id=uuid4(), kind="browser_local", status="running", title="Tocar playlist",
        spec_json={"device_id": "dev-1"}, extra_metadata={},
    )
    db = _JobsDB([_parado(row.id), rodando])
    avisos: list[str] = []

    async def announce(r, texto):
        avisos.append(texto)

    resume = build_job_resumer(db=db, job_manager=None, announce=announce)
    assert await resume(row) is True
    assert db.criados == []
    assert "ocupado" in avisos[0] and "Tocar playlist" in avisos[0]


@pytest.mark.asyncio
async def test_build_resumer_prefere_continuar_o_job_ao_turno_cognitivo():
    from core.release.resume import _esquecer, build_resumer

    turnos: list = []

    class _AI:
        def process_user_input(self, **kw):
            turnos.append(kw)

            async def _gen():
                if False:
                    yield None

            return _gen()

    async def resume_job(row):
        return True

    resume = build_resumer(get_ai_service=lambda: _AI(), resume_job=resume_job)
    row = _aprovada()
    try:
        assert await resume(row) is True
        assert turnos == []
    finally:
        _esquecer(row.id)


# ── 7. `continue_job` de uma tarefa parada esperando liberação ───────────


def _executor(db):
    from core.jobs.executor import JobToolExecutor

    return JobToolExecutor(
        db=db, job_manager=SimpleNamespace(poke=lambda: None),
        user_id=None, session_id=uuid4(), user_message="", device_id="dev-1",
    )


class _ServicoFalso:
    linhas: dict = {}

    def __init__(self, db, notifier=None):
        pass

    async def get(self, rid):
        return self.linhas.get(str(rid))


@pytest.mark.asyncio
async def test_continue_job_com_liberacao_pendente_diz_que_sim_no_chat_nao_aprova(monkeypatch):
    import core.release

    row = _aprovada(status="pendente")
    parado = _parado(row.id)
    _ServicoFalso.linhas = {str(row.id): row}
    monkeypatch.setattr(core.release, "ReleaseService", _ServicoFalso)
    ex = _executor(_JobsDB([parado]))
    out = await ex._retomada_por_liberacao(parado, str(row.id))
    assert out["ok"] is False
    assert "NÃO foi aprovada" in out["error"] and "não vale como aprovação" in out["error"]


@pytest.mark.asyncio
async def test_continue_job_com_liberacao_aprovada_continua(monkeypatch):
    import core.release

    row = _aprovada()
    parado = _parado(row.id)
    _ServicoFalso.linhas = {str(row.id): row}
    monkeypatch.setattr(core.release, "ReleaseService", _ServicoFalso)
    db = _JobsDB([parado])
    out = await _executor(db)._retomada_por_liberacao(parado, str(row.id))
    assert out["ok"] is True and out["resumed_from"] == str(parado.id)
    assert db.criados[0].spec_json["tab_ref"] == 7


# ── 8. o navegador do servidor não recebe marcador ──────────────────────


@pytest.mark.asyncio
async def test_navegador_do_servidor_recusa_marcador_em_vez_de_digitar_o_texto(monkeypatch):
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_agent_enabled", True, raising=False)
    out = await _executor(_JobsDB([]))._browser_task({"task": TAREFA})
    assert out["ok"] is False
    assert "browser_local_task" in out["error"] and "«pks:cep»" in out["error"]
