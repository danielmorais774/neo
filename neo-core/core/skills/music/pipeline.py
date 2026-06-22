"""MusicPipeline — resolves a concrete play order into an actuation.

Deterministic on purpose (no LLM loop): the cognitive core already produced a
clean, concrete order via handoff (e.g. "tocar Tá Escrito do Grupo Revelação no
Spotify"), so this skill only needs to: pick the service, search the exact track,
and decide HOW to play it given what's open (``media_context``). Reliability +
speed beat an agent loop here.

Decision matrix (see neo-music-feature):
  - Spotify + Connect device active → play in place via the Web API (channel
    ``spotify_connect``; this skill already played).
  - Spotify, no active device / not connected → ``launch_uri spotify:track:ID``.
  - YouTube + an open YouTube tab → ``media_command`` (reuse the tab, via extension).
  - YouTube, nothing open → ``open_url`` the watch URL.

The ``desktop``/``browser`` channels return an ``actuation`` that ``_handle_music``
dispatches to the desktop as a client tool; ``spotify_connect`` is fully done here.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.skills import SkillPipeline, SkillResult
from core.skills.music.adapter_spotify import (
    SpotifyAdapter,
    SpotifyNotConnectedError,
)
from core.skills.music.adapter_youtube import YouTubeAdapter
from core.skills.music.service import (
    clean_query,
    detect_action,
    detect_service,
    find_music_tab,
    parse_media_context,
    pick_active_device,
)
from modules import music_config

logger = logging.getLogger(__name__)


class MusicPipeline(SkillPipeline):
    """Immediate-execution pipeline for music playback (Spotify/YouTube)."""

    def __init__(self, agent_executor: Any, db: Any, broker: Any) -> None:
        self.agent_executor = agent_executor
        self.db = db
        self.broker = broker
        self.spotify = SpotifyAdapter(db=db)
        self.youtube = YouTubeAdapter()

    async def run(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        mission_id: UUID | None = None,
        extra: dict[str, Any] | None = None,
        user_id: UUID | None = None,
    ) -> SkillResult:
        extra = extra or {}
        order = user_message
        media_context = parse_media_context(extra)

        # Media control (pause/resume/next/...) — no search, act on the open tab.
        action = detect_action(order)
        if action != "play":
            return self._control(action, media_context)

        spotify_configured = self.spotify.is_configured
        youtube_available = self.youtube.is_configured
        spotify_connected = bool(
            user_id
            and spotify_configured
            and await music_config.is_connected(self.db, user_id)
        )

        from shared.config import get_settings
        service = detect_service(
            order,
            spotify_available=spotify_configured,
            youtube_available=youtube_available,
            default_service=get_settings().music_default_service,
        )

        # Spotify needs app credentials to search; fall back to YouTube if missing.
        if service == "spotify" and not spotify_configured:
            if youtube_available:
                service = "youtube"
            else:
                return SkillResult(
                    ok=False,
                    message="O Spotify ainda não está configurado no neo-core (faltam client_id/secret).",
                )

        query = clean_query(order)

        if service == "spotify":
            return await self._play_spotify(query, user_id, spotify_connected)
        return await self._play_youtube(query, media_context, youtube_available)

    def _control(self, action: str, media_context: dict[str, Any]) -> SkillResult:
        """Pause/resume/next/... on the open YouTube tab via the extension bridge.

        No search; requires a music tab in ``media_context`` (the extension must be
        connected and a YouTube tab open). Spotify-side control would need the
        Connect API and is out of scope for v1.
        """
        if not find_music_tab(media_context, "youtube"):
            return SkillResult(
                ok=False,
                message="Não tem nada tocando no navegador pra eu controlar.",
            )
        labels = {
            "pause": "Pausei", "resume": "Voltei a tocar",
            "next": "Próxima 🎶", "previous": "Voltei a anterior 🎶",
            "stop": "Parei", "mute": "Mutei",
            "volume_up": "Volume pra cima 🔊", "volume_down": "Volume pra baixo 🔉",
        }
        summary = labels.get(action, "Feito")
        return SkillResult(
            ok=True,
            message=f"{summary} 🎵",
            extra={"actuation": {
                "channel": "browser",
                "tool": "media_command",
                "args": {"action": action, "target": "youtube"},
                "summary": summary,
            }},
        )

    async def _play_spotify(
        self, query: str, user_id: UUID | None, connected: bool
    ) -> SkillResult:
        try:
            track = await self.spotify.search_track(query)
        except Exception as e:
            logger.warning("Spotify search failed: %s", e)
            return SkillResult(ok=False, message=f"Não consegui buscar no Spotify: {e}")
        if not track or not track.uri:
            return SkillResult(ok=False, message=f'Não achei "{query}" no Spotify.')

        # Connect playback (play in place) when there's an active device.
        if connected and user_id:
            try:
                devices = await self.spotify.get_devices(user_id)
                active = pick_active_device(devices)
                if active:
                    await self.spotify.play(user_id, [track.uri], active.get("id"))
                    return SkillResult(
                        ok=True,
                        message=f"Tocando {track.label} no Spotify 🎵",
                        extra={"actuation": {
                            "channel": "spotify_connect",
                            "summary": f"{track.label} (Spotify)",
                        }},
                    )
            except SpotifyNotConnectedError:
                pass
            except Exception as e:
                # No active device / non-premium / API hiccup → fall back to launch.
                logger.info("Spotify Connect play unavailable, launching URI: %s", e)

        # Launch the URI on the desktop (opens the app and plays).
        return SkillResult(
            ok=True,
            message=f"Abrindo {track.label} no Spotify 🎵",
            extra={"actuation": {
                "channel": "desktop",
                "tool": "launch_uri",
                "args": {"uri": track.uri},
                "summary": f"{track.label} (Spotify)",
            }},
        )

    async def _play_youtube(
        self, query: str, media_context: dict[str, Any], available: bool
    ) -> SkillResult:
        if not available:
            return SkillResult(
                ok=False,
                message="A busca do YouTube ainda não está configurada (falta a YOUTUBE_API_KEY).",
            )
        try:
            video = await self.youtube.search_video(query)
        except Exception as e:
            logger.warning("YouTube search failed: %s", e)
            return SkillResult(ok=False, message=f"Não consegui buscar no YouTube: {e}")
        if not video or not video.id:
            return SkillResult(ok=False, message=f'Não achei "{query}" no YouTube.')

        # Reuse an already-open YouTube tab via the extension when available.
        if find_music_tab(media_context, "youtube"):
            return SkillResult(
                ok=True,
                message=f"Tocando {video.label} no YouTube 🎵",
                extra={"actuation": {
                    "channel": "browser",
                    "tool": "media_command",
                    "args": {
                        "action": "play",
                        "target": "youtube",
                        "videoId": video.id,
                        "reuseTab": True,
                    },
                    "summary": f"{video.label} (YouTube)",
                }},
            )

        return SkillResult(
            ok=True,
            message=f"Abrindo {video.label} no YouTube 🎵",
            extra={"actuation": {
                "channel": "desktop",
                "tool": "open_url",
                "args": {"url": video.watch_url},
                "summary": f"{video.label} (YouTube)",
            }},
        )
