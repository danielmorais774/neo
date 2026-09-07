import asyncio, json, os, re, sys, subprocess, tempfile, time, shutil, urllib.request
import websockets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RS = '/Users/daniel/Documents/projects/neo/neo-desktop-tauri/src-tauri/src/services/browser_local.rs'
s = open(RS).read()
HELPERS = re.search(r'const JS_HELPERS: &str = r#"(.*?)"#;', s, re.S).group(1)

def js(nome, **kw):
    i = s.index(f"fn {nome}("); j = s.index('r#"', i); k = s.index('"#', j)
    t = s[j+3:k].replace("{{","\x00").replace("}}","\x01").replace("{helpers}", HELPERS)
    for a, b in kw.items(): t = t.replace("{"+a+"}", str(b))
    return t.replace("\x00","{").replace("\x01","}")

CHROME = os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-1234/"
                            "chrome-headless-shell-mac-arm64/chrome-headless-shell")

async def main(url):
    perfil = tempfile.mkdtemp()
    proc = subprocess.Popen([CHROME, "--remote-debugging-port=9444", "--headless", "--disable-gpu",
        "--no-sandbox", "--window-size=1600,1000", f"--user-data-dir={perfil}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        alvo=None
        for _ in range(60):
            try:
                alvo=json.loads(urllib.request.urlopen("http://127.0.0.1:9444/json").read())
                if alvo: break
            except Exception: pass
            time.sleep(0.25)
        async with websockets.connect(alvo[0]["webSocketDebuggerUrl"], max_size=60_000_000) as ws:
            n=[0]
            async def ev(expr):
                n[0]+=1
                await ws.send(json.dumps({"id":n[0],"method":"Runtime.evaluate",
                    "params":{"expression":expr,"returnByValue":True,"awaitPromise":True}}))
                while True:
                    m=json.loads(await asyncio.wait_for(ws.recv(),timeout=60))
                    if m.get("id")==n[0]:
                        r=m["result"]
                        if "exceptionDetails" in r: return {"ERRO": json.dumps(r["exceptionDetails"])[:600]}
                        return r["result"].get("value")
            async def cmd(met,par):
                n[0]+=1
                await ws.send(json.dumps({"id":n[0],"method":met,"params":par}))
                while True:
                    m=json.loads(await asyncio.wait_for(ws.recv(),timeout=60))
                    if m.get("id")==n[0]: return m
            await cmd("Page.enable",{}); await cmd("Page.navigate",{"url":url}); await asyncio.sleep(2.5)

            snap = await ev(js("js_snapshot", max=200, chars=3000))
            els = snap["elements"]
            alt3 = next((e for e in els if "racismo sistemático resultou" in (e.get("text") or "").lower()
                         or "racismo sistemático resultou" in (e.get("text") or "")), None)
            print(f"snapshot: {len(els)} elementos")
            cob = [e for e in els if e.get("coberto")]
            print(f"cobertos: {len(cob)}" + ("".join(f'\n   [{e["ref"]}] "{(e.get("text") or "")[:30]}" ← "{e["coberto"][:36]}"' for e in cob[:4])))
            if not alt3: print("!! não achei a alternativa 3"); return
            print(f'alternativa 3 = ref {alt3["ref"]} · checked={alt3.get("checked")} · controla={alt3.get("controla")}')

            r = await ev(js("js_click", reference=alt3["ref"]))
            print("click →", json.dumps(r, ensure_ascii=False)[:180])

            snap2 = await ev(js("js_snapshot", max=200, chars=3000))
            e2 = next((e for e in snap2["elements"] if e.get("ref")==alt3["ref"]), None)
            resp = next((e for e in snap2["elements"] if (e.get("text") or "")=="Responder"), None)
            print(f'depois do clique: alternativa checked={e2 and e2.get("checked")} · Responder desabilitado={resp and resp.get("disabled")}')

            r = await ev(js("js_click_text", needle=json.dumps("Durante a transição para o trabalho livre")))
            print("click_text →", json.dumps(r, ensure_ascii=False)[:220])
            r = await ev(js("js_click_text", needle=json.dumps("texto que não existe nesta página")))
            print("click_text (inexistente) →", json.dumps(r, ensure_ascii=False)[:160])
    finally:
        proc.terminate(); proc.wait(timeout=10); shutil.rmtree(perfil, ignore_errors=True)

asyncio.run(main(sys.argv[1]))
