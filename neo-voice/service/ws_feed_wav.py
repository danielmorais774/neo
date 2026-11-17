"""
Simula um cliente de microfone: faz streaming de um WAV pro neo_stt_server
(WS /ws/audio) em chunks de 20ms PCM int16 16kHz, acrescenta silencio no fim
para o VAD fechar a fala, e imprime o que o servidor devolver (FINAL_TEXT...).

  python ws_feed_wav.py ../../neo-transcribe-voice/chunk_12.wav
  python ws_feed_wav.py <wav> [ws://host:5002/ws/audio]
"""
import sys
import asyncio
import numpy as np
import soundfile as sf
import websockets

SR = 16000
CHUNK_MS = 20
CHUNK = SR * CHUNK_MS // 1000


def load_pcm16(path):
    data, sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SR:
        # reamostra linear simples (suficiente para o teste)
        n = int(len(data) * SR / sr)
        data = np.interp(np.linspace(0, len(data), n, endpoint=False),
                         np.arange(len(data)), data).astype("float32")
    return (np.clip(data, -1, 1) * 32767).astype("<i2")


async def main():
    path = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else "ws://localhost:5002/ws/audio"
    pcm = load_pcm16(path)
    pcm = np.concatenate([pcm, np.zeros(int(SR * 1.5), dtype="<i2")])  # 1.5s silencio
    print(f"streaming {len(pcm)/SR:.1f}s -> {url}")
    async with websockets.connect(url, max_size=None) as ws:
        async def sender():
            for i in range(0, len(pcm), CHUNK):
                await ws.send(pcm[i:i + CHUNK].tobytes())
                await asyncio.sleep(CHUNK_MS / 1000)  # tempo real
            await ws.send("flush")
        send_task = asyncio.create_task(sender())
        recv_timeout = len(pcm) / SR + 15  # cobre o streaming em tempo real + finalize
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                print("<<", msg)
                if isinstance(msg, str) and msg.startswith("FINAL_TEXT:"):
                    break
        except asyncio.TimeoutError:
            print("timeout sem resposta")
        finally:
            send_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
