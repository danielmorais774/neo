"""Connected devices routes."""

from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException

from db.async_client import AsyncPostgresClient

router = APIRouter(tags=["devices"])          # HTTP — requer panel JWT
ws_router = APIRouter(tags=["devices-ws"])   # WebSocket — desktop, sem auth

_db: AsyncPostgresClient | None = None


def set_db(db: AsyncPostgresClient) -> None:
    global _db
    _db = db


@router.get("/devices")
async def list_connected_devices(user_id: str | None = None) -> dict:
    """Return list of devices from DB, with real-time online status from WS registry.

    Online/offline is determined by merging the persisted device records with the
    WebSocket registry (authoritative when a device has an active connection,
    otherwise the DB value is trusted). Shared with the `list_connected_devices`
    agent tool via `modules.devices.service.list_devices_with_status`.
    """
    from modules.devices.service import list_devices_with_status

    uid_uuid = None
    if user_id:
        try:
            uid_uuid = UUID(user_id)
        except ValueError:
            pass

    devices = await list_devices_with_status(_db, uid_uuid)
    return {"devices": devices}


@router.get("/devices/resolve")
async def resolve_device_by_hardware_id(
    user_id: str,
    hardware_id: str,
) -> dict:
    """Resolve the canonical device_id for a user/hardware_id pair."""
    if not _db:
        raise HTTPException(status_code=503, detail="DB unavailable")

    try:
        uid_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    normalized_hardware_id = hardware_id.strip().upper()
    if not normalized_hardware_id:
        raise HTTPException(status_code=400, detail="hardware_id is required")

    device = await _db.get_device_by_hardware_id(uid_uuid, normalized_hardware_id)
    return {
        "device_id": device.device_id if device else None,
        "found": device is not None,
    }


@router.post("/devices/{device_id}/send-notification")
async def send_test_notification(device_id: str) -> dict:
    """Send a test notification to a device via its active WebSocket connection.

    The device must have an active /ws/devices/notifications/{device_id} connection.
    """
    from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

    broadcaster = get_device_notif_broadcaster()

    notification = {
        "kind": "test_notification",
        "title": "Test Notification",
        "body": f"This is a test notification sent to device {device_id}",
    }

    sent = await broadcaster.send_notification(device_id, notification)
    if not sent:
        raise HTTPException(status_code=503, detail="Device not connected to notifications WS")

    return {"ok": True, "sent_to": device_id}


@router.post("/debug/test-notification")
async def debug_send_notification(notification: dict) -> dict:
    """Send a full notification payload to a device via WebSocket.

    Accepts the complete notification shape with actions, fields, config, etc.
    Device is identified by 'device_id' in the notification body.
    """
    from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

    broadcaster = get_device_notif_broadcaster()

    device_id = notification.get("device_id", "test-device")
    sent = await broadcaster.send_notification(device_id, notification)
    if not sent:
        raise HTTPException(status_code=503, detail="Device not connected to notifications WS")

    return {"ok": True, "sent_to": device_id}


@ws_router.websocket("/ws/devices/online")
async def ws_device_online(websocket: WebSocket, device_id: str = Query(...)):
    """WebSocket endpoint for device online/offline signaling.

    Connection open = online, disconnection = offline (immediate).
    No heartbeats needed — TCP state is the source of truth.
    """
    await websocket.accept()
    # Late import to avoid circular import issues
    from grpc_server import _devices_ws_registry

    try:
        # Pass the WS connection ID so we can track it in the DB
        await _devices_ws_registry.register(device_id, str(id(websocket)))
        try:
            while True:
                # Wait for any message (or just connection close)
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
    finally:
        await _devices_ws_registry.unregister(device_id)


@ws_router.websocket("/ws/devices/notifications/{device_id}")
async def ws_device_notifications(websocket: WebSocket, device_id: str):
    """WebSocket endpoint for receiving notifications on a device.

    Desktop connects here to receive push notifications from neo-core.
    Uses device_id as the connection key (same as /ws/devices/online).
    """
    await websocket.accept()
    from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

    broadcaster = get_device_notif_broadcaster()
    try:
        async for notification in broadcaster.register(device_id):
            await websocket.send_json(notification)
    except WebSocketDisconnect:
        pass
