"""Gatilhos de recall — regressão das 9 frases da sessão 7330e40c.

O usuário pediu de volta, nove vezes e de nove jeitos diferentes, uma resposta
longa que o Neo tinha escrito no mesmo dia. NENHUMA das nove casava com os
padrões: os 11 que existiam cobriam só o que o USUÁRIO tinha dito ("eu te
falei", "a gente conversou"), e todas as nove eram sobre o que o NEO tinha
escrito. Sem gatilho, `conversation_recall` nunca virava obrigatória, e o
modelo acabou lendo arquivos do disco e apresentando-os como a mensagem dele.

Medido no corpus real (2.053 mensagens do usuário): 2,9% de disparo.
Ver docs/context-recall-fixes.md §4.
"""

import pytest

from core.memory.recall_triggers import (
    is_conversation_recall_request,
    is_recall_continuation,
)

# As nove, literais, como ele digitou.
FRASES_DA_SESSAO_7330E40C = [
    "pega a ultima reanalise completa que fizemos e coloca tudo em um arquivo "
    ".md e deixa naquela pasta que a gente tava trabalhando, lembrando bem "
    "talvez esse arquivo ja ate exista lá né",
    "vc me falou aqui pelas mensagems",
    "tem uma que vc fez uma reanalise final e me mostrou, olha ai de novo",
    "vc me mandou pelo chat aqui mesmo na conversa",
    "tinha uma tabela do pior para menor pior",
    "nao é esse que eu queria, a ultima anaise era dessa forma “Reanálise "
    "Completa: Pior para o Brasileiro Médio do Mercado Privado",
    "vc nao fez direito, a sua reanalise ta igual a analise inicial, olha no "
    "seu historico de conversa e veja a reanalise completa",
    "ainda nao acertou, na reanalise o bravo ficou em 3 e na primeira ele "
    "ficou em segundo, se vc olhar o arquivo vai ver que ele parte com a "
    "primeira analise",
    "vc analisou tudo errado, olha a mensagem que vc mesmo escreveu por "
    "completa e me mostre aqui",
]


@pytest.mark.parametrize("frase", FRASES_DA_SESSAO_7330E40C)
def test_toda_frase_da_sessao_dispara_alguma_busca(frase: str) -> None:
    assert is_conversation_recall_request(frase) or is_recall_continuation(frase)


# Os padrões antigos, que precisam continuar valendo.
@pytest.mark.parametrize("frase", [
    "voce se lembra do douglas",
    "ta de boa, eu conversei algo contigo sobre a frança?",
    "a gente ja falou sobre isso?",
    "o que eu te falei sobre o meu treino de perna?",
])
def test_padroes_do_lado_do_usuario_seguem_valendo(frase: str) -> None:
    assert is_conversation_recall_request(frase)


@pytest.mark.parametrize("frase", [
    "nao foi essa conversa, foi antes ainda",
    "nao é esse que eu queria",
    "ainda nao acertou",
    "procura mais pra tras",
])
def test_rejeicoes_disparam_continuacao(frase: str) -> None:
    assert is_recall_continuation(frase)


# Conversa normal não pode virar busca — o custo de um falso positivo é uma
# chamada de tool a mais por turno.
@pytest.mark.parametrize("frase", [
    "liga o ar condicionado do quarto",
    "qual a previsao do tempo pra amanha",
    "cria um arquivo .md com essa lista",
    "toca aquela musica do metallica",
    "quanto é 3847 vezes 92",
    "bom dia, tudo certo por ai?",
    "abre o chrome pra mim",
    "me ajuda a escrever um email pro cliente",
])
def test_conversa_normal_nao_dispara(frase: str) -> None:
    assert not is_conversation_recall_request(frase)
    assert not is_recall_continuation(frase)


class TestMontaDeNovo:
    """"monta a tabela aqui de novo para eu ver" (sessão cbff8f81).

    Sem o gatilho, o Neo não tinha o texto em mãos e foi buscar a tabela no
    ARQUIVO do disco — que continha as notas que ele mesmo fabricara antes.
    """

    @pytest.mark.parametrize("frase", [
        "monta a tabela aqui de novo para eu ver",
        "mostra a analise de novo",
        "refaz aquela tabela novamente",
        "remonta o ranking outra vez",
    ])
    def test_dispara(self, frase):
        assert is_conversation_recall_request(frase) is True

    @pytest.mark.parametrize("frase", [
        "monta uma tabela nova com esses dados",
        "monta a tabela",
        "faz um resumo disso aqui",
        "cria uma lista de compras",
        "a nota ta diferente , achei que o alfa tivesse com 2,5",
    ])
    def test_nao_dispara(self, frase):
        assert is_conversation_recall_request(frase) is False
