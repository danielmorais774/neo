"""De qual aba do YouTube o Neo pode tomar posse.

A política antiga era "reusa qualquer aba do YouTube que estiver aberta", e ela
erra dos dois lados:

* **rouba** — o usuário está com um vídeo aberto, PAUSADO, pra voltar depois; ele
  pede uma música e o Neo navega justamente aquela aba. Parada não quer dizer
  livre;
* **desperdiça** — se a regra fosse "nunca reusa", cada "toca outra" deixaria pra
  trás mais uma aba de música morta.

A linha entre as duas não é o estado do player, é **posse**. São reaproveitáveis:

1. a aba onde o **próprio Neo** colocou música NESTA conversa — trocar AC/DC por
   Bon Jovi é a mesma sessão de música seguindo, e a aba já era dele;
2. a aba que **não tem nada carregado** (home, feed, busca) — não há o que perder.

Qualquer outra é do usuário: abre aba nova.

O reconhecimento de (1) é por id de vídeo/mix guardado em
`conversation_session.session_metadata` — o estado tem que sobreviver ao reload
do neo-core, que acontece a todo salvamento de arquivo, no meio da conversa.
"""

from __future__ import annotations

import pytest

from core.skills.music.service import (
    NEO_PLAYED_KEY,
    neo_played_ids,
    pick_reuse_tab,
    remember_played,
    tab_is_neo_music,
)


def _watch(tab_id, video_id, **kw):
    t = {
        "site": "youtube",
        "tabId": tab_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "kind": "watch",
        "videoId": video_id,
        "hasMedia": True,
    }
    t.update(kw)
    return t


def _home(tab_id=99):
    return {
        "site": "youtube",
        "tabId": tab_id,
        "url": "https://www.youtube.com/",
        "kind": "home",
        "hasMedia": False,
    }


def _ctx(*tabs):
    return {"browser_tabs": list(tabs)}


# ── não tomar a aba do usuário ──────────────────────────────────────────────


def test_video_do_usuario_tocando_nao_e_tomado():
    tab, motivo = pick_reuse_tab(_ctx(_watch(1, "aula", paused=False)), [])
    assert tab is None
    assert "do usuário" in motivo


def test_video_do_usuario_PARADO_tambem_nao_e_tomado():
    """O caso que motivou a mudança: pausado não é livre — é o vídeo que ele vai
    voltar a ver."""
    tab, _ = pick_reuse_tab(_ctx(_watch(1, "aula", paused=True)), [])
    assert tab is None


def test_aba_que_nao_deu_pra_sondar_nao_e_tomada():
    """Sem saber o que tem na aba, tomar é apostar contra o usuário."""
    aba = {"site": "youtube", "tabId": 5, "url": "https://www.youtube.com/watch?v=x",
           "kind": "watch", "probe": "denied"}
    tab, _ = pick_reuse_tab(_ctx(aba), [])
    assert tab is None


def test_varias_abas_do_usuario_continua_sendo_aba_nova():
    tab, _ = pick_reuse_tab(_ctx(_watch(1, "a"), _watch(2, "b", paused=False)), [])
    assert tab is None


# ── a home é de graça ───────────────────────────────────────────────────────


def test_home_sem_player_pode_ser_reaproveitada():
    tab, motivo = pick_reuse_tab(_ctx(_home(7)), [])
    assert tab["tabId"] == 7
    assert "sem nada carregado" in motivo


def test_entre_video_do_usuario_e_home_escolhe_a_home():
    tab, _ = pick_reuse_tab(_ctx(_watch(1, "aula"), _home(7)), [])
    assert tab["tabId"] == 7


def test_sem_aba_nenhuma_abre_nova():
    tab, motivo = pick_reuse_tab(_ctx(), [])
    assert tab is None
    assert "nenhuma aba" in motivo


# ── a aba do próprio Neo ────────────────────────────────────────────────────


def test_acdc_depois_bonjovi_reaproveita_a_aba_do_neo():
    """O exemplo do pedido: o Neo pôs AC/DC; "agora Bon Jovi" troca ali mesmo."""
    tab, motivo = pick_reuse_tab(_ctx(_watch(3, "acdc_id", paused=False)), ["acdc_id"])
    assert tab["tabId"] == 3
    assert "o Neo já usou" in motivo


def test_a_aba_do_neo_ganha_da_home():
    """Trocar a faixa onde a música já está é melhor que abrir na home e deixar
    duas abas de música."""
    tab, _ = pick_reuse_tab(_ctx(_home(7), _watch(3, "acdc_id")), ["acdc_id"])
    assert tab["tabId"] == 3


def test_o_mix_do_neo_sobrevive_ao_autoplay():
    """O YouTube troca a faixa sozinho num mix e o videoId deixa de bater. O id
    da FILA (`RD<id>`) continua sendo a que o Neo abriu."""
    aba = _watch(3, "outra_faixa_qualquer", listId="RDacdc_id")
    tab, _ = pick_reuse_tab(_ctx(aba), ["acdc_id", "RDacdc_id"])
    assert tab["tabId"] == 3


def test_aba_de_video_que_o_neo_nao_tocou_nao_conta():
    assert tab_is_neo_music(_watch(1, "aula"), ["acdc_id"]) is False


def test_sem_historico_nenhuma_aba_e_do_neo():
    assert tab_is_neo_music(_watch(1, "acdc_id"), []) is False


# ── o registro por conversa ─────────────────────────────────────────────────


def test_o_que_o_neo_tocou_fica_na_sessao():
    md = {}
    lista = remember_played(md, "acdc_id", "RDacdc_id")
    assert lista == ["acdc_id", "RDacdc_id"]
    assert neo_played_ids({NEO_PLAYED_KEY: lista}) == ["acdc_id", "RDacdc_id"]


def test_novo_entra_na_frente_e_nao_duplica():
    md = {NEO_PLAYED_KEY: ["acdc_id", "RDacdc_id"]}
    assert remember_played(md, "bonjovi", "acdc_id")[0] == "bonjovi"
    assert remember_played(md, "acdc_id").count("acdc_id") == 1


def test_a_lista_tem_teto():
    """Histórico longo é peso morto e ainda aumenta a chance de casar com uma aba
    velha que o usuário já reaproveitou para outra coisa."""
    md = {NEO_PLAYED_KEY: [f"v{i}" for i in range(50)]}
    assert len(remember_played(md, "novo")) == 12


def test_metadata_estranha_nao_derruba_nada():
    assert neo_played_ids(None) == []
    assert neo_played_ids({NEO_PLAYED_KEY: "não sou lista"}) == []
    assert neo_played_ids({}) == []


# ── ponta a ponta pela pipeline ─────────────────────────────────────────────


class _VideoFake:
    def __init__(self, vid):
        self.id = vid
        self.label = f"{vid} (YouTube)"
        self.watch_url = f"https://www.youtube.com/watch?v={vid}"


class _YouTubeFake:
    is_configured = True

    def __init__(self, vid):
        self._vid = vid

    async def search_video(self, query):
        return _VideoFake(self._vid)


class _DBFake:
    """Dublê do `conversation_session`: guarda o metadata como o Postgres faria."""

    def __init__(self):
        self.metadata = {}

    async def get_conversation_session(self, session_id):
        class S:
            pass

        s = S()
        s.extra_metadata = dict(self.metadata)
        return s

    async def merge_session_metadata(self, session_id, patch):
        self.metadata.update(patch)


def _pipeline(vid, db):
    from core.skills.music.pipeline import MusicPipeline

    p = MusicPipeline(agent_executor=None, db=db, broker=None)
    p.youtube = _YouTubeFake(vid)
    return p


@pytest.mark.asyncio
async def test_fluxo_acdc_depois_bonjovi():
    """Dois turnos seguidos, com o snapshot mudando entre eles — que é o que
    prova o estado sobrevivendo à conversa, e não só à chamada."""
    from uuid import uuid4

    db = _DBFake()
    sid = uuid4()

    # Turno 1: nada aberto → aba nova, e o Neo anota o que tocou.
    r1 = await _pipeline("acdc_id", db)._play_youtube(
        "acdc", {"browser_tabs": []}, available=True, session_id=sid
    )
    assert r1.extra["actuation"]["tool"] == "open_url"
    assert "acdc_id" in db.metadata[NEO_PLAYED_KEY]

    # Turno 2: a aba do turno 1 aparece no snapshot → troca ALI.
    ctx = _ctx(_watch(21, "acdc_id", paused=False))
    r2 = await _pipeline("bonjovi_id", db)._play_youtube(
        "bon jovi", ctx, available=True, session_id=sid
    )
    args = r2.extra["actuation"]["args"]
    assert args["reuseTab"] is True
    assert args["tabId"] == 21
    assert args["videoId"] == "bonjovi_id"


@pytest.mark.asyncio
async def test_aba_do_usuario_no_meio_nao_e_atropelada():
    """Mesma conversa, mas a aba aberta é um vídeo que o Neo nunca tocou."""
    from uuid import uuid4

    db = _DBFake()
    ctx = _ctx(_watch(30, "aula_de_rust", paused=True))
    res = await _pipeline("bonjovi_id", db)._play_youtube(
        "bon jovi", ctx, available=True, session_id=uuid4()
    )
    assert res.extra["actuation"]["tool"] == "open_url"


@pytest.mark.asyncio
async def test_banco_fora_do_ar_nao_derruba_o_play():
    """A otimização de aba não pode custar a música."""
    from uuid import uuid4

    class _DBQuebrado:
        async def get_conversation_session(self, session_id):
            raise RuntimeError("sem banco")

        async def merge_session_metadata(self, session_id, patch):
            raise RuntimeError("sem banco")

    res = await _pipeline("x", _DBQuebrado())._play_youtube(
        "rock", _ctx(_home(7)), available=True, session_id=uuid4()
    )
    assert res.ok is True
    assert res.extra["actuation"]["args"]["tabId"] == 7
