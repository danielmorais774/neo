"""Guardas de compatibilidade das rotas do painel.

**Onde mora a autenticação agora:** ``api/auth/`` (``docs/api-auth-plan.md``).
Este arquivo continua existindo porque ~20 routers usam ``require_panel_jwt``, e
trocar todos de uma vez seria uma mudança grande sem ganho: o que ele guarda são
telas de administração. O que mudou é que ele agora **delega** a verificação de
assinatura para ``api.auth.jwt``, em vez de reimplementá-la.

O JWT que esta função aceita é **estático e compartilhado** (``sub="neo-panel"``):
autentica *o painel*, não uma pessoa. Rota que toca dado pessoal usa
``api.auth.require_owner``, não isto.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.auth import jwt as jwt_mod
from shared.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def verify_panel_token(token: str) -> dict:
    """Valida um token de administração: o estático do painel **ou** um de usuário.

    Aceitar o token de usuário aqui é o que permite o painel ganhar login sem
    tocar em ~20 routers: assim que ele passa a mandar o access token do login,
    toda tela de administração continua funcionando, e o ``strict`` pode matar o
    token estático sem derrubar nada. Ver docs/api-auth-plan.md §4 e F5.
    """
    from api.auth.principal import MODE_STRICT, auth_mode

    settings = get_settings()

    try:
        payload = jwt_mod.verify(token)
    except jwt_mod.TokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    if payload.get("sub") == settings.panel_jwt_subject:
        if auth_mode() == MODE_STRICT:
            raise HTTPException(
                status_code=403,
                detail="o JWT estático do painel não vale mais (modo strict). "
                "Faça login em POST /api/v1/auth/login.",
            )
        return payload

    # Token de usuário (``sub`` = uuid): serve como credencial de administração.
    if payload.get("typ") == "access":
        try:
            UUID(str(payload.get("sub")))
        except (ValueError, TypeError):
            pass
        else:
            return payload

    raise HTTPException(
        status_code=403,
        detail="este token não autoriza rotas de administração",
    )


def require_panel_jwt(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_panel_token: str | None = Header(default=None, alias="X-Panel-Token"),
) -> dict:
    token = credentials.credentials if credentials else x_panel_token
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return verify_panel_token(token)
