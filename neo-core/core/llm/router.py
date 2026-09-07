"""Router para seleção e gestão de LLM providers."""

from typing import Literal

from core.llm.claude_provider import ClaudeProvider
from core.llm.deepseek_provider import DeepSeekProvider
from core.llm.gemini_provider import GeminiProvider
from core.llm.grok_provider import GrokProvider
from core.llm.groq_provider import GroqProvider
from core.llm.ollama_provider import OllamaProvider
from core.llm.openai_provider import OpenAIProvider
from core.llm.openrouter_provider import OpenRouterProvider
from core.llm.provider import LLMProvider
from shared import LLMProviderError, get_settings

ProviderType = Literal[
    "openai", "anthropic", "ollama", "deepseek", "gemini", "grok", "groq", "openrouter"
]

# Mapeamento de nome de modelo → provider
_MODEL_TO_PROVIDER: dict[str, str] = {
    # OpenAI
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    # Família 4.1: visão + parâmetros clássicos do Chat Completions
    # (`max_tokens`/`temperature`, que é o que esta camada manda). É daqui que
    # sai o olho do navegador local — ver browser_local_vision_model.
    "gpt-4.1": "openai",
    "gpt-4.1-mini": "openai",
    "gpt-4.1-nano": "openai",
    "gpt-4-turbo": "openai",
    "gpt-4-turbo-preview": "openai",
    "gpt-3.5-turbo": "openai",
    "meta-llama/llama-4-scout-17b-16e-instruct": "openai",
    # Anthropic
    "claude-3-opus-20240229": "anthropic",
    "claude-3-sonnet-20240229": "anthropic",
    "claude-3-haiku-20240307": "anthropic",
    # DeepSeek
    "deepseek-v4-flash": "deepseek",
    "deepseek-v4-pro": "deepseek",
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
    # OpenRouter (openrouter.ai) — gateway unificado (IDs no formato vendor/modelo)
    "x-ai/grok-4.5": "openrouter",
    "x-ai/grok-4.20": "openrouter",
    "google/gemini-3.1-flash-lite": "openrouter",
    "google/gemini-2.0-flash-001": "openrouter",
    "anthropic/claude-3.5-sonnet": "openrouter",
    "meta-llama/llama-3.3-70b-instruct": "openrouter",
    "qwen/qwen3.5-flash-02-23": "openrouter",
    # É o que o subagente browser-use do servidor (container neo-browser-agent)
    # já usava, cravado no compose. Entrou no catálogo quando virou o role
    # `browser_agent` do painel — sem estar aqui, o valor em uso aparecia como
    # "custom" no dropdown, como se ninguém o tivesse escolhido.
    "google/gemini-2.5-flash": "openrouter",
    # Sufixo "@slug" = pin de provedor de infra, consumido pelo OpenRouterProvider
    # (ver _PROVIDER_PIN_SEP lá) e nunca enviado à API. O V4 Flash tem 17 endpoints
    # no gateway e eles NÃO são equivalentes; o do Baidu (baidu/fp8, sondado em
    # 2026-09-02) entrega 1M de contexto por $0,08386/$0,1677 por M in/out. Sem o
    # pin, o gateway escolheria outro a cada chamada, com outro preço e outro teto.
    # É texto puro (não lê imagem), então fica fora de _VISION_MODELS de propósito.
    "deepseek/deepseek-v4-flash@baidu": "openrouter",
}


# Modelos que aceitam imagem (content block `image_url`) — a capacidade que o
# olho do navegador local exige.
#
# Isto é DADO, não confiança em quem escolhe, porque escolher errado falha de um
# jeito silencioso e caro: a família DeepSeek responde
#     400 "This model does not support image"
# (sondado contra a API real em 2026-08-31, os dois modelos v4), e essa frase
# casa com `_VISION_FAILURE_IS_PERMANENT` no driver — ou seja, apontar a visão
# para o DeepSeek desliga a visão no PASSO 1 de todo job, sem erro visível, e a
# tarefa inteira sai às cegas. Foi assim que o job a0eab88c queimou 25 passos
# clicando no mesmo contêiner (ali por falta de crédito na OpenAI, mesmo efeito).
#
# Verificados com uma chamada real (2026-08-31): google/gemini-3.1-flash-lite
# respondeu 200; qwen/qwen3.5-flash-02-23 e x-ai/grok-4.5 recusaram só o PNG 1x1
# do teste ("image dimensions too small"), o que prova que leem imagem.
_VISION_MODELS: frozenset[str] = frozenset(
    {
        # OpenAI — a família 4.x lê imagem; a 3.5 não.
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4-turbo",
        "gpt-4-turbo-preview",
        # Anthropic
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
        # Gemini
        "gemini-2.5-pro-preview-03-25",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        # OpenRouter
        "google/gemini-3.1-flash-lite",
        "google/gemini-2.0-flash-001",
        "anthropic/claude-3.5-sonnet",
        "qwen/qwen3.5-flash-02-23",
        "x-ai/grok-4.5",
        "x-ai/grok-4.20",
        # input_modalities = [file, image, text, audio, video] (consultado na API
        # do gateway em 2026-09-02). O browser-use manda print a cada passo.
        "google/gemini-2.5-flash",
    }
)


#: Modelo padrão de cada provider — o que o agente usa quando ninguém escolheu.
#:
#: Fica no módulo (e não dentro de `get_default_model`) porque o painel precisa
#: EXIBIR este mesmo valor como "padrão do cérebro". Duplicar a tabela é como se
#: constrói uma tela que mostra um modelo e um runtime que usa outro.
DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "openai": "gpt-4-turbo-preview",
    "anthropic": "claude-3-opus-20240229",
    "ollama": "gemma4:e2b",
    "deepseek": "deepseek-v4-flash",
    "gemini": "gemini-2.0-flash",
    "grok": "grok-3-mini",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "google/gemini-3.1-flash-lite",
}


def provider_for_model(model_name: str | None) -> str | None:
    """Return the provider that owns ``model_name`` (or None if unknown)."""
    if not model_name:
        return None
    return _MODEL_TO_PROVIDER.get(model_name)


def supports_vision(model_name: str | None) -> bool | None:
    """O modelo aceita imagem? ``None`` quando ele não está no catálogo.

    Os três estados importam. ``False`` é um NÃO conhecido (deepseek-v4-flash) e
    justifica barrar a escolha; ``None`` é um modelo que o operador digitou à mão
    e sobre o qual não temos direito de opinar — deixa passar, e o fallback do
    driver cobre se der errado.
    """
    if not model_name:
        return None
    if model_name in _VISION_MODELS:
        return True
    if model_name in _MODEL_TO_PROVIDER:
        return False
    return None


def vision_models_by_provider() -> dict[str, list[str]]:
    """Catálogo só dos modelos COM visão, agrupado por provider (dropdown do painel)."""
    catalog: dict[str, list[str]] = {}
    for model, provider in _MODEL_TO_PROVIDER.items():
        if model in _VISION_MODELS:
            catalog.setdefault(provider, []).append(model)
    return catalog


def models_by_provider() -> dict[str, list[str]]:
    """Catalog of known model ids grouped by provider (for UI dropdowns).

    Inverts :data:`_MODEL_TO_PROVIDER`. Order within each provider follows the
    declaration order above. ``ollama`` models are local and depend on the host,
    so they are advertised but never the safe default for intent classification.
    """
    catalog: dict[str, list[str]] = {}
    for model, provider in _MODEL_TO_PROVIDER.items():
        catalog.setdefault(provider, []).append(model)
    return catalog


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

        # OpenRouter (openrouter.ai) — gateway unificado a vários vendors
        if self.settings.openrouter_api_key:
            self._providers["openrouter"] = OpenRouterProvider(
                api_key=self.settings.openrouter_api_key,
                base_url=self.settings.openrouter_base_url,
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
        return DEFAULT_MODEL_BY_PROVIDER.get(name, self.settings.default_model)
