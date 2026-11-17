"""
neo-voice TTS engine -- servidor de sintese Qwen3-TTS (GPU).

Roda no PC gamer (RTX 5070 Ti). Carrega o Qwen3-TTS 0.6B CustomVoice no torch/CUDA
(bf16, attn sdpa -- sem flash-attn no Windows) e expoe:

    GET  /health   -> status, modelo, GPU, vozes
    GET  /voices   -> lista de vozes (speakers) disponiveis
    POST /tts      -> {text, voice, language, instruct} -> audio/wav

Config por env:
  NEO_TTS_MODEL   (default Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice)
  NEO_TTS_VOICE   voz default (default Ryan)
  NEO_TTS_LANG    idioma default (default Portuguese)
  NEO_TTS_HOST    (default 0.0.0.0)
  NEO_TTS_PORT    (default 5006)
  NEO_TTS_APIKEY  se setado, exige header X-API-Key
"""
import os
import io
import time
import asyncio
import logging
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-tts-engine")

import json
import uuid

import numpy as np
import torch
import soundfile as sf
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
import uvicorn

# Mesma defesa do engine Coqui: evita torchaudio.load -> torchcodec (libs CUDA faltando no WSL).
try:
    import torchaudio as _ta

    def _ta_load_sf(uri, *a, **k):
        data, sr = sf.read(str(uri), dtype="float32", always_2d=True)
        return torch.from_numpy(data.T).contiguous(), int(sr)

    _ta.load = _ta_load_sf
except Exception:
    pass

MODEL_NAME = os.environ.get("NEO_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
BASE_MODEL = os.environ.get("NEO_TTS_BASE_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")  # p/ clonagem
DEFAULT_VOICE = os.environ.get("NEO_TTS_VOICE", "Ryan")
DEFAULT_LANG = os.environ.get("NEO_TTS_LANG", "Portuguese")
HOST = os.environ.get("NEO_TTS_HOST", "0.0.0.0")
PORT = int(os.environ.get("NEO_TTS_PORT", "5006"))
API_KEY = os.environ.get("NEO_TTS_APIKEY") or None
CLONES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clones_qwen")

# vozes do Qwen3-TTS-12Hz-CustomVoice (timbre; o idioma vem do parametro language)
VOICES = [
    {"name": "Ryan", "desc": "Masculino dinamico", "native": "Ingles"},
    {"name": "Aiden", "desc": "Masculino americano, animado", "native": "Ingles"},
    {"name": "Vivian", "desc": "Feminina jovem, brilhante", "native": "Chines"},
    {"name": "Serena", "desc": "Feminina jovem, calorosa", "native": "Chines"},
    {"name": "Uncle_Fu", "desc": "Masculino maduro, suave", "native": "Chines"},
    {"name": "Dylan", "desc": "Masculino jovem (Pequim)", "native": "Chines"},
    {"name": "Eric", "desc": "Masculino animado (Sichuan)", "native": "Chines"},
    {"name": "Ono_Anna", "desc": "Feminina brincalhona", "native": "Japones"},
    {"name": "Sohee", "desc": "Feminina calorosa", "native": "Coreano"},
]
VOICE_NAMES = {v["name"] for v in VOICES}

app = FastAPI(title="neo-voice TTS engine", version="1.0")
_model = None          # CustomVoice (vozes prontas)
_model_base = None     # Base (clonagem) -- carregado lazy no 1o clone
_clones = {}           # id -> {"name", "file", "prompt"} (prompt computado lazy)
_gpu = None
_attn = None
_lock = threading.Lock()  # serializa sintese na GPU


def _load_kwargs():
    """device_map=auto + max_memory forcado: poe tudo na GPU contornando o OOM falso do
    WSL (o accelerate le memoria bugada e recusa com device_map='cuda:0' direto)."""
    if torch.cuda.is_available():
        return {"device_map": "auto", "max_memory": {0: "13GiB", "cpu": "24GiB"}}
    return {"device_map": "cpu"}


def _load_model():
    global _model, _gpu, _attn
    from qwen_tts import Qwen3TTSModel
    _gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    log.info("carregando %s (gpu=%s)", MODEL_NAME, _gpu)
    last = None
    # flash_attention_2 e o mais rapido (Linux/WSL); no Windows ele falha e cai pra sdpa.
    attns = [a.strip() for a in os.environ.get("NEO_TTS_ATTN", "flash_attention_2,sdpa,eager").split(",") if a.strip()]
    for attn in attns:
        try:
            t0 = time.time()
            _model = Qwen3TTSModel.from_pretrained(
                MODEL_NAME, dtype=torch.bfloat16, attn_implementation=attn, **_load_kwargs())
            _attn = attn
            log.info("modelo carregado (attn=%s) em %.1fs", attn, time.time() - t0)
            return
        except Exception as e:
            last = e
            log.warning("attn=%s falhou: %s", attn, repr(e)[:160])
    raise RuntimeError(f"nao carregou o modelo: {last}")


def _to_audio(wavs):
    a = wavs[0]
    if hasattr(a, "detach"):
        a = a.detach().cpu().numpy()
    return np.asarray(a, dtype=np.float32).reshape(-1)


def _synth(text: str, voice: str, language: str, instruct: str | None):
    with _lock:
        if voice in _clones:
            prompt = _clone_prompt(voice)  # lazy: carrega Base + computa o prompt
            wavs, sr = _model_base.generate_voice_clone(
                text=text, language=language, voice_clone_prompt=prompt)
        else:
            kw = dict(text=text, language=language, speaker=voice)
            if instruct:
                kw["instruct"] = instruct
            wavs, sr = _model.generate_custom_voice(**kw)
    return _to_audio(wavs), int(sr)


def _ensure_base():
    """Carrega o modelo Base (clonagem) sob demanda."""
    global _model_base
    if _model_base is not None:
        return
    from qwen_tts import Qwen3TTSModel
    attn = _attn or "sdpa"
    log.info("carregando Base (clonagem) %s (attn=%s)...", BASE_MODEL, attn)
    t0 = time.time()
    _model_base = Qwen3TTSModel.from_pretrained(BASE_MODEL, dtype=torch.bfloat16,
                                                attn_implementation=attn, **_load_kwargs())
    log.info("Base carregado em %.1fs", time.time() - t0)


def _clone_prompt(cid):
    """Devolve (computando/cacheando) o voice_clone_prompt de uma voz clonada."""
    c = _clones[cid]
    if c.get("prompt") is None:
        _ensure_base()
        path = os.path.join(CLONES_DIR, c["file"])
        audio, sr = sf.read(path, dtype="float32", always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        c["prompt"] = _model_base.create_voice_clone_prompt(
            ref_audio=(np.asarray(audio, dtype=np.float32), int(sr)), x_vector_only_mode=True)
    return c["prompt"]


def _save_clones_meta():
    os.makedirs(CLONES_DIR, exist_ok=True)
    meta = {cid: {"name": c["name"], "file": c["file"]} for cid, c in _clones.items()}
    json.dump(meta, open(os.path.join(CLONES_DIR, "clones.json"), "w", encoding="utf-8"), ensure_ascii=False)


def _load_clones():
    meta_path = os.path.join(CLONES_DIR, "clones.json")
    if not os.path.exists(meta_path):
        return
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except Exception:
        return
    for cid, info in meta.items():
        if os.path.exists(os.path.join(CLONES_DIR, info.get("file", ""))):
            _clones[cid] = {"name": info.get("name", cid), "file": info["file"], "prompt": None}
    if _clones:
        log.info("%d voz(es) clonada(s) registrada(s) (prompts lazy)", len(_clones))


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
        _load_clones()
    except Exception as e:
        log.warning("load clones falhou: %s", e)
    try:
        _synth("Olá.", DEFAULT_VOICE, DEFAULT_LANG, None)
        log.info("warmup ok")
    except Exception as e:
        log.warning("warmup falhou: %s", e)


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "loading",
        "model": MODEL_NAME,
        "gpu": _gpu,
        "attn": _attn,
        "default_voice": DEFAULT_VOICE,
        "default_language": DEFAULT_LANG,
        "voices": [v["name"] for v in VOICES],
    }


@app.get("/voices")
def voices():
    clones = [{"name": cid, "desc": "🎤 clone: " + c["name"], "native": "clone"}
              for cid, c in _clones.items()]
    return {"voices": clones + VOICES, "default": DEFAULT_VOICE, "language": DEFAULT_LANG}


@app.post("/clone")
async def clone(file: UploadFile = File(...), name: str = Form(None), x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _model is None:
        raise HTTPException(503, "modelo ainda carregando")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio vazio")
    os.makedirs(CLONES_DIR, exist_ok=True)
    cid = "qclone_" + uuid.uuid4().hex[:8]
    cname = (name or os.path.splitext(file.filename or "")[0] or cid).strip()[:40]
    fname = cid + ".wav"
    try:
        data, sr = sf.read(io.BytesIO(raw))
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        sf.write(os.path.join(CLONES_DIR, fname), data, sr, subtype="PCM_16")
    except Exception as e:
        raise HTTPException(400, f"audio invalido: {e}")
    _clones[cid] = {"name": cname, "file": fname, "prompt": None}
    try:
        await asyncio.to_thread(_clone_prompt, cid)  # carrega Base + computa o prompt agora
    except Exception as e:
        _clones.pop(cid, None)
        raise HTTPException(400, f"falha ao clonar: {e}")
    _save_clones_meta()
    log.info("clone Qwen criado: %s (%s)", cid, cname)
    return {"voice": cid, "name": cname}


@app.post("/tts")
async def tts(req: TTSReq, x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _model is None:
        raise HTTPException(503, "modelo ainda carregando")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "texto vazio")
    voice = req.voice if (req.voice in VOICE_NAMES or req.voice in _clones) else DEFAULT_VOICE
    language = req.language or DEFAULT_LANG

    t0 = time.time()
    audio, sr = await asyncio.to_thread(_synth, text, voice, language, req.instruct)
    infer_ms = (time.time() - t0) * 1000.0
    dur = len(audio) / sr if sr else 0.0

    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    log.info("TTS '%s...' voice=%s %.1fs em %.0fms", text[:40], voice, dur, infer_ms)
    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={
            "X-Audio-Seconds": f"{dur:.3f}",
            "X-Infer-Ms": f"{infer_ms:.1f}",
            "X-RTF": f"{(infer_ms/1000.0)/dur:.4f}" if dur else "0",
            "X-Sample-Rate": str(sr),
            "X-Voice": voice,
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
