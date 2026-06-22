"""Music skill manifest (Spotify/YouTube playback).

NOTE: this skill is **handoff-only** — the router never fast-paths to it. Music
requests go to the cognitive core (which reads the PKS) and it hands off here. The
manifest is still registered so it appears in the generic handoff instruction and
the skill registry. See neo-music-feature.
"""

from core.skills.manifest import SkillManifest
from shared.types import IntentType

music_manifest = SkillManifest(
    name="music",
    description=(
        "Tocar música no Spotify ou YouTube — busca a faixa e toca (no device "
        "ativo do Spotify, abrindo o app, ou numa aba do navegador). Recebe uma "
        "ordem concreta do núcleo cognitivo (ex.: 'tocar Tá Escrito do Grupo "
        "Revelação no Spotify')."
    ),
    keywords=[
        "tocar", "toca", "música", "musica", "som", "playlist", "spotify",
        "youtube", "sertanejo", "pagode", "rock", "funk", "banda", "cantor",
    ],
    intent_type=IntentType.MUSIC,
    tool_names=[
        "spotify_search",
        "spotify_play",
        "spotify_control",
        "youtube_search",
    ],
)

music_manifest.register()
