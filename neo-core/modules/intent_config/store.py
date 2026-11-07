"""Persist + cache the intent classifier's provider/model selection.

Stored as a single global ``user_preference`` row (user_id NULL) so it behaves as
a system-wide setting rather than a per-end-user one. The value is JSON
``{"provider": "...", "model": "..."}``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from core.llm.catalog import (
    default_model_for,
    known_providers,
    models_by_provider,
    provider_for_model,
    provider_label,
)

logger = logging.getLogger(__name__)

# Preference coordinates (mirrors the voice module's pattern).
PREF_SERVICE = "neo-core"
PREF_CATEGORY = "intent_router"
PREF_KEY = "classifier"

# What the router used before this was configurable — also the safe fallback.
DEFAULT_CLASSIFIER: dict[str, str] = {
    "provider": "deepseek",
    "model": "deepseek-flash",
}

# Os providers conhecidos vêm do catálogo (`known_providers()`, editado no painel
# em Configurações → Providers) — não de uma tupla fixa: um provider cadastrado lá
# seria recusado aqui como "desconhecido".

# A sensible fast/cheap model per provider for classification, used to prefill the
# UI and to repair a provider switch that didn't specify a model.
SUGGESTED_MODEL: dict[str, str] = {
    "deepseek": "deepseek-flash",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.1-8b-instant",
    "gemini": "gemini-2.0-flash-lite",
    "grok": "grok-3-mini",
    "anthropic": "claude-3-haiku-20240307",
    "openrouter": "google/gemini-3.1-flash-lite",
}


def suggested_model(provider: str) -> str | None:
    """O barato do provider para classificar — se ainda estiver no catálogo.

    Provider cadastrado no painel não tem entrada em ``SUGGESTED_MODEL``, e um
    sugerido que o operador apagou do catálogo não pode voltar como escolha: nos
    dois casos vale o modelo padrão do provider.
    """
    picked = SUGGESTED_MODEL.get(provider)
    if picked and provider_for_model(picked) == provider:
        return picked
    return default_model_for(provider)

# In-process memoization so we don't hit the DB on every classify(). Invalidated
# on set; the TTL bounds staleness when multiple workers each hold their own cache.
_CACHE_TTL_SEC = 30.0
_cache: dict[str, str] | None = None
_cache_at: float = 0.0


def invalidate_cache() -> None:
    """Drop the memoized config so the next read re-fetches from the DB."""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def normalize(provider: str | None, model: str | None) -> dict[str, str]:
    """Coerce a (provider, model) pair into a complete, valid config dict.

    Repairs partial input: an unknown/blank provider falls back to the default;
    a blank model falls back to the provider's suggested model (or the default).
    """
    prov = (provider or "").strip().lower()
    mdl = (model or "").strip()
    if prov not in known_providers():
        prov = DEFAULT_CLASSIFIER["provider"]
    if not mdl:
        mdl = suggested_model(prov) or DEFAULT_CLASSIFIER["model"]
    return {"provider": prov, "model": mdl}


async def get_classifier_config(db: Any | None) -> dict[str, str]:
    """Return the active ``{provider, model}`` for intent classification.

    Reads the global preference (memoized for ``_CACHE_TTL_SEC``). Falls back to
    :data:`DEFAULT_CLASSIFIER` when no DB is available or nothing is persisted.
    Never raises — a read failure degrades to the default so routing keeps working.
    """
    global _cache, _cache_at
    if _cache is not None and (time.monotonic() - _cache_at) < _CACHE_TTL_SEC:
        return dict(_cache)

    config = dict(DEFAULT_CLASSIFIER)
    if db is not None:
        try:
            prefs = await db.get_preferences(
                user_id=None, service=PREF_SERVICE, category=PREF_CATEGORY
            )
            for pref in prefs:
                if pref.key == PREF_KEY:
                    data = json.loads(pref.value)
                    config = normalize(data.get("provider"), data.get("model"))
                    break
        except Exception as e:  # pragma: no cover - defensive; never break routing
            logger.warning("get_classifier_config failed, using default: %s", e)

    _cache = dict(config)
    _cache_at = time.monotonic()
    return dict(config)


async def set_classifier_config(
    db: Any, provider: str, model: str, source_message: str | None = None
) -> dict[str, str]:
    """Persist a new classifier ``{provider, model}`` and invalidate the cache.

    Validates only the shape (known provider, non-empty model). Whether the
    provider is actually *available* (API key configured) is enforced by the API
    route, which has the LLMRouter; this keeps the store a pure persistence layer.
    """
    config = normalize(provider, model)
    await db.upsert_preference(
        service=PREF_SERVICE,
        category=PREF_CATEGORY,
        key=PREF_KEY,
        value=json.dumps(config),
        user_id=None,
        source_message=source_message,
    )
    invalidate_cache()
    logger.info("Intent classifier set to provider=%s model=%s", config["provider"], config["model"])
    return config


def build_catalog(available_providers: list[str], current: dict[str, str]) -> dict[str, Any]:
    """Shape the data the config UI needs: providers, availability, known models.

    ``available_providers`` comes from ``LLMRouter.list_available_providers()`` —
    a provider is *available* only when its API key (or Ollama host) is configured.
    """
    models = models_by_provider()
    available = set(available_providers)
    providers = []
    for name in known_providers():
        providers.append(
            {
                "name": name,
                "label": provider_label(name),
                "available": name in available,
                "models": models.get(name, []),
                "suggested": suggested_model(name),
            }
        )
    return {
        "current": current,
        "providers": providers,
        "default": DEFAULT_CLASSIFIER,
    }


def is_provider_available(name: str, available_providers: list[str]) -> bool:
    """True when ``name`` is a configured/usable provider."""
    return name in set(available_providers)


# Re-exported so callers don't import from two modules for related concerns.
__all__ = [
    "DEFAULT_CLASSIFIER",
    "PREF_CATEGORY",
    "PREF_KEY",
    "PREF_SERVICE",
    "build_catalog",
    "get_classifier_config",
    "invalidate_cache",
    "is_provider_available",
    "known_providers",
    "normalize",
    "provider_for_model",
    "set_classifier_config",
    "suggested_model",
]
