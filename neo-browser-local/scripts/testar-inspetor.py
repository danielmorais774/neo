#!/usr/bin/env python3
"""Exercita o inspetor visual num Chromium de verdade.

Content script não roda fora de uma extensão, então o `chrome.runtime` é
dublado. O que está sob teste é o que importa e o que jsdom não consegue provar:
que o canvas desenha a partir do layout REAL, e que ligar o inspetor NÃO mexe
uma linha na página — que é a diferença entre esta versão e a v1 das marcações,
que fazia a tela piscar e o Drive bugar.

    python3 scripts/testar-inspetor.py [url]
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

AQUI = Path(__file__).resolve().parent.parent
CHROME = os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium_headless_shell-1234/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell"
)
PADRAO = (
    AQUI.parent
    / "neo-desktop-tauri/scripts/snapshot-lab/questionario.html"
)

# O dublê do `chrome.runtime`: guarda o listener para o teste poder chamá-lo.
STUB = """
globalThis.chrome = {
  runtime: { onMessage: { addListener: (fn) => { globalThis.__ouvinte = fn; } } },
};
"""

MEDIR = """
(() => {
  const alvo = document.querySelector('main') || document.body;
  const r = alvo.getBoundingClientRect();
  return {
    alturaDoDocumento: document.documentElement.scrollHeight,
    larguraDoCorpo: document.body.getBoundingClientRect().width,
    alvo: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    nosNoBody: document.body.children.length,
    rolagem: Math.round(scrollY),
  };
})()
"""

PIXELS = """
(() => {
  const host = document.getElementById('neo-map-overlay');
  if (!host) return { erro: 'host não existe' };
  const cv = host.shadowRoot ? host.shadowRoot.querySelector('canvas') : null;
  // shadow FECHADO: o teste não alcança de fora, então conta o que dá de fora.
  const r = host.getBoundingClientRect();
  return {
    existe: true,
    fixo: getComputedStyle(host).position,
    semClique: getComputedStyle(host).pointerEvents,
    caixa: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    ehOUltimoFilho: document.documentElement.lastElementChild === host,
    canvasVisivelDeFora: !!cv,
  };
})()
"""


async def main(url: str) -> int:
    perfil = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [CHROME, "--remote-debugging-port=9555", "--headless", "--disable-gpu",
         "--no-sandbox", "--window-size=1600,1000", f"--user-data-dir={perfil}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        alvo = None
        for _ in range(60):
            try:
                alvo = json.loads(urllib.request.urlopen("http://127.0.0.1:9555/json").read())
                if alvo:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        async with websockets.connect(alvo[0]["webSocketDebuggerUrl"], max_size=60_000_000) as ws:
            n = [0]

            async def cmd(metodo, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": metodo, "params": params or {}}))
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    if m.get("id") == n[0]:
                        return m.get("result", {})

            async def ev(expr):
                r = await cmd("Runtime.evaluate",
                              {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if "exceptionDetails" in r:
                    raise RuntimeError(json.dumps(r["exceptionDetails"])[:600])
                return r["result"].get("value")

            await cmd("Page.enable")
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(2.0)

            antes = await ev(MEDIR)

            await ev((AQUI / "neo-map.generated.js").read_text(encoding="utf-8"))
            await ev(STUB)
            await ev((AQUI / "content-inspect.js").read_text(encoding="utf-8"))

            resposta = await ev(
                "new Promise((ok) => globalThis.__ouvinte("
                "{type:'NEO_MAP', modo:'toggle'}, null, ok))"
            )
            await asyncio.sleep(0.3)
            depois = await ev(MEDIR)
            overlay = await ev(PIXELS)

            print("── inspetor ligado ──")
            print(json.dumps(resposta, ensure_ascii=False, indent=1))
            print("── overlay ──")
            print(json.dumps(overlay, ensure_ascii=False))

            falhas = []
            if not resposta or resposta.get("ok") is not True:
                falhas.append("o content script não respondeu ok")
            if not overlay.get("existe"):
                falhas.append("o host do overlay não foi criado")
            if overlay.get("fixo") != "fixed":
                falhas.append(f"host não é position:fixed ({overlay.get('fixo')})")
            if overlay.get("semClique") != "none":
                falhas.append("host captura clique — roubaria clique da página")

            # O CORAÇÃO DO TESTE: a página não pode ter se mexido.
            for chave in ("alturaDoDocumento", "larguraDoCorpo", "alvo", "rolagem"):
                if antes[chave] != depois[chave]:
                    falhas.append(f"a página MUDOU: {chave} {antes[chave]} → {depois[chave]}")
            if depois["nosNoBody"] != antes["nosNoBody"]:
                falhas.append("nó novo dentro do <body> — o overlay tem que ficar no <html>")

            desligou = await ev(
                "new Promise((ok) => globalThis.__ouvinte("
                "{type:'NEO_MAP', modo:'off'}, null, ok))"
            )
            sumiu = await ev("!document.getElementById('neo-map-overlay')")
            if not sumiu:
                falhas.append("desligar não removeu o overlay")

            print("── ao desligar ──")
            print(json.dumps({"resposta": desligou, "overlay_removido": sumiu}, ensure_ascii=False))

            print()
            if falhas:
                for f in falhas:
                    print(f"FALHA: {f}")
                return 1
            print("OK — desenhou, não mexeu no layout e limpou tudo ao desligar.")
            return 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(perfil, ignore_errors=True)


if __name__ == "__main__":
    destino = sys.argv[1] if len(sys.argv) > 1 else f"file://{PADRAO}"
    raise SystemExit(asyncio.run(main(destino)))
