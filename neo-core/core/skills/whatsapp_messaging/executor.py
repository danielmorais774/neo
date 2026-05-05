"""WhatsApp Messaging tool executor — handles tool calls during agent loops."""

import logging
from typing import Any

from uuid import UUID

from core.skills.whatsapp_messaging.gateway_client import WhatsAppGatewayClient
from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)


class WhatsAppMessagingExecutor:
    """Executes tool calls for the whatsapp_messaging skill.

    All tools operate within the context of a mission_id.

    In `personal` mode `whatsapp_send_message` is *not* a real send — it is
    rerouted into a confirmation flow (draft + awaiting_confirmation
    notification) so the user always approves before delivery.

    In `commercial` mode the agent sends directly because it manages a
    multi-turn negotiation with a third-party (restaurant, store).
    """

    def __init__(self, db: AsyncPostgresClient, gateway: WhatsAppGatewayClient) -> None:
        self.db = db
        self.gateway = gateway

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        mission_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        if tool_name == "whatsapp_send_message":
            return await self._send_message(
                mission_id,
                arguments["text"],
                user_id,
                arguments.get("recipient"),
            )
        elif tool_name == "whatsapp_request_confirmation":
            return await self._request_confirmation(
                mission_id,
                arguments["question"],
                user_id,
                arguments.get("draft_text"),
            )
        elif tool_name == "whatsapp_complete_mission":
            return await self._complete_mission(
                mission_id,
                arguments.get("summary", ""),
                arguments.get("details"),
                user_id,
            )
        elif tool_name == "whatsapp_fail_mission":
            return await self._fail_mission(mission_id, arguments["reason"], user_id)
        elif tool_name == "whatsapp_lookup_contact":
            return await self._lookup_contact(arguments["label"], user_id)
        elif tool_name == "whatsapp_list_contacts":
            return await self._list_contacts(arguments.get("query"))
        elif tool_name == "whatsapp_notify_user":
            return await self._notify_user(
                mission_id,
                user_id,
                arguments.get("title", ""),
                arguments.get("body", ""),
            )
        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def _send_message(
        self,
        mission_id: UUID,
        text: str,
        user_id: UUID,
        recipient: str | None = None,
    ) -> dict[str, Any]:
        mission = await self.db.get_mission(mission_id)
        if not mission:
            return {"ok": False, "error": "Mission not found"}

        mode = (mission.extra_metadata or {}).get("mode", "commercial")

        if mode == "personal":
            return await self._request_confirmation(
                mission_id,
                question=f"Enviar para {mission.title}?",
                user_id=user_id,
                draft_text=text,
            )

        target_jid = mission.contact_jid
        if recipient:
            contact = await self.db.search_whatsapp_contact(user_id, recipient)
            if contact:
                target_jid = contact.jid
            else:
                raw = recipient.strip()
                if "@" in raw:
                    target_jid = raw
                else:
                    digits = "".join(filter(str.isdigit, raw))
                    target_jid = f"{digits}@c.us" if digits else raw

        try:
            message_id = await self.gateway.send_message(
                jid=target_jid,
                text=text,
                idempotency_key=f"{mission_id}:{len(text)}",
            )
        except Exception as e:
            logger.error({"mission": str(mission_id), "jid": target_jid, "err": str(e)}, "Gateway send failed")
            return {"ok": False, "error": str(e)}

        await self.db.save_message(
            session_id=mission.session_id,
            role="assistant",
            content=text,
            tool_name="whatsapp_send_message",
            tool_call_id=message_id,
            user_id=user_id,
        )

        await self.db.update_mission_status(
            mission_id,
            status="awaiting_reply",
            extra_metadata={"last_outbound_id": message_id},
        )

        return {"ok": True, "message_id": message_id, "to": target_jid}

    async def _request_confirmation(
        self,
        mission_id: UUID,
        question: str,
        user_id: UUID,
        draft_text: str | None = None,
    ) -> dict[str, Any]:
        mission = await self.db.get_mission(mission_id)
        if not mission:
            return {"ok": False, "error": "Mission not found"}

        meta_update: dict[str, Any] = {"pending_question": question}
        if draft_text:
            meta_update["draft_text"] = draft_text

        await self.db.update_mission_status(
            mission_id,
            status="awaiting_confirmation",
            extra_metadata=meta_update,
        )

        contact_label = mission.title or mission.contact_jid
        contact_phone = (mission.extra_metadata or {}).get("contact_phone_e164") or _phone_from_jid(
            mission.contact_jid
        )
        body_lines = [question]
        if draft_text:
            body_lines.append(f"\nMensagem: “{draft_text}”")
        if contact_phone:
            body_lines.append(f"\nDestinatário: {contact_label} ({contact_phone})")
        else:
            body_lines.append(f"\nDestinatário: {contact_label}")

        await self.db.create_notification(
            user_id=user_id,
            mission_id=mission_id,
            kind="approval_required",
            title=f"Aprovar mensagem: {contact_label}",
            body="\n".join(body_lines),
            payload={
                "mission_id": str(mission_id),
                "question": question,
                "draft_text": draft_text,
                "contact_label": contact_label,
                "contact_jid": mission.contact_jid,
                "contact_phone_e164": contact_phone,
            },
        )

        return {"ok": True, "status": "awaiting_confirmation"}

    async def _complete_mission(
        self,
        mission_id: UUID,
        summary: str,
        details: str | None,
        user_id: UUID,
    ) -> dict[str, Any]:
        mission = await self.db.get_mission(mission_id)
        if not mission:
            return {"ok": False, "error": "Mission not found"}

        await self.db.update_mission_status(
            mission_id,
            status="completed",
            result_summary=summary,
        )

        await self.db.create_notification(
            user_id=user_id,
            mission_id=mission_id,
            kind="mission_completed",
            title="Missão concluída",
            body=summary,
            payload={"summary": summary, "details": details},
        )

        return {"ok": True, "status": "completed", "summary": summary}

    async def _fail_mission(
        self, mission_id: UUID, reason: str, user_id: UUID
    ) -> dict[str, Any]:
        mission = await self.db.get_mission(mission_id)
        if not mission:
            return {"ok": False, "error": "Mission not found"}

        await self.db.update_mission_status(
            mission_id,
            status="failed",
            result_summary=reason,
        )

        await self.db.create_notification(
            user_id=user_id,
            mission_id=mission_id,
            kind="mission_failed",
            title="Missão falhou",
            body=reason,
            payload={"reason": reason},
        )

        return {"ok": True, "status": "failed", "reason": reason}

    async def _lookup_contact(self, label: str, user_id: UUID) -> dict[str, Any]:
        results = await self.gateway.list_contacts(query=label, only_saved=False, limit=5)
        if not results:
            local = await self.db.get_whatsapp_contact_by_label(user_id, label)
            if local:
                return {
                    "ok": True,
                    "label": local.label,
                    "name": local.label,
                    "phone_e164": local.phone_e164,
                    "jid": local.jid,
                    "source": "db",
                }
            return {
                "ok": False,
                "error": f"Nenhum contato chamado '{label}' na agenda do WhatsApp.",
            }
        top = results[0]
        return {
            "ok": True,
            "label": top.get("name"),
            "name": top.get("name"),
            "phone_e164": top.get("phone_e164"),
            "jid": top.get("jid"),
            "matches": [
                {"name": r.get("name"), "phone_e164": r.get("phone_e164"), "jid": r.get("jid")}
                for r in results[:5]
            ],
            "source": "whatsapp",
        }

    async def _list_contacts(self, query: str | None) -> dict[str, Any]:
        results = await self.gateway.list_contacts(query=query, only_saved=False, limit=20)
        return {
            "ok": True,
            "count": len(results),
            "contacts": [
                {
                    "name": r.get("name"),
                    "phone_e164": r.get("phone_e164"),
                    "jid": r.get("jid"),
                }
                for r in results
            ],
        }

    async def _notify_user(
        self,
        mission_id: UUID,
        user_id: UUID,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        await self.db.create_notification(
            user_id=user_id,
            mission_id=mission_id,
            kind="mission_update",
            title=title or "Atualização da missão",
            body=body,
            payload={"mission_id": str(mission_id)},
        )
        return {"ok": True}


def _phone_from_jid(jid: str | None) -> str | None:
    if not jid or "@" not in jid:
        return None
    user = jid.split("@", 1)[0]
    if not user.isdigit():
        return None
    return f"+{user}"
