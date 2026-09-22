"""Aterramento das citações: evidência que não está no texto não é evidência.

Sessão f33632b4: sem ler os PDFs, a primeira "conclusão" citou posições das
empresas que vieram de notícias. É exatamente a classe de erro que a checagem
determinística pega num subagente leitor.
"""

from core.documents.grounding import ChunkIndex, normalize, split_pages

TRECHO = (
    "[[página 12]]\n"
    "Vamos fixar metas progressivas de redução do custo logís-\n"
    "tico, começando pelo frete das entregas regionais.\n\n"
    "[[página 13]]\n"
    "Defendemos a revisão contínua das compras internas e dos contratos\n"
    "com fornecedores, de modo a impedir que eles fiquem acima do mercado."
)


def test_citacao_exata_acha_a_pagina_onde_esta_nao_a_que_o_modelo_disse():
    g = ChunkIndex(TRECHO).ground("revisão contínua das compras internas e dos contratos")
    assert g.ok and g.match == "exata"
    assert g.page == 13


def test_hifen_de_quebra_de_linha_e_espacos_nao_derrubam_a_citacao():
    g = ChunkIndex(TRECHO).ground("metas progressivas de redução do custo logístico")
    assert g.ok and g.page == 12


def test_aspas_e_reticencias_nas_pontas_sao_ignoradas():
    g = ChunkIndex(TRECHO).ground("“…começando pelo frete das entregas regionais…”")
    assert g.ok and g.page == 12


def test_citacao_inventada_e_descartada():
    g = ChunkIndex(TRECHO).ground("criaremos um bônus de 2% ao ano para os acionistas")
    assert not g.ok


def test_citacao_curta_demais_nao_prova_nada():
    assert not ChunkIndex(TRECHO).ground("dos contratos").ok


def test_citacao_que_atravessa_a_virada_de_pagina_vale_a_pagina_do_inicio():
    g = ChunkIndex(TRECHO).ground("começando pelo frete das entregas regionais. Defendemos a revisão contínua")
    assert g.ok and g.page == 12


def test_pontas_batem_com_uma_palavra_diferente_no_meio():
    g = ChunkIndex(TRECHO).ground(
        "Defendemos a revisão contínua das compras e dos contratos com fornecedores, de modo a impedir que eles fiquem acima do mercado"
    )
    assert g.ok and g.match == "pontas" and g.page == 13


def test_split_pages_sem_marcador_usa_a_pagina_padrao():
    assert split_pages("texto solto sem marcador", default_page=7) == [(7, "texto solto sem marcador")]


def test_normalize_e_estavel():
    assert normalize("  O  “Custo”\nLogís-\ntico ") == "o custo logístico"
