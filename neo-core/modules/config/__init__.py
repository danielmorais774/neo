"""Config module — generic, registry-driven settings for Neo.

A new adjustable setting is a new :class:`ConfigSpec` in
:mod:`modules.config.specs` (no new tool/executor). The ``config_*`` tools edit any
registered setting, persisting into ``user_preference``. The first batch covers TTS
(``tts.voice`` / ``tts.engine``), which share the ``neo-voice/tts/active`` row that
``modules.voice.get_active_voice`` reads (Pattern A unchanged).
"""

from modules.config.executor import ConfigExecutor
from modules.config.registry import ConfigRegistry, ConfigSpec
from modules.config.specs import build_default_registry
from modules.config.tools import CONFIG_TOOLS

__all__ = [
    "CONFIG_TOOLS",
    "ConfigExecutor",
    "ConfigRegistry",
    "ConfigSpec",
    "build_default_registry",
]
