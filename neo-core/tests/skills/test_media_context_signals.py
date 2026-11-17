"""O que a ponte da extensão passou a contar sobre cada aba.

Antes, uma aba do snapshot era `{site, tabId, url, title, playing}` — e
`playing` era só `tab.audible`, o ícone de auto-falante da guia. Isso deixava o
Neo sem resposta para três perguntas que ele precisava responder:

* **está tocando ou pausado?** `audible` diz "sai som", não "está tocando": um
  clipe rodando no mudo não é audível, e um vídeo pausado com anúncio pode ser;
* **é música ou vídeo comum?** o YouTube sabe (ele carimba uma `category` no
  microformat de cada vídeo), a extensão só não lia;
* **essa aba tem alguma coisa pra controlar?** uma aba na HOME do YouTube é uma
  aba do YouTube sem player nenhum. Ela contava como alvo, o comando de pausar
  ia parar numa página sem `<video>` e a extensão respondia "não achei o player"
  DEPOIS de o Neo já ter dito que pausou.

Aqui está o lado do neo-core: ler esses campos sem nunca exigir que existam (aba
de Spotify, aba carregando e aba onde a injeção falhou chegam só com o básico) e
distinguir **"não tem"** de **"não sei"**, que é a distinção que evita tanto o
comando no vazio quanto a recusa indevida.
"""

from __future__ import annotations

from core.skills.music.service import (
    describe_tab,
    describe_tabs,
    find_music_tab,
    tab_has_media,
    tab_is_playing,
)


def _aba(**kw):
    base = {"site": "youtube", "tabId": 1, "url": "https://www.youtube.com/watch?v=x"}
    base.update(kw)
    return base


# ── tocando × com som ───────────────────────────────────────────────────────


def test_video_no_mudo_continua_tocando():
    """O caso que `audible` sozinho errava: som desligado, vídeo rodando."""
    aba = _aba(hasMedia=True, paused=False, audible=False, pageMuted=True)
    assert tab_is_playing(aba) is True
    assert "TOCANDO" in describe_tab(aba)
    assert "player no mudo" in describe_tab(aba)


def test_pausado_com_o_player_dizendo_pausado():
    aba = _aba(hasMedia=True, paused=True, audible=False)
    assert tab_is_playing(aba) is False
    assert "pausado" in describe_tab(aba)


def test_sem_sonda_cai_no_som_da_guia():
    """Aba que não pôde ser sondada ainda responde pelo sinal antigo."""
    aba = _aba(audible=True)
    assert tab_is_playing(aba) is True


def test_guia_muda_pelo_usuario_aparece_separada_do_player():
    aba = _aba(hasMedia=True, paused=False, audible=False, muted=True)
    assert "guia no mudo" in describe_tab(aba)


# ── canal e link ────────────────────────────────────────────────────────────


def test_canal_e_link_entram_na_linha():
    """O que o usuário pede em texto: "de que canal é isso?" e "manda o link"."""
    aba = _aba(
        hasMedia=True,
        paused=False,
        contentType="video",
        title="Dante Vs Vergil",
        channel="Canal do Zé",
        videoUrl="https://www.youtube.com/watch?v=6_kZHhqeByA",
    )
    linha = describe_tab(aba)
    assert "canal Canal do Zé" in linha
    assert "https://www.youtube.com/watch?v=6_kZHhqeByA" in linha


def test_canal_igual_ao_artista_nao_repete():
    """Em música o player publica o canal como artista — dizer duas vezes é ruído."""
    aba = _aba(hasMedia=True, paused=False, track="Last Resort", artist="Papa Roach",
               channel="papa roach")
    assert describe_tab(aba).count("Papa Roach") == 1
    assert "canal" not in describe_tab(aba)


def test_sem_link_nao_inventa_nada():
    """Aba de Spotify / home: a extensão manda vazio e a linha fica sem link."""
    aba = _aba(
        site="spotify",
        url="https://open.spotify.com/playlist/x",
        title="Rock Classics",
        videoUrl="",
    )
    assert describe_tab(aba) == "spotify · aberta · Rock Classics"


# ── tem o que controlar? ────────────────────────────────────────────────────


def test_home_do_youtube_nao_tem_o_que_tocar():
    aba = _aba(kind="home", url="https://www.youtube.com/", hasMedia=False)
    assert tab_has_media(aba) is False
    assert "nada aberto pra tocar" in describe_tab(aba)


def test_home_sem_sonda_ainda_e_reconhecida_pela_url():
    """A sonda pode falhar; o tipo de página vem da URL e não falha."""
    aba = _aba(kind="home", url="https://www.youtube.com/", probe="denied")
    assert tab_has_media(aba) is False


def test_aba_nao_sondada_e_NAO_SEI_nao_e_NAO_TEM():
    """A distinção que importa: recusar aqui seria pior que tentar."""
    aba = _aba(kind="watch", probe="denied")
    assert tab_has_media(aba) is None


def test_controle_ignora_a_home_e_pega_a_aba_com_player():
    ctx = {
        "browser_tabs": [
            _aba(tabId=10, kind="home", url="https://www.youtube.com/", hasMedia=False),
            _aba(tabId=11, kind="watch", hasMedia=True, paused=True),
        ]
    }
    escolhida = find_music_tab(ctx, "youtube", require_media=True)
    assert escolhida["tabId"] == 11


def test_controle_sem_nenhuma_aba_com_player_nao_tem_alvo():
    """É isto que faz o Neo dizer "não tem nada tocando" em vez de mentir."""
    ctx = {"browser_tabs": [_aba(kind="home", url="https://www.youtube.com/", hasMedia=False)]}
    assert find_music_tab(ctx, "youtube", require_media=True) is None
    # Para dar PLAY a mesma aba serve: navegar a home até o vídeo é o reuso.
    assert find_music_tab(ctx, "youtube") is not None


def test_a_aba_que_toca_ganha_da_primeira_da_lista():
    """Pegar a PRIMEIRA aba do site é o que fazia o comando cair na errada."""
    ctx = {
        "browser_tabs": [
            _aba(tabId=1, kind="watch", hasMedia=True, paused=True),
            _aba(tabId=2, kind="watch", hasMedia=True, paused=False, audible=True),
        ]
    }
    assert find_music_tab(ctx, "youtube")["tabId"] == 2


# ── música ou vídeo comum ───────────────────────────────────────────────────


def test_classificacao_do_youtube_aparece_na_linha():
    assert "música" in describe_tab(_aba(hasMedia=True, contentType="music"))
    assert "vídeo comum" in describe_tab(_aba(hasMedia=True, contentType="video"))


def test_sem_classificacao_nao_inventa():
    linha = describe_tab(_aba(hasMedia=True, contentType="unknown"))
    assert "música" not in linha and "vídeo comum" not in linha


def test_faixa_e_artista_vencem_o_titulo_da_aba():
    """O player já normaliza isso para o centro de mídia do SO; o título da aba
    vem com " - YouTube" grudado e às vezes com o contador de notificações."""
    aba = _aba(
        title="(76) Papa Roach - No Matter What - YouTube",
        track="No Matter What",
        artist="Papa Roach",
        hasMedia=True,
    )
    assert '"No Matter What" — Papa Roach' in describe_tab(aba)
    assert "(76)" not in describe_tab(aba)


# ── tempo ───────────────────────────────────────────────────────────────────


def test_posicao_e_duracao_em_minutos():
    aba = _aba(hasMedia=True, paused=False, currentTime=72.4, duration=245.0, progress=0.295)
    assert "1:12/4:05 (30%)" in describe_tab(aba)


def test_duracao_longa_ganha_hora():
    aba = _aba(hasMedia=True, currentTime=4.0, duration=4353.0)
    assert "0:04/1:12:33" in describe_tab(aba)


def test_ao_vivo_nao_finge_duracao():
    aba = _aba(hasMedia=True, paused=False, live=True, duration=None)
    linha = describe_tab(aba)
    assert "ao vivo" in linha and "/" not in linha.split("·")[-1]


def test_anuncio_e_avisado():
    """Sem isto o Neo anuncia o comercial como se fosse a faixa pedida."""
    aba = _aba(hasMedia=True, paused=False, adShowing=True, contentType="music")
    assert "ANÚNCIO no ar" in describe_tab(aba)


# ── degradação ──────────────────────────────────────────────────────────────


def test_aba_no_formato_antigo_nao_quebra():
    """Snapshot de uma extensão desatualizada — só os cinco campos originais."""
    aba = {"site": "youtube", "tabId": 1, "url": "u", "title": "T", "playing": True}
    assert tab_is_playing(aba) is True
    assert describe_tab(aba).startswith("youtube · TOCANDO")


def test_aba_de_spotify_sem_sonda_sai_inteira():
    aba = {"site": "spotify", "tabId": 9, "url": "u", "title": "Rock Classics", "probe": "skipped"}
    assert "spotify" in describe_tab(aba) and "Rock Classics" in describe_tab(aba)


def test_sonda_recusada_e_dita_em_vez_de_escondida():
    aba = _aba(kind="watch", probe="denied")
    assert "não consegui ler o player" in describe_tab(aba)


def test_lixo_no_snapshot_nao_derruba_a_lista():
    assert describe_tabs(["nao sou dict", None, _aba(hasMedia=True)]) == [
        describe_tab(_aba(hasMedia=True))
    ]


def test_a_lista_sai_da_mais_relevante_para_a_menos():
    linhas = describe_tabs(
        [
            _aba(tabId=1, kind="home", url="https://www.youtube.com/", hasMedia=False),
            _aba(tabId=2, kind="watch", hasMedia=True, paused=True),
            _aba(tabId=3, kind="watch", hasMedia=True, paused=False, audible=True),
        ]
    )
    assert "TOCANDO" in linhas[0]
    assert "pausado" in linhas[1]
    assert "nada aberto pra tocar" in linhas[2]
