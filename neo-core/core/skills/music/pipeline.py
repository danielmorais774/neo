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
from core.skills.music.adapter_youtube import YouTubeAdapter, YouTubeVideo
from core.skills.music.service import (
    clean_query,
    detect_action,
    detect_service,
    find_music_tab,
    parse_direct_link,
    parse_media_context,
    pick_reuse_tab,
    neo_played_ids,
    remember_played,
    NEO_PLAYED_KEY,
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

        # Ordem que JÁ traz o LINK do vídeo: toca ELE, não procura pelo texto
        # dele. A skill `playlists` manda o cérebro despachar a fila "com o
        # link" — justamente para a faixa tocada ser a que a inscrição espera —
        # e o cérebro obedecia; mas aqui o link ia inteiro para o `search.list`
        # do YouTube, que devolvia OUTRO vídeo. Medido em 2026-09-09: a fila
        # pediu `watch?v=YdQ5NKjbd2U`, o play abriu `USELWXVcQUQ`, o `observar`
        # viu faixa diferente da esperada, concluiu "o usuário pediu outra
        # música", PAUSOU a inscrição — e o turno caiu em `open_url`, sem
        # extensão, sem vigia e com o autoplay do YouTube solto no fim da faixa.
        direto = parse_direct_link(order)
        if direto is not None and direto.service == "youtube" and direto.kind == "video":
            return await self._play_youtube(
                query,
                media_context,
                youtube_available,
                session_id=session_id,
                video_id=direto.id,
            )

        if service == "spotify":
            return await self._play_spotify(query, user_id, spotify_connected)
        return await self._play_youtube(
            query, media_context, youtube_available, session_id=session_id
        )

    async def _session_metadata(self, session_id: UUID | None) -> dict[str, Any]:
        """`session_metadata` da conversa; {} em qualquer problema.

        Nunca derruba o play: sem isto, um banco fora do ar transformaria "toca
        uma música" em erro por causa de uma otimização de aba.
        """
        if session_id is None or self.db is None:
            return {}
        try:
            sess = await self.db.get_conversation_session(session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Music] não li a sessão %s: %s", session_id, e)
            return {}
        meta = getattr(sess, "extra_metadata", None)
        # Só dict serve: a coluna é JSONB livre e já veio como lista de prod.
        return meta if isinstance(meta, dict) else {}

    async def _remember_played(
        self, session_id: UUID | None, *ids: str | None
    ) -> None:
        """Guarda o que o Neo acabou de tocar nesta conversa."""
        if session_id is None or self.db is None:
            return
        try:
            atual = await self._session_metadata(session_id)
            await self.db.merge_session_metadata(
                session_id, {NEO_PLAYED_KEY: remember_played(atual, *ids)}
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[Music] não gravei o que toquei: %s", e)

    def _control(self, action: str, media_context: dict[str, Any]) -> SkillResult:
        """Pause/resume/next/... on the open YouTube tab via the extension bridge.

        No search; requires a music tab in ``media_context`` (the extension must be
        connected and a YouTube tab open). Spotify-side control would need the
        Connect API and is out of scope for v1.
        """
        # `require_media`: aba sem player (a home do YouTube) não pausa nem pula.
        # Ela contava como alvo e o comando ia parar numa página sem `<video>` —
        # a extensão respondia "não achei o player na aba" DEPOIS de o Neo já ter
        # dito que pausou.
        tab = find_music_tab(media_context, "youtube", require_media=True)
        if not tab:
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
        if action == "mute":
            # A extensão ALTERNA o mudo (`video.muted = !video.muted`), então o
            # rótulo depende de como a aba está AGORA — dizer "Mutei" ao
            # desmutar é mentira barata e visível. Sem a sonda (`pageMuted`
            # ausente) não dá para saber qual dos dois vai acontecer.
            estado = tab.get("pageMuted")
            if isinstance(estado, bool):
                summary = "Desmutei" if estado else "Mutei"
            else:
                summary = "Alternei o mudo"
        return SkillResult(
            ok=True,
            message=f"{summary} 🎵",
            extra={"actuation": {
                "channel": "browser",
                "tool": "media_command",
                "args": {
                    "action": action,
                    "target": "youtube",
                    "tabId": tab.get("tabId"),
                },
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
        self,
        query: str,
        media_context: dict[str, Any],
        available: bool,
        session_id: UUID | None = None,
        video_id: str = "",
    ) -> SkillResult:
        """Toca no YouTube. Com `video_id`, toca ESSE vídeo; sem, busca pelo texto.

        Com o id em mãos a busca não é só desnecessária — ela é ERRADA (devolve
        outro vídeo) — e a chave da API vira opcional: sem ela ainda dá para
        tocar, só não dá para enfeitar o título.
        """
        if video_id:
            video = await self.youtube.video_by_id(video_id)
            if video is None:
                # Sem título bonito, mas o que importa é o id certo.
                video = YouTubeVideo(id=video_id, title=query or video_id, channel="")
        else:
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

        # Reusar aba só onde há POSSE: a que o próprio Neo colocou música nesta
        # conversa, ou uma sem nada carregado. Aba de vídeo do usuário — tocando
        # OU parada — nunca é tomada; para essa, abre uma nova.
        #
        # O `tabId` vai junto de propósito: a decisão de reusar é tomada AQUI, sobre
        # o snapshot do `media_context`, mas quem executa é a extensão — que antes
        # refazia a escolha sozinha (`tabs.query` + PRIMEIRA aba do YouTube). Com
        # mais de uma aba aberta, ou com o agente de navegador abrindo a dele no meio
        # do turno, os dois lados escolhiam abas diferentes e o play caía na errada.
        played = neo_played_ids(await self._session_metadata(session_id))
        tab, motivo = pick_reuse_tab(media_context, played)
        logger.info("[Music] reuso de aba: %s (%s)", "sim" if tab else "não", motivo)
        # Marca ANTES de atuar: é assim que o próximo "agora toca Bon Jovi"
        # reconhece esta aba como do Neo. Se a atuação falhar, o id sobra na
        # lista sem efeito — ele só casa com uma aba que realmente abriu o vídeo.
        # Guarda o id do vídeo E o do mix que o YouTube deriva dele (`RD<id>` —
        # é o formato real: `v=c-g6X5nsbn8&list=RDc-g6X5nsbn8`). Sem o segundo, a
        # aba deixaria de ser reconhecida como do Neo assim que o autoplay do mix
        # virasse a faixa sozinho.
        await self._remember_played(session_id, video.id, f"RD{video.id}")
        if tab:
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
                        "tabId": tab.get("tabId"),
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
