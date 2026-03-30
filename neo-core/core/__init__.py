"""Core module - Agent engine."""

from core.agent import (
    AgentExecutor,
    ContextCompactor,
    ContextManager,
    LoopDetector,
    PromptBuilder,
    TokenCounter,
)
from core.llm import ClaudeProvider, LLMProvider, LLMRouter, OpenAIProvider
from core.memory import ConversationMemory, SessionManager
from core.memory.hybrid_search import HybridMemorySearch
from core.router import IntentRouter
from core.session import EmbeddingService, PersistentSessionManager, PreferenceExtractor
from core.tools import ToolExecutor, ToolRegistry, ToolValidator

__all__ = [
    # Agent
    "AgentExecutor",
    "ContextCompactor",
    "ContextManager",
    "LoopDetector",
    "PromptBuilder",
    "TokenCounter",
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
    "PreferenceExtractor",
    # Memory
    "HybridMemorySearch",
    # Memory (legacy)
    "ConversationMemory",
    "SessionManager",
    # Tools
    "ToolRegistry",
    "ToolValidator",
    "ToolExecutor",
]
