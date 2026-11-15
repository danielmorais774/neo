"""Scripts da skill docx contra um template no formato do MAPA da session-20cb02b6.

O template de verdade é dele e não entra no repositório: este é montado aqui com a
mesma anatomia — logo numa imagem do cabeçalho de página, tabela de identificação
com células mescladas ("R.A." ocupando duas colunas, "Prazo:" sozinho), rótulo em
negrito com o valor num run separado, e a linha de formatação exigida.

Os defeitos medidos na sessão viram asserções:
* turno 2 apagou o logo ao escrever no cabeçalho de página;
* turno 4 respondeu "R.A. 12345678-9" com a célula do R.A. vazia;
* turno 4 redigitou o conteúdo e perdeu os acentos ("QUESTAO", "Academico");
* turno 5 apagou o título e as instruções do template.

Precisam de python-docx (está na imagem do workbench; no Mac: `uv pip install
python-docx` no venv que rodar o pytest).
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")

from docx.shared import Cm  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "docx" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import docx_conferir  # noqa: E402
import docx_mapa  # noqa: E402
import docx_preencher  # noqa: E402
import docx_renderizar  # noqa: E402

REGRA = ("Formatação exigida para esta atividade: documento Word, Fonte Arial ou Times New Roman "
         "tamanho 12, Espaçamento entre linhas 1,5, texto justificado;")


def _png(caminho: Path) -> Path:
    largura, altura = 40, 4
    linhas = b"".join(b"\x00" + b"\x10\x60\xb0" * largura for _ in range(altura))

    def bloco(tipo: bytes, dados: bytes) -> bytes:
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)

    caminho.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + bloco(b"IHDR", struct.pack(">IIBBBBB", largura, altura, 8, 2, 0, 0, 0))
        + bloco(b"IDAT", zlib.compress(linhas))
        + bloco(b"IEND", b"")
    )
    return caminho


def _template(tmp_path: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    cab = doc.sections[0].header
    cab.is_linked_to_previous = False
    cab.paragraphs[0].add_run().add_picture(str(_png(tmp_path / "logo.png")), width=Cm(16))
    cab.add_paragraph("")

    titulo = doc.add_paragraph("MAPA – Material de Avaliação Prática da Aprendizagem")
    titulo.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph("")
    tab = doc.add_table(rows=4, cols=3)
    tab.cell(0, 0).paragraphs[0].add_run("Acadêmico:").bold = True
    tab.cell(0, 1).merge(tab.cell(0, 2)).paragraphs[0].add_run("R.A.").bold = True
    for linha, (rotulo, valor) in ((1, ("Curso: ", "Curso de Exemplo")),
                                   (2, (" Disciplina: ", "Processos de Software"))):
        cel = tab.cell(linha, 0).merge(tab.cell(linha, 2))
        cel.paragraphs[0].add_run(rotulo).bold = True
        cel.paragraphs[0].add_run(valor)
    valor = tab.cell(3, 0).merge(tab.cell(3, 1))
    valor.paragraphs[0].add_run("Valor da atividade: ").bold = True
    valor.paragraphs[0].add_run("3,5")
    tab.cell(3, 2).paragraphs[0].add_run("Prazo:").bold = True

    doc.add_paragraph("Instruções para Realização da Atividade")
    doc.add_paragraph("Todos os campos acima deverão ser devidamente preenchidos;")
    doc.add_paragraph(REGRA)
    doc.add_paragraph("Bons estudos!")
    caminho = tmp_path / "anexo.docx"
    doc.save(caminho)
    return caminho


OPS = [
    {"op": "campo", "rotulo": "Acadêmico:", "valor": "Carlos Daniel Morais"},
    {"op": "campo", "rotulo": "R.A.", "valor": "12345678-9"},
    {"op": "campo", "rotulo": "Prazo:", "valor": "29/11/2026 até 23:59"},
]


def _tabela(caminho: Path) -> list[list[str]]:
    from docx import Document

    from _docx_comum import W_P, celulas, texto_do

    linhas: dict[int, list[str]] = {}
    for li, _ci, tc, _s in celulas(Document(str(caminho)).tables[0]._tbl):
        linhas.setdefault(li, []).append(" | ".join(texto_do(p) for p in tc.findall(W_P)))
    return [linhas[k] for k in sorted(linhas)]


# ── mapa ─────────────────────────────────────────────────────────────────────

def test_mapa_acha_campos_mescla_logo_e_regra(tmp_path):
    m = docx_mapa.mapa(str(_template(tmp_path)))
    vazios = {c["rotulo"] for c in m["campos"] if c["vazio"]}
    assert {"Acadêmico:", "R.A.", "Prazo:"} <= vazios
    assert any(c["rotulo"] == "Curso:" and c["valor"] == "Curso de Exemplo" for c in m["campos"])
    assert m["tabelas"][0]["linhas"][0][1] == {"c": 1, "texto": "R.A.", "mescla": 2}
    assert any("cabeçalho" in onde and "logo" in onde for onde in m["imagens"])
    assert m["regras_do_template"] == [REGRA]
    assert {"tabela": 0} in m["corpo"]


# ── preencher ────────────────────────────────────────────────────────────────

def test_preenche_os_campos_da_tabela_sem_tocar_no_logo(tmp_path):
    entrada = _template(tmp_path)
    saida = tmp_path / "MAPA_preenchido.docx"
    r = docx_preencher.preencher(str(entrada), str(saida), OPS)

    assert r["ok"], r
    assert _tabela(saida)[0] == ["Acadêmico: Carlos Daniel Morais", "R.A.: 12345678-9"]
    assert _tabela(saida)[3] == ["Valor da atividade: 3,5", "Prazo: 29/11/2026 até 23:59"]
    assert r["conferencia"]["depois"]["imagens nos cabeçalhos"] == 1

    from docx import Document

    runs = Document(str(saida)).tables[0].cell(0, 0).paragraphs[0].runs
    assert [(x.text, bool(x.bold)) for x in runs] == [("Acadêmico:", True), (" Carlos Daniel Morais", False)]


def test_rodar_de_novo_troca_o_valor_e_nao_duplica(tmp_path):
    entrada = _template(tmp_path)
    v1, v2 = tmp_path / "v1.docx", tmp_path / "v2.docx"
    assert docx_preencher.preencher(str(entrada), str(v1), OPS)["ok"]
    nova = [dict(OPS[0], valor="Carlos D. Morais"), *OPS[1:]]
    assert docx_preencher.preencher(str(v1), str(v2), nova)["ok"]
    assert _tabela(v2)[0] == ["Acadêmico: Carlos D. Morais", "R.A.: 12345678-9"]


def test_rotulo_que_nao_existe_reprova_e_nao_gera_a_saida(tmp_path):
    entrada = _template(tmp_path)
    saida = tmp_path / "saida.docx"
    r = docx_preencher.preencher(str(entrada), str(saida), [{"op": "campo", "rotulo": "Matrícula", "valor": "1"}])
    assert not r["ok"] and not saida.exists()
    assert r["reprovado"].endswith("saida.REPROVADO.docx")
    assert "não encontrado" in r["nao_aplicadas"][0]


def test_inserir_de_arquivo_copia_o_texto_com_acento_e_formata(tmp_path):
    entrada = _template(tmp_path)
    fonte = tmp_path / "questoes.txt"
    fonte.write_text("# QUESTÃO 1\nA análise semiótica estuda a relação entre signo e objeto.\n\n> 1) alternativa\n",
                     encoding="utf-8")
    saida = tmp_path / "saida.docx"
    r = docx_preencher.preencher(str(entrada), str(saida), [
        *OPS,
        {"op": "inserir", "depois_de": "Bons estudos", "de_arquivo": str(fonte),
         "formato": {"fonte": "Arial", "tamanho": 12, "espacamento": 1.5, "alinhamento": "justificado"}},
    ])
    assert r["ok"], r

    from docx import Document

    pars = Document(str(saida)).paragraphs
    i = next(k for k, p in enumerate(pars) if p.text == "QUESTÃO 1")
    assert pars[i - 1].text == "Bons estudos!"
    assert pars[i].runs[0].bold and pars[i].runs[0].font.name == "Arial"
    assert pars[i + 1].paragraph_format.line_spacing == 1.5
    assert pars[i + 1].alignment.name == "JUSTIFY"
    assert pars[i + 3].text == "1) alternativa" and pars[i + 3].paragraph_format.left_indent is not None
    # Título e instruções do template continuam lá.
    textos = [p.text for p in pars]
    assert "MAPA – Material de Avaliação Prática da Aprendizagem" in textos and REGRA in textos


def test_substituir_atravessando_runs_mantem_a_formatacao_do_primeiro(tmp_path):
    entrada = _template(tmp_path)
    saida = tmp_path / "s.docx"
    r = docx_preencher.preencher(str(entrada), str(saida), [
        {"op": "substituir", "de": "Software", "para": "Software (EAD)", "onde": "tabela"},
    ])
    assert r["ok"], r
    assert _tabela(saida)[1] == ["Curso: Curso de Exemplo (EAD)"]


# ── conferir: os defeitos da sessão ──────────────────────────────────────────

def test_conferir_pega_o_logo_apagado_do_turno_2(tmp_path):
    from docx import Document

    entrada = _template(tmp_path)
    doc = Document(str(entrada))
    # O que o turno 2 fez: `p.text = ...` nos parágrafos do cabeçalho de página.
    for p, linha in zip(doc.sections[0].header.paragraphs, ["Nome: Carlos Daniel Morais", "R.A.: 12345678-9"]):
        p.text = linha
    ruim = tmp_path / "turno2.docx"
    doc.save(ruim)

    r = docx_conferir.conferir(str(entrada), str(ruim))
    assert not r["ok"]
    assert any(p.startswith("imagens nos cabeçalhos: 1 → 0") for p in r["problemas"])


def test_conferir_pega_o_ra_vazio_e_o_acento_perdido_do_turno_4(tmp_path):
    from docx import Document

    entrada = _template(tmp_path)
    doc = Document(str(entrada))
    doc.tables[0].cell(0, 0).paragraphs[0].runs[0].text = "Academico: Carlos Daniel Morais"
    doc.add_paragraph("QUESTAO 1 — A analise semiotica")
    ruim = tmp_path / "turno4.docx"
    doc.save(ruim)
    fonte = tmp_path / "fonte.txt"
    fonte.write_text("QUESTÃO 1 — A análise semiótica", encoding="utf-8")

    r = docx_conferir.conferir(str(entrada), str(ruim), esperados=["12345678-9"], fontes=[str(fonte)])
    texto = " ".join(r["problemas"])
    assert "não está no arquivo: «12345678-9»" in texto
    assert "Academico (era Acadêmico)" in texto and "QUESTAO (era QUESTÃO)" in texto


def test_conferir_pega_titulo_e_instrucoes_apagados_do_turno_5(tmp_path):
    from docx import Document

    entrada = _template(tmp_path)
    doc = Document(str(entrada))
    for p in list(doc.paragraphs):  # o laço do script final do turno 5
        p._element.getparent().remove(p._element)
    ruim = tmp_path / "turno5.docx"
    doc.save(ruim)

    r = docx_conferir.conferir(str(entrada), str(ruim))
    assert not r["ok"]
    sumidos = next(p for p in r["problemas"] if "sumiram" in p)
    assert "MAPA – Material" in sumidos and "Bons estudos" in sumidos


def test_cli_do_conferir_sai_com_1_quando_reprova(tmp_path):
    entrada = _template(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "docx_conferir.py"), str(entrada), str(entrada), "--espera", "nada-disso"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["problemas"] == ["não está no arquivo: «nada-disso»"]


# ── renderizar ───────────────────────────────────────────────────────────────

def test_sem_libreoffice_diz_onde_esta_a_receita(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_renderizar, "_soffice", lambda: None)
    r = docx_renderizar.renderizar(str(_template(tmp_path)))
    assert not r["ok"] and r["codigo"] == 3
    assert "docker/neo-workbench/DEPENDENCIAS.md" in r["erro"]


@pytest.mark.skipif(docx_renderizar._soffice() is None, reason="LibreOffice ausente nesta máquina")
def test_renderiza_a_primeira_pagina(tmp_path):
    r = docx_renderizar.renderizar(str(_template(tmp_path)), 1, 1, str(tmp_path / "png"))
    assert r["ok"] and r["paginas"] and Path(r["paginas"][0]).stat().st_size > 0

