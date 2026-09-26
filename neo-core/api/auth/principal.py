"""Quem é o dono deste pedido — ponto único de decisão (docs/api-auth-plan.md §3).

Duas credenciais, um princípio: o **token de aparelho pareado** (desktop, notch,
celular — já existia, resolvido por ``_device_from_token``) e o **login de
usuário** (painel, ``api/routes/auth.py``) resolvem para o MESMO
``Principal(user_id)``. Toda rota autentica pela mesma função, e nenhuma rota
inventa a sua.

**O furo que isto fecha não é só "porta aberta".** Era a amarração: o ``user_id``
chegava no CORPO do pedido, e ``db/async_client.py:create_conversation_session``
CRIAVA o usuário se não existisse. Declarar um UUID novo inventava conta;
declarar o dele escrevia na memória dele. Por isso ``owner_of`` existe: a
credencial manda, o corpo é advisório.

**Degraus (``NEO_API_AUTH_MODE``)** — a ordem é a lição do portão de liberação e
do ``NEO_DEVICE_AUTH_REQUIRED``: medir antes de barrar. Local é produção, o core
roda ``--reload``, e virar a chave antes dos clientes carregarem a credencial
derruba o desktop no meio de um turno.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request

from shared.config import get_settings

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODE_STRICT = "strict"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE, MODE_STRICT)

#: Header do token de aparelho. Mesmo nome da metadata do gRPC
#: (``x-device-token`` em ``ai_agent.rs``) — uma credencial, um nome.
DEVICE_TOKEN_HEADER = "X-Device-Token"

KIND_DEVICE = "device"
KIND_USER = "user"
KIND_PANEL_LEGACY = "panel_legacy"
KIND_ANON = "anonimo"


@dataclass(frozen=True)
class Principal:
    """Identidade resolvida de um pedido.

    ``user_id`` é ``None`` em dois casos, e a diferença importa: ``panel_legacy``
    (o JWT estático do painel autentica *o painel*, não uma pessoa) e
    ``anonimo`` (nenhuma credencial — só existe em ``off``/``shadow``).
    """

    kind: Literal["device", "user", "panel_legacy", "anonimo"]
    user_id: UUID | None = None
    device_id: str | None = None
    label: str = "anônimo"
    #: Preenchido quando a credencial apresentada era inválida (token de device
    #: desconhecido, JWT com assinatura ruim). Em ``shadow`` isso passa, mas
    #: aparece no log — é o sinal de cliente desatualizado, não de ataque.
    erro: str | None = None

    @property
    def autenticado(self) -> bool:
        return self.kind in (KIND_DEVICE, KIND_USER, KIND_PANEL_LEGACY)

    @property
    def tem_dono(self) -> bool:
        return self.user_id is not None


ANONIMO = Principal(kind=KIND_ANON)


def auth_mode() -> str:
    mode = (getattr(get_settings(), "neo_api_auth_mode", MODE_SHADOW) or MODE_SHADOW).strip().lower()
    return mode if mode in MODES else MODE_SHADOW


def _credencial_do_pedido(request: Request) -> tuple[str | None, str | None]:
    """``(token_de_aparelho, jwt)`` — o que o pedido apresentou, sem julgar."""
    device_token = request.headers.get(DEVICE_TOKEN_HEADER)

    jwt: str | None = None
    authorization = request.headers.get("Authorization")
    if authorization and authorization.lower().startswith("bearer "):
        jwt = authorization[7:].strip() or None
    if jwt is None:
        # ``require_panel_jwt`` sempre aceitou este header; se o Principal não
        # aceitasse também, alguma tela do painel morreria sem aviso (§7.2).
        jwt = request.headers.get("X-Panel-Token")

    # Query param: o caso do ``<a href download>`` (âncora não manda header) e
    # dos WS, onde o navegador também não deixa. Mesmo nome que ``/devices/mine``
    # e as rotas de liberação já usam.
    if not device_token:
        qs_token = request.query_params.get("token")
        if qs_token:
            # Um JWT tem três partes separadas por ponto; token de aparelho é
            # hex opaco. Não adivinha: tenta como aparelho e, se tiver cara de
            # JWT, entra pelo outro caminho.
            if qs_token.count(".") == 2:
                jwt = jwt or qs_token
            else:
                device_token = qs_token

    return device_token, jwt


async def _por_token_de_aparelho(token: str) -> Principal:
    from api.routes import devices as devices_routes

    try:
        device = await devices_routes._device_from_token(token)
    except HTTPException:
        return Principal(kind=KIND_ANON, erro="token de aparelho inválido")
    nome = device.display_name or device.device_id
    return Principal(
        kind=KIND_DEVICE,
        user_id=device.user_id,
        device_id=device.device_id,
        label=f"device:{nome}",
    )


def _por_jwt(token: str) -> Principal:
    from api.auth import jwt as jwt_mod

    try:
        payload = jwt_mod.verify(token)
    except jwt_mod.TokenError as exc:
        return Principal(kind=KIND_ANON, erro=f"jwt inválido ({exc})")

    sub = str(payload.get("sub") or "")
    settings = get_settings()

    if sub == settings.panel_jwt_subject:
        return Principal(kind=KIND_PANEL_LEGACY, label="painel(legado)")

    if payload.get("typ") != "access":
        return Principal(kind=KIND_ANON, erro="jwt não é access token")

    try:
        user_id = UUID(sub)
    except (ValueError, AttributeError):
        return Principal(kind=KIND_ANON, erro="sub do jwt não é usuário")

    nome = payload.get("username") or str(user_id)
    return Principal(kind=KIND_USER, user_id=user_id, label=f"user:{nome}")


async def resolve_principal(request: Request) -> Principal:
    """Resolve (e memoiza no pedido) quem está chamando.

    Memoiza porque um mesmo pedido pode passar por mais de uma dependência
    (router + rota), e cada resolução de token de aparelho é uma consulta ao
    banco.
    """
    cached = getattr(request.state, "neo_principal", None)
    if cached is not None:
        return cached

    if auth_mode() == MODE_OFF:
        request.state.neo_principal = ANONIMO
        return ANONIMO

    device_token, jwt = _credencial_do_pedido(request)

    principal = ANONIMO
    if device_token:
        principal = await _por_token_de_aparelho(device_token)
    if not principal.autenticado and jwt:
        pelo_jwt = _por_jwt(jwt)
        # Erro do aparelho só interessa se não houver JWT válido atrás.
        principal = pelo_jwt if pelo_jwt.autenticado else replace(
            pelo_jwt, erro=principal.erro or pelo_jwt.erro
        )

    request.state.neo_principal = principal
    return principal


def _rota(request: Request) -> str:
    return f"{request.method} {request.url.path}"


def _sombra(request: Request, principal: Principal, motivo: str) -> None:
    """Registra o que o ``enforce`` teria barrado. É o entregável da F0."""
    cliente = request.headers.get("user-agent", "?")[:60]
    origem = request.client.host if request.client else "?"
    logger.warning(
        "[auth:sombra] %s passaria a ser barrado: %s | principal=%s erro=%s ip=%s ua=%s",
        _rota(request), motivo, principal.kind, principal.erro, origem, cliente,
    )


def _nao_autenticado(principal: Principal) -> HTTPException:
    detalhe = principal.erro or "credencial ausente"
    return HTTPException(
        status_code=401,
        detail=(
            f"não autenticado ({detalhe}). Apresente o token do aparelho pareado "
            f"em {DEVICE_TOKEN_HEADER} ou um access token de usuário em "
            "Authorization: Bearer."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_principal(request: Request) -> Principal:
    """Qualquer credencial válida. Para leitura que não é de ninguém em especial."""
    principal = await resolve_principal(request)
    mode = auth_mode()

    if mode in (MODE_OFF, MODE_SHADOW):
        if not principal.autenticado:
            _sombra(request, principal, "sem credencial válida")
        return principal

    if not principal.autenticado:
        raise _nao_autenticado(principal)
    if mode == MODE_STRICT and principal.kind == KIND_PANEL_LEGACY:
        raise HTTPException(
            status_code=403,
            detail="o JWT estático do painel não vale mais (modo strict). Faça login.",
        )
    return principal


async def require_owner(request: Request) -> Principal:
    """Principal com dono. É a dependência de tudo que toca dado pessoal.

    Recusa ``panel_legacy`` no ``enforce``: aquele token autentica *o painel* e
    não diz de quem é a conversa. Aceitá-lo aqui seria fingir uma identidade que
    não existe — o mesmo motivo por que ele não aprova liberação.
    """
    principal = await resolve_principal(request)
    mode = auth_mode()

    if mode in (MODE_OFF, MODE_SHADOW):
        if not principal.tem_dono:
            _sombra(
                request, principal,
                "sem dono (o user_id continuaria vindo do corpo do pedido)",
            )
        return principal

    if not principal.autenticado:
        raise _nao_autenticado(principal)
    if not principal.tem_dono:
        raise HTTPException(
            status_code=403,
            detail=(
                "esta credencial não identifica um usuário (JWT estático do "
                "painel). Faça login em POST /api/v1/auth/login."
            ),
        )
    return principal


async def require_admin_read(request: Request) -> Principal:
    """Telas de administração somente-leitura — aceita o painel legado."""
    return await require_principal(request)


def owner_of(
    principal: Principal,
    declarado: UUID | str | None = None,
    *,
    request: Request | None = None,
) -> UUID | None:
    """O ``user_id`` que vale neste pedido.

    Em ``enforce``/``strict``: o da credencial, e declarar outro é **403** (não
    descarte silencioso — o ``DEFAULT_USER_ID`` do painel e o fallback do Rust
    precisam APARECER para serem removidos).
    Em ``off``/``shadow``: o declarado continua valendo, exatamente como antes.
    """
    if isinstance(declarado, str):
        try:
            declarado = UUID(declarado)
        except ValueError:
            raise HTTPException(status_code=400, detail="user_id inválido")

    mode = auth_mode()
    conflito = bool(declarado and principal.user_id and declarado != principal.user_id)

    if mode in (MODE_OFF, MODE_SHADOW):
        if conflito and request is not None:
            _sombra(
                request, principal,
                f"user_id declarado ({declarado}) ≠ dono da credencial ({principal.user_id})",
            )
        # Preferir o declarado é o que mantém a sombra realmente sem efeito.
        return declarado or principal.user_id

    if conflito:
        raise HTTPException(
            status_code=403,
            detail="o user_id do pedido não é o dono desta credencial",
        )
    return principal.user_id


def principal_de(request: Request) -> Principal:
    """O principal já resolvido para este pedido (ou anônimo).

    Para código que recebe ``Request`` mas não passou por uma dependência — por
    exemplo uma rota que só quer LOGAR quem chamou.
    """
    return getattr(request.state, "neo_principal", None) or ANONIMO


#: Açúcar para usar em assinatura de rota: ``p: Principal = OWNER``.
OWNER = Depends(require_owner)
ANY_PRINCIPAL = Depends(require_principal)
ADMIN_READ = Depends(require_admin_read)


def assert_secret_configured() -> None:
    """Recusa subir com o segredo de JWT no default embutido.

    O default está no código-fonte e o token minerado com ele esteve commitado
    no painel: quem lê o repo emite token de admin. Só vale com a flag ligada,
    para não travar dev nem a suíte de testes.
    """
    settings = get_settings()
    if not getattr(settings, "neo_api_auth_secret_required", False):
        return
    if settings.panel_jwt_secret == _default_secret():
        raise RuntimeError(
            "PANEL_JWT_SECRET está no default embutido do código. Defina um "
            "segredo próprio no .env (ou desligue NEO_API_AUTH_SECRET_REQUIRED)."
        )


def _default_secret() -> str:
    from shared.config import Settings

    return Settings.model_fields["panel_jwt_secret"].default
