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

Diferencas do detector "stop": cooldown proprio e curto (responsivo + repetivel) e
scoring com `unsafe=True` -- durante o TTS contInuo a janela do mic nunca "comeca em
silencio", entao o guard padrao do EfficientWord-Net descartaria o "pare"; o
unsafe=True ignora esse guard (barge-in).

Outros eventos (stdout, uma linha JSON cada):
    {"event": "ready", "models": [...], "threshold": 0.7, "window": 24000}
    {"event": "error", "message": "..."}     # falha fatal -> sai com !=0
    {"event": "log",   "message": "..."}      # diagnostico

O supervisor Rust (src-tauri/src/services/wake.rs) le isso e converte o `wake`
no evento Tauri `wake-detected` -> o frontend acende o Modo Live (useLiveMode).

Config por env:
    NEO_WAKE_REFS_DIR     pasta com os `*_ref.json` (Rust passa o caminho absoluto).
    NEO_WAKE_THRESHOLD    limiar de similaridade 0..1 (default 0.7).
    NEO_WAKE_COOLDOWN_MS  silencio minimo entre dois disparos (default 3000).
    NEO_WAKE_INPUT_DEVICE indice/nome do dispositivo de entrada (default: SO).

Janela do EfficientWord-Net = 1.5s = 24000 amostras @16k (requisito do modelo).
"""
import os
import sys
import json
import glob
import time
import signal

SAMPLE_RATE = 16000
WINDOW = 24000   # 1.5s @16k -- exigido pelo audioToVector do EfficientWord-Net
HOP = 12000      # desliza 0.75s a cada passo (janela deslizante)
SILENCE_FLOOR = 0.01  # pula a inferencia se a janela for ~silencio (economiza CPU)


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
    threshold = _env_float("NEO_WAKE_THRESHOLD", 0.7)
    cooldown_s = _env_float("NEO_WAKE_COOLDOWN_MS", 3000.0) / 1000.0
    stop_cooldown_s = _env_float("NEO_WAKE_STOP_COOLDOWN_MS", 800.0) / 1000.0
    stop_slugs = {
        s.strip()
        for s in os.environ.get("NEO_WAKE_STOP_SLUGS", "").split(",")
        if s.strip()
    }
    input_device = os.environ.get("NEO_WAKE_INPUT_DEVICE", "").strip() or None

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
    try:
        from eff_word_net.audio_processing import Resnet50_Arc_loss
        from eff_word_net.engine import HotwordDetector
    except Exception as e:
        die(f"EfficientWord-Net ausente/quebrado: {e}. Rode o setup.sh do sidecar.")
        return

    model = Resnet50_Arc_loss()

    # Carrega uma referencia por frase cadastrada. O modelo e compartilhado.
    detectors = []
    skipped = []
    for ref in sorted(glob.glob(os.path.join(refs_dir, "*_ref.json"))):
        slug = os.path.basename(ref)[: -len("_ref.json")]
        try:
            det = HotwordDetector(
                hotword=slug,
                model=model,
                reference_file=ref,
                threshold=threshold,
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
        "window": WINDOW,
        "skipped": skipped,
    })

    def best_match(dets, unsafe: bool):
        """Slug + confianca da melhor frase casada no buffer atual (ou None)."""
        best_slug, best_conf = None, 0.0
        for slug, det in dets:
            res = det.scoreFrame(buf, unsafe=unsafe)
            if res and res.get("match") and res.get("confidence", 0.0) > best_conf:
                best_slug, best_conf = slug, float(res["confidence"])
        return best_slug, best_conf

    buf = np.zeros(WINDOW, dtype=np.float32)
    last_fire_wake = 0.0
    last_fire_stop = 0.0

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

                # Gate de energia: nao roda o modelo em silencio (economia + evita
                # div/0 no guard interno do scoreFrame).
                if np.max(np.abs(buf)) < SILENCE_FLOOR:
                    continue

                now = time.monotonic()

                # Interrupcao ("pare"): cooldown curto + unsafe=True (barge-in
                # durante o TTS, quando a janela nunca comeca em silencio).
                if stop_detectors and now - last_fire_stop >= stop_cooldown_s:
                    slug, conf = best_match(stop_detectors, unsafe=True)
                    if slug is not None:
                        last_fire_stop = now
                        emit({"event": "interrupt", "model": slug, "score": round(conf, 4)})
                        continue

                # Wake ("Ei Neo"): cooldown padrao + guard de silencio (unsafe=False).
                if wake_detectors and now - last_fire_wake >= cooldown_s:
                    slug, conf = best_match(wake_detectors, unsafe=False)
                    if slug is not None:
                        last_fire_wake = now
                        emit({"event": "wake", "model": slug, "score": round(conf, 4)})
    except KeyboardInterrupt:
        pass
    except Exception as e:
        die(f"erro no stream de audio: {e}")


if __name__ == "__main__":
    main()
