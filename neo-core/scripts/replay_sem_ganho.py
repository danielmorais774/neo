#!/usr/bin/env python3
"""Replay da régua de produção do navegador local sobre os diários de job.

Mede, sem rodar navegador nem modelo, em que passo cada job PARARIA por
"N passos seguidos sem acrescentar nada" (``_sem_ganho``) e até onde iria o
orçamento elástico (``_teto_de_passos``), com a régua atual e com as duas que
foram rejeitadas. Roda DENTRO do container, lendo os ``trace.md`` no lugar —
nenhuma cópia do conteúdo das conversas sai de lá:

    ssh <vps> 'docker exec -i -w /app neo-core python -' < scripts/replay_sem_ganho.py

As definições (limiares, ``_fichas``, ``_INTERAGE``, ``_PALAVRA_DA_TELA``) vêm do
próprio driver, então o replay mede o código que está em produção.

Réguas:
  velha  ganho = nota NOVA (a do `look` não conta) — a régua até 2026-11-05
  A      velha + nota ATUALIZADA que ganhou palavras          (rejeitada)
  B      A, se a tela do passo seguinte tem hash inédito      (rejeitada)
  C      velha + nota atualizada que ganhou palavras, depois de uma interação
         que deu certo, com palavras inéditas na tela        (a do driver)

A e B soltavam o b8dbde34, um giro real em "Carregando Questionário…":
o modelo narrava "ainda carregando" na nota e o hash da tela mudava a cada passo.
O simulador erra por até 1 passo nos jobs com ação recusada (a recusa não é
cobrada no driver e o diário não a distingue).
"""

import glob
import hashlib
import os
import re
import sys

from core.browser_local.driver import (
    _INTERAGE,
    _NOTA_CRESCEU_MIN,
    _PALAVRA_DA_TELA,
    _SEM_GANHO_MAX,
    _TELA_INEDITA_MIN,
    _fichas,
)

STEP = re.compile(r"^## Passo (\d+)/(\d+) · ")
NOTE_NEW = re.compile(r"^\*\*[\d:.]+ · anotou \((\d+)\)\*\*$")
NOTE_UPD = re.compile(r"^\*\*[\d:.]+ · atualizei a nota (\d+)\*\*$")
CHOSE = re.compile(r"^- escolheu: `(\w+)`")
PARADA = re.compile(r"Parei: (\d+) passos seguidos sem acrescentar")
NAO_COBRA = (None, "look", "note", "find")


def _na_tela(bloco: list[str]) -> set[str]:
    """As palavras da seção NA TELA AGORA do bloco "página como o modelo viu"."""
    dentro = False
    out: set[str] = set()
    for linha in bloco:
        if linha.startswith(">>> NA TELA AGORA"):
            dentro = True
            continue
        if dentro and (linha.startswith("Elementos (") or linha.startswith(">>> ")):
            break
        if dentro:
            out |= {p.casefold() for p in _PALAVRA_DA_TELA.findall(linha)}
    return out


def _ler(path: str):
    linhas = open(path, encoding="utf-8", errors="replace").read().split("\n")
    base = 25
    for linha in linhas[:20]:
        if m := re.match(r"^\*\*orçamento de passos:\*\* (\d+)", linha):
            base = int(m.group(1))
    passos: list[dict] = []
    cur: dict | None = None
    estado = ""
    parada_real = None
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        if m := STEP.match(linha):
            cur = {"n": int(m.group(1)), "hash": None, "tela": set(), "notas": [], "acao": None, "ok": False, "marcou": False}
            passos.append(cur)
        elif linha.startswith("<details><summary>página como o modelo viu"):
            j = i + 1
            while j < len(linhas) and not linhas[j].startswith("</details>"):
                j += 1
            if cur is not None:
                cur["hash"] = hashlib.sha1("\n".join(linhas[i + 1 : j]).encode()).hexdigest()
                cur["tela"] = _na_tela(linhas[i + 1 : j])
            i = j
        elif cur is not None and (m := NOTE_NEW.match(linha)):
            cur["notas"].append(("nova", int(m.group(1)), linhas[i + 1] if i + 1 < len(linhas) else ""))
        elif cur is not None and (m := NOTE_UPD.match(linha)):
            cur["notas"].append(("upd", int(m.group(1)), linhas[i + 1] if i + 1 < len(linhas) else ""))
        elif cur is not None and (m := CHOSE.match(linha)):
            cur["acao"] = m.group(1)
        elif cur is not None and linha.startswith("- ▶ ") and "→ ok" in linha:
            cur["ok"] = True
            cur["marcou"] = "→ SELECIONADO" in linha
        elif linha.startswith("- estado: "):
            estado = linha.split("**")[1] if "**" in linha else linha
        if PARADA.search(linha) and parada_real is None and cur is not None:
            parada_real = cur["n"]
        i += 1
    return base, passos, estado, parada_real


def _simular(passos: list[dict], regua: str, base: int):
    cobrados = ultimo_ganho = ganhos = 0
    notas: dict[int, str] = {}
    novas = cresceu = 0
    hashes: set[str] = set()
    palavras: set[str] = set()
    interagiu = marcou = False
    for passo in passos:
        ineditas = len(passo["tela"] - palavras)
        palavras |= passo["tela"]
        hash_novo = passo["hash"] is not None and passo["hash"] not in hashes
        if passo["hash"] is not None:
            hashes.add(passo["hash"])
        ganho = novas > 0
        if regua == "A" and cresceu:
            ganho = True
        if regua == "B" and cresceu and hash_novo:
            ganho = True
        if regua in ("C", "D") and cresceu and interagiu and ineditas >= _TELA_INEDITA_MIN:
            ganho = True
        if regua == "D" and marcou and ineditas >= _TELA_INEDITA_MIN:
            ganho = True
        if ganho:
            ultimo_ganho = cobrados
            ganhos += 1
        novas = cresceu = 0
        if notas and cobrados - ultimo_ganho >= _SEM_GANHO_MAX:
            return passo["n"], min(base + ganhos, base * 2)
        for tipo, idx, texto in passo["notas"]:
            if tipo == "nova":
                notas[idx] = texto
                if texto.strip() != "resposta do `look`":
                    novas += 1
            else:
                if len(_fichas(texto) - _fichas(notas.get(idx, ""))) >= _NOTA_CRESCEU_MIN:
                    cresceu += 1
                notas[idx] = texto
        if passo["acao"] not in NAO_COBRA:
            cobrados += 1
        interagiu = passo["acao"] in _INTERAGE and passo["ok"]
        marcou = passo["marcou"]
    return None, min(base + ganhos, base * 2)


def main() -> None:
    raiz = sys.argv[1] if len(sys.argv) > 1 else "/app/logs/browser_local"
    reguas = ("velha", "A", "B", "C", "D")
    print("job      | estado    | passos | base | parada real | " + " | ".join(f"{r:<11}" for r in reguas))
    for path in sorted(glob.glob(f"{raiz}/*/trace.md"), key=os.path.getmtime):
        base, passos, estado, parada_real = _ler(path)
        if not passos:
            continue
        colunas = []
        for regua in reguas:
            para, teto = _simular(passos, regua, base)
            colunas.append(f"{('para@' + str(para)) if para else 'segue':>7} t{teto:<3}")
        print(
            f"{path.split('/')[-2][:8]} | {estado[:9]:<9} | {len(passos):>6} | {base:>4} | "
            f"{str(parada_real or '-'):>11} | " + " | ".join(colunas)
        )


main()
