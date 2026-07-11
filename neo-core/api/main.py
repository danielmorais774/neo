"""Main FastAPI application for Neo Core 2."""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Configure logging early so all loggers (including modules) respect LOG_LEVEL
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

# File handler for app logs
_logs_dir = os.environ.get("SESSION_LOGS_DIR", "/app/logs")
_file_handler = None
try:
    os.makedirs(_logs_dir, exist_ok=True)
    _file_handler = logging.FileHandler(f"{_logs_dir}/app.log")
    _file_handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
    _file_handler.setLevel(getattr(logging, _log_level, logging.INFO))
except Exception as e:
    print(f"⚠️  Could not create app.log file handler: {e}")

# Configure root logger
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(levelname)s:     %(name)s - %(message)s",
    force=True,
)
# Add file handler if available
if _file_handler:
    logging.root.addHandler(_file_handler)
# Silence noisy third-party loggers
for _noisy in ("pymongo", "grpc", "grpc._cython", "httpcore", "httpx", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Also configure uvicorn loggers to write to app.log
for _uv_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_logger_name)
    if _file_handler and _uv_logger.level == 0:
        _uv_logger.addHandler(_file_handler)
        _uv_logger.setLevel(getattr(logging, _log_level, logging.INFO))

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import affect, context, desktop, health, debug, quick_help, session_logs, knowledge_graph, whatsapp, missions, devices, app_logs, letta, intent_config, voice_config, spotify, skills
from api.security import require_panel_jwt
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
from core.agent.context_selector import PromptContextSelector
from core.memory import SessionManager
from core.pks import PKSService
from core.skills.skill_executor import SkillExecutor
from core.skills.whatsapp_messaging.pipeline import WhatsAppMessagingPipeline
from core.notifications import NotificationBroker
from db import AsyncPostgresClient
from modules.desktop import AIService, DesktopService, DESKTOP_TOOLS
from api.routes.devices import ws_router as devices_ws_router
from modules.weather import WEATHER_TOOLS, WeatherExecutor
from modules.devices import DEVICES_TOOLS, DevicesExecutor
from modules.voice import VOICE_TOOLS, VoiceExecutor
from modules.config import CONFIG_TOOLS, ConfigExecutor, build_default_registry
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
    _tool_registry.register_many(DEVICES_TOOLS)       # server-side
    from modules.pks_search import PKS_SEARCH_TOOLS, PKSSearchExecutor
    _tool_registry.register_many(PKS_SEARCH_TOOLS)    # server-side

    # Register server-side module executors
    weather_executor = WeatherExecutor()
    _tool_executor.register_module_executor("weather", weather_executor)

    # Default devices executor (no user scoping) — per-user scoping is injected
    # per-request via DevicesContextExecutor in AIService._handle_server_tool.
    devices_executor = DevicesExecutor(db=_db)
    _tool_executor.register_module_executor("devices", devices_executor)

    # Voice (TTS) introspection — server-side, READ-ONLY (voice_list/voice_get).
    # Default (no user scoping); per-request user scoping via set_context in AIService.
    _tool_registry.register_many(VOICE_TOOLS)
    voice_executor = VoiceExecutor(db=_db)
    _tool_executor.register_module_executor("voice", voice_executor)

    # Generic config — server-side. Registry-driven settings (TTS voice/engine, …);
    # config_set persists the active voice into the same row voice reads (Pattern A).
    # Per-request user scoping via set_context in AIService (like voice/devices).
    _tool_registry.register_many(CONFIG_TOOLS)
    config_executor = ConfigExecutor(db=_db, registry=build_default_registry())
    _tool_executor.register_module_executor("config", config_executor)

    # Workbench — o sandbox de execução do próprio Neo (a "bash do Neo").
    # Server-side: o neo-core roda comandos no container irmão via docker exec.
    _workbench_sandbox = None
    _job_manager = None
    if settings.workbench_enabled:
        from core.workbench import WORKBENCH_TOOLS, WorkbenchExecutor, WorkbenchSandbox

        _workbench_sandbox = WorkbenchSandbox(
            container=settings.workbench_container,
            workspace_root=settings.workbench_workspace_root,
            docker_socket=settings.workbench_docker_socket,
            default_timeout=settings.workbench_default_timeout,
            max_output_bytes=settings.workbench_max_output_bytes,
        )
        _tool_registry.register_many(WORKBENCH_TOOLS)
        _tool_executor.register_module_executor(
            "workbench", WorkbenchExecutor(_workbench_sandbox)
        )
        app.state.workbench_sandbox = _workbench_sandbox
        print(f"✅ Workbench sandbox wired (container={settings.workbench_container})")

        # Background jobs (workbench). Tools registered for LLM/router visibility;
        # neo_bash_background/job_* dispatch through a per-request JobToolExecutor,
        # not the global one. The manager loop is started later (after the dispatcher
        # is wired), so boot-reconcile notifications can be delivered.
        if settings.jobs_enabled:
            from core.jobs import JOB_TOOLS, JobManager

            _tool_registry.register_many(JOB_TOOLS)
            _job_manager = JobManager(
                db=_db,
                sandbox=_workbench_sandbox,
                max_concurrent=settings.job_max_concurrent,
                tick_interval=settings.job_tick_interval,
                default_timeout=settings.job_default_timeout,
                workspace_root=settings.workbench_workspace_root,
            )
            app.state.job_manager = _job_manager
            print("✅ JobManager created (background jobs)")

    # Register whatsapp_messaging tools in the global registry (for discovery / routing)
    from core.skills.whatsapp_messaging.tools import WHATSAPP_MESSAGING_TOOLS
    _tool_registry.register_many(WHATSAPP_MESSAGING_TOOLS)

    # Register home_assistant tools in the global registry (for discovery / routing)
    from core.skills.home_assistant.tools import HOME_ASSISTANT_TOOLS
    _tool_registry.register_many(HOME_ASSISTANT_TOOLS)

    # Import skill manifests to register them in SkillManifest registry
    from core.skills.whatsapp_messaging import whatsapp_messaging_manifest  # noqa: F401
    from core.skills.home_assistant import home_assistant_manifest  # noqa: F401
    from core.skills.music import music_manifest  # noqa: F401

    # ── Skills Library — skills declarativas SKILL.md ─────────────
    # Catálogo de 1 linha/skill no prompt cognitivo + corpo sob demanda via
    # skill_read (progressive disclosure). Ver docs/skills-library-plan.md.
    _skill_library = None
    if settings.skills_library_enabled:
        from core.skills.library import SkillLibrary
        from modules.skills_library import SKILLS_LIBRARY_TOOLS, SkillsLibraryExecutor

        _skill_library = SkillLibrary(
            root=settings.skills_library_path,
            settings=settings,
            tool_registry=_tool_registry,
        )
        _skill_library.load()
        _tool_registry.register_many(SKILLS_LIBRARY_TOOLS)
        _tool_executor.register_module_executor(
            "skills_library", SkillsLibraryExecutor(_skill_library)
        )

        # Tools "gated" por skill (tool_backed): registradas no executor/registry
        # globais para despachar, mas FORA do read set default — o cognitivo só
        # as vê depois de skill_read (tool gating no _run_cognitive_tool_loop).
        from modules.sqlro import SQLRO_TOOLS, SqlRoExecutor

        _tool_registry.register_many(SQLRO_TOOLS)
        _tool_executor.register_module_executor("sqlro", SqlRoExecutor(settings))
        app.state.skill_library = _skill_library
        print(
            f"✅ Skills library ready ({len(_skill_library.all())} skill(s) "
            f"from {settings.skills_library_path})"
        )

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
    intent_router = IntentRouter(
        llm_router=_llm_router, tool_registry=_tool_registry, db=_db,
        skill_library=_skill_library,
    )
    preference_extractor = PreferenceExtractor(llm_router=_llm_router, db=_db)

    # ── Personal Knowledge Store ─────────────────────────────────
    _pks = PKSService(db=_db, llm_router=_llm_router)
    context.set_pks_service(_pks)
    knowledge_graph.set_pks_service(_pks)
    affect.set_pks_service(_pks)
    if settings.neo_affect_enabled:
        # Eager: WhatsApp inbound may be the first turn after a restart, and its
        # pipeline reaches affect only through the shared updater.
        from core.agent.affect_updater import AffectUpdater, set_shared_updater
        set_shared_updater(AffectUpdater(_llm_router, settings, pks=_pks, db=_db))

    # search_pks executor needs the PKS service, so it's registered here (after
    # creation). Per-request user scoping happens via set_context in AIService.
    pks_search_executor = PKSSearchExecutor(pks_service=_pks)
    _tool_executor.register_module_executor("pks_search", pks_search_executor)

    # get_user_location resolves the user's city (PKS → device GPS → ask) and also
    # persists it (save_city). Needs the PKS service; per-request user + GPS scoping
    # happens via set_context in AIService._handle_server_tool.
    from modules.location import LOCATION_TOOLS, LocationExecutor
    _tool_registry.register_many(LOCATION_TOOLS)
    location_executor = LocationExecutor(pks_service=_pks)
    _tool_executor.register_module_executor("location", location_executor)
    print("✅ PKS ready")
    system_pks = await _pks.ensure_system_pks()
    print(
        "✅ Neo system PKS bootstrap ready "
        f"(workspace={system_pks['workspace_id']}, agent={system_pks['agent_id']})"
    )

    # ── Context Selector ─────────────────────────────────────────
    context_selector = PromptContextSelector(
        token_counter=token_counter,
        settings=settings,
        pks=_pks,
        preference_extractor=preference_extractor,
    )
    print("✅ PromptContextSelector ready")
    context.set_context_selector(context_selector)

    _ai_service = AIService(
        llm_router=_llm_router,
        intent_router=intent_router,
        session_manager=persistent_session,
        tool_registry=_tool_registry,
        tool_executor=_tool_executor,
        agent_executor=_agent_executor,
        db=_db,
        preference_extractor=preference_extractor,
        pks=_pks,
        context_selector=context_selector,
        job_manager=_job_manager,
        skill_library=_skill_library,
    )

    # ── Skills + Notifications ─────────────────────────────────────
    _notification_broker = NotificationBroker()
    _skill_executor = SkillExecutor(
        agent_executor=_agent_executor,
        db=_db,
        notification_broker=_notification_broker,
    )

    # ── Background jobs: wire the proactive dispatcher + start the loop ──
    # Done here (not in the workbench block) because the dispatcher needs the
    # notification broker. Starting AFTER set_dispatcher lets boot-reconcile
    # notifications reach the user. See docs/workbench-background-jobs-plan.md.
    if _job_manager is not None:
        from core.jobs import ProactiveDispatcher

        _job_manager.set_dispatcher(
            ProactiveDispatcher(
                db=_db,
                broker=_notification_broker,
                llm_router=_llm_router,
                default_model=settings.default_model,
            )
        )
        await _job_manager.start()
        print("✅ JobManager started (dispatcher wired)")

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

    # Wire devices route
    devices.set_db(_db)

    # Wire intent classifier config route (neo-panel → Config page)
    intent_config.set_dependencies(_db, _llm_router)

    # Wire voice (TTS) config route (neo-panel → Settings page)
    voice_config.set_dependencies(_db)

    # Wire Spotify OAuth route (neo-panel → Music page)
    spotify.set_dependencies(_db)

    # Wire skills route (neo-panel → Skills page). _skill_library é None quando
    # a flag está off — a rota responde 503 em vez de sumir.
    skills.set_dependencies(_skill_library, _tool_registry)

    # ── Legacy DesktopService (REST routes) ───────────────────────
    _desktop_service = DesktopService(
        agent_executor=_agent_executor,
        session_manager=_session_manager,
        tool_registry=_tool_registry,
    )
    desktop.set_desktop_service(_desktop_service)
    desktop.set_ai_service(_ai_service)
    desktop.set_db(_db)

    providers = _llm_router.list_available_providers()
    print(f"✅ Available LLM providers: {', '.join(providers)}")
    print(f"✅ Registered tools: {_tool_registry.count()}")
    print(f"   ├─ Desktop (client-side): {len(DESKTOP_TOOLS)}")
    print(f"   └─ Weather (server-side): {len(WEATHER_TOOLS)}")
    print(f"   └─ WhatsApp Messaging (server-side): {len(WHATSAPP_MESSAGING_TOOLS)}")
    print(f"   └─ Home Assistant (server-side): {len(HOME_ASSISTANT_TOOLS)}")
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

    if _job_manager is not None:
        try:
            await _job_manager.stop()
            print("✅ JobManager stopped")
        except Exception as e:
            print(f"⚠️ JobManager stop error: {e}")

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
app.include_router(debug.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(session_logs.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(context.router, prefix="/api/v1")
app.include_router(knowledge_graph.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(affect.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(quick_help.router, prefix="/api/v1")
app.include_router(whatsapp.router)
app.include_router(missions.router, dependencies=[Depends(require_panel_jwt)])
app.include_router(devices.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(devices_ws_router, prefix="/api/v1")  # WebSocket — desktop, sem JWT
app.include_router(app_logs.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(intent_config.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(voice_config.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(skills.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
# Spotify OAuth: open (the callback is a browser redirect from Spotify, no JWT);
# identity is bound via a signed `state`. See api/routes/spotify.py.
app.include_router(spotify.router, prefix="/api/v1")
app.include_router(letta.router)
