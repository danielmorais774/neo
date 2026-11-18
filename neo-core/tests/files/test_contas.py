"""Conferência de contas da nota e da planilha (session-3b19d27a).

A nota: 8 itens, "Valor a pagar R$ 213,74", dinheiro 150,00 + cartão 63,74. A
visão leu a vodka como "1 × 39,99 = 33,99". Com 33,99 os itens dão 207,74; com
39,99, os 213,74 da nota. O modelo concluiu o inverso e entregou a planilha com
TOTAL = SUM(F6:F13) = 207,74 ao lado de "Total pago" = 213,74.
"""

from __future__ import annotations

import zipfile
from decimal import Decimal as D
from pathlib import Path

import pytest

from core.files.contas import (
    Item,
    Totais,
    brl,
    conferir,
    conferir_planilha,
    de_dicionarios,
    numero,
)


def _nota(vodka_total: str = "39,99", gelo2_total: str = "5,99") -> list[Item]:
    linhas = [
        ("CERVEJA PILSEN SKOL MULTIPACK C15 LT 269", "1", "44,99", "44,99"),
        ("CERVEJA PILSEN SKOL MULTIPACK C15 LT 269", "1", "44,99", "44,99"),
        ("LINGUICA SUINA PERDIGAO kg", "2,168", "23,99", "52,01"),
        ("GELO CUBO 3kg", "1", "5,99", "5,99"),
        ("GELO CUBO 3kg", "1", "5,99", gelo2_total),
        ("ENERGETICO RED WAVE 2L", "1", "17,99", "17,99"),
        ("VODKA NAC SMIRNOFF GF 998ML TRI DESTILADA", "1", "39,99", vodka_total),
        ("COPO VIDRO AMERICANO NADIR 190ML", "1", "1,79", "1,79"),
    ]
    return [Item(descricao=d, qtd=numero(q), unitario=numero(u), total=numero(t)) for d, q, u, t in linhas]


def _totais(**kw) -> Totais:
    base = dict(
        valor_a_pagar=D("213.74"), qtde_itens=8, pagamentos=[D("150"), D("63.74")],
    )
    base.update(kw)
    return Totais(**base)


# ── números ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "bruto, esperado",
    [
        ("39,99", D("39.99")), ("R$ 213,74", D("213.74")), ("1.234,56", D("1234.56")),
        ("1,234.56", D("1234.56")), ("1.234", D("1234")), (39.99, D("39.99")), (2, D("2")),
        ("2,168", D("2.168")), ("-3,50", D("-3.50")), ("abc", None), ("", None), (None, None),
        (True, None),
    ],
)
def test_numero(bruto, esperado):
    assert numero(bruto) == esperado


def test_brl():
    assert brl(D("213.74")) == "213,74"
    assert brl(D("1234.5")) == "1.234,50"
    assert brl(D("-6")) == "-6,00"


# ── a nota ──────────────────────────────────────────────────────────────────

def test_leitura_certa_fecha_e_cobre():
    v = conferir(_nota(), _totais())
    assert v.ok
    assert v.fecha is True
    assert v.cobre is True
    assert v.pagamentos_batem is True
    assert "fecha com o valor a pagar" in v.texto
    assert "não precisa reler a nota" in v.texto


def test_vodka_da_sessao_deduz_39_99_com_confianca_alta():
    v = conferir(_nota(vodka_total="33,99"), _totais())
    assert v.fecha is False
    assert v.diferenca == D("6.00")
    assert [(i, e, l) for i, e, l in v.linhas] == [(6, D("39.99"), D("33.99"))]
    assert len(v.correcoes) == 1
    c = v.correcoes[0]
    assert (c.item, c.de, c.para, c.confianca) == (6, D("33.99"), D("39.99"), "alta")
    # A cobertura também sai: com a correção, as 8 linhas explicam o total.
    assert v.cobre is True
    assert "VODKA" in v.texto and "33,99 → 39,99" in v.texto


def test_digito_trocado_sem_linha_errada():
    """Gelo lido 3,99 com unitário 3,99: a linha bate, só o fechamento acusa."""
    itens = _nota()
    itens[4] = Item(descricao="GELO CUBO 3kg", qtd=D("1"), unitario=D("3.99"), total=D("3.99"))
    v = conferir(itens, _totais())
    assert v.fecha is False
    assert v.diferenca == D("2.00")
    assert any(c.item == 4 and c.para == D("5.99") for c in v.correcoes)


def test_unitario_mal_lido_com_total_certo():
    """A soma fecha: o total da linha está certo, o unitário é que foi mal lido."""
    itens = _nota()
    itens[5] = Item(descricao="ENERGETICO RED WAVE 2L", qtd=D("1"), unitario=D("11.99"), total=D("17.99"))
    v = conferir(itens, _totais())
    assert v.fecha is True
    assert len(v.correcoes) == 1
    assert v.correcoes[0].confianca == "media"
    assert v.correcoes[0].para == D("17.99")


def test_linha_faltando():
    itens = _nota()
    del itens[7]                                    # o copo de 1,79
    v = conferir(itens, _totais())
    assert v.fecha is False
    assert v.cobre is False
    assert "Foram lidas 7 linhas" in v.texto and "falta ler linha" in v.texto


def test_valor_a_pagar_mal_lido():
    """Itens e pagamentos dão 213,74 e o valor a pagar foi lido 218,74."""
    v = conferir(_nota(), _totais(valor_a_pagar=D("218.74"), pagamentos=[D("150"), D("63.74")]))
    assert v.fecha is False
    assert v.correcoes[0].item == -1
    assert v.correcoes[0].para == D("213.74")
    assert v.correcoes[0].confianca == "alta"


def test_desconto_e_troco():
    itens = [Item(descricao="A", qtd=D("2"), unitario=D("10"), total=D("20"))]
    v = conferir(itens, Totais(valor_a_pagar=D("18"), descontos=D("2"), pagamentos=[D("20")], troco=D("2")))
    assert v.ok


def test_item_por_peso_arredonda_ou_trunca():
    # 2,168 × 23,99 = 52,00932 → 52,01 (arredonda) ou 52,00 (trunca): os dois valem.
    for total in ("52,01", "52,00"):
        it = Item(descricao="LINGUICA", qtd=D("2.168"), unitario=D("23.99"), total=numero(total))
        assert conferir([it], Totais()).linhas == []


def test_duas_linhas_erradas_nao_deduz_com_confianca_alta():
    itens = _nota(vodka_total="33,99")
    itens[5] = Item(descricao="ENERGETICO", qtd=D("1"), unitario=D("17.99"), total=D("11.99"))
    v = conferir(itens, _totais())
    assert v.fecha is False
    assert not any(c.confianca == "alta" for c in v.correcoes)


def test_sem_totais_so_confere_as_linhas():
    v = conferir(_nota(), Totais())
    assert v.fecha is None
    assert v.ok


def test_de_dicionarios_aceita_texto_e_numero_e_aponta_problema():
    itens, totais, problemas = de_dicionarios(
        [
            {"descricao": "A", "qtd": "1", "unitario": "39,99", "total": "R$ 39,99"},
            {"descricao": "B", "total": 1.79},
            {"descricao": "C"},
            "lixo",
        ],
        {"valor_a_pagar": "41,78", "qtde_itens": "2", "pagamentos": ["41,78"]},
    )
    assert [i.total for i in itens] == [D("39.99"), D("1.79")]
    assert totais.qtde_itens == 2
    assert problemas == ["item 3: sem 'total' numérico", "item 4: não é um objeto"]
    assert conferir(itens, totais).ok


# ── a planilha ──────────────────────────────────────────────────────────────

def _xlsx(caminho: Path, abas: dict[str, list[list]]) -> Path:
    """Um .xlsx mínimo, como o openpyxl grava: fórmula SEM valor guardado."""

    def _col(i: int) -> str:
        s = ""
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    def _sheet(linhas: list[list]) -> str:
        rows = []
        for li, linha in enumerate(linhas, start=1):
            cells = []
            for ci, v in enumerate(linha, start=1):
                ref = f"{_col(ci)}{li}"
                if v is None:
                    continue
                if isinstance(v, str) and v.startswith("="):
                    cells.append(f'<c r="{ref}"><f>{v[1:]}</f></c>')
                elif isinstance(v, str):
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            rows.append(f'<row r="{li}">{"".join(cells)}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(rows)}</sheetData></worksheet>'
        )

    with zipfile.ZipFile(caminho, "w") as z:
        sheets, rels = [], []
        for n, (nome, linhas) in enumerate(abas.items(), start=1):
            sheets.append(f'<sheet name="{nome}" sheetId="{n}" r:id="rId{n}"/>')
            rels.append(
                f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="/xl/worksheets/sheet{n}.xml"/>'
            )
            z.writestr(f"xl/worksheets/sheet{n}.xml", _sheet(linhas))
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(sheets)}</sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{"".join(rels)}</Relationships>',
        )
    return caminho


def _planilha_da_sessao(vodka_total: float) -> dict[str, list[list]]:
    """A planilha que o Neo entregou no turno 2 (linhas 1–19 da aba Notinha)."""
    itens = [
        ["7891149109043", "CERVEJA PILSEN SKOL MULTIPACK C15 LT 269", 1, "UN", 44.99, 44.99],
        ["7891149109043", "CERVEJA PILSEN SKOL MULTIPACK C15 LT 269", 1, "UN", 44.99, 44.99],
        ["2479", "LINGUICA SUINA PERDIGAO kg", 2.168, "KG", 23.99, 52.01],
        ["95815", "GELO CUBO 3kg", 1, "UN", 5.99, 5.99],
        ["95815", "GELO CUBO 3kg", 1, "UN", 5.99, 5.99],
        ["7898213970363", "ENERGETICO RED WAVE 2L", 1, "UN", 17.99, 17.99],
        ["7893218000473", "VODKA NAC SMIRNOFF GF 990ML TRI DESTILADA", 1, "UN", 39.99, vodka_total],
        ["77736", "COPO VIDRO AMERICANO NADIR 190ML", 1, "UN", 1.79, 1.79],
    ]
    notinha = [
        ["ATACADO DO VALE COMERCIO DE ALIMENTOS LTDA"],
        ["CNPJ 32.768.826/0003-53"],
        ["Cupom: 15/08/26 18:18:08"],
        [],
        ["Código", "Descrição", "Qtd", "Un", "Vl Unit (R$)", "Vl Total (R$)"],
        *itens,
        ["TOTAL", None, None, None, None, "=SUM(F6:F13)"],
        [],
        ["Forma de pagamento"],
        ["Dinheiro", 150],
        ["Cartão de crédito", 63.74],
        ["Total pago", "=SUM(B17:B18)"],
    ]
    resumo = [
        ["Resumo por produto"],
        ["Descrição", "Qtd total", "Total (R$)"],
        ["CERVEJA", 2, 89.98],
        ["TOTAL", None, "=SUM(C3:C3)"],
    ]
    return {"Notinha": notinha, "Resumo": resumo}


def test_planilha_da_sessao_acusa_a_vodka(tmp_path):
    caminho = _xlsx(tmp_path / "notinha-mercado.xlsx", _planilha_da_sessao(33.99))
    aviso = conferir_planilha(caminho, "notinha-mercado.xlsx")
    assert aviso is not None
    assert "aba 'Notinha'" in aviso
    assert "207,74" in aviso and "'Total pago' em B19 = 213,74" in aviso
    assert "'TOTAL' em F14" in aviso
    assert "linha 12" in aviso and "33,99 → 39,99" in aviso
    assert "salve de novo" in aviso


def test_planilha_certa_nao_acusa(tmp_path):
    caminho = _xlsx(tmp_path / "notinha.xlsx", _planilha_da_sessao(39.99))
    assert conferir_planilha(caminho, "notinha.xlsx") is None


def test_planilha_sem_tabela_de_itens_nao_e_conferida(tmp_path):
    """Dois "Total" com valores diferentes numa planilha que não é nota."""
    caminho = _xlsx(tmp_path / "orcamento.xlsx", {"Mês": [
        ["Receitas", "Valor"], ["Salário", 5000], ["Total", "=SUM(B2:B2)"],
        ["Despesas", "Valor"], ["Aluguel", 1500], ["Total", "=SUM(B5:B5)"],
    ]})
    assert conferir_planilha(caminho, "orcamento.xlsx") is None


def test_formula_que_nao_sei_calcular_nao_acusa(tmp_path):
    abas = _planilha_da_sessao(39.99)
    abas["Notinha"][13] = ["TOTAL", None, None, None, None, "=SUMPRODUCT(C6:C13,E6:E13)"]
    caminho = _xlsx(tmp_path / "n.xlsx", abas)
    assert conferir_planilha(caminho, "n.xlsx") is None


def test_total_da_linha_por_formula(tmp_path):
    """Total da linha como =C6*E6 também é calculado."""
    abas = {"N": [
        ["Descrição", "Qtd", "Vl Unit", "Vl Total"],
        ["A", 2, 10, "=B2*C2"],
        ["B", 1, 5, "=B3*C3"],
        ["Valor a pagar", None, None, 26],
    ]}
    aviso = conferir_planilha(_xlsx(tmp_path / "f.xlsx", abas), "f.xlsx")
    assert aviso is not None and "25,00" in aviso and "26,00" in aviso


def test_arquivo_que_nao_e_planilha(tmp_path):
    p = tmp_path / "x.xlsx"
    p.write_bytes(b"nao sou zip")
    assert conferir_planilha(p, "x.xlsx") is None
    assert conferir_planilha(tmp_path / "nao-existe.xlsx", "nao-existe.xlsx") is None
    assert conferir_planilha(p, "x.docx") is None
