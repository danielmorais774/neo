"""Router para seleção e gestão de LLM providers."""

import hashlib
import logging
from typing import Literal

from core.llm import catalog
from core.llm.catalog import (  # noqa: F401 — reexportados: o resto do core importa daqui
    ADAPTERS,
    ENV_KEY_FIELD,
    ProviderSpec,
    default_model_for,
    models_by_provider,
    provider_for_model,
    supports_vision,
    vision_models_by_provider,
)
from core.llm.claude_provider import ClaudeProvider
from core.llm.compatible_provider import OpenAICompatibleProvider
from core.llm.deepseek_provider import DeepSeekProvider
from core.llm.gemini_provider import GeminiProvider
from core.llm.grok_provider import GrokProvider
from core.llm.groq_provider import GroqProvider
from core.llm.ollama_provider import OllamaProvider
from core.llm.openai_provider import OpenAIProvider
from core.llm.openrouter_provider import OpenRouterProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, get_settings

logger = logging.getLogger(__name__)

# Os providers DE FÁBRICA. O catálogo (core/llm/catalog.py, editado no painel)
# aceita outros nomes; isto é só tipagem dos chamadores que pedem um destes.
ProviderType = Literal[
    "openai", "anthropic", "ollama", "deepseek", "gemini", "grok", "groq", "openrouter"
]

# O catálogo e as consultas sobre ele (`provider_for_model`, `supports_vision`…)
# moravam aqui como dicts fixos. Hoje vêm de core/llm/catalog.py — banco, ou a
# semente de core/llm/catalog_seed.py enquanto as tabelas não existem.

_ADAPTER_CLASSES: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": ClaudeProvider,
    "deepseek": DeepSeekProvider,
    "gemini": GeminiProvider,
    "grok": GrokProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


class LLMRouter:
    """Gerencia múltiplos providers e roteia requisições."""

    def __init__(self) -> None:
        """Inicializa o router com providers configurados."""
        self.settings = get_settings()
        self._providers: dict[str, LLMProvider] = {}
        # Impressão digital (adapter, base_url, chave) de cada instância — o
        # `rebuild` só recria o provider cuja config mudou, e o resto mantém o
        # pool keep-alive (e o cache de prefixo do endpoint).
        self._fingerprints: dict[str, str] = {}
        self.rebuild()

    def rebuild(self, db_keys: dict[str, str] | None = None) -> None:
        """(Re)monta os providers a partir do catálogo atual.

        Chamado na subida (catálogo da semente, chaves do .env) e de novo a cada
        gravação no painel, com as chaves decifradas do banco em ``db_keys``.
        Troca o dict INTEIRO no fim: um turno em andamento segura a instância
        que já pegou, e o próximo `get_provider` vê o catálogo novo. Nenhum
        serviço guarda a instância desde o boot (todos chamam `get_provider*` por
        chamada), então trocar aqui vale para o sistema todo sem reiniciar.
        """
        db_keys = db_keys or {}
        built: dict[str, LLMProvider] = {}
        prints: dict[str, str] = {}
        for spec in catalog.current().provider_by_name.values():
            if not spec.enabled:
                continue
            adapter = ADAPTERS.get(spec.adapter)
            if adapter is None:
                logger.warning("Provider '%s': adapter desconhecido '%s'", spec.name, spec.adapter)
                continue
            key = self._resolve_key(spec, db_keys)
            if adapter.needs_key and not key:
                continue
            fingerprint = hashlib.sha256(
                f"{spec.adapter}|{spec.base_url or ''}|{key or ''}".encode()
            ).hexdigest()
            instance = (
                self._providers.get(spec.name)
                if self._fingerprints.get(spec.name) == fingerprint
                else None
            )
            if instance is None:
                try:
                    instance = self._instantiate(spec, key)
                except Exception as e:  # config ruim de UM provider não derruba os outros
                    logger.error("Provider '%s' não pôde ser criado: %s", spec.name, e)
                    continue
            # Só onde a classe já tem a noção de modelo padrão (OpenAI/Anthropic
            # caem num id fixo dentro do `complete`, e o padrão deles chega pelo
            # `get_default_model`).
            if spec.default_model and hasattr(instance, "model"):
                instance.model = spec.default_model
            built[spec.name] = instance
            prints[spec.name] = fingerprint
        self._providers = built
        self._fingerprints = prints
        self._apply_default_model()

    def _resolve_key(self, spec: ProviderSpec, db_keys: dict[str, str]) -> str | None:
        """Chave do painel vence; a do .env é a reserva dos providers de fábrica.

        A mesma regra mora em `modules.llm_catalog.store._key_source`, que diz ao
        painel DE ONDE veio a chave.
        """
        key = db_keys.get(spec.name)
        if key:
            return key
        field = ENV_KEY_FIELD.get(spec.name)
        # Só com o adapter de fábrica: "openai" recriado como openai_compatible
        # apontando para outro servidor não pode levar a chave da OpenAI junto.
        if field and spec.adapter == spec.name:
            env_key = getattr(self.settings, field, None)
            if env_key:
                return env_key
        return None

    def _instantiate(self, spec: ProviderSpec, key: str | None) -> LLMProvider:
        if spec.adapter == "ollama":
            return OllamaProvider(base_url=spec.base_url or self.settings.ollama_base_url)
        if spec.adapter == "openrouter":
            return OpenRouterProvider(
                api_key=key, base_url=spec.base_url or self.settings.openrouter_base_url
            )
        if spec.adapter == "openai_compatible":
            return OpenAICompatibleProvider(
                api_key=key, base_url=spec.base_url, model=spec.default_model
            )
        return _ADAPTER_CLASSES[spec.adapter](api_key=key)

    def _apply_default_model(self) -> None:
        """Faz o provider DEFAULT honrar o ``DEFAULT_MODEL`` do env.

        Sem isto o ``DEFAULT_MODEL`` não valia para ninguém: cada provider nasce
        com o modelo de fábrica DELE (``OpenRouterProvider`` → gemini-flash-lite),
        e quem chama ``get_provider()`` sem nome — o `PlanManager`, o extrator de
        preferências, o compactador de contexto — não passa modelo. O operador
        escrevia ``DEFAULT_MODEL=…`` no ``.env`` e o sistema usava outra coisa
        (ver neo-llm-slots-and-wallets: o modelo real do cérebro nunca foi este).

        Só mexe no provider default, e só quando o catálogo não desmente o par:
        modelo declaradamente de OUTRO provider é engano de configuração, e
        aplicá-lo trocaria um 402 por um 404. Modelo fora do catálogo passa — o
        OpenRouter tem muito mais ids do que a lista conhece, inclusive com o
        sufixo ``@slug`` de pin de endpoint, que não é outro modelo.
        """
        padrao = self._providers.get(self.settings.default_provider)
        modelo = (self.settings.default_model or "").strip()
        if padrao is None or not modelo:
            return
        dono = provider_for_model(modelo.split("@", 1)[0])
        if dono is not None and dono != self.settings.default_provider:
            logger.warning(
                "DEFAULT_MODEL=%r é do provider '%s', não de '%s' — ignorando o par.",
                modelo, dono, self.settings.default_provider,
            )
            return
        padrao.model = modelo

    def get_provider(self, provider_name: ProviderType | str | None = None) -> LLMProvider:
        """
        Retorna um provider específico ou o default.

        Args:
            provider_name: Nome do provider desejado

        Returns:
            Instância do LLMProvider

        Raises:
            LLMProviderError: Se provider não disponível
        """
        name = provider_name or self.settings.default_provider

        if name not in self._providers:
            raise LLMProviderError(
                f"Provider '{name}' não disponível. Verifique configurações.",
                details={"provider": name, "available": list(self._providers.keys())},
            )

        return self._providers[name]

    def list_available_providers(self) -> list[str]:
        """Lista providers disponíveis."""
        return list(self._providers.keys())

    def get_provider_for_model(self, model_name: str | None) -> LLMProvider:
        """Retorna o provider correto para um nome de modelo.

        Usa o catálogo (`provider_for_model`) para inferir o provider.
        Fallback para o provider default se o modelo não for reconhecido.
        """
        if model_name:
            provider_name = provider_for_model(model_name)
            if provider_name and provider_name in self._providers:
                return self._providers[provider_name]
        return self.get_provider()

    def get_default_model(self, provider_name: ProviderType | None = None) -> str:
        """
        Retorna o modelo padrão para um provider.

        Args:
            provider_name: Nome do provider

        Returns:
            Nome do modelo padrão
        """
        name = provider_name or self.settings.default_provider
        return default_model_for(name) or self.settings.default_model
