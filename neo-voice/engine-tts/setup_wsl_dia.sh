#!/usr/bin/env bash
# Setup do engine Nari Dia (1.6B) no WSL2, venv Python 3.11 via uv. Roda como root.
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq git ffmpeg >/dev/null 2>&1 || true
[ -x "$HOME/.local/bin/uv" ] || curl -LsSf https://astral.sh/uv/install.sh | sh
UV="$HOME/.local/bin/uv"

mkdir -p /opt/neo-dia && cd /opt/neo-dia
"$UV" venv --python 3.11 .venv
PY=/opt/neo-dia/.venv/bin/python

echo "== torch cu128 (Blackwell) =="
"$UV" pip install --python "$PY" torch torchaudio --index-url https://download.pytorch.org/whl/cu128

echo "== dia (nari-labs) + server deps =="
"$UV" pip install --python "$PY" "git+https://github.com/nari-labs/dia.git" fastapi "uvicorn[standard]" soundfile numpy

echo "== versoes =="
"$PY" -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
"$PY" -c "from dia.model import Dia; print('dia import OK')"
echo DIA_SETUP_DONE
