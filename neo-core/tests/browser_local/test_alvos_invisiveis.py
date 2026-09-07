"""O buraco do job 9968e68a: o que a página tem e o inventário não enxerga.

Um questionário com 5 alternativas devolveu 56 elementos e NENHUM deles era uma
alternativa — as opções eram `<input type=radio>` escondidos por CSS dentro de
contêineres sem papel. O modelo leu a questão, escolheu a resposta certa e,
obrigado a citar um número que não existia, clicou no menu lateral cinco vezes.

Aqui ficam as travas do lado do core. O inventário em si (as três passadas do
snapshot) mora no device, em `services/browser_local.rs`.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local.driver import ACTIONS, BrowserLocalDriver

from .test_driver import _driver, _FakeLLM, _FakeProvider, _snapshot


def _alternativa(ref: int, texto: str, *, marcado: bool = False, **extra) -> dict:
    """O rosto visível de um rádio escondido — o padrão que cegou o driver."""
    item = {
        "ref": ref,
        "tag": "label",
        "text": texto,
        "checked": marcado,
        "controla": "radio[q1]",
    }
    item.update(extra)
    return item


# ── o que o modelo LÊ ────────────────────────────────────────────────────────


def test_controle_escondido_aparece_como_selecionavel():
    linha = BrowserLocalDriver._element_line(_alternativa(18, "O racismo sistemático resultou"))
    assert "[desmarcado]" in linha
    # Sem isto, "clicar num label" não parece ter nada a ver com "marcar a opção".
    assert "[seleciona radio[q1]]" in linha


def test_elemento_coberto_avisa_quem_esta_na_frente():
    linha = BrowserLocalDriver._element_line(
        {"ref": 4, "tag": "button", "text": "Responder", "coberto": "Aceita cookies?"}
    )
    assert "COBERTO" in linha and "Aceita cookies?" in linha


def test_conteudo_principal_vem_antes_do_texto_do_corpo():
    """O corte de 3000 chars em ordem de documento entregava só a moldura."""
    render = BrowserLocalDriver._render(
        _snapshot(
            main={"label": "QUESTÃO 1", "text": "Alternativas\nO racismo sistemático resultou"},
            text="Início Extensão Estágio Financeiro Bibliotecas",
        )
    )
    assert "CONTEÚDO PRINCIPAL DA PÁGINA" in render
    assert "QUESTÃO 1" in render
    assert render.index("O racismo sistemático") < render.index("Texto da página")


def test_sem_conteudo_principal_o_render_nao_muda():
    render = BrowserLocalDriver._render(_snapshot())
    assert "CONTEÚDO PRINCIPAL" not in render


# ── o que conta como "a tela mudou" ─────────────────────────────────────────


def test_marcar_uma_alternativa_conta_como_mudanca_de_tela():
    """Selecionar não muda URL, nem título, nem texto — só o estado do controle.

    Sem isto o freio de repetição dá o passo por inútil JUSTAMENTE quando ele
    funcionou, e recusa a próxima ação do modelo em cima disso.
    """
    antes = _snapshot(elements=[_alternativa(0, "opção A"), _alternativa(1, "opção B")])
    depois = _snapshot(
        elements=[_alternativa(0, "opção A", marcado=True), _alternativa(1, "opção B")]
    )
    assert BrowserLocalDriver._fingerprint(antes) != BrowserLocalDriver._fingerprint(depois)


def test_botao_de_enviar_habilitando_conta_como_mudanca():
    """O `disabled` do "Responder" é o oráculo grátis de "a seleção entrou"."""
    antes = _snapshot(
        elements=[{"ref": 0, "tag": "button", "text": "Responder", "disabled": True}]
    )
    depois = _snapshot(elements=[{"ref": 0, "tag": "button", "text": "Responder"}])
    assert BrowserLocalDriver._fingerprint(antes) != BrowserLocalDriver._fingerprint(depois)


# ── click_text: a saída quando não há número ────────────────────────────────


def test_click_text_existe_e_pede_texto():
    assert ACTIONS["click_text"] == ("texto",)


@pytest.mark.asyncio
async def test_click_text_vira_comando_no_device():
    acted: list = []
    driver = _driver(
        [
            {"action": "click_text", "args": {"texto": "O racismo sistemático resultou"}},
            {"action": "done", "args": {"answer": "marquei a alternativa"}},
        ],
        acted=acted,
    )
    result = await driver.run("marca a alternativa correta")
    assert result.status == "completed"
    enviados = [args for nome, args in acted if nome == "browser.click_text"]
    assert enviados and enviados[0]["texto"] == "O racismo sistemático resultou"


@pytest.mark.asyncio
async def test_click_text_curto_demais_e_recusado_antes_de_tocar_na_pagina():
    acted: list = []
    driver = _driver(
        [
            {"action": "click_text", "args": {"texto": "ok"}},
            {"action": "done", "args": {"answer": "desisti"}},
        ],
        acted=acted,
    )
    await driver.run("clica em algo")
    assert not any(a == "browser.click_text" for a, _ in acted)


@pytest.mark.asyncio
async def test_click_text_passa_pelas_mesmas_guardas_de_dinheiro():
    """Trocar de ação não pode ser um jeito de furar a trava do clique caro."""
    acted: list = []
    driver = _driver(
        [
            {"action": "click_text", "args": {"texto": "Finalizar compra"}},
            {"action": "done", "args": {"answer": "parei"}},
        ],
        acted=acted,
    )
    await driver.run("compra o produto")
    assert not any(a == "browser.click_text" for a, _ in acted)


# ── evidência de efeito ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clique_que_selecionou_diz_que_selecionou():
    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 1}
        if action == "browser.snapshot":
            return _snapshot(elements=[_alternativa(0, "opção certa")])
        if action == "browser.click":
            return {"ok": True, "label": "opção certa", "checked": True}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM(
            _FakeProvider(
                [
                    {"action": "click", "args": {"ref": 0}},
                    {"action": "done", "args": {"answer": "marquei"}},
                ]
            )
        ),
        model="fake",
    )
    result = await driver.run("marca a opção certa")
    passo = next(s for s in result.steps if s.action == "click")
    assert "SELECIONADO" in (passo.detail or "")


@pytest.mark.asyncio
async def test_clique_que_nao_selecionou_aponta_a_saida():
    """`ok:true` com `checked:false` é o clique que não pegou — e tem alternativa."""

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 1}
        if action == "browser.snapshot":
            return _snapshot(elements=[_alternativa(0, "opção certa")])
        if action == "browser.click":
            return {"ok": True, "label": "opção certa", "checked": False}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM(
            _FakeProvider(
                [
                    {"action": "click", "args": {"ref": 0}},
                    {"action": "done", "args": {"answer": "tentei"}},
                ]
            )
        ),
        model="fake",
    )
    result = await driver.run("marca a opção certa")
    passo = next(s for s in result.steps if s.action == "click")
    assert "NÃO ficou selecionado" in (passo.detail or "")
    assert "click_text" in (passo.detail or "")


# ── clique em elemento coberto ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clique_em_elemento_coberto_e_recusado():
    acted: list = []
    coberto = _snapshot(
        elements=[{"ref": 0, "tag": "button", "text": "Responder", "coberto": "Aceita cookies?"}]
    )
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 0}},
            {"action": "done", "args": {"answer": "parei"}},
        ],
        acted=acted,
        snapshot=coberto,
    )
    await driver.run("clica em responder")
    assert not any(a == "browser.click" for a, _ in acted)


# ── os olhos não podem inventar número ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_visao_recebe_a_lista_de_numeros_que_existem():
    """O device diz QUAIS refs desenhou; a lista vai no prompt dos olhos.

    Sem ela, "cite o número" é um convite a inventar quando o elemento não tem
    marcação — e inventar aqui não falha, clica em outra coisa (job 9968e68a:
    "ref 18" era um item do menu lateral, e o agente clicou lá cinco vezes).
    """
    from .test_driver_vision import _FakeLLM as _LLMVisao
    from .test_driver_vision import _FakeProvider as _ProviderVisao

    olho = _ProviderVisao([])
    olho.answer = "a terceira alternativa, a que começa com 'O racismo sistemático'"  # type: ignore[attr-defined]
    texto = _ProviderVisao(
        [
            {"action": "look", "args": {"pergunta": "qual alternativa está marcada?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot(elements=[_alternativa(0, "opção A")])
        if action == "browser.screenshot":
            return {"ok": True, "data": "UFJJTlQ=", "marks": True, "marks_refs": [0, 1, 45]}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_LLMVisao({"visao": olho, "texto": texto}, texto),
        model="texto",
        vision_model="visao",
    )
    await driver.run("confere a alternativa")

    prompt = olho.calls[0]["messages"][-1].content
    assert "0, 1, 45" in prompt
    assert "os únicos que existem" in prompt


@pytest.mark.asyncio
async def test_print_sem_marcacao_avisa_os_olhos_em_vez_de_calar():
    """Device antigo (ou print sem marcas) não pode virar número inventado."""
    from .test_driver_vision import _FakeLLM as _LLMVisao
    from .test_driver_vision import _FakeProvider as _ProviderVisao

    olho = _ProviderVisao([])
    olho.answer = "vejo cinco alternativas"  # type: ignore[attr-defined]
    texto = _ProviderVisao(
        [
            {"action": "look", "args": {"pergunta": "o que tem na tela?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.screenshot":
            return {"ok": True, "data": "UFJJTlQ=", "marks": False, "marks_refs": []}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_LLMVisao({"visao": olho, "texto": texto}, texto),
        model="texto",
        vision_model="visao",
    )
    await driver.run("olha a tela")

    prompt = olho.calls[0]["messages"][-1].content
    assert "NÃO TEM NENHUM NÚMERO DESENHADO" in prompt


def test_texto_do_corpo_nao_repete_o_conteudo_principal():
    """Repetir as mesmas linhas duas vezes é orçamento de prompt jogado fora.

    Não é recorte: cada linha continua no prompt, uma vez só.
    """
    corpo = "Início\nExtensão\nQUESTÃO 1\n" + "\n".join(f"link {i}" for i in range(60))
    render = BrowserLocalDriver._render(
        _snapshot(main={"label": "QUESTÃO 1", "text": "QUESTÃO 1"}, text=corpo)
    )
    depois = render.split("Texto da página:")[1]
    assert "QUESTÃO 1" not in depois
    assert "Extensão" in depois


def test_dedup_nao_esvazia_o_texto_do_corpo():
    """Se o principal é quase a página toda, o corpo volta inteiro."""
    render = BrowserLocalDriver._render(
        _snapshot(main={"label": "Tudo", "text": "linha A\nlinha B"}, text="linha A\nlinha B")
    )
    assert "linha A" in render.split("Texto da página:")[1]


# ── a janela entre o deploy do core e o restart do desktop ──────────────────


@pytest.mark.asyncio
async def test_device_antigo_nao_recebe_click_text():
    """O core faz deploy sozinho; o Rust do desktop só depois de reiniciar.

    Nesse intervalo o core conhece uma ação que o navegador não implementa —
    e "ação de navegador desconhecida" dentro de um laço de 25 passos é o pior
    jeito possível de descobrir isso.
    """
    acted: list = []
    antigo = _snapshot()  # device velho: sem o campo `main`
    antigo.pop("main", None)
    driver = _driver(
        [
            {"action": "click_text", "args": {"texto": "alguma opção"}},
            {"action": "done", "args": {"answer": "parei"}},
        ],
        acted=acted,
        snapshot=antigo,
    )
    await driver.run("clica na opção")
    assert not any(a == "browser.click_text" for a, _ in acted)


@pytest.mark.asyncio
async def test_device_novo_recebe_click_text():
    acted: list = []
    novo = _snapshot()  # o fixture já representa um device atual (manda `main`)
    driver = _driver(
        [
            {"action": "click_text", "args": {"texto": "alguma opção"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        acted=acted,
        snapshot=novo,
    )
    await driver.run("clica na opção")
    assert any(a == "browser.click_text" for a, _ in acted)
