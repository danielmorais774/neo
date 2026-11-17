"""O corpus do CLIQUE: mira, disparo e sonda sobre botões que enganam.

    python3 clique.py                    # o .rs do repositório, sobre o corpus
    python3 clique.py --rs outro.rs      # outra versão do mesmo arquivo
    python3 clique.py --url <url> "nome" # varre uma página de verdade por um nome

Refaz o caminho de `clicar()` do Rust (mirar → mouse real → sonda → reserva), com
o JS extraído do `.rs`. Cada cena conta quantas vezes o handler de verdade rodou,
e a conta tem de ser EXATAMENTE 1: 0 é o clique morto do job 8bc25dd1, 2 é a
reserva disparando em cima do mouse real. As duas falhas contam.

`--rs` com um `.rs` anterior a este desenho cai no clique antigo, para medir o
antes e o depois sobre as mesmas cenas.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

import websockets

AQUI = os.path.dirname(os.path.abspath(__file__))
RS_PADRAO = os.path.abspath(os.path.join(AQUI, "../../src-tauri/src/services/browser_local.rs"))
PORTA = 9491
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def chrome() -> str:
    achados = glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/"
        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"))
    if not achados:
        raise SystemExit("sem Chromium do Playwright")
    return sorted(achados)[-1]


class Rs:
    def __init__(self, caminho: str) -> None:
        self.s = open(caminho).read()
        self.helpers = re.search(r'const JS_HELPERS: &str = r#"(.*?)"#;', self.s, re.S).group(1)
        self.novo = "fn js_mirar(" in self.s

    def js(self, nome: str, **kw) -> str:
        i = self.s.index(f"fn {nome}(")
        j = self.s.index('r#"', i)
        k = self.s.index('"#', j)
        t = self.s[j + 3:k].replace("{{", "\x00").replace("}}", "\x01").replace("{helpers}", self.helpers)
        for a, b in kw.items():
            t = t.replace("{" + a + "}", str(b))
        return t.replace("\x00", "{").replace("\x01", "}")

    def const(self, nome: str) -> str:
        return re.search(rf'const {nome}: &str = r#"(.*?)"#;', self.s, re.S).group(1)


class Aba:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.n = 0

    async def cmd(self, metodo: str, params: dict | None = None) -> dict:
        self.n += 1
        atual = self.n
        await self.ws.send(json.dumps({"id": atual, "method": metodo, "params": params or {}}))
        while True:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=60))
            if m.get("id") == atual:
                return m

    async def ev(self, expr: str):
        m = await self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        r = m.get("result", {})
        if "exceptionDetails" in r:
            det = r["exceptionDetails"]
            return {"ok": False, "error": "JS: " + str(det.get("exception", {}).get("description") or det.get("text"))[:300]}
        return r.get("result", {}).get("value")

    async def mouse(self, x: float, y: float, vezes: int) -> None:
        async def um(tipo, botao, botoes, n):
            await self.cmd("Input.dispatchMouseEvent", {
                "type": tipo, "x": x, "y": y, "button": botao, "buttons": botoes, "clickCount": n})
        await um("mouseMoved", "none", 0, 0)
        for n in range(1, vezes + 1):
            await um("mousePressed", "left", 1, n)
            await um("mouseReleased", "left", 0, n)


async def clicar(aba: Aba, rs: Rs, mira: str, vezes: int = 1) -> dict:
    """O mesmo fluxo do `clicar()` em Rust."""
    alvo = await aba.ev(mira)
    if not alvo or not alvo.get("ok"):
        return alvo or {"ok": False, "error": "sem resposta"}
    if alvo.get("real"):
        await aba.mouse(alvo["x"], alvo["y"], vezes)
        r = await aba.ev(rs.const("JS_CLIQUE_CONFIRMA"))
        if r is None or r.get("entregue") is None:
            return {**alvo, "via": "pixel"}
        if r.get("entregue"):
            return r
    return await aba.ev(rs.const("JS_CLIQUE_DESPACHA"))


async def por_ref(aba: Aba, rs: Rs, ref: int) -> dict:
    if rs.novo:
        return await clicar(aba, rs, rs.js("js_mirar", reference=ref, tipo="esquerdo", trava="{}"))
    return await aba.ev(rs.js("js_click", reference=ref))


async def por_nome(aba: Aba, rs: Rs, texto: str, trava: str = "{}", n: int = 0) -> dict:
    needle = json.dumps(texto)
    if rs.novo:
        return await clicar(aba, rs, rs.js("js_click_text", needle=needle, n=n, trava=trava))
    return await aba.ev(rs.js("js_click_text", needle=needle))


# (cena, nome visível, contador esperado)
CENAS = [
    ("amazon", "Adicionar ao carrinho", "amazon"),
    ("irmao", "Enviar proposta", "irmao"),
    ("so-value", "Confirmar presença", "so-value"),
    ("nativo", "Salvar nota", "nativo"),
    ("so-aria", "Fechar aviso", "so-aria"),
    ("so-title", "Abrir relatório", "so-title"),
    ("so-alt", "Baixar fatura", "so-alt"),
    ("link", "Ver detalhes do pedido", "link"),
    ("handler", "Escolher entrega expressa", "handler"),
    ("confiavel", "Validar assinatura", "confiavel"),
    ("radio", "Alternativa correta", "radio"),
    ("some", "Discordo totalmente", "some"),
    ("caixa-sem-label", "Plano mensal", "caixa-sem-label"),
    ("aceite", "Li e aceito os termos", "aceite"),
    ("cartao", "Assinar plano", "cartao"),
    ("sombra", "Gerar segunda via", "sombra"),
    ("igual", "Duplicar item", "igual"),
    ("longe", "Reservar horário", "longe"),
    ("sob-painel", "Debaixo do painel", "sob-painel"),
]


async def corpus(aba: Aba, rs: Rs) -> int:
    url = "file://" + os.path.join(AQUI, "botoes-disfarcados.html")
    falhas = 0
    linhas = []
    for modo in ("nome", "ref"):
        for cena, nome, chave in CENAS:
            await aba.cmd("Page.navigate", {"url": url})
            await asyncio.sleep(0.25)
            snap = await aba.ev(rs.js("js_snapshot", max=200, chars=3000))
            ret: dict = {}
            if modo == "nome":
                ret = await por_nome(aba, rs, nome)
            else:
                alvo = next((e for e in snap["elements"]
                             if nome.lower() in (e.get("text") or "").lower()), None)
                if not alvo:
                    linhas.append((modo, cena, "SEM REF", "—", ""))
                    falhas += 1
                    continue
                ret = await por_ref(aba, rs, alvo["ref"])
            await asyncio.sleep(0.05)
            conta = await aba.ev("JSON.stringify(window.contagem || {})")
            conta = json.loads(conta or "{}")
            ok = conta == {chave: 1}
            falhas += 0 if ok else 1
            linhas.append((modo, cena, "ok" if ok else f"FALHOU {conta}", ret.get("via", "—"),
                           (ret.get("alvo") or ret.get("error") or "")[:46]))
    for modo, cena, res, via, alvo in linhas:
        print(f"  {modo:<5} {cena:<16} {res:<26} {via:<6} {alvo}")

    # O controle que SAI da página no clique (job f9370b43): o resultado tem de
    # dizer que a página reagiu, e não "não ficou selecionado".
    for modo in ("nome", "ref"):
        await aba.cmd("Page.navigate", {"url": url})
        await asyncio.sleep(0.25)
        snap = await aba.ev(rs.js("js_snapshot", max=200, chars=3000))
        if modo == "nome":
            ret = await por_nome(aba, rs, "Discordo totalmente")
        else:
            alvo = next((e for e in snap["elements"]
                         if "Discordo totalmente" in (e.get("text") or "")), None)
            ret = await por_ref(aba, rs, alvo["ref"]) if alvo else {"error": "sem ref"}
        ok = (ret or {}).get("saiu") is True and (ret or {}).get("checked") is not False
        falhas += 0 if ok else 1
        print(f"  saiu no clique ({modo:<4})            {'ok' if ok else 'FALHOU'}  "
              f"saiu={(ret or {}).get('saiu')} checked={(ret or {}).get('checked')}")

    # O que NÃO pode acontecer.
    await aba.cmd("Page.navigate", {"url": url})
    await asyncio.sleep(0.25)
    await aba.ev(rs.js("js_snapshot", max=200, chars=3000))
    extras = [
        ("só existe fechado", "Excluir rascunho", "so-escondido"),
        ("desabilitado", "Avançar etapa", "desabilitado"),
        ("nome que não existe", "texto que não existe aqui", "nada"),
    ]
    for rotulo, nome, chave in extras:
        ret = await por_nome(aba, rs, nome)
        conta = json.loads(await aba.ev("JSON.stringify(window.contagem || {})") or "{}")
        ok = chave not in conta and not (ret or {}).get("via")
        falhas += 0 if ok else 1
        print(f"  recusa {rotulo:<22} {'ok' if ok else 'FALHOU'}  {str((ret or {}).get('error'))[:70]}")
    if rs.novo:
        # A trava confere o alvo REAL: o rosto se chama de um jeito, o controle de outro.
        trava = json.dumps({"trava": r"\b(confirmar|comprar)\b", "liberado": ""})
        ret = await por_nome(aba, rs, "Confirmar presença", trava=trava)
        conta = json.loads(await aba.ev("JSON.stringify(window.contagem || {})") or "{}")
        ok = "so-value" not in conta and str((ret or {}).get("error", "")).startswith("SENSIVEL:")
        falhas += 0 if ok else 1
        print(f"  trava  alvo real                 {'ok' if ok else 'FALHOU'}  {str((ret or {}).get('error'))[:70]}")
        # iframe: fora do `NEO.refs`, só a varredura alcança.
        ret = await por_nome(aba, rs, "Pagar boleto dentro do quadro")
        conta = json.loads(await aba.ev("JSON.stringify(window.contagem || {})") or "{}")
        ok = conta.get("iframe") == 1
        falhas += 0 if ok else 1
        print(f"  iframe mesma origem              {'ok' if ok else 'FALHOU ' + str(conta)}  via={(ret or {}).get('via')}")
        # select atrás de um desenho
        await aba.cmd("Page.navigate", {"url": url})
        await asyncio.sleep(0.25)
        snap = await aba.ev(rs.js("js_snapshot", max=200, chars=3000))
        alvo = next((e for e in snap["elements"] if "select" in (e.get("controla") or "")), None)
        if alvo:
            ret = await aba.ev(rs.js("js_select", reference=alvo["ref"], option='"3"'))
        else:
            ret = {"error": "sem ref com controla=select"}
        ok = bool(ret and ret.get("ok"))
        falhas += 0 if ok else 1
        print(f"  select pelo rosto                {'ok' if ok else 'FALHOU'}  {ret}")
    if rs.novo:
        # As duas rotas da reserva: é aqui que um clique dobrado nasceria.
        await aba.cmd("Page.navigate", {"url": url})
        await asyncio.sleep(0.25)
        await aba.ev("Object.defineProperty(document, 'visibilityState', {value: 'hidden'})")
        ret = await por_nome(aba, rs, "Salvar nota")
        conta = json.loads(await aba.ev("JSON.stringify(window.contagem || {})") or "{}")
        ok = conta == {"nativo": 1} and (ret or {}).get("via") == "js"
        falhas += 0 if ok else 1
        print(f"  reserva aba em segundo plano     {'ok' if ok else 'FALHOU ' + str(conta)}  via={(ret or {}).get('via')}")
        await aba.cmd("Page.navigate", {"url": url})
        await asyncio.sleep(0.25)
        alvo = await aba.ev(rs.js("js_click_text", needle=json.dumps("Salvar nota"), n=0, trava="{}"))
        r = await aba.ev(rs.const("JS_CLIQUE_CONFIRMA"))
        ret = await aba.ev(rs.const("JS_CLIQUE_DESPACHA")) if r.get("entregue") is False else r
        conta = json.loads(await aba.ev("JSON.stringify(window.contagem || {})") or "{}")
        ok = conta == {"nativo": 1} and (ret or {}).get("via") == "js"
        falhas += 0 if ok else 1
        print(f"  reserva mouse que não chegou     {'ok' if ok else 'FALHOU ' + str(conta)}  via={(ret or {}).get('via')}")
    print(f"\n{'NENHUMA falha' if not falhas else f'{falhas} falha(s)'}")
    return falhas


async def varrer(aba: Aba, rs: Rs, url: str, nome: str) -> None:
    await aba.cmd("Page.navigate", {"url": url})
    await asyncio.sleep(5)
    t0 = time.monotonic()
    snap = await aba.ev(rs.js("js_snapshot", max=200, chars=3000))
    t1 = time.monotonic()
    r = await aba.ev(rs.js("js_find", texto=json.dumps(nome), limite=8))
    t2 = time.monotonic()
    print(f"snapshot {len((snap or {}).get('elements', []))} elementos em {(t1 - t0) * 1000:.0f} ms · "
          f"varredura em {(t2 - t1) * 1000:.0f} ms · {(r or {}).get('total')} candidatos "
          f"· {(r or {}).get('frames_fechados')} iframes fechados")
    for c in (r or {}).get("lista", []):
        print("  ", json.dumps(c, ensure_ascii=False)[:190])


async def principal(a: argparse.Namespace) -> int:
    rs = Rs(a.rs)
    perfil = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [chrome(), f"--remote-debugging-port={PORTA}", "--headless=new", "--disable-gpu",
         "--window-size=1600,1000", f"--user-data-dir={perfil}", f"--user-agent={UA}",
         "--lang=pt-BR", "--allow-file-access-from-files", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        alvo = None
        for _ in range(80):
            try:
                alvos = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORTA}/json").read())
                alvo = next((x for x in alvos if x.get("type") == "page"), None)
                if alvo:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        async with websockets.connect(alvo["webSocketDebuggerUrl"], max_size=80_000_000) as ws:
            aba = Aba(ws)
            await aba.cmd("Page.enable")
            if a.url:
                await varrer(aba, rs, a.url, a.nome)
                return 0
            return await corpus(aba, rs)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(perfil, ignore_errors=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rs", default=RS_PADRAO)
    p.add_argument("--url")
    p.add_argument("nome", nargs="?", default="Adicionar ao carrinho")
    raise SystemExit(asyncio.run(principal(p.parse_args())))
