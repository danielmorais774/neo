"""Persistência do catálogo de LLM (painel → Configurações → Providers).

Lê ``llm_provider``/``llm_model``, fotografa em ``core.llm.catalog`` e manda o
``LLMRouter`` remontar os providers — tudo em :func:`refresh`, que roda na
subida e depois de CADA gravação. Assim uma chave nova ou um modelo novo valem
no próximo turno, sem reiniciar o core.

Três regras que o resto do sistema assume:

- **Banco vazio é semeado** com ``core/llm/catalog_seed.py`` (o catálogo de antes
  do CRUD). Tabela AUSENTE não é erro: o core segue com a semente em memória e
  as gravações respondem 503 pedindo a migração.
- **A chave nunca sai daqui em claro**: é cifrada com a ``NEO_CRED_KEY`` (o mesmo
  Fernet do cofre) e o painel só vê a origem e os últimos 4 caracteres.
- **Nada em uso é apagado nem desligado**: um slot da aba Modelos, o
  classificador ou o ``DEFAULT_PROVIDER`` apontando para o item barra a gravação
  com 409 dizendo quem usa. Sem isso o ``normalize`` dos slots trocaria o
  modelo por outro em silêncio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlmodel import select

from core.llm import catalog
from core.llm.catalog import (
    ADAPTERS,
    ENV_KEY_FIELD,
    PROVIDER_NAME_RE,
    Catalog,
    ModelSpec,
    ProviderSpec,
)
from db.models.llm_catalog import LLMModel, LLMProvider
from shared import Message, MessageRole, get_settings

logger = logging.getLogger(__name__)


class CatalogError(Exception):
    """Erro de negócio do catálogo, com o status HTTP que a rota devolve."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


#: Providers com chave no banco que não decifra (NEO_CRED_KEY trocada ou
#: ausente). Ficam sem a chave do banco — e caem na do .env, se houver.
_broken_keys: set[str] = set()
#: Por que o catálogo está na semente em vez do banco (None = está no banco).
_last_error: str | None = None


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------


def _fernet() -> Any:
    key = get_settings().neo_cred_key
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


async def _read(db: Any) -> tuple[list[LLMProvider], list[LLMModel]]:
    async with db.get_session() as session:
        providers = (await session.execute(select(LLMProvider))).scalars().all()
        models = (await session.execute(select(LLMModel))).scalars().all()
    return list(providers), list(models)


async def _seed(db: Any) -> None:
    seed = catalog.build_seed_catalog()
    async with db.get_session() as session:
        for p in seed.providers:
            session.add(
                LLMProvider(
                    name=p.name,
                    label=p.label,
                    adapter=p.adapter,
                    default_model=p.default_model,
                    position=p.position,
                )
            )
        await session.flush()
        for m in seed.models:
            session.add(
                LLMModel(
                    provider=m.provider,
                    model_id=m.id,
                    label=m.label,
                    description=m.description,
                    vision=m.vision,
                    in_selector=m.in_selector,
                    position=m.position,
                )
            )
        await session.commit()
    logger.info(
        "[llm-catalog] banco vazio — semente gravada (%d providers, %d modelos)",
        len(seed.providers), len(seed.models),
    )


def _to_catalog(
    providers: list[LLMProvider], models: list[LLMModel]
) -> tuple[Catalog, dict[str, str], set[str]]:
    fernet = _fernet()
    keys: dict[str, str] = {}
    broken: set[str] = set()
    specs: list[ProviderSpec] = []
    for row in providers:
        if row.api_key_encrypted:
            if fernet is None:
                broken.add(row.name)
            else:
                try:
                    keys[row.name] = fernet.decrypt(row.api_key_encrypted).decode()
                except Exception:  # InvalidToken: chave do cofre trocada
                    broken.add(row.name)
        specs.append(
            ProviderSpec(
                name=row.name,
                label=row.label,
                adapter=row.adapter,
                base_url=row.base_url,
                enabled=row.enabled,
                default_model=row.default_model,
                position=row.position,
                has_db_key=bool(row.api_key_encrypted),
                key_hint=row.api_key_hint,
            )
        )
    model_specs = tuple(
        ModelSpec(
            id=m.model_id,
            provider=m.provider,
            label=m.label,
            description=m.description,
            vision=m.vision,
            in_selector=m.in_selector,
            position=m.position,
            notes=m.notes,
            row_id=str(m.id),
        )
        for m in models
    )
    return Catalog(providers=tuple(specs), models=model_specs, source="db"), keys, broken


def _missing_table(exc: Exception) -> bool:
    text = str(exc)
    return "does not exist" in text and ("llm_provider" in text or "llm_model" in text)


async def refresh(db: Any, router: Any | None) -> Catalog:
    """Relê o banco, instala o catálogo e remonta os providers do router.

    Nunca levanta: falha de leitura mantém o catálogo atual (a semente, na
    subida) e fica registrada em ``_last_error`` para o painel mostrar.
    """
    global _broken_keys, _last_error
    if db is None:
        return catalog.current()
    try:
        providers, models = await _read(db)
        if not providers:
            await _seed(db)
            providers, models = await _read(db)
    except Exception as e:
        _last_error = (
            "as tabelas llm_provider/llm_model não existem — rode `alembic upgrade head` "
            "no container neo-core"
            if _missing_table(e)
            else f"falha lendo o catálogo do banco: {e}"
        )
        logger.warning("[llm-catalog] seguindo com o catálogo em memória: %s", _last_error)
        return catalog.current()
    cat, keys, broken = _to_catalog(providers, models)
    catalog.install(cat)
    _broken_keys = broken
    _last_error = None
    if broken:
        logger.error(
            "[llm-catalog] chave do banco não decifra (NEO_CRED_KEY trocada?) em: %s",
            ", ".join(sorted(broken)),
        )
    if router is not None:
        router.rebuild(keys)
    return cat


# ---------------------------------------------------------------------------
# Quem usa o quê (trava de apagar/desligar)
# ---------------------------------------------------------------------------


async def usage(db: Any) -> list[dict[str, Any]]:
    """Todo lugar que aponta para um provider/modelo: slots, classificador, env."""
    from modules.intent_config.store import get_classifier_config
    from modules.model_config.store import ROLES, get_all_configs

    out: list[dict[str, Any]] = []
    try:
        for role, cfg in (await get_all_configs(db)).items():
            out.append({"label": ROLES[role].label, **cfg})
    except Exception as e:  # pragma: no cover - defensivo: sem slots, sem trava deles
        logger.warning("[llm-catalog] slots ilegíveis: %s", e)
    try:
        cfg = await get_classifier_config(db)
        out.append({"label": "Classificador de intenção", **cfg})
    except Exception as e:  # pragma: no cover
        logger.warning("[llm-catalog] classificador ilegível: %s", e)
    out.append(
        {"label": "DEFAULT_PROVIDER do .env", "provider": get_settings().default_provider, "model": None}
    )
    return out


def _users_of(uses: list[dict[str, Any]], *, provider: str | None = None, model: str | None = None) -> list[str]:
    return [
        u["label"]
        for u in uses
        if (provider is not None and u.get("provider") == provider)
        or (model is not None and u.get("model") == model)
    ]


def _refuse_in_use(what: str, users: list[str]) -> None:
    if users:
        raise CatalogError(
            "in_use",
            f"{what} está em uso por: {', '.join(users)}. Troque lá antes.",
            status=409,
        )


# ---------------------------------------------------------------------------
# Payload do painel
# ---------------------------------------------------------------------------


def _key_source(p: ProviderSpec) -> str | None:
    """Mesma regra do ``LLMRouter._resolve_key``: banco → .env → não precisa."""
    if p.has_db_key and p.name not in _broken_keys:
        return "db"
    field = ENV_KEY_FIELD.get(p.name)
    if field and p.adapter == p.name and getattr(get_settings(), field, None):
        return "env"
    adapter = ADAPTERS.get(p.adapter)
    if adapter is not None and not adapter.needs_key:
        return "none"
    return None


def admin_payload(router: Any | None, uses: list[dict[str, Any]]) -> dict[str, Any]:
    cat = catalog.current()
    available = set(router.list_available_providers()) if router is not None else set()
    providers = []
    for p in cat.provider_by_name.values():
        adapter = ADAPTERS.get(p.adapter)
        providers.append(
            {
                "name": p.name,
                "label": p.label,
                "adapter": p.adapter,
                "adapter_label": adapter.label if adapter else p.adapter,
                "base_url": p.base_url,
                "enabled": p.enabled,
                "default_model": p.default_model,
                "position": p.position,
                "available": p.name in available,
                "key": {
                    "source": _key_source(p),
                    "hint": p.key_hint,
                    "stored": p.has_db_key,
                    "broken": p.name in _broken_keys,
                    # Nome da variável de reserva, para o painel dizer qual é.
                    "env_var": (
                        ENV_KEY_FIELD[p.name].upper()
                        if p.name in ENV_KEY_FIELD and p.adapter == p.name
                        else None
                    ),
                },
                "in_use": _users_of(uses, provider=p.name),
                "models": [
                    {
                        "id": m.row_id,
                        "model_id": m.id,
                        "label": m.label,
                        "description": m.description,
                        "vision": m.vision,
                        "in_selector": m.in_selector,
                        "position": m.position,
                        "notes": m.notes,
                        "in_use": _users_of(uses, model=m.id),
                    }
                    for m in cat.models_by_provider.get(p.name, [])
                ],
            }
        )
    return {
        "source": cat.source,
        "error": _last_error,
        "cred_key": bool(get_settings().neo_cred_key),
        "adapters": [
            {"key": a.key, "label": a.label, "needs_key": a.needs_key, "base_url": a.base_url}
            for a in ADAPTERS.values()
        ],
        "providers": providers,
    }


# ---------------------------------------------------------------------------
# Gravação
# ---------------------------------------------------------------------------


def _require_db() -> None:
    if catalog.current().source != "db":
        raise CatalogError(
            "not_migrated",
            _last_error or "o catálogo ainda não foi carregado do banco.",
            status=503,
        )


def _clean(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _check_base_url(adapter: str, base_url: str | None) -> str | None:
    url = (base_url or "").strip() or None
    rule = ADAPTERS[adapter].base_url
    if rule == "required" and not url:
        raise CatalogError("base_url_required", "este tipo de provider exige um base_url.")
    if rule == "none" and url:
        raise CatalogError("base_url_unsupported", "este tipo de provider não aceita base_url.")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise CatalogError("bad_base_url", f"base_url inválido: {url!r} (use http(s)://host/…).")
    return url


async def _get_provider_row(session: Any, name: str) -> LLMProvider:
    row = await session.get(LLMProvider, name)
    if row is None:
        raise CatalogError("not_found", f"provider '{name}' não existe.", status=404)
    return row


async def _get_model_row(session: Any, model_row_id: str) -> LLMModel:
    try:
        uid = UUID(str(model_row_id))
    except ValueError:
        raise CatalogError("not_found", "modelo não encontrado.", status=404) from None
    row = await session.get(LLMModel, uid)
    if row is None:
        raise CatalogError("not_found", "modelo não encontrado.", status=404)
    return row


async def _model_owner(session: Any, model_id: str) -> str | None:
    result = await session.execute(select(LLMModel).where(LLMModel.model_id == model_id))
    row = result.scalars().first()
    return row.provider if row else None


async def create_provider(db: Any, router: Any, data: dict[str, Any]) -> None:
    _require_db()
    name = _clean(data.get("name"), 40).lower()
    if not PROVIDER_NAME_RE.match(name):
        raise CatalogError(
            "bad_name", "nome inválido: use minúsculas, números, '-' ou '_' (até 40)."
        )
    adapter = _clean(data.get("adapter"), 30)
    if adapter not in ADAPTERS:
        raise CatalogError("bad_adapter", f"tipo desconhecido: {adapter!r}.")
    base_url = _check_base_url(adapter, data.get("base_url"))
    label = _clean(data.get("label"), 80) or name
    async with db.get_session() as session:
        if await session.get(LLMProvider, name) is not None:
            raise CatalogError("duplicate", f"já existe um provider '{name}'.", status=409)
        last = max((p.position for p in catalog.current().providers), default=-1)
        session.add(
            LLMProvider(name=name, label=label, adapter=adapter, base_url=base_url, position=last + 1)
        )
        await session.commit()
    logger.info("[llm-catalog] provider criado: %s (%s)", name, adapter)
    await refresh(db, router)


async def update_provider(db: Any, router: Any, name: str, data: dict[str, Any]) -> None:
    _require_db()
    async with db.get_session() as session:
        row = await _get_provider_row(session, name)
        if "adapter" in data and data["adapter"] is not None:
            adapter = _clean(data["adapter"], 30)
            if adapter not in ADAPTERS:
                raise CatalogError("bad_adapter", f"tipo desconhecido: {adapter!r}.")
            row.adapter = adapter
        if "base_url" in data or "adapter" in data:
            row.base_url = _check_base_url(row.adapter, data.get("base_url", row.base_url))
        if data.get("label") is not None:
            row.label = _clean(data["label"], 80) or row.name
        if "default_model" in data:
            default = _clean(data.get("default_model"), 200) or None
            if default is not None and await _model_owner(session, default) != row.name:
                raise CatalogError(
                    "bad_default_model", f"'{default}' não é um modelo do provider '{row.name}'."
                )
            row.default_model = default
        if data.get("enabled") is False and row.enabled:
            _refuse_in_use(f"O provider '{row.name}'", _users_of(await usage(db), provider=row.name))
        if data.get("enabled") is not None:
            row.enabled = bool(data["enabled"])
        row.updated_at = datetime.utcnow()
        session.add(row)
        await session.commit()
    await refresh(db, router)


async def delete_provider(db: Any, router: Any, name: str) -> None:
    _require_db()
    _refuse_in_use(f"O provider '{name}'", _users_of(await usage(db), provider=name))
    async with db.get_session() as session:
        row = await _get_provider_row(session, name)
        await session.delete(row)  # os modelos vão junto (ON DELETE CASCADE)
        await session.commit()
    logger.info("[llm-catalog] provider apagado: %s", name)
    await refresh(db, router)


async def set_key(db: Any, router: Any, name: str, api_key: str | None) -> None:
    """Grava (ou, com ``None``, apaga) a chave do provider. O valor antigo não volta."""
    _require_db()
    secret = (api_key or "").strip() or None
    fernet = _fernet() if secret else None
    if secret and fernet is None:
        raise CatalogError(
            "no_cred_key",
            "NEO_CRED_KEY não está configurada no core — sem ela a chave não pode ser cifrada.",
            status=503,
        )
    async with db.get_session() as session:
        row = await _get_provider_row(session, name)
        if secret:
            row.api_key_encrypted = fernet.encrypt(secret.encode())
            row.api_key_hint = secret[-4:] if len(secret) >= 8 else None
        else:
            row.api_key_encrypted = None
            row.api_key_hint = None
        row.updated_at = datetime.utcnow()
        session.add(row)
        await session.commit()
    logger.info("[llm-catalog] chave do provider '%s' %s", name, "gravada" if secret else "removida")
    await refresh(db, router)


async def reorder_providers(db: Any, router: Any, names: list[str]) -> None:
    _require_db()
    async with db.get_session() as session:
        for i, name in enumerate(names):
            row = await _get_provider_row(session, name)
            row.position = i
            session.add(row)
        await session.commit()
    await refresh(db, router)


def _model_fields(row: LLMModel, data: dict[str, Any]) -> None:
    if data.get("label") is not None:
        row.label = _clean(data["label"], 120) or row.model_id
    if data.get("description") is not None:
        row.description = _clean(data["description"], 300)
    if data.get("notes") is not None:
        row.notes = (data["notes"] or "").strip()
    for flag in ("vision", "in_selector"):
        if data.get(flag) is not None:
            setattr(row, flag, bool(data[flag]))


def _check_model_id(value: str | None) -> str:
    model_id = _clean(value, 200)
    if not model_id or any(ch.isspace() for ch in model_id):
        raise CatalogError("bad_model_id", "id do modelo vazio ou com espaço.")
    return model_id


async def create_model(db: Any, router: Any, provider: str, data: dict[str, Any]) -> None:
    _require_db()
    model_id = _check_model_id(data.get("model_id"))
    async with db.get_session() as session:
        await _get_provider_row(session, provider)
        owner = await _model_owner(session, model_id)
        if owner is not None:
            raise CatalogError(
                "duplicate", f"'{model_id}' já está cadastrado no provider '{owner}'.", status=409
            )
        siblings = catalog.current().models_by_provider.get(provider, [])
        row = LLMModel(
            provider=provider,
            model_id=model_id,
            label=model_id,
            position=max((m.position for m in siblings), default=-1) + 1,
        )
        _model_fields(row, data)
        session.add(row)
        await session.commit()
    logger.info("[llm-catalog] modelo criado: %s/%s", provider, model_id)
    await refresh(db, router)


async def _carry_rename(db: Any, provider: str, old: str, new: str) -> list[str]:
    """Leva o id novo para os slots que apontavam para o antigo.

    Renomear é quase sempre CORRIGIR o id (o caso de 06/11: um id do Hugging
    Face, `google/gemma-4-E4B-it`, que o OpenRouter não conhece, já posto no slot
    do navegador local). Recusar com 409 deixava o operador sem saída; renomear
    sem levar os slots os deixaria apontando para um id que sumiu do catálogo.
    """
    from modules.intent_config import store as intent_store
    from modules.model_config import store as model_store

    note = f"renomeado no catálogo: {old} → {new}"
    carried: list[str] = []
    # Lê do banco, não do cache de 30 s: o slot pode ter mudado agora há pouco.
    model_store.invalidate_cache()
    for role, cfg in (await model_store.get_all_configs(db)).items():
        if cfg.get("provider") == provider and cfg.get("model") == old:
            await model_store.set_model_config(db, role, provider, new, source_message=note)
            carried.append(model_store.ROLES[role].label)
    intent_store.invalidate_cache()
    classifier = await intent_store.get_classifier_config(db)
    if classifier.get("provider") == provider and classifier.get("model") == old:
        await intent_store.set_classifier_config(db, provider, new, source_message=note)
        carried.append("Classificador de intenção")
    if carried:
        logger.info("[llm-catalog] %s → %s levado para: %s", old, new, ", ".join(carried))
    return carried


async def update_model(db: Any, router: Any, model_row_id: str, data: dict[str, Any]) -> None:
    _require_db()
    renamed: tuple[str, str, str] | None = None
    async with db.get_session() as session:
        row = await _get_model_row(session, model_row_id)
        if data.get("model_id") is not None:
            new_id = _check_model_id(data["model_id"])
            if new_id != row.model_id:
                owner = await _model_owner(session, new_id)
                if owner is not None:
                    raise CatalogError(
                        "duplicate", f"'{new_id}' já está cadastrado no provider '{owner}'.", status=409
                    )
                provider_row = await _get_provider_row(session, row.provider)
                if provider_row.default_model == row.model_id:
                    provider_row.default_model = new_id
                    session.add(provider_row)
                renamed = (row.provider, row.model_id, new_id)
                row.model_id = new_id
        _model_fields(row, data)
        row.updated_at = datetime.utcnow()
        session.add(row)
        await session.commit()
    await refresh(db, router)
    # Depois do refresh: o `normalize` dos slots já enxerga o id novo (e a visão
    # dele) ao gravar.
    if renamed is not None:
        await _carry_rename(db, *renamed)


async def delete_model(db: Any, router: Any, model_row_id: str) -> None:
    _require_db()
    async with db.get_session() as session:
        row = await _get_model_row(session, model_row_id)
        _refuse_in_use(f"O modelo '{row.model_id}'", _users_of(await usage(db), model=row.model_id))
        provider_row = await _get_provider_row(session, row.provider)
        if provider_row.default_model == row.model_id:
            raise CatalogError(
                "in_use",
                f"'{row.model_id}' é o modelo padrão do provider '{row.provider}'. Troque o padrão antes.",
                status=409,
            )
        await session.delete(row)
        await session.commit()
    await refresh(db, router)


async def reorder_models(db: Any, router: Any, provider: str, row_ids: list[str]) -> None:
    _require_db()
    async with db.get_session() as session:
        for i, row_id in enumerate(row_ids):
            row = await _get_model_row(session, row_id)
            if row.provider != provider:
                raise CatalogError("bad_order", "a lista mistura modelos de outro provider.")
            row.position = i
            session.add(row)
        await session.commit()
    await refresh(db, router)


# ---------------------------------------------------------------------------
# Teste de chave
# ---------------------------------------------------------------------------


async def probe_provider(router: Any, name: str, model: str | None = None) -> dict[str, Any]:
    """Uma chamada mínima de verdade — gasta centavos da chave testada."""
    try:
        provider = router.get_provider(name)
    except Exception:
        return {
            "ok": False,
            "model": model,
            "error": "provider não está registrado (desativado ou sem chave).",
        }
    chosen = (model or "").strip() or catalog.default_model_for(name)
    if not chosen:
        listed = catalog.current().models_by_provider.get(name, [])
        chosen = listed[0].id if listed else None
    if not chosen:
        return {"ok": False, "model": None, "error": "o provider não tem modelo cadastrado."}
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            provider.complete(
                [Message(role=MessageRole.USER, content="Responda apenas: ok")],
                model=chosen,
                temperature=0,
                max_tokens=64,
            ),
            timeout=30,
        )
        reply = (response.get_content() or "").strip()
        return {
            "ok": True,
            "model": chosen,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reply": reply[:200],
        }
    except Exception as e:
        return {
            "ok": False,
            "model": chosen,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": (str(e) or type(e).__name__)[:500],
        }


__all__ = [
    "CatalogError",
    "admin_payload",
    "create_model",
    "create_provider",
    "delete_model",
    "delete_provider",
    "refresh",
    "reorder_models",
    "reorder_providers",
    "set_key",
    "probe_provider",
    "update_model",
    "update_provider",
    "usage",
]
