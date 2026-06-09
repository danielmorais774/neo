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
    gemini_api_key: str | None = Field(default=None, description="Google Gemini API key")
    grok_api_key: str | None = Field(default=None, description="xAI Grok API key")
    groq_api_key: str | None = Field(default=None, description="Groq API key (groq.com)")
    ollama_base_url: str = Field(
        default="http://neo-ollama:11434", description="Ollama base URL"
    )

    # Default LLM Settings
    default_provider: Literal["openai", "anthropic", "ollama", "deepseek", "gemini", "grok", "groq"] = Field(
        default="openai", description="Default LLM provider"
    )
    default_model: str = Field(default="gpt-4-turbo-preview", description="Default model")
    default_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    default_max_tokens: int = Field(default=8192, ge=1)

    # Agent Settings
    max_iterations: int = Field(default=32, ge=1, le=100, description="Max agent iterations")
    tool_timeout: int = Field(default=30, ge=1, description="Tool execution timeout (seconds)")
    agent_global_timeout: int = Field(
        default=300, ge=30,
        description="Global agent timeout in seconds",
    )

    # Pipeline orchestrator (chaining of feature pipelines — cascade/handoff)
    pipeline_orchestrator_enabled: bool = Field(
        default=True,
        description="Route intent dispatch through PipelineOrchestrator (Phase 1+). "
        "Set false for instant rollback to the legacy inline if/elif dispatch.",
    )
    pipeline_handoff_enabled: bool = Field(
        default=True,
        description="Phase 2: let the cognitive core (DIRECT_RESPONSE) emit "
        "[[HANDOFF:skill]] blocks that chain a follow-up pipeline. Requires "
        "pipeline_orchestrator_enabled. Set false to disable runtime handoffs.",
    )
    pipeline_cascade_enabled: bool = Field(
        default=True,
        description="Phase 4: expand the classifier's declared pipeline_plan into "
        "a multi-step cascade. Set false to ignore pipeline_plan (single step).",
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
    embedding_provider: Literal["local", "openai"] = Field(
        default="local",
        description="Provider de embeddings: 'local' (sentence-transformers) ou 'openai' (API remota)",
    )
    embedding_local_model: str = Field(
        default="all-MiniLM-L6-v2",
        description="Modelo local sentence-transformers (usado quando embedding_provider=local)",
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model (usado quando embedding_provider=openai)",
    )
    embedding_dimensions: int = Field(default=384, description="Embedding vector size (384 para all-MiniLM-L6-v2 local, 1536 para OpenAI)")

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

    # ── Context Selector budgets (tokens por camada) ──────────────
    context_budget_neo_core_ctx: int = Field(
        default=600, ge=0,
        description="Token budget for contextual Neo core items (examples, lessons, projects)",
    )
    context_budget_user_prefs: int = Field(
        default=500, ge=0,
        description="Token budget for user preferences",
    )
    context_budget_user_pks: int = Field(
        default=900, ge=0,
        description="Token budget for user PKS knowledge entities",
    )
    context_budget_retrieved: int = Field(
        default=1200, ge=0,
        description="Token budget for cross-session retrieved memories",
    )
    context_relevance_threshold: float = Field(
        default=0.25, ge=0.0, le=1.0,
        description="Minimum relevance score for non-pinned candidates to enter the prompt",
    )

    # Panel auth (temporary without login flow)
    panel_jwt_secret: str = Field(
        default="l5L0mJ30C7v10OTI4eEOvxShOGwhGkPSEst7eicOiFGq8qG6TgL9u4CQUr6mByjV",
        description="Shared secret used to validate neo-panel JWT tokens (HS256)",
    )
    panel_jwt_subject: str = Field(
        default="neo-panel",
        description="Expected JWT subject for neo-panel bearer tokens",
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

    # WhatsApp Gateway
    whatsapp_gateway_url: str = Field(
        default="http://localhost:3030",
        description="neo-whatsapp HTTP gateway URL",
    )
    whatsapp_webhook_secret: str = Field(
        default="dev-secret",
        description="HMAC secret for inbound webhook validation",
    )
    whatsapp_webhook_path: str = Field(
        default="/api/v1/whatsapp/webhook",
        description="Webhook path for inbound WhatsApp messages",
    )

    # Home Assistant
    home_assistant_url: str = Field(
        default="http://localhost:8123",
        description="Base URL da instância Home Assistant",
    )
    home_assistant_token: str | None = Field(
        default=None,
        description="Long-lived access token do Home Assistant (Bearer)",
    )
    home_assistant_timeout: int = Field(
        default=10,
        description="Timeout HTTP (s) para chamadas ao Home Assistant",
    )

    # Mission Settings
    mission_default_timeout_hours: int = Field(
        default=24, ge=1,
        description="Default timeout for a mission before it's marked failed",
    )
    mission_max_concurrent: int = Field(
        default=8, ge=1,
        description="Maximum concurrent missions across all users",
    )

    # PKS bootstrap
    pks_system_state_file: str = Field(
        default="/tmp/neo-core-dev/pks-system-bootstrap.json",
        description="Local state file used to persist Neo system PKS bootstrap IDs",
    )


@lru_cache
def get_settings() -> Settings:
    """Retorna instância singleton das configurações."""
    return Settings()
