#!/usr/bin/env bash
# Setup do neo-voice TTS engine dentro do WSL2 Ubuntu (roda como root).
#   wsl -d Ubuntu -u root -- bash /mnt/c/Users/dmdan/setup_wsl_tts.sh
set -e
export DEBIAN_FRONTEND=noninteractive

echo "== apt =="
apt-get update -y -qq
apt-get install -y -qq python3-venv python3-pip >/dev/null

mkdir -p /opt/neo-tts
cd /opt/neo-tts
python3 -m venv .venv
PY=/opt/neo-tts/.venv/bin/python
$PY -m pip install -q --upgrade pip

echo "== torch cu128 (Linux/Blackwell) =="
$PY -m pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu128

echo "== qwen-tts + deps =="
$PY -m pip install --no-cache-dir qwen-tts transformers accelerate safetensors soundfile numpy \
    fastapi "uvicorn[standard]" huggingface_hub

echo "== versoes =="
$PY -c "import torch,sys; print('torch',torch.__version__,'cuda',torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
$PY -c "import qwen_tts; print('qwen_tts ok')"
echo WSL_TTS_SETUP_DONE
