"""Verifica qual _attn_implementation esta ativo em cada submodelo do Qwen3-TTS."""
import torch
from qwen_tts import Qwen3TTSModel

MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
m = Qwen3TTSModel.from_pretrained(MODEL, device_map="cuda:0", dtype=torch.bfloat16,
                                  attn_implementation="sdpa")

def attn_of(obj, name):
    cfg = getattr(obj, "config", None)
    impl = getattr(cfg, "_attn_implementation", None) if cfg is not None else None
    print(f"{name}: _attn_implementation = {impl!r}  (type={type(obj).__name__})")

print("== top:")
attn_of(m, "Qwen3TTSModel")
inner = getattr(m, "model", m)
attn_of(inner, "m.model")
# percorre submodulos nomeados procurando configs com _attn_implementation
seen = set()
for n, sub in inner.named_modules():
    cfg = getattr(sub, "config", None)
    if cfg is not None and hasattr(cfg, "_attn_implementation"):
        key = (n.split('.')[0] if n else 'root')
        if key in seen:
            continue
        seen.add(key)
        print(f"  {n or 'root'}: {cfg._attn_implementation!r} ({type(sub).__name__})")
