"""Valida o Nari Dia 1.6B na GPU: carrega, sintetiza (ingles) e mede rtf."""
import os, sys, time
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
import numpy as np
import torch
import soundfile as sf

print("torch", torch.__version__, "cuda", torch.cuda.is_available(),
      torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-")
from dia.model import Dia

t0 = time.time()
model = Dia.from_pretrained("nari-labs/Dia-1.6B", compute_dtype="float16")
print(f"load {time.time()-t0:.1f}s")

text = sys.argv[1] if len(sys.argv) > 1 else \
    "[S1] Hello! I am Neo, running on your RTX through the Nari Dia model. (laughs)"
torch.manual_seed(42)
model.generate("[S1] Hi.", use_torch_compile=False, verbose=False)  # warmup
torch.manual_seed(42)
t0 = time.time()
wav = model.generate(text, use_torch_compile=False, verbose=False)
dt = time.time() - t0
a = np.asarray(wav, dtype=np.float32).reshape(-1)
sr = 44100
dur = len(a) / sr
sf.write("/opt/neo-dia/probe_out.wav", a, sr)
print(f"audio {dur:.1f}s | gen {dt:.2f}s | rtf {dt/dur:.2f} | sr={sr}")
