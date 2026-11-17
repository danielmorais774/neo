#!/usr/bin/env python3
"""Prova a ELEIÇÃO do conteúdo principal do `js_snapshot`, contra um corpus.

Por que existe: no job 29c739d4 o vencedor da eleição foi o wrapper da página
inteira da Amazon. Os 2200 caracteres de orçamento viraram skip-links e a lista
de departamentos, os 5 pedidos começavam 659 caracteres depois do corte, e o
modelo rolou 6 vezes atrás de um texto que não muda com rolagem — `body.innerText`
sai em ordem de documento. Morreu no freio de giro com a resposta na tela.

O critério antigo era `t.length > principal.texto.length`: "o maior texto ganha".
Como `closest(CHROME)` só rejeita quem ESTÁ dentro da moldura — nunca um `<div>`
que a EMBRULHA — esse critério converge sempre para o wrapper mais externo, isto
é, para a página toda. O conserto é pontuar texto LÍQUIDO (fora moldura) e
desempatar pelo MAIS PROFUNDO dentro de 10%.

CORPUS, e não um caso: afinar contra uma página é como se chega num atalho por
site disfarçado de heurística. Cada fixture abaixo é uma FORMA de página, e a
Amazon entra como uma amostra entre outras.

    python3 conteudo-principal.py            # roda o corpus inteiro
    python3 conteudo-principal.py <url|arq>  # inspeciona uma página só

Sai 1 se qualquer caso do corpus falhar.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parents[2]
# `NEO_LAB_RS` mede outra versão do arquivo (a de antes de um conserto, ou uma
# cópia em teste) sem tocar no `.rs` que o `tauri dev` está vigiando.
RS = Path(os.environ.get("NEO_LAB_RS") or RAIZ / "neo-desktop-tauri/src-tauri/src/services/browser_local.rs")
CHROME = os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium_headless_shell-1234/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell"
)

_FONTE = RS.read_text(encoding="utf-8")
_HELPERS = re.search(r'const JS_HELPERS: &str = r#"(.*?)"#;', _FONTE, re.S).group(1)


def js(nome: str, **kw) -> str:
    """Extrai a string JS de dentro do Rust — a MESMA que o desktop injeta."""
    i = _FONTE.index(f"fn {nome}(")
    j = _FONTE.index('r#"', i)
    k = _FONTE.index('"#', j)
    t = _FONTE[j + 3 : k].replace("{{", "\x00").replace("}}", "\x01")
    t = t.replace("{helpers}", _HELPERS)
    for a, b in kw.items():
        t = t.replace("{" + a + "}", str(b))
    return t.replace("\x00", "{").replace("\x01", "}")


# ── o corpus ────────────────────────────────────────────────────────────────
# `espera`: um trecho que TEM de estar no conteúdo eleito (e, quando dado,
# `proibido`: um trecho de moldura que NÃO pode abrir o bloco eleito).
CORPUS = [
    {
        "nome": "sem landmark, wrapper de moldura grande (Amazon, job 29c739d4)",
        "pagina": RAIZ / "sample-log/Seus pedidos.html",
        "espera": "Entregue no dia",
        "proibido_no_inicio": "Ir para",
    },
    {
        "nome": "índice lateral competindo com o enunciado (job 9968e68a)",
        "pagina": AQUI / "questionario.html",
        "espera": "Durante a transição para o trabalho livre",
    },
    {
        # O overlay desta fixture é um `position:fixed` SEM papel ARIA, então ele
        # nunca entra no ramo do modal: o que ela prova é que a eleição continua
        # achando o enunciado por baixo dele (e, no `e2e.py`, que os elementos
        # saem marcados `coberto`).
        "nome": "overlay sem papel ARIA não rouba o conteúdo de baixo",
        "pagina": AQUI / "questionario-com-modal.html",
        "espera": "Tendo em vista a relação entre desigualdade",
    },
    {
        "nome": "modal de verdade (aria-modal + fixed) GANHA do conteúdo atrás",
        "pagina": AQUI / "com-modal-aria.html",
        "espera": "4821-ZX",
        "proibido_no_inicio": "A página atrás do modal",
    },
    {
        "nome": "<main> declarado ganha do wrapper",
        "pagina": AQUI / "com-main.html",
        "espera": "O corpo da matéria começa aqui",
        "proibido_no_inicio": "Pular para",
    },
    {
        # O enunciado mora no `<header>` do `<article>`. Com o seletor antigo da
        # moldura ele era descontado como cabeçalho do site.
        "nome": "<header> dentro do cartão é conteúdo (12axes, job 7b73f755)",
        "pagina": AQUI / "cartao-com-header.html",
        "espera": "declared by each person must be respected",
        "proibido_no_inicio": "Restart quiz",
    },
    {
        "nome": "<aside> grande não rouba o artigo",
        "pagina": AQUI / "com-aside.html",
        "espera": "O artigo de verdade começa aqui",
        "proibido_no_inicio": "Mais lidas da semana",
    },
]


async def _sessao(ev_para):
    perfil = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [
            CHROME,
            "--remote-debugging-port=9445",
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--window-size=1600,1000",
            f"--user-data-dir={perfil}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        alvo = None
        for _ in range(60):
            try:
                alvo = json.loads(urllib.request.urlopen("http://127.0.0.1:9445/json").read())
                if alvo:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not alvo:
            raise RuntimeError("o Chromium headless não subiu")
        async with websockets.connect(
            alvo[0]["webSocketDebuggerUrl"], max_size=60_000_000
        ) as ws:
            n = [0]

            async def ev(expr):
                n[0] += 1
                await ws.send(
                    json.dumps(
                        {
                            "id": n[0],
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expr,
                                "returnByValue": True,
                                "awaitPromise": True,
                            },
                        }
                    )
                )
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    if m.get("id") == n[0]:
                        r = m["result"]
                        if "exceptionDetails" in r:
                            raise RuntimeError(json.dumps(r["exceptionDetails"])[:600])
                        return r["result"].get("value")

            async def cmd(met, par):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": met, "params": par}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    if m.get("id") == n[0]:
                        return m

            await cmd("Page.enable", {})
            await ev_para(ev, cmd)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(perfil, ignore_errors=True)


def _url(p) -> str:
    s = str(p)
    return s if s.startswith(("http://", "https://", "file://")) else f"file://{s}"


async def _rodar(casos: list[dict], verboso: bool) -> int:
    falhas: list[str] = []

    async def corpo(ev, cmd):
        for caso in casos:
            await cmd("Page.navigate", {"url": _url(caso["pagina"])})
            await asyncio.sleep(2.0)
            snap = await ev(js("js_snapshot", max=200, chars=3000))
            main = snap.get("main") or {}
            rotulo = main.get("label") or "(nenhum)"
            texto = main.get("text") or ""
            inicio = " / ".join(
                [l for l in texto.split("\n") if l.strip()][:4]
            )[:110]
            print(f"\n■ {caso['nome']}")
            print(f"  eleito    : «{rotulo}»  ({len(texto)} chars"
                  f"{', CORTADO' if main.get('cut') else ''})")
            print(f"  começa em : {inicio or '(vazio)'}")
            print(f"  corpo     : {len(snap.get('text') or '')} chars"
                  f"{', CORTADO' if snap.get('text_cut') else ''}")
            if verboso:
                print("  ---- texto eleito ----")
                print("  " + texto[:1200].replace("\n", "\n  "))

            espera = caso.get("espera") or ""
            if espera and espera not in texto:
                falhas.append(f"{caso['nome']}: «{espera}» não está no conteúdo eleito")
            proibido = caso.get("proibido_no_inicio")
            if proibido and texto.lstrip().startswith(proibido):
                falhas.append(
                    f"{caso['nome']}: o bloco eleito ABRE com moldura («{proibido}»)"
                )

    await _sessao(corpo)
    if falhas:
        print("\nFALHOU:")
        for f in falhas:
            print(f"  ✗ {f}")
        return 1
    print(f"\n✓ {len(casos)} casos do corpus passaram")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        casos = [{"nome": sys.argv[1], "pagina": sys.argv[1], "espera": ""}]
        sys.exit(asyncio.run(_rodar(casos, verboso=True)))
    faltando = [c["nome"] for c in CORPUS if not Path(str(c["pagina"])).exists()]
    if faltando:
        print("fixtures ausentes:\n  " + "\n  ".join(faltando))
        sys.exit(2)
    sys.exit(asyncio.run(_rodar(CORPUS, verboso=False)))
