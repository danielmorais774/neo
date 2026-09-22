"""Tipos e modelos base compartilhados."""

from dataclasses import dataclass, field
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


class MessageType(str, Enum):
    """Tipo de mensagem no protocolo de chat."""

    USER_INPUT = "user_input"
    MISSION_COMMAND = "mission_command"
    SYSTEM_EVENT = "system_event"


class IntentType(str, Enum):
    """Tipo de intenção classificada pelo router."""

    # The cognitive core (the "cérebro"): the path that reasons over the
    # PKS/context and may hand off to skills. Canonical name + value as of the
    # Phase 5 migration.
    COGNITIVE = "cognitive"
    SERVER_TOOL = "server_tool"
    CLIENT_TOOL = "client_tool"
    WHATSAPP_MESSAGING = "whatsapp_messaging"
    HOME_ASSISTANT = "home_assistant"
    # Music playback (Spotify/YouTube). NOT a router fast-path target: music
    # requests route to COGNITIVE, and the cognitive core hands off here so PKS
    # behavioral rules (taste, refusals) always apply. See neo-music-feature.
    MUSIC = "music"

    # Backward-compat alias: the old name/value was "direct_response". It now
    # resolves to COGNITIVE (value "cognitive"), so the ~60 existing references and
    # any client sending force_intent="DIRECT_RESPONSE" keep working. Historical
    # trace/DB rows that stored the literal "direct_response" are display-only —
    # nothing reconstructs IntentType from that string — so no data migration runs.
    DIRECT_RESPONSE = "cognitive"


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
    # Imagens que acompanham ESTA mensagem: JPEG/PNG em base64 puro (sem o
    # prefixo `data:`) ou já como data URL. Quem monta o corpo multimodal é o
    # provider — aqui só carona. Só faz sentido em mensagens de usuário e só
    # com modelo de visão; provider que ignora o campo continua funcionando
    # com o texto (é por isso que `content` nunca vem vazio quando há imagem).
    images: list[str] | None = None


# ---------------------------------------------------------------------------
# Tool types
# ---------------------------------------------------------------------------

class ToolParameter(BaseModel):
    """Parâmetro de uma tool."""

    type: str
    description: str
    enum: list[str] | None = None
    required: bool = False
    #: Schema dos elementos quando ``type="array"``. Sem ele, provedor estrito
    #: recusa a tool inteira com 400 — e uma tool que vai em todo turno cognitivo
    #: derrubaria todo turno (edit_user_file.edits).
    items: dict | None = None


class ToolSchema(BaseModel):
    """Schema completo de uma tool."""

    name: str
    description: str
    parameters: dict[str, ToolParameter]
    module: str
    execution: Literal["client-side", "server-side"] = "client-side"
    timeout: int = 30
    #: Linha curta usada SÓ no catálogo do classificador de intenção
    #: (``IntentRouter._format_tools``). A ``description`` completa continua
    #: indo para a API como schema da função — é ela que ensina o núcleo
    #: cognitivo a USAR a tool. O classificador tem outro trabalho: só precisa
    #: RECONHECER a tool, e paga a descrição inteira em toda mensagem do
    #: usuário. Sem isto, encurtar para o router significava enfraquecer o
    #: cérebro. Vazio → cai na ``description``.
    router_summary: str | None = None


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

class PipelineStepSpec(BaseModel):
    """One step of a declared cascade (Phase 4 — Modo B).

    The classifier emits a ``pipeline_plan`` of these when a message contains
    multiple INDEPENDENT actions ("apaga as luzes E manda um whatsapp"). Each spec
    is expanded into a ``PipelineStep`` by the orchestrator seed.
    """

    intent: IntentType
    input_text: str
    skill_name: str | None = None


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

    # Hint for the cognitive core: a deterministic guard detected that this turn is
    # about a skill the brain should CONSIDER handing off to (e.g. "music"), but the
    # decision stays with the cognitive core (it reads the PKS and may refuse). Unlike
    # ``skill_name``, this never fires a skill directly. See neo-music-feature.
    skill_hint: str | None = None

    # --- speech act (Workstream B — bias to brain) ---
    # imperative | question | conditional | definition | chitchat.
    # A side-effecting skill (HA/WhatsApp) is only fired on a confident imperative;
    # everything else routes to the cognitive core.
    speech_act: str = "imperative"

    # --- situational register signals (docs/context-register-plan.md) ---
    # gravity: how serious the SITUATION is (not the task's complexity).
    # normal | alta | critica — a deterministic regex floor guarantees "alta"
    # for live-problem messages even when the classifier misses it.
    gravity: str = "normal"
    # register_hint: weak LLM signal, only honored for registers whose
    # deterministic cues are rare: "protocolo" (third parties will see the
    # reply) or "confidente" (the user is venting). None otherwise.
    register_hint: str | None = None

    # --- complexity assessment (avoids separate should_create_plan LLM call) ---
    needs_plan: bool = False  # True when 2+ steps with dependencies are needed
    tipo_tarefa: str = "simples"  # simples | multi_etapas | analitica | criacao

    # --- Phase 4: declared cascade (Modo B) ---
    # When the message has 2+ INDEPENDENT actions, the classifier fills this with
    # the ordered steps; the orchestrator runs them as a chain. None → single step.
    pipeline_plan: list[PipelineStepSpec] | None = None

    # --- classifier observability (optional) ---
    classifier_raw: str | None = None  # raw LLM/text response from classifier
    classifier_payload: dict[str, Any] | None = None  # messages/prompt sent to classifier


# ---------------------------------------------------------------------------
# Mission command
# ---------------------------------------------------------------------------

class MissionCommand(str, Enum):
    """Comandos estruturados para missões."""

    APPROVE = "approve"
    CANCEL = "cancel"
    EDIT = "edit"
    RETRY = "retry"


class MissionCommandPayload(BaseModel):
    """Payload de comando estruturado para missão."""

    command: MissionCommand
    mission_id: UUID
    edited_text: str | None = None
    user_id: UUID | None = None


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


# ---------------------------------------------------------------------------
# Context Selection
# ---------------------------------------------------------------------------

class ContextLayer(str, Enum):
    """Camada de contexto no prompt do Neo."""

    CORE            = "core"           # identidade, idioma, regras — pin=True sempre
    RUNTIME         = "runtime"        # OS, paths, shell — pin=True
    NEO_CORE_PINNED = "neo_core_pinned"  # personality, response-rules críticas — pin=True
    NEO_REGISTER    = "neo_register"    # registro da situação (teto de tom) — pin=True
    NEO_AFFECT      = "neo_affect"      # estado emocional (vetor límbico) do Neo com o usuário — pin=True
    WORKING_MEM     = "working_mem"    # índice do caderno da tarefa atual — scored
    NEO_CORE_CTX    = "neo_core_ctx"   # exemplos, lições, projetos — scored
    USER_PREFS      = "user_prefs"     # preferências do usuário — scored
    USER_PKS        = "user_pks"       # entidades PKS do usuário — scored
    SESSION         = "session"        # histórico recente (gerenciado à parte)
    RETRIEVED       = "retrieved"      # conversas anteriores — scored


@dataclass
class ContextCandidate:
    """Candidato a inclusão no prompt, com score e trace de seleção."""

    layer: ContextLayer
    source: str        # "hardcoded" | "pks_system" | "pks_user" | "prefs" | "hybrid_search"
    key: str           # rule_key / entity_id / "base" / "runtime" / etc.
    pin: bool          # True = entra sempre, ignora score/budget
    text: str
    est_tokens: int    # estimativa de tokens antes de incluir
    relevance: float   # 0..1
    final_score: float = 0.0  # após ponderação
    included: bool = False
    skip_reason: str = ""
    #: Diagnóstico da camada (ex.: o "explain" do ranking do PKS) para o session log.
    detail: dict | None = None
