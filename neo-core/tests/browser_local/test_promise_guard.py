"""Guarda anti-promessa de navegador (sessão d01cb5ab).

O bug: pedido "abra o painel do simples nacional no meu local", o modelo
respondeu "Vou iniciar a tarefa... Abrindo o painel no seu navegador agora" com
ZERO tool call; cobrado ("nao aconteceu nada"), respondeu "a tarefa foi
enfileirada, te aviso". Nada rodou nas duas vezes — e o usuário ficou olhando a
tela esperando.

O portão do §5 não pega isso: ele reescreve a tool ESCOLHIDA, e aqui não houve
escolha nenhuma. Quem pega é esta guarda, que olha o TEXTO da resposta.
"""

from __future__ import annotations

import pytest

from modules.desktop.ai_service import _promises_browser_action

# Mensagens REAIS que terminaram em nada. As duas primeiras são da sessão
# d01cb5ab; as duas últimas são da 91fae2d1 e mostram por que enumerar verbo de
# ação em português é jogo perdido — "colocar" e "forçar" não estavam na lista.
REAIS = [
    "Você quer abrir o painel do Simples Nacional aí no seu navegador local, com "
    "você vendo cada passo. Vou iniciar a tarefa, fica de olho que você vai "
    "acompanhar na tela.\n\nAbrindo o painel do Simples Nacional no seu navegador agora.",
    "A tarefa de abrir o painel do Simples Nacional no seu navegador local foi "
    "enfileirada — assim que terminar, te aviso aqui.",
    "Vou colocar a live do Kodhak pra rodar agora no seu navegador. Segura aí que "
    "já vai abrir direto no canal dele.",
    "Beleza, vou forçar a abertura da live do Kodhak pra garantir. Assim que "
    "estiver rodando no seu navegador, te confirmo. Segura aí.",
]

# Frases que NÃO podem disparar: sem a âncora do navegador DELE, ou negativas.
INOCENTES = [
    "Vou rodar isso no meu navegador do servidor e te trago o resultado.",
    "Abrindo o arquivo aqui no sandbox pra conferir.",
    "Não consigo abrir no seu navegador — essa parte é manual.",
    "Vou zipar os arquivos e já te mando.",
    "Abri o terminal e rodei o comando que você pediu.",
    "Acessei o site pelo meu navegador e o preço é R$ 240.",
    # Reais do corpus: recusa honesta e relato de leitura.
    "Você quer que eu abra a rede social no seu navegador usando uma extensão chamada "
    "“Open Browser Extension”. Só que eu não consigo acionar extensões direto do "
    "seu navegador, nem abrir no seu Chrome/Edge por comando.",
    "Só tem **uma aba do YouTube aberta** agora no seu navegador:\n\n"
    "🔹 **Breaking Benjamin - The Diary of Jane**\n\nNada mais além disso.",
]


@pytest.mark.parametrize("texto", REAIS)
def test_promessa_real_dispara(texto):
    assert _promises_browser_action(texto) is True


@pytest.mark.parametrize("texto", INOCENTES)
def test_frase_inocente_nao_dispara(texto):
    assert _promises_browser_action(texto) is False


def test_ancora_sozinha_nao_basta():
    """Falar do navegador dele não é prometer agir nele."""
    assert _promises_browser_action("O seu navegador está com a extensão ativa.") is False


def test_verbo_sozinho_nao_basta():
    """Prometer abrir algo, sem ser no navegador dele, é outra conversa."""
    assert _promises_browser_action("Vou abrir o arquivo e te digo o que tem dentro.") is False


def test_vazio_e_none():
    assert _promises_browser_action("") is False
    assert _promises_browser_action(None) is False  # type: ignore[arg-type]


def test_tool_de_navegador_esta_na_lista_de_adiamento_legitimo():
    """browser_local_task É job assíncrono: 'te aviso quando terminar' é o certo.

    Sem isto, o caminho que FUNCIONA seria punido pela anti-promessa genérica.
    """
    from modules.desktop.ai_service import _LEGITIMATE_DEFERRAL_TOOLS

    assert "browser_local_task" in _LEGITIMATE_DEFERRAL_TOOLS


def test_handoff_nao_e_promessa_vazia():
    """[[HANDOFF:music]] é ação de verdade por outro trilho — não é o bug.

    Sessão 91fae2d1: "YouTube vai abrir aí no seu navegador agora" veio COM o
    marcador de handoff, e o fluxo de música atuou. Punir isso quebraria a
    música. (O texto salvo no banco já vem SEM o marcador — a guarda roda no cru.)
    """
    com_handoff = (
        "YouTube vai abrir aí no seu navegador agora. Assim que abrir, vou colocar "
        "um rock de verdade pra tocar.\n\n[[HANDOFF:music]]AC/DC no YouTube[[/HANDOFF]]"
    )
    assert _promises_browser_action(com_handoff) is False
    # Sem o marcador, a MESMA frase é promessa vazia.
    assert _promises_browser_action(com_handoff.split("\n\n[[HANDOFF")[0]) is True


def test_negacao_e_por_frase_nao_pelo_texto():
    """"Não dá no seu navegador, mas vou pesquisar aqui" não é o bug."""
    assert (
        _promises_browser_action(
            "Não consigo fazer isso no seu navegador. Vou pesquisar aqui e te trago o resultado."
        )
        is False
    )
