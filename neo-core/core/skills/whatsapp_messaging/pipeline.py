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
import unicodedata
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from observability.session_file_logger import SessionFileLogger

from core.agent.agent_executor import AgentExecutor
from core.agent.prompt_builder import PromptBuilder
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

_JID_RE = re.compile(r"\b(\d{6,20}@(?:c\.us|lid|s\.whatsapp\.net|g\.us))\b")
_PHONE_RE = re.compile(r"\+?\d{10,15}")


class WhatsAppMessagingPipeline(SkillPipeline):
    def __init__(
        self,
        agent_executor: AgentExecutor,
        db: AsyncPostgresClient,
        broker: NotificationBroker,
    ) -> None:
        self.agent_executor = agent_executor
        self.db = db
        self.broker = broker
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
            return await self._handle_inbound(mission, user_message, slog, extra)

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

        if not recipient_hint:
            recipient_hint = self._extract_recipient_hint(user_message)
        if not message_text:
            message_text = self._extract_message_text(user_message)

        mode = self._detect_mode(user_message, recipient_hint, message_text)
        slog.log_event(
            "whatsapp_messaging.kickoff_parse",
            {
                "recipient_hint": recipient_hint,
                "message_text": message_text,
                "detected_mode": mode,
                "raw_user_message": user_message,
            },
        )

        resolved = await self._resolve_recipient(user_id, recipient_hint, user_message)
        slog.log_event(
            "whatsapp_messaging.resolve_recipient",
            {
                "ok": resolved.ok,
                "jid": resolved.jid,
                "name": resolved.name,
                "phone_e164": resolved.phone_e164,
                "error": resolved.error,
                "candidates": resolved.candidates,
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
                "contact_phone_e164": contact_phone or "",
                "contact_label": contact_label,
            },
        )

        if mode == "personal":
            return await self._kickoff_personal(
                mission_id=mission.id,
                user_id=user_id,
                contact_label=contact_label,
                contact_jid=contact_jid,
                contact_phone=contact_phone,
                draft_text=message_text or user_message,
                slog=slog,
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
    ) -> SkillResult:
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
        body = f"Para {contact_label} ({phone_label}):\n\n{draft_text}"
        notification_payload = {
            "mission_id": str(mission_id),
            "draft_text": draft_text,
            "contact_label": contact_label,
            "contact_jid": contact_jid,
            "contact_phone_e164": contact_phone or phone_label,
            "mode": "personal",
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
    ) -> SkillResult:
        system_prompt = self._build_system_prompt(
            mode="commercial",
            contact_label=contact_label,
            contact_jid=contact_jid,
            contact_phone=contact_phone,
            goal=user_message,
        )
        slog.log_event(
            "whatsapp_messaging.commercial_system_prompt",
            {
                "mode": "commercial",
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
                    "mode": "commercial",
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
    ) -> SkillResult:
        locked = await self.db.lock_mission_for_update(mission.id)
        if not locked:
            return SkillResult(ok=False, message="Não foi possível travar a missão")

        mission.status = "processing"
        mission.last_activity_at = datetime.utcnow()

        await self.db.save_message(
            session_id=mission.session_id,
            role="user",
            content=inbound_text,
            user_id=mission.user_id,
            mission_id=mission.id,
        )

        meta = mission.extra_metadata or {}
        mode = meta.get("mode", "commercial")
        contact_label = meta.get("contact_label") or mission.title
        contact_phone = meta.get("contact_phone_e164") or _phone_from_jid(mission.contact_jid)
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
                slog=slog,
            )
            await self.db.update_mission_status(
                mission.id,
                status="awaiting_confirmation",
                extra_metadata={
                    "last_inbound_text": inbound_text,
                    "draft_text": reply_text,
                    "pending_inbound_ack": True,
                },
            )
            await self.db.set_mission_next_action(
                mission_id=mission.id,
                action_type="send_whatsapp",
                action_params={
                    "jid": mission.contact_jid,
                    "text": reply_text,
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
                "mode": "personal",
                "pending_inbound_ack": True,
            }
            await self._notify(
                user_id=mission.user_id,
                mission_id=mission.id,
                kind="confirm_reply",
                title=f"Aprovar resposta para {contact_label}",
                body=f"Recebido: {inbound_text}\n\nVou enviar para {contact_label} ({phone_label}):\n\n{reply_text}",
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
                    "notification": {
                        "kind": "confirm_reply",
                        "title": f"Aprovar resposta para {contact_label}",
                        "body": f"Recebido: {inbound_text}\n\nVou enviar para {contact_label} ({phone_label}):\n\n{reply_text}",
                        "payload": notification_payload,
                    },
                },
            )

            return SkillResult(
                ok=True,
                message=f"{contact_label} respondeu. Rascunho pronto, aguardando sua aprovação para enviar.",
                mission_id=mission.id,
            )

        system_prompt = self._build_system_prompt(
            mode="commercial",
            contact_label=contact_label or mission.title,
            contact_jid=mission.contact_jid,
            contact_phone=contact_phone,
            goal=mission.goal,
        )
        slog.log_event(
            "whatsapp_messaging.inbound_commercial_system_prompt",
            {
                "mode": "commercial",
                "contact_label": contact_label or mission.title,
                "contact_jid": mission.contact_jid,
                "contact_phone": contact_phone,
                "goal": mission.goal,
                "inbound_text": inbound_text,
                "system_prompt": system_prompt,
                "tools": [tool.name for tool in WHATSAPP_MESSAGING_TOOLS],
            },
        )
        slog.log_llm_request(
            stage="whatsapp_messaging.inbound_commercial_initial_agent_request",
            messages=PromptBuilder(system_prompt=system_prompt).build_initial_messages(
                inbound_text,
                WHATSAPP_MESSAGING_TOOLS,
            ),
            model=self.agent_executor.llm_router.get_default_model(None),
            provider=self.agent_executor.llm_router.get_provider(None).__class__.__name__,
            temperature=self.agent_executor.settings.default_temperature,
        )

        result = await self.agent_executor.execute(
            task=inbound_text,
            session_id=mission.session_id,
            system_prompt_override=system_prompt,
            tool_subset=WHATSAPP_MESSAGING_TOOLS,
            tool_executor=self.executor,
            available_tools=WHATSAPP_MESSAGING_TOOLS,
        )
        self._log_agent_steps(slog, result.get("steps", []))

        await self.db.update_mission_status(mission.id, status="awaiting_reply")
        return SkillResult(ok=True, message="Resposta processada.", mission_id=mission.id)

    async def _build_personal_ack_reply(
        self,
        mission_goal: str,
        contact_label: str,
        inbound_text: str,
        slog: SessionFileLogger | None = None,
    ) -> str:
        fallback = _fallback_personal_ack(inbound_text)
        try:
            llm_provider = self.agent_executor.llm_router.get_provider(None)
            model = self.agent_executor.llm_router.get_default_model(None)
            messages = [
                Message(
                    role=MessageRole.SYSTEM,
                    content=(
                        "Você escreve respostas curtas de WhatsApp em português do Brasil. "
                        "Responda como Daniel responderia: natural, direto e sem formalidade excessiva. "
                        "Retorne somente o texto da mensagem, sem aspas, sem explicação, "
                        "sem markdown e com no máximo 180 caracteres. "
                        "Não invente horário, motivo, preço, endereço nem compromisso que não esteja no contexto. "
                        "Se a resposta exigir uma informação ausente, faça uma pergunta curta."
                    ),
                ),
                Message(
                    role=MessageRole.USER,
                    content=(
                        f"Pedido original do usuário: {mission_goal}\n"
                        f"Contato: {contact_label}\n"
                        f"Mensagem recebida do contato: {inbound_text}\n\n"
                        "Escreva uma resposta curta apropriada para enviar ao contato, "
                        "mantendo o tom de conversa de WhatsApp."
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
        logger.info(f"_handle_approval: mission={mission.id} approved={approved} text_to_send={text_to_send[:50] if text_to_send else 'EMPTY'}")
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
                "idempotency_key": action_params.get("idempotency_key") or str(mission.id),
            },
        }
        slog.log_tool_request(
            tool_name="whatsapp_send_message",
            arguments=gateway_payload,
            execution="server-side",
        )
        start = perf_counter()
        try:
            message_id = await self.gateway.send_message(
                jid=target_jid,
                text=text_to_send,
                idempotency_key=action_params.get("idempotency_key") or str(mission.id),
            )
        except Exception as e:
            logger.error("Approval send failed: mission=%s err=%s", mission.id, e)
            slog.log_tool_result(
                tool_name="whatsapp_send_message",
                success=False,
                error=str(e),
                duration_ms=(perf_counter() - start) * 1000,
                execution="server-side",
            )
            return SkillResult(
                ok=False, message=f"Falha ao enviar: {e}", mission_id=mission.id
            )
        slog.log_tool_result(
            tool_name="whatsapp_send_message",
            success=True,
            result={"message_id": message_id, "to": target_jid},
            duration_ms=(perf_counter() - start) * 1000,
            execution="server-side",
        )

        await self.db.save_message(
            session_id=mission.session_id,
            role="assistant",
            content=text_to_send,
            tool_name="whatsapp_send_message",
            tool_call_id=message_id,
            user_id=mission.user_id,
            mission_id=mission.id,
        )
        slog.log_event(
            "Outbound message saved",
            {
                "mission_id": str(mission.id),
                "session_id": str(mission.session_id),
                "role": "assistant",
                "tool_name": "whatsapp_send_message",
                "tool_call_id": message_id,
                "content": text_to_send,
            },
        )

        await self.db.update_mission_status(
            mission.id,
            status="awaiting_reply",
            extra_metadata={"last_outbound_id": message_id},
        )
        await self.db.clear_mission_next_action(mission.id)
        slog.log_event(
            "Mission status updated",
            {
                "mission_id": str(mission.id),
                "status": "awaiting_reply",
                "last_outbound_id": message_id,
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
                body=f"Recebido: {inbound_text}\n\nEnviado: {text_to_send}",
                payload={
                    "mission_id": str(mission.id),
                    "contact_label": contact_label,
                    "contact_jid": mission.contact_jid,
                    "contact_phone_e164": meta.get("contact_phone_e164") or _phone_from_jid(mission.contact_jid),
                    "inbound_text": inbound_text,
                    "reply_text": text_to_send,
                    "message_id": message_id,
                },
            )
            await self.db.update_mission_status(
                mission.id,
                status="completed",
                result_summary=summary,
                extra_metadata={
                    "last_outbound_id": message_id,
                    "last_inbound_text": inbound_text,
                    "auto_reply_text": text_to_send,
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
        }, ensure_ascii=False)
        return SkillResult(
            ok=True,
            message=f"[MISSION:{_mission_marker}]Mensagem enviada para {mission.title}.",
            mission_id=mission.id,
        )

    # ───────────────────── recipient resolution ─────────────────────────

    async def _resolve_recipient(
        self,
        user_id: UUID,
        recipient_hint: str | None,
        full_message: str,
    ) -> "_ResolvedRecipient":
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
                    )
                return _ResolvedRecipient(
                    ok=True,
                    jid=jid,
                    name=None,
                    phone_e164=_phone_from_jid(jid),
                )

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
                )

        label = _strip_stopwords(recipient_hint or "")
        if not label:
            return _ResolvedRecipient(ok=False, error="Sem destinatário.")

        local = await self.db.search_whatsapp_contact(user_id, label)
        if local:
            return _ResolvedRecipient(
                ok=True,
                jid=local.jid,
                name=local.label,
                phone_e164=local.phone_e164,
            )

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

    def _build_system_prompt(
        self,
        mode: str,
        contact_label: str,
        contact_jid: str,
        contact_phone: str | None,
        goal: str,
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
            user_address=DEFAULT_USER_ADDRESS,
            user_phone=DEFAULT_USER_PHONE,
            user_payment_prefs=DEFAULT_PAYMENT_PREFS,
        )

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
    __slots__ = ("ok", "jid", "name", "phone_e164", "error", "candidates")

    def __init__(
        self,
        ok: bool,
        jid: str | None = None,
        name: str | None = None,
        phone_e164: str | None = None,
        error: str | None = None,
        candidates: list[dict] | None = None,
    ) -> None:
        self.ok = ok
        self.jid = jid
        self.name = name
        self.phone_e164 = phone_e164
        self.error = error
        self.candidates = candidates


def _phone_from_jid(jid: str | None) -> str | None:
    if not jid or "@" not in jid:
        return None
    user = jid.split("@", 1)[0]
    return f"+{user}" if user.isdigit() else None


def _sanitize_personal_ack(text: str) -> str:
    text = " ".join((text or "").strip().strip('"“”').split())
    if not text:
        return ""
    lowered = text.lower()
    blocked_fragments = ("```", "whatsapp_send_message", "não posso", "nao posso")
    if any(fragment in lowered for fragment in blocked_fragments):
        return ""
    return text[:180].strip()


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
