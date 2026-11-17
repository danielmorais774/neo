"""URL assinada de vida curta — o caso do ``<a href download>``.

O painel baixa arquivo por âncora (``ChatMessageBubble.tsx``), e navegação de
âncora **não passa pelo wrapper de ``window.fetch``**: nenhum header vai. Fechar
``/files/{id}/download`` só com header quebraria o download.

Então quem já está autenticado pede a URL, e a rota de download aceita a
assinatura no lugar do header. Mesmo molde do ``state`` do Spotify
(``api/routes/spotify.py``): HMAC do segredo do serviço sobre um payload curto,
com prazo. O Rust (``reqwest``) manda header e não precisa disto.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from uuid import UUID

from shared.config import get_settings


class SignatureError(Exception):
    """Assinatura ausente, malformada, de outro recurso ou vencida."""


def _mac(recurso: str, user_id: str, exp: int) -> str:
    secret = get_settings().panel_jwt_secret.encode("utf-8")
    msg = f"{recurso}|{user_id}|{exp}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def sign(recurso: str, user_id: UUID | str, *, ttl_s: int | None = None) -> dict[str, str]:
    """Parâmetros de query que autorizam UM recurso, por pouco tempo."""
    ttl = ttl_s if ttl_s is not None else get_settings().signed_url_ttl_s
    exp = int(time.time()) + int(ttl)
    uid = str(user_id)
    return {"exp": str(exp), "uid": uid, "sig": _mac(recurso, uid, exp)}


def verify(recurso: str, *, exp: str | None, uid: str | None, sig: str | None) -> UUID:
    """Confere a assinatura e devolve o dono. Levanta ``SignatureError``.

    A assinatura cobre o RECURSO: um link de um arquivo não serve para outro —
    sem isso, o link do arquivo público viraria chave de todo o catálogo.
    """
    if not (exp and uid and sig):
        raise SignatureError("assinatura ausente")
    try:
        exp_i = int(exp)
    except (TypeError, ValueError) as e:
        raise SignatureError("exp inválido") from e
    if exp_i < int(time.time()):
        raise SignatureError("link expirado")
    if not hmac.compare_digest(_mac(recurso, uid, exp_i), sig):
        raise SignatureError("assinatura inválida")
    try:
        return UUID(uid)
    except ValueError as e:
        raise SignatureError("uid inválido") from e
