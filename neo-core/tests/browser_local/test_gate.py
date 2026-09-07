"""O portão do navegador local (docs/browser-local-plan.md §5.2).

Quatro quadrantes: o modelo escolhe local/servidor × o usuário pediu/não pediu.
Os dois erros doem, e por motivos diferentes:

- **falso positivo** (age no Chrome do usuário sem ele pedir) é a falha GRAVE —
  por isso a bateria de negativos aqui é maior que a de positivos;
- **falso negativo** (pediu para ver e o Neo faz escondido no servidor) quebra
  a única razão de existir do modo local.
"""

from __future__ import annotations

import pytest

from core.browser_local.gate import local_browser_requested, media_playback_requested


from tests.browser_local.phrases import (
    NAO_PEDIU,
    NAO_TOCA_MIDIA,
    PEDIU,
    TOCA_MIDIA,
)


@pytest.mark.parametrize("frase", PEDIU)
def test_pedido_explicito_liga_o_local(frase):
    assert local_browser_requested(frase) is True


@pytest.mark.parametrize("frase", NAO_PEDIU)
def test_sem_pedido_explicito_fica_no_servidor(frase):
    assert local_browser_requested(frase) is False


def test_vazio_e_none_nao_ligam_o_local():
    assert local_browser_requested("") is False
    assert local_browser_requested(None) is False  # type: ignore[arg-type]


def test_acento_e_caixa_nao_importam():
    assert local_browser_requested("NO MEU NAVEGADOR, abre o site") is True
    assert local_browser_requested("no meu navegador") == local_browser_requested(
        "NO MEU NAVEGADÔR".replace("Ô", "O")
    )


def test_palavra_inteira_nao_substring():
    # "aba" não pode casar dentro de outra palavra ("abacaxi", "sabatina").
    assert local_browser_requested("compra abacaxi aqui no mercado") is False


# ── tolerância a erro de digitação ──────────────────────────────────────────
def test_transposicao_adjacente_conta_como_uma_edicao():
    """Trocar duas letras VIZINHAS é UM erro de digitação, não dois.

    Sem isso "chorme" fica a distância 2 de "chrome" e uma palavra de 6 letras
    nunca seria alcançada pelo orçamento dela."""
    from core.browser_local.gate import _edit_distance

    assert _edit_distance("chorme", "chrome") == 1
    assert _edit_distance("navegdor", "navegador") == 1     # letra faltando
    assert _edit_distance("navegaador", "navegador") == 1   # letra sobrando
    assert _edit_distance("navegador", "navegador") == 0


def test_nevagador_custa_duas_edicoes_e_por_isso_o_orcamento_e_2():
    """O typo que quebrou a sessão d3be77e1 NÃO é uma transposição: em
    "nevagador" o 'a' e o 'e' trocados estão separados pelo 'v', o que são duas
    substituições. Palavra de 9 letras só é alcançada porque o orçamento dela é
    2 — se alguém baixar esse orçamento para 1, o caso original volta a falhar."""
    from core.browser_local.gate import _edit_distance

    assert _edit_distance("nevagador", "navegador") == 2
    assert local_browser_requested("abre no meu nevagador") is True


def test_edit_distance_desiste_cedo_em_palavra_distante():
    """O cap existe para o laço não pagar matriz inteira por token do corpus."""
    from core.browser_local.gate import _edit_distance

    assert _edit_distance("abacaxi", "navegador", cap=2) > 2
    assert _edit_distance("x" * 40, "chrome", cap=1) > 1


def test_typo_nao_dispensa_a_ancora_de_posse():
    """A tolerância afrouxa a ORTOGRAFIA, nunca a regra de duas partes: sem
    âncora de posse, navegador escrito torto continua sendo o servidor."""
    assert local_browser_requested("procura no nevagador") is False
    assert local_browser_requested("busca no chorme quanto custa") is False


def test_extensao_de_arquivo_nao_e_extensao_de_navegador():
    """Mesma palavra, dois sentidos. No sentido de arquivo, abrir o navegador do
    usuário seria agir na tela dele por causa de uma pergunta sobre formato."""
    assert local_browser_requested("usa a extensao") is True
    assert local_browser_requested("qual a extensao desse arquivo") is False
    assert local_browser_requested("salva com extensao .csv") is False


# ── tocar mídia: a terceira afordância que vale sozinha ─────────────────────
#
# `_EXTENSION` e `_MACHINE_OF_USER` já valiam sem âncora de posse pelo mesmo
# motivo: não é preferência do usuário, é o alvo não existir no servidor.
# Reproduzir áudio/vídeo é o terceiro caso — e o único que o cérebro também
# escreve, na TAREFA, não só na frase.


@pytest.mark.parametrize("frase", TOCA_MIDIA)
def test_pedido_de_reproducao_e_reconhecido(frase):
    assert media_playback_requested(frase) is True


@pytest.mark.parametrize("frase", NAO_TOCA_MIDIA)
def test_o_que_nao_e_reproducao_fica_no_servidor(frase):
    assert media_playback_requested(frase) is False


def test_reproducao_exige_verbo_E_midia():
    """Só o verbo abriria o navegador do usuário por qualquer "toca"; só a
    palavra de mídia, por qualquer conversa sobre música."""
    assert media_playback_requested("toca ai") is False
    assert media_playback_requested("essa musica e boa") is False
    assert media_playback_requested("toca essa musica") is True


def test_reproducao_nao_depende_da_frase_pedir_navegador():
    """É este o ponto: a frase do turno real não tinha palavra de navegador
    nenhuma, e mesmo assim o destino certo era a tela dele."""
    frase = "to de boa, coloca uma musica do meu mix para tocar ai"
    assert local_browser_requested(frase) is False
    assert media_playback_requested(frase) is True


def test_vazio_e_none_nao_sao_reproducao():
    assert media_playback_requested("") is False
    assert media_playback_requested(None) is False  # type: ignore[arg-type]


def test_toque_e_com_qu():
    """`toc` sozinho não alcança o imperativo ("toque"), que é justamente a
    forma mais comum numa instrução de navegação — e foi assim que "não toque
    no botão" passou reto como se fosse play."""
    assert media_playback_requested("abre o youtube e nao toque em nada") is False
    assert media_playback_requested("toque a musica") is True


def test_playlist_nao_e_dar_play():
    """`_has_word` casa expressão por SUBSTRING — foi por isso que as formas com
    "play" saíram de `_PLAYBACK_VERBS` para `_EXPLICIT_PLAY`, que usa fronteira
    de palavra. Com "dar play" na lista de verbos, "o link da playlist" era uma
    ordem de tocar e sequestrava o navegador do usuário."""
    assert media_playback_requested("copia o link da playlist") is False
    assert media_playback_requested("toca no botao da playlist") is False
    assert media_playback_requested("dar play na playlist") is True
