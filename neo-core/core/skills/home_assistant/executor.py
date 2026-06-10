"""Home Assistant tool executor — handles tool calls during the agent loop.

Implements the executor contract expected by ``AgentExecutor`` (``execute_tool_call``)
plus a plain ``execute(tool_name, arguments)`` for direct/testing use. All calls
delegate to the :class:`HomeAssistantAdapter`. Actions execute immediately (no
approval flow) per the skill design.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from core.skills.home_assistant.adapter import HomeAssistantAdapter
from core.skills.home_assistant.service import (
    compact_entity,
    filter_entities,
    resolve_entity,
    summarize_state,
)
from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)

# Cap on entities returned to the LLM to keep the context small.
_MAX_ENTITIES = 40

# Expected resulting state per (domain, service) for on/off-style actions, used to
# VERIFY the action after calling it. HA's service-call response only lists states
# that changed synchronously — devices that confirm asynchronously return an empty
# list even on success, which made the agent report a false failure. We re-read the
# entity instead and report the real resulting state.
_EXPECTED_STATE: dict[tuple[str, str], str] = {
    ("light", "turn_on"): "on",
    ("light", "turn_off"): "off",
    ("switch", "turn_on"): "on",
    ("switch", "turn_off"): "off",
    ("fan", "turn_on"): "on",
    ("fan", "turn_off"): "off",
    ("input_boolean", "turn_on"): "on",
    ("input_boolean", "turn_off"): "off",
    ("cover", "open_cover"): "open",
    ("cover", "close_cover"): "closed",
}

# Poll schedule (seconds) for the post-action state read. Early-exits as soon as
# the expected state is observed; total budget ~1.8s for the worst case. Module-level
# so tests can shrink it.
_VERIFY_DELAYS: tuple[float, ...] = (0.3, 0.4, 0.5, 0.6)


class HomeAssistantExecutor:
    """Executes tool calls for the home_assistant skill."""

    def __init__(self, adapter: HomeAssistantAdapter) -> None:
        self.adapter = adapter
        self._user_id: UUID | None = None

    def set_context(self, user_id: UUID | None) -> None:
        """Scope subsequent executions to a single user (reserved for future use)."""
        self._user_id = user_id

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Adapter expected by AgentExecutor._execute_tools."""
        start_time = asyncio.get_event_loop().time()
        try:
            result = await self.execute(tool_call.name, tool_call.arguments)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=result,
                execution_time=asyncio.get_event_loop().time() - start_time,
            )
        except Exception as e:
            logger.warning("HA tool '%s' failed: %s", tool_call.name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start_time,
            )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "ha_list_entities":
            return await self._list_entities(
                arguments.get("domain"),
                arguments.get("query"),
            )
        if tool_name == "ha_get_state":
            return await self._get_state(arguments["entity_id"])
        if tool_name == "ha_call_service":
            return await self._call_service(
                arguments["domain"],
                arguments["service"],
                arguments.get("entity_id"),
                arguments.get("data"),
            )
        if tool_name == "ha_rename_entity":
            return await self._rename_entity(
                arguments["entity_id"],
                arguments.get("name", ""),
            )
        raise ValueError(f"Unknown home_assistant tool: {tool_name}")

    async def _list_entities(
        self,
        domain: str | None,
        query: str | None,
    ) -> dict[str, Any]:
        states = await self.adapter.get_states()
        matched = filter_entities(states, domain=domain, query=query)
        truncated = len(matched) > _MAX_ENTITIES
        entities = [compact_entity(e) for e in matched[:_MAX_ENTITIES]]
        return {
            "entities": entities,
            "count": len(entities),
            "total_matched": len(matched),
            "truncated": truncated,
        }

    async def _get_state(self, entity_id: str) -> dict[str, Any]:
        entity = await self.adapter.get_state(entity_id)
        return {
            "entity": compact_entity(entity),
            "summary": summarize_state(entity),
            "attributes": entity.get("attributes") or {},
        }

    async def _call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None,
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = dict(data or {})
        if entity_id:
            payload["entity_id"] = entity_id
        changed = await self.adapter.call_service(domain, service, payload)

        # ``ok`` means HA ACCEPTED the command (HTTP 2xx). It is NOT proof the
        # device changed — and ``changed`` is often empty for async-confirming
        # devices. So we re-read the target entity and report the real resulting
        # state; the agent confirms/denies based on that, never on changed_count.
        result: dict[str, Any] = {
            "ok": True,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "changed_count": len(changed),
        }
        if isinstance(entity_id, str) and entity_id:
            expected = _EXPECTED_STATE.get((domain, service))
            state, summary = await self._verify_resulting_state(entity_id, expected)
            result["resulting_state"] = state
            result["resulting_summary"] = summary
            if expected is not None:
                result["expected_state"] = expected
                result["verified"] = (state == expected)
        return result

    async def _verify_resulting_state(
        self, entity_id: str, expected: str | None
    ) -> tuple[str | None, str | None]:
        """Re-read ``entity_id`` after an action, polling to absorb HA's eventual
        consistency. Returns ``(state, human_summary)``. Early-exits once the
        expected state is observed (or after one read when there is no expectation).
        """
        state: str | None = None
        summary: str | None = None
        for delay in _VERIFY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                entity = await self.adapter.get_state(entity_id)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("HA verify read failed for %s: %s", entity_id, e)
                break
            state = entity.get("state")
            summary = summarize_state(entity)
            if expected is None or state == expected:
                break
        return state, summary

    async def _rename_entity(self, entity_id: str, name: str) -> dict[str, Any]:
        entry = await self.adapter.rename_entity(entity_id, name)
        return {
            "ok": True,
            "entity_id": entity_id,
            "new_name": name or "(padrão da integração)",
            "entry": entry,
        }
