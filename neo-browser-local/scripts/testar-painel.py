#!/usr/bin/env python3
"""Exercita o painel do Neo (a linha do tempo) num Chromium de verdade.

Content script não roda fora de uma extensão, então `chrome.runtime` e
`chrome.storage` são dublados. O dublê do `sendMessage` é também o ponto de
observação do teste: o shadow é FECHADO, então a lista de itens não é
alcançável de fora — mas o estado que o painel persiste no background passa por
ali inteiro, com a linha do tempo dentro.

O que está sob teste:
  • os eventos EMPILHAM (era uma faixa de uma linha, trocada a cada passo);
  • ligar o painel não mexe uma linha na página (é lateral, `fixed`, sem clique);
  • o PARE marca o atributo que o driver lê no snapshot;
  • `active:false` (o `browser.release`) NÃO apaga a linha do tempo da tela;
  • navegar re-monta a lista a partir do estado do background;
  • esconder pelo id (o que o Rust faz antes de cada print) funciona de fora.

    python3 scripts/testar-painel.py [url]
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
PADRAO = AQUI.parent / "neo-desktop-tauri/scripts/snapshot-lab/questionario.html"

# Os dublês. `sendMessage` guarda tudo o que o painel manda para o background —
# é dali que o teste lê a linha do tempo.
STUB = """
globalThis.__enviadas = [];
globalThis.chrome = {
  runtime: {
    lastError: null,
    onMessage: { addListener: (fn) => { globalThis.__ouvinte = fn; } },
    sendMessage: (msg, cb) => {
      globalThis.__enviadas.push(msg);
      if (typeof cb === 'function') cb(globalThis.__resposta || undefined);
    },
  },
  storage: {
    local: { get: (_k, cb) => cb({}), set: () => {} },
  },
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

HOST = """
(() => {
  const host = document.getElementById('neo-browser-local-overlay');
  if (!host) return { existe: false };
  const s = getComputedStyle(host);
  const r = host.getBoundingClientRect();
  return {
    existe: true,
    fixo: s.position,
    semClique: s.pointerEvents,
    visibilidade: s.visibility,
    opacidade: s.opacity,
    caixa: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    ehOUltimoFilho: document.documentElement.lastElementChild === host,
    larguraDaJanela: innerWidth,
  };
})()
"""

# O estado que o painel persistiu por último — a linha do tempo inteira.
ULTIMO = """
(() => {
  const sets = globalThis.__enviadas.filter((m) => m && m.type === 'NEO_OVERLAY_SET');
  const ultimo = sets[sets.length - 1];
  if (!ultimo) return null;
  const st = ultimo.state;
  return {
    active: st.active,
    stopped: st.stopped,
    itens: (st.items || []).map((i) => [i.kind, i.text, (i.detail || '').slice(0, 24), i.vezes]),
  };
})()
"""


# Onde os pixels mudaram entre duas fotos. Roda NA PÁGINA porque o shadow é
# fechado e porque decodificar PNG em Python pediria dependência nova.
DIFERENCA = """
(async (a, b) => {
  const carrega = (b64) => new Promise((ok) => {
    const img = new Image();
    img.onload = () => ok(img);
    img.src = 'data:image/png;base64,' + b64;
  });
  const [ia, ib] = await Promise.all([carrega(a), carrega(b)]);
  const px = (img) => {
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    c.getContext('2d').drawImage(img, 0, 0);
    return c.getContext('2d').getImageData(0, 0, img.width, img.height).data;
  };
  const [pa, pb] = [px(ia), px(ib)];
  let x0 = 1e9, y0 = 1e9, x1 = -1, y1 = -1, n = 0;
  for (let i = 0; i < pa.length; i += 4) {
    if (Math.abs(pa[i] - pb[i]) + Math.abs(pa[i+1] - pb[i+1]) + Math.abs(pa[i+2] - pb[i+2]) < 12) continue;
    const p = i / 4, x = p % ia.width, y = (p - x) / ia.width;
    n++;
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  return { pixels: n, caixa: n ? [x0, y0, x1 - x0 + 1, y1 - y0 + 1] : null,
           largura: ia.width, altura: ia.height };
})
"""


# Onde está o botão PARE, em pixels. O painel é fixo no TOPO e tem altura
# variável (acompanha a lista), então a posição do botão depende do conteúdo —
# e o shadow é fechado, não dá para perguntar ao DOM. O vermelho do PARE é o
# único da tela: o teste acha o botão do mesmo jeito que um olho acharia.
ACHAR_BOTAO = """
(async (b64) => {
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
    if (!(d[i] > 180 && d[i+1] < 80 && d[i+2] < 80)) continue;
    const p = i / 4, x = p % img.width, y = (p - x) / img.width;
    n++;
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  if (n < 500) return null;
  const escala = img.width / innerWidth;
  return { x: Math.round((x0 + x1) / 2 / escala), y: Math.round((y0 + y1) / 2 / escala),
           pixels: n, altura: Math.round((y1 - y0 + 1) / escala) };
})
"""


def evento(**kwargs) -> str:
    return f"postMessage({{ __neo_overlay: {json.dumps(kwargs)} }}, '*')"


async def main(url: str) -> int:
    perfil = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [CHROME, "--remote-debugging-port=9556", "--headless", "--disable-gpu",
         "--no-sandbox", "--window-size=1600,1000", f"--user-data-dir={perfil}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        alvo = None
        for _ in range(60):
            try:
                alvo = json.loads(urllib.request.urlopen("http://127.0.0.1:9556/json").read())
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
            await ev(STUB)
            await ev((AQUI / "content-overlay.js").read_text(encoding="utf-8"))

            falhas = []

            # Sem tarefa o painel não existe — página limpa.
            if await ev("!!document.getElementById('neo-browser-local-overlay')"):
                falhas.append("o painel subiu sem nenhum evento")

            # A sequência de um passo de verdade.
            await ev(evento(active=True, kind="fase", text="preparando a aba…"))
            await ev(evento(active=True, kind="passo", text="vou abrir a busca",
                            detail="passo 1/14 · abrindo https://exemplo.test"))
            await ev(evento(active=True, kind="ok", text="(li de «resultados»)",
                            detail="X" * 400))
            await ev(evento(active=True, kind="erro", text="não deu: clicando em «Entrar»",
                            detail="elemento desabilitado"))
            await ev(evento(active=True, kind="erro", text="não deu: clicando em «Entrar»",
                            detail="elemento desabilitado"))
            await asyncio.sleep(0.3)

            depois = await ev(MEDIR)
            host = await ev(HOST)
            estado = await ev(ULTIMO)

            print("── host ──")
            print(json.dumps(host, ensure_ascii=False))
            print("── linha do tempo ──")
            print(json.dumps(estado, ensure_ascii=False, indent=1))

            if not host.get("existe"):
                falhas.append("o host do painel não foi criado")
            if host.get("fixo") != "fixed":
                falhas.append(f"host não é position:fixed ({host.get('fixo')})")
            if host.get("semClique") != "none":
                falhas.append("host captura clique — roubaria clique da página")
            if not host.get("ehOUltimoFilho"):
                falhas.append("host não é o último filho do <html>")
            # O painel DESENHA, e só na lateral direita? O host tem 0px de
            # largura de propósito (os filhos são `fixed`), e o shadow é
            # fechado: de fora, a única prova é a imagem. É a mesma foto que o
            # `browser.screenshot` tira, então isto mede os dois de uma vez.
            com = (await cmd("Page.captureScreenshot", {"format": "png"})).get("data", "")
            # Como o Rust esconde (`JS_PAINEL_OCULTAR`): opacidade, não `visibility`
            # — `visibility:hidden` tirava o foco do campo em que ele escrevia.
            await ev("document.getElementById('neo-browser-local-overlay').style.opacity='0'")
            if (await ev(HOST)).get("opacidade") != "0":
                falhas.append("esconder pelo id não funcionou (o print sairia com o painel)")
            sem = (await cmd("Page.captureScreenshot", {"format": "png"})).get("data", "")
            await ev("document.getElementById('neo-browser-local-overlay').style.opacity=''")

            dif = await ev(f"({DIFERENCA})({json.dumps(com)}, {json.dumps(sem)})")
            print("── pixels do painel ──")
            print(json.dumps(dif, ensure_ascii=False))
            if not dif or dif["pixels"] < 2000:
                falhas.append(f"o painel não desenhou nada na foto ({(dif or {}).get('pixels')} px)")
            elif dif["caixa"]:
                x0, _, larg, _ = dif["caixa"]
                if x0 < dif["largura"] * 0.55:
                    falhas.append(f"o painel invade a página: começa em x={x0} de {dif['largura']}")
                if larg > dif["largura"] * 0.40:
                    falhas.append(f"o painel ocupa {larg}px de {dif['largura']} — era para ser uma lateral")

            # O CORAÇÃO: a página não pode ter se mexido.
            for chave in ("alturaDoDocumento", "larguraDoCorpo", "alvo", "rolagem"):
                if antes[chave] != depois[chave]:
                    falhas.append(f"a página MUDOU: {chave} {antes[chave]} → {depois[chave]}")
            if depois["nosNoBody"] != antes["nosNoBody"]:
                falhas.append("nó novo dentro do <body> — o painel tem que ficar no <html>")

            # Os eventos EMPILHARAM (e o repetido virou contador).
            itens = (estado or {}).get("itens") or []
            if len(itens) != 4:
                falhas.append(f"esperava 4 itens na linha do tempo, veio {len(itens)}")
            if [i[0] for i in itens] != ["fase", "passo", "ok", "erro"]:
                falhas.append(f"tipos fora de ordem: {[i[0] for i in itens]}")
            if itens and itens[-1][3] != 2:
                falhas.append(f"evento repetido não virou contador: {itens[-1]}")
            if any(len(i[2]) and len(i[2]) > 24 for i in itens):
                falhas.append("detalhe não foi cortado")

            # PARE. O botão mudou de lugar (era no topo, agora é no pé do
            # painel) e vive num shadow FECHADO: o teste clica como o usuário,
            # por coordenada, com hit-testing de verdade. Se o
            # `pointer-events:auto` do botão se perder num refactor, o clique
            # cai na página e este teste acusa.
            foto = (await cmd("Page.captureScreenshot", {"format": "png"})).get("data", "")
            botao = await ev(f"({ACHAR_BOTAO})({json.dumps(foto)})")
            print("── botão PARE ──")
            print(json.dumps(botao, ensure_ascii=False))
            if not botao:
                falhas.append("não achei o PARE na tela — o botão sumiu do painel")
                botao = {"x": 0, "y": 0}
            bx, by = botao["x"], botao["y"]
            for tipo in ("mousePressed", "mouseReleased"):
                await cmd("Input.dispatchMouseEvent", {
                    "type": tipo, "x": bx, "y": by, "button": "left",
                    "clickCount": 1, "buttons": 1 if tipo == "mousePressed" else 0,
                })
            await asyncio.sleep(0.25)
            freio = await ev("document.documentElement.getAttribute('data-neo-stop')")
            apos_pare = await ev(ULTIMO)
            print("── PARE ──")
            print(json.dumps({"atributo": freio, "stopped": (apos_pare or {}).get("stopped"),
                              "ultimo": ((apos_pare or {}).get("itens") or [[None]])[-1]},
                             ensure_ascii=False))
            if freio != "1":
                falhas.append("o PARE não marcou data-neo-stop — o freio do snapshot morreu")
            if (apos_pare or {}).get("stopped") is not True:
                falhas.append("o PARE não foi persistido")
            if not any(i[0] == "parado" for i in ((apos_pare or {}).get("itens") or [])):
                falhas.append("o PARE não entrou na linha do tempo")
            if not any(m.get("type") == "NEO_STOP" for m in (await ev("globalThis.__enviadas"))):
                falhas.append("o PARE não avisou o background (caminho rápido do neo.stop)")

            # Fim da tarefa (`browser.release` manda active:false): a linha do
            # tempo CONTINUA na tela — é a última coisa que o usuário lê.
            await ev(evento(active=True, kind="fim", text="terminei", detail="achei os 3 preços"))
            await ev(evento(active=False))
            await asyncio.sleep(0.2)
            fim = await ev(HOST)
            estado_fim = await ev(ULTIMO)
            if not fim.get("existe"):
                falhas.append("o painel sumiu no fim da tarefa — a entrega ia com ele")
            if (estado_fim or {}).get("active") is not False:
                falhas.append("o fim não foi persistido como encerrado")
            if len(((estado_fim or {}).get("itens") or [])) < 5:
                falhas.append("a linha do tempo perdeu itens no fim")

            # Navegação: o background devolve o estado e o painel re-monta.
            await ev(f"globalThis.__resposta = {{ state: {json.dumps(estado_fim)} }}")
            await ev("globalThis.__enviadas.length = 0")
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(1.5)
            await ev(STUB.replace("globalThis.__enviadas = [];", ""))
            print("(re-montagem depois de navegar: estado devolvido pelo background)")

            print()
            if falhas:
                for f in falhas:
                    print(f"FALHA: {f}")
                return 1
            print("OK — empilhou, não mexeu no layout, esconde para o print e sobrevive ao fim.")
            return 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(perfil, ignore_errors=True)


if __name__ == "__main__":
    destino = sys.argv[1] if len(sys.argv) > 1 else f"file://{PADRAO}"
    raise SystemExit(asyncio.run(main(destino)))
