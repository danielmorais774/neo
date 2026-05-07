"""Main FastAPI application for Neo Core 2."""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Configure logging early so all loggers (including modules) respect LOG_LEVEL
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(levelname)s:     %(name)s - %(message)s",
    force=True,
)
# Silence noisy third-party loggers
for _noisy in ("pymongo", "grpc", "grpc._cython", "httpcore", "httpx", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import context, desktop, health, debug, quick_help, session_logs, knowledge_graph, whatsapp, missions, devices
from core import (
    AgentExecutor,
    ContextCompactor,
    EmbeddingService,
    HybridMemorySearch,
    IntentRouter,
    LLMRouter,
    PersistentSessionManager,
    PreferenceExtractor,
    TokenCounter,
    ToolExecutor,
    ToolRegistry,
)
from core.memory import SessionManager
from core.skills.skill_executor import SkillExecutor
from core.skills.whatsapp_messaging.pipeline import WhatsAppMessagingPipeline
from core.notifications import NotificationBroker
from db import AsyncPostgresClient
from modules.desktop import AIService, DesktopService, DESKTOP_TOOLS
from modules.weather import WEATHER_TOOLS, WeatherExecutor
from observability.logger import ObservabilityLogger, set_obs
from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Instâncias globais
_tool_registry: ToolRegistry | None = None
_tool_executor: ToolExecutor | None = None
_llm_router: LLMRouter | None = None
_agent_executor: AgentExecutor | None = None
_session_manager: SessionManager | None = None
_desktop_service: DesktopService | None = None
_ai_service: AIService | None = None
_db: AsyncPostgresClient | None = None
_notification_broker: NotificationBroker | None = None
_skill_executor: SkillExecutor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifecycle manager - inicializa e limpa recursos."""
    global _tool_registry, _tool_executor, _llm_router, _agent_executor
    global _session_manager, _desktop_service, _ai_service, _db
    global _notification_broker, _skill_executor

    settings = get_settings()
    print(f"🚀 Initializing Neo Core 2 v2.1.0...")

    # ── Database (async) ──────────────────────────────────────────
    _db = AsyncPostgresClient(settings.database_url)
    print(f"✅ Async database pool ready")

    # ── Observability (MongoDB, fire-and-forget) ──────────────────
    obs = ObservabilityLogger()
    await obs.connect()  # gracefully no-ops if mongo unavailable
    set_obs(obs)

    # ── Intelligence layer ────────────────────────────────────────
    token_counter = TokenCounter()
    context_compactor = ContextCompactor(llm_router=None, token_counter=token_counter)  # wired below after LLMRouter

    # ── Core components ───────────────────────────────────────────
    _tool_registry = ToolRegistry()
    _tool_executor = ToolExecutor(_tool_registry)
    _llm_router = LLMRouter()
    _session_manager = SessionManager()  # legacy in-memory (used by DesktopService)

    # Wire LLM into compactor after LLMRouter is ready
    context_compactor.llm_router = _llm_router

    _agent_executor = AgentExecutor(
        llm_router=_llm_router,
        tool_registry=_tool_registry,
        tool_executor=_tool_executor,
        token_counter=token_counter,
        compactor=context_compactor,
    )

    # ── Register tools ────────────────────────────────────────────
    _tool_registry.register_many(DESKTOP_TOOLS)      # client-side
    _tool_registry.register_many(WEATHER_TOOLS)       # server-side

    # Register server-side module executors
    weather_executor = WeatherExecutor()
    _tool_executor.register_module_executor("weather", weather_executor)

    # Register whatsapp_messaging tools in the global registry (for discovery / routing)
    from core.skills.whatsapp_messaging.tools import WHATSAPP_MESSAGING_TOOLS
    _tool_registry.register_many(WHATSAPP_MESSAGING_TOOLS)

    # Import skill manifests to register them in SkillManifest registry
    from core.skills.whatsapp_messaging import whatsapp_messaging_manifest  # noqa: F401

    # ── New AI pipeline ───────────────────────────────────────────
    embedding_service = EmbeddingService()
    # Pre-warm embedding model so the first user request doesn't pay the cold-start cost (~8s)
    try:
        import asyncio as _asyncio
        import time as _time
        _t0_warmup = _time.monotonic()
        await embedding_service.embed("warmup")
        print(f"✅ Embedding model warm ({((_time.monotonic() - _t0_warmup) * 1000):.0f}ms)")
    except Exception as _e:
        print(f"⚠️  Embedding warm-up failed (will load on first request): {_e}")
    hybrid_search = HybridMemorySearch(db=_db, embedding_service=embedding_service, settings=settings)
    persistent_session = PersistentSessionManager(
        db=_db,
        embedding_service=embedding_service,
        hybrid_search=hybrid_search,
        token_counter=token_counter,
    )
    intent_router = IntentRouter(llm_router=_llm_router, tool_registry=_tool_registry)
    preference_extractor = PreferenceExtractor(llm_router=_llm_router, db=_db)

    # ── Knowledge Graph routes ────────────────────────────────────
    knowledge_graph.set_db(_db)

    # ── Knowledge Retriever (context routes) ────────────────────
    from modules.context.knowledge_retriever import KnowledgeRetriever
    from modules.context.knowledge_writer import KnowledgeGraphWriter
    _knowledge_retriever = KnowledgeRetriever(
        session_factory=_db.session_factory,
        llm_router=_llm_router,
    )
    _knowledge_writer = KnowledgeGraphWriter(llm_router=_llm_router, db=_db)
    context.set_knowledge_retriever(_knowledge_retriever)
    print(f"✅ Knowledge retriever ready")

    _ai_service = AIService(
        llm_router=_llm_router,
        intent_router=intent_router,
        session_manager=persistent_session,
        tool_registry=_tool_registry,
        tool_executor=_tool_executor,
        agent_executor=_agent_executor,
        db=_db,
        preference_extractor=preference_extractor,
        knowledge_retriever=_knowledge_retriever,
        knowledge_writer=_knowledge_writer,
    )

    # ── Skills + Notifications ─────────────────────────────────────
    _notification_broker = NotificationBroker()
    _skill_executor = SkillExecutor(
        agent_executor=_agent_executor,
        db=_db,
        notification_broker=_notification_broker,
    )

    # Wire skill executor into webhook route
    from api.routes.whatsapp import set_dependencies as set_whatsapp_deps
    set_whatsapp_deps(_db, _skill_executor)

    # Wire missions route
    missions.set_db(_db)
    missions.set_pipeline_factory(lambda: WhatsAppMessagingPipeline(
        agent_executor=_agent_executor,
        db=_db,
        broker=_notification_broker,
    ))

    # ── Legacy DesktopService (REST routes) ───────────────────────
    _desktop_service = DesktopService(
        agent_executor=_agent_executor,
        session_manager=_session_manager,
        tool_registry=_tool_registry,
    )
    desktop.set_desktop_service(_desktop_service)

    providers = _llm_router.list_available_providers()
    print(f"✅ Available LLM providers: {', '.join(providers)}")
    print(f"✅ Registered tools: {_tool_registry.count()}")
    print(f"   ├─ Desktop (client-side): {len(DESKTOP_TOOLS)}")
    print(f"   └─ Weather (server-side): {len(WEATHER_TOOLS)}")
    print(f"   └─ WhatsApp Messaging (server-side): {len(WHATSAPP_MESSAGING_TOOLS)}")
    print(f"🌐 API ready on port {settings.api_port}")

    # ── Start gRPC server ─────────────────────────────────────────
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from grpc_server import serve as serve_grpc

        grpc_server = await serve_grpc(
        ai_service=_ai_service,
        port=settings.grpc_port,
        notification_broker=_notification_broker,
        skill_executor=_skill_executor,
    )
        app.state.grpc_server = grpc_server
        print(f"✅ gRPC server started on port {settings.grpc_port}")
    except Exception as e:
        import traceback
        print("⚠️ Failed to start gRPC server:")
        traceback.print_exc()

    print(f"🟢 Neo Core 2 fully initialized")
    yield

    # ── Shutdown ──────────────────────────────────────────────────
    print("🛑 Shutting down Neo Core 2...")

    grpc_server = getattr(app.state, "grpc_server", None)
    if grpc_server is not None:
        await grpc_server.stop(0)
        print("✅ gRPC server stopped")

    if _db:
        await _db.dispose()
        print("✅ Database pool closed")

    obs_instance = globals().get("obs")
    if obs_instance:
        await obs_instance.disconnect()

    if _session_manager:
        _session_manager.clear_all()


# Create FastAPI app
app = FastAPI(
    title="Neo Core 2 API",
    version="2.1.0",
    description="Neo Core — AI-powered orchestrator with IntentRouter, streaming LLM, persistent sessions",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health.router)
app.include_router(desktop.router, prefix="/api/v1")
app.include_router(debug.router, prefix="/api/v1")
app.include_router(session_logs.router, prefix="/api/v1")
app.include_router(context.router, prefix="/api/v1")
app.include_router(knowledge_graph.router, prefix="/api/v1")
app.include_router(quick_help.router, prefix="/api/v1")
app.include_router(whatsapp.router)
app.include_router(missions.router)
app.include_router(devices.router, prefix="/api/v1")
