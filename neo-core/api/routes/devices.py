"""Connected devices routes."""

from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException

from db.async_client import AsyncPostgresClient

router = APIRouter(tags=["devices"])

_db: AsyncPostgresClient | None = None


def set_db(db: AsyncPostgresClient) -> None:
    global _db
    _db = db


@router.get("/devices")
async def list_connected_devices(user_id: str | None = None) -> dict:
    """Return list of devices from DB, with real-time online status from WS registry.

    Online/offline is determined exclusively by the WebSocket registry:
    - WS connected = online (device opened /ws/devices/online)
    - WS disconnected = offline (registry entry removed immediately)
    gRPC registry is no longer used for online status.
    """
    # Late import to avoid circular import issues
    from grpc_server import _devices_ws_registry

    uid_uuid = None
    if user_id:
        try:
            uid_uuid = UUID(user_id)
        except ValueError:
            pass

    # Get devices from DB (persisted device records)
    db_devices = await _db.get_connected_devices(uid_uuid) if _db else []

    # Get real-time online set from WS registry
    online_ws = await _devices_ws_registry.list_online()
    ws_online_set = set(online_ws)

    devices = []
    for dev in db_devices:
        # Use WS registry only when device has an active WS connection (authoritative).
        # When no WS connection exists, trust the DB value (last known good state).
        if dev.device_id in ws_online_set:
            is_online = True
        else:
            is_online = dev.is_online
        devices.append({
            "device_id": dev.device_id,
            "device_type": dev.device_type,
            "model": dev.model,
            "os_platform": dev.os_platform,
            "os_version": dev.os_version,
            "client_version": dev.client_version,
            "ip_address": dev.ip_address,
            "connected_at_unix": int(dev.created_at.timestamp()) if dev.created_at else 0,
            "is_current_device": False,
            "is_online": is_online,
            "ws_connection_id": dev.ws_connection_id,
        })

    return {"devices": devices}


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


@router.websocket("/ws/devices/online")
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


@router.websocket("/ws/devices/notifications/{device_id}")
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