import inspect, os, glob
import qwen_tts
from qwen_tts import Qwen3TTSModel

print("=== metodos publicos do Qwen3TTSModel ===")
for n in dir(Qwen3TTSModel):
    if n.startswith("generate") or "clone" in n.lower() or "voice" in n.lower():
        try:
            print(n, inspect.signature(getattr(Qwen3TTSModel, n)))
        except Exception:
            print(n)

pkg = os.path.dirname(qwen_tts.__file__)
print("\n=== ocorrencias de clone/ref no source ===")
keys = ("def generate", "clone", "ref_audio", "ref_wav", "ref_text", "reference", "prompt_audio",
        "spk_audio", "voice_clone", "Base", "tts_model_type")
for f in glob.glob(os.path.join(pkg, "**", "*.py"), recursive=True):
    try:
        lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        continue
    for i, ln in enumerate(lines, 1):
        low = ln.lower()
        if any(k.lower() in low for k in keys) and ("def " in ln or "=" in ln or "clone" in low or "ref_" in low):
            print(f"{os.path.basename(f)}:{i}: {ln.strip()[:130]}")
