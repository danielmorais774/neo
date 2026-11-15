"""Mapa de um .docx numa chamada só: corpo, tabelas, cabeçalhos, campos e regras.

    python3 /skills/docx/scripts/docx_mapa.py /workspace/files/anexo.docx

Imprime um JSON curto. Os pontos que importam para editar sem estragar:

* ``campos`` — os pares rótulo/valor de formulário ("Acadêmico:", "R.A.",
  "Prazo:") e onde estão. Num template, "cabeçalho" no pedido dele quase sempre
  são ESTES campos, não o cabeçalho de página do Word.
* ``imagens`` — onde há imagem. Parágrafo "vazio" com imagem é o logo: não
  reescreva.
* ``regras_do_template`` — o que o próprio template exige de formatação.

Nasceu da session-20cb02b6: o Neo gastou 8 chamadas para entender uma tabela de
4 linhas com células mescladas, escreveu no cabeçalho do Word (apagando o logo) e
não leu a linha "Fonte Arial ou Times 12, espaçamento 1,5, justificado".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docx import Document  # noqa: E402

from _docx_comum import (  # noqa: E402
    W_P,
    W_TBL,
    W_TC,
    celulas,
    e_regra_de_formatacao,
    imagens_em,
    partes,
    rotulo_de,
    texto_do,
)

_CORTE = 160
_MAX_CORPO = 80


def _paragrafo(p_el, i: int, doc) -> dict:
    texto = texto_do(p_el)
    item = {"i": i, "texto": texto[:_CORTE] + ("…" if len(texto) > _CORTE else "")}
    try:
        from docx.text.paragraph import Paragraph

        par = Paragraph(p_el, doc._body)
        if par.style is not None and par.style.name != "Normal":
            item["estilo"] = par.style.name
        if par.alignment is not None:
            item["alinhamento"] = par.alignment.name.lower()
    except Exception:  # noqa: BLE001 — metadado de estilo é extra
        pass
    n = imagens_em(p_el)
    if n:
        item["imagens"] = n
    return item


def _tabela(tbl, t: int) -> dict:
    linhas: dict[int, list] = {}
    for li, ci, tc, span in celulas(tbl):
        cel = {"c": ci, "texto": " | ".join(texto_do(p) for p in tc.findall(W_P))[:_CORTE]}
        if span > 1:
            cel["mescla"] = span
        if imagens_em(tc):
            cel["imagens"] = imagens_em(tc)
        linhas.setdefault(li, []).append(cel)
    return {"t": t, "linhas": [linhas[k] for k in sorted(linhas)]}


def mapa(caminho: str) -> dict:
    doc = Document(caminho)
    out: dict = {"arquivo": caminho, "secoes": len(doc.sections)}
    corpo: list = []
    tabelas: list = []
    campos: list = []
    regras: list = []
    imagens: list = []

    body = doc.element.body
    i_par = 0
    for filho in body.iterchildren():
        if filho.tag == W_P:
            item = _paragrafo(filho, i_par, doc)
            corpo.append(item)
            texto = texto_do(filho)
            achado = rotulo_de(texto)
            if achado:
                campos.append({"rotulo": achado[0], "valor": achado[1], "vazio": not achado[1],
                               "onde": f"corpo §{i_par}"})
            if e_regra_de_formatacao(texto):
                regras.append(texto[:300])
            if item.get("imagens"):
                imagens.append(f"corpo §{i_par}")
            i_par += 1
        elif filho.tag == W_TBL:
            t = len(tabelas)
            tabelas.append(_tabela(filho, t))
            corpo.append({"tabela": t})
            for li, ci, tc, _span in celulas(filho):
                for p in tc.findall(W_P):
                    achado = rotulo_de(texto_do(p))
                    if achado:
                        campos.append({"rotulo": achado[0], "valor": achado[1], "vazio": not achado[1],
                                       "onde": f"tabela {t} · linha {li} · célula {ci}"})

    cabecalhos: list = []
    for nome, raiz, _part in partes(doc)[1:]:
        pars = []
        for j, p in enumerate(raiz.iter(W_P)):
            # Parágrafo dentro de célula de tabela do cabeçalho também conta.
            texto = texto_do(p)
            n = imagens_em(p)
            if texto or n:
                pars.append({"i": j, "texto": texto[:_CORTE], **({"imagens": n} if n else {})})
            achado = rotulo_de(texto)
            if achado:
                campos.append({"rotulo": achado[0], "valor": achado[1], "vazio": not achado[1],
                               "onde": f"{nome} (cabeçalho/rodapé de PÁGINA do Word)"})
        if imagens_em(raiz):
            imagens.append(f"{nome} ({imagens_em(raiz)} imagem/ns — provavelmente o logo)")
        cabecalhos.append({"parte": nome, "paragrafos_com_conteudo": pars})

    out["corpo"] = corpo[:_MAX_CORPO]
    if len(corpo) > _MAX_CORPO:
        out["corpo_omitidos"] = len(corpo) - _MAX_CORPO
    out["tabelas"] = tabelas
    out["cabecalhos_e_rodapes"] = cabecalhos
    out["campos"] = campos
    out["imagens"] = imagens
    out["regras_do_template"] = regras
    out["dica"] = (
        "Preencha CAMPOS com docx_preencher.py (op 'campo'). Parágrafo com imagem não se "
        "reescreve. Aplique as regras_do_template ao conteúdo que inserir."
    )
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: docx_mapa.py <arquivo.docx>", file=sys.stderr)
        return 2
    print(json.dumps(mapa(sys.argv[1]), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
