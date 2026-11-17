"""
neo-voice Nari Dia engine -- Dia 1.6B (GPU). Mesma API dos outros engines.
Dia gera dialogo ultra-realista (tags [S1]/[S2], sons nao-verbais). SO INGLES.

    GET  /health   -> status, modelo, GPU
    GET  /voices   -> voz unica ("Padrão" — Dia nao tem speakers nomeados)
    POST /tts      -> {text, voice, language, instruct} -> audio/wav (44.1kHz)

Config por env:
  NEO_DIA_MODEL  (default nari-labs/Dia-1.6B)
  NEO_DIA_DTYPE  (default float16)
  NEO_DIA_SEED   (default 42 — voz mais consistente entre chamadas)
  NEO_DIA_HOST   (default 0.0.0.0)
  NEO_DIA_PORT   (default 5008)
  NEO_DIA_APIKEY se setado, exige header X-API-Key
"""
import os
import io
import time
import asyncio
import logging
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-dia-engine")

import numpy as np
import torch
import soundfile as sf
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import Response
from pydantic import BaseModel
import uvicorn

MODEL_NAME = os.environ.get("NEO_DIA_MODEL", "nari-labs/Dia-1.6B")
DTYPE = os.environ.get("NEO_DIA_DTYPE", "float16")
SEED = int(os.environ.get("NEO_DIA_SEED", "42"))
HOST = os.environ.get("NEO_DIA_HOST", "0.0.0.0")
PORT = int(os.environ.get("NEO_DIA_PORT", "5008"))
API_KEY = os.environ.get("NEO_DIA_APIKEY") or None
SR = 44100  # Dia/DAC emite a 44.1kHz

app = FastAPI(title="neo-voice Nari Dia engine", version="1.0")
_model = None
_gpu = None
_lock = threading.Lock()


def _load_model():
    global _model, _gpu
    from dia.model import Dia
    _gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    log.info("carregando %s (dtype=%s, gpu=%s)", MODEL_NAME, DTYPE, _gpu)
    t0 = time.time()
    _model = Dia.from_pretrained(MODEL_NAME, compute_dtype=DTYPE)
    log.info("modelo carregado em %.1fs", time.time() - t0)


def _synth(text):
    t = text.strip()
    if not t.startswith("[S"):
        t = "[S1] " + t  # Dia espera tags de locutor
    with _lock:
        if SEED:
            torch.manual_seed(SEED)
        wav = _model.generate(t, use_torch_compile=False, verbose=False)
    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    return audio, SR


def _check_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")


class TTSReq(BaseModel):
    text: str
    voice: str | None = None
    language: str | None = None
    instruct: str | None = None


@app.on_event("startup")
def _startup():
    _load_model()
    try:
        _synth("Hello.")
        log.info("warmup ok")
    except Exception as e:
        log.warning("warmup falhou: %s", e)


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "loading",
        "model": "nari-dia-1.6b",
        "gpu": _gpu,
        "attn": "dia",
        "default_voice": "Padrão",
        "default_language": "en",
        "voices": ["Padrão"],
        "note": "Dia gera apenas em inglês; use tags [S1]/[S2] e (laughs) etc.",
    }


@app.get("/voices")
def voices():
    return {"voices": [{"name": "Padrão", "desc": "diálogo (só inglês)", "native": "en"}],
            "default": "Padrão", "language": "en"}


@app.post("/tts")
async def tts(req: TTSReq, x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _model is None:
        raise HTTPException(503, "modelo ainda carregando")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "texto vazio")
    t0 = time.time()
    audio, sr = await asyncio.to_thread(_synth, text)
    infer_ms = (time.time() - t0) * 1000.0
    dur = len(audio) / sr if sr else 0.0
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    log.info("TTS '%s...' %.1fs em %.0fms", text[:40], dur, infer_ms)
    return Response(content=buf.read(), media_type="audio/wav", headers={
        "X-Audio-Seconds": f"{dur:.3f}", "X-Infer-Ms": f"{infer_ms:.1f}",
        "X-RTF": f"{(infer_ms/1000.0)/dur:.4f}" if dur else "0",
        "X-Sample-Rate": str(sr), "X-Voice": "Padrão",
    })


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
