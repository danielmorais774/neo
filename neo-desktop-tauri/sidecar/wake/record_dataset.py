#!/usr/bin/env python3
"""
record_dataset -- gravador guiado do dataset do wake word, clipe a clipe.

POR QUE ESTE SCRIPT (e nao a tela do painel): ele grava pelo MESMO caminho de
audio do detector -- `sounddevice` @16k int16 mono, PCM cru. O gravador do painel
usa MediaRecorder, que codifica em opus (perda) antes de virar WAV; treinar em
audio "amassado" e detectar em PCM cru cria um domain gap (bench bom, uso ruim).

Cada clipe sai num arquivo proprio, com rotulo (frase, distancia, jeito) em
`metadata.jsonl` -- entao o treino sabe exatamente o que e cada amostra e da p/
balancear positivos/negativos por categoria.

Uso:
    .venv-eff/bin/python record_dataset.py            # tudo (positivos + negativos)
    .venv-eff/bin/python record_dataset.py --kind pos # so positivos
    .venv-eff/bin/python record_dataset.py --kind neg # so negativos
    .venv-eff/bin/python record_dataset.py --extra 20 # + repeticoes livres

Teclas: ENTER grava | r regrava o ultimo | s pula | q sai (salva o que ja tem)
Saida: sidecar/wake/dataset/{positive,negative}/*.wav + metadata.jsonl
"""
import argparse
import json
import os
import queue
import sys
import time
import uuid
import wave

import numpy as np
import sounddevice as sd

SR = 16000
FRAME = 1280               # 80ms -- mesmo passo do detector
SILENCE_RMS = 0.006        # piso p/ considerar "tem fala"
LEAD_SIL = 0.35            # silencio minimo antes/depois no arquivo salvo
MAX_SECS = 4.0             # teto por clipe
TAIL_SIL = 0.7             # para de gravar apos este tanto de silencio pos-fala

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "dataset")

# ---------------------------------------------------------------- roteiro
# (frase, distancia, jeito). Distancia/jeito variam de proposito: o modelo
# precisa ver a MESMA palavra em condicoes diferentes.
DIST = {
    "perto": "PERTO ~20cm do mic",
    "normal": "BRACO ~60cm",
    "longe": "LONGE 2-3m (projete a voz)",
    "lado": "DE LADO / de costas p/ o mic",
}

POS = [
    ("Ei Neo", "perto", "tom normal"),
    ("Ei Neo", "perto", "mais forte"),
    ("Ei Neo", "perto", "baixo, quase sussurro"),
    ("Ei Neo", "perto", "rapido"),
    ("Ei Neo", "perto", "devagar, arrastando"),
    ("Ei Neo", "normal", "tom normal"),
    ("Ei Neo", "normal", "tom normal (de novo)"),
    ("Ei Neo", "normal", "mais forte"),
    ("Ei Neo", "normal", "mais baixo"),
    ("Ei Neo", "normal", "mais grave"),
    ("Ei Neo", "normal", "mais agudo"),
    ("Ei Neo", "normal", "rapido"),
    ("Ei Neo", "normal", "devagar"),
    ("Ei Neo", "normal", "meio anasalado / cansado"),
    ("Ei Neo", "longe", "projetando a voz"),
    ("Ei Neo", "longe", "tom normal"),
    ("Ei Neo", "longe", "mais forte"),
    ("Ei Neo", "longe", "rapido"),
    ("Ei Neo", "lado", "virado de lado"),
    ("Ei Neo", "lado", "de costas"),
    ("Ei Neo, que horas sao?", "perto", "tom normal"),
    ("Ei Neo, toca musica", "normal", "tom normal"),
    ("Ei Neo, liga a luz da sala", "normal", "tom normal"),
    ("Ei Neo, como esta o tempo?", "longe", "projetando"),
    ("Ei Neo, pausa", "perto", "rapido"),
    ("Ei Neo, aumenta o volume", "normal", "tom normal"),
]

NEG = [
    # partes da frase isoladas -- o coracao do problema
    ("Ei", "perto", "tom normal"),
    ("Ei", "perto", "mais forte"),
    ("Ei", "normal", "tom normal"),
    ("Ei", "normal", "arrastado: eeeei"),
    ("Ei", "longe", "projetando"),
    ("Neo", "perto", "tom normal"),
    ("Neo", "normal", "tom normal"),
    ("Neo", "normal", "mais forte"),
    ("Neo", "longe", "projetando"),
    ("Neo, neo, neo", "normal", "repetido"),
    # o que voce reclamou que dispara
    ("Alo", "perto", "tom normal"),
    ("Alo", "normal", "tom normal"),
    ("Alou", "normal", "tom normal"),
    ("Alo?", "longe", "chamando alguem"),
    ("Oi", "normal", "tom normal"),
    ("Oh", "normal", "tom normal"),
    # confusores foneticos
    ("Ne o", "normal", "tom normal"),
    ("Enio", "normal", "tom normal"),
    ("Meio", "normal", "tom normal"),
    ("Video", "normal", "tom normal"),
    ("Espontaneo", "normal", "tom normal"),
    ("Simultaneo", "normal", "tom normal"),
    ("E o resto?", "normal", "tom normal"),
    # fala comum
    ("Ei, vem ca", "normal", "tom normal"),
    ("Ei, olha isso", "perto", "tom normal"),
    ("Ei, espera ai", "normal", "tom normal"),
    ("Que horas sao?", "normal", "tom normal"),
    ("Liga a luz da sala", "normal", "tom normal"),
    ("Tudo bem com voce?", "longe", "tom normal"),
    ("Vamos ao mercado depois", "normal", "tom normal"),
    ("Nao sei se ele vem hoje", "normal", "tom normal"),
    # ambiente (sem fala) -- ensina o piso
    ("(silencio: nao fale, so o ruido do ambiente)", "normal", "ruido de fundo"),
    ("(digite/mexa em coisas, sem falar)", "perto", "ruido de teclado/objetos"),
]


def rms(x):
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def bar(v, width=28):
    n = int(min(1.0, v * 12) * width)
    return "#" * n + "-" * (width - n)


def record_clip(device=None):
    """Grava ate o fim da fala (ou MAX_SECS). Devolve float32 mono @16k."""
    q = queue.Queue()

    def cb(indata, frames, t, status):
        q.put(indata.copy())

    buf = []
    started = False
    last_voice = None
    t0 = time.time()
    with sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                        blocksize=FRAME, device=device, callback=cb):
        while True:
            try:
                block = q.get(timeout=0.5)
            except queue.Empty:
                if time.time() - t0 > MAX_SECS + 2:
                    break
                continue
            x = block[:, 0].astype(np.float32) / 32768.0
            buf.append(x)
            lvl = rms(x)
            now = time.time()
            if lvl > SILENCE_RMS:
                started = True
                last_voice = now
            sys.stdout.write(f"\r    [{bar(lvl)}] {'gravando' if started else 'aguardando fala'} ")
            sys.stdout.flush()
            if started and last_voice and (now - last_voice) > TAIL_SIL:
                break
            if now - t0 > MAX_SECS:
                break
    sys.stdout.write("\r" + " " * 60 + "\r")
    if not buf:
        return None
    a = np.concatenate(buf)
    # trim: mantem LEAD_SIL de folga antes/depois da fala
    env = np.array([rms(a[i:i + 320]) for i in range(0, max(1, len(a) - 320), 160)])
    voiced = np.where(env > SILENCE_RMS)[0]
    if len(voiced) == 0:
        return a  # ambiente (clipes de ruido)
    s = max(0, int(voiced[0] * 160 - LEAD_SIL * SR))
    e = min(len(a), int((voiced[-1] * 160 + 320) + LEAD_SIL * SR))
    return a[s:e]


def save(a, kind, text, dist, style):
    d = os.path.join(OUT, "positive" if kind == "pos" else "negative")
    os.makedirs(d, exist_ok=True)
    name = f"{kind}_{uuid.uuid4().hex[:12]}.wav"
    path = os.path.join(d, name)
    pcm = np.clip(a * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    meta = {
        "file": os.path.relpath(path, OUT), "kind": kind, "text": text,
        "distance": dist, "style": style, "seconds": round(len(a) / SR, 2),
        "rms": round(rms(a), 5), "sr": SR, "path_capture": "sounddevice-raw-pcm",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "metadata.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    return path, meta


def run(items, kind, device):
    i = 0
    done = 0
    while i < len(items):
        text, dist, style = items[i]
        head = "POSITIVO (deve acordar)" if kind == "pos" else "NEGATIVO (NAO pode acordar)"
        print(f"\n[{i+1}/{len(items)}] {head}")
        print(f"    FALE: \"{text}\"")
        print(f"    onde: {DIST[dist]}   |   como: {style}")
        cmd = input("    ENTER p/ gravar (s=pular, q=sair): ").strip().lower()
        if cmd == "q":
            return done
        if cmd == "s":
            i += 1
            continue
        a = record_clip(device)
        if a is None or len(a) < int(0.3 * SR):
            print("    ! nada capturado, repetindo")
            continue
        path, meta = save(a, kind, text, dist, style)
        print(f"    ok {meta['seconds']}s rms={meta['rms']:.4f} -> {os.path.basename(path)}")
        again = input("    ENTER p/ proximo (r=regravar este): ").strip().lower()
        if again == "r":
            os.remove(path)
            # remove a ultima linha do metadata
            mp = os.path.join(OUT, "metadata.jsonl")
            lines = open(mp, encoding="utf-8").read().splitlines()
            open(mp, "w", encoding="utf-8").write("\n".join(lines[:-1]) + ("\n" if lines[:-1] else ""))
            continue
        done += 1
        i += 1
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["pos", "neg", "all"], default="all")
    ap.add_argument("--extra", type=int, default=0,
                    help="repete o roteiro N vezes a mais (mais sessoes = mais variedade)")
    ap.add_argument("--device", default=os.environ.get("NEO_WAKE_INPUT_DEVICE") or None)
    args = ap.parse_args()

    dev = args.device
    if dev is not None and str(dev).isdigit():
        dev = int(dev)
    print("== Gravador do dataset do wake word (PCM cru, igual ao detector) ==")
    try:
        info = sd.query_devices(dev if dev is not None else sd.default.device[0])
        print(f"   mic: {info['name']}  @{SR}Hz mono int16")
    except Exception as e:
        print(f"   (nao consegui identificar o mic: {e})")
    print(f"   saida: {OUT}")
    print("   dica: faca em 2-3 sessoes diferentes (posicoes/horarios) -- variedade e o que faz o modelo generalizar.\n")

    total = 0
    rounds = 1 + max(0, args.extra)
    for r in range(rounds):
        if rounds > 1:
            print(f"\n===== RODADA {r+1}/{rounds} (mude de lugar/postura) =====")
        if args.kind in ("pos", "all"):
            total += run(POS, "pos", dev)
        if args.kind in ("neg", "all"):
            total += run(NEG, "neg", dev)
    print(f"\nFIM. {total} clipes salvos em {OUT}")
    print("Rotulos em metadata.jsonl. Me avise que eu levo pro treino.")


if __name__ == "__main__":
    main()
