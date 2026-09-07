"""Persist + cache the LLM provider/model selection for named subsystem *roles*.

This generalizes the intent-classifier pattern (``modules.intent_config``) to the
other places in neo-core that run their own LLM and whose model should be
switchable from the neo-panel without a restart:

- ``memory_planner``  — the PKS write planner (subagente que estrutura a memória)
- ``research_volume`` — o modelo barato do Deep Research (plan/triagem/evidência…)
- ``research_writer`` — o modelo forte do Deep Research (escreve/critica/revisa)
- ``browser_agent``   — o subagente browser-use que dirige o Chromium do servidor

Each role is one global ``user_preference`` row (user_id NULL) under
``service="neo-core"``, ``category="model_config"``, ``key=<role>``; the value is
JSON ``{"provider": "...", "model": "..."}``. Reads are memoized in-process
(short TTL) and the cache is invalidated on write, so the change lands on the
next job/plan without a restart — same contract as the classifier.

The provider catalog helpers (known providers, per-provider suggested model,
availability) are shared with :mod:`modules.intent_config` so the panel shows a
single, consistent list.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from core.llm.router import (
    DEFAULT_MODEL_BY_PROVIDER,
    models_by_provider,
    supports_vision,
    vision_models_by_provider,
)
from modules.intent_config.store import (
    KNOWN_PROVIDERS,
    SUGGESTED_MODEL,
    is_provider_available,
)

logger = logging.getLogger(__name__)

# Preference coordinates. One category, one key per role (mirrors the classifier
# which uses its own category; here a single category holds every role so the
# catalog is one DB read).
PREF_SERVICE = "neo-core"
PREF_CATEGORY = "model_config"


@dataclass(frozen=True)
class RoleSpec:
    """A configurable LLM slot the panel can retarget."""

    key: str
    label: str
    description: str
    default_provider: str
    default_model: str
    #: O slot precisa LER IMAGEM. Muda duas coisas: o painel só oferece modelos
    #: com visão e a rota barra os que sabidamente não têm. Sem isso a escolha
    #: errada não dá erro — apenas cega o subsistema (ver `supports_vision`).
    vision: bool = False
    #: Providers que ESTE slot aceita; vazio = todos. Existe porque nem todo role
    #: é servido pelo LLMRouter do neo-core: o `browser_agent` roda dentro de
    #: OUTRO container, que tem uma única chave (a do OpenRouter) e um único
    #: base_url. Oferecer "deepseek" ali seria oferecer um 404 — o modelo iria
    #: para o gateway errado e a tarefa morreria longe de quem escolheu.
    only_providers: tuple[str, ...] = ()

    def accepts(self, provider: str) -> bool:
        """O slot aceita este provider? (sem restrição = aceita todos)"""
        return not self.only_providers or provider in self.only_providers

    @property
    def default(self) -> dict[str, str]:
        return {"provider": self.default_provider, "model": self.default_model}


def _brain_env_default() -> tuple[str, str]:
    """O par (provider, model) que o cérebro usava ANTES de existir este select.

    Não é `settings.default_model`: o `AgentExecutor` chama
    ``get_default_model(provider)``, ou seja, o modelo padrão DO PROVIDER — é por
    isso que o env dizia ``DEFAULT_MODEL=deepseek-chat`` (id aposentado) e mesmo
    assim rodava ``deepseek-v4-flash``. Reproduzir a regra aqui é o que faz o
    painel mostrar como "padrão" exatamente o que roda com o banco vazio.
    """
    try:
        from shared.config import get_settings

        provider = get_settings().default_provider
    except Exception:  # pragma: no cover - defensive; import-time só depende do env
        provider = "deepseek"
    return provider, DEFAULT_MODEL_BY_PROVIDER.get(provider, "deepseek-v4-flash")


_BRAIN_PROVIDER, _BRAIN_MODEL = _brain_env_default()


# The roles the panel exposes. Defaults mirror shared/config.py (the env-driven
# values these subsystems used before this became runtime-configurable) so an
# empty DB behaves exactly like before.
ROLES: dict[str, RoleSpec] = {
    "cognitive": RoleSpec(
        key="cognitive",
        label="Cérebro — agente principal",
        description=(
            "O loop que lê a conversa, decide as tools e escreve a resposta. "
            "É o modelo que o Neo É: mexer aqui muda todo turno, e é o único "
            "slot cujo erro chega como resposta no chat. Precisa aceitar tool "
            "calling — sem isso o agente vira um chat que não executa nada."
        ),
        # Sem picker, este slot vinha de DEFAULT_PROVIDER/get_default_model e era
        # o único LLM do sistema fora do alcance do painel. Em 2026-09-03 a conta
        # do DeepSeek zerou e o 402 virou a RESPOSTA do Neo no chat, enquanto os
        # outros cinco roles já estavam na OpenRouter — o operador tinha trocado
        # tudo que a tela oferecia e mesmo assim o turno morria.
        default_provider=_BRAIN_PROVIDER,
        default_model=_BRAIN_MODEL,
    ),
    "memory_planner": RoleSpec(
        key="memory_planner",
        label="Planejador de memória (PKS)",
        description=(
            "Subagente que estrutura o que é gravado no PKS (entidade, campos, "
            "relações). Muitas chamadas de JSON; um modelo barato dá conta."
        ),
        default_provider="deepseek",
        default_model="deepseek-v4-flash",
    ),
    "research_volume": RoleSpec(
        key="research_volume",
        label="Deep Research — volume (barato)",
        description=(
            "Modelo do VOLUME da pesquisa profunda: intake/plan/triagem/"
            "evidência/reflect/organize. São muitas chamadas sobre texto de web."
        ),
        default_provider="deepseek",
        default_model="deepseek-v4-flash",
    ),
    "research_writer": RoleSpec(
        key="research_writer",
        label="Deep Research — redator (forte)",
        description=(
            "Modelo FORTE da pesquisa: escreve por seção, faz resumo executivo/"
            "conclusão, critica e revisa. Poucas chamadas — decide a qualidade."
        ),
        default_provider="deepseek",
        default_model="deepseek-v4-pro",
    ),
    "browser_local": RoleSpec(
        key="browser_local",
        label="Navegador local — passo a passo",
        description=(
            "Decide UMA ação por vez no navegador do usuário (um JSON curto por "
            "passo, 25 chamadas por tarefa). É volume puro: use o barato."
        ),
        default_provider="deepseek",
        default_model="deepseek-v4-flash",
    ),
    "browser_agent": RoleSpec(
        key="browser_agent",
        label="Navegador do servidor — subagente (browser-use)",
        description=(
            "Dirige o Chromium do SERVIDOR (tool browser_task), não o da sua "
            "máquina. Roda no container neo-browser-agent, que fala só com o "
            "OpenRouter e recebe um print da tela a cada passo — por isso a "
            "lista é limitada a modelos do OpenRouter com visão."
        ),
        # Preserva o que o container já fazia: até virar role, este valor estava
        # cravado no `docker-compose.yml` e no `app.py` do container, fora do
        # alcance de qualquer tela de config. Manter como default é o que faz um
        # banco vazio se comportar exatamente como antes.
        default_provider="openrouter",
        default_model="google/gemini-2.5-flash",
        vision=True,
        only_providers=("openrouter",),
    ),
    "browser_local_vision": RoleSpec(
        key="browser_local_vision",
        label="Navegador local — olho (visão)",
        description=(
            "Modelo que OLHA o print da tela a cada passo, com o número de cada "
            "elemento desenhado em cima. Sem ele o driver decide só pelo texto do "
            "DOM e não percebe que a ação anterior não fez nada. Precisa aceitar "
            "imagem — o DeepSeek NÃO aceita (400 'does not support image')."
        ),
        # Não é o gpt-4.1-mini de antes: aquela chave ficou sem crédito e o
        # driver rodou cego por dois jobs inteiros. Este foi verificado contra a
        # API em 2026-08-31 e é o mais barato com visão entre as chaves ativas.
        default_provider="openrouter",
        default_model="google/gemini-3.1-flash-lite",
        vision=True,
    ),
}


# In-process memoization keyed by role. Invalidated on set; the TTL bounds
# staleness when multiple workers each hold their own cache.
_CACHE_TTL_SEC = 30.0
_cache: dict[str, tuple[dict[str, str], float]] = {}


def invalidate_cache(role: str | None = None) -> None:
    """Drop the memoized config for ``role`` (or all roles when None)."""
    if role is None:
        _cache.clear()
    else:
        _cache.pop(role, None)


def normalize(role: str, provider: str | None, model: str | None) -> dict[str, str]:
    """Coerce a (provider, model) pair into a complete, valid config for ``role``.

    Repairs partial input: an unknown/blank provider falls back to the role's
    default provider; a blank model falls back to the provider's suggested model
    (or the role default).
    """
    spec = ROLES[role]
    prov = (provider or "").strip().lower()
    mdl = (model or "").strip()
    if prov not in KNOWN_PROVIDERS or not spec.accepts(prov):
        prov = spec.default_provider
    if not mdl:
        # O `SUGGESTED_MODEL` conserta "trocou de provider e não disse o modelo":
        # ali o barato do provider NOVO é o melhor palpite. Mas quando o provider
        # é o do PRÓPRIO role — inclusive porque ninguém escolheu nada —, o
        # palpite genérico não tem por que ganhar do default do role, e ganhar
        # quebra a promessa de que banco vazio se comporta como antes. Era o que
        # acontecia com o `research_writer` (default v4-pro, sugerido v4-flash):
        # sem o fallback do chamador, o redator FORTE virava o barato calado.
        mdl = (
            spec.default_model
            if prov == spec.default_provider
            else SUGGESTED_MODEL.get(prov) or spec.default_model
        )
    # Slot de visão nunca pode acabar com um modelo cego. O `SUGGESTED_MODEL` do
    # provider é o barato dele, não o que enxerga: para o `deepseek` sugere
    # deepseek-v4-flash, que responde 400 a imagem e cegaria o navegador local
    # em silêncio. Aqui o padrão do próprio role ganha.
    if spec.vision and supports_vision(mdl) is False:
        logger.warning(
            "Role '%s' exige visão e '%s' não lê imagem — usando %s",
            role, mdl, spec.default_model,
        )
        return dict(spec.default)
    return {"provider": prov, "model": mdl}


async def get_model_config(
    db: Any | None,
    role: str,
    *,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
) -> dict[str, str]:
    """Return the active ``{provider, model}`` for ``role``.

    Reads the global preference (memoized for ``_CACHE_TTL_SEC``). When nothing is
    persisted (or the DB is absent), falls back to ``fallback_*`` if given — the
    caller's env-derived default — else the role's built-in default. Never raises:
    a read failure degrades to the fallback so the subsystem keeps working.
    """
    if role not in ROLES:
        raise KeyError(f"Unknown model-config role '{role}'")

    cached = _cache.get(role)
    if cached is not None and (time.monotonic() - cached[1]) < _CACHE_TTL_SEC:
        return dict(cached[0])

    config = normalize(role, fallback_provider, fallback_model)
    if db is not None:
        try:
            prefs = await db.get_preferences(
                user_id=None, service=PREF_SERVICE, category=PREF_CATEGORY
            )
            for pref in prefs:
                if pref.key == role:
                    data = json.loads(pref.value)
                    config = normalize(role, data.get("provider"), data.get("model"))
                    break
        except Exception as e:  # pragma: no cover - defensive; never break the subsystem
            logger.warning("get_model_config(%s) failed, using fallback: %s", role, e)

    _cache[role] = (dict(config), time.monotonic())
    return dict(config)


async def set_model_config(
    db: Any, role: str, provider: str, model: str, source_message: str | None = None
) -> dict[str, str]:
    """Persist a new ``{provider, model}`` for ``role`` and invalidate its cache.

    Validates only the shape (known provider, non-empty model). Whether the
    provider is actually *available* (API key configured) is enforced by the API
    route, which has the LLMRouter; this keeps the store a pure persistence layer.
    """
    if role not in ROLES:
        raise KeyError(f"Unknown model-config role '{role}'")
    config = normalize(role, provider, model)
    await db.upsert_preference(
        service=PREF_SERVICE,
        category=PREF_CATEGORY,
        key=role,
        value=json.dumps(config),
        user_id=None,
        source_message=source_message,
    )
    invalidate_cache(role)
    logger.info(
        "Model role '%s' set to provider=%s model=%s",
        role, config["provider"], config["model"],
    )
    return config


async def get_all_configs(db: Any | None) -> dict[str, dict[str, str]]:
    """Return ``{role: {provider, model}}`` for every known role (for the catalog)."""
    return {role: await get_model_config(db, role) for role in ROLES}


def build_catalog(
    available_providers: list[str], current: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Shape the data the config UI needs: per-role current + provider catalog.

    ``available_providers`` comes from ``LLMRouter.list_available_providers()`` —
    a provider is *available* only when its API key (or Ollama host) is configured.
    ``current`` maps role → active ``{provider, model}``.
    """
    models = models_by_provider()
    available = set(available_providers)
    providers = [
        {
            "name": name,
            "available": name in available,
            "models": models.get(name, []),
            "suggested": SUGGESTED_MODEL.get(name),
        }
        for name in KNOWN_PROVIDERS
    ]
    roles = [
        {
            "key": spec.key,
            "label": spec.label,
            "description": spec.description,
            "current": current.get(spec.key, spec.default),
            "default": spec.default,
            "vision": spec.vision,
            "only_providers": list(spec.only_providers),
        }
        for spec in ROLES.values()
    ]
    # Lista paralela para os roles com `vision`: o painel troca `providers` por
    # esta ao montar o dropdown, em vez de oferecer um modelo cego e deixar o
    # erro aparecer só três dias depois, num job que ninguém estava olhando.
    return {
        "roles": roles,
        "providers": providers,
        "vision_models": vision_models_by_provider(),
    }


__all__ = [
    "KNOWN_PROVIDERS",
    "PREF_CATEGORY",
    "PREF_SERVICE",
    "ROLES",
    "RoleSpec",
    "build_catalog",
    "get_all_configs",
    "get_model_config",
    "invalidate_cache",
    "is_provider_available",
    "normalize",
    "set_model_config",
]
