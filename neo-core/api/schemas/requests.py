"""Request/Response schemas para a API."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from shared.types import MessageType, MissionCommand


LLMProviderName = Literal[
    "openai", "anthropic", "ollama", "deepseek", "gemini", "grok", "groq", "openrouter"
]


class DesktopChatMessage(BaseModel):
    """Structured desktop chat message.

    USER_INPUT is natural language and belongs on the normal AI path.
    MISSION_COMMAND is an explicit UI command and must bypass intent routing.
    """

    type: MessageType = Field(default=MessageType.USER_INPUT)
    user_message: str | None = None
    session_id: UUID | None = None
    provider: LLMProviderName | None = None
    model: str | None = None
    command: MissionCommand | None = None
    mission_id: UUID | None = None
    edited_text: str | None = None
    user_id: UUID | None = None
    # Ids do File Store (neo-core) anexados a este turno — ver file-store-plan.md §5.
    attachments: list[str] | None = None
    # Caracteres desta mensagem que vieram do CLIPBOARD. Acima do limiar o turno é
    # marcado como contaminado (fonte `colagem`): texto colado é conteúdo de
    # TERCEIRO, e sem esta marca o portão de liberação ficava inerte justamente na
    # entrada mais fácil de injetar. Quem sabe é o cliente — o core só vê o texto
    # final. Ver docs/release-request-plan.md §21.
    pasted_chars: int | None = Field(default=None, ge=0)


class ExecuteTaskRequest(BaseModel):
    """Request para executar uma task."""

    task: str = Field(..., description="Descrição da task a executar")
    session_id: UUID | None = Field(None, description="ID da sessão (opcional)")
    provider: LLMProviderName | None = Field(
        None, description="Provider LLM a usar"
    )
    model: str | None = Field(None, description="Modelo específico")


class ToolResultSchema(BaseModel):
    """Schema de resultado de tool."""

    tool_call_id: str
    name: str
    result: Any
    error: str | None = None
    execution_time: float


class AgentStepSchema(BaseModel):
    """Schema de um step do agente."""

    step_number: int
    thought: str | None = None
    tool_calls: list[dict[str, Any]]
    tool_results: list[ToolResultSchema]


class ExecuteTaskResponse(BaseModel):
    """Response da execução de task."""

    status: str
    final_response: str | None
    steps: list[AgentStepSchema]
    iterations: int
    session_id: str | None


class ToolInfoSchema(BaseModel):
    """Schema de informações de uma tool."""

    name: str
    description: str
    module: str
    execution: str
    parameters: dict[str, Any]


class ListToolsResponse(BaseModel):
    """Response da listagem de tools."""

    tools: list[ToolInfoSchema]
    count: int


class SessionHistoryResponse(BaseModel):
    """Response do histórico de sessão."""

    session_id: str
    created_at: str
    updated_at: str
    message_count: int
    step_count: int
    messages: list[dict[str, str]]


class ErrorResponse(BaseModel):
    """Response de erro."""

    error: str
    details: dict[str, Any] | None = None
