"""A fila mandava tocar um LINK e o YouTube devolvia OUTRO vídeo.

Sessão ``655e9445`` (2026-09-09 03:42 UTC), medida ponta a ponta:

1. a skill `playlists` manda o cérebro despachar a fila **com o link** da linha
   (é o que garante que a faixa tocada é a que a inscrição espera) — e o cérebro
   obedeceu: ``[[HANDOFF:music]]https://www.youtube.com/watch?v=YdQ5NKjbd2U[[/HANDOFF]]``;
2. o pipeline de música mandava a ordem para o ``search.list`` do YouTube mesmo
   quando ela já era um link — e antes disso passava pelo ``clean_query``, que
   tira a palavra "youtube" (keyword de serviço) e **mutila a URL**:
   ``https://www.youtube.com/watch?v=YdQ5NKjbd2U`` virava
   ``https://www. .com/watch?v=YdQ5NKjbd2U``. A busca por esse trapo devolveu
   ``USELWXVcQUQ``, outro vídeo;
3. ``_watch_for_actuation`` comparou o que ia tocar (``USELWXVcQUQ``) com o que a
   inscrição esperava (``YdQ5NKjbd2U``), concluiu "o usuário pediu outra música",
   **pausou a inscrição** e devolveu ``None``;
4. sem ``observar``, a atuação ficou em ``open_url`` — aba nova, fora da ponte.
   Sem ``media_command`` não há ``arm_watch``; sem ``watch`` a extensão não vigia
   nada, e o autoplay do YouTube ficou livre no fim da faixa.

O sintoma que o Daniel via ("a extensão não está interrompendo o autoplay") não
era da extensão: ela nunca foi avisada.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.skills.music.adapter_youtube import YouTubeVideo
from core.skills.music.pipeline import MusicPipeline
from core.skills.music.service import parse_direct_link

LINK = "https://www.youtube.com/watch?v=YdQ5NKjbd2U"
ID = "YdQ5NKjbd2U"


class _YouTubeFalso:
    """Busca que devolve OUTRO vídeo — como a de verdade fez com a URL."""

    is_configured = True

    def __init__(self, configurado: bool = True) -> None:
        self.is_configured = configurado
        self.buscas: list[str] = []
        self.por_id: list[str] = []

    async def search_video(self, query, limit=5):  # noqa: ANN001
        self.buscas.append(query)
        return YouTubeVideo(id="USELWXVcQUQ", title="Outro Vídeo", channel="Outro Canal")

    async def video_by_id(self, video_id):  # noqa: ANN001
        self.por_id.append(video_id)
        if not self.is_configured:
            return None
        return YouTubeVideo(id=video_id, title="These Are The Days", channel="The Exies")


def _pipeline(yt: _YouTubeFalso) -> MusicPipeline:
    p = MusicPipeline.__new__(MusicPipeline)
    p.youtube = yt
    p.spotify = SimpleNamespace(is_configured=False)
    p.db = None
    return p


def _tocar(ordem: str, yt: _YouTubeFalso, media_context=None):  # noqa: ANN001
    p = _pipeline(yt)
    return asyncio.run(
        p.run(
            session_id=None,
            user_message=ordem,
            history=[],
            extra={"media_context": media_context or {"browser_tabs": []}},
        )
    )


def _video_id_da_atuacao(resultado) -> str:  # noqa: ANN001
    at = (resultado.extra or {}).get("actuation") or {}
    args = at.get("args") or {}
    if args.get("videoId"):
        return str(args["videoId"])
    link = parse_direct_link(str(args.get("url") or ""))
    return link.id if link else ""


# ---------------------------------------------------------------------------


def test_link_na_ordem_toca_o_video_do_link():
    yt = _YouTubeFalso()
    r = _tocar(LINK, yt)
    assert r.ok, r.message
    assert _video_id_da_atuacao(r) == ID, r.extra
    assert yt.buscas == [], f"não pode buscar quando já tem o link: {yt.buscas}"
    assert yt.por_id == [ID]


def test_o_titulo_vem_do_video_certo():
    yt = _YouTubeFalso()
    r = _tocar(LINK, yt)
    assert "These Are The Days" in r.message, r.message


def test_sem_chave_da_api_o_link_ainda_toca():
    """Tocar um link não precisa de busca — logo, não precisa de YOUTUBE_API_KEY."""
    yt = _YouTubeFalso(configurado=False)
    r = _tocar(LINK, yt)
    assert r.ok, r.message
    assert _video_id_da_atuacao(r) == ID


def test_pedido_por_NOME_continua_buscando():
    yt = _YouTubeFalso()
    r = _tocar("toca These Are The Days do The Exies no youtube", yt)
    assert r.ok, r.message
    assert yt.buscas, "sem link, a busca é o caminho"
    assert yt.por_id == []
    assert _video_id_da_atuacao(r) == "USELWXVcQUQ"


def test_link_curto_youtu_be_tambem_vale():
    yt = _YouTubeFalso()
    r = _tocar("https://youtu.be/" + ID, yt)
    assert _video_id_da_atuacao(r) == ID
    assert yt.buscas == []


def test_link_com_mix_usa_o_video_nao_o_mix():
    """A URL da aba costuma vir com `list=RD…` (o mix que o YouTube deriva). É o
    VÍDEO que a inscrição espera, não o mix."""
    yt = _YouTubeFalso()
    r = _tocar(f"https://www.youtube.com/watch?v={ID}&list=RD{ID}&start_radio=1", yt)
    assert _video_id_da_atuacao(r) == ID
    assert yt.buscas == []


def test_a_faixa_tocada_e_a_que_a_inscricao_espera():
    """O ponto da correção: é esta igualdade que faz `_watch_for_actuation`
    devolver `observar` em vez de pausar a fila."""
    yt = _YouTubeFalso()
    esperado_pela_inscricao = ID  # expected_ref, gravado pelo event_subscribe
    r = _tocar(LINK, yt)
    assert _video_id_da_atuacao(r) == esperado_pela_inscricao


def test_clean_query_mutila_a_url_e_por_isso_lemos_a_ordem_CRUA():
    """A causa determinística: `clean_query` tira "youtube" da frase — inclusive
    de dentro do domínio. Por isso `parse_direct_link` roda sobre a ordem
    original, e não sobre a query limpa."""
    from core.skills.music.service import clean_query

    trapo = clean_query(LINK)
    assert "youtube.com" not in trapo, trapo
    assert parse_direct_link(trapo) is None or parse_direct_link(trapo).id != ID
    # e a ordem crua continua legível
    assert parse_direct_link(LINK).id == ID
