"""Exceções customizadas do Neo Core."""


class NeoError(Exception):
    """Base exception para erros do Neo."""

    def __init__(self, message: str, details: dict[str, any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class LLMProviderError(NeoError):
    """Erro relacionado a provider de LLM."""

    pass


class ToolExecutionError(NeoError):
    """Erro na execução de uma tool."""

    pass


class ToolNotFoundError(NeoError):
    """Tool não encontrada no registry."""

    pass


class ToolValidationError(NeoError):
    """Erro de validação de parâmetros de tool."""

    pass


class AgentIterationLimitError(NeoError):
    """Limite de iterações do agente atingido."""

    pass


class AgentTimeoutError(NeoError):
    """Timeout na execução do agente."""

    pass
