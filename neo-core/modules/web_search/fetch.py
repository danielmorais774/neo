"""``web_fetch`` — baixa uma URL pública e extrai texto legível (trafilatura/pypdf).

Guard SSRF in-code (o neo-core não está atrás do workbench-proxy): antes de CADA
GET (inclusive redirects) o host é resolvido e QUALQUER IP privado/loopback/
link-local/reservado é recusado com ``blocked_private``. Política "internet sim,
LAN não". Ver docs/web-search-deep-research-plan.md §3.3/§8.

PDF é cidadão de primeira classe (docs/deep-research-v2-plan.md §12.3): a fonte
primária brasileira mora em repositório institucional, que serve
``bitstream/*.pdf`` — sem isso, metade das fontes tier 1 de uma pesquisa
acadêmica morre em ``fetch_failed``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import threading
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from modules.web_search.blocking import classify

logger = logging.getLogger(__name__)

# Formato exigido pela política de UA da Wikimedia (nome/versão + contato +
# lib); sem o contato, wikipedia.org responde 403.
USER_AGENT = "NeoAssistant/1.0 (personal assistant; contact: daniel.morais@acme.com.br) python-httpx"

# ── Perfil de headers ────────────────────────────────────────────────────────
#
# O UA acima se identifica como robô, e era o ÚNICO que saía daqui. Isso não é
# neutro: WAF de CDN bloqueia na hora, e o custo não fica no fetch — ele
# cascateia, porque toda página recusada vira tentativa de navegador, que é o
# caminho caro (na sessão 8c449cb8 virou um job de 570s que terminou em nada).
#
# Medido em 2026-09-20, da VPS, contra a página de dados abertos do portal de dados:
#
#     UA de bot            → 403 (0 KB)
#     headers coerentes    → 200 (230 KB)
#
# O que destrava não é o User-Agent sozinho — é o CONJUNTO. Testei header a
# header contra o CDN do portal de dados e nenhum isolado passa; só o conjunto completo.
# WAF pontua coerência: um Chrome que não manda `Sec-Fetch-*` nem
# `Accept-Language` não é um Chrome.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
_BOT_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6"}

# Hosts cuja política de uso EXIGE identificação. Medido: os dois perfis dão 200
# na Wikipédia, então isto não é necessidade técnica — é cumprir a regra deles
# onde ela existe. Em todo o resto, o perfil de navegador.
_POLICY_UA_SUFFIXES = (
    "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org",
    "wikisource.org", "wikibooks.org", "wikiquote.org", "wikinews.org",
    "wikiversity.org", "wikivoyage.org", "mediawiki.org",
)


def headers_for(host: str) -> dict[str, str]:
    """Perfil de headers do host: identificado onde a política pede, navegador no resto."""
    h = (host or "").lower().rstrip(".")
    if any(h == suf or h.endswith("." + suf) for suf in _POLICY_UA_SUFFIXES):
        return dict(_BOT_HEADERS)
    return dict(_BROWSER_HEADERS)
_MAX_REDIRECTS = 3
_MIN_EXTRACTED_CHARS = 200
_FETCH_TIMEOUT = 15.0
_MAX_BODY_BYTES = 3_000_000  # 3MB de HTML é mais que suficiente p/ extração
_MAX_PDF_BYTES = 15_000_000  # papers passam de 3MB; PDF gigante é digitalização
_MAX_PDF_PAGES = 40  # tese inteira não cabe no contexto — as primeiras bastam


@dataclass
class FetchedPage:
    title: str
    text: str
    truncated: bool
    url: str


@dataclass
class FetchError:
    """Falha de fetch, classificada pelo que ela PEDE como próximo passo.

    - ``blocked_origin``  o site recusou esta origem (anti-bot) → trocar de
                          origem (navegador do usuário) muda o resultado.
    - ``js_required``     veio HTML, mas o conteúdo depende de JS → navegador
                          resolve, a origem não é o problema.
    - ``not_found``       a URL está errada → NÃO escalar, procurar a certa.
    - ``transient``       5xx/timeout → retry, e só.
    - ``blocked_private`` guard de SSRF.
    - ``fetch_failed``    o resto.

    Só ``blocked_origin`` e ``js_required`` sobem a escada de navegador. Essa
    distinção é o ponto: escalar num 404 queima minutos por uma página que não
    existe.
    """

    error: str
    message: str


async def _host_is_private(host: str) -> bool:
    """Resolve o host e retorna True se QUALQUER IP for privado/loopback/etc."""
    try:
        # IP literal não precisa de DNS.
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        pass
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
    except OSError:
        # Não resolve → o GET vai falhar sozinho; não é caso de blocked_private.
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


# trafilatura/htmldate rodam sobre lxml (extensão C) e NÃO são seguros quando
# várias threads extraem ao mesmo tempo: com o pool de fetch em paralelo isso
# corrompeu o heap ("double free or corruption") e MATOU o processo do neo-core
# em produção — o container ficava de pé com o uvicorn morto. A extração custa
# milissegundos perto do I/O de rede, então serializar não pesa na latência.
_EXTRACT_LOCK = threading.Lock()


def _extract_sync(html: str) -> tuple[str, str]:
    """(title, text) via trafilatura — síncrono, roda em to_thread (serializado)."""
    import trafilatura

    with _EXTRACT_LOCK:
        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
        title = ""
        try:
            meta = trafilatura.extract_metadata(html)
            if meta is not None:
                title = meta.title or ""
        except Exception:  # noqa: BLE001
            title = ""
    return title, text


def _extract_pdf_sync(data: bytes) -> tuple[str, str]:
    """(title, text) de um PDF via pypdf — síncrono, roda em to_thread."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages[:_MAX_PDF_PAGES]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 — página quebrada não derruba o documento
            continue
    title = ""
    try:
        title = (reader.metadata or {}).get("/Title") or ""
    except Exception:  # noqa: BLE001
        title = ""
    return str(title).strip(), "\n".join(p.strip() for p in parts if p.strip())


def _looks_like_pdf(content_type: str, url: str, body: bytes) -> bool:
    if "application/pdf" in content_type or "application/x-pdf" in content_type:
        return True
    if body[:5] == b"%PDF-":  # servidor mentindo no content-type (comum em repositórios)
        return True
    return url.lower().split("?")[0].endswith(".pdf")


async def fetch_readable(url: str, *, max_chars: int = 8000) -> FetchedPage | FetchError:
    """GET com até 3 redirects manuais, SSRF guard por hop, extração de texto."""
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return FetchError("fetch_failed", f"Esquema não suportado: {parsed.scheme or '(vazio)'}")
        host = parsed.hostname or ""
        if not host:
            return FetchError("fetch_failed", "URL sem host.")
        if await _host_is_private(host):
            return FetchError(
                "blocked_private",
                f"Host '{host}' resolve para IP privado/local — acesso bloqueado (SSRF guard).",
            )
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=_FETCH_TIMEOUT,
                headers=headers_for(host),
            ) as client:
                resp = await client.get(current)
        except httpx.TimeoutException as e:
            # Timeout é transitório, não bloqueio: trocar de navegador não
            # acelera um servidor lento.
            return FetchError("transient", f"Tempo esgotado ao buscar {current}: {e}")
        except httpx.HTTPError as e:
            return FetchError("fetch_failed", f"Falha no GET: {e}")

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return FetchError("fetch_failed", f"Redirect {resp.status_code} sem Location.")
            current = urljoin(current, location)
            continue

        content_type = (resp.headers.get("content-type") or "").lower()
        raw_body = resp.content

        # Classifica ANTES de tentar extrair: um 403 do WAF e um 200 com
        # interstitial do Cloudflare são a mesma coisa para quem decide o
        # próximo passo, e nenhum dos dois tem conteúdo para extrair.
        problema = classify(
            status=resp.status_code, headers=dict(resp.headers), body=raw_body
        )
        if problema is not None:
            codigo, mensagem = problema
            logger.info("[web_fetch] %s em %s — %s", codigo, current, mensagem[:120])
            return FetchError(codigo, f"{mensagem} (url: {current})")

        if _looks_like_pdf(content_type, current, raw_body):
            try:
                title, text = await asyncio.to_thread(
                    _extract_pdf_sync, raw_body[:_MAX_PDF_BYTES]
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[web_fetch] PDF ilegível em %s: %s", current, e)
                return FetchError("fetch_failed", f"PDF ilegível: {e}")
            if len(text.strip()) < _MIN_EXTRACTED_CHARS:
                # PDF de digitalização (imagem) — sem OCR não há texto a extrair.
                return FetchError(
                    "js_required",
                    "PDF sem camada de texto (provável digitalização). Tente outra fonte.",
                )
            return FetchedPage(
                title=title or (parsed.hostname or ""),
                text=text[:max_chars],
                truncated=len(text) > max_chars,
                url=current,
            )

        body = raw_body[:_MAX_BODY_BYTES]

        if "text/plain" in content_type:
            text = body.decode(resp.encoding or "utf-8", errors="replace")
            truncated = len(text) > max_chars
            return FetchedPage(
                title=parsed.hostname or "", text=text[:max_chars], truncated=truncated, url=current
            )
        if "html" not in content_type and "xml" not in content_type:
            # Não é página: é ARQUIVO. Distinguir importa porque a saída é
            # outra — o `web_fetch` extrai texto, e o que se faz com um .zip é
            # baixar. Enquanto isso era `fetch_failed`, o modelo lia "falhou" e
            # ia tentar navegador, que é o pior jeito de pegar um binário (cai
            # na pasta de downloads, sem bytes na mão, com nome imprevisível).
            tamanho = resp.headers.get("content-length")
            quanto = f" (~{int(tamanho) // 1024} KB)" if (tamanho or "").isdigit() else ""
            return FetchError(
                "download",
                f"Esta URL não é uma página: é um arquivo {content_type or '(tipo desconhecido)'}"
                f"{quanto}, e ele está ACESSÍVEL (HTTP {resp.status_code}) — não há bloqueio "
                "aqui. `web_fetch` só extrai texto de página. Para pegar o arquivo, baixe-o no "
                "sandbox com `neo_bash` (curl) e trate de lá; se o usuário quiser o arquivo no "
                "disco dele, publique com `file_save` e entregue com `save_generated_file`. "
                "NÃO use navegador para isto.",
            )

        html = body.decode(resp.encoding or "utf-8", errors="replace")
        try:
            title, text = await asyncio.to_thread(_extract_sync, html)
        except Exception as e:  # noqa: BLE001
            logger.warning("[web_fetch] extração falhou para %s: %s", current, e)
            return FetchError("fetch_failed", f"Falha na extração de texto: {e}")

        if len((text or "").strip()) < _MIN_EXTRACTED_CHARS:
            return FetchError(
                "js_required",
                "Página sem texto extraível (provável render por JavaScript). Tente outra fonte.",
            )
        truncated = len(text) > max_chars
        return FetchedPage(title=title, text=text[:max_chars], truncated=truncated, url=current)

    return FetchError("fetch_failed", f"Redirects demais (>{_MAX_REDIRECTS}).")
