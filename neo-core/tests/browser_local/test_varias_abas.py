"""Quando um clique abre outra aba, a tarefa vai junto — e sabe voltar.

O caso (job 3512cdd2, 2026-11-02): "veja o preço da AMD BC-250 no AliExpress".
O clique no anúncio abriu o produto numa aba nova, o Edge foi para ela, e o
agente seguiu na aba da busca lendo "clique ok, a tela não mudou". Saiu
navegando pela URL do item na mão, revisitou o anúncio 1 três vezes, anotou 10
notas para 3 produtos e deixou duas abas órfãs no navegador.

Aqui o device é de mentira mas fala o contrato do desktop novo: o clique que abre
aba devolve ``abas_novas``; ``session_tabs`` lista as vivas; ``close_tab`` e
``focus_tab`` agem na aba pedida. Ver docs/browser-multi-aba-plan.md.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.browser_local.driver import BrowserLocalDriver

import asyncio
import json

from tests.browser_local.test_driver_vision import _FakeLLM, _FakeProvider, _FakeResponse

BUSCA = "https://pt.aliexpress.com/w/wholesale-AMD-BC-250.html"
ITEM_1 = "https://pt.aliexpress.com/item/1005008976362329.html"
ITEM_2 = "https://pt.aliexpress.com/item/1005012639519758.html"


def _pagina(url: str) -> dict:
    if url == BUSCA:
        return {
            "url": url, "title": "AMD BC-250 - AliExpress", "ready": "complete", "stop": False,
            "elements": [
                {"ref": 27, "tag": "a", "text": "Placa de Vídeo AMD BC 250 16GB R$1.976,99",
                 "href": "/item/1005008976362329.html"},
                {"ref": 28, "tag": "a", "text": "Memória Gráfica Bc 250 16GB R$4.369,42",
                 "href": "/item/1005012639519758.html"},
                {"ref": 29, "tag": "a", "text": "Anúncio patrocinado"},
                {"ref": 30, "tag": "button", "text": "Entrar com Google"},
            ],
            "text": "Resultados para AMD BC-250 · Placa de Vídeo AMD BC 250 R$1.976,99 · Memória Gráfica R$4.369,42",
        }
    return {
        "url": url, "title": "Produto", "ready": "complete", "stop": False,
        "elements": [
            {"ref": 3, "tag": "button", "text": "Adicionar ao carrinho"},
            {"ref": 4, "tag": "button", "text": "Comprar agora"},
        ],
        "text": f"Página do produto {url.rsplit('/', 1)[-1]} · preço, frete e vendedor",
    }


class _Navegador:
    """Várias abas, cada uma com a sua página. `abre` diz que clique abre o quê."""

    def __init__(self, *, abre: dict | None = None, inicial: int = 42, sem_aba: dict | None = None,
                 fecha_sozinha: set[str] | None = None, desktop_antigo: bool = False,
                 sem_open_tab: bool = False, extras: dict[int, str] | None = None,
                 barreira: set[int] | None = None) -> None:
        self.abas: dict[int, str] = {inicial: BUSCA, **(extras or {})}
        self.sem_open_tab = sem_open_tab
        #: Abas cujo snapshot só sai quando TODAS chegaram: prova que os
        #: subagentes rodam ao mesmo tempo (em fila, o teste trava e falha).
        self.barreira = set(barreira or ())
        self.chegaram: set[int] = set()
        self.extraidas: list[int] = []
        self.vivas_ligadas: list[tuple[int, bool]] = []
        self.abertas_a_pedido: list[str] = []
        self.abre = abre or {}
        self.sem_aba = sem_aba or {}
        self.fecha_sozinha = set(fecha_sozinha or ())
        self.desktop_antigo = desktop_antigo
        self.proxima = max(self.abas) + 1
        self.acted: list[tuple[str, dict]] = []
        self.fechadas: list[int] = []
        self.focos: list[int] = []
        self.inicial = inicial

    async def act(self, action: str, args: dict, timeout: float) -> dict:
        self.acted.append((action, dict(args)))
        tab = args.get("tab_ref")
        if action == "browser.claim":
            return {"tab_ref": args.get("tab_id", self.inicial)}
        if action == "browser.tabs":
            return {"tabs": [{"tabId": t, "url": u} for t, u in self.abas.items()]}
        if action == "browser.open_tab":
            if self.sem_open_tab or self.desktop_antigo:
                raise RuntimeError("ação de navegador desconhecida: browser.open_tab")
            nova = self.proxima
            self.proxima += 1
            self.abas[nova] = args["url"]
            self.abertas_a_pedido.append(args["url"])
            return {"tab_ref": nova, "url": args["url"], "title": "Produto"}
        if action == "browser.snapshot" and tab in self.barreira:
            self.chegaram.add(tab)
            for _ in range(200):
                if self.barreira <= self.chegaram:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError(f"os subagentes não rodaram ao mesmo tempo: {self.chegaram}")
        if action == "browser.extract":
            self.extraidas.append(tab)
            url = self.abas.get(tab, "")
            if args.get("busca"):
                return {"text": f"frete R$19,90 em {url}", "achados": 1, "total": 900}
            return {"text": f"Página do produto {url.rsplit('/', 1)[-1]} · R$1.976,99 · frete grátis"}
        if action == "browser.keep_alive":
            self.vivas_ligadas.append((tab, bool(args.get("ligado"))))
            return {"ok": True}
        if action == "browser.snapshot":
            if tab not in self.abas:
                raise RuntimeError(f"No tab with id: {tab}")
            url = self.abas[tab]
            if url in self.fecha_sozinha:
                # O popup de login: aparece uma vez e se fecha.
                self.fecha_sozinha.discard(url)
                pagina = _pagina(url)
                self.abas.pop(tab)
                return pagina
            return _pagina(url)
        if action in ("browser.click", "browser.click_text"):
            chave = (tab, args.get("ref", args.get("texto")))
            resultado = {"ok": True, "label": f"clique em {chave[1]}"}
            if chave in self.abre and not self.desktop_antigo:
                nova = self.proxima
                self.proxima += 1
                self.abas[nova] = self.abre[chave]
                resultado["abas_novas"] = [
                    {"tab_ref": nova, "url": self.abre[chave], "title": "Produto", "aberta_por": tab}
                ]
            if chave in self.sem_aba:
                resultado["janela_sem_aba"] = self.sem_aba[chave]
            return resultado
        if action == "browser.session_tabs":
            if self.desktop_antigo:
                raise RuntimeError("ação de navegador desconhecida: browser.session_tabs")
            return {"tabs": [{"tab_ref": t, "url": u, "title": ""} for t, u in self.abas.items()]}
        if action == "browser.close_tab":
            self.fechadas.append(tab)
            self.abas.pop(tab, None)
            return {"ok": True}
        if action == "browser.focus_tab":
            self.focos.append(tab)
            return {"ok": True}
        if action == "browser.screenshot":
            return {"ok": True, "format": "jpeg", "data": "UFJJTlQ=", "marks": True}
        if action == "browser.downloads":
            return {"ok": True, "dir": "/Users/daniel/Downloads", "count": 0, "files": []}
        return {"ok": True}

    def snapshots(self) -> list:
        return [a.get("tab_ref") for n, a in self.acted if n == "browser.snapshot"]


def _driver(nav: _Navegador, passos: list[dict], **kw) -> tuple[BrowserLocalDriver, _FakeProvider]:
    provider = _FakeProvider(passos)
    driver = BrowserLocalDriver(
        act=nav.act, llm=_FakeLLM({}, provider), model="texto", user_id=uuid4(), **kw
    )
    return driver, provider


def _prompt(provider: _FakeProvider, i: int) -> str:
    return provider.calls[i]["messages"][1].content


# ── 1. o job 3512cdd2, com as abas ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_segue_a_aba_do_anuncio_volta_para_a_busca_e_conclui_na_seguinte():
    nav = _Navegador(abre={(42, 27): ITEM_1, (42, 28): ITEM_2})
    driver, provider = _driver(nav, [
        {"action": "click", "args": {"ref": 27}},
        {"action": "note", "args": {"texto": "Placa AMD BC 250 16GB — R$1.976,99 — frete grátis"}},
        {"action": "fechar_aba", "args": {}},
        {"action": "click", "args": {"ref": 28}},
        {"action": "note", "args": {"texto": "Memória Gráfica Bc 250 16GB — R$4.369,42 — frete R$197,27"}},
        {"action": "done", "args": {"answer": "dois anúncios lidos"}},
    ])
    out = await driver.run("Veja o preço da AMD BC-250 no AliExpress", start_url=BUSCA)

    assert out.status == "completed", out.summary
    # Seguiu: depois do clique no 27, a percepção é da aba nova (43), não da busca.
    assert nav.snapshots()[:3] == [42, 43, 43]
    clique = out.steps[0]
    assert "o clique abriu a aba B" in clique.detail and "Você está NELA" in clique.detail
    # O passo seguinte vê as duas abas, com a ativa marcada.
    bloco = _prompt(provider, 1)
    assert "ABAS (você age na ▶" in bloco
    assert "▶ B  Produto · pt.aliexpress.com/item/1005008976362329.html  (aberta pela página da A)" in bloco
    # `fechar_aba` fechou a B e voltou para a A; o segundo anúncio virou a C.
    assert nav.fechadas[0] == 43
    assert nav.snapshots()[3] == 42
    assert "voltei para a A" in out.steps[2].detail
    # Cada nota diz em que aba foi lida.
    assert out.notes[0].endswith("[aba B]") and out.notes[1].endswith("[aba C]")
    # Concluiu na C: ela é a entregue — não fecha, e é ela que se solta e se guarda.
    assert 44 not in nav.fechadas
    soltas = [a.get("tab_ref") for n, a in nav.acted if n == "browser.release"]
    assert soltas == [44]
    assert out.tab_ref == 44
    # A inicial (a aba dele / da tarefa) nunca fecha no modo com janela.
    assert 42 not in nav.fechadas
    assert [(a["rotulo"], a["viva"], a["entregue"]) for a in out.abas] == [
        ("A", True, False), ("B", False, False), ("C", True, True)
    ]
    # A aba que vem para a frente é a que o usuário passa a ver.
    assert nav.focos == [43, 42, 44]


@pytest.mark.asyncio
async def test_concluir_na_aba_nova_fecha_so_as_auxiliares():
    nav = _Navegador(abre={(42, 27): ITEM_1, (43, 3): ITEM_2})
    driver, _ = _driver(nav, [
        {"action": "click", "args": {"ref": 27}},            # A → B
        {"action": "click", "args": {"ref": 3}},             # B → C
        {"action": "note", "args": {"texto": "Memória Gráfica — R$4.369,42"}},
        {"action": "trocar_aba", "args": {"aba": "B"}},
        {"action": "done", "args": {"answer": "pronto"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    # Terminou na B: a C (auxiliar) fecha; a A (inicial) fica; a B é a entregue.
    assert nav.fechadas == [44]
    assert out.tab_ref == 43


@pytest.mark.asyncio
async def test_no_headless_soltar_e_fechar_entao_a_inicial_tambem_fecha():
    nav = _Navegador(abre={(42, 27): ITEM_1})
    driver, _ = _driver(nav, [
        {"action": "click", "args": {"ref": 27}},
        {"action": "note", "args": {"texto": "Placa AMD BC 250 — R$1.976,99"}},
        {"action": "done", "args": {"answer": "pronto"}},
    ], headless=True)
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    assert nav.fechadas == [42]
    assert out.tab_ref == 43


# ── 2. a página fecha a aba sozinha (popup de login) ─────────────────────────


@pytest.mark.asyncio
async def test_aba_que_fecha_sozinha_devolve_para_quem_a_abriu():
    login = "https://accounts.google.com/o/oauth2/popup"
    nav = _Navegador(abre={(42, 30): login}, fecha_sozinha={login})
    driver, provider = _driver(nav, [
        {"action": "click", "args": {"ref": 30}},
        {"action": "scroll", "args": {"delta": 300}},       # passo na B, que some depois
        {"action": "done", "args": {"answer": "voltei"}},
    ])
    out = await driver.run("Entre com Google", start_url=BUSCA)
    assert out.status == "completed", out.summary
    # O próximo passo já é na A, e o modelo é avisado uma vez.
    assert nav.snapshots()[-1] == 42
    aviso = "ATENÇÃO: a aba B fechou sozinha (a página fechou); você voltou para a aba A."
    assert any(aviso in c["messages"][1].content for c in provider.calls)
    assert out.tab_ref == 42


# ── 3. o que a página abre e não devia ───────────────────────────────────────


@pytest.mark.asyncio
async def test_aba_para_dominio_proibido_e_fechada_na_hora():
    nav = _Navegador(abre={(42, 29): "https://ads.ruim.com/landing"})
    driver, _ = _driver(nav, [
        {"action": "click", "args": {"ref": 29}},
        {"action": "done", "args": {"answer": "ignorei o anúncio"}},
    ], blocked_domains=("ruim.com",))
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert nav.fechadas == [43]
    assert "lista de domínios proibidos — fechei" in out.steps[0].detail
    assert out.tab_ref == 42 and out.abas == []


@pytest.mark.asyncio
async def test_popup_bloqueado_pelo_navegador_e_dito_e_a_tarefa_continua_na_aba():
    nav = _Navegador(sem_aba={(42, 29): {"url": "https://pop.exemplo/janela", "gesto": False}})
    driver, _ = _driver(nav, [
        {"action": "click", "args": {"ref": 29}},
        {"action": "done", "args": {"answer": "nada abriu"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert "o navegador bloqueou" in out.steps[0].detail
    assert "você continua na aba A" in out.steps[0].detail


# ── 4. o vocabulário recusa o que não faz sentido ────────────────────────────


@pytest.mark.asyncio
async def test_nao_fecha_a_aba_inicial_e_nao_troca_para_aba_que_nao_existe():
    nav = _Navegador(abre={(42, 27): ITEM_1})
    driver, _ = _driver(nav, [
        {"action": "click", "args": {"ref": 27}},
        {"action": "trocar_aba", "args": {"aba": "Z"}},
        {"action": "fechar_aba", "args": {"aba": "a"}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    erros = [s.detail for s in out.steps if not s.ok]
    assert any("não existe aba «Z»" in e and "A, B" in e for e in erros), erros
    assert any("onde a tarefa começou e não fecha" in e for e in erros), erros
    assert 42 not in nav.fechadas


# ── 5. desktop antigo: nada muda ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_desktop_sem_suporte_a_abas_segue_igual_ao_de_antes():
    nav = _Navegador(abre={(42, 27): ITEM_1}, desktop_antigo=True)
    driver, provider = _driver(nav, [
        {"action": "click", "args": {"ref": 27}},
        {"action": "note", "args": {"texto": "Placa AMD BC 250 — R$1.976,99"}},
        {"action": "done", "args": {"answer": "pronto"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    assert "ABAS (" not in _prompt(provider, 1)
    assert out.notes == ["Placa AMD BC 250 — R$1.976,99"]
    assert not [n for n, _ in nav.acted if n in ("browser.session_tabs", "browser.close_tab")]
    assert out.abas == []


# ── 6. a entrega liga a nota ao endereço ─────────────────────────────────────


def test_rodape_das_abas_so_com_mais_de_uma():
    from types import SimpleNamespace

    from core.jobs.runner_browser_local import _rodape_das_abas

    assert _rodape_das_abas(SimpleNamespace(abas=[])) == ""
    out = SimpleNamespace(abas=[
        {"rotulo": "A", "url": BUSCA + "?spm=x", "viva": True, "entregue": False},
        {"rotulo": "B", "url": ITEM_1, "viva": False, "entregue": False},
        {"rotulo": "C", "url": ITEM_2, "viva": True, "entregue": True},
    ])
    assert _rodape_das_abas(out) == (
        "(abas desta tarefa: A pt.aliexpress.com/w/wholesale-AMD-BC-250.html; "
        "B pt.aliexpress.com/item/1005008976362329.html — fechada; "
        "C pt.aliexpress.com/item/1005012639519758.html — a que ficou aberta)"
    )


# ── 7. o job e8092a4b: o mesmo anúncio aberto três vezes ─────────────────────


@pytest.mark.asyncio
async def test_reabrir_o_que_ja_foi_fechado_e_avisado_com_o_motivo():
    """Abriu a D, viu "não é a BC-250", fechou — e reabriu a mesma duas vezes.

    O "não serve" morava só no pensamento do passo; o histórico mostrava "abriu D"
    e "fechou D", e nada impedia a volta. Agora a aba fechada guarda o motivo, o
    prompt lista as fechadas, e abrir o mesmo endereço de novo é dito na hora.
    """
    errado = "https://pt.aliexpress.com/item/1005011777594358.html"
    nav = _Navegador(abre={(42, 30): errado})
    driver, provider = _driver(nav, [
        {"action": "click", "args": {"ref": 30}},
        {"action": "fechar_aba", "args": {"motivo": "é uma placa de banda base 5G, não a BC-250"}},
        {"action": "click", "args": {"ref": 30}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    # Depois de fechar, o prompt da A lembra o que foi visto e por que não serve.
    lembrete = _prompt(provider, 2)
    assert "JÁ ABERTAS E FECHADAS nesta tarefa" in lembrete
    assert (
        "B  pt.aliexpress.com/item/1005011777594358.html · sem nota — "
        "é uma placa de banda base 5G, não a BC-250"
    ) in lembrete
    # Reabrir o mesmo endereço é avisado no próprio passo.
    reaberta = out.steps[2].detail
    assert "o clique abriu a aba C" in reaberta
    assert "⚠ você já abriu este mesmo endereço na aba B e fechou SEM anotar nada" in reaberta
    assert "(motivo: é uma placa de banda base 5G, não a BC-250)" in reaberta


@pytest.mark.asyncio
async def test_endereco_que_ja_esta_aberto_noutra_aba_aponta_para_ela():
    nav = _Navegador(abre={(42, 27): ITEM_1, (43, 3): ITEM_1})
    driver, _ = _driver(nav, [
        {"action": "click", "args": {"ref": 27}},
        {"action": "click", "args": {"ref": 3}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert "⚠ este mesmo endereço já está aberto na aba B — `trocar_aba` vai até ela" in out.steps[1].detail


def test_o_redator_fica_sabendo_que_os_itens_vieram_das_paginas_das_abas():
    from types import SimpleNamespace

    from core.jobs.runner_browser_local import _frase_das_abas

    abas = [
        {"rotulo": "A", "origem": "inicial"},
        {"rotulo": "B", "origem": "site"},
        {"rotulo": "C", "origem": "site"},
        {"rotulo": "D", "origem": "site"},
    ]
    notas = ["Placa — R$1.979,61 [aba B]", "Memória Gráfica — R$4.379,80 [aba C]"]
    assert _frase_das_abas(SimpleNamespace(abas=abas, notes=notas)) == (
        "Abri e li 2 página(s) em abas separadas (B, C): os itens foram lidos nessas "
        "páginas, não só na lista de onde saíram — cada um diz de qual aba veio."
    )
    assert _frase_das_abas(SimpleNamespace(abas=abas, notes=[])) == (
        "Abri 3 página(s) em abas separadas; nenhuma rendeu item."
    )
    assert _frase_das_abas(SimpleNamespace(abas=[], notes=notas)) == ""


@pytest.mark.asyncio
async def test_fechar_sem_nota_exige_o_motivo():
    nav = _Navegador(abre={(42, 30): "https://pt.aliexpress.com/item/1005011777594358.html"})
    driver, _ = _driver(nav, [
        {"action": "click", "args": {"ref": 30}},
        {"action": "fechar_aba", "args": {}},
        {"action": "fechar_aba", "args": {"motivo": "não é a placa"}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    recusa = out.steps[1]
    assert not recusa.ok and "diga em `motivo` por que" in recusa.detail
    assert out.steps[2].ok and "voltei para a A" in out.steps[2].detail


@pytest.mark.asyncio
async def test_na_volta_a_busca_o_link_ja_visto_vem_marcado_na_propria_linha():
    """Reencenado com o modelo de produção: só a lista não segurou (6/6 reabriu)."""
    nav = _Navegador(abre={(42, 27): ITEM_1, (42, 28): ITEM_2})
    driver, provider = _driver(nav, [
        {"action": "click", "args": {"ref": 27}},
        {"action": "fechar_aba", "args": {"motivo": "não é a placa"}},
        {"action": "click", "args": {"ref": 28}},
        {"action": "note", "args": {"texto": "Memória Gráfica — R$4.369,42"}},
        {"action": "fechar_aba", "args": {}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    await driver.run("Veja o preço", start_url=BUSCA)
    volta = _prompt(provider, 2)
    linha27 = next(l for l in volta.split("\n") if "[27]" in l)
    assert linha27.endswith("⟪já aberto na aba B: não é a placa⟫"), linha27
    depois = _prompt(provider, 5)
    linha28 = next(l for l in depois.split("\n") if "[28]" in l)
    assert linha28.endswith("⟪já lido e anotado na aba C — não precisa reabrir⟫"), linha28



# ── 8. F2: o Neo abre as abas por conta própria ──────────────────────────────


@pytest.mark.asyncio
async def test_abre_os_anuncios_em_abas_e_le_um_por_um_sem_a_lista_sair_do_lugar():
    """O AliExpress refazia a lista na volta (job e8092a4b) e os números mudavam.

    Com `abrir_aba`, a lista nunca sai da aba A: abre os dois anúncios, visita
    cada um, anota, fecha, e a A continua onde estava.
    """
    nav = _Navegador()
    driver, provider = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "abrir_aba", "args": {"ref": 28}},
        {"action": "trocar_aba", "args": {"aba": "B"}},
        {"action": "note", "args": {"texto": "Placa AMD BC 250 — R$1.976,99"}},
        {"action": "fechar_aba", "args": {}},
        {"action": "trocar_aba", "args": {"aba": "C"}},
        {"action": "note", "args": {"texto": "Memória Gráfica — R$4.369,42"}},
        {"action": "fechar_aba", "args": {}},
        {"action": "done", "args": {"answer": "dois anúncios"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    # O `href` relativo do link vira o endereço inteiro da página.
    assert nav.abertas_a_pedido == [ITEM_1, ITEM_2]
    # Abrir NÃO tira a tarefa da lista: os dois primeiros passos percebem a A.
    assert nav.snapshots()[:3] == [42, 42, 42]
    assert "abri a aba B (pt.aliexpress.com/item/1005008976362329.html) em segundo plano" in out.steps[0].detail
    assert "você continua na A" in out.steps[0].detail
    bloco = _prompt(provider, 2)
    assert "▶ A" in bloco and "  B  Produto · pt.aliexpress.com/item/1005008976362329.html" in bloco
    # Cada anúncio foi lido na sua aba, fechado, e a volta é para a lista.
    assert out.notes == ["Placa AMD BC 250 — R$1.976,99 [aba B]", "Memória Gráfica — R$4.369,42 [aba C]"]
    assert "voltei para a A" in out.steps[4].detail and "voltei para a A" in out.steps[7].detail
    assert nav.fechadas == [43, 44]
    assert out.tab_ref == 42


@pytest.mark.asyncio
async def test_abrir_aba_recusa_o_que_nao_faz_sentido():
    nav = _Navegador()
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 30}},                        # botão, não link
        {"action": "abrir_aba", "args": {"url": "https://outra-loja.com/x"}},  # fora do escopo
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "abrir_aba", "args": {"ref": 27}},                        # já aberta
        {"action": "abrir_aba", "args": {}},                                 # sem ref nem url
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço no AliExpress", start_url=BUSCA)
    detalhes = [s.detail for s in out.steps[:5]]
    assert "não é um link com endereço — para ele, use `click`" in detalhes[0]
    assert "fora do escopo" in detalhes[1]
    assert out.steps[2].ok
    assert "já está aberto na aba B — use `trocar_aba`" in detalhes[3]
    assert "precisa de `ref`" in detalhes[4]
    assert nav.abertas_a_pedido == [ITEM_1]


@pytest.mark.asyncio
async def test_link_cortado_pelo_snapshot_abre_sem_o_parametro_partido():
    """Job 9a3223a4: o href vinha cortado em 300, e a aba D abriu `…110.02%` — vazia.

    O snapshot agora manda até 2000 e marca `href_cortado` acima disso; o link
    marcado abre sem o último parâmetro, e o que não foi cortado abre inteiro.
    """
    from core.browser_local.driver import _sem_parametro_cortado

    cortado = ITEM_1 + "?algo_pvid=25a7&pdp_ext_f=%7B%22order%22%7D&pdp_npi=6%40dis%21BRL%21647.51%"
    inteiro = ITEM_2 + "?algo_pvid=1&pdp_npi=6%40dis%21BRL"
    nav = _Navegador()
    act = nav.act

    async def act_com_links_longos(action: str, args: dict, timeout: float) -> dict:
        pagina = await act(action, args, timeout)
        if action == "browser.snapshot" and pagina.get("url") == BUSCA:
            pagina["elements"] = [
                *pagina["elements"],
                {"ref": 31, "tag": "a", "text": "Kingston NV3 R$566,76", "href": cortado,
                 "href_cortado": True},
                {"ref": 32, "tag": "a", "text": "Kingston NV3 R$527,10", "href": inteiro},
            ]
        return pagina

    nav.act = act_com_links_longos
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 31}},
        {"action": "abrir_aba", "args": {"ref": 32}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    await driver.run("Veja o preço", start_url=BUSCA)
    assert nav.abertas_a_pedido == [
        ITEM_1 + "?algo_pvid=25a7&pdp_ext_f=%7B%22order%22%7D",
        inteiro,
    ]
    assert _sem_parametro_cortado(ITEM_1 + "?pdp_npi=6%40di") == ITEM_1
    assert _sem_parametro_cortado(ITEM_1 + "/mais-um-pedaç") == ITEM_1 + "/mais-um-pedaç"


@pytest.mark.asyncio
async def test_no_headless_o_teto_e_3_para_o_gerente_nao_fechar_a_lista():
    nav = _Navegador()
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "abrir_aba", "args": {"ref": 28}},
        {"action": "abrir_aba", "args": {"url": "https://pt.aliexpress.com/item/3.html"}},
        {"action": "done", "args": {"answer": "ok"}},
    ], headless=True)
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert "já tem 3 abas abertas" in out.steps[2].detail
    assert len(nav.abertas_a_pedido) == 2


@pytest.mark.asyncio
async def test_desktop_que_nao_abre_aba_manda_clicar():
    nav = _Navegador(sem_open_tab=True)
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert "ainda não abre aba por conta própria — use `click` no link" in out.steps[0].detail


# ── 9. F2: a tarefa que PARA guarda as abas, e a retomada continua com elas ──


@pytest.mark.asyncio
async def test_pausa_nao_fecha_as_abas_e_guarda_o_conjunto():
    nav = _Navegador()
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "trocar_aba", "args": {"aba": "B"}},
        {"action": "click", "args": {"ref": 4}},  # "Comprar agora": irreversível, para
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "needs_confirmation", out.summary
    assert nav.fechadas == []
    assert out.tab_ref == 43
    assert [(a["rotulo"], a["tab_ref"], a["viva"], a["entregue"]) for a in out.abas] == [
        ("A", 42, True, False), ("B", 43, True, True)
    ]

    from core.jobs.runner_browser_local import _abas_para_sessao

    assert _abas_para_sessao(out) == {"abas": out.abas}
    concluida = type(out)(status="completed", summary="", abas=out.abas)
    assert _abas_para_sessao(concluida) == {}


@pytest.mark.asyncio
async def test_retomada_continua_com_as_abas_de_antes_e_os_mesmos_rotulos():
    previas = [
        {"rotulo": "A", "tab_ref": 42, "url": BUSCA, "origem": "inicial", "viva": True, "entregue": False},
        {"rotulo": "B", "tab_ref": 43, "url": ITEM_1, "origem": "agente", "aberta_por": "A",
         "viva": True, "entregue": True},
        {"rotulo": "C", "tab_ref": 44, "url": ITEM_2, "origem": "agente", "aberta_por": "A",
         "viva": False, "motivo": "não é a placa", "entregue": False},
    ]
    nav = _Navegador(extras={43: ITEM_1})
    driver, provider = _driver(nav, [
        {"action": "note", "args": {"texto": "Placa AMD BC 250 — R$1.976,99"}},
        {"action": "trocar_aba", "args": {"aba": "A"}},
        {"action": "done", "args": {"answer": "ok"}},
    ], tab_ref=43, abas_previas=previas)
    out = await driver.run("Veja o preço", reabrir_url=ITEM_1)
    assert out.status == "completed", out.summary
    # Voltou na B (a que estava ativa), com A e B vivas e a C lembrada como fechada.
    assert nav.snapshots()[0] == 43
    bloco = _prompt(provider, 0)
    assert "▶ B" in bloco and "  A  " in bloco
    assert "C  pt.aliexpress.com/item/1005012639519758.html · sem nota — não é a placa" in bloco
    assert out.notes == ["Placa AMD BC 250 — R$1.976,99 [aba B]"]
    assert nav.focos[-1] == 42
    # O próximo rótulo continua a sequência.
    assert driver._registrar_aba(99, origem="agente").rotulo == "D"


def test_as_duas_retomadas_levam_as_abas():
    from types import SimpleNamespace
    from uuid import uuid4

    from core.jobs.release_resume import spec_de_continuacao

    abas = [{"rotulo": "A", "tab_ref": 42}, {"rotulo": "B", "tab_ref": 43, "entregue": True}]
    parado = SimpleNamespace(
        id=uuid4(), spec_json={"task": "t"},
        extra_metadata={"browser_session": {"tab_ref": 43, "url": ITEM_1, "abas": abas}},
    )
    row = SimpleNamespace(id=uuid4(), campos=["pks:cep"], destino="aliexpress.com")
    assert spec_de_continuacao(parado, row)["abas"] == abas


@pytest.mark.asyncio
async def test_continue_job_leva_as_abas_da_tarefa_parada():
    from types import SimpleNamespace

    from core.jobs.executor import JobToolExecutor

    abas = [{"rotulo": "A", "tab_ref": 42}, {"rotulo": "B", "tab_ref": 43, "entregue": True}]
    anterior = SimpleNamespace(
        id=uuid4(), title="Preço AMD BC-250", status="waiting", spec_json={"task": "t"},
        extra_metadata={"browser_session": {"pending": "Comprar agora", "tab_ref": 43,
                                            "url": ITEM_1, "abas": abas}},
    )
    criados: list = []

    class _DB:
        async def create_agent_job(self, job):
            job.id = uuid4()
            criados.append(job)
            return job

        async def update_agent_job(self, *_a, **_k):
            return None

    ex = JobToolExecutor.__new__(JobToolExecutor)
    ex.db, ex.jm = _DB(), SimpleNamespace(poke=lambda: None)
    ex.user_id, ex.session_id, ex.device_id = uuid4(), uuid4(), "dev-1"

    async def _dono(_):
        return anterior

    async def _livre():
        return None

    ex._get_owned, ex._browser_local_em_curso = _dono, _livre
    assert (await ex._retomada(anterior.id))["ok"] is True
    assert criados[0].spec_json["abas"] == abas
    assert criados[0].spec_json["tab_ref"] == 43



# ── 10. F3: várias abas ao mesmo tempo ───────────────────────────────────────


class _Roteador:
    """LLM falso que responde por TAREFA: pai e filhos rodam ao mesmo tempo, e uma
    lista única de respostas sairia embaralhada."""

    def __init__(self, roteiros: dict[str, list[dict]]) -> None:
        self.roteiros = {k: list(v) for k, v in roteiros.items()}
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        primeira = kwargs["messages"][1].content.split("\n", 1)[0]
        tarefa = primeira.removeprefix("TAREFA: ").strip()
        fila = self.roteiros.get(tarefa) or []
        passo = fila.pop(0) if fila else {"action": "give_up", "args": {"reason": "sem roteiro"}}
        return _FakeResponse(json.dumps(passo))

    def get_provider_for_model(self, model):
        return self


def _driver_roteado(nav, roteiros, **kw):
    llm = _Roteador(roteiros)
    driver = BrowserLocalDriver(act=nav.act, llm=llm, model="texto", user_id=uuid4(), **kw)
    return driver, llm


@pytest.mark.asyncio
async def test_ler_abas_le_todas_de_uma_vez_sem_sair_da_lista():
    nav = _Navegador()
    driver, llm = _driver_roteado(nav, {"Veja o preço": [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "abrir_aba", "args": {"ref": 28}},
        {"action": "ler_abas", "args": {"abas": ["B", "C"]}},
        {"action": "done", "args": {"answer": "lidos"}},
    ]})
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    assert sorted(nav.extraidas) == [43, 44]
    leitura = out.steps[2].detail
    assert "── aba B (pt.aliexpress.com/item/1005008976362329.html):" in leitura
    assert "── aba C (pt.aliexpress.com/item/1005012639519758.html):" in leitura
    assert "R$1.976,99" in leitura and "ANOTE o que serve" in leitura
    # Ligou o ciclo de vida para ler e desligou depois (as abas estão escondidas).
    assert sorted(nav.vivas_ligadas) == [(43, False), (43, True), (44, False), (44, True)]
    assert set(nav.snapshots()) == {42}


@pytest.mark.asyncio
async def test_paralelo_roda_um_subagente_por_aba_ao_mesmo_tempo():
    nav = _Navegador(barreira={43, 44})
    driver, llm = _driver_roteado(nav, {
        "Veja o preço": [
            {"action": "abrir_aba", "args": {"ref": 27}},
            {"action": "abrir_aba", "args": {"ref": 28}},
            {"action": "paralelo", "args": {"tarefas": [
                {"aba": "B", "objetivo": "preço e frete do anúncio B"},
                {"aba": "C", "objetivo": "preço e frete do anúncio C"},
            ]}},
            {"action": "done", "args": {"answer": "comparados"}},
        ],
        "preço e frete do anúncio B": [
            {"action": "note", "args": {"texto": "Placa AMD BC 250 — R$1.976,99 — frete grátis"}},
            {"action": "done", "args": {"answer": "lido"}},
        ],
        "preço e frete do anúncio C": [
            {"action": "note", "args": {"texto": "Memória Gráfica — R$4.369,42 — frete R$197,27"}},
            {"action": "done", "args": {"answer": "lido"}},
        ],
    })
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    # Os dois chegaram à barreira: rodaram juntos, cada um na sua aba.
    assert nav.chegaram == {43, 44}
    assert sorted(out.notes) == [
        "Memória Gráfica — R$4.369,42 — frete R$197,27 [aba C]",
        "Placa AMD BC 250 — R$1.976,99 — frete grátis [aba B]",
    ]
    resumo = out.steps[2].detail
    assert "paralelo em 2 aba(s)" in resumo
    assert "- B: pronto, 1 nota(s)" in resumo and "- C: pronto, 1 nota(s)" in resumo
    # O filho recebeu a tarefa maior como contexto, e a sua aba como escopo.
    prompt_filho = next(c["messages"][1].content for c in llm.calls
                        if c["messages"][1].content.startswith("TAREFA: preço e frete do anúncio B"))
    assert "SUBAGENTE trabalhando só na aba B" in prompt_filho and "Veja o preço" in prompt_filho
    # O filho não reivindica nem solta a aba do principal; o principal fecha no fim.
    assert [a.get("tab_ref") for n, a in nav.acted if n == "browser.claim"] == [None]
    assert [a.get("tab_ref") for n, a in nav.acted if n == "browser.release"] == [42]
    assert sorted(nav.fechadas) == [43, 44]


@pytest.mark.asyncio
async def test_subagente_nao_faz_o_irreversivel_nem_mexe_em_abas():
    nav = _Navegador()
    driver, _ = _driver_roteado(nav, {
        "Veja o preço": [
            {"action": "abrir_aba", "args": {"ref": 27}},
            {"action": "abrir_aba", "args": {"ref": 28}},
            {"action": "paralelo", "args": {"tarefas": [
                {"aba": "B", "objetivo": "compre o anúncio B"},
                {"aba": "C", "objetivo": "olhe a aba A também"},
            ]}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        "compre o anúncio B": [{"action": "click", "args": {"ref": 4}}],  # "Comprar agora"
        "olhe a aba A também": [
            {"action": "trocar_aba", "args": {"aba": "A"}},
            {"action": "done", "args": {"answer": "não posso trocar de aba"}},
        ],
    })
    out = await driver.run("Veja o preço", start_url=BUSCA)
    # A tarefa do principal NÃO parou: quem decide o irreversível é ele.
    assert out.status == "completed", out.summary
    resumo = out.steps[2].detail
    assert "- B: parou antes de «Comprar agora» (não desfaz)" in resumo
    assert not [a for n, a in nav.acted if n == "browser.click" and a.get("ref") == 4]


@pytest.mark.asyncio
async def test_subagente_que_tenta_trocar_de_aba_e_recusado():
    nav = _Navegador()
    driver, _ = _driver_roteado(nav, {
        "Veja o preço": [
            {"action": "abrir_aba", "args": {"ref": 27}},
            {"action": "paralelo", "args": {"tarefas": [{"aba": "B", "objetivo": "olhe a A"}]}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        "olhe a A": [
            {"action": "trocar_aba", "args": {"aba": "A"}},
            {"action": "done", "args": {"answer": "fiquei na B"}},
        ],
    })
    filhos: list = []
    original = BrowserLocalDriver._novo_filho

    def espiao(self, aba, passos):
        f = original(self, aba, passos)
        filhos.append(f)
        return f

    BrowserLocalDriver._novo_filho = espiao
    try:
        await driver.run("Veja o preço", start_url=BUSCA)
    finally:
        BrowserLocalDriver._novo_filho = original
    recusa = filhos[0].steps[0]
    assert not recusa.ok and "subagente não usa `trocar_aba`" in recusa.detail


@pytest.mark.asyncio
async def test_paralelo_recusa_tarefa_mal_formada():
    nav = _Navegador()
    driver, _ = _driver_roteado(nav, {"Veja o preço": [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "paralelo", "args": {"tarefas": [{"aba": "Z", "objetivo": "x"}]}},
        {"action": "paralelo", "args": {"tarefas": [{"aba": "B", "objetivo": "x"}, {"aba": "B", "objetivo": "y"}]}},
        {"action": "paralelo", "args": {"tarefas": [{"aba": "B"}]}},
        {"action": "ler_abas", "args": {"abas": ["Q"]}},
        {"action": "done", "args": {"answer": "ok"}},
    ]})
    out = await driver.run("Veja o preço", start_url=BUSCA)
    erros = [s.detail for s in out.steps[1:5]]
    assert "a aba «Z» não está aberta" in erros[0]
    assert "apareceu duas vezes" in erros[1]
    assert "precisa de `objetivo`" in erros[2]
    assert "«Q» não está aberta" in erros[3]


@pytest.mark.asyncio
async def test_no_headless_o_subagente_nao_fecha_a_aba_do_principal():
    nav = _Navegador()
    driver, _ = _driver_roteado(nav, {
        "Veja o preço": [
            {"action": "abrir_aba", "args": {"ref": 27}},
            {"action": "paralelo", "args": {"tarefas": [{"aba": "B", "objetivo": "leia B"}]}},
            {"action": "trocar_aba", "args": {"aba": "B"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        "leia B": [
            {"action": "note", "args": {"texto": "Placa — R$1.976,99"}},
            {"action": "done", "args": {"answer": "lido"}},
        ],
    }, headless=True)
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    # A B sobreviveu ao filho (o principal ainda trocou para ela) e é a entregue;
    # no headless, a inicial A fecha no fim.
    assert out.tab_ref == 43
    assert nav.fechadas == [42]


def test_diario_do_subagente_fica_na_mesma_pasta_com_prints_proprios(tmp_path):
    from core.browser_local.trace import RunTrace

    principal = RunTrace(tmp_path, "job-1")
    filho = principal.filho("B")
    filho.note("anotei", "Placa — R$1.976,99")
    nome = filho.shot(3, "UFJJTlQ=", True)
    assert (tmp_path / "job-1" / "trace-B.md").read_text(encoding="utf-8").count("anotei") == 1
    assert nome == "B-03-tela.jpg" and (tmp_path / "job-1" / "B-03-tela.jpg").exists()
    assert not principal.path.exists() or "anotei" not in principal.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_descartar_tres_anuncios_seguidos_nao_e_giro():
    """Voltar à lista depois de cada anúncio descartado é progresso, não giro.

    Sem a correção, a terceira volta à mesma tela da lista com o caderno vazio
    batia no `_CICLO_TELA_MAX` e matava a tarefa antes de ela procurar de novo.
    """
    nav = _Navegador()
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "trocar_aba", "args": {"aba": "B"}},
        {"action": "fechar_aba", "args": {"motivo": "não é a placa"}},
        {"action": "abrir_aba", "args": {"ref": 28}},
        {"action": "trocar_aba", "args": {"aba": "C"}},
        {"action": "fechar_aba", "args": {"motivo": "não é a placa"}},
        {"action": "abrir_aba", "args": {"url": "https://pt.aliexpress.com/item/3.html"}},
        {"action": "trocar_aba", "args": {"aba": "D"}},
        {"action": "fechar_aba", "args": {"motivo": "não é a placa"}},
        {"action": "done", "args": {"answer": "nenhum dos três é a placa"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    assert len([s for s in out.steps if s.action == "fechar_aba" and s.ok]) == 3


@pytest.mark.asyncio
async def test_bloco_abas_aponta_o_caminho_curto_para_as_nao_lidas():
    """Na regra do sistema, 1–2/6 liam todas de uma vez; no bloco ABAS, 5/6.

    Job 9a3223a4: com UMA aberta, ele ia até ela (6/6) e nunca tinha duas para
    trabalhar juntas; com a dica de abrir as outras antes, 6/6 abriram a próxima.
    """
    nav = _Navegador()
    driver, provider = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "abrir_aba", "args": {"ref": 28}},
        {"action": "trocar_aba", "args": {"aba": "B"}},
        {"action": "note", "args": {"texto": "Placa — R$1.976,99"}},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert "`trocar_aba`" not in out.steps[0].detail
    uma = _prompt(provider, 1)
    assert "→ B aberta, ainda não lida. A tarefa pede outros itens desta lista? Abra-os AGORA" in uma
    duas = _prompt(provider, 2)
    assert "→ ainda não lidas: B, C. Falta abrir algum item que a tarefa pede? Abra antes." in duas
    assert '`ler_abas {"abas": ["B", "C"]}`' in duas
    assert '`paralelo {"tarefas": [{"aba": "B", "objetivo": "…"}, {"aba": "C", "objetivo": "…"}]}`' in duas
    # Na B, com a C por ler: não está na lista de onde a abriu, nada de "abra mais".
    assert "ainda não lid" not in _prompt(provider, 3)
    assert "ainda não lid" not in _prompt(provider, 4)


@pytest.mark.asyncio
async def test_argumento_solto_fora_do_args_vale():
    """`{"action": "paralelo", "tarefas": […]}`: 6/6 com a dica nova, e era recusado."""
    nav = _Navegador()
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "abrir_aba", "args": {"ref": 28}},
        {"action": "ler_abas", "abas": ["B", "C"]},
        {"action": "done", "args": {"answer": "ok"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.steps[2].ok, out.steps[2].detail
    assert sorted(nav.extraidas) == [43, 44]


# ── 11. o E2E da F3 (job b8648f0e): o que saiu estranho ─────────────────────


def test_subdominio_do_site_esta_no_escopo_e_sufixo_falso_nao():
    """`aliexpress.com` redirecionou para `pt.aliexpress.com`, e o `navigate` para a
    página em que a aba JÁ estava foi recusado como "fora do escopo"."""
    driver, _ = _driver(_Navegador(), [])
    driver._allowed_hosts = {"aliexpress.com", "www.aliexpress.com"}
    driver._navegou = True
    driver._check_navigation("https://pt.aliexpress.com/w/wholesale-AMD-BC-250.html")
    with pytest.raises(ValueError, match="fora do escopo"):
        driver._check_navigation("https://aliexpress.com.golpe.tld/x")
    with pytest.raises(ValueError, match="fora do escopo"):
        driver._check_navigation("https://outra-loja.com/x")


@pytest.mark.asyncio
async def test_so_anotar_sem_acao_e_passo_de_anotacao_e_nao_falha():
    nav = _Navegador()
    driver, _ = _driver(nav, [
        {"thought": "anoto os dois", "anotar": ["Placa — R$1.976,99", "Memória — R$4.369,42"]},
        {"action": "done", "args": {"answer": "pronto"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.status == "completed", out.summary
    assert out.notes == ["Placa — R$1.976,99", "Memória — R$4.369,42"]
    assert out.steps[0].action == "note" and out.steps[0].ok
    assert not [s for s in out.steps if not s.ok]


@pytest.mark.asyncio
async def test_nota_que_ja_diz_a_aba_nao_ganha_a_ativa_por_cima():
    """"[aba C] [aba A]" — lida na C pelo `ler_abas`, escrita com a A ativa."""
    nav = _Navegador()
    driver, _ = _driver(nav, [
        {"action": "abrir_aba", "args": {"ref": 27}},
        {"action": "abrir_aba", "args": {"ref": 28}},
        {"action": "ler_abas", "args": {"abas": ["B", "C"]}},
        {"action": "note", "args": {"texto": "Memória Gráfica — R$4.369,42 [aba C]"}},
        {"action": "note", "args": {"texto": "Placa — R$1.976,99"}},
        {"action": "done", "args": {"answer": "pronto"}},
    ])
    out = await driver.run("Veja o preço", start_url=BUSCA)
    assert out.notes == ["Memória Gráfica — R$4.369,42 [aba C]", "Placa — R$1.976,99 [aba A]"]

    from core.jobs.runner_browser_local import _frase_das_abas

    abas = [{"rotulo": "A", "origem": "inicial"}, {"rotulo": "C", "origem": "agente"},
            {"rotulo": "D", "origem": "agente"}]
    # A contagem acha a marca em qualquer lugar (as notas antigas têm as duas).
    assert "Abri e li 2 página(s) em abas separadas (C, D)" in _frase_das_abas(
        type("O", (), {"abas": abas, "notes": ["x [aba C] [aba A]", "y [aba D] [aba A]"]})()
    )
