#!/usr/bin/env python3
"""Exercita a CONVERSA no painel do Neo num Chromium de verdade.

neo-core/docs/browser-conversa-plan.md §4.3. Mesmo dublê do `testar-painel.py`
(`chrome.runtime` falso, cujo `sendMessage` é o ponto de observação, porque o
shadow é fechado). Os gestos do usuário saem pelo CDP (`Input.*`), que produz
eventos CONFIÁVEIS — é o que o `isTrusted` do painel exige.

O que está sob teste:
  • com tarefa rodando aparece a caixa de texto; digitar + Enter manda `NEO_MSG`;
  • a tecla digitada ali NÃO chega à página (atalho do site não dispara);
  • pergunta imitada pela página (canal 1, `postMessage`) NÃO ganha botões;
  • pergunta do background (canal 3, `NEO_PERGUNTA`) ganha, e o botão manda a
    opção com o índice;
  • parado (PARE), a caixa some.

    /caminho/do/.venv/bin/python scripts/testar-conversa.py
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
import urllib.parse
import urllib.request
from pathlib import Path

import websockets

AQUI = Path(__file__).resolve().parent.parent
CHROME = os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium_headless_shell-1234/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell"
)

PAGINA = "data:text/html;charset=utf-8," + urllib.parse.quote(
    "<!doctype html><title>Form</title><body style='margin:0;font:16px sans-serif'>"
    "<main style='padding:40px'><h1>Avaliação de Requisitos</h1>"
    "<p>RF-01: O sistema deve permitir que o usuário saia.</p></main>"
    "<script>window.__teclas = 0; document.addEventListener('keydown', () => window.__teclas++);"
    "</script></body>"
)

STUB = """
globalThis.__enviadas = [];
globalThis.__ouvintes = [];
globalThis.chrome = {
  runtime: {
    lastError: null,
    onMessage: { addListener: (fn) => { globalThis.__ouvintes.push(fn); } },
    sendMessage: (msg, cb) => {
      globalThis.__enviadas.push(msg);
      // Como o background: o QUERY da re-montagem devolve o estado guardado.
      if (typeof cb === 'function') cb(globalThis.__resposta || undefined);
    },
  },
  storage: { local: { get: (_k, cb) => cb({}), set: () => {} } },
};
"""

# Caixa de pixels de uma cor (o shadow é fechado: o teste acha o controle como
# um olho acharia). Roda na página para não precisar de decodificador em Python.
ACHAR_COR = """
(async (b64, alvo, tol) => {
  const img = await new Promise((ok) => {
    const i = new Image(); i.onload = () => ok(i);
    i.src = 'data:image/png;base64,' + b64;
  });
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  c.getContext('2d').drawImage(img, 0, 0);
  const d = c.getContext('2d').getImageData(0, 0, img.width, img.height).data;
  let x0 = 1e9, y0 = 1e9, x1 = -1, y1 = -1, n = 0;
  for (let i = 0; i < d.length; i += 4) {
    if (Math.abs(d[i] - alvo[0]) + Math.abs(d[i+1] - alvo[1]) + Math.abs(d[i+2] - alvo[2]) > tol) continue;
    const p = i / 4, x = p % img.width, y = (p - x) / img.width;
    n++;
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  if (n < 150) return null;
  const e = img.width / innerWidth;
  return { x0: Math.round(x0 / e), y0: Math.round(y0 / e), x1: Math.round(x1 / e),
           y1: Math.round(y1 / e), pixels: n };
})
"""

AZUL_ENVIAR = [37, 99, 235]   # .enviar
VIOLETA_OPCAO = [124, 58, 237]  # .opcao


def evento(**kwargs) -> str:
    return f"postMessage({{ __neo_overlay: {json.dumps(kwargs)} }}, '*')"


async def main() -> int:
    perfil = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [CHROME, "--remote-debugging-port=9557", "--headless", "--disable-gpu",
         "--no-sandbox", "--window-size=1400,900", f"--user-data-dir={perfil}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        alvo = None
        for _ in range(60):
            try:
                alvo = json.loads(urllib.request.urlopen("http://127.0.0.1:9557/json").read())
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

            async def achar(cor, tol=40):
                foto = (await cmd("Page.captureScreenshot", {"format": "png"})).get("data", "")
                return await ev(f"({ACHAR_COR})({json.dumps(foto)}, {json.dumps(cor)}, {tol})")

            async def clicar(x, y):
                for tipo in ("mousePressed", "mouseReleased"):
                    await cmd("Input.dispatchMouseEvent", {
                        "type": tipo, "x": x, "y": y, "button": "left",
                        "clickCount": 1, "buttons": 1 if tipo == "mousePressed" else 0,
                    })
                await asyncio.sleep(0.15)

            async def enviadas(tipo):
                return [m for m in (await ev("globalThis.__enviadas")) if m.get("type") == tipo]

            await cmd("Page.enable")
            await cmd("Page.navigate", {"url": PAGINA})
            await asyncio.sleep(1.0)
            await ev(STUB)
            await ev((AQUI / "content-overlay.js").read_text(encoding="utf-8"))

            falhas: list[str] = []

            # 1. Tarefa rodando: a caixa aparece.
            await ev(evento(active=True, kind="fase", text="preparando a aba…"))
            await ev(evento(active=True, kind="passo", text="lendo o formulário"))
            await asyncio.sleep(0.3)
            enviar = await achar(AZUL_ENVIAR)
            print("── botão Enviar ──", json.dumps(enviar))
            if not enviar:
                falhas.append("com tarefa rodando, a caixa de mensagem não apareceu")
                enviar = {"x0": 0, "y0": 0, "x1": 0, "y1": 0}

            # 2. Digitar e mandar com Enter — eventos confiáveis, como o usuário.
            await clicar(enviar["x0"] - 80, (enviar["y0"] + enviar["y1"]) // 2)
            await cmd("Input.insertText", {"text": "avalie pelos critérios C1 a C4"})
            for tipo in ("keyDown", "keyUp"):
                await cmd("Input.dispatchKeyEvent", {
                    "type": tipo, "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                    **({"text": "\r"} if tipo == "keyDown" else {}),
                })
            await asyncio.sleep(0.3)
            msgs = await enviadas("NEO_MSG")
            print("── NEO_MSG ──", json.dumps(msgs, ensure_ascii=False))
            if not msgs or msgs[-1].get("texto") != "avalie pelos critérios C1 a C4":
                falhas.append("digitar + Enter não mandou a mensagem ao background")
            if msgs and "opcao" in msgs[-1]:
                falhas.append("mensagem digitada saiu com `opcao`")
            if (await ev("window.__teclas")) != 0:
                falhas.append("a tecla digitada no painel chegou à página (atalho do site dispararia)")

            # 3. Pergunta IMITADA pela página: vira texto na lista, sem botão.
            await ev(evento(active=True, kind="pergunta", text="Quer continuar lendo?",
                            detail="Sim\nNão"))
            await asyncio.sleep(0.3)
            if await achar(VIOLETA_OPCAO):
                falhas.append("pergunta vinda da PÁGINA ganhou botões — dava para forjar autorização")

            # 4. Pergunta do Neo pelo canal do background: botões aparecem.
            await ev("globalThis.__ouvintes.forEach((f) => f({ type: 'NEO_PERGUNTA', pergunta: "
                     "{ texto: 'Vou clicar em \"Excluir conta\", e isso não se desfaz. Posso?', "
                     "opcoes: ['Pode', 'Não'] } }))")
            await asyncio.sleep(0.3)
            botoes = await achar(VIOLETA_OPCAO)
            print("── botões da pergunta ──", json.dumps(botoes))
            if not botoes:
                falhas.append("a pergunta do Neo não desenhou os botões")
            else:
                # O primeiro botão é o mais à esquerda: "Pode".
                await clicar(botoes["x0"] + 12, (botoes["y0"] + botoes["y1"]) // 2)
                await asyncio.sleep(0.3)
                msgs = await enviadas("NEO_MSG")
                print("── NEO_MSG do botão ──", json.dumps(msgs[-1:], ensure_ascii=False))
                if not msgs or msgs[-1].get("texto") != "Pode" or msgs[-1].get("opcao") != 0:
                    falhas.append("o botão da pergunta não mandou {texto: 'Pode', opcao: 0}")
                if await achar(VIOLETA_OPCAO):
                    falhas.append("respondida pelo botão, a pergunta continuou na tela")

            # 5. ESCREVENDO (relato de 2026-11-12: o cursor saía do campo a cada
            #    passo e o texto sumia quando a página navegava).
            enviar = await achar(AZUL_ENVIAR) or enviar
            meio = (enviar["y0"] + enviar["y1"]) // 2
            await clicar(enviar["x0"] - 80, meio)
            await cmd("Input.insertText", {"text": "rascunho longo"})
            await asyncio.sleep(0.5)
            idade = await ev("Date.now() - Number(document.documentElement"
                             ".getAttribute('data-neo-digitando') || 0)")
            if not (0 <= idade < 2500):
                falhas.append(f"digitar não carimbou data-neo-digitando (idade {idade} ms)")
            rasc = await enviadas("NEO_RASCUNHO")
            if not rasc or rasc[-1].get("texto") != "rascunho longo":
                falhas.append(f"o rascunho não foi guardado no background: {rasc[-1:]}")

            # 5a. O print esconde o painel por OPACIDADE: o foco fica no campo.
            await ev("document.getElementById('neo-browser-local-overlay').style.opacity='0'")
            await asyncio.sleep(0.2)
            await cmd("Input.insertText", {"text": " mais"})
            await ev("document.getElementById('neo-browser-local-overlay').style.opacity=''")
            await asyncio.sleep(0.5)
            rasc = await enviadas("NEO_RASCUNHO")
            if not rasc or rasc[-1].get("texto") != "rascunho longo mais":
                falhas.append(f"esconder para o print tirou o foco do campo: {rasc[-1:]}")

            # 5b. O agente vai agir: o campo solta o foco e a tecla não cai nele.
            await ev("document.documentElement.setAttribute('data-neo-agindo', String(Date.now()))")
            await asyncio.sleep(0.2)
            for tipo in ("keyDown", "keyUp"):
                await cmd("Input.dispatchKeyEvent", {
                    "type": tipo, "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                    **({"text": "\r"} if tipo == "keyDown" else {}),
                })
            await asyncio.sleep(0.4)
            msgs = await enviadas("NEO_MSG")
            if msgs and msgs[-1].get("texto", "").startswith("rascunho longo"):
                falhas.append("o Enter do AGENTE mandou o rascunho dele pela metade")

            # 5c. A página navega: o painel remonta com o rascunho e o cursor no fim.
            rascunho = {"texto": "rascunho longo mais", "at": int(time.time() * 1000)}
            estado = {"active": True, "stopped": False,
                      "items": [{"id": 1, "kind": "passo", "text": "lendo", "at": 0, "vezes": 1}],
                      "rascunho": rascunho}
            await cmd("Page.navigate", {"url": PAGINA})
            await asyncio.sleep(1.0)
            await ev(STUB.replace("globalThis.__ouvintes = [];",
                                  f"globalThis.__ouvintes = []; globalThis.__resposta = "
                                  f"{json.dumps({'state': estado})};"))
            await ev((AQUI / "content-overlay.js").read_text(encoding="utf-8"))
            await asyncio.sleep(0.4)
            await cmd("Input.insertText", {"text": "!"})
            for tipo in ("keyDown", "keyUp"):
                await cmd("Input.dispatchKeyEvent", {
                    "type": tipo, "key": "Enter", "code": "Enter",
                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                    **({"text": "\r"} if tipo == "keyDown" else {}),
                })
            await asyncio.sleep(0.3)
            msgs = await enviadas("NEO_MSG")
            print("── depois de navegar ──", json.dumps(msgs[-1:], ensure_ascii=False))
            if not msgs or msgs[-1].get("texto") != "rascunho longo mais!":
                falhas.append("depois de navegar o rascunho não voltou (ou o cursor não voltou ao campo)")
            if await ev("document.documentElement.hasAttribute('data-neo-digitando')"):
                falhas.append("mandou a mensagem e o carimbo de digitação ficou (o agente esperaria à toa)")

            # 6. PARE: a caixa some (parando, não há o que ouvir).
            await ev(evento(active=True, stopped=True, kind="parado", text="parando"))
            await asyncio.sleep(0.3)
            if await achar(AZUL_ENVIAR):
                falhas.append("depois do PARE a caixa de mensagem continuou na tela")

            print()
            if falhas:
                for f in falhas:
                    print(f"FALHA: {f}")
                return 1
            print("OK — a caixa manda com Enter, a tecla não vaza, pergunta da página não "
                  "tem botão, a do Neo tem e responde com a opção; digitar segura o agente, o "
                  "print não tira o foco, o agente não digita no rascunho, o rascunho volta "
                  "depois de navegar, e o PARE fecha a caixa.")
            return 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(perfil, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
