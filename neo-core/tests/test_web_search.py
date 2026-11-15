"""Web search (camada 1) — cadeia de backends, cache e guard SSRF do fetch.

Tudo com fakes (sem rede): backends scriptados no executor e monkeypatch do
resolver/fetch no guard. Ver docs/web-search-deep-research-plan.md §12.
"""

import pytest

from modules.web_search import fetch as fetch_mod
from modules.web_search.backends import SearchResult
from modules.web_search.executor import WebSearchExecutor
from modules.web_search.fetch import FetchError, fetch_readable


# ── fakes ───────────────────────────────────────────────────────────────────
class _FakeBackend:
    def __init__(self, name, results=None, fail=False):
        self.name = name
        self.results = results or []
        self.fail = fail
        self.calls = 0
        self.categories: list[str] = []

    async def search(self, query, *, recency, category, n):
        self.calls += 1
        self.categories.append(category)
        if self.fail:
            raise RuntimeError("backend down")
        return self.results[:n]


def _executor_with(primary, fallback):
    ex = WebSearchExecutor()
    ex.backends = [primary, fallback]
    return ex


_HIT = SearchResult(title="Resultado", url="https://exemplo.com/a", snippet="trecho")


# ── cadeia de backends ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_primary_ok_fallback_untouched():
    primary = _FakeBackend("searxng", results=[_HIT])
    fallback = _FakeBackend("ddgs", results=[_HIT])
    ex = _executor_with(primary, fallback)
    out = await ex.search("teste")
    assert out["backend"] == "searxng"
    assert out["results"][0]["url"] == "https://exemplo.com/a"
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_primary_fails_chain_uses_fallback_and_degrades():
    primary = _FakeBackend("searxng", fail=True)
    fallback = _FakeBackend("ddgs", results=[_HIT])
    ex = _executor_with(primary, fallback)

    out = await ex.search("teste")
    assert out["backend"] == "ddgs"
    assert primary.calls == 1

    # Próximo request (query diferente = sem cache) pula o degradado direto.
    out2 = await ex.search("outra coisa")
    assert out2["backend"] == "ddgs"
    assert primary.calls == 1  # não pagou o timeout de novo


@pytest.mark.asyncio
async def test_all_backends_fail_returns_clean_error():
    ex = _executor_with(_FakeBackend("searxng", fail=True), _FakeBackend("ddgs", fail=True))
    out = await ex.search("teste")
    assert out["error"] == "all_backends_failed"
    assert "não conseguiu verificar" in out["message"]


# ── cache ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cache_hit_does_not_search_again():
    primary = _FakeBackend("searxng", results=[_HIT])
    ex = _executor_with(primary, _FakeBackend("ddgs"))
    await ex.search("mesma query")
    await ex.search("mesma query")
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_invalid_params_are_normalized():
    primary = _FakeBackend("searxng", results=[_HIT])
    ex = _executor_with(primary, _FakeBackend("ddgs"))
    out = await ex.search("q", recency="ontem", category="esporte", n=99)
    assert out["backend"] == "searxng"
    assert primary.categories == ["general"]  # categoria inválida → general
    empty = await ex.search("")
    assert empty["error"] == "empty_query"


@pytest.mark.asyncio
async def test_science_category_reaches_the_backend():
    """A categoria científica (arXiv/Scholar/PubMed no SearXNG) é a matéria-prima
    do deep research acadêmico — não pode ser normalizada para 'general'."""
    primary = _FakeBackend("searxng", results=[_HIT])
    ex = _executor_with(primary, _FakeBackend("ddgs"))
    out = await ex.search("industrial revolution", category="science")
    assert out["backend"] == "searxng"
    assert primary.categories == ["science"]


@pytest.mark.asyncio
async def test_searxng_sends_science_category_and_json_format(monkeypatch):
    """O GET no SearXNG precisa levar categories=science (e format=json, o gotcha
    nº1 do deploy: sem formats:[html,json] no settings.yml a API dá 403)."""
    import httpx

    from modules.web_search.backends import SearxngBackend

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"title": "Paper", "url": "https://arxiv.org/abs/1", "content": "abstract"}]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    results = await SearxngBackend("http://searxng:8080").search(
        "industrial revolution", category="science", recency="any", n=5
    )

    assert captured["params"]["categories"] == "science"
    assert captured["params"]["format"] == "json"
    assert "time_range" not in captured["params"]  # recency 'any' não filtra
    assert results[0].url == "https://arxiv.org/abs/1"
    # Session-34d7f528: com `pt-BR` o Google CSE só devolvia página em português e
    # "Windows XP error sound wav" voltava zero. O idioma vem de [web] no neo.toml.
    from shared.ajustes import ajustes

    assert captured["params"]["language"] == ajustes().web.idioma_busca == "all"


# ── guard SSRF do fetch ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fetch_blocks_private_ip_literal():
    result = await fetch_readable("http://192.168.4.186/painel")
    assert isinstance(result, FetchError)
    assert result.error == "blocked_private"


@pytest.mark.asyncio
async def test_fetch_blocks_localhost():
    result = await fetch_readable("http://localhost:8000/api")
    assert isinstance(result, FetchError)
    assert result.error == "blocked_private"


@pytest.mark.asyncio
async def test_fetch_blocks_redirect_to_private(monkeypatch):
    """Página pública que redireciona para IP privado → blocked_private."""

    class _Resp:
        status_code = 302
        headers = {"location": "http://10.0.0.5/interno"}
        content = b""
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

    async def _fake_private(host):
        # público na 1ª volta; privado quando o redirect aponta pra 10.0.0.5
        return host == "10.0.0.5"

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(fetch_mod, "_host_is_private", _fake_private)
    result = await fetch_readable("http://publico.com/pagina")
    assert isinstance(result, FetchError)
    assert result.error == "blocked_private"


@pytest.mark.asyncio
async def test_fetch_rejects_non_http_scheme():
    result = await fetch_readable("ftp://exemplo.com/arquivo")
    assert isinstance(result, FetchError)
    assert result.error == "fetch_failed"


# ── PDF (docs/deep-research-v2-plan.md §12.3) ───────────────────────────────
def _make_pdf(text: str) -> bytes:
    """PDF de 1 página com camada de texto real (content stream + xref válidos).

    Anotação não serve: ``extract_text`` só lê o content stream, que é
    justamente o que um paper de repositório tem.
    """
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    content = f"BT /F1 12 Tf 40 700 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + obj + b"endobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _pdf_client(body: bytes, content_type: str):
    class _Resp:
        status_code = 200
        headers = {"content-type": content_type}
        content = body
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


@pytest.mark.asyncio
async def test_fetch_extracts_text_from_pdf(monkeypatch):
    """Repositório acadêmico serve bitstream/*.pdf — sem isso metade das fontes
    tier 1 de uma pesquisa morria em fetch_failed (era o gargalo em prod)."""
    body = _make_pdf("Energia solar no Nordeste cresceu 40 por cento em 2025 " * 6)
    monkeypatch.setattr(
        fetch_mod.httpx, "AsyncClient", _pdf_client(body, "application/pdf")
    )
    monkeypatch.setattr(fetch_mod, "_host_is_private", lambda h: _false())
    result = await fetch_readable("https://repositorio.ufpi.br/bitstream/1/x.pdf")
    assert not isinstance(result, FetchError), getattr(result, "message", "")
    assert "Energia solar" in result.text


@pytest.mark.asyncio
async def test_fetch_detects_pdf_even_when_content_type_lies(monkeypatch):
    """Repositórios frequentemente servem PDF como text/html."""
    body = _make_pdf("Relatorio tecnico com dados de geracao distribuida " * 8)
    monkeypatch.setattr(
        fetch_mod.httpx, "AsyncClient", _pdf_client(body, "text/html")
    )
    monkeypatch.setattr(fetch_mod, "_host_is_private", lambda h: _false())
    result = await fetch_readable("https://exemplo.org/download?id=9")
    assert not isinstance(result, FetchError), getattr(result, "message", "")
    assert "Relatorio tecnico" in result.text


@pytest.mark.asyncio
async def test_scanned_pdf_without_text_layer_is_js_required(monkeypatch):
    body = _make_pdf("x")  # sem camada de texto útil
    monkeypatch.setattr(
        fetch_mod.httpx, "AsyncClient", _pdf_client(body, "application/pdf")
    )
    monkeypatch.setattr(fetch_mod, "_host_is_private", lambda h: _false())
    result = await fetch_readable("https://exemplo.org/scan.pdf")
    assert isinstance(result, FetchError)
    assert result.error == "js_required"


async def _false():
    return False


@pytest.mark.asyncio
async def test_concurrent_html_extraction_is_serialized(monkeypatch):
    """trafilatura roda sobre lxml (extensão C) e corrompeu o heap ("double free
    or corruption") quando o pool de fetch extraía em paralelo — o processo do
    neo-core MORREU em produção (container de pé, uvicorn morto). A extração é
    serializada por lock; este teste exercita o caminho concorrente."""
    html = (
        "<html><head><title>Estudo</title></head><body>"
        + "<p>Dado relevante sobre energia solar no Nordeste brasileiro.</p>" * 40
        + "</body></html>"
    ).encode()

    monkeypatch.setattr(
        fetch_mod.httpx, "AsyncClient", _pdf_client(html, "text/html; charset=utf-8")
    )
    monkeypatch.setattr(fetch_mod, "_host_is_private", lambda h: _false())

    import asyncio

    results = await asyncio.gather(
        *(fetch_readable(f"https://exemplo.org/p{i}") for i in range(12))
    )
    assert all(not isinstance(r, FetchError) for r in results)
    assert all("energia solar" in r.text for r in results)
