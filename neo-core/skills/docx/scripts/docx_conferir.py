"""Confere a versão nova de um .docx contra o original, antes de entregar.

    python3 /skills/docx/scripts/docx_conferir.py ORIGINAL.docx NOVO.docx \\
        --espera "12345678-9" --espera "Carlos Daniel Morais" [--fonte questoes.txt]

Reprova (exit 1) quando:

* sumiu imagem, tabela ou seção (o logo do cabeçalho, na session-20cb02b6);
* sumiu parágrafo do original que ninguém pediu para mexer (título, instruções);
* falta no arquivo um valor que ele deu (``--espera``) — o R.A. que a resposta
  dizia ter posto e a própria verificação mostrava vazio;
* palavra perdeu o acento em relação ao original ou à ``--fonte`` ("QUESTAO",
  "Academico": conteúdo redigitado pelo modelo em vez de copiado).

Imprime JSON com ``ok`` e a lista de ``problemas``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docx import Document  # noqa: E402

from _docx_comum import W_P, W_SECTPR, W_TBL, chave, imagens_em, partes, sem_acento, texto_do  # noqa: E402

_PALAVRA = re.compile(r"[A-Za-zÀ-ÿ]{4,}")


def _paragrafos(doc) -> list[str]:
    out = []
    for _nome, raiz, _part in partes(doc):
        for p in raiz.iter(W_P):
            t = " ".join(texto_do(p).split())
            if t:
                out.append(t)
    return out


def _contagens(doc) -> dict:
    corpo_imgs = cab_imgs = rod_imgs = 0
    for nome, raiz, _part in partes(doc):
        n = imagens_em(raiz)
        if nome == "corpo":
            corpo_imgs += n
        elif nome.startswith("cabeçalho"):
            cab_imgs += n
        else:
            rod_imgs += n
    body = doc.element.body
    return {
        "imagens no corpo": corpo_imgs,
        "imagens nos cabeçalhos": cab_imgs,
        "imagens nos rodapés": rod_imgs,
        "tabelas": sum(1 for _ in body.iter(W_TBL)),
        "seções": sum(1 for _ in body.iter(W_SECTPR)),
    }


def _acentos_perdidos(texto_novo: str, referencias: list[str]) -> list[str]:
    com_acento: dict[str, str] = {}
    sem: set[str] = set()
    for ref in referencias:
        for w in _PALAVRA.findall(ref):
            if w != sem_acento(w):
                com_acento.setdefault(sem_acento(w).lower(), w)
            else:
                sem.add(w.lower())
    perdidos: list[str] = []
    for w in _PALAVRA.findall(texto_novo):
        baixo = w.lower()
        if w == sem_acento(w) and baixo in com_acento and baixo not in sem:
            exemplo = f"{w} (era {com_acento[baixo]})"
            if exemplo not in perdidos:
                perdidos.append(exemplo)
    return perdidos


def conferir(original: str, novo: str, *, esperados=(), fontes=(), ignorar=()) -> dict:
    """``ignorar``: textos de parágrafos do original que foram mudados DE PROPÓSITO
    (op ``substituir`` do docx_preencher) — não contam como sumidos."""
    doc_o, doc_n = Document(original), Document(novo)
    problemas: list[str] = []

    antes, depois = _contagens(doc_o), _contagens(doc_n)
    for k, v in antes.items():
        if depois.get(k, 0) < v:
            problemas.append(f"{k}: {v} → {depois.get(k, 0)} (sumiu — o original tinha mais)")

    pars_o, pars_n = _paragrafos(doc_o), _paragrafos(doc_n)
    novos = {chave(p) for p in pars_n}
    texto_n = "\n".join(pars_n)
    sumidos = []
    mudados = {chave(t) for t in ignorar if t}
    for p in pars_o:
        k = chave(p)
        if k in novos or k in mudados:
            continue
        # Campo preenchido ("Acadêmico:" → "Acadêmico: Carlos…") não sumiu: mudou de propósito.
        cabeca = k[: max(6, min(len(k), 24))]
        if any(n.startswith(cabeca) for n in novos):
            continue
        sumidos.append(p[:90])
    if sumidos:
        problemas.append(
            f"{len(sumidos)} parágrafo(s) do original sumiram: " + " · ".join(f"«{s}»" for s in sumidos[:8])
        )

    plano = chave(texto_n)
    for valor in esperados:
        if valor and chave(valor) not in plano:
            problemas.append(f"não está no arquivo: «{valor}»")

    textos_fonte = []
    for f in fontes:
        try:
            textos_fonte.append(Path(f).read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            problemas.append(f"fonte ilegível {f}: {exc}")
    perdidos = _acentos_perdidos(texto_n, ["\n".join(pars_o), *textos_fonte])
    if perdidos:
        problemas.append("acento perdido (texto redigitado?): " + ", ".join(perdidos[:10]))

    return {"ok": not problemas, "problemas": problemas, "antes": antes, "depois": depois}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("original")
    ap.add_argument("novo")
    ap.add_argument("--espera", action="append", default=[], help="valor que tem de estar no arquivo")
    ap.add_argument("--fonte", action="append", default=[], help="texto de onde o conteúdo foi copiado")
    a = ap.parse_args()
    r = conferir(a.original, a.novo, esperados=a.espera, fontes=a.fonte)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
