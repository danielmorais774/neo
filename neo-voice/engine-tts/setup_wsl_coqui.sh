#!/usr/bin/env bash
# Setup do engine Coqui (XTTS-v2) no WSL2, venv Python 3.11 via uv (coqui-tts nao
# suporta o Python 3.14 da distro). Roda como root.
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq espeak-ng >/dev/null 2>&1 || true

if [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "== instalando uv =="
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$HOME/.local/bin/uv"

mkdir -p /opt/neo-coqui && cd /opt/neo-coqui
"$UV" venv --python 3.11 .venv
PY=/opt/neo-coqui/.venv/bin/python

echo "== torch cu128 (Blackwell) =="
"$UV" pip install --python "$PY" torch torchaudio --index-url https://download.pytorch.org/whl/cu128

echo "== coqui-tts + server deps =="
"$UV" pip install --python "$PY" coqui-tts fastapi "uvicorn[standard]" soundfile numpy

echo "== versoes =="
"$PY" -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
"$PY" -c "import TTS; print('coqui-tts', getattr(TTS,'__version__','?'))"
echo COQUI_SETUP_DONE
