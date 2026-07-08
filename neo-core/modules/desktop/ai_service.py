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
from typing import Any
from uuid import UUID, uuid4

from core.agent.agent_executor import AgentExecutor
from core.agent.context_selector import PromptContextSelector
from core.llm.router import LLMRouter
from core.pks import PKSService
from core.cognitive import CognitiveReadExecutor, cognitive_tools_for
from core.pipeline.handoff_parser import extract_handoffs
from core.pipeline.orchestrator import PipelineOrchestrator
from core.pipeline.policy import handoff_requires_confirmation
from core.pipeline.types import Handoff, PipelineStep, StepOutcome
from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep
from core.router.intent_router import IntentRouter
from core.session.session_manager import PersistentSessionManager
from core.session.preference_extractor import PreferenceExtractor
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from db.async_client import AsyncPostgresClient
from observability.logger import get_obs, MessageTrace
from observability.session_file_logger import get_session_logger
from core.memory import ConversationMemory
from shared import (
    IntentType,
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

# Injetado no prompt da resposta final quando o turno vem do modo Live (voz):
# o texto será FALADO por TTS e mostrado como legenda, então pedimos texto corrido
# sem markdown/emojis (que soam mal falados e poluem a legenda). Ativado por
# metadata["mode"] == "live" (enviado pelo neo-desktop-tauri).
_LIVE_STYLE_INSTRUCTION = (
    "## MODO VOZ (live)\n"
    "Sua resposta será LIDA EM VOZ ALTA por TTS e exibida como legenda curta. "
    "Responda em TEXTO CORRIDO, natural e direto, como se estivesse falando. "
    "NÃO use markdown, títulos, listas/bullets, negrito, tabelas, blocos de código "
    "ou emojis. Seja conciso, porém completo o suficiente para responder. "
    "Evite ler em voz alta URLs longas, caminhos de arquivo e símbolos."
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
_HANDOFF_INSTRUCTION = (
    "## Acionar uma skill (handoff)\n"
    "Você é o núcleo cognitivo do Neo. Se, DEPOIS de analisar o contexto/PKS, você "
    "concluir que uma AÇÃO concreta deve ser executada agora por uma skill, escreva "
    "normalmente sua resposta ao usuário e, ao FINAL, adicione um bloco de controle "
    "(que o usuário NÃO vê), com a ORDEM REAL escrita dentro — por exemplo:\n"
    "[[HANDOFF:home_assistant]]ligar o ar-condicionado do quarto[[/HANDOFF]]\n"
    "NUNCA escreva a palavra \"ordem\" nem os sinais `< >` dentro do bloco — vá a ação "
    "de verdade.\n\n"
    "Skills disponíveis:\n"
    "- `home_assistant`: controlar a casa (luzes, tomadas, clima, persianas, cenas).\n"
    "- `whatsapp_messaging`: enviar mensagem de WhatsApp (passa por confirmação).\n"
    "- `music`: tocar música no Spotify/YouTube (passe a faixa/artista concretos).\n\n"
    "Regras:\n"
    "- O texto dentro do bloco deve ser uma ordem COMPLETA e sem ambiguidade "
    "(ex.: \"ligar o ar-condicionado do quarto\"), porque a skill NÃO vê esta conversa.\n"
    "- Só emita o bloco se a ação realmente deve acontecer agora — inclusive quando o "
    "usuário condiciona (\"se eu costumo, pode ligar\") e o contexto confirma que sim.\n"
    "- Se não há nada a executar, não escreva nenhum bloco."
)

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
    "/workspace antes). Depois de rodar, explique o resultado ao usuário em "
    "linguagem natural; não cole saídas cruas enormes. Para trabalho demorado, "
    "prefira rodar em background (quando disponível) e avise que vai avisar ao "
    "terminar — não segure a conversa esperando."
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
    "- Responda curto e natural; a mecânica dos blocos fica invisível ao usuário."
)

# Client tools the music skill dispatches to the desktop (Etapa 2). process_tool_result
# confirms these truthfully and skips the generic recovery flow.
_MUSIC_ACTUATION_TOOLS = frozenset({"open_url", "launch_uri", "media_command"})


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
        self._pending_traces: dict[str, MessageTrace] = {}
        # Sessões cujo último turno veio do modo Live — usado para estilizar a
        # resposta de um client tool (process_tool_result), onde o metadata["mode"]
        # original se perde no round-trip. Atualizado em process_user_input.
        self._live_sessions: set[str] = set()
        self._skill_executor: "SkillExecutor | None" = None
        # Lazily-built HA adapter for the cognitive-core live snapshot injection.
        self._ha_adapter: "HomeAssistantAdapter | None" = None
        # Lazily-built affect updater (per-user emotional vector). None until first use.
        self._affect_updater: Any | None = None
        # Strong refs to fire-and-forget post-turn tasks (prefs/PKS). Bare
        # create_task results can be garbage-collected mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()
        self.planner = PlanManager(llm_router)

    def _spawn_bg(self, coro, *, name: str) -> None:
        """Schedule a tracked fire-and-forget task (post-response work only)."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

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
        # 1. Get or create session (with topic rotation check)
        t0_total = time.monotonic()
        logger.info(f"[HISTORY-DEBUG] Input session_id from desktop: {session_id}")
        t0_session = time.monotonic()
        session = await self.sessions.get_or_create_session(
            session_id=session_id,
            user_id=user_id,
            service=service,
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
                    session_id=None, user_id=user_id, service=service
                )
                # Record a continuity link so the next (possibly elliptical)
                # follow-up can still recover the rotated-away conversation,
                # instead of orphaning it forever.
                session.extra_metadata = {
                    **(session.extra_metadata or {}),
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
        mission_context = await self._build_recent_missions_context(sid, user_id)
        if mission_context:
            sys_context = "\n\n".join(part for part in (sys_context, mission_context) if part)

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
        pending_clar = (session.extra_metadata or {}).get("pending_clarification")
        if pending_clar:
            prev_round = int(pending_clar.get("round", 1))
            clarification_round = prev_round + 1
            allow_clarification = prev_round < MAX_CLARIFICATION_ROUNDS
            forced_clarification = True
            # Clear the pending state up front so a failure can't trap the session.
            session.extra_metadata = {
                k: v for k, v in (session.extra_metadata or {}).items()
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
                ):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.SERVER_TOOL:
                async for resp in self._handle_server_tool(sid, user_message, history, decision, trace, slog=slog, user_id=user_id, live=live, metadata=metadata):
                    if resp.is_final:
                        final_response = resp.message
                    yield resp

            elif decision.intent == IntentType.CLIENT_TOOL:
                async for resp in self._handle_client_tool(sid, user_message, decision, trace, slog=slog, user_id=user_id, metadata=metadata):
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
    ) -> AsyncIterator[AIServiceResponse]:
        """Dispatch the turn through the PipelineOrchestrator.

        The ``step_runner`` closure adapts each ``PipelineStep`` to the matching
        ``_handle_*`` generator (whose signatures differ) and reports a
        ``StepOutcome`` once the handler is exhausted. In Phase 1 no handoffs are
        produced, so the orchestrator runs exactly one step.
        """

        handoff_enabled = self.settings.pipeline_handoff_enabled
        pipeline_step_index = 0

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

            if intent == IntentType.DIRECT_RESPONSE:
                gen = self._handle_direct_response(
                    sid, msg, history, dec, trace, sys_context,
                    slog=slog, user_id=user_id, metadata=step_metadata,
                    source_message_id=str(saved_msg.id) if saved_msg else None,
                    allow_clarification=allow_clarification,
                    clarification_round=clarification_round,
                    enable_handoff=handoff_enabled,
                )
            elif intent == IntentType.SERVER_TOOL:
                gen = self._handle_server_tool(
                    sid, msg, history, dec, trace, slog=slog, user_id=user_id,
                    live=(step_metadata or {}).get("mode") == "live",
                    metadata=step_metadata,
                )
            elif intent == IntentType.CLIENT_TOOL:
                gen = self._handle_client_tool(
                    sid, msg, dec, trace, slog=slog, user_id=user_id, metadata=step_metadata,
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
            handoffs: list[Handoff] = []
            async for resp in gen:
                control = (resp.metadata or {}).get("__pipeline_handoffs__")
                if control:
                    # Internal control item — capture, never forward to the client.
                    handoffs = control
                    continue
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
                    handoffs=[h.target_skill or h.target_intent.value for h in handoffs],
                )
            yield None, StepOutcome(ok=True, final_text=last_final, handoffs=handoffs)

        orchestrator = PipelineOrchestrator(_step_runner)
        async for resp in orchestrator.run(self._seed_steps(decision, user_message)):
            yield resp

    # ---------------------------------------------------------------
    # Handler: DIRECT_RESPONSE — conversational, no tools
    # ---------------------------------------------------------------

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

        # Ensure PKS exists and persist direct factual statements before the reply.
        pks_prewrite: dict[str, Any] | None = None
        pks_system_prewrite: dict[str, Any] | None = None
        if self.pks and user_id:
            try:
                await self.pks.ensure_user_pks(user_id=user_id)
                if not is_neo_training:
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
                messages.append(Message(role=MessageRole.SYSTEM, content=_HANDOFF_INSTRUCTION))

        # Workbench: tell the brain it has its own sandbox to write/run code when no
        # specific tool fits. Training turns get no tools, so skip the note there too.
        if (
            self.settings.workbench_enabled
            and self.settings.cognitive_tools_enabled
            and not is_neo_training
        ):
            messages.append(Message(role=MessageRole.SYSTEM, content=_WORKBENCH_INSTRUCTION))

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
                )
                # The cognitive core's toolset = read tools + (when enabled) its own
                # agent tools: the workbench sandbox (neo_*, dispatched through the
                # global executor) and background jobs (neo_bash_background/job_*,
                # which need the turn's user/session — routed to a per-request
                # JobToolExecutor via the composite below).
                cognitive_tools = list(cognitive_tools_for(user_id))
                cog_executor: Any = read_executor
                if self.settings.workbench_enabled:
                    cognitive_tools += self._agent_action_tools()
                    cog_executor = self._build_cognitive_executor(
                        read_executor, user_id=user_id, session_id=session_id
                    )
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
                )
                llm_ms = (time.monotonic() - t0) * 1000
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

        except Exception as e:
            logger.exception("Direct response LLM call failed")
            full_response = f"Erro ao processar: {str(e)}"
            if slog:
                slog.log_event("Erro LLM", f"```\n{str(e)}\n```")

        # Clarification loop: the model signalled it lacks context and asked a
        # question. Persist the pending state and return the question — the user's
        # next message will resolve it in a second LLM iteration.
        clar_question = _extract_clarification_question(full_response) if allow_clarification else None
        if clar_question:
            try:
                sess = await self.db.get_conversation_session(session_id)
                if sess is not None:
                    sess.extra_metadata = {
                        **(sess.extra_metadata or {}),
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
        if enable_handoff:
            full_response, pipeline_handoffs = extract_handoffs(full_response)
            if pipeline_handoffs and not full_response.strip():
                # Model emitted only the directive — give a short acknowledgement.
                full_response = "Ok, já cuido disso."
            if pipeline_handoffs and slog:
                slog.log_event(
                    "Pipeline Handoff",
                    "\n".join(
                        f"- → {h.target_skill or h.target_intent.value}: {h.input_text}"
                        for h in pipeline_handoffs
                    ),
                )
        else:
            # Defensive: strip a stray block even when handoffs are disabled.
            full_response, _ = extract_handoffs(full_response)

        # Music reliability (neo-music-feature): a music turn must resolve to either a
        # [[HANDOFF:music]] (already extracted above) or an explicit [[NO_MUSIC]]. Strip
        # the NO_MUSIC token; if NEITHER was emitted, a cheap verifier decides whether
        # the reply intended to play and synthesizes the handoff so it never no-ops.
        if decision.skill_hint == "music" and enable_handoff:
            had_no_music = "[[NO_MUSIC]]" in full_response or "[[NO MUSIC]]" in full_response
            full_response = full_response.replace("[[NO_MUSIC]]", "").replace("[[NO MUSIC]]", "").strip()
            already_music = any(
                h.target_skill == "music" or h.target_intent == IntentType.MUSIC
                for h in pipeline_handoffs
            )
            if not already_music and not had_no_music and self.settings.music_handoff_verifier_enabled:
                synth = await self._verify_music_decision(user_message, full_response)
                if synth is not None:
                    pipeline_handoffs.append(synth)
                    if slog:
                        slog.log_event("Music Handoff (verifier)", f"→ music: {synth.input_text}")
            if not full_response.strip():
                full_response = "Ok 🎵"

        # Save assistant response
        t0_save = time.monotonic()
        await self.sessions.add_message(
            session_id=session_id,
            role="assistant",
            content=full_response,
            generate_embedding=False,
            user_id=user_id,
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

        # A handoff to a CLIENT-actuating skill (music: media_command/open_url/launch_uri)
        # needs a DESKTOP round-trip AFTER this reply. If we mark this reply is_final, the
        # desktop closes the gRPC stream and the play never reaches it ("cancelled by
        # client"). So keep the stream open for such handoffs — the skill step's tool +
        # its result deliver the real final. Server-side handoffs (home_assistant/
        # whatsapp) act on neo-core, so their reply stays final. See neo-music-feature.
        _client_handoff = any(
            h.target_intent == IntentType.MUSIC for h in pipeline_handoffs
        )

        # Yield final response FIRST — background tasks must never block the user-facing round-trip.
        # On a music handoff this reply isn't the stream's final (the play follows), but it IS a
        # real user-facing message — tag it persist_reply so the desktop renders it as its own chat
        # bubble (not a transient "thinking"), then shows "🎵 …" as a second message.
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=session_id,
            message=full_response,
            is_final=not _client_handoff,
            iterations=1,
            metadata={"persist_reply": True} if _client_handoff else None,
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
                applied = []
                if not is_neo_training and not device_memory_handled:
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

        # Affect is NOT scheduled here: process_user_input's epilogue is the
        # single choke point (covers this path plus skills/HA/tools/WhatsApp
        # outbound), with dedup by source_message_id in the AffectUpdater.
        self._spawn_bg(_bg_prefs(), name="bg-prefs")
        self._spawn_bg(_bg_pks(), name="bg-pks")

        # Internal control item: hand the parsed handoffs back to the orchestrator.
        # ``message=None`` + the private metadata key make it non-user-facing; the
        # step runner consumes it and never forwards it to the client.
        if pipeline_handoffs:
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE,
                session_id=session_id,
                message=None,
                is_final=False,
                metadata={"__pipeline_handoffs__": pipeline_handoffs},
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
                    tools += list(JOB_TOOLS)
            except Exception:  # noqa: BLE001
                pass
        return tools

    def _build_cognitive_executor(
        self, read_executor: Any, *, user_id: UUID | None, session_id: UUID | None
    ) -> Any:
        """Composite executor: job tools → JobToolExecutor; everything else → reads.

        The read executor already delegates unknown names (like ``neo_bash``) to the
        global tool executor, so the workbench tools work through it. Only the
        background-job tools need the turn's user/session context, so they are routed
        to a fresh per-request :class:`JobToolExecutor`.
        """
        if self.job_manager is None:
            return read_executor
        try:
            from core.jobs import JOB_TOOL_NAMES, JobToolExecutor
        except Exception:  # noqa: BLE001
            return read_executor
        job_executor = JobToolExecutor(
            db=self.db,
            job_manager=self.job_manager,
            user_id=user_id,
            session_id=session_id,
        )
        return _CognitiveAgentExecutor(read_executor, job_executor, JOB_TOOL_NAMES)

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
    ) -> str:
        """Bounded read-tool loop for the cognitive core.

        Mirrors the text-based ReAct pattern of AgentExecutor (tool results fed back
        as a user message). Returns the final answer text. Identical buffered output
        to the legacy streaming path when the model calls no tools.
        """
        max_rounds = self.settings.cognitive_max_tool_rounds
        seen: set[tuple[str, str]] = set()
        final_text = ""

        for round_idx in range(max_rounds + 1):
            round_tools = tools if (tools and round_idx < max_rounds) else None
            if slog:
                slog.log_llm_request("COGNITIVE", messages, model=model_name, provider=provider_name, temperature=0.9)
            t0 = time.monotonic()
            resp = await provider.complete(
                messages=messages,
                tools=round_tools,
                model=req_model or None,
                temperature=0.9,
                max_tokens=self.settings.default_max_tokens,
            )
            llm_ms = (time.monotonic() - t0) * 1000
            content = resp.get_content() or ""
            tool_calls = resp.get_tool_calls() if round_tools else []

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
                final_text = content
                if slog:
                    slog.log_llm_response("COGNITIVE", final_text, duration_ms=llm_ms, model=model_name)
                break

            # Feed tool results back as a user message (text-based, like AgentExecutor).
            messages.append(Message(role=MessageRole.ASSISTANT, content=content))
            result_lines: list[str] = []
            for tc in tool_calls:
                key = (tc.name, json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False))
                if key in seen:
                    result_lines.append(f"Tool: {tc.name}\nResult: (já consultado neste turno — não repita; use o resultado anterior)")
                    continue
                seen.add(key)
                if slog:
                    slog.log_event("Cognitive Tool", f"→ `{tc.name}` {tc.arguments}")
                tr = await tool_executor.execute_tool_call(tc)
                if tr.error:
                    result_lines.append(f"Tool: {tc.name}\nError: {tr.error}")
                else:
                    result_lines.append(f"Tool: {tc.name}\nResult: {tr.result}")
                if slog:
                    slog.log_event(
                        "Cognitive Tool Result",
                        f"`{tc.name}` → " + (f"ERRO: {tr.error}" if tr.error else str(tr.result)[:600]),
                    )
            messages.append(Message(role=MessageRole.USER, content="\n\n".join(result_lines)))

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
        from core.skills.music.service import parse_media_context

        tabs = (parse_media_context(metadata) or {}).get("browser_tabs") or []
        lines = []
        for t in tabs:
            if not isinstance(t, dict):
                continue
            site = t.get("site") or "?"
            title = (t.get("title") or t.get("url") or "").strip()
            playing = " (tocando)" if t.get("playing") else ""
            lines.append(f"- {site}: {title}{playing}".rstrip())
        if not lines:
            return None
        return (
            "## Mídia aberta no navegador (tempo real)\n"
            "Abas de música/vídeo abertas agora:\n"
            + "\n".join(lines)
            + "\nSe o usuário pedir pra PAUSAR, RETOMAR/dar play, PULAR, VOLTAR ou mudar "
            "VOLUME dessa mídia, você DEVE emitir ao final o bloco "
            "[[HANDOFF:music]]<ação>[[/HANDOFF]] (ação = pausar | retomar | próxima | "
            "anterior | mudo | aumentar volume | abaixar volume) — é ELE que executa de "
            "verdade. NUNCA diga que pausou/voltou/pulou SEM emitir o bloco. Use também "
            "estas abas pra responder o que está tocando. Se nada estiver tocando, diga "
            "isso em vez de fingir que controlou."
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
            lines.append(f"- sistema: {os_platform}")
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
    ) -> AsyncIterator[AIServiceResponse]:
        """Handle server-side tool execution using AgentExecutor.

        ``live=True`` (turno do modo voz) anexa o estilo de fala ao task para o
        agente responder em texto corrido sem markdown. ``metadata`` carrega
        sinais do cliente (ex: GPS do dispositivo em ``metadata["geo"]``).
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

            # Use AgentExecutor for multi-step tool execution
            result = await self.agent.execute(
                task=task,
                session_id=session_id,
                memory=memory,
                tool_executor=ctx_executor,
            )

            final_text = result.get("final_response", "Tarefa concluída.")
            iterations = result.get("iterations", 1)
            tool_ms = (time.monotonic() - t0) * 1000
            if slog:
                slog.log_tool_result(decision.tool_name or "agent", success=True, result=final_text, duration_ms=tool_ms, execution="server-side")

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
                max_tokens=120,
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

    async def _handle_client_tool(
        self,
        session_id: UUID,
        user_message: str,
        decision: RoutingDecision,
        trace: MessageTrace | None = None,
        slog: "SessionFileLogger | None" = None,
        user_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
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

                # Send plan summary to user
                yield AIServiceResponse(
                    intent=IntentType.CLIENT_TOOL,
                    session_id=session_id,
                    message=plan.to_summary(),
                    is_final=False,
                )

                # Send first step
                first_step = plan.get_current_step()
                if first_step:
                    first_step.status = "running"
                    step_tool_call_id = str(uuid4())

                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        message=f"{plan.progress_text}: {first_step.description}",
                        is_final=False,
                    )

                    yield AIServiceResponse(
                        intent=IntentType.CLIENT_TOOL,
                        session_id=session_id,
                        tool_request={
                            "tool_call_id": step_tool_call_id,
                            "tool_name": first_step.tool_name,
                            "arguments": json.dumps(first_step.tool_arguments, ensure_ascii=False),
                            "requires_approval": first_step.requires_approval,
                        },
                        is_final=False,
                        metadata={
                            "flow": "client_tool",
                            "tool_call_id": step_tool_call_id,
                            "tool_name": first_step.tool_name,
                            "user_message": user_message,
                            "tool_arguments": first_step.tool_arguments,
                            "command": first_step.raw_command or first_step.tool_arguments.get("command", first_step.tool_name),
                            "_has_plan": True,
                        },
                    )
                    return  # Flow continues in process_tool_result

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
                        else:
                            # Send next step directly
                            next_step.status = "running"
                            step_tool_call_id = str(uuid4())

                            # Emit plan update
                            plan_update = self.planner.build_plan_update(active_plan)
                            yield AIServiceResponse(
                                intent=IntentType.CLIENT_TOOL,
                                session_id=session_id,
                                message=f"{active_plan.progress_text}: {next_step.description}",
                                is_final=False,
                                metadata={"plan_update": plan_update},
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

                                    yield AIServiceResponse(
                                        intent=IntentType.CLIENT_TOOL,
                                        session_id=session_id,
                                        message=f"📋 Sub-plano: {subplan.goal}\n" + subplan.to_summary(),
                                        is_final=False,
                                    )
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
                        if decision in ("continue", "retry", "subplan") and next_step:
                            next_step.status = "running"
                            step_tool_call_id = str(uuid4())

                            plan_update = self.planner.build_plan_update(active_plan)
                            yield AIServiceResponse(
                                intent=IntentType.CLIENT_TOOL,
                                session_id=session_id,
                                message=f"{active_plan.progress_text}: {next_step.description}",
                                is_final=False,
                                metadata={"plan_update": plan_update},
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
                            return  # Flow continues when desktop sends next tool_result

                # Plan complete — format final unified response
                if not active_plan.is_complete:
                    active_plan.status = "completed"
                final_text = await self.planner.build_final_response(active_plan, user_message)
                self.planner.clear_plans(sid_str)

                if slog:
                    slog.log_event("Plano Concluído", f"**Status:** {active_plan.status}\n**Resultado:**\n{final_text[:500]}")

            else:
                # Format with AI (trace is passed so the LLM call gets logged)
                final_text = await self._format_tool_result(
                    user_message, command or tool_name, raw_output, trace=trace,
                    live=str(session_id) in self._live_sessions,
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
        if any(k in err_lower for k in ("command not found", "not found", "código 127", "code 127", "no such file")):
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
                    logger.info(f"[Diagnostic] permission_denied but path doesn't exist: {target_path}. Switching to write_file tool.")
                    return {
                        "action": "retry",
                        "tool_name": "write_file",
                        "tool_arguments": {"path": target_path, "content": "", "mode": "write"},
                        "explanation": f"O diretório não existe. Criando o arquivo {target_path} usando write_file (cria diretórios automaticamente)...",
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
    def _build_system_context(metadata: dict[str, Any] | None) -> str | None:
        """Build a compact system context string from metadata sent by the desktop client.

        The desktop sends OS info in the user_input metadata so the AI knows
        what paths, shell, and filesystem conventions to use.
        """
        if not metadata:
            return None
        platform = metadata.get("os_platform")
        if not platform:
            return None

        platform_names = {"linux": "Linux", "darwin": "macOS", "win32": "Windows"}
        os_name = platform_names.get(platform, platform)
        release = metadata.get("os_release", "")
        arch = metadata.get("os_arch", "")
        home = metadata.get("home_dir", "~")
        downloads = metadata.get("downloads_dir", f"{home}/Downloads")
        documents = metadata.get("documents_dir", f"{home}/Documents")
        desktop_dir = metadata.get("desktop_dir", f"{home}/Desktop")

        shell_default = (
            "PowerShell/cmd.exe"
            if platform == "win32"
            else {"linux": "bash/zsh", "darwin": "zsh/bash"}.get(platform, "bash")
        )

        return (
            f"Sistema operacional do usuário: {os_name} {release} ({arch}).\n"
            f"Diretório home: {home}\n"
            f"Diretório de documentos do usuário: {documents}\n"
            f"Diretório de downloads do usuário: {downloads}\n"
            f"Diretório desktop do usuário: {desktop_dir}\n"
            f"Shell padrão: {shell_default}.\n"
            f"IMPORTANTE: Quando o usuário mencionar 'pasta de documentos', 'meus documentos' ou similar, "
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
    ) -> str:
        """Use LLM to format tool output into a user-friendly response.

        ``live=True`` (turno do modo voz) pede texto corrido sem markdown, já que a
        resposta será falada por TTS.
        """
        format_rule = (
            "Responda em TEXTO CORRIDO, natural e direto (será falado em voz alta): "
            "sem markdown, listas, negrito, blocos de código ou emojis."
            if live
            else "Use markdown se necessário."
        )
        prompt = (
            f'O usuário perguntou: "{user_message}"\n'
            f"Foi executado o comando: `{command}`\n"
            f"Saída do comando:\n```\n{output}\n```\n\n"
            "Formate uma resposta clara e amigável em português para o usuário, "
            "resumindo o resultado do comando de forma legível. "
            f"{format_rule} Não invente dados — use apenas o que está na saída."
        )

        system_msg = "Você é Neo, um assistente de desktop. Responda de forma clara e concisa em português."
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
                temperature=0.3,
                max_tokens=600,
            )
            llm_ms = (time.monotonic() - t0) * 1000
            response_text = response.get_content() or f"Resultado de `{command}`:\n```\n{output}\n```"

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
