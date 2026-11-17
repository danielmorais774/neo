"""
neo-voice CosyVoice3 engine -- Fun-CosyVoice3-0.5B (GPU). Mesma API dos engines
Qwen/Coqui para o servico do Mac rotear entre eles (backend=cosyvoice).

    GET  /health   -> status, modelo, GPU, vozes
    GET  /voices   -> lista de vozes (refs PT pre-carregadas + clones)
    POST /clone    -> audio de referencia -> voz clone_<id>
    POST /tts      -> {text, voice, language, instruct} -> audio/wav

CosyVoice3 e zero-shot/cross-lingual (sem locutores embutidos): cada "voz" e um
audio de referencia (prompt). Para PT usamos inference_cross_lingual (nao precisa
de transcricao do prompt). O modelo EXIGE o prefixo
'You are a helpful assistant.<|endofprompt|>' no texto.

Config por env:
  NEO_COSY_MODEL   (default <dir>/pretrained_models/Fun-CosyVoice3-0.5B)
  NEO_COSY_HOST    (default 0.0.0.0)
  NEO_COSY_PORT    (default 5009)
  NEO_COSY_APIKEY  se setado, exige header X-API-Key
"""
import os
import io
import sys
import time
import json
import uuid
import hashlib
import asyncio
import logging
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-cosyvoice-engine")

import numpy as np
import torch
import soundfile as sf
import torchaudio
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
import uvicorn

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
# CosyVoice precisa do Matcha-TTS no path
sys.path.insert(0, os.path.join(ENGINE_DIR, "third_party", "Matcha-TTS"))

# torchaudio 2.11 forca torchcodec; CosyVoice usa torchaudio.load(backend='soundfile').
# Sem torchcodec no WSL -> monkeypatch obrigatorio (le via soundfile).
def _ta_load_sf(uri, *a, **k):
    data, sr = sf.read(str(uri), dtype="float32", always_2d=True)
    return torch.from_numpy(data.T).contiguous(), int(sr)
torchaudio.load = _ta_load_sf
log.info("torchaudio.load -> soundfile (sem torchcodec)")

MODEL_DIR = os.environ.get("NEO_COSY_MODEL",
                           os.path.join(ENGINE_DIR, "pretrained_models", "Fun-CosyVoice3-0.5B"))
HOST = os.environ.get("NEO_COSY_HOST", "0.0.0.0")
PORT = int(os.environ.get("NEO_COSY_PORT", "5009"))
API_KEY = os.environ.get("NEO_COSY_APIKEY") or None
DEFAULT_LANG = "pt"
CLONES_DIR = os.path.join(ENGINE_DIR, "clones")
PROMPTS_DIR = os.path.join(ENGINE_DIR, "prompts_cache")
PFX = "You are a helpful assistant.<|endofprompt|>"

# refs PT existentes no PC (clones do Qwen) pre-carregadas como vozes selecionaveis
PRELOAD_REFS = {
    "Daniel": "/opt/neo-tts/clones_qwen/qclone_662a1ee7.wav",
    "minha voz": "/opt/neo-tts/clones_qwen/qclone_580496e9.wav",
    "JARVIS-PTBR": "/opt/neo-tts/clones_qwen/qclone_04869956.wav",
}

app = FastAPI(title="neo-voice CosyVoice3 engine", version="1.0")
_model = None
_gpu = None
_sr = 24000
_voices = {}      # nome -> caminho do prompt (refs embutidas)
_clones = {}      # cid -> {"name", "file"}
DEFAULT_VOICE = None
_lock = threading.Lock()


def _prep_prompt(src):
    """Normaliza um audio de referencia: mono, <=18s, 16kHz; cacheia e devolve o path."""
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    try:
        key = hashlib.md5((src + str(os.path.getmtime(src))).encode()).hexdigest()[:12]
    except OSError:
        key = hashlib.md5(src.encode()).hexdigest()[:12]
    out = os.path.join(PROMPTS_DIR, key + ".wav")
    if os.path.exists(out):
        return out
    data, sr = sf.read(src, dtype="float32", always_2d=True)
    d = data.mean(axis=1)  # mono
    if len(d) > 18 * sr:
        d = d[: 18 * sr]
    if sr != 16000:
        t = torchaudio.functional.resample(torch.from_numpy(d).unsqueeze(0), sr, 16000)
        d = t.squeeze(0).numpy()
        sr = 16000
    sf.write(out, d, sr, subtype="PCM_16")
    return out


def _load_model():
    global _model, _gpu, _sr, DEFAULT_VOICE
    from cosyvoice.cli.cosyvoice import AutoModel
    _gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    log.info("carregando CosyVoice3 de %s (gpu=%s)", MODEL_DIR, _gpu)
    t0 = time.time()
    _model = AutoModel(model_dir=MODEL_DIR)
    try:
        _sr = int(_model.sample_rate)
    except Exception:
        _sr = 24000
    # registra refs PT que existirem
    for name, p in PRELOAD_REFS.items():
        if os.path.exists(p):
            try:
                _voices[name] = _prep_prompt(p)
            except Exception as e:
                log.warning("ref %s falhou: %s", name, e)
    if _voices and DEFAULT_VOICE is None:
        DEFAULT_VOICE = next(iter(_voices))
    log.info("modelo carregado em %.1fs | sr=%d | %d voz(es) | default=%s",
             time.time() - t0, _sr, len(_voices), DEFAULT_VOICE)


def _prompt_path(voice):
    if voice in _clones:
        return _prep_prompt(os.path.join(CLONES_DIR, _clones[voice]["file"]))
    if voice in _voices:
        return _voices[voice]
    if DEFAULT_VOICE:
        return _voices[DEFAULT_VOICE]
    return None


def _synth(text, voice, instruct=None):
    prompt = _prompt_path(voice)
    if not prompt:
        raise HTTPException(503, "nenhuma voz de referencia disponivel")
    with _lock:
        if instruct:
            gen = _model.inference_instruct2(
                text, f"You are a helpful assistant. {instruct}<|endofprompt|>", prompt, stream=False)
        else:
            gen = _model.inference_cross_lingual(PFX + text, prompt, stream=False)
        chunks = [j["tts_speech"] for j in gen]
    wav = torch.cat(chunks, dim=1) if len(chunks) > 1 else chunks[0]
    audio = wav.squeeze(0).detach().cpu().numpy().astype(np.float32)
    return audio, _sr


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
        _synth("Olá, tudo bem?", DEFAULT_VOICE)
        log.info("warmup ok")
    except Exception as e:
        log.warning("warmup falhou: %s", e)


def _load_clones():
    meta_path = os.path.join(CLONES_DIR, "clones.json")
    if not os.path.exists(meta_path):
        return
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except Exception:
        return
    for cid, info in meta.items():
        if os.path.exists(os.path.join(CLONES_DIR, info["file"])):
            _clones[cid] = {"name": info.get("name", cid), "file": info["file"]}
    if _clones:
        log.info("%d clone(s) recarregado(s)", len(_clones))


def _save_clones_meta():
    os.makedirs(CLONES_DIR, exist_ok=True)
    meta = {cid: {"name": c["name"], "file": c["file"]} for cid, c in _clones.items()}
    json.dump(meta, open(os.path.join(CLONES_DIR, "clones.json"), "w", encoding="utf-8"),
              ensure_ascii=False)


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "loading",
        "model": "cosyvoice3-0.5b",
        "gpu": _gpu,
        "attn": "cosyvoice3",
        "default_voice": DEFAULT_VOICE,
        "default_language": DEFAULT_LANG,
        "voices": list(_voices.keys()),
    }


@app.get("/voices")
def voices():
    clones = [{"name": cid, "desc": "🎤 clone: " + c["name"], "native": "clone"}
              for cid, c in _clones.items()]
    builtin = [{"name": v, "desc": "ref PT", "native": "pt"} for v in _voices]
    return {"voices": clones + builtin, "default": DEFAULT_VOICE, "language": DEFAULT_LANG}


@app.post("/clone")
async def clone(file: UploadFile = File(...), name: str = Form(None), x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _model is None:
        raise HTTPException(503, "modelo ainda carregando")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio vazio")
    os.makedirs(CLONES_DIR, exist_ok=True)
    cid = "clone_" + uuid.uuid4().hex[:8]
    cname = (name or os.path.splitext(file.filename or "")[0] or cid).strip()[:40]
    fname = cid + ".wav"
    fpath = os.path.join(CLONES_DIR, fname)
    try:
        data, sr = sf.read(io.BytesIO(raw), always_2d=True)
        sf.write(fpath, data.mean(axis=1), sr, subtype="PCM_16")
    except Exception:
        fname = cid + os.path.splitext(file.filename or ".wav")[1]
        fpath = os.path.join(CLONES_DIR, fname)
        open(fpath, "wb").write(raw)
    try:
        await asyncio.to_thread(_prep_prompt, fpath)  # valida/cacheia
    except Exception as e:
        raise HTTPException(400, f"falha ao processar o audio: {e}")
    _clones[cid] = {"name": cname, "file": fname}
    _save_clones_meta()
    log.info("clone criado: %s (%s)", cid, cname)
    return {"voice": cid, "name": cname}


@app.post("/tts")
async def tts(req: TTSReq, x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _model is None:
        raise HTTPException(503, "modelo ainda carregando")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "texto vazio")
    voice = req.voice if (req.voice in _voices or req.voice in _clones) else DEFAULT_VOICE
    t0 = time.time()
    audio, sr = await asyncio.to_thread(_synth, text, voice, req.instruct)
    infer_ms = (time.time() - t0) * 1000.0
    dur = len(audio) / sr if sr else 0.0
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    log.info("TTS '%s...' voice=%s %.1fs em %.0fms (RTF=%.3f)",
             text[:40], voice, dur, infer_ms, (infer_ms / 1000.0) / dur if dur else 0)
    return Response(content=buf.read(), media_type="audio/wav", headers={
        "X-Audio-Seconds": f"{dur:.3f}", "X-Infer-Ms": f"{infer_ms:.1f}",
        "X-RTF": f"{(infer_ms/1000.0)/dur:.4f}" if dur else "0",
        "X-Sample-Rate": str(sr), "X-Voice": str(voice),
    })


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
