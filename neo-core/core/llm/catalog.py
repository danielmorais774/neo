"""Catálogo de providers e modelos de LLM — a fonte única do que existe.

Antes deste módulo havia QUATRO cópias: o mapa do ``router.py``, o enum do
desktop (``src/lib/models.ts``), o enum do chat do painel (``chatTypes.ts``) e os
rótulos da aba Modelos. Já divergiam (o seletor oferecia um Claude que o core
não conhecia). Agora:

- o catálogo mora no Postgres e é editado no painel (``modules.llm_catalog``);
- em memória fica um :class:`Catalog` imutável, trocado inteiro a cada gravação
  (:func:`install`) — quem lê nunca vê meio catálogo;
- os clientes buscam a vitrine pronta em ``GET /api/v1/desktop/model-catalog``.

As consultas são SÍNCRONAS de propósito: ``provider_for_model`` e
``supports_vision`` são chamadas no meio de código síncrono (normalização dos
slots, driver do navegador). Por isso o banco é lido uma vez e fotografado aqui.

Sem banco (tabelas ainda não migradas) o catálogo é a semente de
``catalog_seed`` — exatamente o que o ``router.py`` tinha cravado antes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from typing import Iterable

from core.llm import catalog_seed as seed


@dataclass(frozen=True)
class AdapterSpec:
    """Uma classe de provider que o core sabe instanciar."""

    key: str
    label: str
    #: Sem chave o provider não é registrado (o Ollama e servidores locais não pedem).
    needs_key: bool
    #: ``required`` | ``optional`` | ``none`` — o painel mostra o campo conforme.
    base_url: str = "none"


#: As classes de ``core/llm/*_provider.py``. Cadastrar um provider é escolher uma
#: destas: as manias de cada API (pin ``@slug`` do OpenRouter, thinking do
#: DeepSeek…) moram na classe, então um "OpenRouter 2" com o adapter
#: ``openrouter`` herda todas. ``openai_compatible`` é a porta para o resto
#: (Together, Fireworks direto, vLLM…): OpenAI Chat Completions num ``base_url``.
ADAPTERS: dict[str, AdapterSpec] = {
    spec.key: spec
    for spec in (
        AdapterSpec("openai", "OpenAI", needs_key=True),
        AdapterSpec("anthropic", "Anthropic", needs_key=True),
        AdapterSpec("deepseek", "DeepSeek", needs_key=True),
        AdapterSpec("gemini", "Gemini", needs_key=True),
        AdapterSpec("grok", "Grok (xAI)", needs_key=True),
        AdapterSpec("groq", "Groq", needs_key=True),
        AdapterSpec("openrouter", "OpenRouter", needs_key=True, base_url="optional"),
        AdapterSpec("ollama", "Ollama", needs_key=False, base_url="optional"),
        AdapterSpec(
            "openai_compatible", "Compatível com OpenAI", needs_key=False, base_url="required"
        ),
    )
}

#: Campo do ``Settings`` com a chave de cada provider de fábrica. É a RESERVA:
#: chave gravada no painel vence. Só vale quando o adapter é o de fábrica — um
#: provider recriado com o nome ``openai`` apontando para outro ``base_url`` não
#: pode herdar a chave da OpenAI.
ENV_KEY_FIELD: dict[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "deepseek": "deepseek_api_key",
    "gemini": "gemini_api_key",
    "grok": "grok_api_key",
    "groq": "groq_api_key",
    "openrouter": "openrouter_api_key",
}

PROVIDER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    adapter: str
    base_url: str | None = None
    enabled: bool = True
    default_model: str | None = None
    position: int = 0
    #: Há chave gravada no banco? (o valor nunca entra no catálogo)
    has_db_key: bool = False
    #: Últimos 4 caracteres da chave do banco, para o humano conferir.
    key_hint: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    #: O id que vai para a API — com o sufixo ``@slug`` quando há pin de endpoint.
    id: str
    provider: str
    label: str = ""
    description: str = ""
    vision: bool = False
    #: Aparece no seletor do chat. ``False`` = o core roteia, o chat não oferece.
    in_selector: bool = False
    position: int = 0
    notes: str = ""
    #: UUID da linha no banco (``None`` na semente).
    row_id: str | None = None


@dataclass(frozen=True)
class Catalog:
    providers: tuple[ProviderSpec, ...]
    models: tuple[ModelSpec, ...]
    #: ``seed`` (tabelas ausentes, só leitura) | ``db``
    source: str = "seed"

    @cached_property
    def provider_by_name(self) -> dict[str, ProviderSpec]:
        return {p.name: p for p in sorted(self.providers, key=lambda p: p.position)}

    @cached_property
    def model_by_id(self) -> dict[str, ModelSpec]:
        return {m.id: m for m in self.models}

    @cached_property
    def models_by_provider(self) -> dict[str, list[ModelSpec]]:
        out: dict[str, list[ModelSpec]] = {name: [] for name in self.provider_by_name}
        for m in sorted(self.models, key=lambda m: (m.provider, m.position)):
            out.setdefault(m.provider, []).append(m)
        return out


def build_seed_catalog() -> Catalog:
    """O catálogo de antes do CRUD, montado a partir de ``catalog_seed``.

    Dentro de cada provider vêm primeiro os modelos do seletor (na ordem dele),
    depois os que só o core conhece, na ordem do mapa.
    """
    providers = tuple(
        ProviderSpec(
            name=name,
            label=label,
            adapter=name,
            default_model=seed.DEFAULT_MODEL_BY_PROVIDER.get(name),
            position=i,
        )
        for i, (name, label) in enumerate(seed.PROVIDERS)
    )
    models: list[ModelSpec] = []
    for name, _label in seed.PROVIDERS:
        shown = seed.SELECTOR.get(name, ())
        shown_ids = {mid for mid, _, _ in shown}
        for mid, label, desc in shown:
            models.append(
                ModelSpec(
                    id=mid,
                    provider=name,
                    label=label,
                    description=desc,
                    vision=mid in seed.VISION_MODELS,
                    in_selector=True,
                    position=len(models),
                )
            )
        for mid, owner in seed.MODEL_TO_PROVIDER.items():
            if owner == name and mid not in shown_ids:
                models.append(
                    ModelSpec(
                        id=mid,
                        provider=name,
                        label=mid,
                        vision=mid in seed.VISION_MODELS,
                        position=len(models),
                    )
                )
    return Catalog(providers=providers, models=tuple(models), source="seed")


_current: Catalog = build_seed_catalog()


def current() -> Catalog:
    return _current


def install(catalog: Catalog) -> None:
    """Troca o catálogo inteiro (atômico para quem lê)."""
    global _current
    _current = catalog


# ---------------------------------------------------------------------------
# Consultas — as mesmas regras que o router.py aplicava sobre os dicts fixos
# ---------------------------------------------------------------------------


def known_providers() -> tuple[str, ...]:
    """Providers cadastrados e ativos, na ordem do painel."""
    return tuple(p.name for p in _current.provider_by_name.values() if p.enabled)


def provider_label(name: str) -> str:
    spec = _current.provider_by_name.get(name)
    return spec.label if spec else name


def provider_for_model(model_name: str | None) -> str | None:
    """Return the provider that owns ``model_name`` (or None if unknown)."""
    if not model_name:
        return None
    spec = _current.model_by_id.get(model_name)
    return spec.provider if spec else None


def supports_vision(model_name: str | None) -> bool | None:
    """O modelo aceita imagem? ``None`` quando ele não está no catálogo.

    Os três estados importam. ``False`` é um NÃO conhecido (deepseek-flash) e
    justifica barrar a escolha; ``None`` é um modelo que o operador digitou à mão
    e sobre o qual não temos direito de opinar — deixa passar, e o fallback do
    driver cobre se der errado.
    """
    if not model_name:
        return None
    spec = _current.model_by_id.get(model_name)
    if spec is None:
        return None
    return spec.vision


def models_by_provider() -> dict[str, list[str]]:
    """Ids de modelo por provider (dropdowns da aba Modelos), na ordem do painel."""
    active = set(known_providers())
    return {
        name: [m.id for m in models]
        for name, models in _current.models_by_provider.items()
        if name in active
    }


def vision_models_by_provider() -> dict[str, list[str]]:
    """Catálogo só dos modelos COM visão, agrupado por provider (dropdown do painel)."""
    out: dict[str, list[str]] = {}
    for name, ids in models_by_provider().items():
        vision = [mid for mid in ids if _current.model_by_id[mid].vision]
        if vision:
            out[name] = vision
    return out


def default_model_for(provider: str | None) -> str | None:
    """O modelo padrão do provider (coluna ``default_model``)."""
    spec = _current.provider_by_name.get(provider or "")
    return spec.default_model if spec else None


def _provider_blurb(labels: list[str]) -> str:
    """'GPT-4.1, GPT-4.1 Mini...' — gerado, para não envelhecer como o texto fixo."""
    if not labels:
        return ""
    head = ", ".join(labels[:2])
    return f"{head}..." if len(labels) > 2 else head


def selector_payload(
    available: Iterable[str],
    default_provider: str | None,
    default_model: str | None = None,
) -> dict:
    """A vitrine do chat: só provider ativo, COM chave e com modelo no seletor.

    Provider sem chave fica fora: escolher um deles no chat era um turno que
    morria em "Provider não disponível". No painel ele continua visível, com aviso.

    ``default`` é o par do cliente sem escolha salva: o pedido (o cérebro, na
    rota) se estiver na vitrine; senão o ``default_model`` do provider; senão o
    1º dele. Não o 1º às cegas — no OpenRouter o 1º é o Grok 4.5, o mais caro.
    """
    usable = set(available)
    providers = []
    for spec in _current.provider_by_name.values():
        if not spec.enabled or spec.name not in usable:
            continue
        shown = [m for m in _current.models_by_provider.get(spec.name, []) if m.in_selector]
        if not shown:
            continue
        labels = [m.label or m.id for m in shown]
        providers.append(
            {
                "name": spec.name,
                "label": spec.label,
                "description": _provider_blurb(labels),
                "models": [
                    {
                        "id": m.id,
                        "label": m.label or m.id,
                        "description": m.description,
                        "vision": m.vision,
                    }
                    for m in shown
                ],
            }
        )
    default = None
    if providers:
        chosen = next((p for p in providers if p["name"] == default_provider), providers[0])
        shown_ids = [m["id"] for m in chosen["models"]]
        spec = _current.provider_by_name[chosen["name"]]
        model = next(
            (
                m
                for m in (default_model if chosen["name"] == default_provider else None, spec.default_model)
                if m in shown_ids
            ),
            shown_ids[0],
        )
        default = {"provider": chosen["name"], "model": model}
    return {"providers": providers, "default": default}


__all__ = [
    "ADAPTERS",
    "ENV_KEY_FIELD",
    "PROVIDER_NAME_RE",
    "AdapterSpec",
    "Catalog",
    "ModelSpec",
    "ProviderSpec",
    "build_seed_catalog",
    "current",
    "default_model_for",
    "install",
    "known_providers",
    "models_by_provider",
    "provider_for_model",
    "provider_label",
    "selector_payload",
    "supports_vision",
    "vision_models_by_provider",
]
