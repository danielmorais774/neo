"""Built-in :class:`ConfigSpec`s and the default registry.

First batch (per the plan): the two TTS settings. Both persist into the SAME
``neo-voice/tts/active`` row that :func:`modules.voice.get_active_voice` reads, so
Pattern A (voice carried on the final reply) keeps working with zero changes to the
renderer. Adding a future setting = append a ``ConfigSpec`` here.
"""

from __future__ import annotations

from typing import Any

from modules.config.registry import ConfigRegistry, ConfigSpec
from modules.voice.catalog import BACKENDS, VoiceCatalog
from modules.voice.executor import PREF_CATEGORY, PREF_KEY, PREF_SERVICE
from shared.config import Settings, get_settings


def _tts_voice_spec(catalog: VoiceCatalog) -> ConfigSpec:
    """``tts.voice`` — pick the speaker by name from the live neo-voice catalog."""

    def apply(_current: dict, match: dict[str, Any]) -> dict[str, Any]:
        # A resolved catalog entry fully defines the active voice (mirrors the old
        # voice_set): engine + speaker + language + friendly label.
        return {
            "engine": match["engine"],
            "voice": match["voice"],
            "language": match.get("language"),
            "label": match["label"],
        }

    return ConfigSpec(
        key="tts.voice",
        label="Voz do TTS",
        description=(
            "A voz que o Neo usa para FALAR (speaker do TTS). Aceita um nome "
            "aproximado (ex.: 'jarvis', 'Ryan', 'Vivian') resolvido contra o "
            "catálogo do neo-voice (vozes embutidas + clones)."
        ),
        aliases=["voz", "voice", "speaker", "voz do neo", "voz da fala"],
        service=PREF_SERVICE,
        category=PREF_CATEGORY,
        pref_key=PREF_KEY,
        field="label",          # the human-readable current value is the label
        scope="user",
        value_type="dynamic",
        options_provider=catalog.fetch,
        resolver=catalog.resolve,
        apply=apply,
        default=None,           # None → engine picks its default speaker
        describe=lambda m: str(m.get("label") or m.get("voice")) if isinstance(m, dict) else str(m),
    )


def _tts_engine_spec(settings: Settings) -> ConfigSpec:
    """``tts.engine`` — switch the TTS backend (resets the speaker to its default)."""

    def apply(_current: dict, engine: str) -> dict[str, Any]:
        # Switching engine resets the speaker: a voice from another engine wouldn't
        # exist here, so let the new engine pick its own default (voice=None).
        return {"engine": engine, "voice": None, "language": None, "label": None}

    return ConfigSpec(
        key="tts.engine",
        label="Engine de TTS",
        description=(
            "O motor de TTS que o Neo usa para falar. Trocar o engine reseta a voz "
            "para o speaker padrão desse motor."
        ),
        aliases=["engine", "motor", "motor de voz", "engine de voz", "backend"],
        service=PREF_SERVICE,
        category=PREF_CATEGORY,
        pref_key=PREF_KEY,
        field="engine",
        scope="user",
        value_type="enum",
        enum=list(BACKENDS),
        apply=apply,
        default_factory=lambda: settings.neo_voice_default_engine,
    )


def build_default_registry(
    settings: Settings | None = None, catalog: VoiceCatalog | None = None
) -> ConfigRegistry:
    """Registry seeded with the built-in settings. One shared VoiceCatalog so the
    TTS specs reuse the same cached neo-voice catalog (``catalog`` lets tests inject
    a pre-seeded one to skip HTTP)."""
    settings = settings or get_settings()
    catalog = catalog or VoiceCatalog(settings)
    registry = ConfigRegistry()
    registry.register_many([
        _tts_voice_spec(catalog),
        _tts_engine_spec(settings),
    ])
    return registry
