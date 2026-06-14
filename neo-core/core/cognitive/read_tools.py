"""Curated READ-ONLY toolset for the cognitive core + a per-request executor.

The cognitive core (DIRECT_RESPONSE/COGNITIVE) may call these to read/verify
ground truth INLINE (PKS, devices, weather) before answering — never to act.
Side-effecting actions stay behind handoff. See docs/cognitive-agentic-tools.md.

MVP toolset (all globally dispatchable today): search_pks, list_connected_devices,
get_weather. ha_get_state is deferred until a home_assistant module executor is
wired into the global ToolExecutor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from modules.devices.tools import DEVICES_TOOLS
from modules.pks_search.tools import PKS_SEARCH_TOOLS
from modules.weather.tools import WEATHER_TOOLS
from shared import ToolCall, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


def _by_name(tools: list[ToolSchema], name: str) -> ToolSchema:
    return next(t for t in tools if t.name == name)


COGNITIVE_READ_TOOLS: list[ToolSchema] = [
    _by_name(PKS_SEARCH_TOOLS, "search_pks"),
    _by_name(DEVICES_TOOLS, "list_connected_devices"),
    _by_name(WEATHER_TOOLS, "get_weather"),
]

# Tools that need a user to be meaningful — only offered when user_id is known.
_USER_SCOPED = frozenset({"search_pks", "list_connected_devices"})


def cognitive_tools_for(user_id: UUID | None) -> list[ToolSchema]:
    """Read tools the cognitive core may use this turn.

    Without a user, user-scoped tools are withheld (the model can't usefully read
    a specific user's PKS/devices), leaving only global reads like get_weather.
    """
    if user_id is None:
        return [t for t in COGNITIVE_READ_TOOLS if t.name not in _USER_SCOPED]
    return list(COGNITIVE_READ_TOOLS)


class CognitiveReadExecutor:
    """Per-request executor for the cognitive read toolset.

    Builds FRESH, user-scoped executors for the user-bound tools (no shared-state
    race) and delegates everything else to the global ToolExecutor.
    """

    def __init__(self, *, global_executor: Any, db: Any, pks_service: Any, user_id: UUID | None) -> None:
        self._global = global_executor
        self._user_id = user_id
        self._devices: Any = None
        self._pks_search: Any = None
        if user_id is not None:
            from modules.devices import DevicesExecutor
            from modules.pks_search import PKSSearchExecutor

            self._devices = DevicesExecutor(db=db)
            self._devices.set_context(user_id)
            if pks_service is not None:
                self._pks_search = PKSSearchExecutor(pks_service=pks_service)
                self._pks_search.set_context(user_id)

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        name = tool_call.name
        try:
            if name in _USER_SCOPED and self._user_id is None:
                raise ValueError(f"{name} requer um usuário identificado.")
            if name == "list_connected_devices" and self._devices is not None:
                result = await self._devices.execute(name, tool_call.arguments)
            elif name == "search_pks" and self._pks_search is not None:
                result = await self._pks_search.execute(name, tool_call.arguments)
            else:
                # Read-only globals (e.g. get_weather) go through the global executor.
                return await self._global.execute_tool_call(tool_call)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=result,
                execution_time=asyncio.get_event_loop().time() - start,
            )
        except Exception as e:
            logger.warning("Cognitive read tool '%s' failed: %s", name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )
