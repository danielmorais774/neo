"""Assinatura e verificação de JWT HS256 — sem dependência nova.

**Por que este arquivo existe separado de ``api/security.py``.** A função antiga
juntava três coisas numa só: verificar a assinatura, conferir o `sub` contra
``panel_jwt_subject`` e devolver o payload. Com isso, um token de USUÁRIO
(``sub=<uuid>``) falhava na verificação de assinatura — o "Invalid token subject"
saía antes de qualquer decisão de identidade (gotcha §7.1 do plano). Aqui a
assinatura e o prazo são conferidos; QUEM é o dono fica para ``principal.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from shared.config import get_settings


class TokenError(Exception):
    """Token malformado, com assinatura inválida ou vencido."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _secret() -> bytes:
    return get_settings().panel_jwt_secret.encode("utf-8")


def sign(payload: dict[str, Any]) -> str:
    """Assina um payload em JWT HS256 compacto."""
    header_b64 = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(_secret(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def issue(subject: str, typ: str, ttl_s: int, **extra: Any) -> tuple[str, dict[str, Any]]:
    """Emite um token com prazo. Devolve ``(token, payload)``.

    ``jti`` sempre presente: é o que permite revogar um token específico depois,
    sem trocar o segredo (que invalidaria todos).
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": typ,
        "iat": now,
        "exp": now + ttl_s,
        "jti": uuid.uuid4().hex,
        **extra,
    }
    return sign(payload), payload


def verify(token: str) -> dict[str, Any]:
    """Confere assinatura e ``exp``. Devolve o payload cru.

    NÃO decide identidade — quem faz isso é ``principal.py``. Um token do painel
    (``sub="neo-panel"``) e um de usuário (``sub=<uuid>``) passam os dois por
    aqui; o que muda é o que cada um AUTORIZA.

    Token sem ``exp`` é aceito (o JWT estático do painel não tem) — a recusa
    dele é decisão de modo, não de formato, senão o painel morreria antes de
    existir a tela de login.
    """
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:
        raise TokenError("formato de token inválido") from exc

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("codificação de token inválida") from exc

    if not isinstance(payload, dict):
        raise TokenError("payload de token inválido")

    if header.get("alg") != "HS256":
        raise TokenError("algoritmo de token não suportado")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(_secret(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise TokenError("assinatura de token inválida")

    exp = payload.get("exp")
    if exp is not None:
        try:
            if int(exp) < int(time.time()):
                raise TokenError("token expirado")
        except (TypeError, ValueError) as exc:
            raise TokenError("exp inválido") from exc

    return payload
