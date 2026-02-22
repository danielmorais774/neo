"""Router para seleção e gestão de LLM providers."""

from typing import Literal

from core.llm.claude_provider import ClaudeProvider
from core.llm.deepseek_provider import DeepSeekProvider
from core.llm.openai_provider import OpenAIProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, get_settings

ProviderType = Literal["openai", "anthropic", "ollama", "deepseek"]


class LLMRouter:
    """Gerencia múltiplos providers e roteia requisições."""

    def __init__(self) -> None:
        """Inicializa o router com providers configurados."""
        self.settings = get_settings()
        self._providers: dict[str, LLMProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        """Inicializa providers baseado nas configurações."""
        # OpenAI
        if self.settings.openai_api_key:
            self._providers["openai"] = OpenAIProvider(
                api_key=self.settings.openai_api_key
            )

        # Anthropic
        if self.settings.anthropic_api_key:
            self._providers["anthropic"] = ClaudeProvider(
                api_key=self.settings.anthropic_api_key
            )

        # DeepSeek
        if self.settings.deepseek_api_key:
            self._providers["deepseek"] = DeepSeekProvider(
                api_key=self.settings.deepseek_api_key
            )

        # Ollama (futuro)
        # if self.settings.ollama_base_url:
        #     self._providers["ollama"] = OllamaProvider(
        #         base_url=self.settings.ollama_base_url
        #     )

    def get_provider(self, provider_name: ProviderType | None = None) -> LLMProvider:
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

    def get_default_model(self, provider_name: ProviderType | None = None) -> str:
        """
        Retorna o modelo padrão para um provider.

        Args:
            provider_name: Nome do provider

        Returns:
            Nome do modelo padrão
        """
        name = provider_name or self.settings.default_provider

        # Modelos padrão por provider
        defaults = {
            "openai": "gpt-4-turbo-preview",
            "anthropic": "claude-3-opus-20240229",
            "ollama": "llama2",
        }

        return defaults.get(name, self.settings.default_model)
