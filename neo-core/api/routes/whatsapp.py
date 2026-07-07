"""WhatsApp webhook endpoint — receives inbound messages from neo-whatsapp."""

import asyncio
import logging
import hmac
import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from api.security import require_panel_jwt, verify_panel_token
from core.skills.skill_executor import SkillExecutor
from core.skills.whatsapp_messaging.gateway_client import WhatsAppGatewayClient
from core.skills.whatsapp_messaging.pipeline import WhatsAppMessagingPipeline, get_mission_dev_logger
from core.notifications.qr_broadcaster import get_qr_broadcaster
from db.async_client import AsyncPostgresClient
from shared.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp"])

_db: AsyncPostgresClient | None = None
_skill_executor: SkillExecutor | None = None


def set_dependencies(db: AsyncPostgresClient, skill_executor: SkillExecutor) -> None:
    global _db, _skill_executor
    _db = db
    _skill_executor = skill_executor


@router.post("/webhook")
async def webhook(
    body: dict,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
):
    """Receive inbound WhatsApp messages from neo-whatsapp service.

    Validates HMAC signature, checks idempotency, routes to skill pipeline.
    """
    settings = get_settings()
    secret = settings.whatsapp_webhook_secret

    # 1. Validate HMAC
    if not x_signature:
        raise HTTPException(status_code=401, detail="Missing signature")

    payload_bytes = str(body).encode()
    expected = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(x_signature, expected):
        logger.warning("Invalid webhook signature: got=%s", x_signature)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Parse event
    jid = body.get("jid", "")
    text = body.get("text", "")
    message_id = body.get("message_id", "")
    timestamp = body.get("timestamp")

    logger.info("Webhook received: jid=%s message_id=%s text=%s", jid, message_id, text[:50])

    if not _db:
        logger.error("DB not initialized")
        raise HTTPException(status_code=500, detail="DB not initialized")

    # 3. Idempotency: skip if already seen
    # In a high-throughput scenario we check last_inbound_message_id
    # For MVP we rely on the DB update to deduplicate

    # 4. Find active mission for this JID
    mission = await _db.find_active_mission_by_contact(jid)

    if not mission:
        # No active mission: still notify the user that the contact reached
        # out (so personal messages from family/friends don't disappear).
        import asyncio as _aio
        _aio.create_task(_notify_unsolicited(jid, text, message_id))
        return {"ok": True, "action": "notified"}

    slog = get_mission_dev_logger(mission.id)
    slog.log_event(
        "neo-whatsapp webhook received",
        {
            "source": "neo-whatsapp",
            "endpoint": "POST /api/v1/whatsapp/webhook",
            "jid": jid,
            "text": text,
            "message_id": message_id,
            "timestamp": timestamp,
            "body": body,
        },
    )
    slog.log_event("Inbound routed to mission", _describe_mission_route(mission, jid))

    if jid.endswith("@lid"):
        try:
            await _db.merge_mission_extra_metadata(
                mission.id,
                {
                    "contact_lid": jid,
                    "last_inbound_jid": jid,
                },
            )
        except Exception as e:
            logger.warning("Failed to persist contact_lid metadata: mission_id=%s err=%s", mission.id, e)

    # 5. Check idempotency (skip duplicate message_id)
    if mission.last_inbound_message_id == message_id:
        logger.info("Duplicate message, skipping: message_id=%s", message_id)
        slog.log_event(
            "Inbound skipped as duplicate",
            {
                "mission_id": str(mission.id),
                "message_id": message_id,
                "last_inbound_message_id": mission.last_inbound_message_id,
            },
        )
        return {"ok": True, "action": "duplicate"}

    # 6. Dispatch to skill pipeline (fire and forget for speed)
    import asyncio
    asyncio.create_task(_process_inbound(mission, text, message_id, timestamp, jid))

    return {"ok": True, "action": "dispatched"}


async def _notify_unsolicited(jid: str, text: str, message_id: str) -> None:
    """Create a notification for an inbound message with no active mission."""
    if not _db:
        return
    try:
        # Look up which user owns this jid via stored contacts.
        contact = await _db.get_whatsapp_contact_by_jid(jid)
        user_id = contact.user_id if contact else None
        label = contact.label if contact else jid.split("@", 1)[0]
        phone = (contact.phone_e164 if contact else None) or _phone_from_jid(jid) or jid

        if not user_id:
            logger.info("Inbound from unknown contact (no user); not notifying jid=%s", jid)
            return

        payload = {
            "contact_label": label,
            "contact_jid": jid,
            "contact_phone_e164": phone,
            "inbound_text": text,
            "message_id": message_id,
        }
        notif = await _db.create_notification(
            user_id=user_id,
            mission_id=None,
            kind="unsolicited_message",
            title=f"{label} te mandou mensagem",
            body=text[:500],
            payload=payload,
        )
        if _skill_executor and _skill_executor.broker:
            try:
                await _skill_executor.broker.publish(
                    user_id,
                    {
                        "id": str(notif.id),
                        "user_id": str(user_id),
                        "mission_id": "",
                        "kind": "unsolicited_message",
                        "title": f"{label} te mandou mensagem",
                        "body": text[:500],
                        "payload": payload,
                        "created_at_unix": int(notif.created_at.timestamp()) if notif.created_at else 0,
                    },
                )
            except Exception as e:
                logger.warning("Broker publish failed (unsolicited): err=%s", e)
    except Exception as e:
        logger.warning("Unsolicited notify failed: jid=%s err=%s", jid, e)


def _phone_from_jid(jid: str | None) -> str | None:
    if not jid or "@" not in jid:
        return None
    user = jid.split("@", 1)[0]
    return f"+{user}" if user.isdigit() else None


def _digits_from_jid(jid: str | None) -> str:
    if not jid or "@" not in jid:
        return ""
    local = jid.split("@", 1)[0]
    return local if local.isdigit() else ""


def _describe_mission_route(mission, inbound_jid: str) -> dict:
    meta = mission.extra_metadata or {}
    inbound_digits = _digits_from_jid(inbound_jid)
    mission_digits = _digits_from_jid(mission.contact_jid)
    meta_digits = "".join(filter(str.isdigit, meta.get("contact_phone_e164", "") or ""))
    last_outbound_id = str(meta.get("last_outbound_id") or "")

    if inbound_jid == mission.contact_jid:
        reason = "exact_contact_jid"
    elif inbound_digits and inbound_digits in {mission_digits, meta_digits}:
        reason = "phone_digits_match"
    elif inbound_jid and inbound_jid in last_outbound_id:
        reason = "inbound_jid_found_in_last_outbound_id"
    else:
        reason = "fallback_active_mission_match"

    return {
        "mission_id": str(mission.id),
        "mission_status": mission.status,
        "mission_contact_jid": mission.contact_jid,
        "inbound_jid": inbound_jid,
        "match_reason": reason,
        "contact_phone_e164": meta.get("contact_phone_e164"),
        "last_outbound_id": last_outbound_id or None,
        "session_id": str(mission.session_id),
    }


async def _process_inbound(
    mission,
    text: str,
    message_id: str,
    timestamp: int | None,
    jid: str,
) -> None:
    """Background task: run the skill pipeline for an inbound message."""
    if not _skill_executor or not _db:
        logger.error("Dependencies not set for inbound processing")
        return

    try:
        pipeline = WhatsAppMessagingPipeline(
            agent_executor=_skill_executor.agent_executor,
            db=_db,
            broker=_skill_executor.broker,
            pks=getattr(_skill_executor, "pks", None),
        )
        history_messages = await _db.fetch_session_messages(
            mission.session_id,
            limit=30,
            roles=["user", "assistant"],
            exclude_tool_calls=True,
        )
        history = [
            {
                "role": msg.role,
                "content": msg.content,
                "tool_name": msg.tool_name or "",
                "mission_id": str(msg.mission_id) if msg.mission_id else "",
            }
            for msg in history_messages
            if msg.content and len(msg.content.strip()) >= 5
        ]

        result = await pipeline.run(
            session_id=mission.session_id,
            user_message=text,
            history=history,
            mission_id=mission.id,
            extra={
                "user_id": mission.user_id,
                "inbound_message_id": message_id,
                "inbound_jid": jid,
                "webhook_message_id": message_id,
            },
        )

        # WhatsApp conversations move Neo's emotional state too. The shared
        # updater (built by AIService) owns dedup/locks/health; when the desktop
        # service hasn't started it yet, this quietly no-ops.
        try:
            from core.agent.affect_updater import get_shared_updater

            updater = get_shared_updater()
            if updater and mission.user_id:
                updater.schedule(
                    user_message=text,
                    assistant_response=getattr(result, "message", None),
                    user_id=mission.user_id,
                    source="whatsapp",
                    source_message_id=str(message_id) if message_id else None,
                )
        except Exception as e:  # noqa: BLE001 - affect must never break inbound
            logger.warning("Affect schedule (whatsapp) failed: %s", e)
    except Exception as e:
        logger.error("Inbound processing failed: mission_id=%s err=%s", mission.id, e)
        await _db.update_mission_status(
            mission.id,
            status="failed",
            result_summary=f"Inbound error: {e}",
        )
        return

    # Update last_inbound_message_id to prevent duplicate processing
    try:
        await _db.update_mission_last_inbound_message_id(mission.id, message_id)
    except Exception as e:
        logger.warning("Failed to update last_inbound_message_id: mission_id=%s err=%s", mission.id, e)


# ── Panel-facing endpoints ────────────────────────────────────────────────

@router.get("/contacts")
async def list_contacts(user_id: UUID, _: dict = Depends(require_panel_jwt)) -> list[dict]:
    """List WhatsApp contacts for a user (panel use)."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    contacts = await _db.list_whatsapp_contacts(user_id)
    return [
        {
            "id": str(c.id),
            "jid": c.jid,
            "label": c.label,
            "phone_e164": c.phone_e164,
            "notes": c.notes,
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        }
        for c in contacts
    ]


@router.post("/send")
async def send_message(
    user_id: UUID | None = None,
    jid: str = "",
    text: str = "",
    _: dict = Depends(require_panel_jwt),
) -> dict:
    """Send a WhatsApp message via neo-whatsapp gateway."""
    if not _db:
        raise HTTPException(status_code=500, detail="DB not initialized")

    # Upsert contact so it shows up in the panel list
    if user_id:
        await _db.upsert_whatsapp_contact(
            user_id=user_id,
            jid=jid,
            label=jid.split("@")[0],
            phone_e164="",
        )

    gateway = WhatsAppGatewayClient()
    try:
        message_id = await gateway.send_message(jid=jid, text=text)
        return {"ok": True, "message_id": message_id}
    except Exception as e:
        logger.error("Failed to send message: jid=%s err=%s", jid, e)
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        await gateway.close()


@router.get("/connection-status")
async def connection_status(_: dict = Depends(require_panel_jwt)) -> dict:
    """Check neo-whatsapp gateway connectivity."""
    gateway = WhatsAppGatewayClient()
    try:
        client = await gateway._get_client()
        resp = await client.get("/health")
        return {"ok": resp.status_code == 200, "status": resp.json() if resp.status_code == 200 else {}}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await gateway.close()


@router.websocket("/ws/qr-status")
async def ws_qr_status(websocket: WebSocket, token: str = Query(default="")) -> None:
    """WebSocket endpoint for real-time QR status updates.

    Clients connect once and receive push updates whenever QR state changes.
    Eliminates polling /qr-status every 3 seconds.
    """
    verify_panel_token(token)
    await websocket.accept()
    broadcaster = get_qr_broadcaster()
    try:
        async for status in broadcaster.subscribe():
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json(status)
    except WebSocketDisconnect:
        pass


@router.get("/qr-status")
async def qr_status(_: dict = Depends(require_panel_jwt)) -> dict:
    """Get current QR code and connection state from neo-whatsapp.

    Also pushes update to all WebSocket subscribers.
    """
    broadcaster = get_qr_broadcaster()
    gateway = WhatsAppGatewayClient()
    try:
        client = await gateway._get_client()
        resp = await client.get("/qr")
        if resp.status_code == 200:
            status = resp.json()
            asyncio.create_task(broadcaster.broadcast(status))
            return status
        status = {"qr": None, "state": "unknown", "error": f"HTTP {resp.status_code}"}
        asyncio.create_task(broadcaster.broadcast(status))
        return status
    except Exception as e:
        status = {"qr": None, "state": "unreachable", "error": str(e)}
        asyncio.create_task(broadcaster.broadcast(status))
        return status
    finally:
        await gateway.close()


@router.get("/chats")
async def list_chats(_: dict = Depends(require_panel_jwt)) -> dict:
    """List active WhatsApp chats from the connected phone."""
    gateway = WhatsAppGatewayClient()
    try:
        chats = await gateway.list_chats()
        return {"chats": chats}
    except Exception as e:
        logger.error("Failed to list chats: err=%s", e)
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        await gateway.close()


@router.get("/chats/{jid}/messages")
async def get_chat_messages(jid: str, _: dict = Depends(require_panel_jwt)) -> dict:
    """Get recent messages for a WhatsApp chat JID."""
    gateway = WhatsAppGatewayClient()
    try:
        messages = await gateway.get_chat_messages(jid)
        return {"messages": messages}
    except Exception as e:
        logger.error("Failed to get chat messages: jid=%s err=%s", jid, e)
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        await gateway.close()


@router.get("/chats/{jid}/media/{msg_id}")
async def get_chat_media(jid: str, msg_id: str, _: dict = Depends(require_panel_jwt)) -> dict:
    """Download media for a message from the WhatsApp gateway."""
    gateway = WhatsAppGatewayClient()
    try:
        return await gateway.get_chat_media(jid, msg_id)
    except Exception as e:
        logger.error("Failed to get chat media: jid=%s msg_id=%s err=%s", jid, msg_id, e)
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        await gateway.close()


@router.post("/qr-refresh")
async def qr_refresh(_: dict = Depends(require_panel_jwt)) -> dict:
    """Force neo-whatsapp to generate a new QR code."""
    broadcaster = get_qr_broadcaster()
    gateway = WhatsAppGatewayClient()
    try:
        client = await gateway._get_client()
        resp = await client.post("/qr/refresh", json={})
        result = resp.json()
        asyncio.create_task(broadcaster.broadcast(result))
        return result
    except Exception as e:
        status = {"ok": False, "error": str(e)}
        asyncio.create_task(broadcaster.broadcast(status))
        return status
    finally:
        await gateway.close()
