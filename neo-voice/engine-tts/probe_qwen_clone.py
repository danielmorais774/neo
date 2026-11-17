"""Isola o clone do Qwen Base: carrega so o Base e testa create_voice_clone_prompt + synth."""
import os, sys, time
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
import numpy as np, torch, soundfile as sf

# patch torchaudio -> soundfile
try:
    import torchaudio as _ta
    def _l(uri,*a,**k):
        d,sr=sf.read(str(uri),dtype="float32",always_2d=True); return torch.from_numpy(d.T).contiguous(),int(sr)
    _ta.load=_l
except Exception: pass

from qwen_tts import Qwen3TTSModel
attn = sys.argv[2] if len(sys.argv) > 2 else "sdpa"
dmap = os.environ.get("DMAP", "cuda")   # "cuda" (string) vs device_map
t0=time.time()
if dmap == "maxmem":
    m = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-0.6B-Base", device_map="auto",
                                      max_memory={0: "13GiB", "cpu": "24GiB"},
                                      dtype=torch.bfloat16, attn_implementation=attn)
elif dmap == "device_map":
    m = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-0.6B-Base", device_map="cuda:0",
                                      dtype=torch.bfloat16, attn_implementation=attn)
else:
    m = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-0.6B-Base", dtype=torch.bfloat16,
                                      attn_implementation=attn)
    # move o modelo interno p/ GPU sem o accelerate (evita o OOM falso do WSL)
    for attr in ("model",):
        sub = getattr(m, attr, None)
        if sub is not None and hasattr(sub, "to"):
            try: setattr(m, attr, sub.to("cuda")); print(f"moveu m.{attr} -> cuda")
            except Exception as e: print(f"m.{attr}.to(cuda) falhou:", repr(e)[:120])
    try:
        p = next(m.model.parameters())
        print("device do m.model:", p.device, p.dtype)
    except Exception as e: print("device check:", e)
print(f"base loaded ({dmap}) em {time.time()-t0:.1f}s")

ref = sys.argv[1] if len(sys.argv) > 1 else "/opt/neo-tts/ref.wav"
audio, sr = sf.read(ref, dtype="float32")
if getattr(audio, "ndim", 1) > 1: audio = audio.mean(axis=1)
prompt = m.create_voice_clone_prompt(ref_audio=(np.asarray(audio, dtype=np.float32), int(sr)), x_vector_only_mode=True)
print("prompt OK")
t0=time.time()
wavs, sr2 = m.generate_voice_clone(text="Olá, isso é um teste de clonagem de voz.", language="Portuguese", voice_clone_prompt=prompt)
a = wavs[0]
if hasattr(a, "detach"): a = a.detach().cpu().numpy()
a = np.asarray(a).reshape(-1)
sf.write("/opt/neo-tts/qclone_probe.wav", a, int(sr2))
print(f"synth OK {len(a)/sr2:.1f}s em {time.time()-t0:.1f}s")
