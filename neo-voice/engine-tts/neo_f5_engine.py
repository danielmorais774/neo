"""
neo-voice F5-TTS engine -- firstpixel/F5-TTS-pt-br (PT-BR nativo, GPU). Mesma API
dos engines Qwen/Coqui/CosyVoice (backend=f5) p/ o servico do Mac rotear.

    GET  /health   -> status, modelo, GPU, vozes
    GET  /voices   -> refs PT pre-carregadas + clones
    POST /clone    -> audio de referencia -> voz clone_<id>
    POST /tts      -> {text, voice, language, instruct} -> audio/wav

F5-TTS clona zero-shot mas EXIGE ref_audio + ref_text (transcricao da ref). Em vez
do Whisper do F5 (que quebra com torchcodec no WSL), transcrevemos a ref com o
Parakeet STT (:5005, nativo no Windows, alcancado pelo gateway da WSL) e cacheamos.

Config: NEO_F5_MODEL, NEO_F5_HOST(0.0.0.0), NEO_F5_PORT(5010), NEO_F5_APIKEY,
        NEO_F5_NFE(32), NEO_STT_URL (default http://<gateway>:5005).
"""
import os
import io
import re
import sys
import glob
import time
import json
import uuid
import hashlib
import asyncio
import logging
import threading
import subprocess
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-f5-engine")

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
MODEL_DIR = os.environ.get("NEO_F5_MODEL", os.path.join(ENGINE_DIR, "models", "F5-TTS-pt-br"))
HOST = os.environ.get("NEO_F5_HOST", "0.0.0.0")
PORT = int(os.environ.get("NEO_F5_PORT", "5010"))
API_KEY = os.environ.get("NEO_F5_APIKEY") or None
NFE = int(os.environ.get("NEO_F5_NFE", "32"))
SPEED = float(os.environ.get("NEO_F5_SPEED", "0.9"))   # <1 = mais devagar/natural
DEFAULT_LANG = "pt"

try:
    from num2words import num2words
except Exception:
    num2words = None


def _hword(h):
    """Hora por extenso com gênero feminino p/ 1 e 2 (uma hora, duas horas)."""
    if h == 1:
        return "uma"
    if h == 2:
        return "duas"
    return num2words(h, lang="pt")


def _normalize_pt(text):
    """F5-pt-br nao le digitos -> converte numeros/horarios p/ extenso (PT). Preserva
    pontuacao (vírgulas = pausas). Ex: '7:30' -> 'sete e trinta', '14h' -> 'quatorze
    horas', '1,2,10' -> 'um, dois, dez'."""
    if not num2words:
        return text

    def _time(m):  # HH:MM ou HHhMM
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return m.group(0)
        if mi == 0:
            return "uma hora" if h == 1 else "%s horas" % _hword(h)
        return "%s e %s" % (_hword(h), num2words(mi, lang="pt"))

    def _timeh(m):  # HHh (sem minutos): "14h", "8 h"
        h = int(m.group(1))
        if h > 23:
            return m.group(0)
        return "uma hora" if h == 1 else "%s horas" % _hword(h)

    def _ord(m):
        try:
            w = num2words(int(m.group(1)), lang="pt", to="ordinal")
            if m.group(2) == "ª" and w.endswith("o"):   # feminino: segundo->segunda
                w = w[:-1] + "a"
            return w
        except Exception:
            return m.group(0)

    def _n(m):
        try:
            return num2words(int(m.group()), lang="pt")
        except Exception:
            return m.group()

    text = re.sub(r"\b(\d{1,2})[:h](\d{2})\b", _time, text)   # 7:30 / 7h30 / 14h00
    text = re.sub(r"\b(\d{1,2})\s*h\b", _timeh, text)         # 14h / 8 h
    text = re.sub(r"(\d+)\s*([ºª°])", _ord, text)             # 1º, 2ª
    text = re.sub(r"\d+", _n, text)                           # demais números
    return text
CLONES_DIR = os.path.join(ENGINE_DIR, "clones")
PROMPTS_DIR = os.path.join(ENGINE_DIR, "prompts_cache")

PRELOAD_REFS = {
    "Daniel": "/opt/neo-tts/clones_qwen/qclone_662a1ee7.wav",
    "minha voz": "/opt/neo-tts/clones_qwen/qclone_580496e9.wav",
    "JARVIS-PTBR": "/opt/neo-tts/clones_qwen/qclone_04869956.wav",
}


def _gw():
    try:
        o = subprocess.check_output(["ip", "route", "show", "default"]).decode()
        return o.split("via")[1].split()[0]
    except Exception:
        return None


_STT_URL = os.environ.get("NEO_STT_URL")
# LAN IP do PC primeiro (alcançável da WSL); gateway/.1 como fallback. Curto timeout + cache.
STT_HOSTS = ([_STT_URL] if _STT_URL else
             ["http://%s:5005" % h for h in ["192.168.4.185", _gw(), "172.19.48.1"] if h])
_stt_ok = None  # host que respondeu (cache p/ não repetir timeouts)

app = FastAPI(title="neo-voice F5-TTS engine", version="1.0")
_f5 = None
_gpu = None
_sr = 24000
_voices = {}    # nome -> {"file", "text"}
_clones = {}    # cid -> {"name", "file", "text"}
DEFAULT_VOICE = None
_lock = threading.Lock()


def _clip16k(src, out, secs=10):
    """Ref -> mono, <=secs, 16kHz (cacheado)."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    data, sr = sf.read(src, dtype="float32", always_2d=True)
    d = data.mean(axis=1)
    if len(d) > secs * sr:
        d = d[: secs * sr]
    if sr != 16000:
        d = torchaudio.functional.resample(torch.from_numpy(d).unsqueeze(0), sr, 16000).squeeze(0).numpy()
        sr = 16000
    sf.write(out, d, sr, subtype="PCM_16")
    return out, d


def _stt(d16k):
    """Transcreve PCM float mono 16k via Parakeet STT (Windows-native)."""
    global _stt_ok
    pcm = (np.clip(d16k, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    hosts = ([_stt_ok] if _stt_ok else []) + [h for h in STT_HOSTS if h != _stt_ok]
    last = None
    for base in hosts:
        try:
            req = urllib.request.Request(base.rstrip("/") + "/transcribe", data=pcm,
                                         headers={"Content-Type": "application/octet-stream",
                                                  "X-Sample-Rate": "16000"})
            txt = json.loads(urllib.request.urlopen(req, timeout=12).read()).get("text", "").strip()
            _stt_ok = base
            return txt
        except Exception as e:
            last = e
    log.warning("STT falhou (%r): %s", STT_HOSTS, last)
    return ""


def _register_ref(name, src, cached_text=None):
    """Clipa + (transcreve se nao cacheado), devolve {file,text}."""
    safe = hashlib.md5(name.encode()).hexdigest()[:8]
    cf, d = _clip16k(src, os.path.join(PROMPTS_DIR, "ref_%s.wav" % safe))
    txt = cached_text if cached_text else _stt(d)
    return {"file": cf, "text": txt}


def _load_model():
    global _f5, _gpu, _sr, DEFAULT_VOICE
    from f5_tts.api import F5TTS
    _gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    ckpts = glob.glob(os.path.join(MODEL_DIR, "**", "*.safetensors"), recursive=True)
    if not ckpts:
        raise RuntimeError("checkpoint .safetensors nao encontrado em %s" % MODEL_DIR)
    log.info("carregando F5-TTS-pt-br (%s) gpu=%s", ckpts[0], _gpu)
    t0 = time.time()
    _f5 = F5TTS(model="F5TTS_Base", ckpt_file=ckpts[0])
    try:
        _sr = int(_f5.target_sample_rate)
    except Exception:
        _sr = 24000
    rp = os.path.join(PROMPTS_DIR, "refs.json")
    try:
        meta = json.load(open(rp, encoding="utf-8"))
    except Exception:
        meta = {}
    changed = False
    for name, p in PRELOAD_REFS.items():
        if os.path.exists(p):
            try:
                _voices[name] = _register_ref(name, p, meta.get(name))
                if meta.get(name) != _voices[name]["text"]:
                    meta[name] = _voices[name]["text"]
                    changed = True
                log.info("ref '%s' ok (texto: %r)", name, _voices[name]["text"][:60])
            except Exception as e:
                log.warning("ref %s falhou: %s", name, e)
    if changed:
        try:
            os.makedirs(PROMPTS_DIR, exist_ok=True)
            json.dump(meta, open(rp, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            log.warning("save refs.json falhou: %s", e)
    if _voices and DEFAULT_VOICE is None:
        DEFAULT_VOICE = next(iter(_voices))
    log.info("F5 carregado em %.1fs | sr=%d | %d voz(es) | default=%s",
             time.time() - t0, _sr, len(_voices), DEFAULT_VOICE)


def _resolve(voice):
    if voice in _clones:
        c = _clones[voice]
        return os.path.join(CLONES_DIR, c["file"]), c["text"]
    if voice in _voices:
        v = _voices[voice]
        return v["file"], v["text"]
    if DEFAULT_VOICE:
        v = _voices[DEFAULT_VOICE]
        return v["file"], v["text"]
    return None, None


def _synth(text, voice):
    ref_file, ref_text = _resolve(voice)
    if not ref_file:
        raise HTTPException(503, "nenhuma voz de referencia disponivel")
    gen = _normalize_pt(text)
    with _lock:
        wav, sr, _ = _f5.infer(ref_file=ref_file, ref_text=ref_text or "",
                               gen_text=gen, nfe_step=NFE, speed=SPEED, remove_silence=True)
    return np.asarray(wav, dtype=np.float32).reshape(-1), int(sr)


def _check_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")


class TTSReq(BaseModel):
    text: str
    voice: str | None = None
    language: str | None = None
    instruct: str | None = None  # ignorado pelo F5


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
            _clones[cid] = {"name": info.get("name", cid), "file": info["file"], "text": info.get("text", "")}
    if _clones:
        log.info("%d clone(s) recarregado(s)", len(_clones))


def _save_clones_meta():
    os.makedirs(CLONES_DIR, exist_ok=True)
    meta = {cid: {"name": c["name"], "file": c["file"], "text": c.get("text", "")} for cid, c in _clones.items()}
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
        "status": "ok" if _f5 is not None else "loading",
        "model": "f5-tts-pt-br",
        "gpu": _gpu,
        "attn": "f5",
        "default_voice": DEFAULT_VOICE,
        "default_language": DEFAULT_LANG,
        "voices": list(_voices.keys()),
    }


@app.get("/voices")
def voices():
    clones = [{"name": cid, "desc": "🎤 clone: " + c["name"], "native": "clone"} for cid, c in _clones.items()]
    builtin = [{"name": v, "desc": "ref PT-BR", "native": "pt"} for v in _voices]
    return {"voices": clones + builtin, "default": DEFAULT_VOICE, "language": DEFAULT_LANG}


@app.post("/clone")
async def clone(file: UploadFile = File(...), name: str = Form(None), x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _f5 is None:
        raise HTTPException(503, "modelo ainda carregando")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio vazio")
    os.makedirs(CLONES_DIR, exist_ok=True)
    cid = "clone_" + uuid.uuid4().hex[:8]
    cname = (name or os.path.splitext(file.filename or "")[0] or cid).strip()[:40]
    src = os.path.join(CLONES_DIR, cid + "_src" + os.path.splitext(file.filename or ".wav")[1])
    open(src, "wb").write(raw)
    fname = cid + ".wav"
    fpath = os.path.join(CLONES_DIR, fname)
    try:
        _, d = _clip16k(src, fpath)               # ref limpa 16k <=10s
        txt = await asyncio.to_thread(_stt, d)    # transcricao via Parakeet
    except Exception as e:
        raise HTTPException(400, f"falha ao processar o audio: {e}")
    _clones[cid] = {"name": cname, "file": fname, "text": txt}
    _save_clones_meta()
    log.info("clone criado: %s (%s) texto=%r", cid, cname, txt[:60])
    return {"voice": cid, "name": cname}


@app.post("/tts")
async def tts(req: TTSReq, x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _f5 is None:
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
