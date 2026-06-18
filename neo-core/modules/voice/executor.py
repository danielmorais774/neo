"""Voice tool executor — read-only introspection of TTS voices + active selection.

The active voice (engine + speaker) is *set* through the generic config layer
(``modules/config``, setting ``tts.voice`` / ``tts.engine``), which persists it in
``user_preference`` (service="neo-voice", category="tts", key="active"). This module
keeps the READ side: ``voice_list`` (catalog) and ``voice_get`` (active), plus
:func:`get_active_voice`, the Pattern-A reader the final reply uses to tell the
renderer which voice to speak with. Catalog/resolve live in :mod:`modules.voice.catalog`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from modules.voice.catalog import VoiceCatalog, normalize
from shared import ToolCall, ToolResult
from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Preference coordinates for the active voice selection (shared with modules.config).
PREF_SERVICE = "neo-voice"
PREF_CATEGORY = "tts"
PREF_KEY = "active"


def _default_voice(settings: Settings) -> dict[str, Any]:
    """The fallback active voice when nothing is persisted yet."""
    return {
        "engine": settings.neo_voice_default_engine,
        "voice": None,       # None → engine picks its own default speaker
        "language": None,    # None → engine picks its own default language
        "label": None,
    }


async def get_active_voice(db: Any, user_id: UUID | None, settings: Settings | None = None) -> dict[str, Any]:
    """Return the active voice config ``{engine, voice, language, label}``.

    Reads the persisted ``neo-voice/tts/active`` preference for ``user_id`` (or the
    global one when ``user_id`` is None). Falls back to engine defaults when absent.
    """
    settings = settings or get_settings()
    try:
        prefs = await db.get_preferences(user_id=user_id, service=PREF_SERVICE, category=PREF_CATEGORY)
        for pref in prefs:
            if pref.key == PREF_KEY:
                data = json.loads(pref.value)
                # Backfill missing keys with defaults so callers get a stable shape.
                return {**_default_voice(settings), **data}
    except Exception as e:  # pragma: no cover - defensive (don't break a reply on a read miss)
        logger.warning("get_active_voice failed: %s", e)
    return _default_voice(settings)


class VoiceExecutor:
    """Executes the read-only voice_* tools: list catalog, read active selection."""

    def __init__(self, db: Any, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self._user_id: UUID | None = None
        self.catalog = VoiceCatalog(self.settings)

    def set_context(self, user_id: UUID | None) -> None:
        """Scope the active-voice read to a single user (None → global preference)."""
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
            logger.warning("voice tool '%s' failed: %s", tool_call.name, e)
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                result=None,
                error=str(e),
                execution_time=asyncio.get_event_loop().time() - start,
            )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "voice_list":
            return await self._list(arguments.get("engine"))
        if tool_name == "voice_get":
            return await self._get()
        raise ValueError(f"Unknown voice tool: {tool_name}")

    # -- tools -----------------------------------------------------------------
    async def _list(self, engine: str | None) -> dict[str, Any]:
        entries = await self.catalog.fetch()
        if engine:
            entries = [e for e in entries if e["engine"] == normalize(engine)]
        return {"voices": self.catalog.compact(entries), "count": len(entries)}

    async def _get(self) -> dict[str, Any]:
        active = await get_active_voice(self.db, self._user_id, self.settings)
        return {"active": active}
