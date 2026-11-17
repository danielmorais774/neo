"""Testa o WS /ws/panel: faz streaming de um wav e imprime parciais + final (JSON)."""
import sys, json, asyncio, numpy as np, soundfile as sf, websockets

SR = 16000; CHUNK = SR * 100 // 1000  # 100ms


def load_pcm16(path):
    data, sr = sf.read(path, dtype="float32")
    if data.ndim > 1: data = data.mean(axis=1)
    if sr != SR:
        n = int(len(data) * SR / sr)
        data = np.interp(np.linspace(0, len(data), n, endpoint=False), np.arange(len(data)), data).astype("float32")
    return (np.clip(data, -1, 1) * 32767).astype("<i2")


async def main():
    path = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else "ws://localhost:5002/ws/panel"
    pcm = np.concatenate([load_pcm16(path), np.zeros(int(SR * 1.2), "<i2")])
    print(f"streaming {len(pcm)/SR:.1f}s -> {url}")
    async with websockets.connect(url, max_size=None) as ws:
        async def sender():
            for i in range(0, len(pcm), CHUNK):
                await ws.send(pcm[i:i+CHUNK].tobytes()); await asyncio.sleep(0.1)
            await ws.send("flush")
        t = asyncio.create_task(sender())
        try:
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=len(pcm)/SR+15))
                if m["type"] == "partial": print(f"  parcial: {m['text']}")
                elif m["type"] == "final": print(f"FINAL ({m.get('infer_ms')}ms): {m['text']}"); break
                elif m["type"] == "response": print(f"  neo: {m['text']}")
        except asyncio.TimeoutError:
            print("timeout")
        finally:
            t.cancel()


asyncio.run(main())
