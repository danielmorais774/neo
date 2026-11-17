"""Session-94621cc8 (17/11): baixar, recortar e abrir o meme no player.

* turno 2: o follow-up abriu o vídeo no player "para anexar no chat" (ninguém pediu);
* turno 3: o resumo do plano entregou o trecho como link markdown, sem o cartão;
* turno 4: `open -a QuickTime\\\\ Player …` (barra dupla do classificador) falhou, e
  a correção do cérebro foi barrada como "quase repetida" do passo que FALHOU.
"""

from __future__ import annotations

import pytest

from modules.desktop.ai_service import (
    _barra_dupla_para_simples,
    _followup_abre_sem_pedido,
    _links_locais_para_cartao,
    _near_repeated_actuation,
    _repeated_actuation,
)

_TRECHO = "/Users/daniel/Downloads/trecho_jessica.mp4"


def test_correcao_de_comando_que_falhou_nao_e_quase_repeticao():
    passos = [{
        "command": f'open -a QuickTime\\\\ Player "{_TRECHO}"',
        "ok": False,
        "error": "The file /Users/daniel/Player does not exist.",
    }]
    corrigido = f'open -a "QuickTime Player" "{_TRECHO}"'
    assert _near_repeated_actuation(passos, corrigido) == []
    # O comando idêntico continua barrado pela igualdade exata.
    assert _repeated_actuation(passos, passos[0]["command"])


def test_passo_que_deu_certo_continua_cobrindo():
    passos = [{"command": f'ls -la "{_TRECHO}"', "ok": True}]
    assert _near_repeated_actuation(passos, f'ls -la "{_TRECHO}" ') == [1]


def test_barra_dupla_antes_de_espaco_vira_simples():
    assert _barra_dupla_para_simples(f'open -a QuickTime\\\\ Player "{_TRECHO}"') == (
        f'open -a QuickTime\\ Player "{_TRECHO}"'
    )
    assert _barra_dupla_para_simples("ls ~/Library/Application\\ Support") == (
        "ls ~/Library/Application\\ Support"
    )


def test_link_markdown_de_arquivo_local_vira_cartao():
    texto = (
        "O trecho de 7 segundos do vídeo foi processado com sucesso.\n\n"
        f"Você pode acessar o arquivo cortado aqui: [{_TRECHO}]({_TRECHO})"
    )
    out = _links_locais_para_cartao(texto)
    assert f"[[FILE:{_TRECHO}]]" in out
    assert "](" not in out


def test_link_com_colchetes_do_yt_dlp_no_nome():
    caminho = "/Users/daniel/Downloads/Já acabou Jessica [bOY3qic5_t4].mp4"
    out = _links_locais_para_cartao(f"aqui: [o vídeo](<{caminho}>)")
    assert out == f"aqui: [[FILE:{caminho}]]"


def test_link_da_web_fica():
    texto = "veja [o vídeo](https://www.youtube.com/watch?v=bOY3qic5_t4)"
    assert _links_locais_para_cartao(texto) == texto


@pytest.mark.parametrize("pedido", [
    'baixa o vídeo meme do YouTube "Já acabou, Jéssica?" e me manda aqui no chat',
    "corta nos 7 segundos e me manda",
])
def test_followup_nao_abre_o_que_ninguem_pediu(pedido):
    assert _followup_abre_sem_pedido(f"open '{_TRECHO}'", pedido)


@pytest.mark.parametrize("pedido", ["ja abre ele no player para mim", "toca o vídeo"])
def test_followup_abre_quando_pediu(pedido):
    assert not _followup_abre_sem_pedido(f"open '{_TRECHO}'", pedido)


def test_followup_que_nao_abre_passa():
    assert not _followup_abre_sem_pedido(f"ls -la '{_TRECHO}'", "me manda aqui no chat")
