"""
Teste rapido ponta-a-ponta: grava do microfone do Mac e manda pro engine
(Parakeet v3 no PC gamer) para transcrever.

  python mic_to_engine.py            # grava ate 6s e transcreve
  python mic_to_engine.py 10         # grava 10s
  NEO_STT_ENGINE_URL=http://192.168.4.185:5005 python mic_to_engine.py

Requer: sounddevice, numpy, httpx
"""
import sys
import time
import numpy as np
import sounddevice as sd
from engine_client import EngineClient, DEFAULT_ENGINE_URL

SR = 16000


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    print(f"engine: {DEFAULT_ENGINE_URL}")
    c = EngineClient()
    print("health:", c.health())

    print(f"Gravando {seconds:.0f}s ... fale agora.")
    audio = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16")
    sd.wait()
    pcm = audio.tobytes()
    print(f"capturado {len(pcm)/2/SR:.1f}s, enviando ao engine...")
    t0 = time.time()
    res = c.transcribe_pcm(pcm, sample_rate=SR)
    print(f"== resposta em {(time.time()-t0)*1000:.0f}ms (rede+infer)")
    print("   text :", repr(res.get("text")))
    print("   infer:", res.get("infer_ms"), "ms  rtf:", res.get("rtf"), " gpu:", res.get("on_gpu"))


if __name__ == "__main__":
    main()
