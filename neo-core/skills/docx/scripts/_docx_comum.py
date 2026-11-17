"""Peças comuns dos scripts da skill docx (python-docx 1.2, no workbench).

Tudo aqui lê o XML do documento direto, porque é nele que mora o que o
python-docx de alto nível esconde e o modelo quebrava sem ver: célula mesclada
(``gridSpan``), imagem dentro de um parágrafo "vazio" do cabeçalho, run com o
rótulo em negrito e o valor separado.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterator

from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn

W_P = qn("w:p")
W_R = qn("w:r")
W_T = qn("w:t")
W_TBL = qn("w:tbl")
W_TR = qn("w:tr")
W_TC = qn("w:tc")
W_RPR = qn("w:rPr")
W_PPR = qn("w:pPr")
W_SECTPR = qn("w:sectPr")
_IMAGENS = (qn("w:drawing"), qn("w:pict"))

#: Rótulos de formulário acadêmico/administrativo que aparecem SEM dois-pontos.
ROTULOS_CONHECIDOS = (
    "r.a.", "r.a", "ra", "nome", "aluno", "aluna", "academico", "academica", "matricula",
    "curso", "disciplina", "turma", "professor", "professora", "tutor", "tutora", "prazo",
    "data", "valor da atividade", "polo", "semestre", "periodo", "cpf", "rg", "email", "e-mail",
)


def sem_acento(texto: str) -> str:
    normal = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in normal if not unicodedata.combining(c))


def chave(texto: str) -> str:
    """Forma de comparação: sem acento, minúscula, espaços colapsados."""
    return " ".join(sem_acento(texto).lower().split())


def mapa_de_caracteres(texto: str) -> str:
    """Versão sem acento e minúscula com o MESMO comprimento do texto (1 → 1)."""
    out = []
    for c in texto:
        s = sem_acento(c).lower()
        out.append(s[0] if s else c.lower())
    return "".join(out)


def texto_do(el) -> str:
    return "".join(t.text or "" for t in el.iter(W_T))


def imagens_em(el) -> int:
    return sum(1 for tag in _IMAGENS for _ in el.iter(tag))


def tem_imagem(el) -> bool:
    return imagens_em(el) > 0


def partes(doc) -> list[tuple[str, object, object]]:
    """``(nome, elemento raiz, part)`` do corpo, de cada cabeçalho e de cada rodapé.

    Pelos relacionamentos do documento, e não por ``section.header``: ler o
    ``header`` de uma seção sem definição própria CRIA uma — consultar não pode
    mudar o arquivo.
    """
    out: list[tuple[str, object, object]] = [("corpo", doc.element.body, doc.part)]
    cab = rod = 0
    for rel in doc.part.rels.values():
        if rel.reltype == RT.HEADER:
            cab += 1
            out.append((f"cabeçalho {cab}", rel.target_part.element, rel.target_part))
        elif rel.reltype == RT.FOOTER:
            rod += 1
            out.append((f"rodapé {rod}", rel.target_part.element, rel.target_part))
    return out


def celulas(tbl) -> Iterator[tuple[int, int, object, int]]:
    """``(linha, coluna, tc, gridSpan)`` de uma tabela, só as células de verdade."""
    for li, tr in enumerate(tbl.findall(W_TR)):
        for ci, tc in enumerate(tr.findall(W_TC)):
            span = 1
            tcpr = tc.find(qn("w:tcPr"))
            if tcpr is not None:
                g = tcpr.find(qn("w:gridSpan"))
                if g is not None:
                    try:
                        span = int(g.get(qn("w:val")))
                    except (TypeError, ValueError):
                        span = 1
            yield li, ci, tc, span


def rotulo_de(texto: str) -> tuple[str, str] | None:
    """``(rótulo, valor atual)`` se o texto é um campo de formulário, senão None.

    "Acadêmico:" → ("Acadêmico:", ""); "Curso: Engenharia" → ("Curso:", "Engenharia");
    "R.A." → ("R.A.", ""). Frase longa com dois-pontos (instrução) não é campo.
    """
    t = (texto or "").strip()
    if not t or len(t) > 120:
        return None
    m = re.match(r"^([^:\n]{1,32}?)\s*:\s*(.*)$", t, re.S)
    if m and len(m.group(1).split()) <= 4:
        return m.group(1).strip() + ":", m.group(2).strip()
    if chave(t).rstrip(".") in {r.rstrip(".") for r in ROTULOS_CONHECIDOS}:
        return t, ""
    return None


_REGRA = re.compile(
    r"(formata[cç][aã]o|fonte\s|espa[cç]amento|justificad|abnt|margens?\s|tamanho\s*\d+|times new roman|arial)",
    re.IGNORECASE,
)


def e_regra_de_formatacao(texto: str) -> bool:
    return bool(_REGRA.search(texto or ""))
