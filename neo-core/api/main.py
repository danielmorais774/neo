"""Main FastAPI application for Neo Core 2."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Configure logging early so all loggers (including modules) respect LOG_LEVEL
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

# App logs (/app/logs/app.log) are written by the container entrypoint, which tees
# uvicorn's stdout+stderr into that file (see entrypoint-dev.sh, `> >(tee ...) 2>&1`).
# We must NOT also attach a FileHandler to the same path: root's StreamHandler
# already reaches app.log via stderr→tee, so a second FileHandler wrote every
# `logger.*` line TWICE into app.log (while bare `print()`s appeared once). Keeping
# a single writer (the tee) gives one clean copy — and `docker logs` still shows it.
_logs_dir = os.environ.get("SESSION_LOGS_DIR", "/app/logs")
try:
    os.makedirs(_logs_dir, exist_ok=True)
except Exception as e:
    print(f"⚠️  Could not create logs dir {_logs_dir}: {e}")

# Configure root logger. Its StreamHandler goes to stderr, which the entrypoint
# tees into app.log (uvicorn's own loggers already emit there the same way).
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(levelname)s:     %(name)s - %(message)s",
    force=True,
)
# Silence noisy third-party loggers
for _noisy in ("pymongo", "grpc", "grpc._cython", "httpcore", "httpx", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import affect, context, desktop, health, debug, quick_help, session_logs, knowledge_graph, whatsapp, missions, devices, app_logs, letta, intent_config, model_config, voice_config, spotify, skills, research, files, connections
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

    # Web search (camada 1 — docs/web-search-deep-research-plan.md): busca inline
    # do cognitivo (SearXNG self-host + fallback ddgs). Leitura global, como weather.
    _web_search_executor = None
    if settings.web_search_enabled:
        from modules.web_search import WEB_SEARCH_TOOLS, WebSearchExecutor

        _web_search_executor = WebSearchExecutor(
            searxng_url=settings.searxng_url,
            backends_order=settings.web_search_backends,
            cache_ttl=settings.web_search_cache_ttl,
            fetch_max_chars=settings.web_fetch_max_chars,
        )
        _tool_registry.register_many(WEB_SEARCH_TOOLS)
        _tool_executor.register_module_executor("web_search", _web_search_executor)
        app.state.web_search_executor = _web_search_executor
        print(f"✅ Web search wired (backends={settings.web_search_backends})")

    # Service Gateway (docs/service-gateway-dsl-plan.md): HTTP arbitrário para
    # serviços cadastrados (HA, GitHub, …) sem o LLM ver credencial. Registry
    # (db + seed das envs), política deny>confirm>allow, broker env://.
    if settings.neo_gateway_enabled:
        from core.credentials import build_credential_store
        from core.gateway import build_gateway_service
        from modules.services import SERVICE_TOOLS, ServicesExecutor

        # Cofre Fernet (F2): só existe com NEO_CRED_KEY. build_credential_store
        # importa `cryptography` LÁ DENTRO — sem chave, a lib nem é tocada e o
        # broker recusa 'fernet://' com mensagem clara (só 'env://' funciona).
        _cred_store = build_credential_store(db=_db, key=settings.neo_cred_key)
        _gateway_service = build_gateway_service(
            db=_db, settings=settings, fernet_store=_cred_store
        )
        _tool_registry.register_many(SERVICE_TOOLS)
        _tool_executor.register_module_executor("services", ServicesExecutor(_gateway_service))
        connections.set_context(
            gateway=_gateway_service,
            registry=_gateway_service.registry,
            cred_store=_cred_store,
            db=_db,
        )
        app.state.gateway_service = _gateway_service
        app.state.credential_store = _cred_store
        print(
            "✅ Service Gateway wired (tools service_*; cofre: "
            f"{'ON' if _cred_store else 'OFF — sem NEO_CRED_KEY'})"
        )

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
            from core.jobs import JOB_TOOLS, JobManager, build_research_runner

            _tool_registry.register_many(JOB_TOOLS)
            # Runner de deep_research (kind=research), injetado sem acoplar o
            # JobManager ao core/research (docs/web-search-deep-research-plan.md).
            _research_runner = None
            if settings.research_enabled and _web_search_executor is not None:
                _research_runner = build_research_runner(
                    db=_db,
                    llm_router=_llm_router,
                    web_search=_web_search_executor,
                    sandbox=_workbench_sandbox,
                    research_model=settings.research_model,
                    research_model_writer=settings.research_model_writer,
                )
                print(
                    f"✅ Research runner wired (volume={settings.research_model}, "
                    f"writer={settings.research_model_writer})"
                )
            # Runner de browser_task (kind=browser) — delega ao container
            # neo-browser-agent (docs/browser-agent-plan.md).
            _browser_runner = None
            if settings.browser_agent_enabled:
                from core.jobs import build_browser_runner

                _browser_runner = build_browser_runner(
                    db=_db,
                    base_url=settings.browser_agent_url,
                    timeout_s=settings.browser_task_timeout,
                )
                print(f"✅ Browser runner wired (url={settings.browser_agent_url})")
            _job_manager = JobManager(
                db=_db,
                sandbox=_workbench_sandbox,
                max_concurrent=settings.job_max_concurrent,
                tick_interval=settings.job_tick_interval,
                default_timeout=settings.job_default_timeout,
                workspace_root=settings.workbench_workspace_root,
                research_runner=_research_runner,
                browser_runner=_browser_runner,
            )
            app.state.job_manager = _job_manager
            print("✅ JobManager created (background jobs)")

    # File Store (docs/file-store-plan.md): entrada/catálogo de arquivos. Depende
    # do sandbox para o file_stage (materializa o blob em /workspace/files via cp
    # no workbench) — sem workbench, file_stage recusa e o resto segue funcionando.
    _file_store = None
    _files_gc_task = None
    if settings.neo_files_enabled:
        from core.files import FILE_TOOLS, FilesExecutor, build_file_store, gc_loop

        _file_store = build_file_store(db=_db, settings=settings, sandbox=_workbench_sandbox)
        _file_store.ensure_dirs()
        _tool_registry.register_many(FILE_TOOLS)
        _tool_executor.register_module_executor("files", FilesExecutor(_file_store))
        files.set_store(_file_store)
        app.state.file_store = _file_store
        _files_gc_task = asyncio.create_task(gc_loop(_file_store, settings.neo_files_gc_interval_s))
        app.state.files_gc_task = _files_gc_task
        print(f"✅ File Store wired (root={settings.neo_files_root}, tools file_*)")

    # Modo Birra (docs/modo-birra-plan.md §3.3): o sweeper desfaz os atos no
    # prazo. Roda uma varredura já no start — o core recarrega a cada save, e um
    # ato pendente de antes do reload precisa ser recuperado, senão a luz fica
    # apagada. Sobe junto com a flag; sem ela não há ato para desfazer.
    _birra_sweeper_task = None
    if settings.neo_birra_enabled:
        from core.agent.birra.revert import sweeper_loop
        from core.agent.birra.store import BirraStore

        def _ha_adapter_factory():
            from core.skills.home_assistant.adapter import HomeAssistantAdapter

            return HomeAssistantAdapter()

        _birra_sweeper_task = asyncio.create_task(
            sweeper_loop(BirraStore(_db), _ha_adapter_factory)
        )
        app.state.birra_sweeper_task = _birra_sweeper_task
        print("✅ Modo Birra: sweeper de reversão ativo")

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

        # Finanças pessoais (skill 'financas'): tools fin_* gated e user-scoped.
        # O executor global fica SEM usuário (recusa educadamente); o cognitivo
        # cria um executor fresco por request no CognitiveReadExecutor.
        from modules.finance import FINANCE_TOOLS, FinanceExecutor

        _tool_registry.register_many(FINANCE_TOOLS)
        _tool_executor.register_module_executor("finance", FinanceExecutor(db=_db))

        # load() só DEPOIS de registrar as tools gated: ele valida `tools:` do
        # frontmatter contra o registry e avisaria "tools não registradas" para
        # toda skill tool_backed (falso alarme que já custou um diagnóstico).
        _skill_library.load()
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
        file_store=_file_store,
    )
    # Panel /context/prompt-preview reproduces the real turn's prompt assembly
    # (classify → recover_context → register → build_prompt) read-only.
    context.set_ai_service(_ai_service)

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

    # Wire research route (neo-panel → aba Pesquisas)
    research.set_db(_db)
    research.set_job_manager(_job_manager)

    # Wire intent classifier config route (neo-panel → Config page)
    intent_config.set_dependencies(_db, _llm_router)

    # Wire subsystem model config route (neo-panel → Modelos: memory planner +
    # Deep Research volume/writer)
    model_config.set_dependencies(_db, _llm_router)

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

    if _files_gc_task is not None:
        _files_gc_task.cancel()
        try:
            await _files_gc_task
        except (asyncio.CancelledError, Exception):
            pass
        print("✅ File Store GC stopped")

    if _birra_sweeper_task is not None:
        _birra_sweeper_task.cancel()
        try:
            await _birra_sweeper_task
        except (asyncio.CancelledError, Exception):
            pass
        print("✅ Modo Birra sweeper stopped")

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
app.include_router(research.router, dependencies=[Depends(require_panel_jwt)])
# File Store: SEM guarda global — upload/download seguem o nível das rotas do
# desktop (que não têm JWT); a gestão do catálogo pede panel JWT por endpoint.
# Ver docs/file-store-plan.md §8 e o cabeçalho de api/routes/files.py.
app.include_router(files.router)
# Service Gateway (docs/service-gateway-dsl-plan.md §8): tela de administração
# (conexões, cofre de credenciais, auditoria) — sempre atrás do panel JWT.
app.include_router(connections.router, dependencies=[Depends(require_panel_jwt)])
app.include_router(devices.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(devices_ws_router, prefix="/api/v1")  # WebSocket — desktop, sem JWT
app.include_router(app_logs.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(intent_config.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(model_config.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(voice_config.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
app.include_router(skills.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
# Spotify OAuth: open (the callback is a browser redirect from Spotify, no JWT);
# identity is bound via a signed `state`. See api/routes/spotify.py.
app.include_router(spotify.router, prefix="/api/v1")
app.include_router(letta.router)
