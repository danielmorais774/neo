"""A grade de avaliação: `julgar` (um juiz mais forte) e `marcar_grade` (§4.6).

Sessão 697e0628 / job 2f320de8 (12/11): Google Forms da UFC avaliando os
requisitos do AppDemo, cada RF uma grade de 4 critérios × 4 opções. O agente do
passo (deepseek flash, sem raciocínio) marcou 70 de 70 células "Conforme", com
evidência que não casava com o RF — o RF-16 pedia o botão "Iniciar Maratona",
que nunca apareceu na tela, e saiu "CONFIRMADO (inputs por célula)". E gastou 4
cliques por RF: 125 passos acabaram na página 5 de 9.

A grade abaixo é a da página 2 desse formulário, como o snapshot a mostrou.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.browser_local import juiz as juiz_mod
from core.browser_local.driver import Aba, BrowserLocalDriver
from core.browser_local.grade import achar, coluna_de, enunciado_inteiro, ler_grades
from shared import ajustes as ajustes_mod
from tests.browser_local.test_driver_vision import _FakeLLM, _FakeProvider

FORM = "https://docs.google.com/forms/d/e/abc/formResponse"
COLUNAS = ["Conforme", "Não Conforme", "Parcialmente", "Não Aplicável"]
LINHAS = ["Clareza", "Ausência de Ambiguidade", "Completude", "Verificabilidade"]
RF01 = 'RF-01: O sistema deve permitir que o usuário se autentique e encerre a sessão por meio da opção "Sair".'
RF02 = (
    'RF-02: O sistema deve exibir uma página de perfil com nome de exibição, e-mail e data '
    'de cadastro ("Membro desde") do usuário.'
)
TEXTO_DA_PAGINA = "\n".join([
    "CATEGORIA 1 – Autenticação e Perfil do Usuário (Requisitos Funcionais: RF-01 a RF-06)",
    "C1 — Clareza: O requisito está escrito de forma clara e compreensível?",
    "C3 — Completude: O requisito apresenta todas as informações necessárias?",
    f"{RF01} *",
    "Conforme Não Conforme Parcialmente Não Aplicável",
    f"{RF02} *",
])


@pytest.fixture(autouse=True)
def _juiz_de_teste(tmp_path, monkeypatch):
    arquivo = tmp_path / "neo.toml"
    arquivo.write_text(
        '[navegador.julgamento]\nraciocinio_max = 1000\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("NEO_AJUSTES", str(arquivo))
    monkeypatch.setattr(ajustes_mod, "_cache", None)


def _grade(ref0: int, enunciado: str, marcadas: dict[str, str] | None = None) -> list[dict]:
    """Uma pergunta em grade, na ordem em que o snapshot do Google Forms a traz."""
    marcadas = marcadas or {}
    elementos = [
        {"ref": ref0, "tag": "div", "text": f"{enunciado} * Conforme Não C"[:120]},
        {"ref": ref0 + 1, "tag": "div", "text": "Conforme Não Conforme Parcialmente Não Aplicável Clareza"},
    ]
    ref = ref0 + 2
    for linha in LINHAS:
        elementos.append({"ref": ref, "tag": "div", "type": "radiogroup", "text": linha})
        ref += 1
        for coluna in COLUNAS:
            elementos.append({
                "ref": ref, "tag": "div", "type": "radio",
                "text": f"{coluna}, resposta para {linha}",
                "checked": marcadas.get(linha) == coluna,
            })
            ref += 1
    elementos.append({"ref": ref, "tag": "div", "text": " ".join(LINHAS)})
    return elementos


def _pagina(marcadas: dict[str, dict[str, str]] | None = None) -> dict:
    marcadas = marcadas or {}
    return {
        "url": FORM,
        "title": "Avaliação de Requisitos de Software - Plataforma AppDemo",
        "ready": "complete",
        "stop": False,
        "tela": TEXTO_DA_PAGINA,
        "elements": [
            {"ref": 0, "tag": "body", "text": "Avaliação de Requisitos"},
            *_grade(4, RF01, marcadas.get("RF-01")),
            *_grade(40, RF02, marcadas.get("RF-02")),
            {"ref": 90, "tag": "div", "type": "button", "text": "Próxima"},
        ],
    }


# ── a leitura da grade ──────────────────────────────────────────────────────


def test_a_grade_do_google_forms_vira_perguntas_linhas_e_colunas():
    questoes = ler_grades(_pagina({"RF-02": {"Clareza": "Parcialmente"}}))

    assert [q.chave for q in questoes] == ["RF-01", "RF-02"]
    rf01, rf02 = questoes
    assert rf01.rotulo == RF01
    assert [l.rotulo for l in rf01.linhas] == LINHAS
    assert rf01.colunas == COLUNAS
    assert rf01.linhas[0].radios["Conforme"] == 7
    assert rf02.linhas[0].marcada == "Parcialmente" and not rf02.completa()


def test_enunciado_inteiro_vem_do_texto_da_pagina():
    """O snapshot corta o rótulo em ~120 caracteres; o juiz precisa do inteiro."""
    [_, rf02] = ler_grades(_pagina())
    assert len(rf02.rotulo) < len(RF02)
    assert enunciado_inteiro(rf02, TEXTO_DA_PAGINA) == RF02


def test_achar_e_coluna():
    questoes = ler_grades(_pagina())
    assert achar(questoes, "rf 02").chave == "RF-02"
    assert achar(questoes, "RF-02: O sistema deve exibir").chave == "RF-02"
    assert achar(questoes, "RF-09") is None
    rf01 = questoes[0]
    assert coluna_de(rf01, "parcial") == "Parcialmente"
    assert coluna_de(rf01, "nao conforme") == "Não Conforme"
    assert coluna_de(rf01, "Conforme") == "Conforme", "não pode cair em 'Não Conforme'"
    assert coluna_de(rf01, "Talvez") == ""


def test_pagina_sem_grade_nao_tem_perguntas():
    assert ler_grades({"elements": [{"ref": 1, "tag": "input", "text": "Nome"}]}) == []


# ── o juiz ──────────────────────────────────────────────────────────────────


def _resposta_do_juiz(**sobre) -> dict:
    perguntas = [
        {
            "chave": "RF-01",
            "linhas": {
                "Clareza": {"resposta": "Conforme", "porque": "frase direta: autenticar e Sair"},
                "Ausência de Ambiguidade": {"resposta": "Parcialmente", "porque": "'autentique' não diz como"},
                "Completude": {"resposta": "Não Conforme", "porque": "não diz o que acontece com credencial errada"},
                "Verificabilidade": {"resposta": "Conforme", "porque": "dá para testar entrar e sair"},
            },
            "evidencia": "aba B mostrou o menu 'Sair' no /profile",
        },
        {
            "chave": "RF-02",
            "linhas": {
                "Clareza": {"resposta": "Conforme", "porque": "lista os três campos"},
                "Ausência de Ambiguidade": {"resposta": "conforme", "porque": "campos nomeados"},
                "Completude": {"resposta": "Parcial", "porque": "não diz o formato da data"},
                "Verificabilidade": {"resposta": "Conforme", "porque": "três campos visíveis"},
            },
            "evidencia": "sem evidência",
        },
    ]
    perguntas.extend(sobre.get("extra", []))
    return {"perguntas": perguntas}


def test_conferir_so_aceita_o_que_casa_com_a_grade():
    questoes = ler_grades(_pagina())
    lote = [(q, q.rotulo) for q in questoes]
    dados = _resposta_do_juiz()
    dados["perguntas"][0]["linhas"]["Clareza"]["resposta"] = "Excelente"
    dados["perguntas"].append({"chave": "RF-77", "linhas": {}})

    out = juiz_mod.conferir(dados, lote)

    rf01 = out.julgamentos["rf-01"]
    assert "Clareza" not in rf01.linhas and rf01.faltaram == ["Clareza"]
    assert rf01.linhas["Completude"][0] == "Não Conforme"
    assert out.julgamentos["rf-02"].linhas["Completude"][0] == "Parcialmente"
    assert any("Excelente" in p for p in out.problemas)
    assert any("RF-77" in p for p in out.problemas)


def test_o_pedido_ao_juiz_avisa_que_a_anotacao_do_agente_pode_estar_errada():
    questoes = ler_grades(_pagina())
    pedido = juiz_mod.montar_pedido(
        [(q, q.rotulo) for q in questoes], pagina=TEXTO_DA_PAGINA,
        evidencias=["── aba B (appdemo): …"], instrucoes="avalie de verdade",
    )
    assert "avalie de verdade" in pedido and "C1 — Clareza" in pedido
    assert "colunas: Conforme | Não Conforme | Parcialmente | Não Aplicável" in pedido
    assert "NÃO torna o enunciado" in juiz_mod.SISTEMA
    assert "CONFIRMADO" in juiz_mod.SISTEMA


# ── a tarefa inteira ────────────────────────────────────────────────────────


class _Form:
    """O desktop de mentira: a grade marca de verdade e o extract lê a página."""

    def __init__(self) -> None:
        self.marcadas: dict[str, dict[str, str]] = {}
        self.cliques: list[int] = []
        self.extraidos: list[dict] = []
        self.textos_das_abas: dict[int, str] = {}

    def tela(self) -> dict:
        return _pagina(self.marcadas)

    async def act(self, action: str, args: dict, timeout: float) -> dict:
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return self.tela()
        if action == "browser.extract":
            self.extraidos.append(dict(args))
            texto = self.textos_das_abas.get(args.get("tab_ref"), TEXTO_DA_PAGINA)
            return {"text": texto, "total": len(texto)}
        if action == "browser.click":
            ref = args.get("ref")
            self.cliques.append(ref)
            for q in ler_grades(self.tela()):
                for linha in q.linhas:
                    for coluna, r in linha.radios.items():
                        if r == ref:
                            self.marcadas.setdefault(q.chave, {})[linha.rotulo] = coluna
                            return {"ok": True, "label": f"{coluna}, resposta para {linha.rotulo}", "checked": True}
            return {"ok": True, "label": "?"}
        return {"ok": True}


def _driver(passos: list[dict], juiz: dict | None, device: _Form, **kw):
    passo = _FakeProvider(passos)
    o_juiz = _FakeProvider([juiz] if juiz else [])
    driver = BrowserLocalDriver(
        act=device.act,
        llm=_FakeLLM({"juiz-teste": o_juiz}, passo),
        model="passo-barato",
        juiz_modelo="juiz-teste",
        **kw,
    )
    return driver, SimpleNamespace(passo=passo, juiz=o_juiz)


@pytest.mark.asyncio
async def test_julgar_e_marcar_grade_preenchem_o_que_o_juiz_decidiu_numa_acao_por_pergunta():
    device = _Form()
    driver, ctx = _driver(
        [
            {"action": "julgar", "args": {}},
            {"action": "marcar_grade", "args": {"pergunta": "RF-01"}},
            {"action": "marcar_grade", "args": {"pergunta": "RF-02"}},
            {"action": "done", "args": {"answer": "página 2 avaliada"}},
        ],
        _resposta_do_juiz(),
        device,
    )
    teto_antes = driver._teto_de_passos()
    result = await driver.run("avaliar os requisitos do formulário", start_url=FORM)

    assert result.status == "completed"
    assert device.marcadas["RF-01"] == {
        "Clareza": "Conforme", "Ausência de Ambiguidade": "Parcialmente",
        "Completude": "Não Conforme", "Verificabilidade": "Conforme",
    }
    assert device.marcadas["RF-02"]["Completude"] == "Parcialmente"
    assert len(device.cliques) == 8
    # Uma chamada ao juiz, com o modelo do neo.toml, o texto inteiro e raciocínio.
    [chamada] = ctx.juiz.calls
    assert chamada["model"] == "juiz-teste"
    assert chamada["extra_body"] == {"reasoning": {"max_tokens": 1000}}
    pedido = chamada["messages"][-1].content
    assert RF02 in pedido, "o enunciado inteiro, não o cortado do snapshot"
    assert "C3 — Completude" in pedido
    # O caderno guarda o enunciado e o veredito de cada pergunta.
    nota = next(n for n in driver.notes if n.startswith("RF-01 — «"))
    assert RF01[:60] in nota and "Completude: Não Conforme" in nota
    # A conferência mostra o porquê.
    linhas = driver._linhas_de_revisao()
    assert any("RF-01 · Completude» → Não Conforme — julgado: não diz o que acontece" in l for l in linhas)
    # Trabalho novo devolve o passo: julgar + 2 perguntas.
    assert driver._teto_de_passos() >= teto_antes + 3


@pytest.mark.asyncio
async def test_o_prompt_do_passo_aponta_julgar_enquanto_a_grade_esta_aberta():
    device = _Form()
    driver, ctx = _driver(
        [
            {"action": "julgar", "args": {}},
            {"action": "marcar_grade", "args": {"pergunta": "RF-01"}},
            {"action": "marcar_grade", "args": {"pergunta": "RF-02"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        _resposta_do_juiz(),
        device,
    )
    await driver.run("avaliar os requisitos do formulário", start_url=FORM)

    def prompt(i: int) -> str:
        return ctx.passo.calls[i]["messages"][-1].content

    assert "GRADE NESTA PÁGINA" in prompt(0) and "Sem julgamento: RF-01, RF-02" in prompt(0)
    assert "Já julgadas: RF-01, RF-02 → `marcar_grade`" in prompt(1)
    assert "GRADE NESTA PÁGINA" not in prompt(3), "tudo marcado: o aviso some"
    sistema = ctx.passo.calls[0]["messages"][0].content
    assert "- julgar " in sistema and "- marcar_grade " in sistema


@pytest.mark.asyncio
async def test_marcar_sem_julgamento_e_recusado():
    device = _Form()
    driver, _ = _driver(
        [
            {"action": "marcar_grade", "args": {"pergunta": "RF-01"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        None,
        device,
    )
    result = await driver.run("avaliar os requisitos do formulário", start_url=FORM)

    assert device.cliques == []
    falha = [s for s in result.steps if s.action == "marcar_grade" and not s.ok]
    assert falha and "`julgar` primeiro" in falha[0].detail


@pytest.mark.asyncio
async def test_linha_trocada_pelo_agente_aparece_como_escrita_pelo_neo_na_conferencia():
    """O agente pode seguir o Daniel e trocar uma célula — mas não em silêncio."""
    device = _Form()
    driver, _ = _driver(
        [
            {"action": "julgar", "args": {"perguntas": ["RF-01"]}},
            {"action": "marcar_grade", "args": {"pergunta": "RF-01", "linhas": {"Completude": "Parcialmente"}}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        {"perguntas": _resposta_do_juiz()["perguntas"][:1]},
        device,
        falas="no rf 1 a completude é parcialmente, não não conforme",
    )
    await driver.run("avaliar os requisitos do formulário", start_url=FORM)

    assert device.marcadas["RF-01"]["Completude"] == "Parcialmente"
    assert device.marcadas["RF-01"]["Clareza"] == "Conforme", "o resto continua o do juiz"
    linhas = driver._linhas_de_revisao()
    assert any("RF-01 · Completude» → Parcialmente — ⚠ escrito pelo Neo" in l for l in linhas)


@pytest.mark.asyncio
async def test_julgar_numa_pagina_sem_grade_e_recusado_sem_chamar_o_juiz():
    device = _Form()
    device.tela = lambda: {"url": FORM, "title": "x", "ready": "complete", "stop": False,
                           "elements": [{"ref": 1, "tag": "input", "text": "Nome"}]}
    driver, ctx = _driver(
        [{"action": "julgar", "args": {}}, {"action": "done", "args": {"answer": "ok"}}],
        _resposta_do_juiz(),
        device,
    )
    result = await driver.run("avaliar", start_url=FORM)

    assert ctx.juiz.calls == []
    recusa = [s for s in result.steps if s.action == "julgar" and not s.ok]
    assert recusa and "GRADE" in recusa[0].detail


@pytest.mark.asyncio
async def test_a_evidencia_e_o_texto_das_outras_abas_e_as_notas_feitas_nelas():
    device = _Form()
    device.textos_das_abas[99] = "Perfil  Nome de Exibição Neo Teste  Membro desde 11/11/2026  Sair"
    driver = BrowserLocalDriver(act=device.act, llm=None, model="x")
    driver._abas = {
        "A": Aba(rotulo="A", tab_ref=42, origem="inicial", url=FORM),
        "B": Aba(rotulo="B", tab_ref=99, origem="agente", url="https://appdemo.exemplo.app/profile"),
    }
    driver._ativa = "A"
    driver.notes = ["AppDemo /profile: menu Sair visível [aba B]", "PÁGINA 2: RF-01 a RF-06 [aba A]"]

    [bloco] = await driver._evidencias_das_outras_abas(500)

    assert "aba B" in bloco and "Membro desde 11/11/2026" in bloco
    assert "menu Sair visível" in bloco and "PÁGINA 2" not in bloco
    assert "podem ter conclusões erradas" in bloco


# ── quem julga: o slot do painel (aba Modelos), padrão Haiku 5.5 ────────────


def test_o_juiz_e_um_slot_da_aba_modelos_com_haiku_de_padrao():
    from modules.model_config.store import ROLES

    slot = ROLES["browser_local_judge"]
    assert (slot.default_provider, slot.default_model) == ("openrouter", "anthropic/claude-haiku-5.5")
    assert "juiz" in slot.label


def test_o_select_do_juiz_oferece_os_tres_claude_mesmo_fora_do_catalogo():
    """O catálogo só é semeado com o banco vazio; o card do slot traz as opções dele."""
    from modules.model_config.store import build_catalog

    payload = build_catalog(["openrouter"], {})
    [card] = [r for r in payload["roles"] if r["key"] == "browser_local_judge"]
    assert card["suggested_models"] == {"openrouter": [
        "anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5",
    ]}
    assert card["current"] == {"provider": "openrouter", "model": "anthropic/claude-haiku-5.5"}
    outros = [r for r in payload["roles"] if r["key"] != "browser_local_judge"]
    assert all(r["suggested_models"] == {} for r in outros)


@pytest.mark.asyncio
async def test_sem_escolha_no_painel_o_runner_usa_o_padrao_do_slot():
    from core.jobs.runner_browser_local import _resolver_juiz

    assert await _resolver_juiz(None) == ("openrouter", "anthropic/claude-haiku-5.5")


def test_o_juiz_vai_pelo_provider_escolhido_no_painel():
    pedidos: list[str] = []

    class _Router:
        def get_provider(self, nome):
            pedidos.append(nome)
            return f"provider:{nome}"

        def get_provider_for_model(self, modelo):
            return "pelo-catalogo"

    assert juiz_mod._provider(_Router(), "anthropic/claude-haiku-5.5", "openrouter") == "provider:openrouter"
    assert pedidos == ["openrouter"]


# ── job a2242336 (12/11 21:13): o que custou passos e deixou célula sem juiz ──


class _JuizQueQuebraOJson(_FakeProvider):
    """1ª resposta com aspas duplas sem escape no "porque" — como 3 das 15 do job."""

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        from tests.browser_local.test_driver_vision import _FakeResponse

        if len(self.calls) == 1:
            return _FakeResponse('{"perguntas": [{"chave": "RF-01", "linhas": {"Clareza": '
                                 '{"resposta": "Conforme", "porque": "diz "Sair" claramente"}}}]}')
        return _FakeResponse(json.dumps(self.steps.pop(0)))


@pytest.mark.asyncio
async def test_json_quebrado_do_juiz_ganha_uma_segunda_chance_sem_gastar_passo():
    device = _Form()
    passo = _FakeProvider([
        {"action": "julgar", "args": {}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    o_juiz = _JuizQueQuebraOJson([_resposta_do_juiz()])
    driver = BrowserLocalDriver(
        act=device.act, llm=_FakeLLM({"juiz-teste": o_juiz}, passo), model="passo-barato",
        juiz_modelo="juiz-teste",
    )
    result = await driver.run("avaliar os requisitos", start_url=FORM)

    assert len(o_juiz.calls) == 2
    correcao = o_juiz.calls[1]["messages"][-1].content
    assert "não é um JSON válido" in correcao and "« »" in correcao
    assert [s.ok for s in result.steps if s.action == "julgar"] == [True]
    assert set(driver.julgamentos) == {"rf-01", "rf-02"}
    assert "NUNCA aspas duplas" in juiz_mod.SISTEMA


@pytest.mark.asyncio
async def test_pergunta_com_veredito_parcial_e_julgada_de_novo_e_as_linhas_se_juntam():
    """RNF-09: o juiz devolveu 2 das 4 linhas, e o `julgar` seguinte dizia 'já julgada'."""
    parcial = _resposta_do_juiz()
    parcial["perguntas"][1]["linhas"] = {
        k: v for k, v in parcial["perguntas"][1]["linhas"].items() if k in ("Clareza", "Completude")
    }
    resto = {"perguntas": [{"chave": "RF-02", "linhas": {
        "Ausência de Ambiguidade": {"resposta": "Parcialmente", "porque": "x"},
        "Verificabilidade": {"resposta": "Não Conforme", "porque": "y"},
    }}]}
    device = _Form()
    passo = _FakeProvider([
        {"action": "julgar", "args": {}},
        {"action": "marcar_grade", "args": {"pergunta": "RF-02"}},
        {"action": "julgar", "args": {"perguntas": ["RF-02"]}},
        {"action": "marcar_grade", "args": {"pergunta": "RF-02"}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    o_juiz = _FakeProvider([parcial, resto])
    driver = BrowserLocalDriver(
        act=device.act, llm=_FakeLLM({"juiz-teste": o_juiz}, passo), model="passo-barato",
        juiz_modelo="juiz-teste",
    )
    await driver.run("avaliar os requisitos", start_url=FORM)

    assert len(o_juiz.calls) == 2, "a segunda chamada só para o RF-02"
    assert "RF-01" not in o_juiz.calls[1]["messages"][-1].content.split("PERGUNTAS A JULGAR")[1]
    assert device.marcadas["RF-02"] == {
        "Clareza": "Conforme", "Ausência de Ambiguidade": "Parcialmente",
        "Completude": "Parcialmente", "Verificabilidade": "Não Conforme",
    }
    assert driver.julgamentos["rf-02"].faltaram == []


@pytest.mark.asyncio
async def test_grade_ja_preenchida_por_outra_tarefa_aparece_no_aviso_depois_que_ha_julgamento():
    """RF-15/16 vieram marcados do rascunho de outra tarefa e nunca passaram pelo juiz."""
    device = _Form()
    device.marcadas = {"RF-02": {l: "Conforme" for l in LINHAS}}
    driver, ctx = _driver(
        [
            {"action": "julgar", "args": {"perguntas": ["RF-01"]}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        {"perguntas": _resposta_do_juiz()["perguntas"][:1]},
        device,
    )
    await driver.run("avaliar os requisitos", start_url=FORM)

    antes = ctx.passo.calls[0]["messages"][-1].content
    depois = ctx.passo.calls[1]["messages"][-1].content
    assert "SEM julgamento nesta tarefa" not in antes, "sem julgar nada, pode ser o Daniel respondendo"
    assert "Já preenchidas, mas SEM julgamento nesta tarefa: RF-02" in depois


@pytest.mark.asyncio
async def test_clique_que_troca_celula_julgada_e_recusado_e_o_mesmo_valor_passa():
    """O ref 71 que era "Avançar" na página anterior era o rádio do RF-17 nesta."""
    device = _Form()
    [rf01, _] = ler_grades(_pagina())
    verif = rf01.linha("Verificabilidade")
    driver, _ = _driver(
        [
            {"action": "julgar", "args": {}},
            {"action": "click", "args": {"ref": verif.radios["Não Aplicável"]}},
            {"action": "click", "args": {"ref": verif.radios["Conforme"]}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        _resposta_do_juiz(),
        device,
    )
    result = await driver.run("avaliar os requisitos", start_url=FORM)

    cliques = [s for s in result.steps if s.action == "click"]
    assert not cliques[0].ok and "NÃO é botão" in cliques[0].detail and "RF-01" in cliques[0].detail
    assert cliques[1].ok
    assert device.cliques == [verif.radios["Conforme"]]
    # Sessão 8e17b81e: a recusa ensinava o atalho (`marcar_grade` com `linhas`).
    assert "com `linhas`" not in cliques[0].detail
    assert "dá para testar entrar e sair" in cliques[0].detail, "o porquê do juiz vai junto"


# ── sessão 8e17b81e (14/11): RF-06 com 1 de 4 linhas e o veredito "lembrado" ───


def _so_a_clareza_do_rf02() -> dict:
    parcial = _resposta_do_juiz()
    parcial["perguntas"][1]["linhas"] = {"Clareza": {"resposta": "Parcialmente", "porque": "«a inicial» de qual nome?"}}
    return parcial


_RESTO_DO_RF02 = {"perguntas": [{"chave": "RF-02", "linhas": {
    "Clareza": {"resposta": "Parcialmente", "porque": "«a inicial» de qual nome?"},
    "Ausência de Ambiguidade": {"resposta": "Parcialmente", "porque": "x"},
    "Completude": {"resposta": "Parcialmente", "porque": "y"},
    "Verificabilidade": {"resposta": "Conforme", "porque": "z"},
}}]}


@pytest.mark.asyncio
async def test_marcar_grade_rejulga_sozinho_as_linhas_que_faltaram():
    """O modelo não precisa obedecer ao "`julgar` de novo": o código pede ao juiz."""
    device = _Form()
    passo = _FakeProvider([
        {"action": "julgar", "args": {}},
        {"action": "marcar_grade", "args": {"pergunta": "RF-02"}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    o_juiz = _FakeProvider([_so_a_clareza_do_rf02(), _RESTO_DO_RF02])
    driver = BrowserLocalDriver(
        act=device.act, llm=_FakeLLM({"juiz-teste": o_juiz}, passo), model="passo-barato",
        juiz_modelo="juiz-teste",
    )
    result = await driver.run("avaliar os requisitos", start_url=FORM)

    assert len(o_juiz.calls) == 2
    assert "RF-01" not in o_juiz.calls[1]["messages"][-1].content.split("PERGUNTAS A JULGAR")[1]
    assert device.marcadas["RF-02"] == {
        "Clareza": "Parcialmente", "Ausência de Ambiguidade": "Parcialmente",
        "Completude": "Parcialmente", "Verificabilidade": "Conforme",
    }
    [marcou] = [s for s in result.steps if s.action == "marcar_grade"]
    assert "sem veredito" not in marcou.detail


@pytest.mark.asyncio
async def test_rejulga_uma_vez_so_e_depois_manda_nao_chutar():
    device = _Form()
    passo = _FakeProvider([
        {"action": "julgar", "args": {}},
        {"action": "marcar_grade", "args": {"pergunta": "RF-02"}},
        {"action": "marcar_grade", "args": {"pergunta": "RF-02"}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    o_juiz = _FakeProvider([_so_a_clareza_do_rf02(), _so_a_clareza_do_rf02()])
    driver = BrowserLocalDriver(
        act=device.act, llm=_FakeLLM({"juiz-teste": o_juiz}, passo), model="passo-barato",
        juiz_modelo="juiz-teste",
    )
    result = await driver.run("avaliar os requisitos", start_url=FORM)

    assert len(o_juiz.calls) == 2, "uma rejulgada por pergunta, não uma por `marcar_grade`"
    ultima = [s for s in result.steps if s.action == "marcar_grade"][-1]
    assert "nem na segunda vez" in ultima.detail and "não chute" in ultima.detail
    assert device.marcadas["RF-02"] == {"Clareza": "Parcialmente"}


@pytest.mark.asyncio
async def test_linhas_nao_troca_o_veredito_do_juiz_por_conta_propria():
    """Passos 26–28: o modelo "lembrava" Clareza=Conforme e trocou o «Parcialmente» do juiz."""
    device = _Form()
    driver, _ = _driver(
        [
            {"action": "julgar", "args": {}},
            {"action": "marcar_grade", "args": {"pergunta": "RF-01"}},
            {"action": "marcar_grade", "args": {"pergunta": "RF-01", "linhas": {"Completude": "Conforme"}}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        _resposta_do_juiz(),
        device,
    )
    result = await driver.run("avaliar os requisitos", start_url=FORM)

    assert device.marcadas["RF-01"]["Completude"] == "Não Conforme"
    ultima = [s for s in result.steps if s.action == "marcar_grade"][-1]
    assert "NÃO troquei Completude (o juiz decidiu «Não Conforme»" in ultima.detail
    assert "credencial errada" in ultima.detail


def test_daniel_citou_a_pergunta():
    device = _Form()
    driver, _ = _driver([], None, device, falas="o RF-06 tá errado, e o rnf 9 também")
    assert driver._daniel_citou("RF-06") and driver._daniel_citou("RNF-09")
    assert not driver._daniel_citou("RF-60") and not driver._daniel_citou("RF-01")
    driver.mensagens_do_daniel.append(SimpleNamespace(texto="marca o rf01 como conforme"))
    assert driver._daniel_citou("RF-01")


# ── o juiz não responde sobre o Daniel (2026-11-14) ─────────────────────────


def _pessoal_na_clareza_do_rf01() -> dict:
    dados = _resposta_do_juiz()
    dados["perguntas"][0]["linhas"]["Clareza"] = {
        "resposta": "PESSOAL", "porque": "pergunta o quanto o Daniel achou claro: é opinião dele",
    }
    return dados


def test_conferir_separa_a_linha_pessoal_sem_contar_como_falta():
    questoes = ler_grades(_pagina())
    out = juiz_mod.conferir(_pessoal_na_clareza_do_rf01(), [(q, q.rotulo) for q in questoes])
    rf01 = out.julgamentos["rf-01"]
    assert "Clareza" in rf01.pessoais and "Clareza" not in rf01.linhas
    assert rf01.faltaram == [], "não é falta: rejulgar não adiantaria"
    assert not out.problemas
    assert "PESSOAL" in rf01.resumo()


def test_coluna_que_se_chama_pessoal_continua_sendo_coluna():
    questoes = ler_grades(_pagina())
    for q in questoes:
        for linha in q.linhas:
            linha.radios["Pessoal"] = 999
    dados = _resposta_do_juiz()
    dados["perguntas"][0]["linhas"]["Clareza"]["resposta"] = "Pessoal"
    out = juiz_mod.conferir(dados, [(q, q.rotulo) for q in questoes])
    assert out.julgamentos["rf-01"].linhas["Clareza"][0] == "Pessoal"
    assert not out.julgamentos["rf-01"].pessoais


@pytest.mark.asyncio
async def test_linha_pessoal_nao_e_marcada_nem_rejulgada_e_aceita_a_resposta_dele():
    device = _Form()
    passo = _FakeProvider([
        {"action": "julgar", "args": {}},
        {"action": "marcar_grade", "args": {"pergunta": "RF-01"}},
        {"action": "marcar_grade", "args": {"pergunta": "RF-01", "linhas": {"Clareza": "Parcialmente"}}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    o_juiz = _FakeProvider([_pessoal_na_clareza_do_rf01()])
    driver = BrowserLocalDriver(
        act=device.act, llm=_FakeLLM({"juiz-teste": o_juiz}, passo), model="passo-barato",
        juiz_modelo="juiz-teste",
    )
    result = await driver.run("avaliar os requisitos", start_url=FORM)

    assert len(o_juiz.calls) == 1, "PESSOAL não é veredito pela metade"
    julgar = next(s for s in result.steps if s.action == "julgar")
    assert "PESSOAL em RF-01" in julgar.detail and "`consultar`" in julgar.detail
    primeiro, segundo = [s for s in result.steps if s.action == "marcar_grade"]
    assert "PESSOAL (é sobre o Daniel" in primeiro.detail and "sem veredito" not in primeiro.detail
    assert "Clareza" not in device.marcadas["RF-01"] or device.marcadas["RF-01"]["Clareza"] == "Parcialmente"
    assert segundo.ok and device.marcadas["RF-01"]["Clareza"] == "Parcialmente"
    assert "NÃO troquei" not in segundo.detail
    linhas = driver._linhas_de_revisao()
    assert any("RF-01 · Clareza» → Parcialmente — ⚠ escrito pelo Neo" in l for l in linhas)


def test_o_juiz_nao_fala_so_de_requisitos():
    assert "PESSOAL" in juiz_mod.SISTEMA
    assert "os que a PÁGINA define" in juiz_mod.SISTEMA
    assert "o sistema avaliado" not in juiz_mod.montar_pedido([], pagina="x", evidencias=[], instrucoes="")
