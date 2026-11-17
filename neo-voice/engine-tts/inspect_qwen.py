import inspect, os, re, glob
import qwen_tts
from qwen_tts import Qwen3TTSModel

pkg = os.path.dirname(qwen_tts.__file__)
print("pkg:", pkg)

for meth in ("generate_custom_voice", "generate", "_generate", "infer"):
    fn = getattr(Qwen3TTSModel, meth, None)
    if fn is None:
        continue
    try:
        print(f"\n=== {meth} signature: {inspect.signature(fn)}")
    except Exception as e:
        print(meth, "sig err", e)

# procurar defaults de geracao no source
keys = ("cfg", "guidance", "do_sample", "use_cache", "max_new_tokens", "max_length",
        "temperature", "top_k", "top_p", "num_beams", "repetition", "scale")
print("\n=== ocorrencias relevantes ===")
for f in glob.glob(os.path.join(pkg, "**", "*.py"), recursive=True):
    try:
        lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        continue
    for i, ln in enumerate(lines, 1):
        low = ln.lower()
        if any(k in low for k in keys) and ("=" in ln or "def " in ln):
            print(f"{os.path.basename(f)}:{i}: {ln.strip()[:130]}")
