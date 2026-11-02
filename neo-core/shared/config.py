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
    agent_stall_timeout: int = Field(
        default=180, ge=30,
        description=(
            "Segundos SEM AVANÇO até o agente desistir — não é prazo desde o início. "
            "Avanço = resposta do LLM, resultado de tool ou progresso do sandbox. Era "
            "um relógio de 300s (agent_global_timeout) e na session-d35696d4 matou um "
            "agente que já tinha rodado 5 consultas e estava na 6ª ida ao modelo. "
            "Fica abaixo dos 240s de _STALL_WINDOW_S do gRPC: quem desiste primeiro "
            "é o agente, que ainda consegue dizer o que já apurou."
        ),
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
    pipeline_self_handoff_enabled: bool = Field(
        default=True,
        description="Self-continuation: let the cognitive core emit "
        "[[HANDOFF:continuar]] to run ITSELF again with the next concrete order, "
        "instead of ending the turn promising future work ('sigo tentando'). "
        "Requires pipeline_handoff_enabled.",
    )
    pipeline_self_handoff_max_steps: int = Field(
        default=8,
        description="Total step budget for a turn that self-continues. Larger than "
        "the plain handoff budget (a retry cycle burns steps fast) and it is the "
        "hard stop that keeps the cycle finite.",
    )
    pipeline_self_handoff_max_depth: int = Field(
        default=7,
        description="Max chain depth for self-continuation steps. Must stay under "
        "pipeline_self_handoff_max_steps or the step budget never binds.",
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
    # Escrita sob demanda no PKS (docs/pks-write-on-demand-plan.md) — a tool
    # pks_remember que o cognitivo chama quando o usuário pede explicitamente
    # para lembrar de algo. Por baixo delega ao PKSMemoryPlanner (subagente).
    pks_write_enabled: bool = Field(
        default=True,
        description="Liga a tool pks_remember (escrita sob demanda no PKS via "
        "subagente de memória). Off → a tool some do read set do cognitivo.",
    )
    pks_planner_model: str = Field(
        default="deepseek-flash",
        description="Modelo do PKSMemoryPlanner (subagente que estrutura a "
        "escrita no PKS). Explícito porque o default do provider já foi "
        "'deepseek-chat' (aposentado, dava 400 e quebrava TODA gravação). "
        "'deepseek-flash' é o id que api.deepseek.com serve hoje para a flash "
        "atual (V4.1); o 'deepseek-v4-flash' saiu da listagem em 2026-09-20.",
    )
    cognitive_max_tool_rounds: int = Field(
        default=32, ge=1, le=100,
        description="Max tool-call rounds the cognitive core may run before it must "
        "produce a final answer. Default (3) e teto (6) originais eram calibrados só "
        "pras read tools (search_pks/get_weather); depois que o workbench "
        "(neo_write_file/neo_bash/file_save) entrou no mesmo loop, tarefas de "
        "vários arquivos/downloads estouravam o budget. Alinhado com "
        "max_iterations (o teto que o AgentExecutor já usa) — uma rodada extra só "
        "custa latência quando o modelo REALMENTE chama tool.",
    )
    cognitive_reasoning_enabled: bool = Field(
        default=False,
        description=(
            "Deixar o modelo RACIOCINAR antes de cada round do loop cognitivo. "
            "Desligado por padrão: no OpenRouter o reasoning vem ligado sozinho e "
            "não respeita o max_tokens, e ele é gerado ANTES do primeiro token de "
            "texto. Medido em 2026-09-11 replayando a session-a82d344f: um round "
            "de tool custou 11,1s com raciocínio contra 3,5s sem, o turno inteiro "
            "(6 rounds) 94,7s contra 74,0s, e o primeiro token VISÍVEL da resposta "
            "final saiu em 13-40s contra 2,3-3,4s. É isso que tornava o streaming "
            "inútil: o usuário esperava o raciocínio inteiro antes de ver letra."
        ),
    )
    cognitive_stream_enabled: bool = Field(
        default=True,
        description=(
            "Transmitir o round do loop cognitivo em streaming, para o texto ir "
            "aparecendo na bolha de progresso do desktop em vez de o turno ficar "
            "mudo até o fim. Cai para a chamada bufferizada sozinho quando o round "
            "força tool (`tool_choice=required`, que alguns provedores do "
            "OpenRouter recusam com 404) e quando o stream falha antes do primeiro "
            "chunk — a escada de 429 do `complete()` continua sendo a rede."
        ),
    )

    # Workbench — o sandbox de execução do próprio Neo (docs/workbench-background-jobs-plan.md)
    workbench_enabled: bool = Field(
        default=True,
        description="Registra as tools neo_* (sandbox do Neo) e o handoff "
        "[[HANDOFF:workbench]]. Set false para desligar todo o subsistema.",
    )
    workbench_container: str = Field(
        default="neo-workbench",
        description="Nome do container Docker do sandbox (irmão do neo-core).",
    )
    workbench_docker_socket: str = Field(
        default="/var/run/docker.sock",
        description="Socket do Docker Engine (montado no neo-core) para docker exec.",
    )
    workbench_workspace_root: str = Field(
        default="/workspace",
        description="Raiz do workspace persistente dentro do sandbox.",
    )
    workbench_default_timeout: int = Field(
        default=60, ge=5, le=600,
        description="Timeout padrão (s) de um comando foreground no sandbox.",
    )
    workbench_max_output_bytes: int = Field(
        default=60_000, ge=1_000,
        description="Teto de bytes por fluxo (stdout/stderr) devolvido do sandbox.",
    )

    # Jobs — trabalho em background do Neo (JobManager + agent_job)
    jobs_enabled: bool = Field(
        default=True,
        description="Liga o JobManager (fila de jobs em background) e as tools "
        "neo_bash_background/job_*. Requer workbench_enabled.",
    )
    job_max_concurrent: int = Field(
        default=3, ge=1, le=16,
        description="Máximo de jobs rodando em paralelo no sandbox.",
    )
    job_tick_interval: int = Field(
        default=3, ge=1, le=60,
        description="Intervalo (s) do loop que reivindica jobs prontos da fila.",
    )
    job_default_timeout: int = Field(
        default=1800, ge=30, le=21600,
        description="Timeout padrão (s) de um job de sandbox em background.",
    )

    # Web search + deep research (docs/web-search-deep-research-plan.md)
    web_search_enabled: bool = Field(
        default=True,
        description="Liga as tools web_search/web_fetch no toolset do cognitivo "
        "(camada 1: busca inline SearXNG + fallback ddgs).",
    )
    searxng_url: str = Field(
        default="http://searxng:8080",
        description="URL do SearXNG self-hosted (serviço 'searxng' no compose, "
        "sem porta publicada). Precisa de formats:[html,json] no settings.yml.",
    )
    web_search_backends: str = Field(
        default="searxng,ddgs",
        description="Ordem da cadeia de backends de busca (csv). Backend que "
        "falha fica degradado 5min e a cadeia pula direto pro próximo.",
    )
    web_search_cache_ttl: int = Field(
        default=600, ge=30, le=86400,
        description="TTL (s) do cache RAM de buscas/fetches (news/day usa metade).",
    )
    web_fetch_max_chars: int = Field(
        default=8000, ge=500, le=100_000,
        description="Truncamento default do texto extraído por web_fetch.",
    )
    research_enabled: bool = Field(
        default=True,
        description="Liga a tool deep_research (pesquisa profunda em background "
        "via agent_job kind=research). Requer jobs_enabled e web_search_enabled.",
    )
    research_model: str = Field(
        default="deepseek-flash",
        description="Modelo do VOLUME do ResearchAgent (intake/plan/triage/"
        "evidência/reflect/organize). Barato por design — são muitas chamadas "
        "sobre texto de web.",
    )
    research_model_writer: str = Field(
        default="deepseek-v4-pro",
        description="Modelo FORTE do ResearchAgent (escrita por seção, resumo "
        "executivo/conclusão, crítica e revisão). São poucas chamadas e é onde a "
        "qualidade do relatório é decidida — ver docs/deep-research-v2-plan.md.",
    )

    # Leitura delegada de documentos (docs/deep-read-plan.md)
    deep_read_enabled: bool = Field(
        default=True,
        description="Liga a tool deep_read: subagentes leem documentos grandes/vários "
        "documentos por inteiro no sandbox e escrevem notas/<doc>.md + sintese.md, "
        "fora do contexto do cérebro. Requer jobs_enabled e workbench.",
    )
    deep_read_timeout: int = Field(
        default=3600, ge=120, le=21600,
        description="Teto (s) de um job de leitura; estourou, o que foi lido fica e a síntese diz o que faltou.",
    )
    deep_read_doc_concurrency: int = Field(
        default=3, ge=1, le=8,
        description="Documentos extraídos/lidos ao mesmo tempo num job.",
    )
    deep_read_llm_concurrency: int = Field(
        default=6, ge=1, le=16,
        description="Chamadas de LLM simultâneas num job (todas as partes de todos os documentos).",
    )
    deep_read_chunk_chars: int = Field(
        default=16_000, ge=4_000, le=40_000,
        description="Tamanho alvo de cada parte lida por um subagente (respeita a página).",
    )
    deep_read_max_parts_per_doc: int = Field(
        default=150, ge=1, le=1000,
        description="Partes lidas por documento; acima disto a cobertura fica parcial e é dita.",
    )
    deep_read_max_files: int = Field(
        default=30, ge=1, le=200,
        description="Documentos aceitos numa chamada de deep_read.",
    )

    # Browser Agent — subagente browser-use no backend (docs/browser-agent-plan.md)
    browser_agent_enabled: bool = Field(
        default=False,
        description="Liga a tool browser_task (Chromium no backend dirigido pelo "
        "browser-use via agent_job kind=browser). Requer jobs_enabled e o "
        "container neo-browser-agent no ar.",
    )
    browser_agent_url: str = Field(
        default="http://neo-browser-agent:5100",
        description="URL do wrapper FastAPI do neo-browser-agent (rede browser-net "
        "do compose, sem porta publicada).",
    )
    browser_task_timeout: int = Field(
        default=600, ge=60, le=3600,
        description="Timeout (s) de uma browser_task — o loop do subagente é "
        "1 chamada de LLM por passo, então minutos são o normal.",
    )
    browser_default_session: str = Field(
        default="user-default",
        description="Sessão de browser default (perfil Chromium persistente). Uma "
        "por usuário — o auth/cookies atravessa conversas; sessões nomeadas isolam "
        "contextos quando preciso.",
    )
    browser_max_steps: int = Field(
        default=40, ge=3, le=100,
        description="Teto default de passos do subagente browser-use por task.",
    )
    browser_screenshots_default: str = Field(
        default="auto",
        description="Política default de prints da tela numa browser_task: 'auto' "
        "(tira só quando dá erro/timeout — a evidência que o texto não dá), "
        "'always' (tira também no fim de uma task bem-sucedida) ou 'never'. O "
        "cérebro sobrescreve por chamada quando o usuário pede para ver.",
    )
    browser_max_screenshots: int = Field(
        default=3, ge=1, le=6,
        description="Quantos prints uma browser_task pode anexar (o do momento do "
        "erro + os passos anteriores). Vira arquivo temp no File Store.",
    )

    # Mercado Livre — app oficial (docs/product-search-plan.md §2).
    # Medido em 2026-08-19: é o ÚNICO caminho que abre o ML. Sem token a API dá
    # 403, o HTML manda pra /gz/account-verification (inclusive de IP
    # ── Navegador LOCAL (docs/browser-local-plan.md) ──
    # O Neo dirigindo o navegador da máquina do usuário, na tela dele. Default
    # OFF como todo caminho que atua para fora (mesmo critério do device_power):
    # é o Chrome do usuário, com as sessões logadas dele.
    browser_local_enabled: bool = Field(
        default=False,
        description="Liga a tool browser_local_task (navegador do usuário via "
        "/ws/devices/control + extensão neo-browser-local). Requer jobs_enabled.",
    )
    browser_local_model: str = Field(
        default="deepseek-flash",
        description="Modelo que decide o passo do navegador local. Saída curta e "
        "estruturada (um JSON por passo) — não precisa do caro.",
    )
    browser_local_vision: bool = Field(
        default=True,
        description="Manda um print da tela JUNTO com o texto a cada passo do "
        "navegador local. Sem isto o driver é cego: em 2026-08-28 ele clicou 25 "
        "vezes no mesmo botão sem perceber que a tela não mudava.",
    )
    browser_local_vision_model: str = Field(
        default="google/gemini-3.1-flash-lite",
        description="Modelo COM VISÃO que decide o passo quando browser_local_vision "
        "está ligado (substitui browser_local_model nesse caso). Precisa aceitar "
        "image_url + max_tokens/temperature. Se a chamada falhar, o driver cai "
        "sozinho para browser_local_model (texto) pelo resto da tarefa. "
        "Este é só o PADRÃO: o valor de verdade vem do painel (Configurações → "
        "Modelos → 'Navegador local — olho'), lido a cada job. Era gpt-4.1-mini "
        "até 2026-08-31, quando a chave da OpenAI ficou sem crédito e dois jobs "
        "inteiros rodaram cegos sem ninguém saber.",
    )
    browser_local_max_steps: int = Field(
        default=25,
        description="Orçamento BASE de passos por tarefa no navegador local. Menor "
        "que o do headless: cada passo é uma ida-e-volta até a máquina do usuário, "
        "e ele está olhando. Não é mais um teto fixo: cada vez que o caderno cresce "
        "o job ganha um passo de volta (até o DOBRO deste valor, que é o limite "
        "duro), e quem encerra antes é a falta de retorno — 12 passos sem nada novo "
        "no caderno, ou giro na mesma tela sem nada a entregar.",
    )
    browser_local_job_timeout: int = Field(
        default=600,
        description="Timeout (s) de uma tarefa no navegador local.",
    )
    browser_local_blocked_domains: str = Field(
        default="",
        description="Domínios onde o Neo NUNCA entra no navegador local, separados "
        "por vírgula (internet banking, painel do provedor). Sem exceção via LLM.",
    )
    browser_local_confirm_sensitive: bool = Field(
        default=True,
        description="Parar e perguntar antes de clicar em algo irreversível "
        "(comprar/pagar/enviar/excluir) no navegador local.",
    )
    browser_local_trace_dir: str = Field(
        default="logs/browser_local",
        description="Onde fica o diário de bordo de cada tarefa do navegador local "
        "(um diretório por job: trace.md + os JPEGs de cada passo). Vazio desliga. "
        "Guarda as 20 tarefas mais recentes.",
    )
    browser_local_max_screenshots: int = Field(
        default=3,
        description="Quantos prints uma tarefa do navegador local pode anexar.",
    )

    # residencial) e o browser real toma reCAPTCHA em loop.
    ml_app_id: str = Field(
        default="",
        description="AppID (client_id) do app em developers.mercadolivre.com.br. "
        "Vai no .env da RAIZ (ML_APP_ID) — o container não lê o neo-core/.env.",
    )
    ml_client_secret: str = Field(
        default="",
        description="Client Secret do mesmo app (ML_CLIENT_SECRET). Só na env da "
        "VPS — nunca no banco nem no spec_json de job.",
    )

    # Skills Library — skills declarativas SKILL.md (docs/skills-library-plan.md)
    skills_library_enabled: bool = Field(
        default=True,
        description="Liga a biblioteca de skills declarativas (SKILL.md): catálogo "
        "no prompt cognitivo + tools skill_read/skill_list. Set false para "
        "restaurar o comportamento anterior.",
    )
    skills_library_path: str = Field(
        default="/app/skills",
        description="Diretório raiz das skills declarativas (cada uma em "
        "<name>/SKILL.md). No container, ./neo-core/skills montado em /app/skills.",
    )
    # ── Cadastros generativos (docs/datasets-crud-plan.md) ─────────────
    neo_datasets_enabled: bool = Field(
        default=False,
        description="Liga os cadastros generativos: tabelas em runtime no schema "
        "neo_data, tools data_*, instalador de skills e tela /dados do painel. "
        "Default OFF — rollback é desligar a flag.",
    )
    # ── Agenda / cron (docs/scheduler-plan.md) ─────────────────────────
    neo_scheduler_enabled: bool = Field(
        default=False,
        description="Liga a agenda: tabela scheduled_task, loop SchedulerTick, "
        "tools schedule_* e a tela /agenda do painel. Default OFF — com a flag "
        "desligada nada é disparado e nenhuma tarefa é criada.",
    )
    scheduler_tick_interval: int = Field(
        default=30, ge=5, le=300,
        description="Intervalo (s) do loop que procura tarefas vencidas. Não "
        "precisa ser curto: o disparo é materializado como job e quem corre "
        "atrás dele é o JobManager.",
    )
    scheduler_timezone: str = Field(
        default="America/Fortaleza",
        description="Fuso padrão das tarefas agendadas (o mesmo do módulo de "
        "finanças). Cada tarefa pode ter o seu.",
    )

    # ── Barramento de eventos (docs/playlists-skill-plan.md §7) ────────
    neo_events_enabled: bool = Field(
        default=False,
        description="Liga as inscrições de evento: tools event_*, endpoint "
        "/api/v1/eventos/midia e o despacho automático da próxima faixa de uma "
        "fila. Default OFF — com a flag desligada nenhum evento é aceito e "
        "nenhuma inscrição é criada (o pedido avulso de música fica idêntico).",
    )
    neo_tool_index_enabled: bool = Field(
        default=False,
        description="Coloca tool_find/tool_run no read set do cognitivo. O ÍNDICE é "
        "montado de qualquer jeito (o gating por skill usa ele); esta flag controla só "
        "se as duas tools chegam ao prompt — custam ~150 tokens por turno, então nasce "
        "OFF e liga quando você quiser o dispatcher genérico.",
    )
    neo_skill_auto_install: bool = Field(
        default=False,
        description="Aplica sozinho o plano de instalação ADITIVO quando o SKILL.md "
        "muda (campo novo). Destrutivo nunca é automático. Default OFF.",
    )
    neo_skill_py_tools_enabled: bool = Field(
        default=False,
        description="Permite que uma skill traga o próprio tools.py, importado DENTRO "
        "do neo-core (banco + NEO_CRED_KEY + LAN). Instalação humana apenas — nenhuma "
        "tool do modelo cria skill com tools.py. Default OFF.",
    )
    skills_discovery_default: str = Field(
        default="always",
        description="Modo de descoberta das skills que não declaram 'discovery:' — "
        "'always' (comportamento histórico: 1 linha de catálogo em toda mensagem) ou "
        "'keyword' (só quando a mensagem casa; economiza tokens com muitas skills).",
    )
    skills_catalog_max_entries: int = Field(
        default=30, ge=1, le=200,
        description="Máximo de skills no catálogo do prompt cognitivo; acima disso "
        "o catálogo é prefiltrado por keywords da mensagem (skill_list cobre o resto).",
    )
    skill_read_max_bytes: int = Field(
        default=16_384, ge=1_024,
        description="Teto de bytes devolvidos por skill_read (corpo ou reference).",
    )
    skills_router_note_enabled: bool = Field(
        default=True,
        description="Injeta no prompt do classificador a lista de assuntos "
        "cobertos por skills do cérebro (→ DIRECT_RESPONSE), evitando misroute "
        "para CLIENT_TOOL/run_command.",
    )

    # SQL read-only (skill postgres-neo — tool gated via skills library)
    sqlro_dsn: str = Field(
        default="",
        description="DSN asyncpg para sql_query_ro (ideal: role só-leitura, ex. "
        "postgresql://neo_ro:...@neo-postgres:5432/neodb). Vazio → usa database_url; "
        "a sessão roda com default_transaction_read_only=on em ambos os casos.",
    )
    sqlro_statement_timeout_ms: int = Field(
        default=5_000, ge=100, le=60_000,
        description="statement_timeout (ms) da sessão sql_query_ro.",
    )
    sqlro_max_rows: int = Field(
        default=200, ge=1, le=2_000,
        description="Teto absoluto de linhas devolvidas por sql_query_ro.",
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
        default=30, ge=1,
        description="Minutes of inactivity before auto-closing a LIVE (voice) session",
    )
    #: O chat não é uma ligação telefônica: um intervalo de 30min nele é normal
    #: (você sai, volta e continua a MESMA conversa), enquanto no live ele quer
    #: mesmo dizer que a conversa acabou. Medido no corpus de 536 sessões: das 28
    #: nascidas do relógio, ~metade das de chat abria com uma continuação órfã
    #: ("tenta de novo", "converte o preço da ponte que eu tinha pedido") contra
    #: ZERO das de live — cujas aberturas são todas partidas do zero ("Alô, você
    #: está me ouvindo?", "Testando o módulo de voz"). Daí o relógio curto valer
    #: só para o live. Este backstop longo existe porque `close_session` é quem
    #: gera `topic_summary`/`topic_embedding`: sem NENHUMA expiração eles nunca
    #: seriam produzidos. Use 0 para nunca expirar por tempo fora do live.
    session_inactivity_chat_minutes: int = Field(
        default=720, ge=0,
        description="Minutes of inactivity before auto-closing a non-live session (0 = never)",
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

    # ── Janela de histórico da sessão (docs/context-recall-fixes.md §2) ──
    #: Quanto da conversa ATUAL volta para o classificador e para o cérebro a
    #: cada turno. Era `limit=20, max_tokens_budget=3000` cravado no código, e
    #: 3000 é MENOS que uma resposta longa do próprio Neo: na sessão 7330e40c a
    #: "Reanálise Completa" tinha 3.982 tokens — não cabia nem sozinha. A partir
    #: do turno seguinte ela era matematicamente inalcançável, e o Neo passou 13
    #: turnos jurando que aquela análise não existia, até começar a ler arquivos
    #: do disco e apresentá-los como "a mensagem que eu postei no chat". A
    #: sessão INTEIRA tinha 14.498 tokens — cabia folgada em qualquer modelo que
    #: usamos. O teto nunca foi do LLM; era este número.
    #: Custo: o histórico vai em DOIS prompts por turno (classificador +
    #: cognitivo), então subir isto sobe token de entrada nos dois. Com cache de
    #: prompt o prefixo estável é barato; ainda assim, é o botão a girar se a
    #: latência ou a conta subirem.
    history_limit_messages: int = Field(
        default=40, ge=2,
        description="Max messages pulled from the current session for LLM context",
    )
    history_max_tokens_budget: int = Field(
        default=24000, ge=500,
        description="Token budget for the current-session history window",
    )
    #: Quantas mensagens grandes demais podem virar um marcador de omissão antes
    #: de a varredura parar de vez. O marcador existe para o modelo SABER que
    #: algo foi cortado — silêncio é o que o fazia inventar.
    history_max_elisions: int = Field(
        default=3, ge=0,
        description="Max oversized messages replaced by an elision marker before truncating",
    )
    #: Resgate semântico DENTRO da sessão: quando a conversa passa deste
    #: tamanho, as mensagens que não couberam na janela ainda podem voltar por
    #: similaridade com o que o usuário acabou de perguntar.
    history_semantic_merge_min_messages: int = Field(
        default=12, ge=2,
        description="Session size above which the in-session semantic merge runs",
    )
    history_semantic_merge_limit: int = Field(
        default=4, ge=0,
        description="Max older in-session messages re-injected by similarity",
    )
    history_semantic_merge_min_similarity: float = Field(
        default=0.35, ge=0.0, le=1.0,
        description="Minimum cosine similarity for the in-session semantic merge",
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
    context_budget_working_mem: int = Field(
        default=700, ge=0,
        description="Token budget for the working-memory index (docs/working-memory-plan.md)",
    )

    # ── Ranking do PKS (docs/pks-ranking-plan.md) ─────────────────
    pks_ranker_v2_enabled: bool = Field(
        default=False,
        description=(
            "Bloco 'Conhecimento do Usuário' e search_pks pela nota de prioridade "
            "(core/pks/ranking.py) em vez do teto de 10 por ordem de chegada. Ligar "
            "só depois do gabarito revisado (§6 do plano)."
        ),
    )
    pks_ranker_v2_shadow: bool = Field(
        default=True,
        description=(
            "Com o v2 desligado: calcula o v2 junto, injeta o antigo e registra no "
            "session log o que entraria/sairia. Nunca muda o prompt."
        ),
    )
    pks_rank_weights: dict[str, float] = Field(
        default_factory=dict,
        description="Sobrescreve pesos do ranking (nome/lex/conv/grafo/tipo/prior/sem). Vazio = os do plano.",
    )
    pks_rank_min_score: float = Field(default=0.12, ge=0.0, le=1.0)
    pks_rank_relative_cutoff: float = Field(default=0.45, ge=0.0, le=1.0)
    pks_rank_kappa: float = Field(default=6.0, gt=0.0)
    pks_rank_history_turns: int = Field(default=3, ge=0, le=10)
    pks_rank_list_cap: int = Field(default=20, ge=1)
    # Orçamento ELÁSTICO do bloco (tokens). Medido em 2150 turnos reais + gabarito
    # (docs/pks-ranking-plan.md §12): todo grupo cabe na BASE; só grupo forte
    # (nota ≥ strong_ratio × a melhor) usa a extensão até o TETO. Fixo em 880,
    # 14 grupos fortes eram cortados; fixo em 1600, entravam 101 grupos de cauda.
    pks_rank_budget_base: int = Field(default=600, ge=100)
    pks_rank_budget_max: int = Field(default=1600, ge=100)
    pks_rank_strong_ratio: float = Field(default=0.7, ge=0.0, le=1.0)

    # ── Memória de trabalho — o caderno da tarefa (docs/working-memory-plan.md) ──
    # Terceiro tipo de memória, entre a sessão (volátil) e o PKS (permanente): o
    # Neo grava a CONCLUSÃO do trabalho que acabou de fazer (número apurado,
    # formato do arquivo, decisão, caminho de artefato) para o turno seguinte não
    # precisar reler nem reprocessar. Índice no prompt + corpo sob demanda.
    working_memory_enabled: bool = Field(
        default=False,
        description="Liga o caderno de trabalho: tools wm_write/wm_read + índice no "
        "system prompt + regra 12 no CORE. Off → prompt e toolset idênticos aos de antes.",
    )
    working_memory_autonote_enabled: bool = Field(
        default=True,
        description="Backstop determinístico (sem LLM): turno que rodou tool pesada e NÃO "
        "chamou wm_write ganha um append automático com o tool_trace. Requer working_memory_enabled.",
    )
    working_memory_max_notes_in_index: int = Field(
        default=8, ge=1,
        description="Máximo de notas ativas listadas no índice injetado no prompt",
    )
    working_memory_index_auto_quota: int = Field(
        default=2, ge=0,
        description="Quantas vagas do índice uma nota AUTOMÁTICA de outra sessão pode ocupar. "
        "O resumo delas é fixo ('registro automático…'), então são indistinguíveis entre si: "
        "sem teto, o backstop (uma nota por sessão, tocada todo turno) toma o índice inteiro na "
        "recência e expulsa as notas que o modelo escreveu de propósito. 0 desliga o enchimento.",
    )
    working_memory_note_max_bytes: int = Field(
        default=262144, ge=1024,
        description=(
            "Teto do corpo de uma nota; ao estourar, as entradas MAIS ANTIGAS caem. "
            "256 KB e não 32 KB: as entradas passaram a carregar o resultado farto "
            "(stdout inteiro, resumo de job, links) e com o teto antigo escrever mais "
            "significava EXPULSAR o histórico mais rápido. O corpo é texto no Postgres "
            "e não entra em prompt nenhum — quem paga contexto é o índice (240 chars "
            "por nota) e o wm_read, que tem orçamento próprio (TAIL_MAX_CHARS)."
        ),
    )
    working_memory_ttl_days: int = Field(
        default=30, ge=1,
        description="TTL de toque: nota sem escrita por esse tempo é arquivada pelo GC (pinned não expira)",
    )
    working_memory_read_tail_entries: int = Field(
        default=4, ge=1,
        description="Quantas entradas o wm_read devolve por padrão (full=true traz tudo)",
    )
    working_memory_gc_interval_s: int = Field(
        default=21600, ge=60,
        description="Intervalo do GC do caderno de trabalho (arquiva nota vencida)",
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
    # This runs fire-and-forget OFF the reply hot-path, so a generous cap costs
    # nothing on latency — 12s was too tight for a remote LLM under load and the
    # appraisal was timing out on nearly every turn (panel "Appraiser degradado"),
    # which froze the affect state at baseline because only the floors ever landed.
    neo_affect_appraisal_timeout_s: float = Field(default=25.0, gt=0.0)

    # ── Afeto no roteamento (docs/modo-birra-plan.md §3.7) ──────────────
    # A skill de casa é CEGA a emoção (só neo_persona("slim") + prompt.md), então
    # com o Neo irritado "acende a luz" volta simpático e a personalidade quebra
    # num comando simples. Duas correções, independentes do Modo Birra:
    #   1. desviar o turno para o núcleo cognitivo quando o estado pode mudar a
    #      RESPOSTA CERTA (não só o tom);
    #   2. colar a diretiva de afeto no prompt da skill, para a mensagem de
    #      confirmação — que é a última coisa que o usuário lê — ter tom.
    # Default conservador: nada muda em prod até medir com scripts/eval_router.py.
    neo_affect_route_to_brain: str = Field(
        default="never",
        description="never | decision_changing (ira/medo fortes ou postura ativa) | "
        "any_salient (qualquer eixo fora do repouso). Birra armada desvia mesmo em 'never'.",
    )
    neo_affect_router_hint_enabled: bool = Field(
        default=False,
        description="Injeta o bloco de estado emocional (valores + níveis) no prompt do "
        "classificador. Ausente quando o Neo está no repouso — prompt idêntico ao de hoje.",
    )
    neo_affect_skill_directive_enabled: bool = Field(
        default=False,
        description="Cola a diretiva de tom do afeto no prompt da skill home_assistant",
    )

    # ── Modo Birra (docs/modo-birra-plan.md) ────────────────────────────
    # Com a ira no teto E maltrato acumulado, pedido BOBO de casa pode ser
    # recusado ou feito diferente até o usuário pedir desculpa. Só light e
    # climate.set_temperature, sempre declarado, sempre reversível.
    neo_birra_enabled: bool = Field(
        default=False, description="Kill switch geral do Modo Birra"
    )
    neo_birra_arm_ira: float = Field(default=60.0, ge=0.0, le=100.0)
    # Histerese: arma em 60, desarma em 45 — sem isso a birra piscaria a cada
    # oscilação do appraiser.
    neo_birra_disarm_ira: float = Field(default=45.0, ge=0.0, le=100.0)
    # Ofensas na janela de rancor. Discussão chata (trigger "obtuse") NÃO abre
    # rancor, então teimosia sozinha nunca arma birra — só maltrato.
    neo_birra_min_offenses: int = Field(default=2, ge=1)
    neo_birra_max_physical_acts: int = Field(default=2, ge=0)
    neo_birra_physical_cooldown_min: float = Field(default=10.0, ge=0.0)
    neo_birra_revert_after_s: int = Field(default=120, ge=10)
    neo_birra_max_episode_min: float = Field(default=45.0, gt=0.0)
    # Janela só-verbal: nada de alteração física nem aviso proativo de madrugada.
    neo_birra_quiet_hours: str = Field(default="22-7")
    neo_birra_temp_delta: float = Field(default=2.0, ge=0.0)
    neo_birra_temp_clamp: str = Field(default="22,26")
    neo_birra_entity_allow: str = Field(
        default="", description="CSV de entity_ids liberados FORA do allowlist de domínio"
    )
    neo_birra_entity_deny: str = Field(
        default="", description="CSV de entity_ids nunca sujeitos a birra"
    )

    # ── Neo situational register (tone-regulation layer) ────────────────
    # Second tone regulator, orthogonal to affect: Caráter × Registro × Afeto.
    # The register is derived deterministically per turn (no new LLM call) and
    # sets the CEILING for humor/ceremony/verbosity/initiative; the emotional
    # state moves inside it. See docs/context-register-plan.md.
    neo_register_enabled: bool = Field(
        default=False,
        description="Inject the situational register layer and clamp the affect directive",
    )
    neo_register_shadow: bool = Field(
        default=True,
        description="Derive + log the register per turn WITHOUT injecting (calibration mode; no-op when neo_register_enabled)",
    )

    # Service Gateway (docs/service-gateway-dsl-plan.md) — HTTP arbitrário para
    # serviços cadastrados sem o LLM ver credencial. Default off (padrão da casa).
    neo_gateway_enabled: bool = Field(
        default=False,
        description="Liga o Service Gateway: tools service_list/service_docs/service_request/"
        "service_confirm + registro de conexões (host/auth por referência) + política "
        "deny>confirm>allow. Off restaura o comportamento anterior (só tools curadas).",
    )

    # Cofre de credenciais (F2 do gateway + F3 do browser-agent — store ÚNICO).
    # Chave Fernet base64 (32 bytes). Vazia → sem cofre: 'fernet://' recusa com
    # mensagem clara e só 'env://' funciona. Gere com:
    #   python -c "from core.credentials import generate_credential_key as g; print(g())"
    neo_cred_key: str = Field(
        default="",
        description="Chave Fernet do cofre de credenciais (NEO_CRED_KEY). Só na env "
        "da VPS — nunca no banco. Trocar a chave invalida os segredos já cifrados.",
    )
    neo_email_send_enabled: bool = Field(
        default=True,
        description="Deixa o cérebro ENVIAR email pela caixa do próprio Neo (tool "
        "`email_send`, SMTP com a mesma senha de app do IMAP). SEPARADA da leitura de "
        "propósito: ler é reversível, um email enviado não volta. O `From` é sempre o "
        "endereço do Neo — ele nunca escreve em nome do usuário. Off → a tool some do "
        "registry e do toolset do cérebro; `email_inbox` continua funcionando.",
    )

    # File Store (docs/file-store-plan.md §12) — entrada/catálogo de arquivos.
    neo_files_enabled: bool = Field(
        default=False,
        description="Liga o File Store: tools file_* + rotas /api/v1/files + GC. "
        "Off → tools somem do registry e as rotas devolvem 503.",
    )
    neo_files_root: str = Field(
        default="/data/files",
        description="Raiz do store no neo-core (volume neo-files, rw): blobs/ + derived/",
    )
    neo_files_sandbox_mount: str = Field(
        default="/files",
        description="Onde o volume do store aparece DENTRO do neo-workbench (read-only) — "
        "usado pelo file_stage para copiar o blob sem passar pelo core",
    )
    neo_files_ttl_days: int = Field(
        default=7, ge=1,
        description="Validade de um arquivo 'temp' (o GC expira depois disso)",
    )
    neo_files_max_file_mb: int = Field(
        default=500, ge=1,
        description="Tamanho máximo por arquivo no upload/ingestão",
    )
    neo_files_quota_gb: float = Field(
        default=2.0, gt=0,
        description="Quota total do store (uso real em disco, dedup por sha256)",
    )
    neo_files_read_cap_chars: int = Field(
        default=8000, ge=500,
        description="Teto de caracteres por chamada de file_read (economia de contexto)",
    )
    neo_files_gc_interval_s: int = Field(
        default=3600, ge=60,
        description="Intervalo do tick do GC de arquivos temporários expirados",
    )
    neo_file_actuation_enabled: bool = Field(
        default=False,
        description="Deixa o núcleo cognitivo OBTER/ENTREGAR arquivos por conta própria: "
        "as tools client-actuating request_user_file (puxa um arquivo do disco do "
        "usuário pro File Store) e save_generated_file (entrega no disco) entram no "
        "toolset do cérebro e viajam pelo round-trip tool_request/tool_result. "
        "Requer neo_files_enabled. Off → o cérebro não vê essas tools.",
    )
    neo_brain_shell_enabled: bool = Field(
        default=True,
        description="Deixa o núcleo cognitivo RODAR comando no terminal do usuário por "
        "conta própria (tool client-actuating `run_command`, mesmo round-trip de "
        "save_generated_file, com a mesma aprovação do fluxo CLIENT_TOOL). Sem isso o "
        "cérebro é um beco sem saída quando o classificador manda pra ele um pedido que "
        "precisa da máquina do usuário: ele responde 'não tenho acesso ao terminal, roda "
        "aí e me manda a saída' (sessão 69feab87). Off → o cérebro volta a só sugerir.",
    )

    # ── Liberação de informação (docs/release-request-plan.md) ─────────
    neo_pks_redaction_enabled: bool = Field(
        default=True,
        description="Campo de identificação do PKS (cpf, endereço, cep, telefone, "
        "e-mail, cartão, saldo, senha) sai do prompt e do resultado do search_pks, "
        "trocado por um endereço `«pks:<campo>»`. O valor volta sob demanda pela tool "
        "`pks_reveal` — que num turno contaminado passa pelo portão. Off → o valor "
        "viaja inteiro em todo turno que recuperar a entidade, como era antes.",
    )
    neo_paste_taint_enabled: bool = Field(
        default=True,
        description="Texto COLADO acima do limiar marca o turno como contaminado "
        "(fonte `colagem`). É o fio que faltava: conteúdo de terceiro que chega por "
        "tool (página, e-mail, PDF) contamina, mas colado entrava como se fosse o "
        "próprio usuário falando — `principal=admin, contaminado=não` — e todo o "
        "portão ficava inerte, inclusive para 'me dê o CPF e manda para x@y.com'. "
        "Com isto, revelar passa a pedir o card (pks_reveal, email, WhatsApp, "
        "gateway). Off → volta a confiar em tudo que foi colado.",
    )
    neo_paste_taint_min_chars: int = Field(
        default=240, ge=1,
        description="A partir de quantos caracteres colados o turno é marcado. Colar "
        "um link, um nome ou uma linha de erro não é 'bloco de terceiro' — e marcar "
        "isso só geraria card em cima de uso legítimo. O cliente informa quantos "
        "caracteres vieram do clipboard (`pasted_chars` no metadata).",
    )
    neo_release_gate_mode: str = Field(
        default="shadow",
        description="Portão de autoridade do turno (core/authority). Três degraus: "
        "'shadow' (padrão) não barra nada e só registra '[authority][sombra]'; "
        "'revelacao' barra o que ENTREGA a terceiro (email_send, whatsapp, gateway, "
        "web_fetch) mais os 'negar' explícitos, e deixa ATUAÇÃO (terminal, sandbox, "
        "casa, memória) em sombra — porque revelação tem caminho de recuperação "
        "(solicitar_dado → card → sua aprovação) e atuação ainda não tem; "
        "'enforce' vale para tudo.",
    )

    # ── Controle de devices (docs/device-control-plan.md) ──────────────
    neo_device_control_enabled: bool = Field(
        default=True,
        description="Deixa o cérebro CONTROLAR dispositivos do usuário (tool "
        "`device_control`: volume, mudo, play/pause, faixa, bloquear tela), inclusive "
        "um dispositivo que NÃO é o desta conversa — 'abaixa o volume do PC da sala' "
        "dito no celular. Roda pelo canal /ws/devices/control, que exige token de "
        "pareamento. Off → o cérebro não vê a tool.",
    )
    neo_device_power_enabled: bool = Field(
        default=False,
        description="Habilita a tool `device_power` (suspend/shutdown/restart de um "
        "dispositivo). SEPARADA de device_control de propósito: desligar a máquina "
        "errada é irreversível de um jeito que abaixar o volume não é. O aparelho alvo "
        "sempre pede confirmação na própria tela antes de executar. Default OFF — ligue "
        "só depois de conferir que a :8000 não está aberta na internet.",
    )
    neo_device_auth_required: bool = Field(
        default=False,
        description="Exige token de pareamento também nos canais ANTIGOS "
        "(/ws/devices/online e /ws/devices/notifications) e no gRPC. Default OFF porque "
        "esses canais nasceram sem auth e virar a chave derruba o desktop em execução "
        "no meio de um save (o core roda --reload). O canal de CONTROLE exige token "
        "sempre, independente desta flag — ele é novo e não tem cliente antigo a "
        "quebrar. Ligue depois que todo device estiver pareado.",
    )

    # ── Auth da API (docs/api-auth-plan.md) ──────────────────────
    # O segredo assina TODOS os JWT (o legado do painel e o do login de
    # usuário). O default embutido é o de desenvolvimento: ele está no repo, e
    # quem lê o repo emite token de admin. Em produção vem do .env da RAIZ.
    panel_jwt_secret: str = Field(
        default="l5L0mJ30C7v10OTI4eEOvxShOGwhGkPSEst7eicOiFGq8qG6TgL9u4CQUr6mByjV",
        description="Segredo HS256 dos JWT (painel legado e login de usuário). "
        "TROQUE em produção: o default está no código-fonte.",
    )
    panel_jwt_subject: str = Field(
        default="neo-panel",
        description="Subject do JWT ESTÁTICO do painel (legado). Não identifica "
        "usuário — por isso não aprova liberação (release-request-plan §11).",
    )
    neo_api_auth_mode: str = Field(
        default="shadow",
        description="Portão de autenticação das rotas HTTP (api/auth/principal.py). "
        "`off` = como antes de existir auth (rollback de incidente). "
        "`shadow` (padrão) = resolve o principal, LOGA e deixa passar — mede quem "
        "chama o quê sem derrubar cliente nenhum. `enforce` = 401 sem credencial "
        "(o JWT estático do painel ainda vale nas telas de administração). "
        "`strict` = só token de aparelho pareado e login de usuário; o JWT "
        "estático é recusado. Suba os degraus nesta ordem: os clientes precisam "
        "carregar a credencial ANTES do enforce.",
    )
    neo_api_auth_secret_required: bool = Field(
        default=False,
        description="Recusa subir com `panel_jwt_secret` no default embutido. "
        "Default OFF para não travar dev/teste; ligue junto com o enforce.",
    )
    access_token_ttl_s: int = Field(
        default=1800, ge=60,
        description="Validade do access token do login de usuário (segundos).",
    )
    refresh_token_ttl_s: int = Field(
        default=60 * 60 * 24 * 30, ge=3600,
        description="Validade do refresh token (opaco, no banco, revogável).",
    )
    login_max_attempts: int = Field(
        default=8, ge=1,
        description="Tentativas de login por (usuário+IP) dentro da janela antes "
        "de recusar. Contador em memória, como o TTL do código de pareamento.",
    )
    login_attempt_window_s: int = Field(
        default=300, ge=30,
        description="Janela do contador de tentativas de login (segundos).",
    )
    signed_url_ttl_s: int = Field(
        default=300, ge=30,
        description="Validade da URL assinada de download (`/files/{id}/download`). "
        "Existe porque `<a href download>` não manda header nenhum.",
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

    # ── Conversation Recall (docs/conversation-recall-plan.md) ────
    conversation_recall_enabled: bool = Field(
        default=True,
        description=(
            "Tool `conversation_recall`: busca no arquivo de conversas passadas. "
            "Read-only; a flag existe para desligar em incidente."
        ),
    )
    conversation_recall_sessions_per_page: int = Field(
        default=3, ge=1, le=10,
        description="Conversas por página do recall (o resto vira 'foi antes ainda')",
    )
    conversation_recall_excerpts_per_session: int = Field(
        default=4, ge=1, le=10,
        description="Trechos mostrados por conversa recuperada",
    )
    #: Quantos trechos abrir quando o modelo foca numa conversa específica
    #: (`conversa="7330e40c"`). Tem que ser bem maior que o teto normal: focar
    #: existe justamente porque o que ele procura ficou FORA dos 4 primeiros.
    conversation_recall_excerpts_focused: int = Field(
        default=12, ge=2, le=40,
        description="Trechos por conversa quando o recall foca numa conversa só",
    )
    #: Tamanho do trecho de cada mensagem no bloco "Arquivo de conversas".
    #: 240 chars respondem "a gente já falou disso?" — mas NÃO respondem "me
    #: mostra aquela análise de novo", que foi o pedido da sessão 7330e40c. Com
    #: o texto cortado, a única saída do modelo é parafrasear de cabeça, que é
    #: exatamente a confabulação que se quer evitar. Daí o segundo teto: quando
    #: o modelo pede `completo=true`, as mensagens que CASARAM vêm inteiras (as
    #: vizinhas continuam curtas, que é só contexto).
    conversation_recall_excerpt_chars: int = Field(
        default=240, ge=40, le=4000,
        description="Chars por trecho no bloco de recall",
    )
    conversation_recall_full_excerpt_chars: int = Field(
        default=20000, ge=500, le=60000,
        description="Chars por trecho quando o recall é pedido com completo=true",
    )
    #: Teto SOMADO dos trechos longos num mesmo recall. O teto por mensagem
    #: sozinho não limita nada (3 sessões × 4 trechos × 20k = 240k chars); é
    #: este que garante que `completo=true` não estoure o prompt. Gasto do mais
    #: recente para o mais antigo — o que não couber volta ao trecho curto.
    conversation_recall_full_total_chars: int = Field(
        default=40000, ge=1000, le=200000,
        description="Orçamento somado dos trechos longos de um recall completo",
    )
    #: A sessão ATUAL voltou a ser pesquisável pelo recall, com corte no turno de
    #: agora. Excluí-la inteira (como era) matava o caso mais comum: pedir de
    #: volta algo que o Neo escreveu nesta mesma conversa, longe demais para
    #: caber na janela de histórico. Ver docs/context-recall-fixes.md §3.
    conversation_recall_include_current_session: bool = Field(
        default=True,
        description="Deixa o recall buscar também na conversa atual (até o turno anterior)",
    )
    #: Fatiar mensagens longas em pedaços com vetor próprio. O embedder local
    #: (all-MiniLM-L6-v2) trunca em 256 tokens SEM AVISAR — o vetor de uma
    #: mensagem de 12.811 chars representava só os ~1.000 primeiros, e a busca
    #: pela tabela que estava no offset 9.399 devolvia 83º de 2.304.
    embedding_chunk_enabled: bool = Field(
        default=True,
        description="Indexa mensagens longas em pedaços (contorna o corte de 256 tokens)",
    )
    #: 500/250 MEDIDO contra 5 pedidos reais de recall da mesma mensagem (rank
    #: do alvo entre 2.303 candidatas, menor é melhor):
    #:
    #:     sem chunk  583   |  1200/300  281   |  900/450  266
    #:     650/325    177   |  600/300   149   |  500/250   70   ← escolhido
    #:
    #: O tokenizer gasta ~2,9 chars/token em pt-BR, então 1.200 chars viram
    #: ~416 tokens e TODO pedaço era truncado de novo pelo modelo — o mesmo
    #: defeito, um nível abaixo. 500 chars ≈ 169 tokens, com folga.
    #: Calibrar num caso só engana: 900/450 ganhava no exemplo isolado (0,7334)
    #: por alinhamento sortudo da fatia com a tabela, e é medíocre na média.
    embedding_chunk_chars: int = Field(
        default=500, ge=200, le=4000,
        description="Tamanho de cada pedaço, em chars (tem que caber em 256 tokens)",
    )
    embedding_chunk_overlap: int = Field(
        default=250, ge=0, le=1000,
        description="Sobreposição entre pedaços (evita cortar a resposta no meio)",
    )
    embedding_chunk_min_chars: int = Field(
        default=1000, ge=300, le=8000,
        description="Só fatia mensagens acima disso (2,9% do corpus)",
    )
    assistant_embeddings_enabled: bool = Field(
        default=True,
        description=(
            "Embeddar as respostas do Neo em background depois de salvá-las. "
            "Sem isto, só as mensagens do usuário têm embedding e a busca "
            "vetorial nunca acha o que o próprio Neo disse."
        ),
    )

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
