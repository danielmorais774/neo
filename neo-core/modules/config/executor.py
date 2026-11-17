"""Generic config executor — drives every setting off the :class:`ConfigRegistry`.

``config_set`` resolves + validates + persists a registered setting; ``config_get``
reads the current value (with its options); ``config_list`` enumerates what is
configurable. All persistence goes through ``user_preference`` (no migration). Only
whitelisted specs are writable — there is no arbitrary key/value path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from modules.config.registry import ConfigRegistry, ConfigSpec
from shared import ToolCall, ToolResult
from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)


class ConfigExecutor:
    """Executes config_* tool calls against a :class:`ConfigRegistry`."""

    def __init__(self, db: Any, registry: ConfigRegistry, settings: Settings | None = None) -> None:
        self.db = db
        self.registry = registry
        self.settings = settings or get_settings()
        self._user_id: UUID | None = None

    def set_context(self, user_id: UUID | None) -> None:
        """Scope user-scoped settings to this user (global specs ignore it)."""
        self._user_id = user_id

    # -- AgentExecutor contract ------------------------------------------------
    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        try:
            result = await self.execute(tool_call.name, tool_call.arguments)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=result,
                execution_time=asyncio.get_event_loop().time() - start,
            )
        except Exception as e:
            logger.warning("config tool '%s' failed: %s", tool_call.name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "config_set":
            return await self._set(arguments.get("setting", ""), arguments.get("value", ""))
        if tool_name == "config_get":
            return await self._get(arguments.get("setting", ""))
        if tool_name == "config_list":
            return await self._list()
        raise ValueError(f"Unknown config tool: {tool_name}")

    # -- persistence helpers ---------------------------------------------------
    def _uid_for(self, spec: ConfigSpec) -> UUID | None:
        return None if spec.scope == "global" else self._user_id

    async def _read_value(self, spec: ConfigSpec) -> Any | None:
        """Parsed stored value for a spec's row, or None when absent."""
        try:
            prefs = await self.db.get_preferences(
                user_id=self._uid_for(spec), service=spec.service, category=spec.category
            )
            for p in prefs:
                if p.key == spec.pref_key:
                    return json.loads(p.value)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("config read '%s' failed: %s", spec.key, e)
        return None

    def _current(self, spec: ConfigSpec, stored: Any | None) -> Any:
        """Current effective value of a spec (field of the blob, with default)."""
        if spec.field is not None:
            val = stored.get(spec.field) if isinstance(stored, dict) else None
        else:
            val = stored
        return val if val is not None else spec.current_default()

    async def _options_view(self, spec: ConfigSpec) -> list[str] | None:
        """Option labels for display (compacts dynamic dict options)."""
        try:
            opts = await spec.options()
        except Exception as e:
            logger.info("config options for '%s' unavailable: %s", spec.key, e)
            return None
        if opts is None:
            return None
        return [spec.display(o) if isinstance(o, dict) else str(o) for o in opts]

    # -- tools -----------------------------------------------------------------
    async def _list(self) -> dict[str, Any]:
        items = []
        for spec in self.registry.all():
            stored = await self._read_value(spec)
            items.append({
                "setting": spec.key,
                "label": spec.label,
                "description": spec.description,
                "scope": spec.scope,
                "type": spec.value_type,
                "current": self._current(spec, stored),
                "options": await self._options_view(spec),
            })
        return {"settings": items, "count": len(items)}

    async def _get(self, setting: str) -> dict[str, Any]:
        spec = self.registry.match(setting)
        if spec is None:
            return {"ok": False, "error": f"Configuração '{setting}' não existe.",
                    "available": [s.key for s in self.registry.all()]}
        stored = await self._read_value(spec)
        return {
            "ok": True,
            "setting": spec.key,
            "label": spec.label,
            "current": self._current(spec, stored),
            "options": await self._options_view(spec),
        }

    async def _set(self, setting: str, value: str) -> dict[str, Any]:
        setting = (setting or "").strip()
        value = (value or "").strip()
        spec = self.registry.match(setting)
        if spec is None:
            return {"ok": False, "error": f"Configuração '{setting}' não existe.",
                    "available": [s.key for s in self.registry.all()]}
        if not value:
            return {"ok": False, "error": f"Valor vazio para '{spec.key}'."}

        try:
            options = await spec.options()
        except Exception as e:
            return {"ok": False, "error": f"Não foi possível listar opções de '{spec.key}': {e}"}

        resolved = spec.resolve(value, options)
        if resolved is None:
            return {
                "ok": False,
                "error": f"Não encontrei um valor parecido com '{value}' para '{spec.label}'.",
                "available": await self._options_view(spec),
            }

        stored = await self._read_value(spec)
        current_blob = stored if isinstance(stored, dict) else {}
        new_value = spec.merge(current_blob, resolved)

        await self.db.upsert_preference(
            service=spec.service,
            category=spec.category,
            key=spec.pref_key,
            value=json.dumps(new_value, ensure_ascii=False),
            user_id=self._uid_for(spec),
            source_message=f"config_set: {spec.key}={value}",
            metadata={"setting": spec.key},
        )
        if spec.on_set is not None:
            try:
                spec.on_set()
            except Exception:  # pragma: no cover - hook must not break the set
                logger.warning("config on_set hook for '%s' failed", spec.key, exc_info=True)

        display = spec.display(resolved)
        logger.info("config '%s' -> %s", spec.key, display)
        return {"ok": True, "setting": spec.key, "label": spec.label,
                "value": display, "stored": new_value}
