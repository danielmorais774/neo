"""Devices tool executor — lists devices connected to neo-core.

Two pieces:

- ``DevicesExecutor`` implements the standard module-executor contract
  (``execute(tool_name, arguments)``) so it can be registered in the global
  ``ToolExecutor`` as a fallback (no user scoping). It also supports a
  per-request ``set_context(user_id)`` to scope results to a single user.

- ``DevicesContextExecutor`` is a thin per-request wrapper injected into
  ``AgentExecutor.execute(tool_executor=...)``. It intercepts
  ``list_connected_devices`` to inject the session's ``user_id`` and delegates
  every other tool call to the global executor unchanged. This reuses the same
  seam the WhatsApp skill uses (see
  ``core/skills/whatsapp_messaging/executor.py``) and avoids changing the
  generic ``ToolExecutorProtocol`` contract.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from db.async_client import AsyncPostgresClient
from modules.devices.control import execute_device_action
from modules.devices.service import list_devices_with_status
from modules.devices.tools import DEVICE_CONTROL_TOOL_NAMES
from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)

# Tools que este executor intercepta (o resto cai no executor global).
_DEVICES_TOOL_NAMES: frozenset[str] = frozenset(
    {"list_connected_devices"}
) | DEVICE_CONTROL_TOOL_NAMES


class DevicesExecutor:
    """Executes `list_connected_devices`, `device_control` and `device_power`."""

    def __init__(self, db: AsyncPostgresClient) -> None:
        self.db = db
        self._user_id: UUID | None = None
        self._current_device_id: str | None = None

    def set_context(
        self, user_id: UUID | None, current_device_id: str | None = None
    ) -> None:
        """Scope subsequent executions to a single user (and its current device).

        ``current_device_id`` vem de ``metadata["device_id"]`` da conexão — é o
        que permite "abaixa o volume" sem citar aparelho nenhum significar *este*
        aparelho. Sem ele a resolução ainda funciona, mas só quando existe
        exatamente um device com canal aberto.
        """
        self._user_id = user_id
        self._current_device_id = current_device_id

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a devices tool.

        Args:
            tool_name: 'list_connected_devices', 'device_control' or 'device_power'.
            arguments: Tool arguments.

        Returns:
            Dict with the tool result.
        """
        if tool_name == "list_connected_devices":
            only_online = bool(arguments.get("only_online", False))
            devices = await list_devices_with_status(
                self.db, user_id=self._user_id, only_online=only_online
            )
            return {"devices": devices, "count": len(devices)}

        if tool_name in DEVICE_CONTROL_TOOL_NAMES:
            if self._user_id is None:
                raise ValueError(f"{tool_name} requer um usuário identificado.")
            action = str(arguments.get("action") or "").strip()
            # `device_power` não aceita alvo implícito: desligar a máquina errada
            # é irreversível de um jeito que abaixar o volume não é.
            device_name = arguments.get("device")
            if tool_name == "device_power" and not (device_name or "").strip():
                return {
                    "ok": False,
                    "error": "Diga qual dispositivo — não escolho sozinho para desligar/suspender.",
                }
            return await execute_device_action(
                self.db,
                self._user_id,
                action=action,
                device_name=device_name,
                value=arguments.get("value"),
                current_device_id=self._current_device_id,
            )

        raise ValueError(f"Unknown devices tool: {tool_name}")


class DevicesContextExecutor:
    """Per-request executor that scopes `list_connected_devices` to a user.

    Acts as the ``tool_executor`` for a single ``AgentExecutor.execute`` call:
    intercepts the devices tool (injecting ``user_id``) and delegates all other
    tool calls to the global executor.
    """

    def __init__(
        self,
        global_executor: Any,
        devices_executor: DevicesExecutor,
        user_id: UUID | None,
        current_device_id: str | None = None,
    ) -> None:
        self._global_executor = global_executor
        self._devices_executor = devices_executor
        self._devices_executor.set_context(user_id, current_device_id)

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Adapter expected by AgentExecutor._execute_tools."""
        if tool_call.name not in _DEVICES_TOOL_NAMES:
            return await self._global_executor.execute_tool_call(tool_call)

        start_time = asyncio.get_event_loop().time()
        try:
            result = await self._devices_executor.execute(
                tool_call.name, tool_call.arguments
            )
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=result,
                execution_time=asyncio.get_event_loop().time() - start_time,
            )
        except Exception as e:
            logger.exception("%s execution failed", tool_call.name)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start_time,
            )
