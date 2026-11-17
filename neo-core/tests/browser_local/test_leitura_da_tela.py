"""O modelo lê o que está na tela — e consegue chegar no que não está.

Job 45e85695 (2026-10-31), notebooks na Amazon, janela de ~1900px. O carrinho é uma
faixa de 150px presa na borda direita, sem X, que não fecha. O device a elegeu
"conteúdo principal" em TODA página de produto: o prompt trazia o carrinho e 1200
caracteres de menu de departamentos. Dos 19 snapshots assim, 15 não tinham o preço
do produto e 4 só o traziam enterrado numa linha de variação. Fechar o painel era a
decisão racional para quem só via isso. Foram 196s, 5 Escapes, e o job morreu com 3
notas. O headless do mesmo dia, em 1280px (o carrinho fica fora da tela), coletou 12
itens em 100s.

O que está sob teste aqui é a parte do driver; a percepção em si roda num Chromium
de verdade, em `neo-desktop-tauri/scripts/snapshot-lab/leitura.py`.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local.driver import (
    _BUSCAS_VAZIAS_MAX,
    _SEM_GANHO_MAX,
    BrowserLocalDriver,
    _acao_em_palavras,
    _mesma_nota,
)

TELA = (
    "Notebook ASUS Vivobook 15, Intel Core i5\n"
    "R$ 3.399 00\n"
    "Em até 12x de R$ 283,25 sem juros\n"
    "Entrega GRÁTIS quarta-feira, 11 de Novembro\n"
    "Somente 3 em estoque."
)


class _Resp:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _Provider:
    def __init__(self, steps=None, *, answer: str | None = None) -> None:
        self.steps = list(steps or [])
        self.answer = answer
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.answer is not None:
            return _Resp(self.answer)
        step = self.steps.pop(0) if self.steps else {"action": "give_up", "args": {"reason": "fim"}}
        return _Resp(json.dumps(step))


class _LLM:
    def __init__(self, by_model: dict, default) -> None:
        self.by_model = by_model
        self.default = default

    def get_provider_for_model(self, model: str):
        return self.by_model.get(model, self.default)


def _snapshot(**over):
    base = {
        "url": "https://www.amazon.com.br/dp/B0F85LVW2W",
        "title": "Notebook ASUS",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "input", "text": "Pesquisar", "chrome": True, "value": "notebook"},
            {"ref": 1, "tag": "a", "text": "Ofertas do Dia", "chrome": True,
             "href": "https://www.amazon.com.br/deals?ref_=nav", "box": [0, 0, 100, 20]},
            {"ref": 2, "tag": "button", "text": "Adicionar ao carrinho", "box": [1500, 700, 240, 40]},
            {"ref": 3, "tag": "a", "text": "Ir para o carrinho", "lateral": "Subtotal", "chrome": True,
             "href": "https://www.amazon.com.br/cart", "box": [1770, 200, 120, 20]},
        ],
        "text": "Ir para Conteúdo principal Todos os departamentos",
        "text_cut": True,
        "main": {"label": "Patrocinado", "text": f"{TELA}\n1\n2\n3\nMarca ASUS\nCor Prata", "cut": False},
        "tela": TELA,
        "tela_cut": False,
        "tela_total": len(TELA),
        "resto": "Descrição do produto\nBateria de 42Wh\nAvaliações\nTeclado confortável",
        "resto_total": 70,
        "barras": [{"rotulo": "Subtotal", "texto": "Subtotal · R$ 54,90 · Ir para o carrinho"}],
        "iframes": [],
        "omitidos": 0,
        "truncated": False,
    }
    base.update(over)
    return base


def _driver(steps, *, snapshots=None, vision=None, resultado=None):
    pedidos: list = []
    fila = list(snapshots or [_snapshot()])

    async def act(action, args, timeout):
        pedidos.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return fila.pop(0) if len(fila) > 1 else fila[0]
        if action == "browser.screenshot":
            return {"ok": True, "data": "UFJJTlQ=", "marks": False}
        if action == "browser.extract":
            return resultado or {"ok": True, "text": "texto", "label": "x"}
        if action == "browser.click":
            return {"ok": True, "label": "ok"}
        return {"ok": True}

    texto = _Provider(steps)
    olho = _Provider(answer=vision) if vision else None
    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM({"olhos": olho} if olho else {}, texto),
        model="texto",
        vision_model="olhos" if olho else None,
    )
    driver._pedidos = pedidos  # type: ignore[attr-defined]
    driver._texto = texto  # type: ignore[attr-defined]
    driver._olho = olho  # type: ignore[attr-defined]
    return driver


# ── 1. o que o prompt mostra ────────────────────────────────────────────────


def test_a_tela_vem_antes_da_lista_de_elementos():
    render = BrowserLocalDriver._render(_snapshot())
    assert "NA TELA AGORA" in render
    assert "12x de R$ 283,25" in render
    assert render.index("NA TELA AGORA") < render.index("\nElementos (")


def test_barra_lateral_nao_vira_painel_aberto_nem_pede_para_fechar():
    """A causa do job: ">>> O PAINEL «Subtotal» ABRIU (...) pode fechar e seguir"."""
    snap = _snapshot()
    render = BrowserLocalDriver._render(snap, {})
    assert "BARRA LATERAL FIXA «Subtotal»" in render
    assert "normalmente não fecha" in render
    assert "O PAINEL" not in render
    assert "MENU/DIÁLOGO ABERTO" not in render
    assert BrowserLocalDriver._panels(snap) == {}, "barra lateral não é painel: nada para triar"


def test_o_que_a_tela_ja_mostrou_nao_se_repete():
    render = BrowserLocalDriver._render(_snapshot())
    assert render.count("12x de R$ 283,25") == 1
    assert "Marca ASUS" in render, "o que não está na tela continua aparecendo"


def test_opcao_de_select_nao_come_o_orcamento():
    """Quantidade 1 a 30, uma por linha, eram o miolo do conteúdo principal."""
    bloco = BrowserLocalDriver._render(_snapshot()).split("MAIS DO CONTEÚDO PRINCIPAL")[1]
    assert "\n1\n" not in bloco


def test_com_tela_o_dump_em_ordem_de_documento_sai():
    render = BrowserLocalDriver._render(_snapshot())
    assert "Texto da página:" not in render
    assert "Todos os departamentos" not in render


def test_device_antigo_continua_com_o_prompt_de_antes():
    """O core sobe sozinho; o desktop só quando o `tauri dev` reinicia."""
    velho = _snapshot()
    for chave in ("tela", "tela_cut", "tela_total", "resto", "resto_total", "barras", "iframes", "omitidos"):
        velho.pop(chave)
    render = BrowserLocalDriver._render(velho)
    assert "NA TELA AGORA" not in render
    assert "CONTEÚDO PRINCIPAL DA PÁGINA" in render
    assert "Texto da página:" in render


def test_moldura_vai_numa_linha_e_a_busca_do_site_nao():
    render = BrowserLocalDriver._render(_snapshot())
    assert "[moldura do site] [1] Ofertas do Dia" in render
    assert '[0] input "Pesquisar"' in render, "o campo de busca é o elemento mais útil da moldura"


def test_rastro_de_url_sai_e_query_curta_fica():
    origem = "https://www.amazon.com.br"
    item = {"ref": 1, "tag": "a", "text": "Produto",
            "href": f"{origem}/Notebook/dp/B0F?dib=" + "x" * 120 + "&sr=8-7"}
    assert "/Notebook/dp/B0F?…" in BrowserLocalDriver._element_line(item, None, origem)
    curto = {"ref": 2, "tag": "a", "text": "Vídeo", "href": f"{origem}/watch?v=abc123"}
    assert "/watch?v=abc123" in BrowserLocalDriver._element_line(curto, None, origem)


def test_link_repetido_sai_da_lista_mas_o_ref_continua_valendo():
    """A foto e o título do produto eram duas linhas iguais: 1 em cada 4 da lista."""
    url = "https://www.amazon.com.br/Notebook/dp/B0F"
    elementos = [
        {"ref": 7, "tag": "a", "text": "Notebook Lenovo", "href": f"{url}?ref=foto"},
        {"ref": 8, "tag": "a", "text": "Notebook Lenovo", "href": f"{url}?ref=titulo"},
        {"ref": 9, "tag": "a", "text": "R$ 3.447,00", "href": url},
        {"ref": 10, "tag": "span", "text": "Adicionar ao carrinho"},
        {"ref": 11, "tag": "span", "text": "Adicionar ao carrinho"},
    ]
    snap = _snapshot(elements=elementos)
    render = BrowserLocalDriver._render(snap)
    assert "[7] a" in render and "[8] a" not in render
    assert "[9] a" in render, "mesmo destino com outro texto é outra informação"
    assert "[10] span" in render and "[11] span" in render, "botão sem destino não é repetição"
    driver = _driver([])
    driver._validate({"action": "click", "args": {"ref": 8}}, snap)


def test_o_aviso_de_corte_aponta_uma_saida_que_existe():
    """Mandava `extract` no `ref` do bloco, e texto puro não tem ref."""
    render = BrowserLocalDriver._render(_snapshot(resto="z" * 3000, resto_total=9000))
    assert '`extract {"busca": "termo"}`' in render
    assert '`extract {"de":' in render


def test_iframe_de_outro_site_e_declarado():
    render = BrowserLocalDriver._render(
        _snapshot(iframes=[{"url": "https://pagamento.exemplo/x", "titulo": "Pagamento", "legivel": False, "texto": ""}])
    )
    assert "IFRAME de outro site" in render
    assert "https://pagamento.exemplo/x" in render


# ── 2. anotar junto da ação ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anotar_vai_junto_da_acao_e_nao_gasta_rodada():
    """12 das 28 rodadas do job 2d416462 foram só `note`."""
    driver = _driver(
        [
            {"anotar": ["ASUS — R$ 3.399,00", "Dell — R$ 3.999,00"], "action": "click", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    await driver.run("preço dos notebooks")
    assert driver.notes == ["ASUS — R$ 3.399,00", "Dell — R$ 3.999,00"]
    assert len(driver._texto.calls) == 2, "duas notas e um clique têm que caber em UMA rodada"


@pytest.mark.asyncio
async def test_anotacao_sobrevive_a_acao_recusada():
    driver = _driver(
        [
            {"anotar": "ASUS — R$ 3.399,00", "action": "click", "args": {"ref": 999}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    await driver.run("preço do notebook")
    assert driver.notes == ["ASUS — R$ 3.399,00"], "dado lido é dado lido"


@pytest.mark.asyncio
async def test_a_url_da_nota_nao_e_mais_cortada():
    """A entrega saiu com "…amazon.com.br/dp/B0F": o `[:400]` cortava em silêncio."""
    nota = "Notebook " + "x" * 420 + " — https://www.amazon.com.br/dp/B0F85LVW2W"
    driver = _driver(
        [{"action": "note", "args": {"texto": nota}}, {"action": "done", "args": {"answer": "ok"}}]
    )
    await driver.run("coletar")
    assert driver.notes[0].endswith("/dp/B0F85LVW2W")


# ── 3. esperar a página ─────────────────────────────────────────────────────


def _pedidos_de_snapshot(driver) -> list[dict]:
    return [a for nome, a in driver._pedidos if nome == "browser.snapshot"]


@pytest.mark.asyncio
async def test_clique_em_link_espera_a_navegacao():
    """Snapshot na hora do clique saía com 59 elementos, todos de cabeçalho."""
    link = {"ref": 5, "tag": "a", "text": "Abrir produto", "href": "https://www.amazon.com.br/dp/OUTRO"}
    snap = _snapshot(elements=[link])
    driver = _driver(
        [{"action": "click", "args": {"ref": 5}}, {"action": "done", "args": {"answer": "ok"}}],
        snapshots=[snap],
    )
    await driver.run("abra o produto")
    depois = _pedidos_de_snapshot(driver)[1]
    assert depois["assentar_ms"] > 0
    assert depois["url_antes"] == snap["url"]


@pytest.mark.asyncio
async def test_clique_que_nao_navega_nao_espera_navegacao():
    driver = _driver(
        [{"action": "click", "args": {"ref": 2}}, {"action": "done", "args": {"answer": "ok"}}]
    )
    await driver.run("adicione")
    depois = _pedidos_de_snapshot(driver)[1]
    assert depois["assentar_ms"] > 0
    assert "url_antes" not in depois


@pytest.mark.asyncio
async def test_leitura_nao_paga_espera_nenhuma():
    driver = _driver(
        [{"action": "extract", "args": {}}, {"action": "done", "args": {"answer": "ok"}}]
    )
    await driver.run("leia")
    assert all("assentar_ms" not in p for p in _pedidos_de_snapshot(driver))


# ── 4. extract que chega em tudo ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_busca_diz_quando_nao_tem():
    driver = _driver(
        [
            {"action": "extract", "args": {"busca": "teletransporte"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        resultado={"ok": True, "busca": True, "achados": 0, "total": 9000, "text": ""},
    )
    result = await driver.run("tem teletransporte?")
    passo = next(s for s in result.steps if s.action == "extract")
    assert passo.detail.startswith("NÃO achei «teletransporte»")


@pytest.mark.asyncio
async def test_leitura_cortada_diz_de_onde_continuar():
    driver = _driver(
        [{"action": "extract", "args": {}}, {"action": "done", "args": {"answer": "ok"}}],
        resultado={"ok": True, "text": "abc " * 900, "label": "a página", "proximo": 3500, "total": 12000},
    )
    result = await driver.run("leia")
    passo = next(s for s in result.steps if s.action == "extract")
    assert '`extract {"de": 3500}`' in passo.detail


def test_extract_sem_ref_e_com_alvo_recusa_o_que_nao_faz_sentido():
    driver = _driver([])
    with pytest.raises(ValueError, match="busca"):
        driver._validate({"action": "extract", "args": {"busca": " "}}, _snapshot())
    with pytest.raises(ValueError, match="de"):
        driver._validate({"action": "extract", "args": {"de": "muito"}}, _snapshot())


def test_a_acao_em_palavras_mostra_o_que_procura():
    assert "garantia" in _acao_em_palavras("extract", {"busca": "garantia"})


# ── 5. a visão ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_triagem_nao_deixa_o_raciocinio_comer_a_resposta():
    """10 de 10 triagens vazias, contra 0 de 15 olhadas: o `_look` mandava isto."""
    driver = _driver([], vision='{"tipo": "intruso", "porque": "propaganda"}')
    await driver._triage_vision("classifique", "UFJJTlQ=")
    assert driver._olho.calls[0].get("disable_thinking") is True


@pytest.mark.asyncio
async def test_pagina_que_muda_sozinha_nao_libera_outra_olhada():
    """Três olhadas de 8s na mesma página de produto (job 4a1e6748)."""
    driver = _driver(
        [
            {"action": "look", "args": {"pergunta": "qual o preço?"}},
            {"action": "extract", "args": {}},
            {"action": "look", "args": {"pergunta": "e o frete?"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        # O carrossel mexe: outra assinatura a cada snapshot, sem ninguém agir.
        snapshots=[_snapshot(tela=f"{TELA}\ncarrossel {n}") for n in range(5)],
        vision="O preço é R$ 3.399,00 e o frete é grátis para o CEP cadastrado.",
    )
    await driver.run("preço")
    assert len(driver._olho.calls) == 1


@pytest.mark.asyncio
async def test_olhar_de_novo_vale_depois_de_agir():
    driver = _driver(
        [
            {"action": "look", "args": {"pergunta": "qual o preço?"}},
            {"action": "scroll", "args": {"delta": 600}},
            {"action": "look", "args": {"pergunta": "e o frete?"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        snapshots=[_snapshot(tela=f"{TELA}\ntrecho {n}") for n in range(5)],
        vision="O preço é R$ 3.399,00 e o frete é grátis para o CEP cadastrado.",
    )
    await driver.run("preço")
    assert len(driver._olho.calls) == 2


# ── 6. o job aa4c89f8: 35 notas para 5 produtos, 32 decisões atrás de uma foto ──
#
# O snapshot nunca expôs endereço de imagem, a tarefa exigia a URL da foto e o modelo,
# corretamente, não inventou. Ficou procurando: 26 buscas, com 15 termos diferentes, na `busca`, a
# convite da mensagem de "não achei". 59 dos 128s foram isso. Cada rodada ainda
# escrevia uma nota, cada nota contava como produção, o orçamento subiu de 35 para 66
# e o freio de improdutividade nunca disparou.

P2 = (
    "PRODUTO 2: ASUS Vivobook Go 15 E1504FA-NJ1288 — Ryzen 5 7520U — 16GB RAM — 512GB SSD "
    "— Linux KeepOS — R$3.185,39 à vista — 12x R$280,50 sem juros — Frete GRÁTIS — "
    "Enviado por Amazon — 4,6 (83) — /dp/B0DJWQKLS7"
)


def test_mesmo_item_reescrito_nao_vira_outra_nota():
    assert _mesma_nota(P2, P2.replace("PRODUTO 2:", "PRODUTO 2 (completo):"))
    assert _mesma_nota(P2, P2.replace("PRODUTO 2:", "PRODUTO 2 detalhes:") + " — foto: não informado")


def test_item_diferente_continua_sendo_outra_nota():
    outro = P2.replace("/dp/B0DJWQKLS7", "/dp/B0F85P7QB8").replace("3.185,39", "3.989,05")
    assert not _mesma_nota(P2, outro), "endereço diferente é outro item, por mais parecido que seja"
    assert not _mesma_nota(
        "PRODUTO 1 foto principal: não informado",
        "PRODUTO 4 foto principal: não informado",
    ), "um dígito de diferença é outro item"


def test_foto_do_item_nao_some_dentro_da_nota_do_item():
    foto = "PRODUTO 2 foto: https://imagens.exemplo/I/61abc._AC_SX679_.jpg — /dp/B0DJWQKLS7"
    assert not _mesma_nota(P2, foto)


@pytest.mark.asyncio
async def test_item_reescrito_fica_com_a_versao_mais_completa():
    completa = f"{P2} — garantia de 12 meses"
    driver = _driver(
        [
            {"anotar": [P2], "action": "extract", "args": {}},
            {"anotar": [completa], "action": "done", "args": {"answer": "ok"}},
        ]
    )
    await driver.run("coletar")
    assert driver.notes == [completa]


@pytest.mark.asyncio
async def test_bastidor_nao_entra_no_caderno():
    driver = _driver(
        [
            {"anotar": ["PRODUTO 4 foto: tentar extract 'AC_'"], "action": "extract", "args": {}},
            {"action": "note", "args": {"texto": "PRODUTO 4 foto: tentando dynamic-image"}},
            {"action": "done", "args": {"answer": "ok"}},
        ]
    )
    result = await driver.run("coletar")
    assert driver.notes == []
    passo = next(s for s in result.steps if s.action == "note")
    assert "não anotei" in passo.detail


@pytest.mark.asyncio
async def test_nota_repetida_nao_devolve_passo():
    driver = _driver(
        [
            {"anotar": [P2], "action": "extract", "args": {"busca": "a1"}},
            {"anotar": [P2.replace("PRODUTO 2:", "PRODUTO 2 FINAL:")], "action": "extract", "args": {"busca": "a2"}},
            {"anotar": [P2.replace("PRODUTO 2:", "PRODUTO 2 relido:")], "action": "extract", "args": {"busca": "a3"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        resultado={"ok": True, "busca": True, "achados": 1, "text": "x"},
    )
    await driver.run("coletar")
    assert driver._ganhos == 1, "reescrever o que já se tem não é produção"
    assert driver._teto_de_passos() == driver.max_steps + 1


@pytest.mark.asyncio
async def test_o_freio_volta_a_parar_quem_so_reescreve_nota():
    """Foi a nota nova de cada rodada que desarmou o freio neste job."""
    passos = [{"anotar": [P2], "action": "scroll", "args": {"delta": 300}}]
    for n in range(_SEM_GANHO_MAX + 6):
        passos.append(
            {
                "anotar": [P2.replace("PRODUTO 2:", f"PRODUTO 2 releitura {n}:")],
                "action": "scroll",
                "args": {"delta": 300 + n},
            }
        )
    driver = _driver(passos, snapshots=[_snapshot(tela=f"{TELA}\ntrecho {n}") for n in range(40)])
    result = await driver.run("coletar")
    assert result.status == "failed"
    assert "sem acrescentar nada" in result.summary
    assert len(driver.notes) == 1


@pytest.mark.asyncio
async def test_busca_vazia_nao_convida_a_tentar_outra_palavra():
    driver = _driver(
        [
            {"action": "extract", "args": {"busca": "images/I"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        resultado={"ok": True, "busca": True, "achados": 0, "total": 12230, "text": ""},
    )
    result = await driver.run("foto")
    passo = next(s for s in result.steps if s.action == "extract")
    assert "outra palavra" not in passo.detail
    assert "não informado" in passo.detail


@pytest.mark.asyncio
async def test_repetir_a_busca_vazia_e_recusado_mesmo_com_a_pagina_mudando():
    """A trava de leitura repetida soltava a cada snapshot: página viva muda sozinha."""
    driver = _driver(
        [
            {"action": "extract", "args": {"busca": "images/I"}},
            {"action": "extract", "args": {"busca": "IMAGES/I"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        snapshots=[_snapshot(tela=f"{TELA}\ncarrossel {n}") for n in range(5)],
        resultado={"ok": True, "busca": True, "achados": 0, "total": 100, "text": ""},
    )
    result = await driver.run("foto")
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "já procurou" in recusa.detail


@pytest.mark.asyncio
async def test_buscas_vazias_seguidas_mandam_seguir_em_frente():
    termos = [f"termo{n}" for n in range(_BUSCAS_VAZIAS_MAX + 1)]
    passos = [{"action": "extract", "args": {"busca": t}} for t in termos]
    passos[0]["anotar"] = [P2]  # caderno vazio na mesma tela tem freio próprio, mais curto
    driver = _driver(
        passos + [{"action": "done", "args": {"answer": "ok"}}],
        resultado={"ok": True, "busca": True, "achados": 0, "total": 100, "text": ""},
    )
    result = await driver.run("foto")
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "não está aqui" in recusa.detail
    assert "não informado" in recusa.detail


@pytest.mark.asyncio
async def test_ler_uma_imagem_devolve_o_endereco_dela():
    """`extract` no `img` voltava ok e vazio, sem explicar nada: 5 tentativas no job."""
    driver = _driver(
        [{"action": "extract", "args": {"ref": 2}}, {"action": "done", "args": {"answer": "ok"}}],
        resultado={"ok": True, "text": "", "imagem": "https://imagens.exemplo/I/61abc.jpg", "alt": "Notebook"},
    )
    result = await driver.run("foto")
    passo = next(s for s in result.steps if s.action == "extract")
    assert passo.detail.startswith("imagem: https://imagens.exemplo/I/61abc.jpg")


@pytest.mark.asyncio
async def test_elemento_sem_texto_diz_que_nao_tem_texto():
    driver = _driver(
        [{"action": "extract", "args": {"ref": 2}}, {"action": "done", "args": {"answer": "ok"}}],
        resultado={"ok": True, "text": "", "label": "x"},
    )
    result = await driver.run("leia")
    passo = next(s for s in result.steps if s.action == "extract")
    assert "não tem texto" in passo.detail


def test_o_endereco_das_imagens_chega_ao_prompt():
    snap = _snapshot(
        imagens=[{"url": "https://imagens.exemplo/I/61abc.jpg", "alt": "Notebook ASUS", "w": 489, "h": 326}]
    )
    render = BrowserLocalDriver._render(snap)
    assert "IMAGENS NA TELA" in render
    assert "489×326 «Notebook ASUS» https://imagens.exemplo/I/61abc.jpg" in render
    assert render.index("NA TELA AGORA") < render.index("IMAGENS NA TELA") < render.index("\nElementos (")
    linha = BrowserLocalDriver._element_line(
        {"ref": 192, "tag": "img", "text": "Notebook", "src": "https://imagens.exemplo/I/61abc.jpg"}
    )
    assert "→ https://imagens.exemplo/I/61abc.jpg" in linha
