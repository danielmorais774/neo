"""Edita um .docx por OPERAÇÕES, sem reescrever o que não foi pedido.

    python3 /skills/docx/scripts/docx_preencher.py ENTRADA.docx SAIDA.docx --ops OPS.json

OPS.json é uma lista. Operações:

  {"op": "campo", "rotulo": "R.A.", "valor": "12345678-9"}
      Põe o valor DEPOIS do rótulo, no mesmo parágrafo (ou na célula vazia ao lado),
      com a formatação do rótulo menos o negrito. Rodar de novo troca o valor, não
      duplica. Procura nas tabelas do corpo, depois no corpo, e só por último no
      cabeçalho de PÁGINA do Word. "onde": "tabela" | "corpo" | "cabecalho_de_pagina"
      restringe; "negrito": true mantém o negrito.
  {"op": "substituir", "de": "texto exato", "para": "texto novo"}
  {"op": "inserir", "depois_de": "trecho de um parágrafo do corpo"
                    | "antes_de": "trecho" | "depois_da_tabela": 0 | "no_fim": true,
   "de_arquivo": "/workspace/files/conteudo.txt",      # OU "paragrafos": [...]
   "formato": {"fonte": "Arial", "tamanho": 12, "espacamento": 1.5,
               "alinhamento": "justificado"}}
      "de_arquivo": uma linha = um parágrafo; linha vazia = parágrafo vazio;
      "# " no começo = negrito; "> " no começo = recuo. É assim que conteúdo longo
      entra SEM o modelo redigitar (e perder acento). "paragrafos": itens com
      "texto" e, se quiser, "negrito", "italico", "alinhamento", "recuo_cm",
      "tamanho", "fonte", "espacamento", "espaco_depois_pt", "estilo".

Nunca apaga imagem: run com desenho fica intacto. Antes de liberar a SAIDA roda a
conferência contra a ENTRADA (docx_conferir.py) com os valores dos campos como
esperados; se algo sumiu ou faltou, a saída vira SAIDA.REPROVADO.docx e o exit é 1.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Cm, Pt  # noqa: E402
from docx.text.paragraph import Paragraph  # noqa: E402
from docx.text.run import Run  # noqa: E402

import docx_conferir  # noqa: E402
from _docx_comum import (  # noqa: E402
    W_P,
    W_R,
    W_RPR,
    W_SECTPR,
    W_TBL,
    W_TC,
    chave,
    mapa_de_caracteres,
    partes,
    tem_imagem,
    texto_do,
)

_ALINHAMENTO = {
    "esquerda": WD_ALIGN_PARAGRAPH.LEFT,
    "centro": WD_ALIGN_PARAGRAPH.CENTER,
    "centralizado": WD_ALIGN_PARAGRAPH.CENTER,
    "direita": WD_ALIGN_PARAGRAPH.RIGHT,
    "justificado": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


class Falha(Exception):
    pass


def _texto_run(r) -> str:
    return Run(r, None).text


def _runs(p_el) -> list:
    return p_el.findall(W_R)


def _texto_par(p_el) -> str:
    return "".join(_texto_run(r) for r in _runs(p_el))


# ── campo ────────────────────────────────────────────────────────────────────

def _candidatos(doc, onde: str | None) -> list:
    body = doc.element.body
    tabelas = [p for tbl in body.iter(W_TBL) for p in tbl.iter(W_P)]
    corpo = body.findall(W_P)
    pagina = [p for _nome, raiz, _part in partes(doc)[1:] for p in raiz.iter(W_P)]
    return {"tabela": tabelas, "corpo": corpo, "cabecalho_de_pagina": pagina}.get(
        onde or "", tabelas + corpo + pagina
    )


def _fim_do_rotulo(texto: str, rotulo: str) -> int | None:
    """Posição logo depois do rótulo (e do ':' que vier), ou None se não começa com ele."""
    alvo = chave(rotulo).rstrip(":").strip()
    if not alvo:
        return None
    m = mapa_de_caracteres(texto)
    inicio = len(m) - len(m.lstrip())
    if not m[inicio:].startswith(alvo):
        return None
    fim = inicio + len(alvo)
    if fim < len(m) and m[fim].isalnum():
        return None  # "Nome" não casa "Nomeação"
    j = fim
    while j < len(texto) and texto[j] in " \t":
        j += 1
    if j < len(texto) and texto[j] == ":":
        fim = j + 1
    return fim


def _run_de_valor(modelo, texto: str, negrito: bool):
    novo = OxmlElement("w:r")
    rpr = modelo.find(W_RPR) if modelo is not None else None
    if rpr is not None:
        rpr = copy.deepcopy(rpr)
        if not negrito:
            for tag in ("w:b", "w:bCs"):
                for el in rpr.findall(qn(tag)):
                    rpr.remove(el)
        novo.append(rpr)
    Run(novo, None).text = texto
    if negrito:
        Run(novo, None).bold = True
    return novo


def _celula_ao_lado(p_el):
    tc = p_el.getparent()
    if tc is None or tc.tag != W_TC:
        return None
    viz = tc.getnext()
    while viz is not None and viz.tag != W_TC:
        viz = viz.getnext()
    return viz


def _limpar_texto(p_el) -> None:
    for r in _runs(p_el):
        if not tem_imagem(r):
            p_el.remove(r)


def aplicar_campo(doc, op: dict, alterados: list[str]) -> str:
    rotulo, valor = str(op.get("rotulo") or ""), str(op.get("valor") or "")
    negrito = bool(op.get("negrito"))
    if not rotulo or not valor:
        raise Falha("campo precisa de 'rotulo' e 'valor'")
    for p in _candidatos(doc, op.get("onde")):
        texto = _texto_par(p)
        fim = _fim_do_rotulo(texto, rotulo)
        if fim is None:
            continue
        runs = _runs(p)
        modelo = next((r for r in runs if _texto_run(r) and not tem_imagem(r)), None)
        alterados.append(" ".join(texto_do(p).split()))

        # Rótulo sozinho na célula e a do lado vazia (ou já com este valor): vai lá.
        vizinha = _celula_ao_lado(p)
        if texto.strip() == texto[:fim].strip() and vizinha is not None and not tem_imagem(vizinha):
            atual = texto_do(vizinha).strip()
            if not atual or chave(atual) == chave(valor) or op.get("celula_ao_lado"):
                destino = vizinha.find(W_P)
                if destino is None:
                    destino = OxmlElement("w:p")
                    vizinha.append(destino)
                alterados.append(" ".join(texto_do(destino).split()))
                _limpar_texto(destino)
                destino.append(_run_de_valor(modelo, valor, negrito))
                return f"campo «{rotulo}» → célula ao lado"

        pos, ultimo, sobra = 0, None, []
        for r in runs:
            if tem_imagem(r):
                continue  # imagem fica onde está, sempre
            t = _texto_run(r)
            ini, fim_r = pos, pos + len(t)
            pos = fim_r
            if fim_r <= fim:
                ultimo = r if t else ultimo
            elif ini < fim:
                Run(r, None).text = t[: fim - ini]
                ultimo = r
            else:
                sobra.append(r)  # valor antigo: sai, para não duplicar
        for r in sobra:
            p.remove(r)
        separador = " " if texto[:fim].rstrip().endswith(":") else ": "
        novo = _run_de_valor(ultimo if ultimo is not None else modelo, separador + valor, negrito)
        if ultimo is not None:
            ultimo.addnext(novo)
        else:
            p.append(novo)
        return f"campo «{rotulo}» = «{valor}»"
    raise Falha(f"rótulo «{rotulo}» não encontrado — use o 'rotulo' exatamente como o docx_mapa.py mostra")


# ── substituir ───────────────────────────────────────────────────────────────

def aplicar_substituir(doc, op: dict, alterados: list[str]) -> str:
    de, para = str(op.get("de") or ""), str(op.get("para") if op.get("para") is not None else "")
    if not de:
        raise Falha("substituir precisa de 'de'")
    feitos = 0
    for p in _candidatos(doc, op.get("onde")):
        runs = _runs(p)
        cheio = "".join(_texto_run(r) for r in runs)
        if de not in cheio:
            continue
        alterados.append(" ".join(texto_do(p).split()))
        num_run = False
        for r in runs:
            t = _texto_run(r)
            if de in t and not tem_imagem(r):
                Run(r, None).text = t.replace(de, para)
                feitos += t.count(de)
                num_run = True
        if num_run:
            continue
        # O trecho atravessa runs: fica no primeiro, com a formatação dele.
        inicio = cheio.index(de)
        fim = inicio + len(de)
        pos, primeiro = 0, None
        for r in runs:
            t = _texto_run(r)
            ini, fim_r = pos, pos + len(t)
            pos = fim_r
            if tem_imagem(r) or fim_r <= inicio or ini >= fim:
                continue
            if primeiro is None:
                primeiro = r
                Run(r, None).text = t[: inicio - ini] + para + (t[fim - ini:] if fim < fim_r else "")
            else:
                resto = t[fim - ini:] if fim < fim_r else ""
                if resto:
                    Run(r, None).text = resto
                else:
                    p.remove(r)
        feitos += 1
    if not feitos:
        raise Falha(f"texto «{de[:60]}» não encontrado")
    return f"substituído {feitos}x: «{de[:40]}» → «{para[:40]}»"


# ── inserir ──────────────────────────────────────────────────────────────────

def _linhas_de_arquivo(caminho: str) -> list[dict]:
    itens: list[dict] = []
    vazia_antes = False
    for linha in Path(caminho).read_text(encoding="utf-8").splitlines():
        if not linha.strip():
            if not vazia_antes and itens:
                itens.append({"texto": ""})
            vazia_antes = True
            continue
        vazia_antes = False
        item: dict = {}
        if linha.startswith("# "):
            item["negrito"], linha = True, linha[2:]
        elif linha.startswith("> "):
            item["recuo_cm"], linha = 0.6, linha[2:]
        item["texto"] = linha.rstrip()
        itens.append(item)
    return itens


def _ancora(doc, op: dict):
    body = doc.element.body
    if op.get("no_fim"):
        ultimo = body[-1]
        return (ultimo.getprevious() if ultimo.tag == W_SECTPR else ultimo), "depois"
    if op.get("depois_da_tabela") is not None:
        tabelas = [el for el in body if el.tag == W_TBL]
        n = int(op["depois_da_tabela"])
        if n >= len(tabelas):
            raise Falha(f"não há tabela {n} (o corpo tem {len(tabelas)})")
        return tabelas[n], "depois"
    for chave_op, lado in (("depois_de", "depois"), ("antes_de", "antes")):
        trecho = op.get(chave_op)
        if trecho:
            alvo = chave(str(trecho))
            for p in body.findall(W_P):
                if alvo and alvo in chave(texto_do(p)):
                    return p, lado
            raise Falha(f"parágrafo com «{str(trecho)[:60]}» não encontrado no corpo")
    raise Falha("inserir precisa de 'depois_de', 'antes_de', 'depois_da_tabela' ou 'no_fim'")


def aplicar_inserir(doc, op: dict) -> str:
    if op.get("de_arquivo"):
        itens = _linhas_de_arquivo(str(op["de_arquivo"]))
    else:
        itens = [i if isinstance(i, dict) else {"texto": str(i)} for i in (op.get("paragrafos") or [])]
    if not itens:
        raise Falha("inserir sem conteúdo ('de_arquivo' vazio ou 'paragrafos' vazio)")
    formato = dict(op.get("formato") or {})
    ancora, lado = _ancora(doc, op)
    pai = doc._body
    for item in itens:
        f = {**formato, **item}
        novo = OxmlElement("w:p")
        if lado == "depois":
            ancora.addnext(novo)
            ancora = novo
        else:
            ancora.addprevious(novo)
        par = Paragraph(novo, pai)
        if f.get("estilo"):
            try:
                par.style = f["estilo"]
            except (KeyError, ValueError):
                pass
        pf = par.paragraph_format
        if f.get("espacamento"):
            pf.line_spacing = float(f["espacamento"])
        if f.get("alinhamento") in _ALINHAMENTO:
            par.alignment = _ALINHAMENTO[f["alinhamento"]]
        if f.get("recuo_cm"):
            pf.left_indent = Cm(float(f["recuo_cm"]))
        if f.get("espaco_depois_pt") is not None:
            pf.space_after = Pt(float(f["espaco_depois_pt"]))
        texto = str(f.get("texto") or "")
        if texto:
            run = par.add_run(texto)
            if f.get("negrito"):
                run.bold = True
            if f.get("italico"):
                run.italic = True
            if f.get("fonte"):
                run.font.name = str(f["fonte"])
            if f.get("tamanho"):
                run.font.size = Pt(float(f["tamanho"]))
    return f"inseridos {len(itens)} parágrafo(s)"


# ── principal ────────────────────────────────────────────────────────────────

def preencher(entrada: str, saida: str, ops: list[dict]) -> dict:
    doc = Document(entrada)
    aplicadas, nao_aplicadas, alterados = [], [], []
    for i, op in enumerate(ops):
        tipo = op.get("op")
        try:
            if tipo == "campo":
                aplicadas.append(aplicar_campo(doc, op, alterados))
            elif tipo == "substituir":
                aplicadas.append(aplicar_substituir(doc, op, alterados))
            elif tipo == "inserir":
                aplicadas.append(aplicar_inserir(doc, op))
            else:
                raise Falha(f"op desconhecida {tipo!r} (campo | substituir | inserir)")
        except Falha as exc:
            nao_aplicadas.append(f"op {i} ({tipo}): {exc}")

    destino = Path(saida)
    provisoria = destino.with_name(destino.stem + ".provisorio.docx")
    doc.save(provisoria)
    esperados = [str(op["valor"]) for op in ops if op.get("op") == "campo" and op.get("valor")]
    fontes = [str(op["de_arquivo"]) for op in ops if op.get("op") == "inserir" and op.get("de_arquivo")]
    conf = docx_conferir.conferir(entrada, str(provisoria), esperados=esperados, fontes=fontes, ignorar=alterados)

    ok = conf["ok"] and not nao_aplicadas
    if ok:
        provisoria.replace(destino)
        final, reprovado = str(destino), None
    else:
        rep = destino.with_name(destino.stem + ".REPROVADO.docx")
        provisoria.replace(rep)
        final, reprovado = None, str(rep)
    return {
        "ok": ok,
        "saida": final,
        "reprovado": reprovado,
        "aplicadas": aplicadas,
        "nao_aplicadas": nao_aplicadas,
        "conferencia": conf,
        "proximo_passo": (
            "Renderize e olhe a página (docx_renderizar.py) antes do file_save."
            if ok else
            "NÃO entregue o .REPROVADO. Corrija as ops (ou o rótulo) e rode de novo."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("entrada")
    ap.add_argument("saida")
    ap.add_argument("--ops", help="arquivo JSON com a lista de operações")
    ap.add_argument("--ops-json", help="a lista de operações direto no argumento")
    a = ap.parse_args()
    if a.ops:
        ops = json.loads(Path(a.ops).read_text(encoding="utf-8"))
    elif a.ops_json:
        ops = json.loads(a.ops_json)
    else:
        print("passe --ops ARQUIVO.json ou --ops-json '[...]'", file=sys.stderr)
        return 2
    if isinstance(ops, dict):
        ops = ops.get("ops") or [ops]
    r = preencher(a.entrada, a.saida, ops)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
