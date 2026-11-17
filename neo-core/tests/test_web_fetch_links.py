"""`web_fetch` devolve os LINKS da página, e eles ganham procedência no portão.

Session-a20f4722 (13/11): o Daniel pediu os sons de "Windows 10 connect/disconnect
USB" no myinstants. O `web_fetch` da busca devolveu 553 caracteres de texto e
NENHUM endereço. O cérebro então:

1. foi barrado no 2º `web_fetch` (URL montada, turno já contaminado), e
2. raspou o HTML no sandbox com `curl` + `grep` em 3 rodadas para achar a página
   de cada som e o `og:audio`.

Com os links no resultado, a próxima página e o MP3 aparecem num resultado de
tool, então têm procedência e o caminho legítimo não esbarra no portão.
"""

from __future__ import annotations

import pytest

from core.authority.turn import TurnAuthority
from modules.web_search import fetch as fetch_mod
from modules.web_search.executor import WebSearchExecutor
from modules.web_search.fetch import FetchedPage, _links_sync, fetch_readable

_BASE = "https://www.myinstants.com/en/search/?name=windows+10+connect"

# O formato real das páginas do myinstants (busca e página do som), reduzido.
_HTML = """<!doctype html><html><head>
<title>Windows 10 Connect Soundboard - Instant Sound Buttons</title>
<meta property="og:audio" content="https://www.myinstants.com/media/sounds/connect.mp3"/>
<meta property="og:image" content="/media/images/thumb.png"/>
</head><body>
<a href="#">topo</a>
<a href="javascript:void(0)">menu</a>
<a href="mailto:contato@myinstants.com">contato</a>
<a href="/en/instant/windows-10-usb-connect-38512/">Windows 10 USB connect</a>
<a href="/en/instant/windows-10-hardware-remove-disconnect-19981/">Windows 10 hardware Remove/disconnect</a>
<a href="/en/instant/windows-10-usb-connect-38512/#comentarios">de novo, com âncora</a>
<a href="https://twitter.com/myinstants">twitter</a>
<audio src="/media/sounds/ding.mp3"></audio>
<p>""" + ("Texto de exemplo para passar do mínimo de extração. " * 20) + """</p>
</body></html>"""


def test_links_absolutos_midia_primeiro_sem_lixo():
    links = _links_sync(_HTML, _BASE, 40)

    assert links[0] == "https://www.myinstants.com/media/sounds/connect.mp3"
    assert "https://www.myinstants.com/media/sounds/ding.mp3" in links[:2]
    assert "https://www.myinstants.com/en/instant/windows-10-usb-connect-38512/" in links
    assert (
        "https://www.myinstants.com/en/instant/windows-10-hardware-remove-disconnect-19981/"
        in links
    )
    # Mesmo site antes de site de fora.
    assert links.index("https://twitter.com/myinstants") > links.index(
        "https://www.myinstants.com/en/instant/windows-10-usb-connect-38512/"
    )
    # Âncora, javascript:, mailto: e a repetição com #fragmento ficam de fora.
    assert not any(u.startswith(("javascript:", "mailto:")) or "#" in u for u in links)
    assert len(links) == len(set(links))


def test_teto_de_links():
    assert len(_links_sync(_HTML, _BASE, 2)) == 2
    assert _links_sync(_HTML, _BASE, 0) == []


def test_html_quebrado_nao_derruba():
    assert _links_sync("", _BASE, 10) == []


def _html_client(body: str):
    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = body.encode()
        encoding = "utf-8"

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    return _Client


async def _false():
    return False


@pytest.mark.asyncio
async def test_fetch_readable_traz_os_links(monkeypatch):
    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", _html_client(_HTML))
    monkeypatch.setattr(fetch_mod, "_host_is_private", lambda h: _false())

    page = await fetch_readable(_BASE)

    assert isinstance(page, FetchedPage)
    assert page.links[0] == "https://www.myinstants.com/media/sounds/connect.mp3"


@pytest.mark.asyncio
async def test_resultado_do_web_fetch_da_procedencia_aos_links():
    """O caso da a20f4722: a página do som e o MP3 passam no portão."""
    executor = WebSearchExecutor.__new__(WebSearchExecutor)

    async def _fetch(url, *, max_chars=None):
        return FetchedPage(
            title="Windows 10 Connect Soundboard",
            text="Myinstants Home Just added",
            truncated=False,
            url=_BASE,
            links=_links_sync(_HTML, _BASE, 40),
        )

    executor.fetch = _fetch
    result = await executor._web_fetch({"url": _BASE})

    assert "Links da página" in result["text"]
    assert result["text"].rstrip().endswith("</web_content>"), "links ficam DENTRO do delimitador"

    authority = TurnAuthority()
    authority.note_tool_result("web_fetch", result)
    assert authority.tainted
    assert authority.url_has_provenance(
        "https://www.myinstants.com/en/instant/windows-10-hardware-remove-disconnect-19981/"
    )
    assert authority.url_has_provenance("https://www.myinstants.com/media/sounds/connect.mp3")


@pytest.mark.asyncio
async def test_pagina_sem_links_fica_como_antes():
    executor = WebSearchExecutor.__new__(WebSearchExecutor)

    async def _fetch(url, *, max_chars=None):
        return FetchedPage(title="t", text="só texto", truncated=False, url=_BASE)

    executor.fetch = _fetch
    result = await executor._web_fetch({"url": _BASE})
    assert "Links da página" not in result["text"]


def test_menu_do_site_vai_para_o_fim():
    """Na busca do myinstants o menu eram os 23 primeiros links."""
    html = (
        "<html><body><nav>"
        + "".join(f'<a href="/en/categories/c{i}/">c{i}</a>' for i in range(30))
        + '</nav><div id="instants_container">'
        '<a href="/en/instant/windows-10-usb-disconnect-8906/">disconnect</a>'
        "</div><footer><a href=\"/sobre/\">sobre</a></footer></body></html>"
    )
    links = _links_sync(html, _BASE, 5)
    assert links[0] == "https://www.myinstants.com/en/instant/windows-10-usb-disconnect-8906/"
