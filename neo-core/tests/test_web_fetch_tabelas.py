"""`web_fetch` devolve as tabelas de DADOS que o extrator de texto descartou.

Session-607c5313 (14/11): "10 países mais populosos → .xlsx". O trafilatura
descartou a tabela da "Lista de países por população" (muito link) e o
`web_fetch` voltou só a introdução. O cérebro foi ao wikitext pela API e passou
24 das 29 rodadas escrevendo regex: leu a coluna da posição como população, e a
China saiu sem nome.

O acréscimo não pode mexer no que já funcionava: o texto e os links (o caminho
do download dos sons do Windows) saem iguais, e as tabelas vêm DEPOIS dos links.
"""

from __future__ import annotations

import pytest

from modules.web_search import fetch as fetch_mod
from modules.web_search.executor import WebSearchExecutor
from modules.web_search.fetch import FetchedPage, _tabelas_sync, fetch_readable

# O formato do HTML da Wikipédia, reduzido: coluna de ícone (sem texto), nota de
# rodapé, chave de ordenação escondida e número com espaço fino/não separável.
_WIKITABLE = """
<table class="wikitable sortable">
<tr><th></th><th>Posição</th><th>País</th><th>Estimativa da ONU (2021)</th></tr>
<tr><td><img alt="Estável" src="x.png"></td><td>1</td><td><a href="/wiki/Índia">Índia</a></td>
<td>1 417 492 000<sup class="mw-ref reference"><a href="#n1">[1]</a></sup></td></tr>
<tr><td><img alt="Baixa" src="y.png"></td><td>2</td><td><a href="/wiki/China">China</a></td>
<td><span class="sortkey" style="display:none">7001</span>1\xa0407\xa0934\xa0000</td></tr>
<tr><td></td><td>3</td><td>Estados Unidos</td><td>342 181 000</td></tr>
</table>
"""

_NAVBOX = """
<table class="nowraplinks hlist navbox-inner"><tr><td>
<table class="navbox-subgroup"><tr><th>Ásia</th><td>Índia · China</td></tr>
<tr><th>Europa</th><td>França · Itália</td></tr></table>
</td></tr></table>
"""

# O player do winhistory.de: uma tabela por som, só ícone embaixo do cabeçalho.
_PLAYER = """
<table><tr><th>Streaming</th><th>MP3</th><th>Wave</th></tr>
<tr><td><audio src="a.ogg"></audio></td><td>\U0001F3A7</td><td>\U0001F4BE</td></tr></table>
"""


def _html(*tabelas: str) -> str:
    return "<html><body><p>Lista.</p>" + "".join(tabelas) + "</body></html>"


def test_tabela_da_wikipedia_sai_limpa():
    blocos = _tabelas_sync(_html(_WIKITABLE), 4000)

    assert len(blocos) == 1
    linhas = blocos[0].splitlines()
    assert linhas[0] == "Tabela 1 — 4 linhas:"
    # A coluna do ícone (vazia em toda linha) some; nota e chave escondida também.
    assert linhas[1] == "Posição | País | Estimativa da ONU (2021)"
    assert linhas[2] == "1 | Índia | 1 417 492 000"
    assert linhas[3] == "2 | China | 1 407 934 000"
    assert linhas[4] == "3 | Estados Unidos | 342 181 000"


def test_navegacao_e_player_de_som_nao_sao_dado():
    assert _tabelas_sync(_html(_NAVBOX), 4000) == []
    assert _tabelas_sync(_html(_PLAYER * 19), 4000) == []


def test_tabela_de_layout_e_menu_ficam_de_fora():
    layout = "<table><tr><td>" + _WIKITABLE + "</td><td>lado</td></tr></table>"
    no_menu = "<nav>" + _WIKITABLE + "</nav>"
    assert _tabelas_sync(_html(layout), 4000) == []
    assert _tabelas_sync(_html(no_menu), 4000) == []


def test_tabela_repetida_entra_uma_vez():
    assert len(_tabelas_sync(_html(_WIKITABLE, _WIKITABLE), 4000)) == 1


def test_tabela_que_o_texto_ja_tem_nao_repete():
    """A lista de área da Wikipédia: o trafilatura guarda, e ela não entra de novo."""
    texto = (
        "| Posição | País |\n| 1 | Índia | 1 417 492 000 |\n| 2 | China | 1 407 934 000 |\n"
        "| 3 | Estados Unidos | 342 181 000 |"
    )
    assert _tabelas_sync(_html(_WIKITABLE), 4000, texto) == []
    # Texto que só fala do assunto não conta como a tabela.
    assert len(_tabelas_sync(_html(_WIKITABLE), 4000, "A Índia e a China lideram.")) == 1


def test_teto_corta_e_avisa_o_que_ficou_de_fora():
    linhas = "".join(
        f"<tr><td>{i}</td><td>País {i}</td><td>{i * 1000}</td></tr>" for i in range(1, 201)
    )
    grande = f"<table><tr><th>N</th><th>País</th><th>Pop</th></tr>{linhas}</table>"
    outra = "<table><tr><th>A</th><th>B</th></tr><tr><td>x1</td><td>y1</td></tr></table>"

    blocos = _tabelas_sync(_html(grande, outra), 300)

    assert len(blocos) == 1
    assert "linhas desta tabela não couberam" in blocos[0]
    assert "(+ 1 tabela(s) da página não couberam)" in blocos[0]
    corpo = blocos[0].split("\n(… mais")[0]
    assert len(corpo) <= 300


def test_tabela_que_nao_cabe_nem_uma_linha_nao_entra_vazia():
    linhas = "".join(f"<tr><td>{i}</td><td>{'x' * 200}</td></tr>" for i in range(5))
    larga = f"<table><tr><th>N</th><th>Texto</th></tr>{linhas}</table>"
    pequena = "<table><tr><th>A</th><th>B</th></tr><tr><td>x1</td><td>y1</td></tr></table>"
    blocos = _tabelas_sync(_html(pequena, larga), 120)
    assert len(blocos) == 1
    assert blocos[0].startswith("Tabela 1 — 2 linhas:\nA | B\nx1 | y1")
    assert "(+ 1 tabela(s) da página não couberam)" in blocos[0]


def test_teto_zero_desliga():
    assert _tabelas_sync(_html(_WIKITABLE), 0) == []


def test_pagina_sem_table_nem_parseia(monkeypatch):
    import lxml.html

    def _explode(*a, **k):  # pragma: no cover - não pode ser chamado
        raise AssertionError("parseou página sem <table>")

    monkeypatch.setattr(lxml.html, "fromstring", _explode)
    assert _tabelas_sync("<html><body><p>só texto</p></body></html>", 4000) == []


# ── no resultado da tool ────────────────────────────────────────────────────

_URL = "https://pt.wikipedia.org/wiki/Lista_de_pa%C3%ADses_por_popula%C3%A7%C3%A3o"


def _executor_com(page: FetchedPage) -> WebSearchExecutor:
    executor = WebSearchExecutor.__new__(WebSearchExecutor)

    async def _fetch(url, *, max_chars=None):
        return page

    executor.fetch = _fetch
    return executor


@pytest.mark.asyncio
async def test_tabelas_vem_depois_dos_links_dentro_do_delimitador():
    page = FetchedPage(
        title="Lista", text="Introdução da lista.", truncated=False, url=_URL,
        links=["https://www.myinstants.com/media/sounds/connect.mp3"],
        tabelas=["Tabela 1 — 2 linhas:\nPaís | Pop\nÍndia | 1 417 492 000"],
    )
    texto = (await _executor_com(page)._web_fetch({"url": _URL}))["text"]

    assert texto.index("Links da página") < texto.index("Tabelas da página")
    assert "- https://www.myinstants.com/media/sounds/connect.mp3\n\nTabelas da página" in texto
    assert "Índia | 1 417 492 000" in texto
    assert texto.rstrip().endswith("</web_content>")


@pytest.mark.asyncio
async def test_sem_tabelas_o_resultado_e_o_de_antes():
    page = FetchedPage(
        title="t", text="só texto", truncated=False, url=_URL,
        links=["https://www.myinstants.com/media/sounds/connect.mp3"],
    )
    texto = (await _executor_com(page)._web_fetch({"url": _URL}))["text"]
    assert texto == (
        f'<web_content url="{_URL}">\nsó texto\n\n'
        "Links da página (mídia primeiro, depois o mesmo site):\n"
        "- https://www.myinstants.com/media/sounds/connect.mp3\n</web_content>"
    )


# ── no fetch ─────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body: bytes):
        self.status_code = 200
        self.content = body
        self.encoding = "utf-8"
        self.headers = {"content-type": "text/html; charset=utf-8"}


def _cliente(body: bytes):
    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp(body)

    return _Cliente


async def _nao_privado(host):
    return False


# Tabela com link em toda célula, como as listas da Wikipédia: é a que o
# trafilatura descarta (medido: a lista de população real e esta).
_TABELA_DE_LINKS = (
    '<table class="wikitable"><tr><th>N</th><th>País</th><th>População</th></tr>'
    + "".join(
        f'<tr><td><a href="/wiki/P{i}">{i}</a></td>'
        f'<td><a href="/wiki/Pais_{i}">País número {i}</a></td>'
        f'<td><a href="/wiki/Pop_{i}">{i} 000 000</a></td></tr>'
        for i in range(1, 41)
    )
    + "</table>"
)

_PAGINA = (
    "<html><head><meta property='og:audio' content='https://x.com/som.mp3'></head><body>"
    "<p>" + "Texto da página para passar do mínimo de extração. " * 20 + "</p>"
    + _TABELA_DE_LINKS + "</body></html>"
)


@pytest.mark.asyncio
async def test_fetch_traz_tabelas_e_os_links_de_sempre(monkeypatch):
    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", _cliente(_PAGINA.encode()))
    monkeypatch.setattr(fetch_mod, "_host_is_private", _nao_privado)

    page = await fetch_readable(_URL)

    assert isinstance(page, FetchedPage)
    assert page.links[0] == "https://x.com/som.mp3"
    assert "País número 7" not in page.text, "o extrator de texto descartou a tabela"
    assert any("7 | País número 7 | 7 000 000" in t for t in page.tabelas)


@pytest.mark.asyncio
async def test_tabela_que_o_extrator_guarda_nao_vem_duas_vezes(monkeypatch):
    """Página pequena: o trafilatura guarda a tabela no texto, e ela não repete."""
    pagina = (
        "<html><body><p>" + "Texto da página para passar do mínimo. " * 20 + "</p>"
        + _WIKITABLE + "</body></html>"
    )
    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", _cliente(pagina.encode()))
    monkeypatch.setattr(fetch_mod, "_host_is_private", _nao_privado)

    page = await fetch_readable(_URL)

    assert isinstance(page, FetchedPage)
    assert "Índia" in page.text
    assert page.tabelas == []


@pytest.mark.asyncio
async def test_falha_nas_tabelas_nao_derruba_o_fetch(monkeypatch):
    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", _cliente(_PAGINA.encode()))
    monkeypatch.setattr(fetch_mod, "_host_is_private", _nao_privado)

    def _quebra(*a, **k):
        raise RuntimeError("lxml caiu")

    monkeypatch.setattr(fetch_mod, "_extract_tables_sync", _quebra)

    page = await fetch_readable(_URL)

    assert isinstance(page, FetchedPage)
    assert page.tabelas == []
    assert page.links[0] == "https://x.com/som.mp3"


@pytest.mark.asyncio
async def test_teto_zero_no_neo_toml_nao_procura_tabela(monkeypatch):
    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", _cliente(_PAGINA.encode()))
    monkeypatch.setattr(fetch_mod, "_host_is_private", _nao_privado)
    monkeypatch.setattr(fetch_mod, "_tabelas_max", lambda: 0)

    def _nao_chama(*a, **k):  # pragma: no cover - não pode ser chamado
        raise AssertionError("procurou tabela com o teto em 0")

    monkeypatch.setattr(fetch_mod, "_extract_tables_sync", _nao_chama)

    page = await fetch_readable(_URL)
    assert isinstance(page, FetchedPage) and page.tabelas == []
