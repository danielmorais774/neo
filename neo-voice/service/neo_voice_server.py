"""
neo-voice service (Mac) -- orquestra voz: STT (Parakeet) + TTS (Qwen3-TTS), usando
o PC gamer como motor (duas engines na RTX). Serve tambem o painel web com 2 abas.

  GET /         -- painel web (abas STT e TTS).
  WS  /ws/panel -- STT: recebe PCM int16 16kHz, devolve JSON {type: partial|final|...}.
  WS  /ws/audio -- STT contrato antigo (FINAL_TEXT:/FINAL_RESPONSE:), drop-in.
  GET /voices   -- TTS: proxy das vozes do engine de TTS.
  POST /tts     -- TTS: {text, voice, language, instruct} -> audio/wav (proxy).
  GET /health   -- status do service + engines STT/TTS.

Config (env):
  NEO_STT_ENGINE_URL   default http://192.168.4.185:5005  (engine STT no PC)
  NEO_TTS_ENGINE_URL   default http://192.168.4.185:5006  (engine TTS no PC)
  NEO_STT_APIKEY       opcional, repassado aos engines
  NEO_CORE_URL         opcional, ex http://localhost:8000/message
  NEO_STT_PORT         default 5002
  VAD_SILENCE_SEC / VAD_MIN_SPEECH_SEC / VAD_PREROLL_SEC / VAD_PARTIAL_SEC

  Cascata de STT: PC gamer (GPU) -> parakeet-cpu (LXC) -> OpenRouter (so finais).
  NEO_STT_CPU_ENGINE_URL        default http://192.168.4.230:5005 ("" desliga a camada)

  Fallback de STT (so quando os engines locais estao offline):
  OPENROUTER_API_KEY            chave OpenRouter; sem ela o fallback fica desligado
  NEO_STT_OPENROUTER_MODEL      default nvidia/parakeet-tdt-0.6b-v3
  NEO_STT_FALLBACK              default 1 (0/false desliga o fallback)
  NEO_STT_FALLBACK_PARTIALS     default 0 (1 manda tambem parciais p/ a nuvem)
  NEO_STT_ENGINE_DOWN_COOLDOWN  default 15s (circuit breaker do engine local)
  NEO_STT_ENGINE_CONNECT_TIMEOUT/NEO_STT_ENGINE_TIMEOUT/NEO_STT_OPENROUTER_TIMEOUT

  STT: o texto transcrito passa por stt_text.py ANTES de sair (corrige o nome do
  assistente — "Neil/Nil/New/knew" -> "Neo" — e descarta eco do wake word). O WS
  devolve `raw_text`/`fixes` junto do `text` corrigido; final vazio + `dropped`
  significa "nao era turno". NEO_STT_NORMALIZE=0 desliga; /stt/lexicon edita o
  lexico em runtime. Ver neo-core/docs/voice-input-normalization-plan.md.

  TTS: o texto e normalizado (tts_text.py: numeros/simbolos por extenso pt-BR +
  lexico de pronuncia, "Neo" -> "níu"; NEO_TTS_LEXICON estende) antes de QUALQUER
  engine. NEO_TTS_ENGINE_TIMEOUT (default 20s) e o read timeout por chunk;
  NEO_TTS_FIRST_AUDIO_HEDGE (default 6s) e o prazo do 1o audio no /ws/tts antes
  de correr o Kokoro em paralelo (fala quem chegar primeiro, fonte sticky).
"""
from __future__ import annotations

import os
import io
import re
import json
import base64
import time
import logging
import asyncio
from collections import deque
from typing import Optional

import numpy as np
import soundfile as sf
import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from engine_monitor import EngineHealthMonitor, EngineState, UP, DOWN, UNKNOWN
from tts_text import normalize_tts_text
import stt_text
from stt_text import normalize_stt_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neo-voice")

# Carrega um .env opcional ao lado deste arquivo (service/.env) ANTES de ler as
# envs abaixo. Defensivo: se python-dotenv nao estiver instalado, segue sem .env
# (as envs ja exportadas no ambiente continuam valendo). override=False -> uma var
# realmente exportada no shell tem prioridade sobre o arquivo.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
except ImportError:
    pass

SAMPLE_RATE = 16000
ENGINE_URL = os.environ.get("NEO_STT_ENGINE_URL", "http://192.168.4.185:5005").rstrip("/")
# 2o engine local: mesmo Parakeet em CPU pura (LXC parakeet-cpu). Cobre o PC gamer
# desligado sem mandar audio (nem dinheiro) pra nuvem. Vazio desliga a camada.
CPU_ENGINE_URL = os.environ.get("NEO_STT_CPU_ENGINE_URL", "http://192.168.4.230:5005").rstrip("/")
TTS_ENGINE_URL = os.environ.get("NEO_TTS_ENGINE_URL", "http://192.168.4.185:5006").rstrip("/")
COQUI_ENGINE_URL = os.environ.get("NEO_COQUI_ENGINE_URL", "http://192.168.4.185:5007").rstrip("/")
DIA_ENGINE_URL = os.environ.get("NEO_DIA_ENGINE_URL", "http://192.168.4.185:5008").rstrip("/")
COSYVOICE_ENGINE_URL = os.environ.get("NEO_COSYVOICE_ENGINE_URL", "http://192.168.4.185:5009").rstrip("/")
F5_ENGINE_URL = os.environ.get("NEO_F5_ENGINE_URL", "http://192.168.4.185:5010").rstrip("/")
CHATTERBOX_ENGINE_URL = os.environ.get("NEO_CHATTERBOX_ENGINE_URL", "http://192.168.4.185:5011").rstrip("/")
# motores de TTS selecionaveis no painel
TTS_BACKENDS = {"qwen": TTS_ENGINE_URL, "coqui": COQUI_ENGINE_URL, "dia": DIA_ENGINE_URL,
                "cosyvoice": COSYVOICE_ENGINE_URL, "f5": F5_ENGINE_URL,
                "chatterbox": CHATTERBOX_ENGINE_URL}


def tts_url(backend):
    return TTS_BACKENDS.get((backend or "qwen").lower(), TTS_ENGINE_URL)


API_KEY = os.environ.get("NEO_STT_APIKEY") or None
NEO_CORE_URL = os.environ.get("NEO_CORE_URL") or None
PORT = int(os.environ.get("NEO_STT_PORT", "5002"))

# --- Fallback de STT via OpenRouter -----------------------------------------
# O engine local (Parakeet no PC gamer) e sempre a 1a opcao. So caimos pro
# OpenRouter (mesmo modelo, nvidia/parakeet-tdt-0.6b-v3) quando o PC gamer esta
# offline/inacessivel. Para nao pagar caro nem travar, o fallback so vale para
# FINAIS — parciais (legenda ao vivo, a cada VAD_PARTIAL_SEC) sao puladas.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("NEO_OPENROUTER_APIKEY") or None
OPENROUTER_STT_MODEL = os.environ.get("NEO_STT_OPENROUTER_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
STT_FALLBACK = os.environ.get("NEO_STT_FALLBACK", "1").lower() not in ("0", "false", "no")
STT_FALLBACK_PARTIALS = os.environ.get("NEO_STT_FALLBACK_PARTIALS", "0").lower() in ("1", "true", "yes")
# timeouts: conexao curta p/ detectar PC gamer caido rapido; leitura maior p/ inferencia.
ENGINE_CONNECT_TIMEOUT = float(os.environ.get("NEO_STT_ENGINE_CONNECT_TIMEOUT", "4"))
ENGINE_READ_TIMEOUT = float(os.environ.get("NEO_STT_ENGINE_TIMEOUT", "20"))
OPENROUTER_TIMEOUT = float(os.environ.get("NEO_STT_OPENROUTER_TIMEOUT", "30"))
# circuit breaker: apos uma falha do local, vai direto p/ OpenRouter por N s
# (evita esperar timeout do host morto a cada chamada).
ENGINE_DOWN_COOLDOWN = float(os.environ.get("NEO_STT_ENGINE_DOWN_COOLDOWN", "15"))
_engine_down_until = 0.0  # epoch; enquanto now < isto, pula o engine local

# Override do fallback em runtime, controlado pelo neo-panel (None = usa o default
# do env STT_FALLBACK). Persistido em disco p/ sobreviver a restart do container.
_fallback_runtime = None  # Optional[bool]
_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
_FALLBACK_STATE_FILE = os.path.join(_STATE_DIR, "stt_fallback.json")


def _fallback_enabled() -> bool:
    """Flag efetiva do fallback (override do painel tem prioridade sobre o env)."""
    return STT_FALLBACK if _fallback_runtime is None else _fallback_runtime


def _fallback_available() -> bool:
    """So usa OpenRouter se o fallback estiver LIGADO e houver chave."""
    return bool(_fallback_enabled() and OPENROUTER_API_KEY)


def _load_fallback_state():
    global _fallback_runtime
    try:
        with open(_FALLBACK_STATE_FILE) as f:
            v = json.load(f).get("enabled")
        if isinstance(v, bool):
            _fallback_runtime = v
            log.info("fallback OpenRouter: override carregado do disco = %s", v)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("nao consegui ler o estado do fallback: %s", e)


def _save_fallback_state():
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_FALLBACK_STATE_FILE, "w") as f:
            json.dump({"enabled": _fallback_runtime}, f)
    except Exception as e:
        log.warning("nao consegui persistir o estado do fallback: %s", e)


# --- Lexico de correcao do ASR (stt_text) ------------------------------------
# O texto transcrito passa por stt_text.normalize_stt_text ANTES de sair daqui:
# e o unico ponto por onde os 3 clientes de voz (chat do desktop, notch, painel)
# passam. O lexico extra/keep-list e editavel em runtime pelo neo-panel e
# persistido em disco, igual ao fallback acima.
_LEXICON_STATE_FILE = os.path.join(_STATE_DIR, "stt_lexicon.json")


def _load_lexicon_state():
    try:
        with open(_LEXICON_STATE_FILE) as f:
            data = json.load(f)
        stt_text.set_runtime_config(lexicon=data.get("lexicon"), keep=data.get("keep"))
        log.info("lexico de STT carregado do disco (%d entradas, %d na keep-list)",
                 len(data.get("lexicon") or {}), len(data.get("keep") or []))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("nao consegui ler o lexico de STT: %s", e)


def _save_lexicon_state():
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        cfg = stt_text.get_runtime_config()
        with open(_LEXICON_STATE_FILE, "w") as f:
            json.dump({"lexicon": cfg["extra_lexicon"], "keep": cfg["keep"]}, f)
    except Exception as e:
        log.warning("nao consegui persistir o lexico de STT: %s", e)


# --- Fallback de TTS via Kokoro (modelo local, roda na CPU do container) ------
# Quando os engines de TTS do PC gamer estao inalcancaveis (tunnel/host offline),
# o neo-voice sintetiza localmente com o Kokoro-82M. Espelha o fallback de STT.
TTS_FALLBACK = os.environ.get("NEO_TTS_FALLBACK", "1").lower() not in ("0", "false", "no")
KOKORO_VOICE = os.environ.get("NEO_TTS_KOKORO_VOICE", "pf_dora")  # pt-BR feminina
KOKORO_LANG = os.environ.get("NEO_TTS_KOKORO_LANG", "pt-br")      # idioma espeak (kokoro-onnx)
KOKORO_SPEED = float(os.environ.get("NEO_TTS_KOKORO_SPEED", "1.0"))
KOKORO_MODEL_PATH = os.environ.get("NEO_TTS_KOKORO_MODEL", "/app/models/kokoro-v1.0.onnx")
KOKORO_VOICES_PATH = os.environ.get("NEO_TTS_KOKORO_VOICES", "/app/models/voices-v1.0.bin")
KOKORO_SR = 24000                                                 # Kokoro gera em 24kHz
# read timeout POR CHUNK da sintese no engine. 20s cobre com folga um chunk de
# ~200 chars em qualquer engine saudavel (Qwen/F5 ~2-8s); 60s so servia p/ segurar
# o usuario em silencio quando o engine travava (WSL portproxy / GPU ocupada).
TTS_ENGINE_READ_TIMEOUT = float(os.environ.get("NEO_TTS_ENGINE_TIMEOUT", "20"))
# hedge de 1o audio (WS): se o engine nao entregou o 1o chunk em N s, o Kokoro e
# disparado EM PARALELO e a resposta sai por quem chegar primeiro (fonte sticky).
# Limita o pior silencio inicial a ~hedge+1-2s em vez do read timeout inteiro.
TTS_FIRST_AUDIO_HEDGE = float(os.environ.get("NEO_TTS_FIRST_AUDIO_HEDGE", "6"))
# ping rapido de liveness ANTES de sintetizar: um engine morto-que-aceita-conexao
# (portproxy WSL up, backend down) trava a sintese ate o read timeout. O /health
# com timeout curto deixa cair no Kokoro em ~2s em vez de esperar 12-60s.
TTS_PROBE_TIMEOUT = float(os.environ.get("NEO_TTS_PROBE_TIMEOUT", "2.0"))
TTS_ENGINE_DOWN_COOLDOWN = float(os.environ.get("NEO_TTS_ENGINE_DOWN_COOLDOWN", "15"))
# tentativas extras na sintese de UM chunk antes de desistir do engine: absorve blips
# transitorios (jitter no portproxy/tunnel, engine ocupado) sem trocar a voz no meio.
TTS_ENGINE_RETRIES = int(os.environ.get("NEO_TTS_ENGINE_RETRIES", "2"))


def _env_json(name: str, default: dict) -> dict:
    """dict de config por backend via env (JSON); merge raso sobre o default,
    p/ sobrescrever um backend sem redigitar os outros."""
    try:
        v = json.loads(os.environ[name])
        return {**default, **v} if isinstance(v, dict) else default
    except (KeyError, ValueError):
        return default


# --- Rampa de tamanho dos trechos do /ws/tts ---------------------------------
# O gap entre trechos acontece quando a sintese do trecho N+1 demora mais que o
# audio ainda nao tocado no cliente. O tamanho de cada trecho e decidido na hora
# (pack_next_chunk) a partir da margem de buffer real e do RTF do engine — medido
# por chunk (EMA) e semeado pelos valores abaixo. Ver _RampState.
# Medidos na RTX 5070 Ti (2026-07-12): qwen 1.89 (mais LENTO que tempo real — nao
# sustenta streaming sem prebuffer), chatterbox ~0.57, coqui ~0.14.
TTS_RTF_SEED = _env_json("NEO_TTS_RTF", {
    "coqui": 0.14, "chatterbox": 0.57, "qwen": 1.9, "cosyvoice": 0.5,
    "f5": 0.2, "kokoro": 0.4, "*": 0.5,
})
# custo fixo por chamada de sintese (s): conexao + preamble do modelo. No Qwen e
# desprezivel (0.3s medido) — o custo dele e todo proporcional ao audio.
TTS_SYNTH_OVERHEAD = _env_json("NEO_TTS_SYNTH_OVERHEAD", {"chatterbox": 1.2, "qwen": 0.3, "*": 0.6})
# teto de chars por trecho. Chatterbox: <= NEO_CB_MAXCHARS (200) do engine, senao
# ele re-fatia internamente e injeta 120ms de silencio entre os pedacos.
TTS_CHUNK_MAX = _env_json("NEO_TTS_CHUNK_MAX", {"chatterbox": 200, "coqui": 250, "*": 200})
# fala pt-BR ~14 chars/s: converte duracao de audio em budget de chars.
TTS_CHARS_PER_SEC = float(os.environ.get("NEO_TTS_CHARS_PER_SEC", "14"))
# fracao da margem de buffer que o proximo trecho pode gastar sintetizando (o
# resto e colchao p/ jitter de rede e o relogio do cliente andar diferente).
TTS_RAMP_SAFETY = float(os.environ.get("NEO_TTS_RAMP_SAFETY", "0.7"))
# tamanho minimo do 1o trecho: prompts minusculos ("Ola!") fazem os modelos
# autoregressivos engolirem os primeiros fonemas por falta de contexto.
TTS_FIRST_MIN_CHARS = int(os.environ.get("NEO_TTS_FIRST_MIN_CHARS", "20"))
# silencio prepended ao 1o chunk (ms): o agendamento do cliente (resume() do
# AudioContext, latencia do grafo) come os primeiros ms — que viram zeros, nao
# fonema. Todos os clientes (webview, Swift, painel) se beneficiam de graca.
TTS_ONSET_PAD_MS = float(os.environ.get("NEO_TTS_ONSET_PAD_MS", "60"))
# sr anunciado na msg `start` (o real de cada chunk vai no proprio chunk; o notch
# Swift usa o do start p/ montar o player antes do 1o audio).
TTS_START_SR = _env_json("NEO_TTS_START_SR", {"dia": 44100, "*": 24000})
# Colchao p/ engines mais lentos que tempo real (RTF>1, ex.: Qwen ~1.9): segura o
# 1o chunk ate ter audio suficiente p/ a fala sair inteira, em vez de picotar a
# cada trecho. Custa espera no inicio; 0/false volta ao streaming imediato.
TTS_PREBUFFER = os.environ.get("NEO_TTS_PREBUFFER", "1").lower() not in ("0", "false", "no")
TTS_PREBUFFER_SAFETY = float(os.environ.get("NEO_TTS_PREBUFFER_SAFETY", "1.15"))
# Folga sobre o tempo ESPERADO de sintese do 1o trecho antes de correr o Kokoro em
# paralelo. O hedge fixo de 6s condenava todo engine lento (o Qwen leva 7-9s e
# perdia sempre a corrida, roubando a voz escolhida pelo usuario); agora o prazo
# acompanha o que o engine realmente deve demorar — um engine TRAVADO ainda estoura.
TTS_HEDGE_SLACK = float(os.environ.get("NEO_TTS_HEDGE_SLACK", "1.6"))
# Liga/desliga o hedge inteiro (toggle do neo-panel, `/tts/hedge`). DESLIGADO = a voz
# escolhida toca "a todo custo": o Kokoro nunca corre em paralelo p/ ganhar por
# lentidao. O fallback por FALHA (engine 500/offline) continua valendo — quem
# desliga isso e o toggle `/tts/fallback`.
TTS_HEDGE = os.environ.get("NEO_TTS_HEDGE", "1").lower() not in ("0", "false", "no")
# Backoff das re-tentativas no modo "a todo custo" (hedge desligado): 1s, 2s, 4s...
# ate o teto. Sem limite de tentativas — so o cliente desistindo encerra.
TTS_RETRY_BACKOFF = float(os.environ.get("NEO_TTS_RETRY_BACKOFF", "1.0"))
TTS_RETRY_BACKOFF_MAX = float(os.environ.get("NEO_TTS_RETRY_BACKOFF_MAX", "15"))
# "A todo custo" com subdivisao: apos SPLIT_AFTER tentativas falhas no trecho
# inteiro, parte o trecho ao meio (em virgula/espaco) e sintetiza cada metade
# SEPARADAMENTE, verificando uma por uma. Um trecho que o engine se recusa a
# gerar (500 deterministico por tamanho/conteudo) deixa de condenar a resposta
# toda: so o pedaco irrecuperavel — menor que SPLIT_FLOOR chars — e pulado.
TTS_SPLIT_AFTER = int(os.environ.get("NEO_TTS_SPLIT_AFTER", "2"))
TTS_SPLIT_FLOOR = int(os.environ.get("NEO_TTS_SPLIT_FLOOR", "40"))
TTS_SPLIT_JOIN_MS = float(os.environ.get("NEO_TTS_SPLIT_JOIN_MS", "60"))
_tts_hedge_runtime = None     # Optional[bool]; override do painel (None = env)
_TTS_HEDGE_STATE_FILE = os.path.join(_STATE_DIR, "tts_hedge.json")
_tts_engine_down_until = 0.0
_tts_fallback_runtime = None  # Optional[bool]; override do painel (None = env)
_TTS_FALLBACK_STATE_FILE = os.path.join(_STATE_DIR, "tts_fallback.json")
_kokoro = None            # instancia Kokoro (kokoro-onnx, lazy)
_kokoro_ok = None         # None=nao testado, True/False=importavel


def _kokoro_importable() -> bool:
    global _kokoro_ok
    if _kokoro_ok is None:
        try:
            import kokoro_onnx  # noqa: F401
            _kokoro_ok = True
        except Exception as e:
            _kokoro_ok = False
            log.warning("Kokoro indisponivel (import falhou): %s", e)
    return _kokoro_ok


def _tts_fallback_enabled() -> bool:
    return TTS_FALLBACK if _tts_fallback_runtime is None else _tts_fallback_runtime


def _tts_fallback_available() -> bool:
    """So usa Kokoro se o fallback estiver LIGADO, o pacote kokoro carregar E o hedge
    NAO estiver desligado.

    Hedge desligado significa "a voz escolhida a todo custo": nem lentidao, nem falha
    do engine, nem engine offline podem trocar a voz. Como esta funcao guarda TODOS os
    caminhos que levam ao Kokoro (hedge, fallback por chunk, rota HTTP /tts), desligar
    o hedge fecha a porta em todos eles de uma vez."""
    return bool(_tts_hedge_enabled() and _tts_fallback_enabled() and _kokoro_importable())


def _load_tts_fallback_state():
    global _tts_fallback_runtime
    try:
        with open(_TTS_FALLBACK_STATE_FILE) as f:
            v = json.load(f).get("enabled")
        if isinstance(v, bool):
            _tts_fallback_runtime = v
            log.info("fallback TTS (Kokoro): override carregado do disco = %s", v)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("nao consegui ler o estado do fallback TTS: %s", e)


def _save_tts_fallback_state():
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_TTS_FALLBACK_STATE_FILE, "w") as f:
            json.dump({"enabled": _tts_fallback_runtime}, f)
    except Exception as e:
        log.warning("nao consegui persistir o estado do fallback TTS: %s", e)


def _tts_hedge_enabled() -> bool:
    """Trocar a voz pelo Kokoro quando o engine escolhido esta DEMORANDO?

    DESLIGADO = a voz escolhida toca a todo custo: esperamos o engine, por mais
    lento que ele seja (o prebuffer segura a fala p/ ela sair inteira). Nao mexe no
    fallback por FALHA — engine 500/offline continua caindo no Kokoro enquanto o
    toggle `/tts/fallback` estiver ligado."""
    return TTS_HEDGE if _tts_hedge_runtime is None else _tts_hedge_runtime


def _load_tts_hedge_state():
    global _tts_hedge_runtime
    try:
        with open(_TTS_HEDGE_STATE_FILE) as f:
            v = json.load(f).get("enabled")
        if isinstance(v, bool):
            _tts_hedge_runtime = v
            log.info("hedge de TTS: override carregado do disco = %s", v)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("nao consegui ler o estado do hedge de TTS: %s", e)


def _save_tts_hedge_state():
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_TTS_HEDGE_STATE_FILE, "w") as f:
            json.dump({"enabled": _tts_hedge_runtime}, f)
    except Exception as e:
        log.warning("nao consegui persistir o estado do hedge de TTS: %s", e)


def _kokoro_synth(text: str) -> tuple:
    """Sintetiza `text` com kokoro-onnx local -> (pcm_int16_bytes, sr). BLOQUEANTE
    (CPU/ONNX): chamar via asyncio.to_thread p/ nao travar o event loop. Usa sempre a
    voz/idioma configurados (a voz do request e do engine do PC gamer, nao mapeia)."""
    global _kokoro
    from kokoro_onnx import Kokoro
    if _kokoro is None:
        _kokoro = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
    samples, sr = _kokoro.create(text, voice=KOKORO_VOICE, speed=KOKORO_SPEED, lang=KOKORO_LANG)
    if samples is None or len(samples) == 0:
        return b"", sr or KOKORO_SR
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    return pcm, int(sr or KOKORO_SR)
SILENCE_SEC = float(os.environ.get("VAD_SILENCE_SEC", "0.8"))
MIN_SPEECH_SEC = float(os.environ.get("VAD_MIN_SPEECH_SEC", "0.3"))
MAX_UTTER_SEC = float(os.environ.get("VAD_MAX_UTTER_SEC", "28"))  # limite do Parakeet (~30s)
PREROLL_SEC = float(os.environ.get("VAD_PREROLL_SEC", "0.2"))     # captura o ataque da fala (onset)
PARTIAL_SEC = float(os.environ.get("VAD_PARTIAL_SEC", "0.6"))     # intervalo entre transcricoes parciais

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

app = FastAPI(title="neo-voice service", version="1.0")
_http = httpx.AsyncClient(timeout=120.0)

# --- Silero VAD via onnxruntime (sem torch) ---------------------------------
_vad = None
_vad_window = 512  # amostras por frame para 16kHz (Silero v5)


def _load_vad():
    global _vad
    from silero_vad import load_silero_vad
    _vad = load_silero_vad(onnx=False)  # modelo torch JIT (torch ja e dep do silero-vad)
    log.info("Silero VAD (torch) carregado")


def _engine_headers(extra=None):
    h = dict(extra or {})
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


# --- Monitor de alcancabilidade dos engines (tabela de status em RAM) --------
# Um poller em background (engine_monitor.py) pinga o /health de cada engine local
# e mantem up/down/unknown por engine. STT/TTS LEEM essa tabela em O(1) e, quando o
# primario esta DOWN, vao DIRETO pro fallback sem pagar o connect/read timeout. Sem
# isto, cada request redescobria a queda na marra (lento, repetido). A tabela e
# construida no start (1a varredura no boot) e vive so em RAM.
MONITOR_ENABLED = os.environ.get("NEO_VOICE_MONITOR", "1").lower() not in ("0", "false", "no")
MONITOR_INTERVAL = float(os.environ.get("NEO_VOICE_MONITOR_INTERVAL", "4"))
MONITOR_PROBE_TIMEOUT = float(os.environ.get("NEO_VOICE_MONITOR_PROBE_TIMEOUT", "2"))
MONITOR_FAIL_THRESHOLD = int(os.environ.get("NEO_VOICE_MONITOR_FAIL_THRESHOLD", "2"))

# nome logico -> (url, grupo, chave do corpo no /health, chave da url no /health).
# Os nomes de TTS batem com as keys de TTS_BACKENDS (usados por tts_url/backend).
_MONITOR_ENGINES = [
    EngineState("stt", ENGINE_URL, "stt", "engine", "engine_url"),
    *([EngineState("stt_cpu", CPU_ENGINE_URL, "stt", "stt_cpu", "stt_cpu_engine_url")]
      if CPU_ENGINE_URL else []),
    EngineState("qwen", TTS_ENGINE_URL, "tts", "tts", "tts_engine_url"),
    EngineState("coqui", COQUI_ENGINE_URL, "tts", "coqui", "coqui_engine_url"),
    EngineState("dia", DIA_ENGINE_URL, "tts", "dia", "dia_engine_url"),
    EngineState("cosyvoice", COSYVOICE_ENGINE_URL, "tts", "cosyvoice", "cosyvoice_engine_url"),
    EngineState("f5", F5_ENGINE_URL, "tts", "f5", "f5_engine_url"),
    EngineState("chatterbox", CHATTERBOX_ENGINE_URL, "tts", "chatterbox", "chatterbox_engine_url"),
]
monitor = EngineHealthMonitor(
    _http, _MONITOR_ENGINES, headers_fn=_engine_headers,
    interval=MONITOR_INTERVAL, probe_timeout=MONITOR_PROBE_TIMEOUT,
    fail_threshold=MONITOR_FAIL_THRESHOLD,
)


async def _engine_transcribe_local(pcm_int16: bytes, sr: int, base_url: str = "",
                                   source: str = "local") -> dict:
    """Transcreve num engine local (Parakeet). Levanta se inacessivel."""
    r = await _http.post(
        f"{base_url or ENGINE_URL}/transcribe",
        content=pcm_int16,
        headers=_engine_headers({
            "Content-Type": "application/octet-stream",
            "X-Sample-Rate": str(sr),
        }),
        timeout=httpx.Timeout(ENGINE_READ_TIMEOUT, connect=ENGINE_CONNECT_TIMEOUT),
    )
    r.raise_for_status()
    data = r.json()
    data["source"] = source  # "local" (PC gamer) ou "local-cpu" (LXC) p/ logs/health
    return data


def _pcm_to_wav_bytes(pcm_int16: bytes, sr: int) -> bytes:
    """Embrulha PCM int16 cru num container WAV (o OpenRouter exige arquivo de audio)."""
    audio = np.frombuffer(pcm_int16, dtype=np.int16)
    bio = io.BytesIO()
    sf.write(bio, audio, sr, format="WAV", subtype="PCM_16")
    return bio.getvalue()


async def _engine_transcribe_openrouter(pcm_int16: bytes, sr: int) -> dict:
    """Fallback: transcreve via OpenRouter (mesmo modelo Parakeet, na nuvem)."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY ausente — sem fallback de STT")
    b64 = base64.b64encode(_pcm_to_wav_bytes(pcm_int16, sr)).decode("ascii")
    r = await _http.post(
        OPENROUTER_STT_URL,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": OPENROUTER_STT_MODEL, "input_audio": {"data": b64, "format": "wav"}},
        timeout=OPENROUTER_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    data.setdefault("source", "openrouter")  # marca a origem (o local nao envia isto)
    log.info("STT via OpenRouter (FALLBACK) modelo=%s -> %r",
             OPENROUTER_STT_MODEL, (data.get("text") or "").strip()[:80])
    return data


def _local_stt_tiers() -> list[tuple[str, str, str]]:
    """Engines locais em ordem de preferencia: (nome no monitor, url, rotulo da origem).
    A GPU do PC gamer vem primeiro; o LXC so-CPU e a rede de seguranca local."""
    tiers = [("stt", ENGINE_URL, "local")]
    if CPU_ENGINE_URL:
        tiers.append(("stt_cpu", CPU_ENGINE_URL, "local-cpu"))
    return tiers


async def engine_transcribe_pcm(pcm_int16: bytes, sr: int = SAMPLE_RATE, partial: bool = False) -> dict:
    """STT em cascata: PC gamer (GPU) -> LXC parakeet-cpu -> OpenRouter (so finais).

    Roteamento pela tabela do monitor: um engine marcado DOWN nem e' tentado (0ms), sem
    pagar o connect/read timeout do host morto a cada parcial/final. Um sucesso/falha
    real aqui realimenta a tabela na hora. O engine de CPU e local e gratuito, entao
    serve tambem as parciais -- so a nuvem e' que fica reservada aos finais."""
    global _engine_down_until
    err = None
    for name, url, source in _local_stt_tiers():
        # _engine_down_until segue significando "a GPU do PC gamer esta fora" — e' o que
        # /health e /stt/fallback expoem como engine_down. Por isso e' armado no tier da
        # GPU, mesmo quando o engine de CPU salva o request logo abaixo.
        if not monitor.should_try(name):
            if name == "stt":
                _engine_down_until = time.time() + ENGINE_DOWN_COOLDOWN
            continue
        try:
            res = await _engine_transcribe_local(pcm_int16, sr, url, source)
            monitor.record_request(name, True)
            if name == "stt":
                _engine_down_until = 0.0  # a GPU respondeu
            else:
                log.info("STT via engine de CPU (%s) — PC gamer indisponivel", url)
            return res
        except Exception as e:
            err = e
            monitor.record_request(name, False, e)  # marca down na hora (sinal real)
            if name == "stt":
                _engine_down_until = time.time() + ENGINE_DOWN_COOLDOWN
            log.warning("STT engine %s (%s) indisponivel: %s", name, url, e)

    # nenhum engine local atendeu (falhou agora ou ja estava DOWN na tabela)
    if not _fallback_available():
        why = "sem chave OpenRouter" if not OPENROUTER_API_KEY else "fallback DESLIGADO no painel"
        if err is None:
            raise RuntimeError(f"engines locais de STT indisponiveis e {why}")
        raise err
    # parciais sao so legenda ao vivo: nao vale o custo/latencia de mandar pro OpenRouter.
    if partial and not STT_FALLBACK_PARTIALS:
        return {"text": "", "source": "openrouter-skip"}
    return await _engine_transcribe_openrouter(pcm_int16, sr)


async def call_neo_core(text: str) -> str | None:
    if not NEO_CORE_URL:
        return None
    try:
        r = await _http.post(NEO_CORE_URL, json={"message": text})
        r.raise_for_status()
        data = r.json()
        return data.get("neo") or data.get("response") or json.dumps(data)
    except Exception as e:
        log.warning("neo-core falhou: %s", e)
        return None


@app.on_event("startup")
def _startup():
    _load_vad()
    _load_fallback_state()
    _load_tts_fallback_state()
    _load_tts_hedge_state()
    _load_lexicon_state()
    # Pre-aquece o Kokoro em BACKGROUND (construir o KPipeline custa ~5s). Sem isso,
    # a 1a resposta de TTS no fallback pagaria esse pedago. Roda em thread daemon p/
    # nao atrasar o boot do servidor.
    if _tts_fallback_available():
        import threading

        def _warm_kokoro():
            try:
                t0 = time.time()
                _kokoro_synth("aquecendo a voz")
                log.info("Kokoro pre-aquecido em %.1fs", time.time() - t0)
            except Exception as e:
                log.warning("falha ao pre-aquecer Kokoro: %s", e)

        threading.Thread(target=_warm_kokoro, daemon=True).start()


@app.on_event("startup")
async def _start_monitor():
    """Sobe o poller da tabela de status dos engines (constroi a tabela no boot)."""
    if MONITOR_ENABLED:
        monitor.start()
    else:
        log.info("monitor de engines DESLIGADO (NEO_VOICE_MONITOR=0) — roteamento por tentativa/timeout")


@app.on_event("shutdown")
async def _stop_monitor():
    await monitor.stop()


class FallbackReq(BaseModel):
    enabled: bool


@app.get("/tts/fallback")
async def get_tts_fallback():
    """Estado do fallback de TTS (Kokoro local) — lido pelo neo-panel."""
    return {
        "enabled": _tts_fallback_enabled(),
        "available": _tts_fallback_available(),
        "kokoro_ok": _kokoro_importable(),
        "voice": KOKORO_VOICE,
        "lang": KOKORO_LANG,
        "env_default": TTS_FALLBACK,
        "runtime_override": _tts_fallback_runtime,
        "engine_down": time.time() < _tts_engine_down_until,
    }


@app.post("/tts/fallback")
async def set_tts_fallback(req: FallbackReq):
    """Liga/desliga o fallback Kokoro em runtime (controle do neo-panel)."""
    global _tts_fallback_runtime
    _tts_fallback_runtime = bool(req.enabled)
    _save_tts_fallback_state()
    log.info("fallback TTS (Kokoro) %s pelo painel", "LIGADO" if _tts_fallback_runtime else "DESLIGADO")
    return await get_tts_fallback()


@app.get("/tts/hedge")
async def get_tts_hedge():
    """Estado do hedge de TTS (trocar a voz quando o engine demora) — neo-panel."""
    return {
        "enabled": _tts_hedge_enabled(),
        "env_default": TTS_HEDGE,
        "runtime_override": _tts_hedge_runtime,
        "first_audio_hedge_s": TTS_FIRST_AUDIO_HEDGE,
    }


@app.post("/tts/hedge")
async def set_tts_hedge(req: FallbackReq):
    """Liga/desliga o hedge em runtime. DESLIGADO = usa a voz escolhida a todo custo
    (espera o engine, por mais lento que seja)."""
    global _tts_hedge_runtime
    _tts_hedge_runtime = bool(req.enabled)
    _save_tts_hedge_state()
    log.info("hedge de TTS %s pelo painel%s", "LIGADO" if _tts_hedge_runtime else "DESLIGADO",
             "" if _tts_hedge_runtime else " — a voz escolhida sera usada a todo custo")
    return await get_tts_hedge()


@app.get("/stt/fallback")
async def get_stt_fallback():
    """Estado do fallback de STT (OpenRouter) — lido pelo neo-panel."""
    return {
        "enabled": _fallback_enabled(),         # o toggle (env ou override do painel)
        "available": _fallback_available(),     # toggle E chave presente
        "openrouter_key": bool(OPENROUTER_API_KEY),
        "model": OPENROUTER_STT_MODEL,
        "env_default": STT_FALLBACK,
        "runtime_override": _fallback_runtime,
        "partials": STT_FALLBACK_PARTIALS,
        "engine_down": time.time() < _engine_down_until,
    }


@app.post("/stt/fallback")
async def set_stt_fallback(req: FallbackReq):
    """Liga/desliga o fallback OpenRouter em runtime (controle do neo-panel)."""
    global _fallback_runtime
    _fallback_runtime = bool(req.enabled)
    _save_fallback_state()
    log.info("fallback OpenRouter %s pelo painel", "LIGADO" if _fallback_runtime else "DESLIGADO")
    return await get_stt_fallback()


class LexiconReq(BaseModel):
    """`lexicon`: {variante: substituicao}. `keep`: palavras intocaveis.
    Campo ausente (None) mantem o valor atual; {} / [] limpam."""
    lexicon: dict | None = None
    keep: list | None = None


@app.get("/stt/lexicon")
async def get_stt_lexicon():
    """Estado do corretor de ASR (o que vira "Neo", o que nunca e tocado)."""
    return stt_text.get_runtime_config()


@app.post("/stt/lexicon")
async def set_stt_lexicon(req: LexiconReq):
    """Edita lexico/keep-list em runtime (neo-panel) e persiste em disco."""
    stt_text.set_runtime_config(lexicon=req.lexicon, keep=req.keep)
    _save_lexicon_state()
    cfg = stt_text.get_runtime_config()
    log.info("lexico de STT atualizado pelo painel: %d entradas, %d na keep-list",
             len(cfg["extra_lexicon"]), len(cfg["keep"]))
    return cfg


@app.post("/stt/normalize")
async def preview_stt_normalize(req: dict):
    """Dry-run do corretor num texto (debug/painel): mostra o que mudaria."""
    res = normalize_stt_text(str(req.get("text") or ""))
    return {"text": res.text, "raw_text": res.raw, "fixes": res.fixes, "dropped": res.dropped}


async def _engine_health_body(name: str, url: str) -> dict:
    """Corpo /health de um engine SERVIDO DA TABELA (sem re-pingar): UP -> ultimo
    /health cacheado (traz on_gpu/gpu que o painel usa); DOWN -> {error} (painel
    mostra 'offline'); UNKNOWN (monitor off/cold start) -> probe ao vivo. Assim o
    /health responde na hora em vez de sondar 7 engines em serie (antes >25s off)."""
    e = monitor.get(name)
    if e is not None and e.status == UP and e.body is not None:
        return e.body
    if e is not None and e.status == DOWN:
        return {"error": e.last_error or "sem resposta", "status": "down"}
    try:  # UNKNOWN: fallback p/ o probe ao vivo de antes
        r = await _http.get(f"{url}/health", headers=_engine_headers(), timeout=5)
        return r.json()
    except Exception as ex:
        return {"error": str(ex)}


@app.get("/engines/status")
async def engines_status():
    """Tabela de alcancabilidade dos engines (status em RAM do monitor). Reflete o
    que o roteamento de STT/TTS ve: up/down/unknown, latencia, ha quanto tempo, erro."""
    return {"enabled": MONITOR_ENABLED, "interval_s": MONITOR_INTERVAL,
            "probe_timeout_s": MONITOR_PROBE_TIMEOUT, "engines": monitor.snapshot()}


@app.get("/health")
async def health():
    global _engine_down_until
    engine = await _engine_health_body("stt", ENGINE_URL)
    if monitor.is_up("stt"):
        _engine_down_until = 0.0  # engine respondeu: cura o circuit breaker
    stt_cpu = await _engine_health_body("stt_cpu", CPU_ENGINE_URL) if CPU_ENGINE_URL else None
    tts = await _engine_health_body("qwen", TTS_ENGINE_URL)
    coqui = await _engine_health_body("coqui", COQUI_ENGINE_URL)
    dia = await _engine_health_body("dia", DIA_ENGINE_URL)
    cosyvoice = await _engine_health_body("cosyvoice", COSYVOICE_ENGINE_URL)
    f5 = await _engine_health_body("f5", F5_ENGINE_URL)
    chatterbox = await _engine_health_body("chatterbox", CHATTERBOX_ENGINE_URL)
    return {"status": "ok", "engine_url": ENGINE_URL, "engine": engine,
            "stt_cpu_engine_url": CPU_ENGINE_URL or None, "stt_cpu": stt_cpu,
            "tts_engine_url": TTS_ENGINE_URL, "tts": tts,
            "coqui_engine_url": COQUI_ENGINE_URL, "coqui": coqui,
            "dia_engine_url": DIA_ENGINE_URL, "dia": dia,
            "cosyvoice_engine_url": COSYVOICE_ENGINE_URL, "cosyvoice": cosyvoice,
            "f5_engine_url": F5_ENGINE_URL, "f5": f5,
            "chatterbox_engine_url": CHATTERBOX_ENGINE_URL, "chatterbox": chatterbox,
            "engines_status": monitor.snapshot(),
            "neo_core": bool(NEO_CORE_URL),
            "stt_fallback": {
                "enabled": _fallback_enabled(),
                "available": _fallback_available(),
                "model": OPENROUTER_STT_MODEL,
                "openrouter_key": bool(OPENROUTER_API_KEY),
                "runtime_override": _fallback_runtime,
                "env_default": STT_FALLBACK,
                "partials": STT_FALLBACK_PARTIALS,
                "engine_down": time.time() < _engine_down_until,
            },
            "tts_fallback": {
                "enabled": _tts_fallback_enabled(),
                "available": _tts_fallback_available(),
                "kokoro_ok": _kokoro_importable(),
                "voice": KOKORO_VOICE,
                "lang": KOKORO_LANG,
                "runtime_override": _tts_fallback_runtime,
                "env_default": TTS_FALLBACK,
                "engine_down": time.time() < _tts_engine_down_until,
            },
            "tts_hedge": {
                "enabled": _tts_hedge_enabled(),
                "runtime_override": _tts_hedge_runtime,
                "env_default": TTS_HEDGE,
            }}


# --- TTS (proxy p/ os engines; backend selecionavel: qwen | coqui) ----------
class TTSReq(BaseModel):
    text: str
    voice: Optional[str] = None
    language: Optional[str] = None
    instruct: Optional[str] = None
    backend: Optional[str] = None


@app.get("/voices")
async def voices(backend: str = "qwen"):
    try:
        r = await _http.get(f"{tts_url(backend)}/voices", headers=_engine_headers(), timeout=10)
        return JSONResponse(r.json())
    except Exception as e:
        raise HTTPException(502, f"engine TTS '{backend}' indisponivel: {e}")


@app.post("/clone")
async def clone(file: UploadFile = File(...), name: str = Form(None), backend: str = Form("coqui")):
    """Encaminha um audio de referencia para o engine clonar a voz (Coqui XTTS)."""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "audio vazio")
    try:
        r = await _http.post(
            f"{tts_url(backend)}/clone",
            files={"file": (file.filename or "ref.wav", raw, file.content_type or "audio/wav")},
            data={"name": name or ""},
            headers=_engine_headers(),
            timeout=120,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:
        raise HTTPException(502, f"clone falhou: {e}")
    return JSONResponse(r.json())


@app.post("/tts")
async def tts(req: TTSReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "texto vazio")
    text = normalize_tts_text(text, req.language)
    try:
        pcm, sr, infer_ms, source = await engine_tts_pcm(
            text, req.voice, req.language, req.instruct, req.backend or "qwen")
    except Exception as e:
        raise HTTPException(502, f"TTS falhou (engine e fallback): {e}")
    bio = io.BytesIO()
    sf.write(bio, np.frombuffer(pcm, dtype=np.int16), sr, format="WAV", subtype="PCM_16")
    return Response(content=bio.getvalue(), media_type="audio/wav",
                    headers={"X-Infer-Ms": str(infer_ms), "X-TTS-Source": source})


# --- TTS streaming por frases (WS /ws/tts) ----------------------------------
def _cut_at_clause(s: str, budget: int):
    """Corta `s` perto de `budget` chars preferindo virgula/ponto-e-virgula (pausa
    natural na fala) e caindo p/ espaco -> (cabeca, resto|None). Sem onde cortar,
    devolve a sentenca inteira."""
    window = s[:budget]
    cut = max(window.rfind(","), window.rfind(";"))
    if cut < budget // 3:  # virgula cedo demais desperdicaria o budget
        cut = window.rfind(" ")
    if cut <= 0:
        return s, None
    head, tail = s[: cut + 1].strip(), s[cut + 1:].strip()
    return (head, tail or None) if head else (s, None)


def _split_long(s: str, hard_max: int):
    """Re-quebra uma sentenca > hard_max em pedacos <= hard_max (virgula > espaco).
    Uma sentenca gigante sem pontuacao interna seguraria o stream por mais do que
    o read-timeout do engine tolera."""
    out = []
    while len(s) > hard_max:
        head, tail = _cut_at_clause(s, hard_max)
        if tail is None:
            break  # bloco indivisivel (ex.: URL enorme): segue inteiro
        out.append(head)
        s = tail
    out.append(s)
    return out


def tokenize_sentences(text: str, min_len: int = 14, hard_max: int = 300):
    """Fatia o texto nas sentencas 'atomicas' que o packer do /ws/tts consome:
    quebra por fim de sentenca, junta fragmentos minusculos ao proximo (o 1o com um
    minimo maior — prompts curtos fazem os modelos engolirem os primeiros fonemas) e
    re-quebra sentencas gigantes em virgula/espaco."""
    text = (text or "").strip()
    if not text:
        return []
    sents = [p.strip() for p in re.split(r"(?<=[\.\!\?…])\s+|\n+", text) if p.strip()]
    tmp, i = [], 0
    while i < len(sents):
        cur = sents[i]
        need = max(min_len, TTS_FIRST_MIN_CHARS) if not tmp else min_len
        while len(cur) < need and i + 1 < len(sents):
            i += 1
            cur = (cur + " " + sents[i]).strip()
        tmp.append(cur)
        i += 1
    sents = tmp or [text]
    out = []
    for s in sents:
        out.extend(_split_long(s, hard_max))
    return out


def pack_next_chunk(pending: deque, budget_chars: int, cap_chars: int, allow_cut: bool) -> str:
    """Monta o proximo trecho consumindo sentencas da frente de `pending` ate encher
    `budget_chars` (0 = nao junta: usa so a proxima sentenca).

    Dois cortes, com motivos diferentes:
      - CAP (duro, sempre): uma sentenca acima do teto leva mais que o read timeout
        p/ sintetizar num engine lento — a chamada estoura e a resposta cai no
        fallback. Corta em qualquer ponto do stream, inclusive no 1o trecho.
      - BUDGET (macio, so no comeco da rampa): enquanto a margem de buffer e pequena,
        uma sentenca muito maior que o budget abriria um gap; corta em virgula. Depois
        que o buffer cresce, deixa a sentenca inteira (prosodia melhor).
    O resto sempre volta p/ a frente da fila."""
    cur = pending.popleft()
    if len(cur) > cap_chars:
        head, tail = _cut_at_clause(cur, cap_chars)
        if tail:
            pending.appendleft(tail)
            return head
    elif allow_cut and len(cur) > budget_chars * 1.5 and budget_chars >= 40:
        head, tail = _cut_at_clause(cur, budget_chars)
        if tail:
            pending.appendleft(tail)
            return head
    while pending and len(cur) + 1 + len(pending[0]) <= budget_chars:
        cur += " " + pending.popleft()
    return cur


class _RampState:
    """Rampa de tamanho dos trechos do /ws/tts, guiada pela margem de buffer real.

    margem = audio ja entregue - tempo decorrido desde o 1o envio (aproximacao
    conservadora do playhead do cliente pelo relogio do servidor). O proximo trecho
    pode gastar TTS_RAMP_SAFETY dessa margem sintetizando; dividido pelo RTF vira
    duracao de audio, vezes TTS_CHARS_PER_SEC vira budget de chars. O RTF e MEDIDO
    a cada chunk (EMA 50/50), semeado por backend (NEO_TTS_RTF) e re-semeado quando
    a fonte muda (engine->kokoro tem regime de velocidade proprio)."""

    def __init__(self, backend: str):
        self.rtf_ema = float(TTS_RTF_SEED.get(backend, TTS_RTF_SEED["*"]))
        self.overhead = float(TTS_SYNTH_OVERHEAD.get(backend, TTS_SYNTH_OVERHEAD["*"]))
        self.cap = int(TTS_CHUNK_MAX.get(backend, TTS_CHUNK_MAX["*"]))
        self.produced_s = 0.0     # duracao total sintetizada
        self.chars_done = 0       # chars ja sintetizados (p/ medir a taxa de fala)
        self.delivered_s = 0.0    # duracao total ja enviada ao cliente
        self.t_first_send = None  # monotonic do 1o envio (playback ~comeca ai)
        self._last_source = None

    def observe(self, synth_wall: float, dur_s: float, source: str, chars: int = 0) -> None:
        """Realimenta o RTF (e a taxa de fala) com a sintese que acabou de acontecer."""
        if dur_s <= 0:
            return
        rtf = synth_wall / dur_s
        if source != self._last_source:
            self.rtf_ema = max(rtf, 0.05)
            self._last_source = source
            self.produced_s = self.chars_done = 0  # taxa de fala e por voz/engine
        else:
            self.rtf_ema = 0.5 * rtf + 0.5 * self.rtf_ema
        self.produced_s += dur_s
        self.chars_done += chars

    def speech_rate(self) -> float:
        """Chars por segundo de fala, MEDIDO (a voz/engine define o ritmo). Cai no
        default so antes do 1o chunk."""
        if self.chars_done >= 20 and self.produced_s > 0:
            return self.chars_done / self.produced_s
        return TTS_CHARS_PER_SEC

    def margin_s(self) -> float:
        """Audio entregue e ainda nao tocado. Antes do 1o envio, tudo que foi
        produzido e margem (o playback ainda nem comecou)."""
        if self.t_first_send is None:
            return self.produced_s
        return self.delivered_s - (time.monotonic() - self.t_first_send)

    def note_delivery(self, dur_s: float) -> float:
        """Registra o envio de um chunk; devolve a margem (s) apos este envio."""
        if self.t_first_send is None:
            self.t_first_send = time.monotonic()
        self.delivered_s += dur_s
        return self.margin_s()

    def cap_chars(self) -> int:
        """Teto de chars por trecho: o do backend, mas nunca tao grande que a
        sintese passe do read timeout. Um engine lento (RTF~1.9) leva 20s num
        trecho de 200 chars — bateria no NEO_TTS_ENGINE_TIMEOUT, que NAO e
        re-tentado, e a resposta cairia inteira no Kokoro."""
        by_timeout = int(0.6 * TTS_ENGINE_READ_TIMEOUT * TTS_CHARS_PER_SEC / max(self.rtf_ema, 0.05))
        return max(min(self.cap, by_timeout), 40)

    def budget_chars(self) -> int:
        """Chars que o proximo trecho pode ter sem furar a margem. Piso de 40
        (margem negativa = underrun em curso: trechos minimos recuperam rapido) e
        teto por backend/timeout."""
        budget_synth = TTS_RAMP_SAFETY * max(self.margin_s(), 0.5) - self.overhead
        dur_max = max(budget_synth, 1.0) / max(self.rtf_ema, 0.05)
        return max(min(int(dur_max * TTS_CHARS_PER_SEC), self.cap_chars()), 40)

    def expected_synth_s(self, chars: int) -> float:
        """Quanto a sintese de um trecho de `chars` deve demorar neste engine."""
        return self.overhead + self.rtf_ema * (chars / TTS_CHARS_PER_SEC)

    def prebuffer_s(self, total_chars: int) -> float:
        """Audio (s) a acumular ANTES de mandar o 1o chunk, p/ a fala nao picotar.

        Engine mais lento que tempo real (RTF>1) produz audio mais devagar do que o
        cliente consome: sem colchao, o playhead alcanca a sintese e a fala trava a
        cada trecho. Sendo T a duracao total da resposta e B o colchao, tocar sem
        buracos exige, em cada trecho i:  RTF*S_i <= B + S_i - d_i  (o trecho tem de
        estar pronto antes de o playhead chegar nele). O pior caso e o ULTIMO trecho:

            B >= T*(1 - 1/RTF) + d_ultimo/RTF

        O 2o termo e o que faltava numa 1a versao — sem ele o ultimo trecho (o mais
        caro de sintetizar) chegava atrasado e abria um buraco. `d_ultimo` e limitado
        pelo teto de chars, entao usamos o pior trecho possivel.

        RTF<=1 -> 0 (engine acompanha o playback: streaming imediato, sem espera)."""
        if not TTS_PREBUFFER or self.rtf_ema <= 1.0:
            return 0.0
        rate = self.speech_rate()
        total_s = total_chars / rate
        worst_chunk_s = self.cap_chars() / rate
        return TTS_PREBUFFER_SAFETY * (
            total_s * (1.0 - 1.0 / self.rtf_ema) + worst_chunk_s / self.rtf_ema
        )


async def _tts_engine_alive(backend, attempts: int = 1) -> bool:
    """Decide se o engine de TTS esta vivo. PRIMEIRO consulta a tabela do monitor
    (O(1), sem esperar rede): se DOWN -> False na hora (vai pro Kokoro sem probe); se
    UP (poller viu /health OK ha poucos segundos) -> True na hora. So quando UNKNOWN
    (cold start, ou monitor desligado) cai no probe ao vivo de antes.

    Bound em TTS_PROBE_TIMEOUT p/ nao travar quando o engine aceita conexao mas o
    backend (WSL) esta morto. `attempts` re-tenta o ping p/ absorver um /health lento
    transitorio: um falso-negativo jogaria a RESPOSTA INTEIRA no Kokoro (a fonte e
    decidida uma vez por stream)."""
    st = monitor.status(backend)
    if st == DOWN:
        return False
    if st == UP:
        return True
    # UNKNOWN -> probe ao vivo (semeia a decisao ate o poller cobrir esse backend)
    for attempt in range(max(1, attempts)):
        try:
            r = await _http.get(f"{tts_url(backend)}/health", headers=_engine_headers(),
                                timeout=httpx.Timeout(TTS_PROBE_TIMEOUT))
            if r.status_code == 200:
                return True
        except Exception:
            pass
        if attempt + 1 < attempts:
            await asyncio.sleep(0.3)
    return False


# Cada engine fala um "dialeto" de idioma diferente: o Qwen exige o NOME por extenso
# ("portuguese") e responde 500 ao codigo ISO ("pt"); Coqui/Chatterbox/F5 querem o ISO.
# O resto do sistema (neo-panel, neo-core, desktop) trafega ISO — entao a traducao mora
# aqui, a unica camada que conhece todos os engines. Sem isso, escolher uma voz clonada
# do Qwen no painel derrubava a sintese em 500 e a resposta saia no Kokoro (voz errada,
# em silencio).
_QWEN_LANGS = {
    "pt": "portuguese", "pt-br": "portuguese", "pt_br": "portuguese",
    "en": "english", "en-us": "english", "es": "spanish", "fr": "french",
    "de": "german", "it": "italian", "ja": "japanese", "ko": "korean",
    "ru": "russian", "zh": "chinese", "zh-cn": "chinese",
}


def engine_language(backend, language):
    """Codigo de idioma no dialeto do engine. None = deixa o engine escolher."""
    lang = (language or "").strip()
    if not lang:
        return None
    if (backend or "").lower() == "qwen":
        return _QWEN_LANGS.get(lang.lower(), lang)
    return lang


async def _engine_tts_pcm_remote(text, voice, language, instruct, backend):
    """TTS no engine do PC gamer (backend escolhido). Levanta se inacessivel."""
    r = await _http.post(
        f"{tts_url(backend)}/tts",
        json={"text": text, "voice": voice,
              "language": engine_language(backend, language), "instruct": instruct},
        headers=_engine_headers(),
        timeout=httpx.Timeout(TTS_ENGINE_READ_TIMEOUT, connect=ENGINE_CONNECT_TIMEOUT),
    )
    r.raise_for_status()
    data, sr = sf.read(io.BytesIO(r.content), dtype="int16")
    if data.ndim > 1:
        data = data[:, 0]
    return np.ascontiguousarray(data).tobytes(), int(sr), float(r.headers.get("X-Infer-Ms", 0))


def _tts_retryable(e: Exception) -> bool:
    """So re-tenta o que falha RAPIDO e pode ser blip (conexao recusada/reset, 5xx,
    erro de protocolo). Um ReadTimeout ja custou o budget inteiro do chunk — re-tentar
    seria segurar o usuario em silencio por 2-3x o timeout; melhor degradar logo.
    4xx e deterministico (payload/rota), repetir nao muda nada."""
    if isinstance(e, (httpx.ReadTimeout, httpx.PoolTimeout)):
        return False
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
        return False
    return True


async def _engine_tts_pcm_retry(text, voice, language, instruct, backend, retries=TTS_ENGINE_RETRIES):
    """Sintetiza UM chunk no engine, re-tentando alguns blips transitorios antes de
    desistir. Levanta a ultima excecao se todas as tentativas falharem — quem chama
    decide se degrada pro Kokoro. Mantem a voz consistente: um jitter momentaneo nao
    troca a fonte no meio da resposta."""
    last = None
    for attempt in range(retries + 1):
        try:
            return await _engine_tts_pcm_remote(text, voice, language, instruct, backend)
        except Exception as e:
            last = e
            if attempt < retries and _tts_retryable(e):
                log.warning("TTS engine (%s) blip %d/%d na sintese: %s — re-tentando",
                            backend, attempt + 1, retries, e)
                await asyncio.sleep(0.3)
            else:
                break
    raise last


async def _kokoro_chunk(text):
    """Sintetiza um chunk no Kokoro local -> mesma tupla do caminho engine."""
    pcm, sr = await asyncio.to_thread(_kokoro_synth, text)
    return pcm, sr, 0, "kokoro"


async def _engine_tts_pcm_forever(text, voice, language, instruct, backend, depth=0):
    """Modo "a todo custo" (hedge DESLIGADO no painel): sintetiza no engine escolhido
    e NUNCA cai no Kokoro — nem por lentidao, nem por erro, nem com o engine marcado
    DOWN pelo monitor. Insiste com backoff no MESMO engine.

    Depois de TTS_SPLIT_AFTER tentativas falhas, para de bater no trecho inteiro e
    SUBDIVIDE: parte no meio (virgula/espaco) e sintetiza cada metade separadamente,
    verificando uma por uma (ver _engine_tts_pcm_halves). Um 500 que so acontece
    naquele trecho — tamanho, uma sequencia que o modelo engasga — deixa de condenar
    o resto da resposta: o engine gera as partes que consegue e no maximo um pedaco
    atomico (< TTS_SPLIT_FLOOR chars) e pulado.

    Devolve pcm vazio quando nem subdividindo o engine gera aquele pedaco. Quem chama
    pula o trecho e segue a resposta — antes esse caso travava a fala p/ sempre,
    esperando um engine que nao ia voltar."""
    attempt = 0
    while True:
        try:
            pcm, sr, infer_ms = await _engine_tts_pcm_remote(text, voice, language, instruct, backend)
            # verificacao por etapa: 200 com corpo vazio e falha, nao sucesso — sem
            # isso um pedaco mudo passaria batido e sumiria da fala em silencio.
            if not pcm:
                raise RuntimeError("engine devolveu audio vazio")
            monitor.record_request(backend, True)
            if attempt:
                log.info("TTS [a todo custo] engine (%s) voltou na tentativa %d (%d chars)",
                         backend, attempt + 1, len(text))
            return pcm, sr, infer_ms, "engine"
        except asyncio.CancelledError:
            raise  # cliente desistiu: sai limpo
        except Exception as e:
            attempt += 1
            monitor.record_request(backend, False, e)  # o monitor reflete a realidade...
            if attempt >= TTS_SPLIT_AFTER and len(text) > TTS_SPLIT_FLOOR:
                log.warning("TTS [a todo custo] engine (%s) falhou %d vezes em %d chars: %r "
                            "— subdividindo o trecho", backend, attempt, len(text), e)
                return await _engine_tts_pcm_halves(text, voice, language, instruct,
                                                    backend, depth)
            if attempt >= TTS_SPLIT_AFTER:
                # ja e atomico (<= floor) e continua falhando: pular ESTE pedaco e o
                # unico jeito de a resposta seguir. Perder uma oracao curta e muito
                # melhor que travar o Modo Live esperando o engine voltar.
                log.error("TTS [a todo custo] engine (%s) nao gerou o pedaco atomico "
                          "(%d chars) apos %d tentativas: %r — PULANDO: %r",
                          backend, len(text), attempt, e, text[:80])
                return b"", 0, 0.0, "engine"
            delay = min(TTS_RETRY_BACKOFF_MAX, TTS_RETRY_BACKOFF * 2 ** min(attempt - 1, 5))
            log.warning("TTS [a todo custo] engine (%s) falhou (tentativa %d, %d chars): %r — "
                        "re-tentando em %.1fs (Kokoro proibido: hedge desligado)",
                        backend, attempt, len(text), e, delay)
            await asyncio.sleep(delay)  # ...mas aqui a gente ignora e tenta de novo


async def _engine_tts_pcm_halves(text, voice, language, instruct, backend, depth):
    """Parte `text` ao meio numa pausa natural e sintetiza cada metade por conta
    propria (recursivamente), emendando o audio. Cada metade e verificada sozinha: a
    que sair vazia e descartada e as outras seguem — a resposta perde no maximo o
    pedaco que o engine se recusa a gerar."""
    head, tail = _cut_at_clause(text, max(len(text) // 2, TTS_SPLIT_FLOOR))
    if tail is None:  # bloco indivisivel: nada a subdividir, trata como atomico
        log.error("TTS [a todo custo] trecho indivisivel de %d chars — PULANDO: %r",
                  len(text), text[:80])
        return b"", 0, 0.0, "engine"

    out, sr_out, infer_total, ok, skipped = [], 0, 0.0, 0, 0
    for part in (head, tail):
        pcm, sr, infer_ms, _ = await _engine_tts_pcm_forever(
            part, voice, language, instruct, backend, depth + 1)
        if not pcm:
            skipped += len(part)  # pedaco irrecuperavel, ja logado la dentro
            continue
        sr_out = sr_out or sr
        infer_total += infer_ms
        if ok and TTS_SPLIT_JOIN_MS > 0:
            # emendar duas sinteses independentes estala; o corte foi em virgula/
            # espaco, entao uma pausa curta ali soa como a pausa que ja existiria.
            out.append(b"\x00\x00" * int(sr_out * TTS_SPLIT_JOIN_MS / 1000.0))
        out.append(pcm)
        ok += 1
    if not out:
        return b"", 0, 0.0, "engine"
    log.info("TTS [a todo custo] trecho de %d chars sintetizado em %d parte(s) "
             "(nivel %d, %d chars perdidos)", len(text), ok, depth + 1, skipped)
    return b"".join(out), sr_out, infer_total, "engine"


async def _first_chunk_hedged(text, voice, language, instruct, backend, deadline=None):
    """1o chunk do stream com hedge de latencia: o engine tem TTS_FIRST_AUDIO_HEDGE
    segundos para entregar o 1o audio; estourou, o Kokoro e disparado EM PARALELO e
    fala quem chegar primeiro (empate favorece o engine, que tem a voz boa). A fonte
    vencedora vira a fonte sticky da resposta — nao mistura voz no meio.

    Retorna (pcm, sr, infer_ms, source). Excecao do engine SEM fallback disponivel
    propaga para o caller tratar como antes.

    Cancel-safe: se ESTA corrotina for cancelada (cliente desconectou no meio do
    hedge), as tasks internas sao canceladas junto — senao a sintese continuaria
    rodando no engine sem ninguem para ouvir."""
    deadline = TTS_FIRST_AUDIO_HEDGE if deadline is None else deadline
    eng = asyncio.create_task(_engine_tts_pcm_retry(text, voice, language, instruct, backend))
    kok = None
    try:
        done, _ = await asyncio.wait({eng}, timeout=deadline)
        if eng in done or not _tts_fallback_available():
            pcm, sr, infer_ms = await eng  # rapido (ou sem hedge possivel); excecao propaga
            return pcm, sr, infer_ms, "engine"

        log.warning("TTS engine (%s) sem 1o audio em %.1fs — hedge: Kokoro em paralelo",
                    backend, deadline)
        kok = asyncio.create_task(_kokoro_chunk(text))
        await asyncio.wait({eng, kok}, return_when=asyncio.FIRST_COMPLETED)
        if eng.done():
            if eng.exception() is None:
                kok.cancel()
                pcm, sr, infer_ms = eng.result()
                return pcm, sr, infer_ms, "engine"
            # engine falhou de vez durante o hedge: o Kokoro (ja rodando) assume
            monitor.record_request(backend, False, eng.exception())
            log.warning("TTS engine (%s) falhou no hedge: %s — resposta via Kokoro",
                        backend, eng.exception())
            return await kok
        if kok.exception() is not None:  # Kokoro quebrou (raro): volta a esperar o engine
            pcm, sr, infer_ms = await eng
            return pcm, sr, infer_ms, "engine"
        # Kokoro chegou primeiro: fala ja; abandona a chamada lenta do engine (o monitor
        # nao e marcado down — lento nao e morto; o poller continua decidindo isso).
        eng.cancel()
        log.warning("TTS engine (%s) mais lento que o hedge+Kokoro — resposta via Kokoro", backend)
        return kok.result()
    except asyncio.CancelledError:
        eng.cancel()
        if kok is not None:
            kok.cancel()
        raise


async def engine_tts_pcm(text, voice, language, instruct, backend="qwen"):
    """TTS com fallback: tenta o engine do PC gamer; se offline, cai pro Kokoro local.
    Retorna (pcm_int16, sr, infer_ms, source) — source in {"engine","kokoro"}.

    Decisao pela tabela do monitor (_tts_engine_alive): se o backend esta DOWN, cai
    direto no Kokoro sem esperar timeout; se UP, sintetiza no engine. O resultado real
    da sintese realimenta a tabela (record_request), acelerando a proxima decisao."""
    global _tts_engine_down_until
    err = None
    if await _tts_engine_alive(backend):
        try:
            pcm, sr, infer_ms = await _engine_tts_pcm_remote(text, voice, language, instruct, backend)
            monitor.record_request(backend, True)
            _tts_engine_down_until = 0.0
            return pcm, sr, infer_ms, "engine"
        except Exception as e:
            err = e  # caiu DURANTE a sintese (raro: tabela up mas /tts falhou)
            monitor.record_request(backend, False, e)  # marca down na hora
    else:
        err = RuntimeError(f"engine '{backend}' marcado DOWN pelo monitor")
    if _tts_fallback_available():
        _tts_engine_down_until = time.time() + TTS_ENGINE_DOWN_COOLDOWN
        log.warning("TTS engine (%s) indisponivel: %s — usando Kokoro local", backend, err)
    else:
        why = "kokoro nao instalado" if not _kokoro_importable() else "fallback DESLIGADO no painel"
        log.warning("TTS engine (%s) falhou e %s: %s", backend, why, err)

    if not _tts_fallback_available():
        raise err if err is not None else RuntimeError("TTS engine indisponivel e fallback desligado")
    t0 = time.time()
    pcm, sr = await asyncio.to_thread(_kokoro_synth, text)
    log.info("TTS via Kokoro (FALLBACK) voice=%s lang=%s -> %d amostras",
             KOKORO_VOICE, KOKORO_LANG, len(pcm) // 2)
    return pcm, sr, round((time.time() - t0) * 1000), "kokoro"


@app.websocket("/ws/tts")
async def ws_tts(ws: WebSocket):
    await ws.accept()
    try:
        req = await ws.receive_json()
    except Exception:
        await ws.close()
        return
    text = (req.get("text") or "").strip()
    if not text:
        await ws.send_json({"type": "error", "error": "texto vazio"})
        await ws.close()
        return
    voice, language, instruct = req.get("voice"), req.get("language"), req.get("instruct")
    backend = req.get("backend") or "qwen"
    # normaliza ANTES de fatiar: numeros/simbolos por extenso + lexico de pronuncia
    # (Neo -> "níu") valem p/ o engine E p/ o Kokoro do fallback.
    text = normalize_tts_text(text, language)
    # Dia randomiza a voz por geracao -> nao fatiar (uma chamada, voz consistente)
    sentences = [text] if backend == "dia" else tokenize_sentences(text)
    await ws.send_json({"type": "start", "sr": TTS_START_SR.get(backend, TTS_START_SR["*"]),
                        "sentences": len(sentences)})
    t0 = time.time()

    # Hedge DESLIGADO no painel = "a voz escolhida a todo custo": o Kokoro nao entra
    # de jeito nenhum. Nem o monitor derruba a decisao aqui — mesmo marcado DOWN, o
    # engine e tentado (e re-tentado) ate responder. Ver _engine_tts_pcm_forever.
    at_all_costs = not _tts_hedge_enabled()

    # Fonte STICKY por resposta: decide engine vs. Kokoro UMA vez, no inicio do stream
    # lendo a tabela do monitor (O(1), sem probe na rede). Assim a fonte nao muda no
    # meio por um blip. attempts=2 so importa no cold start (backend ainda UNKNOWN na
    # tabela): evita um falso-negativo do probe ao vivo condenar a resposta inteira.
    global _tts_engine_down_until
    use_engine = True if at_all_costs else await _tts_engine_alive(backend, attempts=2)
    if at_all_costs:
        log.info("TTS [a todo custo] backend=%s — hedge desligado: sem Kokoro, insistindo no engine",
                 backend)
    elif use_engine:
        _tts_engine_down_until = 0.0
    elif _tts_fallback_available():
        _tts_engine_down_until = time.time() + TTS_ENGINE_DOWN_COOLDOWN
        log.warning("TTS engine (%s) indisponivel (monitor) — resposta inteira via Kokoro", backend)

    if not use_engine and not _tts_fallback_available():
        await ws.send_json({"type": "error", "error": "TTS engine indisponivel e fallback desligado"})
        await ws.close()
        return

    # Produtor/consumidor: o produtor sintetiza o PROXIMO trecho enquanto o atual
    # e enviado/tocado, dimensionando cada trecho pela margem de buffer real
    # (_RampState) — e o que elimina o gap audivel entre trechos. Serial de
    # proposito: 2 sinteses simultaneas no mesmo engine so enfileirariam no _lock
    # da GPU queimando o read-timeout. A fila maxsize=1 = no maximo 1 trecho
    # pronto aguardando envio; erro viaja pela fila como item (preserva a ordem).
    state = _RampState(backend if use_engine else "kokoro")
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    async def producer():
        nonlocal use_engine
        global _tts_engine_down_until
        pending = deque(sentences)
        idx = 0
        while pending:
            if idx == 0:
                # 1o trecho: 1 sentenca isolada (1o som rapido). budget=0 impede juntar
                # a proxima; o cap ainda corta uma 1a sentenca gigante.
                s = pack_next_chunk(pending, 0, state.cap_chars(), allow_cut=False)
            else:
                s = pack_next_chunk(pending, state.budget_chars(), state.cap_chars(),
                                    allow_cut=idx <= 3)
            t_synth = time.monotonic()
            if at_all_costs:
                # Nenhum caminho para o Kokoro: insiste no engine ate ele responder.
                pcm, sr, infer_ms, source = await _engine_tts_pcm_forever(
                    s, voice, language, instruct, backend)
            elif use_engine:
                try:
                    if idx == 0 and state.rtf_ema <= 1.0:
                        # hedge de 1o audio: engine lento -> Kokoro em paralelo, fala
                        # quem chegar primeiro; a fonte vencedora fica sticky. O prazo
                        # segue o tempo ESPERADO deste engine p/ este trecho, em vez
                        # de um valor fixo que castiga engine lento.
                        #
                        # Engine mais lento que tempo real (RTF>1, ex.: Qwen) NAO entra
                        # no hedge: o Kokoro venceria sempre a corrida e a voz escolhida
                        # pelo usuario nunca tocaria. Quem escolhe um engine desses ja
                        # aceitou esperar (o prebuffer segura a fala ate dar p/ tocar
                        # inteira); um engine de fato TRAVADO ainda cai no Kokoro pelo
                        # read timeout + o breaker do monitor. Mesma coisa quando o
                        # painel DESLIGA o hedge (`/tts/hedge`): voz escolhida a todo custo.
                        deadline = max(TTS_FIRST_AUDIO_HEDGE,
                                       TTS_HEDGE_SLACK * state.expected_synth_s(len(s)))
                        pcm, sr, infer_ms, source = await _first_chunk_hedged(
                            s, voice, language, instruct, backend, deadline=deadline)
                        if source != "engine":
                            use_engine = False
                    else:
                        pcm, sr, infer_ms = await _engine_tts_pcm_retry(
                            s, voice, language, instruct, backend)
                        source = "engine"
                    if source == "engine":
                        monitor.record_request(backend, True)  # sintese real OK -> tabela up
                except asyncio.CancelledError:
                    raise  # cliente desconectou: sai limpo, sem marcar o engine down
                except Exception as e:
                    # engine morreu DE VERDADE no meio (apos os retries): so aqui a fonte
                    # muda — degrada o resto da resposta pro Kokoro e arma o breaker.
                    monitor.record_request(backend, False, e)  # marca down na hora
                    if not _tts_fallback_available():
                        await queue.put(("error", e))
                        return
                    _tts_engine_down_until = time.time() + TTS_ENGINE_DOWN_COOLDOWN
                    log.warning("TTS engine (%s) caiu no meio da resposta (chunk %d): %s — resto via Kokoro",
                                backend, idx, e)
                    use_engine = False
                    pcm, sr, infer_ms, source = await _kokoro_chunk(s)
            else:
                pcm, sr, infer_ms, source = await _kokoro_chunk(s)
            if not pcm:
                # "a todo custo" desistiu deste trecho (ja logado). Segue a resposta
                # sem ele em vez de mandar um chunk mudo — e sem gastar `idx`, que o
                # cliente usa p/ saber qual e o 1o audio.
                log.error("TTS trecho %d nao sintetizado — seguindo sem ele: %r", idx, s[:80])
                continue
            state.observe(time.monotonic() - t_synth, len(pcm) / 2.0 / max(sr, 1), source, len(s))
            await queue.put(("chunk", idx, s, pcm, sr, infer_ms, source))
            idx += 1
        await queue.put(("done",))

    total_chars = sum(len(s) for s in sentences)

    async def consumer():
        # Engine mais lento que tempo real (RTF>1, ex.: Qwen ~1.9): o cliente consome
        # audio mais rapido do que o engine produz, entao mandar o 1o chunk assim que
        # ele fica pronto so garante que a fala vai travar a cada trecho. Seguramos os
        # primeiros chunks ate o colchao dar p/ tocar a resposta inteira sem buracos
        # (prebuffer_s). Engine rapido -> colchao 0, streaming imediato como antes.
        held: list = []
        held_s = 0.0
        streaming = False

        async def flush(items):
            for it in items:
                _, idx, s, pcm, sr, infer_ms, source = it
                if idx == 0:
                    if TTS_ONSET_PAD_MS > 0:
                        # colchao de zeros na frente do 1o audio: o que o agendamento do
                        # cliente comer (resume() do AudioContext, latencia do grafo) e
                        # silencio, nao o primeiro fonema.
                        pcm = b"\x00\x00" * int(sr * TTS_ONSET_PAD_MS / 1000.0) + pcm
                    log.info("TTS stream [%s] backend=%s 1o_chunk=%dms",
                             source, backend, round((time.time() - t0) * 1000))
                await ws.send_json({"type": "chunk", "idx": idx, "text": s, "sr": sr,
                                    "infer_ms": infer_ms, "source": source,
                                    "first_ms": round((time.time() - t0) * 1000) if idx == 0 else None})
                await ws.send_bytes(pcm)
                margin = state.note_delivery(len(pcm) / 2.0 / max(sr, 1))
                # margem negativa = underrun servidor-side (gap audivel provavel)
                log.info("TTS chunk %d [%s] chars=%d dur=%.2fs margem=%+.0fms rtf=%.2f",
                         idx, source, len(s), len(pcm) / 2.0 / max(sr, 1),
                         margin * 1000, state.rtf_ema)

        while True:
            item = await queue.get()
            if item[0] == "done":
                if held:
                    await flush(held)  # resposta acabou antes de encher o colchao
                await ws.send_json({"type": "done"})
                return
            if item[0] == "error":
                raise item[1]
            if streaming:
                await flush([item])
                continue

            held.append(item)
            held_s += len(item[3]) / 2.0 / max(item[4], 1)
            need = state.prebuffer_s(total_chars)
            if held_s >= need:
                if need > 0:
                    log.info("TTS prebuffer [%s] %.1fs de audio acumulado (alvo %.1fs, rtf=%.2f) "
                             "— tocando sem buracos", backend, held_s, need, state.rtf_ema)
                await flush(held)
                held, held_s, streaming = [], 0.0, True

    async def watch_disconnect():
        # clientes nao mandam nada depois do JSON inicial: receive() so retorna
        # quando o cliente some (ou manda algo inesperado, que ignoramos)
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return

    prod = asyncio.create_task(producer())
    cons = asyncio.create_task(consumer())
    watch = asyncio.create_task(watch_disconnect())
    try:
        done, _ = await asyncio.wait({cons, watch}, return_when=asyncio.FIRST_COMPLETED)
        if cons in done:
            cons.result()  # propaga erro da sintese -> {"type":"error"} abaixo
        else:
            log.info("TTS /ws/tts: cliente desconectou no meio — sintese cancelada")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "error": str(e)})
        except Exception:
            pass
    finally:
        # cancela os 3 sempre: solta o produtor preso no put() se o consumidor
        # morreu (fila cheia) e derruba a sintese em voo na desconexao.
        for t in (prod, cons, watch):
            t.cancel()
        await asyncio.gather(prod, cons, watch, return_exceptions=True)
        try:
            await ws.close()
        except Exception:
            pass


class Utterance:
    """Acumula audio de fala e decide quando fechar por silencio (VAD por frame)."""

    def __init__(self):
        self.buf = bytearray()               # PCM int16 da fala atual (+ pre-roll/cauda)
        self.tail = np.zeros(0, np.float32)  # resto < janela do VAD
        self.preroll = np.zeros(0, np.float32)  # audio rolante anterior ao onset
        self.speaking = False
        self.last_speech_t = 0.0
        self.speech_samples = 0

    def reset(self):
        self.buf = bytearray()
        self.preroll = np.zeros(0, np.float32)
        self.speaking = False
        self.speech_samples = 0
        try:
            _vad.reset_states()
        except Exception:
            pass

    @staticmethod
    def _to_pcm(frame: np.ndarray) -> bytes:
        return (np.clip(frame, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

    def add(self, pcm_bytes: bytes) -> bool:
        """Processa um chunk. Retorna True se a fala deve ser fechada agora."""
        f = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self.tail = np.concatenate([self.tail, f])
        n = len(self.tail)
        i = 0
        any_voice = False
        preroll_n = int(PREROLL_SEC * SAMPLE_RATE)
        while i + _vad_window <= n:
            frame = self.tail[i:i + _vad_window]
            prob = _vad_prob(frame)
            i += _vad_window
            if prob >= 0.5:
                if not self.speaking and preroll_n > 0 and len(self.preroll):
                    # onset: inclui o ataque da fala que o VAD perdeu
                    self.buf.extend(self._to_pcm(self.preroll[-preroll_n:]))
                self.preroll = np.zeros(0, np.float32)
                any_voice = True
                self.speaking = True
                self.last_speech_t = time.time()
                self.buf.extend(self._to_pcm(frame))
                self.speech_samples += _vad_window
            elif self.speaking:
                # guarda silencio curto dentro da fala (continuidade)
                self.buf.extend(self._to_pcm(frame))
            elif preroll_n > 0:
                # acumula audio anterior ao onset (rolante)
                self.preroll = np.concatenate([self.preroll, frame])[-preroll_n:]
        self.tail = self.tail[i:]

        if self.speaking:
            dur = len(self.buf) / 2 / SAMPLE_RATE
            silent_for = time.time() - self.last_speech_t
            if dur >= MAX_UTTER_SEC:
                return True
            if not any_voice and silent_for >= SILENCE_SEC:
                return True
        return False

    def final_pcm(self) -> bytes:
        """PCM completo da fala (com pre-roll do onset + a cauda de silencio acumulada).
        A cauda (~SILENCE_SEC) NAO e aparada de proposito: o transducer Parakeet usa
        esse silencio final para fechar o ultimo token (apara-la degrada a ultima palavra)."""
        return bytes(self.buf)

    def speech_seconds(self) -> float:
        return self.speech_samples / SAMPLE_RATE


def _vad_prob(frame: np.ndarray) -> float:
    """Probabilidade de fala do Silero-onnx para um frame de 512 amostras."""
    import torch
    with torch.no_grad():
        return float(_vad(torch.from_numpy(frame), SAMPLE_RATE))


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    log.info("cliente conectado /ws/audio")
    utt = Utterance()
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is None:
                # controle textual opcional
                if msg.get("text", "").strip().lower() == "flush":
                    await _finalize(ws, utt)
                continue
            should_close = utt.add(data)
            if should_close:
                await _finalize(ws, utt)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("erro ws: %s", e)
    finally:
        log.info("cliente desconectado")


async def _finalize(ws: WebSocket, utt: Utterance):
    if utt.speech_seconds() < MIN_SPEECH_SEC or len(utt.buf) == 0:
        utt.reset()
        return
    pcm = utt.final_pcm()
    utt.reset()
    try:
        res = await engine_transcribe_pcm(pcm)
        text = (res.get("text") or "").strip()
    except Exception as e:
        log.warning("engine falhou: %s", e)
        return
    if not text:
        return
    fix = normalize_stt_text(text)
    if fix.dropped:
        log.info("FINAL_TEXT descartado (%s): %r", fix.dropped, text)
        return
    text = fix.text
    log.info("FINAL_TEXT [%s]: %s%s (%.0fms, gpu=%s)",
             res.get("source", "?"), text,
             f"  [cru: {fix.raw}]" if fix.fixes else "",
             res.get("infer_ms", 0), res.get("on_gpu"))
    await ws.send_text(f"FINAL_TEXT:{text}")
    resp = await call_neo_core(text)
    if resp:
        await ws.send_text(f"FINAL_RESPONSE:{resp}")


# ---------------------------------------------------------------------------
# Painel web: pagina + WS com transcricao em tempo real (parciais + final)
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


async def _panel_final(ws: WebSocket, utt: "Utterance"):
    if utt.speech_seconds() < MIN_SPEECH_SEC or len(utt.buf) == 0:
        utt.reset()
        return
    pcm = utt.final_pcm()
    utt.reset()
    try:
        res = await engine_transcribe_pcm(pcm)
        text = (res.get("text") or "").strip()
    except Exception as e:
        log.warning("panel engine falhou: %s", e)
        return
    if not text:
        return
    # Corretor do ASR (stt_text): este ponto + o _finalize acima sao por onde
    # passam as 3 superficies de voz — chat do desktop, notch e painel web.
    # `text` vazio + `dropped` = eco do wake word: os clientes ja ignoram final
    # vazio, entao o descarte nao exige mudanca em nenhum deles.
    fix = normalize_stt_text(text)
    if fix.dropped:
        log.info("PANEL FINAL descartado (%s): %r", fix.dropped, text)
        await ws.send_text(json.dumps({
            "type": "final", "text": "", "raw_text": fix.raw, "dropped": fix.dropped,
        }))
        return
    text = fix.text
    log.info("PANEL FINAL [%s]: %s%s (%.0fms)", res.get("source", "?"), text,
             f"  [cru: {fix.raw}]" if fix.fixes else "", res.get("infer_ms", 0))
    await ws.send_text(json.dumps({
        "type": "final", "text": text, "raw_text": fix.raw, "fixes": fix.fixes,
        "asr_engine": res.get("source"),
        "infer_ms": res.get("infer_ms"), "audio_seconds": res.get("audio_seconds"),
        "on_gpu": res.get("on_gpu"),
    }))
    resp = await call_neo_core(text)
    if resp:
        await ws.send_text(json.dumps({"type": "response", "text": resp}))


@app.websocket("/ws/panel")
async def ws_panel(ws: WebSocket):
    await ws.accept()
    if API_KEY and ws.query_params.get("api_key") != API_KEY:
        await ws.close(code=4401)
        return
    log.info("painel conectado /ws/panel")
    utt = Utterance()
    last_partial = 0.0
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is None:
                cmd = (msg.get("text") or "").strip().lower()
                if cmd == "flush":
                    await _panel_final(ws, utt)
                    last_partial = 0.0
                continue
            should_close = utt.add(data)
            if should_close:
                await _panel_final(ws, utt)
                last_partial = 0.0
                continue
            # transcricao parcial enquanto fala (re-transcreve o buffer atual)
            now = time.time()
            if utt.speaking and utt.speech_seconds() >= 0.35 and (now - last_partial) >= PARTIAL_SEC:
                last_partial = now
                try:
                    res = await engine_transcribe_pcm(bytes(utt.buf), partial=True)
                    txt = (res.get("text") or "").strip()
                    if txt:
                        # parcial e legenda: corrige o nome (ver "Neil" piscando na
                        # tela e o mesmo bug), mas NUNCA descarta — a fala ainda
                        # esta em curso e o final decide.
                        txt = normalize_stt_text(txt, allow_drop=False).text
                        await ws.send_text(json.dumps({"type": "partial", "text": txt}))
                except Exception as e:
                    log.warning("partial falhou: %s", e)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("erro ws painel: %s", e)
    finally:
        log.info("painel desconectado")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
