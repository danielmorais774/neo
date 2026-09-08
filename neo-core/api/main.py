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

from api.routes import affect, context, desktop, health, debug, quick_help, session_logs, knowledge_graph, whatsapp, missions, devices, app_logs, letta, intent_config, model_config, voice_config, spotify, skills, research, files, connections, datasets
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
from modules.devices import DEVICE_CONTROL_TOOLS, DEVICES_TOOLS, DevicesExecutor
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
        # Lê o role "cognitive" (aba Modelos do painel) a cada turno.
        db=_db,
    )

    # ── Register tools ────────────────────────────────────────────
    _tool_registry.register_many(DESKTOP_TOOLS)      # client-side
    _tool_registry.register_many(WEATHER_TOOLS)       # server-side
    _tool_registry.register_many(DEVICES_TOOLS)       # server-side
    # device_control / device_power (docs/device-control-plan.md). Registradas
    # sempre; quem decide se o cérebro as VÊ são as flags neo_device_control_enabled
    # e neo_device_power_enabled em cognitive_tools_for.
    _tool_registry.register_many(DEVICE_CONTROL_TOOLS)  # server-side
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

    # Cofre Fernet: só existe com NEO_CRED_KEY. build_credential_store importa
    # `cryptography` LÁ DENTRO — sem chave, a lib nem é tocada e o broker recusa
    # 'fernet://' com mensagem clara (só 'env://' funciona).
    #
    # Fica FORA do bloco do gateway de propósito: o cofre passou a ter três
    # consumidores (gateway, tools de email e credenciais do browser), e amarrá-lo
    # a NEO_GATEWAY_ENABLED faria o email do Neo sumir ao desligar o gateway.
    from core.credentials import build_credential_store

    _cred_store = build_credential_store(db=_db, key=settings.neo_cred_key)
    app.state.credential_store = _cred_store

    # Cofre visível ao cérebro (catálogo SEM segredo) + caixa de email do Neo.
    # Registry e executor no MESMO lugar: registrar a tool sem executor faz o
    # roteador oferecer algo que morre na hora de rodar (lição das job tools).
    if _cred_store is not None:
        from core.credentials.tools import VAULT_TOOLS, VaultToolExecutor
        from core.mailbox import EMAIL_SEND_TOOLS, EMAIL_TOOLS, MailboxToolExecutor

        _tool_registry.register_many(VAULT_TOOLS)
        _tool_executor.register_module_executor("vault", VaultToolExecutor(_cred_store))
        _email_tools = list(EMAIL_TOOLS)
        if settings.neo_email_send_enabled:
            _email_tools += EMAIL_SEND_TOOLS
        _tool_registry.register_many(_email_tools)
        _tool_executor.register_module_executor("email", MailboxToolExecutor(_cred_store))
        _nomes = "/".join(t.name for t in _email_tools)
        print(f"✅ Cofre wired (vault_list + {_nomes})")

    # Service Gateway (docs/service-gateway-dsl-plan.md): HTTP arbitrário para
    # serviços cadastrados (HA, GitHub, …) sem o LLM ver credencial. Registry
    # (db + seed das envs), política deny>confirm>allow, broker env://.
    if settings.neo_gateway_enabled:
        from core.gateway import build_gateway_service
        from modules.services import SERVICE_TOOLS, ServicesExecutor

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
                    # GETTER: o File Store nasce mais abaixo neste mesmo bootstrap
                    # (o JobManager vem antes), então quem resolve é o runner, na
                    # hora de guardar o print. Store off = task sem print, só isso.
                    get_file_store=lambda: getattr(app.state, "file_store", None),
                    # Mesmo motivo do getter acima: o cofre nasce antes, mas o
                    # runner só precisa dele na hora de decifrar a credencial.
                    get_credential_store=lambda: getattr(app.state, "credential_store", None),
                    default_screenshots=settings.browser_screenshots_default,
                    max_screenshots=settings.browser_max_screenshots,
                )
                print(f"✅ Browser runner wired (url={settings.browser_agent_url})")
            # Runner do navegador LOCAL (kind=browser_local) — dirige o navegador
            # da máquina do usuário pelo canal de devices (docs/browser-local-plan.md).
            _browser_local_runner = None
            if settings.browser_local_enabled:
                from core.jobs import build_browser_local_runner

                _browser_local_runner = build_browser_local_runner(
                    db=_db,
                    llm_router=_llm_router,
                    model=settings.browser_local_model,
                    max_steps=settings.browser_local_max_steps,
                    job_timeout_s=settings.browser_local_job_timeout,
                    blocked_domains=tuple(
                        d.strip()
                        for d in settings.browser_local_blocked_domains.split(",")
                        if d.strip()
                    ),
                    confirm_sensitive=settings.browser_local_confirm_sensitive,
                    # Mesmo getter do irmão: o File Store nasce depois do JobManager.
                    get_file_store=lambda: getattr(app.state, "file_store", None),
                    max_screenshots=settings.browser_local_max_screenshots,
                    trace_dir=settings.browser_local_trace_dir,
                    # O olho do driver. None = modo texto (o comportamento antigo).
                    vision_model=(
                        settings.browser_local_vision_model
                        if settings.browser_local_vision
                        else None
                    ),
                )
                _vision = (
                    settings.browser_local_vision_model
                    if settings.browser_local_vision
                    else "off"
                )
                print(
                    f"✅ Browser LOCAL runner wired (model={settings.browser_local_model}, "
                    f"visão={_vision})"
                )
            # Vigia de download: laço em código, sem LLM no meio. Só existe se o
            # navegador local existe — é ele quem enfileira o job.
            _download_watch_runner = None
            if settings.browser_local_enabled:
                from core.jobs import build_download_watch_runner

                _download_watch_runner = build_download_watch_runner(db=_db)
                print("✅ Download watch runner wired")
            _job_manager = JobManager(
                db=_db,
                sandbox=_workbench_sandbox,
                max_concurrent=settings.job_max_concurrent,
                tick_interval=settings.job_tick_interval,
                default_timeout=settings.job_default_timeout,
                workspace_root=settings.workbench_workspace_root,
                research_runner=_research_runner,
                browser_runner=_browser_runner,
                browser_local_runner=_browser_local_runner,
                download_watch_runner=_download_watch_runner,
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

    # ── Cadastros generativos (docs/datasets-crud-plan.md) ────────
    # Tabelas criadas em RUNTIME no schema neo_data, seis tools genéricas
    # parametrizadas por slug e um instalador que roda uma vez por skill.
    # Flag OFF = nada disto existe para o cliente (rollback = desligar).
    _dataset_registry = None
    _skill_installer = None
    if settings.neo_datasets_enabled:
        from core.datasets import DatasetRegistry
        from core.skills.installer import SkillInstaller
        from modules.datasets import ALL_DATASET_TOOLS, DatasetsExecutor

        _dataset_registry = DatasetRegistry(db=_db)
        # Registradas no registry global para DESPACHAR; ficam fora do read set
        # do cognitivo — quem as destrava é o skill_read (tool gating).
        _tool_registry.register_many(ALL_DATASET_TOOLS)
        _tool_executor.register_module_executor(
            "datasets", DatasetsExecutor(db=_db, registry=_dataset_registry)
        )
        app.state.dataset_registry = _dataset_registry
        print(f"✅ Datasets wired ({len(ALL_DATASET_TOOLS)} tools data_*, schema neo_data)")

    # ── Agregador de tools (docs/datasets-crud-plan.md §2) ────────
    # Espelha TUDO que foi registrado até aqui e dá endereço único a cada tool,
    # venha ela de modules/*, do gerador de cadastros ou de uma skill instalada.
    # É o que `SkillLibrary.tools_for` consulta para o gating.
    from core.tools.aggregator import get_aggregator

    _aggregator = get_aggregator()
    _mirrored = _aggregator.mirror_registry(_tool_registry)
    if _dataset_registry is not None:
        from modules.datasets import ALL_DATASET_TOOLS as _DS_TOOLS

        _aggregator.bind_many(
            _DS_TOOLS, origin="dataset", source="core/datasets", tags=["cadastro", "crud"],
            replace=True,
        )
    if settings.neo_tool_index_enabled:
        from modules.tool_index import TOOL_INDEX_TOOLS, ToolIndexExecutor

        _tool_registry.register_many(TOOL_INDEX_TOOLS)
        _tool_index_executor = ToolIndexExecutor(aggregator=_aggregator)
        # O dispatcher é o executor GLOBAL aqui; no caminho cognitivo o
        # CognitiveReadExecutor injeta a si mesmo, para que tool_run alcance os
        # executores user-scoped (senão fin_*/data_* voltariam vazias).
        _tool_index_executor.set_dispatcher(_tool_executor.execute_tool_call)
        _tool_executor.register_module_executor("tool_index", _tool_index_executor)
        _aggregator.bind_many(
            TOOL_INDEX_TOOLS, origin="core", source="modules/tool_index", tags=["meta"],
            replace=True,
        )
    app.state.tool_aggregator = _aggregator
    print(f"✅ Tool aggregator ready ({_mirrored} espelhadas · {_aggregator.stats()})")

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
            "skills_library", SkillsLibraryExecutor(_skill_library, _dataset_registry)
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
        # As tools gated (sql_*, fin_*) foram registradas ACIMA do load — o
        # agregador precisa de uma segunda passada para indexá-las.
        _aggregator.mirror_registry(_tool_registry)
        if _dataset_registry is not None:
            try:
                _known = await _dataset_registry.all()
                _skill_library.set_known_datasets([d.slug for d in _known])
                _skill_installer = SkillInstaller(
                    db=_db,
                    library=_skill_library,
                    dataset_registry=_dataset_registry,
                    aggregator=_aggregator,
                    settings=settings,
                )
                app.state.skill_installer = _skill_installer
                print(f"✅ Skill installer ready ({len(_known)} cadastro(s) conhecido(s))")
            except Exception as _exc:  # noqa: BLE001 — cadastro fora do ar não derruba o boot
                print(f"⚠️  Datasets indisponíveis no boot ({_exc}) — gate de datasets fica aberto")
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

    # Mercado Livre (docs/product-search-plan.md §2). É a ÚNICA porta pro ML: o
    # site responde 403 a web_fetch e o browser real toma reCAPTCHA. Registrado
    # só com credencial — sem ela as tools sumiriam do catálogo em vez de falhar
    # no meio do turno.
    if settings.ml_app_id and settings.ml_client_secret:
        from modules.meli import MELI_TOOLS, MeliClient, MeliExecutor

        _tool_registry.register_many(MELI_TOOLS)
        _tool_executor.register_module_executor(
            "meli",
            MeliExecutor(
                MeliClient(
                    app_id=settings.ml_app_id,
                    client_secret=settings.ml_client_secret,
                )
            ),
        )
        print("✅ Mercado Livre wired (catálogo: fotos/atributos/variações; sem preço)")
    else:
        print("⏭️  Mercado Livre OFF (ML_APP_ID/ML_CLIENT_SECRET ausentes no .env da raiz)")

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

    # ── Memória de trabalho — o caderno da tarefa (docs/working-memory-plan.md)
    # Não registra executor global: as tools wm_* são user-scoped e saem do
    # CognitiveReadExecutor (mesmo caminho de pks_remember/conversation_recall).
    # O store aqui serve ao ÍNDICE que o seletor injeta no prompt, e ao GC.
    _working_memory = None
    _wm_gc_task = None
    if settings.working_memory_enabled:
        from core.working_memory import WorkingMemoryStore
        from core.working_memory.gc import gc_loop as wm_gc_loop

        _working_memory = WorkingMemoryStore(
            db=_db,
            max_note_bytes=settings.working_memory_note_max_bytes,
            ttl_days=settings.working_memory_ttl_days,
            max_index_notes=settings.working_memory_max_notes_in_index,
            index_auto_quota=settings.working_memory_index_auto_quota,
            read_tail_entries=settings.working_memory_read_tail_entries,
        )
        app.state.working_memory = _working_memory
        _wm_gc_task = asyncio.create_task(
            wm_gc_loop(_working_memory, settings.working_memory_gc_interval_s)
        )
        app.state.working_memory_gc_task = _wm_gc_task
        print("✅ Memória de trabalho ativa (tools wm_*, índice no prompt)")

    # ── Context Selector ─────────────────────────────────────────
    context_selector = PromptContextSelector(
        token_counter=token_counter,
        settings=settings,
        pks=_pks,
        preference_extractor=preference_extractor,
        working_memory=_working_memory,
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
        dataset_registry=_dataset_registry,
        file_store=_file_store,
        # Identidade: com uma caixa cadastrada, o Neo sabe o próprio email sem
        # precisar chamar tool para descobrir (ver _neo_identity_block).
        credential_store=_cred_store,
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
    # Cadastros + instalador (docs/datasets-crud-plan.md). Com a flag off os dois
    # ficam None e as rotas respondem 503 — a feature não existe para o cliente.
    datasets.set_dependencies(_dataset_registry, db=_db, installer=_skill_installer)

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

    if _wm_gc_task is not None:
        _wm_gc_task.cancel()
        try:
            await _wm_gc_task
        except (asyncio.CancelledError, Exception):
            pass
        print("✅ Working memory GC stopped")

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
app.include_router(datasets.install_router)
app.include_router(datasets.router)
app.include_router(skills.router, prefix="/api/v1", dependencies=[Depends(require_panel_jwt)])
# Spotify OAuth: open (the callback is a browser redirect from Spotify, no JWT);
# identity is bound via a signed `state`. See api/routes/spotify.py.
app.include_router(spotify.router, prefix="/api/v1")
app.include_router(letta.router)
