#!/usr/bin/env python3
"""
neo_wake -- detector de wake word local (sempre ligado) do Neo Desktop.

Reconhecimento **few-shot pela sua voz** (EfficientWord-Net): voce grava algumas
amostras da frase ("Ei Neo") na interface; cada frase vira um arquivo de
referencia `<slug>_ref.json` (embeddings). Aqui, ao vivo, comparamos o microfone
com essas referencias por similaridade -- speaker-dependent por construcao.

Captura o microfone do Mac (16kHz mono) e roda na CPU, offline -- NAO depende do
PC gamer nem da rede. Ao bater acima do limiar, emite UMA linha JSON no stdout:

    {"event": "wake", "model": "<slug>", "score": 0.83}

Frases com papel "stop" (env NEO_WAKE_STOP_SLUGS, ex.: "pare") nao acendem o Live:
elas INTERROMPEM a fala/o turno em andamento. Para essas, o sidecar emite:

    {"event": "interrupt", "model": "<slug>", "score": 0.83}

Diferencas do detector "stop": cooldown proprio e curto (responsivo + repetivel),
threshold proprio (recall > precisao: um FP so age se o Neo estiver falando/pensando)
e scoring com `unsafe=True` -- durante o TTS contInuo a janela do mic nunca "comeca
em silencio", entao o guard padrao do EfficientWord-Net descartaria o "pare".

Regra de disparo do WAKE (anti-falso-positivo, calibrada com as gravacoes reais
do usuario -- ver docs no commit):
  * hop de 0.375s (o alinhamento frase-vs-janela muda o score em ate ~0.15;
    hop denso garante uma janela quase alinhada);
  * 2 hits do MESMO slug em janelas proximas (<= ~1.1s), sendo que o 1o hit
    exige a janela "comecando em silencio" (guard) -- fala continua (TV, TTS,
    conversa) raramente produz 2 hits guardados do mesmo slug;
  * escape de 1 hit unico muito forte (threshold + margem) p/ nao perder
    disparos obvios;
  * detectores com continuous=False: o relaxation_time interno da lib zera o
    score de hits consecutivos (<0.8s) e mataria a regra de 2 hits.

Outros eventos (stdout, uma linha JSON cada):
    {"event": "ready", "models": [...], "threshold": 0.64, "window": 24000}
    {"event": "error", "message": "..."}     # falha fatal -> sai com !=0
    {"event": "log",   "message": "..."}      # diagnostico

O supervisor Rust (src-tauri/src/services/wake.rs) le isso e converte o `wake`
no evento Tauri `wake-detected` -> o frontend acende o Modo Live (useLiveMode).
O Rust tambem descarta `wake` enquanto o proprio Neo esta falando (gate anti-eco).

Config por env:
    NEO_WAKE_REFS_DIR        pasta com os `*_ref.json` (Rust passa o caminho absoluto).
    NEO_WAKE_THRESHOLD       limiar de similaridade 0..1 (default 0.64).
    NEO_WAKE_STOP_THRESHOLD  limiar das frases "stop" (default 0.64).
    NEO_WAKE_COOLDOWN_MS     silencio minimo entre dois disparos (default 3000).
    NEO_WAKE_INPUT_DEVICE    indice/nome do dispositivo de entrada (default: SO).
    NEO_WAKE_DEBUG           1 = loga scores proximos do limiar (tuning).

Janela do EfficientWord-Net = 1.5s = 24000 amostras @16k (requisito do modelo).
"""
import os
import sys
import json
import glob
import time
import signal
import threading

# Cap de threads: cada inferencia em 1 thread. Sem isso, numpy/numba/scipy/librosa
# (via estas env, lidas no import) E o onnxruntime (via monkeypatch em main) espalham
# cada uma das ~16 inferencias/s por TODOS os cores -> ~5 cores saturados no Mac. O
# modelo (Resnet50 pequeno) roda igual de rapido single-thread. setdefault: override
# explicito ainda vence. DEVE vir antes de importar numpy/onnxruntime/numba.
for _thr_env in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS",
):
    os.environ.setdefault(_thr_env, "1")

SAMPLE_RATE = 16000
WINDOW = 24000   # 1.5s @16k -- exigido pelo audioToVector do EfficientWord-Net
HOP = 6000       # desliza 0.375s a cada passo (denso: alinhamento frase-janela)
SILENCE_FLOOR = 0.01  # pula a inferencia se a janela for ~silencio (economiza CPU)
GUARD_HEAD = 1600     # 100ms iniciais: janela "comeca em silencio" (guard do wake)
CONFIRM_WINDOW_S = 1.2   # 2o hit do wake deve chegar ate aqui apos o 1o
STRONG_MARGIN = 0.12     # hit unico >= threshold+margem dispara direto (com guard)


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


# Estado de fala do Neo, empurrado pelo supervisor Rust via stdin ({"tts": bool}).
# Enquanto o Neo fala, so os detectores de "pare" (barge-in) importam; ocioso, so
# os de wake. Rodar apenas o grupo relevante corta ~metade das inferencias.
_tts_speaking = threading.Event()


def _stdin_reader() -> None:
    """Le comandos de controle do Rust, uma linha JSON por vez. Hoje so
    `{"tts": true|false}`. Roda numa thread daemon: nao bloqueia o loop de audio.
    Sem alimentador (execucao avulsa), fica ocioso e o loop roda so o wake."""
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
    """Sai se o app pai (Tauri) morrer -- no macOS o pai vira 1 (reparent p/
    launchd). Sem isso, um `tauri dev` morto por SIGKILL (que NAO roda o
    kill_on_drop do Rust) deixa o sidecar orfao segurando o mic e queimando CPU
    (ja aconteceu: 2 orfaos de 9-11 dias). Poll barato de 2s."""
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


def _default_refs_dir() -> str:
    return os.path.expanduser(
        "~/Library/Application Support/neo-desktop/wake/refs"
    )


def main() -> None:
    refs_dir = os.environ.get("NEO_WAKE_REFS_DIR", "").strip() or _default_refs_dir()
    threshold = _env_float("NEO_WAKE_THRESHOLD", 0.64)
    stop_threshold = _env_float("NEO_WAKE_STOP_THRESHOLD", 0.64)
    debug = os.environ.get("NEO_WAKE_DEBUG", "").strip() in ("1", "true", "yes", "on")
    cooldown_s = _env_float("NEO_WAKE_COOLDOWN_MS", 3000.0) / 1000.0
    stop_cooldown_s = _env_float("NEO_WAKE_STOP_COOLDOWN_MS", 800.0) / 1000.0
    stop_slugs = {
        s.strip()
        for s in os.environ.get("NEO_WAKE_STOP_SLUGS", "").split(",")
        if s.strip()
    }
    # Indice do PortAudio, ou o NOME do dispositivo (o Rust manda o nome que o
    # CoreAudio reporta -- "MacBook Pro Microphone" -- e o PortAudio casa por
    # substring). Nome que nao casa levanta na abertura do stream, e o supervisor
    # do Rust reinicia: melhor falhar visivel do que cair calado no mic errado.
    input_device = os.environ.get("NEO_WAKE_INPUT_DEVICE", "").strip() or None
    if input_device is not None and input_device.isdigit():
        input_device = int(input_device)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        import numpy as np
    except Exception as e:
        die(f"numpy ausente: {e}. Rode o setup.sh do sidecar.")
        return
    try:
        import sounddevice as sd
    except Exception as e:
        die(
            "sounddevice ausente ou PortAudio nao instalado: "
            f"{e}. `brew install portaudio` e rode o setup.sh."
        )
        return
    # onnxruntime (backend do EfficientWord-Net) usa por padrao TODOS os cores por
    # inferencia (intra_op=0) -- e o wheel nao respeita OMP_NUM_THREADS (thread pool
    # proprio). Forca 1 thread interceptando a criacao da sessao ANTES de instanciar
    # o modelo: o Resnet50 e pequeno, roda igual de rapido e para de saturar a CPU.
    try:
        import onnxruntime as _ort
        _orig_ort_session = _ort.InferenceSession

        def _single_thread_session(*a, **kw):
            so = kw.get("sess_options") or _ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            so.execution_mode = _ort.ExecutionMode.ORT_SEQUENTIAL
            kw["sess_options"] = so
            return _orig_ort_session(*a, **kw)

        _ort.InferenceSession = _single_thread_session
    except Exception as e:
        log(f"cap de thread do onnxruntime nao aplicado: {e}")

    try:
        from eff_word_net.audio_processing import Resnet50_Arc_loss
        from eff_word_net.engine import HotwordDetector
    except Exception as e:
        die(f"EfficientWord-Net ausente/quebrado: {e}. Rode o setup.sh do sidecar.")
        return

    model = Resnet50_Arc_loss()

    # Carrega uma referencia por frase cadastrada. O modelo e compartilhado.
    # continuous=False: o relaxation_time interno da lib devolveria 0.001 p/
    # hits consecutivos em <0.8s -- exatamente o que a regra de 2 hits precisa
    # ver. Cooldown/dedup e todo nosso, aqui fora.
    detectors = []
    skipped = []
    for ref in sorted(glob.glob(os.path.join(refs_dir, "*_ref.json"))):
        slug = os.path.basename(ref)[: -len("_ref.json")]
        try:
            det = HotwordDetector(
                hotword=slug,
                model=model,
                reference_file=ref,
                threshold=stop_threshold if slug in stop_slugs else threshold,
                continuous=False,
                verbose=False,
            )
            detectors.append((slug, det))
        except AssertionError as e:
            # ex.: < 4 amostras, ou model_type incompativel
            skipped.append(slug)
            log(f"frase '{slug}' ignorada: {e}")
        except Exception as e:
            skipped.append(slug)
            log(f"frase '{slug}' falhou ao carregar: {e}")

    if not detectors:
        emit({"event": "ready", "models": [], "threshold": threshold, "window": WINDOW})
        log(
            f"nenhuma frase cadastrada em {refs_dir} (cadastre >=4 amostras na UI). "
            "Mic NAO sera aberto ate haver uma frase."
        )
        # Fica vivo (sem abrir o mic) ate o SIGTERM do supervisor.
        while True:
            time.sleep(3600)

    # Frases "stop" (ex.: "pare") interrompem; as demais acendem o Live. Cada
    # grupo tem cooldown proprio e o stop e scoreado com unsafe=True (barge-in).
    wake_detectors = [(s, d) for s, d in detectors if s not in stop_slugs]
    stop_detectors = [(s, d) for s, d in detectors if s in stop_slugs]

    emit({
        "event": "ready",
        "models": [s for s, _ in detectors],
        "stop_models": [s for s, _ in stop_detectors],
        "threshold": threshold,
        "stop_threshold": stop_threshold,
        "window": WINDOW,
        "hop": HOP,
        "skipped": skipped,
    })

    # Escuta o estado de TTS do supervisor (thread daemon, nao bloqueia o audio).
    threading.Thread(target=_stdin_reader, name="wake-stdin", daemon=True).start()
    # Auto-termina se o app pai morrer (evita orfaos apos hard-kill do tauri dev).
    threading.Thread(target=_parent_death_watch, name="wake-ppid", daemon=True).start()

    def scores(dets):
        """[(slug, confianca)] do buffer atual, sempre via unsafe=True: o guard
        de silencio da lib e binario e mataria o 2o hit do wake (a frase ja
        esta dentro da janela quando ela desliza). O guard e aplicado por fora,
        so onde a regra pede (1o hit / hit forte)."""
        out = []
        for slug, det in dets:
            res = det.scoreFrame(buf, unsafe=True)
            if res:
                out.append((slug, float(res.get("confidence", 0.0))))
        return out

    def guard_ok():
        """True se a janela 'comeca em silencio' (mesma heuristica da lib):
        os primeiros 100ms tem pico <= 20% do pico da janela."""
        peak = float(np.max(np.abs(buf)))
        if peak <= 0.0:
            return False
        return float(np.max(np.abs(buf[:GUARD_HEAD]))) / peak <= 0.2

    buf = np.zeros(WINDOW, dtype=np.float32)
    last_fire_wake = 0.0
    last_fire_stop = 0.0
    # 1o hit guardado de cada slug de wake, aguardando confirmacao: slug -> ts.
    pending_wake = {}

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=HOP,
            device=input_device,
        ) as stream:
            while True:
                data, overflowed = stream.read(HOP)
                if overflowed:
                    log("overflow de audio (perdeu amostras)")
                chunk = data[:, 0].astype(np.float32) / 32768.0
                buf = np.concatenate((buf[HOP:], chunk))

                # Gate de energia: nao roda o modelo em silencio.
                if np.max(np.abs(buf)) < SILENCE_FLOOR:
                    continue

                now = time.monotonic()
                # Enquanto o Neo fala: so os detectores de "pare" (o Rust descarta
                # o wake por anti-eco). Ocioso: so os de wake (nao ha nada p/
                # interromper). Rodar so o grupo relevante corta ~metade da CPU sem
                # mudar comportamento -- e mata o "perdeu amostras" (overflow).
                speaking = _tts_speaking.is_set()
                if speaking:
                    pending_wake.clear()

                # Interrupcao ("pare"): 1 hit, cooldown curto, sem guard --
                # durante o TTS a janela nunca comeca em silencio (barge-in).
                if stop_detectors and speaking and now - last_fire_stop >= stop_cooldown_s:
                    best_slug, best_conf = None, 0.0
                    for slug, conf in scores(stop_detectors):
                        if debug and conf >= stop_threshold - 0.08:
                            log(f"stop '{slug}' score={conf:.3f} (T={stop_threshold})")
                        if conf >= stop_threshold and conf > best_conf:
                            best_slug, best_conf = slug, conf
                    if best_slug is not None:
                        last_fire_stop = now
                        pending_wake.clear()
                        emit({"event": "interrupt", "model": best_slug, "score": round(best_conf, 4)})
                        continue

                # Wake ("Ei Neo"): 2 hits do mesmo slug (1o com guard) ou 1 hit
                # forte (threshold+margem, com guard). Cooldown padrao. So quando
                # ocioso -- na fala do Neo o wake e descartado de qualquer jeito.
                if wake_detectors and not speaking and now - last_fire_wake >= cooldown_s:
                    guarded = guard_ok()
                    fired = None
                    for slug, conf in scores(wake_detectors):
                        if debug and conf >= threshold - 0.08:
                            log(
                                f"wake '{slug}' score={conf:.3f} guard={guarded} "
                                f"pending={slug in pending_wake}"
                            )
                        if conf < threshold:
                            continue
                        if guarded and conf >= threshold + STRONG_MARGIN:
                            fired = (slug, conf)  # hit unico forte
                            break
                        t0 = pending_wake.get(slug)
                        if t0 is not None and now - t0 <= CONFIRM_WINDOW_S:
                            fired = (slug, conf)  # confirmacao do 2o hit
                            break
                        if guarded:
                            pending_wake[slug] = now  # vira candidato a 1o hit
                    # expira candidatos velhos
                    for slug in [s for s, t in pending_wake.items() if now - t > CONFIRM_WINDOW_S]:
                        del pending_wake[slug]
                    if fired is not None:
                        last_fire_wake = now
                        pending_wake.clear()
                        emit({"event": "wake", "model": fired[0], "score": round(fired[1], 4)})
    except KeyboardInterrupt:
        pass
    except Exception as e:
        die(f"erro no stream de audio: {e}")


if __name__ == "__main__":
    main()
