"""Credential Broker + AuthSchemes (docs/service-gateway-dsl-plan.md §2.2).

Resolve ``credential_ref`` → material de segredo, EM MEMÓRIA, no momento da
execução, e só para o executor. O segredo nunca aparece em ToolResult, log,
exceção ou spec de job — o ``SecretMaterial`` serializa sempre como ``***``.

Schemes de referência em F1:
  - ``env://VAR_NAME``      lê da Settings/ambiente da VPS (onde os tokens já vivem)
  - ``fernet://<id>``       cofre cifrado compartilhado com o browser-agent [F2]

O broker é consultado DEPOIS do allow da política: segredo jamais é
materializado para uma request que seria negada.
"""

from __future__ import annotations

import base64
from typing import Any

from core.gateway.ir import AuthType, ConnectionConfig, GatewayError


class SecretMaterial:
    """Envelope de um segredo. Aplica-se a uma request e nunca se revela.

    Guarda o valor num atributo "privado" e sobrescreve todas as formas de
    serialização acidental (``repr``/``str``) para ``***``.
    """

    __slots__ = ("_value", "_extra")

    def __init__(self, value: str, extra: dict[str, Any] | None = None) -> None:
        self._value = value
        self._extra = extra or {}

    def reveal(self) -> str:
        """Único ponto que expõe o valor — chamado só pelo AuthScheme/executor."""
        return self._value

    def __repr__(self) -> str:  # noqa: D105
        return "SecretMaterial(***)"

    def __str__(self) -> str:  # noqa: D105
        return "***"


class ResolvedAuth:
    """O que o executor precisa para autenticar: headers e/ou params extra."""

    __slots__ = ("headers", "params")

    def __init__(self, headers: dict[str, str] | None = None, params: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.params = params or {}


class CredentialResolver:
    """Resolve schemes de ``credential_ref`` em ``SecretMaterial``.

    F1 implementa ``env://``. ``fernet://`` fica registrado e levanta um erro
    claro até a store da F2 existir (core/credentials/), para o cadastro não
    prometer o que ainda não entrega.
    """

    def __init__(self, *, settings: Any = None, fernet_store: Any = None) -> None:
        self._settings = settings
        self._fernet_store = fernet_store

    async def resolve(self, credential_ref: str | None) -> SecretMaterial | None:
        """Async porque o cofre ``fernet://`` lê do banco. O ``env://`` não
        precisaria, mas um contrato só é melhor que dois."""
        if not credential_ref:
            return None
        scheme, _, rest = credential_ref.partition("://")
        if not rest:
            raise GatewayError("bad_credential_ref", f"credential_ref inválido: '{credential_ref}'.")
        if scheme == "env":
            return self._resolve_env(rest)
        if scheme == "fernet":
            return await self._resolve_fernet(rest)
        if scheme == "vault":
            raise GatewayError("unsupported_credential", "scheme 'vault://' ainda não implementado.")
        raise GatewayError("bad_credential_ref", f"scheme de credencial desconhecido: '{scheme}'.")

    def _resolve_env(self, var_name: str) -> SecretMaterial:
        # Preferimos a Settings (fonte única já carregada); caímos no os.environ.
        value: str | None = None
        if self._settings is not None:
            # A Settings expõe atributos snake_case; aceitamos tanto o nome da env
            # (HOME_ASSISTANT_TOKEN) quanto o atributo (home_assistant_token).
            value = getattr(self._settings, var_name, None) or getattr(
                self._settings, var_name.lower(), None
            )
        if value is None:
            import os

            value = os.environ.get(var_name)
        if not value:
            raise GatewayError(
                "credential_unavailable",
                f"credencial '{var_name}' não configurada no ambiente.",
            )
        return SecretMaterial(str(value))

    async def _resolve_fernet(self, credential_id: str) -> SecretMaterial:
        if self._fernet_store is None:
            raise GatewayError(
                "credential_unavailable",
                "cofre de credenciais indisponível — defina NEO_CRED_KEY na env do neo-core "
                "(ou use uma conexão com credential_ref env://).",
            )
        try:
            value = await self._fernet_store.get_secret(credential_id)
        except Exception as exc:  # CredentialError (chave trocada) e afins
            message = getattr(exc, "message", str(exc))
            raise GatewayError("credential_unavailable", message) from exc
        if not value:
            raise GatewayError(
                "credential_unavailable", f"credencial '{credential_id}' não existe no cofre."
            )
        return SecretMaterial(str(value))


class CredentialBroker:
    """Ponte entre a conexão e o segredo aplicado: resolve + aplica o AuthScheme."""

    def __init__(self, resolver: CredentialResolver) -> None:
        self._resolver = resolver

    async def build_auth(self, conn: ConnectionConfig) -> ResolvedAuth:
        """Devolve headers/params de autenticação para a conexão. Sem segredo à vista.

        O ``ResolvedAuth`` carrega o valor real (é usado na hora do request e
        descartado), mas nunca é logado nem devolvido ao modelo.
        """
        if conn.auth_type is AuthType.NONE:
            return ResolvedAuth()

        secret = await self._resolver.resolve(conn.credential_ref)
        if secret is None:
            raise GatewayError(
                "credential_unavailable",
                f"conexão '{conn.id}' usa auth '{conn.auth_type.value}' mas não tem credential_ref.",
            )

        cfg = conn.auth_config or {}
        if conn.auth_type is AuthType.BEARER:
            return ResolvedAuth(headers={"Authorization": f"Bearer {secret.reveal()}"})

        if conn.auth_type is AuthType.BASIC:
            # auth_config.username + segredo como senha (padrão); ou segredo já "user:pass".
            username = cfg.get("username")
            if username:
                raw = f"{username}:{secret.reveal()}"
            else:
                raw = secret.reveal()
            token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
            return ResolvedAuth(headers={"Authorization": f"Basic {token}"})

        if conn.auth_type is AuthType.HEADER:
            header_name = cfg.get("header", "X-Api-Key")
            template = cfg.get("template", "{secret}")
            return ResolvedAuth(headers={header_name: template.format(secret=secret.reveal())})

        if conn.auth_type is AuthType.QUERY_PARAM:
            param_name = cfg.get("param", "token")
            return ResolvedAuth(params={param_name: secret.reveal()})

        raise GatewayError(
            "unsupported_auth",
            f"auth '{conn.auth_type.value}' ainda não implementada (F4).",
        )
