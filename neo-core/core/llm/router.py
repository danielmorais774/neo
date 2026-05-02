"""Router para seleção e gestão de LLM providers."""

from typing import Literal

from core.llm.claude_provider import ClaudeProvider
from core.llm.deepseek_provider import DeepSeekProvider
from core.llm.gemini_provider import GeminiProvider
from core.llm.grok_provider import GrokProvider
from core.llm.groq_provider import GroqProvider
from core.llm.ollama_provider import OllamaProvider
from core.llm.openai_provider import OpenAIProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, get_settings

ProviderType = Literal["openai", "anthropic", "ollama", "deepseek", "gemini", "grok", "groq"]

# Mapeamento de nome de modelo → provider
_MODEL_TO_PROVIDER: dict[str, str] = {
    # OpenAI
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "gpt-4-turbo": "openai",
    "gpt-4-turbo-preview": "openai",
    "gpt-3.5-turbo": "openai",
    "meta-llama/llama-4-scout-17b-16e-instruct": "openai",
    # Anthropic
    "claude-3-opus-20240229": "anthropic",
    "claude-3-sonnet-20240229": "anthropic",
    "claude-3-haiku-20240307": "anthropic",
    # DeepSeek
    "deepseek-chat": "deepseek",
    "deepseek-reasoner": "deepseek",
    # Gemini
    "gemini-2.5-pro-preview-03-25": "gemini",
    "gemini-2.0-flash": "gemini",
    "gemini-2.0-flash-lite": "gemini",
    "gemini-1.5-pro": "gemini",
    "gemini-1.5-flash": "gemini",
    # Ollama (modelos locais)
    "gemma4:e2b": "ollama",
    "gemma4:12b": "ollama",
    "llama3": "ollama",
    "llama3:8b": "ollama",
    "llama3:70b": "ollama",
    "mistral": "ollama",
    "phi4": "ollama",
    # Grok (xAI) — free plan
    "grok-3-mini": "grok",
    "grok-3-mini-fast": "grok",
    "grok-2-1212": "grok",
    # Grok (xAI) — paid
    "grok-3": "grok",
    "grok-3-fast": "grok",
    # Groq (groq.com) — ultra-fast LPU inference
    "llama-3.3-70b-versatile": "groq",
    "llama-3.1-8b-instant": "groq",
    "llama3-70b-8192": "groq",
    "mixtral-8x7b-32768": "groq",
    "gemma2-9b-it": "groq",
}


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

        # Gemini
        if self.settings.gemini_api_key:
            self._providers["gemini"] = GeminiProvider(
                api_key=self.settings.gemini_api_key
            )

        # Grok (xAI)
        if self.settings.grok_api_key:
            self._providers["grok"] = GrokProvider(
                api_key=self.settings.grok_api_key
            )

        # Groq (groq.com) — ultra-fast LPU
        if self.settings.groq_api_key:
            self._providers["groq"] = GroqProvider(
                api_key=self.settings.groq_api_key
            )

        # Ollama — sempre disponível (não requer API key)
        self._providers["ollama"] = OllamaProvider(
            base_url=self.settings.ollama_base_url,
        )

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

    def get_provider_for_model(self, model_name: str | None) -> LLMProvider:
        """Retorna o provider correto para um nome de modelo.

        Usa o mapeamento _MODEL_TO_PROVIDER para inferir o provider.
        Fallback para o provider default se o modelo não for reconhecido.
        """
        if model_name:
            provider_name = _MODEL_TO_PROVIDER.get(model_name)
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

        # Modelos padrão por provider
        defaults = {
            "openai": "gpt-4-turbo-preview",
            "anthropic": "claude-3-opus-20240229",
            "ollama": "gemma4:e2b",
            "deepseek": "deepseek-chat",
            "gemini": "gemini-2.0-flash",
        }

        return defaults.get(name, self.settings.default_model)
