"""Devices module — listagem e CONTROLE dos dispositivos do usuário."""

from modules.devices.control import (
    CONTROL_ACTIONS,
    POWER_ACTIONS,
    DeviceResolutionError,
    execute_device_action,
    resolve_device,
)
from modules.devices.executor import DevicesExecutor, DevicesContextExecutor
from modules.devices.service import list_devices_with_status
from modules.devices.tools import (
    DEVICE_CONTROL_TOOL_NAMES,
    DEVICE_CONTROL_TOOLS,
    DEVICES_TOOLS,
)

__all__ = [
    "DEVICES_TOOLS",
    "DEVICE_CONTROL_TOOLS",
    "DEVICE_CONTROL_TOOL_NAMES",
    "CONTROL_ACTIONS",
    "POWER_ACTIONS",
    "DeviceResolutionError",
    "DevicesExecutor",
    "DevicesContextExecutor",
    "execute_device_action",
    "list_devices_with_status",
    "resolve_device",
]
