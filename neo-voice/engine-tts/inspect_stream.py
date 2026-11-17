import inspect, os, glob
import qwen_tts
from qwen_tts import Qwen3TTSModel

pkg = os.path.dirname(qwen_tts.__file__)
print("pkg:", pkg)

# fonte do generate_custom_voice
try:
    print("\n===== generate_custom_voice =====")
    print(inspect.getsource(Qwen3TTSModel.generate_custom_voice))
except Exception as e:
    print("err:", e)

# procurar mecanismo de streaming
print("\n===== ocorrencias streaming =====")
for f in glob.glob(os.path.join(pkg, "**", "*.py"), recursive=True):
    try:
        lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        continue
    for i, ln in enumerate(lines, 1):
        low = ln.lower()
        if any(k in low for k in ("yield", "streamer", "non_streaming", "queue", "callback", "def generate", "audio_chunk", "chunk")):
            print(f"{os.path.basename(f)}:{i}: {ln.strip()[:140]}")
