"""
neo-voice Chatterbox Multilingual engine -- ResembleAI chatterbox (t3_model v3, GPU).
Mesma API dos outros engines (backend=chatterbox) p/ o servico do Mac rotear.

    GET  /health   -> status, modelo, GPU, vozes
    GET  /voices   -> refs PT pre-carregadas + clones
    POST /clone    -> audio de referencia -> voz clone_<id>  (sem transcricao!)
    POST /tts      -> {text, voice, language, instruct} -> audio/wav

Chatterbox clona zero-shot SO com o audio de referencia (sem ref_text). Tem controle
de expressividade (exaggeration) e cfg_weight. PT via language_id='pt' (23 idiomas).

Config: NEO_CB_HOST(0.0.0.0), NEO_CB_PORT(5011), NEO_CB_APIKEY, NEO_CB_LANG(pt),
        NEO_CB_EXAG(0.5), NEO_CB_CFG(0.5), NEO_CB_TEMP(0.7), NEO_CB_REP(2.0),
        NEO_CB_MINP(0.05), NEO_CB_MAXCHARS(200), NEO_CB_GAPMS(120), NEO_CB_FADEMS(8).

Chatterbox e autoregressivo: geracoes longas degradam (timbre metalico, repeticao).
Por isso fatiamos o texto por frase (<= NEO_CB_MAXCHARS) e geramos cada pedaco curto,
emendando com um pequeno silencio + fade nas bordas. temperature/repetition_penalty
ajudam a estabilizar.
"""
import os
import io
import re
import sys
import time
import json
import uuid
import hashlib
import asyncio
import logging
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-chatterbox-engine")

import numpy as np
import torch
import soundfile as sf
import torchaudio
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
import uvicorn

# torchaudio 2.11 forca torchcodec (ausente no WSL) -> le via soundfile
def _ta_load_sf(uri, *a, **k):
    data, sr = sf.read(str(uri), dtype="float32", always_2d=True)
    return torch.from_numpy(data.T).contiguous(), int(sr)
torchaudio.load = _ta_load_sf

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("NEO_CB_HOST", "0.0.0.0")
PORT = int(os.environ.get("NEO_CB_PORT", "5011"))
API_KEY = os.environ.get("NEO_CB_APIKEY") or None
LANG = os.environ.get("NEO_CB_LANG", "pt")
EXAG = float(os.environ.get("NEO_CB_EXAG", "0.5"))   # expressividade/emocao
CFG = float(os.environ.get("NEO_CB_CFG", "0.5"))     # cfg_weight (menor = mais devagar/estavel)
TEMP = float(os.environ.get("NEO_CB_TEMP", "0.7"))      # <0.8 reduz artefato/alucinacao aleatoria
REP = float(os.environ.get("NEO_CB_REP", "2.0"))        # repetition_penalty (anti loop/metalico)
MINP = float(os.environ.get("NEO_CB_MINP", "0.05"))     # min_p sampling
MAX_CHARS = int(os.environ.get("NEO_CB_MAXCHARS", "200"))  # fatia interna p/ geracoes curtas/estaveis
# silencio entre frases concatenadas. 120ms somado ao fatiamento do neo-voice
# dava pausa dupla e fala "engasgada"; 60ms e uma respiracao natural.
GAP_MS = int(os.environ.get("NEO_CB_GAPMS", "60"))
FADE_MS = int(os.environ.get("NEO_CB_FADEMS", "8"))     # fade nas bordas p/ matar clique na emenda
CLONES_DIR = os.path.join(ENGINE_DIR, "clones")
PROMPTS_DIR = os.path.join(ENGINE_DIR, "prompts_cache")

PRELOAD_REFS = {
    "Daniel": "/opt/neo-tts/clones_qwen/qclone_662a1ee7.wav",
    "minha voz": "/opt/neo-tts/clones_qwen/qclone_580496e9.wav",
    "JARVIS-PTBR": "/opt/neo-tts/clones_qwen/qclone_04869956.wav",
}

app = FastAPI(title="neo-voice Chatterbox engine", version="1.0")
_model = None
_gpu = None
_sr = 24000
_voices = {}    # nome -> caminho do prompt (refs embutidas)
_clones = {}    # cid -> {"name", "file"}
DEFAULT_VOICE = None
_lock = threading.Lock()


def _prep_prompt(src, secs=15):
    """Ref -> mono, <=secs (cacheado). Chatterbox reamostra internamente."""
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    try:
        key = hashlib.md5((src + str(os.path.getmtime(src))).encode()).hexdigest()[:12]
    except OSError:
        key = hashlib.md5(src.encode()).hexdigest()[:12]
    out = os.path.join(PROMPTS_DIR, key + ".wav")
    if os.path.exists(out):
        return out
    data, sr = sf.read(src, dtype="float32", always_2d=True)
    d = data.mean(axis=1)
    if len(d) > secs * sr:
        d = d[: secs * sr]
    sf.write(out, d, sr, subtype="PCM_16")
    return out


def _load_model():
    global _model, _gpu, _sr, DEFAULT_VOICE
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    _gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("carregando Chatterbox Multilingual em %s (gpu=%s)", dev, _gpu)
    t0 = time.time()
    _model = ChatterboxMultilingualTTS.from_pretrained(device=dev)
    try:
        _sr = int(_model.sr)
    except Exception:
        _sr = 24000
    for name, p in PRELOAD_REFS.items():
        if os.path.exists(p):
            try:
                _voices[name] = _prep_prompt(p)
            except Exception as e:
                log.warning("ref %s falhou: %s", name, e)
    if _voices and DEFAULT_VOICE is None:
        DEFAULT_VOICE = next(iter(_voices))
    log.info("Chatterbox carregado em %.1fs | sr=%d | %d voz(es) | default=%s",
             time.time() - t0, _sr, len(_voices), DEFAULT_VOICE)


def _prompt_path(voice):
    if voice in _clones:
        return _prep_prompt(os.path.join(CLONES_DIR, _clones[voice]["file"]))
    if voice in _voices:
        return _voices[voice]
    if DEFAULT_VOICE:
        return _voices[DEFAULT_VOICE]
    return None


def _split_for_cb(text, max_chars=MAX_CHARS):
    """Fatia por frase juntando ate max_chars; frases gigantes quebram em virgula/espaco.
    Chatterbox e AR e degrada em geracoes longas -> cada pedaco curto = geracao estavel."""
    text = " ".join((text or "").split())
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts = re.split(r"(?<=[\.\!\?…:;])\s+", text)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > max_chars:          # frase unica maior que o limite
            head = p[:max_chars]
            cut = max(head.rfind(", "), head.rfind("; "), head.rfind(" "))
            if cut < max_chars // 2:
                cut = max_chars
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if not cur:
            cur = p
        elif len(cur) + 1 + len(p) <= max_chars:
            cur += " " + p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def _edge_fade(a, n, fade_in=True):
    """Fade linear de n amostras nas pontas p/ evitar clique na emenda. `fade_in`
    desligado preserva o ataque do 1o pedaco (o fade-in comia o inicio da fala);
    o fade-out e sempre aplicado (e ele que mata o clique na emenda seguinte)."""
    if n > 0 and len(a) > 2 * n:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        if fade_in:
            a[:n] *= ramp
        a[-n:] *= ramp[::-1]
    return a


def _synth(text, voice):
    prompt = _prompt_path(voice)
    pieces = _split_for_cb(text)
    if not pieces:
        return np.zeros(0, dtype=np.float32), _sr
    fade_n = int(_sr * FADE_MS / 1000)
    gap = np.zeros(int(_sr * GAP_MS / 1000), dtype=np.float32)
    out = []
    with _lock:
        for i, piece in enumerate(pieces):
            wav = _model.generate(piece, language_id=LANG, audio_prompt_path=prompt,
                                  exaggeration=EXAG, cfg_weight=CFG, temperature=TEMP,
                                  repetition_penalty=REP, min_p=MINP)
            a = wav.squeeze(0).detach().cpu().numpy().astype(np.float32)
            out.append(_edge_fade(a, fade_n, fade_in=(i > 0)))
            if i < len(pieces) - 1:
                out.append(gap)
    audio = np.concatenate(out) if len(out) > 1 else out[0]
    return audio, _sr


def _check_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")


class TTSReq(BaseModel):
    text: str
    voice: str | None = None
    language: str | None = None
    instruct: str | None = None  # ignorado


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
    json.dump(meta, open(os.path.join(CLONES_DIR, "clones.json"), "w", encoding="utf-8"), ensure_ascii=False)


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


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "loading",
        "model": "chatterbox-mtl-v3",
        "gpu": _gpu,
        "attn": "chatterbox",
        "default_voice": DEFAULT_VOICE,
        "default_language": LANG,
        "voices": list(_voices.keys()),
    }


@app.get("/voices")
def voices():
    clones = [{"name": cid, "desc": "🎤 clone: " + c["name"], "native": "clone"} for cid, c in _clones.items()]
    builtin = [{"name": v, "desc": "ref PT", "native": "pt"} for v in _voices]
    return {"voices": clones + builtin, "default": DEFAULT_VOICE, "language": LANG}


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
        await asyncio.to_thread(_prep_prompt, fpath)
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
    audio, sr = await asyncio.to_thread(_synth, text, voice)
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
