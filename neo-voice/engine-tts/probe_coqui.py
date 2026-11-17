"""Valida o Coqui XTTS-v2 na GPU: baixa o modelo, lista vozes, sintetiza PT, mede rtf."""
import os, sys, time
os.environ.setdefault("COQUI_TOS_AGREED", "1")
import numpy as np
import torch
import soundfile as sf

print("torch", torch.__version__, "cuda", torch.cuda.is_available(),
      torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-")
import transformers
print("transformers", transformers.__version__)

from TTS.api import TTS
dev = "cuda" if torch.cuda.is_available() else "cpu"
t0 = time.time()
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev)
print(f"load {time.time()-t0:.1f}s")

try:
    voices = list(tts.synthesizer.tts_model.speaker_manager.speakers.keys())
except Exception:
    voices = list(getattr(tts, "speakers", []) or [])
print(f"voices: {len(voices)} -> {voices[:6]}")
sr = int(tts.synthesizer.output_sample_rate)
spk = sys.argv[2] if len(sys.argv) > 2 else (voices[0] if voices else None)
text = sys.argv[1] if len(sys.argv) > 1 else \
    "Olá! Eu sou o Neo, agora com a voz do Coqui rodando direto na sua placa de vídeo."

tts.tts(text="Olá.", speaker=spk, language="pt")  # warmup
t0 = time.time()
wav = tts.tts(text=text, speaker=spk, language="pt")
dt = time.time() - t0
a = np.asarray(wav, dtype=np.float32).reshape(-1)
dur = len(a) / sr
sf.write("/opt/neo-coqui/probe_out.wav", a, sr)
print(f"speaker={spk} | sr={sr} | audio {dur:.1f}s | gen {dt:.2f}s | rtf {dt/dur:.2f}")
