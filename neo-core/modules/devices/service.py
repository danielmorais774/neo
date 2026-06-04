"""Shared helper — list connected devices merged with real-time WS status.

Single source of truth for the device-listing logic used by both the
`GET /devices` HTTP route and the `list_connected_devices` agent tool.

Online/offline is determined by merging the persisted `connected_device`
records with the authoritative WebSocket registry:
- device present in the WS registry  → online (active /ws/devices/online)
- device absent from the WS registry  → trust the DB `is_online` (last known state)
"""

from uuid import UUID
from typing import Any

from db.async_client import AsyncPostgresClient


async def list_devices_with_status(
    db: AsyncPostgresClient | None,
    user_id: UUID | None = None,
    only_online: bool = False,
) -> list[dict[str, Any]]:
    """Return device records (optionally scoped to a user) with live online status.

    Args:
        db: Async Postgres client (may be None → returns empty list).
        user_id: When provided, only that user's devices are returned.
        only_online: When True, filters out devices that are not currently online.

    Returns:
        List of device dicts (same shape as the GET /devices route).
    """
    # Late import to avoid circular import with grpc_server (matches api/routes/devices.py)
    from grpc_server import _devices_ws_registry

    db_devices = await db.get_connected_devices(user_id) if db else []

    online_ws = await _devices_ws_registry.list_online()
    ws_online_set = set(online_ws)

    devices: list[dict[str, Any]] = []
    for dev in db_devices:
        # WS registry is authoritative when the device has an active connection;
        # otherwise trust the DB value (last known good state).
        if dev.device_id in ws_online_set:
            is_online = True
        else:
            is_online = dev.is_online

        if only_online and not is_online:
            continue

        devices.append({
            "device_id": dev.device_id,
            "device_type": dev.device_type,
            "model": dev.model,
            "hardware_id": dev.hardware_id,
            "os_platform": dev.os_platform,
            "os_version": dev.os_version,
            "client_version": dev.client_version,
            "ip_address": dev.ip_address,
            "connected_at_unix": int(dev.created_at.timestamp()) if dev.created_at else 0,
            "is_current_device": False,
            "is_online": is_online,
            "ws_connection_id": dev.ws_connection_id,
        })

    return devices
