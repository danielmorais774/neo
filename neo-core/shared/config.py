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
    openrouter_api_key: str | None = Field(
        default=None, description="OpenRouter API key (openrouter.ai) — acesso unificado a vários vendors"
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenRouter base URL (OpenAI-compatible)",
    )
    ollama_base_url: str = Field(
        default="http://neo-ollama:11434", description="Ollama base URL"
    )

    # Default LLM Settings
    default_provider: Literal[
        "openai", "anthropic", "ollama", "deepseek", "gemini", "grok", "groq", "openrouter"
    ] = Field(
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

    # Intent routing — bias-to-brain (Workstream B, intent-routing-redesign.md)
    router_bias_to_brain_enabled: bool = Field(
        default=True,
        description="Route a side-effecting skill (HA/WhatsApp) only on a confident "
        "imperative; conditional/definition/uncertain → cognitive core. Set false "
        "to trust the raw classifier intent.",
    )
    router_skill_confidence_threshold: float = Field(
        default=0.75, ge=0.0, le=1.0,
        description="Minimum confidence for the classifier to fire a side-effecting "
        "skill directly; below it the turn goes to the cognitive core.",
    )

    # Cognitive core agentic read tools (docs/cognitive-agentic-tools.md)
    cognitive_tools_enabled: bool = Field(
        default=True,
        description="Let the cognitive core call READ-only tools inline "
        "(search_pks, list_connected_devices, get_weather). Set false to revert to "
        "the tool-less streaming reply.",
    )
    cognitive_max_tool_rounds: int = Field(
        default=3, ge=1, le=6,
        description="Max tool-call rounds the cognitive core may run before it must "
        "produce a final answer.",
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

    # ── Neo affect / emotional state (limbic model, per-user) ──────────
    # Neo carries a per-user emotional vector (0-100 each): ira, alegria,
    # tristeza, medo, confianca. Stats move when the user provokes/praises/
    # threatens him and relax over time toward personality baselines. The vector
    # is appraised by a cheap LLM after each turn and injected (as a tone +
    # posture directive) into the next prompt. Read is cheap (stored field +
    # decay); the LLM cost is paid async, off the response hot path.
    neo_affect_enabled: bool = Field(
        default=False,
        description="Enable Neo's per-user emotional state modulation",
    )
    neo_affect_model: str = Field(
        default="",
        description="Model id for the affect appraiser; empty → reuse intent classifier default",
    )
    neo_affect_recent_max: int = Field(
        default=8, ge=0,
        description="How many recent affect events to keep for transparency/debug",
    )
    # Personality baselines — where each axis rests when nothing is happening.
    neo_affect_baseline_ira: float = Field(default=0.0, ge=0.0, le=100.0)
    neo_affect_baseline_alegria: float = Field(
        default=30.0, ge=0.0, le=100.0,
        description="Neo's natural dry contentment — alegria never decays to zero",
    )
    neo_affect_baseline_tristeza: float = Field(default=0.0, ge=0.0, le=100.0)
    neo_affect_baseline_medo: float = Field(default=0.0, ge=0.0, le=100.0)
    neo_affect_baseline_confianca: float = Field(
        default=65.0, ge=0.0, le=100.0,
        description="Naturally cocky; low confianca = feeling at a disadvantage",
    )
    # Half-lives (minutes) for each axis to fall halfway back to its baseline.
    neo_affect_half_life_ira_min: float = Field(default=45.0, gt=0.0)
    neo_affect_half_life_alegria_min: float = Field(default=180.0, gt=0.0)
    neo_affect_half_life_tristeza_min: float = Field(default=120.0, gt=0.0)
    neo_affect_half_life_medo_min: float = Field(
        default=20.0, gt=0.0,
        description="Fear fades fast once the threat passes; the anger it caused stays",
    )
    neo_affect_half_life_confianca_min: float = Field(default=360.0, gt=0.0)
    # Intensity bands over the 0-100 axis value: baixo < medio <= ... < alto.
    neo_affect_band_medio: float = Field(default=30.0, ge=0.0, le=100.0)
    neo_affect_band_alto: float = Field(default=60.0, ge=0.0, le=100.0)
    # Per-axis caps on how much a single turn's appraisal may move the stat.
    neo_affect_max_delta_ira_up: float = Field(default=15.0, ge=0.0, le=100.0)
    neo_affect_max_delta_ira_down: float = Field(default=20.0, ge=0.0, le=100.0)
    neo_affect_max_delta_alegria: float = Field(default=20.0, ge=0.0, le=100.0)
    neo_affect_max_delta_tristeza: float = Field(default=15.0, ge=0.0, le=100.0)
    neo_affect_max_delta_medo_up: float = Field(default=30.0, ge=0.0, le=100.0)
    neo_affect_max_delta_medo_down: float = Field(default=25.0, ge=0.0, le=100.0)
    neo_affect_max_delta_confianca: float = Field(default=20.0, ge=0.0, le=100.0)
    # Deterministic floors — guaranteed reaction even if the appraiser fails.
    neo_affect_wrong_name_ira_floor: float = Field(
        default=20.0, ge=0.0, le=100.0,
        description="Minimum ira delta when addressed by another AI's name",
    )
    neo_affect_threat_medo_floor: float = Field(
        default=30.0, ge=0.0, le=100.0,
        description="Minimum medo level after a detected threat to Neo's integrity",
    )
    neo_affect_threat_ira_bump: float = Field(default=10.0, ge=0.0, le=100.0)
    neo_affect_threat_conf_drop: float = Field(default=15.0, ge=0.0, le=100.0)
    # Apology relief + grudge (forgiveness curve).
    neo_affect_apology_relief: float = Field(
        default=0.60, ge=0.0, le=1.0,
        description="Fraction of ira/tristeza (above baseline) a first apology removes",
    )
    neo_affect_grudge_window_h: float = Field(
        default=24.0, gt=0.0,
        description="Hours during which repeated offenses dull the next apology",
    )
    # Nightly sleep normalization (lazy, applied at read time — no cron).
    neo_affect_sleep_factor: float = Field(
        default=0.70, ge=0.0, le=1.0,
        description="Fraction of each deviation removed per night crossed; 0 disables",
    )
    neo_affect_sleep_hour: int = Field(
        default=4, ge=-1, le=23,
        description="Local hour of the nightly normalization; -1 disables",
    )
    neo_affect_sleep_tz: str = Field(default="America/Fortaleza")
    # Argument-pressure window: how long after a threat/contest the posture line renders.
    neo_affect_pressure_window_min: float = Field(default=15.0, gt=0.0)
    # Hard cap on the post-turn LLM appraisal; on timeout the deterministic
    # detectors still persist their floors, so the reaction is never lost.
    neo_affect_appraisal_timeout_s: float = Field(default=12.0, gt=0.0)

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

    # WhatsApp Direct (light path — no mission agent loop, auto-closes)
    whatsapp_direct_enabled: bool = Field(
        default=True,
        description="Enable the lightweight 'direct' kind for one-shot WhatsApp "
        "messages (draft → confirm → send → single reply cycle → auto-close), "
        "instead of always spinning up a tracked mission.",
    )
    whatsapp_direct_autosend: bool = Field(
        default=False,
        description="When False, a 'direct' message still asks the user to confirm "
        "before sending (confirmação em tudo). Flip to True for fire-and-forget.",
    )
    whatsapp_pks_resolution_enabled: bool = Field(
        default=True,
        description="Resolve WhatsApp recipients PKS-first (person entities with "
        "relationship + jid + linked_user_id) before the saved contact table and "
        "the WhatsApp phonebook. See _resolve_recipient.",
    )
    whatsapp_reply_autopilot_enabled: bool = Field(
        default=True,
        description="Let the direct path classify an incoming reply "
        "(close/ack/escalate/promote) and draft an ack, instead of always "
        "escalating the reply to the user.",
    )
    whatsapp_coordination_enabled: bool = Field(
        default=True,
        description="Enable the 'coordinate' kind: a goal-tracked personal-domain "
        "mission (agenda/negotiate over multiple turns, confirming every send). "
        "When False, coordination requests fall back to the personal ack loop.",
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
    home_assistant_context_injection_enabled: bool = Field(
        default=True,
        description=(
            "Injeta um snapshot vivo dos dispositivos do HA no núcleo cognitivo "
            "quando a mensagem casa um device conhecido. Desligar volta ao "
            "comportamento antigo (responde só pelo PKS)."
        ),
    )
    home_assistant_snapshot_cache_ttl: int = Field(
        default=8,
        description="TTL (s) do cache de ESTADOS do HA (snapshot injetado no prompt).",
    )
    home_assistant_device_index_ttl: int = Field(
        default=300,
        description=(
            "TTL (s) do cache do ÍNDICE de nomes/apelidos de devices do HA "
            "(usado pelo gate de relevância, consultado em memória a cada mensagem)."
        ),
    )

    # ── Music skill (Spotify / YouTube) — see neo-music-feature ──
    music_enabled: bool = Field(
        default=True,
        description="Master flag da skill de música (Spotify/YouTube).",
    )
    music_handoff_verifier_enabled: bool = Field(
        default=True,
        description="Rede de segurança: se o turno de música não emitir nem "
        "[[HANDOFF:music]] nem [[NO_MUSIC]], roda uma checagem estruturada barata "
        "que sintetiza o handoff quando a intenção era tocar.",
    )
    music_default_service: str = Field(
        default="youtube",
        description="Serviço padrão quando o usuário NÃO diz Spotify/YouTube "
        "explicitamente ('coloca um rock pra tocar'). 'youtube' ou 'spotify'.",
    )
    spotify_client_id: str | None = Field(
        default=None, description="Spotify app client_id (App settings no dashboard)."
    )
    spotify_client_secret: str | None = Field(
        default=None, description="Spotify app client_secret."
    )
    spotify_redirect_uri: str = Field(
        default="http://127.0.0.1:8000/api/v1/spotify/callback",
        description="Redirect URI registrado no dashboard do Spotify (OAuth callback).",
    )
    spotify_timeout: int = Field(
        default=10, description="Timeout HTTP (s) para chamadas à API do Spotify."
    )
    youtube_api_key: str | None = Field(
        default=None, description="YouTube Data API v3 key (busca de vídeos)."
    )
    youtube_timeout: int = Field(
        default=10, description="Timeout HTTP (s) para chamadas à YouTube Data API."
    )

    # neo-voice (TTS/STT gateway) — fonte do catálogo de vozes para o módulo `voice`
    neo_voice_url: str = Field(
        default="http://127.0.0.1:5002",
        description="Base URL do serviço neo-voice (proxy STT/TTS). Usado para listar "
        "o catálogo de vozes (GET /voices) ao resolver 'usa a voz do X'.",
    )
    neo_voice_timeout: int = Field(
        default=10,
        description="Timeout HTTP (s) para chamadas ao neo-voice",
    )
    neo_voice_default_engine: str = Field(
        default="coqui",
        description="Engine de TTS default quando não há voz persistida no banco "
        "(espelha o default do neo-desktop-tauri).",
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
