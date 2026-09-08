"""Pure helpers for the music skill — service/query parsing and media-context reads.

Kept free of I/O so they're trivially testable. The pipeline does the HTTP and the
side effects; these functions only normalize text and inspect the desktop-provided
``media_context`` snapshot.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_ACCENT_MAP = {
    "á": "a", "à": "a", "â": "a", "ã": "a",
    "é": "e", "ê": "e", "í": "i",
    "ó": "o", "ô": "o", "õ": "o",
    "ú": "u", "ç": "c",
}


def _normalize(text: str) -> str:
    out = (text or "").lower().strip()
    for src, dst in _ACCENT_MAP.items():
        out = out.replace(src, dst)
    return " ".join(out.split())


# Media-control actions detected from the handoff order. The cognitive core emits a
# clean control word (pausar/retomar/próxima/...) for control, or a track for play.
# Ordered: unambiguous controls first; anything else falls through to "play". We avoid
# ambiguous tokens like "para"/"volta" here — the cognitive translates those to clean
# words (see _MUSIC_HANDOFF_INSTRUCTION).
_ACTION_RULES = [
    ("next", r"\b(proxima|proximo|pula|pular|avancar|skip|next)\b"),
    ("previous", r"\b(anterior|previous)\b"),
    ("volume_up", r"\b(aumenta|aumentar|sobe|subir)\b"),
    ("volume_down", r"\b(abaixa|abaixar|diminui|diminuir|baixa|baixar)\b"),
    # `desmuta` casa aqui de propósito: a extensão ALTERNA o mudo, então
    # desmutar é o mesmo comando. Sem esta entrada, "desmuta" não casava
    # nenhuma regra (o \b não pega "muta" dentro de "desmuta") e caía em
    # "play" — o Neo ia BUSCAR "desmuta" no YouTube.
    ("mute", r"\b(mudo|muta|mutar|mute|desmuta|desmutar|silencia|silenciar)\b"),
    ("pause", r"\b(pausa|pausar|pause|stop|interrompe)\b"),
    ("resume", r"(\b(retoma|retomar|continua|continuar|continue|despausa|despausar|resume)\b"
               r"|\b(da|dar|de)\s+(o\s+)?play\b|\bplay de novo\b|\bvolta(r)? a tocar\b)"),
]


def detect_action(text: str) -> str:
    """Classify the handoff order into a media action; default 'play' (a track)."""
    n = _normalize(text)
    for action, pat in _ACTION_RULES:
        if re.search(pat, n):
            return action
    return "play"


def detect_service(
    text: str,
    *,
    spotify_available: bool,
    youtube_available: bool,
    default_service: str = "youtube",
) -> str:
    """Return 'spotify' or 'youtube'.

    Explicit mention in the text ALWAYS wins. Otherwise pick ``default_service``
    (configurable via ``music_default_service``; defaults to YouTube since it needs
    no premium/login), falling back to whichever service is actually available.
    """
    n = _normalize(text)
    if "spotify" in n:
        return "spotify"
    if "youtube" in n or "you tube" in n or re.search(r"\byt\b", n):
        return "youtube"

    preferred = default_service if default_service in ("spotify", "youtube") else "youtube"
    fallback = "spotify" if preferred == "youtube" else "youtube"
    avail = {"spotify": spotify_available, "youtube": youtube_available}
    if avail.get(preferred):
        return preferred
    if avail.get(fallback):
        return fallback
    # Nothing configured — return the preference so the caller surfaces a hint.
    return preferred


# Phrases stripped from the handoff order to leave a clean search query. Applied
# case-insensitively with word boundaries; order matters (longer first).
_QUERY_STRIP = [
    r"\b(no|na|pelo|pela|la no|ai no|aqui no)\s+(spotify|youtube|yt|deezer)\b",
    r"\b(spotify|youtube|deezer)\b",
    r"^\s*(por favor|pf)\b",
    r"\b(toca|tocar|toque|coloca|colocar|bota|botar|poe|poem|reproduz|reproduzir|manda|mandar)\b",
    r"\b(quero|queria|gostaria de|pode|poderia)\s+(ouvir|escutar|tocar|colocar)\b",
    r"\b(ouvir|escutar)\b",
    r"\b(uma|um|a|o|aquela|aquele|aquela musica|aquele som)\s+(musica|som|playlist|cancao|faixa)\b",
    r"\b(pra mim|pra eu|pra gente|pra nos|ai|por favor|agora)\b",
]


def clean_query(text: str) -> str:
    """Strip play verbs / service mentions / fillers to leave a search query.

    Imperfect on purpose — Spotify/YouTube search tolerate messy queries, so we
    only remove the obvious noise and never return empty (falls back to the input).
    """
    out = " " + (text or "").strip() + " "
    for pattern in _QUERY_STRIP:
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    out = " ".join(out.split()).strip(" ,.-")
    return out or (text or "").strip()


# ---------------------------------------------------------------------------
# Link concreto na ordem (docs/playlists-skill-plan.md §6)
# ---------------------------------------------------------------------------
# Quando o cognitivo já resolveu O QUE tocar — porque leu o link de um cadastro,
# de uma playlist salva ou o usuário colou a URL — buscar de novo é errado duas
# vezes: gasta uma chamada de API e pode achar OUTRA coisa ("Tá Escrito" tem 40
# uploads no YouTube). Estes parsers deixam a pipeline pular a busca.
#
# Nada aqui aceita texto livre: o que não casar com um dos formatos conhecidos
# volta como None e o fluxo normal (busca) segue valendo.

_YT_VIDEO_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^\s]*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
_YT_LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]{2,})")
_SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|playlist|album|artist)/([A-Za-z0-9]{22})"
)
_SPOTIFY_URI_RE = re.compile(r"\bspotify:(track|playlist|album|artist):([A-Za-z0-9]{22})\b")
_ANY_URL_RE = re.compile(r"https?://[^\s<>\"']+")


@dataclass(frozen=True)
class DirectLink:
    """Um alvo concreto extraído da ordem — nunca vem de busca.

    ``service``: "youtube" | "spotify" | "outro".
    ``kind``: "video" | "playlist" | "album" | "artist" | "url".
    ``id``: id do vídeo/recurso quando existe (o que a extensão precisa).
    ``uri``: ``spotify:playlist:…`` para o app nativo; vazio fora do Spotify.
    ``url``: URL canônica para abrir no navegador.
    """

    service: str
    kind: str
    id: str = ""
    uri: str = ""
    url: str = ""

    @property
    def label(self) -> str:
        return self.url or self.uri or self.id


def parse_direct_link(text: str) -> DirectLink | None:
    """Extrai um alvo tocável da ordem, ou None quando não há link nenhum.

    Ordem de preferência: YouTube (vídeo, depois playlist), Spotify (URL e URI)
    e por fim qualquer http(s) — Deezer, Apple Music, rádio — que só sabe abrir
    numa aba. O texto ao redor é ignorado: "toca https://… pra mim" funciona.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    match = _YT_VIDEO_RE.search(raw)
    if match:
        video_id = match.group(1)
        # Link canônico de propósito: a URL da aba costuma carregar `list=` (o
        # mix que o YouTube deriva sozinho) e `t=`, e é exatamente isso que faz
        # o autoplay assumir a fila de volta.
        return DirectLink(
            service="youtube",
            kind="video",
            id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
        )

    if "youtube.com" in raw or "youtu.be" in raw:
        listed = _YT_LIST_RE.search(raw)
        if listed:
            list_id = listed.group(1)
            return DirectLink(
                service="youtube",
                kind="playlist",
                id=list_id,
                url=f"https://www.youtube.com/playlist?list={list_id}",
            )

    match = _SPOTIFY_URL_RE.search(raw) or _SPOTIFY_URI_RE.search(raw)
    if match:
        kind, resource_id = match.group(1), match.group(2)
        return DirectLink(
            service="spotify",
            kind=kind,
            id=resource_id,
            uri=f"spotify:{kind}:{resource_id}",
            url=f"https://open.spotify.com/{kind}/{resource_id}",
        )

    match = _ANY_URL_RE.search(raw)
    if match:
        url = match.group(0).rstrip(".,;)")
        return DirectLink(service="outro", kind="url", url=url)
    return None


def parse_media_context(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Read ``metadata['media_context']`` (a JSON string) into a dict; never raises."""
    if not metadata:
        return {}
    raw = metadata.get("media_context")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


# ── leitura do snapshot da extensão ────────────────────────────────────────
#
# A ponte reporta, por aba: o som da guia (`audible` — o ícone de auto-falante),
# o estado real do player (`paused`/`hasMedia`/`duration`), o TIPO de página
# (`kind`: watch, home, feed, busca…), se o YouTube classificou aquele vídeo
# como música (`contentType`) e a faixa/artista que o próprio player publica no
# centro de mídia do SO. Nada disso é obrigatório: aba de Spotify, aba ainda
# carregando ou aba onde a injeção falhou chegam só com o básico, e toda leitura
# aqui degrada para "não sei" em vez de mentir.


def tab_has_media(tab: dict[str, Any]) -> bool | None:
    """Esta aba tem um player com mídia carregada? ``None`` = não deu para saber.

    Distinguir "não tem" de "não sei" é o ponto: uma aba na HOME do YouTube é
    uma aba do YouTube sem nada para pausar, e mandar um comando para ela falha
    com "não achei o player". Uma aba que não pôde ser sondada pode muito bem
    ter vídeo — recusar ali seria pior.
    """
    if not isinstance(tab, dict):
        return None
    if isinstance(tab.get("hasMedia"), bool):
        return tab["hasMedia"]
    if tab.get("audible"):
        return True
    # Sem sonda: só a URL responde, e só para as páginas que nunca têm player.
    if tab.get("kind") in ("home", "feed", "search", "channel", "music_home"):
        return False
    return None


def tab_is_playing(tab: dict[str, Any]) -> bool:
    """Está tocando AGORA (não pausado). Vídeo no mudo continua tocando."""
    if not isinstance(tab, dict):
        return False
    if isinstance(tab.get("paused"), bool):
        return not tab["paused"] and tab_has_media(tab) is not False
    return bool(tab.get("playing") or tab.get("audible"))


def _rank(tab: dict[str, Any]) -> tuple[int, int, int]:
    """Ordem de preferência ao escolher UMA aba: tocando > com mídia > watch."""
    tocando = 1 if tab_is_playing(tab) else 0
    com_midia = 1 if tab_has_media(tab) is True else 0
    pagina = 1 if tab.get("kind") in ("watch", "music", "shorts", "embed") else 0
    return (tocando, com_midia, pagina)


def find_music_tab(
    media_context: dict[str, Any], site: str, *, require_media: bool = False
) -> dict[str, Any] | None:
    """Escolhe a aba daquele site — a que está tocando primeiro, não a primeira.

    Pegar a PRIMEIRA aba do site era o comportamento antigo e é o que faz o
    comando cair na aba errada quando há duas abas do YouTube abertas: a home
    numa e a música na outra.

    ``require_media=True`` recusa aba sem player (a home). Serve para
    pausar/pular, que precisam de algo tocando; para dar play não vale, porque
    navegar a home para o vídeo é exatamente o reuso que se quer.
    """
    tabs = [
        t
        for t in (media_context.get("browser_tabs") or [])
        if isinstance(t, dict) and t.get("site") == site
    ]
    if require_media:
        tabs = [t for t in tabs if tab_has_media(t) is not False]
    if not tabs:
        return None
    return max(tabs, key=_rank)


# ── de qual aba dá pra tomar posse ──────────────────────────────────────────
#
# Reusar QUALQUER aba do YouTube era o padrão antigo, e ele desperdiça de um
# jeito e é chato do outro:
#
# * reusar uma aba de vídeo que o usuário abriu — mesmo PARADA — tira da frente
#   dele uma coisa que ele escolheu ver. Aba pausada não é aba livre; muitas
#   vezes é justamente a que ele vai voltar a ver;
# * abrir aba nova toda vez enche o navegador de abas de música mortas.
#
# A linha entre os dois é POSSE, não estado do player: dá pra tomar a aba que o
# próprio Neo colocou música nesta conversa (trocar AC/DC por Bon Jovi é a
# mesma sessão de música continuando) e a que não tem nada carregado (a home do
# YouTube não perde nada). Qualquer outra aba é do usuário — abre uma nova.

#: Chave em ``conversation_session.session_metadata`` com o que o Neo tocou
#: nesta conversa (ids de vídeo e de playlist/mix).
NEO_PLAYED_KEY = "music_neo_played"

#: Teto da lista. Só serve para reconhecer a aba corrente; histórico longo é
#: peso morto e ainda aumentaria a chance de casar com uma aba antiga que o
#: usuário reaproveitou para outra coisa.
NEO_PLAYED_MAX = 12


def neo_played_ids(session_metadata: dict[str, Any] | None) -> list[str]:
    """Ids (vídeo/playlist) que o Neo tocou nesta conversa, mais recente primeiro."""
    if not isinstance(session_metadata, dict):
        return []
    vals = session_metadata.get(NEO_PLAYED_KEY)
    if not isinstance(vals, list):
        return []
    return [str(v) for v in vals if isinstance(v, (str, int)) and str(v).strip()]


def remember_played(
    session_metadata: dict[str, Any] | None, *ids: str | None
) -> list[str]:
    """Lista atualizada para gravar de volta: novos na frente, sem repetir."""
    novos = [str(i).strip() for i in ids if i and str(i).strip()]
    atual = neo_played_ids(session_metadata)
    saida: list[str] = []
    for i in novos + atual:
        if i not in saida:
            saida.append(i)
    return saida[:NEO_PLAYED_MAX]


def tab_is_neo_music(tab: dict[str, Any], played: list[str]) -> bool:
    """Esta aba está com algo que o NEO colocou nesta conversa?

    Casa por ``videoId`` e também por ``listId``: quando o Neo põe um mix/rádio
    (``list=RD…``), o autoplay do YouTube troca o vídeo sozinho e o id do vídeo
    deixa de bater — mas a fila continua sendo a que o Neo abriu.
    """
    if not played or not isinstance(tab, dict):
        return False
    return any(
        str(tab.get(campo) or "") in played for campo in ("videoId", "listId")
    )


def pick_reuse_tab(
    media_context: dict[str, Any], played: list[str] | None = None
) -> tuple[dict[str, Any] | None, str]:
    """A aba que pode ser reaproveitada, e POR QUÊ (a razão vai para o log).

    ``(None, motivo)`` = abre aba nova. Nunca devolve uma aba de vídeo do
    usuário, tocando ou parada.
    """
    played = played or []
    tabs = [
        t
        for t in (media_context.get("browser_tabs") or [])
        if isinstance(t, dict) and t.get("site") == "youtube"
    ]
    if not tabs:
        return None, "nenhuma aba do YouTube aberta"

    minhas = [t for t in tabs if tab_is_neo_music(t, played)]
    if minhas:
        return max(minhas, key=_rank), "aba que o Neo já usou para tocar nesta conversa"

    livres = [t for t in tabs if tab_has_media(t) is False]
    if livres:
        return max(livres, key=_rank), "aba sem nada carregado (home/feed/busca)"

    return None, "as abas abertas são vídeos do usuário — não vou tomar nenhuma"


def _mmss(seconds: Any) -> str:
    """Segundos → 4:05 / 1:12:33. Vazio quando não dá para saber."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    horas, resto = divmod(total, 3600)
    minutos, segundos = divmod(resto, 60)
    if horas:
        return f"{horas}:{minutos:02d}:{segundos:02d}"
    return f"{minutos}:{segundos:02d}"


_KIND_LABEL = {
    "home": "página inicial (sem vídeo aberto)",
    "music_home": "página inicial do YouTube Music",
    "feed": "feed (sem vídeo aberto)",
    "search": "página de busca",
    "channel": "página de canal",
    "playlist": "página de playlist",
    "results": "página de busca",
    "shorts": "Shorts",
}


def describe_tab(tab: dict[str, Any]) -> str:
    """Uma linha legível por aba, só com o que a extensão realmente sabe."""
    partes: list[str] = [str(tab.get("site") or "?")]

    tem_midia = tab_has_media(tab)
    if tem_midia is False:
        partes.append("nada aberto pra tocar")
    elif tab_is_playing(tab):
        partes.append("TOCANDO")
    elif isinstance(tab.get("paused"), bool):
        partes.append("pausado")
    elif tab.get("audible"):
        partes.append("com som")
    else:
        partes.append("aberta")

    if tab.get("adShowing"):
        partes.append("ANÚNCIO no ar (o que toca agora não é a faixa)")

    tipo = tab.get("contentType")
    if tipo == "music":
        partes.append("música")
    elif tipo == "video":
        partes.append("vídeo comum")

    # Faixa/artista quando o player publica; senão o título da aba.
    faixa = (tab.get("track") or "").strip()
    artista = (tab.get("artist") or "").strip()
    if faixa and artista:
        partes.append(f'"{faixa}" — {artista}')
    elif faixa:
        partes.append(f'"{faixa}"')
    else:
        titulo = (tab.get("title") or tab.get("url") or "").strip()
        if titulo:
            partes.append(titulo[:120])

    # Quem publicou. Só quando acrescenta: em música o mediaSession costuma
    # repetir o canal no artista, e dizer "Papa Roach · canal Papa Roach" é ruído.
    canal = (tab.get("channel") or "").strip()
    if canal and canal.casefold() != artista.casefold():
        partes.append(f"canal {canal}")

    if tab.get("live"):
        partes.append("ao vivo")
    elif tem_midia is not False:
        pos, dur = _mmss(tab.get("currentTime")), _mmss(tab.get("duration"))
        if pos and dur:
            pct = tab.get("progress")
            pct_txt = f" ({round(float(pct) * 100)}%)" if isinstance(pct, (int, float)) else ""
            partes.append(f"{pos}/{dur}{pct_txt}")
        elif dur:
            partes.append(f"duração {dur}")

    # O ícone de auto-falante da guia, e por que ele pode estar apagado.
    if tab.get("audible"):
        partes.append("som na guia")
    elif tab.get("muted"):
        partes.append("guia no mudo")
    elif tab.get("pageMuted"):
        partes.append("player no mudo")

    rotulo = _KIND_LABEL.get(str(tab.get("kind") or ""))
    if rotulo and tem_midia is not True:
        partes.append(rotulo)

    # O link REAL do vídeo, já limpo pela extensão. É o que evita o Neo montar
    # uma URL de cabeça quando o usuário pede "manda o link".
    link = (tab.get("videoUrl") or "").strip()
    if link:
        partes.append(link)

    if tab.get("probe") in ("denied", "error"):
        partes.append("(não consegui ler o player desta aba)")
    elif tab.get("probe") == "loading":
        partes.append("(ainda carregando)")

    return " · ".join(p for p in partes if p)


def describe_tabs(tabs: list[Any]) -> list[str]:
    """Linhas prontas para o prompt, na mesma ordem de preferência da escolha."""
    validas = [t for t in tabs if isinstance(t, dict)]
    validas.sort(key=_rank, reverse=True)
    return [describe_tab(t) for t in validas]


def pick_active_device(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the active Spotify Connect device, else the first available one."""
    if not devices:
        return None
    for dev in devices:
        if dev.get("is_active"):
            return dev
    return devices[0]
