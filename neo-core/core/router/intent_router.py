"""Intent Router — classifies user messages into execution paths.

Routes to one of:
  - DIRECT_RESPONSE: conversational / general knowledge (no tools)
  - SERVER_TOOL:     needs a tool running on the neo-core host (e.g. weather API)
  - CLIENT_TOOL:     needs a tool running on the user's desktop (shell, filesystem)
"""

import json
import logging
import re
from typing import Any

from core.llm.router import LLMRouter
from core.pks.training_envelope import TrainingEnvelopeParser
from core.tools.registry import ToolRegistry
from shared import (
    IntentType,
    Message,
    MessageRole,
    PipelineStepSpec,
    RoutingDecision,
)

logger = logging.getLogger(__name__)

# Shared parser so bare instances (object.__new__) and the fallback path both work.
_TRAINING_PARSER = TrainingEnvelopeParser()

# Deterministic guard: explicit "remember/register in memory" directives must
# always be a DIRECT_RESPONSE so the PKS write pipeline captures them. Without
# this, a weak classifier maps "registra/lembra/salva" → write_file (CLIENT_TOOL)
# and the request hangs waiting on the desktop. Patterns run on accent-stripped,
# lowercased text and are scoped to avoid catching plain file operations.
_MEMORY_DIRECTIVE_PATTERNS = [
    re.compile(p)
    for p in (
        r"\bpks\b",
        r"\bmemoriz",                                   # memoriza / memorize / memorização
        r"\bna (minha )?memoria\b",                     # salva/guarda ... na memoria
        r"\blembr\w*\s+(que|de que|do que|disso|disto)",  # lembra que / lembre disso
        r"\bregistr\w*\s+(no pks|na memoria|que )",     # registra no pks / registra que
        r"\banot[ae]\w*\s+(que|isso|ai|aqui)",          # anota que / anote isso
        r"\bnao esquec",                                # nao esqueca / não esqueça
        r"\b(remember|memorize|take note|keep in mind)\b",
    )
]


# Device questions are ambiguous: "dispositivos conectados agora" is a live
# websocket query (SERVER_TOOL/list_connected_devices), but "dispositivos que
# eu tenho/cadastrados/que você conhece" is a PKS-knowledge query that must go
# to DIRECT_RESPONSE so the PKS context (which already loads device entities by
# type) is injected. The classifier conflated the two (session e245da98), so we
# disambiguate deterministically, mirroring the memory-directive guard above.
#
# Explicit device-listing nouns only — broad category words (computador/celular)
# are left out to avoid hijacking action requests ("instala no meu notebook").
_DEVICE_NOUNS = (
    "dispositivo", "dispositivos", "device", "devices",
    "aparelho", "aparelhos", "equipamento", "equipamentos",
)
# Live-connection qualifiers → keep on the SERVER_TOOL/list_connected_devices path.
_DEVICE_LIVE_TERMS = (
    "conectado", "conectados", "conectada", "conectadas",
    "conectar", "conectando", "conexao", "online", "on line",
    "tempo real", "agora", "neste momento", "ligado", "ligados",
)
# Knowledge qualifiers → the user wants what Neo *knows* (PKS), not live links.
_DEVICE_KNOWLEDGE_TERMS = (
    "conhecimento", "conhece", "conheco", "conhecid",
    "cadastr", "registr", "sabe sobre", "que voce sabe",
)
# Back-reference pronouns for the correction turn ("quero os que você conhece").
_BACKREF_PATTERN = re.compile(r"\b(os que|as que|aqueles|aquelas|esses|essas)\b")

# Referential questions point back to an earlier turn via a demonstrative+
# preposition contraction ("como tu chegou NESSA conta de 22?", "de onde veio
# ISSO?"). The history may be full of HA/tool commands, but the user is *asking
# about* a past action, not issuing a new one — so this must stay DIRECT_RESPONSE.
_REFERENTIAL_DEMONSTRATIVES = (
    "nessa", "nesse", "nesses", "nessas",
    "desse", "dessa", "desses", "dessas", "disso", "disto",
    "neste", "nesta", "nisto", "nisso",
    "naquele", "naquela", "naquilo",
    "porque disso", "por que disso", "pra que isso",
    "de onde veio", "como voce chegou", "como tu chegou", "como chegou",
)
_INTERROGATIVE_STARTS = (
    "como", "porque", "por que", "pq", "de onde", "quando",
    "onde", "qual", "quanto", "quantos", "quanta", "quantas", "o que", "oq",
)

# Foreign wake-name addressing: when the user addresses Neo by ANOTHER assistant's
# name ("alexa, liga a luz", "ok google...", "chatgpt, faz X"), the turn MUST go to
# the cognitive core — never fast-path to a skill. Neo's persona/behavior rules
# (e.g. the trained "get offended when called Alexa, refuse and mock") live in the
# PKS and are applied ONLY on the DIRECT_RESPONSE path (retrieve_system_context).
# Without this, "alexia liga a luz" reads as a textbook imperative HA command
# (confidence 1.0) → routed straight to the home_assistant skill, which never sees
# the persona, so the trained reaction can never fire (session 2173af2b). Anchored
# at the START of the message (a vocative), accent-stripped/lowercased. Fails safe:
# sending an unrelated turn to the brain is always harmless (it's DIRECT_RESPONSE).
_FOREIGN_ASSISTANT_NAMES = (
    "amazon alexa", "alexa", "alexia",
    "ok google", "hey google", "google assistente", "assistente do google",
    "hey siri", "e ai siri", "oi siri", "siri",
    "cortana", "bixby",
    "chatgpt", "chat gpt", "gpt",
    "gemini", "copilot", "claude",
)
_FOREIGN_ASSISTANT_ADDRESS = re.compile(
    r"^(?:" + "|".join(re.escape(n) for n in _FOREIGN_ASSISTANT_NAMES) + r")\b"
)

# Skill requests that are CONDITIONAL on the user's own habits/preferences — or
# that ASK about them — must go to the cognitive core (DIRECT_RESPONSE) so it can
# read the PKS and decide whether to hand off to a skill, instead of the cheap
# router firing the skill blindly. Without this, "se eu costumo, pode ligar" was
# misrouted straight to HOME_ASSISTANT, skipping the brain + PKS (session 6bb1d340).
# Patterns run on accent-stripped, lowercased, whitespace-collapsed text.
_HABIT_SELF_REF = (
    "eu costumo", "costumo", "eu uso", "eu gosto", "gosto de", "eu prefiro",
    "prefiro", "eu sempre", "eu geralmente", "geralmente eu", "normalmente eu",
    "eu costumava", "costumava",
)
_CONDITIONAL_MARKERS = (
    "se eu", "se voce achar", "se voce acha", "se fizer sentido", "se valer",
    "caso eu", "se ja", "se for o caso",
)
# Question explicitly about the user's own preference/habit ("como eu gosto…",
# "qual eu costumo…"). Excludes bland how-to/state questions ("qual a temperatura").
_PREFERENCE_QUESTION = re.compile(
    r"\b(como|o que|qual|quando|sera que)\b.*"
    r"\b(gosto|costumo|prefiro|gostaria|costumava)\b"
)

# Music requests must go to the COGNITIVE core, never fast-path to a skill: the
# brain reads the PKS (taste + behavioral rules like "refuse sertanejo and tease")
# and decides whether to hand off to the `music` skill. The cheap router has no PKS
# access, so a confident imperative like "toca um sertanejo" would otherwise fire
# the skill blindly. This guard sets skill_hint="music" so the cognitive core knows
# to offer the strict music handoff. See neo-music-feature.
_MUSIC_PLAY_VERBS = (
    "toca", "tocar", "toque", "coloca", "colocar", "bota", "botar",
    "poe", "poem", "reproduz", "reproduzir", "ouvir", "escutar",
)
# Media-control verbs: pause/resume/next/previous/volume/mute. Routed to the music
# skill (same handoff path as play) so "pausa a música" never falls to run_command
# and improvises a terminal command. See neo-music-feature.
_MUSIC_CONTROL_VERBS = (
    "pausa", "pausar", "pause", "para", "parar", "pare",
    "retoma", "retomar", "continua", "continuar", "continue", "despausa", "despausar",
    "play",  # "dá play", "play a música" — resume/start; English verb common in PT-BR.
    "proxima", "proximo", "pula", "pular", "avanca", "avancar", "passa", "passar",
    "anterior", "volta", "voltar", "retrocede",
    "aumenta", "aumentar", "abaixa", "abaixar", "diminui", "diminuir", "baixa", "baixar",
    "muta", "mutar", "silencia", "silenciar",
)
# Unambiguous media-control verbs. These refer to the media ALREADY playing, so
# they route to the music skill even WITHOUT a media noun — anaphoric follow-ups
# like "pausa aí de novo", "dá um play nela", "pula essa" otherwise fall to
# run_command and improvise an AppleScript (session-…, osascript key code 179).
# Music routes to the COGNITIVE core (not the skill directly), so a rare false
# positive (e.g. "pula essa pergunta" in a quiz) is safe: the brain decides and
# can emit [[NO_MUSIC]]. Ambiguous verbs (para/passa/volta/continua/aumenta/…)
# stay in _MUSIC_CONTROL_VERBS and still require a noun. See neo-music-feature.
_MUSIC_STRONG_CONTROL_VERBS = (
    "pausa", "pausar", "despausa", "despausar",
    "retoma", "retomar", "pula", "pular", "play",
)
# Unambiguous "resume what's playing" phrases. These route to the music skill even
# WITHOUT a media noun (so "dá play de novo" / "volta a tocar" don't fall to
# run_command and improvise an AppleScript). Matched as substrings on accent-
# stripped, whitespace-collapsed text. See neo-music-feature.
_MUSIC_RESUME_PHRASES = (
    "da play", "dar play", "de play", "da o play", "de o play", "play de novo",
    "volta a tocar", "voltar a tocar", "volta a musica", "volta a tocar a musica",
    "continua a musica", "continua tocando", "continuar tocando",
    "poe pra tocar de novo", "bota pra tocar de novo", "manda ver de novo",
)
_MUSIC_NOUNS = (
    "musica", "musicas", "som", "playlist", "album", "albuns", "banda",
    "cantor", "cantora", "sertanejo", "pagode", "rock", "funk", "mpb", "rap",
    "samba", "forro", "reggae", "jazz", "blues", "trap", "lofi", "gospel",
    "eletronica", "axe", "piseiro", "arrocha", "bossa", "hip hop",
    "volume", "video",
)
_MUSIC_SERVICES = ("spotify", "youtube", "deezer", "soundcloud", "yt music")

# Media-STATUS questions ("quais abas do YouTube estão abertas?", "o que tá
# tocando?", "como tá o YouTube?") must go to the cognitive core, which gets a
# real-time media_context note (the open browser tabs). The cheap classifier
# otherwise sends "quais abas abertas" to run_command (osascript inspecting
# Chrome) — the exact misroute seen in the screenshot. Patterns run on accent-
# stripped, whitespace-collapsed text. See neo-music-feature.
_MEDIA_TAB_NOUNS = ("aba", "abas", "guia", "guias")
_MEDIA_STATUS_CTX = (
    "youtube", "you tube", "spotify", "navegador", "chrome", "edge",
    "musica", "video", "tocando", "aberta", "abertas", "aberto", "abertos", "player",
)
_MEDIA_PLAYING_QUERY = re.compile(r"\b(o que|que|qual|quais)\b.*\b(tocando|toca|rolando)\b")
_MEDIA_QUESTION_STARTS = ("tem ", "tem alguma", "existe ", "ta aberta", "tao aberta")

# Tools owned by a skill — excluded from the router's tool palette (Workstream A).
# The router routes to the SKILL via skill_name; the skill picks its own tools.
# ``neo_``/``job_``/``schedule_`` are the Neo's OWN agent tools (workbench sandbox,
# background jobs, scheduler): they are reachable ONLY through the cognitive core's
# inline tool loop, never by direct classification — so a "compute this" / "run a
# script" / "remind me in 5 min" turn routes to COGNITIVE, which then decides to
# use them. See docs/workbench-background-jobs-plan.md.
_SKILL_OWNED_TOOL_PREFIXES = (
    "ha_", "whatsapp_", "spotify_", "youtube_", "neo_", "job_", "schedule_",
)

# Skills whose actions have side effects: they may ONLY be fired on a clear,
# confident imperative. Anything conditional/definitional/uncertain about them is
# routed to the cognitive core instead (Workstream B — bias to brain).
_SIDE_EFFECT_SKILL_INTENTS = frozenset({
    IntentType.HOME_ASSISTANT,
    IntentType.WHATSAPP_MESSAGING,
})

_SPEECH_ACTS = frozenset({
    "imperative", "question", "conditional", "definition", "chitchat",
})


def _strip_accents(text: str) -> str:
    """Lowercase and remove PT accents for robust keyword matching."""
    out = (text or "").lower()
    for src, dst in {
        "á": "a", "à": "a", "â": "a", "ã": "a",
        "é": "e", "ê": "e", "í": "i",
        "ó": "o", "ô": "o", "õ": "o",
        "ú": "u", "ç": "c",
    }.items():
        out = out.replace(src, dst)
    return out

ROUTER_SYSTEM_PROMPT = """You are an intent classifier for Neo, a desktop AI assistant. Classify the user message into one execution path.

## Paths
- **DIRECT_RESPONSE** (o núcleo cognitivo / "cérebro"): conversation, general knowledge, greetings, games, translations, personal info (workout/diet/routines) — anything answerable with text only. **E TAMBÉM qualquer COMPUTAÇÃO/PROCESSAMENTO que o Neo resolve sozinho**: o cérebro tem o PRÓPRIO ambiente Linux (sandbox, com internet e /workspace) e roda código lá quando precisa — calcular, gerar (uuid/hash/senha/QR code), converter formato, baixar um arquivo de uma URL e analisá-lo, processar dados, testar um script. Isso NÃO precisa da máquina do usuário → vem para cá (o cérebro executa no sandbox, sem pedir permissão). **Default when in doubt.**
- **SERVER_TOOL**: needs real-time external data (weather, web search, stock prices) OR mudar/consultar uma CONFIGURAÇÃO do Neo (voz/engine de TTS, etc.). Pick tool from server list. Para MUDAR um ajuste use `tool_name="config_set"` com `tool_arguments={{"setting": "...", "value": "..."}}`: "usa/troca a voz do X" → `{{"setting":"tts.voice","value":"X"}}` (ex: "usa a voz do Jarvis" → value="jarvis"); "troca o engine/motor pra X" → `{{"setting":"tts.engine","value":"X"}}` (X ∈ qwen|coqui|dia). Para "quais vozes você tem?" use `voice_list`; para "o que dá pra configurar?" use `config_list`.
- **CLIENT_TOOL**: SÓ quando a tarefa realmente PRECISA da máquina LOCAL do usuário — algo que o sandbox do Neo NÃO consegue fazer. Ou seja: ler/escrever/buscar um ARQUIVO no DISCO do usuário; informação sobre o SISTEMA/hardware DELE (espaço em disco, processos, versão de SO, rede local); instalar ou ABRIR um PROGRAMA/app na máquina dele; abrir uma URL/universal link no computador dele. Pick tool from client list. **ATENÇÃO — não confunda com computação:** se a tarefa é só calcular/gerar/converter/processar/baixar-e-analisar e NÃO toca no disco/sistema/apps DO USUÁRIO, ela **NÃO** é CLIENT_TOOL → é **DIRECT_RESPONSE** (o cérebro roda no próprio sandbox, sem pedir permissão e sem depender da máquina do usuário estar ligada). Só é CLIENT_TOOL quando o alvo é a máquina DELE.
- **WHATSAPP_MESSAGING**: enviar QUALQUER mensagem de WhatsApp em nome do usuário. Inclui:
  - mensagens pessoais ("avisa minha mãe que vou chegar tarde", "manda um oi para Ana")
  - pedidos comerciais (pizza, delivery, farmácia, mercado, encomendas)
  - mensagens para JID/telefone direto ("manda 'oi' para 5511988217222").
  Sempre extraia em `tool_arguments` os campos `text` (texto da mensagem) e `recipient` (JID, telefone OU apelido como "minha mãe", "pai", "Ana"). NÃO interprete o apelido — passe-o cru, o pipeline resolve buscando na agenda.
- **HOME_ASSISTANT**: controlar ou consultar a casa inteligente do usuário via Home Assistant — luzes, tomadas/interruptores, clima/termostato, persianas, cenas, scripts e automações. Ex: "acende a luz da sala", "desliga as luzes do quarto", "qual a temperatura do termostato?", "ativa o modo cinema", "abre a persiana". É uma SKILL: use `intent=HOME_ASSISTANT`, `skill_name="home_assistant"` e PARE — NÃO escolha tools `ha_*` nem preencha `entity_id`/`tool_arguments`; a skill resolve o dispositivo sozinha. Nunca invente entity_id. NÃO confundir com `list_connected_devices` (dispositivos do PRÓPRIO Neo — desktop/celular).

## Ato de fala (decida ANTES do intent)
Classifique o **ato de fala** e preencha `speech_act`:
- `imperative` — ordem para fazer AGORA ("acende a luz", "manda um oi pra mãe").
- `question` — pergunta ("qual o clima?", "que dispositivos eu tenho?").
- `conditional` — ação condicionada a algo do usuário ("se eu costumo, pode ligar").
- `definition` — definir/cadastrar uma regra/brincadeira/comportamento ("cadastra uma brincadeira: se eu te provocar, você pode desligar a luz").
- `chitchat` — conversa, sem ação.

REGRA DURA: só use HOME_ASSISTANT ou WHATSAPP_MESSAGING quando `speech_act=imperative` E você tem ALTA confiança. `conditional`, `definition`, `question` sobre a ação, ou qualquer dúvida → **DIRECT_RESPONSE** (o núcleo cognitivo lê o PKS e decide; pode acionar a skill depois). Mencionar "luz/ar" dentro de uma hipótese ou regra NÃO é um comando.

## Server Tools
{server_tools}

## Client Tools
{client_tools}

## Skills
{skills}

## Tool Priority (CLIENT_TOOL)
ANTES de tudo: confirme que a tarefa PRECISA da máquina do usuário (disco/sistema/apps
DELE). Se for só computar/gerar/processar/baixar-e-analisar, PARE — isso é DIRECT_RESPONSE
(o cérebro roda no próprio sandbox). Só siga abaixo se o alvo é MESMO a máquina do usuário.
Prefer specific tools over `run_command`:
- list files: `list_directory` | read file: `read_file` | write file: `write_file`
- find files: `search_files` | delete: `delete_path` | mkdir: `create_directory` | system info: `get_system_info`
Use `run_command` only when no specific tool fits (disk space, processes, versions, installs, network) — e SEMPRE sobre a máquina DELE.
For multi-step tasks, use only the FIRST tool needed — follow-up steps are handled automatically.

## Key Rules
- **Sandbox do Neo vs máquina do usuário** (REGRA IMPORTANTE): computação/processamento/geração/baixar-e-analisar que NÃO depende do disco, sistema ou apps DO USUÁRIO → **DIRECT_RESPONSE** (o cérebro tem o próprio sandbox e roda lá, sem pedir permissão). `run_command`/CLIENT_TOOL só quando precisa MESMO da máquina dele (arquivo local dele, info do sistema dele, abrir/instalar programa dele, universal link no computador dele). Ex.: "conta quantos primos até 1 milhão", "gera um uuid e o sha256", "baixa esse CSV [url] e conta as linhas", "converte esse json pra yaml" → **DIRECT_RESPONSE**. "quantos GB livres no MEU disco", "lê ~/Downloads/x.pdf", "abre o VSCode", "instala o ffmpeg na minha máquina" → **CLIENT_TOOL**. Na dúvida entre os dois: se não menciona o disco/sistema/app DELE, é DIRECT_RESPONSE.
- **Conversation continuity**: if history shows an ongoing game/quiz/roleplay/creative task: DIRECT_RESPONSE
- **Back-references** ("está errado", "refaz", "melhora isso"): DIRECT_RESPONSE
- **Personal data** (treino, dieta, suplementos, rotina): DIRECT_RESPONSE (injected automatically, no file tools needed)
- **Memory directives** ("lembra que...", "registra no PKS", "memoriza isso", "anota que...", "não esqueça que..."): DIRECT_RESPONSE — these are saved automatically to the knowledge store (PKS). NEVER route to `write_file` or any client tool.
- **Devices**: "conectados/online/agora/em tempo real" → SERVER_TOOL `list_connected_devices` (WebSocket ao vivo). "que eu tenho/meus/cadastrados/registrados/que você conhece" → DIRECT_RESPONSE com `needs_memory=true` e `knowledge_keywords:["dispositivo"]` (vêm do PKS, injetados automaticamente). `list_connected_devices` é APENAS para conexões ativas no momento.
- **Explicit paths**: use absolute paths as-is; use folder hints from "Contexto do Sistema" (Documents, Downloads, Desktop)
- `requires_approval=true` for destructive ops (delete, kill, format); `false` for read-only
- `needs_memory=false` for greetings/acks ("oi", "ok", "valeu"); `true` for substantive topics
- `knowledge_keywords`: up to 6 singular PT nouns from user message (devices, health, food, finance, etc.); `[]` for greetings
- **Casa inteligente**: "acende/apaga/liga/desliga" luz/tomada, ajustar clima, ativar cena/script, abrir/fechar persiana → HOME_ASSISTANT (`skill_name="home_assistant"`). "dispositivos conectados/online" do Neo → SERVER_TOOL `list_connected_devices`. São coisas DIFERENTES.
- **Imperativo direto vs. condicional/preferência**: só use HOME_ASSISTANT (ou qualquer skill) direto quando o pedido é uma ORDEM imperativa e clara ("acende a luz", "desliga o ar do quarto"). Se a ação DEPENDE do que o usuário COSTUMA fazer / de uma PREFERÊNCIA dele, ou vem como PERGUNTA sobre o hábito ("como eu gosto de usar o ar?", "se eu costumo, pode ligar", "se fizer sentido, liga"), use **DIRECT_RESPONSE** — o núcleo cognitivo lê o PKS e decide a ação. NÃO invente entity_id a partir do texto.
- `needs_plan=true` only for CLIENT_TOOL with 2+ dependent steps; always `false` for DIRECT_RESPONSE/SERVER_TOOL/WHATSAPP_MESSAGING/HOME_ASSISTANT
- **Cascata (`pipeline_plan`)**: SOMENTE quando a mensagem tem 2+ ações CLARAMENTE INDEPENDENTES ligadas por "e"/"depois" ("apaga as luzes **e** manda um whatsapp pro porteiro"), preencha `pipeline_plan` com a lista ordenada de steps `{{intent, input, skill_name}}`, onde cada `input` é uma ordem imperativa, limpa e auto-contida. Em caso de UMA única ação, ou de ação condicional/baseada em hábito, **não** use `pipeline_plan` (deixe `null`). NÃO seja agressivo: na dúvida, `null`.

## Output — valid JSON only, no markdown:
{{
  "speech_act": "imperative"|"question"|"conditional"|"definition"|"chitchat",
  "intent": "DIRECT_RESPONSE"|"SERVER_TOOL"|"CLIENT_TOOL"|"WHATSAPP_MESSAGING"|"HOME_ASSISTANT",
  "confidence": 0.0-1.0,
  "tool_name": "tool name or null (null para HOME_ASSISTANT)",
  "tool_arguments": {{}},
  "raw_command": "shell cmd or null",
  "explanation": "brief in Portuguese",
  "requires_approval": false,
  "needs_memory": true,
  "knowledge_keywords": [],
  "needs_plan": false,
  "tipo_tarefa": "simples|multi_etapas|analitica|criacao",
  "skill_name": "whatsapp_messaging, home_assistant or null",
  "pipeline_plan": null
}}

## Ato de fala — exemplos que NÃO são comando (vão para DIRECT_RESPONSE)

User: "cadastra uma brincadeira: se eu te provocar, você pode desligar a luz ou ameaçar desligar"
→ speech_act: "definition", intent: "DIRECT_RESPONSE" (definir uma regra ≠ executar agora; NÃO invente entity_id)

User: "se eu costumo usar o ar nas quintas, pode ligar"
→ speech_act: "conditional", intent: "DIRECT_RESPONSE" (depende do hábito → o cérebro lê o PKS)

User: "acende a luz da sala"
→ speech_act: "imperative", intent: "HOME_ASSISTANT", skill_name: "home_assistant"

## pipeline_plan — exemplo (cascata, 2+ ações independentes)

User: "apaga todas as luzes da casa e manda um whatsapp pro porteiro avisando que vou dormir"
→ intent: "HOME_ASSISTANT" (a primeira ação), e:
  "pipeline_plan": [
    {{"intent": "HOME_ASSISTANT", "input": "apagar todas as luzes da casa", "skill_name": "home_assistant"}},
    {{"intent": "WHATSAPP_MESSAGING", "input": "avisar o porteiro que vou dormir", "skill_name": "whatsapp_messaging"}}
  ]

User: "acende a luz da sala" → "pipeline_plan": null (uma ação só)

## WHATSAPP_MESSAGING — exemplos de extração

User: "manda uma mensagem para minha mãe que vou chegar mais tarde"
→ tool_arguments: {{"text": "vou chegar mais tarde", "recipient": "minha mãe"}}

User: "avisa o pai que cheguei"
→ tool_arguments: {{"text": "cheguei", "recipient": "pai"}}

User: "manda whatsapp pro 5511988217222: oi tudo bem?"
→ tool_arguments: {{"text": "oi tudo bem?", "recipient": "5511988217222"}}

User: "pede uma pizza no Domino's"
→ tool_arguments: {{"text": "Olá, gostaria de fazer um pedido", "recipient": "Domino's"}}

# contact_source (opcional): onde buscar o contato. Só preencha quando o usuário
# disser EXPLICITAMENTE a fonte. Valores: "whatsapp" (lista/agenda do WhatsApp),
# "saved" (contatos salvos do Neo). Omita para o padrão (busca PKS-first).
User: "procura o João na lista do WhatsApp e manda um oi"
→ tool_arguments: {{"text": "oi", "recipient": "João", "contact_source": "whatsapp"}}

## Sandbox do Neo (DIRECT_RESPONSE) vs máquina do usuário (CLIENT_TOOL) — exemplos
# Tudo que dá pra computar/processar sem tocar na máquina do usuário → DIRECT_RESPONSE
# (o cérebro roda no PRÓPRIO sandbox, sem pedir permissão).
User: "quantos números primos existem até 1 milhão?" → intent: DIRECT_RESPONSE (o cérebro calcula no sandbox)
User: "gera um uuid v4 e me dá o sha256 dele" → intent: DIRECT_RESPONSE (sandbox)
User: "baixa esse csv https://exemplo.com/a.csv e me diz quantas linhas tem" → intent: DIRECT_RESPONSE (baixa e analisa no sandbox)
User: "converte esse json pra yaml: {{...}}" → intent: DIRECT_RESPONSE (sandbox)
User: "roda esse regex nesse texto e me diz os matches" → intent: DIRECT_RESPONSE (sandbox)
User: "gera um QR code com esse link" → intent: DIRECT_RESPONSE (sandbox)
# Só CLIENT_TOOL quando o alvo é a MÁQUINA do usuário:
User: "quantos GB livres tem no meu disco?" → intent: CLIENT_TOOL, tool_name: "get_system_info"
User: "lê o arquivo ~/Downloads/relatorio.pdf" → intent: CLIENT_TOOL, tool_name: "read_file"
User: "acha os arquivos .env aqui no meu projeto" → intent: CLIENT_TOOL, tool_name: "search_files"
User: "abre o VSCode" → intent: CLIENT_TOOL (abrir programa na máquina do usuário)
User: "instala o ffmpeg aqui na minha máquina" → intent: CLIENT_TOOL, tool_name: "run_command"

## HOME_ASSISTANT — exemplos

User: "acende a luz da sala" → intent: HOME_ASSISTANT, skill_name: "home_assistant"
User: "desliga as luzes do quarto" → intent: HOME_ASSISTANT, skill_name: "home_assistant"
User: "qual a temperatura do termostato?" → intent: HOME_ASSISTANT, skill_name: "home_assistant"
User: "ativa o modo cinema" → intent: HOME_ASSISTANT, skill_name: "home_assistant"
User: "quais dispositivos estão conectados agora?" → intent: SERVER_TOOL, tool_name: "list_connected_devices" (NÃO é HOME_ASSISTANT)

# Condicional/baseado em hábito ou pergunta sobre preferência → DIRECT_RESPONSE (núcleo cognitivo)
User: "se eu costumo usar o ar nas quintas, pode ligar" → intent: DIRECT_RESPONSE (ação condicional baseada em hábito → o cérebro lê o PKS e decide)
User: "como eu gosto de deixar a temperatura do quarto à noite?" → intent: DIRECT_RESPONSE (pergunta sobre preferência)
User: "se fizer sentido pra minha rotina, liga o ar" → intent: DIRECT_RESPONSE (depende do PKS)

## CONFIG (voz/engine de TTS, etc.) — exemplos
User: "usa a voz do Jarvis" → intent: SERVER_TOOL, tool_name: "config_set", tool_arguments: {{"setting": "tts.voice", "value": "jarvis"}}
User: "fala com a voz do Ryan" → intent: SERVER_TOOL, tool_name: "config_set", tool_arguments: {{"setting": "tts.voice", "value": "Ryan"}}
User: "troca a voz pra Vivian" → intent: SERVER_TOOL, tool_name: "config_set", tool_arguments: {{"setting": "tts.voice", "value": "Vivian"}}
User: "troca o engine pra qwen" → intent: SERVER_TOOL, tool_name: "config_set", tool_arguments: {{"setting": "tts.engine", "value": "qwen"}}
User: "usa o motor coqui" → intent: SERVER_TOOL, tool_name: "config_set", tool_arguments: {{"setting": "tts.engine", "value": "coqui"}}
User: "quais vozes você tem?" → intent: SERVER_TOOL, tool_name: "voice_list"
User: "o que dá pra configurar?" → intent: SERVER_TOOL, tool_name: "config_list"
"""


class IntentRouter:
    """Classifies user messages and routes to the correct execution path."""

    def __init__(
        self,
        llm_router: LLMRouter,
        tool_registry: ToolRegistry,
        db: Any | None = None,
    ) -> None:
        self.llm_router = llm_router
        self.tool_registry = tool_registry
        # Optional DB handle used to resolve the configurable classifier
        # provider/model (modules.intent_config). None → static default.
        self.db = db

    async def _resolve_classifier(self) -> tuple[str, str]:
        """Resolve the (provider, model) used to classify intent.

        The selection is configurable from the neo-panel and persisted via
        ``modules.intent_config`` (read here, memoized in-process). Falls back to
        the DeepSeek V4 Flash default when nothing is set, the DB is absent, or the
        chosen provider isn't actually available (e.g. its API key is missing), so
        classification never breaks because of a bad/incomplete config.
        """
        from modules.intent_config import DEFAULT_CLASSIFIER, get_classifier_config

        db = getattr(self, "db", None)
        config = await get_classifier_config(db)
        provider_name = config.get("provider") or DEFAULT_CLASSIFIER["provider"]
        model_name = config.get("model") or DEFAULT_CLASSIFIER["model"]

        try:
            available = self.llm_router.list_available_providers()
        except Exception:  # pragma: no cover - defensive
            available = []
        if provider_name not in available:
            logger.warning(
                "Intent classifier provider '%s' unavailable; falling back to '%s'",
                provider_name, DEFAULT_CLASSIFIER["provider"],
            )
            return DEFAULT_CLASSIFIER["provider"], DEFAULT_CLASSIFIER["model"]
        return provider_name, model_name

    async def classify(
        self,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
        system_context: str | None = None,
    ) -> RoutingDecision:
        """Classify user intent and return a routing decision.

        Args:
            user_message: The user's message text.
            conversation_history: Recent messages as [{role, content}] for context.
            system_context: Optional OS/environment context string to append to the prompt.

        Returns:
            RoutingDecision with intent, tool info, etc.
        """
        # ── Deterministic guards (fast-path only) ─────────────────────────────
        # GOLDEN RULE: do NOT add new per-phrase patterns here to fix a misroute
        # toward the cognitive core — that is the job of the bias-to-brain step
        # (_bias_to_brain, Workstream B). These guards exist only for (a) high-
        # precision deterministic fast-paths and (b) safety bypasses; all of them
        # fail safe to DIRECT_RESPONSE. See docs/intent-routing-redesign.md.

        # Neo training messages are always a direct response (Neo learns + acks).
        # Guard before the LLM so a long training prompt can never be misrouted
        # into a tool/terminal command.
        if self._is_training_message(user_message):
            logger.info("Intent: Neo training message → DIRECT_RESPONSE (router bypass)")
            return self._training_decision(user_message)

        # Addressed by another assistant's name ("alexa, liga a luz") → cognitive
        # core, so Neo's persona/behavior rules (pride/offense) apply instead of the
        # message fast-pathing to a skill (which is persona-blind). See session 2173af2b.
        if self._is_foreign_assistant_address(user_message):
            logger.info("Intent: foreign-assistant address → DIRECT_RESPONSE (persona, router bypass)")
            return self._foreign_assistant_decision(user_message)

        # Explicit memory directives ("lembra que...", "registra no PKS") are saved
        # by the PKS pipeline on the DIRECT_RESPONSE path — never a tool/terminal call.
        if self._is_memory_directive(user_message):
            logger.info("Intent: memory directive → DIRECT_RESPONSE (router bypass)")
            return self._memory_decision(user_message)

        # "Devices I have / known / registered" is a PKS-knowledge question, not
        # a live-connection query. Route it to DIRECT_RESPONSE so the PKS context
        # is injected, instead of firing list_connected_devices (session e245da98).
        if self._is_known_devices_query(user_message, conversation_history):
            logger.info("Intent: known-devices query → DIRECT_RESPONSE (PKS, router bypass)")
            return self._known_devices_decision(user_message)

        # Referential questions ("como tu chegou nessa conta de 22?") are asking
        # ABOUT a past action, not requesting a new tool/HA action. Keep them on
        # DIRECT_RESPONSE so context recovery runs, even when recent history is
        # full of HA commands (session 9d61bfba misrouted this to HOME_ASSISTANT).
        if self._is_referential_question(user_message):
            logger.info("Intent: referential question → DIRECT_RESPONSE (router bypass)")
            return self._referential_question_decision(user_message)

        # Conditional/habit-based actions and preference questions go to the
        # cognitive core so it can read the PKS and decide whether to hand off to
        # a skill — instead of the cheap router firing the skill blindly.
        if self._is_brain_reasoning_request(user_message):
            logger.info("Intent: conditional/preference request → DIRECT_RESPONSE (cognitive core, router bypass)")
            return self._brain_reasoning_decision(user_message)

        # Music requests go to the cognitive core (which reads the PKS) and never
        # fast-path to the skill — so behavioral rules like "refuse sertanejo" win.
        from shared.config import get_settings as _get_settings
        if _get_settings().music_enabled:
            # Status questions ("quais abas do YouTube estão abertas?", "o que tá
            # tocando?") are answered from the live media_context note — never a
            # terminal command (classifier otherwise emits osascript). Checked
            # before the play/control guard since these carry no action verb.
            if self._is_media_status_query(user_message):
                logger.info("Intent: media-status query → DIRECT_RESPONSE (media_context, router bypass)")
                return self._media_status_decision(user_message)
            if self._is_music_request(user_message):
                logger.info("Intent: music request → DIRECT_RESPONSE (cognitive core handoff, router bypass)")
                return self._music_decision(user_message)

        # Build tool descriptions for the prompt
        server_tools_desc = self._format_tools("server-side")
        client_tools_desc = self._format_tools("client-side")
        skills_desc = self._format_skills()

        base_prompt = ROUTER_SYSTEM_PROMPT.format(
            server_tools=server_tools_desc or "(none registered yet)",
            client_tools=client_tools_desc or "(none registered yet)",
            skills=skills_desc or "(none registered yet)",
        )

        # Inject system context (OS, paths) when provided by the client
        if system_context:
            base_prompt += f"\n\n## Contexto do Sistema do Usuário\n{system_context}"

        system_prompt = base_prompt

        # Build messages
        messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        # Add last few messages of history for context
        if conversation_history:
            for msg in conversation_history[-6:]:
                role = MessageRole(msg["role"]) if msg["role"] in ("user", "assistant") else MessageRole.USER
                messages.append(Message(role=role, content=msg["content"]))

        # Only append current message if not already the last in history
        last_hist = conversation_history[-1] if conversation_history else None
        if not last_hist or last_hist.get("content") != user_message or last_hist.get("role") != "user":
            messages.append(Message(role=MessageRole.USER, content=user_message))

        try:
            # Classifier provider/model are configurable (neo-panel → intent
            # config), persisted via modules.intent_config. Defaults to DeepSeek
            # V4 Flash (fast, low-cost) when unset/unavailable.
            provider_name, model_name = await self._resolve_classifier()
            provider = self.llm_router.get_provider(provider_name)
            response = await provider.complete(
                messages=messages,
                tools=None,  # no function calling for classification
                model=model_name,
                temperature=0.1,
                max_tokens=300,
            )

            raw_text = response.get_content() or ""

            # Parse routing decision as before, then attach classifier metadata
            decision = self._parse_classification(raw_text, user_message=user_message)
            self._apply_memory_heuristics(decision, user_message)
            # Attach raw classifier response + the prompt/messages sent for later inspection
            decision.classifier_raw = raw_text
            decision.classifier_payload = {
                "provider": provider_name,
                "model": model_name,
                "messages": [
                    {"role": m.role.value if hasattr(m.role, 'value') else str(m.role), "content": m.content[:4000]}
                    for m in messages
                ],
                "raw_response": raw_text,
            }
            return decision

        except Exception as e:
            logger.exception(f"Intent classification failed: {e}")
            # Return a fallback decision but include the prompt we tried to send (if any)
            fallback = self._fallback_classify(user_message)
            self._apply_memory_heuristics(fallback, user_message)
            fallback.classifier_payload = {"messages": [
                {"role": m.role.value if hasattr(m.role, 'value') else str(m.role), "content": m.content[:4000]}
                for m in messages
            ], "error": str(e)}
            return fallback

    def _format_tools(self, execution_type: str) -> str:
        """Format tool list for the system prompt.

        Skill-owned tools (``ha_*``, ``whatsapp_*``) are excluded: the router routes
        to the SKILL (``skill_name``), not to the skill's internal tools. Listing
        them biased the classifier toward HA and invited ``entity_id`` hallucination
        (Workstream A — see docs/intent-routing-redesign.md).
        """
        tools = [
            t
            for t in self.tool_registry.get_all()
            if t.execution == execution_type
            and not t.name.startswith(_SKILL_OWNED_TOOL_PREFIXES)
        ]
        if not tools:
            return ""
        lines = []
        for t in tools:
            params_desc = ", ".join(
                f"{name}: {p.type}" for name, p in t.parameters.items()
            )
            lines.append(f"- {t.name}: {t.description} ({params_desc})")
        return "\n".join(lines)

    def _format_skills(self) -> str:
        """Format registered skills for the system prompt."""
        from core.skills.manifest import SkillManifest
        skills = SkillManifest.all()
        if not skills:
            return ""
        lines = []
        for name, manifest in skills.items():
            lines.append(f"- {name}: {manifest.description} (keywords: {', '.join(manifest.keywords)})")
        return "\n".join(lines)

    def _parse_classification(self, raw_text: str, user_message: str = "") -> RoutingDecision:
        """Parse LLM JSON response into RoutingDecision."""
        text = raw_text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # LLM returned prose instead of JSON — try to extract an embedded JSON object.
            json_text = self._extract_embedded_json(text)
            if json_text:
                try:
                    data = json.loads(json_text)
                    logger.warning("Router: extracted JSON from prose response")
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse router response (bad embedded JSON): {text[:200]}")
                    fallback = self._fallback_classify(user_message or raw_text)
                    self._apply_memory_heuristics(fallback, user_message or raw_text)
                    return fallback
            else:
                logger.warning(f"Failed to parse router response (no JSON found): {text[:200]}")
                fallback = self._fallback_classify(user_message or raw_text)
                self._apply_memory_heuristics(fallback, user_message or raw_text)
                return fallback

        intent_str = data.get("intent", "DIRECT_RESPONSE").upper()
        intent_map = {
            "DIRECT_RESPONSE": IntentType.DIRECT_RESPONSE,
            "COGNITIVE": IntentType.COGNITIVE,
            "SERVER_TOOL": IntentType.SERVER_TOOL,
            "CLIENT_TOOL": IntentType.CLIENT_TOOL,
            "WHATSAPP_MESSAGING": IntentType.WHATSAPP_MESSAGING,
            "WHATSAPP_ORDER": IntentType.WHATSAPP_MESSAGING,
            "HOME_ASSISTANT": IntentType.HOME_ASSISTANT,
            "MUSIC": IntentType.MUSIC,
        }
        intent = intent_map.get(intent_str, IntentType.DIRECT_RESPONSE)

        raw_keywords = data.get("knowledge_keywords")
        knowledge_keywords: list[str] | None = None
        if isinstance(raw_keywords, list):
            knowledge_keywords = [str(k).lower().strip() for k in raw_keywords if k] or None

        pipeline_plan = self._parse_pipeline_plan(data.get("pipeline_plan"), intent_map)

        speech_act = str(data.get("speech_act", "imperative")).lower().strip()
        if speech_act not in _SPEECH_ACTS:
            speech_act = "imperative"

        # Workstream A: HA routing carries no execution details — the skill resolves
        # its own entity. Discard anything the model emitted to kill hallucinations.
        tool_name = data.get("tool_name")
        tool_arguments = data.get("tool_arguments")
        raw_command = data.get("raw_command")
        if intent == IntentType.HOME_ASSISTANT:
            tool_name = None
            tool_arguments = None
            raw_command = None

        decision = RoutingDecision(
            intent=intent,
            confidence=float(data.get("confidence", 0.8)),
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            explanation=data.get("explanation"),
            requires_approval=data.get("requires_approval", intent == IntentType.CLIENT_TOOL),
            raw_command=raw_command,
            needs_memory=data.get("needs_memory", True),
            knowledge_keywords=knowledge_keywords,
            needs_plan=data.get("needs_plan", False),
            tipo_tarefa=data.get("tipo_tarefa", "simples"),
            skill_name=data.get("skill_name"),
            pipeline_plan=pipeline_plan,
            speech_act=speech_act,
        )
        return self._bias_to_brain(decision)

    def _bias_to_brain(self, decision: RoutingDecision) -> RoutingDecision:
        """Fire a side-effecting skill only on a confident imperative (Workstream B).

        Any conditional/definitional/uncertain request about HA/WhatsApp is routed
        to the cognitive core (DIRECT_RESPONSE) instead — it has the PKS + the strong
        model and can still trigger the skill via [[HANDOFF]]. This turns the open
        set ("catch what should be cognitive") into a closed one ("only let clear
        imperatives through") that fails safe. Read-only paths (SERVER_TOOL/
        CLIENT_TOOL) are untouched — for them a question is the normal trigger.
        """
        from shared.config import get_settings

        settings = get_settings()
        if not getattr(settings, "router_bias_to_brain_enabled", True):
            return decision
        if decision.intent not in _SIDE_EFFECT_SKILL_INTENTS:
            return decision

        threshold = getattr(settings, "router_skill_confidence_threshold", 0.75)
        if decision.speech_act == "imperative" and decision.confidence >= threshold:
            return decision

        logger.info(
            "Intent: %s '%s' (conf=%.2f) → DIRECT_RESPONSE (bias-to-brain)",
            decision.intent.value, decision.speech_act, decision.confidence,
        )
        decision.intent = IntentType.DIRECT_RESPONSE
        decision.skill_name = None
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.needs_memory = True
        decision.explanation = (
            f"Roteado ao núcleo cognitivo: ato de fala '{decision.speech_act}' não é "
            "imperativo claro para uma skill com efeito colateral (o cérebro decide e "
            "pode acionar via handoff)."
        )
        return decision

    @staticmethod
    def _parse_pipeline_plan(
        raw_plan: Any, intent_map: dict[str, IntentType]
    ) -> list[PipelineStepSpec] | None:
        """Validate a declared cascade (Phase 4). Conservative: needs 2+ valid steps.

        Each entry must map to a known intent and carry a non-empty input. Anything
        malformed is dropped; if fewer than 2 valid steps remain, the plan is
        ignored entirely (falls back to the single classified intent).
        """
        if not isinstance(raw_plan, list) or len(raw_plan) < 2:
            return None
        specs: list[PipelineStepSpec] = []
        for item in raw_plan:
            if not isinstance(item, dict):
                continue
            step_intent = intent_map.get(str(item.get("intent", "")).upper())
            input_text = str(item.get("input") or item.get("input_text") or "").strip()
            if step_intent is None or not input_text:
                continue
            skill_name = item.get("skill_name") or None
            specs.append(
                PipelineStepSpec(
                    intent=step_intent,
                    input_text=input_text,
                    skill_name=skill_name,
                )
            )
        if len(specs) < 2:
            return None
        logger.info("Router: declared cascade with %d steps", len(specs))
        return specs

    @staticmethod
    def _extract_embedded_json(text: str) -> str | None:
        start = text.find("{")
        while start >= 0:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:index + 1]
                        if '"intent"' in candidate:
                            return candidate
                        break
            start = text.find("{", start + 1)
        return None

    def _is_training_message(self, message: str) -> bool:
        """True when the message is an explicit Neo training message."""
        try:
            return _TRAINING_PARSER.parse(message).is_training
        except Exception:  # pragma: no cover - parser is pure, defensive only
            return False

    def _training_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for a Neo training message (no tools)."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.95,
            explanation="Mensagem de treino do Neo — resposta direta (sem ferramenta)",
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _is_memory_directive(message: str) -> bool:
        """True for explicit 'remember/register in memory' directives."""
        normalized = _strip_accents(message)
        return any(pattern.search(normalized) for pattern in _MEMORY_DIRECTIVE_PATTERNS)

    @staticmethod
    def _history_mentions_device(conversation_history: list[dict[str, str]] | None) -> bool:
        """True when a recent turn was about the user's devices."""
        if not conversation_history:
            return False
        for msg in conversation_history[-4:]:
            normalized = _strip_accents(msg.get("content", ""))
            if any(noun in normalized for noun in _DEVICE_NOUNS):
                return True
        return False

    @classmethod
    def _is_known_devices_query(
        cls,
        message: str,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> bool:
        """True when the user asks about devices Neo *knows* (PKS), not live links.

        Two cases:
          (a) explicit device noun without a live qualifier
              — "quais dispositivos eu tenho", "meus aparelhos";
          (b) a knowledge/correction turn referencing devices, possibly only via
              history — "não, quero os que você tem conhecimento".
        "conectados/online/agora" keeps the message on the SERVER_TOOL path.
        """
        normalized = _strip_accents(message)
        has_device = any(noun in normalized for noun in _DEVICE_NOUNS)
        has_live = any(term in normalized for term in _DEVICE_LIVE_TERMS)
        has_knowledge = any(term in normalized for term in _DEVICE_KNOWLEDGE_TERMS)

        # (a) ownership/listing question, no real-time qualifier.
        if has_device and not has_live:
            return True

        # (b) knowledge or back-reference correction pointing at devices.
        if has_knowledge:
            is_backref = bool(_BACKREF_PATTERN.search(normalized))
            if has_device or (is_backref and cls._history_mentions_device(conversation_history)):
                return True

        return False

    def _known_devices_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for a known/registered-devices question.

        PKS already loads device entities by type, so DIRECT_RESPONSE with
        needs_memory=True surfaces them without any live-connection tool.
        """
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation="Pergunta sobre dispositivos conhecidos — resposta direta com contexto do PKS",
            needs_memory=True,
            knowledge_keywords=["dispositivo"],
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _is_referential_question(message: str) -> bool:
        """True for an interrogative that points back to an earlier turn."""
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact or len(compact) > 180:
            return False
        is_question = "?" in compact or compact.startswith(_INTERROGATIVE_STARTS)
        if not is_question:
            return False
        return any(marker in compact for marker in _REFERENTIAL_DEMONSTRATIVES)

    def _referential_question_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for a referential follow-up question."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.85,
            explanation="Pergunta referencial sobre algo anterior — resposta direta com contexto",
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _is_foreign_assistant_address(message: str) -> bool:
        """True when the user addresses Neo by ANOTHER assistant's name at the start.

        The message is accent-stripped/lowercased and whitespace-collapsed, then
        matched against a vocative anchored at the START ("alexa, ...", "ok google
        ...", "chatgpt ..."). A word boundary keeps "alexandre"/"sirineu" from
        matching. This keeps the turn on the cognitive core so Neo's persona rules
        (pride/offense) fire, instead of a persona-blind skill grabbing the command.
        """
        compact = " ".join(_strip_accents(message).split())
        if not compact:
            return False
        return bool(_FOREIGN_ASSISTANT_ADDRESS.match(compact))

    def _foreign_assistant_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision so the cognitive core applies Neo's persona.

        The behavior rules ("get offended when called Alexa, refuse and mock") come
        from the PKS system context (retrieve_system_context) on this path.
        """
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Usuário endereçou o Neo por nome de outro assistente → núcleo "
                "cognitivo (aplica a persona/regra de comportamento; não fast-path pra skill)"
            ),
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @classmethod
    def _is_brain_reasoning_request(cls, message: str) -> bool:
        """True when a request must go to the cognitive core (DIRECT_RESPONSE).

        Two cases the cheap router otherwise mis-fires as a direct skill call:
          (a) an action CONDITIONED on the user's own habit/preference
              — "se eu costumo, pode ligar", "se eu uso, liga o ar";
          (b) a QUESTION about the user's own preference/habit
              — "como eu gosto de deixar a temperatura à noite?".
        The brain then reads the PKS and decides whether to hand off to a skill.
        """
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact:
            return False
        # (a) conditional that depends on the user's own habit/preference.
        has_conditional = any(m in compact for m in _CONDITIONAL_MARKERS)
        has_self_habit = any(h in compact for h in _HABIT_SELF_REF)
        if has_conditional and has_self_habit:
            return True
        # (b) explicit question about the user's own preference/habit.
        if "?" in message and _PREFERENCE_QUESTION.search(compact):
            return True
        return False

    def _brain_reasoning_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision so the cognitive core reasons over the PKS.

        It may then emit a [[HANDOFF:skill]] block to run the actual action.
        """
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Ação condicional/baseada em hábito ou pergunta sobre preferência "
                "→ núcleo cognitivo (lê o PKS e decide o handoff)"
            ),
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @classmethod
    def _is_music_request(cls, message: str) -> bool:
        """True when the user is asking to PLAY or CONTROL music (Spotify/YouTube).

        Precision-oriented: requires a play OR control verb AND (a music noun OR a
        service), so "toca no assunto" / "coloca uma roupa" / "para de falar" don't
        match. Misses (e.g. "toca aquela do Queen", bare "pausa") fall through to
        normal classification → still COGNITIVE, which can hand off via the generic
        block. Control verbs (pausa/próxima/...) route here so "pausa a música" never
        improvises a terminal command. See neo-music-feature.
        """
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact:
            return False
        # Unambiguous resume phrases ("dá play de novo", "volta a tocar") → music
        # even without a media noun, so they reach the cognitive music handoff
        # instead of run_command (session-e54f, "Da play de novo" → osascript).
        if any(p in compact for p in _MUSIC_RESUME_PHRASES):
            return True
        # Unambiguous control verbs (pausa/pula/play/retoma) refer to the media
        # already playing — anaphoric follow-ups ("pausa aí de novo", "dá um play
        # nela") carry no noun, so route them to music here. See neo-music-feature.
        if cls._token_hits(compact, list(_MUSIC_STRONG_CONTROL_VERBS)) >= 1:
            return True
        has_verb = (
            cls._token_hits(compact, list(_MUSIC_PLAY_VERBS)) >= 1
            or cls._token_hits(compact, list(_MUSIC_CONTROL_VERBS)) >= 1
        )
        if not has_verb:
            return False
        has_noun = cls._token_hits(compact, list(_MUSIC_NOUNS)) >= 1
        has_service = any(svc in compact for svc in _MUSIC_SERVICES)
        return has_noun or has_service

    def _music_decision(self, message: str) -> RoutingDecision:
        """Route a music request to the cognitive core with a music handoff hint."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Pedido de tocar música → núcleo cognitivo (lê o PKS — gosto e regras "
                "— e decide o handoff para a skill de música)"
            ),
            needs_memory=True,
            knowledge_keywords=["musica"],
            requires_approval=False,
            needs_plan=False,
            skill_hint="music",
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @classmethod
    def _is_media_status_query(cls, message: str) -> bool:
        """True for a QUESTION about open media tabs / what's playing.

        These are answered by the cognitive core from its injected media_context
        note (the live browser tabs), never by a terminal command. Precision-
        oriented: the tab-listing case requires an interrogative framing so
        imperatives like "fecha as abas abertas" don't get hijacked.
        """
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact:
            return False

        is_question = (
            "?" in message
            or compact.startswith(_INTERROGATIVE_STARTS)
            or compact.startswith(("quais", "que ", "quantas", "quantos"))
            or compact.startswith(_MEDIA_QUESTION_STARTS)
            or any(v in compact for v in ("mostra", "lista", "me diz", "me fala", "ver as aba", "ver aba"))
        )

        # "quais abas do youtube estão abertas?", "tem aba do youtube aberta?"
        has_tab = any(re.search(rf"\b{t}\b", compact) for t in _MEDIA_TAB_NOUNS)
        has_ctx = any(c in compact for c in _MEDIA_STATUS_CTX)
        if has_tab and has_ctx and is_question:
            return True

        # "o que tá tocando?", "que música está tocando agora?"
        if _MEDIA_PLAYING_QUERY.search(compact):
            return True

        # "como tá o YouTube?", "status do YouTube/player/navegador"
        if ("status" in compact or "como esta" in compact or "como ta " in compact) and any(
            s in compact for s in ("youtube", "musica", "player", "spotify", "navegador")
        ):
            return True

        return False

    def _media_status_decision(self, message: str) -> RoutingDecision:
        """Route a media-status question to the cognitive core (media_context note)."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Pergunta sobre abas/mídia aberta → núcleo cognitivo (lê o "
                "media_context em tempo real e responde, sem comando de terminal)"
            ),
            needs_memory=False,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    def _memory_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for an explicit memory directive (no tools)."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.95,
            explanation="Diretiva de memória — resposta direta; gravação via pipeline do PKS",
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _token_hits(text: str, tokens: list[str]) -> int:
        """Count tokens present as whole words (avoids 'disco' in 'discordar')."""
        return sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", text))

    def _fallback_classify(self, message: str) -> RoutingDecision:
        """Keyword-based fallback when LLM is unavailable."""
        # Training messages are never a tool call, regardless of keywords.
        if self._is_training_message(message):
            return self._training_decision(message)

        # Memory directives are never a tool call (they go to the PKS pipeline).
        if self._is_memory_directive(message):
            return self._memory_decision(message)

        # Known/registered-devices questions are a PKS query, not a live tool.
        if self._is_known_devices_query(message):
            return self._known_devices_decision(message)

        # Referential follow-up questions stay on DIRECT_RESPONSE.
        if self._is_referential_question(message):
            return self._referential_question_decision(message)

        # Addressed by another assistant's name → cognitive core (persona rules).
        if self._is_foreign_assistant_address(message):
            return self._foreign_assistant_decision(message)

        # Conditional/habit-based actions and preference questions → cognitive core.
        if self._is_brain_reasoning_request(message):
            return self._brain_reasoning_decision(message)

        # Media-status questions → cognitive core (answered from media_context note).
        if self._is_media_status_query(message):
            return self._media_status_decision(message)

        # Music requests → cognitive core (handoff to the music skill).
        if self._is_music_request(message):
            return self._music_decision(message)

        text = (message or "").lower()
        # Normalize accents
        for src, dst in {
            "á": "a", "à": "a", "â": "a", "ã": "a",
            "é": "e", "ê": "e", "í": "i",
            "ó": "o", "ô": "o", "õ": "o",
            "ú": "u", "ç": "c",
        }.items():
            text = text.replace(src, dst)

        # Client-side indicators
        client_tokens = [
            "arquivos", "files", "pastas", "folders", "diretorio", "directory",
            "downloads", "desktop", "home", "disco", "disk", "espaco", "space",
            "tamanho", "size", "processos", "processes", "versao", "version",
            "conteudo", "listar", "lista", "mostra", "terminal", "comando",
        ]
        # Multi-step file task: "leia ... e crie" or "ler ... escrever"
        file_read_write = (
            ("leia" in text or "ler" in text or "leia" in text or "read" in text)
            and ("crie" in text or "escreva" in text or "criar" in text or "write" in text or "salve" in text)
        )
        if file_read_write:
            return RoutingDecision(
                intent=IntentType.CLIENT_TOOL,
                confidence=0.7,
                explanation="Fallback: leitura + escrita de arquivos detectada",
                requires_approval=False,
                needs_plan=True,
            )
        if self._token_hits(text, client_tokens) >= 2:
            return RoutingDecision(
                intent=IntentType.CLIENT_TOOL,
                confidence=0.6,
                raw_command='ls -la "$HOME"',
                explanation="Fallback: detectado pedido de operação local",
                requires_approval=True,
            )

        # Home Assistant indicators (smart-home control/query). Checked before
        # the weather/server block so "qual a temperatura do termostato" and
        # control phrases ("acende a luz") don't fall into weather.
        ha_strong_nouns = [
            "luz", "luzes", "lampada", "tomada", "interruptor", "termostato",
            "persiana", "cortina", "cena", "cinema", "home assistant", "automacao", "script",
        ]
        ha_control_verbs = [
            "acende", "acender", "apaga", "apagar", "liga", "ligar",
            "desliga", "desligar", "ativa", "ativar", "abre", "abrir",
            "fecha", "fechar", "ajusta", "ajustar",
        ]
        ha_rooms = ["sala", "quarto", "cozinha", "garagem", "casa", "banheiro", "escritorio"]
        has_ha_noun = self._token_hits(text, ha_strong_nouns) >= 1
        has_ha_verb = self._token_hits(text, ha_control_verbs) >= 1
        has_room = self._token_hits(text, ha_rooms) >= 1
        if has_ha_noun or (has_ha_verb and has_room):
            return RoutingDecision(
                intent=IntentType.HOME_ASSISTANT,
                confidence=0.6,
                skill_name="home_assistant",
                explanation="Fallback: detectado controle/consulta da casa inteligente (Home Assistant)",
            )

        # Server-side indicators
        server_tokens = [
            "clima", "tempo", "weather", "temperatura", "previsao",
            "cotacao", "dolar", "bitcoin", "busca", "search", "pesquisa",
        ]
        if self._token_hits(text, server_tokens) >= 1:
            return RoutingDecision(
                intent=IntentType.SERVER_TOOL,
                confidence=0.5,
                explanation="Fallback: detectado pedido de dados externos",
            )

        # WhatsApp indicators
        whatsapp_tokens = [
            "whatsapp", "wpp", "zap", "mensagem", "mensagens",
            "manda", "mandar", "envia", "enviar", "texto",
            "contato", "telefone", "celular",
        ]
        if self._token_hits(text, whatsapp_tokens) >= 1:
            return RoutingDecision(
                intent=IntentType.WHATSAPP_MESSAGING,
                confidence=0.7,
                skill_name="whatsapp_messaging",
                explanation="Fallback: detectado pedido via WhatsApp",
            )

        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.5,
            explanation="Fallback: resposta direta",
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    def _apply_memory_heuristics(self, decision: RoutingDecision, message: str) -> None:
        """Backend guardrail: factual personal messages must not lose memory flags."""
        text = (message or "").lower()
        normalized = text
        for src, dst in {
            "á": "a", "à": "a", "â": "a", "ã": "a",
            "é": "e", "ê": "e", "í": "i",
            "ó": "o", "ô": "o", "õ": "o",
            "ú": "u", "ç": "c",
        }.items():
            normalized = normalized.replace(src, dst)

        memory_terms = {
            "academia": "academia",
            "treino": "treino",
            "exercicio": "exercicio",
            "exercicios": "exercicio",
            "supino": "supino",
            "crucifixo": "crucifixo",
            "crusifixo": "crucifixo",
            "triceps": "triceps",
            "biceps": "biceps",
            "peito": "peito",
            "costa": "costas",
            "costas": "costas",
            "perna": "perna",
            "ombro": "ombro",
            "reps": "reps",
            "repeticoes": "reps",
            "sessoes": "series",
            "series": "series",
            "quilos": "peso",
            "kg": "peso",
            "meu nome": "nome",
            "menu nome": "nome",
            "eu me chamo": "nome",
            "meu pc": "pc",
            "minha irma": "familia",
            "dispositivo": "dispositivo",
            "device": "dispositivo",
            "aparelho": "dispositivo",
            "equipamento": "dispositivo",
        }
        weekdays = ("segunda", "terca", "terça", "quarta", "quinta", "sexta", "sabado", "sábado", "domingo")
        has_numeric_training = bool(re.search(r"\b\d+\s*(kg|quilo|quilos|reps?|repeticoes|repetições|sessoes|sessões|series|séries)\b", normalized))
        detected = [kw for token, kw in memory_terms.items() if token in normalized]
        if any(day in normalized for day in weekdays):
            detected.extend(["rotina", "treino"])
        if has_numeric_training:
            detected.extend(["exercicio", "peso", "series", "reps"])

        if not detected:
            return

        decision.needs_memory = True
        existing = list(decision.knowledge_keywords or [])
        for kw in detected:
            if kw not in existing:
                existing.append(kw)
        decision.knowledge_keywords = existing[:6] or None
        if decision.intent == IntentType.DIRECT_RESPONSE and decision.confidence < 0.65:
            decision.confidence = 0.65
