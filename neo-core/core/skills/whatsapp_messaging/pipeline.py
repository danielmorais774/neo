"""WhatsAppMessagingPipeline — orchestration for WhatsApp messaging missions.

Two modes share this pipeline:

* **personal** — single, deterministic flow: resolve recipient → build draft →
  pause for human approval → send on approve. No agent loop on kickoff. The
  user always sees the destination phone before the message goes out.

* **commercial** — agent loop with the LLM negotiating multi-turn (delivery
  orders, store interactions). Confirmation is requested via tool calls when
  the agent crosses sensitive thresholds (price, address, payment).

Inbound replies and approvals reuse the existing handlers.
"""

import logging
import os
import re
import asyncio
import unicodedata
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from observability.session_file_logger import SessionFileLogger
    from core.pks import PKSService

from core.agent.agent_executor import AgentExecutor
from core.agent.prompt_builder import PromptBuilder
from core.memory import ConversationMemory
from core.skills import SkillPipeline, SkillResult
from core.skills.whatsapp_messaging.executor import WhatsAppMessagingExecutor
from core.skills.whatsapp_messaging.gateway_client import WhatsAppGatewayClient
from core.skills.whatsapp_messaging.tools import WHATSAPP_MESSAGING_TOOLS
from core.notifications.broker import NotificationBroker
from db.async_client import AsyncPostgresClient
from observability.session_file_logger import SessionFileLogger, get_session_logger
from shared import Message, MessageRole

logger = logging.getLogger(__name__)

_LOGS_DIR = Path(os.environ.get("SESSION_LOGS_DIR", "/app/logs"))
if not _LOGS_DIR.exists():
    _LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _get_pipeline_logger(session_id: UUID) -> SessionFileLogger:
    # Reuse one logger instance/path per session to avoid splitting logs.
    return get_session_logger(str(session_id))


def _get_mission_logger(mission_id: UUID) -> SessionFileLogger:
    return SessionFileLogger(
        str(mission_id),
        file_prefix="mission",
        include_timestamp=False,
        title="Mission Dev Log",
    )


def get_mission_dev_logger(mission_id: UUID) -> SessionFileLogger:
    return _get_mission_logger(mission_id)


class _FanoutLogger:
    def __init__(self, *targets: SessionFileLogger) -> None:
        self._targets = [t for t in targets if t is not None]

    def __getattr__(self, name: str):
        def _call(*args, **kwargs):
            for target in self._targets:
                fn = getattr(target, name, None)
                if callable(fn):
                    fn(*args, **kwargs)
        return _call


DEFAULT_USER_ADDRESS = "Rua Exemplo 123, São Paulo, SP"
DEFAULT_USER_PHONE = "+55 11 99999-9999"
DEFAULT_PAYMENT_PREFS = "PIX ou dinheiro na entrega"

# Stopwords stripped before fuzzy contact lookup. "minha mãe" → "mãe".
_RECIPIENT_STOPWORDS = {
    "minha", "meu", "para", "pra", "pro", "o", "a", "do", "da", "de", "no",
    "na", "ao", "à", "as", "os", "uma", "um",
}

_COMMERCIAL_HINTS = {
    "pizza", "pizzaria", "delivery", "ifood", "rappi", "uber", "domino",
    "dominos", "burger", "hamburguer", "lanche", "esfiha", "marmita",
    "pedido", "pedir", "encomenda", "encomendar", "comprar", "compra",
    "farmacia", "farmácia", "remedio", "remédio", "mercado", "supermercado",
    "loja", "restaurante", "comida",
}

# Signals that a personal-domain request needs a tracked mission (negotiation /
# goal completion), not a one-shot "direct" send. Accent-folded before matching.
_COORDINATION_HINTS = {
    "agenda", "agendar", "agende", "combina", "combinar", "combine",
    "marcar", "remarcar", "confirma se", "confirmar se", "confirme se",
    "verifica com", "verificar com", "verifique com", "pergunta pra todos",
    "pergunte a todos", "pergunta a todos", "quem tem", "quem pode", "quem tiver",
    "quando ele pode", "quando ela pode", "que dia ele", "que dia ela",
    "que horas ele", "que horas ela", "disponibilidade", "orcamento", "cotacao",
    "negocia", "negociar",
}

_JID_RE = re.compile(r"\b(\d{6,20}@(?:c\.us|lid|s\.whatsapp\.net|g\.us))\b")
_PHONE_RE = re.compile(r"\+?\d{10,15}")
_DEFAULT_MESSAGE_DELAY_MS = 1200
_MAX_PERSONAL_MESSAGE_PARTS = 3
_MAX_COMMERCIAL_MESSAGE_PARTS = 2
_MAX_MESSAGE_PART_CHARS = 180
_FOLLOWUP_INSTRUCTION_PREFIXES = (
    "se ",
    "caso ",
    "quando ",
    "e se ",
    "ah, se ",
    "ah se ",
    "tambem ",
    "também ",
)
_FOLLOWUP_INSTRUCTION_MARKERS = (
    " ela ",
    " ele ",
    " eles ",
    " elas ",
    " responder",
    " perguntar",
    " falar",
    " disser",
    " mandar",
    " enviar",
)
_ACTIVE_MISSION_FOLLOWUP_MARKERS = (
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
    "refaça",
    "reescreve",
    "reescrever",
    "muda a resposta",
    "troca a resposta",
    "melhor resposta",
    "mais natural",
    "mais curto",
    "nova sugestao",
    "nova sugestão",
    "outro rascunho",
)
_DRAFT_EDIT_MARKERS = (
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
    "no começo",
    "no comeco",
    "mais gratidao",
    "mais gratidão",
    "mais agradecido",
    "mais agradecida",
    "mais carinho",
    "mais carinhoso",
    "mais carinhosa",
    "mais seco",
    "mais direta",
    "mais direto",
)
_MISSION_CANCEL_MARKERS = (
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
    "não faz nada",
    "nao precisa responder",
    "não precisa responder",
    "sem resposta",
    "ignora",
    "ignore",
)


class WhatsAppMessagingPipeline(SkillPipeline):
    def __init__(
        self,
        agent_executor: AgentExecutor,
        db: AsyncPostgresClient,
        broker: NotificationBroker,
        pks: "PKSService | None" = None,
    ) -> None:
        self.agent_executor = agent_executor
        self.db = db
        self.broker = broker
        self.pks = pks
        self.gateway = WhatsAppGatewayClient()
        self.executor = WhatsAppMessagingExecutor(db, self.gateway)

    async def _notify(
        self,
        user_id: UUID,
        mission_id: UUID | None,
        kind: str,
        title: str,
        body: str,
        payload: dict | None = None,
        actions: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Persist notification and push it to the user's live broker queue."""
        notif = await self.db.create_notification(
            user_id=user_id,
            mission_id=mission_id,
            kind=kind,
            title=title,
            body=body,
            payload=payload or {},
        )
        notification = {
            "id": str(notif.id),
            "user_id": str(user_id),
            "mission_id": str(mission_id) if mission_id else "",
            "kind": kind,
            "title": title,
            "body": body,
            "description": body,
            "payload": payload or {},
            "actions": actions or [],
            "config": config or {},
            "created_at_unix": int(notif.created_at.timestamp()) if notif.created_at else 0,
        }

        try:
            await self.broker.publish(
                user_id,
                notification,
            )
        except Exception as e:
            logger.warning("Broker publish failed kind=%s err=%s", kind, e)

        try:
            from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

            broadcaster = get_device_notif_broadcaster()
            devices = await self.db.get_connected_devices(user_id)
            sent_to: set[str] = set()
            for device in devices:
                for key in (device.device_id, device.ws_connection_id):
                    if key and key not in sent_to:
                        if await broadcaster.send_notification(key, notification):
                            sent_to.add(key)
            if sent_to:
                logger.info(
                    "Device websocket notification sent kind=%s devices=%s",
                    kind,
                    ",".join(sorted(sent_to)),
                )
        except Exception as e:
            logger.warning("Device websocket publish failed kind=%s err=%s", kind, e)

    async def run(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        mission_id: UUID | None = None,
        extra: dict[str, Any] | None = None,
        user_id: UUID | None = None,
    ) -> SkillResult:
        extra = extra or {}
        user_id = user_id or extra.get("user_id")

        slog = _get_pipeline_logger(session_id)
        mission_slog = _get_mission_logger(mission_id) if mission_id else None
        if mission_slog:
            slog = _FanoutLogger(slog, mission_slog)
        slog.new_interaction(
            user_message,
            metadata={
                "skill": "whatsapp_messaging",
                "mission_id": str(mission_id) if mission_id else None,
                "user_id": str(user_id) if user_id else None,
            },
        )

        if mission_id:
            mission = await self.db.get_mission(mission_id)
            if not mission:
                slog.log_error_recovery(
                    1, "MissionNotFound", "abort", {"mission_id": str(mission_id)}
                )
                return SkillResult(ok=False, message="Missão não encontrada")

            slog.log_event(
                "Mission context loaded",
                {
                    "mission_id": str(mission.id),
                    "session_id": str(mission.session_id),
                    "user_id": str(mission.user_id),
                    "status": mission.status,
                    "title": mission.title,
                    "goal": mission.goal,
                    "contact_jid": mission.contact_jid,
                    "extra_metadata": mission.extra_metadata or {},
                    "extra": extra,
                },
            )

            if extra.get("approved") is not None:
                return await self._handle_approval(mission, extra, slog)
            if self._is_operator_instruction(extra):
                return await self._handle_active_mission_user_message(mission, user_message, slog)
            # Direct (light) missions classify the reply (close/ack/escalate/promote)
            # and auto-close, instead of always drafting an ack like a tracked mission.
            if (mission.extra_metadata or {}).get("kind") == "direct":
                return await self._handle_direct_reply(mission, user_message, slog, extra, history)
            return await self._handle_inbound(mission, user_message, slog, extra, history)

        if user_id and self._looks_like_active_mission_followup(user_message):
            active_mission = await self.db.find_active_whatsapp_mission_by_session(session_id, user_id)
            if active_mission:
                mission_slog = _get_mission_logger(active_mission.id)
                fanout_slog = _FanoutLogger(slog, mission_slog)
                fanout_slog.log_event(
                    "whatsapp_messaging.user_message_routed_to_active_mission",
                    {
                        "mission_id": str(active_mission.id),
                        "session_id": str(session_id),
                        "user_message": user_message,
                        "reason": "active_whatsapp_mission_in_session",
                    },
                )
                return await self._handle_active_mission_user_message(
                    active_mission,
                    user_message,
                    fanout_slog,
                )

        return await self._handle_kickoff(session_id, user_message, extra, slog, user_id)

    async def execute_mission_command(
        self,
        mission_id: UUID,
        command: str,
        edited_text: str | None = None,
        user_id: UUID | None = None,
        source: str = "api",
    ) -> SkillResult:
        """Direct structured command dispatch for mission actions."""
        mission = await self.db.get_mission(mission_id)
        if not mission:
            return SkillResult(ok=False, message="Missão não encontrada", mission_id=mission_id)

        slog = _FanoutLogger(_get_pipeline_logger(mission.session_id), _get_mission_logger(mission_id))
        slog.new_interaction(
            f"mission_command:{command}",
            metadata={
                "skill": "whatsapp_messaging",
                "mission_id": str(mission_id),
                "user_id": str(user_id or mission.user_id),
                "source": source,
            },
        )

        if command == "cancel":
            return await self._handle_approval(
                mission,
                {"approved": False, "approval_source": source, "user_id": user_id or mission.user_id},
                slog,
            )

        if command == "edit" and edited_text:
            return await self._handle_approval(
                mission,
                {
                    "approved": True,
                    "edited_text": edited_text,
                    "approval_source": source,
                    "user_id": user_id or mission.user_id,
                },
                slog,
            )
        if command == "edit":
            return SkillResult(
                ok=False,
                message="Comando edit requer texto editado.",
                mission_id=mission_id,
            )

        if command in {"approve", "edit", "retry"}:
            return await self._handle_approval(
                mission,
                {"approved": True, "approval_source": source, "user_id": user_id or mission.user_id},
                slog,
            )

        return SkillResult(ok=False, message=f"Comando inválido: {command}", mission_id=mission_id)

    # ───────────────────────────── kickoff ─────────────────────────────

    async def _handle_kickoff(
        self,
        session_id: UUID,
        user_message: str,
        extra: dict[str, Any],
        slog: "SessionFileLogger",
        user_id: UUID | None,
    ) -> SkillResult:
        if not user_id:
            return SkillResult(ok=False, message="user_id obrigatório")

        # ── Implicit approval ──────────────────────────────────────
        # If the user sends a confirmation word ("pode enviar", "sim", etc.)
        # and has an active awaiting_confirmation mission, treat it as approval.
        CONFIRM_WORDS = {"pode", "sim", "envia", "aprovado", "ok", "confirma",
                          "confirmação", "aprova", "yes", "enviar", "mandar"}
        msg_lower = user_message.lower().strip()
        if any(w in msg_lower for w in CONFIRM_WORDS):
            active = await self.db.find_active_confirmation_mission(user_id)
            if active:
                logger.info(f"Implicit approval: mission={active.id} found for user={user_id}")
                return await self._handle_approval(active, {"approved": True}, slog)

        tool_args = extra.get("tool_arguments") or {}
        recipient_hint = (tool_args.get("recipient") or "").strip() or None
        message_text = (tool_args.get("text") or "").strip() or None
        # Source override: the router may pass it; else detect it from the message
        # ("procura no WhatsApp", "contato salvo"). Default "auto" = PKS-first stack.
        contact_source = (tool_args.get("contact_source") or "").strip().lower() or None
        if not contact_source:
            contact_source = self._detect_contact_source(user_message)

        if not recipient_hint:
            recipient_hint = self._extract_recipient_hint(user_message)
        if not message_text:
            message_text = self._extract_message_text(user_message)

        mode = self._detect_mode(user_message, recipient_hint, message_text)
        kind = self._classify_kind(user_message, message_text, mode, extra)
        # A coordination mission runs the agent loop with a personal tone; store it
        # as its own mode so the executor still intercepts sends (confirmação em tudo)
        # and the inbound path routes it through the agent loop.
        if kind == "coordinate":
            mode = "coordinate"
        slog.log_event(
            "whatsapp_messaging.kickoff_parse",
            {
                "recipient_hint": recipient_hint,
                "message_text": message_text,
                "detected_mode": mode,
                "detected_kind": kind,
                "contact_source": contact_source,
                "raw_user_message": user_message,
            },
        )

        resolved = await self._resolve_recipient(
            user_id, recipient_hint, user_message, contact_source=contact_source
        )
        slog.log_event(
            "whatsapp_messaging.resolve_recipient",
            {
                "ok": resolved.ok,
                "jid": resolved.jid,
                "name": resolved.name,
                "phone_e164": resolved.phone_e164,
                "error": resolved.error,
                "candidates": resolved.candidates,
                "source": resolved.source,
                "relationship": resolved.relationship,
                "linked_user_id": resolved.linked_user_id,
            },
        )
        if not resolved.ok:
            if resolved.candidates:
                lines = [
                    f"- {c.get('name')} ({c.get('phone_e164') or c.get('jid')})"
                    for c in resolved.candidates[:5]
                ]
                msg = (
                    f"Encontrei vários contatos para “{recipient_hint or user_message}”. "
                    "Diga qual:\n" + "\n".join(lines)
                )
            else:
                msg = (
                    f"Não encontrei contato para “{recipient_hint or user_message}”. "
                    "Diga o nome do contato como aparece no WhatsApp ou um telefone."
                )
            return SkillResult(ok=False, message=msg)

        contact_jid = resolved.jid
        contact_label = resolved.name or recipient_hint or contact_jid
        contact_phone = resolved.phone_e164

        try:
            await self.db.upsert_whatsapp_contact(
                user_id=user_id,
                jid=contact_jid,
                label=contact_label,
                phone_e164=contact_phone or "",
            )
        except Exception as e:
            logger.warning("upsert_whatsapp_contact failed (non-fatal): err=%s", e)

        mission = await self.db.create_mission(
            user_id=user_id,
            session_id=session_id,
            parent_session_id=None,
            title=contact_label[:64],
            goal=user_message,
            contact_jid=contact_jid,
            channel="whatsapp",
        )
        mission_slog = _get_mission_logger(mission.id)
        mission_slog.new_interaction(
            user_message,
            metadata={
                "skill": "whatsapp_messaging",
                "mission_id": str(mission.id),
                "user_id": str(user_id),
            },
        )
        slog = _FanoutLogger(slog, mission_slog)
        await self.db.update_mission_status(
            mission.id,
            status="pending",
            extra_metadata={
                "mode": mode,
                "kind": kind,
                "contact_phone_e164": contact_phone or "",
                "contact_label": contact_label,
                "recipient_source": resolved.source,
                "linked_user_id": resolved.linked_user_id,
                "relationship": resolved.relationship,
            },
        )

        if kind == "coordinate":
            # Goal-tracked personal-domain mission: agent loop drives toward the
            # objective (agendar/negociar) and confirms every send.
            return await self._kickoff_coordination(
                mission_id=mission.id,
                mission_session_id=session_id,
                user_id=user_id,
                user_message=user_message,
                contact_label=contact_label,
                contact_jid=contact_jid,
                contact_phone=contact_phone,
                slog=slog,
            )

        if mode == "personal":
            # kind ∈ {"direct","personal"}: both draft→confirm→send here, but a
            # "direct" auto-closes after one reply cycle (see _handle_direct_reply);
            # a "personal" mission keeps the ongoing ack loop (_handle_inbound).
            return await self._kickoff_personal(
                mission_id=mission.id,
                user_id=user_id,
                contact_label=contact_label,
                contact_jid=contact_jid,
                contact_phone=contact_phone,
                draft_text=message_text or user_message,
                slog=slog,
                kind=kind,
            )

        return await self._kickoff_commercial(
            mission_id=mission.id,
            mission_session_id=session_id,
            user_id=user_id,
            user_message=user_message,
            contact_label=contact_label,
            contact_jid=contact_jid,
            contact_phone=contact_phone,
            slog=slog,
        )

    async def _kickoff_personal(
        self,
        mission_id: UUID,
        user_id: UUID,
        contact_label: str,
        contact_jid: str,
        contact_phone: str | None,
        draft_text: str,
        slog: "SessionFileLogger",
        kind: str = "personal",
    ) -> SkillResult:
        draft_messages = _split_whatsapp_messages(draft_text, mode="personal")
        await self.db.update_mission_status(
            mission_id,
            status="awaiting_confirmation",
            extra_metadata={"draft_text": draft_text, "draft_messages": draft_messages, "kind": kind},
        )
        await self.db.set_mission_next_action(
            mission_id=mission_id,
            action_type="send_whatsapp",
            action_params={
                "jid": contact_jid,
                "text": draft_text,
                "messages": draft_messages,
                "delay_ms": _DEFAULT_MESSAGE_DELAY_MS,
                "idempotency_key": str(mission_id),
            },
        )

        phone_label = contact_phone or _phone_from_jid(contact_jid) or contact_jid
        body = _build_approval_body(contact_label, phone_label, draft_text, draft_messages)
        notification_payload = {
            "mission_id": str(mission_id),
            "draft_text": draft_text,
            "draft_messages": draft_messages,
            "contact_label": contact_label,
            "contact_jid": contact_jid,
            "contact_phone_e164": contact_phone or phone_label,
            "mode": "personal",
            "kind": kind,
        }
        await self._notify(
            user_id=user_id,
            mission_id=mission_id,
            kind="confirm_reply",
            title=f"Enviar mensagem para {contact_label}",
            body=body,
            payload=notification_payload,
            actions=[
                {"action_type": "approve", "label": "Aprovo", "button_style": "contained"},
                {"action_type": "edit", "label": "Eu escrevo", "button_style": "outlined"},
                {"action_type": "cancel", "label": "Não fazer nada", "button_style": "ghost"},
            ],
            config={"persistent": True, "position": "bottom_right", "play_sound": True},
        )

        slog.log_event(
            "whatsapp_messaging.kickoff_personal",
            {
                "mission_id": str(mission_id),
                "contact_label": contact_label,
                "contact_jid": contact_jid,
                "phone": phone_label,
                "draft_text": draft_text,
                "draft_messages": draft_messages,
                "notification": {
                    "kind": "confirm_reply",
                    "title": f"Enviar mensagem para {contact_label}",
                    "body": body,
                    "payload": notification_payload,
                },
            },
        )

        import json as _json
        _mission_marker = _json.dumps({
            "missionId": str(mission_id),
            "contactLabel": contact_label,
            "contactJid": contact_jid,
            "contactPhone": contact_phone or phone_label,
            "draftText": draft_text,
            "draftMessages": draft_messages,
        }, ensure_ascii=False)
        return SkillResult(
            ok=True,
            message=f"[MISSION:{_mission_marker}]Pronto para enviar para {contact_label} ({phone_label}). Aguardando sua aprovação.",
            mission_id=mission_id,
            draft_text=draft_text,
            notification_kind="confirm_reply",
            mission_contact_label=contact_label,
            mission_contact_jid=contact_jid,
            mission_contact_phone=contact_phone or phone_label,
        )

    async def _kickoff_coordination(
        self,
        mission_id: UUID,
        mission_session_id: UUID,
        user_id: UUID,
        user_message: str,
        contact_label: str,
        contact_jid: str,
        contact_phone: str | None,
        slog: "SessionFileLogger",
    ) -> SkillResult:
        """Kick off a goal-tracked coordination mission (agent loop, personal tone)."""
        return await self._kickoff_commercial(
            mission_id=mission_id,
            mission_session_id=mission_session_id,
            user_id=user_id,
            user_message=user_message,
            contact_label=contact_label,
            contact_jid=contact_jid,
            contact_phone=contact_phone,
            slog=slog,
            mode="coordinate",
        )

    async def _kickoff_commercial(
        self,
        mission_id: UUID,
        mission_session_id: UUID,
        user_id: UUID,
        user_message: str,
        contact_label: str,
        contact_jid: str,
        contact_phone: str | None,
        slog: "SessionFileLogger",
        mode: str = "commercial",
    ) -> SkillResult:
        system_prompt = self._build_system_prompt(
            mode=mode,
            contact_label=contact_label,
            contact_jid=contact_jid,
            contact_phone=contact_phone,
            goal=user_message,
        )
        slog.log_event(
            "whatsapp_messaging.commercial_system_prompt",
            {
                "mode": mode,
                "contact_label": contact_label,
                "contact_jid": contact_jid,
                "contact_phone": contact_phone,
                "goal": user_message,
                "system_prompt": system_prompt,
                "tools": [tool.name for tool in WHATSAPP_MESSAGING_TOOLS],
            },
        )
        slog.log_llm_request(
            stage="whatsapp_messaging.commercial_initial_agent_request",
            messages=PromptBuilder(system_prompt=system_prompt).build_initial_messages(
                user_message,
                WHATSAPP_MESSAGING_TOOLS,
            ),
            model=self.agent_executor.llm_router.get_default_model(None),
            provider=self.agent_executor.llm_router.get_provider(None).__class__.__name__,
            temperature=self.agent_executor.settings.default_temperature,
        )

        self.executor.set_context(mission_id=mission_id, user_id=user_id)
        result = await self.agent_executor.execute(
            task=user_message,
            session_id=mission_session_id,
            system_prompt_override=system_prompt,
            tool_subset=WHATSAPP_MESSAGING_TOOLS,
            tool_executor=self.executor,
            available_tools=WHATSAPP_MESSAGING_TOOLS,
        )
        self._log_agent_steps(slog, result.get("steps", []))

        draft_text = result.get("draft_text")
        if draft_text:
            await self.db.update_mission_status(
                mission_id,
                status="awaiting_confirmation",
                extra_metadata={"draft_text": draft_text},
            )
            await self.db.set_mission_next_action(
                mission_id=mission_id,
                action_type="send_whatsapp",
                action_params={
                    "jid": contact_jid,
                    "text": draft_text,
                    "idempotency_key": str(mission_id),
                },
            )
            phone_label = contact_phone or _phone_from_jid(contact_jid) or contact_jid
            await self._notify(
                user_id=user_id,
                mission_id=mission_id,
                kind="confirm_reply",
                title=f"Aprovar mensagem: {contact_label}",
                body=f"Para {contact_label} ({phone_label}):\n\n{draft_text}",
                payload={
                    "mission_id": str(mission_id),
                    "draft_text": draft_text,
                    "contact_label": contact_label,
                    "contact_jid": contact_jid,
                    "contact_phone_e164": contact_phone or phone_label,
                    "mode": mode,
                },
                actions=[
                    {"action_type": "approve", "label": "Aprovo", "button_style": "contained"},
                    {"action_type": "edit", "label": "Eu escrevo", "button_style": "outlined"},
                    {"action_type": "cancel", "label": "Não fazer nada", "button_style": "ghost"},
                ],
                config={"persistent": True, "position": "bottom_right", "play_sound": True},
            )
            return SkillResult(
                ok=True,
                message="Missão iniciada — aprovação pendente.",
                mission_id=mission_id,
                draft_text=draft_text,
                notification_kind="confirm_reply",
            )

        return SkillResult(ok=True, message="Missão criada.", mission_id=mission_id)

    # ─────────────────────────── inbound reply ───────────────────────────

    async def _handle_inbound(
        self,
        mission,
        inbound_text: str,
        slog: "SessionFileLogger",
        extra: dict[str, Any],
        history: list[dict[str, str]] | None = None,
    ) -> SkillResult:
        locked = await self.db.lock_mission_for_update(mission.id)
        if not locked:
            return SkillResult(ok=False, message="Não foi possível travar a missão")

        mission.status = "processing"
        mission.last_activity_at = datetime.utcnow()
        meta = mission.extra_metadata or {}
        mode = meta.get("mode", "commercial")
        contact_label = meta.get("contact_label") or mission.title
        contact_phone = meta.get("contact_phone_e164") or _phone_from_jid(mission.contact_jid)

        await self.db.save_message(
            session_id=mission.session_id,
            role="user",
            content=inbound_text,
            user_id=mission.user_id,
            mission_id=mission.id,
            metadata={
                "message_type": "whatsapp_inbound",
                "contact_label": contact_label,
                "contact_jid": mission.contact_jid,
                "contact_phone_e164": contact_phone,
                "inbound_message_id": extra.get("inbound_message_id") or extra.get("webhook_message_id"),
            },
        )

        slog.log_event(
            "whatsapp_messaging.inbound_message",
            {
                "mission_id": str(mission.id),
                "mode": mode,
                "contact_label": contact_label,
                "contact_jid": mission.contact_jid,
                "contact_phone": contact_phone,
                "inbound_text": inbound_text,
                "extra": extra,
            },
        )

        if mode == "personal":
            reply_text = await self._build_personal_ack_reply(
                mission_goal=mission.goal,
                contact_label=contact_label,
                inbound_text=inbound_text,
                operator_instructions=self._get_operator_instructions(meta),
                prior_sent_messages=[
                    *(meta.get("last_outbound_messages") or []),
                    meta.get("draft_text") or "",
                ],
                slog=slog,
            )
            reply_messages = _split_whatsapp_messages(reply_text, mode="personal")
            await self.db.update_mission_status(
                mission.id,
                status="awaiting_confirmation",
                extra_metadata={
                    "last_inbound_text": inbound_text,
                    "draft_text": reply_text,
                    "draft_messages": reply_messages,
                    "pending_inbound_ack": True,
                },
            )
            await self.db.set_mission_next_action(
                mission_id=mission.id,
                action_type="send_whatsapp",
                action_params={
                    "jid": mission.contact_jid,
                    "text": reply_text,
                    "messages": reply_messages,
                    "delay_ms": _DEFAULT_MESSAGE_DELAY_MS,
                    "idempotency_key": str(mission.id),
                },
            )

            phone_label = contact_phone or _phone_from_jid(mission.contact_jid) or mission.contact_jid
            notification_payload = {
                "mission_id": str(mission.id),
                "contact_label": contact_label,
                "contact_jid": mission.contact_jid,
                "contact_phone_e164": contact_phone or phone_label,
                "inbound_text": inbound_text,
                "draft_text": reply_text,
                "draft_messages": reply_messages,
                "mode": "personal",
                "pending_inbound_ack": True,
            }
            approval_body = _build_approval_body(
                contact_label,
                phone_label,
                reply_text,
                reply_messages,
                prefix=f"Recebido: {inbound_text}",
            )
            await self._notify(
                user_id=mission.user_id,
                mission_id=mission.id,
                kind="confirm_reply",
                title=f"Aprovar resposta para {contact_label}",
                body=approval_body,
                payload=notification_payload,
                actions=[
                    {"action_type": "approve", "label": "Aprovo", "button_style": "contained"},
                    {"action_type": "edit", "label": "Eu escrevo", "button_style": "outlined"},
                    {"action_type": "cancel", "label": "Não fazer nada", "button_style": "ghost"},
                ],
                config={"persistent": True, "position": "bottom_right", "play_sound": True},
            )
            slog.log_event(
                "whatsapp_messaging.inbound_personal_confirmation",
                {
                    "mission_id": str(mission.id),
                    "received_text": inbound_text,
                    "draft_reply": reply_text,
                    "draft_messages": reply_messages,
                    "notification": {
                        "kind": "confirm_reply",
                        "title": f"Aprovar resposta para {contact_label}",
                        "body": approval_body,
                        "payload": notification_payload,
                    },
                },
            )

            return SkillResult(
                ok=True,
                message=f"{contact_label} respondeu. Rascunho pronto, aguardando sua aprovação para enviar.",
                mission_id=mission.id,
            )

        memory = await self._build_conversation_memory(
            session_id=mission.session_id,
            current_user_message=inbound_text,
            history=history,
            mission_id=mission.id,
        )
        system_prompt = self._build_system_prompt(
            mode=mode,
            contact_label=contact_label or mission.title,
            contact_jid=mission.contact_jid,
            contact_phone=contact_phone,
            goal=mission.goal,
            goal_status=self._build_goal_status(memory),
            extra_instructions=self._build_extra_instructions_block(meta),
        )
        slog.log_event(
            "whatsapp_messaging.inbound_commercial_system_prompt",
            {
                "mode": mode,
                "contact_label": contact_label or mission.title,
                "contact_jid": mission.contact_jid,
                "contact_phone": contact_phone,
                "goal": mission.goal,
                "goal_already_communicated": self._has_prior_assistant_message(
                    memory.get_messages()
                ),
                "history_message_count": memory.message_count(),
                "inbound_text": inbound_text,
                "system_prompt": system_prompt,
                "tools": [tool.name for tool in WHATSAPP_MESSAGING_TOOLS],
            },
        )
        slog.log_llm_request(
            stage="whatsapp_messaging.inbound_commercial_initial_agent_request",
            messages=self._build_logged_initial_messages(
                system_prompt,
                inbound_text,
                memory,
            ),
            model=self.agent_executor.llm_router.get_default_model(None),
            provider=self.agent_executor.llm_router.get_provider(None).__class__.__name__,
            temperature=self.agent_executor.settings.default_temperature,
        )

        self.executor.set_context(mission_id=mission.id, user_id=mission.user_id)
        result = await self.agent_executor.execute(
            task=inbound_text,
            session_id=mission.session_id,
            system_prompt_override=system_prompt,
            tool_subset=WHATSAPP_MESSAGING_TOOLS,
            tool_executor=self.executor,
            available_tools=WHATSAPP_MESSAGING_TOOLS,
            memory=memory,
        )
        self._log_agent_steps(slog, result.get("steps", []))

        await self.db.update_mission_status(mission.id, status="awaiting_reply")
        return SkillResult(ok=True, message="Resposta processada.", mission_id=mission.id)

    # ───────────────────────── direct reply (light) ─────────────────────────

    async def _handle_direct_reply(
        self,
        mission,
        inbound_text: str,
        slog: "SessionFileLogger",
        extra: dict[str, Any],
        history: list[dict[str, str]] | None = None,
    ) -> SkillResult:
        """Handle a reply on a *direct* (light) mission.

        Classifies the contact's reply and, instead of always drafting an ack:
          - ``close``    → complete the mission and just summarise it to the user
                           (nothing is sent, so no confirmation is needed).
          - ``ack``      → draft a short reply and ask the user to approve it
                           (reuses the tracked-mission confirm flow — "confirmação
                           em tudo").
          - ``escalate`` → same confirm flow, but flagged as needing the user (never
                           auto-answered — the guard-rail lives in the classifier).
          - ``promote``  → the thread became a task: upgrade to a tracked mission
                           (``kind=personal``) and hand it to the ongoing ack loop.
        """
        meta = mission.extra_metadata or {}
        contact_label = meta.get("contact_label") or mission.title

        decision = await self._classify_reply(inbound_text, mission, slog)
        slog.log_event(
            "whatsapp_messaging.direct_reply_classified",
            {
                "mission_id": str(mission.id),
                "contact_label": contact_label,
                "inbound_text": inbound_text,
                "decision": decision,
            },
        )

        if decision == "close":
            # Nothing to send — record the inbound, close the thread, tell the user.
            contact_phone = meta.get("contact_phone_e164") or _phone_from_jid(mission.contact_jid)
            await self.db.save_message(
                session_id=mission.session_id,
                role="user",
                content=inbound_text,
                user_id=mission.user_id,
                mission_id=mission.id,
                metadata={
                    "message_type": "whatsapp_inbound",
                    "contact_label": contact_label,
                    "contact_jid": mission.contact_jid,
                    "contact_phone_e164": contact_phone,
                    "inbound_message_id": extra.get("inbound_message_id") or extra.get("webhook_message_id"),
                },
            )
            await self.db.update_mission_status(
                mission.id,
                status="completed",
                extra_metadata={"last_inbound_text": inbound_text, "closed_reason": "direct_reply_close"},
            )
            await self._notify(
                user_id=mission.user_id,
                mission_id=mission.id,
                kind="mission_completed",
                title=f"Conversa com {contact_label} encerrada",
                body=f"Falei com {contact_label}, tudo certo.\n\n{contact_label}: {inbound_text}",
                payload={"mission_id": str(mission.id), "contact_label": contact_label},
                config={"position": "bottom_right"},
            )
            return SkillResult(
                ok=True,
                message=f"Conversa com {contact_label} encerrada.",
                mission_id=mission.id,
            )

        if decision == "promote":
            # Became a task → keep it as a tracked mission from here on.
            await self.db.update_mission_status(
                mission.id,
                status="processing",
                extra_metadata={"kind": "personal", "promoted_from": "direct"},
            )
            slog.log_event(
                "whatsapp_messaging.direct_reply_promoted",
                {"mission_id": str(mission.id), "inbound_text": inbound_text},
            )

        # ack / escalate / promote → draft a reply and ask the user to approve it.
        # (The classifier already refuses to auto-answer decisions/questions.)
        return await self._handle_inbound(mission, inbound_text, slog, extra, history)

    async def _classify_reply(
        self,
        inbound_text: str,
        mission,
        slog: "SessionFileLogger | None" = None,
    ) -> str:
        """Classify a contact's reply → "close" | "ack" | "escalate" | "promote".

        Fail-safe: autopilot off, empty input, or any uncertainty → "escalate"
        (never auto-handle something that might need the user).
        """
        if not getattr(self.agent_executor.settings, "whatsapp_reply_autopilot_enabled", True):
            return "escalate"
        text = (inbound_text or "").strip()
        if not text:
            return "escalate"

        # Deterministic: a pure social closer needs no reply and no user.
        folded = _fold_accents(text.lower()).strip(" !.")
        _CLOSERS = {
            "ok", "okay", "blz", "beleza", "ta bom", "tabom", "ta", "ta bem",
            "valeu", "vlw", "obrigado", "obrigada", "brigado", "combinado",
            "de nada", "isso", "certo", "perfeito", "show", "otimo", "otima",
            "fechou", "ta certo", "ta otimo", "🙏", "👍",
        }
        if folded in _CLOSERS:
            return "close"

        try:
            provider = self.agent_executor.llm_router.get_provider(None)
            model = self.agent_executor.llm_router.get_default_model(None)
            messages = [
                Message(
                    role=MessageRole.SYSTEM,
                    content=(
                        "Você classifica a resposta que um CONTATO enviou no WhatsApp a uma "
                        "mensagem que o Neo mandou em nome do Daniel. Responda com UMA palavra:\n"
                        "- close: fecho social sem conteúdo novo ('ok', 'beleza', 'valeu').\n"
                        "- ack: o contato confirmou/aceitou e trouxe uma info simples que só "
                        "precisa de um 'ok' educado ('ok, deixei comida na mesa').\n"
                        "- escalate: o contato fez uma PERGUNTA, pediu uma DECISÃO, ou algo que "
                        "compromete o Daniel / que só ele pode responder.\n"
                        "- promote: a conversa virou uma TAREFA que exige combinar/agendar/"
                        "negociar em vários passos.\n"
                        "Na dúvida, responda escalate. Nunca invente. Responda só a palavra."
                    ),
                ),
                Message(
                    role=MessageRole.USER,
                    content=(
                        f"Pedido original do Daniel: {mission.goal}\n"
                        f"Contato: {mission.extra_metadata.get('contact_label') if mission.extra_metadata else mission.title}\n"
                        f"Resposta recebida do contato: {text}\n\n"
                        "Classifique em uma palavra: close, ack, escalate ou promote."
                    ),
                ),
            ]
            response = await provider.complete(
                messages=messages, tools=[], model=model, temperature=0.0, max_tokens=6
            )
            raw = _fold_accents((response.get_content() or "").strip().lower())
            for label in ("promote", "escalate", "ack", "close"):
                if label in raw:
                    return label
            return "escalate"
        except Exception as e:
            logger.warning("direct reply classification failed err=%s", e)
            if slog:
                slog.log_error_recovery(
                    1, "DirectReplyClassificationFailed", "escalate", {"error": str(e)}
                )
            return "escalate"

    async def _build_personal_ack_reply(
        self,
        mission_goal: str,
        contact_label: str,
        inbound_text: str,
        operator_instructions: list[str] | None = None,
        prior_sent_messages: list[str] | None = None,
        rewrite_request: str | None = None,
        previous_draft: str | None = None,
        slog: SessionFileLogger | None = None,
    ) -> str:
        instruction_reply = None
        if not rewrite_request:
            instruction_reply = _reply_from_matching_operator_instruction(
                operator_instructions or [],
                inbound_text,
            )
        if instruction_reply:
            if slog:
                slog.log_event(
                    "whatsapp_messaging.personal_ack_operator_instruction_match",
                    {
                        "inbound_text": inbound_text,
                        "reply_text": instruction_reply,
                        "operator_instructions": operator_instructions or [],
                    },
                )
            return instruction_reply

        fallback = _fallback_personal_ack(inbound_text)
        try:
            llm_provider = self.agent_executor.llm_router.get_provider(None)
            model = self.agent_executor.llm_router.get_default_model(None)
            instructions_block = "\n".join(
                f"- {item}" for item in (operator_instructions or [])[-5:] if item.strip()
            ) or "Nenhuma."
            messages = [
                Message(
                    role=MessageRole.SYSTEM,
                    content=(
                        "Você escreve respostas curtas de WhatsApp em português do Brasil. "
                        "Responda como Daniel responderia: natural, direto e sem formalidade excessiva. "
                        "Retorne somente o texto da mensagem, sem aspas, sem explicação, "
                        "sem markdown e com no máximo 180 caracteres. "
                        "Assuma que o contato já conhece o contexto anterior da conversa. "
                        "Reaja principalmente à última mensagem recebida do contato. "
                        "Quando a mensagem recebida for lembrete ou confirmação simples, responda só com confirmação curta. "
                        "Se houver instrução adicional do Daniel aplicável à mensagem recebida, siga essa instrução acima do pedido original. "
                        "Se Daniel pediu uma nova versão do rascunho, mantenha o sentido das instruções aplicáveis, mas ajuste o estilo pedido. "
                        "Não repita nem reexplique o pedido original do usuário, salvo se o contato pedir esclarecimento. "
                        "Evite respostas completas demais; prefira continuação natural da conversa. "
                        "Não adicione risadas, emojis ou gírias fortes sem sinal explícito no contexto. "
                        "Não invente horário, motivo, preço, endereço nem compromisso que não esteja no contexto. "
                        "Se a resposta exigir uma informação ausente, faça uma pergunta curta."
                    ),
                ),
                Message(
                    role=MessageRole.USER,
                    content=(
                        f"Pedido original do usuário: {mission_goal}\n"
                        f"Contato: {contact_label}\n"
                        f"Instruções adicionais do Daniel:\n{instructions_block}\n"
                        f"Mensagem recebida do contato: {inbound_text}\n\n"
                        f"Pedido atual do Daniel sobre o rascunho: {rewrite_request or 'Gerar resposta curta.'}\n"
                        f"Rascunho anterior: {previous_draft or '(nenhum)'}\n\n"
                        "Escreva uma resposta curta reagindo à mensagem recebida. "
                        "Se uma instrução adicional combinar com a mensagem recebida, responda conforme essa instrução. "
                        "Se houver rascunho anterior, gere uma alternativa diferente e respeite o pedido atual de estilo. "
                        "Use o pedido original apenas para entender o contexto, não para repetir informações já comunicadas. "
                        "Exemplo ruim: 'Vou esquentar sim, mas vou chegar mais tarde hoje'. "
                        "Exemplo bom: 'Pode deixar, vou esquentar sim'."
                    ),
                ),
            ]
            if slog:
                slog.log_llm_request(
                    stage="whatsapp_messaging.personal_ack_reply",
                    messages=messages,
                    model=model,
                    provider=llm_provider.__class__.__name__,
                    temperature=0.4,
                )
            start = perf_counter()
            response = await llm_provider.complete(
                messages=messages,
                tools=[],
                model=model,
                temperature=0.4,
                max_tokens=80,
            )
            text = _sanitize_personal_ack(response.get_content() or "")
            text = _strip_redundant_personal_context(
                text,
                forbidden_phrases=[mission_goal, previous_draft or "", *(prior_sent_messages or [])],
            )
            if slog:
                slog.log_llm_response(
                    stage="whatsapp_messaging.personal_ack_reply",
                    response=response.get_content() or "",
                    duration_ms=(perf_counter() - start) * 1000,
                    model=model,
                )
            return text or fallback
        except Exception as e:
            logger.warning("Personal ack generation failed err=%s", e)
            if slog:
                slog.log_error_recovery(
                    1,
                    "PersonalAckGenerationFailed",
                    "fallback_personal_ack",
                    {"error": str(e), "fallback": fallback},
                )
            return fallback

    # ─────────────────────────── approval ───────────────────────────────

    async def _handle_approval(
        self,
        mission,
        extra: dict[str, Any],
        slog: "SessionFileLogger",
    ) -> SkillResult:
        approved = extra.get("approved", False)
        edited_text = extra.get("edited_text")
        slog.log_event(
            "Mission approval received",
            {
                "mission_id": str(mission.id),
                "approved": approved,
                "edited_text": edited_text,
                "source_extra": extra,
            },
        )

        if not approved:
            await self.db.update_mission_status(mission.id, status="cancelled")
            await self.db.clear_mission_next_action(mission.id)
            slog.log_event("Mission cancelled by approval flow", {"mission_id": str(mission.id)})
            return SkillResult(ok=False, message="Missão cancelada.", mission_id=mission.id)

        action_type = mission.next_action_type
        action_params = mission.next_action_params or {}
        if not action_type:
            action_type = "send_whatsapp"
            action_params = {
                "jid": mission.contact_jid,
                "text": (mission.extra_metadata or {}).get("draft_text", ""),
                "idempotency_key": str(mission.id),
            }
        if action_type != "send_whatsapp":
            return SkillResult(
                ok=False,
                message=f"Ação pendente não suportada: {action_type}",
                mission_id=mission.id,
            )

        target_jid = action_params.get("jid") or mission.contact_jid
        action_text = action_params.get("text") or (mission.extra_metadata or {}).get("draft_text", "")
        text_to_send = edited_text or action_text

        # ── Dev mode phone block ─────────────────────────────────────────
        import os
        allowed_phone = os.environ.get("ALLOWED_WHATSAPP_PHONE", "5511912345678")
        jid_digits = "".join(filter(str.isdigit, target_jid or ""))
        if jid_digits and jid_digits != allowed_phone:
            slog.log_event(
                "Dev block: rejected send to non-allowed number",
                {"target_jid": target_jid, "digits": jid_digits, "allowed": allowed_phone},
            )
            return SkillResult(
                ok=False,
                message=f"Envio bloqueado: número {target_jid} não autorizado (permitido: {allowed_phone})",
                mission_id=mission.id,
            )

        message_parts = _normalize_message_parts(
            _split_whatsapp_messages(text_to_send, mode=(mission.extra_metadata or {}).get("mode", "personal"))
            if edited_text
            else action_params.get("messages"),
            fallback_text=text_to_send,
        )
        delay_ms = _normalize_delay_ms(action_params.get("delay_ms"))
        logger.info(
            "_handle_approval: mission=%s approved=%s parts=%s text=%s",
            mission.id,
            approved,
            len(message_parts),
            text_to_send[:50] if text_to_send else "EMPTY",
        )
        if not text_to_send:
            slog.log_error_recovery(
                1,
                "EmptyDraftText",
                "abort_send",
                {"mission_id": str(mission.id), "extra_metadata": mission.extra_metadata or {}},
            )
            return SkillResult(
                ok=False, message="Nenhum texto para enviar.", mission_id=mission.id
            )

        gateway_payload = {
            "method": "POST",
            "url": f"{self.gateway.base_url}/send",
            "json": {
                "jid": target_jid,
                "text": text_to_send,
                "messages": message_parts,
                "delay_ms": delay_ms,
                "idempotency_key": action_params.get("idempotency_key") or str(mission.id),
            },
        }
        slog.log_tool_request(
            tool_name="whatsapp_send_message",
            arguments=gateway_payload,
            execution="server-side",
        )
        start = perf_counter()
        sent_messages: list[dict[str, Any]] = []
        try:
            base_idempotency_key = action_params.get("idempotency_key") or str(mission.id)
            for index, message_part in enumerate(message_parts):
                message_id = await self.gateway.send_message(
                    jid=target_jid,
                    text=message_part,
                    idempotency_key=f"{base_idempotency_key}:{index}",
                )
                sent_messages.append(
                    {
                        "index": index,
                        "message_id": message_id,
                        "text": message_part,
                    }
                )
                if index < len(message_parts) - 1:
                    await asyncio.sleep(delay_ms / 1000)
        except Exception as e:
            logger.error("Approval send failed: mission=%s err=%s", mission.id, e)
            slog.log_tool_result(
                tool_name="whatsapp_send_message",
                success=False,
                error=str(e),
                result={"sent_messages": sent_messages},
                duration_ms=(perf_counter() - start) * 1000,
                execution="server-side",
            )
            return SkillResult(
                ok=False, message=f"Falha ao enviar: {e}", mission_id=mission.id
            )
        slog.log_tool_result(
            tool_name="whatsapp_send_message",
            success=True,
            result={
                "message_ids": [m["message_id"] for m in sent_messages],
                "messages": sent_messages,
                "to": target_jid,
                "delay_ms": delay_ms,
            },
            duration_ms=(perf_counter() - start) * 1000,
            execution="server-side",
        )

        for item in sent_messages:
            await self.db.save_message(
                session_id=mission.session_id,
                role="assistant",
                content=item["text"],
                tool_name="whatsapp_send_message",
                tool_call_id=item["message_id"],
                user_id=mission.user_id,
                mission_id=mission.id,
                metadata={
                    "batch_id": str(mission.id),
                    "batch_index": item["index"],
                    "batch_size": len(sent_messages),
                    "send_mode": "batch" if len(sent_messages) > 1 else "single",
                },
            )
        last_message_id = sent_messages[-1]["message_id"]
        slog.log_event(
            "Outbound messages saved",
            {
                "mission_id": str(mission.id),
                "session_id": str(mission.session_id),
                "role": "assistant",
                "tool_name": "whatsapp_send_message",
                "message_count": len(sent_messages),
                "message_ids": [m["message_id"] for m in sent_messages],
                "delay_ms": delay_ms,
                "content": text_to_send,
                "messages": [m["text"] for m in sent_messages],
            },
        )

        await self.db.update_mission_status(
            mission.id,
            status="awaiting_reply",
            extra_metadata={
                "last_outbound_id": last_message_id,
                "last_outbound_ids": [m["message_id"] for m in sent_messages],
                "last_outbound_messages": [m["text"] for m in sent_messages],
            },
        )
        await self.db.clear_mission_next_action(mission.id)
        slog.log_event(
            "Mission status updated",
            {
                "mission_id": str(mission.id),
                "status": "awaiting_reply",
                "last_outbound_id": last_message_id,
                "message_count": len(sent_messages),
            },
        )

        meta = mission.extra_metadata or {}
        if meta.get("pending_inbound_ack"):
            inbound_text = meta.get("last_inbound_text", "")
            contact_label = (meta.get("contact_label") or mission.title or mission.contact_jid)
            summary = f"{contact_label} respondeu: {inbound_text[:180]} | Neo respondeu: {text_to_send[:180]}"
            await self._notify(
                user_id=mission.user_id,
                mission_id=mission.id,
                kind="mission_completed",
                title=f"Resposta enviada para {contact_label}",
                body=_build_sent_body(inbound_text, message_parts),
                payload={
                    "mission_id": str(mission.id),
                    "contact_label": contact_label,
                    "contact_jid": mission.contact_jid,
                    "contact_phone_e164": meta.get("contact_phone_e164") or _phone_from_jid(mission.contact_jid),
                    "inbound_text": inbound_text,
                    "reply_text": text_to_send,
                    "reply_messages": message_parts,
                    "message_id": last_message_id,
                    "message_ids": [m["message_id"] for m in sent_messages],
                },
            )
            await self.db.update_mission_status(
                mission.id,
                status="completed",
                result_summary=summary,
                extra_metadata={
                    "last_outbound_id": last_message_id,
                    "last_outbound_ids": [m["message_id"] for m in sent_messages],
                    "last_inbound_text": inbound_text,
                    "auto_reply_text": text_to_send,
                    "auto_reply_messages": message_parts,
                    "pending_inbound_ack": False,
                },
            )
            slog.log_event(
                "Mission completed",
                {
                    "mission_id": str(mission.id),
                    "status": "completed",
                    "result_summary": summary,
                    "last_inbound_text": inbound_text,
                    "auto_reply_text": text_to_send,
                    "auto_reply_messages": message_parts,
                    "last_outbound_id": last_message_id,
                    "pending_inbound_ack": False,
                },
            )

        import json as _json
        _mission_marker = _json.dumps({
            "missionId": str(mission.id),
            "contactLabel": mission.title,
            "contactJid": mission.contact_jid,
            "contactPhone": (mission.extra_metadata or {}).get("contact_phone_e164") or _phone_from_jid(mission.contact_jid) or mission.contact_jid,
            "draftText": text_to_send,
            "draftMessages": message_parts,
        }, ensure_ascii=False)
        return SkillResult(
            ok=True,
            message=f"[MISSION:{_mission_marker}]Mensagem enviada para {mission.title}.",
            mission_id=mission.id,
            extra={
                "display_message": f"Acabei de enviar a sua mensagem para {mission.title}.",
                "sent_text": text_to_send,
                "sent_messages": message_parts,
                "is_auto_reply": bool(meta.get("pending_inbound_ack")),
            },
        )

    # ───────────────────── recipient resolution ─────────────────────────

    async def _resolve_recipient(
        self,
        user_id: UUID,
        recipient_hint: str | None,
        full_message: str,
        contact_source: str = "auto",
    ) -> "_ResolvedRecipient":
        """Resolve a recipient into a sendable contact.

        Priority (``contact_source="auto"``):
          1. explicit JID in the message      (exact target, source-agnostic)
          2. explicit phone in the hint        (exact target, source-agnostic)
          3. PKS person entity                 (relationship + jid + linked_user_id)
          4. saved contact table (agenda)       (cache)
          5. WhatsApp phonebook (gateway)       (fuzzy, last resort)

        An explicit ``contact_source`` overrides the default order: ``"whatsapp"``
        goes straight to the gateway, ``"saved"`` straight to the agenda — the user
        pinned the source, so we honour it (mirrors "explicit target wins").
        """
        # 1. Explicit JID anywhere → exact target, regardless of source.
        for source in (recipient_hint or "", full_message or ""):
            m = _JID_RE.search(source)
            if m:
                jid = m.group(1)
                # Look up name via DB (cheap, exact) — gateway fuzzy would
                # treat the JID as text tokens and rank random matches.
                local = await self.db.get_whatsapp_contact_by_jid(jid)
                if local:
                    return _ResolvedRecipient(
                        ok=True,
                        jid=local.jid,
                        name=local.label,
                        phone_e164=local.phone_e164 or _phone_from_jid(jid),
                        source="explicit",
                    )
                return _ResolvedRecipient(
                    ok=True,
                    jid=jid,
                    name=None,
                    phone_e164=_phone_from_jid(jid),
                    source="explicit",
                )

        # 2. Explicit phone in the hint → exact target, regardless of source.
        if recipient_hint:
            phone_match = _PHONE_RE.search(recipient_hint)
            if phone_match and len(re.sub(r"\D", "", phone_match.group(0))) >= 10:
                digits = re.sub(r"\D", "", phone_match.group(0))
                jid = f"{digits}@c.us"
                try:
                    confirmed_jid, exists = await self.gateway.lookup_contact(digits)
                    if exists and confirmed_jid:
                        jid = confirmed_jid
                except Exception:
                    pass
                return _ResolvedRecipient(
                    ok=True,
                    jid=jid,
                    name=None,
                    phone_e164=f"+{digits}",
                    source="explicit",
                )

        label = _strip_stopwords(recipient_hint or "")
        if not label:
            return _ResolvedRecipient(ok=False, error="Sem destinatário.")

        source_hint = (contact_source or "auto").lower()

        # 3. PKS-first (default only). Pinned sources ("whatsapp"/"saved") skip it.
        if (
            source_hint == "auto"
            and self.pks is not None
            and getattr(self.agent_executor.settings, "whatsapp_pks_resolution_enabled", True)
        ):
            try:
                person = await self.pks.resolve_person_contact(user_id=user_id, query=label)
            except Exception as e:
                logger.warning("PKS person resolution failed (non-fatal): err=%s", e)
                person = None
            if person and (person.get("jid") or person.get("phone_e164")):
                jid = person.get("jid")
                phone = person.get("phone_e164")
                if not jid and phone:
                    digits = re.sub(r"\D", "", phone)
                    jid = f"{digits}@c.us"
                return _ResolvedRecipient(
                    ok=True,
                    jid=jid,
                    name=person.get("name") or label,
                    phone_e164=phone or _phone_from_jid(jid),
                    linked_user_id=person.get("linked_user_id"),
                    relationship=person.get("relationship"),
                    pks_entity_id=person.get("entity_id"),
                    source="pks",
                )
            # Person known but without a number → fall through to agenda/gateway to
            # find one; the caller can then link it back onto this PKS entity.

        # 4. Saved contact table (agenda). Primary when source pinned to "saved".
        if source_hint in ("auto", "saved"):
            local = await self.db.search_whatsapp_contact(user_id, label)
            if local:
                return _ResolvedRecipient(
                    ok=True,
                    jid=local.jid,
                    name=local.label,
                    phone_e164=local.phone_e164,
                    source="saved",
                )

        # 5. WhatsApp phonebook (gateway) — last resort / explicit "whatsapp" source.
        return await self._resolve_via_gateway(label)

    async def _resolve_via_gateway(self, label: str) -> "_ResolvedRecipient":
        """Fuzzy-resolve ``label`` against the WhatsApp phonebook (the fuzziest source)."""
        results = await self.gateway.list_contacts(query=label, only_saved=False, limit=10)
        if not results:
            return _ResolvedRecipient(
                ok=False, error=f"Nenhum contato '{label}' encontrado."
            )

        # Prefer .c.us JID variant when same contact appears as both @lid and @c.us
        # (@lid entries often have the raw digits as phone without country code)
        top = results[0]
        if len(results) > 1 and top.get("jid", "").endswith("@lid"):
            c_us = next((r for r in results[1:] if r.get("jid", "").endswith("@c.us") and r.get("name") == top.get("name")), None)
            if c_us:
                top = c_us

        if len(results) == 1 or _strong_match(top, label):
            return _ResolvedRecipient(
                ok=True,
                jid=top.get("jid"),
                name=top.get("name"),
                phone_e164=top.get("phone_e164"),
                source="gateway",
            )
        return _ResolvedRecipient(
            ok=False,
            error="Vários contatos encontrados",
            candidates=results,
        )

    # ───────────────────────── parsing helpers ─────────────────────────

    def _extract_recipient_hint(self, text: str) -> str | None:
        m = _JID_RE.search(text)
        if m:
            return m.group(1)
        markers = [" para ", " pra ", " pro ", " pra a ", " pra o "]
        lower = text.lower()
        for marker in markers:
            idx = lower.find(marker)
            if idx >= 0:
                tail = text[idx + len(marker):].strip(" ,.!?:\"'")
                cut = re.split(r"[:\"“]| que | dizendo ", tail, maxsplit=1)
                hint = cut[0].strip(" ,.!?:\"'") if cut else tail
                if hint:
                    return hint
        return None

    def _extract_message_text(self, text: str) -> str | None:
        m = re.search(r'["“\']([^"”\']{2,})["”\']', text)
        if m:
            return m.group(1).strip()
        m = re.search(r"\b(?:que|dizendo)\s+(.+)$", text, re.IGNORECASE)
        if m:
            return m.group(1).strip(" ,.!?")
        return None

    def _detect_mode(
        self,
        user_message: str,
        recipient_hint: str | None,
        message_text: str | None,
    ) -> str:
        haystack = " ".join(
            filter(None, [user_message, recipient_hint, message_text])
        ).lower()
        haystack = _fold_accents(haystack)
        if any(token in haystack for token in _COMMERCIAL_HINTS):
            return "commercial"
        return "personal"

    @staticmethod
    def _detect_contact_source(user_message: str) -> str:
        """Deterministic fallback for the resolution-source override.

        An explicit instruction about WHERE to look wins over the default PKS-first
        stack: "procura na lista do WhatsApp" → gateway; "contato salvo" → agenda.
        Returns "whatsapp" | "saved" | "auto".
        """
        haystack = _fold_accents((user_message or "").lower())
        whatsapp_markers = (
            "lista de contato do whatsapp", "lista de contatos do whatsapp",
            "lista do whatsapp", "contatos do whatsapp", "nos contatos do whatsapp",
            "na agenda do whatsapp", "no whatsapp", "pelo whatsapp",
        )
        saved_markers = (
            "contato salvo", "contatos salvos", "salvo como", "na minha agenda",
            "no meu contato", "nos meus contatos", "que eu salvei",
        )
        if any(m in haystack for m in whatsapp_markers):
            return "whatsapp"
        if any(m in haystack for m in saved_markers):
            return "saved"
        return "auto"

    def _classify_kind(
        self,
        user_message: str,
        message_text: str | None,
        mode: str,
        extra: dict[str, Any],
    ) -> str:
        """Decide the execution kind: "direct" | "personal" | "commercial".

        - ``commercial`` (from ``_detect_mode``) → agent-loop order pipeline.
        - a coordination request (agendar, "quem tem", negotiate) → ``personal``
          (a tracked mission with an ongoing reply loop).
        - otherwise a one-shot personal message → ``direct`` (light: draft → confirm
          → send → single reply cycle → auto-close), when the feature is enabled.

        The cognitive core may override via ``extra["whatsapp_kind"]``.
        """
        if mode == "commercial":
            return "commercial"

        settings = self.agent_executor.settings
        coordination_on = getattr(settings, "whatsapp_coordination_enabled", True)

        hint = (extra.get("whatsapp_kind") or "").strip().lower()
        if hint in ("direct", "personal", "commercial"):
            return hint
        if hint == "coordinate":
            return "coordinate" if coordination_on else "personal"

        haystack = _fold_accents(" ".join(filter(None, [user_message, message_text])).lower())
        if any(sig in haystack for sig in _COORDINATION_HINTS):
            # A goal-tracked coordination mission, unless the feature is disabled
            # (then fall back to the reactive personal ack loop).
            return "coordinate" if coordination_on else "personal"

        if getattr(settings, "whatsapp_direct_enabled", True):
            return "direct"
        return "personal"

    def _build_system_prompt(
        self,
        mode: str,
        contact_label: str,
        contact_jid: str,
        contact_phone: str | None,
        goal: str,
        goal_status: str | None = None,
        extra_instructions: str = "",
    ) -> str:
        try:
            template = (Path(__file__).parent / "prompt.md").read_text()
        except Exception:
            template = "Você é o Neo. OBJETIVO: {goal}"

        return template.format(
            mode=mode,
            contact_label=contact_label,
            contact_jid=contact_jid,
            contact_phone=contact_phone or "(telefone não disponível)",
            goal=goal,
            goal_status=goal_status or "Ainda não há histórico carregado sobre o andamento do objetivo.",
            extra_instructions=extra_instructions or "Nenhuma instrução adicional.",
            user_address=DEFAULT_USER_ADDRESS,
            user_phone=DEFAULT_USER_PHONE,
            user_payment_prefs=DEFAULT_PAYMENT_PREFS,
        )

    @staticmethod
    def _is_operator_instruction(extra: dict[str, Any]) -> bool:
        if extra.get("approved") is not None:
            return False
        # Webhook inbounds sempre trazem identificadores da mensagem recebida.
        if any(extra.get(k) for k in ("inbound_message_id", "webhook_message_id", "inbound_jid")):
            return False
        return True

    @staticmethod
    def _looks_like_operator_followup_instruction(text: str) -> bool:
        normalized = _fold_accents((text or "").strip().lower())
        if not normalized:
            return False
        if any(normalized.startswith(prefix) for prefix in _FOLLOWUP_INSTRUCTION_PREFIXES):
            return True
        padded = f" {normalized} "
        return any(marker in padded for marker in _FOLLOWUP_INSTRUCTION_MARKERS) and any(
            verb in padded for verb in (" responder", " perguntar", " falar", " disser")
        )

    @staticmethod
    def _looks_like_active_mission_followup(text: str) -> bool:
        normalized = _fold_accents((text or "").strip().lower())
        if not normalized:
            return False
        return (
            WhatsAppMessagingPipeline._looks_like_operator_followup_instruction(text)
            or WhatsAppMessagingPipeline._looks_like_mission_cancel_request(text)
            or WhatsAppMessagingPipeline._looks_like_draft_edit_request(text)
            or any(marker in normalized for marker in _ACTIVE_MISSION_FOLLOWUP_MARKERS)
        )

    @staticmethod
    def _looks_like_reply_regeneration_request(text: str) -> bool:
        normalized = _fold_accents((text or "").strip().lower())
        return any(marker in normalized for marker in _ACTIVE_MISSION_FOLLOWUP_MARKERS)

    @staticmethod
    def _looks_like_mission_cancel_request(text: str) -> bool:
        normalized = _fold_accents((text or "").strip().lower())
        normalized = " ".join(normalized.split())
        if not normalized:
            return False
        return any(marker in normalized for marker in _MISSION_CANCEL_MARKERS)

    @staticmethod
    def _looks_like_draft_edit_request(text: str) -> bool:
        normalized = _fold_accents((text or "").strip().lower())
        normalized = " ".join(normalized.split())
        if not normalized:
            return False
        return any(marker in normalized for marker in _DRAFT_EDIT_MARKERS)

    async def _handle_active_mission_user_message(
        self,
        mission,
        user_message: str,
        slog: "SessionFileLogger",
    ) -> SkillResult:
        meta = mission.extra_metadata or {}
        if self._looks_like_mission_cancel_request(user_message):
            return await self._cancel_active_mission(mission, user_message, slog)

        if (
            self._looks_like_draft_edit_request(user_message)
            and meta.get("draft_text")
        ):
            return await self._edit_personal_pending_reply(mission, user_message, slog)
        if self._looks_like_draft_edit_request(user_message):
            return SkillResult(
                ok=False,
                message="Não encontrei um rascunho pendente nessa missão para alterar.",
                mission_id=mission.id,
            )

        if (
            self._looks_like_reply_regeneration_request(user_message)
            and meta.get("pending_inbound_ack")
            and meta.get("last_inbound_text")
        ):
            return await self._regenerate_personal_inbound_reply(mission, user_message, slog)
        if self._looks_like_reply_regeneration_request(user_message):
            return SkillResult(
                ok=False,
                message="Não encontrei um rascunho pendente nessa missão para gerar outra resposta.",
                mission_id=mission.id,
            )

        if self._looks_like_operator_followup_instruction(user_message):
            return await self._handle_operator_instruction(mission, user_message, slog)

        slog.log_event(
            "whatsapp_messaging.active_mission_message_ignored",
            {
                "mission_id": str(mission.id),
                "user_message": user_message,
                "reason": "not_instruction_or_regeneration_request",
            },
        )
        return SkillResult(
            ok=True,
            message="Entendi. Não alterei a missão porque não identifiquei uma instrução futura nem um pedido de novo rascunho.",
            mission_id=mission.id,
            extra={
                "message_type": "mission_note",
                "mission_id": str(mission.id),
            },
        )

    async def _cancel_active_mission(
        self,
        mission,
        user_message: str,
        slog: "SessionFileLogger",
    ) -> SkillResult:
        await self.db.update_mission_status(
            mission.id,
            status="cancelled",
            result_summary="Cancelada por comando do usuário.",
            extra_metadata={
                "pending_inbound_ack": False,
                "cancelled_by_user_message": user_message,
            },
        )
        await self.db.clear_mission_next_action(mission.id)
        try:
            acked_count = await self.db.mark_notifications_acked_for_mission(mission.id)
        except Exception as e:
            acked_count = 0
            logger.warning("Failed to ack mission notifications on cancel mission=%s err=%s", mission.id, e)

        slog.log_event(
            "whatsapp_messaging.active_mission_cancelled_by_user",
            {
                "mission_id": str(mission.id),
                "user_message": user_message,
                "acked_notifications": acked_count,
            },
        )
        return SkillResult(
            ok=True,
            message="Missão cancelada. Não vou continuar nem responder essa conversa.",
            mission_id=mission.id,
            extra={
                "message_type": "mission_cancelled",
                "mission_id": str(mission.id),
                "status": "cancelled",
                "acked_notifications": acked_count,
            },
        )

    async def _edit_personal_pending_reply(
        self,
        mission,
        user_message: str,
        slog: "SessionFileLogger",
    ) -> SkillResult:
        meta = mission.extra_metadata or {}
        previous_draft = (meta.get("draft_text") or "").strip()
        if not previous_draft:
            return SkillResult(
                ok=False,
                message="Não encontrei um rascunho pendente nessa missão para alterar.",
                mission_id=mission.id,
            )

        edited_text = _apply_simple_pending_reply_edit(previous_draft, user_message)
        if not edited_text:
            return await self._regenerate_personal_inbound_reply(mission, user_message, slog)

        return await self._replace_personal_pending_reply(
            mission=mission,
            reply_text=edited_text,
            user_message=user_message,
            slog=slog,
            event_name="whatsapp_messaging.personal_reply_edited",
        )

    async def _regenerate_personal_inbound_reply(
        self,
        mission,
        user_message: str,
        slog: "SessionFileLogger",
    ) -> SkillResult:
        meta = mission.extra_metadata or {}
        contact_label = meta.get("contact_label") or mission.title
        contact_phone = meta.get("contact_phone_e164") or _phone_from_jid(mission.contact_jid)
        inbound_text = (meta.get("last_inbound_text") or "").strip()
        previous_draft = (meta.get("draft_text") or "").strip()
        if not inbound_text:
            return SkillResult(
                ok=False,
                message="Não encontrei a última mensagem recebida dessa missão para gerar outra resposta.",
                mission_id=mission.id,
            )

        reply_text = await self._build_personal_ack_reply(
            mission_goal=mission.goal,
            contact_label=contact_label,
            inbound_text=inbound_text,
            operator_instructions=self._get_operator_instructions(meta),
            prior_sent_messages=[
                *(meta.get("last_outbound_messages") or []),
                previous_draft,
            ],
            rewrite_request=user_message,
            previous_draft=previous_draft,
            slog=slog,
        )
        return await self._replace_personal_pending_reply(
            mission=mission,
            reply_text=reply_text,
            user_message=user_message,
            slog=slog,
            event_name="whatsapp_messaging.personal_reply_regenerated",
        )

    async def _replace_personal_pending_reply(
        self,
        mission,
        reply_text: str,
        user_message: str,
        slog: "SessionFileLogger",
        event_name: str,
    ) -> SkillResult:
        meta = mission.extra_metadata or {}
        contact_label = meta.get("contact_label") or mission.title
        contact_phone = meta.get("contact_phone_e164") or _phone_from_jid(mission.contact_jid)
        inbound_text = (meta.get("last_inbound_text") or "").strip()
        previous_draft = (meta.get("draft_text") or "").strip()
        reply_messages = _split_whatsapp_messages(reply_text, mode="personal")
        await self.db.update_mission_status(
            mission.id,
            status="awaiting_confirmation",
            extra_metadata={
                "draft_text": reply_text,
                "draft_messages": reply_messages,
                "pending_inbound_ack": True,
                "last_reply_edit_request": user_message,
            },
        )
        await self.db.set_mission_next_action(
            mission_id=mission.id,
            action_type="send_whatsapp",
            action_params={
                "jid": mission.contact_jid,
                "text": reply_text,
                "messages": reply_messages,
                "delay_ms": _DEFAULT_MESSAGE_DELAY_MS,
                "idempotency_key": str(mission.id),
            },
        )

        phone_label = contact_phone or _phone_from_jid(mission.contact_jid) or mission.contact_jid
        notification_payload = {
            "mission_id": str(mission.id),
            "contact_label": contact_label,
            "contact_jid": mission.contact_jid,
            "contact_phone_e164": contact_phone or phone_label,
            "inbound_text": inbound_text,
            "draft_text": reply_text,
            "draft_messages": reply_messages,
            "mode": "personal",
            "pending_inbound_ack": True,
        }
        approval_body = _build_approval_body(
            contact_label,
            phone_label,
            reply_text,
            reply_messages,
            prefix=f"Recebido: {inbound_text}" if inbound_text else None,
        )
        await self._notify(
            user_id=mission.user_id,
            mission_id=mission.id,
            kind="confirm_reply",
            title=f"Nova sugestão para {contact_label}",
            body=approval_body,
            payload=notification_payload,
            actions=[
                {"action_type": "approve", "label": "Aprovo", "button_style": "contained"},
                {"action_type": "edit", "label": "Eu escrevo", "button_style": "outlined"},
                {"action_type": "cancel", "label": "Não fazer nada", "button_style": "ghost"},
            ],
            config={"persistent": True, "position": "bottom_right", "play_sound": True},
        )
        slog.log_event(
            event_name,
            {
                "mission_id": str(mission.id),
                "inbound_text": inbound_text,
                "previous_draft": previous_draft,
                "draft_reply": reply_text,
                "draft_messages": reply_messages,
                "request": user_message,
            },
        )
        return SkillResult(
            ok=True,
            message=f"Nova sugestão de resposta para {contact_label}.",
            mission_id=mission.id,
        )

    async def _handle_operator_instruction(
        self,
        mission,
        user_message: str,
        slog: "SessionFileLogger",
    ) -> SkillResult:
        instruction = (user_message or "").strip()
        if not instruction:
            return SkillResult(ok=False, message="Instrução vazia.", mission_id=mission.id)

        meta = mission.extra_metadata or {}
        prior = list(meta.get("operator_instructions") or [])
        prior.append(instruction)
        prior = prior[-10:]

        await self.db.update_mission_status(
            mission.id,
            status=mission.status,
            extra_metadata={
                "operator_instruction": instruction,
                "operator_instructions": prior,
            },
        )

        slog.log_event(
            "whatsapp_messaging.operator_instruction_registered",
            {
                "mission_id": str(mission.id),
                "instruction": instruction,
                "instruction_count": len(prior),
            },
        )

        return SkillResult(
            ok=True,
            message="Entendi. Registrei essa instrução e vou seguir isso nas próximas respostas dessa missão.",
            mission_id=mission.id,
            extra={
                "message_type": "mission_instruction",
                "mission_id": str(mission.id),
                "instruction": instruction,
            },
        )

    @staticmethod
    def _build_extra_instructions_block(meta: dict[str, Any]) -> str:
        instructions = WhatsAppMessagingPipeline._get_operator_instructions(meta)
        if not instructions:
            return "Nenhuma instrução adicional."
        return "\n".join(f"- {item}" for item in instructions[-5:])

    @staticmethod
    def _get_operator_instructions(meta: dict[str, Any]) -> list[str]:
        instructions = list(meta.get("operator_instructions") or [])
        latest = (meta.get("operator_instruction") or "").strip()
        if latest and (not instructions or instructions[-1] != latest):
            instructions.append(latest)
        return [str(item).strip() for item in instructions if str(item).strip()]

    async def _build_conversation_memory(
        self,
        session_id: UUID,
        current_user_message: str,
        history: list[dict[str, str]] | None = None,
        mission_id: UUID | None = None,
    ) -> ConversationMemory:
        memory = ConversationMemory(session_id=session_id)
        rows = history or await self.db.fetch_session_history(session_id, limit=20)
        rows = self._whatsapp_conversation_rows(rows, mission_id)
        for index, row in enumerate(rows):
            role = row.get("role")
            content = (row.get("content") or "").strip()
            if not content or role not in {"user", "assistant"}:
                continue
            if role == "user" and content == current_user_message and index == len(rows) - 1:
                continue
            memory.add_message(
                Message(
                    role=MessageRole(role),
                    content=content,
                )
            )
        return memory

    @staticmethod
    def _whatsapp_conversation_rows(
        rows: list[dict[str, str]],
        mission_id: UUID | None = None,
    ) -> list[dict[str, str]]:
        mission_key = str(mission_id) if mission_id else ""
        if mission_key and any(row.get("mission_id") == mission_key for row in rows):
            return [row for row in rows if row.get("mission_id") == mission_key]
        for index, row in enumerate(rows):
            if row.get("tool_name") == "whatsapp_send_message":
                return rows[index:]
        for index, row in enumerate(rows):
            if row.get("role") == "assistant":
                return rows[index:]
        return []

    def _build_goal_status(self, memory: ConversationMemory) -> str:
        if self._has_prior_assistant_message(memory.get_messages()):
            return (
                "Já houve mensagem enviada por você nesta missão. "
                "Use o histórico para ver o que já foi comunicado e não repita o objetivo original "
                "a menos que o contato peça esclarecimento."
            )
        return (
            "Ainda não há mensagem sua no histórico desta missão. "
            "Se precisar agir, comunique o objetivo sem inventar dados."
        )

    @staticmethod
    def _has_prior_assistant_message(messages: list[Message]) -> bool:
        return any(msg.role == MessageRole.ASSISTANT for msg in messages)

    @staticmethod
    def _build_logged_initial_messages(
        system_prompt: str,
        inbound_text: str,
        memory: ConversationMemory,
    ) -> list[Message]:
        messages = PromptBuilder(system_prompt=system_prompt).build_initial_messages(
            inbound_text,
            WHATSAPP_MESSAGING_TOOLS,
        )
        if not memory.get_messages():
            return messages
        task_message = messages[-1]
        history_messages = [
            msg for msg in memory.get_messages()
            if msg.role != MessageRole.SYSTEM
        ]
        messages = [messages[0], *history_messages]
        if not history_messages or history_messages[-1].content != task_message.content:
            messages.append(task_message)
        return messages

    def _log_agent_steps(
        self,
        slog: "SessionFileLogger",
        steps: list[dict[str, Any]],
    ) -> None:
        for step in steps or []:
            step_number = _obj_get(step, "step_number", "?")
            thought = _obj_get(step, "thought", None)
            if thought:
                slog.log_llm_response(
                    stage=f"whatsapp_messaging_agent.step_{step_number}.thought",
                    response=thought,
                    duration_ms=0,
                )
            tool_results = {
                _obj_get(result, "tool_call_id", ""): result
                for result in (_obj_get(step, "tool_results", []) or [])
            }
            for tc in _obj_get(step, "tool_calls", []) or []:
                tool_call_id = _obj_get(tc, "id", "")
                tool_name = _obj_get(tc, "name", "?")
                slog.log_tool_request(
                    tool_name=tool_name,
                    arguments={
                        "tool_call_id": tool_call_id,
                        "arguments": _obj_get(tc, "arguments", {}),
                    },
                    execution="server-side",
                )
                result = tool_results.get(tool_call_id)
                if result:
                    slog.log_tool_result(
                        tool_name=tool_name,
                        success=_obj_get(result, "error", None) is None,
                        result=_obj_get(result, "result", None),
                        error=_obj_get(result, "error", None),
                        duration_ms=float(_obj_get(result, "execution_time", 0) or 0) * 1000,
                        execution="server-side",
                    )


# ─────────────────────────── helpers ────────────────────────────────────

def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class _ResolvedRecipient:
    __slots__ = (
        "ok", "jid", "name", "phone_e164", "error", "candidates",
        "linked_user_id", "relationship", "source", "pks_entity_id",
    )

    def __init__(
        self,
        ok: bool,
        jid: str | None = None,
        name: str | None = None,
        phone_e164: str | None = None,
        error: str | None = None,
        candidates: list[dict] | None = None,
        linked_user_id: str | None = None,
        relationship: str | None = None,
        source: str | None = None,
        pks_entity_id: str | None = None,
    ) -> None:
        self.ok = ok
        self.jid = jid
        self.name = name
        self.phone_e164 = phone_e164
        self.error = error
        self.candidates = candidates
        # Where the match came from: "explicit" | "pks" | "saved" | "gateway".
        self.source = source
        # Present only when the matched person is a known account (their own PKS).
        self.linked_user_id = linked_user_id
        self.relationship = relationship
        self.pks_entity_id = pks_entity_id


def _phone_from_jid(jid: str | None) -> str | None:
    if not jid or "@" not in jid:
        return None
    user = jid.split("@", 1)[0]
    return f"+{user}" if user.isdigit() else None


def _split_whatsapp_messages(text: str, mode: str = "personal") -> list[str]:
    """Split a WhatsApp draft into a small, natural batch of message bubbles."""
    text = " ".join((text or "").strip().split())
    if not text:
        return []

    max_parts = _MAX_COMMERCIAL_MESSAGE_PARTS if mode == "commercial" else _MAX_PERSONAL_MESSAGE_PARTS
    if len(text) <= 55:
        return [text]

    protected = re.search(r"https?://|www\.|\b\d{1,2}:\d{2}\b|R\$\s*\d|@\w+|\+\d{10,15}", text)
    if protected:
        return [text[:_MAX_MESSAGE_PART_CHARS].strip()]

    sentences = [
        part.strip(" ,")
        for part in re.split(r"(?<=[.!?])\s+|\s+-\s+|\s+;\s+", text)
        if part.strip(" ,")
    ]
    if len(sentences) <= 1:
        sentences = [
            part.strip(" ,")
            for part in re.split(r"\s*,\s+|\s+e\s+", text)
            if part.strip(" ,")
        ]

    parts: list[str] = []
    current = ""
    consumed = 0
    for index, sentence in enumerate(sentences):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= 70 or not current:
            current = candidate
            consumed = index + 1
            continue
        parts.append(current)
        current = sentence
        consumed = index + 1
        if len(parts) >= max_parts - 1:
            break

    remainder = " ".join([current, *sentences[consumed:]]).strip()
    if remainder:
        parts.append(remainder)
    if not parts:
        parts = [text]

    cleaned = [part[:_MAX_MESSAGE_PART_CHARS].strip(" ,") for part in parts if part.strip(" ,")]
    return cleaned[:max_parts] or [text[:_MAX_MESSAGE_PART_CHARS].strip()]


def _normalize_message_parts(value: Any, fallback_text: str) -> list[str]:
    if isinstance(value, list):
        parts = [" ".join(str(item).strip().split()) for item in value if str(item).strip()]
    else:
        parts = []
    if not parts:
        parts = _split_whatsapp_messages(fallback_text, mode="personal")
    return parts or [fallback_text.strip()]


def _normalize_delay_ms(value: Any) -> int:
    try:
        delay = int(value)
    except (TypeError, ValueError):
        delay = _DEFAULT_MESSAGE_DELAY_MS
    return max(500, min(delay, 3000))


def _format_message_parts(messages: list[str]) -> str:
    if len(messages) <= 1:
        return messages[0] if messages else ""
    return "\n".join(f"{index}. {message}" for index, message in enumerate(messages, start=1))


def _build_approval_body(
    contact_label: str,
    phone_label: str,
    draft_text: str,
    messages: list[str],
    prefix: str | None = None,
) -> str:
    formatted = _format_message_parts(messages or [draft_text])
    header = f"Vou enviar para {contact_label} ({phone_label}):" if prefix else f"Para {contact_label} ({phone_label}):"
    if prefix:
        return f"{prefix}\n\n{header}\n\n{formatted}"
    return f"{header}\n\n{formatted}"


def _build_sent_body(inbound_text: str, messages: list[str]) -> str:
    return f"Recebido: {inbound_text}\n\nEnviado:\n\n{_format_message_parts(messages)}"


def _sanitize_personal_ack(text: str) -> str:
    text = " ".join((text or "").strip().strip('"“”').split())
    if not text:
        return ""
    lowered = text.lower()
    blocked_fragments = ("```", "whatsapp_send_message", "não posso", "nao posso")
    if any(fragment in lowered for fragment in blocked_fragments):
        return ""
    return text[:180].strip()


def _strip_redundant_personal_context(text: str, forbidden_phrases: list[str]) -> str:
    if not text:
        return text

    cleaned = text
    for phrase in forbidden_phrases:
        candidate = " ".join((phrase or "").strip().split())
        if not candidate or len(candidate) < 8:
            continue
        pattern = rf"(?:,?\s*(?:mas|porem|porém|so que|só que)\s*)?{re.escape(candidate)}(?:\s+hoje)?"
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;:-")
    cleaned = re.sub(r"(?:,?\s*(?:mas|porem|porém|so que|só que))\s*$", "", cleaned, flags=re.IGNORECASE).strip(" ,;:-")
    if len(cleaned) < 4:
        return text
    return cleaned[:180].strip()


def _reply_from_matching_operator_instruction(
    instructions: list[str],
    inbound_text: str,
) -> str | None:
    inbound_normalized = _fold_accents(inbound_text or "").lower()
    if not inbound_normalized.strip():
        return None

    for instruction in reversed(instructions):
        raw_instruction = (instruction or "").strip()
        if not raw_instruction:
            continue

        reply_match = re.search(
            r"\b(?:diz|responde|fala|mande|manda)\s+que\s+(.+)$",
            raw_instruction,
            flags=re.IGNORECASE,
        )
        if not reply_match:
            continue

        condition = raw_instruction[: reply_match.start()]
        if not _operator_instruction_condition_matches(condition, inbound_normalized):
            continue

        reply = _sanitize_personal_ack(reply_match.group(1))
        if reply:
            return reply[:180].strip()
    return None


def _operator_instruction_condition_matches(condition: str, inbound_normalized: str) -> bool:
    condition_normalized = _fold_accents(condition or "").lower()
    if not condition_normalized.strip():
        return False

    semantic_groups = (
        {"comida", "janta", "jantar", "almoco", "almocar", "mesa", "geladeira", "quentinha", "marmita"},
        {"ketchup", "molho", "maionese", "mostarda"},
        {"chegar", "cheguei", "chegando", "atrasar", "atrasado", "tarde"},
    )
    inbound_tokens = set(re.findall(r"\w{4,}", inbound_normalized))
    condition_tokens = set(re.findall(r"\w{4,}", condition_normalized))

    for group in semantic_groups:
        if condition_tokens & group and inbound_tokens & group:
            return True

    ignored = {
        "minha", "meu", "irma", "irmao", "falar", "responder", "perguntar",
        "disser", "dizer", "deixar", "deixou", "deixa", "caso", "quando",
        "para", "pela", "pelo", "isso", "essa", "esse",
    }
    meaningful_terms = {token for token in condition_tokens if token not in ignored and len(token) >= 4}
    return bool(meaningful_terms & inbound_tokens)


def _apply_simple_pending_reply_edit(previous_draft: str, user_message: str) -> str | None:
    draft = " ".join((previous_draft or "").strip().split())
    request = " ".join((user_message or "").strip().split())
    if not draft or not request:
        return None

    append_match = re.search(
        r"\b(?:coloca|coloque|adiciona|adicione|bota|inclui|inclua|acrescenta|acrescente)\s+(.+?)\s+(?:no|ao)\s+final\b",
        request,
        flags=re.IGNORECASE,
    )
    if append_match:
        addition = _clean_draft_edit_fragment(append_match.group(1))
        if not addition:
            return None
        folded_draft = _fold_accents(draft).lower()
        folded_addition = _fold_accents(addition).lower()
        if folded_draft.endswith(folded_addition):
            return draft[:180].strip()
        base = re.sub(r"[.!?]+$", "", draft).strip()
        separator = " " if base.endswith((",", ";", ":")) else ", "
        return f"{base}{separator}{addition}"[:180].strip(" ,")

    replace_match = re.search(
        r"\b(?:troca|troque|muda|mude)\s+(?:por|para)\s+(.+)$",
        request,
        flags=re.IGNORECASE,
    )
    if replace_match:
        replacement = _sanitize_personal_ack(replace_match.group(1))
        return replacement or None

    remove_match = re.search(
        r"\b(?:tira|retira|remove|remova)\s+(.+)$",
        request,
        flags=re.IGNORECASE,
    )
    if remove_match:
        fragment = _clean_draft_edit_fragment(remove_match.group(1))
        if not fragment:
            return None
        cleaned = re.sub(re.escape(fragment), "", draft, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;:-")
        return cleaned[:180].strip() if cleaned and cleaned != draft else None

    return None


def _clean_draft_edit_fragment(fragment: str) -> str:
    text = " ".join((fragment or "").strip().strip('"“”').split())
    text = re.sub(r"^(?:um|uma|uns|umas)\s+", "", text, flags=re.IGNORECASE)
    text = text.strip(" ,.;:")
    return text[:80].strip()


def _fallback_personal_ack(inbound_text: str) -> str:
    normalized = _fold_accents(inbound_text or "").lower()
    if any(word in normalized for word in ("comida", "janta", "almoco", "mesa", "prato")):
        return "Ok! Quando chegar eu como. Obrigado!"
    if any(word in normalized for word in ("obrigad", "valeu")):
        return "Ok, obrigado!"
    if any(word in normalized for word in ("sim", "ok", "beleza", "certo", "ta bom", "tudo bem")):
        return "Ok, combinado!"
    return "Ok, obrigado por avisar!"


def _fold_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _strip_stopwords(text: str) -> str:
    folded = _fold_accents(text or "").lower()
    tokens = [t for t in re.split(r"[^\w]+", folded) if t and t not in _RECIPIENT_STOPWORDS]
    return " ".join(tokens).strip()


def _strong_match(contact: dict, query: str) -> bool:
    name = _fold_accents((contact.get("name") or "").lower())
    q = _fold_accents(query.lower())
    if not name or not q:
        return False
    return name == q or name.startswith(q) or q in name.split()
