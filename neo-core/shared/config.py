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
    max_iterations: int = Field(default=10, ge=1, le=50, description="Max agent iterations")
    tool_timeout: int = Field(default=30, ge=1, description="Tool execution timeout (seconds)")

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
        default=0.78, ge=0.0, le=1.0,
        description="Cosine similarity threshold to detect topic change",
    )
    session_history_recovery_limit: int = Field(
        default=5, ge=1, description="Max previous sessions to search for context recovery",
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
