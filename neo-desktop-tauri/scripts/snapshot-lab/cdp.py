"""Cliente CDP mínimo: abre uma URL no headless, roda o snapshot e devolve o JSON."""
import asyncio, json, subprocess, sys, time, urllib.request, os, shutil, tempfile
import websockets

CHROME = os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium_headless_shell-1234/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell")

async def rodar(url, js, espera=4.0, porta=9333):
    perfil = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [CHROME, f"--remote-debugging-port={porta}", "--headless", "--disable-gpu",
         "--no-sandbox", "--window-size=1600,1000", f"--user-data-dir={perfil}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        alvo = None
        for _ in range(60):
            try:
                alvo = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{porta}/json").read())
                if alvo: break
            except Exception: pass
            time.sleep(0.25)
        ws_url = alvo[0]["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=60_000_000) as ws:
            n = [0]
            async def cmd(metodo, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": metodo, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    if msg.get("id") == n[0]:
                        if "error" in msg: raise RuntimeError(msg["error"])
                        return msg.get("result", {})
            await cmd("Page.enable")
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(espera)
            t0 = time.monotonic()
            r = await cmd("Runtime.evaluate",
                          {"expression": js, "returnByValue": True, "awaitPromise": True})
            ms = (time.monotonic() - t0) * 1000
            if "exceptionDetails" in r:
                return {"ERRO": json.dumps(r["exceptionDetails"])[:1500]}, ms
            return r["result"].get("value"), ms
    finally:
        proc.terminate(); proc.wait(timeout=10)
        shutil.rmtree(perfil, ignore_errors=True)

if __name__ == "__main__":
    url, arquivo = sys.argv[1], sys.argv[2]
    js = open(arquivo).read()
    valor, ms = asyncio.run(rodar(url, js, float(sys.argv[3]) if len(sys.argv) > 3 else 4.0))
    print(json.dumps({"ms": round(ms, 1), "r": valor}, ensure_ascii=False))
