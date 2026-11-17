# neo_wake — wake word local pela sua voz (few-shot)

Detector de palavra-de-despertar do Neo Desktop. Roda **no Mac**, na CPU,
**offline** — independe do PC gamer e da rede. É o **Estágio 1** do Modo Live:
ao ouvir o gatilho, o app acende o Modo Live (Estágio 2, `useLiveMode`).

Reconhecimento **few-shot pela sua voz** (EfficientWord-Net): você grava algumas
amostras da frase ("Ei Neo") na interface; cada frase vira uma referência de
embeddings (`<slug>_ref.json`) e, ao vivo, o mic é comparado por similaridade.
É *speaker-dependent* — sintoniza no seu timbre.

```
mic (16kHz, 1.5s) → embedding → cosine vs refs → {"event":"wake",...} no stdout
                  → src-tauri/src/services/wake.rs (supervisor)
                  → evento Tauri "wake-detected" → useWakeWord → toggleLive()
```

## Setup (uma vez)

```bash
cd sidecar/wake
./setup.sh          # brew portaudio + venv .venv-eff + EfficientWord-Net (--no-deps) + pins
```

> Por que `--no-deps`: o EfficientWord-Net pina dependências quebradas
> (`PyAudio==0.2.11` não compila no arm64; `numpy==1.22`) e ainda roda
> `pip install` no *import-time* para `tflite_runtime`/`librosa`. O `setup.sh`
> instala o pacote sem deps e fixa o conjunto que funciona (ver `requirements.txt`),
> deixando o import limpo e offline.

## Como usar

1. App → **Configurações → Palavra de despertar**.
2. Digite o rótulo (ex.: "Ei Neo"), clique no mic e fale a frase. Repita até
   ter **≥ 4 amostras** (requisito do EfficientWord-Net).
3. **Gerar referência** (enroll) → ativa o detector.
4. Ligue o toggle. Fale a frase → o Modo Live acende.

## Arquivos

- `neo_wake.py` — detector (sempre ligado): carrega `refs/*_ref.json`, escuta o
  mic em janelas de 1.5s (24000 amostras @16k) e emite `wake`.
- `neo_wake_enroll.py` — one-shot: gera `<slug>_ref.json` a partir dos `.wav`.
- `requirements.txt` / `setup.sh` — ambiente.

Armazenamento (gerenciado pelo Rust): `~/Library/Application Support/neo-desktop/wake/`
→ `manifest.json`, `samples/<slug>/NN.wav`, `refs/<slug>_ref.json`.

## Config por env (opcional; o Rust passa o que precisa)

```bash
NEO_WAKE_ENABLED=1          # autostart do detector ao abrir o app
NEO_WAKE_PYTHON=/abs/sidecar/wake/.venv-eff/bin/python
NEO_WAKE_SCRIPT=/abs/sidecar/wake/neo_wake.py
# NEO_WAKE_REFS_DIR=...      # default: ~/Library/Application Support/neo-desktop/wake/refs
# NEO_WAKE_THRESHOLD=0.7     # similaridade mínima 0..1
# NEO_WAKE_COOLDOWN_MS=3000
```

## Teste isolado (sem o app)

```bash
# precisa de uma frase já cadastrada (refs). Threshold/refs via env:
NEO_WAKE_REFS_DIR="$HOME/Library/Application Support/neo-desktop/wake/refs" \
  ./.venv-eff/bin/python neo_wake.py
```

## Precisão

Few-shot com poucas amostras é instantâneo e personalizado, porém mais sensível
a ruído/distância. Para ficar bom: grave **4–6 amostras** variando levemente o
tom, e ajuste `NEO_WAKE_THRESHOLD` (↑ menos falso-positivo, ↓ mais sensível).
