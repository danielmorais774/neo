"""Scrub central de segredos (docs/service-gateway-dsl-plan.md §1.2, §2.2).

Três pontos de aplicação: resposta antes de entrar no ToolResult, mensagens de
exceção, e registros de auditoria. Esconder o *nosso* Bearer não basta — a
resposta de um serviço pode ecoar tokens (config do n8n, dumps), e o ``repr``
de erros do httpx pode embutir headers. Por isso o scrub é por PADRÃO, não por
lista de valores conhecidos.

Também mora aqui o cap de tamanho da resposta: corpo grande estoura o contexto
do LLM tanto quanto um segredo o polui.
"""

from __future__ import annotations

import re

# Padrões de segredo conhecidos. Conservador de propósito: melhor um falso
# positivo (redacta um valor inocente) do que vazar um token no contexto/log.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Authorization: Bearer <token>  /  "authorization": "Bearer ..."
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]{8,}=*"),
    re.compile(r"(?i)(basic\s+)[A-Za-z0-9+/]{8,}=*"),
    # Header de auth ecoado num JSON/texto: "authorization": "...", "x-api-key": "..."
    re.compile(r'(?i)("?(?:authorization|x-api-key|api[_-]?key|cookie|set-cookie|x-auth-token)"?\s*[:=]\s*"?)[^"\s,}]{6,}'),
    # Tokens de provedores comuns.
    re.compile(r"gh[posru]_[A-Za-z0-9]{20,}"),                 # GitHub
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),               # Slack
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                        # OpenAI-like
    re.compile(r"AKIA[0-9A-Z]{16}"),                           # AWS access key id
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),  # JWT
    # Chaves privadas PEM (bloco inteiro).
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]

_PLACEHOLDER = "«redacted»"


def redact_text(value: str) -> str:
    """Substitui segredos reconhecidos por um placeholder. Idempotente."""
    if not value:
        return value
    out = value
    for pattern in _SECRET_PATTERNS:
        # Preserva o prefixo do grupo 1 quando existe (ex.: "Bearer ", '"api_key": '),
        # trocando só a parte sensível — mantém o texto legível para o modelo.
        if pattern.groups:
            out = pattern.sub(lambda m: (m.group(1) or "") + _PLACEHOLDER, out)
        else:
            out = pattern.sub(_PLACEHOLDER, out)
    return out


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Zera valores de headers sensíveis inteiros (nunca serializa auth)."""
    sensitive = {
        "authorization", "cookie", "set-cookie", "proxy-authorization",
        "x-api-key", "x-auth-token", "api-key",
    }
    out: dict[str, str] = {}
    for key, val in headers.items():
        if key.lower() in sensitive:
            out[key] = _PLACEHOLDER
        else:
            out[key] = redact_text(str(val))
    return out


def cap_body(body: str, max_bytes: int) -> tuple[str, bool]:
    """Trunca por bytes (UTF-8), devolvendo (texto, truncated).

    Corta na fronteira de bytes e reconstrói o texto ignorando um caractere
    partido no fim — evita ``UnicodeDecodeError`` e mede o limite real da rede.
    """
    encoded = body.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return body, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped + "\n… [resposta truncada pelo gateway]", True


def redact_error(exc: BaseException) -> str:
    """Mensagem de erro segura para logar/devolver — sem headers no ``repr``."""
    return redact_text(f"{type(exc).__name__}: {exc}")
