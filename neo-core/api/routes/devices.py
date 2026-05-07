"""Connected devices routes."""

from fastapi import APIRouter

router = APIRouter(tags=["devices"])


@router.get("/devices")
async def list_connected_devices() -> dict:
    """Return list of devices currently connected via gRPC."""
    # Late import to avoid circular import issues
    from grpc_server import _connected_devices

    all_devices = _connected_devices.list_all()
    return {
        "devices": [
            {
                "device_id": d.get("device_id", ""),
                "device_type": d.get("device_type", "unknown"),
                "model": d.get("model", "unknown"),
                "os_platform": d.get("os_platform", "unknown"),
                "os_version": d.get("os_version", "unknown"),
                "client_version": d.get("client_version", ""),
                "ip_address": d.get("ip_address", ""),
                "connected_at_unix": d.get("connected_at_unix", 0),
                "is_current_device": d.get("is_current_device", False),
            }
            for d in all_devices
        ]
    }