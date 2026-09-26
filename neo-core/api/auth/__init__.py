"""Autenticação da API HTTP (docs/api-auth-plan.md).

Um ``Principal`` por pedido, resolvido de token de aparelho pareado OU de login
de usuário, e três dependências (``require_principal``, ``require_owner``,
``require_admin_read``). Ver ``principal.py`` para o porquê de cada degrau.
"""

from api.auth.principal import (
    ADMIN_READ,
    ANONIMO,
    ANY_PRINCIPAL,
    DEVICE_TOKEN_HEADER,
    KIND_ANON,
    KIND_DEVICE,
    KIND_PANEL_LEGACY,
    KIND_USER,
    MODE_ENFORCE,
    MODE_OFF,
    MODE_SHADOW,
    MODE_STRICT,
    MODES,
    OWNER,
    Principal,
    assert_secret_configured,
    auth_mode,
    owner_of,
    principal_de,
    require_admin_read,
    require_owner,
    require_principal,
    resolve_principal,
)

__all__ = [
    "ADMIN_READ",
    "ANONIMO",
    "ANY_PRINCIPAL",
    "DEVICE_TOKEN_HEADER",
    "KIND_ANON",
    "KIND_DEVICE",
    "KIND_PANEL_LEGACY",
    "KIND_USER",
    "MODES",
    "MODE_ENFORCE",
    "MODE_OFF",
    "MODE_SHADOW",
    "MODE_STRICT",
    "OWNER",
    "Principal",
    "assert_secret_configured",
    "auth_mode",
    "owner_of",
    "principal_de",
    "require_admin_read",
    "require_owner",
    "require_principal",
    "resolve_principal",
]
