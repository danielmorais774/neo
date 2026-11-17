"""Testa o WS /ws/tts (streaming por frases): latencia do 1o som, total e GAPS.

Simula o playhead de um cliente real: o chunk N comeca a tocar assim que chega
(ou quando o anterior termina); se o chunk N+1 chegar DEPOIS do fim tocavel do
audio ja recebido, a diferenca e um gap audivel (silencio no meio da fala).

    python ws_tts_test.py [texto] [backend] [url]

Meta pos-rampa: gap_total = 0ms em textos longos, em qualquer backend.
"""
import sys, json, time, asyncio, numpy as np, soundfile as sf, websockets

DEFAULT_TEXT = (
    "Olá! Eu sou o Neo, seu assistente pessoal. "
    "Hoje o dia está bonito em Campinas, com sol e vento fraco pela manhã. "
    "A previsão para amanhã indica pancadas de chuva no fim da tarde, então talvez "
    "valha a pena levar um guarda-chuva quando você sair de casa. "
    "Sobre a sua agenda, você tem duas reuniões pela manhã e uma consulta às quinze horas. "
    "Além disso, o backup semanal do servidor terminou sem erros e todos os serviços "
    "estão operacionais. "
    "Quer que eu leia as notícias do dia agora, ou prefere ouvir uma música primeiro?")


async def main():
    text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEXT
    backend = sys.argv[2] if len(sys.argv) > 2 else "qwen"
    url = sys.argv[3] if len(sys.argv) > 3 else "ws://localhost:5002/ws/tts"
    chunks = []
    sr = 24000
    t0 = time.time()
    first = None
    playhead_end = None  # instante (relativo a t0) em que o audio recebido acaba
    gap_total = 0.0
    print(f"backend={backend} url={url}")
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"text": text, "backend": backend}))
        hdr = None
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=90)
            if isinstance(msg, str):
                m = json.loads(msg)
                if m["type"] == "start":
                    print(f"start: {m['sentences']} frase(s), sr={m['sr']}")
                elif m["type"] == "chunk":
                    hdr = m
                elif m["type"] == "done":
                    print("done"); break
                elif m["type"] == "error":
                    print("ERRO:", m["error"]); return
            else:
                now = time.time() - t0
                if first is None:
                    first = now
                a = np.frombuffer(msg, dtype="<i2")
                sr = (hdr or {}).get("sr", sr)
                dur = len(a) / sr
                if playhead_end is None:
                    gap = 0.0
                    playhead_end = now + dur
                else:
                    gap = max(0.0, now - playhead_end)
                    playhead_end = max(now, playhead_end) + dur
                gap_total += gap
                idx = (hdr or {}).get("idx", len(chunks))
                src = (hdr or {}).get("source", "?")
                txt = ((hdr or {}).get("text") or "")[:40]
                flag = f"  << GAP {gap*1000:.0f}ms" if gap > 0.02 else ""
                print(f"  chunk {idx} [{src}] {len(txt) and txt!r} dur={dur:.2f}s "
                      f"chegou@{now:.2f}s infer={(hdr or {}).get('infer_ms')}ms{flag}")
                chunks.append(a)
    total = time.time() - t0
    audio = np.concatenate(chunks) if chunks else np.zeros(0, "<i2")
    dur = len(audio) / sr
    sf.write("/tmp/tts_stream_out.wav", audio, sr)
    print(f"== {dur:.1f}s de audio | 1o som {first*1000:.0f}ms | total {total*1000:.0f}ms "
          f"| rtf_total {total/max(dur,0.01):.2f} | GAP TOTAL {gap_total*1000:.0f}ms")
    print("   salvo /tmp/tts_stream_out.wav")

asyncio.run(main())
