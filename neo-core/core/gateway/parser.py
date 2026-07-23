"""Parser do dialeto ``.http`` (F3 — §6 do plano).

Formato de **autoria**, não o wire format do LLM (§1.1): exemplos executáveis em
``references/*.http`` de skills, requests salvos, fixtures de teste. O caminho
quente (LLM → execução) continua sendo a tool JSON, sem parser.

O dialeto é uma versão RESTRITA do REST Client/HTTPYac. As diferenças são
deliberadas e todas apertam a segurança:

- ``@service <slug>`` é obrigatório (a conexão não é adivinhada);
- a request line aceita **só path relativo** — URL absoluta é erro (no REST
  Client original a URL é livre: é exatamente a herança que recusamos, porque
  deixaria o host vir do arquivo em vez da conexão);
- sem ``{{variáveis}}`` e sem interpolação de env (é onde ferramentas do gênero
  vazam segredo);
- sem scripts embutidos (HTTPYac permite JS);
- headers reservados são recusados aqui também — o validator repete a checagem,
  mas errar cedo dá mensagem melhor.

Saída: a MESMA ``ServiceRequestIR`` da tool. Um formato, uma verdade.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.gateway.ir import GatewayError, ServiceRequestIR
from core.gateway.validator import _RESERVED_HEADERS, _RESERVED_HEADER_PREFIXES

_REQUEST_LINE = re.compile(
    r"^(?P<method>GET|HEAD|POST|PUT|PATCH|DELETE)\s+(?P<target>\S+)(?:\s+HTTP/[\d.]+)?\s*$",
    re.IGNORECASE,
)
_SERVICE_LINE = re.compile(r"^@service\s+(?P<slug>[a-z0-9][a-z0-9-]{1,39})\s*$", re.IGNORECASE)
_HEADER_LINE = re.compile(r"^(?P<name>[A-Za-z0-9-]+)\s*:\s*(?P<value>.*)$")
_SEPARATOR = re.compile(r"^###")


def split_requests(text: str) -> list[str]:
    """Divide um arquivo em blocos por ``###`` (execução é sempre 1 por vez)."""
    blocks: list[str] = []
    current: list[str] = []
    for line in (text or "").splitlines():
        if _SEPARATOR.match(line):
            if any(l.strip() for l in current):
                blocks.append("\n".join(current))
            current = []
            continue
        current.append(line)
    if any(l.strip() for l in current):
        blocks.append("\n".join(current))
    return blocks


def parse_http(text: str) -> ServiceRequestIR:
    """Converte UM bloco ``.http`` na IR. Levanta GatewayError com linha/motivo."""
    lines = (text or "").splitlines()
    service: str | None = None
    method: str | None = None
    target: str | None = None
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        stripped = line.strip()

        if in_body:
            body_lines.append(line)
            continue

        # Linha em branco DEPOIS da request line abre o corpo (fim dos headers).
        if not stripped:
            if method is not None:
                in_body = True
            continue

        if stripped.startswith("#") or stripped.startswith("//"):
            continue

        if stripped.startswith("@"):
            match = _SERVICE_LINE.match(stripped)
            if match:
                service = match.group("slug").lower()
                continue
            if stripped.lower().startswith("@service"):
                raise GatewayError(
                    "bad_service_line",
                    f"'@service' inválido: {stripped!r}. Use: @service <slug> "
                    "(minúsculas, números e hífen).",
                )
            raise GatewayError(
                "unsupported_directive",
                f"diretiva {stripped.split()[0]!r} não é suportada — este dialeto não "
                "aceita variáveis nem scripts (§6 do plano).",
            )

        if method is None:
            match = _REQUEST_LINE.match(stripped)
            if not match:
                raise GatewayError(
                    "bad_request_line",
                    f"esperava uma request line (ex.: 'POST /api/x'), veio {stripped!r}.",
                )
            method = match.group("method").upper()
            target = match.group("target")
            continue

        # Depois da request line: headers até a linha em branco.
        match = _HEADER_LINE.match(stripped)
        if match:
            name = match.group("name")
            low = name.lower()
            if low in _RESERVED_HEADERS or low.startswith(_RESERVED_HEADER_PREFIXES):
                raise GatewayError(
                    "reserved_header",
                    f"header '{name}' é reservado: autenticação e transporte são do "
                    "gateway, não do arquivo.",
                )
            headers[name] = match.group("value").strip()
            continue

        raise GatewayError("bad_header", f"linha de header inválida: {stripped!r}")

    if service is None:
        raise GatewayError(
            "missing_service",
            "falta '@service <slug>' — o dialeto exige dizer QUAL conexão usar "
            "(o host nunca vem do arquivo).",
        )
    if method is None or not target:
        raise GatewayError("missing_request_line", "falta a request line (ex.: 'GET /api/states').")

    path, query = _split_target(target)
    body = _parse_body("\n".join(body_lines).strip(), headers)

    return ServiceRequestIR(
        service=service, method=method, path=path, query=query, headers=headers, body=body,
    )



def _split_target(target: str) -> tuple[str, dict[str, Any]]:
    """Separa path e query. Recusa URL absoluta (a herança do REST Client que não
    aceitamos: o host é da CONEXÃO)."""
    lowered = target.lower()
    if "://" in lowered or lowered.startswith("//"):
        raise GatewayError(
            "absolute_url",
            f"URL absoluta não é permitida ({target!r}): informe só o path relativo — "
            "o host vem da conexão '@service'.",
        )
    if not target.startswith("/"):
        raise GatewayError("relative_path_required", f"o path precisa começar com '/': {target!r}")

    path, sep, raw_query = target.partition("?")
    query: dict[str, Any] = {}
    if sep and raw_query:
        from urllib.parse import parse_qs

        for key, values in parse_qs(raw_query, keep_blank_values=True).items():
            query[key] = values[0] if len(values) == 1 else values
    return path, query


def _parse_body(raw: str, headers: dict[str, str]) -> Any:
    if not raw:
        return None
    content_type = ""
    for name, value in headers.items():
        if name.lower() == "content-type":
            content_type = value.lower()
            break
    # JSON explícito, ou "parece JSON" (o caso comum dos exemplos de doc).
    if "json" in content_type or raw.lstrip().startswith(("{", "[")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            if "json" in content_type:
                raise GatewayError("bad_json_body", f"corpo não é JSON válido: {exc}") from exc
    return raw
