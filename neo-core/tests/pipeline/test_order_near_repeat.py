"""Quase-repetição de ordem do ciclo — medido nas ordens REAIS que causaram o bug.

A chave do visited-set (`PipelineStep.key`) é igualdade literal, e quem escreve as
ordens do ciclo é um LLM que parafraseia. Na session-389114d4, interação 29 ("tem o
mais recente do INSS que foi um rombo ainda maior"), o verificador de promessa
sintetizou quatro ordens em sequência pedindo a MESMA pesquisa; a guarda só acordou
na quarta, quando ela saiu idêntica à terceira. Resultado: 4 rodadas de LLM, 30,1 s,
zero tool, 4 bolhas quase idênticas no chat e um "Parei aqui: o próximo passo seria
repetir um que já rodou neste turno" no lugar da resposta.

As ordens abaixo são verbatim do log.
"""

from core.pipeline.types import ORDER_COVERAGE, content_tokens, order_is_near_repeat

# session-389114d4, interação 29 — as quatro ordens sintetizadas, em ordem.
ORDEM_1 = (
    "levantar o histórico completo dos casos do Charlie e Bravo com status "
    "processual para fechar a camada 5"
)
ORDEM_2 = (
    "levantar o histórico completo dos casos do Charlie e Bravo com status "
    "processual real e fonte"
)
ORDEM_3 = (
    "levantar o histórico completo dos casos do Charlie e Bravo com status "
    "processual real, o que foi provado, o que foi anulado ou prescrito, e a "
    "fonte de cada caso"
)
ORDEM_4 = ORDEM_3   # verbatim: é aqui que a guarda antiga finalmente acordava


def _cobertura(novo: str, antigo: str) -> float:
    n = content_tokens(novo)
    return len(n & content_tokens(antigo)) / len(n)


def test_a_segunda_ordem_e_pega_pela_cobertura():
    """0,80 de cobertura: a paráfrase não engana mais a guarda."""
    assert _cobertura(ORDEM_2, ORDEM_1) == 0.8
    assert order_is_near_repeat(ORDEM_2, [ORDEM_1]) == ORDEM_1


def test_a_terceira_ordem_fica_ABAIXO_do_corte_e_isso_e_proposital():
    """A fronteira medida, registrada em vez de acomodada.

    Ela vai ficando mais específica a cada volta, e isso a AFASTA das anteriores:
    0,53 contra a ORDEM_1 e 0,67 contra a ORDEM_2 — as duas abaixo de 0,70. Vale
    lembrar qual comparação acontece de verdade: a ORDEM_2 foi a que levou o
    empurrão, e ordem devolvida não entra na lista do que rodou, então a ORDEM_3 é
    conferida só contra a ORDEM_1 (0,53).

    Baixar o limiar para pegar estas também compraria falso positivo em ordem que
    apenas compartilha o assunto — e falso positivo aqui não custa uma chamada
    barata, custa recusar um passo que ia trabalhar. Quem fecha este caso é o que
    vem depois: dois passos seguidos sem executar nada disparam o fechamento
    (`MAX_IDLE_CYCLE_STEPS`), que é o caminho exercitado em
    test_cycle_self_correction.py.
    """
    assert round(_cobertura(ORDEM_3, ORDEM_1), 2) == 0.53
    assert round(_cobertura(ORDEM_3, ORDEM_2), 2) == 0.67
    assert max(_cobertura(ORDEM_3, ORDEM_1), _cobertura(ORDEM_3, ORDEM_2)) < ORDER_COVERAGE
    assert order_is_near_repeat(ORDEM_3, [ORDEM_1, ORDEM_2]) is None


def test_a_quarta_ordem_e_igualdade_literal_nao_cobertura():
    """A repetição exata continua sendo trabalho do `visited`, não desta função."""
    assert ORDEM_4 == ORDEM_3
    assert order_is_near_repeat(ORDEM_4, [ORDEM_3]) == ORDEM_3


def test_ordem_que_pede_coisa_nova_passa():
    """O "não atrapalhar": alvo inédito tem interseção baixa e roda.

    Mesmo princípio do `_near_repeated_actuation`, que libera o comando quando um
    segmento pede caminho que ainda não apareceu.
    """
    anteriores = [ORDEM_1, ORDEM_2, ORDEM_3]
    for nova in (
        # O passo que o usuário realmente queria no fim daquela sessão.
        "escrever o veredito final com os dois rankings em veredito.md",
        # session-389114d4 interação 33 — outro assunto, mesma conversa.
        "apurar o status atualizado do INSS, das rachadinhas e do chocolate",
        "publicar o arquivo no catálogo e entregar no Desktop do usuário",
    ):
        assert order_is_near_repeat(nova, anteriores) is None, nova


def test_ordem_curta_demais_nao_entra_na_comparacao_por_conteudo():
    """Com 1-2 palavras de conteúdo, cobertura vira sorteio: só literal vale.

    Mesma trava que faz `df -h` ser comparado só por string no loop de shell.
    """
    assert order_is_near_repeat("continuar", [ORDEM_1]) is None
    assert order_is_near_repeat("tentar de novo", ["tentar de outro jeito"]) is None


def test_palavras_de_funcao_nao_criam_parentesco():
    """Sem tirar artigo/preposição, dois assuntos diferentes já empatam em ~50%."""
    a = "ligar a luz da sala e o ar do quarto"
    b = "mandar a mensagem para o cliente e o resumo da reunião"
    assert order_is_near_repeat(b, [a]) is None


def test_acento_e_caixa_nao_escondem_repeticao():
    assert order_is_near_repeat(
        "LEVANTAR O HISTORICO COMPLETO DOS CASOS DO CHARLIE E BRAVO COM STATUS PROCESSUAL",
        [ORDEM_1],
    ) == ORDEM_1
