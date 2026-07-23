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

logger = logging.getLogger(__name__)

# Formato exigido pela política de UA da Wikimedia (nome/versão + contato +
# lib); sem o contato, wikipedia.org responde 403.
USER_AGENT = "NeoAssistant/1.0 (personal assistant; contact: daniel.morais@acme.com.br) python-httpx"
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
    error: str  # js_required | blocked_private | fetch_failed
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
                headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6"},
            ) as client:
                resp = await client.get(current)
        except httpx.HTTPError as e:
            return FetchError("fetch_failed", f"Falha no GET: {e}")

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return FetchError("fetch_failed", f"Redirect {resp.status_code} sem Location.")
            current = urljoin(current, location)
            continue

        if resp.status_code >= 400:
            return FetchError("fetch_failed", f"HTTP {resp.status_code} em {current}")

        content_type = (resp.headers.get("content-type") or "").lower()
        raw_body = resp.content

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
            return FetchError(
                "fetch_failed", f"Content-type não suportado: {content_type or '(desconhecido)'}"
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
