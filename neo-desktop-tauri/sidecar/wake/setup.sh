#!/usr/bin/env bash
# Setup do venv do sidecar de wake word (EfficientWord-Net) no Mac.
# Idempotente: pode rodar de novo. Ver README.md.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew nao encontrado. Instale o brew ou o PortAudio manualmente." >&2
fi
brew list portaudio >/dev/null 2>&1 || brew install portaudio

PY="${PYTHON:-python3}"
if [ ! -d .venv-eff ]; then
  "$PY" -m venv .venv-eff
fi
./.venv-eff/bin/python -m pip install --upgrade pip

# EfficientWord-Net tem pins quebrados (PyAudio==0.2.11 nao compila no arm64,
# numpy==1.22) -> instalamos sem deps e fixamos o conjunto que funciona.
./.venv-eff/bin/python -m pip install --no-deps EfficientWord-Net==1.0.2
./.venv-eff/bin/python -m pip install -r requirements.txt

echo
echo "OK. Teste rapido (sem o app):"
echo "  ./.venv-eff/bin/python neo_wake.py   # precisa de frases cadastradas (refs)"
