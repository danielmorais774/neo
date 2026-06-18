"""Voice module — server-side READ-ONLY tools to inspect TTS voices.

The active selection is persisted in ``user_preference`` (service="neo-voice") and
read back at response time via :func:`get_active_voice` so the final agent reply
carries the voice/engine to whoever renders the audio. *Setting* the active voice
goes through ``modules/config`` (settings ``tts.voice`` / ``tts.engine``), which
reuses :class:`VoiceCatalog` for the catalog + name resolution.
"""

from modules.voice.catalog import VoiceCatalog
from modules.voice.executor import VoiceExecutor, get_active_voice
from modules.voice.tools import VOICE_TOOLS

__all__ = ["VOICE_TOOLS", "VoiceCatalog", "VoiceExecutor", "get_active_voice"]
