"""Core module - Agent engine."""

from core.agent import AgentExecutor, ContextManager, PromptBuilder
from core.llm import ClaudeProvider, LLMProvider, LLMRouter, OpenAIProvider
from core.memory import ConversationMemory, SessionManager
from core.router import IntentRouter
from core.session import EmbeddingService, PersistentSessionManager
from core.tools import ToolExecutor, ToolRegistry, ToolValidator

__all__ = [
    # Agent
    "AgentExecutor",
    "PromptBuilder",
    "ContextManager",
    # LLM
    "LLMProvider",
    "LLMRouter",
    "OpenAIProvider",
    "ClaudeProvider",
    # Router
    "IntentRouter",
    # Session
    "PersistentSessionManager",
    "EmbeddingService",
    # Tools
    "ToolRegistry",
    "ToolValidator",
    "ToolExecutor",
    # Memory (legacy)
    "ConversationMemory",
    "SessionManager",
]
