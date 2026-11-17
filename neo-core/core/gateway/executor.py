"""HTTP Executor (docs/service-gateway-dsl-plan.md §2.2).

O ÚNICO ponto do sistema que monta a URL completa e toca a rede com a
credencial aplicada. Recebe a request já validada, autorizada (policy=allow) e
o ``ResolvedAuth`` do broker. Responsabilidades: montar a URL (base_url fixo +
path normalizado — o host jamais vem do LLM), aplicar auth, timeout, retry só
para idempotentes, redirects OFF por default (revalidando host se ligados),
medir duração/tamanho e redactar+truncar a resposta.

Espelha o estilo de retry/backoff do adapter HA
(core/skills/home_assistant/adapter.py): 4xx é permanente, 5xx/rede re-tenta.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from typing import Any

import httpx

from core.gateway.broker import ResolvedAuth
from core.gateway.ir import (
    IDEMPOTENT_METHODS,
    ConnectionConfig,
    GatewayError,
    ServiceRequestIR,
    ServiceResponse,
)
from core.gateway.redact import cap_body, redact_error, redact_headers, redact_text

logger = logging.getLogger(__name__)

# Headers de resposta que vale devolver ao modelo (o resto é ruído/risco).
_SAFE_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "date", "etag", "last-modified",
    "location", "retry-after", "x-request-id", "x-ratelimit-remaining",
})


class HTTPExecutor:
    """Executa uma ServiceRequestIR autorizada e devolve uma ServiceResponse."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # ``transport`` é injetado só em teste (httpx.MockTransport) — em produção
        # fica None e o AsyncClient usa a rede real.
        self._transport = transport

    async def execute(
        self,
        ir: ServiceRequestIR,
        conn: ConnectionConfig,
        auth: ResolvedAuth,
        *,
        request_id: str,
    ) -> ServiceResponse:
        base = conn.base_url.rstrip("/")
        parsed_base = urllib.parse.urlsplit(base)
        if parsed_base.scheme not in ("http", "https"):
            raise GatewayError("bad_base_url", f"base_url da conexão '{conn.id}' não é http(s).")
        if parsed_base.scheme == "http" and not conn.allow_http:
            raise GatewayError("insecure_base_url", f"conexão '{conn.id}' usa http sem allow_http.")

        # URL montada por nós — path já normalizado, host imutável.
        full_url = base + ir.path

        headers = dict(conn.default_headers)
        headers.update(ir.headers)          # complementares do LLM (já validados)
        headers.update(auth.headers)        # auth por último — vence e nunca é logada

        params = dict(ir.query)
        params.update(auth.params)

        timeout = min(float(ir.timeout_s or conn.timeout_s), float(conn.timeout_s) * 2)
        can_retry = ir.method.upper() in IDEMPOTENT_METHODS
        max_attempts = (conn.retry_max + 1) if can_retry else 1

        body_kwargs = self._body_kwargs(ir)

        start = time.monotonic()
        last_error: Exception | None = None
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout),
            "verify": conn.verify_tls,
            "follow_redirects": False,  # nunca automático — validamos host por hop abaixo
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            for attempt in range(max_attempts):
                try:
                    response = await self._send_with_redirects(
                        client, ir.method, full_url, headers, params, body_kwargs, conn,
                    )
                    if response.status_code < 500:
                        return self._build_response(response, conn, request_id, start)
                    last_error = Exception(f"{response.status_code}")
                except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                    last_error = exc
                except GatewayError:
                    raise
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)

        raise GatewayError(
            "upstream_unreachable",
            f"serviço '{conn.id}' não respondeu após {max_attempts} tentativa(s): {redact_error(last_error) if last_error else 'erro desconhecido'}",
        )

    async def _send_with_redirects(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
        body_kwargs: dict[str, Any],
        conn: ConnectionConfig,
    ) -> httpx.Response:
        """Envia a request; se ``follow_redirects`` estiver ligado na conexão,
        segue manualmente revalidando que cada hop fica no MESMO host (SSRF guard
        por hop, padrão de modules/web_search/fetch.py)."""
        origin_host = urllib.parse.urlsplit(url).netloc
        current_url = url
        for _ in range(4):
            response = await client.request(
                method, current_url, headers=headers, params=params, **body_kwargs
            )
            if not (300 <= response.status_code < 400) or not conn.follow_redirects:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            next_url = urllib.parse.urljoin(current_url, location)
            if urllib.parse.urlsplit(next_url).netloc != origin_host:
                raise GatewayError(
                    "cross_host_redirect",
                    f"redirect para host diferente bloqueado ({origin_host} → outro).",
                )
            current_url = next_url
            params = {}  # a Location já carrega a query do destino
        return response

    @staticmethod
    def _body_kwargs(ir: ServiceRequestIR) -> dict[str, Any]:
        if ir.body is None or ir.method.upper() in ("GET", "HEAD"):
            return {}
        if isinstance(ir.body, (dict, list)):
            return {"json": ir.body}
        if isinstance(ir.body, (bytes, bytearray)):
            return {"content": bytes(ir.body)}
        return {"content": str(ir.body).encode("utf-8")}

    @staticmethod
    def _build_response(
        response: httpx.Response, conn: ConnectionConfig, request_id: str, start: float
    ) -> ServiceResponse:
        raw_text = response.text or ""
        redacted = redact_text(raw_text)
        body, truncated = cap_body(redacted, conn.max_response_bytes)
        safe_headers = {
            k: v for k, v in response.headers.items() if k.lower() in _SAFE_RESPONSE_HEADERS
        }
        return ServiceResponse(
            status=response.status_code,
            headers=redact_headers(safe_headers),
            body=body,
            truncated=truncated,
            duration_ms=int((time.monotonic() - start) * 1000),
            request_id=request_id,
        )
