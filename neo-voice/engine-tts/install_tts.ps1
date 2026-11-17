# Instala a stack do Qwen3-TTS no PC gamer (Windows + RTX 5070 Ti / Blackwell).
# torch cu128 = kernels Blackwell sm_120. --no-cache-dir poupa disco (10.9GB livres).
# flash-attn NAO e instalado (nao compila no Windows); usamos attn sdpa.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"

Write-Host "== [1/2] torch cu128 (Blackwell) =="
& $py -m pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu128

Write-Host "== [2/2] qwen-tts + transformers + server deps =="
& $py -m pip install --no-cache-dir qwen-tts transformers accelerate safetensors soundfile numpy `
    fastapi "uvicorn[standard]" huggingface_hub

Write-Host "INSTALL_DONE"
