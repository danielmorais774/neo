"""
Valida o engine Parakeet TDT 0.6B v3 na GPU (RTX 5070 Ti / Blackwell).

- Adiciona as DLLs das wheels nvidia-* ao DLL search path (Windows) antes de
  importar o onnxruntime, para o CUDA Execution Provider achar cuDNN/cuBLAS.
- Carrega o modelo via onnx-asr preferindo CUDA.
- Inspeciona quais Execution Providers as InferenceSessions realmente usam
  (sinal confiavel: se "CUDAExecutionProvider" aparecer, a GPU foi inicializada).
- Transcreve os WAVs passados como argumento e mede latencia/RTF.

Uso:
    python probe_gpu.py [audio1.wav audio2.wav ...]

Variaveis de ambiente:
    PARAKEET_QUANT   quantizacao do modelo onnx (ex: fp16, int8). Default: padrao do repo.
"""
import os
import sys
import glob
import site
import time


def add_nvidia_dll_dirs():
    """Adiciona .../site-packages/nvidia/*/bin ao DLL search path (Windows)."""
    if os.name != "nt":
        return []
    added = []
    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    # tambem o site-packages do venv atual
    roots.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    for sp in dict.fromkeys(roots):
        for binpath in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            if os.path.isdir(binpath):
                try:
                    os.add_dll_directory(binpath)
                    added.append(os.path.normpath(binpath))
                except Exception as e:
                    print("  [warn] add_dll_directory falhou:", binpath, e)
    # PATH e necessario para o onnxruntime resolver deps transitivas (cublasLt etc.)
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")
    return added


def find_sessions(obj, seen=None, depth=0):
    import onnxruntime as ort
    if seen is None:
        seen = set()
    if id(obj) in seen or depth > 4:
        return []
    seen.add(id(obj))
    if isinstance(obj, ort.InferenceSession):
        return [obj]
    out = []
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        for v in d.values():
            out += find_sessions(v, seen, depth + 1)
    return out


def main():
    added = add_nvidia_dll_dirs()
    print(f"== nvidia DLL dirs adicionados: {len(added)}")
    for a in added:
        print("   ", a)

    import onnxruntime as ort
    print("== onnxruntime:", ort.__version__)
    print("== providers disponiveis:", ort.get_available_providers())

    import onnx_asr

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    quant = os.environ.get("PARAKEET_QUANT") or None
    print(f"== carregando nemo-parakeet-tdt-0.6b-v3 (quant={quant}) ...")
    t0 = time.time()
    kwargs = {"providers": providers}
    if quant:
        kwargs["quantization"] = quant
    model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", **kwargs)
    print(f"== modelo carregado em {time.time()-t0:.1f}s")

    sessions = find_sessions(model)
    print(f"== InferenceSessions encontradas: {len(sessions)}")
    on_gpu = False
    for s in sessions:
        provs = s.get_providers()
        print("   providers:", provs)
        if "CUDAExecutionProvider" in provs:
            on_gpu = True
    print("== GPU (CUDA) ATIVA" if on_gpu else "== !! RODANDO EM CPU (CUDA nao inicializou)")

    wavs = [w for w in sys.argv[1:] if os.path.exists(w)]
    if not wavs:
        import numpy as np
        sr = 16000
        audio = (np.random.randn(sr * 3).astype("float32")) * 0.001
        for i in range(2):  # 2x: 1o inclui warmup
            t0 = time.time()
            txt = model.recognize(audio, sample_rate=sr)
            dt = time.time() - t0
            tag = "warmup" if i == 0 else "medido"
            print(f"[sintetico 3s/{tag}] -> '{txt}'  ({dt:.2f}s, rtf={dt/3:.3f})")
        return

    import soundfile as sf
    # warmup
    try:
        model.recognize(wavs[0])
    except Exception as e:
        print("warmup falhou:", e)
    for w in wavs:
        try:
            info = sf.info(w)
            dur = info.frames / info.samplerate
        except Exception:
            dur = 0.0
        t0 = time.time()
        txt = model.recognize(w)
        dt = time.time() - t0
        rtf = dt / dur if dur else float("nan")
        print(f"[{os.path.basename(w)} {dur:.1f}s] -> {txt!r}  ({dt:.2f}s, rtf={rtf:.3f})")


if __name__ == "__main__":
    main()
