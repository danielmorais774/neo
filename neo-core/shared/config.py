"""Configuração centralizada do Neo Core."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configurações globais do Neo Core."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # API Settings
    api_host: str = Field(default="0.0.0.0", description="API host")
    api_port: int = Field(default=8000, description="API port")
    grpc_port: int = Field(default=50051, description="gRPC port")
    api_workers: int = Field(default=4, description="Number of workers")
    api_reload: bool = Field(default=True, description="Auto-reload on code changes")

    # LLM Providers
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    deepseek_api_key: str | None = Field(default=None, description="DeepSeek API key")
    ollama_base_url: str = Field(
        default="http://localhost:11434", description="Ollama base URL"
    )

    # Default LLM Settings
    default_provider: Literal["openai", "anthropic", "ollama", "deepseek"] = Field(
        default="openai", description="Default LLM provider"
    )
    default_model: str = Field(default="gpt-4-turbo-preview", description="Default model")
    default_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    default_max_tokens: int = Field(default=4096, ge=1)

    # Agent Settings
    max_iterations: int = Field(default=32, ge=1, le=100, description="Max agent iterations")
    tool_timeout: int = Field(default=30, ge=1, description="Tool execution timeout (seconds)")
    agent_global_timeout: int = Field(
        default=300, ge=30,
        description="Global agent timeout in seconds",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://neo:neo@localhost:5432/neodb",
        description="Async SQLAlchemy database URL",
    )
    database_url_sync: str = Field(
        default="postgresql://neo:neo@localhost:5432/neodb",
        description="Sync SQLAlchemy/Alembic database URL",
    )

    # Embeddings
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model",
    )
    embedding_dimensions: int = Field(default=1536, description="Embedding vector size")

    # Session lifecycle
    session_inactivity_minutes: int = Field(
        default=30, ge=1, description="Minutes of inactivity before auto-closing a session"
    )
    session_topic_similarity_threshold: float = Field(
        default=0.45, ge=0.0, le=1.0,
        description="Cosine similarity threshold to detect topic change (lower = only extreme changes)",
    )
    session_rotation_cooldown_minutes: int = Field(
        default=5, ge=0,
        description="Minutes after last activity before topic rotation is even considered",
    )
    session_rotation_min_messages: int = Field(
        default=8, ge=1,
        description="Minimum messages in session before topic rotation is considered",
    )
    session_history_recovery_limit: int = Field(
        default=5, ge=1, description="Max previous sessions to search for context recovery",
    )

    # ── Context Compaction ────────────────────────────────────────
    max_context_tokens: int = Field(
        default=120_000, ge=1000,
        description="Token budget for conversation context",
    )
    compaction_threshold_ratio: float = Field(
        default=0.75, ge=0.3, le=0.95,
        description="Compact when tokens exceed this fraction of max_context_tokens",
    )
    compaction_chunk_size: int = Field(
        default=10, ge=2,
        description="Messages per chunk when summarizing during compaction",
    )

    # ── Hybrid Memory Search ─────────────────────────────────────
    fts_weight: float = Field(default=0.3, ge=0.0, le=1.0, description="Full-text search weight")
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0, description="Vector search weight")
    temporal_weight: float = Field(default=0.2, ge=0.0, le=1.0, description="Temporal decay weight")
    temporal_decay_days: int = Field(default=30, ge=1, description="Half-life for temporal decay")
    mmr_lambda: float = Field(default=0.7, ge=0.0, le=1.0, description="MMR diversity vs relevance")
    memory_search_limit: int = Field(default=20, ge=1, description="Results per memory search")

    # ── Loop Detection ────────────────────────────────────────────
    loop_max_identical_calls: int = Field(
        default=2, ge=1,
        description="Max identical tool calls before loop detection triggers",
    )
    loop_max_no_progress: int = Field(
        default=3, ge=2,
        description="Iterations without progress before detection triggers",
    )
    loop_circuit_breaker_limit: int = Field(
        default=50, ge=10,
        description="Total tool calls before circuit breaker triggers",
    )

    # MongoDB (Observability)
    mongo_url: str = Field(
        default="mongodb://localhost:27017",
        description="MongoDB connection URL for observability logs",
    )
    mongo_db: str = Field(
        default="neo_observability",
        description="MongoDB database name for observability",
    )

    # Security
    secret_key: str = Field(default="dev-secret-key", description="JWT secret key")
    algorithm: str = Field(default="HS256", description="JWT algorithm")
    access_token_expire_minutes: int = Field(default=30, ge=1)


@lru_cache
def get_settings() -> Settings:
    """Retorna instância singleton das configurações."""
    return Settings()
