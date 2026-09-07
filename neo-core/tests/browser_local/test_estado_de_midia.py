"""O estado do player não se lê no rótulo do botão (job 717a8fbb).

Pedido: "dar play no vídeo do Super Xandão". O vídeo ESTAVA tocando. O snapshot
mostrava `[11] button "Pausa atalho do teclado k"` — que é como o YouTube nomeia
a AÇÃO do botão, justamente quando está tocando. O modelo leu "Pausa" como
estado, concluiu que estava parado e clicou. Aí o rótulo virou "Reproduzir", ele
leu de novo, clicou de novo — e como o botão é um INTERRUPTOR, cada leitura
errada inverteu o que estava valendo. Vinte e cinco passos assim, com o usuário
vendo o vídeo piscar, e o job morreu no teto sem nunca ter "dado play".

As guardas antigas não pegam: a tela MUDA a cada clique (o tempo corre), então
"a tela NÃO mudou" nunca dispara; e o clique volta `ok:true`, porque o evento
foi mesmo despachado.

O conserto é dar o FATO em vez do rótulo: o device lê `video.paused` do próprio
elemento e manda em `snapshot["midia"]`.
"""

from __future__ import annotations

from core.browser_local.driver import BrowserLocalDriver, _SYSTEM


def _snapshot(midia=None):
    snap = {
        "url": "https://www.youtube.com/watch?v=FAxC894afIY",
        "title": "SUPER XANDÃO ASSISTINDO ATTACK ON TITAN",
        "scroll": {"y": 0, "height": 0},
        # Os rótulos REAIS do passo 1 do job 717a8fbb.
        "elements": [
            {"ref": 11, "tag": "button", "text": "Pausa atalho do teclado k"},
            {"ref": 13, "tag": "div", "text": "1 Minutos 33 Segundos de 20 Minutos 54 Segundos"},
        ],
        "text": "",
    }
    if midia is not None:
        snap["midia"] = midia
    return snap


TOCANDO = {"tipo": "video", "tocando": True, "mudo": False, "pos": "1:33", "dur": "20:54"}
PARADO = {"tipo": "video", "tocando": False, "mudo": False, "pos": "0:00", "dur": "20:54"}
MUDO = {"tipo": "video", "tocando": True, "mudo": True, "pos": "0:08", "dur": "20:54"}


def test_tocando_aparece_como_estado_e_nao_como_rotulo():
    render = BrowserLocalDriver._render(_snapshot(TOCANDO))
    assert "Mídia na página:" in render
    assert "TOCANDO AGORA" in render
    assert "1:33 de 20:54" in render


def test_parado_e_dito_com_todas_as_letras():
    render = BrowserLocalDriver._render(_snapshot(PARADO))
    assert "PARADO (pausado)" in render
    assert "TOCANDO AGORA" not in render


def test_mudo_e_um_fato_separado_de_estar_tocando():
    """"Não estou ouvindo" tem duas causas e conserto diferente: dar play não
    resolve som mudo, e tirar o mudo não resolve vídeo parado."""
    render = BrowserLocalDriver._render(_snapshot(MUDO))
    assert "TOCANDO AGORA" in render
    assert "SEM SOM (mudo)" in render


def test_estado_vem_antes_da_lista_de_elementos():
    """Enterrada depois de 74 elementos a linha não serve: quando a tarefa é
    sobre tocar/pausar, ela é a única que importa."""
    render = BrowserLocalDriver._render(_snapshot(TOCANDO))
    assert render.index("Mídia na página:") < render.index("Elementos (")


def test_pagina_sem_midia_nao_ganha_linha_nenhuma():
    """A esmagadora maioria das páginas não tem player — nada de ruído nelas."""
    assert "Mídia na página" not in BrowserLocalDriver._render(_snapshot())


def test_midia_malformada_nao_derruba_o_render():
    """O device é outro processo e outra versão: campo faltando não pode virar
    exceção no meio de um passo."""
    render = BrowserLocalDriver._render(_snapshot({"tocando": True}))
    assert "TOCANDO AGORA" in render
    assert "? de ?" in render


def test_o_prompt_ensina_a_nao_confiar_no_rotulo_do_botao():
    """Sem a regra, o fato no snapshot ainda perde para o rótulo: foi lendo
    'Pausa' que o modelo decidiu clicar."""
    assert "Mídia na página" in _SYSTEM
    assert "INTERRUPTOR" in _SYSTEM
