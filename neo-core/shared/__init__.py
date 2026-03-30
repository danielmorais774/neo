"""Shared utilities module."""

from shared.config import Settings, get_settings
from shared.exceptions import (
    AgentIterationLimitError,
    AgentLoopDetectedError,
    AgentTimeoutError,
    ContextOverflowError,
    LLMProviderError,
    NeoError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)
from shared.types import (
    AgentStep,
    CompactionResult,
    ExecutionStatus,
    IntentType,
    LoopType,
    MemorySearchResult,
    Message,
    MessageRole,
    RoutingDecision,
    SessionStatus,
    StreamChunk,
    StreamEventType,
    ToolCall,
    ToolParameter,
    ToolResult,
    ToolSchema,
)

__all__ = [
    # Config
    "Settings",
    "get_settings",
    # Types
    "Message",
    "MessageRole",
    "ToolParameter",
    "ToolSchema",
    "ToolCall",
    "ToolResult",
    "AgentStep",
    "ExecutionStatus",
    "IntentType",
    "LoopType",
    "RoutingDecision",
    "SessionStatus",
    "StreamChunk",
    "StreamEventType",
    "MemorySearchResult",
    "CompactionResult",
    # Exceptions
    "NeoError",
    "LLMProviderError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolValidationError",
    "AgentIterationLimitError",
    "AgentTimeoutError",
    "AgentLoopDetectedError",
    "ContextOverflowError",
]
