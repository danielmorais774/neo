"""Profiler do Qwen3-TTS no WSL: mede rtf/tok-s/util e compara attn/ compile.

  COMPILE=1 ATTN=sdpa python profile_wsl.py
"""
import os, time, threading, subprocess
import numpy as np
import torch

MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
ATTNS = [a for a in os.environ.get("ATTN", "flash_attention_2,sdpa,eager").split(",") if a]
TEXT = "Este é um teste de velocidade da síntese de voz rodando agora dentro do Linux."

print("cap:", torch.cuda.get_device_capability(), "| torch", torch.__version__)
from qwen_tts import Qwen3TTSModel
model = None
for a in ATTNS:
    try:
        t0 = time.time()
        model = Qwen3TTSModel.from_pretrained(MODEL, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation=a)
        print(f"load ok attn={a} em {time.time()-t0:.1f}s"); used_attn = a; break
    except Exception as e:
        print(f"attn={a} falhou: {repr(e)[:130]}")
if model is None:
    raise SystemExit("nao carregou")

if os.environ.get("COMPILE") == "1":
    try:
        model.model = torch.compile(model.model, mode="reduce-overhead", fullgraph=False)
        print("torch.compile aplicado (reduce-overhead)")
    except Exception as e:
        print("compile falhou:", repr(e)[:130])

def synth():
    return model.generate_custom_voice(text=TEXT, language="Portuguese", speaker="Ryan")

# warmups (compile compila no 1o/2o)
for i in range(2 if os.environ.get("COMPILE") == "1" else 1):
    t0 = time.time(); synth(); print(f"warmup{i} {time.time()-t0:.2f}s")

stop = False; util = []
def mon():
    while not stop:
        try:
            o = subprocess.run(["nvidia-smi","--query-gpu=utilization.gpu,power.draw,clocks.sm",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout.strip()
            util.append(o)
        except Exception: pass
        time.sleep(0.15)
th = threading.Thread(target=mon); th.start()
t0 = time.time(); wavs, sr = synth(); dt = time.time() - t0
stop = True; th.join()

a = wavs[0]
if hasattr(a, "detach"): a = a.detach().cpu().numpy()
a = np.asarray(a).reshape(-1)
dur = len(a)/sr; toks = dur*12
print(f"\n== attn={used_attn} compile={os.environ.get('COMPILE','0')}")
print(f"audio {dur:.2f}s | gen {dt:.2f}s | rtf {dt/dur:.2f} | ~{toks:.0f} tok -> {toks/dt:.1f} tok/s")
if util:
    us = [float(x.split(',')[0]) for x in util if x]
    pw = [float(x.split(',')[1]) for x in util if x]
    print(f"GPU util medio {sum(us)/len(us):.0f}% (max {max(us):.0f}%) | power medio {sum(pw)/len(pw):.0f}W")
