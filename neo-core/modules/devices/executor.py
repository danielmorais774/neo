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
from modules.devices.service import list_devices_with_status
from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class DevicesExecutor:
    """Executes the `list_connected_devices` tool."""

    def __init__(self, db: AsyncPostgresClient) -> None:
        self.db = db
        self._user_id: UUID | None = None

    def set_context(self, user_id: UUID | None) -> None:
        """Scope subsequent executions to a single user."""
        self._user_id = user_id

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a devices tool.

        Args:
            tool_name: Tool name (currently only 'list_connected_devices').
            arguments: Tool arguments (optional 'only_online' boolean).

        Returns:
            Dict with the device list and count.
        """
        if tool_name != "list_connected_devices":
            raise ValueError(f"Unknown devices tool: {tool_name}")

        only_online = bool(arguments.get("only_online", False))
        devices = await list_devices_with_status(
            self.db, user_id=self._user_id, only_online=only_online
        )
        return {"devices": devices, "count": len(devices)}


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
    ) -> None:
        self._global_executor = global_executor
        self._devices_executor = devices_executor
        self._devices_executor.set_context(user_id)

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Adapter expected by AgentExecutor._execute_tools."""
        if tool_call.name != "list_connected_devices":
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
            logger.exception("list_connected_devices execution failed")
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start_time,
            )
