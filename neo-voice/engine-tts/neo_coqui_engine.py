"""
neo-voice Coqui TTS engine -- XTTS-v2 (GPU). Mesma API do engine Qwen para o
serviço do Mac rotear entre os dois.

    GET  /health   -> status, modelo, GPU, vozes
    GET  /voices   -> lista de vozes (speakers embutidos do XTTS-v2)
    POST /tts      -> {text, voice, language, instruct} -> audio/wav

Config por env:
  NEO_COQUI_MODEL  (default tts_models/multilingual/multi-dataset/xtts_v2)
  NEO_COQUI_VOICE  voz default (default: 1a voz disponivel)
  NEO_COQUI_LANG   idioma default (default pt)
  NEO_COQUI_HOST   (default 0.0.0.0)
  NEO_COQUI_PORT   (default 5007)
  NEO_COQUI_APIKEY se setado, exige header X-API-Key
  NEO_COQUI_SENT_GAP_MS / _ELLIPSIS_GAP_MS / _TAIL_GAP_MS
                   pausas que substituem o ponto final (ver _sentences_for_xtts)
"""
import os
import io
import re
import time
import asyncio
import logging
import threading

os.environ.setdefault("COQUI_TOS_AGREED", "1")  # aceita a licença CPML do XTTS (uso pessoal)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-coqui-engine")

import json
import uuid

import numpy as np
import torch
import soundfile as sf
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
import uvicorn

# O XTTS carrega o audio de referencia (clonagem) via torchaudio.load, que no torch 2.9+
# tenta o torchcodec — que exige libs CUDA extras no WSL e quebra. Trocamos por soundfile.
try:
    import torchaudio as _ta

    def _ta_load_sf(uri, *a, **k):
        data, sr = sf.read(str(uri), dtype="float32", always_2d=True)
        return torch.from_numpy(data.T).contiguous(), int(sr)

    _ta.load = _ta_load_sf
    log.info("torchaudio.load -> soundfile (sem torchcodec)")
except Exception as _e:
    log.warning("patch torchaudio falhou: %s", _e)

MODEL_NAME = os.environ.get("NEO_COQUI_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2")
DEFAULT_VOICE = os.environ.get("NEO_COQUI_VOICE") or None
DEFAULT_LANG = os.environ.get("NEO_COQUI_LANG", "pt")
HOST = os.environ.get("NEO_COQUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("NEO_COQUI_PORT", "5007"))
API_KEY = os.environ.get("NEO_COQUI_APIKEY") or None
CLONES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clones")

# pausas (ms) que substituem os pontos finais removidos -- ver _sentences_for_xtts
SENT_GAP_MS = int(os.environ.get("NEO_COQUI_SENT_GAP_MS", "200"))       # entre sentencas
ELLIPSIS_GAP_MS = int(os.environ.get("NEO_COQUI_ELLIPSIS_GAP_MS", "380"))  # "..." pausa mais
TAIL_GAP_MS = int(os.environ.get("NEO_COQUI_TAIL_GAP_MS", "180"))       # fim do audio

app = FastAPI(title="neo-voice Coqui engine", version="1.0")
_tts = None
_gpu = None
_sr = 24000
_voices = []                 # speakers embutidos
_clones = {}                 # id -> {"name", "gpt", "spk", "file"} (vozes clonadas)
_lock = threading.Lock()


def _load_model():
    global _tts, _gpu, _sr, _voices, DEFAULT_VOICE
    from TTS.api import TTS
    _gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("carregando %s em %s (gpu=%s)", MODEL_NAME, dev, _gpu)
    t0 = time.time()
    _tts = TTS(MODEL_NAME).to(dev)
    try:
        _sr = int(_tts.synthesizer.output_sample_rate)
    except Exception:
        _sr = 24000
    # lista de speakers embutidos
    try:
        sm = _tts.synthesizer.tts_model.speaker_manager
        _voices = list(sm.speakers.keys()) if getattr(sm, "speakers", None) else []
    except Exception:
        _voices = []
    if not _voices:
        try:
            _voices = list(_tts.speakers or [])
        except Exception:
            _voices = []
    if _voices and (DEFAULT_VOICE not in _voices):
        # prefere uma voz neutra conhecida, senao a 1a
        for pref in ("Ana Florence", "Claribel Dervla", "Daisy Studious"):
            if pref in _voices:
                DEFAULT_VOICE = pref
                break
        else:
            DEFAULT_VOICE = _voices[0]
    log.info("modelo carregado em %.1fs | sr=%d | %d vozes | default=%s",
             time.time() - t0, _sr, len(_voices), DEFAULT_VOICE)


# ---------------------------------------------------------------------------
# "ponto" falado (artefato do XTTS-v2)
# ---------------------------------------------------------------------------
# O XTTS sintetiza UMA sentenca por vez e vocaliza o ponto FINAL dela: "Beleza."
# sai como "beleza ponto" (medido: 6/6 em frase curta na voz clonada, ~3/4 na
# builtin; frases longas tambem, so que menos). Ponto NO MEIO do texto nunca e
# vocalizado -- vira pausa normalmente.
# Como o ponto final so serve de pausa, fatiamos as sentencas aqui, mandamos cada
# uma SEM o ponto e emendamos o audio com silencio de verdade (a pausa que o ponto
# daria). "!" e "?" nao sao vocalizados e carregam entonacao: ficam no texto.
_ABBREVS = {"sr", "sra", "dr", "dra", "st", "co", "jr", "ltd",   # o cleaner do XTTS
            "prof", "profa", "av", "ex", "etc", "fig",           # expande estas -- e
            "pag", "pág", "num", "núm"}                          # precisa do ponto
_SENT_END_RE = re.compile(r"([.!?…]+)(\s+|$)")
_TRAIL_DOT_RE = re.compile(r"[.…]+\s*$")


def _ends_in_abbrev(head: str) -> bool:
    """O ponto depois de `head` e de abreviacao ("Sr.", "D.")? Entao nao e fim de frase."""
    m = re.search(r"([\wÀ-ÿ]+)$", head)
    w = m.group(1).lower() if m else ""
    return w in _ABBREVS or len(w) == 1


def _strip_final_dot(seg: str) -> str:
    core = _TRAIL_DOT_RE.sub("", seg).rstrip()
    if not core or _ends_in_abbrev(core):  # "...na Ltd." -- o cleaner precisa do ponto
        return seg
    return core


def _sentences_for_xtts(text: str):
    """Fatia `text` em [(sentenca_sem_ponto_final, pausa_ms_depois), ...]."""
    text = re.sub(r"\s+", " ", text).strip()
    parts, start = [], 0
    for m in _SENT_END_RE.finditer(text):
        punct = m.group(1)
        if punct[0] in ".…" and _ends_in_abbrev(text[start:m.start()]):
            continue
        seg = text[start:m.end()].strip()
        if seg:
            gap = ELLIPSIS_GAP_MS if len(punct) > 1 or punct == "…" else SENT_GAP_MS
            parts.append((_strip_final_dot(seg), gap))
        start = m.end()
    tail = text[start:].strip()
    if tail:
        parts.append((_strip_final_dot(tail), 0))
    return parts or [(text, 0)]


def _silence(ms: int):
    return np.zeros(int(_sr * ms / 1000.0), dtype=np.float32)


def _synth(text, voice, language):
    parts = _sentences_for_xtts(text)
    chunks = []
    with _lock:
        for i, (seg, gap_ms) in enumerate(parts):
            if voice in _clones:
                c = _clones[voice]
                out = _tts.synthesizer.tts_model.inference(
                    seg, language, c["gpt"], c["spk"], temperature=0.7, enable_text_splitting=True)
                wav = out["wav"]
            else:
                spk = voice if voice in _voices else DEFAULT_VOICE
                wav = _tts.tts(text=seg, speaker=spk, language=language)
            chunks.append(np.asarray(wav, dtype=np.float32).reshape(-1))
            # ultima sentenca leva o respiro do fim: no /ws/tts cada chunk e uma
            # sentenca, e sem ele as sentencas colam umas nas outras na reproducao
            gap = gap_ms if i < len(parts) - 1 else TAIL_GAP_MS
            if gap > 0:
                chunks.append(_silence(gap))
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return audio, _sr


def _compute_latents(audio_path):
    """Extrai os latents do locutor de um audio de referencia (clonagem).

    NAO subir gpt_cond_len/max_ref_length (30s) nem temperature/repetition_penalty
    tunados sem investigar: essa versao ficou no repo sem nunca ir p/ o PC gamer e,
    quando foi implantada (12/07/2026), o engine passou a morrer de kill duro (sem
    traceback, as vezes derrubando a VM do WSL) depois de 3-4 sinteses -- os defaults
    abaixo rodam estaveis. Ver [[neo-voice-coqui-ponto-fix]]."""
    with _lock:
        gpt, spk = _tts.synthesizer.tts_model.get_conditioning_latents(audio_path=[audio_path])
    return gpt, spk


def _load_clones():
    """Recarrega vozes clonadas salvas (clones.json + audios) -> recomputa latents."""
    meta_path = os.path.join(CLONES_DIR, "clones.json")
    if not os.path.exists(meta_path):
        return
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except Exception:
        return
    for cid, info in meta.items():
        f = os.path.join(CLONES_DIR, info["file"])
        if not os.path.exists(f):
            continue
        try:
            gpt, spk = _compute_latents(f)
            _clones[cid] = {"name": info.get("name", cid), "gpt": gpt, "spk": spk, "file": info["file"]}
        except Exception as e:
            log.warning("clone %s falhou ao recarregar: %s", cid, e)
    if _clones:
        log.info("%d voz(es) clonada(s) recarregada(s)", len(_clones))


def _save_clones_meta():
    os.makedirs(CLONES_DIR, exist_ok=True)
    meta = {cid: {"name": c["name"], "file": c["file"]} for cid, c in _clones.items()}
    json.dump(meta, open(os.path.join(CLONES_DIR, "clones.json"), "w", encoding="utf-8"), ensure_ascii=False)


def _check_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")


class TTSReq(BaseModel):
    text: str
    voice: str | None = None
    language: str | None = None
    instruct: str | None = None  # ignorado pelo XTTS


@app.on_event("startup")
def _startup():
    _load_model()
    try:
        _load_clones()
    except Exception as e:
        log.warning("load clones falhou: %s", e)
    try:
        _synth("Olá.", DEFAULT_VOICE, DEFAULT_LANG)
        log.info("warmup ok")
    except Exception as e:
        log.warning("warmup falhou: %s", e)


@app.get("/health")
def health():
    return {
        "status": "ok" if _tts is not None else "loading",
        "model": "coqui-xtts-v2",
        "gpu": _gpu,
        "attn": "xtts",
        "default_voice": DEFAULT_VOICE,
        "default_language": DEFAULT_LANG,
        "voices": _voices,
    }


@app.get("/voices")
def voices():
    clones = [{"name": cid, "desc": "🎤 clone: " + c["name"], "native": "clone"}
              for cid, c in _clones.items()]
    builtin = [{"name": v, "desc": "XTTS", "native": ""} for v in _voices]
    return {"voices": clones + builtin, "default": DEFAULT_VOICE, "language": DEFAULT_LANG}


@app.post("/clone")
async def clone(file: UploadFile = File(...), name: str = Form(None), x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _tts is None:
        raise HTTPException(503, "modelo ainda carregando")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio vazio")
    os.makedirs(CLONES_DIR, exist_ok=True)
    cid = "clone_" + uuid.uuid4().hex[:8]
    cname = (name or os.path.splitext(file.filename or "")[0] or cid).strip()[:40]
    # normaliza para wav mono via soundfile (XTTS reamostra internamente)
    fname = cid + ".wav"
    fpath = os.path.join(CLONES_DIR, fname)
    try:
        data, sr = sf.read(io.BytesIO(raw))
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        sf.write(fpath, data, sr, subtype="PCM_16")
    except Exception:
        # formato nao-wav (mp3/m4a...): salva cru e deixa o backend de audio decodificar
        fname = cid + os.path.splitext(file.filename or ".wav")[1]
        fpath = os.path.join(CLONES_DIR, fname)
        open(fpath, "wb").write(raw)
    t0 = time.time()
    try:
        gpt, spk = await asyncio.to_thread(_compute_latents, fpath)
    except Exception as e:
        raise HTTPException(400, f"falha ao processar o audio: {e}")
    _clones[cid] = {"name": cname, "gpt": gpt, "spk": spk, "file": fname}
    _save_clones_meta()
    log.info("clone criado: %s (%s) em %.1fs", cid, cname, time.time() - t0)
    return {"voice": cid, "name": cname}


@app.post("/tts")
async def tts(req: TTSReq, x_api_key: str = Header(None)):
    _check_key(x_api_key)
    if _tts is None:
        raise HTTPException(503, "modelo ainda carregando")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "texto vazio")
    voice = req.voice if (req.voice in _voices or req.voice in _clones) else DEFAULT_VOICE
    language = req.language or DEFAULT_LANG
    t0 = time.time()
    audio, sr = await asyncio.to_thread(_synth, text, voice, language)
    infer_ms = (time.time() - t0) * 1000.0
    dur = len(audio) / sr if sr else 0.0
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    log.info("TTS '%s...' voice=%s %.1fs em %.0fms", text[:40], voice, dur, infer_ms)
    return Response(content=buf.read(), media_type="audio/wav", headers={
        "X-Audio-Seconds": f"{dur:.3f}", "X-Infer-Ms": f"{infer_ms:.1f}",
        "X-RTF": f"{(infer_ms/1000.0)/dur:.4f}" if dur else "0",
        "X-Sample-Rate": str(sr), "X-Voice": voice,
    })


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
