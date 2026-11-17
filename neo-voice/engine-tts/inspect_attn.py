import os, glob
import qwen_tts
pkg = os.path.dirname(qwen_tts.__file__)
f = glob.glob(os.path.join(pkg, "**", "modeling_qwen3_tts.py"), recursive=True)[0]
print("file:", f)
lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
keys = ("flash_attn", "flash attn", "manual", "scaled_dot_product", "sdpa", "attn_implementation",
        "Will only run", "is_flash", "_attn_implementation", "eager", "def forward", "class ", "softmax",
        "matmul", "warning", "warn(")
print("== modeling_qwen3_tts.py linhas de interesse ==")
for i, ln in enumerate(lines, 1):
    low = ln.lower()
    if any(k.lower() in low for k in keys):
        print(f"{i}: {ln.rstrip()[:150]}")
