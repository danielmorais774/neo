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
    state_satisfies_spec,
    summarize_state,
    verify_spec,
)
from shared import ToolCall, ToolResult

logger = logging.getLogger(__name__)

# Cap on entities returned to the LLM to keep the context small.
_MAX_ENTITIES = 40

# Verification is driven by ``verify_spec`` (core/skills/home_assistant/service.py):
# HA's service-call response only lists states that changed synchronously — devices
# that confirm asynchronously return an empty list even on success, which made the
# agent report a false failure. We re-read the entity instead and confirm against
# the spec (including non-"on/off" states like climate hvac_mode and attribute
# targets like the requested temperature).

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

    @staticmethod
    def _is_concrete_entity_id(entity_id: str | None) -> bool:
        """True for a single, concrete ``domain.object_id`` — not a list, entity
        group or keyword (``all``/``none``), which we don't pre-validate."""
        if not isinstance(entity_id, str):
            return False
        eid = entity_id.strip()
        if not eid or "," in eid or eid in {"all", "none"}:
            return False
        return eid.count(".") == 1

    async def _entity_exists(self, entity_id: str) -> bool | None:
        """Whether ``entity_id`` is in HA's live roster. ``None`` when the roster
        can't be fetched, so the caller fails open (an infra blip must not block a
        real command)."""
        try:
            states = await self.adapter.get_states_cached(8.0)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("HA entity validation fetch failed for %s: %s", entity_id, e)
            return None
        if not states:
            return None
        return any(s.get("entity_id") == entity_id for s in states)

    async def _call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None,
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # Guard: never fire a service at an invented entity_id. HA's REST API answers
        # 200 even when the target entity does not exist (it just affects nothing), so
        # a hallucinated id like ``light.quarto`` silently no-ops while looking like a
        # success. Validate a single concrete id against the live roster and bounce the
        # agent back to ha_list_entities. (Multi/group targets and unfetchable rosters
        # fail open — they proceed to the real call.)
        if self._is_concrete_entity_id(entity_id):
            exists = await self._entity_exists(entity_id)  # type: ignore[arg-type]
            if exists is False:
                logger.warning("HA call_service blocked: unknown entity_id %s", entity_id)
                return {
                    "ok": False,
                    "unknown_entity": True,
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id,
                    "error": (
                        f"O entity_id '{entity_id}' não existe no Home Assistant — o "
                        "comando NÃO foi enviado. Chame ha_list_entities para achar o "
                        "entity_id correto e refaça a ação."
                    ),
                }

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
            spec = verify_spec(domain, service, payload)
            state, summary, verified = await self._verify_resulting_state(entity_id, spec)
            result["resulting_state"] = state
            result["resulting_summary"] = summary
            if spec is not None:
                result["expected"] = {"kind": spec[0], "target": spec[1]}
                result["verified"] = verified
        return result

    async def _verify_resulting_state(
        self, entity_id: str, spec: tuple[str, Any] | None
    ) -> tuple[str | None, str | None, bool | None]:
        """Re-read ``entity_id`` after an action, polling to absorb HA's eventual
        consistency. Returns ``(state, human_summary, verified)``. Early-exits once
        the spec is satisfied (or after one read when there is no spec).

        ``verified`` is ``None`` ONLY when there is no spec to check. When a spec
        exists but we could never read the entity back (e.g. it 404s), that is a
        FAILURE, not "unknown": we return ``verified=False`` so the agent never
        reports a phantom success. A permanent 4xx stops polling early; transient
        errors retry across the schedule.
        """
        state: str | None = None
        summary: str | None = None
        verified: bool | None = None
        read_ok = False
        for delay in _VERIFY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                entity = await self.adapter.get_state(entity_id)
            except ValueError as e:
                # 4xx (e.g. 404 entity-not-found) is permanent — stop polling.
                logger.warning("HA verify read failed for %s: %s", entity_id, e)
                break
            except Exception as e:  # pragma: no cover - defensive
                # Transient (network/5xx already retried in the adapter) — try again.
                logger.warning("HA verify read error for %s: %s", entity_id, e)
                continue
            read_ok = True
            state = entity.get("state")
            summary = summarize_state(entity)
            if spec is None:
                break
            verified = state_satisfies_spec(entity, spec)
            if verified:
                break
        if spec is not None and not read_ok:
            # Asked to verify a concrete expectation but never got a state back →
            # unconfirmed/failed, never a success.
            verified = False
        return state, summary, verified

    async def _rename_entity(self, entity_id: str, name: str) -> dict[str, Any]:
        entry = await self.adapter.rename_entity(entity_id, name)
        return {
            "ok": True,
            "entity_id": entity_id,
            "new_name": name or "(padrão da integração)",
            "entry": entry,
        }
