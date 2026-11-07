"""O que ficou pendente do navegador local, um caso real por teste.

1. sem `start_url`, a tarefa trabalhava na aba que estivesse aberta (job
   65e940af: "deu tudo certo" na confirmação de OUTRO formulário);
2. Enter que envia formulário não passava pela conferência antes do envio;
3. card de liberação que vencia deixava a tarefa "esperando você" para sempre;
4. a busca da memória na conversa escondia os treinos de cada dia;
5. cancelar uma tarefa que esperava um card deixava o card no painel.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.driver import BrowserLocalDriver, _mesmo_lugar, _url_da_tarefa

from tests.browser_local.test_driver_vision import _FakeLLM, _FakeProvider

FORM_PEDIDO = "https://docs.google.com/forms/d/e/1FAIpQLSe-UO-rK2E_zdxT6lvw8wGwFpiAmWN2g7boa8Fxn0gF-aGv2A/viewform"
FORM_ANTIGO = "https://docs.google.com/forms/u/0/d/e/1FAIpQLSen9wfl_d1Y6pS_KlSSXMlM6GY8NwoxsRC6x4NDeSnc4G5QVg/formResponse"


def _tela(url: str, *, campos: int = 2, enviar: bool = True) -> dict:
    elementos = [{"ref": 10 + i, "tag": "input", "type": "text", "text": f"pergunta {i}"} for i in range(campos)]
    if enviar:
        elementos.append({"ref": 50, "tag": "div", "type": "button", "text": "Enviar"})
    return {"url": url, "title": "Formulário", "ready": "complete", "stop": False,
            "elements": elementos, "text": "Formulário " + " ".join(e["text"] for e in elementos)}


class _Aparelho:
    def __init__(self, *, aba_url: str | None = None, campos: int = 2, enviar: bool = True) -> None:
        self.url = aba_url or "about:blank"
        self.reaproveitavel = aba_url is not None
        self.campos, self.enviar = campos, enviar
        self.acted: list = []
        self.navegou_para: list[str] = []
        self.digitado: dict = {}

    async def act(self, action, args, timeout):
        self.acted.append((action, dict(args)))
        if action == "browser.tabs":
            return {"tabs": [{"tabId": 7, "url": self.url}] if self.reaproveitavel else []}
        if action == "browser.claim":
            return {"tab_ref": args.get("tab_id", 7)}
        if action == "browser.navigate":
            self.url = args["url"]
            self.navegou_para.append(args["url"])
            return {"ok": True, "url": args["url"]}
        if action == "browser.snapshot":
            tela = _tela(self.url, campos=self.campos, enviar=self.enviar)
            for item in tela["elements"]:
                if item["ref"] in self.digitado:
                    item["value"] = self.digitado[item["ref"]]
            return tela
        if action == "browser.fill":
            self.digitado[args["ref"]] = args["value"]
            return {"ok": True}
        if action == "browser.screenshot":
            return {"ok": True, "format": "jpeg", "data": "UFJJTlQ=", "marks": True}
        if action == "browser.downloads":
            return {"ok": True, "files": [], "count": 0}
        return {"ok": True}


def _driver(ap: _Aparelho, passos, **kw):
    return BrowserLocalDriver(act=ap.act, llm=_FakeLLM({}, _FakeProvider(passos)), model="m", user_id=uuid4(), **kw)


# ── 1. a URL da tarefa é onde começar ────────────────────────────────────────


def test_url_da_tarefa_e_mesmo_lugar():
    tarefa = f"Abrir a URL {FORM_PEDIDO}?usp=publish-editor e preencher. Depois, enviar."
    assert _url_da_tarefa(tarefa) == f"{FORM_PEDIDO}?usp=publish-editor"
    assert _url_da_tarefa("preencha o formulário") == ""
    # Mesmo formulário (mesmo id), com outra query e outra rota: mesmo lugar.
    assert _mesmo_lugar(FORM_PEDIDO + "?x=1", FORM_PEDIDO + "?usp=publish-editor")
    # Mesmo site, OUTRO formulário: lugar diferente (o job 65e940af).
    assert not _mesmo_lugar(FORM_ANTIGO, FORM_PEDIDO)
    # Página de início do site: estar em qualquer página dele já serve.
    assert _mesmo_lugar("https://pt.aliexpress.com/item/1.html", "https://www.aliexpress.com/")
    assert not _mesmo_lugar("https://outra.com/x", "https://www.aliexpress.com/")


@pytest.mark.asyncio
async def test_aba_reaproveitada_noutro_formulario_vai_para_o_da_tarefa():
    ap = _Aparelho(aba_url=FORM_ANTIGO)
    driver = _driver(ap, [{"action": "done", "args": {"answer": "ok"}}], tab_ref=7)
    await driver.run(f"Abrir a URL {FORM_PEDIDO}?usp=publish-editor e preencher todos os campos")
    assert ap.navegou_para == [f"{FORM_PEDIDO}?usp=publish-editor"]


@pytest.mark.asyncio
async def test_aba_ja_no_formulario_certo_nao_recarrega():
    ap = _Aparelho(aba_url=FORM_PEDIDO)
    driver = _driver(ap, [{"action": "done", "args": {"answer": "ok"}}], tab_ref=7)
    await driver.run(f"Preencha o formulário {FORM_PEDIDO}?usp=publish-editor (a aba já está aberta)")
    assert ap.navegou_para == []


@pytest.mark.asyncio
async def test_aba_nova_abre_a_url_da_tarefa_e_tarefa_sem_url_nao_navega():
    ap = _Aparelho()
    await _driver(ap, [{"action": "done", "args": {"answer": "ok"}}]).run(f"Veja {FORM_PEDIDO}")
    assert ap.navegou_para == [FORM_PEDIDO]
    ap2 = _Aparelho()
    await _driver(ap2, [{"action": "done", "args": {"answer": "ok"}}]).run("Procure o preço do teclado")
    assert ap2.navegou_para == []


# ── 2. Enter que envia passa pela conferência ────────────────────────────────


@pytest.mark.asyncio
async def test_enter_num_formulario_preenchido_pelo_neo_para_antes_de_enviar():
    ap = _Aparelho(aba_url=FORM_PEDIDO)
    driver = _driver(ap, [
        {"action": "fill", "args": {"ref": 10, "value": "Supino reto 4x8"}},
        {"action": "fill", "args": {"ref": 11, "value": "Academia Exemplo"}},
        {"action": "press", "args": {"key": "Enter"}},
    ], tab_ref=7, pedido="preencha e envie")
    out = await driver.run(f"Preencha {FORM_PEDIDO} e envie")
    assert out.status == "needs_confirmation", out.summary
    assert out.pending == "Enter (enviar o formulário)"
    assert not [a for n, a in ap.acted if n == "browser.press"]
    assert any("escrito pelo Neo" in linha for linha in out.revisao)


@pytest.mark.asyncio
async def test_enter_numa_busca_nao_e_envio_de_formulario():
    ap = _Aparelho(aba_url="https://pt.aliexpress.com/", campos=1, enviar=False)
    driver = _driver(ap, [
        {"action": "fill", "args": {"ref": 10, "value": "AMD BC-250"}},
        {"action": "press", "args": {"key": "Enter"}},
        {"action": "done", "args": {"answer": "buscado"}},
    ], tab_ref=7)
    out = await driver.run("Busque a AMD BC-250 no AliExpress")
    assert out.status == "completed", out.summary
    assert [a for n, a in ap.acted if n == "browser.press"]


@pytest.mark.asyncio
async def test_retomada_da_conferencia_aceita_enter_ou_clique():
    """Parou no Enter; depois do "pode enviar", o agente clica em Enviar — passa."""
    ap = _Aparelho(aba_url=FORM_PEDIDO)
    driver = _driver(ap, [
        {"action": "fill", "args": {"ref": 10, "value": "Supino reto 4x8"}},
        {"action": "fill", "args": {"ref": 11, "value": "Academia Exemplo"}},
        {"action": "click", "args": {"ref": 50}},
        {"action": "done", "args": {"answer": "enviado"}},
    ], tab_ref=7, allow_sensitive_label="Enter (enviar o formulário)")
    out = await driver.run(f"Preencha {FORM_PEDIDO} e envie")
    assert out.status == "completed", out.summary
    assert [a for n, a in ap.acted if n == "browser.click" and a.get("ref") == 50]


# ── 3. o card que vence encerra a espera ─────────────────────────────────────


class _Jobs:
    def __init__(self, jobs):
        self.jobs = {j.id: j for j in jobs}

    async def list_agent_jobs(self, user_id=None, *, statuses=None, limit=20):
        return [j for j in self.jobs.values() if not statuses or j.status in statuses]

    async def update_agent_job(self, job_id, **campos):
        for k, v in campos.items():
            setattr(self.jobs[job_id], k, v)
        return self.jobs[job_id]


def _esperando(rid, kind="browser_local"):
    return SimpleNamespace(
        id=uuid4(), kind=kind, status="waiting", title="Preencher formulário",
        extra_metadata={"browser_session": {"release_pending": str(rid)}}, spec_json={},
    )


@pytest.mark.asyncio
async def test_card_vencido_encerra_a_tarefa_com_aviso_e_o_pendente_espera():
    from core.jobs.release_resume import encerrar_esperas_vencidas

    vencido, pendente = uuid4(), uuid4()
    linhas = {
        str(vencido): SimpleNamespace(id=vencido, status="expirada", campos=["pks:cep", "pks:cpf"], destino="docs.google.com"),
        str(pendente): SimpleNamespace(id=pendente, status="pendente", campos=["pks:cep"], destino="loja.com"),
    }
    servico = SimpleNamespace(get=lambda rid: _async(linhas.get(str(rid))))
    a, b, c = _esperando(vencido), _esperando(pendente), _esperando(vencido, kind="sandbox_task")
    db = _Jobs([a, b, c])
    avisos: list[str] = []

    async def announce(row, texto):
        avisos.append(texto)

    assert await encerrar_esperas_vencidas(db, servico, announce) == 1
    assert a.status == "cancelled" and b.status == "waiting" and c.status == "waiting"
    assert avisos == [
        "⌛ O pedido para liberar CEP, CPF em docs.google.com venceu sem resposta, então "
        "encerrei “Preencher formulário”. Se ainda quiser, é só me pedir de novo."
    ]


async def _async(v):
    return v


@pytest.mark.asyncio
async def test_o_tick_roda_a_varredura_no_intervalo_e_engole_erro():
    from core.jobs.manager import JobManager

    jm = JobManager.__new__(JobManager)
    jm._periodicas = []
    chamadas: list[int] = []

    async def varrer():
        chamadas.append(1)

    async def quebra():
        raise RuntimeError("banco caiu")

    jm.add_periodic(varrer, every_s=60)
    jm.add_periodic(quebra, every_s=60)
    await jm._rodar_periodicas()
    await jm._rodar_periodicas()  # dentro do intervalo: não roda de novo
    assert chamadas == [1]


# ── 4. a busca da memória escolhe pela forma da pergunta ─────────────────────


def _servico_pks(antigos, ranqueados):
    from core.pks.service import PKSService

    svc = PKSService.__new__(PKSService)

    async def legacy(**_):
        return antigos

    async def ranked(**_):
        return ranqueados

    svc._search_legacy = legacy
    svc._search_ranked = ranked
    return svc


@pytest.mark.asyncio
async def test_pergunta_longa_vai_pelo_ranking_e_curta_pela_busca_de_sempre(monkeypatch):
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "pks_ranker_v2_enabled", False, raising=False)
    exercicios = [{"name": "Farmer's walk", "matched_by": "field_key"}]
    treinos = [{"name": "Treino de segunda-feira", "matched_by": "nome"}]
    longa = "treino segunda terça quarta quinta sexta exercícios repetições"

    svc = _servico_pks(exercicios, treinos)
    assert [r["name"] for r in await svc.search_entities(user_id=uuid4(), query=longa)] == ["Treino de segunda-feira"]
    # "meu cep": a busca de sempre acha o dono pela chave; o ranking não.
    dono = [{"name": "Daniel Morais", "matched_by": "field_key"}]
    svc = _servico_pks(dono, [{"name": "Academia Exemplo", "matched_by": "lex"}])
    assert [r["name"] for r in await svc.search_entities(user_id=uuid4(), query="meu cep")] == ["Daniel Morais"]
    # Curta sem nada na de sempre: o ranking é a reserva.
    svc = _servico_pks([], [{"name": "Plano de treino de academia", "matched_by": "lex"}])
    assert [r["name"] for r in await svc.search_entities(user_id=uuid4(), query="minha academia")] == [
        "Plano de treino de academia"
    ]
    # Acerto EXATO de nome da de sempre entra na frente do ranking.
    svc = _servico_pks([{"name": "Academia Exemplo", "matched_by": "name"}], treinos)
    assert [r["name"] for r in await svc.search_entities(user_id=uuid4(), query="academia exemplo academia treino semana")] == [
        "Academia Exemplo", "Treino de segunda-feira"
    ]


# ── 5. cancelar a espera encerra o card ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelar_a_tarefa_que_esperava_card_encerra_o_card(monkeypatch):
    from core.jobs import executor as ex_mod

    decididos: list = []

    class _Servico:
        def __init__(self, db):
            pass

        async def decide(self, rid, *, approved, decided_by, source):
            decididos.append((rid, approved, source))

    monkeypatch.setattr("core.release.service.ReleaseService", _Servico)
    rid = uuid4()
    job = _esperando(rid)
    job.user_id = uuid4()
    job.result_summary = ""
    db = _Jobs([job])
    out = await ex_mod.cancel_job(db, None, job)
    assert out["ok"] is True and db.jobs[job.id].status == "cancelled"
    assert decididos == [(str(rid), False, "tarefa_cancelada")]

    continuada = SimpleNamespace(**{**vars(_esperando(uuid4())), "status": "continued"})
    assert (await ex_mod.cancel_job(_Jobs([continuada]), None, continuada))["ok"] is False


# ── 6. a conclusão do agente chega ao redator (job a0c7951a) ─────────────────


def test_a_conclusao_da_ultima_nota_vai_para_o_redator_e_descricao_nao():
    from core.jobs.runner_browser_local import _frase_da_conclusao

    notas = [
        "Anúncio 1 — Placa — R$1.979,61 — vendedor recomendado pela loja [aba B]",
        "RESULTADO FINAL — 3 anúncios\n1) Placa — R$1.979,61\n3) Bc 250 — R$1.975,70\n"
        "MAIS BARATA: anúncio 3, R$1.975,70 (variação única). [aba D]",
    ]
    assert _frase_da_conclusao(SimpleNamespace(notes=notas)) == (
        "A conclusão do agente está no último item da lista abaixo: «MAIS BARATA: anúncio 3, "
        "R$1.975,70 (variação única).». Diga-a numa frase — sem oferecer comparar de novo."
    )
    # Só a ÚLTIMA nota conta: "recomendado" numa descrição do meio não é conclusão.
    assert _frase_da_conclusao(SimpleNamespace(notes=notas[:1] + ["Memória — R$4.379,80"])) == ""
    assert _frase_da_conclusao(SimpleNamespace(notes=[])) == ""


def test_veredito_em_linha_comprida_e_por_melhor_preco_tambem_chega():
    """Job 9a3223a4: a nota real (271 chars, "melhor preço por GB") saía vazia, e a
    mensagem perguntou "quer que eu detalhe qual passo falhou?" no lugar do veredito."""
    from core.jobs.runner_browser_local import _frase_da_conclusao

    nota = (
        "RESULTADO FINAL — melhor preço por GB: Anúncio 2 (Kingston NV3 PCIe 4.0 M.2 2280 NVMe "
        "SSD, /item/1005007791079105.html, Bo Cheng Store) — variação 2TB a R$1.586,18 = "
        "R$0,79/GB. Comparativo: Anúncio 1 (1TB, R$1.244,17) = R$1,24/GB; Anúncio 3 (2TB, "
        "R$1.673,99) = R$0,84/GB. [aba A]"
    )
    assert _frase_da_conclusao(SimpleNamespace(notes=[nota])) == (
        "A conclusão do agente está no último item da lista abaixo: «RESULTADO FINAL — melhor "
        "preço por GB: Anúncio 2 (Kingston NV3 PCIe 4.0 M.2 2280 NVMe SSD, "
        "/item/1005007791079105.html, Bo Cheng Store) — variação 2TB a R$1.586,18 = "
        "R$0,79/GB.». Diga-a numa frase — sem oferecer comparar de novo."
    )
    # Título não é veredito.
    assert _frase_da_conclusao(SimpleNamespace(notes=["RESULTADO FINAL — 3 anúncios"])) == ""
