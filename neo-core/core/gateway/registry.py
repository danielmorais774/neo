"""Connection Registry (docs/service-gateway-dsl-plan.md §2.2).

Fonte de verdade das conexões: linhas de ``service_connection`` no Postgres +
seeds de bootstrap a partir das envs que já existem (HA), para não duplicar
segredo nem quebrar o que roda hoje. Cache em memória com invalidação por
update (mesmo padrão de config/skills).

O que sai daqui é ``ConnectionConfig`` — SEM segredo, só ``credential_ref``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from core.gateway.ir import (
    AuthType,
    ConnectionConfig,
    PolicyEffect,
    PolicyRule,
)

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def is_valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def _ha_seed_policies() -> list[PolicyRule]:
    """Política default do Home Assistant (§11.1: allowlist + confirm no resto).

    Leitura livre; domínios de controle comuns liberados; serviços perigosos
    (shell_command, hassio, restart/stop) negados; qualquer outra escrita cai no
    ``default_write=confirm``.
    """
    safe_domains = (
        "light", "switch", "scene", "script", "media_player", "cover",
        "climate", "fan", "automation", "input_boolean", "input_number",
        "input_select", "vacuum", "lock", "button", "notify",
    )
    rules: list[PolicyRule] = [
        PolicyRule(effect=PolicyEffect.DENY, methods=["*"], path="/api/services/shell_command/*",
                   description="shell_command executa comando arbitrário"),
        PolicyRule(effect=PolicyEffect.DENY, methods=["*"], path="/api/services/hassio/*",
                   description="hassio controla o supervisor/host"),
        PolicyRule(effect=PolicyEffect.DENY, methods=["*"], path="/api/services/homeassistant/restart",
                   description="restart derruba a instância"),
        PolicyRule(effect=PolicyEffect.DENY, methods=["*"], path="/api/services/homeassistant/stop"),
        PolicyRule(effect=PolicyEffect.ALLOW, methods=["GET", "HEAD"], path="/api/*"),
    ]
    for domain in safe_domains:
        rules.append(
            PolicyRule(effect=PolicyEffect.ALLOW, methods=["POST"], path=f"/api/services/{domain}/*")
        )
    return rules


class ConnectionRegistry:
    """Carrega + cacheia conexões (db + seeds de env)."""

    def __init__(self, *, db: Any = None, settings: Any = None) -> None:
        self._db = db
        self._settings = settings
        self._cache: dict[str, ConnectionConfig] | None = None

    async def _ensure_loaded(self) -> dict[str, ConnectionConfig]:
        if self._cache is None:
            await self.refresh()
        assert self._cache is not None
        return self._cache

    async def refresh(self) -> None:
        """Recarrega o cache (env seeds primeiro; db sobrepõe por slug)."""
        merged: dict[str, ConnectionConfig] = {}
        for seed in self._env_seeds():
            merged[seed.id] = seed
        for cfg in await self._load_db():
            merged[cfg.id] = cfg  # db vence o seed de mesmo slug
        self._cache = merged
        logger.info("[gateway] registry carregado: %d conexões (%s)", len(merged), ", ".join(sorted(merged)))

    def invalidate(self) -> None:
        self._cache = None

    async def get(self, slug: str) -> ConnectionConfig | None:
        cache = await self._ensure_loaded()
        return cache.get(slug)

    async def list_enabled(self) -> list[ConnectionConfig]:
        cache = await self._ensure_loaded()
        return [c for c in cache.values() if c.enabled]

    async def list_all(self) -> list[ConnectionConfig]:
        """Inclui as desabilitadas — é a visão do painel, não a do modelo."""
        cache = await self._ensure_loaded()
        return list(cache.values())

    def is_seed(self, slug: str) -> bool:
        """Conexão vinda de env (seed) — read-only no painel: quem manda é o .env."""
        return any(s.id == slug for s in self._env_seeds())

    # ------------------------------------------------------------------
    # CRUD (painel — F2)
    # ------------------------------------------------------------------

    async def upsert(self, payload: dict[str, Any]) -> ConnectionConfig:
        """Cria/atualiza uma conexão a partir do payload do painel.

        Valida o slug e a base_url aqui (não no handler HTTP): quem grava é quem
        garante o invariante. Invalida o cache no fim.
        """
        from db.models.gateway import ServiceConnection

        slug = str(payload.get("id") or "").strip().lower()
        if not is_valid_slug(slug):
            raise ValueError(
                "slug inválido: use minúsculas, números e hífen (2-40 chars), "
                "começando por letra/número. Ex.: 'github-personal'"
            )
        base_url = str(payload.get("base_url") or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url precisa começar com http:// ou https://")
        if base_url.startswith("http://") and not payload.get("allow_http"):
            raise ValueError(
                "base_url é http:// (sem TLS) — marque 'permitir HTTP' se o serviço "
                "é interno e isso é intencional."
            )
        if self.is_seed(slug):
            raise ValueError(
                f"'{slug}' vem das variáveis de ambiente e é read-only aqui — "
                "edite o .env do neo-core."
            )
        # Valida as regras de política antes de gravar (erro cedo > erro no request).
        rules = [PolicyRule(**r) if isinstance(r, dict) else r for r in (payload.get("policies") or [])]

        async with self._db.get_session() as session:
            row = await session.get(ServiceConnection, slug)
            created = row is None
            if row is None:
                row = ServiceConnection(id=slug, name=slug, base_url=base_url)
            for field in (
                "name", "provider", "auth_type", "auth_config", "credential_ref",
                "default_headers", "timeout_s", "retry_max", "verify_tls", "allow_http",
                "follow_redirects", "max_body_bytes", "max_response_bytes",
                "default_read", "default_write", "docs_url", "capabilities", "enabled",
                "openapi_spec", "openapi_ref",
            ):
                if field in payload and payload[field] is not None:
                    setattr(row, field, payload[field])
            row.base_url = base_url
            row.policies = [r.model_dump(mode="json") for r in rules]
            row.updated_at = datetime.utcnow()
            row.managed_by = "panel"
            session.add(row)
            await session.commit()
            await session.refresh(row)
            config = self._row_to_config(row)

        self.invalidate()
        logger.info("[gateway] conexão '%s' %s pelo painel", slug, "criada" if created else "atualizada")
        return config

    async def delete(self, slug: str) -> bool:
        from db.models.gateway import ServiceConnection

        if self.is_seed(slug):
            raise ValueError(f"'{slug}' vem do .env — não dá para apagar pelo painel.")
        async with self._db.get_session() as session:
            row = await session.get(ServiceConnection, slug)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        self.invalidate()
        logger.info("[gateway] conexão '%s' removida pelo painel", slug)
        return True

    # ------------------------------------------------------------------
    # Fontes
    # ------------------------------------------------------------------

    def _env_seeds(self) -> list[ConnectionConfig]:
        seeds: list[ConnectionConfig] = []
        s = self._settings
        if s is None:
            return seeds
        # Home Assistant — reaproveita HOME_ASSISTANT_URL/TOKEN já configurados.
        token = getattr(s, "home_assistant_token", None)
        url = getattr(s, "home_assistant_url", None)
        if token and url:
            seeds.append(
                ConnectionConfig(
                    id="homeassistant",
                    name="Home Assistant",
                    provider="homeassistant",
                    base_url=url,
                    auth_type=AuthType.BEARER,
                    credential_ref="env://home_assistant_token",
                    # HA no homelab é http:// na LAN/rede interna — o admin configurou
                    # essa URL de propósito, então honramos o scheme dela.
                    allow_http=url.lower().startswith("http://"),
                    timeout_s=float(getattr(s, "home_assistant_timeout", 15) or 15),
                    default_read=PolicyEffect.ALLOW,
                    default_write=PolicyEffect.CONFIRM,
                    policies=_ha_seed_policies(),
                    docs_url="https://developers.home-assistant.io/docs/api/rest/",
                    capabilities={"tags": ["home-automation", "rest"]},
                )
            )
        return seeds

    async def _load_db(self) -> list[ConnectionConfig]:
        if self._db is None:
            return []
        try:
            from sqlmodel import select

            from db.models.gateway import ServiceConnection

            async with self._db.get_session() as session:
                result = await session.execute(select(ServiceConnection))
                rows = result.scalars().all()
        except Exception as exc:  # noqa: BLE001 — sem tabela ainda (pré-migração) ⇒ só seeds
            logger.warning("[gateway] não foi possível ler service_connection (%s); usando só seeds", exc)
            return []
        return [self._row_to_config(row) for row in rows]

    @staticmethod
    def _row_to_config(row: Any) -> ConnectionConfig:
        rules = [PolicyRule(**r) if isinstance(r, dict) else r for r in (row.policies or [])]
        return ConnectionConfig(
            id=row.id,
            name=row.name,
            provider=row.provider,
            base_url=row.base_url,
            auth_type=AuthType(row.auth_type),
            auth_config=row.auth_config or {},
            credential_ref=row.credential_ref,
            default_headers=row.default_headers or {},
            timeout_s=float(row.timeout_s or 15),
            retry_max=int(row.retry_max or 0),
            verify_tls=bool(row.verify_tls),
            allow_http=bool(row.allow_http),
            follow_redirects=bool(row.follow_redirects),
            max_body_bytes=int(row.max_body_bytes),
            max_response_bytes=int(row.max_response_bytes),
            default_read=PolicyEffect(row.default_read),
            default_write=PolicyEffect(row.default_write),
            policies=rules,
            docs_url=row.docs_url,
            openapi_spec=getattr(row, "openapi_spec", None),
            openapi_ref=row.openapi_ref,
            capabilities=row.capabilities or {},
            enabled=bool(row.enabled),
        )
