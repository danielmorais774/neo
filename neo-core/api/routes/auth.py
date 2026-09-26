"""Login de usuário (docs/api-auth-plan.md §5).

**Por que existe.** A credencial que o núcleo tinha era o token de aparelho
pareado — ótimo para desktop/notch/celular, inútil para o painel, que roda num
navegador e não pareia. O painel usava um JWT **estático**, com
``sub="neo-panel"``, sem prazo, commitado no fonte: autenticava *o painel*, não
uma pessoa. Por isso ele nunca pôde aprovar liberação de dado
(``release-request-plan.md`` §11) — não há a quem atribuir a decisão.

**O que este login NÃO é.** Não é multiusuário: continua um dono. Ele existe para
que "só eu, com o meu usuário" seja **verificável**, e para o painel poder decidir
coisas em seu nome.

Estas rotas ficam **fora** do portão (quem faz login ainda não tem credencial),
mas o ``/me`` usa ``require_owner`` — é o teste de fumaça do painel.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.auth import jwt as jwt_mod
from api.auth import passwords, throttle
from api.auth.principal import Principal, require_owner
from db import AsyncPostgresClient
from shared.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_db: AsyncPostgresClient | None = None


def set_db(db: AsyncPostgresClient) -> None:
    global _db
    _db = db


def _require_db() -> AsyncPostgresClient:
    if _db is None:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    return _db


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


def _hash_refresh(token: str) -> str:
    """sha256 do refresh. Sem salt: são 32 bytes aleatórios (nada a adivinhar) e
    o hash precisa ser determinístico para servir de índice — mesma decisão do
    ``hash_device_token``."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _origem(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:128]


async def _emitir(user, request: Request) -> dict[str, Any]:
    settings = get_settings()
    db = _require_db()

    access, payload = jwt_mod.issue(
        str(user.id),
        "access",
        settings.access_token_ttl_s,
        username=user.username or "",
    )
    refresh = secrets.token_urlsafe(32)
    await db.create_refresh_token(
        user_id=user.id,
        token_hash=_hash_refresh(refresh),
        expires_at=datetime.utcnow() + timedelta(seconds=settings.refresh_token_ttl_s),
        origem=_origem(request),
    )
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_s,
        "expires_at": payload["exp"],
        "refresh_token": refresh,
        "user": {
            "id": str(user.id),
            "username": user.username,
            "display_name": user.display_name,
        },
    }


@router.get("/mode")
async def mode() -> dict[str, Any]:
    """Em que degrau o portão está — rota pública, de propósito.

    O painel precisa saber, ANTES de ter credencial, se login é obrigatório ou se
    o token legado ainda serve. Sem isto, a única forma de descobrir seria tomar
    401 em alguma tela e adivinhar o motivo.

    Não revela nada: o modo é uma política, não um segredo, e quem já está do lado
    de fora descobre o mesmo tentando uma rota.
    """
    from api.auth.principal import auth_mode

    modo = auth_mode()
    return {
        "mode": modo,
        "login_required": modo in ("enforce", "strict"),
        "legacy_panel_token_accepted": modo != "strict",
    }


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> dict[str, Any]:
    """Troca usuário+senha por um par access/refresh.

    **Mesma resposta e mesmo custo** para usuário inexistente e senha errada: a
    verificação sempre roda um PBKDF2 completo (ver ``passwords.verify_password``),
    senão a latência diferencia os dois casos e vira enumeração de conta.
    """
    db = _require_db()
    ip = _ip(request)
    usuario = body.username.strip()

    espera = throttle.bloqueado(usuario, ip)
    if espera:
        raise HTTPException(
            status_code=429,
            detail=f"tentativas demais. Tente de novo em {espera}s.",
            headers={"Retry-After": str(espera)},
        )

    user = await db.get_user_by_username(usuario)
    stored = user.password_hash if user else None

    if not passwords.verify_password(body.password, stored):
        throttle.registrar_falha(usuario, ip)
        logger.warning("[auth] login recusado para %r de %s", usuario, ip)
        raise HTTPException(status_code=401, detail="usuário ou senha inválidos")

    throttle.limpar(usuario, ip)

    # Migração do hash legado (sha256 com salt fixo de ``db/seeds.py``): só aqui
    # a senha em claro existe, então é aqui ou nunca.
    if passwords.needs_rehash(stored):
        try:
            await db.set_user_password_hash(user.id, passwords.hash_password(body.password))
            logger.info("[auth] hash de senha migrado para pbkdf2 (user=%s)", user.id)
        except Exception as exc:  # noqa: BLE001 - não impedir o login por isso
            logger.warning("[auth] falhei em migrar o hash: %s", exc)

    logger.info("[auth] login ok user=%s de %s", user.id, ip)
    return await _emitir(user, request)


@router.post("/refresh")
async def refresh(body: RefreshRequest, request: Request) -> dict[str, Any]:
    """Renova o access token, **rotacionando** o refresh.

    Rotação: o refresh apresentado morre e outro nasce, na mesma transação. Um
    refresh já usado que reaparece é sinal de token copiado — e aí a resposta é
    401, não um par novo.
    """
    db = _require_db()
    settings = get_settings()

    antigo_hash = _hash_refresh(body.refresh_token)
    novo = secrets.token_urlsafe(32)
    row = await db.rotate_refresh_token(
        antigo_hash=antigo_hash,
        novo_hash=_hash_refresh(novo),
        expires_at=datetime.utcnow() + timedelta(seconds=settings.refresh_token_ttl_s),
        origem=_origem(request),
    )
    if row is None:
        raise HTTPException(status_code=401, detail="refresh token inválido ou expirado")

    user = await db.get_user_by_id(row.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="usuário do token não existe mais")

    access, payload = jwt_mod.issue(
        str(user.id), "access", settings.access_token_ttl_s, username=user.username or ""
    )
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_s,
        "expires_at": payload["exp"],
        "refresh_token": novo,
        "user": {
            "id": str(user.id),
            "username": user.username,
            "display_name": user.display_name,
        },
    }


@router.post("/logout")
async def logout(body: RefreshRequest) -> dict[str, Any]:
    """Revoga UM refresh (esta sessão). Idempotente."""
    db = _require_db()
    revogado = await db.revoke_refresh_token(_hash_refresh(body.refresh_token))
    return {"ok": True, "revogado": revogado}


@router.post("/logout-all")
async def logout_all(principal: Principal = Depends(require_owner)) -> dict[str, Any]:
    """Sai de todos os aparelhos — exige credencial, não o refresh."""
    db = _require_db()
    if principal.user_id is None:
        raise HTTPException(status_code=403, detail="credencial sem dono")
    quantos = await db.revoke_all_refresh_tokens(principal.user_id)
    return {"ok": True, "revogados": quantos}


@router.get("/me")
async def me(principal: Principal = Depends(require_owner)) -> dict[str, Any]:
    """Quem sou eu, segundo a credencial apresentada.

    **401 sem dono em QUALQUER modo**, inclusive em ``shadow``. O portão em sombra
    deixa passar de propósito — é o que mantém a virada de chave reversível —, mas
    aqui a identidade *é* a resposta: devolver 200 com ``user: null`` para um token
    inventado transforma a rota de "quem sou eu" num "sim". Medido contra produção
    em 2026-09-26: token `nao.e.token` respondia 200. Nenhum cliente antigo chama
    esta rota (ela nasceu agora), então endurecer não quebra ninguém.

    Devolve o modo junto: é assim que o painel explica a situação na tela.
    """
    from api.auth.principal import auth_mode

    if not principal.tem_dono:
        raise HTTPException(
            status_code=401,
            detail=f"credencial não identifica um usuário ({principal.erro or 'ausente'})",
            headers={"WWW-Authenticate": "Bearer"},
        )

    db = _db
    user: Any = None
    if db is not None and principal.user_id is not None:
        user = await db.get_user_by_id(principal.user_id)

    return {
        "auth_mode": auth_mode(),
        "principal": {
            "kind": principal.kind,
            "label": principal.label,
            "device_id": principal.device_id,
        },
        "user": (
            {
                "id": str(user.id),
                "username": user.username,
                "display_name": user.display_name,
                "email": user.email,
            }
            if user is not None
            else None
        ),
    }
