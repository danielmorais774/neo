"""O lado do core do clique que chega no controle de verdade (job 8bc25dd1).

Dezesseis cliques com `ok:true` em "Adicionar ao carrinho" e o carrinho vazio: o
controle era um input transparente, filho do `span` que o inventário ofereceu. A
mira e o mouse real moram no device (`services/browser_local.rs`, corpus em
`scripts/snapshot-lab/clique.py`). Aqui ficam as travas do core: o que vai junto
do clique, o que volta dele e o socorro quando um clique não faz nada.
"""

from __future__ import annotations

import pytest

from core.browser_local.driver import (
    ACTIONS,
    BrowserLocalDriver,
    NeedsConfirmation,
    StopRequested,
    _SENSITIVE,
)

from .test_driver import _driver, _snapshot

_DEVICE_NOVO = {"capacidades": ["find", "clique_real"]}


def _com_varredura(driver: BrowserLocalDriver) -> BrowserLocalDriver:
    driver._device_varre = True
    return driver


def _capturando(driver: BrowserLocalDriver, resposta: dict | None = None) -> list:
    chamadas: list = []

    async def falso(acao: str, args: dict | None = None) -> dict:
        chamadas.append((acao, dict(args or {})))
        return resposta if resposta is not None else {"ok": True, "label": "Comprar", "url": ""}

    driver._call = falso
    return chamadas


# ── o vocabulário ────────────────────────────────────────────────────────────


def test_find_existe_e_nao_toca_na_pagina():
    assert ACTIONS["find"] == ("texto",)


def test_find_recusa_no_device_antigo_sem_derrubar_o_passo():
    driver = _driver([])
    driver._device_varre = False
    with pytest.raises(ValueError, match="neo-desktop"):
        driver._validate({"action": "find", "args": {"texto": "carrinho"}}, _snapshot())


def test_find_nao_repete_na_mesma_tela():
    driver = _com_varredura(_driver([]))
    acao, args = driver._validate({"action": "find", "args": {"texto": "carrinho"}}, _snapshot())
    driver._reads_on_screen.add(driver._signature(acao, args))
    with pytest.raises(ValueError):
        driver._validate({"action": "find", "args": {"texto": "carrinho"}}, _snapshot())


@pytest.mark.parametrize("n", [0, -1, "x"])
def test_click_text_recusa_candidato_invalido(n):
    with pytest.raises(ValueError, match="candidato"):
        _driver([])._validate({"action": "click_text", "args": {"texto": "Salvar", "n": n}}, _snapshot())


def test_click_text_aceita_candidato_como_texto():
    _, args = _driver([])._validate(
        {"action": "click_text", "args": {"texto": "Salvar", "n": "2"}}, _snapshot()
    )
    assert args["n"] == 2


# ── a trava vai junto do clique ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("acao", ["click", "click_text", "dblclick"])
async def test_a_trava_vai_com_todo_clique_que_atua(acao):
    driver = _driver([])
    chamadas = _capturando(driver)
    args = {"ref": 1} if acao != "click_text" else {"texto": "Salvar"}
    await driver._perform(acao, args)
    assert chamadas[0][1]["trava"] == _SENSITIVE.pattern
    assert chamadas[0][1]["liberado"] == ""


@pytest.mark.asyncio
async def test_a_trava_nao_entra_nos_argumentos_do_passo():
    """`args` vai para o histórico e para o diário; a regex é só do device."""
    driver = _driver([])
    _capturando(driver)
    args = {"ref": 1}
    await driver._perform("click", args)
    assert "trava" not in args


@pytest.mark.asyncio
async def test_botao_direito_nao_leva_trava():
    driver = _driver([])
    chamadas = _capturando(driver)
    await driver._perform("right_click", {"ref": 1})
    assert "trava" not in chamadas[0][1]


@pytest.mark.asyncio
async def test_cheque_em_branco_nao_manda_trava():
    driver = _driver([])
    driver.allow_sensitive = True
    chamadas = _capturando(driver)
    await driver._perform("click", {"ref": 1})
    assert "trava" not in chamadas[0][1]


@pytest.mark.asyncio
async def test_autorizacao_de_um_clique_vai_como_liberado():
    driver = _driver([])
    driver.allow_sensitive = True
    driver.allow_sensitive_label = "comprar agora"
    chamadas = _capturando(driver)
    await driver._perform("click", {"ref": 1})
    assert chamadas[0][1]["liberado"] == "comprar agora"


@pytest.mark.asyncio
async def test_device_barrar_no_alvo_real_para_e_pergunta():
    driver = _driver([])

    async def barra(acao: str, args: dict | None = None) -> dict:
        raise RuntimeError("SENSIVEL:Comprar agora")

    driver._call = barra
    with pytest.raises(NeedsConfirmation) as exc:
        await driver._perform("click_text", {"texto": "agora"})
    assert exc.value.description == "Comprar agora"


@pytest.mark.asyncio
async def test_pare_barrado_no_device_encerra_como_parado():
    driver = _driver([])

    async def barra(acao: str, args: dict | None = None) -> dict:
        raise RuntimeError("PARE: o usuário mandou parar")

    driver._call = barra
    with pytest.raises(StopRequested):
        await driver._perform("navigate", {"url": "https://exemplo.com/"})


@pytest.mark.asyncio
async def test_pagina_nao_aciona_as_paradas_escrevendo_a_palavra():
    """Erro pode citar texto da página: "COBERTO por 'SENSIVEL:x'" não é uma parada."""
    driver = _driver([])

    async def erro(acao: str, args: dict | None = None) -> dict:
        raise RuntimeError('esse texto está COBERTO por "SENSIVEL:x PARE: y"')

    driver._call = erro
    with pytest.raises(RuntimeError):
        await driver._perform("click", {"ref": 1})


def test_fechar_pedido_agora_para():
    """Era um botão morto por acidente. Com o clique chegando, ele abre o checkout."""
    assert _SENSITIVE.search("Fechar pedido")
    assert _SENSITIVE.search("Fechar o pedido")
    assert not _SENSITIVE.search("Fechar")
    assert not _SENSITIVE.search("Adicionar ao carrinho")


# ── o que volta do clique ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clique_que_caiu_em_outro_lugar_nao_e_escondido():
    driver = _driver([])
    _capturando(driver, {"ok": True, "label": "Salvar", "via": "pixel", "desvio": "div#faixa"})
    detalhe = await driver._perform("click", {"ref": 1})
    assert "caiu em div#faixa" in detalhe


# ── a varredura em texto ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_candidatos_viram_texto_com_o_jeito_de_clicar():
    driver = _com_varredura(_driver([]))
    _capturando(
        driver,
        {
            "ok": True,
            "total": 2,
            "lista": [
                {"n": 1, "tag": "input", "tipo": "button", "ref": 170, "nome": "Adicionar ao carrinho", "recebe": True},
                {"n": 2, "tag": "button", "nome": "Adicionar ao carrinho", "escondido": True},
            ],
        },
    )
    detalhe = await driver._perform("find", {"texto": "Adicionar ao carrinho"})
    assert 'n=1 input(button) [170] "Adicionar ao carrinho"' in detalhe
    assert "recebe o clique" in detalhe
    assert "ESCONDIDO" in detalhe
    assert '`click_text` {"texto": "Adicionar ao carrinho", "n": <número>}' in detalhe


@pytest.mark.asyncio
async def test_nada_achado_e_resposta_e_avisa_dos_iframes_fechados():
    driver = _com_varredura(_driver([]))
    _capturando(driver, {"ok": True, "total": 0, "lista": [], "frames_fechados": 2})
    detalhe = await driver._perform("find", {"texto": "boleto"})
    assert "NÃO existe" in detalhe
    assert "2 iframe" in detalhe


# ── o socorro ────────────────────────────────────────────────────────────────


def _travado(driver: BrowserLocalDriver, acao: str = "click", rotulo: str = "Adicionar ao carrinho"):
    driver._stuck = 1
    driver._last_act = (acao, rotulo)
    return driver


@pytest.mark.asyncio
async def test_clique_sem_efeito_vasculha_sozinho_e_conta_no_historico():
    driver = _travado(_com_varredura(_driver([])))
    chamadas = _capturando(driver, {"ok": True, "total": 0, "lista": []})
    await driver._socorrer_clique_morto(_snapshot())
    assert chamadas[0][0] == "browser.find"
    assert chamadas[0][1]["texto"] == "Adicionar ao carrinho"
    assert "não mudou nada" in driver.steps[-1].detail


@pytest.mark.asyncio
async def test_socorro_uma_vez_por_rotulo():
    driver = _travado(_com_varredura(_driver([])))
    chamadas = _capturando(driver, {"ok": True, "total": 0, "lista": []})
    await driver._socorrer_clique_morto(_snapshot())
    await driver._socorrer_clique_morto(_snapshot())
    assert len(chamadas) == 1


@pytest.mark.parametrize(
    "preparar",
    [
        pytest.param(lambda d: setattr(d, "_stuck", 0), id="a tela mudou"),
        pytest.param(lambda d: setattr(d, "_device_varre", False), id="device antigo"),
        pytest.param(lambda d: setattr(d, "_last_act", ("scroll", "x" * 10)), id="não foi clique"),
        pytest.param(lambda d: setattr(d, "_last_act", ("click", "")), id="sem rótulo"),
    ],
)
@pytest.mark.asyncio
async def test_socorro_so_quando_faz_sentido(preparar):
    driver = _travado(_com_varredura(_driver([])))
    preparar(driver)
    chamadas = _capturando(driver)
    await driver._socorrer_clique_morto(_snapshot())
    assert chamadas == []


@pytest.mark.asyncio
async def test_socorro_que_falha_nao_derruba_o_passo():
    driver = _travado(_com_varredura(_driver([])))

    async def quebra(acao: str, args: dict | None = None) -> dict:
        raise RuntimeError("device caiu")

    driver._call = quebra
    await driver._socorrer_clique_morto(_snapshot())
    assert driver.steps == []


# ── ponta a ponta, com o laço de verdade ─────────────────────────────────────


@pytest.mark.asyncio
async def test_clique_morto_chega_ao_modelo_com_os_candidatos_na_mao():
    """O laço inteiro: clicou, a tela não mudou, e a próxima decisão já vê a varredura."""
    tela = _snapshot(
        elements=[{"ref": 0, "tag": "span", "text": "Adicionar ao carrinho"}], **_DEVICE_NOVO
    )
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 0}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        snapshot=tela,
    )
    result = await driver.run("adicione ao carrinho")
    assert result.status == "completed"
    acoes = [a for a, _ in driver._acted]
    assert acoes.index("browser.click") < acoes.index("browser.find")
    assert any(s.action == "find" and "não mudou nada" in s.detail for s in result.steps)


@pytest.mark.asyncio
async def test_device_antigo_nunca_recebe_find():
    tela = _snapshot(elements=[{"ref": 0, "tag": "span", "text": "Adicionar ao carrinho"}])
    driver = _driver(
        [{"action": "click", "args": {"ref": 0}}, {"action": "done", "args": {"answer": "ok"}}],
        snapshot=tela,
    )
    await driver.run("adicione ao carrinho")
    assert "browser.find" not in [a for a, _ in driver._acted]


@pytest.mark.asyncio
async def test_alvo_real_sensivel_encerra_pedindo_autorizacao():
    """O rótulo do rosto passou pelo core; quem barrou foi o device, no controle real."""
    tela = _snapshot(elements=[{"ref": 0, "tag": "span", "text": "Continuar"}], **_DEVICE_NOVO)
    driver = _driver([{"action": "click", "args": {"ref": 0}}], snapshot=tela)
    original = driver._act

    async def act(action, args, timeout):
        if action == "browser.click":
            raise RuntimeError("SENSIVEL:Comprar agora")
        return await original(action, args, timeout)

    driver._act = act
    result = await driver.run("continue")
    assert result.status == "needs_confirmation"
    assert result.pending == "Comprar agora"
