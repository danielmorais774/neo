#!/usr/bin/env python3
"""
neo_wake_oww -- detector de wake word local do Neo Desktop, versao openWakeWord.

Substitui o few-shot do EfficientWord-Net (neo_wake.py) por um modelo openWakeWord
TREINADO (ei_neo.onnx, na 5070 Ti: TTS + a voz real do usuario -- ver
neo-wake-openwakeword-training). Motivo: a separacao de score do few-shot era
ridicula (positivos reais 0.64-0.69 com limiar 0.64 = margem 0.004). O openWakeWord
separa muito melhor, entao a regra de disparo aqui e SIMPLES (limiar + N frames +
cooldown), sem o hack de "2 hits guardados" que compensava o detector antigo.

Contrato JSON com o supervisor Rust (services/wake.rs) e IDENTICO ao neo_wake.py --
mesmos eventos, mesmas envs -- entao o Rust nao muda; so aponta NEO_WAKE_SCRIPT aqui.

    {"event":"ready","models":[...],"threshold":0.5,"window":1280}
    {"event":"wake","model":"ei_neo","score":0.93}      # acende o Modo Live
    {"event":"interrupt","model":"pare","score":0.88}    # barge-in (EfficientWord-Net)
    {"event":"log","message":"..."}
    {"event":"error","message":"..."}

WAKE = openWakeWord (models/ei_neo.onnx). STOP ("pare") continua no EfficientWord-Net
(few-shot pelas refs cadastradas): so age durante a fala do Neo, recall > precisao, e
nao foi (ainda) migrado. Os dois frameworks coexistem no .venv-eff.

Envs (compat com neo_wake.py):
    NEO_WAKE_OWW_MODEL       caminho do .onnx (default: <script_dir>/models/ei_neo.onnx)
    NEO_WAKE_THRESHOLD       limiar do wake (default 0.5)
    NEO_WAKE_OWW_FRAMES      frames consecutivos >= limiar p/ disparar (default 1)
    NEO_WAKE_STOP_THRESHOLD  limiar do "pare" (default 0.64)
    NEO_WAKE_COOLDOWN_MS     silencio minimo entre wakes (default 3000)
    NEO_WAKE_STOP_COOLDOWN_MS  cooldown do "pare" (default 800)
    NEO_WAKE_STOP_SLUGS      slugs que sao "stop" (ex.: "pare,pause")
    NEO_WAKE_REFS_DIR        pasta dos *_ref.json (stop). Default: Application Support.
    NEO_WAKE_INPUT_DEVICE    indice/nome do mic (default: SO)
    NEO_WAKE_DEBUG           1 = loga scores perto do limiar
"""
import os
import sys
import json
import glob
import time
import signal
import threading

# Cap de threads ANTES de importar numpy/onnxruntime: cada inferencia em 1 thread.
# openWakeWord roda ~12 inferencias/s; sem cap, o onnxruntime espalha cada uma por
# todos os cores e satura o Mac (ja aconteceu no neo_wake.py -> 418% CPU).
for _thr_env in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS",
):
    os.environ.setdefault(_thr_env, "1")

SAMPLE_RATE = 16000
FRAME = 1280       # 80ms @16k -- o passo nativo do openWakeWord
STOP_WINDOW = 24000  # 1.5s -- janela do EfficientWord-Net (stop)
STOP_HOP = 6000      # 0.375s -- desliza a janela do stop
SILENCE_FLOOR = 0.01
# Piso p/ emitir o evento "score" (monitor de teste no app): so quando ha algo
# tipo-wake (>= isto) -- em silencio/fala comum o score fica ~0 e nao emite nada.
SCORE_EMIT_FLOOR = 0.25


def emit(obj: dict) -> None:
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        os._exit(0)


def log(msg) -> None:
    emit({"event": "log", "message": str(msg)})


def die(msg) -> None:
    emit({"event": "error", "message": str(msg)})
    sys.exit(1)


_tts_speaking = threading.Event()


def _stdin_reader() -> None:
    """Estado de fala do Neo, empurrado pelo Rust ({"tts": bool}). Enquanto fala,
    so o "pare" importa; ocioso, so o wake."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "tts" in msg:
            if msg.get("tts"):
                _tts_speaking.set()
            else:
                _tts_speaking.clear()


def _parent_death_watch() -> None:
    """Sai se o app pai morrer (ppid vira 1) -- evita orfao segurando o mic."""
    while True:
        time.sleep(2)
        if os.getppid() == 1:
            os._exit(0)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log(f"{name} invalido ({raw!r}); usando {default}")
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _default_refs_dir() -> str:
    return os.path.expanduser("~/Library/Application Support/neo-desktop/wake/refs")


def _default_model() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "ei_neo.onnx")


def main() -> None:
    model_path = os.environ.get("NEO_WAKE_OWW_MODEL", "").strip() or _default_model()
    threshold = _env_float("NEO_WAKE_THRESHOLD", 0.5)
    # default 1: o modelo v5 dá um pico AGUDO de 1 frame no "ei neo" (0.77-0.95) e
    # cai no frame seguinte; exigir 2 consecutivos perdia quase todo wake real. Como
    # o "ei" sozinho nunca passa de ~0.49 (< limiar), 1 frame >= limiar já separa.
    need_frames = max(1, _env_int("NEO_WAKE_OWW_FRAMES", 1))
    stop_threshold = _env_float("NEO_WAKE_STOP_THRESHOLD", 0.64)
    cooldown_s = _env_float("NEO_WAKE_COOLDOWN_MS", 3000.0) / 1000.0
    stop_cooldown_s = _env_float("NEO_WAKE_STOP_COOLDOWN_MS", 800.0) / 1000.0
    debug = os.environ.get("NEO_WAKE_DEBUG", "").strip() in ("1", "true", "yes", "on")
    refs_dir = os.environ.get("NEO_WAKE_REFS_DIR", "").strip() or _default_refs_dir()
    stop_slugs = {
        s.strip() for s in os.environ.get("NEO_WAKE_STOP_SLUGS", "").split(",") if s.strip()
    }
    input_device = os.environ.get("NEO_WAKE_INPUT_DEVICE", "").strip() or None
    if input_device is not None and input_device.isdigit():
        input_device = int(input_device)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if not os.path.exists(model_path):
        die(f"modelo openWakeWord nao encontrado: {model_path}")
        return

    try:
        import numpy as np
    except Exception as e:
        die(f"numpy ausente: {e}")
        return
    try:
        import sounddevice as sd
    except Exception as e:
        die(f"sounddevice/PortAudio ausente: {e}")
        return

    # Cap de 1 thread no onnxruntime (o wheel ignora OMP_NUM_THREADS; tem pool proprio).
    try:
        import onnxruntime as _ort
        _orig = _ort.InferenceSession

        def _single_thread(*a, **kw):
            so = kw.get("sess_options") or _ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            so.execution_mode = _ort.ExecutionMode.ORT_SEQUENTIAL
            kw["sess_options"] = so
            return _orig(*a, **kw)

        _ort.InferenceSession = _single_thread
    except Exception as e:
        log(f"cap de thread do onnxruntime nao aplicado: {e}")

    # --- WAKE: openWakeWord --------------------------------------------------
    try:
        from openwakeword.model import Model as OwwModel
    except Exception as e:
        die(f"openwakeword ausente/quebrado: {e}")
        return
    try:
        oww = OwwModel(wakeword_models=[model_path], inference_framework="onnx")
        wake_key = list(oww.models.keys())[0]
    except Exception as e:
        die(f"falha ao carregar o modelo openWakeWord: {e}")
        return

    # --- STOP: EfficientWord-Net (few-shot, opcional) ------------------------
    stop_detectors = []
    if stop_slugs:
        try:
            from eff_word_net.audio_processing import Resnet50_Arc_loss
            from eff_word_net.engine import HotwordDetector
            eff_model = Resnet50_Arc_loss()
            for ref in sorted(glob.glob(os.path.join(refs_dir, "*_ref.json"))):
                slug = os.path.basename(ref)[: -len("_ref.json")]
                if slug not in stop_slugs:
                    continue
                try:
                    det = HotwordDetector(
                        hotword=slug, model=eff_model, reference_file=ref,
                        threshold=stop_threshold, continuous=False, verbose=False,
                    )
                    stop_detectors.append((slug, det))
                except Exception as e:
                    log(f"stop '{slug}' ignorado: {e}")
        except Exception as e:
            log(f"EfficientWord-Net indisponivel; barge-in desligado: {e}")

    threading.Thread(target=_stdin_reader, daemon=True).start()
    threading.Thread(target=_parent_death_watch, daemon=True).start()

    emit({
        "event": "ready",
        "models": [wake_key] + [s for s, _ in stop_detectors],
        "threshold": threshold,
        "window": FRAME,
    })
    log(f"openWakeWord wake='{wake_key}' T={threshold} frames={need_frames}; "
        f"stop={[s for s, _ in stop_detectors] or 'off'}")

    stop_buf = np.zeros(STOP_WINDOW, dtype=np.float32)
    stop_fill = 0                 # amostras acumuladas desde o ultimo scoreFrame do stop
    last_fire_wake = 0.0
    last_fire_stop = 0.0
    consec = 0                    # frames consecutivos do wake >= threshold

    def stop_scores():
        out = []
        for slug, det in stop_detectors:
            try:
                res = det.scoreFrame(stop_buf, unsafe=True)
            except Exception:
                res = None
            if res:
                out.append((slug, float(res.get("confidence", 0.0))))
        return out

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=FRAME, device=input_device,
        ) as stream:
            while True:
                data, overflowed = stream.read(FRAME)
                if overflowed:
                    log("overflow de audio")
                frame_i16 = data[:, 0]
                frame_f = frame_i16.astype(np.float32) / 32768.0
                now = time.monotonic()
                speaking = _tts_speaking.is_set()

                if speaking:
                    # --- barge-in "pare": alimenta a janela do EfficientWord-Net ---
                    consec = 0
                    if stop_detectors:
                        stop_buf = np.concatenate((stop_buf[FRAME:], frame_f))
                        stop_fill += FRAME
                        if stop_fill >= STOP_HOP and now - last_fire_stop >= stop_cooldown_s:
                            stop_fill = 0
                            if np.max(np.abs(stop_buf)) >= SILENCE_FLOOR:
                                best_slug, best = None, 0.0
                                for slug, conf in stop_scores():
                                    if debug and conf >= stop_threshold - 0.08:
                                        log(f"stop '{slug}' score={conf:.3f}")
                                    if conf >= stop_threshold and conf > best:
                                        best_slug, best = slug, conf
                                if best_slug is not None:
                                    last_fire_stop = now
                                    emit({"event": "interrupt", "model": best_slug,
                                          "score": round(best, 4)})
                    continue

                # --- WAKE (ocioso) ---
                # openWakeWord (doc do predict) recomenda ALIMENTAR e PONTUAR todo
                # frame de 1280 (80ms) continuamente -- ele mesmo trata silencio
                # (score ~0). O gate de SILENCE_FLOOR anterior PULAVA o score de
                # frames baixos (continue) e fragmentava a deteccao: com mic mais
                # quieto dava "nem escuta". Agora scoreia sempre, como o upstream.
                score = oww.predict(frame_i16)[wake_key]
                if score >= SCORE_EMIT_FLOOR:
                    # monitor de teste no app (tela "Teste de Wake"): mostra o score
                    # de cada fala tipo-wake, mesmo abaixo do limiar (near-misses).
                    emit({"event": "score", "model": wake_key, "score": round(float(score), 4)})
                if debug and score >= threshold - 0.15:
                    log(f"wake score={score:.3f} consec={consec}")
                if score >= threshold:
                    consec += 1
                else:
                    consec = 0
                if consec >= need_frames and now - last_fire_wake >= cooldown_s:
                    last_fire_wake = now
                    consec = 0
                    emit({"event": "wake", "model": wake_key, "score": round(float(score), 4)})
    except KeyboardInterrupt:
        pass
    except Exception as e:
        die(f"erro no stream de audio: {e}")


if __name__ == "__main__":
    main()
