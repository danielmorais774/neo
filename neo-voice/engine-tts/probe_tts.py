"""
Valida o Qwen3-TTS 0.6B CustomVoice na GPU (RTX 5070 Ti / Blackwell).
Carrega o modelo (sdpa, sem flash-attn), sintetiza uma frase em PT e mede latencia.

  python probe_tts.py ["texto"] [speaker]
"""
import sys
import time
import numpy as np
import torch
import soundfile as sf

MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"


def main():
    print("torch:", torch.__version__, "| cuda:", torch.cuda.is_available(),
          "|", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU")

    from qwen_tts import Qwen3TTSModel
    dev = "cuda:0" if torch.cuda.is_available() else "cpu"
    model = None
    for attn in ("sdpa", "eager"):
        try:
            t0 = time.time()
            model = Qwen3TTSModel.from_pretrained(
                MODEL, device_map=dev, dtype=torch.bfloat16, attn_implementation=attn)
            print(f"modelo carregado (attn={attn}) em {time.time()-t0:.1f}s")
            break
        except Exception as e:
            print(f"attn={attn} falhou: {repr(e)[:160]}")
    if model is None:
        raise SystemExit("nao foi possivel carregar o modelo")

    text = sys.argv[1] if len(sys.argv) > 1 else \
        "Olá! Eu sou o Neo, sua assistência de voz rodando direto na sua placa de vídeo."
    speaker = sys.argv[2] if len(sys.argv) > 2 else "Ryan"

    for i in range(2):  # 1a = warmup
        t0 = time.time()
        wavs, sr = model.generate_custom_voice(text=text, language="Portuguese", speaker=speaker)
        dt = time.time() - t0
        a = wavs[0]
        if hasattr(a, "detach"):
            a = a.detach().cpu().numpy()
        a = np.asarray(a, dtype=np.float32).reshape(-1)
        dur = len(a) / sr
        tag = "warmup" if i == 0 else "medido"
        print(f"[{tag}] {dur:.1f}s de audio em {dt:.2f}s (rtf={dt/dur:.3f}) sr={sr}")
    sf.write("probe_out.wav", a, sr)
    print(f"salvo probe_out.wav ({dur:.1f}s, speaker={speaker})")


if __name__ == "__main__":
    main()
