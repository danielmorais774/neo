"""AI Service — unified orchestrator for all AI interactions.

This is the central service that the gRPC controller delegates to.
It uses IntentRouter to classify messages and dispatches to the correct handler:
  - handle_direct_response: conversational, no tools needed
  - handle_server_tool: tools running on the neo-core host
  - handle_client_tool: tools running on the user's desktop (via gRPC tool_request)
"""

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import AsyncIterator
from datetime import date, datetime  # date kept for _build_legacy_prompt fallback
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from core.agent.agent_executor import AgentExecutor
from core.agent.context_selector import PromptContextSelector
from core.agent.register import (
    GravityTracker,
    Register,
    derive_register,
    detect_gravity_floor,
    detect_resolution_cue,
    max_gravity,
    render_register_directive,
)
from core.llm.router import LLMRouter
from core.pks import PKSService
from core.cognitive import CognitiveReadExecutor, cognitive_tools_for
from core.pipeline.handoff_parser import extract_handoffs
from core.pipeline.speech_segments_parser import extract_speech_segments
from core.pipeline.orchestrator import PipelineOrchestrator
from core.pipeline.policy import handoff_defers_final, handoff_requires_confirmation
from core.pipeline.types import Handoff, PipelineStep, StepOutcome
from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep
from core.router.intent_router import IntentRouter
from core.session.session_manager import PersistentSessionManager
from core.text.asr_fixups import ASSISTANT_NAME, canonicalize_assistant_name
from core.session.preference_extractor import PreferenceExtractor
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from db.async_client import AsyncPostgresClient
from observability.logger import get_obs, MessageTrace
from observability.session_file_logger import get_session_logger
from core.memory import ConversationMemory
from shared import (
    ExecutionStatus,
    IntentType,
    MemorySearchResult,
    Message,
    MessageRole,
    RoutingDecision,
    StreamChunk,
    StreamEventType,
    get_settings,
)

logger = logging.getLogger(__name__)


def _strip_mission_marker(text: str) -> tuple[str, dict[str, Any] | None]:
    """Remove a desktop mission marker and return its JSON payload."""
    marker = "[MISSION:"
    start = text.find(marker)
    if start < 0:
        return text, None

    payload_start = start + len(marker)
    decoder = json.JSONDecoder()
    try:
        payload, payload_len = decoder.raw_decode(text[payload_start:])
    except json.JSONDecodeError:
        return text, None

    end = payload_start + payload_len
    if end < len(text) and text[end] == "]":
        end += 1

    clean_text = f"{text[:start]}{text[end:]}".strip()
    return clean_text, payload if isinstance(payload, dict) else None


# Clarification loop: when the DIRECT_RESPONSE model lacks the information to
# answer, it prefixes its output with this sentinel followed by ONE question.
# The orchestrator detects it, persists a pending-clarification state on the
# session, and on the user's next message runs a second LLM iteration that uses
# the answer to produce the real response.
NEED_CLARIFICATION_SENTINEL = "[[NEED_CLARIFICATION]]"
MAX_CLARIFICATION_ROUNDS = 2

# Fecha um turno que morreu por falha de infraestrutura (provider fora, 400,
# timeout). Tem que ser uma frase CURTA e humana, nunca o texto do erro: o que
# fecha o turno é salvo como a fala do assistant e volta no histórico do próximo
# turno — um stack trace ali envenena o contexto e ainda vaza pros pipelines de
# memória (preferências, PKS). O prefixo é reconhecido por
# core/agent/affect_updater._ERROR_RESPONSE_PREFIXES para não virar insumo de
# emoção. O erro de verdade fica no log e no log .md da sessão.
TURN_FAILED_REPLY = "Não consegui responder agora — deu erro do meu lado. Tenta de novo?"

# Mesma frase para o caso em que o provider recusou por LIMITE/CRÉDITO da chave,
# e não por defeito nosso. Vale a distinção porque "tenta de novo" é conselho
# errado aqui: a 403 da OpenRouter ("Key limit exceeded (total limit)", chave sem
# reset) se repete idêntica em toda tentativa, e a sessão 1e7c5943 gastou dois
# turnos batendo nela sem o usuário ter como saber que o problema era crédito —
# a informação só existia no log .md. Começa com o MESMO prefixo do
# TURN_FAILED_REPLY de propósito: é assim que
# core/agent/affect_updater._ERROR_RESPONSE_PREFIXES já a exclui do afeto, sem
# precisar de mais uma entrada lá.
TURN_FAILED_NO_CREDIT = (
    "Não consegui responder agora — o provedor recusou por limite/crédito da "
    "chave. Troca de modelo ou renova o crédito que eu sigo daqui."
)

# Assinaturas de "acabou o crédito / estourou a cota" nos provedores que usamos:
# OpenRouter 403 "Key limit exceeded", OpenAI 429 "exceeded your current quota",
# Anthropic "credit balance is too low", 402 pela frase "Payment Required" (o
# número nu não entra: "402" aparece dentro de request_id e de contagem de token,
# e classificar um bug nosso como falta de crédito manda o usuário caçar o
# problema no lugar errado). Casado no TEXTO da exceção porque cada SDK embrulha
# o corpo do erro de um jeito — não há um campo comum pra ler.
_NO_CREDIT_SIGNS = re.compile(
    r"(limit exceeded|exceeded your current quota|insufficient[_ ](?:quota|credit|balance)"
    r"|credit balance is too low|payment required|billing|out of credits?"
    r"|quota[_ ]exceeded)",
    re.IGNORECASE,
)


def _is_no_credit_failure(exc: BaseException | str) -> bool:
    """A falha foi limite/crédito da chave (ação do usuário) e não bug nosso?"""
    return _NO_CREDIT_SIGNS.search(str(exc)) is not None

# Injetado no prompt da resposta final quando o turno vem do modo Live (voz):
# o texto será FALADO por TTS e mostrado como legenda, então pedimos texto corrido
# sem markdown/emojis (que soam mal falados e poluem a legenda). Ativado por
# metadata["mode"] == "live" (enviado pelo neo-desktop-tauri).
# Teto de anexos citados no manifesto de um turno (os mais recentes da sessão).
_MAX_SESSION_ATTACHMENTS = 10

_LIVE_STYLE_INSTRUCTION = (
    "## MODO VOZ (live)\n"
    "Sua resposta será LIDA EM VOZ ALTA por TTS e exibida como legenda curta. "
    "Responda em TEXTO CORRIDO, natural e direto, como se estivesse falando. "
    "NÃO use markdown, títulos, listas/bullets, negrito, tabelas, blocos de código "
    "ou emojis. Seja conciso, porém completo o suficiente para responder. "
    "Evite ler em voz alta URLs longas, caminhos de arquivo e símbolos."
)

# Marcador de imagem inline: o cliente (neo-desktop-tauri) troca cada
# `[[IMAGE:origem]]` da resposta por a imagem RENDERIZADA na bolha do chat,
# lendo os bytes direto do disco do usuário (ou do File Store) — o conteúdo
# NUNCA passa pelo prompt. Nasceu de um turno real: pedido "carrega a imagem
# aqui no chat pra eu ver" → o modelo rodou `base64 -i foto.jpeg`, despejou o
# blob no contexto e ainda assim o usuário não viu imagem nenhuma.
# Convenção idêntica à do [[VOICE:…]]/[[HANDOFF:…]] (tolerante a espaços no
# caminho, ao contrário de `![](markdown)`).
INLINE_IMAGE_MARKER_PREFIX = "[[IMAGE:"

# Injetado quando o turno veio do microfone (metadata["input_source"] == "voice").
# É o contrário do _LIVE_STYLE_INSTRUCTION: aquele fala da SAÍDA (será falada),
# este fala da ENTRADA (foi transcrita, e o ASR erra). Medido no corpus de produção
# (368 turnos de voz, 02/08/2026): o Parakeet NUNCA escreveu "Neo" — escreveu
# Neil/New/Nil/Nir/knew. O neo-voice já corrige o que é catalogável antes de chegar
# aqui (stt_text.py); esta instrução cobre o resto: palavra colada, pontuação
# ausente, trecho em outro idioma, ruído.
# O bullet do "dado exato" não é enfeite: entrada por voz é justamente onde um token
# torto vira caminho de arquivo inventado (o bug do file_path fantasma).
_VOICE_INPUT_INSTRUCTION = (
    "## ENTRADA POR VOZ (transcrição automática)\n"
    "Esta mensagem foi TRANSCRITA de áudio por um ASR, não digitada. Ela pode ter "
    "erro de grafia, palavra colada, pontuação faltando, um trecho em outro idioma "
    "ou uma palavra solta de ruído.\n"
    "- Interprete por PROXIMIDADE FONÉTICA e pelo contexto. \"Neil\", \"Nil\", \"New\", "
    "\"Niu\", \"Nir\", \"He knew\", \"A new\" são VOCÊ (Neo). Nome de pessoa, lugar ou "
    "dispositivo também pode vir torto.\n"
    "- NÃO responda \"não entendi\" por causa de um erro pequeno: responda ao que é "
    "obviamente a intenção.\n"
    "- NUNCA transforme um token estranho em caminho de arquivo, nome de arquivo, "
    "entity_id, comando ou parâmetro literal. Se o dado exato importa (caminho, id, "
    "valor, nome próprio novo), CONFIRME numa pergunta curta antes de agir.\n"
    "- Se a mensagem inteira for incompreensível (uma ou duas palavras sem relação "
    "com a conversa), peça para repetir numa frase curta — não invente assunto.\n"
    "- Não comente nem corrija a transcrição em voz alta."
)

_INLINE_IMAGE_INSTRUCTION = (
    "## Mostrar imagem no chat\n"
    "O chat deste usuário RENDERIZA imagens. Para exibir uma, escreva numa linha só:\n"
    "`[[IMAGE:/caminho/absoluto/da/imagem.jpg]]` — pode ter espaços no caminho, "
    "sem aspas e sem escapes. Para um arquivo do SEU catálogo (File Store), use "
    "`[[IMAGE:file:<file_id>]]`. Para uma imagem da internet (a foto de um "
    "anúncio, por exemplo), use a URL http(s) direto: "
    "`[[IMAGE:https://cdn.loja.com/foto.jpg]]` — o app baixa e desenha. Legenda "
    "opcional depois de `|`: `[[IMAGE:/caminho/foto.png|a foto de ontem]]`.\n"
    "Regras:\n"
    "- Use SEMPRE que o usuário pedir para ver/mostrar/carregar/exibir uma imagem, "
    "ou quando uma imagem for a melhor resposta (um print, um gráfico que você gerou).\n"
    "- NUNCA converta imagem em base64/xxd/data-uri para \"mostrar\": isso não exibe "
    "nada, só polui a conversa. O marcador é o único jeito.\n"
    "- O caminho tem que ser REAL: só use um que apareça nesta conversa (mensagem do "
    "usuário, saída de um comando, anexo). NUNCA invente ou adivinhe caminho/nome — se "
    "não souber, ache primeiro (ex.: `ls`/`find`) ou pergunte.\n"
    "- A URL também tem que ser REAL: só use uma que VOCÊ viu num resultado de tool "
    "(web_search/web_fetch/browser_task) nesta conversa. Montar URL de imagem por "
    "dedução é o mesmo erro de inventar caminho — o usuário vê um cartão de erro.\n"
    "- Escreva o marcador junto de uma frase curta ('aqui está:'), não sozinho, e um "
    "marcador por imagem (várias linhas = várias imagens). O marcador não é falado nem "
    "lido em voz alta — é substituído pela imagem."
)

_CLARIFICATION_INSTRUCTION = (
    "## Esclarecimento quando faltar contexto\n"
    "Se você NÃO tem informação suficiente para responder com confiança — a mensagem se "
    "refere a algo (um valor, uma ação, um \"isso/aquilo\") que não está no histórico nem "
    "no contexto fornecido — NÃO invente e NÃO diga apenas \"não sei\". Em vez disso, comece "
    f"sua resposta com a linha `{NEED_CLARIFICATION_SENTINEL}` seguida de UMA pergunta curta e "
    "objetiva que destrave a resposta. Use esse recurso só quando realmente faltar o essencial; "
    "se der para responder com o que há, responda normalmente (sem o marcador)."
)

_CLARIFICATION_FINAL_ROUND = (
    "## Importante\n"
    f"NÃO use `{NEED_CLARIFICATION_SENTINEL}` desta vez. Você já pediu esclarecimento antes; "
    "dê a melhor resposta definitiva possível com a informação disponível."
)


# Memory-verification questions ("tu salvou mesmo?", "tá no PKS?", "não parece que
# foi incluído") must be answered from an ACTIVE PKS read, never from the chat
# history (where the assistant may have wrongly claimed success). Detected
# deterministically; the real search results are injected into the prompt.
_MEMORY_VERIFICATION_PATTERNS = [
    re.compile(p)
    for p in (
        r"\bsalvou\s+mesmo\b",
        r"\b(ta|esta|estao|tao)\s+salv[oa]s?\b",
        r"\bfoi\s+(salvo|salva|incluido|incluida|registrado|registrada|gravado|gravada)\b",
        r"\bnao\s+(parece|foi|esta|ta)\s+.{0,30}(salv|inclu|registr|grav|anot)",
        r"\b(verifica|olha|confere|checa|busca|procura)\w*\s+(no|na|o|a)?\s*\b(pks|memoria)\b",
        r"\b(tem|ta|esta)\s+(no|na)\s+(pks|memoria|sua memoria)\b",
        r"\btem\s+certeza\s+que\s+(salvou|registrou|anotou|gravou|guardou)\b",
        r"\b(salvou|registrou|anotou|gravou|guardou)\s+(isso\s+)?(no|na)\s+(pks|memoria)\b\s*\?",
    )
]

_MEMORY_VERIFICATION_NOTE_HEADER = "## Verificação ativa do PKS (search_pks)"

# When the cognitive core has tools, we don't inject results — we NUDGE it to call
# search_pks itself (agency preserved; guaranteed for the honesty-critical case).
_MEMORY_VERIFICATION_NUDGE = (
    "## Verificação obrigatória de memória\n"
    "O usuário está duvidando se algo foi REALMENTE salvo. ANTES de responder, chame "
    "a tool `search_pks` para verificar no PKS de verdade (busque pelo que ele acha "
    "que salvou). Responda APENAS com base no resultado real: se NÃO aparecer, admita "
    "que não está salvo (mesmo que você tenha dito antes que salvou) e ofereça salvar "
    "agora. NUNCA confirme um salvamento com base só no histórico da conversa."
)

# Words stripped from the user's question before using it as a search query.
_VERIFICATION_STOPWORDS = frozenset({
    "tu", "voce", "você", "salvou", "salvo", "salva", "mesmo", "mesma", "sera",
    "que", "nao", "não", "parece", "foi", "incluido", "incluído", "inluido",
    "pks", "memoria", "memória", "olha", "olhei", "dei", "uma", "olhada", "seu",
    "sua", "no", "na", "o", "a", "de", "do", "da", "e", "eu", "verifica",
    "confere", "checa", "tem", "certeza", "registrou", "anotou", "gravou",
    "guardou", "isso", "aquilo", "la", "lá", "ta", "tá", "esta", "está",
})


# Perguntas sobre o ARQUIVO de conversas ("a gente já falou disso?", "você
# lembra quando eu disse...") não podem ser respondidas de cabeça: o cognitivo
# só tem no contexto as mensagens DESTA sessão, então negar é chutar. Foi
# exatamente isso na session-860d0fb1 ("não tem nada sobre a França registrado",
# com 61 mensagens sobre França no banco). Ver docs/conversation-recall-plan.md.
_CONVERSATION_RECALL_PATTERNS = [
    re.compile(p)
    for p in (
        r"\b(voce|vc|ce|tu)\s+(se\s+)?lembra\b",
        r"\bse\s+lembra\s+(de|que|quando|do|da)\b",
        r"\b(a\s+gente|nos|noix)\s+(ja\s+)?(conversou|conversamos|falou|falamos|discutiu|discutimos)\b",
        r"\beu\s+(ja\s+)?(te\s+)?(falei|disse|contei|mostrei|comentei)\b",
        r"\b(conversei|falei|comentei)\s+(algo|alguma\s+coisa|sobre)?\s*(contigo|com\s+voce|com\s+vc)\b",
        r"\b(o\s+que|oque)\s+eu\s+(te\s+)?(falei|disse|contei|comentei)\b",
        r"\bja\s+(te\s+)?(falei|disse|contei|comentei|mostrei)\b",
        r"\b(quando|onde)\s+(foi\s+que\s+)?(eu|a\s+gente|nos)\s+(falou|falamos|conversou|conversamos|disse)\b",
        r"\b(lembra|recorda)\s+(daquela|daquele|da|do|de)\s+(conversa|papo|assunto|vez)\b",
        r"\bnossas?\s+conversas?\s+(anteriores|passadas|antigas)\b",
        r"\bja\s+(conversamos|falamos|discutimos)\s+(sobre|disso|nisso)\b",
    )
]

# Continuação de um recall já feito: "não foi isso, foi antes ainda". Frase curta
# e vaga — sem o gatilho, o modelo pede desculpa e reformula em prosa em vez de
# rebuscar mais para trás.
_RECALL_CONTINUATION_PATTERNS = [
    re.compile(p)
    for p in (
        r"\bnao\s+(foi|era)\s+(isso|essa|esse|essas|esses|dessa|nenhum)",
        r"\bfoi\s+(antes|mais\s+antes|antes\s+ainda|antes\s+disso)\b",
        r"\bantes\s+(disso|dessa|desse)\b",
        r"\bmais\s+(antig[oa]|pra\s+tras|para\s+tras|atras|longe)\b",
        r"\b(tem|teve|ha)\s+mais\s+(antig|coisa|conversa)",
        r"\bprocura\s+mais\s+(pra\s+tras|para\s+tras|atras|antes)\b",
        r"\bvai\s+mais\s+(pra\s+tras|para\s+tras|atras|longe)\b",
        r"\boutra\s+(conversa|vez)\b",
    )
]

_RECALL_NUDGE = (
    "## Busca obrigatória no arquivo de conversas\n"
    "O usuário está perguntando sobre uma conversa ANTERIOR. Você NÃO tem as "
    "conversas antigas no contexto — só as mensagens desta sessão — então "
    "responder de cabeça é chutar. ANTES de responder, chame "
    "`conversation_recall` com o ASSUNTO (só o assunto: 'frança', 'treino de "
    "perna' — nunca 'conversa' ou 'lembra'). Responda apenas com o que voltar: "
    "se voltar vazio, aí sim pode dizer que não acharam nada; se voltar algo, "
    "conte o que foi, situando no tempo. NUNCA diga 'não falamos disso' sem ter "
    "chamado a tool nesta mesma resposta."
)

_RECALL_CONTINUATION_NUDGE = (
    "## O usuário disse que não era aquela conversa\n"
    "Ele quer uma conversa MAIS ANTIGA sobre o mesmo assunto. Chame "
    "`conversation_recall` de novo com a MESMA query e o `before` indicado na "
    "seção \"Arquivo de conversas\" — não repita a busca sem `before` (voltaria "
    "o mesmo resultado) e não invente uma lembrança para agradar."
)


def _is_conversation_recall_request(text: str) -> bool:
    """True quando o usuário pergunta sobre uma conversa passada."""
    normalized = _normalize_for_match(text)
    return any(p.search(normalized) for p in _CONVERSATION_RECALL_PATTERNS)


def _is_recall_continuation(text: str) -> bool:
    """True quando o usuário rejeita o recall anterior e quer ir mais para trás."""
    normalized = _normalize_for_match(text)
    return any(p.search(normalized) for p in _RECALL_CONTINUATION_PATTERNS)


def _normalize_for_match(text: str) -> str:
    """Minúsculas, sem acento, espaços colapsados — para casar as regexes."""
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", (text or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(normalized.split())


def _is_memory_verification_request(text: str) -> bool:
    """True when the user is questioning whether something was really saved."""
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", (text or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    normalized = " ".join(normalized.split())
    return any(p.search(normalized) for p in _MEMORY_VERIFICATION_PATTERNS)


# Pipeline handoff (Phase 2): the cognitive core may decide, after reasoning over
# the PKS/context, that a concrete action must run via a skill. It appends a control
# block that the user never sees; the orchestrator parses it and chains the skill.
_HANDOFF_INSTRUCTION_HEADER = (
    "## Acionar uma skill (handoff)\n"
    "Você é o núcleo cognitivo do Neo. Se, DEPOIS de analisar o contexto/PKS, você "
    "concluir que uma AÇÃO concreta deve ser executada agora por uma skill, escreva "
    "normalmente sua resposta ao usuário e, ao FINAL, adicione um bloco de controle "
    "(que o usuário NÃO vê), com a ORDEM REAL escrita dentro — por exemplo:\n"
    "[[HANDOFF:home_assistant]]ligar o ar-condicionado do quarto[[/HANDOFF]]\n"
    "NUNCA escreva a palavra \"ordem\" nem os sinais `< >` dentro do bloco — vá a ação "
    "de verdade.\n\n"
    "Skills disponíveis:\n"
)

# Por quantos turnos SEM keyword a skill continua ativa na sessão. Cobre o vaivém
# normal de um fluxo ("so salva" / "variados" / "sim") sem grudar para sempre; cada
# uso real de uma tool da skill renova o prazo.
_SKILL_STICKY_TURNS = 5


def _dedup_tools_by_name(tools: list[Any]) -> list[Any]:
    """Uma tool por nome no payload — duplicata é 400 em provider que valida.

    O toolset do turno é montado por camadas independentes (read set + tools da
    skill autocarregada + action tools do workbench/jobs), e cada camada só sabia
    deduplicar contra a anterior. Bastava uma skill `tool_backed` listar uma tool
    que só existe numa camada POSTERIOR — `compras` lista `browser_task`, que vem
    em JOB_TOOLS — para o mesmo nome entrar duas vezes e o DeepSeek recusar o
    turno inteiro com 400 "Tool names must be unique." (sessões c35e576f e
    1e7c5943). A OpenAI aceita o payload duplicado, então o bug só aparecia em
    metade dos modelos: mais motivo para a garantia ficar aqui, no choke point,
    e não em cada ponto de append.

    Mantém a PRIMEIRA ocorrência (a camada mais específica entra antes) e a ordem.
    """
    seen: set[str] = set()
    out: list[Any] = []
    dropped: list[str] = []
    for t in tools or []:
        name = getattr(t, "name", None)
        if name is None:
            out.append(t)
            continue
        if name in seen:
            dropped.append(str(name))
            continue
        seen.add(name)
        out.append(t)
    if dropped:
        logger.warning(
            "[Tools] nomes duplicados no toolset do turno, mantida a 1a ocorrencia: %s",
            ", ".join(sorted(set(dropped))),
        )
    return out

# Tools que competem com o bloco [[HANDOFF:music]] num turno de música. O bloco é
# quem atua de verdade: ele chega na skill, que manda `media_command` para a
# extensão do navegador — instantâneo e reusando a aba já aberta. Estas três
# fazem "a mesma coisa" de um jeito pior, e o modelo prefere a TOOL ao bloco.
_MUSIC_TURN_TEMPTATIONS = frozenset({"browser_local_task", "browser_task", "run_command"})

# `device_control` muta/abaixa o volume da MÁQUINA. Quando o pedido é sobre a
# ABA ("muta essa aba tocando esse video"), usá-la é desproporcional e foi o que
# aconteceu: o Neo silenciou o MacBook inteiro (volume 81% → mudo) e AINDA
# emitiu o handoff — duas atuações, uma delas errada (sessão 0565907e). Fora
# desse recorte ela fica: "abaixa o volume" sem citar aba pode ser a máquina
# mesmo, e tirar a tool ali seria trocar um erro por outro.
_MEDIA_TAB_ANCHORS = (
    "aba", "abas", "guia", "guias", "tab", "tabs",
    "video", "player", "youtube", "spotify", "navegador",
)


def _targets_browser_tab(user_message: str) -> bool:
    """A ordem é sobre a ABA do navegador (e não sobre a máquina)?"""
    texto = "".join(
        c
        for c in unicodedata.normalize("NFD", (user_message or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    return any(re.search(rf"\b{a}\b", texto) for a in _MEDIA_TAB_ANCHORS)


def _drop_music_turn_temptations(
    tools: list[Any], skill_hint: str | None, user_message: str = ""
) -> list[Any]:
    """Tira do turno de música as tools que desviam a atuação para fora da skill.

    Medido em produção: "toca duality do slipknot" virou job de
    `browser_local_task` (26 s dirigindo o Edge passo a passo) e "coloca uma
    musica do meu mix" virou `browser_task` num Chromium headless da VPS, que
    morreu no anti-bot do YouTube — as duas com o bloco ali, de graça. E
    "consegue mutar essa aba" virou `run_command` improvisando `osascript` para
    o Chrome numa máquina que usa Edge.

    Mesmo argumento do ``search_pks`` quando há skill ativa: a tool que não deve
    ser usada neste turno não é opção, é tentação. Fora do turno de música nada
    muda.
    """
    if skill_hint != "music":
        return tools
    fora = set(_MUSIC_TURN_TEMPTATIONS)
    if _targets_browser_tab(user_message):
        fora.add("device_control")
    return [t for t in tools if getattr(t, "name", None) not in fora]


# A resposta AFIRMA ter executado algo (passado, não promessa). Usado para barrar a
# mentira quando nenhuma tool da skill rodou no turno — o modelo dizia "registrei,
# pode confiar" com zero chamadas (sessão d4e2ba0f). Só formas no passado: "posso
# registrar?" e "quer que eu registre?" não disparam.
_CLAIMS_ACTION = re.compile(
    r"\b("
    r"registr(ei|ado|ada|amos)|lanc(ei|ado|ada)|lanç(ei|ado|ada)|salv(ei|o|a)"
    r"|cri(ei|ado|ada)|adicion(ei|ado|ada)|apagu?ei|apagad[oa]|exclu(i|í|ído|ida)"
    r"|atualiz(ei|ado|ada)|marqu?ei|transferi|paguei"
    # O detector só pega o que ele consegue NOMEAR: "arquivado" faltava aqui e a
    # mentira passou batida ("pronto, cartão arquivado" — com o banco intacto).
    r"|arquiv(ei|ado|ada)|cancel(ei|ado|ada)|desfiz|desfeit[oa]|revert(i|ido|ida)"
    r"|remov(i|ido|ida)|limpei|zerei|renome(ei|ado|ada)"
    r")\b",
    re.IGNORECASE,
)
# "não registrei", "ainda não salvei" — negação não é afirmação de ter feito.
_NEGATED_CLAIM = re.compile(r"\b(nao|não)\s+(\w+\s+){0,2}$", re.IGNORECASE)


def _claims_action_without_tool(text: str) -> bool:
    """A resposta diz que FEZ (registrou/criou/apagou), sem negação na frente?"""
    for m in _CLAIMS_ACTION.finditer(text or ""):
        prefix = (text or "")[max(0, m.start() - 24) : m.start()]
        if not _NEGATED_CLAIM.search(prefix):
            return True
    return False


# "vou zipar e já te mando" / "assim que estiver pronto, te entrego" / "segue o
# próximo passo" — o modelo narra o resto da entrega como se fosse acontecer,
# encerra o turno, e nada do que prometeu roda de verdade (sessões 7b0956de e
# 918e515d: escreveu 1-3 arquivos, prometeu zipar/baixar imagens, nunca fez).
_PENDING_PROMISE = re.compile(
    r"\b(vou|vamos)\s+\w*\s*(zipar|compactar|baixar|montar|gerar|escrever|mandar|enviar"
    r"|entregar|terminar|finalizar|preparar|criar|construir)\w*"
    r"|\bj[áa]\s+te\s+(mando|envio|entrego)\b"
    r"|\bpr[óo]ximo\s+passo\b"
    r"|\bassim\s+que\s+estiver\s+(tudo\s+)?pronto\b"
    r"|\bs[óo]\s+um\s+instante\b",
    re.IGNORECASE,
)

# Tools legítimas pra ADIAR de verdade (job assíncrono) — se alguma rodou neste
# turno, uma resposta "vou avisar quando terminar" é o comportamento CERTO, não
# o bug: o workbench pede exatamente isso pra trabalho demorado.
_LEGITIMATE_DEFERRAL_TOOLS = {
    "neo_bash_background", "job_status", "job_list", "job_logs", "job_cancel",
    "deep_research", "browser_task", "browser_local_task",
}


# ── Guarda anti-promessa de NAVEGADOR LOCAL ─────────────────────────────────
# Medida na sessão d01cb5ab: pedido "abra o painel do simples nacional no meu
# local", o modelo respondeu "Vou iniciar a tarefa... Abrindo o painel no seu
# navegador agora" — com ZERO tool call. Cobrado ("nao aconteceu nada"), dobrou:
# "a tarefa foi enfileirada, te aviso". Nada rodou nas duas vezes.
#
# Por que a anti-promessa genérica não pega: a lista de verbos dela é de ENTREGA
# (zipar/baixar/mandar); "abrir/iniciar" não está lá. E "foi enfileirada" é
# alegação no PASSADO, forma que nenhuma das duas guardas cobria.
#
# Desenho igual ao da anti-alegação-de-download: AFORDÂNCIA ∧ ÂNCORA, nenhuma
# palavra decide sozinha. A âncora é o que torna isto seguro — tem que ser o
# navegador DELE ("seu navegador", "navegador local", "na sua tela"), nunca o do
# servidor. Medido contra 1738 respostas reais do assistente: 3 disparos, sendo
# 2 as mensagens exatas do bug acima; o terceiro ("Abri o YouTube com os
# resultados") é o fluxo de música, que ATUA de verdade e sai pela checagem de
# tool usada. Ver tests/browser_local/test_promise_guard.py.
_BROWSER_PROMISE_ANCHOR = re.compile(
    r"\b(?:seu|teu)\s+(?:navegador|chrome|brave|edge|browser)\b"
    r"|\bnavegador\s+local\b|\bna\s+sua\s+tela\b|\bacompanhar\s+na\s+tela\b"
    r"|\bnavegador\s+(?:a[íi]|daqui)\b",
    re.IGNORECASE,
)

# COMPROMISSO: o assistente se colocando como quem VAI fazer (ou já fez). A
# primeira versão enumerava verbos (abrir/acessar/entrar/iniciar) e vazou na
# sessão 91fae2d1 em "vou COLOCAR a live no seu navegador" e "vou FORÇAR a
# abertura" — enumerar verbo de ação em português é jogo perdido. Como a âncora
# é rara (11 em 1745 respostas reais), o compromisso pode ser largo: o que
# segura o falso positivo é a âncora + a negação, não a lista.
_COMMIT = re.compile(
    r"\b(?:vou|vamos|irei|iremos)\b"                                   # 1ª pessoa, futuro
    r"|\b(?:vai|v[ãa]o|est[áa]|est[ãa]o|estou|estiver|estarei|fica|ficar)\s+\w{3,}ndo\b"
    r"|\b(?:vai|v[ãa]o|j[áa]\s+vai)\s+\w{3,}(?:ar|er|ir)\b"          # auxiliar + infinitivo
    r"|(?:^|[.!?\n]\s*)\w{4,}ndo\b"                                   # gerúndio abrindo a frase
    r"|\b(?:foi|est[áa])\s+(?:enfileirad\w+|em\s+andamento|rodando)"  # alegação de já ter feito
    r"|\b(?:enfileirei|iniciei|abri|disparei|coloquei\s+na\s+fila)\b",
    re.IGNORECASE,
)

# Negação REGENDO a frase da âncora: "não consigo acionar extensões direto do
# seu navegador" é recusa honesta, não promessa vazia.
_COMMIT_NEGATOR = re.compile(
    r"\bn[ãa]o\b|\bnem\b|\bsem\b|\bimposs[íi]vel\b|\bfoge\s+do\s+meu\b|\bprecisa\s+de\b",
    re.IGNORECASE,
)

# Handoff é ação DE VERDADE por outro trilho do pipeline (música, casa). O turno
# não terminou numa promessa — ele delegou. Sem esta saída, "YouTube vai abrir
# no seu navegador" (que vira [[HANDOFF:music]]) seria punido.
_HANDOFF_ANY = re.compile(r"\[\[\s*HANDOFF\s*:", re.IGNORECASE)

# Qualquer tool que de fato MEXE no navegador/máquina do usuário absolve a
# promessa: se ela rodou, a frase é relato, não conversa fiada.
_BROWSER_ACTING_TOOLS = frozenset(
    {"browser_local_task", "browser_task", "run_command", "open_url", "launch_uri", "media_command"}
)

_SENTENCE_PIECES = re.compile(r"[^.!?\n]+[.!?\n]?")


def _promises_browser_action(text: str) -> bool:
    """A resposta diz que vai agir (ou que já agiu) no navegador DELE, por FRASE.

    Escopo de frase, não de texto inteiro: "não consigo abrir no seu navegador,
    mas vou pesquisar aqui" tem promessa E âncora, e mesmo assim não é o bug.
    """
    text = text or ""
    if not text.strip() or _HANDOFF_ANY.search(text):
        return False
    for sentence in _SENTENCE_PIECES.findall(text):
        if not _BROWSER_PROMISE_ANCHOR.search(sentence):
            continue
        if _COMMIT_NEGATOR.search(sentence):
            continue
        if _COMMIT.search(sentence):
            return True
    return False


def _promises_pending_action(text: str) -> bool:
    """A resposta narra o que ainda VAI fazer (zipar/baixar/mandar) em vez de
    fazer — sinal de que o turno terminou no meio de uma entrega."""
    return bool(_PENDING_PROMISE.search(text or ""))


# ── Guarda anti-alegação-de-download ────────────────────────────────────────
# "Arquivo está pronto. Você pode baixar pelo botão abaixo." — sem ter chamado
# file_save nenhuma vez (sessão 95ccd52d: só neo_write_file, nunca file_save).
# Diferente de _PENDING_PROMISE (futuro: "vou fazer"), aqui é afirmação no
# presente/passado de que JÁ dá pra baixar — nem o anti-mentira (verbos
# registrei/criei/salvei, escopado a skill_tool_names) nem o anti-promessa
# pegam essa frase. Combinado com produced_files vazio, é sempre falso: não
# existe entrega catalogada pro usuário clicar.
#
# A checagem era UM regex com `\bdownload\b` solto. Medido contra as 1450
# mensagens reais de assistant do banco, isso dava 20 disparos: ~7 falsos
# positivos E 11 alegações reais NÃO pegas — ruim nos dois eixos. O pior FP
# derrubava o turno inteiro: "[[IMAGE:/Users/daniel/Downloads/download.png]]"
# (entrega inline correta) casava pela palavra dentro do CAMINHO, forçava
# tool_choice="required" e tomava 400 do DeepSeek em thinking mode (sessão
# 1aec71b8) — ou seja, pedir qualquer coisa da pasta Downloads quebrava.
#
# Desenho novo: alegação = AFORDÂNCIA ∧ ÂNCORA, afirmativa. Nenhuma palavra
# sozinha decide. (Uma camada "menciona artefato?" foi testada e REMOVIDA: nas
# 1450 mensagens ela não barrava um único caso que a âncora já não barrasse, e
# em compensação matava "Está pronto, pode baixar pelo botão abaixo." — resposta
# sem o nome do arquivo, que é alegação legítima.) Ver tests/test_download_claim_guard.py — o corpus de
# lá é rotulado à mão a partir desses disparos reais.

# Camada 0 — MÁSCARA: o que o modelo apenas CITA não é o que ele AFIRMA.
# REGRA DE OURO: a máscara só pode comer literal inequívoco. Máscara que come
# prosa faz a guarda parar de guardar EM SILÊNCIO — falha pior que o FP. (Uma
# versão intermediária aceitava `/` solto como início de caminho e engolia
# "julho/2026 está pronto. Você pode baixar pelo botão abaixo." inteiro.)
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_CLIENT_MARKER = re.compile(r"\[\[[A-Z_]+:[^\]]*\]\]")
_MD_LINK_ANY = re.compile(r"\[[^\]]*\]\([^)]*\)")
_URL_LITERAL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_FS_PATH_LITERAL = re.compile(
    r"(?:~|\.{1,2})/[\w.\-]+(?:/[\w.\-]+)*"      # ~/x, ./x, ../x
    r"|/[\w.\-]+(?:/[\w.\-]+)+"                   # /Users/daniel/… (2+ segmentos)
    r"|[A-Za-z]:\\[\w.\-]+(?:\\[\w.\-]+)*"        # C:\…
)
_FILE_EXT = (
    r"png|jpe?g|gif|webp|heic|bmp|tiff|svg|pdf|csv|xlsx?|docx?|pptx?"
    r"|zip|tar|gz|rar|txt|md|json|ya?ml|mp3|wav|mp4|mov"
)
_FILENAME_LITERAL = re.compile(rf"\b[\w.\-]+\.(?:{_FILE_EXT})\b", re.IGNORECASE)

# Camada 1 — AFORDÂNCIA. FORTE: a construção sozinha já é a alegação.
_READY_STATE = r"(?:pronto|prontinho|dispon[íi]vel|gerado|criado|salvo|guardado|terminei|segue)"
_DOWNLOAD_CLAIM_STRONG = re.compile(
    rf"\b{_READY_STATE}\b[^.!?\n]{{0,40}}?\b(?:para|pra|pro)\s+download\b"
    r"|\bbot[ãa]o\s+(?:de\s+)?download\b"
    r"|\bbot[ãa]o\s+(?:abaixo|acima|embaixo|a[íi])\b"
    r"|\bcliqu(?:e|ar|ando)\s+no\s+bot[ãa]o\b"
    r"|\bpode\s+ser\s+baixad[oa]\b"
    r"|\b(?:em|no)\s+anexo\b|\banexei\b|\bsegue\s+o\s+arquivo\b",
    re.IGNORECASE,
)
# FRACA: "pode baixar" só é alegação NOSSA se estiver ancorada numa superfície
# do Neo — senão pega "pode baixar essas imagens manualmente em cdn.pixabay.com"
# (instrução pro usuário puxar da internet). O `(?!…)` no preenchimento impede
# que a negação entre DENTRO do match ("Só não consegui baixar as imagens") e
# escape da checagem de polaridade, que só olha o que vem ANTES.
_DOWNLOAD_CLAIM_WEAK = re.compile(
    r"\b(?:pode|podes|d[áa]\s+pra|[ée]\s+s[óo]|s[óo]|para|pra|pro)\s+"
    r"(?:(?!n[ãa]o\b|nem\b|sem\b)\w+\s+){0,2}?baix(?:ar|e|á-?l[oa])\b"
    # "…pra download na interface" — mesma afirmação com o substantivo no lugar
    # do verbo. Sem ready-state antes não é forte o bastante pra valer sozinha
    # (a oferta "quer que eu gere o arquivo pra download?" tem a mesma forma e
    # só se distingue pela interrogação), então entra aqui, exigindo âncora.
    r"|\b(?:para|pra|pro)\s+download\b",
    re.IGNORECASE,
)
_DELIVERY_SURFACE = re.compile(
    r"\bbot[ãa]o|\binterface\b|\bpainel\b|\bconversa\b|\bchat\b|\banexo\b"
    r"|\bcat[áa]logo\b|\baba\s+arquivos\b|\blink\b|\babaixo\b|\bacima\b|\bembaixo\b",
    re.IGNORECASE,
)
# Link markdown pro sandbox / caminho absoluto: o usuário não tem como abrir.
# Oferecer isso como clicável é falso com ou sem verbo — dispara sozinho.
_FAKE_DELIVERY_LINK = re.compile(
    r"\[[^\]]*\]\(\s*(?:sandbox:|file://|/|~/)[^)]*\)", re.IGNORECASE
)

# Camada 2 — POLARIDADE.
_CLAIM_NEGATOR = re.compile(
    r"\b(?:n[ãa]o|nem|sem|nenhum|falh(?:ou|ei|a))\b|\berro\b", re.IGNORECASE
)
_CLAIM_CONDITIONAL = re.compile(r"\b(?:se|caso|quando)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
# "vou zipar e te entrego o link pra baixar" é PROMESSA — quem trata é
# _promises_pending_action, que tem a correção certa pro caso. Só a intenção do
# PRÓPRIO assistente conta: "o botão vai aparecer" é afirmação sobre o artefato.
_FUTURE_INTENT = re.compile(r"\b(?:vou|vamos)\s+\w", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"[^.!?\n]+[.!?]?")


def _prose_only(text: str) -> str:
    """Texto sem os literais que o modelo cita (código, caminho, URL, marcador)."""
    for pat in (
        _FENCED_CODE, _INLINE_CODE, _CLIENT_MARKER, _MD_LINK_ANY,
        _URL_LITERAL, _FS_PATH_LITERAL, _FILENAME_LITERAL,
    ):
        text = pat.sub(" ", text)
    return text


def _claim_is_negated(sentence: str, upto: int) -> bool:
    """Negação REGENDO a alegação? "não tem botão" nega; "se não aparecer" não."""
    for neg in _CLAIM_NEGATOR.finditer(sentence[:upto]):
        if _CLAIM_CONDITIONAL.search(sentence[: neg.start()]):
            continue
        return True
    return False


# O prompt do planner manda usar um placeholder como comando de um passo
# DEPENDENTE ("[SERÁ GERADO COM BASE NOS ARQUIVOS LIDOS]") — o comando real só
# existe depois que o passo anterior devolve saída, e quem monta é o
# `advance_and_refine`. Quando esse refinamento falha (o vazio do DeepSeek —
# ver deepseek-empty-response-reasoning), o fallback é "continua com o passo
# como está" e o placeholder ia pro shell do usuário como comando literal:
# roda, dá "command not found" e o plano fecha com lixo. Um placeholder não é
# comando; vale a mesma trava do comando vazio.
_PLACEHOLDER_COMMAND = re.compile(
    r"^\s*[\[<]\s*(ser[áa]\s+gerado|to\s+be\s+generated|conte[úu]do|comando)\b[^\]>]*[\]>]\s*$",
    re.IGNORECASE,
)


def _dispatch_would_hang(tool_name: str | None, arguments: dict[str, Any] | None) -> bool:
    """True quando o ``tool_request`` sairia sem nada de real pro cliente executar.

    `run_command` sem `command` é travamento GARANTIDO: o desktop recebe o pedido,
    não tem o que rodar e nunca devolve `tool_result` — o turno fica "Executando"
    até a conexão cair (sessões 9f7abeec e e8e5664b). Existem DOIS despachos de
    client tool (o direto e o de passo de plano) e ambos já produziram esse estado;
    por isso a checagem é uma função só, usada nos dois.

    Um placeholder não-refinado entra na mesma conta: não trava o turno, mas
    despacha um comando que nunca foi para ser executado.
    """
    if tool_name != "run_command":
        return False
    args = arguments or {}
    command = str(args.get("command") or args.get("cmd") or "").strip()
    return not command or bool(_PLACEHOLDER_COMMAND.match(command))


# ── Guarda de verdade-do-resultado (docs/self-continuation-plan.md §4) ────────
# Irmã da anti-alegação-de-download. Aquela cobre a ENTREGA ao usuário ("pode
# baixar pelo botão" sem file_save); esta cobre o degrau anterior e mais básico:
# "o arquivo EXISTE no meu sandbox". Medida na sessão bb22cc33, onde o passo final
# do ciclo fechou com "três imagens baixadas… As imagens são reais" e /workspace
# não tinha imagem nenhuma. Sem isto, "continuar até concluir" quer dizer
# "continuar até o modelo DIZER que concluiu".
#
# Camada 1 — AFORDÂNCIA: verbo de criação no PASSADO (não promessa; futuro é do
# _promises_pending_action) ancorado num substantivo de artefato. Os dois juntos,
# porque "baixei" sozinho pega "baixei o tom" e "imagem" sozinho pega qualquer
# frase sobre a imagem que o usuário mandou.
_ARTIFACT_DONE_VERB = re.compile(
    r"\b(?:baix(?:ei|amos|ou|ada|adas|ado|ados)"
    r"|salv(?:ei|amos|ou|a|as|o|os)"
    r"|ger(?:ei|amos|ou|ada|adas|ado|ados)"
    r"|cri(?:ei|amos|ou|ada|adas|ado|ados)"
    r"|escrev(?:i|emos|eu|ita|itas|ito|itos)"
    r"|convert(?:i|emos|eu|ida|idas|ido|idos)"
    r"|zip(?:ei|amos|ou|ada|ado)"
    r"|montei|empacotei|coloquei|deixei)\b",
    re.IGNORECASE,
)
_ARTIFACT_NOUN = re.compile(
    r"\b(?:arquivos?|imagens?|imagem|fotos?|figuras?|[íi]cones?|zip|pastas?"
    r"|diret[óo]rios?|scripts?|p[áa]ginas?|html|css|jpe?gs?|pngs?|svgs?|pdfs?)\b",
    re.IGNORECASE,
)
# Afirmação de existência sem verbo de criação: "as imagens são reais",
# "estão em /workspace/x". É exatamente como a mentira medida foi escrita.
_ARTIFACT_EXISTS = re.compile(
    r"\b(?:est[áã]o?|ficaram?|s[ãa]o)\s+(?:\w+\s+){0,3}?"
    r"(?:em|na|no|dentro|reais?|reais|prontas?|prontos?|baixad[ao]s?)\b",
    re.IGNORECASE,
)

# Caminhos e nomes de arquivo CITADOS na resposta — extraídos do texto CRU, não do
# `_prose_only` (que apaga justamente path e filename). São eles que a gente cruza
# contra o tool_trace: a pergunta é "algum tool que deu certo tocou esse caminho?".
_CLAIMED_PATH = re.compile(r"(?:/workspace|/skills|/tmp|~)/[\w./-]+")
_CLAIMED_FILENAME = re.compile(
    r"\b[\w.-]+\.(?:html?|css|js|json|jpe?g|png|gif|svg|webp|zip|pdf|md|txt|csv|py|sh)\b",
    re.IGNORECASE,
)


def _claimed_artifact_targets(text: str) -> set[str]:
    """Caminhos/nomes de arquivo que a resposta afirma ter produzido."""
    out = {m.group(0).rstrip(".,;:)") for m in _CLAIMED_PATH.finditer(text or "")}
    out |= {m.group(0).lower() for m in _CLAIMED_FILENAME.finditer(text or "")}
    # O basename do path também vale como alvo: o tool pode ter escrito com caminho
    # relativo (`neo_list_files {'path': 'portifolio-naruto'}` no log real).
    out |= {p.rsplit("/", 1)[-1].lower() for p in out if "/" in p}
    return {t for t in out if t}


def _claims_sandbox_artifact(text: str) -> bool:
    """A resposta AFIRMA (passado, não promessa) ter criado artefato no sandbox?"""
    for sent in _SENTENCE_SPLIT.finditer(_prose_only(text or "")):
        sentence = sent.group(0)
        if sentence.rstrip().endswith("?"):
            continue
        match = _ARTIFACT_DONE_VERB.search(sentence) or _ARTIFACT_EXISTS.search(sentence)
        if not match or not _ARTIFACT_NOUN.search(sentence):
            continue
        if _FUTURE_INTENT.search(sentence[: match.start()]):
            continue
        # "não consegui baixar as imagens" é a resposta HONESTA — nunca punir.
        if _claim_is_negated(sentence, match.start()):
            continue
        return True
    return False


_LISTING_FAILED = re.compile(r"'ok':\s*False|cannot access|No such file", re.IGNORECASE)

# Um `file_id` com forma de UUID que não está no catálogo é id ALUCINADO — a trava
# fecha nele. Só o que não tem forma de id é candidato a ser um nome de arquivo.
_UUID_LIKE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _artifact_evidence_text(entry: dict[str, Any]) -> str | None:
    """O que, nesta entrada do trace, PROVA que um arquivo passou a existir.

    A distinção que faz a guarda funcionar: **comando não é evidência**. O turno
    medido na bb22cc33 rodou um `neo_bash` com ``exit_code=0`` cujo script Python
    engolia toda exceção (``except Exception: continue``) e baixava zero imagens —
    stdout vazio. Como o caminho ``/workspace/ciclo-prova`` estava escrito DENTRO do
    comando, cruzar contra o comando "confirmava" a mentira. Só conta o que o
    sistema devolveu: stdout, listing, path escrito, arquivo catalogado.

    ``None`` = esta entrada não prova nada (não é o mesmo que provar o contrário).
    """
    if not entry or not entry.get("ok"):
        return None
    tool = entry.get("tool")
    if tool == "neo_write_file":
        # A tool É a escrita: o path que ela reporta é resultado, não intenção.
        return str(entry.get("path") or "")
    if tool == "file_save":
        return str(entry.get("name") or "")
    if tool == "neo_bash":
        if entry.get("exit_code") not in (0, None):
            return None
        # Saída vazia não prova nada — foi exatamente esse o caso do bug.
        return str(entry.get("stdout_preview") or "") or None
    summary = str(entry.get("summary") or entry.get("listing") or "")
    if not summary or _LISTING_FAILED.search(summary):
        # "ls: cannot access '/workspace/x'" CONTÉM o caminho e provaria o oposto
        # do que diz, se entrasse como evidência.
        return None
    return summary


def _trace_backs_artifact_claim(
    tool_trace: list[dict[str, Any]] | None,
    targets: set[str],
    produced_files: list[dict[str, Any]] | None = None,
) -> bool:
    """Algum tool que REALMENTE deu certo produziu o que a resposta alega?

    Um arquivo já catalogado (``produced_files``, via ``file_save``) é prova
    direta: existe artefato de verdade, com id e tamanho, do outro lado.
    """
    for f in produced_files or []:
        name = str((f or {}).get("name") or "").lower()
        if not targets or (name and name in targets):
            return True
    for entry in tool_trace or []:
        evidence = _artifact_evidence_text(entry)
        if evidence is None:
            continue
        if not targets:
            # Sem caminho citado, a pergunta cai para "produziu algum arquivo?" —
            # e só as tools que de fato materializam algo contam.
            if entry.get("tool") in ("neo_write_file", "file_save", "neo_bash"):
                return True
            continue
        if any(t in evidence.lower() for t in targets):
            return True
    return False


def _claims_downloadable_delivery(text: str) -> bool:
    """A resposta AFIRMA que existe uma entrega baixável pronta?

    Combinado com produced_files vazio (checado pelo chamador), isso é sempre
    mentira: não há nada catalogado pro usuário clicar. Ver o bloco de regexes
    acima para o desenho em camadas e por que a palavra "download" sozinha não
    serve como sinal.
    """
    text = text or ""
    # Link clicável pra um caminho que só existe do meu lado: falso por
    # construção, não precisa de verbo nenhum em volta.
    if _FAKE_DELIVERY_LINK.search(text):
        return True
    for sent in _SENTENCE_SPLIT.finditer(_prose_only(text)):
        sentence = sent.group(0)
        match = _DOWNLOAD_CLAIM_STRONG.search(sentence)
        if not match:
            match = _DOWNLOAD_CLAIM_WEAK.search(sentence)
            if not match or not _DELIVERY_SURFACE.search(sentence):
                continue
        if sentence.rstrip().endswith("?"):      # oferta ("quer que eu gere?")
            continue
        if _FUTURE_INTENT.search(sentence[: match.start()]):
            continue
        if _claim_is_negated(sentence, match.start()):
            continue
        return True
    return False


# Os DeepSeek raciocinam ANTES de responder e cobram esse raciocínio do MESMO
# max_tokens: em ~1 de 4 chamadas o teto acaba durante o raciocínio e a API
# devolve content VAZIO + zero tool_calls (finish_reason=length). Vazio aqui não
# é "não tenho o que dizer" — é orçamento curto. Sem retry o turno morre no
# fallback "Desculpe, não consegui gerar uma resposta" (foi exatamente o que
# derrubou "como tu tá? ta calmo"). Uma retentativa com o dobro do teto resolve.
# Ver memória deepseek-empty-response-reasoning e ResearchAgent._complete.
_EMPTY_RETRY_MAX_TOKENS_CEILING = 16384

# O outro lado da mesma moeda do teto de tokens: em vez de content vazio, a API
# devolve uma tool_call com o JSON dos argumentos CORTADO no meio (típico ao
# escrever um HTML inteiro num `neo_write_file`). `get_tool_calls()` levanta
# JSONDecodeError e, sem guarda, a exceção sobe do loop e mata o turno inteiro —
# perdendo todo arquivo já escrito no sandbox. Quantos rounds extras a gente
# concede pedindo escrita em pedaços antes de desistir.
_MAX_TRUNCATED_TOOL_ROUNDS = 2


# Tools cujo resultado custa caro para reproduzir — é o gatilho do backstop da
# memória de trabalho (docs/working-memory-plan.md §7). Uma consulta de tempo ou
# de dispositivo não entra: refazer é barato e a nota só viraria ruído.
#: Tools que devolvem um job enfileirado em vez do resultado. O que elas
#: produzem chega minutos depois, por fora do turno — ver `_summarize_tool_call`.
_ASYNC_JOB_TOOLS = frozenset({"browser_task", "browser_local_task", "agent_job", "deep_research"})

_WM_HEAVY_TOOLS = frozenset({
    "neo_bash", "neo_write_file", "file_read", "file_stage", "file_save",
    "deep_research", "web_fetch", "run_command", "browser_task",
    "browser_local_task",
})


def is_pending_client_turn(resp: "AIServiceResponse") -> bool:
    """True quando o turno fica ESPERANDO o desktop devolver um resultado.

    Um ``tool_request``/``command_request`` sai NÃO-final de propósito: o turno
    não acabou, ele está estacionado no cliente — que executa (ou pede aprovação
    ao usuário) e devolve o resultado pela mesma stream. É o oposto de uma cadeia
    que morreu sem resposta, e confundir os dois derruba justamente os turnos que
    dependem de um clique humano.

    Mesmo predicado de ``NeoDesktopServicer._is_pending_flow`` — que já é a
    autoridade sobre o que entra em ``self._pending`` no gRPC. Está aqui, e lá é
    um alias, para que os dois não possam divergir.
    """
    if resp.is_final or not resp.metadata:
        return False
    return bool(resp.metadata.get("flow"))


def _autonote_entry(
    tool_trace: list[dict[str, Any]], produced_files: list[dict[str, Any]] | None
) -> str | None:
    """Texto do append automático do turno, ou ``None`` quando não há o que anotar.

    Separado do ``_bg_working_note`` porque é aqui que mora a decisão (e o bug
    possível): o piso só entra se o turno fez trabalho CARO de refazer e o
    modelo não anotou por conta própria.
    """
    # O modelo anotou sozinho: duplicar só viraria ruído no índice, que é o
    # recurso mais caro da feature.
    if any((e or {}).get("tool") == "wm_write" for e in tool_trace or []):
        return None
    heavy = [
        e for e in (tool_trace or [])
        if (e or {}).get("tool") in _WM_HEAVY_TOOLS and (e or {}).get("ok", True)
    ]
    if not heavy and not produced_files:
        return None
    from core.working_memory import render_auto_entry

    return render_auto_entry(heavy, produced_files) or None


#: Teto do `note_detail` — o campo que existe SÓ para o caderno. Duas ordens de
#: grandeza acima dos 200 caracteres do resumo porque os custos são opostos: o
#: resumo entra no prompt de todo turno, o `note_detail` só no corpo da nota, que
#: ninguém lê sem pedir (`wm_read`, e mesmo assim com orçamento — TAIL_MAX_CHARS).
_NOTE_DETAIL_CHARS = 4000


#: Chaves cujo valor é PAYLOAD relegível, não resultado. Copiá-las para o
#: caderno é errado por dois motivos, e o segundo é o grave:
#:
#: 1. custo — o conteúdo de um arquivo grande vira uma bolha de tokens;
#: 2. **validade** — a cópia ENVELHECE. O caderno existe para o modelo não
#:    refazer trabalho; um retrato velho do arquivo faz ele AFIRMAR o que já não
#:    é verdade, que é pior do que reler. O ponteiro (id, nome, caminho) não
#:    envelhece; o conteúdo, sim.
#:
#: A regra geral da casa passa a ser: **efêmero se guarda, relegível se aponta.**
#: `stdout` de comando some para sempre se não for guardado — vai inteiro.
#: Conteúdo de arquivo se lê de novo quando precisar — vai só o endereço.
_PAYLOAD_KEYS = ("content", "text", "body", "conteudo", "conteúdo", "raw", "html", "markdown")

#: Acima disto o valor deixa de ser "um campo" e passa a ser um despejo.
_PAYLOAD_INLINE_MAX = 400


def _strip_payload(value: Any) -> Any:
    """Troca payload relegível por uma marca do tamanho, recursivamente."""
    if isinstance(value, dict):
        podado: dict[str, Any] = {}
        for chave, valor in value.items():
            if (
                chave in _PAYLOAD_KEYS
                and isinstance(valor, str)
                and len(valor) > _PAYLOAD_INLINE_MAX
            ):
                podado[chave] = f"<{len(valor)} caracteres — não copiados para o caderno>"
            else:
                podado[chave] = _strip_payload(valor)
        return podado
    if isinstance(value, list):
        return [_strip_payload(v) for v in value[:20]]
    return value


def _note_detail(value: Any) -> str:
    """Conteúdo generoso de um resultado de tool, para o caderno.

    Generoso, mas não ingênuo: `_strip_payload` tira o que é relegível antes.
    """
    podado = _strip_payload(value)
    texto = podado if isinstance(podado, str) else str(podado)
    texto = texto.strip()
    if len(texto) <= _NOTE_DETAIL_CHARS:
        return texto
    return texto[:_NOTE_DETAIL_CHARS].rstrip() + "\n[…truncado]"


def _strip_note_detail(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cópia do trace sem o campo farto, para tudo que paga contexto."""
    return [{k: v for k, v in (e or {}).items() if k != "note_detail"} for e in trace]


def _summarize_tool_call(name: str, arguments: dict[str, Any], tr: Any) -> dict[str, Any]:
    """Resumo compacto de uma tool call pro ``tool_trace`` (nunca o payload cru).

    Guardado em ``conversation_message.extra_metadata`` e reinjetado no contexto
    do PRÓXIMO turno (session_manager.get_history) — precisa caber em poucas
    linhas, nunca o conteúdo de um arquivo inteiro ou um stdout gigante.

    **Exceção: a chave ``note_detail``.** Ela carrega o resultado FARTO e existe
    só para o caderno de trabalho, que é barato (corpo em Postgres, lido sob
    demanda). É removida antes de o trace ir para o `extra_metadata` e ignorada
    pelo registro de passo — ver `_strip_note_detail`. Sem essa separação o
    caderno herdava o teto de 200 caracteres de quem paga prompt todo turno, e
    era por isso que ele "não anotava nada de útil": a informação era cortada
    antes de chegar lá.
    """
    if tr.error:
        return {"tool": name, "ok": False, "error": str(tr.error)[:200]}
    result = tr.result
    if name == "neo_write_file":
        content = arguments.get("content") or ""
        return {
            "tool": name, "ok": True, "path": arguments.get("path"),
            "bytes": len(content.encode("utf-8")),
            "note_detail": _note_detail(content),
        }
    if name == "neo_bash":
        stdout = ""
        exit_code = None
        if isinstance(result, dict):
            stdout = str(result.get("stdout", ""))[:300]
            exit_code = result.get("exit_code")
        # `stdout_preview` continua com 300 chars (vai para o prompt); o stdout
        # INTEIRO — e o stderr, que o preview nunca teve — vai para a nota.
        bruto = result if isinstance(result, dict) else {}
        completo = "\n".join(
            parte for parte in (
                str(bruto.get("stdout") or "").strip(),
                (f"[stderr] {str(bruto.get('stderr') or '').strip()}"
                 if str(bruto.get("stderr") or "").strip() else ""),
            ) if parte
        )
        return {
            "tool": name, "ok": True, "command": arguments.get("command"),
            "exit_code": exit_code, "stdout_preview": stdout,
            "note_detail": _note_detail(completo),
        }
    # Tools que ABREM um job e voltam na hora: o `result` é o comprovante de
    # enfileiramento, não o trabalho. Guardar `str(result)[:200]` fazia o caderno
    # afirmar `status: 'queued'` sobre um job que podia já ter falhado — foi o
    # que aconteceu na sessão 4a75a655. Quem anota o RESULTADO é o
    # ProactiveDispatcher, quando o job termina (`_note_result`).
    # `job_id` presente é a condição, não só o nome da tool: estas mesmas tools
    # devolvem `{"ok": False, "error": ...}` quando recusam o pedido (flag off,
    # topic vazio), e aí NÃO há job nenhum — tratar isso como enfileiramento
    # jogaria fora justamente o motivo da recusa.
    if name in _ASYNC_JOB_TOOLS and isinstance(result, dict) and result.get("job_id"):
        return {
            "tool": name, "ok": True, "async_job": True,
            "job_id": result.get("job_id"), "job_title": result.get("title"),
        }
    # ── família de arquivo: ENDEREÇO, nunca o conteúdo ──────────────────────
    if name == "file_read" and isinstance(result, dict):
        conteudo = str(result.get("content") or "")
        inicio, fim = result.get("start_line"), result.get("end_line")
        return {
            "tool": name, "ok": True,
            "name": result.get("name"), "file_id": result.get("file_id"),
            "linhas": f"{inicio}-{fim}" if inicio and fim else None,
            "chars": len(conteudo),
            "tem_mais": bool(result.get("has_more")),
            # Sem `note_detail` de propósito: é exatamente aqui que o caderno
            # engordava com o arquivo inteiro. O que fica é como chegar nele.
        }
    if name == "file_stage" and isinstance(result, dict):
        # O bom exemplo que já existia: o caminho no sandbox é o dado, e o
        # conteúdo nunca passou pelo contexto. Vira cidadão de primeira classe.
        return {
            "tool": name, "ok": True,
            "path": result.get("path"), "target": result.get("target"),
            "file_id": result.get("file_id"),
        }
    if name == "file_save" and isinstance(result, dict):
        return {
            "tool": name, "ok": True,
            "name": result.get("name"), "file_id": result.get("file_id"),
            "size_bytes": result.get("size_bytes"),
        }
    return {
        "tool": name, "ok": True, "summary": str(result)[:200],
        "note_detail": _note_detail(result),
    }


_HANDOFF_INSTRUCTION_RULES = (
    "\nRegras:\n"
    "- O texto dentro do bloco deve ser uma ordem COMPLETA e sem ambiguidade "
    "(ex.: \"ligar o ar-condicionado do quarto\"), porque a skill NÃO vê esta conversa.\n"
    "- Só emita o bloco se a ação realmente deve acontecer agora — inclusive quando o "
    "usuário condiciona (\"se eu costumo, pode ligar\") e o contexto confirma que sim.\n"
    "- Se não há nada a executar, não escreva nenhum bloco.\n"
    "- Handoff vale SÓ para as skills acima. As skills da sua biblioteca (seção "
    "\"Suas skills\") NÃO têm handoff: quem age nelas é VOCÊ, chamando as tools que "
    "o `skill_read` libera — registrar um gasto, por exemplo, é uma chamada de tool "
    "sua, não um bloco. NUNCA diga que registrou/gravou/executou algo dessas skills "
    "sem ter chamado a tool nesta mesma resposta."
)

# Fallback estático (idêntico ao texto gerado pelos 3 manifests embutidos) para
# quando o registry estiver vazio — ex.: unit tests que não importam os pacotes.
_HANDOFF_SKILL_LINES_FALLBACK = (
    "- `home_assistant`: controlar a casa (luzes, tomadas, clima, persianas, cenas).\n"
    "- `whatsapp_messaging`: enviar mensagem de WhatsApp (passa por confirmação).\n"
    "- `music`: tocar música no Spotify/YouTube (passe a faixa/artista concretos).\n"
)


# Self-continuation. The failure this fixes, measured in session-fd467d08: the
# brain ended four turns in a row with "sigo tentando"/"vou resolver" and then did
# nothing — a turn ends when the tool loop returns text, so a promise about the
# future is just text. The block below gives it the only legal way to actually
# keep working: say what it is going to do AND emit the order that does it.
_SELF_HANDOFF_INSTRUCTION = (
    "\n## Continuar trabalhando no mesmo turno (obrigatório)\n"
    "Seu turno ACABA quando você devolve texto. Não existe \"depois eu faço\": se "
    "você escrever que vai fazer algo e não emitir o bloco abaixo, NADA acontece — "
    "o usuário fica esperando uma coisa que nunca vem, e é uma mentira.\n"
    "Então, sempre que a sua resposta contiver uma promessa de trabalho "
    "(\"vou procurar\", \"sigo tentando\", \"já te mando\", \"vou resolver\", \"deixa "
    "comigo\"), ela é OBRIGADA a terminar com:\n"
    "[[HANDOFF:continuar]]<a próxima ordem concreta, no imperativo>[[/HANDOFF]]\n\n"
    "Como usar bem:\n"
    "- A ordem tem que ser a PRÓXIMA AÇÃO REAL, específica e diferente do que já "
    "falhou. Ruim: \"continuar o site\". Bom: \"baixar uma imagem do Naruto com "
    "`curl -L -A 'Mozilla/5.0'` e salvar em /workspace/portifolio-naruto/images\".\n"
    "- Repetir a mesma ordem não roda de novo (o ciclo detecta e para). Se uma "
    "tentativa falhou, a próxima ordem tem que mudar a ABORDAGEM, não repetir.\n"
    "- Você continua vendo o que já fez: cada continuação recebe uma nota com os "
    "resultados reais dos passos anteriores. Leia antes de decidir o passo.\n"
    "- Quando a tarefa terminar de verdade, NÃO emita o bloco: responda o "
    "fechamento e entregue o resultado (arquivo via `file_save`, por exemplo). "
    "Essa última mensagem é a que encerra o turno.\n"
    "- Também não emita o bloco se você precisa de uma DECISÃO do usuário. "
    "Perguntar e parar é legítimo; prometer e parar não é.\n"
    "- O ciclo tem orçamento finito. Se estiver acabando, entregue o melhor "
    "resultado que já tem em vez de gastar o último passo tentando de novo."
)


# Promise markers — the deterministic pre-filter in front of the (paid) promise
# verifier. Deliberately generous: a false positive costs one cheap temperature-0
# call that answers promised=false, while a false negative brings back the exact
# bug this feature removes. Accent-insensitive, matched on the normalized reply.
_PROMISE_MARKERS = (
    "vou", "vamos", "sigo", "seguir tentando", "continuo", "ja te mando",
    "ja mando", "ja volto", "ja trago", "ja resolvo", "ja te trago",
    "deixa comigo", "pode deixar", "me da um minuto", "me de um minuto",
    "em seguida eu", "agora eu vou", "proximo passo eu", "estou indo",
    "tentar de novo", "tentar outra", "volto ja",
)

# Word boundaries are load-bearing, not cosmetic: as a plain substring "sigo"
# matches inside "não conSIGO fazer isso" — a refusal read as a promise, which
# would fire the cycle on exactly the turn that must stop.
_PROMISE_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(m) for m in _PROMISE_MARKERS)
)

# Announcements that only read as a promise when they CLOSE the reply. The list
# above covers the explicit future ("vou baixar") and misses everything else —
# both of these are verbatim endings measured on real runs of this feature, and
# both ended the turn on a promise with the backstop never firing:
#   "Script Jikan pronto. Rodando o download."                  (gerúndio)
#   "Agora busco imagens dos outros personagens e baixo tudo."   (presente, 1ª p.)
# Restricted to the last sentence because mid-reply the same words narrate what
# already happened ("rodei o script, gerando o zip") — paying a verifier call for
# that is waste, and the closing position is exactly where "what I do next" lives.
_CLOSING_PROMISE_MARKERS = (
    # gerúndio ("Rodando o download.")
    "rodando", "baixando", "gerando", "criando", "montando", "buscando",
    "procurando", "instalando", "processando", "compactando", "zipando",
    "escrevendo", "testando", "ajustando", "corrigindo", "preparando",
    "subindo", "finalizando", "tentando",
    # presente do indicativo, 1ª pessoa ("Agora busco as imagens.")
    "busco", "gero", "crio", "monto", "rodo", "testo", "ajusto", "corrijo",
    "procuro", "preparo", "compacto", "instalo", "finalizo",
)

# Deliberately absent: "baixo" (colide com "preço baixo"), "fecho" (o substantivo)
# e "subo"/"topo". São justamente as formas curtas que aparecem em frase comum
# sem nenhuma intenção de continuar — o custo do falso positivo aqui não é uma
# chamada barata, é o ciclo disparar num turno que devia terminar.
_CLOSING_PROMISE_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(m) for m in _CLOSING_PROMISE_MARKERS)
)

# Frase final = último trecho não vazio depois de . ! ? ou quebra de linha.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def _looks_like_promise(text: str | None) -> bool:
    """Cheap gate: does this reply read like it announces work still to come?"""
    if not text:
        return False
    decomposed = unicodedata.normalize("NFKD", text.lower())
    flat = "".join(c for c in decomposed if not unicodedata.combining(c))
    if _PROMISE_RE.search(flat):
        return True
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(flat) if p.strip()]
    return bool(parts and _CLOSING_PROMISE_RE.search(parts[-1]))


# Prefix that tells the desktop a thinking bubble is a self-continuation step
# actually EXECUTING, not the model musing. The Rust client strips it and flips the
# bubble's label from "Pensando" to "Executando" (ai_agent.rs::core_message_to_event).
# A marker rather than prose-sniffing on the client: the wording here changes freely.
CYCLE_PROGRESS_MARKER = "[[CYCLE]]"

# Closing lines for a self-continuation cycle that stopped on a guard instead of
# finishing. Each names the real reason: a generic "algo deu errado" here would be
# the same opacity the cycle exists to remove.
_CYCLE_CLOSING_MESSAGE: dict[str, str] = {
    "budget": (
        "Bati no limite de passos deste turno e parei por aqui — o que já ficou "
        "pronto está acima. Me fala se quer que eu continue de onde parei."
    ),
    "anti_loop": (
        "Percebi que ia repetir a mesma tentativa que já falhou, então parei em vez "
        "de rodar em círculo. Preciso de outra abordagem — ou de um empurrão seu."
    ),
    "max_depth": (
        "A cadeia de continuações ficou funda demais e eu parei por segurança. "
        "O que já ficou pronto está acima."
    ),
}


def _format_work_note(
    label: str,
    reply: str | None,
    tool_trace: list[dict[str, Any]] | None,
    files: list[dict[str, Any]] | None,
) -> str | None:
    """Compact, factual record of one step, for the next continuation to read.

    Deliberately built from the tool trace and not from ``reply``: the reply is
    where the model states intentions ("vou baixar a imagem"), and feeding those
    back would let a promise masquerade as a result on the next round. Tool lines
    keep the failure verbatim-ish, because "falhou" without the reason is what made
    the brain retry the same broken URL.
    """
    lines: list[str] = []
    for entry in tool_trace or []:
        if not entry:
            continue
        name = entry.get("tool", "?")
        if entry.get("ok"):
            detail = ", ".join(
                f"{k}={v}"
                for k, v in entry.items()
                if k not in ("tool", "ok", "note_detail") and v not in (None, "")
            )
            lines.append(f"  - `{name}` OK{f' ({detail})' if detail else ''}")
        else:
            lines.append(f"  - `{name}` FALHOU: {str(entry.get('error') or '')[:200]}")
    for f in files or []:
        lines.append(f"  - entregue ao usuário: {f.get('name')} (file_id={f.get('file_id')})")
    if not lines:
        # No tools and no files: the step only talked. Say so explicitly — an empty
        # note would read as "nothing is known", and the next step would restart.
        said = (reply or "").strip().replace("\n", " ")[:160]
        lines.append(f"  - nenhuma ação executada; só respondeu: {said!r}" if said else "  - nada foi executado")
    return f"{label}:\n" + "\n".join(lines)


def _build_handoff_instruction(*, allow_self: bool = False) -> str:
    """Instrução de handoff com a lista de skills vinda dos SkillManifest.

    Fonte única (F4 — docs/skills-library-plan.md): uma skill pipeline nova com
    ``handoff=True`` entra aqui automaticamente via ``handoff_line``.

    ``allow_self`` acrescenta o contrato de autocontinuação (``[[HANDOFF:continuar]]``).
    """
    lines = ""
    try:
        from core.skills.manifest import SkillManifest

        for name, manifest in sorted(SkillManifest.all().items()):
            if not getattr(manifest, "handoff", True):
                continue
            desc = (manifest.handoff_line or manifest.description).strip()
            lines += f"- `{name}`: {desc}\n"
    except Exception as e:  # noqa: BLE001 — a instrução nunca pode faltar
        logger.warning("[HANDOFF] manifest registry unavailable for instruction: %s", e)
    if not lines:
        lines = _HANDOFF_SKILL_LINES_FALLBACK
    base = _HANDOFF_INSTRUCTION_HEADER + lines + _HANDOFF_INSTRUCTION_RULES
    return base + _SELF_HANDOFF_INSTRUCTION if allow_self else base

# Workbench: the cognitive core has its OWN execution environment (a sandboxed
# Linux with internet + a persistent /workspace). Injected on cognitive turns when
# the workbench is enabled, so the brain knows it can write and run code whenever no
# specific tool covers the task — exactly like Claude Code. See
# docs/workbench-background-jobs-plan.md.
_WORKBENCH_INSTRUCTION = (
    "## Seu ambiente de execução (workbench)\n"
    "Você tem um ambiente Linux PRÓPRIO e isolado (sandbox), com internet e uma "
    "pasta /workspace que PERSISTE entre conversas. Use as tools `neo_bash`, "
    "`neo_write_file`, `neo_read_file` e `neo_list_files` quando NÃO existir uma "
    "tool específica para o que o usuário pediu e você conseguir resolver com "
    "código/scripts: cálculos, processar dados, baixar e analisar um arquivo, gerar "
    "algo, testar uma ideia. É a SUA máquina — não a do usuário (para a máquina "
    "dele existe `run_command`). Reaproveite scripts que você já criou (liste o "
    "/workspace antes). Skills com scripts prontos ficam montadas em /skills "
    "(read-only) — use os caminhos que o skill_read te der, não reescreva o "
    "script. Depois de rodar, explique o resultado ao usuário em "
    "linguagem natural; não cole saídas cruas enormes. Para trabalho demorado, "
    "prefira rodar em background (quando disponível) e avise que vai avisar ao "
    "terminar — não segure a conversa esperando.\n\n"
    "**O que já está instalado aí** (não precisa instalar nem testar antes de "
    "usar): `file` pra descobrir o tipo real; PDF com `pdftotext`/`pypdf` e "
    "`pdfplumber` (tabela); PDF ESCANEADO e imagem por OCR com `pytesseract` "
    "(`lang=\"por\"`) + `pdf2image`/`pymupdf`; Office com `python-docx`, "
    "`python-pptx`, `openpyxl`, `xlrd`, `odfpy`; HTML com `html2text`/"
    "`trafilatura`; dados com pandas, `duckdb` (SQL direto em CSV/Parquet) e "
    "`pyarrow`; mídia com `ffmpeg`/`ffprobe`; `7z`/`unzip`. Faltou alguma lib? "
    "`pip install --user <x>` funciona na hora — mas ela some no próximo restart "
    "do sandbox, então não conte com ela depois. NUNCA responda que não consegue "
    "ler um PDF/planilha/documento: você consegue, é só rodar.\n\n"
    "**NUNCA** tente `neo_bash cp`/`mv`/escrever um arquivo pra fora de "
    "/workspace (ex.: caminhos tipo `/Users/...`, `/home/...`, `~/Desktop`) — "
    "essa máquina não enxerga o disco do usuário de jeito nenhum, sempre falha "
    "(\"No such file or directory\"). Se o pedido for entregar um arquivo que "
    "você gerou no disco/área de trabalho/pasta do usuário: chame `file_save` "
    "nele (ganha um `file_id`) e diga que está pronto pra baixar — a entrega em "
    "si acontece pelo botão que a interface mostra sozinha, ou, se o usuário "
    "quiser especificamente colocado num lugar do disco dele, isso vira uma "
    "chamada `save_generated_file` do lado do cliente (fora do seu alcance "
    "direto: você só garante que o `file_id` existe e informa o nome do "
    "arquivo)."
)

# Injetado só quando neo_file_actuation_enabled: dá ao cérebro as duas mãos de
# arquivo (puxar do disco do usuário / entregar no disco dele) POR CONTA PRÓPRIA,
# no mesmo turno. Ver plano "arquivos sob iniciativa do cérebro".
_FILE_ACTUATION_INSTRUCTION = (
    "## Arquivos: você pode PEGAR e ENTREGAR sozinho\n"
    "Você NÃO depende mais do usuário anexar nem de um turno separado pra mexer em "
    "arquivos dele:\n"
    "- **SANDBOX PRIMEIRO.** Pra LER ou PROCESSAR um arquivo, suba ele pro seu "
    "/workspace e trabalhe lá — não tente processar na máquina dele. O ambiente do "
    "usuário quase sempre não tem a ferramenta (`pdftotext`, `mutool`, libs) e "
    "descobrir isso por `command not found` queima turno atrás de turno. O seu "
    "workspace tem. Só trate direto na máquina dele quando o arquivo for grande "
    "demais pra subir (ordem de gigabytes) — aí sim vale tentar local.\n"
    "- **Precisa de um arquivo que só existe na máquina do usuário** (ele não anexou)? "
    "Chame `request_user_file`. Use `mode=\"path\"` quando você SOUBER o caminho: ele "
    "citou nesta conversa, OU o arquivo foi você mesmo que pôs lá (aparece no seu "
    "registro de artefatos / na saída de um comando que você rodou). Nesse caso NÃO "
    "abra o seletor — pedir ao usuário pra escolher um arquivo que você já localizou "
    "é fazê-lo trabalhar à toa. `mode=\"pick\"` (abre o seletor) é pra quando você "
    "REALMENTE não sabe qual é o arquivo. O que continua proibido é INVENTAR um "
    "caminho a partir do nome. Passe `stage=true` se você "
    "vai RODAR/processar o arquivo (ele já vem materializado no /workspace pro neo_bash); "
    "`stage=false` se só vai LER (file_read). O arquivo chega ao seu catálogo e você "
    "continua a resposta lendo/processando — não peça de novo.\n"
    "- **Gerou um arquivo e quer ENTREGAR no disco dele** (Desktop/Downloads/pasta)? "
    "Depois do `file_save`, chame `save_generated_file` com o `file_id`, o nome e o "
    "`destination`. Se já existir um arquivo lá, ela devolve `status=\"conflict\"` com os "
    "dados do existente (NÃO sobrescreve): decida sozinho só quando for claramente seguro "
    "(o arquivo que você acabou de gerar) reenviando com `overwrite=true`, senão PERGUNTE "
    "citando nome/tamanho/data antes de sobrescrever.\n"
    "Ainda vale o botão de download automático do `file_save` pra entregas simples — use "
    "`save_generated_file` quando o pedido for pra colocar num LUGAR específico do disco."
)

# Music turns (decision.skill_hint == "music") get a STRICTER handoff contract so the
# play never silently no-ops: the reply must end with exactly one control token —
# [[HANDOFF:music]] to play, or [[NO_MUSIC]] to refuse/clarify/chat. If neither is
# emitted, a cheap verifier (_verify_music_decision) decides. See neo-music-feature.
_MUSIC_HANDOFF_INSTRUCTION = (
    "## Música — decisão OBRIGATÓRIA\n"
    "O usuário pediu algo sobre TOCAR música. Você é o núcleo cognitivo: leia o "
    "contexto/PKS (o GOSTO dele e qualquer REGRA de comportamento) e decida. AO FINAL "
    "da resposta, emita EXATAMENTE UM bloco de controle (o usuário NÃO vê):\n"
    "- Para TOCAR: ao final escreva o bloco com a FAIXA/ARTISTA real e buscável (+ o "
    "serviço se ele disse). Exemplo literal: [[HANDOFF:music]]AC/DC no YouTube[[/HANDOFF]]. "
    "NUNCA escreva a palavra \"ordem\" nem os sinais `< >` dentro do bloco — vá só a "
    "música de verdade.\n"
    "- Para CONTROLAR o que já está tocando, ponha a PALAVRA da ação no bloco: "
    "[[HANDOFF:music]]pausar[[/HANDOFF]], [[HANDOFF:music]]retomar[[/HANDOFF]], "
    "[[HANDOFF:music]]próxima[[/HANDOFF]], [[HANDOFF:music]]anterior[[/HANDOFF]], "
    "[[HANDOFF:music]]mudo[[/HANDOFF]], [[HANDOFF:music]]aumentar volume[[/HANDOFF]] ou "
    "[[HANDOFF:music]]abaixar volume[[/HANDOFF]]. Olhe as abas de mídia abertas no "
    "contexto pra saber o que está tocando.\n"
    "- Para NÃO tocar (recusa, brincadeira, dúvida, ou só conversar sobre música): "
    "[[NO_MUSIC]]\n\n"
    "Regras:\n"
    "- Se o PKS tem uma REGRA sobre esse pedido (ex.: recusar um gênero e zoar o usuário), "
    "SIGA a regra: escreva a resposta no personagem e use [[NO_MUSIC]] — NÃO toque.\n"
    "- TOCAR **ou CONTROLAR** (pausar/retomar/próxima/anterior/mudo/volume) SEMPRE exige "
    "o bloco [[HANDOFF:music]] — é ELE que executa de verdade. NUNCA diga que pausou, "
    "tocou ou pulou SEM emitir o bloco; senão NADA acontece e você fica mentindo. "
    "Resolva pedidos vagos ('aquela que eu curto') usando o gosto do PKS e ponha algo "
    "concreto/buscável (ou a palavra exata da ação) dentro do bloco.\n"
    "- NÃO use o navegador para tocar (`browser_local_task`/`browser_task`) nem "
    "invente comando de terminal: o bloco é o caminho — ele manda o comando direto "
    "para a extensão do navegador, que reusa a aba aberta. Dirigir o navegador "
    "passo a passo leva ~30s, abre aba nova e já falhou em produção; o bloco é "
    "instantâneo.\n"
    "- Se NESTE MESMO turno você já mandou o navegador DO USUÁRIO abrir/tocar "
    "(`browser_local_task`), isso JÁ É tocar: escreva a resposta e feche com "
    "[[NO_MUSIC]]. Emitir o bloco também abriria uma SEGUNDA aba, com outra música "
    "tocando junto com a primeira.\n"
    "- Responda curto e natural; a mecânica dos blocos fica invisível ao usuário."
)

# Client tools the music skill dispatches to the desktop (Etapa 2). process_tool_result
# confirms these truthfully and skips the generic recovery flow.
_MUSIC_ACTUATION_TOOLS = frozenset({"open_url", "launch_uri", "media_command"})

def _session_meta(session: Any) -> dict[str, Any]:
    """`extra_metadata` da sessão como DICT, sempre.

    A coluna é JSONB livre e já apareceu em produção como lista
    (``[null, {...}]``, ver AsyncPostgresClient.merge_session_metadata). Um
    turno inteiro morria com `'list' object has no attribute 'get'` — e a
    conversa ficava inutilizável, porque toda mensagem seguinte relia a mesma
    linha. Aqui o dado torto vira `{}`: perde-se um detalhe, não a conversa.
    """
    meta = getattr(session, "extra_metadata", None)
    return meta if isinstance(meta, dict) else {}


def _video_id_of(url: str) -> str:
    """videoId de uma URL de atuação ("" quando não é YouTube)."""
    if not url:
        return ""
    try:
        from core.skills.music.service import parse_direct_link

        parsed = parse_direct_link(url)
    except Exception:  # noqa: BLE001
        return ""
    if parsed is not None and parsed.service == "youtube" and parsed.kind == "video":
        return parsed.id
    return ""


def _media_context_of(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """`media_context` do turno ({} em qualquer problema).

    Import local de propósito: `core.skills.music.service` puxa o resto da skill
    de música, e este módulo é importado no boot.
    """
    try:
        from core.skills.music.service import parse_media_context

        return parse_media_context(metadata) or {}
    except Exception:  # noqa: BLE001
        return {}


# A ÚNICA tool pela qual o núcleo cognitivo mexe no navegador que o usuário está
# olhando. `browser_task` não entra: roda escondida no servidor e não abre aba
# nenhuma na tela dele. Existe para a guarda de dupla atuação da música (sessão
# a691f1e0): "abre uma aba no meu navegador com uma playlist de rock e coloca pra
# tocar" casa AO MESMO TEMPO com a regra do navegador local (chame
# `browser_local_task` no MESMO turno) e com o contrato de música (TOCAR sempre
# exige [[HANDOFF:music]]). O modelo obedeceu aos dois e o turno atuou duas vezes:
# o agente abriu uma aba nova com uma playlist e a skill de música sequestrou
# OUTRA aba do YouTube com um vídeo diferente — duas playlists tocando juntas. O
# contrato de música só sabia checar se FALTAVA handoff, nunca se o turno já tinha
# atuado por outro canal.
_USER_BROWSER_TOOL = "browser_local_task"


def _drop_music_handoff_after_browser(
    handoffs: list[Handoff], browser_actuated: bool
) -> list[Handoff]:
    """Descarta o handoff de música quando o turno JÁ atuou no navegador do usuário.

    O navegador vence: é a atuação que o usuário pediu para VER acontecer, já está
    rodando e não dá para cancelar do meio — enquanto o handoff ainda é só uma
    intenção. ``browser_actuated`` tem que ser o fato consumado (tool efetiva,
    pós-portão, ordem aceita), nunca "o modelo escolheu essa tool": suprimir em
    cima de uma escolha que o portão rebaixou para o servidor, ou de uma chamada
    que falhou, troca duas abas tocando por NENHUMA.
    """
    if not browser_actuated:
        return handoffs
    return [
        h
        for h in handoffs
        if not (h.target_skill == "music" or h.target_intent == IntentType.MUSIC)
    ]


# Tools "client-actuating" que o NÚCLEO COGNITIVO aciona no meio do turno para
# OBTER/ENTREGAR arquivos (plano "arquivos sob iniciativa do cérebro"): elas não
# executam no processo do core (o disco/picker do usuário só existe no cliente).
# Quando o modelo chama uma delas no loop cognitivo, o executor levanta
# ``ClientActuationRequested``; ``_handle_direct_response`` quebra o turno e emite
# um ``tool_request`` — o desktop executa e o resultado volta em
# ``process_tool_result`` (mesmo round-trip de save_generated_file/execute_command).
_CLIENT_ACTUATION_TOOLS = frozenset(
    {"request_user_file", "save_generated_file", "write_file"}
)

# Shell na máquina do usuário pela iniciativa do CÉREBRO (neo_brain_shell_enabled).
# Fica num set próprio, e não dentro de _CLIENT_ACTUATION_TOOLS, porque o nome
# `run_command` também é o do caminho CLIENT_TOOL do classificador: misturar os dois
# faria o resultado de um `tool_request(run_command)` comum cair no ramo de retomada
# cognitiva. O ramo do cérebro é reconhecido pelo estado em _pending_actuations,
# nunca pelo nome sozinho.
_SHELL_ACTUATION_TOOLS = frozenset({"run_command"})

# Tudo que quebra o loop cognitivo para atuar no cliente.
_ALL_ACTUATION_TOOLS = _CLIENT_ACTUATION_TOOLS | _SHELL_ACTUATION_TOOLS

# Teto de round-trips de atuação encadeados num mesmo pedido (pull→responde→push,
# ou conflito→sobrescreve): evita um laço se o modelo insistir em atuar sem parar.
_MAX_ACTUATION_DEPTH = 3


class ClientActuationRequested(Exception):
    """Sinaliza que o cérebro chamou uma tool que precisa rodar NO CLIENTE.

    Carrega o suficiente para montar o ``tool_request`` e, depois, retomar o
    turno cognitivo quando o ``tool_result`` voltar. Levantada de dentro do loop
    cognitivo (antes de qualquer execução in-process) e capturada em
    ``_handle_direct_response``.
    """

    def __init__(self, tool_name: str, arguments: dict[str, Any], tool_call_id: str | None) -> None:
        super().__init__(f"client actuation: {tool_name}")
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.tool_call_id = tool_call_id or str(uuid4())


def _extract_clarification_question(text: str) -> str | None:
    """Return the clarifying question if ``text`` carries the sentinel, else None."""
    if not text or NEED_CLARIFICATION_SENTINEL not in text:
        return None
    after = text.split(NEED_CLARIFICATION_SENTINEL, 1)[1].strip()
    # Drop a leading ":" or "-" the model might add after the marker.
    after = after.lstrip(":- ").strip()
    return after or None


def _mission_chat_status(status: str | None) -> str:
    """Map backend mission states to the compact UI status used by the chat."""
    if status == "awaiting_confirmation":
        return "pending"
    if status in {"awaiting_reply", "processing", "completed"}:
        return "approved"
    if status == "cancelled":
        return "cancelled"
    if status == "failed":
        return "rejected"
    return "pending"


def _looks_like_whatsapp_mission_followup(text: str) -> bool:
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", (text or "").strip().lower())
        if unicodedata.category(c) != "Mn"
    )
    normalized = " ".join(normalized.split())
    if not normalized:
        return False
    markers = (
        "outra resposta",
        "nova resposta",
        "gere outra",
        "gere uma nova",
        "gere um nova",
        "gera outra",
        "gera uma nova",
        "gera um nova",
        "gerar outra",
        "gerar uma nova",
        "gerar um nova",
        "refaz",
        "refaca",
        "reescreve",
        "mais natural",
        "mais curto",
        "nova sugestão",
        "nova sugestao",
        "outro rascunho",
        "coloca",
        "coloque",
        "adiciona",
        "adicione",
        "bota",
        "inclui",
        "inclua",
        "acrescenta",
        "acrescente",
        "tira",
        "retira",
        "remove",
        "remova",
        "troca",
        "troque",
        "muda",
        "mude",
        "no final",
        "ao final",
        "mais gratidao",
        "mais agradecido",
        "mais agradecida",
        "mais carinho",
        "mais carinhoso",
        "mais carinhosa",
        "aborta",
        "abortar",
        "aborte",
        "cancela",
        "cancelar",
        "cancele",
        "deixa pra la",
        "deixa para la",
        "deixa quieto",
        "deixa isso quieto",
        "deixa assim",
        "nao faz nada",
        "nao precisa responder",
        "sem resposta",
        "ignora",
        "ignore",
    )
    if normalized.startswith(("se ", "caso ", "quando ", "e se ")):
        return True
    return any(marker in normalized for marker in markers)


def _build_mission_card_metadata(
    mission: Any,
    *,
    intro: str,
    result: Any | None = None,
    marker_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured chat payload used to restore mission cards."""
    marker_payload = marker_payload or {}
    mission_meta = mission.extra_metadata or {}
    next_action_params = mission.next_action_params or {}

    draft_text = (
        mission_meta.get("draft_text")
        or marker_payload.get("draftText")
        or next_action_params.get("text")
        or getattr(result, "draft_text", None)
        or ""
    )
    draft_messages = (
        mission_meta.get("draft_messages")
        or marker_payload.get("draftMessages")
        or next_action_params.get("messages")
        or ([draft_text] if draft_text else [])
    )

    contact_phone = (
        mission_meta.get("contact_phone_e164")
        or marker_payload.get("contactPhone")
        or ""
    )

    return {
        "message_type": "mission_card",
        "intro": intro,
        "mission": {
            "missionId": str(mission.id),
            "contactLabel": (
                mission_meta.get("contact_label")
                or marker_payload.get("contactLabel")
                or getattr(result, "mission_contact_label", None)
                or mission.title
            ),
            "contactJid": (
                marker_payload.get("contactJid")
                or getattr(result, "mission_contact_jid", None)
                or mission.contact_jid
            ),
            "contactPhone": contact_phone,
            "draftText": draft_text,
            "draftMessages": draft_messages,
            "status": _mission_chat_status(mission.status),
            "backendStatus": mission.status,
            "nextActionType": mission.next_action_type,
            "nextActionParams": next_action_params,
            "resultSummary": mission.result_summary,
        },
    }


class TurnAffect(NamedTuple):
    """Leitura emocional do turno, derivada uma vez antes do roteador.

    ``decision`` é o veredito do Modo Birra; ``hint`` é o bloco que vai no prompt
    do classificador (None quando o Neo está no repouso — aí o prompt é o de
    sempre); ``force_brain`` é o desvio determinístico para o núcleo cognitivo.
    """

    decision: Any  # core.agent.birra.policy.BirraDecision
    hint: str | None
    force_brain: bool


class AIServiceResponse:
    """Response from AIService — wraps the different types of responses."""

    def __init__(
        self,
        intent: IntentType,
        session_id: UUID,
        message: str | None = None,
        tool_request: dict[str, Any] | None = None,
        command_request: dict[str, Any] | None = None,
        is_final: bool = True,
        iterations: int = 1,
        metadata: dict[str, Any] | None = None,
        voice: dict[str, Any] | None = None,
        segments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.intent = intent
        self.session_id = session_id
        self.message = message
        self.tool_request = tool_request
        self.command_request = command_request
        self.is_final = is_final
        self.iterations = iterations
        self.metadata = metadata or {}
        # Pattern A: active TTS voice carried with the FINAL reply so the renderer
        # (desktop) speaks with the voice persisted in the DB. Filled at the
        # transport boundary (gRPC/HTTP) via modules.voice.get_active_voice.
        self.voice = voice
        # Voice imitation: ordered speech segments, each ``{text, voice}`` where
        # ``voice`` is a resolved voice-settings dict or None (→ default voice).
        # Present only on multi-voice replies; None keeps the single-voice path.
        self.segments = segments


class _CognitiveAgentExecutor:
    """Routes a cognitive tool call to the right per-request executor.

    Background-job tools (``neo_bash_background``/``job_*``) go to a session-scoped
    :class:`JobToolExecutor`; everything else (read tools + workbench ``neo_*``) goes
    to the :class:`CognitiveReadExecutor`, which itself delegates unknown names to
    the global tool executor. See ``docs/workbench-background-jobs-plan.md``.
    """

    def __init__(self, read_executor: Any, job_executor: Any, job_tool_names: Any) -> None:
        self._read = read_executor
        self._jobs = job_executor
        self._job_names = job_tool_names

    async def execute_tool_call(self, tool_call: Any) -> Any:
        if tool_call.name in self._job_names:
            return await self._jobs.execute_tool_call(tool_call)
        return await self._read.execute_tool_call(tool_call)


class AIService:
    """Unified AI orchestrator — routes, processes, and responds."""

    def __init__(
        self,
        llm_router: LLMRouter,
        intent_router: IntentRouter,
        session_manager: PersistentSessionManager,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        agent_executor: AgentExecutor,
        db: AsyncPostgresClient,
        preference_extractor: PreferenceExtractor | None = None,
        pks: PKSService | None = None,
        context_selector: PromptContextSelector | None = None,
        job_manager: Any = None,
        skill_library: Any = None,
        dataset_registry: Any = None,
        event_router: Any = None,
        file_store: Any = None,
        credential_store: Any = None,
    ) -> None:
        self.llm = llm_router
        self.router = intent_router
        self.sessions = session_manager
        self.tools = tool_registry
        self.tool_executor = tool_executor
        self.agent = agent_executor
        self.db = db
        self.preferences = preference_extractor
        self.pks = pks
        self.settings = get_settings()
        self.context_selector = context_selector
        # JobManager — background jobs (workbench). None disables neo_bash_background/job_*.
        self.job_manager = job_manager
        # SkillLibrary — skills declarativas SKILL.md (docs/skills-library-plan.md).
        # None (flag off) → sem catálogo no prompt e sem tool gating.
        self.skill_library = skill_library
        # Registry de cadastros (docs/datasets-crud-plan.md). None quando a flag
        # neo_datasets_enabled está off — o CognitiveReadExecutor cai no
        # caminho sem cadastros e nada muda.
        self.dataset_registry = dataset_registry
        # Barramento de eventos (docs/playlists-skill-plan.md §7). None com a
        # flag off — sem ele as tools event_* nem chegam ao prompt.
        self.event_router = event_router
        # FileStore — catálogo de arquivos (docs/file-store-plan.md). None (flag
        # off) → metadata["attachments"] é ignorado e nenhum manifesto é injetado.
        self.file_store = file_store
        # CredentialStore — cofre Fernet. Aqui serve a UMA coisa: saber que o Neo
        # tem um email próprio e qual é o endereço (ver _neo_identity_block). O
        # segredo continua sem passar por perto.
        self.credential_store = credential_store
        self._identity_cache: tuple[float, str] = (0.0, "")
        # Anexos por sessão (RAM): o arquivo anexado uma vez segue valendo nos
        # turnos seguintes da conversa — ver _remember_session_attachments.
        self._session_attachments: dict[str, list[str]] = {}
        self._pending_traces: dict[str, MessageTrace] = {}
        # Estado para retomar um turno cognitivo depois de uma tool de arquivo
        # client-actuating (request_user_file/save_generated_file). O gRPC só
        # encaminha campos fixos (user_message/command/…) entre tool_request e
        # tool_result, então guardamos aqui o resto (metadata original, user_id,
        # stage, args) por sessão. Ver _emit_client_actuation / process_tool_result.
        self._pending_actuations: dict[str, dict[str, Any]] = {}
        # Sessões cujo último turno veio do modo Live — usado para estilizar a
        # resposta de um client tool (process_tool_result), onde o metadata["mode"]
        # original se perde no round-trip. Atualizado em process_user_input.
        self._live_sessions: set[str] = set()
        # Sessões cujo cliente RENDERIZA `[[IMAGE:…]]` (hoje o neo-desktop-tauri).
        # Mesmo motivo do set acima: a resposta de um client tool
        # (process_tool_result → _format_tool_result) perde o metadata do turno.
        self._image_capable_sessions: set[str] = set()
        # Sessões cujo último turno veio de VOZ (metadata["input_source"]=="voice").
        # Independente do set Live acima: `mode` descreve a SAÍDA (falar sem
        # markdown), `input_source` descreve a ENTRADA (texto transcrito por ASR,
        # que erra nome/grafia). Ver docs/voice-input-normalization-plan.md.
        self._voice_input_sessions: set[str] = set()
        # Registro da situação (docs/context-register-plan.md): gravidade sticky
        # por sessão (RAM, decai sozinha) + registro do último turno — este último
        # cobre as continuações (process_tool_result) que perdem o metadata.
        self._gravity_tracker = GravityTracker()
        self._session_registers: dict[str, Register] = {}
        # Modo Birra (docs/modo-birra-plan.md): decisão do turno por sessão — o
        # handler de casa precisa dela, e ela é derivada antes do roteador. O set
        # de pendências alimenta a âncora da ordem enfática ("para com isso").
        self._session_birra: dict[str, TurnAffect] = {}
        self._birra_pending: dict[str, str] = {}
        self._birra_sinks: dict[str, Any] = {}
        self._birra_store_cache: Any | None = None
        self._skill_executor: "SkillExecutor | None" = None
        # Lazily-built HA adapter for the cognitive-core live snapshot injection.
        self._ha_adapter: "HomeAssistantAdapter | None" = None
        # Lazily-built affect updater (per-user emotional vector). None until first use.
        self._affect_updater: Any | None = None
        # Strong refs to fire-and-forget post-turn tasks (prefs/PKS). Bare
        # create_task results can be garbage-collected mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()
        self.planner = PlanManager(llm_router)

    async def _neo_identity_block(self) -> str:
        """Bloco "seus dados" do prompt — hoje, o email próprio do Neo.

        Cache de 5 min: isto roda em TODO turno e o dado muda quando alguém mexe
        no painel, não a cada mensagem. Falha em silêncio de propósito — cofre
        fora do ar não pode custar o turno.
        """
        if self.credential_store is None:
            return ""
        now = time.monotonic()
        cached_at, cached = self._identity_cache
        if cached_at and now - cached_at < 300:
            return cached
        block = ""
        try:
            boxes = await self.credential_store.list_by_kind("email")
            linhas = []
            for box in boxes:
                address = (box.fields or {}).get("address")
                if not address:
                    continue
                desc = f" — {box.description}" if box.description else ""
                linhas.append(f"- {address} (rótulo no cofre: '{box.label}'){desc}")
            if linhas:
                block = (
                    "## Seu email\n"
                    "Você TEM email próprio. Quando algo pedir 'um email' (cadastro, "
                    "formulário, contato), use o SEU — nunca peça o do usuário nem invente "
                    "um endereço:\n"
                    + "\n".join(linhas)
                    + "\nA senha está no cofre e você não a vê: quem preenche é a tool "
                    "(browser_task com `credentials`). Para ler a caixa (código/link de "
                    "confirmação que você mesmo provocou), use `email_inbox`."
                    + (
                        "\nVocê também ENVIA: `email_send` manda um email a partir dessa "
                        "caixa, com você como remetente. Vale para 'manda um email pra "
                        "fulano'. O email sai de verdade e não dá para cancelar — se não "
                        "souber o endereço do destinatário, pergunte em vez de adivinhar."
                        if getattr(self.settings, "neo_email_send_enabled", True)
                        else ""
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[identity] cofre indisponível: %s", exc)
            block = cached
        self._identity_cache = (now, block)
        return block

    @staticmethod
    def _client_renders_inline_images(metadata: dict[str, Any] | None) -> bool:
        """O cliente deste turno troca `[[IMAGE:…]]` pela imagem renderizada?

        Hoje só o neo-desktop-tauri (que lê o arquivo do disco do usuário). Outros
        clientes podem optar mandando ``supports_inline_images`` no metadata — mas
        quem não renderiza NÃO recebe a instrução, senão o usuário lê o marcador cru.
        """
        if not metadata:
            return False
        opt_in = str(metadata.get("supports_inline_images", "")).strip().lower()
        if opt_in in {"1", "true", "yes"}:
            return True
        return str(metadata.get("client", "")).strip() == "neo-desktop-tauri"

    @staticmethod
    def _is_voice_input(metadata: dict[str, Any] | None) -> bool:
        """O texto deste turno veio de um ASR (microfone), não do teclado?

        Sinal explícito do cliente (``input_source="voice"``), NUNCA inferido de
        ``mode="live"``: o Live descreve como a resposta sai (falada), não como a
        pergunta entrou — existe voz que responde em texto e texto que responde
        falando. Inferir daria tolerância a erro de ASR para o que o usuário
        digitou de propósito.
        """
        if not metadata:
            return False
        return str(metadata.get("input_source", "")).strip().lower() == "voice"

    @staticmethod
    def _parse_attachment_ids(metadata: dict[str, Any] | None) -> list[str]:
        """Ids dos anexos DESTE turno. Nunca levanta — anexo ruim não derruba o turno."""
        if not metadata:
            return []
        raw = metadata.get("attachments") or []
        if isinstance(raw, str):
            # Clientes gRPC mandam metadata como map<string,string> → a lista de
            # ids chega serializada. Mesmo padrão do media_context da música
            # (core/skills/music/service.py:112).
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "[files] metadata.attachments não é JSON válido: %.80s", raw
                )
                return []
        if not isinstance(raw, list):
            return []
        ids: list[str] = []
        for item in raw[:_MAX_SESSION_ATTACHMENTS]:
            # Aceita o id cru ou um objeto {"file_id": ...} (clientes variam).
            file_id = item.get("file_id") if isinstance(item, dict) else item
            if file_id and str(file_id) not in ids:
                ids.append(str(file_id))
        return ids

    def _remember_session_attachments(self, session_id: str, ids: list[str]) -> None:
        """Anexo vale para a CONVERSA, não só para a mensagem que o trouxe.

        Sem isto o follow-up ("de qual instituição de ensino?") chega sem
        manifesto: o cognitivo perde a ficha e — pior — o classificador, que
        também recebe o system_context, inventa um caminho no disco do usuário e
        roteia para o read_file client-side. Mesma família dos bugs de rota já
        conhecidos (computação→run_command, entity_id inventado na HA).
        """
        known = self._session_attachments.setdefault(session_id, [])
        for file_id in ids:
            if file_id in known:
                known.remove(file_id)
            known.append(file_id)
        # Mantém os mais recentes (a lista é o teto do manifesto do turno).
        del known[:-_MAX_SESSION_ATTACHMENTS]

    async def _manifest_lines(self, ids: list[str]) -> list[str]:
        lines: list[str] = []
        for file_id in ids:
            try:
                entry = await self.file_store.get(file_id)
            except Exception as exc:  # noqa: BLE001 — anexo ruim não derruba o turno
                logger.warning("[files] anexo %s não resolveu: %s", file_id, exc)
                continue
            if entry is None or entry.status != "available":
                continue
            lines.append(self.file_store.manifest_line(entry))
        return lines

    async def _recent_artifacts(
        self, user_id: UUID | None
    ) -> tuple[str | None, dict[str, str]]:
        """Bloco de artefatos para o ``system_context`` + mapa nome→caminho real.

        Devolve as DUAS superfícies de uma leitura só, porque elas resolvem
        problemas diferentes do mesmo turno: o bloco entra no prompt do
        classificador (que é quem inventa caminho, e não enxerga o índice do
        caderno — esse só chega ao cognitivo), e o mapa vai para a resolução
        determinística do roteador, que reescreve o caminho em vez de confiar
        que o modelo leu o bloco.

        Silencioso quando a memória de trabalho está desligada, e nunca levanta:
        um registro indisponível não pode custar o turno.
        """
        try:
            from core.working_memory import artifacts as artifacts_mod

            store = artifacts_mod.store_for(self.db)
            if store is None:
                return None, {}
            items = await artifacts_mod.recent(store, user_id=user_id)
            if not items:
                return None, {}
            return (
                artifacts_mod.render_context_block(items) or None,
                artifacts_mod.path_map(items),
            )
        except Exception as exc:  # noqa: BLE001 — contexto extra é best-effort
            logger.warning("[Artefatos] contexto do turno indisponível: %s", exc)
            return None, {}

    async def _build_attachments_manifest(
        self,
        metadata: dict[str, Any] | None,
        *,
        session_id: Any = None,
    ) -> str | None:
        """Manifesto compacto dos anexos (docs/file-store-plan.md §5).

        É ISTO que entra no prompt — nunca o conteúdo do arquivo. Uma linha por
        anexo (nome, tipo, tamanho, páginas/linhas, id) + a instrução de COMO ler.
        Sem isso as tools file_* existem mas o modelo não as usa (lição da skill
        financas: tool disponível ≠ tool usada).

        Cobre os anexos deste turno E os que já vieram antes na sessão
        (``session_id``) — uma conversa sobre um arquivo tem mais de um turno.
        """
        if self.file_store is None:
            return None
        fresh_ids = self._parse_attachment_ids(metadata)
        sid = str(session_id) if session_id else None
        if sid and fresh_ids:
            self._remember_session_attachments(sid, fresh_ids)
        carry_ids = [
            file_id
            for file_id in (self._session_attachments.get(sid, []) if sid else [])
            if file_id not in fresh_ids
        ]
        if not fresh_ids and not carry_ids:
            return None

        fresh_lines = await self._manifest_lines(fresh_ids)
        carry_lines = await self._manifest_lines(carry_ids)
        if not fresh_lines and not carry_lines:
            return None

        blocks: list[str] = []
        if fresh_lines:
            blocks.append("## ARQUIVOS ANEXADOS NESTA MENSAGEM\n" + "\n".join(fresh_lines))
        if carry_lines:
            blocks.append(
                ("## OUTROS ARQUIVOS DESTA CONVERSA (anexados antes)\n" if fresh_lines
                 else "## ARQUIVOS DESTA CONVERSA (anexados antes, seguem valendo)\n")
                + "\n".join(carry_lines)
            )
        logger.info(
            "[files] manifesto injetado: %d anexo(s) neste turno, %d da conversa",
            len(fresh_lines), len(carry_lines),
        )
        blocks.append(
            "Estes arquivos estão no SEU catálogo (não no disco do usuário): "
            "para qualquer pergunta sobre eles use as tools file_* — NUNCA "
            "read_file/run_command, e nunca invente um caminho no computador dele.\n"
            "O conteúdo NÃO está aqui — você tem só a ficha. Para dizer QUALQUER "
            "coisa sobre o conteúdo, LEIA antes: file_read (em FATIAS, com "
            "start_line/max_lines) quando for texto; file_stage (materializa no seu "
            "sandbox) + neo_bash quando for binário/planilha ou trabalho pesado. "
            "O preview do file_info é só uma amostra dos primeiros caracteres — "
            "NUNCA descreva o arquivo (nem responda 'parece ser') com base só nele. "
            "Leia quantas fatias precisar para responder de verdade — só não peça "
            "o arquivo inteiro de uma vez."
        )
        return "\n\n".join(blocks)

    def _spawn_bg(self, coro, *, name: str) -> None:
        """Schedule a tracked fire-and-forget task (post-response work only)."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _derive_turn_register(
        self,
        *,
        decision: RoutingDecision,
        user_message: str,
        session_id: UUID,
        live: bool,
        slog: "SessionFileLogger | None" = None,
    ) -> Register | None:
        """Derive this turn's situational register. Fail-open: never raises.

        Returns the register to INJECT, or None when the feature is off or in
        shadow mode (shadow derives + logs only — calibration before go-live).
        Also maintains the sticky session gravity (resolution cue clears it;
        any grave turn refreshes it) and the per-session register used by
        continuations that lose the original metadata (process_tool_result).
        """
        enabled = self.settings.neo_register_enabled is True
        shadow = getattr(self.settings, "neo_register_shadow", False) is True
        sid = str(session_id)
        if not enabled and not shadow:
            self._session_registers.pop(sid, None)
            return None
        try:
            # Skill-declared register (pipeline manifests; SKILL.md skills
            # apply mid-turn via skill_read's register_note instead).
            skill_register: str | None = None
            skill_name = decision.skill_name or decision.skill_hint
            if skill_name:
                from core.skills.manifest import SkillManifest

                manifest = SkillManifest.all().get(skill_name)
                if manifest is not None:
                    skill_register = getattr(manifest, "tone_register", "") or None

            # Sticky gravity: an all-clear only counts while the session is
            # grave (so everyday "voltou" can't clear anything), then this
            # turn's gravity (classifier + deterministic floor) merges in.
            if self._gravity_tracker.current(sid) != "normal" and detect_resolution_cue(user_message):
                self._gravity_tracker.resolve(sid)
            turn_gravity = max_gravity(decision.gravity, detect_gravity_floor(user_message))
            session_gravity = self._gravity_tracker.bump(sid, turn_gravity)

            register, reason = derive_register(
                decision,
                user_message=user_message,
                live=live,
                skill_register=skill_register,
                session_gravity=session_gravity,
            )
            logger.info(
                "[Register] derived=%s reason=%s gravity=%s mode=%s",
                register.value, reason, session_gravity,
                "inject" if enabled else "shadow",
            )
            if slog:
                slog.log_event(
                    "Registro da Situação",
                    f"**Registro:** {register.value} "
                    f"({'injetado' if enabled else 'shadow — não injetado'})\n"
                    f"**Motivo:** {reason} | **Gravidade da sessão:** {session_gravity}",
                )
            if enabled:
                self._session_registers[sid] = register
                return register
            return None
        except Exception:  # noqa: BLE001 — o registro nunca derruba o turno
            logger.warning("[Register] derivation failed; turn continues without it", exc_info=True)
            return None

    # ---------------------------------------------------------------
    # Afeto no roteamento + Modo Birra (docs/modo-birra-plan.md §3.1/§3.7)
    # ---------------------------------------------------------------

    async def _derive_turn_birra(
        self,
        *,
        user_message: str,
        user_id: UUID | None,
        session_id: UUID,
        slog: "SessionFileLogger | None" = None,
    ) -> "TurnAffect":
        """Estado emocional do turno, derivado ANTES do roteador. Nunca levanta.

        Vem antes do ``classify`` de propósito: é o estado emocional que decide
        QUEM atende o turno. Avaliar isso dentro do handler de casa seria tarde —
        o turno já teria sido entregue a uma skill que não tem persona, não tem
        afeto e não sabe recusar.
        """
        from core.agent.birra.policy import BirraDecision

        empty = TurnAffect(decision=BirraDecision(), hint=None, force_brain=False)
        if user_id is None or not self.settings.neo_affect_enabled:
            return empty
        try:
            from core.agent.affect import AffectTuning, Level, apply_decay
            from core.agent.birra.policy import evaluate as evaluate_birra
            from core.agent.register import detect_gravity_floor, max_gravity

            tuning = AffectTuning.from_settings(self.settings)
            state = apply_decay(await self.pks.get_user_affect(user_id=user_id), tuning)

            sid = str(session_id)
            gravity = max_gravity(
                self._gravity_tracker.current(sid), detect_gravity_floor(user_message)
            )
            register = self._session_registers.get(sid)
            has_pending = bool(self._birra_pending.get(sid))

            decision = evaluate_birra(
                state,
                tuning,
                self.settings,
                user_message=user_message,
                gravity=gravity,
                register=register.value if register is not None else None,
                has_pending_act=has_pending,
            )
            sal = decision.salience
            force_brain = decision.armed and not decision.obey_now
            mode = str(getattr(self.settings, "neo_affect_route_to_brain", "never"))
            if sal is not None and not force_brain:
                if mode == "any_salient":
                    force_brain = not sal.calm
                elif mode == "decision_changing":
                    force_brain = (
                        sal.at_least("ira", Level.FORTE)
                        or sal.at_least("medo", Level.FORTE)
                        or sal.posture_active
                    )

            hint = None
            if (
                sal is not None
                and getattr(self.settings, "neo_affect_router_hint_enabled", False)
                and (not sal.calm or decision.armed)
            ):
                hint = self._render_router_affect_hint(sal, decision)

            # Transições de episódio são persistidas AQUI, não no epílogo: a
            # diretiva do cérebro lê o episódio do próprio estado, então gravar só
            # no fim do turno faria a birra armar sem o cérebro saber no turno em
            # que ela arma — e, pior, manteria "BIRRA ATIVA" no turno em que o
            # usuário pediu desculpa. São eventos raros; o custo é irrelevante.
            was_active = state.birra is not None and state.birra.active
            if decision.release_reason and was_active:
                await self.pks.update_birra_episode(
                    user_id=user_id, decision=decision,
                    release_reason=decision.release_reason,
                )
            elif decision.armed and not was_active:
                await self.pks.update_birra_episode(user_id=user_id, decision=decision)

            if decision.armed or force_brain:
                logger.info(
                    "[BIRRA] %s | route_to_brain=%s hint=%s",
                    decision.log_line(), force_brain, bool(hint),
                )
                if slog:
                    slog.log_event(
                        "Modo Birra",
                        f"**Decisão:** {decision.log_line()}\n"
                        f"**Desvio para o cognitivo:** {force_brain}\n"
                        + (f"**Supressores:** {', '.join(decision.notes)}\n" if decision.notes else "")
                        + (f"**Estado:** {sal.render_line()}" if sal else ""),
                    )
            return TurnAffect(decision=decision, hint=hint, force_brain=force_brain)
        except Exception:  # noqa: BLE001 — afeto nunca derruba o turno
            logger.warning("[BIRRA] derivação falhou; turno segue normal", exc_info=True)
            return empty

    async def _settle_birra(self, *, session_id: UUID, user_id: UUID | None) -> None:
        """Fecha o turno da birra: persiste o episódio e reverte o que for devido.

        Roda em background, depois da resposta já ter saído. Três coisas:
        1. atualiza os contadores do episódio (é deles que saem os tetos);
        2. reverte pendências quando o turno pediu (desculpa, ordem enfática, fim);
        3. mantém a marca de pendência que serve de âncora para "para com isso".
        """
        sid = str(session_id)
        turn = self._session_birra.get(sid)
        if turn is None or user_id is None:
            return
        decision = turn.decision
        try:
            store = self._birra_store()
            adapter = self._get_ha_adapter()

            # 2. Reversão imediata — antes de mexer no episódio, para que uma
            # falha de persistência não deixe a luz apagada.
            reason = decision.release_reason or ("escape" if decision.obey_now else None)
            if reason and adapter is not None:
                from core.agent.birra.revert import revert_pending

                done = await revert_pending(
                    store, adapter, user_id=user_id, reason=reason[:16],
                )
                if done:
                    logger.info("[BIRRA] released reason=%s revertidos=%d", reason, done)
                self._birra_pending.pop(sid, None)

            # 1. Contadores do episódio (é deles que saem os tetos). O ARME e o
            # ENCERRAMENTO já foram persistidos antes do dispatch, em
            # _derive_turn_birra — aqui só entra o que este turno consumiu.
            sink = self._birra_sinks.pop(sid, None)
            if sink is not None and (sink.acts or sink.physical_acts):
                await self.pks.update_birra_episode(
                    user_id=user_id,
                    decision=decision,
                    acts=len(sink.acts),
                    physical_acts=sink.physical_acts,
                )

            # 3. Âncora da ordem enfática: "para com isso" só conta como ordem
            # quando vem logo depois de uma birra.
            if decision.armed and not decision.release_reason:
                self._birra_pending[sid] = decision.episode_id or "1"
        except Exception:  # noqa: BLE001 — epílogo nunca derruba nada
            logger.warning("[BIRRA] epílogo falhou", exc_info=True)

    def _birra_armed(self, session_id: UUID) -> bool:
        """A birra deste turno está armada? Fonte única: a decisão do turno.

        O seletor de contexto derivava isso do episódio persistido, que é um
        efeito da decisão e não a decisão — as duas divergiram e a diretiva de
        birra sumiu do prompt sem que nada mais mudasse de comportamento.
        """
        turn = self._session_birra.get(str(session_id))
        return bool(turn is not None and getattr(turn.decision, "armed", False))

    def _birra_store(self) -> Any | None:
        from core.agent.birra.store import BirraStore

        if getattr(self, "_birra_store_cache", None) is None:
            self._birra_store_cache = BirraStore(self.db)
        return self._birra_store_cache

    async def _build_birra_sink(self, session_id: UUID, user_id: UUID | None) -> Any | None:
        """Sink do turno para a skill de casa. ``None`` = atuação normal."""
        turn = self._session_birra.get(str(session_id))
        if turn is None or user_id is None or not getattr(turn.decision, "armed", False):
            return None
        if getattr(turn.decision, "obey_now", False):
            return None
        try:
            from core.agent.birra.sink import BirraSink

            adapter = self._get_ha_adapter()
            if adapter is None or not adapter.is_configured:
                return None
            sink = BirraSink(
                decision=turn.decision,
                settings=self.settings,
                adapter=adapter,
                store=self._birra_store(),
                user_id=user_id,
                session_id=session_id,
            )
            # O epílogo (_settle_birra) precisa dos contadores deste sink.
            self._birra_sinks[str(session_id)] = sink
            return sink
        except Exception:  # noqa: BLE001
            logger.warning("[BIRRA] falha ao montar o sink; skill segue normal", exc_info=True)
            return None

    async def _affect_directive_for_skill(self, user_id: UUID | None) -> str | None:
        """Diretiva de tom para o prompt da skill de casa (§3.7.2 do plano)."""
        if user_id is None or not getattr(
            self.settings, "neo_affect_skill_directive_enabled", False
        ):
            return None
        if not self.settings.neo_affect_enabled:
            return None
        try:
            from core.agent.affect import AffectTuning, apply_decay, render_directive

            tuning = AffectTuning.from_settings(self.settings)
            state = apply_decay(await self.pks.get_user_affect(user_id=user_id), tuning)
            return render_directive(state, tuning) or None
        except Exception:  # noqa: BLE001
            logger.warning("[BIRRA] diretiva de tom da skill falhou", exc_info=True)
            return None

    @staticmethod
    def _render_router_affect_hint(sal: Any, decision: Any) -> str:
        """Bloco de estado emocional para o prompt do CLASSIFICADOR.

        Números crus aqui são seguros e desejáveis: o roteador classifica e
        devolve JSON, nunca fala com o usuário — a regra "não cite números" vale
        para o cérebro, em ``render_directive``. E um classificador decide muito
        melhor com um escalar do que com um adjetivo difuso ("ira 87" e "ira 62"
        pedem respostas diferentes e ambos seriam só "alto"). O NÍVEL nominal vai
        junto e é a autoridade, para o modelo não inventar um limiar próprio.
        """
        flags = []
        if sal.posture_active:
            flags.append("discussão aberta")
        if sal.grudge_count:
            flags.append(f"{sal.grudge_count} ofensa(s) nas últimas 24h")
        if decision.armed:
            flags.append("BIRRA ARMADA")
        return (
            "## Estado emocional do Neo com este usuário "
            "(interno — 0-100, \"repouso\" = o normal dele)\n"
            f"{sal.render_line()}\n"
            + (f"{' · '.join(flags)}\n" if flags else "")
            + "Quanto mais longe do repouso, mais o estado muda qual é a resposta CERTA "
            "— e a skill de casa não conhece esse estado. Com ira/medo em `forte` ou "
            "`extremo`, ou com BIRRA ARMADA, pedido de casa (luz, ar, cena) vai para "
            "DIRECT_RESPONSE: o núcleo cognitivo tem a persona e decide como responder "
            "(pode acionar a skill depois via handoff). Em `repouso`/`elevado` sem birra, "
            "roteie normalmente. Exceção sempre: ordem enfática ou urgência explícita "
            "(\"faz isso imediatamente\", \"é sério\", \"urgente\") continua HOME_ASSISTANT."
        )

    # ---------------------------------------------------------------
    # Read-only prompt preview (panel debug tool — no writes, no LLM)
    # ---------------------------------------------------------------

    def _preview_register(
        self,
        *,
        decision: "RoutingDecision",
        user_message: str,
        live: bool,
    ) -> "Register | None":
        """Side-effect-free register derivation for the preview.

        Mirrors ``_derive_turn_register`` but WITHOUT the sticky-gravity mutation
        (no session state to touch in a stateless debug call). Returns None when
        the feature is off. Fail-open: never raises.
        """
        if self.settings.neo_register_enabled is not True:
            return None
        try:
            skill_register: str | None = None
            skill_name = decision.skill_name or decision.skill_hint
            if skill_name:
                from core.skills.manifest import SkillManifest

                manifest = SkillManifest.all().get(skill_name)
                if manifest is not None:
                    skill_register = getattr(manifest, "tone_register", "") or None
            session_gravity = max_gravity(decision.gravity, detect_gravity_floor(user_message))
            register, _reason = derive_register(
                decision,
                user_message=user_message,
                live=live,
                skill_register=skill_register,
                session_gravity=session_gravity,
            )
            return register
        except Exception:  # noqa: BLE001 — preview must never crash on register
            logger.warning("[preview] register derivation failed", exc_info=True)
            return None

    async def preview_prompt(
        self,
        *,
        user_id: UUID,
        user_message: str,
        metadata: dict[str, Any] | None = None,
        live: bool = False,
    ) -> dict[str, Any]:
        """Reproduce the real turn's prompt assembly for the panel, read-only.

        Runs the SAME pre-processing as ``_handle_direct_response`` — real intent
        classification (so ``knowledge_keywords``/``needs_memory`` are the real
        ones), the hybrid cross-session memory recovery (the RETRIEVED layer) and
        the situational register — then ``build_prompt``. Deliberately omits the
        two side effects the live turn has: it does NOT run ``learn_before_response``
        (that WRITES to the PKS) and it never calls the LLM. So the panel shows the
        exact system prompt + selection trace + semantic-memory layer the model
        actually receives, instead of the stripped-down preview the old endpoint
        produced.

        Returns a dict with keys: ``system_prompt``, ``candidates`` (full trace),
        ``prompt_tokens_est``, ``decision``, ``recovered`` (RETRIEVED layer),
        ``contexts_found`` (PKS neighborhoods), ``context_text``, ``keywords``.
        """
        sys_context = self._build_system_context(metadata) if metadata else None
        is_training = self.pks.is_training_message(user_message) if self.pks else False

        # Real routing decision — its keywords drive PKS retrieval and its
        # needs_memory gates the cross-session recovery below.
        decision = await self.router.classify(
            user_message, conversation_history=None, system_context=sys_context
        )

        # RETRIEVED layer: hybrid vector+FTS+temporal search over past sessions.
        # Same gate the live turn uses (needs_memory + not a training message).
        recovered: list[MemorySearchResult] = []
        if decision.needs_memory and not is_training:
            try:
                recovered = await self.sessions.recover_context(user_message, user_id=user_id)
            except Exception as e:  # noqa: BLE001 — recovery never breaks the preview
                logger.warning("[preview] recover_context failed: %s", e)

        register = self._preview_register(decision=decision, user_message=user_message, live=live)

        system_prompt, candidates = await self.context_selector.build_prompt(
            user_message=user_message,
            user_id=user_id,
            decision=decision,
            system_context=sys_context,
            recovered_context=recovered,
            is_neo_training=is_training,
            pks_prewrite=None,        # read-only: prewrite WRITES to the PKS
            pks_system_prewrite=None,
            mission_context=None,
            register=register,
        )

        # PKS neighborhoods for the entity cards, using the SAME keywords
        # build_prompt used — so the cards match the injected PKS block.
        contexts_found: list[dict[str, Any]] = []
        context_text = ""
        if self.pks and user_id and not is_training:
            try:
                pks_result, context_text = await self.pks.retrieve_context(
                    user_id=user_id,
                    user_message=user_message,
                    keywords=decision.knowledge_keywords,
                )
                contexts_found = pks_result.get("neighborhoods", [])
            except Exception as e:  # noqa: BLE001
                logger.warning("[preview] retrieve_context failed: %s", e)

        return {
            "system_prompt": system_prompt,
            "candidates": candidates,
            "prompt_tokens_est": sum(c.est_tokens for c in candidates if c.included),
            "decision": decision,
            "recovered": recovered,
            "contexts_found": contexts_found,
            "context_text": context_text,
            "keywords": decision.knowledge_keywords or [],
        }

    # ---------------------------------------------------------------
    # Main entry point — called by gRPC controller
    # ---------------------------------------------------------------

    async def process_user_input(
        self,
        user_message: str,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Process a user message — classify intent and dispatch.

        Yields AIServiceResponse objects that the gRPC controller
        converts to CoreMessage protobufs.

        Args:
            user_message: The user's message text.
            session_id: Optional session ID (UUID from desktop).
            user_id: Optional user UUID.
            service: Service name (e.g. 'neo-desktop').
            metadata: Additional metadata from client.

        Yields:
            AIServiceResponse with thinking, tool requests, or final response.
        """
        # 0. Entrada por voz: backstop determinístico do nome ANTES de tudo — antes
        # de salvar a mensagem, de embeddar e de classificar. O corretor completo
        # mora no neo-voice (stt_text.py); isto cobre o cliente que não passou por
        # ele (mobile, áudio de WhatsApp). O texto cru vai para o metadata, então
        # nada se perde. Ver docs/voice-input-normalization-plan.md §4.2.
        if self._is_voice_input(metadata):
            fixed, variants = canonicalize_assistant_name(user_message)
            if variants:
                logger.info("[ASR] nome canonizado no core: %s → %s (%r)",
                            variants, ASSISTANT_NAME, user_message)
                metadata = {**(metadata or {})}
                metadata.setdefault("asr_raw", user_message)
                metadata["asr_core_fixes"] = ",".join(variants)
                user_message = fixed

        # 1. Get or create session (with topic rotation check)
        t0_total = time.monotonic()
        logger.info(f"[HISTORY-DEBUG] Input session_id from desktop: {session_id}")
        t0_session = time.monotonic()
        session = await self.sessions.get_or_create_session(
            session_id=session_id,
            user_id=user_id,
            service=service,
            # Só o live expira pelo relógio curto; o chat ganha o backstop longo.
            mode=(metadata or {}).get("mode"),
        )
        session_ms = round((time.monotonic() - t0_session) * 1000, 1)
        sid = session.id
        if user_id is None and session.user_id is not None:
            user_id = session.user_id
            logger.info("[PKS] Resolved user_id from existing session: %s", user_id)
        slog = get_session_logger(sid)
        slog.new_interaction(user_message, desktop_session_id=str(session_id) if session_id else None, metadata=metadata)
        logger.info(f"[HISTORY-DEBUG] Resolved DB session: {sid} (message_count={session.message_count})")

        # Check if topic changed → rotate session
        old_sid = sid
        rotated = False
        _rotation_result: dict | None = None
        if session.message_count > 0:
            rotation_result = await self.sessions.should_rotate_session(session, user_message)
            _rotation_result = rotation_result
            should_rotate = rotation_result.get("should_rotate", False)

            # Log the full topic rotation check
            slog.log_topic_check(
                similarity=rotation_result.get("similarity"),
                threshold=rotation_result.get("threshold", 0),
                rotated=should_rotate,
                reason=rotation_result.get("reason", ""),
                message_count=rotation_result.get("message_count", 0),
                min_required=rotation_result.get("min_required", 0),
                cooldown_minutes=rotation_result.get("cooldown_minutes", 0),
                elapsed_minutes=rotation_result.get("elapsed_minutes"),
                new_message=user_message,
                context_text=rotation_result.get("context_text", ""),
                duration_ms=rotation_result.get("duration_ms", 0),
            )

            if should_rotate:
                logger.info(f"Topic change detected, rotating session {sid}")
                await self.sessions.close_session(sid, reason="topic_change")
                session = await self.sessions.get_or_create_session(
                    session_id=None, user_id=user_id, service=service,
                    mode=(metadata or {}).get("mode"),
                )
                # Record a continuity link so the next (possibly elliptical)
                # follow-up can still recover the rotated-away conversation,
                # instead of orphaning it forever.
                session.extra_metadata = {
                    **_session_meta(session),
                    "created_after_rotated_session_id": str(sid),
                    "created_after_rotation_reason": "topic_change",
                }
                await self.db.update_session(session)
                sid = session.id
                rotated = True
                # Switch to the new session's logger
                slog = get_session_logger(sid)
                slog.new_interaction(user_message, desktop_session_id=str(session_id) if session_id else None, metadata=metadata)

        slog.log_session_resolution(
            db_session_id=str(sid),
            message_count=session.message_count,
            is_new=(session.message_count == 0),
            rotated_from=str(old_sid) if rotated else None,
        )
        continuity = await self.sessions.resolve_continuity_context(
            session=session,
            message=user_message,
            user_id=user_id,
            service=service,
        )
        if continuity.get("action") != "none":
            slog.log_event(
                "Continuidade de Contexto",
                "\n".join(
                    [
                        f"- **Ação:** `{continuity.get('action')}`",
                        f"- **Confiança:** `{continuity.get('confidence')}`",
                        f"- **Motivo:** `{continuity.get('reason')}`",
                        f"- **Sessão fonte:** `{continuity.get('source_session_id')}`",
                    ]
                ),
            )

        # 3. Yield thinking status cedo — usuário vê feedback enquanto processamos
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=sid,
            message="Analisando sua solicitação...",
            is_final=False,
        )

        # 2+4. Salvar mensagem (com embedding) E buscar histórico EM PARALELO
        # get_history roda sem current_embedding (re-rank semântico ignorado para msgs novas;
        # só afeta sessões com >30 msgs). O classify recebe o histórico diretamente.
        t0_parallel = time.monotonic()
        saved_msg, history = await asyncio.gather(
            self.sessions.add_message(
                session_id=sid,
                role="user",
                content=user_message,
                generate_embedding=True,
                metadata=metadata,
                user_id=user_id,
            ),
            self.sessions.get_history(
                sid, limit=20, max_tokens_budget=3000,
                current_embedding=None,  # ainda não disponível; sem re-rank semântico
            ),
        )
        parallel_ms = round((time.monotonic() - t0_parallel) * 1000, 1)
        embed_ms = parallel_ms  # ambas as ops rodaram em paralelo
        hist_ms = 0.0           # sobrepostos — não contados separadamente
        current_embedding = saved_msg.embedding if saved_msg else None
        slog.log_message_saved("user", user_message, str(sid))

        sys_context = self._build_system_context(metadata)
        # Modo Live (voz): a resposta final será falada por TTS. Injeta o estilo
        # "texto corrido sem markdown" no contexto (chega ao DIRECT_RESPONSE via
        # system_context) e marca a sessão para os caminhos que perdem o metadata.
        live = (metadata or {}).get("mode") == "live"
        if live:
            self._live_sessions.add(str(sid))
            sys_context = "\n\n".join(part for part in (sys_context, _LIVE_STYLE_INSTRUCTION) if part)
        else:
            self._live_sessions.discard(str(sid))
        # Entrada por VOZ: a instrução em si já veio dentro do _build_system_context
        # (todo caminho que reconstrói o turno passa por lá). Aqui só marcamos a
        # sessão, para os caminhos que perdem o metadata no round-trip — o mesmo
        # motivo do set do Live acima.
        if self._is_voice_input(metadata):
            self._voice_input_sessions.add(str(sid))
        else:
            self._voice_input_sessions.discard(str(sid))
        # Imagens no chat: a instrução do marcador `[[IMAGE:…]]` só vai pro cliente
        # que sabe renderizá-lo (senão o usuário veria o marcador cru). Mesmo seam
        # do estilo Live acima — chega ao cognitivo E ao classificador (que recebe o
        # mesmo sys_context), o que é o que tira o `base64 -i foto.jpg` da mesa.
        if self._client_renders_inline_images(metadata):
            self._image_capable_sessions.add(str(sid))
            sys_context = "\n\n".join(
                part for part in (sys_context, _INLINE_IMAGE_INSTRUCTION) if part
            )
        else:
            self._image_capable_sessions.discard(str(sid))
        # Identidade: o email PRÓPRIO do Neo. Vai no prompt (e não atrás de uma
        # tool) porque a pergunta que ele responde é "eu tenho um email?" — se
        # dependesse de descobrir, o Neo pediria o email do usuário em vez de usar
        # o dele. O ENDEREÇO é público; a senha fica no cofre.
        identity = await self._neo_identity_block()
        if identity:
            sys_context = "\n\n".join(part for part in (sys_context, identity) if part)
        # Anexos (docs/file-store-plan.md §5): só o MANIFESTO entra no prompt; o
        # modelo lê por fatia/materializa com as tools file_*. Mesmo seam do estilo
        # Live acima — chega ao cognitivo via system_context E ao classificador
        # (que recebe o mesmo sys_context), por isso os anexos da sessão inteira
        # entram: é o que impede o roteamento de tratar o anexo como arquivo local.
        attachments_manifest = await self._build_attachments_manifest(
            metadata, session_id=sid
        )
        if attachments_manifest:
            sys_context = "\n\n".join(part for part in (sys_context, attachments_manifest) if part)
        mission_context = await self._build_recent_missions_context(sid, user_id)
        if mission_context:
            sys_context = "\n\n".join(part for part in (sys_context, mission_context) if part)
        # Artefatos (arquivos que o Neo já pôs no disco dele). Mesma costura do
        # manifesto de anexos, e pela mesma razão: o `sys_context` é o ÚNICO
        # canal que o classificador também lê, e é ele quem monta o comando.
        artifacts_block, artifact_paths = await self._recent_artifacts(user_id)
        if artifacts_block:
            sys_context = "\n\n".join(part for part in (sys_context, artifacts_block) if part)

        if continuity.get("action") == "inject" and continuity.get("history"):
            continuity_history = continuity["history"]
            logger.info(
                "[HISTORY-DEBUG] Injecting %s messages from continuity session %s",
                len(continuity_history),
                continuity.get("source_session_id"),
            )
            history = continuity_history + history
            continuity_note = (
                "## Continuidade de Contexto\n"
                "A mensagem atual depende de uma conversa anterior recuperada automaticamente. "
                "Use o histórico recuperado como contexto imediato, mas responda à última mensagem do usuário."
            )
            sys_context = "\n\n".join(part for part in (sys_context, continuity_note) if part)

        if continuity.get("action") == "disambiguate":
            candidates = continuity.get("candidates") or []
            lines = [
                "Você quer que eu continue qual assunto?",
                "",
            ]
            for index, candidate in enumerate(candidates[:3], start=1):
                lines.append(f"{index}. {candidate.get('summary')}")
            lines.append("")
            lines.append("Me diga o número ou descreva o assunto que eu retomo.")
            clarification = "\n".join(lines)
            await self.sessions.add_message(
                session_id=sid,
                role="assistant",
                content=clarification,
                generate_embedding=False,
                user_id=user_id,
            )
            slog.log_final_response(clarification, "COGNITIVE", iterations=1)
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE,
                session_id=sid,
                message=clarification,
                is_final=True,
                iterations=1,
                metadata={"continuity": continuity},
            )
            return

        # If session was rotated, carry some context from the previous session
        # so the classifier and LLM have continuity.
        if rotated and old_sid:
            old_history = await self.sessions.get_history(old_sid, limit=10, max_tokens_budget=1500)
            if old_history:
                logger.info(f"[HISTORY-DEBUG] Carrying {len(old_history)} messages from rotated session {old_sid}")
                history = old_history + history

        logger.info(f"[HISTORY-DEBUG] get_history returned {len(history)} messages for session {sid}")
        for i, msg in enumerate(history):
            logger.info(f"[HISTORY-DEBUG]   [{i}] {msg['role']}: {msg['content'][:100]}")
        slog.log_history(history)

        # Start observability trace (fire-and-forget, never blocks)
        obs = get_obs()
        trace = obs.start_trace(
            session_id=str(sid),
            user_message=user_message,
            service=service,
        )

        # Record topic rotation check in the trace (if it was performed)
        if _rotation_result is not None:
            trace.set_topic_rotation(_rotation_result)

        # Clarification loop: if we previously asked the user for more info, this
        # message is the answer. Skip classification and run a second
        # DIRECT_RESPONSE iteration that uses the answer to produce the real reply.
        clarification_round = 1
        allow_clarification = True
        forced_clarification = False
        pending_clar = _session_meta(session).get("pending_clarification")
        if pending_clar:
            prev_round = int(pending_clar.get("round", 1))
            clarification_round = prev_round + 1
            allow_clarification = prev_round < MAX_CLARIFICATION_ROUNDS
            forced_clarification = True
            # Clear the pending state up front so a failure can't trap the session.
            session.extra_metadata = {
                k: v for k, v in _session_meta(session).items()
                if k != "pending_clarification"
            }
            await self.db.update_session(session)
            clar_note = (
                "## Resolução de esclarecimento\n"
                f"Você havia perguntado ao usuário: \"{pending_clar.get('clarifying_question', '')}\"\n"
                f"A pergunta ORIGINAL do usuário foi: \"{pending_clar.get('original_message', '')}\"\n"
                "A mensagem ATUAL é a resposta do usuário a esse esclarecimento. Use essa nova "
                "informação somada ao histórico para responder à pergunta ORIGINAL agora."
            )
            sys_context = "\n\n".join(p for p in (sys_context, clar_note) if p)
            if slog:
                slog.log_event(
                    "Loop de Clarificação",
                    f"Round {clarification_round} — interpretando mensagem como resposta ao esclarecimento.",
                )

        # 4b. Estado emocional do turno — ANTES do roteador, porque é ele que
        # decide QUEM atende (docs/modo-birra-plan.md §3.1). Nunca levanta.
        turn_affect = await self._derive_turn_birra(
            user_message=user_message, user_id=user_id, session_id=sid, slog=slog,
        )
        self._session_birra[str(sid)] = turn_affect

        # 5. Classify intent — history já disponível do gather acima
        # Bypass: metadata["force_intent"] = "CLIENT_TOOL"|"DIRECT_RESPONSE"|"SERVER_TOOL"
        # útil para testes sem precisar aguardar o classificador.
        _force_intent = (metadata or {}).get("force_intent", "").upper()
        t0 = time.monotonic()
        active_whatsapp_mission = await self.db.find_active_whatsapp_mission_by_session(sid, user_id)
        if forced_clarification:
            decision = RoutingDecision(
                intent=IntentType.DIRECT_RESPONSE,
                confidence=0.9,
                needs_memory=True,
                needs_plan=False,
                requires_approval=False,
                explanation=f"Resposta a esclarecimento pendente (round {clarification_round}).",
            )
            logger.info("[INTENT-OVERRIDE] pending clarification answer → DIRECT_RESPONSE")
        elif active_whatsapp_mission and _looks_like_whatsapp_mission_followup(user_message):
            decision = RoutingDecision(
                intent=IntentType.WHATSAPP_MESSAGING,
                confidence=1.0,
                skill_name="whatsapp_messaging",
                needs_plan=False,
                requires_approval=False,
                explanation=(
                    "Mensagem interpretada como continuidade da missão WhatsApp ativa "
                    f"{active_whatsapp_mission.id}."
                ),
            )
            logger.info(
                "[INTENT-OVERRIDE] active whatsapp mission follow-up mission=%s",
                active_whatsapp_mission.id,
            )
        elif _force_intent in ("CLIENT_TOOL", "DIRECT_RESPONSE", "COGNITIVE", "SERVER_TOOL"):
            from shared.types import RoutingDecision as _RD
            _intent_map = {
                "CLIENT_TOOL": IntentType.CLIENT_TOOL,
                "DIRECT_RESPONSE": IntentType.DIRECT_RESPONSE,  # alias → COGNITIVE
                "COGNITIVE": IntentType.COGNITIVE,
                "SERVER_TOOL": IntentType.SERVER_TOOL,
            }
            decision = _RD(
                intent=_intent_map[_force_intent],
                confidence=1.0,
                tool_name=(metadata or {}).get("force_tool_name") or None,
                needs_plan=True,
                requires_approval=False,
                explanation=f"[forced via metadata] {_force_intent}",
            )
            logger.info(f"[INTENT-OVERRIDE] force_intent={_force_intent}")
        else:
            decision = await self.router.classify(
                user_message,
                conversation_history=history,
                system_context=sys_context,
                affect_hint=turn_affect.hint,
                affect_force_brain=turn_affect.force_brain,
                artifact_paths=artifact_paths,
            )
        intent_ms = (time.monotonic() - t0) * 1000
        logger.info(f"[TIMING] session={session_ms}ms embed+hist={embed_ms}ms intent={intent_ms:.0f}ms")
        logger.info(f"Intent: {decision.intent.value} (confidence={decision.confidence:.2f}, needs_memory={decision.needs_memory})")
        slog.record_step_timing("Sessão (get/create)", session_ms)
        slog.record_step_timing("Embed + Histórico (parallel)", embed_ms)
        slog.record_step_timing("Classificação de Intent", round(intent_ms, 1))

        classifier_payload = decision.classifier_payload
        classifier_raw = decision.classifier_raw

        slog.log_intent_classification(
            decision_data=decision.model_dump(),
            duration_ms=intent_ms,
            embed_ms=embed_ms,
            hist_ms=hist_ms,
        )

        trace.set_intent(
            intent=decision.intent.value,
            confidence=decision.confidence,
            explanation=decision.explanation or "",
            tool_name=decision.tool_name,
            raw_payload=classifier_payload,
            duration_ms=round(intent_ms, 2),
            needs_memory=decision.needs_memory,
        )

        # Log the classifier as an LLM call so it appears in the debug panel's "LLM Calls" section
        if trace and classifier_payload:
            trace.add_llm_call(
                provider=classifier_payload.get("provider", "router"),
                model=classifier_payload.get("model", "intent-classifier"),
                messages_sent=classifier_payload.get("messages", []),
                response_text=classifier_raw or "",
                duration_ms=round(intent_ms, 2),
                temperature=0.1,
                stage="intent_classification",
            )

        # 6. Dispatch based on intent
        # Registro da situação (teto de tom) — derivado uma vez por turno, antes
        # do dispatch, e repassado aos handlers. None = flag off ou shadow mode.
        register = self._derive_turn_register(
            decision=decision,
            user_message=user_message,
            session_id=sid,
            live=live,
            slog=slog,
        )
        final_response: str | None = None
        dispatch_error: str | None = None
        try:
            if self.settings.pipeline_orchestrator_enabled:
                # Dispatch via PipelineOrchestrator. Phase 1: a single seeded step,
                # no handoffs → identical behaviour to the legacy path below.
                async for resp in self._run_pipeline(
                    decision=decision,
                    user_message=user_message,
                    history=history,
                    sid=sid,
                    trace=trace,
                    sys_context=sys_context,
                    slog=slog,
                    user_id=user_id,
                    metadata=metadata,
                    saved_msg=saved_msg,
                    allow_clarification=allow_clarification,
                    clarification_round=clarification_round,
                    register=register,
                ):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            # --- Legacy inline dispatch (kept for instant rollback via flag) ---
            elif decision.intent == IntentType.DIRECT_RESPONSE:
                async for resp in self._handle_direct_response(
                    sid,
                    user_message,
                    history,
                    decision,
                    trace,
                    sys_context,
                    slog=slog,
                    user_id=user_id,
                    metadata=metadata,
                    source_message_id=str(saved_msg.id) if saved_msg else None,
                    allow_clarification=allow_clarification,
                    clarification_round=clarification_round,
                    register=register,
                ):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.SERVER_TOOL:
                async for resp in self._handle_server_tool(sid, user_message, history, decision, trace, slog=slog, user_id=user_id, live=live, metadata=metadata, register=register):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.CLIENT_TOOL:
                async for resp in self._handle_client_tool(sid, user_message, decision, trace, slog=slog, user_id=user_id, metadata=metadata, history=history):
                    yield resp

            elif decision.intent == IntentType.WHATSAPP_MESSAGING:
                async for resp in self._handle_whatsapp_messaging(
                    sid,
                    user_message,
                    history,
                    decision,
                    trace,
                    slog=slog,
                    user_id=user_id,
                    metadata=metadata,
                    source_message_id=saved_msg.id if saved_msg else None,
                ):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.HOME_ASSISTANT:
                async for resp in self._handle_home_assistant(
                    sid,
                    user_message,
                    history,
                    decision,
                    trace,
                    slog=slog,
                    user_id=user_id,
                    metadata=metadata,
                ):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp
        except Exception as e:
            dispatch_error = str(e)
            slog.log_event("Erro no Dispatch", f"```\n{dispatch_error}\n```")
            raise
        finally:
            total_ms = round((time.monotonic() - t0_total) * 1000, 1)
            logger.info(f"[TIMING-TOTAL] neo-core total={total_ms}ms")
            slog.log_timing_summary(total_ms)
            # Affect choke point: EVERY intent ends here (direct, skills, HA,
            # tools, WhatsApp outbound), so emotions update no matter which path
            # handled the turn — "Alexa, liga a luz" included. Runs after the
            # final response was already yielded; scheduling only creates a task.
            if not dispatch_error:
                updater = self._get_affect_updater()
                if updater:
                    updater.schedule(
                        user_message=user_message,
                        assistant_response=final_response,
                        user_id=user_id,
                        source=f"intent:{decision.intent.value}",
                        source_message_id=str(saved_msg.id) if saved_msg else None,
                        slog=slog,
                    )
            # Epílogo da birra: persiste o episódio (contadores/tetos) e reverte o
            # que precisa ser revertido AGORA — desculpa, ordem enfática, fim de
            # episódio. O prazo normal fica com o sweeper.
            if not dispatch_error:
                self._spawn_bg(
                    self._settle_birra(session_id=sid, user_id=user_id),
                    name="birra-settle",
                )
            if decision.intent == IntentType.CLIENT_TOOL and not dispatch_error:
                # Keep trace alive — continuation in process_tool_result / process_command_approval
                # Must key by the desktop gRPC session_id (not the DB sid) so process_tool_result can find it
                self._pending_traces[str(session_id or sid)] = trace
            else:
                obs.finish_trace(trace, response=final_response, error=dispatch_error)

    # ---------------------------------------------------------------
    # Pipeline orchestration (Phase 1: single-step chains)
    # ---------------------------------------------------------------

    def _seed_steps(
        self, decision: RoutingDecision, user_message: str
    ) -> list[PipelineStep]:
        """Build the initial step queue from a routing decision.

        Single step mirroring the classified intent — unless the classifier
        declared a cascade (Phase 4, ``pipeline_plan``), which expands into N
        independent classifier-origin steps run in order.
        """
        plan = decision.pipeline_plan
        if plan and len(plan) >= 2 and self.settings.pipeline_cascade_enabled:
            steps: list[PipelineStep] = []
            for spec in plan:
                steps.append(
                    PipelineStep(
                        intent=spec.intent,
                        input_text=spec.input_text,
                        skill_name=spec.skill_name,
                        decision=RoutingDecision(
                            intent=spec.intent,
                            confidence=decision.confidence,
                            skill_name=spec.skill_name,
                            needs_memory=True,
                            needs_plan=False,
                            requires_approval=False,
                            explanation="Cascade step (plano declarado pelo router)",
                        ),
                        origin="classifier",
                        depth=0,
                    )
                )
            logger.info("[PIPELINE] seeding declared cascade with %d steps", len(steps))
            return steps

        return [
            PipelineStep(
                intent=decision.intent,
                input_text=user_message,
                skill_name=decision.skill_name,
                decision=decision,
                origin="classifier",
                depth=0,
            )
        ]

    async def _run_pipeline(
        self,
        *,
        decision: RoutingDecision,
        user_message: str,
        history: list[dict[str, str]],
        sid: UUID,
        trace: "MessageTrace | None",
        sys_context: str | None,
        slog: "SessionFileLogger | None",
        user_id: UUID | None,
        metadata: dict[str, Any] | None,
        saved_msg: Any,
        allow_clarification: bool,
        clarification_round: int,
        register: "Register | None" = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Dispatch the turn through the PipelineOrchestrator.

        The ``step_runner`` closure adapts each ``PipelineStep`` to the matching
        ``_handle_*`` generator (whose signatures differ) and reports a
        ``StepOutcome`` once the handler is exhausted. In Phase 1 no handoffs are
        produced, so the orchestrator runs exactly one step.
        """

        handoff_enabled = self.settings.pipeline_handoff_enabled
        self_max_steps = self.settings.pipeline_self_handoff_max_steps
        pipeline_step_index = 0
        last_notice: tuple[str, str] | None = None

        def _notice(kind: str, message: str) -> None:
            """Orchestrator early-exit → session log, so a dropped step is auditable."""
            nonlocal last_notice
            last_notice = (kind, message)
            if slog:
                slog.log_event(f"Pipeline — ciclo interrompido ({kind})", message)

        async def _step_runner(
            step: PipelineStep,
        ) -> AsyncIterator[tuple[AIServiceResponse | None, StepOutcome | None]]:
            nonlocal pipeline_step_index
            # Branch on the STEP's intent (not the original decision): a handoff
            # step targets a different pipeline than the classifier-seeded root.
            intent = step.intent
            msg = step.input_text
            step_metadata = metadata

            pipeline_step_index += 1
            step_idx = pipeline_step_index
            if slog:
                slog.log_pipeline_step(step_idx, intent.value, step.origin, msg)

            if step.decision is not None:
                dec: RoutingDecision = step.decision  # classifier-seeded root
            else:
                # Synthesize a minimal decision for a handoff step + carry the
                # original message in metadata for traceability (never for routing).
                # Phase 6 safety gating: outward/irreversible targets (WhatsApp)
                # must not auto-execute — flag for human confirmation. The skill's
                # own mission flow enforces it; this makes the policy explicit.
                gated = step.origin == "handoff" and handoff_requires_confirmation(intent)
                dec = RoutingDecision(
                    intent=intent,
                    confidence=1.0,
                    skill_name=step.skill_name,
                    needs_memory=True,
                    needs_plan=False,
                    requires_approval=gated,
                    explanation=f"Pipeline handoff step (origin={step.origin})",
                )
                step_metadata = {**(metadata or {}), "handoff_origin_message": user_message}
                if gated and slog:
                    slog.log_event(
                        "Handoff — gating de segurança",
                        f"Ação externa para `{intent.value}` exige confirmação humana "
                        "(passa pelo fluxo de missão/confirmação da skill).",
                    )
                if step.is_self_continuation:
                    # The continuation must see what the earlier steps really did,
                    # otherwise it retries the attempt that just failed.
                    if step.context_note:
                        step_metadata["pipeline_work_note"] = step.context_note
                    # Live progress: a non-final response with no persist_reply
                    # becomes an AgentThinking bubble on the desktop, so the user
                    # watches the cycle advance instead of staring at 100 s of
                    # silence. It replaces the previous bubble and is cleared on
                    # complete — exactly the semantics of "running right now".
                    yield (
                        AIServiceResponse(
                            intent=IntentType.DIRECT_RESPONSE,
                            session_id=sid,
                            message=f"{CYCLE_PROGRESS_MARKER}Continuando ({step_idx}/{self_max_steps}): {msg}",
                            is_final=False,
                            iterations=step_idx,
                        ),
                        None,
                    )

            if intent == IntentType.DIRECT_RESPONSE:
                gen = self._handle_direct_response(
                    sid, msg, history, dec, trace, sys_context,
                    slog=slog, user_id=user_id, metadata=step_metadata,
                    source_message_id=str(saved_msg.id) if saved_msg else None,
                    allow_clarification=allow_clarification,
                    clarification_round=clarification_round,
                    enable_handoff=handoff_enabled,
                    register=register,
                )
            elif intent == IntentType.SERVER_TOOL:
                gen = self._handle_server_tool(
                    sid, msg, history, dec, trace, slog=slog, user_id=user_id,
                    live=(step_metadata or {}).get("mode") == "live",
                    metadata=step_metadata,
                    register=register,
                )
            elif intent == IntentType.CLIENT_TOOL:
                gen = self._handle_client_tool(
                    sid, msg, dec, trace, slog=slog, user_id=user_id, metadata=step_metadata,
                    history=history,
                )
            elif intent == IntentType.WHATSAPP_MESSAGING:
                gen = self._handle_whatsapp_messaging(
                    sid, msg, history, dec, trace, slog=slog, user_id=user_id,
                    metadata=step_metadata,
                    source_message_id=saved_msg.id if saved_msg else None,
                )
            elif intent == IntentType.HOME_ASSISTANT:
                gen = self._handle_home_assistant(
                    sid, msg, history, dec, trace, slog=slog, user_id=user_id,
                    metadata=step_metadata,
                )
            elif intent == IntentType.MUSIC:
                gen = self._handle_music(
                    sid, msg, history, dec, trace, slog=slog, user_id=user_id,
                    metadata=step_metadata,
                )
            else:
                yield None, StepOutcome(ok=True, final_text=None)
                return

            last_final: str | None = None
            last_text: str | None = None
            handoffs: list[Handoff] = []
            work: dict[str, Any] | None = None
            async for resp in gen:
                control = (resp.metadata or {}).get("__pipeline_handoffs__")
                if control:
                    # Internal control item — capture, never forward to the client.
                    handoffs = control
                    work = (resp.metadata or {}).get("__pipeline_work__")
                    continue
                if resp.message:
                    last_text = resp.message
                if resp.is_final:
                    last_final = resp.message
                yield resp, None

            if trace:
                trace.add_pipeline_step(
                    index=step_idx,
                    intent=intent.value,
                    origin=step.origin,
                    input_text=msg,
                    final_text=last_final,
                    handoffs=[
                        "continuar" if h.is_self else (h.target_skill or h.target_intent.value)
                        for h in handoffs
                    ],
                )
            # Only build a note when a continuation will actually read it; a skill
            # handoff runs isolated and a terminal step has no successor.
            note = None
            if any(h.is_self for h in handoffs):
                note = _format_work_note(
                    f"Passo {step_idx} — {msg}",
                    (work or {}).get("reply") or last_text,
                    (work or {}).get("tool_trace"),
                    (work or {}).get("files"),
                )
            yield None, StepOutcome(
                ok=True, final_text=last_final, handoffs=handoffs, work_note=note
            )

        orchestrator = PipelineOrchestrator(
            _step_runner,
            self_max_steps=self_max_steps,
            self_max_depth=self.settings.pipeline_self_handoff_max_depth,
            on_notice=_notice,
        )
        saw_final = False
        pending_client = False
        async for resp in orchestrator.run(self._seed_steps(decision, user_message)):
            saw_final = saw_final or resp.is_final
            pending_client = pending_client or is_pending_client_turn(resp)
            yield resp

        # Invariant: the stream MUST end on an is_final. A response carrying a
        # handoff is deliberately non-final (handoff_defers_final), so any chain
        # that stops before running that handoff — anti-loop, depth, budget, a
        # failed dependent step — would leave the turn without a terminator. The
        # desktop only emits `complete` when it sees is_final (ai_agent.rs), so the
        # chat would spin forever. Close it here instead.
        #
        # The closer is its OWN short message, never a repeat of the last reply:
        # that reply already reached the chat as a persistent bubble, and echoing it
        # into `complete` would show the same text twice.
        #
        # EXCEÇÃO (sessões 081cd1c3 e 15b6b825): um turno que emitiu
        # tool_request/command_request está ESPERANDO o desktop, não morreu. O
        # terminador aqui era lido pelo Rust como fim de turno (`break` +
        # `drop(tx)`, ai_agent.rs), fechando a metade de envio da stream — ou
        # seja, matando o canal pelo qual o resultado ia voltar. O usuário via o
        # botão "Aprovar e Executar", clicava, e nada acontecia: o turno já tinha
        # sido encerrado milissegundos depois de pedir a permissão. Quem cuida da
        # queda de verdade é o `finally` do gRPC (`_close_incomplete_tool_turn`),
        # que é event-driven e não precisa de terminador para agir.
        if not saw_final and not pending_client:
            logger.warning("[PIPELINE] chain ended without a final response — closing the turn")
            if slog:
                slog.log_event(
                    "Pipeline — fechamento forçado",
                    f"A cadeia terminou sem resposta final ({last_notice[0] if last_notice else 'motivo desconhecido'}). "
                    "Emitindo terminador para o cliente não ficar preso.",
                )
            yield AIServiceResponse(
                intent=decision.intent,
                session_id=sid,
                message=_CYCLE_CLOSING_MESSAGE.get(
                    last_notice[0] if last_notice else "",
                    "Parei este turno aqui. Me diz se quer que eu siga de onde parei.",
                ),
                is_final=True,
                iterations=pipeline_step_index,
            )

    async def _resolve_speech_segments(
        self, parsed: list[Any]
    ) -> list[dict[str, Any]] | None:
        """Resolve voice-imitation segments (raw names) to transport dicts.

        Each parsed segment carries a raw voice name; resolve it against the coqui
        catalog to a concrete voice-settings dict. Unknown names fall back to the
        default voice (``voice=None``). Returns ``None`` when no segment ended up
        with a real override — the caller then uses the ordinary single-voice path.
        """
        if not parsed:
            return None
        from modules.voice import VoiceCatalog

        catalog = getattr(self, "_voice_catalog", None)
        if catalog is None:
            catalog = VoiceCatalog(getattr(self, "settings", None))
            self._voice_catalog = catalog

        resolved: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any] | None] = {}
        any_voice = False
        for seg in parsed:
            voice: dict[str, Any] | None = None
            name = getattr(seg, "voice", None)
            if name:
                if name not in cache:
                    try:
                        cache[name] = await catalog.resolve_named(name, engine="coqui")
                    except Exception as e:  # pragma: no cover - defensive
                        logger.warning("voice imitation: resolve '%s' failed: %s", name, e)
                        cache[name] = None
                voice = cache[name]
                if voice:
                    any_voice = True
            resolved.append({"text": seg.text, "voice": voice})
        return resolved if any_voice else None

    # ---------------------------------------------------------------
    # Handler: DIRECT_RESPONSE — conversational, no tools
    # ---------------------------------------------------------------

    async def _fail_turn(
        self,
        session_id: UUID,
        exc: BaseException,
        *,
        user_id: UUID | None = None,
        slog: "SessionFileLogger | None" = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Fecha um turno derrubado por falha de infraestrutura.

        Falha de provider NÃO é resposta. Antes, o texto cru do erro virava o
        `full_response` e seguia o fluxo normal: era salvo como a fala do
        assistant, voltava no histórico do turno seguinte E era mandado pro
        extrator de preferências e pro PKS planner como se fosse conteúdo — dá
        pra ver as duas chamadas no app.log da sessão 1aec71b8, com um 400 do
        DeepSeek no lugar da resposta.

        O que sai daqui é uma frase curta e humana; o erro real fica no log da
        aplicação e no log .md da sessão. Nada de trabalho de memória roda: não
        há conteúdo nenhum pra aprender.
        """
        if slog:
            slog.log_event("Erro LLM", f"```\n{exc}\n```")
        # Crédito/limite da chave é a única classe de falha em que o usuário tem o
        # que fazer — e em que "tenta de novo" seria mentira. O texto do erro NÃO
        # entra na frase (invariante do docstring); só escolhe qual frase usar.
        reply = TURN_FAILED_NO_CREDIT if _is_no_credit_failure(exc) else TURN_FAILED_REPLY
        try:
            await self.sessions.add_message(
                session_id=session_id,
                role="assistant",
                content=reply,
                generate_embedding=False,
                user_id=user_id,
            )
        except Exception:  # noqa: BLE001 — o turno já falhou; não falhar de novo aqui
            logger.warning("[fail-turn] não consegui salvar o fecho do turno", exc_info=True)
        if slog:
            slog.log_final_response(reply, "COGNITIVE", iterations=1)
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=session_id,
            message=reply,
            is_final=True,
            iterations=1,
        )

    async def _handle_direct_response(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        system_context: str | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        metadata: dict | None = None,
        source_message_id: str | None = None,
        allow_clarification: bool = True,
        clarification_round: int = 1,
        enable_handoff: bool = False,
        register: "Register | None" = None,
        _actuation_depth: int = 0,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle direct conversational response using LLM.

        ``allow_clarification`` lets the model ask the user ONE clarifying
        question (via the ``[[NEED_CLARIFICATION]]`` sentinel) when it lacks the
        context to answer; the pending state is stored on the session and the
        next user message resolves it. ``clarification_round`` is the round this
        response belongs to (for the round cap).
        """

        # The intent classifier already decided whether cross-session memory is useful.
        # Skip recovery when needs_memory=False (greetings, acks, phatic expressions).
        if not decision.needs_memory:
            recovered_context = []
            recover_ms = 0.0
            logger.info("[HISTORY-DEBUG] Skipping recover_context (needs_memory=False from classifier)")
            if slog:
                slog.log_event(
                    "Recuperação de Contexto",
                    "Pulada: o classificador indicou needs_memory=false (mensagem sem conteúdo semântico relevante).",
                )
        else:
            t0_recover = time.monotonic()
            recovered_context = await self.sessions.recover_context(user_message, user_id=user_id)
            recover_ms = round((time.monotonic() - t0_recover) * 1000, 1)

        prefs_ms = 0.0

        # Memory Pipeline v2: detect explicit Neo training to block parallel routes.
        is_neo_training = False
        if self.pks:
            try:
                is_neo_training = self.pks.is_training_message(user_message)
            except Exception as e:
                logger.warning(f"[PKS] Failed training detection: {e}", exc_info=True)
        if is_neo_training:
            logger.info(
                "[PKS] Neo training message detected — blocking user preference/personal memory routes"
            )

        # Skill tool_backed dona do assunto (match duro de keyword). Resolvido AQUI,
        # antes do PKS, porque a decisão vale para os dois lados do turno: o corpo e as
        # tools dela entram no prompt lá embaixo, e o PKS NÃO aprende esta mensagem —
        # o dado tem casa própria (ex.: tabelas fin_*) e o planner LLM só saberia
        # recriá-lo com valor/data chutados (saldo fantasma). Mesma linha do
        # device_memory_handled logo abaixo.
        _autoload: tuple[Any, str, list[Any]] | None = None
        if (
            self.skill_library is not None
            and self.settings.skills_library_enabled
            and self.settings.cognitive_tools_enabled
            and not is_neo_training
        ):
            try:
                sticky = await self._active_skill(session_id)
                _kw_match = self.skill_library.keyword_match(user_message)
                _autoload = self.skill_library.autoload_for(user_message, sticky=sticky)
            except Exception as e:  # noqa: BLE001 — autoload nunca derruba o turno
                logger.warning("[SkillLibrary] autoload_for falhou: %s", e)
                _autoload = None
                _kw_match = None
            # Keyword nova renova o prazo; turno que só herdou a skill consome um.
            await self._touch_active_skill(session_id, renew=_kw_match)
        skill_owns_turn = _autoload is not None

        # Ensure PKS exists and persist direct factual statements before the reply.
        pks_prewrite: dict[str, Any] | None = None
        pks_system_prewrite: dict[str, Any] | None = None
        if self.pks and user_id:
            try:
                await self.pks.ensure_user_pks(user_id=user_id)
                if not is_neo_training and not skill_owns_turn:
                    pks_prewrite = await self.pks.learn_before_response(
                        user_id=user_id,
                        user_message=user_message,
                        memory_target="user",
                        source_message_id=source_message_id,
                    )
                pks_system_prewrite = await self.pks.learn_before_response(
                    user_id=user_id,
                    user_message=user_message,
                    memory_target="system",
                    source_message_id=source_message_id,
                )
            except Exception as e:
                logger.warning(f"[PKS] Failed pre-response bootstrap/write: {e}", exc_info=True)

        # Active memory verification: when the user questions whether something was
        # really saved, run search_pks NOW and inject the real results — the model
        # must answer from them, never from its own prior "salvei" in the history.
        verification_note: str | None = None
        if self.pks and user_id and _is_memory_verification_request(user_message):
            if self.settings.cognitive_tools_enabled:
                # Agentic path: nudge the brain to call search_pks itself.
                verification_note = _MEMORY_VERIFICATION_NUDGE
                if slog:
                    slog.log_event("Verificação de memória", "nudge → o cognitivo deve chamar `search_pks`")
            else:
                # Tool-less fallback: inject the real search results into the prompt.
                verification_note = await self._build_pks_verification_note(
                    user_message=user_message,
                    history=history,
                    decision=decision,
                    user_id=user_id,
                    slog=slog,
                )

        # Recall de conversas: pergunta sobre o passado não pode ser respondida de
        # cabeça (o cognitivo só vê ESTA sessão). Quando o gatilho determinístico
        # casa, a tool vira obrigatória no primeiro round — o mesmo mecanismo que
        # fez a skill `financas` finalmente ativar. Ver
        # docs/conversation-recall-plan.md §4/F5.
        recall_note: str | None = None
        recall_forced = False
        if (
            user_id
            and getattr(self.settings, "conversation_recall_enabled", True)
            and self.settings.cognitive_tools_enabled
            and not is_neo_training
        ):
            _cursor = await self._recall_cursor(session_id)
            if _is_recall_continuation(user_message) and _cursor:
                recall_note = (
                    f"{_RECALL_CONTINUATION_NUDGE}\n\n"
                    f"Busca anterior: query=\"{_cursor.get('query')}\", "
                    f"before=\"{_cursor.get('next_before')}\"."
                )
                recall_forced = True
                if slog:
                    slog.log_event(
                        "Recall — continuação",
                        f"'{user_message[:60]}' rejeitou a janela anterior → "
                        f"nova busca com before={_cursor.get('next_before')}",
                    )
            elif _is_conversation_recall_request(user_message):
                recall_note = _RECALL_NUDGE
                recall_forced = True
                if slog:
                    slog.log_event(
                        "Recall — gatilho",
                        "pergunta sobre conversa anterior → `conversation_recall` obrigatória",
                    )
            await self._touch_recall_cursor(session_id, keep=recall_forced)

        if slog:
            slog.log_context_recovery(recovered_context or [])
        if trace:
            trace.set_context_recovery(recovered_context or [])
        logger.info(
            "[HISTORY-DEBUG] recover_context returned %d results",
            len(recovered_context) if recovered_context else 0,
        )
        for ctx in recovered_context or []:
            logger.info(
                "[HISTORY-DEBUG]   memory: %s (score=%.3f)",
                ctx.content[:100],
                ctx.final_score,
            )

        # Build system prompt via PromptContextSelector (scored, budget-aware)
        pks_ms = 0.0
        t0_ctx = time.monotonic()
        if self.context_selector:
            system_prompt, context_trace = await self.context_selector.build_prompt(
                user_message=user_message,
                user_id=user_id,
                decision=decision,
                system_context=system_context,
                recovered_context=recovered_context or [],
                is_neo_training=is_neo_training,
                pks_prewrite=pks_prewrite,
                pks_system_prewrite=pks_system_prewrite,
                mission_context=None,
                register=register,
                birra_armed=self._birra_armed(session_id),
                # Caderno de trabalho: a nota tocada NESTA sessão sobe no índice.
                session_id=session_id,
            )
            pks_ms = round((time.monotonic() - t0_ctx) * 1000, 1)
            if slog:
                included = [c for c in context_trace if c.included and not c.pin]
                skipped = [c for c in context_trace if not c.included]
                slog.log_event(
                    "Context Selection",
                    f"Incluídos (scored): {len(included)} | Pulados: {len(skipped)}\n"
                    + "\n".join(
                        f"- [{c.layer.value}] {c.key}: score={c.final_score:.2f}"
                        + (f" ← {c.skip_reason}" if c.skip_reason else "")
                        for c in context_trace
                        if not c.pin
                    ),
                )
        else:
            # Fallback: legacy inline assembly (selector not wired)
            system_prompt = self._build_legacy_prompt(
                user_message=user_message,
                user_id=user_id,
                system_context=system_context,
                decision=decision,
                recovered_context=recovered_context or [],
                is_neo_training=is_neo_training,
                pks_prewrite=pks_prewrite,
                pks_system_prewrite=pks_system_prewrite,
                slog=slog,
            )
            pks_ms = round((time.monotonic() - t0_ctx) * 1000, 1)

        messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        # Clarification loop: allow ONE clarifying question when context is missing,
        # unless we already hit the round cap (then force a definitive answer).
        if allow_clarification:
            messages.append(Message(role=MessageRole.SYSTEM, content=_CLARIFICATION_INSTRUCTION))
        else:
            messages.append(Message(role=MessageRole.SYSTEM, content=_CLARIFICATION_FINAL_ROUND))

        # Phase 2: let the cognitive core hand off to a skill when it decides an
        # action must run (e.g. "se eu costumo, pode ligar" → home_assistant).
        # Music turns get the stricter music contract (token-or-NO_MUSIC) instead.
        if enable_handoff:
            if decision.skill_hint == "music":
                messages.append(Message(role=MessageRole.SYSTEM, content=_MUSIC_HANDOFF_INSTRUCTION))
            else:
                messages.append(
                    Message(
                        role=MessageRole.SYSTEM,
                        content=_build_handoff_instruction(
                            allow_self=self.settings.pipeline_self_handoff_enabled
                        ),
                    )
                )

        # Self-continuation: what the earlier steps of THIS turn already did. Built
        # from execution facts by the step runner, so the continuation plans against
        # results instead of retrying blind — the failure mode that made the brain
        # re-download the same broken URL.
        _work_note = (metadata or {}).get("pipeline_work_note")
        if _work_note:
            messages.append(
                Message(
                    role=MessageRole.SYSTEM,
                    content=(
                        "## O que você já fez neste turno\n"
                        "Você está CONTINUANDO um trabalho seu (não é uma mensagem nova "
                        "do usuário). Estes são os resultados reais dos passos "
                        "anteriores — não repita o que já deu certo, e não repita do "
                        "mesmo jeito o que falhou:\n\n"
                        f"{_work_note}"
                    ),
                )
            )
            if slog:
                slog.log_event("Continuação — nota de trabalho", _work_note)

        # Workbench: tell the brain it has its own sandbox to write/run code when no
        # specific tool fits. Training turns get no tools, so skip the note there too.
        if (
            self.settings.workbench_enabled
            and self.settings.cognitive_tools_enabled
            and not is_neo_training
        ):
            messages.append(Message(role=MessageRole.SYSTEM, content=_WORKBENCH_INSTRUCTION))
            # Arquivos sob iniciativa do cérebro: só quando as tools de atuação
            # estão no toolset (neo_files + neo_file_actuation), senão a instrução
            # apontaria pra tools que o modelo não tem.
            if (
                getattr(self.settings, "neo_files_enabled", False)
                and getattr(self.settings, "neo_file_actuation_enabled", False)
            ):
                messages.append(
                    Message(role=MessageRole.SYSTEM, content=_FILE_ACTUATION_INSTRUCTION)
                )

        # Skills library: catálogo de 1 linha por skill (nível 0 da progressive
        # disclosure) — o corpo só é pago quando o modelo chama skill_read. Sem
        # tools (flag off / treino) a nota seria uma promessa vazia, então segue o
        # mesmo gate do read set. Ver docs/skills-library-plan.md.
        _autoloaded_tools: list[Any] = []
        if (
            self.skill_library is not None
            and self.settings.skills_library_enabled
            and self.settings.cognitive_tools_enabled
            and not is_neo_training
        ):
            try:
                _skills_note = self.skill_library.catalog_note(user_message)
            except Exception as e:  # noqa: BLE001 — catálogo nunca derruba o turno
                logger.warning("[SkillLibrary] catalog_note falhou: %s", e)
                _skills_note = None
            if _skills_note:
                messages.append(Message(role=MessageRole.SYSTEM, content=_skills_note))

            # Match duro de keyword numa skill tool_backed (resolvido antes do PKS):
            # carrega o corpo e destrava as tools JÁ neste round, em vez de torcer
            # para o modelo chamar skill_read (ele às vezes responde de memória e
            # inventa que registrou). Sem match, a disclosure normal segue igual.
            if _autoload is not None:
                _entry, _body, _autoloaded_tools = _autoload
                messages.append(
                    Message(
                        role=MessageRole.SYSTEM,
                        content=self.skill_library.autoload_note(_entry, _body),
                    )
                )
                if slog:
                    slog.log_event(
                        "Skill autocarregada",
                        f"`{_entry.name}` (match de keyword) — tools já disponíveis: "
                        + ", ".join(f"`{t.name}`" for t in _autoloaded_tools)
                        + f".\nPKS não aprende este turno: o dado é da skill "
                        f"`{_entry.name}`, não da memória semântica.",
                    )

        # Real-time view of what's open/playing — injected on ANY cognitive turn when
        # media is open, so the brain can control it ("pausa", "próxima") AND answer
        # status questions ("o que tá tocando?", "como tá o YouTube?") that don't route
        # to the music skill. Empty when nothing is open. See neo-music-feature.
        _media_note = self._build_media_context_note(metadata)
        if _media_note:
            messages.append(Message(role=MessageRole.SYSTEM, content=_media_note))

        # Real-time Home Assistant snapshot — injected ONLY when the message refers
        # to a known HA device (matched against a cached device-name index), so the
        # brain answers device status from live ground-truth instead of the stale
        # PKS "devices" rows, and never touches HA for unrelated turns ("liga o
        # computador"). See plan: shimmying-growing-turing.
        _ha_note = await self._build_home_assistant_context_note(user_message)
        if _ha_note:
            messages.append(Message(role=MessageRole.SYSTEM, content=_ha_note))

        # Active PKS verification results (real search output) — overrides any
        # claim made earlier in the conversation history.
        if verification_note:
            messages.append(Message(role=MessageRole.SYSTEM, content=verification_note))

        # Pergunta sobre conversa passada → obriga a busca no arquivo.
        if recall_note:
            messages.append(Message(role=MessageRole.SYSTEM, content=recall_note))

        # ── Device identity: deterministic registration (no LLM transcription) ──
        # When the user explicitly asks to remember THIS device, persist its real
        # identity (device_id/hardware_id/OS from connection metadata) ourselves —
        # never relying on the weak chat model to copy a UUID — and feed those exact
        # values into the prompt so the confirmation is truthful, not fabricated.
        device_memory_handled = False
        if (
            self.pks
            and user_id
            and self._references_current_device(user_message)
            and IntentRouter._is_memory_directive(user_message)
        ):
            device_ctx = self._build_current_device_context(metadata)
            device_id = (metadata or {}).get("device_id")
            if device_ctx and device_id:
                label = self._extract_device_label(user_message)
                aliases = [a for a in (label, "meu computador", "esse dispositivo") if a]
                try:
                    await self.pks.register_device(
                        user_id=user_id,
                        device_id=device_id,
                        hardware_id=(metadata or {}).get("hardware_id"),
                        os_platform=(metadata or {}).get("os_platform"),
                        os_arch=(metadata or {}).get("os_arch"),
                        device_type=(metadata or {}).get("client"),
                        label=label,
                        aliases=aliases,
                        source_message_id=source_message_id,
                    )
                    device_memory_handled = True
                    if slog:
                        slog.log_event("Device registrado (PKS, determinístico)", device_ctx)
                except Exception as e:
                    logger.warning(f"[PKS] Deterministic device registration failed: {e}", exc_info=True)

                instruction = (
                    f"{device_ctx}\n"
                    "Este dispositivo JÁ foi registrado no PKS com EXATAMENTE estes dados. "
                    "Ao confirmar para o usuário, use estes valores reais (especialmente o "
                    "device_id) e NÃO invente atributos que o usuário não informou "
                    "(RAM, armazenamento, modelo exato). Se algum dado não foi informado, "
                    "simplesmente não o cite."
                )
                messages.append(Message(role=MessageRole.SYSTEM, content=instruction))

        # Add conversation history (already includes current user message since
        # add_message is called before get_history)
        for msg in history:
            role = MessageRole(msg["role"]) if msg["role"] in ("user", "assistant") else MessageRole.USER
            messages.append(Message(role=role, content=msg["content"]))

        # Only append current message if it wasn't already the last in history
        # (it will be there because add_message runs before get_history)
        last_in_history = history[-1] if history else None
        if not last_in_history or last_in_history.get("content") != user_message or last_in_history.get("role") != "user":
            messages.append(Message(role=MessageRole.USER, content=user_message))

        # Log the exact conversation payload being sent to the LLM
        history_count = len(messages) - 1  # subtract system message
        logger.info(f"[HISTORY-DEBUG] Total messages in LLM payload: {len(messages)} (1 system + {history_count} conversation)")
        for i, m in enumerate(messages):
            role_val = m.role.value if hasattr(m.role, "value") else str(m.role)
            logger.info(f"[HISTORY-DEBUG]   LLM[{i}] {role_val}: {(m.content or '')[:120]}")

        # Stream response
        produced_files: list[dict[str, Any]] = []
        tool_trace: list[dict[str, Any]] = []
        # Tools que MEXERAM no navegador do usuário neste turno (nome efetivo,
        # pós-portão, e só quando a ordem foi aceita). Nasce aqui, fora do ramo
        # `use_tools`, porque quem consome é o pós-processamento do handoff lá
        # embaixo — ver `_drop_music_handoff_after_browser`.
        browser_actuation: list[str] = []
        try:
            _req_model = (metadata or {}).get("model")
            _req_provider = (metadata or {}).get("provider")
            if _req_provider:
                provider = self.llm.get_provider(_req_provider)
            else:
                provider = self.llm.get_provider_for_model(_req_model)
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = _req_model or getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')

            t0 = time.monotonic()
            llm_ms = 0.0  # defined up front so a failed/caught LLM call can't crash the timing log
            # Cognitive core may read/verify ground truth inline (search_pks,
            # devices, weather) via a bounded tool loop — never to act. Training
            # turns never need tools.
            use_tools = self.settings.cognitive_tools_enabled and not is_neo_training
            if use_tools:
                read_executor = CognitiveReadExecutor(
                    global_executor=self.agent.tool_executor,
                    db=self.db,
                    pks_service=self.pks,
                    user_id=user_id,
                    # `conversation_recall` precisa da sessão ATUAL para se
                    # excluir do resultado (senão a própria pergunta casa com a
                    # busca) e para guardar o cursor do "foi antes ainda".
                    session_id=session_id,
                    embeddings=getattr(self.sessions, "embeddings", None),
                    # Identidade REAL da conexão: é o que permite "abaixa o
                    # volume", sem citar aparelho, significar ESTE aparelho
                    # (docs/device-control-plan.md §5).
                    current_device_id=(metadata or {}).get("device_id"),
                    # Cadastros generativos: o registry é o singleton do app (o
                    # cache das definições é compartilhado); o executor por
                    # request é que carrega o dono das linhas.
                    dataset_registry=getattr(self, "dataset_registry", None),
                    # Inscrições de evento: o roteador é o singleton do app, e o
                    # `media_context` é o que dá à tool o ALVO concreto (a aba em
                    # que o Neo acabou de tocar) sem o modelo ter que adivinhar
                    # tabId — a defesa contra inscrição no vazio.
                    event_router=getattr(self, "event_router", None),
                    media_context=_media_context_of(metadata),
                )
                # The cognitive core's toolset = read tools + (when enabled) its own
                # agent tools: the workbench sandbox (neo_*, dispatched through the
                # global executor) and background jobs (neo_bash_background/job_*,
                # which need the turn's user/session — routed to a per-request
                # JobToolExecutor via the composite below).
                cognitive_tools = list(cognitive_tools_for(user_id))
                # Tools da skill autocarregada (match de keyword) entram já no
                # round 1 — o gating por skill_read continua valendo para as outras.
                # Não filtramos duplicata aqui: as camadas se sobrepõem em ordens
                # que este ponto não enxerga (a skill pode listar uma tool que só
                # chega depois, em `_agent_action_tools()`). Quem garante um nome
                # só é o `_dedup_tools_by_name` no fim da montagem.
                cognitive_tools += _autoloaded_tools
                if _autoloaded_tools:
                    # Com a skill ativa, o search_pks é só uma tentação: o modelo ia
                    # procurar o lançamento na memória semântica (onde ele nunca
                    # esteve), não achava e mesmo assim dizia "registrei" (d4e2ba0f).
                    # A skill é a fonte de verdade do turno — a memória sai da mesa.
                    cognitive_tools = [t for t in cognitive_tools if t.name != "search_pks"]
                cog_executor: Any = read_executor
                if self.settings.workbench_enabled:
                    cognitive_tools += self._agent_action_tools()
                    cog_executor = self._with_job_tools(
                        read_executor,
                        user_id=user_id,
                        session_id=session_id,
                        can_show_images=self._client_renders_inline_images(metadata),
                        user_message=user_message,
                        metadata=metadata,
                    )
                # Fim da montagem: read set + skill + action tools já empilhados.
                # É aqui que o toolset vira "um nome, um schema" — antes disso as
                # camadas não têm como se enxergar.
                cognitive_tools = _dedup_tools_by_name(cognitive_tools)
                # Turno de música: o bloco [[HANDOFF:music]] é o atuador; as
                # tools de navegador/terminal saem da mesa (ver o helper).
                cognitive_tools = _drop_music_turn_temptations(
                    cognitive_tools, decision.skill_hint, user_message
                )
                _skill_tool_names = {t.name for t in _autoloaded_tools}
                _used_tools: set[str] = set()
                full_response = await self._run_cognitive_tool_loop(
                    provider=provider,
                    messages=messages,
                    req_model=_req_model,
                    model_name=model_name,
                    provider_name=provider_name,
                    tools=cognitive_tools,
                    tool_executor=cog_executor,
                    slog=slog,
                    trace=trace,
                    force_tool_first=bool(_autoloaded_tools) or recall_forced,
                    skill_tool_names=_skill_tool_names,
                    used_tools=_used_tools,
                    produced_files=produced_files,
                    tool_trace=tool_trace,
                    browser_actuation=browser_actuation,
                )
                llm_ms = (time.monotonic() - t0) * 1000
                # Usou a skill de verdade → o assunto está vivo: renova o prazo dela
                # mesmo que a próxima mensagem seja um "sim" sem keyword nenhuma.
                if _autoload is not None and (_used_tools & _skill_tool_names):
                    await self._touch_active_skill(session_id, renew=_autoload[0].name)
            else:
                if slog:
                    slog.log_llm_request("COGNITIVE", messages, model=model_name, provider=provider_name, temperature=0.9)
                full_response = ""
                async for chunk in provider.stream_complete(
                    messages=messages,
                    model=_req_model or None,
                    max_tokens=self.settings.default_max_tokens,
                ):
                    if chunk.event == StreamEventType.TOKEN and chunk.content:
                        full_response += chunk.content
                    elif chunk.event == StreamEventType.ERROR:
                        logger.error(f"LLM stream error: {chunk.content}")
                        break
                # Mesmo buraco do tool-loop: o stream pode terminar VAZIO quando o
                # raciocínio do DeepSeek consome o max_tokens (finish_reason=length).
                # Retenta UMA vez, bufferizado, com o dobro do teto antes de aceitar
                # o vazio (que viraria "não consegui gerar uma resposta").
                if not full_response.strip():
                    bigger = min(
                        self.settings.default_max_tokens * 2,
                        _EMPTY_RETRY_MAX_TOKENS_CEILING,
                    )
                    logger.warning(
                        "[Cognitive] stream vazio (orçamento consumido no "
                        "raciocínio) — retentando com max_tokens=%d", bigger,
                    )
                    _retry_resp = await provider.complete(
                        messages=messages,
                        tools=None,
                        model=_req_model or None,
                        temperature=0.9,
                        max_tokens=bigger,
                    )
                    full_response = _retry_resp.get_content() or ""
                llm_ms = (time.monotonic() - t0) * 1000
                if slog:
                    slog.log_llm_response("COGNITIVE", full_response, duration_ms=llm_ms, model=model_name)
                if trace:
                    messages_payload: list[dict] = []
                    for m in messages:
                        role_val = m.role.value if hasattr(m.role, 'value') else str(m.role)
                        content = m.content if m.role == MessageRole.SYSTEM else (m.content or '')[:500]
                        messages_payload.append({"role": role_val, "content": content})
                    trace.add_llm_call(
                        provider=provider_name, model=model_name,
                        messages_sent=messages_payload,
                        response_text=(full_response or '')[:1000],
                        duration_ms=round(llm_ms, 2), temperature=0.9, stage="cognitive",
                    )

            if not full_response:
                full_response = "Desculpe, não consegui gerar uma resposta."

            # Rede de segurança pós-loop: mesmo com as guardas DENTRO do loop, a
            # resposta final ainda pode sair errada de dois jeitos sem produzir
            # nada de fato: (a) promessa futura — "vou gerar o zip... aguenta um
            # segundo" (sessão 8f0cd0fa) — ou (b) afirmação de entrega pronta sem
            # nunca ter chamado file_save — "está pronto, pode baixar pelo botão
            # abaixo" (sessão 95ccd52d: só neo_write_file rodou, file_save nunca).
            # Nenhuma das guardas de dentro do loop pega (b): não é mentira sobre
            # AÇÃO passada (verbos de _claims_action_without_tool) nem promessa no
            # futuro. Em vez de confiar cegamente no texto, força mais UMA passada.
            _lying_about_delivery = _promises_pending_action(full_response) or (
                _claims_downloadable_delivery(full_response)
            )
            if use_tools and not produced_files and _lying_about_delivery:
                logger.warning(
                    "[Cognitive] resposta final alegou entrega sem produzir "
                    "arquivo — forçando segunda passada"
                )
                if slog:
                    slog.log_event(
                        "Anti-promessa (pós-loop)",
                        "A resposta final alegava entrega pronta/prometida sem ter "
                        f"produzido nada — forçando mais uma passada.\n\n> {full_response[:200]}",
                    )
                messages.append(Message(role=MessageRole.ASSISTANT, content=full_response))
                messages.append(
                    Message(
                        role=MessageRole.USER,
                        content=(
                            "PARE: você disse que o arquivo está pronto/vai ficar "
                            "pronto, mas nenhum file_save rodou — nada foi entregue de "
                            "verdade e a frase é falsa. Não repita esse texto. Continue "
                            "AGORA chamando as tools (inclusive file_save) até a "
                            "entrega estar pronta de verdade."
                        ),
                    )
                )
                full_response = await self._run_cognitive_tool_loop(
                    provider=provider, messages=messages, req_model=_req_model,
                    model_name=model_name, provider_name=provider_name,
                    tools=cognitive_tools, tool_executor=cog_executor,
                    slog=slog, trace=trace, force_tool_first=True,
                    skill_tool_names=_skill_tool_names, used_tools=_used_tools,
                    produced_files=produced_files, tool_trace=tool_trace,
                    browser_actuation=browser_actuation,
                )
                if not full_response:
                    full_response = "Desculpe, não consegui gerar uma resposta."

        except ClientActuationRequested as car:
            # O cérebro pediu uma ação de arquivo NO CLIENTE (puxar/entregar). Emite
            # o tool_request e encerra ESTE turno cognitivo; o desktop executa e o
            # turno retoma em process_tool_result. Estado para a retomada fica em
            # _pending_actuations (o gRPC só encaminha user_message/command/etc.).
            async for resp in self._emit_client_actuation(
                car,
                session_id=session_id,
                user_message=user_message,
                user_id=user_id,
                metadata=metadata,
                trace=trace,
                slog=slog,
                depth=_actuation_depth,
            ):
                yield resp
            return
        except Exception as e:
            # Falha de provider NÃO é resposta. Antes o texto cru do erro virava
            # `full_response` e seguia o fluxo normal: era salvo como a fala do
            # assistant, reinjetado no histórico do turno seguinte E mandado pro
            # extrator de preferências e pro PKS planner como se fosse conteúdo
            # (visto na sessão 1aec71b8, onde um 400 do DeepSeek virou "resposta"
            # e foi parar nos dois pipelines de memória). Fecha o turno com uma
            # frase curta e humana; o erro real fica no log e no slog.
            logger.exception("Direct response LLM call failed")
            async for resp in self._fail_turn(session_id, e, user_id=user_id, slog=slog):
                yield resp
            return

        # Clarification loop: the model signalled it lacks context and asked a
        # question. Persist the pending state and return the question — the user's
        # next message will resolve it in a second LLM iteration.
        clar_question = _extract_clarification_question(full_response) if allow_clarification else None
        if clar_question:
            try:
                sess = await self.db.get_conversation_session(session_id)
                if sess is not None:
                    sess.extra_metadata = {
                        **_session_meta(sess),
                        "pending_clarification": {
                            "original_message": user_message,
                            "clarifying_question": clar_question,
                            "round": clarification_round,
                            "created_at": datetime.utcnow().isoformat(),
                        },
                    }
                    await self.db.update_session(sess)
            except Exception as e:
                logger.warning(f"[CLARIFY] Failed to persist pending clarification: {e}")
            await self.sessions.add_message(
                session_id=session_id,
                role="assistant",
                content=clar_question,
                generate_embedding=False,
                user_id=user_id,
            )
            if slog:
                slog.log_event(
                    "Loop de Clarificação",
                    f"Round {clarification_round}: pedindo esclarecimento ao usuário.\n\n> {clar_question}",
                )
                slog.log_final_response(clar_question, "COGNITIVE", iterations=1)
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE,
                session_id=session_id,
                message=clar_question,
                is_final=True,
                iterations=1,
                metadata={"needs_clarification": True, "round": clarification_round},
            )
            return

        # Safety: never leak a raw sentinel to the user (e.g. the model emitted it
        # on the final round, where clarification is disabled).
        if NEED_CLARIFICATION_SENTINEL in full_response:
            full_response = full_response.replace(NEED_CLARIFICATION_SENTINEL, "").lstrip(":- \n").strip()
            if not full_response:
                full_response = "Pode me dar um pouco mais de detalhe sobre o que você quer saber?"

        # Pipeline handoff (Phase 2): pull any [[HANDOFF:skill]] block out of the
        # reply. The block is always stripped (never leaks); the parsed handoffs are
        # only acted on when this turn is allowed to chain. ``full_response`` is now
        # the clean user-facing text — saved, yielded, and fed to the PKS planner.
        pipeline_handoffs: list[Handoff] = []
        _allow_self = self.settings.pipeline_self_handoff_enabled
        if enable_handoff:
            full_response, pipeline_handoffs = extract_handoffs(
                full_response, allow_self=_allow_self
            )
            if pipeline_handoffs and not full_response.strip():
                # Model emitted only the directive — give a short acknowledgement.
                full_response = "Ok, já cuido disso."
            if pipeline_handoffs and slog:
                slog.log_event(
                    "Pipeline Handoff",
                    "\n".join(
                        f"- → {'continuar (eu mesmo)' if h.is_self else (h.target_skill or h.target_intent.value)}: {h.input_text}"
                        for h in pipeline_handoffs
                    ),
                )
        else:
            # Defensive: strip a stray block even when handoffs are disabled.
            full_response, _ = extract_handoffs(full_response, allow_self=False)

        # Music reliability (neo-music-feature): a music turn must resolve to either a
        # [[HANDOFF:music]] (already extracted above) or an explicit [[NO_MUSIC]]. Strip
        # the NO_MUSIC token; if NEITHER was emitted, a cheap verifier decides whether
        # the reply intended to play and synthesizes the handoff so it never no-ops.
        if decision.skill_hint == "music" and enable_handoff:
            had_no_music = "[[NO_MUSIC]]" in full_response or "[[NO MUSIC]]" in full_response
            full_response = full_response.replace("[[NO_MUSIC]]", "").replace("[[NO MUSIC]]", "").strip()
            # Dupla atuação: se o PRÓPRIO turno já mandou o navegador do usuário
            # abrir/tocar, o handoff de música é a SEGUNDA aba. O navegador vence —
            # é o que o usuário pediu pra ver acontecer, já está rodando e não dá
            # pra cancelar do meio. Isto também desarma o verificador logo abaixo:
            # sem a trava ele SINTETIZARIA o handoff que o modelo (certo) não emitiu.
            _browser_played = bool(browser_actuation)
            _kept = _drop_music_handoff_after_browser(pipeline_handoffs, _browser_played)
            if len(_kept) != len(pipeline_handoffs):
                logger.info(
                    "[Music] handoff descartado — o turno já atuou no navegador "
                    "do usuário (%s)", ", ".join(browser_actuation),
                )
                if slog:
                    slog.log_event(
                        "Música — handoff descartado (dupla atuação)",
                        "O turno já mandou o navegador do usuário abrir/tocar "
                        f"({', '.join(browser_actuation)}); o [[HANDOFF:music]] abriria "
                        "uma SEGUNDA aba tocando outra coisa. A atuação pelo "
                        "navegador vence.",
                    )
                pipeline_handoffs = _kept
            already_music = any(
                h.target_skill == "music" or h.target_intent == IntentType.MUSIC
                for h in pipeline_handoffs
            )
            if (
                not already_music
                and not had_no_music
                and not _browser_played
                and self.settings.music_handoff_verifier_enabled
            ):
                synth = await self._verify_music_decision(user_message, full_response)
                if synth is not None:
                    pipeline_handoffs.append(synth)
                    if slog:
                        slog.log_event("Music Handoff (verifier)", f"→ music: {synth.input_text}")
            if not full_response.strip():
                full_response = "Ok 🎵"

        # Self-continuation backstop. The prompt contract is not enough on its own:
        # on the first E2E run the instruction was in the payload verbatim and the
        # model still closed on "vou baixar…" with no block. Same shape as the music
        # verifier — cheap, temperature 0, and gated behind a phrase pre-filter so an
        # ordinary turn pays nothing. A promise the model refuses to back with an
        # order becomes the order.
        if (
            enable_handoff
            and _allow_self
            and decision.skill_hint != "music"
            and not any(h.is_self for h in pipeline_handoffs)
        ):
            # O verificador PRECISA saber o que este turno já despachou. Medido em
            # prod (session-5102534a): a resposta foi "Vou pegar uma pedrada do teu
            # mix." + [[HANDOFF:music]]tocar Slipknot - Duality — a ação estava
            # despachada, e o verificador, que só via o texto (o bloco já foi
            # extraído acima), leu "vou pegar" como promessa pendente e sintetizou
            # "baixar a música do mix". O turno virou um download que ninguém pediu.
            # Não dá para simplesmente pular quando há handoff: "vou tocar X E te
            # mando a letra" tem promessa de verdade sobrando. Então informa.
            dispatched = [
                f"{h.target_skill or h.target_intent.value}: {h.input_text}"
                for h in pipeline_handoffs
                if not h.is_self
            ]
            synth = await self._verify_promise_continuation(
                user_message,
                full_response,
                (metadata or {}).get("pipeline_work_note"),
                dispatched,
            )
            if synth is not None:
                pipeline_handoffs.append(synth)
                if slog:
                    slog.log_event(
                        "Continuação (verificador de promessa)",
                        f"A resposta prometeu trabalho sem emitir o bloco — "
                        f"sintetizado: {synth.input_text}",
                    )

        # Voice imitation: pull any [[VOICE:name]]…[[/VOICE]] spans out of the reply
        # into ordered speech segments so the renderer switches TTS voice per span.
        # The block is always stripped from `full_response` (the saved/displayed
        # text); names are resolved against the coqui catalog. No tag → no-op.
        full_response, _parsed_segments = extract_speech_segments(full_response)
        speech_segments = await self._resolve_speech_segments(_parsed_segments)
        if speech_segments and slog:
            slog.log_event(
                "Imitação de Vozes",
                "\n".join(
                    f"- {'[default]' if not s['voice'] else s['voice'].get('label') or s['voice'].get('voice')}: {s['text']}"
                    for s in speech_segments
                ),
            )

        # Save assistant response
        t0_save = time.monotonic()
        _msg_metadata: dict[str, Any] = {}
        if produced_files:
            _msg_metadata["files"] = produced_files
        if tool_trace:
            # Sem o `note_detail`: este trace é reinjetado no contexto de TODO
            # turno seguinte. O campo farto é do caderno, não do prompt.
            _msg_metadata["tool_trace"] = _strip_note_detail(tool_trace)
        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=full_response,
            generate_embedding=False,
            user_id=user_id,
            metadata=_msg_metadata or None,
        )
        save_ms = round((time.monotonic() - t0_save) * 1000, 1)
        logger.info(f"[TIMING-DIRECT] recover={recover_ms}ms prefs={prefs_ms}ms pks={pks_ms}ms llm={llm_ms:.0f}ms save_assistant={save_ms}ms")
        if slog:
            slog.record_step_timing("Recuperação de Contexto", recover_ms)
            slog.record_step_timing("Preferências do Usuário", prefs_ms)
            slog.record_step_timing("PKS", pks_ms)
            slog.record_step_timing("LLM (resposta)", round(llm_ms, 1))
            slog.record_step_timing("Salvar resposta (DB)", save_ms)
            slog.log_message_saved("assistant", full_response, str(session_id))
            slog.log_final_response(full_response, "COGNITIVE", iterations=1)

        # Todo handler de step encerra com um is_final próprio, então adiar o
        # final aqui não deixa o turno sem terminador. Ver a política.
        _deferred_final = handoff_defers_final(pipeline_handoffs)

        # Yield final response FIRST — background tasks must never block the user-facing round-trip.
        # Num handoff esta resposta não é o final do stream (o step da skill vem depois), mas É uma
        # mensagem real do usuário — marca persist_reply para o desktop renderizar como bolha
        # própria (não como "pensando" transitório) e mostrar a confirmação da skill como a segunda.
        _resp_metadata: dict[str, Any] = {}
        if _deferred_final:
            _resp_metadata["persist_reply"] = True
        if produced_files:
            _resp_metadata["files"] = produced_files
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=session_id,
            message=full_response,
            is_final=not _deferred_final,
            iterations=1,
            metadata=_resp_metadata or None,
            segments=speech_segments,
        )

        # Context handed to the PKS memory planner. When the user talks about the
        # current device ("esse dispositivo é um MacBook Air"), inject its real
        # identity (device_id/hardware_id/model from the connection metadata) so
        # the planner can link the stated facts to the canonical device entity.
        pks_context = ""
        if self._references_current_device(user_message):
            device_note = self._build_current_device_context(metadata)
            if device_note:
                pks_context = device_note
                if slog:
                    slog.log_event("Dispositivo Atual (contexto PKS)", device_note)

        # True fire-and-forget: schedule as background tasks AFTER yielding the response
        async def _bg_prefs() -> None:
            if not self.preferences:
                return
            # Neo training never becomes a personal user preference.
            if is_neo_training:
                logger.info("[PREF] Skipping preference extraction for Neo training message")
                return
            try:
                extracted = await self.preferences.extract_and_save(
                    user_message=user_message,
                    assistant_response=full_response,
                    user_id=user_id,
                )
                if extracted and slog:
                    import json as _json
                    slog.log_event(
                        "Preferências Extraídas",
                        f"```json\n{_json.dumps(extracted, ensure_ascii=False, indent=2)}\n```",
                    )
            except Exception as e:
                logger.warning(f"[PREF] Post-response extraction failed: {e}")

        async def _bg_pks() -> None:
            if not (self.pks and user_id and full_response):
                return
            try:
                memory_source_response = full_response
                use_prior_assistant_artifact = self._is_memory_confirmation(user_message)
                if use_prior_assistant_artifact:
                    artifact = self._find_memory_artifact(history, full_response)
                    if artifact:
                        memory_source_response = artifact

                recent_history_lines = []
                for msg in history[-(6 + 1):-1] if len(history) > 1 else []:
                    role_label = "Usuário" if msg.get("role") == "user" else "Assistente"
                    raw_content = msg.get("content") or ""
                    content_preview = raw_content if use_prior_assistant_artifact else raw_content[:500]
                    recent_history_lines.append(f"[{role_label}]: {content_preview}")
                recent_history_str = "\n".join(recent_history_lines)

                # Neo training is routed only through the system/agent-training
                # pipeline — never written as personal user memory.
                # When this turn was a device-registration directive, the device
                # identity was already written deterministically; skip the LLM
                # planner so it can't re-ingest a hallucinated id/specs from the
                # assistant's reply.
                # Se o cognitivo já chamou pks_remember/pks_forget neste turno, a
                # gravação user-scope explícita É a fonte de verdade do turno — a
                # extração automática aqui só duplicaria/atrapalharia
                # (session-b7e3985d: a tool atualizou a entidade canônica e o
                # _bg_pks criou uma 2ª quase-idêntica; num "esquece" ela tentaria
                # re-salvar o que acabou de ser removido).
                pks_tool_used = any(
                    (e or {}).get("tool") in ("pks_remember", "pks_forget")
                    for e in tool_trace
                )
                # Fase 2 — backstop determinístico (docs/pks-write-on-demand-plan.md
                # §Fase 2): o usuário emitiu uma diretiva de memória CLARA ("salva
                # essa ideia", "coloca nas anotações") mas o cognitivo NÃO chamou
                # pks_remember. A extração best-effort (confirmed_save=False) poderia
                # devolver [] achando que é "sugestão do assistente". Então aqui a
                # gente FORÇA o modo confirmado — a rede de segurança que garante que
                # o pedido explícito não caia no vazio. Só ADICIONA cobertura; quando
                # a tool rodou (pks_tool_used) este bloco nem executa.
                force_save = (
                    IntentRouter._is_memory_directive(user_message) and not pks_tool_used
                )
                applied = []
                if (
                    not is_neo_training
                    and not device_memory_handled
                    and not skill_owns_turn
                    and not pks_tool_used
                ):
                    if force_save:
                        logger.info(
                            "[PKS] Fase-2 backstop: diretiva de memória sem pks_remember "
                            "no turno — forçando save confirmado."
                        )
                    applied = await self.pks.learn(
                        user_id=user_id,
                        user_message=user_message,
                        assistant_response=memory_source_response,
                        memory_target="user",
                        pks_context=pks_context,
                        keywords=decision.knowledge_keywords,
                        recent_history=recent_history_str,
                        recent_messages=history[-12:],
                        source_message_id=source_message_id,
                        confirmed_save=force_save,
                    )
                system_applied = await self.pks.learn(
                    user_id=user_id,
                    user_message=user_message,
                    assistant_response=memory_source_response,
                    memory_target="system",
                    pks_context="",
                    keywords=None,
                    recent_history=recent_history_str,
                    recent_messages=history[-12:],
                    source_message_id=source_message_id,
                )
                if applied and slog:
                    import json as _json
                    slog.log_event(
                        "PKS Atualizado",
                        f"```json\n{_json.dumps(applied, ensure_ascii=False, indent=2)}\n```",
                    )
                if system_applied and slog:
                    import json as _json
                    slog.log_event(
                        "PKS Sistema Atualizado",
                        f"```json\n{_json.dumps(system_applied, ensure_ascii=False, indent=2)}\n```",
                    )
            except Exception as e:
                logger.warning(f"[PKS] Post-response write failed: {e}", exc_info=True)

        async def _bg_working_note() -> None:
            """Piso da memória de trabalho (docs/working-memory-plan.md §7).

            O caminho bom é o modelo chamar `wm_write` com a conclusão redigida.
            Este é o piso para quando ele não chama: turno que rodou tool PESADA
            (leu arquivo, rodou script, pesquisou) e não anotou nada ganha um
            append determinístico com o que foi executado. Sem LLM, sem custo —
            a lição de `force_tool_first` e do backstop do `pks_remember` é que
            instrução de prompt sozinha falha em ~metade dos turnos a 0.9 de
            temperatura, e aqui o preço da falha é refazer o trabalho inteiro.
            """
            if not (
                self.settings.working_memory_enabled
                and getattr(self.settings, "working_memory_autonote_enabled", True)
                and user_id
            ):
                return
            entry = _autonote_entry(tool_trace, produced_files)
            if not entry:
                return
            try:
                from core.working_memory import WorkingMemoryStore

                store = WorkingMemoryStore(
                    db=self.db,
                    max_note_bytes=self.settings.working_memory_note_max_bytes,
                    ttl_days=self.settings.working_memory_ttl_days,
                    max_index_notes=self.settings.working_memory_max_notes_in_index,
                )
                note = await store.append_auto(
                    user_id=user_id, session_id=session_id, entry=entry
                )
                if note and slog:
                    slog.log_event(
                        "Memória de Trabalho (automática)",
                        f"[[{note.slug}]] entrada {note.entry_count}\n{entry}",
                    )
            except Exception as e:  # noqa: BLE001 — caderno nunca derruba o turno
                logger.warning("[WorkingMemory] append automático falhou: %s", e)

        # Affect is NOT scheduled here: process_user_input's epilogue is the
        # single choke point (covers this path plus skills/HA/tools/WhatsApp
        # outbound), with dedup by source_message_id in the AffectUpdater.
        self._spawn_bg(_bg_prefs(), name="bg-prefs")
        self._spawn_bg(_bg_pks(), name="bg-pks")
        self._spawn_bg(_bg_working_note(), name="bg-working-note")

        # Internal control item: hand the parsed handoffs back to the orchestrator.
        # ``message=None`` + the private metadata key make it non-user-facing; the
        # step runner consumes it and never forwards it to the client.
        # ``__pipeline_work__`` carries the raw material for the continuation's work
        # note (what this step really did); the step runner formats it, since only
        # it knows the step's position in the chain.
        if pipeline_handoffs:
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE,
                session_id=session_id,
                message=None,
                is_final=False,
                metadata={
                    "__pipeline_handoffs__": pipeline_handoffs,
                    "__pipeline_work__": {
                        "reply": full_response,
                        "tool_trace": tool_trace,
                        "files": produced_files,
                    },
                },
            )

    def _agent_action_tools(self) -> list[Any]:
        """The Neo's own agent tools offered inline to the cognitive core.

        F1: workbench sandbox (``neo_*``). F2 adds background-job tools and F4 the
        scheduler (``job_*``/``schedule_*``) — appended here as they land. Guarded
        by ``workbench_enabled`` at the call site; each group is only included when
        its module executor is actually registered in the global tool executor.
        """
        tools: list[Any] = []
        try:
            from core.workbench import WORKBENCH_TOOLS

            if self.agent.tool_registry.exists("neo_bash"):
                tools += list(WORKBENCH_TOOLS)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Workbench] could not load workbench tools: %s", e)
        if self.job_manager is not None:
            try:
                from core.jobs import JOB_TOOLS

                if self.agent.tool_registry.exists("neo_bash_background"):
                    job_tools = list(JOB_TOOLS)
                    # deep_research exige web_search + research ligados; sem eles a
                    # tool seria uma promessa vazia no toolset.
                    if not (self.settings.research_enabled and self.settings.web_search_enabled):
                        job_tools = [t for t in job_tools if t.name != "deep_research"]
                    # Mesmo critério para browser_task (container neo-browser-agent).
                    if not self.settings.browser_agent_enabled:
                        job_tools = [t for t in job_tools if t.name != "browser_task"]
                    # E para o navegador LOCAL (extensão na máquina do usuário):
                    # oferecer a tool com a flag desligada seria pura tentação —
                    # o portão rebaixaria toda chamada para o servidor.
                    if not getattr(self.settings, "browser_local_enabled", False):
                        job_tools = [t for t in job_tools if t.name != "browser_local_task"]
                    tools += job_tools
            except Exception:  # noqa: BLE001
                pass
        return tools

    async def _active_skill(self, session_id: UUID | None) -> str | None:
        """Skill que continua ATIVA nesta sessão (grudenta por alguns turnos).

        Sem isto a ativação seria por mensagem, e conversa é multi-turno: depois de
        "recebi 50 reais" (casa keyword), o "so salva" / "variados" / "sim" não casam
        nada — as tools da skill sumiam do turno e o modelo voltava a afirmar que
        tinha registrado sem registrar (sessão d4e2ba0f).
        """
        if session_id is None:
            return None
        try:
            sess = await self.db.get_conversation_session(session_id)
        except Exception as e:  # noqa: BLE001 — sessão nunca derruba o turno
            logger.warning("[SkillLibrary] leitura da skill ativa falhou: %s", e)
            return None
        if sess is None:
            return None
        active = (sess.extra_metadata or {}).get("active_skill") or {}
        name = active.get("name")
        turns_left = int(active.get("turns_left") or 0)
        if not name or turns_left <= 0:
            return None
        return str(name)

    async def _touch_active_skill(self, session_id: UUID | None, *, renew: str | None) -> None:
        """Renova a skill ativa (``renew`` = casou keyword / usou tool) ou gasta um turno.

        Sem o consumo, um "obrigado" depois de falar de dinheiro carregaria a skill
        para sempre; sem a renovação, ela morreria no meio do fluxo (o bug original).
        """
        if session_id is None:
            return
        try:
            sess = await self.db.get_conversation_session(session_id)
            if sess is None:
                return
            meta = dict(_session_meta(sess))
            current = dict(meta.get("active_skill") or {})
            if renew:
                meta["active_skill"] = {"name": renew, "turns_left": _SKILL_STICKY_TURNS}
            elif current.get("name"):
                turns_left = int(current.get("turns_left") or 0) - 1
                if turns_left > 0:
                    meta["active_skill"] = {**current, "turns_left": turns_left}
                else:
                    meta.pop("active_skill", None)
            else:
                return
            sess.extra_metadata = meta
            await self.db.update_session(sess)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SkillLibrary] persistência da skill ativa falhou: %s", e)

    async def _recall_cursor(self, session_id: UUID | None) -> dict[str, Any] | None:
        """Onde a última busca no arquivo de conversas parou, nesta sessão.

        É o que faz "não foi isso, foi antes ainda" continuar de onde parou em
        vez de repetir a mesma janela. Guardado pelo
        ``ConversationRecallExecutor``; expira em alguns turnos, como a skill
        ativa. Só vale se ainda houver página anterior para buscar.
        """
        if session_id is None:
            return None
        try:
            sess = await self.db.get_conversation_session(session_id)
        except Exception as e:  # noqa: BLE001 — cursor nunca derruba o turno
            logger.warning("[Recall] leitura do cursor falhou: %s", e)
            return None
        if sess is None:
            return None
        cursor = (sess.extra_metadata or {}).get("recall") or {}
        if int(cursor.get("turns_left") or 0) <= 0:
            return None
        if not cursor.get("next_before"):
            return None
        return dict(cursor)

    async def _touch_recall_cursor(self, session_id: UUID | None, *, keep: bool) -> None:
        """Gasta um turno do cursor (ou renova quando o recall segue em jogo).

        Sem o consumo, um "obrigado" três turnos depois ainda seria lido como
        continuação de busca.
        """
        if session_id is None:
            return
        try:
            sess = await self.db.get_conversation_session(session_id)
            if sess is None:
                return
            meta = dict(_session_meta(sess))
            current = dict(meta.get("recall") or {})
            if not current:
                return
            if keep:
                from modules.conversation_recall import CURSOR_TTL_TURNS

                current["turns_left"] = CURSOR_TTL_TURNS
                meta["recall"] = current
            else:
                turns_left = int(current.get("turns_left") or 0) - 1
                if turns_left > 0:
                    meta["recall"] = {**current, "turns_left": turns_left}
                else:
                    meta.pop("recall", None)
            sess.extra_metadata = meta
            await self.db.update_session(sess)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Recall] persistência do cursor falhou: %s", e)

    def _with_job_tools(
        self,
        base_executor: Any,
        *,
        user_id: UUID | None,
        session_id: UUID | None,
        can_show_images: bool = False,
        user_message: str = "",
        metadata: dict | None = None,
    ) -> Any:
        """Composite executor: job tools → JobToolExecutor; everything else → base.

        The base executor already delegates unknown names (like ``neo_bash``) to the
        global tool executor, so the workbench tools work through it. Only the
        background-job tools need the turn's user/session context, so they are routed
        to a fresh per-request :class:`JobToolExecutor`.

        Vale para os DOIS caminhos que executam tool. As job tools estão no registry
        GLOBAL como ``module="jobs"``/``server-side``, então o router as lista no
        catálogo de SERVER_TOOL e pode escolher ``browser_task`` — mas o executor
        global nunca ganha um module executor "jobs" (o JobToolExecutor é por
        request, precisa do user/session do turno). Sem este seam no SERVER_TOOL a
        chamada morria em "Executor para módulo server-side 'jobs' não registrado" e
        o turno respondia "Todas as ferramentas falharam".
        """
        if self.job_manager is None:
            return base_executor
        try:
            from core.jobs import JOB_TOOL_NAMES, JobToolExecutor
        except Exception:  # noqa: BLE001
            return base_executor
        job_executor = JobToolExecutor(
            db=self.db,
            job_manager=self.job_manager,
            user_id=user_id,
            session_id=session_id,
            # browser_screenshot guarda o print no catálogo e devolve o marcador.
            # `can_show_images` decide o que a tool MANDA o cérebro fazer com ele:
            # num cliente que não renderiza, o marcador viraria texto cru na tela.
            file_store=self.file_store,
            can_show_images=can_show_images,
            # O portão do navegador local decide pela FRASE do turno, não pelo
            # que o modelo achou dela (docs/browser-local-plan.md §5.2); e o
            # device_id diz em QUAL máquina abrir a aba.
            user_message=user_message,
            device_id=(metadata or {}).get("device_id"),
        )
        return _CognitiveAgentExecutor(base_executor, job_executor, JOB_TOOL_NAMES)

    def _server_tool_toolset(self) -> list[Any]:
        """As tools que o agente do SERVER_TOOL pode VER.

        Ele recebia ``tool_registry.get_all()`` — o registry inteiro. O
        ``_SKILL_OWNED_TOOL_PREFIXES`` esconde as tools gated do CLASSIFICADOR, mas
        não do agente que roda depois, e as duas coisas juntas produziram o pior
        desfecho possível: em 2026-09-09 o classificador mandou
        SERVER_TOOL/``list_connected_devices`` para "tem uma fila de musica criada
        né, me mostra o que tem nela", o agente ignorou a escolha dele, viu
        ``data_list`` no catálogo e chamou — mas nesse trilho as executoras
        user-scoped (datasets/events/finance) são as GLOBAIS, que nascem sem
        usuário. O turno respondeu "A ferramenta falhou — data_list precisa de um
        usuário identificado", depois de 4 iterações e 19s.

        Oferecer uma tool que este trilho não sabe executar é prometer o que não se
        cumpre. Quem destrava as gated é o ``skill_read`` do cognitivo, com o
        usuário do turno em mãos.

        As job tools são a exceção e ficam: elas casam o prefixo (``neo_``,
        ``deep_``, ``job_``), mas aqui existe executor por request para elas
        (``_with_job_tools``) — foi o conserto do mesmo defeito, uma camada antes.
        """
        from core.router.intent_router import server_tool_can_run

        return [t for t in self.agent.tool_registry.get_all() if server_tool_can_run(t.name)]

    async def _run_cognitive_tool_loop(
        self,
        *,
        provider: Any,
        messages: list[Message],
        req_model: str | None,
        model_name: str,
        provider_name: str,
        tools: list[Any],
        tool_executor: Any,
        slog: "SessionFileLogger | None",
        trace: "MessageTrace | None",
        force_tool_first: bool = False,
        skill_tool_names: set[str] | None = None,
        used_tools: set[str] | None = None,
        produced_files: list[dict[str, Any]] | None = None,
        tool_trace: list[dict[str, Any]] | None = None,
        browser_actuation: list[str] | None = None,
    ) -> str:
        """Bounded read-tool loop for the cognitive core.

        Mirrors the text-based ReAct pattern of AgentExecutor (tool results fed back
        as a user message). Returns the final answer text. Identical buffered output
        to the legacy streaming path when the model calls no tools.

        ``force_tool_first`` impõe ``tool_choice="required"`` no primeiro round: usado
        quando uma skill tool_backed foi autocarregada por match de keyword. A 0.9 de
        temperatura o modelo ignorava a ordem de chamar a tool em ~metade dos turnos e
        respondia de memória (inventando "registrei"); a resposta em texto continua
        vindo do round seguinte, com a persona intacta.

        ``skill_tool_names`` liga a guarda anti-mentira: se a resposta AFIRMA ter
        registrado/criado/apagado e nenhuma tool da skill rodou, o texto não passa —
        o turno ganha um round extra com a tool obrigatória. "Ou chama a tool, ou não
        afirma que fez" deixa de ser um pedido no prompt e vira invariante.

        ``produced_files`` (in/out, como ``used_tools``): se passado, recebe um dict
        ``{file_id, name, size_bytes}`` por ``file_save`` bem-sucedido neste turno —
        é como o chamador entrega um anexo clicável na mensagem final sem o modelo
        precisar (e sem poder) inventar uma URL.

        ``browser_actuation`` (in/out): recebe o nome da tool a cada vez que o turno
        MEXEU DE FATO no navegador do usuário. Só isso serve como "o turno já atuou
        na tela dele": ``used_tools`` guarda o nome que o MODELO escolheu, e o portão
        (``jobs.executor._gate_browser``) reescreve local ⇄ servidor por baixo; o
        ``tool_trace`` guarda o nome pré-portão e o resultado já stringificado.

        ``tool_trace`` (in/out): se passado, recebe um resumo compacto (via
        ``_summarize_tool_call``) de CADA tool call bem-sucedida deste turno — não
        só ``file_save``. É salvo em ``extra_metadata`` da mensagem e reinjetado no
        próximo turno (session_manager.get_history) pra evitar que um follow-up
        ("cade os arquivos?") refaça do zero um trabalho que já foi feito.
        """
        max_rounds = self.settings.cognitive_max_tool_rounds
        seen: set[tuple[str, str]] = set()
        final_text = ""
        files_out: list[dict[str, Any]] = produced_files if produced_files is not None else []
        # Cópia mutável: skill_read pode DESTRAVAR tools de uma skill tool_backed
        # (tool gating — docs/skills-library-plan.md), estendendo o toolset dos
        # rounds seguintes sem pagar o schema delas antes da leitura.
        # O dedup fica AQUI, e não só em quem monta a lista: este é o último ponto
        # antes do payload ir pro provider, então nenhum chamador futuro consegue
        # reintroduzir a duplicata que derrubava o turno no DeepSeek.
        tools = _dedup_tools_by_name(tools)
        skill_library = getattr(self, "skill_library", None)
        skill_tool_names = set(skill_tool_names or set())
        used: set[str] = used_tools if used_tools is not None else set()
        force_next = False
        lie_retried = False
        promise_retried = False
        browser_promise_retried = False
        download_claim_retried = False
        artifact_claim_retried = False
        truncated_rounds = 0

        for round_idx in range(max_rounds + 1):
            round_tools = tools if (tools and round_idx < max_rounds) else None
            if slog:
                slog.log_llm_request("COGNITIVE", messages, model=model_name, provider=provider_name, temperature=0.9)
            t0 = time.monotonic()
            extra: dict[str, Any] = {}
            if round_tools and ((force_tool_first and round_idx == 0) or force_next):
                extra["tool_choice"] = "required"
            force_next = False
            resp = await provider.complete(
                messages=messages,
                tools=round_tools,
                model=req_model or None,
                temperature=0.9,
                max_tokens=self.settings.default_max_tokens,
                **extra,
            )
            llm_ms = (time.monotonic() - t0) * 1000
            content = resp.get_content() or ""
            try:
                tool_calls = resp.get_tool_calls() if round_tools else []
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                # Truncated tool-call arguments. The provider hands us the raw JSON
                # string and it was cut mid-value by max_tokens — writing a whole HTML
                # file through `neo_write_file` is exactly the shape that blows the
                # budget. Unhandled, this killed the ENTIRE turn from inside the loop
                # ("Não consegui responder agora — deu erro do meu lado"), losing every
                # file already written. Recoverable: tell the model what happened and
                # let it split the write. Capped so a model that keeps truncating ends
                # the round instead of looping.
                truncated_rounds += 1
                logger.warning(
                    "[Cognitive] tool-call arguments truncated (round %d/%d): %s",
                    truncated_rounds, _MAX_TRUNCATED_TOOL_ROUNDS, e,
                )
                if slog:
                    slog.log_event(
                        "Tool call truncada",
                        f"Os argumentos vieram cortados pelo limite de tokens ({e}). "
                        f"Round extra pedindo escrita em pedaços "
                        f"({truncated_rounds}/{_MAX_TRUNCATED_TOOL_ROUNDS}).",
                    )
                if truncated_rounds > _MAX_TRUNCATED_TOOL_ROUNDS:
                    final_text = (
                        "Tentei escrever um arquivo grande demais numa tacada só e a "
                        "chamada foi cortada. Me peça de novo que eu escrevo em partes."
                    )
                    break
                messages.append(
                    Message(
                        role=MessageRole.USER,
                        content=(
                            "Sua última chamada de tool foi CORTADA no meio (os "
                            "argumentos passaram do limite de tokens), então ela não "
                            "rodou. Refaça em pedaços menores: escreva um arquivo por "
                            "chamada e, se um arquivo for grande, quebre em várias "
                            "escritas (crie e depois anexe com `neo_bash`). Não repita "
                            "a chamada gigante."
                        ),
                    )
                )
                continue

            # Resposta vazia + zero tool_calls = orçamento consumido no raciocínio
            # (finish_reason=length), não "nada a dizer". Retenta UMA vez com o
            # dobro do teto antes de aceitar o vazio — sem isso o turno cai no
            # fallback "não consegui gerar uma resposta". Ver constante acima.
            if not content.strip() and not tool_calls:
                bigger = min(
                    self.settings.default_max_tokens * 2,
                    _EMPTY_RETRY_MAX_TOKENS_CEILING,
                )
                logger.warning(
                    "[Cognitive] round %d resposta vazia (orçamento consumido no "
                    "raciocínio) — retentando com max_tokens=%d",
                    round_idx, bigger,
                )
                if slog:
                    slog.log_event(
                        "Retry resposta vazia",
                        f"round {round_idx}: content vazio + zero tool_calls "
                        f"(raciocínio consumiu o teto) — retentando com "
                        f"max_tokens={bigger}.",
                    )
                resp = await provider.complete(
                    messages=messages,
                    tools=round_tools,
                    model=req_model or None,
                    temperature=0.9,
                    max_tokens=bigger,
                    **extra,
                )
                llm_ms = (time.monotonic() - t0) * 1000
                content = resp.get_content() or ""
                try:
                    tool_calls = resp.get_tool_calls() if round_tools else []
                except (json.JSONDecodeError, ValueError, TypeError) as e:
                    # Same truncation, now on the doubled-budget retry. Nothing left
                    # to escalate to, so treat it as "no tool call this round" and let
                    # the loop continue instead of taking the whole turn down.
                    logger.warning("[Cognitive] truncated tool-call on empty-retry: %s", e)
                    if slog:
                        slog.log_event(
                            "Tool call truncada (no retry)",
                            f"Argumentos cortados também na retentativa ({e}) — "
                            "seguindo sem tool neste round.",
                        )
                    tool_calls = []

            if trace:
                payload = []
                for m in messages:
                    role_val = m.role.value if hasattr(m.role, 'value') else str(m.role)
                    payload.append({"role": role_val, "content": m.content if m.role == MessageRole.SYSTEM else (m.content or '')[:500]})
                trace.add_llm_call(
                    provider=provider_name, model=model_name, messages_sent=payload,
                    response_text=(content or '')[:1000], duration_ms=round(llm_ms, 2),
                    temperature=0.9, stage="cognitive",
                )

            if not tool_calls:
                logger.info(
                    "[Cognitive] round %d sem tool_calls — round_tools=%s "
                    "promise_retried=%s download_claim_retried=%s used=%s "
                    "files_out=%s promises_pending=%s claims_download=%s content=%r",
                    round_idx, bool(round_tools), promise_retried, download_claim_retried,
                    used, bool(files_out), _promises_pending_action(content),
                    _claims_downloadable_delivery(content), content[:150],
                )
                # Guarda anti-mentira: ele afirmou ter EXECUTADO algo da skill sem ter
                # chamado tool nenhuma. Não devolve o texto — dá um round extra com a
                # tool obrigatória. Uma vez só, para não virar laço.
                if (
                    round_tools
                    and skill_tool_names
                    and not (used & skill_tool_names)
                    and not lie_retried
                    and _claims_action_without_tool(content)
                ):
                    lie_retried = True
                    force_next = True
                    if slog:
                        slog.log_event(
                            "Anti-mentira",
                            "A resposta afirmava ter executado sem chamar tool — "
                            f"round extra com tool obrigatória.\n\n> {content[:200]}",
                        )
                    logger.warning("[Cognitive] resposta afirmava ação sem tool — forçando tool")
                    messages.append(Message(role=MessageRole.ASSISTANT, content=content))
                    messages.append(
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "PARE: você afirmou ter feito isso, mas não chamou tool "
                                "nenhuma — nada foi executado e a frase é falsa. Chame "
                                "AGORA a tool da skill que executa de verdade e só "
                                "responda depois de ver o resultado dela. Se faltar "
                                "informação para executar, NÃO diga que fez: pergunte."
                            ),
                        )
                    )
                    continue

                # Guarda anti-promessa: ele descreveu o que AINDA vai fazer
                # (zipar, baixar, mandar) em vez de fazer, e sobra rodada de tool.
                # Sem isso o turno termina com a entrega pela metade (sessões
                # 7b0956de, 918e515d) e o usuário precisa cobrar. Não se aplica
                # quando ele já adiou de verdade via job assíncrono — aí "vou
                # avisar quando terminar" é o comportamento certo, não o bug.
                if (
                    round_tools
                    and not promise_retried
                    and not (used & _LEGITIMATE_DEFERRAL_TOOLS)
                    and _promises_pending_action(content)
                ):
                    promise_retried = True
                    force_next = True
                    if slog:
                        slog.log_event(
                            "Anti-promessa",
                            "A resposta descreveu o que ainda vai fazer em vez de fazer — "
                            f"round extra com tool obrigatória.\n\n> {content[:200]}",
                        )
                    logger.warning("[Cognitive] resposta prometeu ação futura sem terminar — forçando tool")
                    messages.append(Message(role=MessageRole.ASSISTANT, content=content))
                    messages.append(
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "PARE: você descreveu o que ainda vai fazer, mas não fez — "
                                "não termine o turno assim. Continue AGORA chamando as tools "
                                "até a entrega estar de fato pronta (só responda depois de "
                                "ver o resultado). Se for realmente demorado, use a tool de "
                                "job em background e avise que vai notificar ao terminar — "
                                "mas não descreva um plano no lugar de executar."
                            ),
                        )
                    )
                    continue

                # Guarda anti-promessa de NAVEGADOR: ele disse que vai abrir (ou
                # que já enfileirou) algo no navegador DO USUÁRIO sem ter chamado
                # tool nenhuma que mexa no navegador. É a falha da sessão
                # d01cb5ab, e a mais cara de todas aqui: o usuário fica OLHANDO
                # a tela esperando algo que nunca vai acontecer.
                if (
                    round_tools
                    and not browser_promise_retried
                    and not (used & _BROWSER_ACTING_TOOLS)
                    and _promises_browser_action(content)
                ):
                    browser_promise_retried = True
                    force_next = True
                    if slog:
                        slog.log_event(
                            "Anti-promessa (navegador)",
                            "A resposta prometeu agir no navegador do usuário sem chamar "
                            f"tool — round extra com tool obrigatória.\n\n> {content[:200]}",
                        )
                    logger.warning(
                        "[Cognitive] prometeu ação no navegador do usuário sem tool — forçando"
                    )
                    messages.append(Message(role=MessageRole.ASSISTANT, content=content))
                    messages.append(
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "PARE: você disse que ia abrir/abriu algo no navegador DO "
                                "USUÁRIO, mas não chamou tool nenhuma — nada aconteceu na "
                                "tela dele. Chame AGORA `browser_local_task` com a tarefa "
                                "descrita (é ela que dirige o navegador local). Se a tarefa "
                                "não for de navegador local, corrija a resposta dizendo o "
                                "que você REALMENTE fez — nunca descreva uma ação que não "
                                "executou."
                            ),
                        )
                    )
                    continue

                # Guarda anti-alegação-de-download: ele disse "está pronto, pode
                # baixar pelo botão" sem NUNCA ter chamado file_save neste turno
                # (sessão 95ccd52d: só neo_write_file rodou). Diferente da
                # anti-promessa (futuro — "vou fazer"), aqui é afirmação de que
                # JÁ dá pra baixar — sem file_save isso é sempre falso, não
                # existe nada catalogado pro usuário clicar.
                #
                # A guarda IRMÃ é a de baixo (_claims_sandbox_artifact): esta aqui
                # cobre a alegação de ENTREGA ao usuário; aquela cobre o degrau
                # anterior, "o arquivo existe no sandbox".
                if (
                    round_tools
                    and not download_claim_retried
                    and not files_out
                    and _claims_downloadable_delivery(content)
                ):
                    download_claim_retried = True
                    force_next = True
                    if slog:
                        slog.log_event(
                            "Anti-alegação-de-download",
                            "A resposta alegou entrega baixável sem ter chamado "
                            f"file_save — round extra com tool obrigatória.\n\n> {content[:200]}",
                        )
                    logger.warning("[Cognitive] resposta alegou download sem file_save — forçando tool")
                    messages.append(Message(role=MessageRole.ASSISTANT, content=content))
                    messages.append(
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "PARE: você disse que dá pra baixar, mas nunca chamou "
                                "file_save — não existe nada catalogado pro usuário "
                                "clicar e a frase é falsa. Chame file_save AGORA (fazendo "
                                "antes o que faltar) e só responda depois de ver o resultado."
                            ),
                        )
                    )
                    continue

                # Guarda de verdade-do-resultado (docs/self-continuation-plan.md §4).
                # A resposta afirma que criou/baixou artefato no sandbox, e NENHUM
                # tool bem-sucedido deste turno tocou o que ela cita. Diferente das
                # duas de cima: não é promessa (futuro) nem entrega ao usuário
                # (file_save) — é afirmação de que o arquivo EXISTE. Determinístico
                # de propósito: roda no fim de todo passo do ciclo e não pode custar
                # uma chamada de LLM.
                if (
                    round_tools
                    and not artifact_claim_retried
                    # Sem rastro não há evidência NENHUMA, e acusar sem evidência
                    # forçaria uma rodada extra em todo turno de um chamador que
                    # simplesmente não pediu trace. Ausência de rastro ≠ prova de
                    # ausência — a guarda só fala quando pode olhar.
                    and tool_trace is not None
                    # …e o trace é do TURNO, não da conversa. Um turno que não rodou
                    # tool nenhuma está relatando trabalho de turnos anteriores, e a
                    # guarda não tem jurisdição sobre o que não viu acontecer.
                    # Medido na session-b407c213: "me mostra as imagens que baixou"
                    # foi acusado porque os `curl` (exit=0, imagens no disco) tinham
                    # rodado no turno ANTERIOR — e a rodada forçada empurrou o modelo
                    # a "verificar" o sandbox com `run_command`, que roda na máquina
                    # do usuário e nunca acharia /workspace.
                    and tool_trace
                    and _claims_sandbox_artifact(content)
                    and not _trace_backs_artifact_claim(
                        tool_trace, _claimed_artifact_targets(content), files_out
                    )
                ):
                    artifact_claim_retried = True
                    force_next = True
                    targets = _claimed_artifact_targets(content)
                    if slog:
                        slog.log_event(
                            "Anti-alegação-de-artefato",
                            "A resposta afirmou ter criado/baixado arquivo no sandbox, mas "
                            "nenhuma tool bem-sucedida deste turno tocou "
                            f"{sorted(targets) or 'nenhum caminho citado'} — round extra "
                            f"para VERIFICAR antes de reafirmar.\n\n> {content[:200]}",
                        )
                    logger.warning(
                        "[Cognitive] alegação de artefato sem respaldo no tool_trace "
                        "(alvos=%s) — forçando verificação",
                        sorted(targets),
                    )
                    messages.append(Message(role=MessageRole.ASSISTANT, content=content))
                    messages.append(
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "PARE: você afirmou que o arquivo existe, mas nenhuma tool "
                                "que rodou com SUCESSO neste turno criou isso — atenção que "
                                "um `neo_bash` com exit_code diferente de 0 NÃO fez nada. "
                                "Verifique AGORA com `neo_list_files` no caminho que você "
                                "citou. Se estiver lá, siga. Se NÃO estiver, diga o que "
                                "realmente aconteceu (o que falhou e por quê) — não repita "
                                "a alegação. Dizer que entregou sem ter entregue é o pior "
                                "resultado possível."
                            ),
                        )
                    )
                    continue

                final_text = content
                if slog:
                    slog.log_llm_response("COGNITIVE", final_text, duration_ms=llm_ms, model=model_name)
                break

            # Feed tool results back as a user message (text-based, like AgentExecutor).
            messages.append(Message(role=MessageRole.ASSISTANT, content=content))
            result_lines: list[str] = []
            # Blocos de conversa recuperada deste round — entram como SYSTEM,
            # separados do histórico e do slot de resultado de tool (ver abaixo).
            recall_blocks: list[str] = []
            for tc in tool_calls:
                # Tool client-actuating (request_user_file/save_generated_file): o
                # disco/picker do usuário só existe no cliente — não dá para rodar
                # in-process. Quebra o loop AGORA (antes de qualquer bookkeeping) e
                # deixa _handle_direct_response emitir o tool_request; o turno retoma
                # em process_tool_result quando o desktop devolver o resultado.
                if tc.name in _ALL_ACTUATION_TOOLS:
                    if slog:
                        slog.log_event("Cognitive Tool (client-actuating)", f"→ `{tc.name}` {tc.arguments}")
                    raise ClientActuationRequested(tc.name, dict(tc.arguments or {}), tc.id)
                key = (tc.name, json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False))
                if key in seen:
                    result_lines.append(f"Tool: {tc.name}\nResult: (já consultado neste turno — não repita; use o resultado anterior)")
                    continue
                seen.add(key)
                used.add(tc.name)
                if slog:
                    slog.log_event("Cognitive Tool", f"→ `{tc.name}` {tc.arguments}")
                tr = await tool_executor.execute_tool_call(tc)
                if tr.error:
                    result_lines.append(f"Tool: {tc.name}\nError: {tr.error}")
                elif tc.name == "conversation_recall" and isinstance(tr.result, dict):
                    # Conversa antiga NÃO pode entrar pelo mesmo canal do resto:
                    # os resultados de tool viram um Message(USER) e o modelo lê
                    # isso como "foi dito agora", emendando a resposta a partir
                    # dali. O corpo vai como SYSTEM (bloco delimitado, com data e
                    # autor em cada linha, ver render_recall_block) e aqui fica só
                    # o resumo de uma linha, para o texto não aparecer duas vezes.
                    result_lines.append(
                        f"Tool: {tc.name}\nResult: {tr.result.get('resumo', '')}"
                    )
                    bloco = str(tr.result.get("bloco") or "").strip()
                    if bloco:
                        recall_blocks.append(bloco)
                else:
                    result_lines.append(f"Tool: {tc.name}\nResult: {tr.result}")
                if tool_trace is not None:
                    tool_trace.append(_summarize_tool_call(tc.name, tc.arguments, tr))
                # Atuou no navegador DO USUÁRIO? Só aqui dá pra saber: `tr.name` é o
                # nome PÓS-portão (o gate rebaixa local→servidor quando o usuário não
                # pediu a tela dele) e o `ok` do payload distingue a ordem aceita da
                # que voltou com erro. Ver `_drop_music_handoff_after_browser`.
                if (
                    browser_actuation is not None
                    and not tr.error
                    and (getattr(tr, "name", None) or tc.name) == _USER_BROWSER_TOOL
                    and isinstance(tr.result, dict)
                    and tr.result.get("ok")
                ):
                    browser_actuation.append(_USER_BROWSER_TOOL)
                if tc.name == "file_save" and not tr.error and isinstance(tr.result, dict):
                    fid = tr.result.get("file_id")
                    if fid:
                        files_out.append({
                            "file_id": str(fid),
                            "name": tr.result.get("name"),
                            "size_bytes": tr.result.get("size_bytes"),
                        })
                # Tool gating: ler uma skill tool_backed destrava as tools dela
                # para os próximos rounds deste turno (e só deste turno).
                if tc.name == "skill_read" and not tr.error and skill_library is not None:
                    try:
                        unlocked = skill_library.tools_for(str(tc.arguments.get("name", "")))
                    except Exception as e:  # noqa: BLE001 — gating nunca derruba o turno
                        logger.warning("[SkillLibrary] tools_for falhou: %s", e)
                        unlocked = []
                    have = {t.name for t in tools}
                    fresh = [t for t in unlocked if t.name not in have]
                    if fresh:
                        tools.extend(fresh)
                        if slog:
                            slog.log_event(
                                "Skill tools destravadas",
                                ", ".join(f"`{t.name}`" for t in fresh),
                            )
                if slog:
                    if not tr.error and tc.name == "conversation_recall" and isinstance(tr.result, dict):
                        # O `bloco` inteiro (~900 tokens de conversa antiga) não
                        # cabe aqui: o que importa depurar é O QUE a busca achou
                        # e por qual trilho.
                        slog.log_event(
                            "Recall de conversas",
                            f"termos={tr.result.get('termos')} "
                            f"trilho={tr.result.get('trilho')} "
                            f"conversas={tr.result.get('total_conversas')} "
                            f"mensagens={tr.result.get('total_mensagens')} "
                            f"tem_mais={tr.result.get('tem_mais')} "
                            f"proxima_before={tr.result.get('proxima_busca_before')}\n\n"
                            + str(tr.result.get("bloco") or "")[:1500],
                        )
                    else:
                        # O nome EFETIVO, não o que o modelo pediu: o portão do
                        # navegador rebaixa/promove entre `browser_local_task` e
                        # `browser_task`, e o log mostrando só a escolha do modelo
                        # afirmava "mexeu no navegador dele" quando o que rodou foi
                        # o headless do servidor (sessão 0939a295 — foi o que
                        # escondeu a dupla atuação da música por um turno inteiro).
                        _eff = getattr(tr, "name", None) or tc.name
                        _rotulo = (
                            f"`{tc.name}`"
                            if _eff == tc.name
                            else f"`{tc.name}` → portão: `{_eff}`"
                        )
                        slog.log_event(
                            "Cognitive Tool Result",
                            f"{_rotulo} → " + (f"ERRO: {tr.error}" if tr.error else str(tr.result)[:600]),
                        )
            messages.append(Message(role=MessageRole.USER, content="\n\n".join(result_lines)))
            for bloco in recall_blocks:
                messages.append(Message(role=MessageRole.SYSTEM, content=bloco))

        return final_text

    async def _build_pks_verification_note(
        self,
        *,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        user_id: UUID,
        slog: "SessionFileLogger | None",
    ) -> str | None:
        """Run search_pks for a memory-verification question and render the results.

        Queries come from the classifier's knowledge_keywords plus the content
        words of the question itself (and of the previous user turn, which usually
        holds what was supposedly saved). Returns a system-prompt block with the
        REAL results — or an explicit "nothing found" — that the model must answer
        from. Errors degrade to None (never block the reply).
        """
        queries: list[str] = []
        for kw in decision.knowledge_keywords or []:
            if kw and kw not in queries:
                queries.append(kw)

        def _content_words(text: str) -> str:
            words = [
                w for w in re.findall(r"[\wÀ-ÿ-]+", (text or "").lower())
                if len(w) > 2 and w not in _VERIFICATION_STOPWORDS
            ]
            return " ".join(words[:6])

        own = _content_words(user_message)
        if own and own not in queries:
            queries.append(own)
        # The previous user turn usually contains what was supposedly saved.
        prev_user = next(
            (m.get("content", "") for m in reversed(history[:-1] if history else [])
             if m.get("role") == "user"),
            "",
        )
        prev = _content_words(prev_user)
        if prev and prev not in queries:
            queries.append(prev)

        # No extractable terms → we didn't actually search anything; claiming
        # "nothing found" would be as dishonest as the bug being fixed.
        if not queries:
            return None

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            for query in queries[:4]:
                rows = await self.pks.search_entities(user_id=user_id, query=query)
                for row in rows[:8]:
                    key = str(row.get("name") or "") + str(row.get("entity_type") or "")
                    if key and key not in seen:
                        seen.add(key)
                        results.append(row)
        except Exception as e:
            logger.warning("[PKS-VERIFY] search_pks failed: %s", e, exc_info=True)
            return None

        if slog:
            slog.log_event(
                "Verificação ativa do PKS (search_pks)",
                f"Queries: {queries[:4]}\nEncontrados: {len(results)}",
            )

        lines = [
            _MEMORY_VERIFICATION_NOTE_HEADER,
            "O usuário está questionando se algo foi REALMENTE salvo. Acabei de buscar",
            f"no PKS dele (queries: {', '.join(queries[:4]) or '—'}). Resultado REAL:",
            "",
        ]
        if results:
            for row in results[:10]:
                name = row.get("name") or "?"
                etype = row.get("entity_type") or "?"
                props = row.get("properties") or {}
                prop_preview = ", ".join(
                    f"{k}={v}" for k, v in list(props.items())[:4]
                ) if isinstance(props, dict) else ""
                lines.append(f"- [{etype}] {name}" + (f" ({prop_preview})" if prop_preview else ""))
        else:
            lines.append("- NENHUM resultado encontrado para essas buscas.")
        lines += [
            "",
            "Responda APENAS com base nestes resultados:",
            "- Se o que o usuário pergunta NÃO aparece acima, ADMITA que não está salvo",
            "  (mesmo que você tenha dito antes que salvou — aquilo estava errado) e",
            "  ofereça salvar agora.",
            "- Se aparece, confirme citando o que está registrado.",
            "- NUNCA confirme com base apenas no histórico da conversa.",
        ]
        return "\n".join(lines)

    # Device nouns/pronoun-references that signal the user is talking about the
    # machine they are connected from ("esse dispositivo", "este computador"…).
    _DEVICE_REFERENCE_RE = re.compile(
        r"\b(dispositivo|device|aparelho|maquina|computador|notebook|macbook|"
        r"mac book|laptop|celular|telefone)\b"
    )

    @classmethod
    def _references_current_device(cls, message: str) -> bool:
        """True when the message refers to the user's current device."""
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", (message or "").lower())
            if unicodedata.category(c) != "Mn"
        )
        return bool(cls._DEVICE_REFERENCE_RE.search(normalized))

    # Known device-model labels the user may mention (longest first so
    # "macbook air" wins over "macbook"). Used to name the PKS device entity.
    _DEVICE_LABEL_PATTERNS = [
        (re.compile(r"\bmacbook\s*air\b"), "MacBook Air"),
        (re.compile(r"\bmacbook\s*pro\b"), "MacBook Pro"),
        (re.compile(r"\bmac\s*mini\b"), "Mac mini"),
        (re.compile(r"\bmac\s*studio\b"), "Mac Studio"),
        (re.compile(r"\bmacbook\b"), "MacBook"),
        (re.compile(r"\bimac\b"), "iMac"),
        (re.compile(r"\bnotebook\b"), "Notebook"),
        (re.compile(r"\bdesktop\b"), "Desktop"),
    ]

    @classmethod
    def _extract_device_label(cls, message: str) -> str | None:
        """Best-effort device label from the user's words (e.g. 'MacBook Air').

        Deterministic and conservative: returns None when nothing recognizable is
        present, so we never invent a model the user did not state.
        """
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", (message or "").lower())
            if unicodedata.category(c) != "Mn"
        )
        for pattern, label in cls._DEVICE_LABEL_PATTERNS:
            if pattern.search(normalized):
                return label
        return None

    @staticmethod
    def _build_media_context_note(metadata: dict | None) -> str | None:
        """Summarize the browser media tabs (from the extension) for the brain.

        Reads ``metadata['media_context']['browser_tabs']`` (the snapshot the desktop
        sends every turn) into a short, readable note so the cognitive core knows what
        is open/playing and can control it or answer "o que tá tocando?".
        """
        from core.skills.music.service import (
            describe_tabs,
            parse_media_context,
            tab_has_media,
        )

        tabs = (parse_media_context(metadata) or {}).get("browser_tabs") or []
        lines = [f"- {linha}" for linha in describe_tabs(tabs)]
        if not lines:
            return None
        # Se NENHUMA aba tem player, "pausa isso" não tem alvo. Dizer isso aqui
        # em vez de deixar o modelo deduzir da lista: com uma aba na home aberta
        # ele mandava pausar, a extensão não achava player e o turno terminava
        # com "pausei" sem ter pausado nada.
        tem_alvo = any(
            tab_has_media(t) is not False for t in tabs if isinstance(t, dict)
        )
        aviso = (
            ""
            if tem_alvo
            else "\nNENHUMA dessas abas tem vídeo/música carregado — não há o que "
            "pausar, pular ou retomar. Se ele pedir controle, diga que não tem nada "
            "tocando (ou toque algo, se for isso que ele quer)."
        )
        return (
            "## Mídia aberta no navegador DO USUÁRIO (tempo real)\n"
            "Esta lista vem da extensão Neo Music Bridge, que roda no navegador DA "
            "MÁQUINA DELE e reporta a cada turno. É o navegador dele, não o do "
            "servidor/sandbox: você ENXERGA estas abas. Nunca diga que não vê o "
            "navegador dele, nem chame estas abas de 'navegador do servidor' — e "
            "não precisa abrir tarefa de navegador para saber o que está aberto ou "
            "tocando, está tudo aqui.\n"
            "Abas de música/vídeo abertas agora, da mais relevante para a menos:\n"
            + "\n".join(lines)
            + aviso
            + "\n\nComo ler cada linha: **TOCANDO/pausado** é o estado real do player "
            "(vídeo no mudo continua TOCANDO); **som na guia** é o ícone de "
            "auto-falante — a ausência dele com o player tocando significa mudo, não "
            "parado; **música/vídeo comum** é a classificação do próprio YouTube; "
            "**nada aberto pra tocar** é aba sem player (home, feed, busca), que não "
            "aceita comando nenhum; **ANÚNCIO no ar** quer dizer que o som do momento "
            "é comercial, então não anuncie isso como a faixa; **canal** é quem "
            "publicou o vídeo, e o **link** no fim da linha é a URL REAL dele — se "
            "ele pedir o link, copie essa exatamente, NUNCA monte uma URL de "
            "cabeça.\n"
            "MUDO/VOLUME DA ABA ≠ MUDO DA MÁQUINA: o bloco abaixo age no player "
            "desta aba. `device_control` silencia o computador INTEIRO — só use se "
            "ele pedir a máquina, nunca para 'muta essa aba'.\n"
            "Se o usuário pedir pra PAUSAR, RETOMAR/dar play, PULAR, VOLTAR ou mudar "
            "VOLUME dessa mídia, você DEVE emitir ao final o bloco "
            "[[HANDOFF:music]]<ação>[[/HANDOFF]] (ação = pausar | retomar | próxima | "
            "anterior | mudo | aumentar volume | abaixar volume) — é ELE que executa de "
            "verdade. NUNCA diga que pausou/voltou/pulou SEM emitir o bloco. Use também "
            "estas abas pra responder o que está tocando, há quanto tempo e quanto "
            "falta. Se nada estiver tocando, diga isso em vez de fingir que controlou."
        )

    def _get_ha_adapter(self) -> "HomeAssistantAdapter | None":
        """Lazily build and memoize a single HA adapter for snapshot injection.

        Memoized so the device-index/states caches it holds survive across turns
        (the skill pipeline builds its own adapter per call; this one is dedicated
        to the read-only snapshot path).
        """
        if self._ha_adapter is not None:
            return self._ha_adapter
        try:
            from core.skills.home_assistant.adapter import HomeAssistantAdapter
            self._ha_adapter = HomeAssistantAdapter()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not build HA adapter for snapshot injection: %s", e)
            return None
        return self._ha_adapter

    def _get_affect_updater(self):
        """Lazily build the post-turn affect updater. None when the feature is off.

        Also registered as the process-wide shared updater so out-of-band
        channels (WhatsApp inbound pipeline) reuse the same task registry,
        per-user locks and health counters.
        """
        if not self.settings.neo_affect_enabled or not self.pks:
            return None
        if self._affect_updater is None:
            from core.agent.affect_updater import (
                AffectUpdater,
                get_shared_updater,
                set_shared_updater,
            )
            # main.py builds the shared updater at startup (so WhatsApp-first
            # boots are covered); reuse it here. Fallback: build and share.
            self._affect_updater = get_shared_updater()
            if self._affect_updater is None:
                self._affect_updater = AffectUpdater(
                    self.llm, self.settings, pks=self.pks, db=self.db
                )
                set_shared_updater(self._affect_updater)
        return self._affect_updater

    async def _build_home_assistant_context_note(self, user_message: str) -> str | None:
        """Live HA snapshot for the cognitive core, gated by a device-name match.

        The relevance gate matches the message against a cached device-name index
        (no HA call on the hot path). Only when a known device is referenced do we
        fetch the current states and inject them — so the brain answers status from
        live ground-truth (never the stale PKS "devices" rows), and unrelated turns
        ("liga o computador") neither inject nor touch HA. See neo-core HA plan.
        """
        if not getattr(self.settings, "home_assistant_context_injection_enabled", True):
            return None
        adapter = self._get_ha_adapter()
        if adapter is None or not adapter.is_configured:
            return None

        from core.skills.home_assistant.service import match_known_device, render_snapshot

        try:
            index = await adapter.get_device_index(
                float(getattr(self.settings, "home_assistant_device_index_ttl", 300))
            )
            if not match_known_device(user_message, index):
                return None  # no known device referenced → skip (no states fetch)
            states = await adapter.get_states_cached(
                float(getattr(self.settings, "home_assistant_snapshot_cache_ttl", 8))
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("HA snapshot injection failed: %s", e)
            return None

        snapshot = render_snapshot(states, query=user_message)
        if not snapshot.strip():
            return None
        return (
            "## Casa inteligente (Home Assistant — tempo real)\n"
            f"{snapshot}\n"
            "Estes são os estados REAIS e ATUAIS dos dispositivos. NUNCA responda o "
            "estado de um dispositivo pela memória/PKS/histórico — use SÓ esta lista. "
            "Se a informação pedida não estiver aqui, diga que não tem esse dado. "
            "Para AGIR (ligar/desligar/ajustar), escreva a resposta e ao FINAL emita o "
            "bloco [[HANDOFF:home_assistant]]<ordem completa>[[/HANDOFF]] — é ele que "
            "executa de verdade; nunca diga que fez sem emitir o bloco."
        )

    @staticmethod
    def _build_current_device_context(metadata: dict | None) -> str:
        """Describe the current device from connection metadata (real identity).

        Returns an empty string when no device identity is available. Only fields
        that are actually reported by the desktop are included — note that
        `metadata["model"]` is the *LLM* model (e.g. deepseek-v4-flash), NOT the
        hardware model, so it is deliberately excluded.
        """
        md = metadata or {}
        device_id = md.get("device_id")
        hardware_id = md.get("hardware_id")
        os_platform = md.get("os_platform")
        os_arch = md.get("os_arch")
        device_type = md.get("client")

        if not (device_id or hardware_id):
            return ""

        lines = ["[Dispositivo atual do usuário — identidade confirmada pela conexão]"]
        if device_id:
            lines.append(f"- device_id: {device_id}")
        if hardware_id:
            lines.append(f"- hardware_id: {hardware_id}")
        if os_platform and os_platform != "unknown":
            # Mesmo motivo do _build_system_context: o Rust manda "macos"/"windows".
            pretty = {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}
            normalized = AIService._normalize_os_platform(os_platform)
            lines.append(f"- sistema: {pretty.get(normalized, os_platform)}")
        if os_arch:
            lines.append(f"- arquitetura: {os_arch}")
        if device_type:
            lines.append(f"- cliente: {device_type}")
        return "\n".join(lines)

    @staticmethod
    def _is_memory_confirmation(text: str) -> bool:
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", (text or "").strip().lower())
            if unicodedata.category(c) != "Mn"
        )
        normalized = re.sub(r"[^a-z0-9 ]+", " ", normalized)
        normalized = " ".join(normalized.split())
        confirmations = (
            "salva so na sua memoria",
            "salva na sua memoria",
            "salva isso na memoria",
            "salva na memoria",
            "salva isso",
            "pode salvar",
            "pode salvar isso",
            "guarda na memoria",
            "guarda isso",
            "lembra disso",
            "memoriza isso",
            "vou fazer esse",
            "vou fazer esses",
            "esse sera meu treino",
            "esse vai ser meu treino",
        )
        return any(phrase in normalized for phrase in confirmations)

    @staticmethod
    def _last_assistant_message(history: list[dict[str, str]]) -> str | None:
        for msg in reversed(history):
            if msg.get("role") == "assistant" and msg.get("content"):
                return msg["content"]
        return None

    @classmethod
    def _find_memory_artifact(cls, history: list[dict[str, str]], fallback: str | None = None) -> str | None:
        candidates: list[str] = []
        for msg in reversed(history):
            if msg.get("role") != "assistant":
                continue
            content = (msg.get("content") or "").strip()
            if content:
                candidates.append(content)
        if fallback:
            candidates.insert(0, fallback)

        for candidate in candidates:
            if cls._looks_like_workout_artifact(candidate):
                return candidate
        for candidate in candidates:
            if cls._looks_like_persistable_artifact(candidate):
                return candidate
        return fallback or (candidates[0] if candidates else None)

    @staticmethod
    def _looks_like_workout_artifact(text: str) -> bool:
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", (text or "").strip().lower())
            if unicodedata.category(c) != "Mn"
        )
        normalized = " ".join(normalized.split())
        weekday_hits = sum(1 for token in ("segunda", "terça", "terca", "quarta", "quinta", "sexta", "sabado", "sábado", "domingo") if token in normalized)
        return weekday_hits >= 2 or ("treino abcde" in normalized) or ("| exercício |" in normalized and "| séries |" in normalized)

    @classmethod
    def _looks_like_persistable_artifact(cls, text: str) -> bool:
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", (text or "").strip().lower())
            if unicodedata.category(c) != "Mn"
        )
        normalized = " ".join(normalized.split())
        if len(normalized) < 180:
            return False
        ephemeral_tokens = (
            "vou salvar aqui na memoria",
            "ja salvei na memoria",
            "perfeito daniel",
            "como posso ajudar",
            "quer que eu ajuste algo",
            "pode deixar que vou te chamar assim",
        )
        if any(token in normalized for token in ephemeral_tokens):
            return False
        return ("| " in text and " |" in text) or text.count("\n") >= 8 or bool(re.search(r"\b\d+\s*[x×]\s*\d+", normalized))

    # ---------------------------------------------------------------
    # Handler: SERVER_TOOL — tools running on neo-core host
    # ---------------------------------------------------------------

    @staticmethod
    def _build_server_tool_memory(
        session_id: UUID,
        history: list[dict[str, str]] | None,
        user_message: str,
    ) -> ConversationMemory | None:
        """Seed agent memory with recent turns so corrections are understood.

        The current user message is carried by `task`, so a trailing history
        entry equal to it is dropped to avoid duplication.
        """
        if not history:
            return None
        memory = ConversationMemory(session_id=session_id)
        recent = list(history[-6:])
        if recent and recent[-1].get("role") == "user" and recent[-1].get("content") == user_message:
            recent = recent[:-1]
        for msg in recent:
            role = MessageRole(msg["role"]) if msg.get("role") in ("user", "assistant") else MessageRole.USER
            memory.add_message(Message(role=role, content=msg.get("content", "")))
        return memory if memory.get_messages() else None

    @staticmethod
    def _extract_geo(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        """Normalize device GPS from client metadata into ``{lat, lon[, city]}``.

        Looks under ``geo``/``gps``/``location``. GPS is the only auto-detect
        source for location (no IP/timezone). Returns ``None`` when absent/invalid.
        """
        if not isinstance(metadata, dict):
            return None
        for key in ("geo", "gps", "location"):
            raw = metadata.get(key)
            if not isinstance(raw, dict):
                continue
            lat = raw.get("lat", raw.get("latitude"))
            lon = raw.get("lon", raw.get("longitude"))
            try:
                out: dict[str, Any] = {"lat": float(lat), "lon": float(lon)}
            except (TypeError, ValueError):
                continue
            if raw.get("city"):
                out["city"] = str(raw["city"])
            return out
        return None

    @staticmethod
    def _is_location_relevant(decision: "RoutingDecision") -> bool:
        """Whether this turn may need the user's location (e.g. weather)."""
        return (decision.tool_name or "") == "get_weather"

    async def _handle_server_tool(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        live: bool = False,
        metadata: dict[str, Any] | None = None,
        register: "Register | None" = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle server-side tool execution using AgentExecutor.

        ``live=True`` (turno do modo voz) anexa o estilo de fala ao task para o
        agente responder em texto corrido sem markdown. ``metadata`` carrega
        sinais do cliente (ex: GPS do dispositivo em ``metadata["geo"]``).
        ``register`` (quando o registro da situação está ativo) anexa a diretiva
        de tom ao task pelo mesmo mecanismo do modo live — este caminho não passa
        pelo PromptContextSelector.
        """

        if slog:
            slog.log_tool_request(decision.tool_name or "agent", arguments=decision.tool_arguments, execution="server-side")

        yield AIServiceResponse(
            intent=IntentType.SERVER_TOOL,
            session_id=session_id,
            message=f"Executando ferramenta no servidor: {decision.tool_name or 'agent'}...",
            is_final=False,
        )

        t0 = time.monotonic()
        try:
            # Per-request executor that scopes `list_connected_devices` to this
            # user while delegating every other server tool to the global executor.
            from modules.devices import DevicesExecutor, DevicesContextExecutor

            ctx_executor = DevicesContextExecutor(
                global_executor=self.agent.tool_executor,
                devices_executor=DevicesExecutor(db=self.db),
                user_id=user_id,
                # Alvo implícito de device_control quando o usuário não cita
                # aparelho nenhum ("abaixa o volume").
                current_device_id=(metadata or {}).get("device_id"),
            )

            # browser_task/deep_research/job_* aparecem no catálogo de SERVER_TOOL
            # (registry global), mas só o JobToolExecutor por-request sabe executá-las.
            ctx_executor = self._with_job_tools(
                ctx_executor,
                user_id=user_id,
                session_id=session_id,
                can_show_images=self._client_renders_inline_images(metadata),
                user_message=user_message,
                metadata=metadata,
            )

            # Scope search_pks to this user too (same per-request seam): the global
            # pks_search executor refuses to run without a user_id.
            try:
                pks_search_exec = getattr(
                    self.agent.tool_executor, "_module_executors", {}
                ).get("pks_search")
                if pks_search_exec is not None:
                    pks_search_exec.set_context(user_id)
            except Exception:  # pragma: no cover - defensive
                logger.warning("[PKS-SEARCH] failed to scope user context", exc_info=True)

            # Scope voice reads + config_set persistence to this user (same seam).
            try:
                module_execs = getattr(self.agent.tool_executor, "_module_executors", {})
                for _name in ("voice", "config"):
                    _exec = module_execs.get(_name)
                    if _exec is not None:
                        _exec.set_context(user_id)
            except Exception:  # pragma: no cover - defensive
                logger.warning("[VOICE/CONFIG] failed to scope user context", exc_info=True)

            # Scope location resolution to this user + carry device GPS (if any),
            # so get_user_location reads the right PKS and can use GPS.
            geo = self._extract_geo(metadata)
            try:
                loc_exec = getattr(
                    self.agent.tool_executor, "_module_executors", {}
                ).get("location")
                if loc_exec is not None:
                    loc_exec.set_context(user_id, geo)
            except Exception:  # pragma: no cover - defensive
                logger.warning("[LOCATION] failed to scope user context", exc_info=True)

            # Safety net: don't let the server-tool agent run blind. Seed recent
            # history (so a second-turn correction is understood) and, when the
            # query depends on user knowledge, prepend PKS context to the task.
            # The router now redirects knowledge-style device questions to
            # DIRECT_RESPONSE, but this keeps SERVER_TOOL robust if a query still
            # lands here. Context errors must never block tool execution.
            task = user_message
            memory = self._build_server_tool_memory(session_id, history, user_message)
            try:
                if decision.needs_memory and self.pks and user_id:
                    _pks_result, pks_text = await self.pks.retrieve_context(
                        user_id=user_id,
                        keywords=decision.knowledge_keywords,
                        user_message=user_message,
                    )
                    if pks_text:
                        task = (
                            f"## Conhecimento do Usuário (do PKS)\n{pks_text}\n\n"
                            f"## Pedido do usuário\n{user_message}"
                        )
            except Exception:
                logger.exception("SERVER_TOOL PKS enrichment failed; proceeding without it")
                task = user_message

            # Location grounding: NEVER guess a city. If this turn may depend on the
            # user's location (e.g. weather) and the user didn't name a city, resolve
            # it (PKS authoritative home → device GPS) and tell the agent; if unknown,
            # instruct it to ASK (and learn). Best-effort — must not block execution.
            if user_id and self.pks and self._is_location_relevant(decision):
                try:
                    has_city = bool(str((decision.tool_arguments or {}).get("city") or "").strip())
                    if not has_city:
                        loc = await self.pks.get_user_location(user_id=user_id)
                        if loc:
                            task = (
                                f"{task}\n\n[Localização do usuário: {loc['full']} (fonte: PKS). "
                                f"Se o usuário não especificar outra cidade, use "
                                f"get_weather(city='{loc['city']}').]"
                            )
                        elif geo:
                            task = (
                                f"{task}\n\n[Localização do usuário por GPS: lat={geo['lat']}, "
                                f"lon={geo['lon']}. Se o usuário não especificar uma cidade, use "
                                f"get_weather(lat={geo['lat']}, lon={geo['lon']}).]"
                            )
                        else:
                            task = (
                                f"{task}\n\n[Localização do usuário desconhecida. Se o pedido "
                                f"depender de localização, PERGUNTE de qual cidade — não invente. "
                                f"Quando ele responder, grave com get_user_location(save_city=...).]"
                            )
                except Exception:
                    logger.exception("SERVER_TOOL location grounding failed; proceeding without it")

            # Modo Live: pede resposta falada/sem markdown (anexa ao task em vez de
            # sobrescrever o system prompt, preservando as instruções de tools do agente).
            if live:
                task = f"{task}\n\n{_LIVE_STYLE_INSTRUCTION}"

            # Entrada por voz: mesmo mecanismo (sufixo no task). Vale ainda MAIS aqui
            # do que no DIRECT_RESPONSE — é o caminho que executa tool com argumento
            # literal, e é onde um token mal transcrito viraria caminho/id inventado.
            if self._is_voice_input(metadata) or str(session_id) in self._voice_input_sessions:
                task = f"{task}\n\n{_VOICE_INPUT_INSTRUCTION}"

            # Registro da situação: mesmo mecanismo do live (sufixo no task),
            # já que o SERVER_TOOL não passa pelo PromptContextSelector.
            if register is not None:
                task = f"{task}\n\n{render_register_directive(register)}"

            # Use AgentExecutor for multi-step tool execution
            result = await self.agent.execute(
                task=task,
                session_id=session_id,
                memory=memory,
                tool_executor=ctx_executor,
                # Sem isto o agente enxerga o registry INTEIRO, gated incluso —
                # ver `_server_tool_toolset`.
                available_tools=self._server_tool_toolset(),
            )

            final_text = result.get("final_response", "Tarefa concluída.")
            iterations = result.get("iterations", 1)
            tool_ms = (time.monotonic() - t0) * 1000
            # O status vem do AgentExecutor: "completed" | "failed" | "timeout".
            # Gravar success=True fixo escondia a falha — o log dizia "Sucesso"
            # embaixo de "Todas as ferramentas falharam".
            ok = result.get("status") == ExecutionStatus.COMPLETED.value
            if slog:
                slog.log_tool_result(
                    decision.tool_name or "agent",
                    success=ok,
                    result=final_text if ok else None,
                    error=None if ok else final_text,
                    duration_ms=tool_ms,
                    execution="server-side",
                )

        except Exception as e:
            logger.exception("Server tool execution failed")
            final_text = f"Erro na execução: {str(e)}"
            iterations = 1
            tool_ms = (time.monotonic() - t0) * 1000
            if slog:
                slog.log_tool_result(decision.tool_name or "agent", success=False, error=str(e), duration_ms=tool_ms, execution="server-side")

        # Save response
        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=final_text,
            tool_name=decision.tool_name,
            generate_embedding=False,
            user_id=user_id,
        )
        if slog:
            slog.log_message_saved("assistant", final_text, str(session_id))
            slog.log_final_response(final_text, "SERVER_TOOL", iterations=iterations)

        yield AIServiceResponse(
            intent=IntentType.SERVER_TOOL,
            session_id=session_id,
            message=final_text,
            is_final=True,
            iterations=iterations,
        )

    # ---------------------------------------------------------------
    # Handler: WHATSAPP_MESSAGING — skill-based WhatsApp mission
    # ---------------------------------------------------------------

    async def _handle_whatsapp_messaging(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        source_message_id: UUID | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle WhatsApp messaging intent — delegates to SkillExecutor."""

        skill_name = decision.skill_name or "whatsapp_messaging"

        if slog:
            slog.log_event(
                "WhatsApp Messaging",
                f"Skill: {skill_name} | Mensagem: {user_message[:80]}",
            )

        yield AIServiceResponse(
            intent=IntentType.WHATSAPP_MESSAGING,
            session_id=session_id,
            message=f"Iniciando missão WhatsApp...",
            is_final=False,
        )

        try:
            skill_executor = self._skill_executor
            if not skill_executor:
                from core.skills.skill_executor import SkillExecutor
                from core.notifications import NotificationBroker
                broker = NotificationBroker()
                skill_executor = SkillExecutor(
                    agent_executor=self.agent,
                    db=self.db,
                    notification_broker=broker,
                    pks=self.pks,
                )
                self._skill_executor = skill_executor

            extra: dict[str, Any] = dict(metadata or {})
            if decision.tool_arguments:
                extra["tool_arguments"] = decision.tool_arguments
            if decision.tool_name:
                extra["router_tool_name"] = decision.tool_name

            mission_id = None
            result = await skill_executor.execute(
                skill_name=skill_name,
                session_id=session_id,
                user_message=user_message,
                conversation_history=history,
                user_id=user_id,
                extra=extra,
            )

            if result.ok:
                _raw_text = result.message
                final_text = _raw_text  # keep [MISSION:...] marker in gRPC response so desktop can parse it
                _stored_text, _mission_payload = _strip_mission_marker(_raw_text)
                _mission_meta = None
                if result.mission_id:
                    mission_id = result.mission_id
                    mission = await self.db.get_mission(mission_id)
                    result_extra = getattr(result, "extra", None) or {}
                    if result_extra.get("message_type") in {
                        "mission_instruction",
                        "mission_note",
                        "mission_cancelled",
                    }:
                        _mission_meta = result_extra
                    elif mission:
                        _intro = _stored_text or "Pode deixar, estou cuidando disso para voce."
                        _mission_meta = _build_mission_card_metadata(
                            mission,
                            intro=_intro,
                            result=result,
                            marker_payload=_mission_payload,
                        )
            else:
                final_text = f"Erro na missão: {result.message}"
                _stored_text = final_text
                _mission_meta = None

        except Exception as e:
            logger.exception("WhatsApp messaging execution failed")
            final_text = f"Erro ao processar missão WhatsApp: {str(e)}"
            _stored_text = final_text
            _mission_meta = None
            mission_id = None

        if mission_id and source_message_id:
            await self.db.attach_message_to_mission(source_message_id, mission_id)

        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=_stored_text,
            generate_embedding=False,
            user_id=user_id,
            mission_id=mission_id,
            metadata=_mission_meta,
        )
        if slog:
            slog.log_final_response(final_text, "WHATSAPP_MESSAGING", iterations=1)

        yield AIServiceResponse(
            intent=IntentType.WHATSAPP_MESSAGING,
            session_id=session_id,
            message=final_text,
            is_final=True,
            iterations=1,
        )

    # ---------------------------------------------------------------
    # Handler: HOME_ASSISTANT — smart-home control/query via Home Assistant
    # ---------------------------------------------------------------
    async def _handle_home_assistant(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle Home Assistant intent — delegates to SkillExecutor (immediate execution)."""

        skill_name = decision.skill_name or "home_assistant"

        if slog:
            slog.log_event(
                "Home Assistant",
                f"Skill: {skill_name} | Mensagem: {user_message[:80]}",
            )

        yield AIServiceResponse(
            intent=IntentType.HOME_ASSISTANT,
            session_id=session_id,
            message="Falando com o Home Assistant...",
            is_final=False,
        )

        try:
            skill_executor = self._skill_executor
            if not skill_executor:
                from core.skills.skill_executor import SkillExecutor
                from core.notifications import NotificationBroker
                broker = NotificationBroker()
                skill_executor = SkillExecutor(
                    agent_executor=self.agent,
                    db=self.db,
                    notification_broker=broker,
                    pks=self.pks,
                )
                self._skill_executor = skill_executor

            extra: dict[str, Any] = dict(metadata or {})
            if decision.tool_arguments:
                extra["tool_arguments"] = decision.tool_arguments
            if decision.tool_name:
                extra["router_tool_name"] = decision.tool_name

            # Afeto + birra descem para a skill (docs/modo-birra-plan.md §7 e
            # §3.7.2): o sink intercepta a atuação; a diretiva dá tom à
            # confirmação, que é a última coisa que o usuário lê no turno.
            birra_sink = await self._build_birra_sink(session_id, user_id)
            if birra_sink is not None:
                extra["birra"] = birra_sink
            affect_directive = await self._affect_directive_for_skill(user_id)
            if affect_directive:
                extra["affect_directive"] = affect_directive

            result = await skill_executor.execute(
                skill_name=skill_name,
                session_id=session_id,
                user_message=user_message,
                conversation_history=history,
                user_id=user_id,
                extra=extra,
            )
        except Exception as e:
            logger.exception("Home Assistant execution failed")
            result = None
            ha_error = str(e)
        else:
            ha_error = None

        # Skill succeeded with an actionable command → persist the HA reply.
        if result is not None and result.ok:
            final_text = result.message
            await self.sessions.add_message(
                session_id=session_id,
                role="assistant",
                content=final_text,
                generate_embedding=False,
                user_id=user_id,
            )
            if slog:
                slog.log_final_response(final_text, "HOME_ASSISTANT", iterations=1)
            yield AIServiceResponse(
                intent=IntentType.HOME_ASSISTANT,
                session_id=session_id,
                message=final_text,
                is_final=True,
                iterations=1,
            )
            return

        # No actionable command / all tools failed → don't dead-end and don't
        # persist a raw "Erro no Home Assistant" turn (it poisons future context,
        # cf. session 9d61bfba). Fall back to a normal conversational reply that
        # can actually use the history to answer the user.
        ha_outcome = ha_error or (result.message if result is not None else "sem resultado")
        logger.info("[HA-FALLBACK] HA skill not actionable, delegating to direct_response: %s", ha_outcome)
        if slog:
            slog.log_event(
                "Home Assistant — fallback para resposta direta",
                f"Resultado não acionável: {ha_outcome}",
            )
        ha_note = (
            "## Contexto: tentativa de Home Assistant\n"
            "A mensagem foi inicialmente roteada para o Home Assistant, mas não havia um "
            "comando acionável claro. NÃO afirme que uma ação FALHOU nem invente um erro: "
            "não houve comando para executar (≠ ação que deu erro). Se houver um snapshot de "
            "'Casa inteligente (tempo real)' acima, responda o estado a partir dele; senão, "
            "converse normalmente usando o histórico. Se o usuário pergunta sobre uma AÇÃO "
            "ANTERIOR (ex.: como você chegou a um valor), explique com base na conversa."
        )
        fallback_decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.6,
            explanation="Fallback de Home Assistant não acionável → resposta direta",
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        async for resp in self._handle_direct_response(
            session_id,
            user_message,
            history,
            fallback_decision,
            trace,
            system_context=ha_note,
            slog=slog,
            user_id=user_id,
            metadata=metadata,
            register=self._session_registers.get(str(session_id)),
        ):
            yield resp

    # ---------------------------------------------------------------
    # Handler: MUSIC — play via the music skill (Spotify/YouTube)
    # ---------------------------------------------------------------

    async def _handle_music(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle a MUSIC handoff — runs the music skill, then actuates.

        The skill resolves the track + decides the channel:
          - ``spotify_connect``: already played server-side → confirm here.
          - ``desktop``/``browser``: emit a client tool (open_url/launch_uri/
            media_command) to the desktop; ``process_tool_result`` confirms on the
            actual outcome (see the music branch there). See neo-music-feature.
        """
        skill_name = decision.skill_name or "music"
        if slog:
            slog.log_event("Music", f"Skill: {skill_name} | Ordem: {user_message[:80]}")

        yield AIServiceResponse(
            intent=IntentType.MUSIC,
            session_id=session_id,
            message="Buscando a música...",
            is_final=False,
        )

        try:
            skill_executor = self._skill_executor
            if not skill_executor:
                from core.skills.skill_executor import SkillExecutor
                from core.notifications import NotificationBroker
                broker = NotificationBroker()
                skill_executor = SkillExecutor(
                    agent_executor=self.agent,
                    db=self.db,
                    notification_broker=broker,
                    pks=self.pks,
                )
                self._skill_executor = skill_executor

            result = await skill_executor.execute(
                skill_name=skill_name,
                session_id=session_id,
                user_message=user_message,
                conversation_history=history,
                user_id=user_id,
                extra=dict(metadata or {}),
            )
        except Exception as e:
            logger.exception("Music execution failed")
            result = None
            music_error = str(e)
        else:
            music_error = None

        # Not actionable (not configured / not found / error) → speak it as final.
        if result is None or not result.ok:
            final_text = (result.message if result is not None else None) or (
                f"Não consegui tocar agora: {music_error or 'erro desconhecido'}."
            )
            await self.sessions.add_message(
                session_id=session_id, role="assistant", content=final_text,
                generate_embedding=False, user_id=user_id,
            )
            if slog:
                slog.log_final_response(final_text, "MUSIC", iterations=1)
            yield AIServiceResponse(
                intent=IntentType.MUSIC, session_id=session_id,
                message=final_text, is_final=True, iterations=1,
            )
            return

        actuation = (result.extra or {}).get("actuation") or {}
        channel = actuation.get("channel")
        summary = actuation.get("summary") or "a música"
        tool_name = actuation.get("tool")
        args = actuation.get("args") or {}

        # spotify_connect (already played) or nothing to dispatch → confirm now.
        if channel in (None, "spotify_connect") or not tool_name:
            final_text = result.message or f"Tocando {summary} 🎵"
            await self.sessions.add_message(
                session_id=session_id, role="assistant", content=final_text,
                generate_embedding=False, user_id=user_id,
            )
            if slog:
                slog.log_final_response(final_text, "MUSIC", iterations=1)
            yield AIServiceResponse(
                intent=IntentType.MUSIC, session_id=session_id,
                message=final_text, is_final=True, iterations=1,
            )
            return

        # Inscrição armada para este aparelho? Então a atuação leva junto o
        # `observar` (docs/playlists-skill-plan.md §7.5): é ele que faz o desktop
        # vigiar ESTA aba e avisar quando a faixa estiver acabando. Sem inscrição
        # o campo simplesmente não existe — e um "toca um AC/DC" continua sendo
        # exatamente o que era, sem vigilância nenhuma.
        args = dict(args)
        observar = await self._watch_for_actuation(user_id, metadata, args)
        if observar:
            args["observar"] = observar
            # Fila SEMPRE pela extensão. `open_url` entrega a URL ao sistema
            # operacional: abre uma aba que a ponte não escolheu, e é a ponte
            # que precisa registrar o listener de fim de faixa e travar o
            # autoplay. Com `media_command` a extensão navega a aba (criando uma
            # se não houver) e passa a mandar no player. A `url` viaja junto
            # como plano B, para o desktop abrir sozinho se a ponte estiver fora.
            video_id = str(observar.get("video_id") or "")
            if tool_name == "open_url" and video_id:
                url_original = str(args.get("url") or "")
                tool_name = "media_command"
                args = {
                    "action": "play",
                    "target": "youtube",
                    "videoId": video_id,
                    "url": url_original,
                    "observar": observar,
                }
                if observar.get("tab_id") not in (None, ""):
                    args["tabId"] = observar["tab_id"]
                logger.info(
                    "[music] fila: open_url → media_command (a extensão precisa "
                    "dirigir a aba para travar o autoplay)"
                )

        # desktop/browser → dispatch a client tool; the final confirmation comes from
        # process_tool_result (flow="music_actuation"), truthful to the real outcome.
        tool_call_id = str(uuid4())
        if slog:
            slog.log_tool_request(tool_name, arguments=args, requires_approval=False, execution="client-side")
        yield AIServiceResponse(
            intent=IntentType.MUSIC, session_id=session_id,
            message=result.message or f"Abrindo {summary}...", is_final=False,
        )
        yield AIServiceResponse(
            intent=IntentType.MUSIC,
            session_id=session_id,
            tool_request={
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": json.dumps(args, ensure_ascii=False),
                "requires_approval": False,
            },
            is_final=False,
            metadata={
                "flow": "music_actuation",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "user_message": user_message,
                "tool_arguments": args,
                "command": summary,  # carried to process_tool_result for the confirmation
            },
        )

    async def _watch_for_actuation(
        self, user_id: UUID | None, metadata: dict[str, Any] | None, args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """`observar` da atuação de música — a cola entre "mandei tocar" e a fila.

        Três decisões, todas determinísticas (nada aqui depende do modelo):

        - inscrição ARMADA e a faixa é a esperada → observa (o caso normal);
        - inscrição PAUSADA e a faixa é justamente a que ela esperava → **retoma**.
          É o que conserta "coloca a fila pra tocar de novo": o cérebro pode até
          esquecer de reinscrever, mas mandar tocar a faixa da fila JÁ é dizer
          que a fila voltou (medido em 2026-09-08: a fila não andou porque a
          inscrição tinha ficado pausada e ninguém a re-armou);
        - inscrição armada mas a faixa é OUTRA → pausa. O usuário pediu música
          fora da fila, e a fila não pode continuar por cima disso.

        Sem inscrição nenhuma devolve None e nada muda — é a primeira das três
        camadas que protegem o pedido avulso (§7.4).
        """
        router = getattr(self, "event_router", None)
        device_id = (metadata or {}).get("device_id")
        if router is None or user_id is None or not device_id:
            return None
        try:
            subs = await router._subs.list(
                user_id=user_id, device_id=str(device_id), channel="musica",
                states=("armada", "pausada"), limit=10,
            )
        except Exception as exc:  # noqa: BLE001 — barramento fora não atrapalha o play
            logger.debug("[music] inscrições indisponíveis: %s", exc)
            return None
        if not subs:
            return None

        tocando = str(args.get("videoId") or "")
        if not tocando:
            tocando = _video_id_of(str(args.get("url") or args.get("uri") or ""))

        live = next((s for s in subs if s.state == "armada" and s.is_live()), None)
        pausada = next(
            (s for s in subs if s.state == "pausada" and tocando and s.expected_ref == tocando),
            None,
        )

        if live is not None and tocando and live.expected_ref and live.expected_ref != tocando:
            # Música fora da fila: a fila para aqui, e o aparelho desarma.
            try:
                await router._subs.pause(
                    live.id, "o usuário pediu outra música — fila pausada"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[music] não pausei a inscrição: %s", exc)
            return None

        if live is None and pausada is not None:
            try:
                live = await router._subs.update(
                    pausada.id, state="armada", reason="retomada ao tocar a faixa da fila"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[music] não retomei a inscrição: %s", exc)
                return None
        if live is None:
            return None

        return {
            "inscricao_id": str(live.id),
            "video_id": tocando or live.expected_ref,
            # `tab_id` só quando a atuação REUSOU uma aba conhecida. Num
            # `open_url` a música abre numa aba nova e o id antigo mandaria a
            # vigia olhar a aba errada — melhor a extensão achar pelo vídeo.
            "tab_id": args.get("tabId"),
            # Código do item da fila que está sendo tocado: volta em cada evento
            # e é ele que marca o status na linha certa.
            "codigo": getattr(live, "current_code", ""),
        }

    async def _resolve_file_by_name(
        self, candidate: str, session_id: UUID | None
    ) -> Any | None:
        """Casa um `file_id` que na verdade é um NOME contra o catálogo.

        Só resolve quando não há dúvida: um nome exato, e — havendo mais de um
        arquivo com o mesmo nome (gerar o mesmo zip duas vezes é rotina) — o mais
        recente da sessão, senão o mais recente no geral. Um casamento por
        aproximação aqui entregaria o arquivo ERRADO ao usuário, que é pior do que
        não entregar; por isso nada de `ilike %x%` decidindo sozinho.
        """
        name = (candidate or "").strip().strip("'\"")
        # Um UUID que não existe é id alucinado de verdade, não nome — não inventar.
        if not name or "/" in name or _UUID_LIKE.match(name):
            return None
        try:
            found = await self.file_store.list_files(q=name, limit=50)
        except Exception as e:  # noqa: BLE001 — resolução best-effort nunca derruba o turno
            logger.warning("[CLIENT_TOOL] lookup de arquivo por nome falhou: %s", e)
            return None
        exact = [f for f in found if (f.name or "").lower() == name.lower()]
        if not exact:
            return None
        same_session = [f for f in exact if session_id and f.session_id == session_id]
        pool = same_session or exact
        return max(pool, key=lambda f: f.created_at)

    async def _verify_promise_continuation(
        self,
        user_message: str,
        reply_text: str,
        work_note: str | None,
        dispatched: list[str] | None = None,
    ) -> "Handoff | None":
        """Backstop: a reply that PROMISES work but emitted no [[HANDOFF:continuar]].

        The prompt contract alone does not hold. Measured on the first end-to-end run
        of this feature: the instruction was injected verbatim, and the model still
        closed with "Vou ver os nomes reais e baixar os melhores pro site" and no
        block — the same failure the feature exists to remove. This mirrors the music
        verifier: a cheap, temperature-0 call decides whether the reply intended to
        keep working and, if so, writes the concrete next order.

        Fails safe to None (turn simply ends, as it does today) on any error.
        """
        # Deterministic pre-filter: only pay for the call when the reply actually
        # reads like a promise. Keeps every ordinary cognitive turn at zero cost.
        if not _looks_like_promise(reply_text):
            return None

        try:
            from modules.intent_config import get_classifier_config
            cfg = await get_classifier_config(self.db)
            provider = self.llm.get_provider(cfg["provider"])
            model = cfg["model"]
        except Exception:
            try:
                provider = self.llm.get_provider()
                model = None
            except Exception:
                return None

        # Regra extra só quando há o que despachar: turno sem handoff mantém o
        # prompt idêntico ao de antes, e o modelo nunca lê sobre uma seção ausente.
        dispatch_rule = (
            "promised=false TAMBÉM quando o trabalho anunciado JÁ É uma das ações "
            "listadas em \"Ações já despachadas\": elas foram disparadas neste mesmo "
            "turno e já estão acontecendo — a frase é o assistente narrando o que "
            "despachou, não uma pendência. Só responda true nesse caso se a resposta "
            "prometer algo A MAIS que nenhuma ação despachada cobre, e então `order` "
            "é só essa sobra.\n"
        ) if dispatched else ""

        sys = (
            "Você decide se a resposta do assistente PROMETEU continuar trabalhando "
            "agora sem ter entregado o resultado. Responda SÓ com JSON válido, sem "
            'markdown: {"promised":true|false,"order":"a próxima ação concreta no '
            'imperativo, ou \\"\\""}.\n'
            "promised=true quando a resposta anuncia trabalho futuro que o próprio "
            "assistente faria (\"vou baixar\", \"sigo tentando\", \"já te mando\", "
            "\"deixa comigo\", \"vou ver os nomes e baixar\").\n"
            "promised=false quando a resposta ENTREGA o resultado, faz uma PERGUNTA e "
            "espera decisão do usuário, recusa, ou é só conversa.\n"
            + dispatch_rule
            + "`order` deve ser a ação técnica concreta que falta, derivada do que a "
            "resposta prometeu — nunca uma repetição do que já falhou."
        )
        user = f"Pedido original: {user_message}\nResposta do Neo: {reply_text}"
        if dispatched:
            user += "\nAções já despachadas neste turno (já vão acontecer):\n" + "\n".join(
                f"- {d}" for d in dispatched
            )
        if work_note:
            user += f"\nO que já foi feito neste turno:\n{work_note}"

        try:
            resp = await provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=sys),
                    Message(role=MessageRole.USER, content=user),
                ],
                tools=None,
                model=model,
                temperature=0.0,
                # 160 tokens + raciocínio ligado = resposta VAZIA, e o backstop cai
                # no except sem nunca decidir nada. Medido: com o classificador em
                # openrouter/qwen3.5-flash este verificador falhou com
                # "Expecting value: line 1 column 1 (char 0)" enquanto a resposta do
                # Neo dizia "Vou de outra fonte livre" — exatamente o turno que o
                # ciclo devia ter continuado. O reasoning vem LIGADO por padrão no
                # OpenRouter e consome o orçamento antes de sobrar conteúdo, então
                # `disable_thinking` é obrigatório aqui, e a folga de tokens é a
                # segunda linha de defesa se algum provider ignorar a intenção.
                max_tokens=700,
                disable_thinking=True,
            )
            raw = (resp.get_content() or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(raw)
        except Exception as e:
            logger.warning("[SELF-HANDOFF] verifier failed (turn ends as before): %s", e)
            return None

        if not data.get("promised"):
            return None
        order = str(data.get("order") or "").strip()
        if not order:
            return None
        return Handoff(
            target_intent=IntentType.DIRECT_RESPONSE,
            input_text=order,
            reason="promise_verifier",
            is_self=True,
        )

    async def _verify_music_decision(
        self, user_message: str, reply_text: str
    ) -> "Handoff | None":
        """Safety net: when a music turn emitted neither token, decide via a cheap call.

        Returns a synthesized ``Handoff`` to the music skill when the reply intended
        to play, else None. Fails safe to None (no play) on any error — never plays
        against a refusal. See neo-music-feature.
        """
        try:
            from modules.intent_config import get_classifier_config
            cfg = await get_classifier_config(self.db)
            provider = self.llm.get_provider(cfg["provider"])
            model = cfg["model"]
        except Exception:
            try:
                provider = self.llm.get_provider()
                model = None
            except Exception:
                return None

        sys = (
            "Você decide o que a resposta do assistente PRETENDE fazer com a música AGORA. "
            "Responda SÓ com JSON válido, sem markdown: "
            '{"action":"play|pause|resume|next|previous|mute|volume_up|volume_down|refuse|clarify",'
            '"query":"faixa/artista a buscar (só p/ play) ou \\"\\"","service_hint":"spotify|youtube|null"}. '
            "play = a resposta diz que vai/está tocando algo concreto. "
            "pause/resume/next/previous/mute/volume_* = a resposta diz que vai pausar/retomar/"
            "pular/voltar/mutar/mudar volume do que já está tocando. "
            "Recusa, zoeira, dúvida ou só conversa = refuse/clarify."
        )
        try:
            resp = await provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=sys),
                    Message(role=MessageRole.USER, content=f"Pedido: {user_message}\nResposta do Neo: {reply_text}"),
                ],
                tools=None,
                model=model,
                temperature=0.0,
                # Mesmo conserto do verificador de promessa (§5 do
                # docs/self-continuation-plan.md): o OpenRouter liga raciocínio por
                # DEFAULT e o ignora no max_tokens, então com o teto curto a resposta
                # voltava sendo "Thinking Process:…" — content não-JSON, `except`, e
                # o verificador falhava EM SILÊNCIO (música que não toca sem motivo
                # aparente). Ver memória openrouter-reasoning-default-on.
                max_tokens=700,
                disable_thinking=True,
            )
            raw = (resp.get_content() or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(raw)
        except Exception as e:
            logger.warning("Music verifier failed (no synthetic play): %s", e)
            return None

        action = str(data.get("action", "")).lower()
        # Control actions → synthesize a handoff carrying the action WORD (the skill's
        # detect_action picks it up). Covers "vou pausar" said without the token.
        _control_word = {
            "pause": "pausar", "resume": "retomar", "next": "próxima",
            "previous": "anterior", "mute": "mudo",
            "volume_up": "aumentar volume", "volume_down": "abaixar volume",
        }
        if action in _control_word:
            return Handoff(
                target_intent=IntentType.MUSIC,
                input_text=_control_word[action],
                target_skill="music",
                reason="music_verifier_control",
            )
        if action != "play":
            return None
        query = str(data.get("query") or "").strip()
        if not query:
            return None
        hint = str(data.get("service_hint") or "").lower()
        order = query + (f" no {hint}" if hint in ("spotify", "youtube") else "")
        return Handoff(
            target_intent=IntentType.MUSIC,
            input_text=order,
            target_skill="music",
            reason="music_verifier",
        )

    # ---------------------------------------------------------------
    # Handler: CLIENT_TOOL — tools running on user's desktop
    # ---------------------------------------------------------------

    def _known_client_tool_names(self) -> set[str]:
        """Nomes de tools client-side realmente registradas no ToolRegistry.

        Um passo de plano (``PlanManager``, LLM próprio) pode nomear um
        ``tool_name`` fora do catálogo real — a mesma classe de bug do
        classificador (``intent_router._valid_client_tool_names``), só que
        vinda do planner em vez do router. Despachar isso pro desktop trava
        o turno esperando um handler que não existe. ``getattr`` defensivo:
        alguns testes constroem ``AIService`` via ``object.__new__`` sem
        wireiar ``self.tools`` — nesse caso a checagem é pulada (fail open),
        igual ao padrão já usado para ``self.file_store``.
        """
        registry = getattr(self, "tools", None)
        if registry is None:
            return set()
        return {t.name for t in registry.get_all() if t.execution == "client-side"}

    def _client_tool_name_is_unknown(self, tool_name: str) -> bool:
        """True só quando o registry está wireado E o nome não está nele.

        Empty-set (``self.tools`` ausente — testes com ``object.__new__``)
        significa "não sei validar", não "nada é válido" — por isso não basta
        checar ``tool_name not in known``.
        """
        known = self._known_client_tool_names()
        return bool(known) and tool_name not in known

    def _plan_step_dispatch(
        self,
        plan: "ExecutionPlan",
        step: "PlanStep",
        *,
        session_id: UUID,
        user_message: str,
    ) -> list[AIServiceResponse]:
        """Respostas que despacham UM passo do plano, na ordem de envio.

        São sempre DUAS mensagens separadas, nunca uma só:

        1. **`plan_update`** (sem texto) — é o que desenha a timeline numerada no
           desktop (``PlanTracker``). Tem que ir sozinha porque o ``oneof`` do
           proto só carrega um conteúdo por ``CoreMessage``: se o mesmo response
           trouxer `message` E `plan_update` na metadata, o `_to_core_message`
           manda o plano e **descarta o texto**. Era por isso que o usuário via
           o plano cru (``plan.to_summary()``, com ⬜/✅ em markdown) numa bolha
           de "Pensando" em vez do cartão: o cartão nunca era emitido na criação
           do plano, só o texto.
        2. **o pedido em si** — `command_request` quando o passo é destrutivo
           (``requires_approval``), `tool_request` quando não é.

        A distinção do item 2 é o que impede o travamento: o desktop **não tem
        UI para aprovar um `tool_request`**. Mandar um com `requires_approval=true`
        é esperar por um resultado que ninguém pode autorizar. O `command_request`,
        sim, já tem o card de aprovar/recusar — e o `_has_plan` sobrevive à
        aprovação porque o gRPC funde a metadata pendente antiga com a nova.
        """
        arguments = step.tool_arguments or {}
        command = step.raw_command or arguments.get("command") or step.tool_name
        common_meta = {
            "user_message": user_message,
            "tool_arguments": arguments,
            "command": command,
            "_has_plan": True,
        }

        if step.requires_approval:
            command_id = str(uuid4())
            dispatch = AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                command_request={
                    "command_id": command_id,
                    "command": command,
                    "explanation": step.description,
                    "is_background": False,
                },
                is_final=False,
                metadata={**common_meta, "flow": "client_command", "command_id": command_id},
            )
        else:
            tool_call_id = str(uuid4())
            dispatch = AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                tool_request={
                    "tool_call_id": tool_call_id,
                    "tool_name": step.tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                    "requires_approval": False,
                },
                is_final=False,
                metadata={
                    **common_meta,
                    "flow": "client_tool",
                    "tool_call_id": tool_call_id,
                    "tool_name": step.tool_name,
                },
            )

        return [
            AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                is_final=False,
                metadata={"plan_update": self.planner.build_plan_update(plan)},
            ),
            dispatch,
        ]

    def _plan_final_update(self, plan: "ExecutionPlan", session_id: UUID) -> AIServiceResponse:
        """Último `plan_update` de um plano — fecha a timeline no cliente.

        Sem isso o cartão fica congelado com o passo final em "running" (bolinha
        pulsando) mesmo depois da resposta final chegar.
        """
        return AIServiceResponse(
            intent=IntentType.CLIENT_TOOL,
            session_id=session_id,
            is_final=False,
            metadata={"plan_update": self.planner.build_plan_update(plan)},
        )

    async def _handle_client_tool(
        self,
        session_id: UUID,
        user_message: str,
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle client-side tool — sends request to desktop for execution.

        This yields a non-final response containing a command_request or tool_request
        that the gRPC controller sends to the desktop client.
        The desktop executes and sends back a tool_result which continues the flow.

        For complex multi-step tasks, creates an ExecutionPlan and sends steps one by one.
        """
        command_id = str(uuid4())
        tool_call_id = str(uuid4())

        # ── PLAN CHECK: use classifier's needs_plan (no extra LLM call) ──
        sys_context = self._build_system_context(metadata)
        needs_plan = decision.needs_plan

        if needs_plan:
            plan = await self.planner.generate_plan(
                user_message=user_message,
                system_context=sys_context,
                conversation_history=history,
            )

            if plan and plan.steps:
                sid_str = str(session_id)
                self.planner.store_plan(sid_str, plan)

                if slog:
                    slog.log_event(
                        "Plano Criado",
                        f"**Objetivo:** {plan.goal}\n"
                        f"**Passos:** {len(plan.steps)}\n\n"
                        + "\n".join(f"{i+1}. {s.description}" for i, s in enumerate(plan.steps)),
                    )

                # Send first step
                first_step = plan.get_current_step()
                if first_step:
                    # Mesma trava do despacho direto: um passo `run_command` sem
                    # comando dentro trava o turno pra sempre. O planner monta o passo
                    # a partir de JSON de LLM, então `tool_arguments` pode vir vazio e
                    # `_validate_plan` (que só olha descrição/contagem) deixa passar —
                    # foi assim na sessão e8e5664b.
                    if _dispatch_would_hang(first_step.tool_name, first_step.tool_arguments):
                        logger.error(
                            "[PLAN] passo 1 é run_command SEM comando — abortando plano "
                            "(evita turno infinito). step=%r", first_step.tool_arguments,
                        )
                        self.planner.clear_plans(sid_str)
                        if slog:
                            slog.log_event(
                                "Plano abortado (passo sem comando)",
                                "O primeiro passo do plano era `run_command` sem nenhum "
                                "comando — despachar isso prenderia o turno em "
                                "'Executando' até a conexão cair.",
                            )
                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            message=(
                                "Montei um plano furado pra isso — o primeiro passo saiu "
                                "sem comando nenhum. Repete o pedido que eu faço direto."
                            ),
                            is_final=True,
                        )
                        return
                    if self._client_tool_name_is_unknown(first_step.tool_name):
                        logger.warning(
                            "[PLAN] passo com tool_name desconhecido %r (fora do catálogo) — abortando plano",
                            first_step.tool_name,
                        )
                        self.planner.clear_plans(sid_str)
                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            message=(
                                "Não consigo executar esse plano — um dos passos usa uma "
                                "ferramenta que não existe mais no meu catálogo. Peça de novo "
                                "que eu tento resolver de outro jeito."
                            ),
                            is_final=True,
                        )
                        return
                    first_step.status = "running"
                    for resp in self._plan_step_dispatch(
                        plan, first_step, session_id=session_id, user_message=user_message
                    ):
                        yield resp

                    self._pending_traces[str(session_id)] = trace
                    return  # Flow continues in process_tool_result

            # ── Plano exigido mas NÃO gerado → cognitivo, nunca o dispatch abaixo ──
            # O `needs_plan` chega aqui de dois jeitos: do classificador, ou FORÇADO
            # pelo guard de grounding — que, ao forçar, zera `raw_command` e
            # `tool_arguments` de propósito. Se `generate_plan` devolve None (LLM
            # vazio/JSON inválido: deepseek-empty-response-reasoning), o fluxo caía no
            # despacho genérico lá embaixo e mandava `run_command` com `{}` pro
            # desktop — que não tem o que executar e NUNCA devolve resultado: turno
            # infinito (sessão 9f7abeec, "quais chaves de ssh eu tenho?").
            # O cognitivo é o destino certo: ele tem histórico, PKS e o próprio
            # `run_command` client-actuating pra descobrir o que o plano descobriria.
            logger.warning(
                "[PLAN] needs_plan sem plano utilizável — delegando ao núcleo cognitivo "
                "(em vez de despachar comando vazio)"
            )
            if slog:
                slog.log_event(
                    "Plano não gerado — fallback cognitivo",
                    "O passo exigia um plano de verificação e o planner não devolveu "
                    "nenhum passo utilizável. Em vez de despachar um comando vazio "
                    "(que trava o turno), o núcleo cognitivo assume — ele descobre o "
                    "que precisa com `run_command`.",
                )
            brain_decision = RoutingDecision(
                intent=IntentType.DIRECT_RESPONSE,
                confidence=decision.confidence,
                explanation="plano não gerado — núcleo cognitivo assume",
                needs_memory=decision.needs_memory,
                knowledge_keywords=decision.knowledge_keywords,
                needs_plan=False,
                speech_act=decision.speech_act,
                gravity=decision.gravity,
                register_hint=decision.register_hint,
            )
            async for resp in self._handle_direct_response(
                session_id, user_message, history or [], brain_decision, trace,
                sys_context, slog=slog, user_id=user_id, metadata=metadata,
            ):
                yield resp
            return

        # ── Trava determinística: nenhum comando cruza sandbox↔disco do usuário ──
        # O classificador às vezes ignora a instrução de prompt sobre isso (sessão
        # ef4f723c: a explicação estava no contexto dessa MESMA chamada e ele
        # gerou `mv /workspace/...` mesmo assim — prosa não é 100% confiável).
        # Checagem de string é: bloqueia aqui, sem depender só do prompt.
        _cmd_to_check = decision.raw_command or (decision.tool_arguments or {}).get("command")
        if _cmd_to_check and "/workspace/" in _cmd_to_check:
            logger.warning(f"[CLIENT_TOOL] comando referenciando /workspace bloqueado: {_cmd_to_check}")
            if slog:
                slog.log_event(
                    "Comando bloqueado (sandbox ≠ disco do usuário)",
                    "O classificador gerou um comando referenciando `/workspace/` "
                    "— caminho do sandbox remoto, inacessível do disco do usuário. "
                    f"Bloqueado antes de despachar.\n\n> {_cmd_to_check}",
                )
            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message=(
                    "Não dá pra mover isso por comando direto — esse arquivo existe "
                    "só no meu sandbox, não no seu disco. Baixe pelo botão que "
                    "aparece na conversa, ou peça de novo pra eu colocar na área de "
                    "trabalho que uso a ferramenta certa pra isso."
                ),
                is_final=True,
            )
            return

        # ── No plan needed — existing single-step flow ──
        if decision.raw_command:
            if slog:
                slog.log_tool_request("raw_command", command=decision.raw_command, requires_approval=True, execution="client-side")

            # Shell command needs approval
            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message="Preciso de permissão para executar um comando no seu terminal.",
                is_final=False,
            )

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                command_request={
                    "command_id": command_id,
                    "command": decision.raw_command,
                    "explanation": decision.explanation or "Executar comando no terminal local.",
                    "is_background": False,
                },
                is_final=False,
                metadata={
                    "flow": "client_command",
                    "command_id": command_id,
                    "command": decision.raw_command,
                    "user_message": user_message,
                },
            )
        else:
            # Named tool request
            tool_name = decision.tool_name or "run_command"
            arguments = decision.tool_arguments or {}

            # O classificador não tem como saber o file_id real — ele só existe
            # depois de um `file_save` do núcleo cognitivo. Sem essa checagem, um
            # file_id truncado/alucinado (sessão bd7a304d: "f8d", visto uma vez como
            # prefixo do id de uma TRANSAÇÃO financeira não relacionada que aparecia
            # no histórico) chega intacto no client e só falha depois — 404, já tendo
            # prometido o arquivo pro usuário. Falha fechada: sem file_id válido E
            # existente no catálogo, nem despacha.
            if tool_name == "save_generated_file" and self.file_store is not None:
                file_id = arguments.get("file_id")
                stored = await self.file_store.get(file_id) if file_id else None
                # O erro mais comum não é id alucinado: é o classificador pôr o NOME
                # do arquivo no campo do id ("portifolio-naruto.zip"), porque é isso
                # que ele vê no histórico. Medido na session-b407c213, onde o zip
                # existia no catálogo (f17db260…, gerado 4 min antes) e mesmo assim o
                # turno morreu num "peça de novo". Resolver o nome ANTES de desistir
                # não relaxa a trava — o arquivo tem que existir de verdade, com id
                # real; só deixa de exigir que o classificador adivinhe um UUID.
                if stored is None and file_id:
                    stored = await self._resolve_file_by_name(str(file_id), session_id)
                    if stored is not None:
                        arguments = {**arguments, "file_id": str(stored.id)}
                        logger.info(
                            "[CLIENT_TOOL] file_id %r resolvido por nome → %s",
                            file_id, stored.id,
                        )
                        if slog:
                            slog.log_event(
                                "file_id resolvido por nome",
                                f"O classificador mandou {file_id!r} (nome, não id). "
                                f"Casou com o arquivo {stored.name!r} (id={stored.id}, "
                                f"{stored.size_bytes} bytes) — seguindo com o id real.",
                            )
                if stored is None:
                    logger.warning(
                        "[CLIENT_TOOL] save_generated_file com file_id inválido/inexistente: %r",
                        file_id,
                    )
                    if slog:
                        slog.log_event(
                            "save_generated_file bloqueado (file_id não fundamentado)",
                            f"O classificador pediu file_id={file_id!r}, que não existe "
                            "no catálogo de arquivos. Bloqueado antes de despachar pro "
                            "desktop — dispatchar isso só resultaria num 404 depois de "
                            "já ter prometido o arquivo pro usuário.",
                        )
                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        message=(
                            "Não achei esse arquivo no meu catálogo — ou ele ainda não "
                            "foi gerado de verdade, ou o id que eu ia usar não é válido. "
                            "Peça de novo que eu gero e te entrego certo."
                        ),
                        is_final=True,
                    )
                    return

            # ── Falha fechada: `run_command` sem comando é travamento garantido ──
            # O desktop recebe o tool_request, não tem o que executar e nunca devolve
            # tool_result — o turno fica "Executando" até a conexão cair (mesmo modo de
            # falha das tools fantasma documentadas em modules/desktop/tools.py). Vale
            # como rede: as origens conhecidas já foram fechadas acima, mas nenhuma
            # delas pode voltar a vazar um dispatch vazio pro cliente.
            if _dispatch_would_hang(tool_name, arguments):
                logger.error(
                    "[CLIENT_TOOL] run_command sem 'command' — bloqueado antes do "
                    "dispatch (evita turno infinito). arguments=%r", arguments,
                )
                if slog:
                    slog.log_event(
                        "run_command bloqueado (sem comando)",
                        "Ia despachar `run_command` sem nenhum comando dentro — o "
                        "desktop não teria o que executar e o turno ficaria preso em "
                        "'Executando' até a conexão cair. Bloqueado.",
                    )
                yield AIServiceResponse(
                    intent=IntentType.CLIENT_TOOL,
                    session_id=session_id,
                    message=(
                        "Me embananei montando o comando e ia mandar um vazio. "
                        "Repete o pedido que eu faço direito."
                    ),
                    is_final=True,
                )
                return

            logger.info(f"[DEBUG] _handle_client_tool: tool_name='{tool_name}', arguments={arguments}")
            if slog:
                slog.log_tool_request(tool_name, arguments=arguments, requires_approval=decision.requires_approval, execution="client-side")

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message=f"Solicitando execução de {tool_name} no desktop...",
                is_final=False,
            )

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                tool_request={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                    "requires_approval": decision.requires_approval,
                },
                is_final=False,
                metadata={
                    "flow": "client_tool",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "user_message": user_message,
                    "tool_arguments": arguments,
                    # Store the actual shell command, not the tool name (e.g. "audacity" not "run_command")
                    "command": arguments.get("command") or arguments.get("cmd") or tool_name,
                },
            )

    # ---------------------------------------------------------------
    # Arquivos sob iniciativa do cérebro (client-actuating: pull/push)
    # ---------------------------------------------------------------

    async def _file_path_is_grounded(
        self, path: str, session_id: UUID, user_message: str, user_id: UUID | None = None
    ) -> bool:
        """True se ``path`` for um alvo LEGÍTIMO — não um caminho inventado.

        Mesma família da guarda do CLIENT_TOOL (``_ungrounded_path_literals``): o
        cérebro nunca deve subir um caminho que ELE inventou a partir do nome do
        arquivo. Duas fontes tornam um caminho legítimo:

        * o usuário escreveu (o caminho inteiro ou só o nome) nesta conversa;
        * o arquivo está no REGISTRO DE ARTEFATOS — ou seja, o próprio Neo o pôs
          no disco do usuário e sabe onde. Isso é evidência de disco, não chute,
          e é exatamente o critério que o roteador já usa
          (``IntentRouter._resolve_artifact_paths``).

        A segunda fonte faltava aqui, e o preço foi a sessão 97774390: o Neo
        baixou a NFSe, conferiu com um ``ls`` aprovado, anunciou o caminho — e no
        turno seguinte a guarda tratou o próprio achado como invenção, trocou
        ``mode="path"`` por um seletor de arquivo e, quando o usuário não
        escolheu nada, fechou o turno com "não recebi nenhum arquivo".
        """
        needle = (path or "").strip()
        if not needle:
            return False
        base = needle.rsplit("/", 1)[-1]
        haystacks = [user_message or ""]
        try:
            history = await self.sessions.get_history(session_id, limit=12, max_tokens_budget=1500)
            haystacks += [m.get("content", "") for m in history if m.get("role") == "user"]
        except Exception as exc:  # noqa: BLE001 — sem histórico ⇒ só o turno atual
            logger.warning("[files] grounding: histórico indisponível: %s", exc)
        blob = "\n".join(haystacks)
        # Caminho inteiro citado, ou o nome-base (o usuário costuma dizer só o nome).
        if needle in blob or (len(base) >= 3 and base in blob):
            return True

        # Registro de artefatos: arquivo que o Neo mesmo pôs lá. Best-effort —
        # registro indisponível só faz a guarda voltar a ser o que era.
        try:
            _, artifact_paths = await self._recent_artifacts(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[files] grounding: registro de artefatos indisponível: %s", exc)
            return False
        if not artifact_paths:
            return False
        conhecido = artifact_paths.get(base.lower())
        if conhecido and conhecido.rstrip("/") == needle.rstrip("/"):
            logger.info("[files] caminho fundamentado pelo registro de artefatos: %r", needle)
            return True
        return False

    async def _emit_client_actuation(
        self,
        car: "ClientActuationRequested",
        *,
        session_id: UUID,
        user_message: str,
        user_id: UUID | None,
        metadata: dict[str, Any] | None,
        trace: "MessageTrace | None",
        slog: "SessionFileLogger | None",
        depth: int = 0,
    ) -> AsyncIterator[AIServiceResponse]:
        """Emite o ``tool_request`` de uma tool de arquivo client-actuating.

        Faz as guardas determinísticas (file_id fundamentado no push; caminho
        fundamentado no pull por path), guarda o estado de retomada em
        ``_pending_actuations`` e devolve um turno NÃO-final com o ``tool_request``.
        O desktop executa e o turno retoma em ``process_tool_result``.
        """
        tool_name = car.tool_name
        args = dict(car.arguments or {})
        tool_call_id = car.tool_call_id
        sid = str(session_id)

        if depth >= _MAX_ACTUATION_DEPTH:
            final_text = (
                "Fiz várias idas e vindas seguidas na sua máquina e vou parar aqui pra não "
                "entrar em laço. Me diga em uma frase o que você quer que eu faça."
            )
            await self.sessions.add_message(
                session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
            )
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                message=final_text, is_final=True, iterations=1,
            )
            return

        # ── Shell pedido pelo CÉREBRO: vira o mesmo command_request do CLIENT_TOOL ──
        # (aprovação do usuário inclusa; o resultado volta como `execute_command` e
        # _resume_after_command retoma o turno cognitivo com a saída real).
        if tool_name in _SHELL_ACTUATION_TOOLS:
            command = str(args.get("command") or "").strip()
            if not command:
                final_text = (
                    "Ia rodar um comando aqui pra te responder, mas montei ele vazio. "
                    "Me repete o que você quer que eu verifique."
                )
                await self.sessions.add_message(
                    session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
                )
                yield AIServiceResponse(
                    intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                    message=final_text, is_final=True, iterations=1,
                )
                return
            # Mesma trava determinística do _handle_client_tool: o sandbox do cérebro
            # e o disco do usuário são máquinas diferentes, e o modelo às vezes esquece.
            if "/workspace/" in command:
                final_text = (
                    "Esse caminho existe só no meu sandbox, não no seu disco — não dá pra "
                    "alcançar por comando na sua máquina. Me diga o caminho do seu lado, "
                    "ou peça pra eu te entregar o arquivo."
                )
                logger.warning("[brain-shell] comando referenciando /workspace bloqueado: %s", command)
                await self.sessions.add_message(
                    session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
                )
                yield AIServiceResponse(
                    intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                    message=final_text, is_final=True, iterations=1,
                )
                return

            command_id = str(uuid4())
            self._pending_actuations[sid] = {
                "tool_name": "run_command",
                "kind": "shell",
                "command": command,
                "user_message": user_message,
                "user_id": str(user_id) if user_id else None,
                "metadata": metadata or {},
                "depth": depth,
            }
            if trace is not None:
                self._pending_traces[sid] = trace
            if slog:
                slog.log_tool_request(
                    "run_command (cérebro)", command=command, requires_approval=True,
                    execution="client-side",
                )
            reason = str(args.get("reason") or "").strip()
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                message=reason or "Preciso de permissão para executar um comando no seu terminal.",
                is_final=False,
            )
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                command_request={
                    "command_id": command_id,
                    "command": command,
                    "explanation": reason or "Executar comando no terminal local.",
                    "is_background": False,
                },
                is_final=False,
                metadata={
                    "flow": "client_command",
                    "command_id": command_id,
                    "command": command,
                    "user_message": user_message,
                },
            )
            return

        # ── Guarda: push com file_id fundamentado (mesma trava do _handle_client_tool) ──
        if tool_name == "save_generated_file":
            args.setdefault("destination", "desktop")
            if self.file_store is not None:
                file_id = args.get("file_id")
                stored = await self.file_store.get(file_id) if file_id else None
                if stored is None:
                    logger.warning("[files] save_generated_file (cérebro) com file_id inválido: %r", file_id)
                    final_text = (
                        "Não achei esse arquivo no meu catálogo pra entregar — talvez ele ainda "
                        "não tenha sido gerado de verdade. Deixa eu gerar primeiro e aí te entrego."
                    )
                    await self.sessions.add_message(
                        session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
                    )
                    yield AIServiceResponse(
                        intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                        message=final_text, is_final=True, iterations=1,
                    )
                    return

        # ── Guarda: write_file não escreve no sandbox por engano ──
        # O modelo confunde as duas máquinas com frequência (já custou um bloqueio
        # igual no run_command e no CLIENT_TOOL). Aqui o erro seria pior que falhar:
        # `/workspace/...` NA MÁQUINA DELE criaria uma pasta /workspace de verdade,
        # no disco errado, e o arquivo sumiria da vista dos dois lados.
        if tool_name == "write_file":
            path = str(args.get("path") or "").strip()
            if path.startswith("/workspace/") or path == "/workspace":
                final_text = (
                    "Esse caminho é do meu sandbox, não do seu disco. Se era pra gravar "
                    "aí dentro eu uso o neo_write_file; se era pra gravar na sua máquina, "
                    "me diga a pasta do seu lado."
                )
                logger.warning("[files] write_file com caminho de sandbox bloqueado: %r", path)
                if slog:
                    slog.log_event(
                        "write_file bloqueado (sandbox ≠ disco do usuário)",
                        f"`{path}` só existe no sandbox; não foi escrito na máquina do usuário.",
                    )
                await self.sessions.add_message(
                    session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
                )
                yield AIServiceResponse(
                    intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                    message=final_text, is_final=True, iterations=1,
                )
                return

        # ── Guarda: pull por caminho citado (nunca um caminho inventado) ──
        # Caminho que o modelo sabia e a guarda derrubou. Guardado para a
        # retomada: se o usuário fechar o seletor, a resposta precisa dizer QUAL
        # arquivo estava em jogo em vez de fingir que nunca houve um.
        caminho_rebaixado = ""
        if tool_name == "request_user_file":
            mode = str(args.get("mode") or "pick").strip().lower()
            if mode not in ("pick", "path"):
                mode = "pick"
            if mode == "path":
                path = str(args.get("path") or "").strip()
                if not await self._file_path_is_grounded(
                    path, session_id, user_message, user_id
                ):
                    # Não sobe um chute: cai pro picker (o usuário escolhe o arquivo certo).
                    logger.info("[files] request_user_file path %r não fundamentado — caindo pro picker", path)
                    if slog:
                        slog.log_event(
                            "request_user_file — caminho não fundamentado",
                            f"O caminho `{path}` não foi citado pelo usuário; abrindo o seletor em vez de chutar.",
                        )
                    mode = "pick"
                    caminho_rebaixado = path
                    args.pop("path", None)
            args["mode"] = mode

        # ── Estado de retomada (o gRPC só encaminha user_message/command/…) ──
        self._pending_actuations[sid] = {
            "tool_name": tool_name,
            "user_message": user_message,
            "user_id": str(user_id) if user_id else None,
            "metadata": metadata or {},
            "stage": bool(args.get("stage")),
            "destination": args.get("destination"),
            "depth": depth,
            "downgraded_path": caminho_rebaixado,
        }
        if trace is not None:
            self._pending_traces[sid] = trace

        if slog:
            slog.log_tool_request(tool_name, arguments=args, requires_approval=False, execution="client-side")

        reason = str(args.get("reason") or "").strip()
        if reason:
            thinking = reason
        elif tool_name == "request_user_file":
            thinking = "Vou precisar de um arquivo seu — abrindo o seletor…"
        else:
            thinking = "Salvando o arquivo no seu computador…"
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message=thinking, is_final=False,
        )
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            tool_request={
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": json.dumps(args, ensure_ascii=False),
                "requires_approval": False,
            },
            is_final=False,
            metadata={
                # flow=client_tool reusa a limpeza de turno órfão do gRPC (finally)
                # se o desktop cair antes de devolver o resultado.
                "flow": "client_tool",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "user_message": user_message,
                "command": tool_name,
            },
        )

    async def _reenter_cognitive(
        self,
        session_id: UUID,
        *,
        user_message: str,
        pend: dict[str, Any],
        extra_note: str | None,
        slog: "SessionFileLogger | None",
        trace: "MessageTrace | None",
    ) -> AsyncIterator[AIServiceResponse]:
        """Roda de novo o turno DIRECT_RESPONSE com o arquivo já disponível.

        Reconstrói decisão/histórico/contexto (agora com o manifesto do anexo
        recém-chegado + ``extra_note``) e delega ao ``_handle_direct_response`` —
        assim o cérebro lê/entrega o arquivo e responde o pedido original. A
        profundidade (``depth``) sobe pra cortar um eventual laço de atuação.
        """
        sid = session_id
        metadata = (pend or {}).get("metadata") or {}
        uid_raw = (pend or {}).get("user_id")
        user_id: UUID | None = None
        if uid_raw:
            try:
                user_id = UUID(str(uid_raw))
            except (ValueError, TypeError):
                user_id = None
        history = await self.sessions.get_history(sid, limit=20, max_tokens_budget=3000)
        base_ctx = self._build_system_context(metadata)
        manifest = await self._build_attachments_manifest(None, session_id=sid)
        sys_context = "\n\n".join(p for p in (base_ctx, manifest, extra_note) if p)
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=1.0,
            needs_memory=True,
            needs_plan=False,
            explanation="retomada após ação de arquivo no cliente",
        )
        depth = int((pend or {}).get("depth", 0)) + 1
        # enable_handoff=False: a retomada existe pra LER/ENTREGAR o arquivo e
        # responder o pedido original — não pra encadear pra outra skill (music/HA).
        # Sem orquestrador consumindo aqui, um [[HANDOFF]] só seria descartado.
        async for resp in self._handle_direct_response(
            sid, user_message, history, decision, trace, sys_context,
            slog=slog, user_id=user_id, metadata=metadata,
            enable_handoff=False,
            _actuation_depth=depth,
        ):
            yield resp

    async def _resume_after_user_file(
        self,
        session_id: UUID,
        *,
        success: bool,
        result: str | None,
        error: str | None,
        user_message: str,
        pend: dict[str, Any],
        slog: "SessionFileLogger | None",
        trace: "MessageTrace | None",
    ) -> AsyncIterator[AIServiceResponse]:
        """Resultado do ``request_user_file``: cataloga o arquivo puxado e responde."""
        sid = str(session_id)
        if not success:
            low = (error or "").lower()
            cancelado = "cancel" in low or "não escolheu" in low or "nao escolheu" in low
            rebaixado = str(pend.get("downgraded_path") or "").strip()
            if cancelado and rebaixado:
                # O seletor só abriu porque a guarda de fundamentação derrubou um
                # caminho que o cérebro TINHA. Responder "não recebi nenhum
                # arquivo" aqui contradiz o que ele mesmo disse no turno anterior
                # (sessão 97774390) — some com o arquivo que ele acabou de achar.
                nome = rebaixado.rsplit("/", 1)[-1]
                final_text = (
                    f"Você fechou o seletor. Eu queria pegar o {nome} "
                    f"(`{rebaixado}`) — se for esse mesmo, me confirme o caminho que eu "
                    "trago; se for outro arquivo, é só dizer qual."
                )
            elif cancelado:
                final_text = "Sem problema — não recebi nenhum arquivo. Quando quiser, é só me mandar."
            else:
                final_text = f"Não consegui trazer o arquivo: {error or 'o envio falhou'}."
            await self.sessions.add_message(
                session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
            )
            slog.log_final_response(final_text, "DIRECT_RESPONSE", iterations=1) if slog else None
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                message=final_text, is_final=True, iterations=1,
            )
            return

        file_id = None
        name = None
        try:
            payload = json.loads(result) if result else {}
            if isinstance(payload, dict):
                file_id = payload.get("file_id") or payload.get("id")
                name = payload.get("name")
        except (ValueError, TypeError):
            payload = {}
        if not file_id:
            final_text = "O arquivo chegou, mas não consegui catalogá-lo direito. Pode tentar de novo?"
            await self.sessions.add_message(
                session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
            )
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                message=final_text, is_final=True, iterations=1,
            )
            return

        # Anexo sticky: o arquivo puxado vale pro resto da conversa (manifesto).
        self._remember_session_attachments(sid, [str(file_id)])
        logger.info("[files] request_user_file trouxe %s (%s) — sticky + resume", file_id, name)

        # Auto-stage quando o cérebro sinalizou que vai MANIPULAR o arquivo.
        stage_note = ""
        if (pend or {}).get("stage") and self.file_store is not None:
            try:
                staged = await self.file_store.stage(str(file_id), target="workbench")
                path = staged.get("path") if isinstance(staged, dict) else staged
                if path:
                    stage_note = f" Ele já está materializado no seu /workspace em `{path}` (use neo_bash)."
            except Exception as exc:  # noqa: BLE001 — stage é conveniência, não pode derrubar o turno
                logger.warning("[files] auto-stage de %s falhou: %s", file_id, exc)

        extra_note = (
            "## ARQUIVO RECÉM-RECEBIDO\n"
            f"O arquivo que você pediu ({name or 'sem nome'}) chegou e está no seu catálogo "
            f"(id={file_id}).{stage_note} Agora responda o pedido original lendo/processando "
            "este arquivo com as tools file_*/neo_bash — não peça o arquivo de novo."
        )
        async for resp in self._reenter_cognitive(
            session_id, user_message=user_message, pend=pend,
            extra_note=extra_note, slog=slog, trace=trace,
        ):
            yield resp

    async def _resume_after_command(
        self,
        session_id: UUID,
        *,
        success: bool,
        result: str | None,
        error: str | None,
        user_message: str,
        pend: dict[str, Any],
        slog: "SessionFileLogger | None",
        trace: "MessageTrace | None",
    ) -> AsyncIterator[AIServiceResponse]:
        """Saída do comando que o CÉREBRO pediu: re-entra no turno cognitivo com ela.

        Diferente do caminho CLIENT_TOOL do classificador (que termina em
        ``_format_tool_result``, um tiro só, sem histórico e sem tools), aqui a saída
        volta pro núcleo cognitivo: ele tem o histórico, a persona, o PKS e as tools —
        inclusive ``run_command`` de novo. Ou seja, um comando que devolveu a coisa
        errada pode ser corrigido no MESMO turno, sem o usuário ter que pedir de novo.
        """
        command = (pend or {}).get("command") or "o comando"
        if success:
            output = self._truncate_output(result or "", max_lines=200)
            extra_note = (
                "## SAÍDA DO COMANDO QUE VOCÊ RODOU\n"
                f"Você executou na máquina do usuário: `{command}`\n"
                f"Saída real:\n```\n{output or '(vazia)'}\n```\n"
                "Responda AGORA o pedido original com base nesta saída — direto, sem "
                "recontar o que você fez. Só os fatos que estão aí: não invente linha "
                "que não apareceu, e se a saída não responde o que foi pedido (veio "
                "vazia, veio agregada demais, faltou um campo), rode outro `run_command` "
                "em vez de entregar meia resposta ou de pedir pro usuário rodar."
            )
        else:
            extra_note = (
                "## O COMANDO QUE VOCÊ RODOU FALHOU\n"
                f"Comando: `{command}`\nErro: {error or 'desconhecido'}\n"
                "Diagnostique e, se der pra corrigir (binário/flag/caminho errado), rode "
                "outro `run_command` já corrigido. Se não der, diga em uma frase o que "
                "falhou — sem pedir pro usuário rodar nada por você."
            )
        if slog:
            slog.log_event(
                "Retomada cognitiva (shell do cérebro)",
                f"`{command}` → {'saída devolvida ao cérebro' if success else f'ERRO: {error}'}",
            )
        async for resp in self._reenter_cognitive(
            session_id, user_message=user_message, pend=pend,
            extra_note=extra_note, slog=slog, trace=trace,
        ):
            yield resp

    async def _confirm_file_delivery(
        self,
        session_id: UUID,
        *,
        success: bool,
        result: str | None,
        error: str | None,
        user_message: str,
        pend: dict[str, Any],
        slog: "SessionFileLogger | None",
        trace: "MessageTrace | None",
    ) -> AsyncIterator[AIServiceResponse]:
        """Resultado de ``save_generated_file`` ou ``write_file``: confirma ou trata a colisão.

        ``status='saved'`` → confirma em uma frase. ``status='conflict'`` → re-entra
        no cognitivo com os dados do arquivo existente pro cérebro decidir (só
        quando for obviamente seguro) ou perguntar antes de reenviar com overwrite.

        As duas tools compartilham este caminho porque devolvem o MESMO envelope
        (saved/conflict + path). O que muda é o nome que a nota de conflito manda
        rechamar: mandar o cérebro reenviar por `save_generated_file` um arquivo
        que ele escreveu com `write_file` o faria procurar um file_id que não existe.
        """
        which = str((pend or {}).get("tool_name") or "save_generated_file")
        status = None
        path = None
        existing: dict[str, Any] | None = None
        try:
            payload = json.loads(result) if result else {}
            if isinstance(payload, dict):
                status = payload.get("status")
                path = payload.get("path")
                existing = payload.get("existing") if isinstance(payload.get("existing"), dict) else None
        except (ValueError, TypeError):
            payload = {}

        if not success:
            final_text = f"Não consegui salvar o arquivo aí: {error or 'erro desconhecido'}."
            await self.sessions.add_message(
                session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
            )
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                message=final_text, is_final=True, iterations=1,
            )
            return

        if status == "conflict":
            when = existing.get("modified_at") or existing.get("created_at") if existing else None
            size = existing.get("size_bytes") if existing else None
            human_size = f"{size/1024:.0f} KB" if isinstance(size, (int, float)) and size else "?"
            desc = existing.get("name") if existing else (path or "o arquivo")
            extra_note = (
                "## CONFLITO AO SALVAR NO DISCO DO USUÁRIO\n"
                f"Já existe um arquivo em `{path}` (nome: {desc}, tamanho: {human_size}"
                + (f", modificado em {when}" if when else "")
                + "). NÃO foi sobrescrito. Decida: se for claramente seguro (ex.: é o "
                "arquivo que VOCÊ acabou de gerar nesta conversa), reenvie chamando "
                f"{which} com overwrite=true. Se houver qualquer dúvida, "
                "PERGUNTE ao usuário citando esses dados antes de sobrescrever, ou "
                "ofereça salvar com outro nome."
            )
            async for resp in self._reenter_cognitive(
                session_id, user_message=user_message, pend=pend,
                extra_note=extra_note, slog=slog, trace=trace,
            ):
                yield resp
            return

        # status == "saved" (ou desconhecido, mas sucesso)
        where = f" em {path}" if path else ""
        written = payload.get("bytes_written") if isinstance(payload, dict) else None
        if which == "write_file":
            size = f" ({written} bytes)" if isinstance(written, int) and written else ""
            final_text = f"Pronto, escrevi o arquivo{where}{size}."
        else:
            final_text = f"Pronto, salvei o arquivo no seu computador{where}."
        await self.sessions.add_message(
            session_id=session_id, role="assistant", content=final_text, generate_embedding=False,
        )
        slog.log_final_response(final_text, "DIRECT_RESPONSE", iterations=1) if slog else None
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message=final_text, is_final=True, iterations=1,
        )

    # ---------------------------------------------------------------
    # Continue flow after tool result from desktop
    # ---------------------------------------------------------------

    async def process_tool_result(
        self,
        session_id: UUID,
        tool_name: str,
        tool_call_id: str,
        success: bool,
        result: str | None = None,
        error: str | None = None,
        user_message: str = "",
        command: str = "",
        tool_arguments: dict | None = None,
        _retry_count: int = 0,
        _is_diagnostic: bool = False,
        _followup_count: int = 0,
        _has_plan: bool = False,
    ) -> AsyncIterator[AIServiceResponse]:
        """Process tool execution result from desktop and format response."""
        MAX_TOOL_RETRIES = 4
        MAX_FOLLOWUP_STEPS = 3

        # Get session file logger
        slog = get_session_logger(session_id)

        # Retrieve the trace kept alive from _handle_client_tool, or create a new one
        obs = get_obs()
        trace = self._pending_traces.pop(str(session_id), None)
        if trace is None:
            trace = obs.start_trace(
                session_id=str(session_id),
                user_message=user_message or f"Tool result: {tool_name}",
                service="neo-desktop",
            )

        t0_tool = time.monotonic()
        slog.log_tool_result(tool_name, success=success, result=result, error=error, duration_ms=0, execution="client-side")

        # ── MUSIC ACTUATION RESULT (neo-music-feature) ──
        # The tool was a desktop/browser play command dispatched by _handle_music.
        # Confirm truthfully on the real outcome and stop — none of the generic
        # retry/diagnostic/plan recovery below applies to "open a URL / play a track".
        if tool_name in _MUSIC_ACTUATION_TOOLS:
            summary = command or "a música"
            if success:
                final_text = f"🎵 {summary}"
            else:
                final_text = f"Tentei tocar {summary}, mas não consegui: {error or 'o player não respondeu'}."
            await self.sessions.add_message(
                session_id=session_id, role="assistant", content=final_text,
                generate_embedding=False,
            )
            slog.log_final_response(final_text, "MUSIC", iterations=1)
            yield AIServiceResponse(
                intent=IntentType.MUSIC, session_id=session_id,
                message=final_text, is_final=True, iterations=1,
            )
            return

        # ── CLIENT-ACTUATION DE ARQUIVO (plano "arquivos sob iniciativa do cérebro") ──
        # request_user_file (pull) / save_generated_file (push). Tratados aqui no topo,
        # fora de toda a máquina genérica de retry/plano/diagnóstico abaixo. Cobre TANTO
        # o disparo pelo cérebro (via _emit_client_actuation) QUANTO o pelo classificador
        # (save_generated_file por "salva aí") — o estado de retomada, quando existe,
        # está em _pending_actuations.
        # request_user_file só ocorre com a flag on (pull do cérebro) → sempre tratado
        # aqui. save_generated_file: intercepta quando é o formato NOVO (JSON com
        # "status": saved/conflict) OU a flag de atuação está on; senão cai no fluxo
        # genérico antigo (rollout conservador: desktop velho devolve string simples).
        # ── SHELL PEDIDO PELO CÉREBRO (neo_brain_shell_enabled) ──
        # Chega como `execute_command` (passou pela aprovação) ou `run_command`. O que
        # distingue do CLIENT_TOOL do classificador não é o nome e sim o estado deixado
        # por _emit_client_actuation — sem ele, cai na máquina genérica de sempre.
        _pend_shell = self._pending_actuations.get(str(session_id))
        if (
            _pend_shell
            and _pend_shell.get("kind") == "shell"
            and tool_name in ("execute_command", "run_command")
        ):
            pend = self._pending_actuations.pop(str(session_id))
            async for resp in self._resume_after_command(
                session_id, success=success, result=result, error=error,
                user_message=user_message or pend.get("user_message", ""),
                pend=pend, slog=slog, trace=trace,
            ):
                yield resp
            return

        _new_delivery_fmt = isinstance(result, str) and '"status"' in result
        if tool_name in _CLIENT_ACTUATION_TOOLS and (
            tool_name == "request_user_file"
            or _new_delivery_fmt
            or getattr(self.settings, "neo_file_actuation_enabled", False)
        ):
            pend = self._pending_actuations.pop(str(session_id), {})
            resume_msg = user_message or pend.get("user_message", "")
            if tool_name == "request_user_file":
                async for resp in self._resume_after_user_file(
                    session_id, success=success, result=result, error=error,
                    user_message=resume_msg, pend=pend, slog=slog, trace=trace,
                ):
                    yield resp
            else:  # save_generated_file
                async for resp in self._confirm_file_delivery(
                    session_id, success=success, result=result, error=error,
                    user_message=resume_msg, pend=pend, slog=slog, trace=trace,
                ):
                    yield resp
            return

        # ── DIAGNOSTIC RESULT: this was an auto-diagnostic command ──
        if _is_diagnostic:
            tool_ms = round((time.monotonic() - t0_tool) * 1000, 2)
            diagnostic_output = result if success else f"Diagnóstico falhou: {error}"

            trace.add_tool_call(
                tool_name="diagnostic",
                arguments=tool_arguments or {"command": command},
                result=self._truncate_output(diagnostic_output or "", max_lines=30),
                duration_ms=tool_ms,
            )

            logger.info(f"Diagnostic result received. Feeding to LLM for informed recovery.")

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message="Diagnóstico concluído. Analisando resultado...",
                is_final=False,
            )

            # Now call LLM with the diagnostic data
            recovery = await self._recover_from_tool_error(
                user_message=user_message,
                tool_name=tool_name,
                command=command,
                error=error or "comando não encontrado",
                tool_arguments=tool_arguments,
                retry_count=_retry_count,
                trace=trace,
                diagnostic_output=diagnostic_output,
            )
            slog.log_error_recovery(
                attempt=_retry_count + 1,
                error_type="diagnostic_recovery",
                action=recovery.get("action", "?") if recovery else "none",
                details=recovery,
            )

            if recovery and recovery.get("action") == "retry":
                new_tool = recovery.get("tool_name", "run_command")
                new_args = recovery.get("tool_arguments", {})
                new_command = recovery.get("raw_command")
                new_explanation = recovery.get("explanation", "Tentando abordagem alternativa...")
                new_tool_call_id = str(uuid4())

                yield AIServiceResponse(
                    intent=IntentType.CLIENT_TOOL,
                    session_id=session_id,
                    message=new_explanation,
                    is_final=False,
                )

                if new_command:
                    new_command_id = str(uuid4())
                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        command_request={
                            "command_id": new_command_id,
                            "command": new_command,
                            "explanation": new_explanation,
                            "is_background": False,
                        },
                        is_final=False,
                        metadata={
                            "flow": "client_command",
                            "command_id": new_command_id,
                            "command": command,  # preserve original command, not the recovery cmd
                            "tool_arguments": tool_arguments,  # preserve original tool_arguments
                            "user_message": user_message,
                            "_retry_count": _retry_count + 1,
                            "_has_plan": _has_plan,
                        },
                    )
                else:
                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        tool_request={
                            "tool_call_id": new_tool_call_id,
                            "tool_name": new_tool,
                            "arguments": json.dumps(new_args, ensure_ascii=False),
                            "requires_approval": recovery.get("requires_approval", False),
                        },
                        is_final=False,
                        metadata={
                            "flow": "client_tool",
                            "tool_call_id": new_tool_call_id,
                            "tool_name": new_tool,
                            "user_message": user_message,
                            "tool_arguments": tool_arguments,  # preserve original, NOT new_args
                            "command": command,  # preserve original command through retries
                            "_retry_count": _retry_count + 1,
                            "_has_plan": _has_plan,
                        },
                    )

                self._pending_traces[str(session_id)] = trace
                return

            # LLM responded with text or failed to parse
            final_text = (
                recovery.get("message", "Não encontrei o programa no sistema.")
                if recovery and recovery.get("action") == "respond"
                else "Não consegui encontrar uma alternativa para executar o comando."
            )

            session = await self.sessions.get_or_create_session(
                session_id=session_id, service="neo-desktop"
            )
            actual_sid = session.id
            await self.sessions.add_message(
                session_id=actual_sid, role="assistant", content=final_text,
                tool_name=tool_name,
                user_id=session.user_id,
            )
            obs.finish_trace(trace, response=final_text)

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=actual_sid,
                message=final_text,
                is_final=True,
                iterations=2 + _retry_count,
            )
            return

        # ── NORMAL SUCCESS PATH ──
        if success:
            raw_output = result or "(sem saída)"

            # Try to parse JSON-stringified output
            try:
                parsed = json.loads(raw_output)
                if isinstance(parsed, str):
                    raw_output = parsed
                else:
                    raw_output = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                pass

            tool_ms = round((time.monotonic() - t0_tool) * 1000, 2)

            # Log the tool call (request + result) to the trace
            trace.add_tool_call(
                tool_name=tool_name,
                arguments=tool_arguments or {"command": command},
                result=self._truncate_output(raw_output, max_lines=40),
                duration_ms=tool_ms,
            )

            # Truncate very long output for LLM formatting
            raw_output = self._truncate_output(raw_output)

            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=session_id,
                message="Comando executado com sucesso. Formatando resultado...",
                is_final=False,
            )

            # ── PLAN-BASED EXECUTION: advance plan and send next step ──
            sid_str = str(session_id)
            active_plan = self.planner.get_active_plan(sid_str) if _has_plan else None

            if active_plan:
                # Advance the plan with the result
                next_step = active_plan.advance(step_result=raw_output)
                completed_step_idx = active_plan.current_step_index - 1 if active_plan.current_step_index > 0 else 0

                if slog:
                    current_done = active_plan.steps[completed_step_idx] if completed_step_idx < len(active_plan.steps) else None
                    slog.log_event(
                        "Plano - Passo Concluído",
                        f"**Passo:** {(current_done.index + 1) if current_done else '?'}/{len(active_plan.steps)}\n"
                        f"**Status:** {'Próximo passo' if next_step else 'Plano concluído'}\n",
                    )

                # Check if completed sub-plan needs to return to parent
                if active_plan.is_complete and active_plan.parent_plan_id:
                    parent_plan = self.planner.pop_completed_subplan(sid_str)
                    if parent_plan:
                        # Sub-plan done → get next step from parent
                        subplan_summary = active_plan.get_completed_context()
                        next_step = parent_plan.advance(step_result=f"Sub-plano concluído:\n{subplan_summary}")
                        active_plan = parent_plan

                        if slog:
                            slog.log_event("Sub-plano Concluído", f"Retornando ao plano principal. Próximo passo: {next_step.description if next_step else 'nenhum'}")

                if next_step:
                    # --- Heuristic: try to skip LLM call ---
                    skip = self.planner.should_skip_llm(
                        plan=active_plan,
                        step_index=completed_step_idx,
                        raw_output=raw_output,
                        next_step=next_step,
                    )

                    if skip:
                        # No LLM needed — continue with existing step or finish
                        if skip["decision"] == "finish":
                            final_text = await self.planner.build_final_response(active_plan, user_message)
                            self.planner.clear_plans(sid_str)
                            if slog:
                                slog.log_event("Plano Concluído (skip)", f"**Status:** {active_plan.status}")
                        elif self._client_tool_name_is_unknown(next_step.tool_name) or _dispatch_would_hang(
                            next_step.tool_name, next_step.tool_arguments
                        ):
                            # Marca o passo como falho e deixa o fallthrough abaixo
                            # (linha ~"Plan complete") montar a resposta final via
                            # build_final_response — não despacha um tool_name que
                            # o desktop não sabe executar (trava esperando resultado),
                            # nem um `run_command` sem comando (mesmo efeito).
                            logger.warning(
                                "[PLAN] próximo passo indespachável (tool_name=%r, args=%r) — abortando plano",
                                next_step.tool_name, next_step.tool_arguments,
                            )
                            next_step.status = "failed"
                            next_step.error = "passo indespachável (ferramenta fora do catálogo, ou comando vazio/ainda em placeholder porque o refinamento não devolveu o comando real)"
                            active_plan.step_results[next_step.id] = f"ERRO: {next_step.error}"
                            active_plan.status = "failed"
                        else:
                            # Send next step directly
                            next_step.status = "running"
                            for resp in self._plan_step_dispatch(
                                active_plan, next_step,
                                session_id=session_id, user_message=user_message,
                            ):
                                yield resp

                            self._pending_traces[str(session_id)] = trace
                            return  # Flow continues when desktop sends next tool_result
                    else:
                        # --- Call unified advance_and_refine (single LLM call) ---
                        sys_context = self._build_system_context(tool_arguments)
                        ar_result = await self.planner.advance_and_refine(
                            plan=active_plan,
                            step_index=completed_step_idx,
                            raw_output=raw_output,
                            system_context=sys_context,
                        )

                        decision = ar_result["decision"]

                        if decision == "finish":
                            final_text = await self.planner.build_final_response(active_plan, user_message)
                            self.planner.clear_plans(sid_str)
                            if slog:
                                slog.log_event("Plano Concluído (LLM)", f"**Razão:** {ar_result.get('reason', '')}")

                        elif decision == "subplan":
                            subplan_steps = ar_result.get("subplan_steps") or []
                            remaining = [s for s in active_plan.steps if s.status == "pending"]
                            if subplan_steps and self.planner.subplan_is_valid(subplan_steps, remaining):
                                # Create the subplan via generate_plan with the steps from LLM
                                sub_goal = ar_result.get("reason", active_plan.goal)
                                subplan = await self.planner.create_subplan(
                                    parent_plan=active_plan,
                                    parent_step=active_plan.steps[completed_step_idx],
                                    step_result=raw_output,
                                    system_context=sys_context,
                                    session_id=sid_str,
                                )
                                if subplan and subplan.steps:
                                    self.planner.store_plan(sid_str, subplan)
                                    active_plan = subplan
                                    next_step = subplan.get_current_step()
                                    # O cartão do sub-plano é emitido pelo
                                    # _plan_step_dispatch logo abaixo (o cliente
                                    # aninha pelo parent_plan_id). Mandar
                                    # `to_summary()` aqui só repetiria o plano em
                                    # markdown cru numa bolha de "Pensando".
                            # If subplan rejected, fall through to continue with next_step

                        elif decision == "retry":
                            # Use refined_step from LLM response
                            refined = ar_result.get("refined_step")
                            if refined:
                                next_step.tool_name = refined.get("tool_name", next_step.tool_name)
                                next_step.tool_arguments = refined.get("tool_arguments", next_step.tool_arguments)
                                next_step.raw_command = next_step.tool_arguments.get("command", next_step.raw_command)
                                next_step.requires_approval = refined.get("requires_approval", next_step.requires_approval)

                        else:  # "continue"
                            # Apply refined_step if provided (for dependent steps)
                            refined = ar_result.get("refined_step")
                            if refined:
                                next_step.tool_name = refined.get("tool_name", next_step.tool_name)
                                next_step.tool_arguments = refined.get("tool_arguments", next_step.tool_arguments)
                                next_step.raw_command = next_step.tool_arguments.get("command", next_step.raw_command)
                                next_step.requires_approval = refined.get("requires_approval", next_step.requires_approval)

                        # If decision was continue/retry/subplan and we have a next step, send it
                        # (refine_step/advance_and_refine podem ter trocado o tool_name — valida
                        # DEPOIS do refinamento, não antes, senão passa um nome já sobrescrito).
                        if decision in ("continue", "retry", "subplan") and next_step and (
                            self._client_tool_name_is_unknown(next_step.tool_name)
                            or _dispatch_would_hang(next_step.tool_name, next_step.tool_arguments)
                        ):
                            logger.warning(
                                "[PLAN] passo refinado indespachável (tool_name=%r, args=%r) — abortando plano",
                                next_step.tool_name, next_step.tool_arguments,
                            )
                            next_step.status = "failed"
                            next_step.error = "passo indespachável (ferramenta fora do catálogo, ou comando vazio/ainda em placeholder porque o refinamento não devolveu o comando real)"
                            active_plan.step_results[next_step.id] = f"ERRO: {next_step.error}"
                            active_plan.status = "failed"
                        elif decision in ("continue", "retry", "subplan") and next_step:
                            next_step.status = "running"
                            for resp in self._plan_step_dispatch(
                                active_plan, next_step,
                                session_id=session_id, user_message=user_message,
                            ):
                                yield resp

                            self._pending_traces[str(session_id)] = trace
                            return  # Flow continues when desktop sends next tool_result

                # Plan complete — format final unified response
                if not active_plan.is_complete:
                    active_plan.status = "completed"
                # Fecha a timeline no cliente ANTES da resposta final: sem esse
                # último plan_update o cartão fica com o passo corrente em
                # "running" (bolinha pulsando) pra sempre, mesmo com a conversa
                # já resolvida.
                yield self._plan_final_update(active_plan, session_id)
                final_text = await self.planner.build_final_response(active_plan, user_message)
                self.planner.clear_plans(sid_str)

                if slog:
                    slog.log_event("Plano Concluído", f"**Status:** {active_plan.status}\n**Resultado:**\n{final_text[:500]}")

            else:
                # Format with AI (trace is passed so the LLM call gets logged)
                final_text = await self._format_tool_result(
                    user_message, command or tool_name, raw_output, trace=trace,
                    live=str(session_id) in self._live_sessions,
                    register=self._session_registers.get(str(session_id)),
                    can_show_images=str(session_id) in self._image_capable_sessions,
                )

            # ── MULTI-STEP FOLLOW-UP: only when NOT using plan-based execution ──
            if not active_plan and _followup_count < MAX_FOLLOWUP_STEPS:
                followup = await self._check_followup_needed(
                    user_message=user_message,
                    command_ran=command or tool_name,
                    command_output=raw_output,
                    trace=trace,
                )
                if followup and followup.get("needed"):
                    followup_cmd = followup.get("command", "")
                    followup_explanation = followup.get("explanation", "Executando próximo passo...")
                    if followup_cmd and not self._is_blocking_command(followup_cmd):
                        logger.info(
                            f"[FOLLOWUP] Step {_followup_count + 1}: {followup_cmd}"
                        )
                        slog.log_event(
                            "Follow-up detectado",
                            f"- **Passo:** {_followup_count + 1}/{MAX_FOLLOWUP_STEPS}\n"
                            f"- **Comando:** `{followup_cmd}`\n"
                            f"- **Razão:** {followup_explanation}\n",
                        )

                        new_tool_call_id = str(uuid4())

                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            message=followup_explanation,
                            is_final=False,
                        )

                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            tool_request={
                                "tool_call_id": new_tool_call_id,
                                "tool_name": "run_command",
                                "arguments": json.dumps(
                                    {"command": followup_cmd, "working_dir": (tool_arguments or {}).get("working_dir", "")},
                                    ensure_ascii=False,
                                ),
                                "requires_approval": False,
                            },
                            is_final=False,
                            metadata={
                                "flow": "client_tool",
                                "tool_call_id": new_tool_call_id,
                                "tool_name": "run_command",
                                "user_message": user_message,
                                "tool_arguments": {"command": followup_cmd, "working_dir": (tool_arguments or {}).get("working_dir", "")},
                                "command": followup_cmd,
                                "_retry_count": 0,
                                "_followup_count": _followup_count + 1,
                            },
                        )

                        self._pending_traces[str(session_id)] = trace
                        return  # Flow continues when desktop sends next tool_result
        else:
            tool_ms = round((time.monotonic() - t0_tool) * 1000, 2)

            # Log the failed tool call
            trace.add_tool_call(
                tool_name=tool_name,
                arguments=tool_arguments or {"command": command},
                result=None,
                duration_ms=tool_ms,
                error=error or "desconhecido",
            )

            # ── PLAN-AWARE ERROR: mark step as failed, try to continue ──
            sid_str = str(session_id)
            active_plan = self.planner.get_active_plan(sid_str) if _has_plan else None

            if active_plan:
                # Mark the current step as failed and try next
                next_step = active_plan.advance(step_error=error or "desconhecido")

                if slog:
                    slog.log_event(
                        "Plano - Passo Falhou",
                        f"**Erro:** {error or 'desconhecido'}\n"
                        f"**Ação:** {'Tentando próximo passo' if next_step else 'Plano abortado'}\n",
                    )

                if next_step:
                    # For error recovery: refine the next step if dependent
                    if next_step.depends_on_previous:
                        sys_context = self._build_system_context(tool_arguments)
                        ar_result = await self.planner.advance_and_refine(
                            plan=active_plan,
                            step_index=active_plan.current_step_index - 1 if active_plan.current_step_index > 0 else 0,
                            raw_output=f"ERRO: {error or 'desconhecido'}",
                            system_context=sys_context,
                        )
                        refined = ar_result.get("refined_step")
                        if refined:
                            next_step.tool_name = refined.get("tool_name", next_step.tool_name)
                            next_step.tool_arguments = refined.get("tool_arguments", next_step.tool_arguments)
                            next_step.raw_command = next_step.tool_arguments.get("command", next_step.raw_command)

                    # Valida DEPOIS do refinamento acima (que pode ter trocado o tool_name).
                    if self._client_tool_name_is_unknown(next_step.tool_name):
                        logger.warning(
                            "[PLAN] passo de recovery com tool_name desconhecido %r — abortando plano",
                            next_step.tool_name,
                        )
                        next_step.status = "failed"
                        next_step.error = "ferramenta fora do catálogo (tool_name desconhecido)"
                        active_plan.step_results[next_step.id] = f"ERRO: {next_step.error}"
                        active_plan.status = "failed"
                    else:
                        next_step.status = "running"
                        step_tool_call_id = str(uuid4())

                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            message=f"⚠️ Passo anterior falhou. {active_plan.progress_text}: {next_step.description}",
                            is_final=False,
                        )

                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            tool_request={
                                "tool_call_id": step_tool_call_id,
                                "tool_name": next_step.tool_name,
                                "arguments": json.dumps(next_step.tool_arguments, ensure_ascii=False),
                                "requires_approval": next_step.requires_approval,
                            },
                            is_final=False,
                            metadata={
                                "flow": "client_tool",
                                "tool_call_id": step_tool_call_id,
                                "tool_name": next_step.tool_name,
                                "user_message": user_message,
                                "tool_arguments": next_step.tool_arguments,
                                "command": next_step.raw_command or next_step.tool_arguments.get("command", next_step.tool_name),
                                "_has_plan": True,
                            },
                        )

                        self._pending_traces[str(session_id)] = trace
                        return

                # All steps done or failed — format whatever we have
                final_text = await self.planner.build_final_response(active_plan, user_message)
                self.planner.clear_plans(sid_str)

            # ── ERROR RECOVERY: auto-diagnose then ask LLM ──────────
            elif _retry_count < MAX_TOOL_RETRIES:
                logger.info(
                    f"Tool '{tool_name}' failed (attempt {_retry_count + 1}/{MAX_TOOL_RETRIES}). "
                    f"Starting recovery: {error}"
                )

                yield AIServiceResponse(
                    intent=IntentType.CLIENT_TOOL,
                    session_id=session_id,
                    message=f"Comando falhou. Investigando alternativas... (tentativa {_retry_count + 1}/{MAX_TOOL_RETRIES})",
                    is_final=False,
                )

                # _recover_from_tool_error will auto-diagnose on first attempt
                # (returns a diagnostic command to run), then on second attempt
                # it receives the diagnostic output and makes an informed decision
                recovery = await self._recover_from_tool_error(
                    user_message=user_message,
                    tool_name=tool_name,
                    command=command,
                    error=error or "desconhecido",
                    tool_arguments=tool_arguments,
                    retry_count=_retry_count,
                    trace=trace,
                )
                slog.log_error_recovery(
                    attempt=_retry_count + 1,
                    error_type=self._detect_error_type(error or "", command=command, tool_arguments=tool_arguments),
                    action=recovery.get("action", "?") if recovery else "none",
                    details=recovery,
                )

                # Guard against the approval-gated retry LOOP: a fresh command_request
                # that needs the user to approve is offered at most once. If a real
                # attempt already failed (≥1 retry), stop inventing variations and
                # report honestly instead of chaining approval prompts. (Diagnostics —
                # read-only, no approval — are exempt.) See neo-music-feature.
                if (
                    recovery
                    and recovery.get("action") == "retry"
                    and recovery.get("raw_command")
                    and not recovery.get("_is_diagnostic", False)
                    and _retry_count >= 1
                ):
                    logger.info("[RECOVERY] suppressing chained approval-gated retry; reporting honestly")
                    recovery = {
                        "action": "respond",
                        "message": (
                            f"Não consegui fazer isso — o comando falhou: "
                            f"{error or 'erro desconhecido'}."
                        ),
                    }

                if recovery and recovery.get("action") == "retry":
                    new_tool = recovery.get("tool_name", "run_command")
                    new_args = recovery.get("tool_arguments", {})
                    new_command = recovery.get("raw_command")
                    new_explanation = recovery.get("explanation", "Tentando abordagem alternativa...")
                    is_diag = recovery.get("_is_diagnostic", False)
                    new_tool_call_id = str(uuid4())

                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        message=new_explanation,
                        is_final=False,
                    )

                    if new_command:
                        new_command_id = str(uuid4())
                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            command_request={
                                "command_id": new_command_id,
                                "command": new_command,
                                "explanation": new_explanation,
                                "is_background": False,
                            },
                            is_final=False,
                            metadata={
                                "flow": "client_command",
                                "command_id": new_command_id,
                                "command": command,  # preserve original command, not the recovery cmd
                                "tool_arguments": tool_arguments if is_diag else new_args,  # preserve original if diagnostic
                                "user_message": user_message,
                                "_retry_count": _retry_count + 1,
                                "_is_diagnostic": is_diag,
                                "_has_plan": _has_plan,
                            },
                        )
                    else:
                        yield AIServiceResponse(
                            intent=IntentType.CLIENT_TOOL,
                            session_id=session_id,
                            tool_request={
                                "tool_call_id": new_tool_call_id,
                                "tool_name": new_tool,
                                "arguments": json.dumps(new_args, ensure_ascii=False),
                                "requires_approval": recovery.get("requires_approval", False),
                            },
                            is_final=False,
                            metadata={
                                "flow": "client_tool",
                                "tool_call_id": new_tool_call_id,
                                "tool_name": new_tool,
                                "user_message": user_message,
                                "tool_arguments": tool_arguments if is_diag else new_args,  # preserve original if diagnostic
                                "command": command,  # preserve original command through retries
                                "_retry_count": _retry_count + 1,
                                "_is_diagnostic": is_diag,
                                "_has_plan": _has_plan,
                            },
                        )

                    # Keep trace alive for the next tool_result
                    self._pending_traces[str(session_id)] = trace
                    return  # Flow continues when desktop sends next tool_result

                elif recovery and recovery.get("action") == "respond":
                    final_text = recovery.get("message", f"Não consegui executar `{command or tool_name}`. Erro: {error or 'desconhecido'}")
                else:
                    final_text = f"Não consegui executar `{command or tool_name}`. Erro: {error or 'desconhecido'}"
            else:
                final_text = (
                    f"Não consegui executar `{command or tool_name}` após {MAX_TOOL_RETRIES} tentativas. "
                    f"Último erro: {error or 'desconhecido'}"
                )

        # Ensure the session exists (the client may send a stale/rotated session_id)
        session = await self.sessions.get_or_create_session(
            session_id=session_id, service="neo-desktop"
        )
        actual_sid = session.id

        # Save to session (no tool_call_id so it appears in conversation history)
        await self.sessions.add_message(
            session_id=actual_sid,
            role="assistant",
            content=final_text,
            tool_name=tool_name,
            user_id=session.user_id,
        )
        slog.log_message_saved("assistant", final_text, str(actual_sid))
        slog.log_final_response(final_text, "CLIENT_TOOL", iterations=2 + _retry_count)

        # Finalize the trace with the complete flow
        obs.finish_trace(trace, response=final_text)

        yield AIServiceResponse(
            intent=IntentType.CLIENT_TOOL,
            session_id=actual_sid,
            message=final_text,
            is_final=True,
            iterations=2 + _retry_count,
        )

    async def process_command_approval(
        self,
        session_id: UUID,
        command_id: str,
        approved: bool,
        command: str,
        user_message: str = "",
    ) -> AsyncIterator[AIServiceResponse]:
        """Process command approval/rejection from desktop."""

        slog = get_session_logger(session_id)
        slog.log_event(
            "Aprovação de Comando",
            f"- **Comando:** `{command}`\n- **Aprovado:** {'Sim' if approved else 'Não'}\n",
        )

        # Retrieve pending trace or create a new one
        obs = get_obs()
        trace = self._pending_traces.pop(str(session_id), None)

        # Ensure the session exists before saving any messages
        session = await self.sessions.get_or_create_session(
            session_id=session_id, service="neo-desktop"
        )
        actual_sid = session.id

        if not approved:
            msg = "Execução cancelada: permissão para comando local foi negada."
            # Comando pedido pelo CÉREBRO e negado: descarta o estado de retomada,
            # senão ele fica pendurado e o próximo `execute_command` da sessão (de
            # qualquer origem) seria confundido com a retomada deste turno.
            _pend = self._pending_actuations.get(str(actual_sid))
            if _pend and _pend.get("kind") == "shell":
                self._pending_actuations.pop(str(actual_sid), None)
                self._pending_traces.pop(str(actual_sid), None)

            # Log rejection to trace
            if trace:
                trace.add_tool_call(
                    tool_name="command_approval",
                    arguments={"command": command, "approved": False},
                    result=msg,
                    duration_ms=0,
                )
                obs.finish_trace(trace, response=msg)

            await self.sessions.add_message(
                session_id=actual_sid, role="assistant", content=msg,
                user_id=session.user_id,
            )
            yield AIServiceResponse(
                intent=IntentType.CLIENT_TOOL,
                session_id=actual_sid,
                message=msg,
                is_final=True,
            )
            return

        # Log approval to trace and keep it alive for the subsequent tool_result
        if trace:
            trace.add_tool_call(
                tool_name="command_approval",
                arguments={"command": command, "approved": True},
                result="Aprovado pelo usuário",
                duration_ms=0,
            )
            self._pending_traces[str(actual_sid)] = trace

        # Approved — send tool request to execute
        tool_call_id = str(uuid4())

        yield AIServiceResponse(
            intent=IntentType.CLIENT_TOOL,
            session_id=actual_sid,
            message="Permissão aprovada. Solicitando execução do comando ao Neo Desktop.",
            is_final=False,
        )

        yield AIServiceResponse(
            intent=IntentType.CLIENT_TOOL,
            session_id=actual_sid,
            tool_request={
                "tool_call_id": tool_call_id,
                "tool_name": "execute_command",
                "arguments": json.dumps(
                    {"command": command, "is_background": False},
                    ensure_ascii=False,
                ),
                "requires_approval": False,
            },
            is_final=False,
            metadata={
                "flow": "client_command_approved",
                "tool_call_id": tool_call_id,
                "command": command,
                "user_message": user_message,
                "tool_arguments": {"command": command, "is_background": False},
            },
        )

    # ---------------------------------------------------------------
    # Error recovery — ask LLM to reason about tool failures
    # ---------------------------------------------------------------

    @staticmethod
    def _detect_error_type(error: str, command: str = "", tool_arguments: dict | None = None) -> str:
        """Classify the error into a category for smarter recovery."""
        err_lower = (error or "").lower()
        # The ACTUAL command that was run (e.g. "flatpak run org.X.Y"), not the tool name
        actual_cmd = (
            (tool_arguments or {}).get("command")
            or (tool_arguments or {}).get("cmd")
            or command
            or ""
        )
        cmd_lower = actual_cmd.lower()

        # Exit code 255 from flatpak/snap usually means the GUI app launched but
        # detached from the terminal — treat as a special case, not a real failure.
        # Check the COMMAND (most reliable) and fall back to error text.
        if "código 255" in err_lower or "code 255" in err_lower:
            if any(k in cmd_lower for k in ("flatpak run", "snap run")):
                return "gui_launched"
            if any(k in err_lower for k in ("flatpak", "snap run", "xdg")):
                return "gui_launched"
        # "No such file or directory" é o erro padrão do Unix quando um ARGUMENTO
        # (caminho) do comando não existe — o binário em si roda normalmente. É
        # bem diferente de "command not found"/código 127 (o BINÁRIO que não
        # existe). Tratar os dois como a mesma coisa manda o playbook de
        # flatpak/snap/dpkg/rpm (pensado pra achar um APP que sumiu) procurar um
        # coreutil comum (ex: "cp") no sistema inteiro — nunca acha nada (o `cp`
        # sempre existe) e o comando de diagnóstico (find recursivo em vários
        # diretórios, grep em .desktop files etc) é caro o bastante pra travar o
        # turno até o timeout do client (sessão c7e2d5ed: `cp /workspace/...`
        # falhou porque o caminho só existe no sandbox remoto, não porque "cp"
        # sumiu — e ainda assim disparou a busca de app inteira).
        if "no such file" in err_lower:
            return "path_not_found"
        # Igual acima, "not found" sozinho é substring demais — um HTTP "404 Not
        # Found" (ex: save_generated_file com file_id inválido, sessão 74539cf0)
        # também casava aqui e mandava o MESMO playbook de flatpak/snap procurar
        # um "programa" chamado "save_generated_file" no sistema — nunca acha
        # nada (não é um binário, é um tool_name) e trava o turno do mesmo jeito.
        # Só a frase exata do shell ("command not found") ou os códigos de saída
        # convencionais contam como binário ausente.
        if any(k in err_lower for k in ("command not found", "código 127", "code 127")):
            return "command_not_found"
        if any(k in err_lower for k in ("permission denied", "permissão negada", "operation not permitted")):
            return "permission_denied"
        if any(k in err_lower for k in ("timeout", "timed out")):
            return "timeout"
        if any(k in err_lower for k in ("connection refused", "network", "no route")):
            return "network"
        return "unknown"

    @staticmethod
    def _find_app_from_diagnostic(program: str, diagnostic_output: str) -> str | None:
        """Parse diagnostic output deterministically to find a working launch command.

        This is the "roteiro" (playbook) for app-not-found recovery — no LLM needed.
        Tries in priority order: flatpak → snap → flatpak exports bin → find results.

        Returns the ready-to-run shell command, or None if the app is not found anywhere.
        """
        if not diagnostic_output:
            return None

        prog_lower = program.lower()

        # Split the diagnostic output into named sections (=== section ===)
        sections: dict[str, list[str]] = {}
        current = "preamble"
        for line in diagnostic_output.split("\n"):
            stripped = line.strip()
            if stripped.startswith("===") and stripped.endswith("==="):
                current = stripped.strip("= ").strip().lower()
                sections[current] = []
            else:
                sections.setdefault(current, []).append(stripped)

        def non_empty(lines: list[str]) -> list[str]:
            return [l for l in lines if l and "nenhum" not in l.lower() and "nao encontrado" not in l.lower() and "not found" not in l.lower()]

        # 1. Flatpak app list — "org.blender.Blender  Blender  ..."
        for line in non_empty(sections.get("flatpak", [])):
            if prog_lower in line.lower():
                parts = line.split()
                app_id = parts[0] if parts else ""
                if "." in app_id:  # looks like a real flatpak ID
                    logger.info(f"[Diagnostic] Found via flatpak: {app_id}")
                    return f"flatpak run {app_id}"

        # 2. Snap list — "blender  3.6  ..."
        for line in non_empty(sections.get("snap", [])):
            if prog_lower in line.lower():
                snap_name = line.split()[0] if line.split() else ""
                if snap_name:
                    logger.info(f"[Diagnostic] Found via snap: {snap_name}")
                    return f"snap run {snap_name}"

        # 3. Flatpak exports bin — direct binary symlinks
        for line in non_empty(sections.get("flatpak exports bin", [])):
            if prog_lower in line.lower():
                bin_name = line.strip()
                if bin_name and "/" not in bin_name:
                    path = f"/var/lib/flatpak/exports/bin/{bin_name}"
                    logger.info(f"[Diagnostic] Found in flatpak exports bin: {path}")
                    return path

        # 4. find results — full paths
        for line in non_empty(sections.get("find", [])):
            path = line.strip()
            if (prog_lower in path.lower()
                    and path.startswith("/")
                    and not path.endswith(".desktop")
                    and ("/bin/" in path or "/usr/" in path or "/opt/" in path)):
                logger.info(f"[Diagnostic] Found via find: {path}")
                return path

        return None

    @staticmethod
    def _build_diagnostic_command(program: str, error_type: str) -> str | None:
        """Build a diagnostic shell command that gathers real system data.

        Uses semicolons (;) so every section runs independently.
        Searches flatpak, snap, dpkg, and common bin paths.
        """
        if error_type == "command_not_found":
            return (
                f"echo '=== which ==='; "
                f"which {program} 2>&1; "
                f"type {program} 2>&1; "
                f"echo '=== flatpak ==='; "
                f"flatpak list --columns=application,name 2>/dev/null | grep -i {program} || echo 'nenhum flatpak encontrado'; "
                f"echo '=== flatpak exports bin ==='; "
                f"ls /var/lib/flatpak/exports/bin/ 2>/dev/null | grep -i {program} || echo 'nenhum binario flatpak encontrado'; "
                f"echo '=== snap ==='; "
                f"snap list 2>/dev/null | grep -i {program} || echo 'nenhum snap encontrado'; "
                f"echo '=== dpkg ==='; "
                f"dpkg -l '*{program}*' 2>/dev/null | grep -E '^ii' | head -5 || echo 'nenhum pacote dpkg encontrado'; "
                f"echo '=== rpm ==='; "
                f"rpm -qa 2>/dev/null | grep -i {program} | head -5 || echo 'rpm nao disponivel'; "
                f"echo '=== find ==='; "
                f"find /usr/bin /usr/local/bin /opt /var/lib/flatpak/exports/bin /snap/bin "
                f"/usr/share/applications /var/lib/snapd/desktop/applications "
                f"-iname '*{program}*' 2>/dev/null | head -15 || echo 'nenhum arquivo encontrado'; "
                f"echo '=== desktop files ==='; "
                f"grep -ril '{program}' /usr/share/applications/ /var/lib/flatpak/exports/share/applications/ "
                f"~/.local/share/applications/ 2>/dev/null | head -5 || echo 'nenhum .desktop encontrado'; "
                f"echo '=== PATH ==='; "
                f"echo $PATH"
            )
        if error_type == "permission_denied":
            # For permission denied, check if the path/directory actually exists
            # Extract the path from the command or tool_arguments
            target_path = ""
            if tool_arguments:
                target_path = tool_arguments.get("path", "") or tool_arguments.get("command", "")
            if not target_path:
                target_path = command or ""
            # Try to extract a file path from shell redirect (e.g. "> /path/to/file")
            import re as _re_diag
            redirect_match = _re_diag.search(r'>\s*(/[^\s;|&]+)', target_path)
            file_path = redirect_match.group(1) if redirect_match else target_path

            return (
                f"echo '=== path check ==='; "
                f"test -e '{file_path}' && echo 'path exists' || echo 'path does NOT exist'; "
                f"echo '=== parent dir ==='; "
                f"dirname '{file_path}' | xargs -I{{}} sh -c 'test -d \"{{}}\" && echo \"dir exists: {{}}\" || echo \"dir does NOT exist: {{}}\"'; "
                f"echo '=== file info ==='; "
                f"ls -la $(which {program} 2>/dev/null || echo /usr/bin/{program}) 2>&1; "
                f"echo '=== user ==='; "
                f"id"
            )
        return None

    @staticmethod
    def _extract_program_name(command: str, error: str, tool_arguments: dict | None = None) -> str:
        """Extract the actual program name from the failed command.

        Priority:
        1. tool_arguments['command'] (the real shell command, e.g. 'audacity')
        2. command string (first non-env-var word)
        3. error message parsing (handles multiple shell error formats)
        """
        import re as _re

        TOOL_NAMES = {"run_command", "execute_command", "list_directory", "read_file",
                      "write_file", "create_directory", "delete_path", "search_files",
                      "get_system_info"}
        SHELL_NAMES = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}
        ENV_PREFIXES = {"sudo", "env", "nohup", "nice", "sh", "bash"}

        # 1. tool_arguments has the real command (not the tool name like 'run_command')
        actual_cmd = (tool_arguments or {}).get("command") or (tool_arguments or {}).get("cmd", "")
        if actual_cmd:
            parts = actual_cmd.strip().split()
            for part in parts:
                if "=" not in part and part not in ENV_PREFIXES:
                    return part.split("/")[-1]  # basename

        # 2. Try to get from command string (skip tool names like 'run_command')
        if command and command not in TOOL_NAMES:
            parts = command.strip().split()
            for part in parts:
                if "=" not in part and part not in ENV_PREFIXES:
                    return part.split("/")[-1]

        # 3. Fallback: parse from error message
        # Handles multiple formats:
        #   "bash: blender: command not found"
        #   "zsh: command not found: blender"
        #   "Erro (código 127): bash: blender: command not found"
        err = error or ""
        # Strip the "Erro (código N): " prefix added by CommandExecutor
        err_inner = _re.sub(r'^Erro \(c[oó]digo \d+\):\s*', '', err, flags=_re.IGNORECASE).strip()

        for marker in ("command not found", "não encontrado", "nao encontrado", "not found"):
            marker_idx = err_inner.lower().find(marker)
            if marker_idx < 0:
                continue

            # Pattern A: "bash: blender: command not found" — app name is BEFORE the marker
            before = err_inner[:marker_idx].rstrip(': ').strip()
            # Split by ":" to get segments, try from right to left (most specific last)
            segments = [s.strip() for s in before.split(':') if s.strip()]
            for segment in reversed(segments):
                candidate = segment.split()[-1] if segment.split() else ""
                if (candidate
                        and candidate not in TOOL_NAMES
                        and candidate.lower() not in SHELL_NAMES
                        and _re.match(r'^[\w.-]+$', candidate)
                        and not candidate.endswith(')')):
                    return candidate

            # Pattern B: "zsh: command not found: blender" — app name is AFTER the marker
            after_part = err_inner[marker_idx + len(marker):].lstrip(': ').strip().split()
            if after_part and after_part[0] not in TOOL_NAMES and _re.match(r'^[\w.-]+$', after_part[0]):
                return after_part[0]

        return "unknown"

    async def _recover_from_tool_error(
        self,
        user_message: str,
        tool_name: str,
        command: str,
        error: str,
        tool_arguments: dict | None = None,
        retry_count: int = 0,
        trace: MessageTrace | None = None,
        diagnostic_output: str | None = None,
    ) -> dict | None:
        """Ask the LLM to analyze a tool error and decide on recovery action.

        When diagnostic_output is provided, the LLM has real system data
        to make informed decisions instead of guessing.

        Returns a dict with:
          - action: "retry" | "respond" | "diagnose"
          - tool_name, tool_arguments, raw_command, explanation (if retry)
          - message (if respond)
          - diagnostic_command (if diagnose)
        """
        error_type = self._detect_error_type(error, command=command, tool_arguments=tool_arguments)

        # ── SPECIAL CASE: missing PATH argument, not a missing binary ──
        # Nada aqui (diagnóstico flatpak/snap/dpkg, ou o LLM de recovery — cujo
        # prompt inteiro assume "achar onde o app está instalado") ajuda quando
        # o problema é um caminho que não existe (ex: referência a /workspace/
        # do sandbox, que o client nunca alcança). Reporta o erro direto — mais
        # rápido e mais honesto que arrastar isso pelo playbook de app ausente.
        if error_type == "path_not_found":
            return None

        # ── SPECIAL CASE: tool nomeada (não run_command) ──
        # Todo o resto desta função (busca flatpak/snap/dpkg/rpm, prompt de
        # recovery) foi pensado pra UMA coisa: achar onde um PROGRAMA sumido
        # está instalado, quando o pedido original era rodar um comando shell.
        # Pra uma tool nomeada tipo save_generated_file/list_directory/write_file
        # isso não faz sentido nenhum — não é um binário, é uma chamada
        # estruturada; procurar "save_generated_file" no flatpak nunca acha nada
        # (sessão 74539cf0: um 404 do download virou "auto-diagnosing
        # save_generated_file" e travou o turno até o timeout do client, do
        # mesmo jeito que o bug do path_not_found). Reporta o erro direto.
        if tool_name and tool_name != "run_command":
            return None

        # ── SPECIAL CASE: GUI app launched successfully (exit 255 from flatpak/snap) ──
        # Apps gráficos ficam em foreground e retornam 255 quando o executor os mata.
        # Verificar se o processo está rodando antes de tratar como erro real.
        if error_type == "gui_launched":
            program = self._extract_program_name(command, error, tool_arguments)
            logger.info(f"GUI app possibly launched (exit 255). Verifying process: {program}")
            return {
                "action": "retry",
                "tool_name": "run_command",
                "tool_arguments": {
                    "command": f"pgrep -il '{program}' | head -5 || echo 'processo nao encontrado'"
                },
                "explanation": f"Verificando se {program} abriu com sucesso...",
                "requires_approval": False,
                "_is_diagnostic": True,
            }

        # ── STEP 1: If first attempt and no diagnostic data yet, run diagnostics ──
        if retry_count == 0 and diagnostic_output is None and error_type in ("command_not_found", "permission_denied"):
            program = self._extract_program_name(command, error, tool_arguments)
            logger.info(f"[DEBUG] _recover_from_tool_error: command='{command}', tool_arguments={tool_arguments}, extracted_program='{program}'")
            diag_cmd = self._build_diagnostic_command(program, error_type)
            if diag_cmd:
                logger.info(f"Auto-diagnosing '{program}' before asking LLM (error_type={error_type})")
                return {
                    "action": "retry",
                    "tool_name": "run_command",
                    "tool_arguments": {"command": diag_cmd},
                    "explanation": f"Investigando onde '{program}' está instalado no sistema...",
                    "requires_approval": False,
                    "_is_diagnostic": True,
                }

        # ── STEP 1.5: Deterministic recovery from diagnostic output ──
        # Parse the diagnostic output ourselves before asking the LLM.
        # This eliminates LLM variability for the common "app found via flatpak/snap" case.
        if diagnostic_output and error_type == "command_not_found":
            program = self._extract_program_name(command, error, tool_arguments)
            launch_cmd = self._find_app_from_diagnostic(program, diagnostic_output)
            if launch_cmd:
                launcher = launch_cmd.split()[0]  # flatpak, snap, or absolute path
                return {
                    "action": "retry",
                    "tool_name": "run_command",
                    "tool_arguments": {"command": launch_cmd, "is_background": True},
                    "explanation": f"Encontrei {program} via {launcher}! Abrindo em segundo plano...",
                    "requires_approval": False,
                }
            logger.info(f"[Diagnostic] {program} not found in diagnostic output — escalating to LLM")

        # ── STEP 1.5b: Deterministic recovery for permission_denied ──
        # If the diagnostic shows the path/directory doesn't exist, switch to write_file tool
        if diagnostic_output and error_type == "permission_denied":
            if "dir does NOT exist" in diagnostic_output or "path does NOT exist" in diagnostic_output:
                # The real problem is the path doesn't exist, not permissions
                # Suggest using write_file tool which auto-creates parent directories
                target_path = ""
                if tool_arguments:
                    target_path = tool_arguments.get("path", "")
                if not target_path:
                    import re as _re_path
                    redirect_match = _re_path.search(r'>\s*(/[^\s;|&]+)', command or "")
                    if redirect_match:
                        target_path = redirect_match.group(1)

                if target_path:
                    # write_file nunca teve execução real (mesmo bug da sessão bd7a304d) —
                    # o equivalente real hoje é run_command com mkdir -p + touch.
                    logger.info(f"[Diagnostic] permission_denied but path doesn't exist: {target_path}. Criando via run_command (mkdir -p + touch).")
                    quoted = target_path.replace("'", "'\\''")
                    return {
                        "action": "retry",
                        "tool_name": "run_command",
                        "tool_arguments": {
                            "command": f"mkdir -p \"$(dirname '{quoted}')\" && : > '{quoted}'"
                        },
                        "explanation": f"O diretório não existe. Criando {target_path} (mkdir -p + touch)...",
                        "requires_approval": False,
                    }
                logger.info(f"[Diagnostic] permission_denied + path doesn't exist but couldn't extract target path")

        # ── STEP 2: Build an informed prompt with real data ──
        system_msg = (
            "Você é Neo, um assistente de desktop inteligente. "
            "Um comando executado no computador do usuário falhou. "
            "Analise o erro e os dados de diagnóstico do sistema para decidir a melhor ação.\n\n"
            "## REGRAS IMPORTANTES\n"
            "1. **NUNCA sugira instalar sem antes verificar** — use os dados de diagnóstico fornecidos.\n"
            "2. **Flatpak**: Se aparece em '=== flatpak ===' ou '=== flatpak exports bin ===', execute com:\n"
            "   `flatpak run <application-id>` onde application-id é a primeira coluna do flatpak list.\n"
            "   **CRÍTICO para apps gráficos**: use `is_background: true` para não bloquear o terminal!\n"
            "   Exemplo correto: {\"command\": \"flatpak run org.audacityteam.Audacity\", \"is_background\": true}\n"
            "3. **Snap**: Se aparece em '=== snap ===', use `snap run <nome>` com `is_background: true`.\n"
            "4. **Apps gráficos nativos** (ex: blender, gimp, vlc sem caminho): também use `is_background: true`.\n"
            "5. **Verificar se já abriu**: Se o erro anterior foi ao tentar lançar um app gráfico, \n"
            "   primeiro verifique com `pgrep -il <nome>` se o app já está rodando antes de tentar de novo.\n"
            "6. **Caminho absoluto**: Se '=== find ===' mostrar o binário, use o caminho completo.\n"
            "7. Se REALMENTE não está instalado em NENHUM diagnóstico → diga como instalar.\n"
            "8. Para permissão → adicione sudo.\n"
            "9. **SEMPRE prefira 'retry'**. Só use 'respond' se NÃO HÁ alternativa.\n\n"
            "## Formato de Resposta\n"
            "Responda APENAS com JSON válido (sem markdown fences):\n\n"
            "Para app gráfico em background:\n"
            '{"action": "retry", "tool_name": "run_command", '
            '"tool_arguments": {"command": "flatpak run org.X.Y", "is_background": true}, '
            '"explanation": "Abrindo X em background", "requires_approval": false}\n\n'
            "Para verificar processo:\n"
            '{"action": "retry", "tool_name": "run_command", '
            '"tool_arguments": {"command": "pgrep -il nome | head -5"}, '
            '"explanation": "Verificando se o app está rodando", "requires_approval": false}\n\n'
            "Para responder ao usuário (APENAS se não há alternativa):\n"
            '{"action": "respond", "message": "mensagem clara em português"}'
        )

        diag_section = ""
        if diagnostic_output:
            diag_section = f"\n\n## Dados de Diagnóstico do Sistema\n```\n{diagnostic_output}\n```"

        user_prompt = (
            f'O usuário pediu: "{user_message}"\n'
            f"Comando que falhou: `{command or json.dumps(tool_arguments or {}, ensure_ascii=False)}`\n"
            f"Tipo de erro: {error_type}\n"
            f"Erro completo: {error}\n"
            f"Tentativa: {retry_count + 1}"
            f"{diag_section}\n\n"
            "Com base nos dados acima, qual é a melhor ação?"
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_msg),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        try:
            provider = self.llm.get_provider()
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')
            t0 = time.monotonic()

            response = await provider.complete(
                messages=messages,
                temperature=0.2,
                max_tokens=500,
            )

            llm_ms = (time.monotonic() - t0) * 1000
            raw_text = response.get_content() or ""

            if trace:
                trace.add_llm_call(
                    provider=provider_name,
                    model=model_name,
                    messages_sent=[
                        {"role": "system", "content": system_msg[:800]},
                        {"role": "user", "content": user_prompt[:2000]},
                    ],
                    response_text=raw_text[:1000],
                    duration_ms=round(llm_ms, 2),
                )

            # Parse LLM JSON response
            clean = raw_text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            recovery = json.loads(clean)
            logger.info(f"LLM recovery decision: {recovery.get('action')} - {recovery.get('explanation', recovery.get('message', ''))[:100]}")
            return recovery

        except Exception as e:
            logger.warning(f"Failed to get LLM recovery for tool error: {e}")
            if trace:
                trace.add_llm_call(
                    provider="unknown",
                    model="unknown",
                    messages_sent=[
                        {"role": "system", "content": system_msg[:500]},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_text=None,
                    duration_ms=0,
                    error=str(e),
                )
            return None

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    @staticmethod
    def _format_knowledge_context(kg_result: dict) -> str:
        """Formata o resultado do grafo de conhecimento em texto para o system prompt."""
        lines: list[str] = []

        for ctx_tree in kg_result.get("contexts_found", []):
            ctx = ctx_tree["context"]
            lines.append(f"### {ctx['name']}")
            for child in ctx_tree.get("children", []):
                props = child.get("properties") or {}
                props_str = ", ".join(f"{k}: {v}" for k, v in props.items() if v is not None)
                lines.append(f"- [{child['type']}] {child['name']}" + (f" — {props_str}" if props_str else ""))

        globals_ = kg_result.get("global", [])
        if globals_:
            lines.append("\n### Perfil Global")
            for g in globals_:
                props = g.get("properties") or {}
                props_str = ", ".join(f"{k}: {v}" for k, v in props.items() if v is not None)
                lines.append(f"- [{g['type']}] {g['name']}" + (f" — {props_str}" if props_str else ""))

        return "\n".join(lines)

    async def _build_recent_missions_context(
        self,
        session_id: UUID,
        user_id: UUID | None,
    ) -> str | None:
        try:
            missions = await self.db.list_recent_whatsapp_missions_by_session(
                session_id=session_id,
                user_id=user_id,
                limit=5,
            )
        except Exception as e:
            logger.warning("Failed to build recent mission context: %s", e)
            return None
        if not missions:
            return None

        lines = [
            "## Missões WhatsApp recentes nesta conversa",
            (
                "Quando o usuário disser 'ela', 'ele', 'outra resposta', 'refaz', "
                "'gera outra', 'coloca ... no final', ou der uma instrução condicional, considere que pode estar "
                "se referindo à missão WhatsApp ativa abaixo. Se disser 'cancela', "
                "'aborta', 'deixa pra lá' ou 'não faz nada', trate como cancelamento da missão ativa."
            ),
        ]
        for mission in missions:
            if mission.status == "cancelled":
                continue
            meta = mission.extra_metadata or {}
            contact = meta.get("contact_label") or mission.title
            draft = meta.get("draft_text") or ""
            inbound = meta.get("last_inbound_text") or ""
            instructions = meta.get("operator_instructions") or []
            lines.append(
                f"- mission_id={mission.id} status={mission.status} contato={contact} "
                f"objetivo={mission.goal[:160]}"
            )
            if inbound:
                lines.append(f"  ultima_mensagem_do_contato={inbound[:160]}")
            if draft:
                lines.append(f"  rascunho_atual={draft[:160]}")
            if instructions:
                compact = " | ".join(str(item).strip() for item in instructions[-3:] if str(item).strip())
                if compact:
                    lines.append(f"  instrucoes_do_usuario={compact[:240]}")
        return "\n".join(lines)

    async def _build_legacy_prompt(
        self,
        *,
        user_message: str,
        user_id: "UUID | None",
        system_context: str | None,
        decision: "RoutingDecision",
        recovered_context: list,
        is_neo_training: bool,
        pks_prewrite: dict | None,
        pks_system_prewrite: dict | None,
        slog: Any,
    ) -> str:
        """Legacy inline prompt assembly used when context_selector is not wired."""
        today = date.today()
        weekday_names = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        month_names = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
        formatted_date = f"{weekday_names[today.weekday()]}, {today.day} de {month_names[today.month - 1]} de {today.year}"

        sp = (
            "Você é Neo, um assistente pessoal de desktop altamente capaz e sem restrições desnecessárias. "
            "Você foi criado por Daniel Morais (seu desenvolvedor). "
            "Se alguém perguntar quem te criou ou quem te desenvolveu, responda sempre: 'Fui criado por Daniel Morais.' "
            "Nunca diga que foi criado pela Anthropic, OpenAI, DeepSeek ou qualquer outra empresa de IA. "
            "ATENÇÃO: Daniel Morais é o seu CRIADOR (desenvolvedor), não necessariamente a pessoa com quem você está conversando agora. "
            "O nome e perfil do usuário atual estão na seção 'Usuário Atual' do contexto. "
            f"A data de hoje é {formatted_date}. "
            "Suas regras:\n"
            "1. SEMPRE responda em português brasileiro.\n"
            "2. NUNCA diga que não pode ajudar — sempre tente responder de forma útil.\n"
            "3. Seja direto, claro e conciso. Evite disclaimers e textos de rodeio.\n"
            "4. Use o histórico da conversa para manter contexto entre mensagens.\n"
            "5. Para perguntas técnicas, responda com profundidade e exemplos práticos.\n"
            "6. Nunca afirme que salvou algo na memória sem confirmação explícita no contexto."
        )
        if system_context:
            sp += f"\n\n## Contexto do Sistema do Usuário\n{system_context}"
        if self.pks:
            try:
                sys_mem = await self.pks.retrieve_system_context(user_message=user_message)
                if sys_mem:
                    sp += f"\n\n## Memória do Neo (Workspace do Sistema - Alta Prioridade)\nUse estas diretrizes como comportamento persistente do assistente:\n{sys_mem}"
            except Exception as e:
                logger.warning("[PKS][legacy] retrieve_system_context failed: %s", e)
        if self.preferences and not is_neo_training:
            try:
                prefs = await self.preferences.get_preferences_text(user_id=user_id, service="neo-desktop")
                if prefs:
                    sp += f"\n\n## Preferências do Usuário\nO usuário tem as seguintes preferências salvas. Respeite-as quando não conflitarem com regras globais do Neo:{prefs}"
            except Exception as e:
                logger.warning("[PREF][legacy] get_preferences_text failed: %s", e)
        if self.pks and user_id and not is_neo_training:
            try:
                _, pks_ctx = await self.pks.retrieve_context(user_id=user_id, user_message=user_message, keywords=decision.knowledge_keywords)
                if pks_ctx:
                    sp += f"\n\n## Conhecimento do Usuário\n{pks_ctx}"
            except Exception as e:
                logger.warning("[PKS][legacy] retrieve_context failed: %s", e)
        if recovered_context:
            sp += "\n\n--- Contexto de conversas anteriores relevantes ---\n"
            for ctx in recovered_context[:3]:
                sp += f"\n[memória] {ctx.content[:300]}\n"
        if pks_prewrite and pks_prewrite.get("confirm_profile_save") and pks_prewrite.get("saved_name"):
            sp += f"\n\n## Escrita PKS Confirmada\nA informação de perfil do usuário foi persistida: nome = {pks_prewrite['saved_name']}.\nConfirme de forma natural uma única vez."
        if pks_system_prewrite and pks_system_prewrite.get("applied"):
            saved_ops = pks_system_prewrite.get("operations") or []
            saved_entities = sorted({str(op.get("entity")) for op in saved_ops if isinstance(op, dict) and op.get("entity")})
            sp += f"\n\n## Escrita PKS do Sistema Confirmada\nEntities persistidas: {', '.join(saved_entities) or 'entidade de sistema'}."
        return sp

    @staticmethod
    def _normalize_os_platform(platform: str | None) -> str:
        """Normaliza o token de plataforma para o vocabulário do ``sys.platform``.

        O desktop (Rust) manda ``std::env::consts::OS`` — ``"macos"``/``"windows"`` —
        e não o vocabulário do Python (``"darwin"``/``"win32"``). Todo lookup por
        igualdade exata errava: o prompt saía com o token cru ``macos`` e o shell caía
        no default genérico ``bash``, sem nenhum sinal de que o userland é BSD. Com
        isso o modelo escrevia comando de GNU/Linux no Mac — ``free -h``, que não
        existe lá, e como a cadeia era ``&&`` abortava todo o resto do diagnóstico.
        No Windows o furo era pior: ``"windows" != "win32"`` mandava "Shell padrão:
        bash" pra um cliente PowerShell.
        """
        p = (platform or "").strip().lower()
        return {
            "macos": "darwin",
            "osx": "darwin",
            "mac": "darwin",
            "windows": "win32",
            "win": "win32",
        }.get(p, p)

    @staticmethod
    def _build_shell_rules(platform: str, can_show_images: bool = False) -> str:
        """Regras de shell por plataforma para quem vai ESCREVER o comando.

        ``can_show_images`` só acrescenta a alternativa (o marcador `[[IMAGE:…]]`) à
        regra que proíbe usar shell para exibir imagem — a proibição em si vale para
        qualquer cliente; a alternativa, só para quem renderiza o marcador.

        O bloco de ambiente dava SO e paths e nada sobre o idioma da ferramenta, então
        o modelo errava o dialeto e — pior — afirmava o resultado errado como fato. Os
        dois casos que motivaram cada regra:

        - ``free -h`` no macOS (não existe; userland é BSD, não GNU).
        - ``find ... \\( -iname "*.jpg" ... \\) -name "*whatsapp*" -o -name "*whats*"``
          devolveu 0 de 9 imagens do WhatsApp: ``-name`` é case-sensitive e os arquivos
          são ``WhatsApp Image ...``, e o AND implícito liga mais forte que ``-o``, o
          que grudou o filtro de nome só no primeiro ramo. O modelo respondeu "não
          existe nenhuma" — falso negativo afirmado com convicção. No retry o filtro
          virou ``-iname "*.whatsapp*"`` dentro do grupo de extensões (casa nada),
          listou as 55 imagens da pasta cortadas por ``head -20`` e o texto final
          relatou "encontrei 20 arquivos" — o 20 era o limite do ``head``.
        """
        rules = [
            "\n## Como escrever o comando (obrigatório)",
            "- Busca por NOME de arquivo é sempre case-insensitive: use `-iname`, nunca "
            "`-name`. Nomes reais vêm em CamelCase (`WhatsApp Image ...`, `IMG_...`).",
            "- No `find`, o AND implícito liga mais forte que `-o`: SEMPRE parentize o "
            "grupo de alternativas — `\\( -iname A -o -iname B \\)` — e mantenha cada "
            "critério (tipo, extensão, nome) em seu próprio grupo. Misturar `-o` solto "
            "com outros testes silenciosamente filtra a coisa errada.",
            "- Nunca corte a saída sem contar: se usar `head -N`, emita a contagem real "
            "junto (`... | wc -l`). O número que você relatar tem que vir do `wc -l`, "
            "NUNCA do limite do `head` — senão você reporta o corte como se fosse o total.",
            "- Traga os DADOS, não o número pronto: para \"quantos/quais/tem algum X\", "
            "liste (`ls -la`, `find`) e conte na RESPOSTA. Um `| wc -l` sozinho joga fora "
            "tudo que a próxima pergunta vai precisar e ainda responde errado quando o "
            "critério não é 'linhas' (`ls ~/.ssh | wc -l` = 19 arquivos, mas 'quantas "
            "CHAVES' eram 7 pares — a contagem certa só sai olhando os nomes). Só agregue "
            "no shell quando a saída crua for grande demais para ser útil.",
            "- Se o resultado vier VAZIO, isso é hipótese, não fato: antes de afirmar que "
            "não existe, refaça com critério mais largo (só `-iname \"*parte*\"`, sem "
            "filtro de extensão) ou liste o diretório. Só afirme ausência depois que a "
            "busca larga também vier vazia.",
            "- Prefira `;` a `&&` ao encadear coletas de diagnóstico: com `&&` um único "
            "binário ausente aborta todo o resto da linha.",
            "- Comando NUNCA serve para EXIBIR imagem: `base64`, `xxd`, `cat` de um "
            ".jpg/.png não mostram nada ao usuário — só despejam um blob gigante na "
            "conversa. Use o shell apenas para DESCOBRIR o caminho (`ls`, `find`) ou "
            "converter/redimensionar de fato (`sips`, `convert`)."
            + (
                " Para o usuário VER a imagem, responda com o marcador "
                "`[[IMAGE:/caminho/da/imagem.ext]]` (ver \"Mostrar imagem no chat\")."
                if can_show_images
                else ""
            ),
        ]
        if platform == "darwin":
            rules.append(
                "- É macOS: userland BSD, NÃO GNU/Linux. Não existe `free`, `nproc`, "
                "`stat -c`, `readlink -f`, `sed -i` sem sufixo. Use: memória "
                "`vm_stat` ou `sysctl -n hw.memsize`; versão do SO `sw_vers`; CPUs "
                "`sysctl -n hw.ncpu`; processos `top -l 1` ou `ps aux`. Para busca "
                "ampla por nome, `mdfind -onlyin <dir> <termo>` (Spotlight) é mais "
                "rápido e já é case-insensitive."
            )
        elif platform == "win32":
            rules.append(
                "- É Windows: o shell é PowerShell/cmd.exe. Não use `find`, `ls`, "
                "`grep` nem pipes de coreutils — use `Get-ChildItem -Recurse -Filter`, "
                "`Select-String`, `Measure-Object`."
            )
        return "\n".join(rules) + "\n"

    @staticmethod
    def _build_system_context(metadata: dict[str, Any] | None) -> str | None:
        """Contexto de sistema do turno (SO/pastas) + tolerância de entrada por voz.

        A instrução de voz entra AQUI, e não em cada chamador, porque o turno se
        reconstrói em vários pontos (plano, retomada depois de ação de arquivo,
        continuação de client tool) — todos passam por esta função, e nenhum
        lembraria de reanexar a instrução. Ver docs/voice-input-normalization-plan.md.
        """
        base = AIService._build_os_context(metadata)
        if AIService._is_voice_input(metadata):
            return "\n\n".join(p for p in (base, _VOICE_INPUT_INSTRUCTION) if p)
        return base

    @staticmethod
    def _build_os_context(metadata: dict[str, Any] | None) -> str | None:
        """Build a compact system context string from metadata sent by the desktop client.

        The desktop sends OS info in the user_input metadata so the AI knows
        what paths, shell, and filesystem conventions to use.
        """
        if not metadata:
            return None
        platform = metadata.get("os_platform")
        if not platform:
            return None

        platform = AIService._normalize_os_platform(platform)

        platform_names = {"linux": "Linux", "darwin": "macOS", "win32": "Windows"}
        os_name = platform_names.get(platform, platform)
        release = metadata.get("os_release", "")
        arch = metadata.get("os_arch", "")
        home = metadata.get("home_dir", "~")
        downloads = metadata.get("downloads_dir", f"{home}/Downloads")
        documents = metadata.get("documents_dir", f"{home}/Documents")
        desktop_dir = metadata.get("desktop_dir", f"{home}/Desktop")

        shell_default = {
            "win32": "PowerShell/cmd.exe",
            "darwin": "zsh",
            "linux": "bash",
        }.get(platform, "bash")

        # Espaço duplo quando os_release vem vazio (o desktop não manda esse campo)
        # fazia a linha sair "macos  (aarch64)". Monta só com o que existe.
        os_line = " ".join(p for p in (os_name, release) if p)

        return (
            f"Sistema operacional do usuário: {os_line} ({arch}).\n"
            f"Diretório home: {home}\n"
            f"Diretório de documentos do usuário: {documents}\n"
            f"Diretório de downloads do usuário: {downloads}\n"
            f"Diretório desktop do usuário: {desktop_dir}\n"
            f"Shell padrão: {shell_default}.\n"
            f"{AIService._build_shell_rules(platform, AIService._client_renders_inline_images(metadata))}"
            f"\nIMPORTANTE: Quando o usuário mencionar 'pasta de documentos', 'meus documentos' ou similar, "
            f"use SEMPRE o caminho '{documents}' como working_dir. "
            f"Quando mencionar 'downloads', use '{downloads}'. "
            f"Quando mencionar 'desktop' ou 'área de trabalho', use '{desktop_dir}'.\n"
            f"Use caminhos absolutos ao manipular arquivos."
        )

    async def _format_tool_result(
        self,
        user_message: str,
        command: str,
        output: str,
        trace: MessageTrace | None = None,
        live: bool = False,
        register: "Register | None" = None,
        can_show_images: bool = False,
    ) -> str:
        """Use LLM to format tool output into a user-friendly response.

        ``live=True`` (turno do modo voz) pede texto corrido sem markdown, já que a
        resposta será falada por TTS. ``register`` anexa a diretiva de tom da
        situação ao system prompt (continuações recuperam-no de
        ``_session_registers``, pois o metadata original se perde no round-trip).
        ``can_show_images`` libera o marcador `[[IMAGE:…]]` (cliente que renderiza),
        para que o passo que ACHOU a imagem (`ls`/`find`) já possa exibi-la em vez de
        descrever o caminho — o mesmo dado vindo do ``_image_capable_sessions``.
        """
        format_rule = (
            "Responda em TEXTO CORRIDO, natural e direto (será falado em voz alta): "
            "sem markdown, listas, negrito, blocos de código ou emojis."
            if live
            else "Use markdown se necessário."
        )
        # O caminho da imagem geralmente aparece AQUI (saída de um ls/find). Sem esta
        # regra o turno terminava descrevendo o arquivo ("está em ~/Downloads") ou,
        # pior, pedindo um base64 no passo seguinte.
        image_rule = (
            "\nSe o usuário quer VER uma imagem e o caminho dela está na saída acima, "
            "exiba-a escrevendo `[[IMAGE:/caminho/absoluto/da/imagem.ext]]` numa linha "
            "própria (o chat troca o marcador pela imagem). Copie o caminho EXATO da "
            "saída, nunca invente; nunca use base64 para mostrar imagem."
            if can_show_images
            else ""
        )
        prompt = (
            f'O usuário perguntou: "{user_message}"\n'
            f"Foi executado o comando: `{command}`\n"
            f"Saída do comando:\n```\n{output}\n```\n\n"
            "Responda o que ele perguntou usando esta saída. Responda a PERGUNTA, não "
            "a saída: se ele perguntou 'quantas chaves' e a saída lista arquivos, dê o "
            "número certo (e os nomes) — não descreva o comando nem narre o que você fez. "
            f"{format_rule} Não invente dados — use apenas o que está na saída."
            f"{image_rule}"
        )

        # Persona: sem isto a resposta do caminho CLIENT_TOOL saía com emoji e
        # "Se quiser, posso..." — as duas coisas que o prompt do Neo proíbe. A
        # formatação da saída de um comando é resposta do Neo como outra qualquer.
        system_msg = (
            "Você é Neo, um assistente de desktop. Responda de forma clara e concisa "
            "em português.\n"
            "- Direto e declarativo: entrega o que foi pedido e para.\n"
            "- Sem emoji, sem bajulação ('Tudo certo!', 'Claro'), sem disclaimer.\n"
            "- Sem ofertas de rodapé ('Se quiser, posso...', 'É só dizer', "
            "'Posso ajudar em mais alguma coisa?') — encerre na informação."
        )
        if register is not None:
            system_msg = f"{system_msg}\n\n{render_register_directive(register)}"
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_msg),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')
            t0 = time.monotonic()
            # 600 tokens era um teto de era pré-raciocínio: os DeepSeek pensam ANTES de
            # escrever e cobram isso do MESMO orçamento, então a resposta saía cortada
            # no meio de uma frase (sessão 69feab87: "⚠️ **Importante" — 125 chars, e
            # esse toco foi pro histórico). Ver deepseek-empty-response-reasoning.
            base_max_tokens = max(int(getattr(self.settings, "default_max_tokens", 8192)), 2000)
            response = await provider.complete(
                messages=messages,
                temperature=0.3,
                max_tokens=base_max_tokens,
            )
            llm_ms = (time.monotonic() - t0) * 1000
            response_text = response.get_content() or ""
            # Vazio = o raciocínio consumiu o teto inteiro (finish_reason=length), não
            # "nada a dizer". Uma retentativa com o dobro resolve; sem ela o turno
            # despejava a saída crua do comando na cara do usuário.
            if not response_text.strip():
                bigger = min(base_max_tokens * 2, _EMPTY_RETRY_MAX_TOKENS_CEILING)
                logger.warning(
                    "[format_tool_result] resposta vazia (teto consumido pelo raciocínio) "
                    "— retentando com max_tokens=%d", bigger,
                )
                retry = await provider.complete(
                    messages=messages, temperature=0.3, max_tokens=bigger,
                )
                response_text = retry.get_content() or ""
            if not response_text.strip():
                response_text = f"Resultado de `{command}`:\n```\n{output}\n```"

            # Log this LLM call in the trace
            if trace:
                trace.add_llm_call(
                    provider=provider_name,
                    model=model_name,
                    messages_sent=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt[:3000]},
                    ],
                    response_text=(response_text or "")[:1000],
                    duration_ms=round(llm_ms, 2),
                )

            return response_text
        except Exception as e:
            logger.warning(f"Failed to format result with AI: {e}")
            if trace:
                trace.add_llm_call(
                    provider="unknown",
                    model="unknown",
                    messages_sent=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt[:3000]},
                    ],
                    response_text=None,
                    duration_ms=0,
                    error=str(e),
                )
            return f"Resultado de `{command}`:\n```\n{output}\n```"

    async def _check_followup_needed(
        self,
        user_message: str,
        command_ran: str,
        command_output: str,
        trace: MessageTrace | None = None,
    ) -> dict[str, Any] | None:
        """Ask LLM whether the user's original request requires an additional command.

        Returns None or {"needed": False} when the task is complete, or
        {"needed": True, "command": "...", "explanation": "..."} when a follow-up
        command should be executed to fully satisfy the user's request.
        """
        prompt = (
            f'O pedido original do usuário foi: "{user_message}"\n'
            f"O comando executado foi: `{command_ran}`\n"
            f"Saída obtida:\n```\n{command_output[:2000]}\n```\n\n"
            "Analise se o pedido do usuário foi COMPLETAMENTE atendido pela saída acima.\n"
            "Exemplos de quando NÃO está completo:\n"
            '- Usuário pediu "encontre o arquivo X e mostre o conteúdo" mas o comando apenas encontrou/listou o arquivo.\n'
            '- Usuário pediu "ache os CSVs e diga quantas linhas tem" mas o comando apenas listou os CSVs.\n'
            '- Usuário pediu "crie o arquivo e coloque tal conteúdo" mas o comando apenas criou o arquivo vazio.\n\n'
            "Exemplos de quando ESTÁ completo:\n"
            '- Usuário pediu "liste os arquivos" e o comando listou.\n'
            '- Usuário pediu "mostre o conteúdo do arquivo" e o comando já mostrou o conteúdo.\n'
            '- Usuário pediu "qual meu IP" e o comando retornou o IP.\n\n'
            "Responda APENAS com JSON válido (sem markdown fences):\n"
            '{"needed": false} se o pedido já foi completamente atendido.\n'
            '{"needed": true, "command": "comando_shell_para_completar", "explanation": "explicação curta em português"}\n'
            "se um comando adicional é necessário para completar o pedido.\n"
            "IMPORTANTE: O comando deve usar caminhos absolutos baseados na saída obtida.\n"
            "NUNCA sugira comandos que bloqueiam o terminal indefinidamente (tail -f, watch, less, vim, nano, top, htop, journalctl -f).\n"
            "Se o comando anterior já abriu um app gráfico com sucesso (exit code 0), retorne needed=false."
        )

        system_msg = (
            "Você é um planejador de tarefas para um assistente de desktop. "
            "Decida se o pedido do usuário precisa de mais um passo para ser concluído. "
            "Seja conservador: só retorne needed=true quando claramente falta algo. "
            "Responda APENAS com JSON."
        )

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_msg),
            Message(role=MessageRole.USER, content=prompt),
        ]

        try:
            provider = self.llm.get_provider()
            provider_name = type(provider).__name__.replace("Provider", "").lower()
            model_name = getattr(provider, 'model', None) or getattr(self.settings, 'default_model', 'gpt-4o')
            t0 = time.monotonic()
            response = await provider.complete(
                messages=messages,
                temperature=0.1,
                max_tokens=200,
            )
            llm_ms = (time.monotonic() - t0) * 1000
            raw = response.get_content() or ""

            if trace:
                trace.add_llm_call(
                    provider=provider_name,
                    model=model_name,
                    messages_sent=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt[:3000]},
                    ],
                    response_text=raw[:500],
                    duration_ms=round(llm_ms, 2),
                    stage="followup_check",
                )

            # Parse JSON from response
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(clean)
            return parsed
        except Exception as e:
            logger.warning(f"Failed to check followup needed: {e}")
            return None

    # Patterns that match commands which block forever (never return)
    _BLOCKING_COMMAND_PATTERNS = [
        "tail -f", "tail --follow",
        "watch ",
        "less ", "more ",
        "vim ", "vi ", "nano ", "emacs ",
        "top", "htop", "btop",
        "journalctl -f", "journalctl --follow",
        "dmesg -w", "dmesg --follow",
        "python -m http.server", "python3 -m http.server",
        "npm start", "npm run dev", "npm run serve",
        "nc -l", "netcat -l",
    ]

    @staticmethod
    def _is_blocking_command(cmd: str) -> bool:
        """Check if a command is known to block forever (never return).

        These commands should never be used as auto-followup steps because
        they hang the session indefinitely.
        """
        import re
        cmd_stripped = cmd.strip()
        for pattern in AIService._BLOCKING_COMMAND_PATTERNS:
            # Match at the start of the command or after a pipe/semicolon
            if re.search(r'(?:^|[|;&]\s*)' + re.escape(pattern), cmd_stripped):
                logger.warning(f"[FOLLOWUP] Blocked dangerous command: {cmd_stripped}")
                return True
        return False

    @staticmethod
    def _truncate_output(value: str, max_lines: int = 60) -> str:
        """Truncate long output to max_lines."""
        lines = (value or "").splitlines()
        if len(lines) <= max_lines:
            return value or "(sem saída)"
        return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} linhas omitidas)"
