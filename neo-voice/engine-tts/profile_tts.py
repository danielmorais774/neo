"""Diagnostico de velocidade do Qwen3-TTS: util de GPU + backend de atencao."""
import time, threading, subprocess
import numpy as np
import torch

MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
print("device cap (sm):", torch.cuda.get_device_capability())
print("flash sdp enabled:", torch.backends.cuda.flash_sdp_enabled())
print("mem_efficient sdp:", torch.backends.cuda.mem_efficient_sdp_enabled())
print("math sdp:", torch.backends.cuda.math_sdp_enabled())
print("cudnn sdp:", torch.backends.cuda.cudnn_sdp_enabled())

from qwen_tts import Qwen3TTSModel
t0 = time.time()
model = Qwen3TTSModel.from_pretrained(MODEL, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
print(f"load {time.time()-t0:.1f}s")

text = "Este é um teste de velocidade da síntese de voz rodando no PC gamer."

def synth():
    return model.generate_custom_voice(text=text, language="Portuguese", speaker="Ryan")

# warmup
synth()

stop = False
util = []
def mon():
    while not stop:
        try:
            o = subprocess.run(["nvidia-smi","--query-gpu=utilization.gpu,power.draw,clocks.sm,memory.used",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2).stdout.strip()
            util.append(o)
        except Exception:
            pass
        time.sleep(0.15)
th = threading.Thread(target=mon); th.start()
t0 = time.time()
wavs, sr = synth()
dt = time.time() - t0
stop = True; th.join()

a = wavs[0]
if hasattr(a, "detach"): a = a.detach().cpu().numpy()
a = np.asarray(a).reshape(-1)
dur = len(a)/sr
toks = dur*12  # 12Hz
print(f"\naudio {dur:.2f}s | gen {dt:.2f}s | rtf {dt/dur:.2f} | ~{toks:.0f} tokens -> {toks/dt:.1f} tok/s")
print("GPU util/power/smclk/mem durante a sintese:")
step = max(1, len(util)//12)
for s in util[::step]:
    print("  ", s)
