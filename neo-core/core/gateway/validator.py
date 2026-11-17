"""Validação estrutural da requisição (docs/service-gateway-dsl-plan.md §1.4).

Sem rede, sem segredo, sem política — só forma. Roda ANTES de tudo. A parte
mais sensível é ``normalize_path``: é ela que garante que o ``path`` não escapa
do host fixo da conexão (traversal, path absoluto, protocol-relative, bypass por
encoding). O match de política acontece SOBRE a forma normalizada e a execução
usa exatamente essa forma — sem divergência entre o que foi autorizado e o que
foi enviado.
"""

from __future__ import annotations

import urllib.parse

from core.gateway.ir import ConnectionConfig, GatewayError, ServiceRequestIR

# Headers que o LLM NUNCA pode setar: autenticação (é papel do broker), controle
# de transporte (corpo/host) e override de método. Denylist dura → erro, não
# silêncio. Case-insensitive.
_RESERVED_HEADERS: frozenset[str] = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "host", "content-length", "transfer-encoding", "connection",
    "x-http-method-override", "x-method-override",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-real-ip", "forwarded",
})
# Prefixo proibido inteiro (qualquer Proxy-*).
_RESERVED_HEADER_PREFIXES: tuple[str, ...] = ("proxy-",)

# Query params reservados (método escondido / auth por querystring do LLM).
_RESERVED_QUERY_PARAMS: frozenset[str] = frozenset({"_method"})

_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {"\x7f"}


def _decode_repeatedly(value: str, rounds: int = 3) -> str:
    """Percent-decode até estabilizar (fecha double/triple encoding p/ o check)."""
    current = value
    for _ in range(rounds):
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            break
        current = decoded
    return current


def normalize_path(raw: str) -> str:
    """Normaliza e valida o path. Levanta GatewayError em qualquer suspeita.

    Regras: precisa começar com ``/``; sem ``//`` inicial (protocol-relative);
    sem esquema/host embutido; sem ``?``/``#`` (query vem em campo próprio); sem
    caracteres de controle ou ``\\``; nenhum segmento ``..`` (nem via encoding);
    segmentos ``.`` colapsados.
    """
    if not isinstance(raw, str) or not raw:
        raise GatewayError("invalid_path", "path vazio ou inválido.")
    if any(ch in _CONTROL_CHARS for ch in raw) or "\\" in raw:
        raise GatewayError("invalid_path", "path contém caractere de controle ou barra invertida.")
    if "#" in raw:
        raise GatewayError("invalid_path", "fragmento (#) não é permitido no path.")
    if "?" in raw:
        raise GatewayError("invalid_path", "querystring deve ir no campo 'query', não no path.")
    # Esquema absoluto embutido (http://, https://, //host) → tentativa de trocar host.
    lowered = _decode_repeatedly(raw).lower()
    if "://" in lowered or lowered.startswith("//"):
        raise GatewayError("invalid_path", "path não pode conter URL absoluta nem host.")
    if not raw.startswith("/"):
        raise GatewayError("invalid_path", "path precisa começar com '/'.")
    if raw.startswith("//"):
        raise GatewayError("invalid_path", "path não pode começar com '//' (protocol-relative).")

    # Detecta traversal mesmo por encoding: decodifica e olha os segmentos.
    decoded = _decode_repeatedly(raw)
    if ".." in decoded.split("/"):
        raise GatewayError("invalid_path", "path não pode conter segmento '..' (traversal).")

    # Colapsa segmentos '.' e barras duplicadas internas, preservando trailing slash.
    segments = [seg for seg in raw.split("/") if seg not in ("", ".")]
    normalized = "/" + "/".join(segments)
    if raw.endswith("/") and not normalized.endswith("/") and normalized != "/":
        normalized += "/"
    return normalized


def validate_request(ir: ServiceRequestIR, conn: ConnectionConfig) -> ServiceRequestIR:
    """Valida a IR contra a conexão e devolve uma cópia com o path normalizado.

    Não toca rede nem broker. Levanta GatewayError na primeira violação.
    """
    # Headers reservados vindos do LLM.
    for key in ir.headers:
        low = key.lower()
        if low in _RESERVED_HEADERS or low.startswith(_RESERVED_HEADER_PREFIXES):
            raise GatewayError(
                "reserved_header",
                f"header '{key}' é reservado e não pode ser enviado pelo modelo.",
            )
        if any(ch in _CONTROL_CHARS for ch in key) or any(ch in _CONTROL_CHARS for ch in str(ir.headers[key])):
            raise GatewayError("invalid_header", f"header '{key}' contém caractere de controle.")

    # Query params reservados.
    for key in ir.query:
        if key.lower() in _RESERVED_QUERY_PARAMS:
            raise GatewayError("reserved_query", f"query param '{key}' é reservado.")

    # Tamanho do body (mede a serialização real que irá pra rede).
    if ir.body is not None:
        if isinstance(ir.body, (str, bytes)):
            size = len(ir.body.encode("utf-8") if isinstance(ir.body, str) else ir.body)
        else:
            import json

            size = len(json.dumps(ir.body, ensure_ascii=False).encode("utf-8"))
        if size > conn.max_body_bytes:
            raise GatewayError(
                "body_too_large",
                f"body de {size} bytes excede o limite da conexão ({conn.max_body_bytes}).",
            )

    normalized_path = normalize_path(ir.path)
    return ir.model_copy(update={"path": normalized_path})
