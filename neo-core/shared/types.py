"""Tipos e modelos base compartilhados."""

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    """Roles de mensagens no chat."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class IntentType(str, Enum):
    """Tipo de intenção classificada pelo router."""

    DIRECT_RESPONSE = "direct_response"
    SERVER_TOOL = "server_tool"
    CLIENT_TOOL = "client_tool"
    WHATSAPP_MESSAGING = "whatsapp_messaging"


class MissionStatus(str, Enum):
    """Status de uma missão WhatsApp."""

    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    AWAITING_REPLY = "awaiting_reply"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NotificationKind(str, Enum):
    """Tipo de notificação push."""

    MISSION_STARTED = "mission_started"
    MISSION_UPDATE = "mission_update"
    MISSION_COMPLETED = "mission_completed"
    MISSION_FAILED = "mission_failed"
    APPROVAL_REQUIRED = "approval_required"
    CONFIRM_REPLY = "confirm_reply"
    UNSOLICITED_MESSAGE = "unsolicited_message"


class SessionStatus(str, Enum):
    """Status de uma sessão de conversa."""

    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


class ExecutionStatus(str, Enum):
    """Status de uma execução."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class StreamEventType(str, Enum):
    """Tipos de eventos no streaming LLM."""

    TOKEN = "token"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_ARGS = "tool_call_args"
    TOOL_CALL_END = "tool_call_end"
    DONE = "done"
    ERROR = "error"


class LoopType(str, Enum):
    """Tipos de loop detectados no agente."""

    GENERIC_REPEAT = "generic_repeat"
    PING_PONG = "ping_pong"
    NO_PROGRESS = "no_progress"
    CIRCUIT_BREAKER = "circuit_breaker"


# ---------------------------------------------------------------------------
# Message / Chat
# ---------------------------------------------------------------------------

class Message(BaseModel):
    """Mensagem no histórico de conversação."""

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list["ToolCall"] | None = None


# ---------------------------------------------------------------------------
# Tool types
# ---------------------------------------------------------------------------

class ToolParameter(BaseModel):
    """Parâmetro de uma tool."""

    type: str
    description: str
    enum: list[str] | None = None
    required: bool = False


class ToolSchema(BaseModel):
    """Schema completo de uma tool."""

    name: str
    description: str
    parameters: dict[str, ToolParameter]
    module: str
    execution: Literal["client-side", "server-side"] = "client-side"
    timeout: int = 30


class ToolCall(BaseModel):
    """Chamada de tool solicitada pelo LLM."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    """Resultado da execução de uma tool."""

    tool_call_id: str
    name: str
    result: Any
    error: str | None = None
    execution_time: float


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

class AgentStep(BaseModel):
    """Um passo na execução do agente."""

    step_number: int
    thought: str | None = None
    tool_calls: list[ToolCall]
    tool_results: list[ToolResult] = []
    timestamp: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Router / Intent
# ---------------------------------------------------------------------------

class RoutingDecision(BaseModel):
    """Decisão do IntentRouter sobre como processar uma mensagem.

    Extended with optional classifier metadata so the caller can persist the
    full classifier payload/response for debugging/observability.
    """

    intent: IntentType
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    explanation: str | None = None
    requires_approval: bool = False
    raw_command: str | None = None  # para CLIENT_TOOL (comando shell)
    needs_memory: bool = True  # whether cross-session memory recovery is useful
    knowledge_keywords: list[str] | None = None  # keywords for KG retrieval (classified by intent LLM)
    skill_name: str | None = None  # qual skill dispara (quando intent é uma skill)

    # --- complexity assessment (avoids separate should_create_plan LLM call) ---
    needs_plan: bool = False  # True when 2+ steps with dependencies are needed
    tipo_tarefa: str = "simples"  # simples | multi_etapas | analitica | criacao

    # --- classifier observability (optional) ---
    classifier_raw: str | None = None  # raw LLM/text response from classifier
    classifier_payload: dict[str, Any] | None = None  # messages/prompt sent to classifier


# ---------------------------------------------------------------------------
# LLM Streaming
# ---------------------------------------------------------------------------

class StreamChunk(BaseModel):
    """Chunk individual de streaming LLM."""

    event: StreamEventType
    content: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# Memory Search
# ---------------------------------------------------------------------------

class MemorySearchResult(BaseModel):
    """Resultado de uma busca na memória híbrida."""

    content: str
    source: Literal["session", "message"] = "message"
    similarity: float = 0.0
    fts_score: float = 0.0
    temporal_score: float = 0.0
    final_score: float = 0.0
    session_id: UUID | None = None
    message_id: UUID | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Context Compaction
# ---------------------------------------------------------------------------

class CompactionResult(BaseModel):
    """Resultado de uma compactação de contexto."""

    summary: str
    preserved_identifiers: list[str] = []
    original_token_count: int = 0
    compacted_token_count: int = 0
