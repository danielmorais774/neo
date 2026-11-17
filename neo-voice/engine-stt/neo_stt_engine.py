"""
neo-stt engine -- servidor de transcricao Parakeet TDT 0.6B v3 (GPU).

Roda no PC gamer (RTX 5070 Ti). Carrega o modelo via onnx-asr no CUDA
Execution Provider e expoe:

    GET  /health              -> status, modelo, providers reais, GPU
    POST /transcribe          -> transcreve um audio (wav multipart OU PCM bruto)
    WS   /ws/transcribe       -> acumula chunks PCM e transcreve por "flush"

Entrada de audio:
  * multipart/form-data campo "file": qualquer wav/flac/ogg (onnx-asr le e reamostra)
  * application/octet-stream: PCM int16 mono; sample-rate no header X-Sample-Rate (default 16000)

Config por env:
  NEO_STT_MODEL   (default: nemo-parakeet-tdt-0.6b-v3)
  NEO_STT_QUANT   quantizacao onnx (ex: fp16, int8). Default: padrao do repo.
  NEO_STT_PROVIDERS  lista separada por virgula. Default: CUDA,CPU. Providers
                     indisponiveis no onnxruntime instalado sao descartados, entao
                     o mesmo arquivo serve a build -gpu (PC gamer) e a build CPU.
  NEO_STT_THREADS intra_op_num_threads do onnxruntime. 0/vazio = default do ORT.
                  So faz diferenca na CPU (na GPU o grafo roda no CUDA EP).
  NEO_STT_HOST    (default: 0.0.0.0)
  NEO_STT_PORT    (default: 5005)
  NEO_STT_APIKEY  se setado, exige header X-API-Key
"""
import os
import sys
import glob
import site
import time
import json
import asyncio
import logging
import tempfile
import threading

# Logs vao para stderr; quando rodado destacado (run_engine.cmd), o cmd redireciona
# stdout+stderr para engine.log. Nao usar FileHandler aqui para evitar conflito de
# duplo-writer no mesmo arquivo no Windows (PermissionError / sharing violation).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-stt-engine")


# ----------------------------------------------------------------------------
# DLLs CUDA (cuDNN/cuBLAS) das wheels nvidia-* precisam estar no DLL search path
# ANTES de importar o onnxruntime, senao o CUDA EP nao inicializa no Windows.
# ----------------------------------------------------------------------------
def _add_nvidia_dll_dirs():
    if os.name != "nt":
        return []
    added = []
    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    roots.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    for sp in dict.fromkeys(roots):
        for binpath in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            if os.path.isdir(binpath):
                try:
                    os.add_dll_directory(binpath)
                    added.append(os.path.normpath(binpath))
                except Exception as e:
                    log.warning("add_dll_directory falhou em %s: %s", binpath, e)
    # PATH e necessario para o onnxruntime resolver deps transitivas (cublasLt etc.)
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")
    return added


_DLL_DIRS = _add_nvidia_dll_dirs()
log.info("nvidia DLL dirs: %d", len(_DLL_DIRS))

import numpy as np
import onnxruntime as ort
import onnx_asr
import soundfile as sf
from fastapi import FastAPI, Request, UploadFile, File, WebSocket, HTTPException, Header
from fastapi.responses import JSONResponse
from websockets.exceptions import ConnectionClosed
import uvicorn


MODEL_NAME = os.environ.get("NEO_STT_MODEL", "nemo-parakeet-tdt-0.6b-v3")
QUANT = os.environ.get("NEO_STT_QUANT") or None
HOST = os.environ.get("NEO_STT_HOST", "0.0.0.0")
PORT = int(os.environ.get("NEO_STT_PORT", "5005"))
API_KEY = os.environ.get("NEO_STT_APIKEY") or None
THREADS = int(os.environ.get("NEO_STT_THREADS") or 0)
WANTED_PROVIDERS = [
    p.strip() for p in
    os.environ.get("NEO_STT_PROVIDERS", "CUDAExecutionProvider,CPUExecutionProvider").split(",")
    if p.strip()
]
TARGET_SR = 16000

app = FastAPI(title="neo-stt engine", version="1.0")

_model = None
_session_providers = []
_on_gpu = False
_infer_lock = threading.Lock()  # serializa inferencia na GPU


def _find_sessions(obj, seen=None, depth=0):
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
            out += _find_sessions(v, seen, depth + 1)
    return out


def _load_model():
    global _model, _session_providers, _on_gpu
    # A build CPU do onnxruntime levanta ValueError se pedirem CUDA; filtrar aqui
    # deixa o mesmo engine rodar no PC gamer e no LXC so-CPU sem branch por host.
    available = ort.get_available_providers()
    providers = [p for p in WANTED_PROVIDERS if p in available]
    if not providers:
        providers = ["CPUExecutionProvider"]
    dropped = [p for p in WANTED_PROVIDERS if p not in available]
    if dropped:
        log.info("providers indisponiveis nesta build, ignorados: %s", dropped)

    sess_options = None
    if THREADS > 0:
        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = THREADS
        sess_options.inter_op_num_threads = 1

    log.info("carregando modelo %s (quant=%s) providers=%s threads=%s",
             MODEL_NAME, QUANT, providers, THREADS or "auto")
    t0 = time.time()
    kwargs = {"providers": providers}
    if QUANT:
        kwargs["quantization"] = QUANT
    if sess_options is not None:
        kwargs["sess_options"] = sess_options
    _model = onnx_asr.load_model(MODEL_NAME, **kwargs)
    provs = set()
    for s in _find_sessions(_model):
        provs.update(s.get_providers())
    _session_providers = sorted(provs)
    _on_gpu = "CUDAExecutionProvider" in provs
    log.info("modelo carregado em %.1fs | providers reais=%s | GPU=%s",
             time.time() - t0, _session_providers, _on_gpu)
    if not _on_gpu and "CUDAExecutionProvider" in providers:
        log.warning("!! CUDA nao inicializou -- rodando em CPU. Verifique wheels nvidia/cuDNN.")


def _recognize_array(audio: np.ndarray, sr: int) -> str:
    """Transcreve um array float32 mono. Serializado por lock (GPU unica)."""
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    with _infer_lock:
        try:
            return _model.recognize(audio, sample_rate=sr)
        except TypeError:
            # API antiga sem sample_rate: assume ja em TARGET_SR
            return _model.recognize(audio)


def _recognize_file(path: str) -> str:
    with _infer_lock:
        # channel="mean": sem isto o onnx-asr levanta SupportedOnlyMonoAudioError
        # em qualquer wav estereo. Mesma media de canais do caminho PCM acima.
        return _model.recognize(path, channel="mean")


def _check_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


@app.on_event("startup")
def _startup():
    _load_model()
    # warmup
    try:
        warm = (np.random.randn(TARGET_SR).astype("float32")) * 0.001
        _recognize_array(warm, TARGET_SR)
        log.info("warmup ok")
    except Exception as e:
        log.warning("warmup falhou: %s", e)


@app.get("/health")
def health():
    gpu_name = None
    if _on_gpu:
        # so' paga o subprocess onde ha GPU: o /health e' pollado de 4 em 4s.
        try:
            import subprocess
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5)
            gpu_name = out.stdout.strip() or None
        except Exception:
            pass
    return {
        "status": "ok" if _model is not None else "loading",
        "model": MODEL_NAME,
        "quant": QUANT,
        "providers": _session_providers,
        "on_gpu": _on_gpu,
        "gpu": gpu_name,
        "threads": THREADS or None,
        "target_sr": TARGET_SR,
    }


@app.post("/transcribe")
async def transcribe(
    request: Request,
    file: UploadFile = File(None),
    x_api_key: str = Header(None),
    x_sample_rate: str = Header(None),
):
    _check_key(x_api_key)
    if _model is None:
        raise HTTPException(503, "modelo ainda carregando")

    t0 = time.time()
    if file is not None:
        # wav/flac/ogg -> arquivo temp -> onnx-asr le e reamostra
        suffix = os.path.splitext(file.filename or "")[1] or ".wav"
        data = await file.read()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            tmp.write(data)
            tmp.close()
            try:
                info = sf.info(tmp.name)
                dur = info.frames / info.samplerate
            except Exception:
                dur = 0.0
            text = await asyncio.to_thread(_recognize_file, tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
    else:
        # PCM int16 bruto
        raw = await request.body()
        if not raw:
            raise HTTPException(400, "corpo vazio: envie multipart 'file' ou PCM int16")
        sr = int(x_sample_rate) if x_sample_rate else TARGET_SR
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        dur = len(audio) / sr
        text = await asyncio.to_thread(_recognize_array, audio, sr)

    infer_ms = (time.time() - t0) * 1000.0
    return {
        "text": (text or "").strip(),
        "audio_seconds": round(dur, 3),
        "infer_ms": round(infer_ms, 1),
        "rtf": round((infer_ms / 1000.0) / dur, 4) if dur else None,
        "on_gpu": _on_gpu,
    }


@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()
    if API_KEY and ws.query_params.get("api_key") != API_KEY:
        await ws.close(code=4401)
        return
    sr = int(ws.query_params.get("sample_rate", TARGET_SR))
    buf = bytearray()
    log.info("ws conectado (sr=%d)", sr)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                buf.extend(msg["bytes"])
            elif msg.get("text") is not None:
                cmd = msg["text"].strip().lower()
                if cmd in ("flush", "final", "end"):
                    if not buf:
                        await ws.send_text(json.dumps({"type": "final", "text": ""}))
                        continue
                    audio = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
                    dur = len(audio) / sr
                    t0 = time.time()
                    text = await asyncio.to_thread(_recognize_array, audio, sr)
                    buf = bytearray()
                    await ws.send_text(json.dumps({
                        "type": "final",
                        "text": (text or "").strip(),
                        "audio_seconds": round(dur, 3),
                        "infer_ms": round((time.time() - t0) * 1000.0, 1),
                    }))
                elif cmd == "reset":
                    buf = bytearray()
                elif cmd == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
    except (ConnectionClosed, RuntimeError):
        pass
    except Exception as e:
        log.warning("ws erro: %s", e)
    finally:
        log.info("ws desconectado")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
