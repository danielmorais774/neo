"""Semente do catálogo de LLM — o que o banco recebe quando nasce vazio.

O catálogo de providers e modelos mora no Postgres (``llm_provider`` /
``llm_model``) e é editado pelo painel (Configurações → Providers). Este arquivo
só vale em dois momentos:

1. **banco vazio** — a primeira subida depois da migração grava isto lá;
2. **tabelas ausentes** — o core subiu antes da migração (aqui salvar é deploy,
   ver neo-dev-topology-prod-tunnel): o catálogo roda em memória a partir daqui,
   igual ao que era antes de existir o CRUD, e o painel avisa que falta migrar.

Depois que o banco tem linhas, **o painel é a fonte**: mexer aqui não muda um
catálogo já semeado. Os comentários abaixo ficam porque são a história de cada
entrada (por que o pin, por que fora do seletor) — o painel tem o campo
``notes`` para o que vier depois.

``SELECTOR`` é a vitrine do chat (desktop e painel): ordem, rótulo e descrição.
Estar no mapa e não no seletor é proposital para os ids legados — o core ainda
sabe rotear uma config antiga, mas o chat não oferece.
"""


# Mapeamento de nome de modelo → provider
MODEL_TO_PROVIDER: dict[str, str] = {
    # OpenAI
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    # Família 4.1: visão + parâmetros clássicos do Chat Completions
    # (`max_tokens`/`temperature`, que é o que esta camada manda). É daqui que
    # sai o olho do navegador local — ver browser_local_vision_model.
    "gpt-4.1": "openai",
    "gpt-4.1-mini": "openai",
    "gpt-4.1-nano": "openai",
    "gpt-4-turbo": "openai",
    "gpt-4-turbo-preview": "openai",
    "gpt-3.5-turbo": "openai",
    "meta-llama/llama-4-scout-17b-16e-instruct": "openai",
    # Anthropic
    "claude-3-opus-20240229": "anthropic",
    "claude-3-sonnet-20240229": "anthropic",
    "claude-3-haiku-20240307": "anthropic",
    # DeepSeek
    # ``deepseek-flash`` é o id da flash atual (V4.1) na API direta; o
    # ``deepseek-v4-flash`` saiu da listagem em 2026-09-20 mas fica aqui para
    # que uma config antiga continue resolvendo para o provider certo.
    "deepseek-flash": "deepseek",
    "deepseek-v4-flash": "deepseek",
    "deepseek-v4-pro": "deepseek",
    "deepseek-chat": "deepseek",
    "deepseek-reasoner": "deepseek",
    # Gemini
    "gemini-2.5-pro-preview-03-25": "gemini",
    "gemini-2.0-flash": "gemini",
    "gemini-2.0-flash-lite": "gemini",
    "gemini-1.5-pro": "gemini",
    "gemini-1.5-flash": "gemini",
    # Ollama (modelos locais)
    "gemma4:e2b": "ollama",
    "gemma4:12b": "ollama",
    "llama3": "ollama",
    "llama3:8b": "ollama",
    "llama3:70b": "ollama",
    "mistral": "ollama",
    "phi4": "ollama",
    # Grok (xAI) — free plan
    "grok-3-mini": "grok",
    "grok-3-mini-fast": "grok",
    "grok-2-1212": "grok",
    # Grok (xAI) — paid
    "grok-3": "grok",
    "grok-3-fast": "grok",
    # Groq (groq.com) — ultra-fast LPU inference
    "llama-3.3-70b-versatile": "groq",
    "llama-3.1-8b-instant": "groq",
    "llama3-70b-8192": "groq",
    "mixtral-8x7b-32768": "groq",
    "gemma2-9b-it": "groq",
    # OpenRouter (openrouter.ai) — gateway unificado (IDs no formato vendor/modelo)
    "x-ai/grok-4.5": "openrouter",
    "x-ai/grok-4.20": "openrouter",
    "google/gemini-3.1-flash-lite": "openrouter",
    "google/gemini-2.0-flash-001": "openrouter",
    "anthropic/claude-3.5-sonnet": "openrouter",
    "meta-llama/llama-3.3-70b-instruct": "openrouter",
    "qwen/qwen3.5-flash-02-23": "openrouter",
    # É o que o subagente browser-use do servidor (container neo-browser-agent)
    # já usava, cravado no compose. Entrou no catálogo quando virou o role
    # `browser_agent` do painel — sem estar aqui, o valor em uso aparecia como
    # "custom" no dropdown, como se ninguém o tivesse escolhido.
    "google/gemini-2.5-flash": "openrouter",
    # Sufixo "@slug" = pin de provedor de infra, consumido pelo OpenRouterProvider
    # (ver _PROVIDER_PIN_SEP lá) e nunca enviado à API. O mesmo modelo tem dezenas
    # de endpoints no gateway e eles NÃO são equivalentes; sem o pin o gateway
    # escolheria outro a cada chamada, com outro preço, outro teto e outra
    # velocidade. É texto puro (não lê imagem), então fica fora de VISION_MODELS
    # de propósito.
    #
    # O V4.1 Flash **não tem endpoint do Baidu** — o pin que o V4 Flash usava não
    # existe nesta geração. Sondados os 22 endpoints em 2026-09-20 (2 chamadas de
    # ~200 tokens cada, mais uma com `tool_choice` forçado):
    #   @fireworks  85/69 tok/s   tool forçada OK   $0,22/$0,66 por M
    #   @streamlake 46/59 tok/s   tool forçada OK   $0,22/$0,88
    #   @deepinfra  49/24 tok/s   tool forçada OK   $0,14/$0,42
    #   @relace / @morph          404 na tool forçada (não servem o caminho quente)
    #   @deepseek                 404 "No endpoints found" (não roteia por aqui)
    #   sem pin     50/105 tok/s, sorteando provedor a cada chamada
    # O @fireworks é o único que empata com os 74-80 tok/s que o @baidu entregava
    # no V4 Flash E aceita tool forçada — por isso é ele o pin do catálogo.
    "deepseek/deepseek-v4.1-flash@fireworks": "openrouter",
    # O MESMO modelo sem pin. Precisa estar aqui explicitamente, e não só como
    # opção do seletor: sem esta linha `get_provider_for_model` não acha o ID,
    # cai no provider default e o turno vai parar em `api.deepseek.com` — que é
    # outra carteira, hoje com saldo zero (402 "Insufficient Balance"). O ID com
    # barra é do GATEWAY; o sem barra (`deepseek-flash`, acima) é que é a API
    # direta da DeepSeek. Eles não são intercambiáveis.
    "deepseek/deepseek-v4.1-flash": "openrouter",
    # Geração anterior. Continua no mapa (e não no seletor) porque é ~6x mais
    # barata na entrada e é para onde se volta se a V4.1 decepcionar.
    "deepseek/deepseek-v4-flash@baidu": "openrouter",
    "deepseek/deepseek-v4-flash": "openrouter",
    # NVIDIA Nemotron 3.5 (listagem do gateway em 2026-11-05). Sem pin: nenhum
    # endpoint foi medido ainda, e sem pin o gateway só sorteia entre os que
    # aceitam os parâmetros da chamada (tool forçada inclusive).
    #   super-vl   120B/12B ativos, lê imagem e vídeo, 9 endpoints, 1M de
    #              contexto em 4 deles (262k nos outros), $0,06-0,30 / $0,35-0,90 por M
    #   lightning  30B/3B ativos, só texto, 262k, 5 endpoints, $0,025-0,07 /
    #              $0,12-0,20 por M; o @fireworks não aceita `tool_choice`
    # Ficam de fora o `content-safety` (classificador, sem tools) e as variantes
    # `:free` (cota diária do gateway, que um laço de passos estoura).
    "nvidia/nemotron-3.5-super-vl": "openrouter",
    "nvidia/nemotron-3.5-lightning": "openrouter",
}


# Modelos que aceitam imagem (content block `image_url`) — a capacidade que o
# olho do navegador local exige.
#
# Isto é DADO, não confiança em quem escolhe, porque escolher errado falha de um
# jeito silencioso e caro: a família DeepSeek responde
#     400 "This model does not support image"
# (sondado contra a API real em 2026-08-31, os dois modelos v4), e essa frase
# casa com `_VISION_FAILURE_IS_PERMANENT` no driver — ou seja, apontar a visão
# para o DeepSeek desliga a visão no PASSO 1 de todo job, sem erro visível, e a
# tarefa inteira sai às cegas. Foi assim que o job a0eab88c queimou 25 passos
# clicando no mesmo contêiner (ali por falta de crédito na OpenAI, mesmo efeito).
#
# Verificados com uma chamada real (2026-08-31): google/gemini-3.1-flash-lite
# respondeu 200; qwen/qwen3.5-flash-02-23 e x-ai/grok-4.5 recusaram só o PNG 1x1
# do teste ("image dimensions too small"), o que prova que leem imagem.
VISION_MODELS: frozenset[str] = frozenset(
    {
        # OpenAI — a família 4.x lê imagem; a 3.5 não.
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4-turbo",
        "gpt-4-turbo-preview",
        # Anthropic
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
        # Gemini
        "gemini-2.5-pro-preview-03-25",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        # OpenRouter
        "google/gemini-3.1-flash-lite",
        "google/gemini-2.0-flash-001",
        "anthropic/claude-3.5-sonnet",
        "qwen/qwen3.5-flash-02-23",
        "x-ai/grok-4.5",
        "x-ai/grok-4.20",
        # input_modalities = [file, image, text, audio, video] (consultado na API
        # do gateway em 2026-09-02). O browser-use manda print a cada passo.
        "google/gemini-2.5-flash",
        # input_modalities = [text, image, video] (API do gateway, 2026-11-05).
        "nvidia/nemotron-3.5-super-vl",
    }
)


#: Modelo padrão de cada provider — o que o agente usa quando ninguém escolheu.
#:
#: Vira a coluna ``default_model`` do provider: é o MESMO valor que o painel
#: exibe como "padrão do cérebro" e que ``LLMRouter.get_default_model`` devolve.
#: Duplicar a tabela é como se constrói uma tela que mostra um modelo e um
#: runtime que usa outro. Não é o 1º do SELECTOR (que é só o que o chat escolhe
#: ao trocar de provider): o ``gpt-4-turbo-preview`` nem aparece no seletor.
DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "openai": "gpt-4-turbo-preview",
    "anthropic": "claude-3-opus-20240229",
    "ollama": "gemma4:e2b",
    "deepseek": "deepseek-flash",
    "gemini": "gemini-2.0-flash",
    "grok": "grok-3-mini",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "google/gemini-3.1-flash-lite",
}



#: Providers na ordem do seletor, com o rótulo que o chat mostra. O ``adapter`` é
#: a classe que fala com a API (``core.llm.catalog.ADAPTERS``); nos providers de
#: fábrica ele tem o mesmo nome do provider.
PROVIDERS: tuple[tuple[str, str], ...] = (
    ("openai", "OpenAI"),
    ("groq", "Groq"),
    ("grok", "Grok (xAI)"),
    ("deepseek", "DeepSeek"),
    ("anthropic", "Anthropic"),
    ("gemini", "Gemini"),
    ("ollama", "Ollama (local)"),
    ("openrouter", "OpenRouter"),
)

#: Vitrine do chat: ``provider → [(model_id, rótulo, descrição)]``, na ordem em
#: que aparecem. O primeiro de cada provider é o que o seletor escolhe ao trocar
#: de provider. Veio do catálogo do desktop (neo-desktop-tauri/src/lib/models.ts),
#: que era copiado à mão no painel — sem o Claude 3.5 Sonnet, que o seletor
#: oferecia e o core não conhecia.
SELECTOR: dict[str, tuple[tuple[str, str, str], ...]] = {
    "openai": (
        ("gpt-4.1", "GPT-4.1", "Mais capaz"),
        ("gpt-4.1-mini", "GPT-4.1 Mini", "Rápido"),
        ("gpt-4.1-nano", "GPT-4.1 Nano", "Mais rápido"),
        ("gpt-4o", "GPT-4o", "Equilíbrio"),
        ("gpt-4o-mini", "GPT-4o-mini", "Econômico"),
    ),
    "groq": (
        ("llama-3.3-70b-versatile", "Llama-3.3 70B", "Padrão (free)"),
        ("llama-3.1-8b-instant", "Llama-3.1 8B Fast", "Ultra-rápido (free)"),
        ("llama3-70b-8192", "Llama3 70B 8k", "Contexto longo (free)"),
        ("mixtral-8x7b-32768", "Mixtral 8x7B", "32k tokens (free)"),
        ("gemma2-9b-it", "Gemma2 9B", "Google (free)"),
    ),
    "grok": (
        ("grok-3-mini-fast", "Grok-3 Mini Fast", "Mais rápido (free)"),
        ("grok-3-mini", "Grok-3 Mini", "Rápido (free)"),
        ("grok-2-1212", "Grok-2", "Completo (free)"),
        ("grok-3", "Grok-3", "Mais capaz (pago)"),
    ),
    "deepseek": (
        ("deepseek-flash", "DeepSeek Flash", "Mais rápido (V4.1)"),
        ("deepseek-v4-pro", "DeepSeek V4 Pro", "Mais capaz"),
        ("deepseek-chat", "DeepSeek Chat", "Uso geral"),
        ("deepseek-reasoner", "DeepSeek Reasoner", "Raciocínio lento"),
    ),
    "anthropic": (
        ("claude-3-haiku-20240307", "Claude 3 Haiku", "Rápido"),
    ),
    "gemini": (
        ("gemini-2.5-pro-preview-03-25", "Gemini 2.5 Pro", "Avançado"),
        ("gemini-2.0-flash", "Gemini 2.0 Flash", "Rápido"),
        ("gemini-2.0-flash-lite", "Gemini Flash Lite", "Leve"),
    ),
    "ollama": (
        ("gemma4:e2b", "Gemma4 2B (Local)", "Ultra-leve"),
        ("gemma4:12b", "Gemma4 12B (Local)", "Local"),
        ("phi4", "Phi-4 (Local)", "Local"),
    ),
    "openrouter": (
        ("x-ai/grok-4.5", "Grok 4.5", "Mais capaz (raciocínio)"),
        ("x-ai/grok-4.20", "Grok 4.20", "Equilíbrio, 2M contexto"),
        ("google/gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite", "Econômico"),
        # As DUAS entradas do V4.1 Flash existem de propósito, e a escolha é preço
        # x latência — ver o comentário do pin em MODEL_TO_PROVIDER.
        (
            "deepseek/deepseek-v4.1-flash@fireworks",
            "DeepSeek V4.1 Flash (Fireworks)",
            "1M contexto, 69-85 tok/s — endpoint estável",
        ),
        (
            "deepseek/deepseek-v4.1-flash",
            "DeepSeek V4.1 Flash (auto)",
            "Sem pin: qualquer endpoint livre, preço varia",
        ),
        (
            "deepseek/deepseek-v4-flash@baidu",
            "DeepSeek V4 Flash (Baidu)",
            "Geração anterior — ~6x mais barato na entrada",
        ),
        ("nvidia/nemotron-3.5-super-vl", "Nemotron 3.5 Super VL", "Lê imagem, até 1M contexto"),
        ("nvidia/nemotron-3.5-lightning", "Nemotron 3.5 Lightning", "Leve e barato, só texto"),
    ),
}
